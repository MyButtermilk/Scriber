"""Read-only, fail-closed preparation for the ML GitHub handoff.

The module deliberately does not stage, commit, push, or call GitHub.  It
captures the exact Git index/worktree change set, applies a narrow allow-list,
scans both index and worktree bytes for credentials, and proposes logical
commits.  A draft pull-request body can only contain values extracted from
JSON evidence whose exact SHA-256 was declared by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

from jsonschema import Draft202012Validator


class GitHandoffError(ValueError):
    """Raised when a Git handoff cannot be inventoried safely."""


CONTRACTS_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "contracts"
DEFAULT_SCOPE: Final = "ml/scriber_polishing"
MAX_TEXT_FILE_BYTES: Final = 2 * 1024 * 1024
MAX_SAMPLE_FILE_BYTES: Final = 512 * 1024
MAX_TOTAL_INCLUDED_BYTES: Final = 25 * 1024 * 1024
MAX_CREDENTIAL_SCAN_BYTES: Final = 10 * 1024 * 1024

PR_SECTIONS: Final = (
    ("objective", "Ziel"),
    ("data_pipeline", "Datenpipeline"),
    ("agent_roles", "Agentenrollen"),
    ("training", "Training"),
    ("metrics", "Metriken"),
    ("risks", "Risiken"),
    ("hf_artifacts", "Hugging-Face-Artefakte"),
    ("final_status", "Finaler Status"),
)

_FORBIDDEN_SUFFIXES: Final = frozenset(
    {
        ".7z",
        ".bin",
        ".ckpt",
        ".gguf",
        ".gz",
        ".model",
        ".npz",
        ".onnx",
        ".ot",
        ".parquet",
        ".pkl",
        ".pt",
        ".pth",
        ".safetensors",
        ".tar",
        ".zip",
    }
)
_FORBIDDEN_SEGMENTS: Final = frozenset(
    {
        ".cache",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "agent_reviews",
        "ai_gold",
        "cache",
        "challenge",
        "checkpoints",
        "generated",
        "hf-cache",
        "runs",
        "tmp",
        "validation",
        "venv",
    }
)
_FORBIDDEN_DATA_FILENAMES: Final = frozenset(
    {
        "challenge.jsonl",
        "plans.jsonl",
        "targets.jsonl",
        "test.jsonl",
        "train.jsonl",
        "validation.jsonl",
    }
)
_SECRET_NAME_RE: Final = re.compile(
    r"(?:^|[._-])(?:credential|credentials|id_rsa|private[_-]?key|secret|secrets)(?:[._-]|$)",
    re.IGNORECASE,
)

# Build high-confidence token patterns from separated literals so auditing this
# scanner does not make its own source look like it contains a credential.
_CREDENTIAL_PATTERNS: Final = (
    (
        "private_key",
        re.compile(
            (
                "-----BEGIN "
                + r"(?:RSA |EC |OPENSSH |DSA )?"
                + "PRIVATE "
                + "KEY-----"
            ).encode("ascii")
        ),
    ),
    (
        "hugging_face_token",
        re.compile(("h" + "f_" + r"[A-Za-z0-9]{24,}").encode("ascii")),
    ),
    (
        "github_token",
        re.compile(
            (
                r"(?:"
                + "github"
                + r"_pat_[A-Za-z0-9_]{20,}|g"
                + r"h[pousr]_[A-Za-z0-9]{20,})"
            ).encode("ascii")
        ),
    ),
    (
        "openai_token",
        re.compile(("s" + "k-" + r"[A-Za-z0-9_-]{20,}").encode("ascii")),
    ),
    (
        "aws_access_key",
        re.compile(("A" + "KIA" + r"[A-Z0-9]{16}").encode("ascii")),
    ),
    (
        "jwt",
        re.compile(
            (
                "e"
                + "yJ"
                + r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            ).encode("ascii")
        ),
    ),
)
_QUOTED_SECRET_ASSIGNMENT_RE: Final = re.compile(
    (
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
        r"private[_-]?key|session[_-]?token|client[_-]?secret)\b"
        r"\s*[:=]\s*[\"']([^\"'\r\n]{12,})[\"']"
    ).encode("ascii")
)
_PLACEHOLDER_TERMS: Final = (
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "redacted",
    "replace",
    "test-only",
    "your_",
    "${",
    "<",
)

_COMMIT_ORDER: Final = (
    (
        "data_factory",
        "feat(ml): add AI-only transcript data factory",
    ),
    (
        "semantic_sst",
        "feat(ml): add semantic plans and SST document AST",
    ),
    (
        "normalization",
        "feat(ml): add German legal and unit normalization",
    ),
    (
        "critics",
        "feat(ml): add autonomous critic ensemble",
    ),
    (
        "training",
        "feat(ml): add Gemma 3 270M training pipeline",
    ),
    (
        "tests",
        "test(ml): add critical transcript regression suite",
    ),
    (
        "documentation",
        "docs(ml): document AI-only training and release workflow",
    ),
    (
        "foundation",
        "feat(ml): add transcript-polishing pipeline foundations",
    ),
)


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic UTF-8 JSON bytes."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise GitHandoffError(f"value is not canonical JSON: {error}") from error
    return encoded.encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise GitHandoffError(f"evidence is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    allow_failure: bool = False,
) -> bytes | None:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
    )
    if process.returncode == 0:
        return process.stdout
    if allow_failure:
        return None
    message = process.stderr.decode("utf-8", errors="replace").strip()
    raise GitHandoffError(
        f"read-only git command failed ({' '.join(arguments)}): {message}"
    )


def discover_repository(path: Path) -> Path:
    """Resolve the repository root without changing Git state."""

    candidate = path.resolve()
    output = _run_git(candidate, ["rev-parse", "--show-toplevel"])
    assert output is not None
    decoded = output.decode("utf-8", errors="strict").strip()
    repository = Path(decoded).resolve()
    if not repository.is_dir():
        raise GitHandoffError("Git reported a repository root that is not a directory")
    return repository


def _decode_git_path(payload: bytes) -> str:
    try:
        path = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GitHandoffError("Git path is not valid UTF-8") from error
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in path
        or ":" in path
        or "\x00" in path
    ):
        raise GitHandoffError(f"unsafe Git path: {path!r}")
    return pure.as_posix()


def parse_porcelain_v1_z(payload: bytes) -> list[dict[str, Any]]:
    """Parse ``git status --porcelain=v1 -z`` without lossy shell quoting."""

    fields = payload.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise GitHandoffError("malformed porcelain-v1 status record")
        try:
            index_state = chr(record[0])
            worktree_state = chr(record[1])
        except ValueError as error:
            raise GitHandoffError("non-ASCII Git status code") from error
        path = _decode_git_path(record[3:])
        entry: dict[str, Any] = {
            "path": path,
            "status": {
                "index": index_state,
                "worktree": worktree_state,
            },
        }
        if index_state in {"R", "C"} or worktree_state in {"R", "C"}:
            index += 1
            if index >= len(fields):
                raise GitHandoffError("rename/copy status is missing its original path")
            entry["original_path"] = _decode_git_path(fields[index])
        entries.append(entry)
        index += 1
    if len({entry["path"] for entry in entries}) != len(entries):
        raise GitHandoffError("Git status contains duplicate destination paths")
    return entries


def _relative_worktree_path(repository: Path, git_path: str) -> Path:
    resolved_repository = repository.resolve()
    lexical = resolved_repository.joinpath(*PurePosixPath(git_path).parts)
    if not lexical.is_relative_to(resolved_repository):
        raise GitHandoffError(f"Git path escapes repository: {git_path}")
    return lexical


def _parse_index_stage_listing(
    output: bytes,
) -> dict[str, list[tuple[str, str, int]]]:
    records: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, separator, listed_path = raw.partition(b"\t")
        if not separator:
            raise GitHandoffError("malformed tracked-index record")
        git_path = _decode_git_path(listed_path)
        parts = metadata.decode("ascii", errors="strict").split(" ")
        if len(parts) != 3 or not parts[2].isdigit():
            raise GitHandoffError(f"malformed index metadata for {git_path}")
        records[git_path].append((parts[0], parts[1], int(parts[2])))
    return records


def _snapshot_from_index_records(
    repository: Path,
    records: Sequence[tuple[str, str, int]],
) -> tuple[dict[str, Any], bytes | None, list[str]]:
    stage_zero = [record for record in records if record[2] == 0]
    if len(stage_zero) != 1:
        reasons = ["unmerged_index"] if records else ["missing_index_entry"]
        return {"state": "absent"}, None, reasons
    mode, object_id, _stage = stage_zero[0]
    if mode == "160000":
        return {
            "state": "gitlink",
            "git_mode": mode,
            "git_object": object_id,
        }, None, ["gitlink"]
    payload = _run_git(repository, ["cat-file", "blob", object_id])
    assert payload is not None
    reasons = ["symlink"] if mode == "120000" else []
    return {
        "state": "present",
        "size_bytes": len(payload),
        "sha256": sha256_bytes(payload),
        "git_mode": mode,
        "git_object": object_id,
    }, payload, reasons


def _index_snapshot(
    repository: Path,
    git_path: str,
    *,
    deleted: bool,
) -> tuple[dict[str, Any], bytes | None, list[str]]:
    if deleted:
        return {"state": "deleted"}, None, []
    output = _run_git(
        repository,
        ["ls-files", "--stage", "-z", "--", git_path],
    )
    assert output is not None
    records = _parse_index_stage_listing(output).get(git_path, [])
    return _snapshot_from_index_records(repository, records)


def _worktree_snapshot(
    repository: Path,
    git_path: str,
    *,
    deleted: bool,
) -> tuple[dict[str, Any], bytes | None, list[str]]:
    if deleted:
        return {"state": "deleted"}, None, []
    path = _relative_worktree_path(repository, git_path)
    repository_root = repository.resolve()
    relative_parts = path.relative_to(repository_root).parts
    current = repository_root
    has_symlink = False
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            has_symlink = True
            break
    if has_symlink:
        return {"state": "symlink"}, None, ["symlink"]
    if not path.exists():
        return {"state": "absent"}, None, ["missing_worktree_file"]
    if not path.is_file():
        return {"state": "non_regular"}, None, ["non_regular_file"]
    payload = path.read_bytes()
    return {
        "state": "present",
        "size_bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }, payload, []


def _capture_snapshots(
    repository: Path,
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes], list[str]]:
    status = entry["status"]
    untracked = status["index"] == "?" and status["worktree"] == "?"
    if untracked:
        index_snapshot = {"state": "absent"}
        index_payload = None
        index_reasons: list[str] = []
    else:
        index_snapshot, index_payload, index_reasons = _index_snapshot(
            repository,
            entry["path"],
            deleted=status["index"] == "D",
        )
    worktree_snapshot, worktree_payload, worktree_reasons = _worktree_snapshot(
        repository,
        entry["path"],
        deleted=(
            status["worktree"] == "D"
            or (status["index"] == "D" and status["worktree"] == " ")
        ),
    )
    payloads = {}
    if index_payload is not None:
        payloads["index"] = index_payload
    if worktree_payload is not None:
        payloads["worktree"] = worktree_payload
    return (
        {"index": index_snapshot, "worktree": worktree_snapshot},
        payloads,
        sorted(set(index_reasons + worktree_reasons)),
    )


def _path_is_in_scope(
    path: str,
    scope: str,
    *,
    include_repository_policy_files: bool,
) -> bool:
    if path == scope or path.startswith(scope + "/"):
        return True
    return include_repository_policy_files and path in {".gitattributes", ".gitignore"}


def _classify_path(path: str, scope: str) -> tuple[str | None, list[str], int]:
    if path in {".gitattributes", ".gitignore"}:
        return "repository_policy", [], MAX_TEXT_FILE_BYTES
    relative = path.removeprefix(scope + "/")
    pure = PurePosixPath(relative)
    parts = tuple(part.casefold() for part in pure.parts)
    filename = pure.name.casefold()
    suffix = pure.suffix.casefold()

    if suffix in _FORBIDDEN_SUFFIXES:
        return None, ["forbidden_model_or_archive_extension"], 0
    if filename == ".env" or _SECRET_NAME_RE.search(filename):
        return None, ["secret_bearing_filename"], 0
    if any(
        segment in _FORBIDDEN_SEGMENTS
        or segment.startswith("checkpoint-")
        or segment in {"final", "pilot", "test"}
        for segment in parts
    ):
        return None, ["forbidden_generated_or_cache_path"], 0
    if filename in _FORBIDDEN_DATA_FILENAMES:
        return None, ["full_dataset_filename"], 0

    if relative in {"README.md", "pyproject.toml", "uv.lock", ".env.example"}:
        return "project_metadata", [], MAX_TEXT_FILE_BYTES
    if filename == ".gitkeep":
        return "repository_placeholder", [], MAX_TEXT_FILE_BYTES
    if relative.startswith(("src/", "scripts/", "tests/")) and suffix == ".py":
        category = "tests" if relative.startswith("tests/") else "pipeline_code"
        return category, [], MAX_TEXT_FILE_BYTES
    if relative.startswith("scripts/") and suffix == ".sh":
        return "pipeline_code", [], MAX_TEXT_FILE_BYTES
    if relative.startswith("configs/") and suffix in {".json", ".toml", ".yaml", ".yml"}:
        return "configuration", [], MAX_TEXT_FILE_BYTES
    if relative.startswith("contracts/"):
        if suffix in {".json", ".yaml", ".yml"}:
            return "schema", [], MAX_TEXT_FILE_BYTES
        if suffix in {".md", ".txt"}:
            return "prompt_or_contract_documentation", [], MAX_TEXT_FILE_BYTES
    if relative.startswith("handoffs/") and suffix in {".json", ".md", ".txt", ".yaml", ".yml"}:
        return "agent_prompt_or_handoff", [], MAX_TEXT_FILE_BYTES
    if relative.startswith(("docs/",)) and suffix in {".md", ".txt"}:
        return "documentation", [], MAX_TEXT_FILE_BYTES
    if relative.startswith(("lexica/", "data/lexica/")) and suffix in {
        ".json",
        ".jsonl",
        ".txt",
        ".yaml",
        ".yml",
    }:
        return "lexicon", [], MAX_TEXT_FILE_BYTES
    if relative.startswith(("data/seeds/", "data/targets/")):
        if suffix == ".py":
            return "generator_code", [], MAX_TEXT_FILE_BYTES
        if suffix in {".json", ".md", ".sha256"}:
            return "generator_manifest_or_report", [], MAX_TEXT_FILE_BYTES
        if suffix == ".jsonl" and any(
            marker in filename for marker in ("rejection", "sample", "test")
        ):
            return "small_sample", [], MAX_SAMPLE_FILE_BYTES
    if relative.startswith(("samples/", "test_samples/")) and suffix in {
        ".json",
        ".jsonl",
        ".md",
        ".txt",
    }:
        return "small_sample", [], MAX_SAMPLE_FILE_BYTES
    if relative.startswith(("reports/", "manifests/", "checksums/")) and suffix in {
        ".json",
        ".md",
        ".sha256",
        ".txt",
    }:
        return "report_manifest_or_checksum", [], MAX_TEXT_FILE_BYTES

    allowed_artifact_prefixes = (
        "artifacts/checksums/",
        "artifacts/evaluation/reports/",
        "artifacts/final_report/",
        "artifacts/judges/summaries/",
        "artifacts/manifests/",
        "artifacts/release/",
    )
    if relative.startswith(allowed_artifact_prefixes) and suffix in {
        ".json",
        ".md",
        ".sha256",
        ".txt",
    }:
        return "report_manifest_or_checksum", [], MAX_TEXT_FILE_BYTES
    return None, ["path_or_extension_not_allowlisted"], 0


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = defaultdict(int)
    for character in text:
        counts[character] += 1
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return any(term in lowered for term in _PLACEHOLDER_TERMS)


def scan_credentials(
    payload: bytes,
    *,
    location: str,
    path: str,
) -> list[dict[str, Any]]:
    """Return redacted credential findings; matched values are never retained."""

    findings: list[dict[str, Any]] = []
    for rule, pattern in _CREDENTIAL_PATTERNS:
        for match in pattern.finditer(payload):
            line = payload.count(b"\n", 0, match.start()) + 1
            findings.append(
                {
                    "location": location,
                    "path": path,
                    "rule": rule,
                    "line": line,
                }
            )
    for match in _QUOTED_SECRET_ASSIGNMENT_RE.finditer(payload):
        value = match.group(1).decode("utf-8", errors="ignore").strip()
        if (
            _looks_like_placeholder(value)
            or len(value) < 20
            or _entropy(value) < 3.2
        ):
            continue
        line = payload.count(b"\n", 0, match.start()) + 1
        findings.append(
            {
                "location": location,
                "path": path,
                "rule": "high_entropy_secret_assignment",
                "line": line,
            }
        )
    unique = {
        (item["location"], item["path"], item["rule"], item["line"]): item
        for item in findings
    }
    return [unique[key] for key in sorted(unique)]


def _text_reasons(payload: bytes, maximum_bytes: int) -> list[str]:
    reasons = []
    if len(payload) > maximum_bytes:
        reasons.append("file_too_large")
    if b"\0" in payload:
        reasons.append("binary_content")
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        reasons.append("non_utf8_content")
    return reasons


def _sanitize_remote_url(url: str) -> tuple[str, bool]:
    if "://" not in url:
        return url, False
    parsed = urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return url, False
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname += f":{parsed.port}"
    sanitized = SplitResult(
        scheme=parsed.scheme,
        netloc="***@" + hostname,
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(sanitized), True


def _decode_git_line(output: bytes | None) -> str | None:
    if output is None:
        return None
    value = output.decode("utf-8", errors="strict").strip()
    return value or None


def _repository_identity(
    repository: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    head = _decode_git_line(_run_git(repository, ["rev-parse", "HEAD"]))
    if head is None or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise GitHandoffError("HEAD is not a full lowercase Git SHA")
    branch = _decode_git_line(
        _run_git(repository, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True)
    )
    upstream_ref = _decode_git_line(
        _run_git(
            repository,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            allow_failure=True,
        )
    )
    upstream: dict[str, str] | None = None
    if upstream_ref is not None:
        remote_name, separator, remote_branch = upstream_ref.partition("/")
        upstream = {
            "ref": upstream_ref,
            "remote": remote_name if separator else "",
            "branch": remote_branch if separator else upstream_ref,
        }

    names_output = _run_git(repository, ["remote"])
    assert names_output is not None
    remotes = []
    remote_findings: list[dict[str, Any]] = []
    for name in sorted(
        line for line in names_output.decode("utf-8", errors="strict").splitlines() if line
    ):
        fetch = _decode_git_line(_run_git(repository, ["remote", "get-url", name]))
        push = _decode_git_line(
            _run_git(repository, ["remote", "get-url", "--push", name])
        )
        if fetch is None or push is None:
            raise GitHandoffError(f"remote {name} has no usable URL")
        safe_fetch, fetch_has_credentials = _sanitize_remote_url(fetch)
        safe_push, push_has_credentials = _sanitize_remote_url(push)
        if fetch_has_credentials:
            remote_findings.append(
                {
                    "location": "remote",
                    "path": f"remote:{name}:fetch",
                    "rule": "url_userinfo",
                }
            )
        if push_has_credentials:
            remote_findings.append(
                {
                    "location": "remote",
                    "path": f"remote:{name}:push",
                    "rule": "url_userinfo",
                }
            )
        for direction, raw_url in (("fetch", fetch), ("push", push)):
            remote_findings.extend(
                scan_credentials(
                    raw_url.encode("utf-8"),
                    location="remote",
                    path=f"remote:{name}:{direction}",
                )
            )
        remotes.append(
            {
                "name": name,
                "fetch_url": safe_fetch,
                "push_url": safe_push,
            }
        )
    return {
        "branch": branch,
        "detached": branch is None,
        "head_sha": head,
        "upstream": upstream,
        "remotes": remotes,
    }, remote_findings


def _json_pointer(value: object, pointer: str, label: str) -> object:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise GitHandoffError(f"{label} is not an RFC 6901 JSON Pointer")
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise GitHandoffError(f"{label} does not exist: {pointer}")
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            if not token.isdigit() or int(token) >= len(current):
                raise GitHandoffError(f"{label} has an invalid array index: {pointer}")
            current = current[int(token)]
        else:
            raise GitHandoffError(f"{label} traverses a scalar: {pointer}")
    return current


def _safe_evidence_path(repository: Path, declared: str) -> Path:
    pure = PurePosixPath(declared)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in declared
        or ":" in declared
    ):
        raise GitHandoffError(f"unsafe PR evidence path: {declared}")
    repository_root = repository.resolve()
    lexical = repository_root.joinpath(*pure.parts)
    if not lexical.is_relative_to(repository_root):
        raise GitHandoffError(f"PR evidence path escapes repository: {declared}")
    current = repository_root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise GitHandoffError(f"PR evidence traverses a symlink: {declared}")
    if not lexical.is_file():
        raise GitHandoffError(f"PR evidence is not a regular file: {declared}")
    return lexical


def load_pr_evidence(
    repository: Path,
    manifest_path: Path | None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]], list[str]]:
    """Load PR section values through exact path/hash/pointer bindings."""

    if manifest_path is None:
        return {}, {}, [section for section, _title in PR_SECTIONS]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise GitHandoffError("PR evidence manifest must be a schema_version=1 object")
    sections = manifest.get("sections")
    if not isinstance(sections, dict):
        raise GitHandoffError("PR evidence manifest sections must be an object")
    allowed = {section for section, _title in PR_SECTIONS}
    unknown = sorted(set(sections).difference(allowed))
    if unknown:
        raise GitHandoffError(f"unknown PR evidence sections: {', '.join(unknown)}")

    values: dict[str, Any] = {}
    sources: dict[str, dict[str, str]] = {}
    for section, binding in sorted(sections.items()):
        if not isinstance(binding, dict) or set(binding) != {"path", "pointer", "sha256"}:
            raise GitHandoffError(
                f"PR evidence binding {section} must contain path, pointer, and sha256 exactly"
            )
        declared_path = binding["path"]
        pointer = binding["pointer"]
        declared_hash = binding["sha256"]
        if not all(isinstance(item, str) for item in (declared_path, pointer, declared_hash)):
            raise GitHandoffError(f"PR evidence binding {section} values must be strings")
        evidence_path = _safe_evidence_path(repository, declared_path)
        payload = evidence_path.read_bytes()
        if len(payload) > MAX_TEXT_FILE_BYTES:
            raise GitHandoffError(f"PR evidence is too large for {section}")
        actual_hash = sha256_bytes(payload)
        if actual_hash != declared_hash:
            raise GitHandoffError(
                f"PR evidence hash mismatch for {section}: {declared_path}"
            )
        findings = scan_credentials(
            payload,
            location="evidence",
            path=declared_path,
        )
        if findings:
            raise GitHandoffError(
                f"credential-like content found in PR evidence for {section}"
            )
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHandoffError(
                f"PR evidence for {section} is not valid UTF-8 JSON"
            ) from error
        extracted = _json_pointer(document, pointer, f"PR evidence {section}")
        if extracted is None or isinstance(extracted, bool):
            raise GitHandoffError(f"PR evidence {section} is null or boolean")
        if isinstance(extracted, str) and not extracted.strip():
            raise GitHandoffError(f"PR evidence {section} is empty")
        if isinstance(extracted, list | dict) and not extracted:
            raise GitHandoffError(f"PR evidence {section} is empty")
        values[section] = json.loads(canonical_json_bytes(extracted))
        sources[section] = {
            "path": declared_path,
            "sha256": actual_hash,
            "pointer": pointer,
        }
    missing = [section for section, _title in PR_SECTIONS if section not in values]
    return values, sources, missing


def _render_evidence_value(value: object) -> list[str]:
    if isinstance(value, str):
        return value.splitlines()
    return [
        "```json",
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        "```",
    ]


def build_draft_pr_description(
    values: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, str]],
) -> tuple[str, list[str]]:
    """Render a draft PR body without inventing any project-status claims."""

    missing = [section for section, _title in PR_SECTIONS if section not in values]
    lines = [
        "<!-- DRAFT: Inhalte sind ausschließlich hashgebundene Evidenzprojektionen. -->",
        "",
    ]
    for section, title in PR_SECTIONS:
        lines.extend([f"## {title}", ""])
        if section not in values:
            lines.append("_Nicht durch hashgebundene Evidenz belegt._")
        else:
            lines.extend(_render_evidence_value(values[section]))
            source = sources[section]
            lines.extend(
                [
                    "",
                    (
                        f"_Evidenz: `{source['path']}` · `{source['sha256']}` "
                        f"· JSON-Pointer `{source['pointer']}`_"
                    ),
                ]
            )
        lines.append("")
    return "\n".join(lines), missing


def _commit_bucket(path: str) -> str:
    lowered = path.casefold()
    filename = PurePosixPath(lowered).name
    if "/tests/" in lowered or lowered.endswith(("/test", "_test.py")) or filename.startswith("test_"):
        return "tests"
    if lowered.endswith((".md", ".txt")) or "/handoffs/" in lowered or "/docs/" in lowered:
        return "documentation"
    if any(term in lowered for term in ("critic", "judge", "review_aggregation")):
        return "critics"
    if any(
        term in lowered
        for term in (
            "ast_codec",
            "condition_realization",
            "document_ast",
            "semantic",
            "sst",
            "target_renderer",
        )
    ):
        return "semantic_sst"
    if any(
        term in lowered
        for term in ("legal", "unit", "protected_span", "pronoun", "orthograph")
    ):
        return "normalization"
    if any(
        term in lowered
        for term in (
            "train",
            "checkpoint",
            "inference",
            "benchmark",
            "quant",
            "hub",
            "evaluation",
            "metric",
            "release",
        )
    ):
        return "training"
    if any(
        term in lowered
        for term in (
            "/data/seeds/",
            "/data/targets/",
            "canonical",
            "corruption",
            "dataset",
            "generator",
            "gold",
        )
    ):
        return "data_factory"
    return "foundation"


def propose_commit_groups(paths: Sequence[str]) -> list[dict[str, Any]]:
    """Assign every safe path to exactly one deterministic logical commit."""

    buckets: dict[str, list[str]] = defaultdict(list)
    for path in sorted(set(paths)):
        buckets[_commit_bucket(path)].append(path)
    groups = []
    for bucket, title in _COMMIT_ORDER:
        if bucket in buckets:
            groups.append(
                {
                    "group": bucket,
                    "suggested_message": title,
                    "paths": buckets[bucket],
                }
            )
    flattened = [path for group in groups for path in group["paths"]]
    if len(flattened) != len(set(paths)) or set(flattened) != set(paths):
        raise GitHandoffError("commit grouping did not preserve the exact safe path set")
    return groups


def _validate_schema(report: object, contracts_root: Path) -> None:
    schema_path = contracts_root / "git_handoff_schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GitHandoffError(f"cannot load Git handoff schema: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise GitHandoffError(
            f"Git handoff schema failed at {location}: {first.message}"
        )


def _validate_handoff_invariants(report: Mapping[str, Any]) -> None:
    inventory = report["inventory"]
    included = inventory["included"]
    excluded = inventory["excluded"]
    out_of_scope = inventory["out_of_scope"]
    tracked = inventory["tracked_index"]
    forbidden_tracked = inventory["forbidden_tracked_index"]
    expected_counts = {
        "included_count": len(included),
        "excluded_count": len(excluded),
        "out_of_scope_count": len(out_of_scope),
        "scoped_entry_count": len(included) + len(excluded),
        "status_entry_count": len(included) + len(excluded) + len(out_of_scope),
        "tracked_index_count": len(tracked) + len(forbidden_tracked),
        "forbidden_tracked_index_count": len(forbidden_tracked),
    }
    for field, expected in expected_counts.items():
        if inventory[field] != expected:
            raise GitHandoffError(f"inventory {field} is internally inconsistent")

    status_paths = [
        item["path"] for item in (*included, *excluded, *out_of_scope)
    ]
    if len(status_paths) != len(set(status_paths)):
        raise GitHandoffError("status inventory paths are not unique")
    tracked_paths = [item["path"] for item in (*tracked, *forbidden_tracked)]
    if len(tracked_paths) != len(set(tracked_paths)):
        raise GitHandoffError("tracked-index inventory paths are not unique")

    expected_included_bytes = sum(
        max(
            (
                snapshot.get("size_bytes", 0)
                for snapshot in item["snapshots"].values()
                if snapshot["state"] == "present"
            ),
            default=0,
        )
        for item in included
    )
    if inventory["included_bytes"] != expected_included_bytes:
        raise GitHandoffError("inventory included_bytes is internally inconsistent")
    expected_tracked_bytes = sum(
        item["snapshot"].get("size_bytes", 0)
        for item in (*tracked, *forbidden_tracked)
        if item["snapshot"]["state"] == "present"
    )
    if inventory["tracked_index_bytes"] != expected_tracked_bytes:
        raise GitHandoffError("inventory tracked_index_bytes is internally inconsistent")

    credential_scan = report["credential_scan"]
    if credential_scan["finding_count"] != len(credential_scan["findings"]):
        raise GitHandoffError("credential finding count is internally inconsistent")
    if credential_scan["passed"] != (not credential_scan["findings"]):
        raise GitHandoffError("credential pass state is internally inconsistent")

    draft = report["pull_request_draft"]
    source_sections = set(draft["evidence_sources"])
    expected_missing = [
        section for section, _title in PR_SECTIONS if section not in source_sections
    ]
    if draft["missing_evidence_sections"] != expected_missing:
        raise GitHandoffError("draft PR missing-evidence sections are inconsistent")
    if draft["complete"] != (not expected_missing):
        raise GitHandoffError("draft PR completeness state is inconsistent")
    for _section, title in PR_SECTIONS:
        if f"## {title}" not in draft["body"]:
            raise GitHandoffError(f"draft PR body is missing section: {title}")

    credential_paths = {
        finding["path"]
        for finding in credential_scan["findings"]
        if finding["location"] in {"index", "worktree"}
    }
    expected_commit_paths = {
        item["path"] for item in included if item["path"] not in credential_paths
    }
    proposed_commit_paths = [
        path for group in report["commit_groups"] for path in group["paths"]
    ]
    if (
        len(proposed_commit_paths) != len(set(proposed_commit_paths))
        or set(proposed_commit_paths) != expected_commit_paths
    ):
        raise GitHandoffError("logical commit groups do not match the safe change set")

    repository = report["repository"]
    if repository["detached"] != (repository["branch"] is None):
        raise GitHandoffError("detached-head state and branch are inconsistent")
    remote_names = [remote["name"] for remote in repository["remotes"]]
    if len(remote_names) != len(set(remote_names)):
        raise GitHandoffError("repository remote names are not unique")

    global_blockers = set()
    if inventory["included_bytes"] > report["policy"]["max_total_included_bytes"]:
        global_blockers.add("included_total_size_exceeds_limit")
    if repository["detached"]:
        global_blockers.add("detached_head")
    if not repository["remotes"]:
        global_blockers.add("no_git_remote")
    if excluded:
        global_blockers.add("scoped_files_rejected")
    if forbidden_tracked:
        global_blockers.add("forbidden_files_in_tracked_index")
    if credential_scan["findings"]:
        global_blockers.add("credential_findings")
    readiness = report["readiness"]
    if readiness["inventory_safe_to_stage"] != (not global_blockers):
        raise GitHandoffError("inventory staging readiness is internally inconsistent")
    if not global_blockers.issubset(set(readiness["blocker_codes"])):
        raise GitHandoffError("readiness omits an inventory blocker")
    if "push_and_pr_not_verified" not in readiness["blocker_codes"]:
        raise GitHandoffError("readiness must retain unverified push/PR blocker")


def verify_git_handoff(
    report: Mapping[str, Any],
    *,
    contracts_root: Path = CONTRACTS_DIRECTORY,
) -> dict[str, Any]:
    """Validate the report schema and its canonical body digest."""

    materialized = json.loads(canonical_json_bytes(report))
    _validate_schema(materialized, contracts_root)
    _validate_handoff_invariants(materialized)
    body = {key: value for key, value in materialized.items() if key != "integrity"}
    if materialized["integrity"]["report_body_sha256"] != sha256_bytes(
        canonical_json_bytes(body)
    ):
        raise GitHandoffError("Git handoff report body hash does not verify")
    return materialized


def verify_git_pr_evidence(
    evidence: Mapping[str, Any],
    *,
    contracts_root: Path = CONTRACTS_DIRECTORY,
) -> dict[str, Any]:
    """Verify post-mutation Git/PR evidence before final-report ingestion.

    JSON Schema establishes the explicit Section 44 shape.  These additional
    equality checks bind the pushed remote head and PR head to the same exact
    commit that the final-report builder reads from ``/commit``.
    """

    materialized = json.loads(canonical_json_bytes(evidence))
    schema_path = contracts_root / "git_pr_evidence_schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GitHandoffError(f"cannot load Git/PR evidence schema: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(materialized),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise GitHandoffError(
            f"Git/PR evidence schema failed at {location}: {first.message}"
        )

    branch = materialized["branch"]
    commit = materialized["commit"]
    push = materialized["push"]
    pull_request = materialized["pull_request"]
    if push["remote_branch"] != branch:
        raise GitHandoffError("pushed remote branch differs from evidence branch")
    if push["remote_head_sha"] != commit:
        raise GitHandoffError("pushed remote head differs from evidence commit")
    if pull_request["head_branch"] != branch:
        raise GitHandoffError("pull-request head branch differs from evidence branch")
    if pull_request["head_sha"] != commit:
        raise GitHandoffError("pull-request head SHA differs from evidence commit")
    if not pull_request["url"].endswith(f"/pull/{pull_request['id']}"):
        raise GitHandoffError("pull-request URL and numeric ID differ")

    remote_url = materialized["remote"]["url"]
    _safe_url, contains_userinfo = _sanitize_remote_url(remote_url)
    if contains_userinfo or scan_credentials(
        remote_url.encode("utf-8"),
        location="remote",
        path=f"remote:{materialized['remote']['name']}",
    ):
        raise GitHandoffError("Git/PR evidence remote URL contains credential-like content")
    return materialized


def build_git_handoff(
    repository: Path,
    *,
    scope: str = DEFAULT_SCOPE,
    include_repository_policy_files: bool = False,
    pr_evidence_manifest: Path | None = None,
    contracts_root: Path = CONTRACTS_DIRECTORY,
) -> dict[str, Any]:
    """Inventory the current handoff without mutating Git or any external system."""

    repository = discover_repository(repository)
    scope_path = PurePosixPath(scope)
    normalized_scope = scope_path.as_posix()
    if (
        not normalized_scope
        or scope_path.is_absolute()
        or ".." in scope_path.parts
        or "\\" in scope
        or ":" in scope
    ):
        raise GitHandoffError("scope must be a safe repository-relative path")

    status_arguments = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
    ]
    status_payload = _run_git(repository, status_arguments)
    assert status_payload is not None
    entries = parse_porcelain_v1_z(status_payload)
    identity, remote_findings = _repository_identity(repository)

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    credential_findings = list(remote_findings)
    captured_snapshots: dict[str, dict[str, Any]] = {}

    for entry in entries:
        path = entry["path"]
        if not _path_is_in_scope(
            path,
            normalized_scope,
            include_repository_policy_files=include_repository_policy_files,
        ):
            out_of_scope.append(dict(entry))
            continue

        snapshots, payloads, snapshot_reasons = _capture_snapshots(repository, entry)
        captured_snapshots[path] = snapshots
        if entry["status"]["index"] == "D":
            category, path_reasons, maximum_bytes = (
                "remove_forbidden_or_obsolete_file",
                [],
                MAX_TEXT_FILE_BYTES,
            )
        else:
            category, path_reasons, maximum_bytes = _classify_path(path, normalized_scope)
        reasons = list(snapshot_reasons + path_reasons)
        if entry["status"]["index"] == "U" or entry["status"]["worktree"] == "U":
            reasons.append("unmerged_status")
        if category is not None:
            for location, payload in sorted(payloads.items()):
                reasons.extend(_text_reasons(payload, maximum_bytes))
                if len(payload) <= MAX_CREDENTIAL_SCAN_BYTES:
                    credential_findings.extend(
                        scan_credentials(payload, location=location, path=path)
                    )
        record = {
            **entry,
            "snapshots": snapshots,
        }
        if category is not None and not reasons:
            record["category"] = category
            included.append(record)
        else:
            record["reason_codes"] = sorted(set(reasons or ["not_allowlisted"]))
            excluded.append(record)

    included.sort(key=lambda item: item["path"])
    excluded.sort(key=lambda item: item["path"])
    out_of_scope.sort(key=lambda item: item["path"])
    credential_findings = sorted(
        {
            (
                finding["location"],
                finding["path"],
                finding["rule"],
                finding.get("line"),
            ): finding
            for finding in credential_findings
        }.values(),
        key=lambda item: (
            item["path"],
            item["location"],
            item["rule"],
            item.get("line", 0),
        ),
    )

    total_bytes = sum(
        max(
            (
                snapshot.get("size_bytes", 0)
                for snapshot in item["snapshots"].values()
                if snapshot["state"] == "present"
            ),
            default=0,
        )
        for item in included
    )

    tracked_payload = _run_git(
        repository,
        ["ls-files", "--stage", "-z", "--", normalized_scope],
    )
    assert tracked_payload is not None
    tracked_records = _parse_index_stage_listing(tracked_payload)
    tracked_paths = sorted(tracked_records)
    status_paths = {entry["path"] for entry in entries}
    tracked_index: list[dict[str, Any]] = []
    forbidden_tracked_index: list[dict[str, Any]] = []
    tracked_index_bytes = 0
    for path in tracked_paths:
        snapshot, payload, snapshot_reasons = _snapshot_from_index_records(
            repository,
            tracked_records[path],
        )
        category, path_reasons, maximum_bytes = _classify_path(path, normalized_scope)
        reasons = list(snapshot_reasons + path_reasons)
        if payload is not None:
            tracked_index_bytes += len(payload)
            if category is not None:
                reasons.extend(_text_reasons(payload, maximum_bytes))
                if path not in status_paths and len(payload) <= MAX_CREDENTIAL_SCAN_BYTES:
                    credential_findings.extend(
                        scan_credentials(payload, location="index", path=path)
                    )
        record = {
            "path": path,
            "snapshot": snapshot,
        }
        if category is not None and not reasons:
            record["category"] = category
            tracked_index.append(record)
        else:
            record["reason_codes"] = sorted(set(reasons or ["not_allowlisted"]))
            forbidden_tracked_index.append(record)

    credential_findings = sorted(
        {
            (
                finding["location"],
                finding["path"],
                finding["rule"],
                finding.get("line"),
            ): finding
            for finding in credential_findings
        }.values(),
        key=lambda item: (
            item["path"],
            item["location"],
            item["rule"],
            item.get("line", 0),
        ),
    )
    global_blockers = []
    if total_bytes > MAX_TOTAL_INCLUDED_BYTES:
        global_blockers.append("included_total_size_exceeds_limit")
    if identity["detached"]:
        global_blockers.append("detached_head")
    if not identity["remotes"]:
        global_blockers.append("no_git_remote")
    if excluded:
        global_blockers.append("scoped_files_rejected")
    if forbidden_tracked_index:
        global_blockers.append("forbidden_files_in_tracked_index")
    if credential_findings:
        global_blockers.append("credential_findings")

    safe_paths = [
        item["path"]
        for item in included
        if not any(
            finding["path"] == item["path"] for finding in credential_findings
        )
    ]
    commit_groups = propose_commit_groups(safe_paths)

    values, evidence_sources, missing_pr_sections = load_pr_evidence(
        repository,
        pr_evidence_manifest,
    )
    pr_body, rendered_missing = build_draft_pr_description(values, evidence_sources)
    if rendered_missing != [
        section for section, _title in PR_SECTIONS if section in set(missing_pr_sections)
    ]:
        raise GitHandoffError("PR evidence completeness calculation is inconsistent")

    staged_paths = {
        entry["path"]
        for entry in entries
        if entry["status"]["index"] not in {" ", "?"}
    }
    safe_path_set = set(safe_paths)
    eligible_unstaged = {
        item["path"]
        for item in included
        if item["status"]["worktree"] not in {" "}
        or item["status"]["index"] == "?"
    }
    unsafe_staged = staged_paths.difference(safe_path_set)
    current_index_complete = (
        bool(staged_paths)
        and not unsafe_staged
        and not eligible_unstaged
        and not global_blockers
    )
    current_head_safe_to_push = (
        not entries
        and identity["branch"] is not None
        and identity["upstream"] is not None
        and bool(identity["remotes"])
        and not credential_findings
        and not global_blockers
    )
    readiness_blockers = list(global_blockers)
    if unsafe_staged:
        readiness_blockers.append("unsafe_or_out_of_scope_paths_staged")
    if identity["upstream"] is None:
        readiness_blockers.append("upstream_not_configured")
    if missing_pr_sections:
        readiness_blockers.append("pr_evidence_incomplete")
    readiness_blockers.append("push_and_pr_not_verified")

    body: dict[str, Any] = {
        "schema_version": 1,
        "repository": {
            **identity,
            "scope": normalized_scope,
            "status_porcelain_v1_sha256": sha256_bytes(status_payload),
        },
        "policy": {
            "mode": "read_only_fail_closed_v1",
            "include_repository_policy_files": include_repository_policy_files,
            "max_text_file_bytes": MAX_TEXT_FILE_BYTES,
            "max_sample_file_bytes": MAX_SAMPLE_FILE_BYTES,
            "max_total_included_bytes": MAX_TOTAL_INCLUDED_BYTES,
        },
        "inventory": {
            "status_entry_count": len(entries),
            "scoped_entry_count": len(included) + len(excluded),
            "tracked_index_count": len(tracked_paths),
            "tracked_index_bytes": tracked_index_bytes,
            "tracked_index_listing_sha256": sha256_bytes(tracked_payload),
            "tracked_index": tracked_index,
            "forbidden_tracked_index_count": len(forbidden_tracked_index),
            "forbidden_tracked_index": forbidden_tracked_index,
            "included_count": len(included),
            "included_bytes": total_bytes,
            "excluded_count": len(excluded),
            "out_of_scope_count": len(out_of_scope),
            "included": included,
            "excluded": excluded,
            "out_of_scope": out_of_scope,
        },
        "credential_scan": {
            "passed": not credential_findings,
            "finding_count": len(credential_findings),
            "findings": credential_findings,
        },
        "readiness": {
            "inventory_safe_to_stage": not global_blockers,
            "current_index_safe_to_commit": current_index_complete,
            "current_head_safe_to_push": current_head_safe_to_push,
            "safe_to_create_draft_pr": False,
            "blocker_codes": sorted(set(readiness_blockers)),
        },
        "commit_groups": commit_groups,
        "pull_request_draft": {
            "title": "Draft: AI-only transcript polishing for Scriber",
            "complete": not missing_pr_sections,
            "missing_evidence_sections": missing_pr_sections,
            "evidence_sources": evidence_sources,
            "body": pr_body,
            "mutation_performed": False,
        },
    }

    # Detect both status transitions and same-status byte changes during the
    # audit.  This prevents a mixed-time report when another process edits Git.
    final_status = _run_git(repository, status_arguments)
    if final_status != status_payload:
        raise GitHandoffError("Git status changed while the handoff was being inventoried")
    final_identity, final_remote_findings = _repository_identity(repository)
    if final_identity != identity or final_remote_findings != remote_findings:
        raise GitHandoffError("repository identity changed during the handoff audit")
    final_tracked_payload = _run_git(
        repository,
        ["ls-files", "--stage", "-z", "--", normalized_scope],
    )
    if final_tracked_payload != tracked_payload:
        raise GitHandoffError("tracked index changed during the handoff audit")
    for entry in entries:
        if entry["path"] not in captured_snapshots:
            continue
        snapshots, _payloads, _reasons = _capture_snapshots(repository, entry)
        if snapshots != captured_snapshots[entry["path"]]:
            raise GitHandoffError(
                f"file changed during the handoff audit: {entry['path']}"
            )

    report = {
        **body,
        "integrity": {
            "canonicalization": "utf8-json-sort-v1",
            "report_body_sha256": sha256_bytes(canonical_json_bytes(body)),
        },
    }
    return verify_git_handoff(report, contracts_root=contracts_root)


def write_git_handoff(report: Mapping[str, Any], path: Path) -> str:
    """Atomically write a verified handoff report and return its SHA-256."""

    verified = verify_git_handoff(report)
    payload = (
        json.dumps(
            verified,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if path.is_symlink():
        raise GitHandoffError(f"refusing to replace symlink output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise GitHandoffError(f"temporary output already exists: {temporary}")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return sha256_bytes(payload)
