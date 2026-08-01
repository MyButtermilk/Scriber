"""Fail-closed HF cloud materialization of the pinned llama.cpp b10156 CUDA bundle."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

from .llama_cpp_bundle import (
    CUDA_HOST_DRIVER_SONAMES,
    LlamaCppBundleError,
    VerifiedLlamaCppBundle,
    load_verified_llama_cpp_bundle,
    materialize_llama_cpp_bundle_lock,
    verify_llama_cpp_runtime_closure,
)
from .oci_llama_cpp_bundle import (
    CONFIG_DIGEST,
    INDEX_DIGEST,
    MANIFEST_DIGEST,
    REPOSITORY,
    SELECTED_LAYERS,
    TAG,
    OciBundleError,
    extract_verified_oci_layers,
    verify_reviewed_llama_cpp_oci_cache,
)

TOOLCHAIN_COMMIT = "91f8c9c5fb038c086e13e9cd823c29b33b07ba54"
TOOLCHAIN_VERSION = "b10156"
TOOLCHAIN_ATTEMPT = "v19"
REVIEWED_SCRIBER_GIT_HEAD = "7ecd7e1ed349d1bcadbbb8151b584b766e9746d5"
TOOLCHAIN_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
TOOLCHAIN_FLAVOR = "cpu-basic"
TOOLCHAIN_GPU_FLAVOR = "l4x1"
TOOLCHAIN_FALLBACK_FLAVOR = "a10g-small"
TOOLCHAIN_HOURLY_RATES_USD = {
    TOOLCHAIN_FLAVOR: Decimal("0.010"),
    TOOLCHAIN_GPU_FLAVOR: Decimal("0.800"),
    TOOLCHAIN_FALLBACK_FLAVOR: Decimal("1.000"),
}
TOOLCHAIN_TIMEOUT_MINUTES = 20
TOOLCHAIN_HOURLY_RATE_USD = Decimal("0.010")
TOOLCHAIN_MAXIMUM_COST_USD = Decimal("0.003333")
QUANTIZATION_RESERVED_COST_USD = Decimal("3.800")
TOTAL_CAMPAIGN_BUDGET_USD = Decimal("13.500")
REMAINING_HF_CREDITS_AUTHORIZATION_USD = Decimal("5.500")
# The account-reported spend already incurred inside the user's final $5.50
# authorization, captured before the reviewed v19 CPU materialization.  Local
# per-job reconstruction may be incomplete, so it is a hard conservative floor.
POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD = Decimal("1.476999")
TOTAL_CAMPAIGN_BUDGET_AUTHORIZATION_SHA256 = "sha256:ea566717d68b8372a4c5f39c6279e24efa36f906c1dfe06c048339bd6ae86c0d"
TOOLCHAIN_OUTPUT_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
TOOLCHAIN_OUTPUT_MOUNT_RELATIVE = "toolchains/llama-cpp-b10156-linux-cuda-v1/v19"
TOOLCHAIN_OUTPUT_MOUNT_URI = f"{TOOLCHAIN_OUTPUT_BUCKET}/{TOOLCHAIN_OUTPUT_MOUNT_RELATIVE}"
TOOLCHAIN_OUTPUT_RELATIVE = f"{TOOLCHAIN_OUTPUT_MOUNT_RELATIVE}/final"
TOOLCHAIN_OUTPUT_ROOT = "/outputs/final"
TOOLCHAIN_STAGING_ROOT = "/outputs/.staging"
TOOLCHAIN_REMOTE_OUTPUT_URI = f"{TOOLCHAIN_OUTPUT_BUCKET}/{TOOLCHAIN_OUTPUT_RELATIVE}"
TOOLCHAIN_RESULT_FILENAME = "toolchain-materialization-result.json"
TOOLCHAIN_LOCK_FILENAME = "llama-cpp-bundle-lock.json"
TOOLCHAIN_JOB_NAME = "scriber-full-120k-v2-llama-toolchain-v19"
TOOLCHAIN_CAMPAIGN_LABEL = "campaign=full-120k-v2-quantized-eval"
TOOLCHAIN_ROLE_LABEL = "role=quantized-eval-toolchain"
TOOLCHAIN_ATTEMPT_LABEL = "attempt=v19"
SOURCE_REPOSITORY = "https://github.com/ggml-org/llama.cpp"
CUDA_VERSION = "12.8.1"
COMPILER_ID = "GNU"
COMPILER_VERSION = "14.2.0"
BUILD_GENERATOR = "Unix Makefiles"
BUILD_CONFIGURATION = "Release"
BUILD_FLAGS = (
    "-DGGML_NATIVE=OFF",
    "-DGGML_CUDA=ON",
    "-DGGML_BACKEND_DL=ON",
    "-DGGML_CPU_ALL_VARIANTS=ON",
    "-DLLAMA_BUILD_TESTS=OFF",
    "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined",
)
ACTUAL_COST_MAX_AGE_SECONDS = 15 * 60
ACTUAL_COST_FUTURE_SKEW_SECONDS = 2 * 60
POST_AUTHORIZATION_TOOLCHAIN_ROLES = frozenset(
    {
        "toolchain_git_status_probe",
        "toolchain_cuda_library_probe",
        "toolchain_materialization",
    }
)

_PROJECT_ROOT = Path(__file__).parents[2]
_MANIFEST_SCHEMA = _PROJECT_ROOT / "contracts" / "hf_llama_cpp_toolchain_job_manifest_schema.json"
_RESULT_SCHEMA = _PROJECT_ROOT / "contracts" / "hf_llama_cpp_toolchain_result_schema.json"
_COST_SCHEMA = _PROJECT_ROOT / "contracts" / "hf_actual_cost_evidence_schema.json"
_PREFIXED_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PRIVATE_BUCKET_PREFIX = "hf://buckets/Buttermilk03/"
_BUCKET_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PACKET_EXCLUDED = frozenset({"job-manifest.json"})
_RESULT_EXCLUDED = frozenset({TOOLCHAIN_RESULT_FILENAME})
_OWNER_TOKEN = re.compile(r"^[0-9a-f]{64}$")
TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER = "__V19_LAUNCH_CLAIM_OWNER__"

TOOLCHAIN_OFFLINE_WHEELS = (
    (
        "attrs-24.3.0-py3-none-any.whl",
        63397,
        "sha256:ac96cd038792094f438ad1f6ff80837353805ac950cd2aa0e0625ef19850c308",
        "attrs==24.3.0",
    ),
    (
        "jsonschema_specifications-2025.9.1-py3-none-any.whl",
        18437,
        "sha256:98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe",
        "jsonschema-specifications==2025.9.1",
    ),
    (
        "jsonschema-4.26.0-py3-none-any.whl",
        90630,
        "sha256:d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce",
        "jsonschema==4.26.0",
    ),
    (
        "referencing-0.36.2-py3-none-any.whl",
        26775,
        "sha256:e8699adbbf8b5c7de96d8ffa0eb5c158b3beafce084968e2ea8bb08c6794dcd0",
        "referencing==0.36.2",
    ),
    (
        "rpds_py-0.27.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        386883,
        "sha256:466bfe65bd932da36ff279ddd92de56b042f2266d752719beb97b08526268ec5",
        "rpds-py==0.27.1",
    ),
    (
        "typing_extensions-4.15.0-py3-none-any.whl",
        44614,
        "sha256:f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548",
        "typing-extensions==4.15.0",
    ),
)


class HfLlamaCppToolchainError(ValueError):
    """The dedicated llama.cpp toolchain packet or result is not trustworthy."""


@dataclass(frozen=True, slots=True)
class HfLlamaCppToolchainPacket:
    """One immutable local packet which has not been uploaded or executed."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise HfLlamaCppToolchainError(f"{label} must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _prefixed_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _PREFIXED_HASH.fullmatch(value) is None:
        raise HfLlamaCppToolchainError(f"{label} must use sha256:<64 lowercase hex>")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HfLlamaCppToolchainError(f"{label} must be an object")
    return value


def _read_json_object(path: str | Path, label: str) -> tuple[dict[str, Any], bytes]:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise HfLlamaCppToolchainError(f"{label} must be a regular file: {candidate}")
    before = candidate.stat()
    try:
        payload = candidate.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HfLlamaCppToolchainError(f"{label} is invalid JSON: {error}") from error
    after = candidate.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise HfLlamaCppToolchainError(f"{label} changed while it was read")
    if not isinstance(value, dict):
        raise HfLlamaCppToolchainError(f"{label} must contain an object")
    return value, payload


def _write_immutable_json(path: str | Path, value: object) -> Path:
    destination = Path(path).expanduser().resolve()
    payload = _canonical_bytes(value)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise HfLlamaCppToolchainError(f"refusing to overwrite different output: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise HfLlamaCppToolchainError(f"temporary output already exists: {temporary}")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)
    return destination


def _tree_inventory(
    root: str | Path,
    *,
    excluded: frozenset[str] = frozenset(),
    allow_safe_symlinks: bool = False,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    candidate = Path(root).expanduser()
    if candidate.is_symlink():
        raise HfLlamaCppToolchainError(f"inventory root must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise HfLlamaCppToolchainError(f"inventory root must be a directory: {resolved}")
    records: list[dict[str, object]] = []
    for path in sorted(resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()):
        relative = path.relative_to(resolved).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            if not allow_safe_symlinks:
                raise HfLlamaCppToolchainError(f"inventory contains a symlink: {relative}")
            target = os.readlink(path)
            if Path(target).is_absolute() or "\\" in target:
                raise HfLlamaCppToolchainError(f"inventory symlink target is unsafe: {relative}")
            try:
                target_path = path.resolve(strict=True)
                common = Path(os.path.commonpath([resolved, target_path]))
            except (OSError, ValueError) as error:
                raise HfLlamaCppToolchainError(f"inventory symlink is broken or unsafe: {relative}") from error
            if common != resolved or not target_path.is_file():
                raise HfLlamaCppToolchainError(f"inventory symlink escapes its root: {relative}")
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": target,
                    "sha256": _sha256(b"symlink\0" + target.encode("utf-8")),
                }
            )
        elif path.is_dir():
            continue
        elif path.is_file():
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path, f"inventory entry {relative}"),
                }
            )
        else:
            raise HfLlamaCppToolchainError(f"inventory contains a non-file entry: {relative}")
    if not records:
        raise HfLlamaCppToolchainError("inventory is empty")
    digest = hashlib.sha256()
    for record in records:
        digest.update(_canonical_bytes(record))
    summary = {
        "algorithm": "sha256-canonical-path-kind-size-content-or-target-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "entry_count": len(records),
        "total_file_bytes": sum(int(record["bytes"]) for record in records if record["kind"] == "file"),
    }
    return records, summary


def _llama_source_inventory(source: Path) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if path.is_symlink():
            raise HfLlamaCppToolchainError(f"llama.cpp source contains a non-regular file: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise HfLlamaCppToolchainError(f"llama.cpp source contains a non-regular file: {relative}")
        records.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path, f"llama.cpp source {relative}"),
            }
        )
    if not records:
        raise HfLlamaCppToolchainError("llama.cpp source inventory is empty")
    digest = hashlib.sha256()
    for record in records:
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256-path-size-content-v1",
        "tree_sha256": "sha256:" + digest.hexdigest(),
        "file_count": len(records),
        "total_bytes": sum(int(record["bytes"]) for record in records),
    }


def _run_git(source: Path, arguments: list[str], label: str) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={source}", *arguments],
            cwd=source,
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HfLlamaCppToolchainError(f"{label} failed: {error}") from error
    if completed.returncode != 0 or completed.stderr.strip():
        detail = completed.stderr[-4096:].decode("utf-8", errors="replace").strip()
        raise HfLlamaCppToolchainError(f"{label} failed: {detail}")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _verify_source_checkout(
    source_root: str | Path,
    *,
    require_clean_status: bool = True,
) -> dict[str, object]:
    source = Path(source_root).expanduser()
    if source.is_symlink():
        raise HfLlamaCppToolchainError("llama.cpp source root must not be a symlink")
    source = source.resolve()
    if not source.is_dir() or not (source / ".git").is_dir():
        raise HfLlamaCppToolchainError("llama.cpp source must be a complete local Git checkout")
    head = _run_git(source, ["rev-parse", "--verify", "HEAD"], "llama.cpp HEAD probe")
    status = (
        _run_git(
            source,
            ["status", "--porcelain=v1", "--untracked-files=all"],
            "llama.cpp clean-tree probe",
        )
        if require_clean_status
        else ""
    )
    origin = _run_git(source, ["remote", "get-url", "origin"], "llama.cpp origin probe")
    normalized_origin = origin.removesuffix(".git").rstrip("/")
    if head != TOOLCHAIN_COMMIT or status or normalized_origin != SOURCE_REPOSITORY:
        raise HfLlamaCppToolchainError("llama.cpp checkout is not the exact clean official b10156 commit")
    _, checkout = _tree_inventory(source)
    payload = _llama_source_inventory(source)
    return {
        "repository": SOURCE_REPOSITORY,
        "commit": TOOLCHAIN_COMMIT,
        "version": TOOLCHAIN_VERSION,
        "checkout_inventory": checkout,
        "source_inventory": payload,
    }


def verify_llama_cpp_source_checkout(
    source_root: str | Path,
) -> dict[str, object]:
    """Verify and inventory the exact clean official b10156 source checkout."""

    return _verify_source_checkout(source_root)


def _strict_copy_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise HfLlamaCppToolchainError(f"refusing to overwrite copied tree: {destination}")
    _tree_inventory(source)
    shutil.copytree(source, destination, symlinks=False)
    before = _tree_inventory(source)[1]
    after = _tree_inventory(destination)[1]
    if before != after:
        shutil.rmtree(destination, ignore_errors=True)
        raise HfLlamaCppToolchainError("copied tree differs from its source")


def _private_bucket_uri(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith(_PRIVATE_BUCKET_PREFIX):
        raise HfLlamaCppToolchainError(f"{label} must be an exact private HF bucket URI")
    remainder = value.removeprefix("hf://buckets/")
    parts = remainder.split("/")
    if (
        len(parts) < 2
        or any(not part for part in parts)
        or any(part in {".", ".."} for part in parts)
        or any(_BUCKET_COMPONENT.fullmatch(part) is None for part in parts)
        or any(character in value for character in ("?", "#", "\\", "%", "\n", "\r", "\t"))
    ):
        raise HfLlamaCppToolchainError(
            f"{label} must be a canonical private HF bucket URI"
        )
    canonical = "hf://buckets/" + "/".join(parts)
    if canonical != value:
        raise HfLlamaCppToolchainError(
            f"{label} must be a canonical private HF bucket URI"
        )
    return canonical


def canonical_private_bucket_uri(value: object, label: str) -> str:
    """Return one canonical private bucket URI or fail on every path alias."""

    return _private_bucket_uri(value, label)


def _bucket_uri_parts(value: str, label: str) -> tuple[str, tuple[str, ...]]:
    uri = _private_bucket_uri(value, label)
    remainder = uri.removeprefix("hf://buckets/")
    parts = tuple(remainder.split("/"))
    if len(parts) < 2:
        raise HfLlamaCppToolchainError(f"{label} must name a bucket")
    return "/".join(parts[:2]), parts[2:]


def validate_no_writable_read_only_uri_overlap(
    *,
    writable_uri: str,
    read_only_uris: list[str] | tuple[str, ...],
) -> None:
    """Reject equal, ancestor, or descendant aliases in one HF bucket."""

    writable_bucket, writable_path = _bucket_uri_parts(writable_uri, "writable output URI")
    if not writable_path:
        raise HfLlamaCppToolchainError("writable output URI must be a narrow bucket prefix")
    for index, read_only_uri in enumerate(read_only_uris):
        read_bucket, read_path = _bucket_uri_parts(
            read_only_uri,
            f"read-only URI {index}",
        )
        if read_bucket != writable_bucket:
            continue
        common_length = min(len(writable_path), len(read_path))
        if writable_path[:common_length] == read_path[:common_length]:
            raise HfLlamaCppToolchainError(
                "writable output URI overlaps a read-only input URI"
            )


def _offline_requirements_payload() -> bytes:
    lines = [
        f"{requirement} --hash={digest}"
        for _filename, _size, digest, requirement in TOOLCHAIN_OFFLINE_WHEELS
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _verify_offline_wheelhouse(root: str | Path) -> dict[str, object]:
    wheelhouse = Path(root).expanduser().resolve()
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise HfLlamaCppToolchainError("offline wheelhouse must be a regular directory")
    expected_names = {record[0] for record in TOOLCHAIN_OFFLINE_WHEELS}
    actual_names = {
        path.name
        for path in wheelhouse.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if actual_names != expected_names or any(
        path.is_dir() or path.is_symlink()
        for path in wheelhouse.iterdir()
    ):
        raise HfLlamaCppToolchainError("offline wheelhouse file set differs from its lock")
    records: list[dict[str, object]] = []
    for filename, size, digest, requirement in TOOLCHAIN_OFFLINE_WHEELS:
        path = wheelhouse / filename
        if path.stat().st_size != size or _sha256_file(path, filename) != digest:
            raise HfLlamaCppToolchainError(f"offline wheel differs from its lock: {filename}")
        records.append(
            {
                "filename": filename,
                "bytes": size,
                "sha256": digest,
                "requirement": requirement,
            }
        )
    requirements = _offline_requirements_payload()
    return {
        "mode": "offline_no_index_require_hashes",
        "requirements_sha256": _sha256(requirements),
        "wheels": records,
    }


def _rounded_cost(rate: Decimal, billed_minutes: int) -> Decimal:
    return (rate * Decimal(billed_minutes) / Decimal(60)).quantize(
        Decimal("0.000001"),
        rounding=ROUND_HALF_UP,
    )


def _toolchain_cost_binding(flavor: str) -> tuple[Decimal, Decimal]:
    if flavor != TOOLCHAIN_FLAVOR:
        raise HfLlamaCppToolchainError(
            "the v19 toolchain contract permits only cpu-basic"
        )
    rate = TOOLCHAIN_HOURLY_RATES_USD[TOOLCHAIN_FLAVOR]
    return rate, _rounded_cost(rate, TOOLCHAIN_TIMEOUT_MINUTES)


def _verify_cost_freshness(
    retrieved_at_utc: str,
    *,
    now_utc: datetime | None = None,
) -> dict[str, str]:
    if _UTC_TIMESTAMP.fullmatch(retrieved_at_utc) is None:
        raise HfLlamaCppToolchainError("HF actual-cost evidence timestamp is invalid")
    retrieved = datetime.strptime(retrieved_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        raise HfLlamaCppToolchainError("HF cost freshness clock must be timezone-aware")
    now = now.astimezone(UTC)
    age = (now - retrieved).total_seconds()
    if age < -ACTUAL_COST_FUTURE_SKEW_SECONDS or age > ACTUAL_COST_MAX_AGE_SECONDS:
        raise HfLlamaCppToolchainError("HF actual-cost evidence is stale or from the future")
    expires = retrieved + timedelta(seconds=ACTUAL_COST_MAX_AGE_SECONDS)
    return {
        "retrieved_at_utc": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_hf_toolchain_cost_guard(
    evidence: Mapping[str, object],
    *,
    evidence_sha256: str,
    flavor: str = TOOLCHAIN_FLAVOR,
) -> dict[str, object]:
    """Canonically account for terminal campaign jobs before reserving this GPU job."""

    actual = dict(_mapping(evidence, "HF actual-cost evidence"))
    bound_hash = _prefixed_hash(evidence_sha256, "HF actual-cost evidence hash")
    if _sha256(_canonical_bytes(actual)) != bound_hash:
        raise HfLlamaCppToolchainError("HF actual-cost evidence hash differs")
    _validate_schema(actual, validator=_cost_validator(), label="HF actual-cost evidence")
    attempts = actual.get("final_evaluation_attempts")
    auxiliary = actual.get("auxiliary_jobs")
    if not isinstance(attempts, list) or not isinstance(auxiliary, list):
        raise HfLlamaCppToolchainError("HF actual-cost evidence has incomplete terminal job lists")
    seen_job_ids: set[str] = set()
    for record in auxiliary:
        row = _mapping(record, "terminal HF cost job")
        job_id = str(row.get("job_id", ""))
        seconds = row.get("running_seconds")
        billed = row.get("billed_minutes")
        if (
            job_id in seen_job_ids
            or isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or isinstance(billed, bool)
            or not isinstance(billed, int)
            or billed != (seconds + 59) // 60
        ):
            raise HfLlamaCppToolchainError("HF terminal jobs must be unique and use exact minute billing")
        seen_job_ids.add(job_id)
        rate = Decimal(str(row.get("hourly_rate_usd", "")))
        if Decimal(str(row.get("actual_cost_usd", ""))) != _rounded_cost(rate, billed):
            raise HfLlamaCppToolchainError("HF terminal job cost differs from exact minute billing")
    observed_attempt_cost = Decimal("0")
    reserved_unknown_cost = Decimal("0")
    accounted_attempt_cost = Decimal("0")
    for record in attempts:
        row = _mapping(record, "terminal final-evaluation cost job")
        job_id = str(row.get("job_id", ""))
        if job_id in seen_job_ids:
            raise HfLlamaCppToolchainError("HF terminal jobs must use unique job IDs")
        seen_job_ids.add(job_id)
        rate = Decimal(str(row.get("hourly_rate_usd", "")))
        seconds = row.get("running_seconds")
        billed = row.get("billed_minutes")
        actual_cost = row.get("actual_cost_usd")
        reserved_minutes = row.get("reserved_minutes")
        reserved_cost = Decimal(str(row.get("reserved_cost_usd", "")))
        accounted_cost = Decimal(str(row.get("accounted_cost_usd", "")))
        if row.get("cost_basis") == "observed_minute_billing":
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or isinstance(billed, bool)
                or not isinstance(billed, int)
                or billed != (seconds + 59) // 60
                or actual_cost is None
                or reserved_minutes != 0
                or reserved_cost != Decimal("0")
            ):
                raise HfLlamaCppToolchainError(
                    "observed HF evaluation cost must use exact minute billing without a reservation"
                )
            observed = Decimal(str(actual_cost))
            if observed != _rounded_cost(rate, billed) or accounted_cost != observed:
                raise HfLlamaCppToolchainError("observed HF evaluation cost differs from exact minute billing")
            observed_attempt_cost += observed
        elif row.get("cost_basis") == "full_timeout_reservation":
            timeout = row.get("timeout_minutes")
            if (
                seconds is not None
                or billed is not None
                or actual_cost is not None
                or row.get("terminal_duration_available") is not False
                or isinstance(timeout, bool)
                or not isinstance(timeout, int)
                or reserved_minutes != timeout
                or reserved_cost != _rounded_cost(rate, timeout)
                or accounted_cost != reserved_cost
            ):
                raise HfLlamaCppToolchainError(
                    "duration-unavailable HF evaluation cost must retain its full-timeout reservation"
                )
            reserved_unknown_cost += reserved_cost
        else:
            raise HfLlamaCppToolchainError("HF evaluation cost basis is invalid")
        accounted_attempt_cost += accounted_cost
    completed_results = [
        str(row["result_sha256"])
        for row in attempts
        if row.get("terminal_status") == "COMPLETED"
    ]
    if not completed_results:
        raise HfLlamaCppToolchainError(
            "HF actual-cost evidence must contain a completed final evaluation"
        )
    if any(
        row.get("role") == "toolchain_materialization" and row.get("terminal_status") == "COMPLETED"
        for row in auxiliary
    ):
        raise HfLlamaCppToolchainError("a completed toolchain materialization cannot be launched again")
    anchor = _mapping(actual.get("cost_anchor"), "HF cost anchor")
    if actual.get("cost_anchor_sha256") != _sha256(_canonical_bytes(anchor)):
        raise HfLlamaCppToolchainError("HF cost anchor hash differs")
    campaign_actual = Decimal(str(anchor.get("actual_before_first_final_evaluation_usd", "")))
    campaign_actual += observed_attempt_cost
    auxiliary_actual = sum(
        (Decimal(str(row["actual_cost_usd"])) for row in auxiliary),
        Decimal("0"),
    )
    campaign_actual += auxiliary_actual
    accounted_campaign = campaign_actual + reserved_unknown_cost
    toolchain_rate, toolchain_maximum = _toolchain_cost_binding(flavor)
    reconstructed_post_authorization_actual = sum(
        (
            Decimal(str(row["actual_cost_usd"]))
            for row in auxiliary
            if row.get("role") in POST_AUTHORIZATION_TOOLCHAIN_ROLES
        ),
        Decimal("0"),
    )
    post_authorization_actual = max(
        reconstructed_post_authorization_actual,
        POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD,
    )
    new_reservation = toolchain_maximum + QUANTIZATION_RESERVED_COST_USD
    maximum_new_spend = post_authorization_actual + new_reservation
    if maximum_new_spend >= REMAINING_HF_CREDITS_AUTHORIZATION_USD:
        raise HfLlamaCppToolchainError(
            "post-authorization toolchain spend plus the new toolchain and "
            "quantization reservation must remain strictly below "
            f"the user-reported ${REMAINING_HF_CREDITS_AUTHORIZATION_USD:.3f} "
            "remaining HF credits"
        )
    chain_jobs = anchor.get("chain_jobs")
    if not isinstance(chain_jobs, list):
        raise HfLlamaCppToolchainError("HF cost anchor has no complete chain-job inventory")
    accounted_job_ids = sorted(
        {
            *(str(row.get("job_id", "")) for row in chain_jobs if isinstance(row, Mapping)),
            *seen_job_ids,
        }
    )
    if (
        len(accounted_job_ids) != len(chain_jobs) + len(seen_job_ids)
        or any(re.fullmatch(r"[0-9a-f]{24}", job_id) is None for job_id in accounted_job_ids)
    ):
        raise HfLlamaCppToolchainError("HF accounted campaign job IDs are incomplete or duplicated")
    accounted_job_ids_sha256 = _sha256(_canonical_bytes(accounted_job_ids))
    return {
        "actual_cost_evidence_sha256": bound_hash,
        "actual_cost_as_of_utc": actual["retrieved_at_utc"],
        "actual_campaign_cost_usd": format(campaign_actual, ".6f"),
        "unknown_cost_reservation_usd": format(reserved_unknown_cost, ".6f"),
        "accounted_campaign_cost_usd": format(accounted_campaign, ".6f"),
        "accounted_campaign_job_ids": accounted_job_ids,
        "accounted_campaign_job_ids_sha256": accounted_job_ids_sha256,
        "budget_scope": "user_reported_remaining_hf_credits",
        "remaining_credit_authorization_usd": format(
            REMAINING_HF_CREDITS_AUTHORIZATION_USD,
            ".3f",
        ),
        "remaining_credit_authorization_sha256": (
            TOTAL_CAMPAIGN_BUDGET_AUTHORIZATION_SHA256
        ),
        "toolchain_flavor": flavor,
        "toolchain_hourly_rate_usd": format(toolchain_rate, ".3f"),
        "toolchain_timeout_minutes": TOOLCHAIN_TIMEOUT_MINUTES,
        "toolchain_maximum_cost_usd": format(toolchain_maximum, ".6f"),
        "quantization_reserved_cost_usd": format(QUANTIZATION_RESERVED_COST_USD, ".3f"),
        "post_authorization_actual_spend_usd": format(
            post_authorization_actual,
            ".6f",
        ),
        "new_toolchain_and_quantization_reservation_usd": format(
            new_reservation,
            ".6f",
        ),
        "maximum_authorized_new_spend_usd": format(maximum_new_spend, ".6f"),
        "remaining_credit_after_reservations_usd": format(
            REMAINING_HF_CREDITS_AUTHORIZATION_USD - maximum_new_spend,
            ".6f",
        ),
        "strictly_below_remaining_credit_authorization": True,
    }


def _oci_binding(cache: Path) -> dict[str, object]:
    try:
        verified = verify_reviewed_llama_cpp_oci_cache(cache)
    except OciBundleError as error:
        raise HfLlamaCppToolchainError(f"reviewed OCI cache verification failed: {error}") from error
    layer_records = []
    purposes = ("native_libraries", "tools_and_conversion")
    for purpose, (digest, size), layer_path in zip(
        purposes,
        SELECTED_LAYERS.items(),
        verified["layer_paths"],
        strict=True,
    ):
        layer_records.append(
            {
                "digest": digest,
                "compressed_bytes": size,
                "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                "purpose": purpose,
                "packet_filename": Path(str(layer_path)).name,
            }
        )
    return {
        "repository": f"ghcr.io/{REPOSITORY}",
        "tag": TAG,
        "index_digest": INDEX_DIGEST,
        "manifest_digest": MANIFEST_DIGEST,
        "config_digest": CONFIG_DIGEST,
        "revision": TOOLCHAIN_COMMIT,
        "platform": {"os": "linux", "architecture": "amd64"},
        "layers": layer_records,
    }


def _build_binding() -> dict[str, object]:
    return {
        "llama_cpp_version": TOOLCHAIN_VERSION,
        "compiler": {"id": COMPILER_ID, "version": COMPILER_VERSION},
        "cuda": {"enabled": True, "version": CUDA_VERSION},
        "generator": BUILD_GENERATOR,
        "configuration": BUILD_CONFIGURATION,
        "flags": list(BUILD_FLAGS),
    }


def reviewed_toolchain_build_binding() -> dict[str, object]:
    """Return the complete reviewed b10156 CUDA build identity."""

    return _build_binding()


@cache
def _manifest_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_MANIFEST_SCHEMA.read_text(encoding="utf-8")))


@cache
def _result_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_RESULT_SCHEMA.read_text(encoding="utf-8")))


@cache
def _cost_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_COST_SCHEMA.read_text(encoding="utf-8")))


def _validate_schema(
    value: Mapping[str, object],
    *,
    validator: Draft202012Validator,
    label: str,
) -> None:
    errors = sorted(
        validator.iter_errors(dict(value)),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise HfLlamaCppToolchainError(f"{label} schema failed at {location}: {errors[0].message}")


def validate_hf_llama_cpp_toolchain_manifest(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Validate the closed b10156 materialization manifest."""

    manifest = dict(_mapping(value, "HF llama.cpp toolchain manifest"))
    _validate_schema(manifest, validator=_manifest_validator(), label="HF llama.cpp toolchain manifest")
    if (
        manifest.get("attempt") != TOOLCHAIN_ATTEMPT
        or not isinstance(manifest.get("source_git_head"), str)
        or _COMMIT.fullmatch(str(manifest["source_git_head"])) is None
        or manifest.get("image") != TOOLCHAIN_IMAGE
        or manifest.get("flavor") != TOOLCHAIN_FLAVOR
        or manifest.get("timeout_minutes") != TOOLCHAIN_TIMEOUT_MINUTES
        or manifest.get("build") != _build_binding()
    ):
        raise HfLlamaCppToolchainError("HF llama.cpp toolchain manifest identity differs")
    source = _mapping(manifest.get("source"), "toolchain source")
    if (
        source.get("repository") != SOURCE_REPOSITORY
        or source.get("commit") != TOOLCHAIN_COMMIT
        or source.get("version") != TOOLCHAIN_VERSION
    ):
        raise HfLlamaCppToolchainError("toolchain source identity differs")
    oci = _mapping(manifest.get("oci"), "toolchain OCI binding")
    if (
        oci.get("repository") != f"ghcr.io/{REPOSITORY}"
        or oci.get("tag") != TAG
        or oci.get("index_digest") != INDEX_DIGEST
        or oci.get("manifest_digest") != MANIFEST_DIGEST
        or oci.get("config_digest") != CONFIG_DIGEST
        or oci.get("revision") != TOOLCHAIN_COMMIT
    ):
        raise HfLlamaCppToolchainError("toolchain OCI identity differs")
    output = _mapping(manifest.get("output"), "toolchain output")
    if output != {
        "bucket_root": TOOLCHAIN_OUTPUT_BUCKET,
        "mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
        "mount_container_path": "/outputs",
        "relative_path": TOOLCHAIN_OUTPUT_RELATIVE,
        "container_path": TOOLCHAIN_OUTPUT_ROOT,
        "staging_container_path": TOOLCHAIN_STAGING_ROOT,
        "remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "unique": True,
        "overwrite_allowed": False,
        "publication_protocol": "owner_token_stage_rename_noreplace_v1",
    }:
        raise HfLlamaCppToolchainError("toolchain output is not the exact fresh private prefix")
    if manifest.get("side_effects") != {
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
        "publication_allowed": False,
        "secrets_required": [],
    }:
        raise HfLlamaCppToolchainError("toolchain materialization side effects are not closed")
    cost_guard = _mapping(manifest.get("cost_guard"), "toolchain cost guard")
    dependencies = _mapping(manifest.get("dependencies"), "toolchain offline dependencies")
    if (
        dependencies.get("mode") != "offline_no_index_require_hashes"
        or dependencies.get("requirements_sha256")
        != _sha256(_offline_requirements_payload())
        or dependencies.get("wheels")
        != [
            {
                "filename": filename,
                "bytes": size,
                "sha256": digest,
                "requirement": requirement,
            }
            for filename, size, digest, requirement in TOOLCHAIN_OFFLINE_WHEELS
        ]
    ):
        raise HfLlamaCppToolchainError(
            "toolchain offline dependency binding differs"
        )
    rate, maximum = _toolchain_cost_binding(str(manifest["flavor"]))
    if (
        cost_guard.get("toolchain_flavor") != manifest["flavor"]
        or cost_guard.get("toolchain_hourly_rate_usd") != format(rate, ".3f")
        or cost_guard.get("toolchain_maximum_cost_usd")
        != format(maximum, ".6f")
    ):
        raise HfLlamaCppToolchainError(
            "toolchain flavor differs from its exact cost guard"
        )
    return manifest


def _git_head() -> str:
    repository = Path(__file__).parents[4]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    head = completed.stdout.strip()
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", REVIEWED_SCRIBER_GIT_HEAD, head],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if (
        completed.returncode != 0
        or completed.stderr.strip()
        or _COMMIT.fullmatch(head) is None
        or ancestry.returncode != 0
        or ancestry.stdout.strip()
        or ancestry.stderr.strip()
    ):
        raise HfLlamaCppToolchainError("cannot bind the Scriber source Git HEAD")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if status.returncode != 0 or status.stderr.strip() or status.stdout.strip():
        raise HfLlamaCppToolchainError(
            "cannot bind a dirty or ambiguous Scriber source checkout"
        )
    return head


def hf_llama_cpp_toolchain_bootstrap_payload(
    packet_root: str | Path = "/input/repo",
    runner_executable: str = "/bin/bash",
) -> str:
    """Return the stdlib-only bootstrap embedded directly in the HF command."""

    raw_root = str(packet_root)
    root = Path(raw_root)
    if not root.is_absolute() and not PurePosixPath(raw_root).is_absolute():
        raise HfLlamaCppToolchainError("trusted bootstrap packet root must be absolute")
    if not runner_executable or "\x00" in runner_executable:
        raise HfLlamaCppToolchainError("trusted bootstrap runner executable is invalid")
    script = r'''import hashlib,json,os,re,subprocess,sys
from pathlib import Path

root=Path(__PACKET_ROOT__).resolve()
expected_tree,expected_runner,expected_manifest,owner=sys.argv[1:5]
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_tree) is None:
    raise SystemExit("trusted bootstrap: invalid expected packet tree")
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_runner) is None:
    raise SystemExit("trusted bootstrap: invalid expected runner hash")
if re.fullmatch(r"sha256:[0-9a-f]{64}",expected_manifest) is None:
    raise SystemExit("trusted bootstrap: invalid expected manifest hash")
if re.fullmatch(r"[0-9a-f]{64}",owner) is None:
    raise SystemExit("trusted bootstrap: invalid launch-claim owner token")

def canonical(value):
    return (json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True,allow_nan=False)+"\n").encode("utf-8")

def file_hash(path):
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block=stream.read(1024*1024)
            if not block:
                break
            digest.update(block)
    return "sha256:"+digest.hexdigest()

manifest_path=root/"job-manifest.json"
payload=manifest_path.read_bytes()
if file_hash(manifest_path) != expected_manifest:
    raise SystemExit("trusted bootstrap: manifest hash differs")
manifest=json.loads(payload)
if payload != canonical(manifest):
    raise SystemExit("trusted bootstrap: manifest is not canonical")
if manifest.get("attempt") != "v19" or manifest.get("flavor") != "cpu-basic" or manifest.get("timeout_minutes") != 20:
    raise SystemExit("trusted bootstrap: v19 cpu-basic identity differs")
if re.fullmatch(r"[0-9a-f]{40}",str(manifest.get("source_git_head", ""))) is None:
    raise SystemExit("trusted bootstrap: bound Scriber commit is invalid")
records=[]
for path in sorted(root.rglob("*"),key=lambda item:item.relative_to(root).as_posix()):
    relative=path.relative_to(root).as_posix()
    if relative == "job-manifest.json":
        continue
    if path.is_symlink():
        raise SystemExit("trusted bootstrap: packet contains a non-regular entry: "+relative)
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit("trusted bootstrap: packet contains a non-regular entry: "+relative)
    records.append({"path":relative,"kind":"file","bytes":path.stat().st_size,"sha256":file_hash(path)})
digest=hashlib.sha256()
for record in records:
    digest.update(canonical(record))
tree="sha256:"+digest.hexdigest()
if tree != expected_tree or manifest.get("packet_tree_sha256") != expected_tree:
    raise SystemExit("trusted bootstrap: packet tree differs")
if manifest.get("file_count") != len(records) or manifest.get("byte_count") != sum(int(row["bytes"]) for row in records):
    raise SystemExit("trusted bootstrap: packet inventory summary differs")
runner=root/"run-hf-llama-cpp-toolchain.sh"
if file_hash(runner) != expected_runner or manifest.get("runner_sha256") != expected_runner:
    raise SystemExit("trusted bootstrap: runner differs")
os.environ["SCRIBER_TRUSTED_PACKET_TREE_SHA256"]=expected_tree
os.environ["SCRIBER_TRUSTED_MANIFEST_SHA256"]=expected_manifest
os.environ["SCRIBER_V19_LAUNCH_CLAIM_OWNER"]=owner
runner_argv=[__RUNNER_EXEC__,str(runner),expected_tree,expected_manifest,owner]
if os.name == "nt":
    raise SystemExit(subprocess.run(runner_argv,env=os.environ,check=False).returncode)
os.execvpe(__RUNNER_EXEC__,runner_argv,os.environ)
'''
    return script.replace("__PACKET_ROOT__", json.dumps(str(root))).replace(
        "__RUNNER_EXEC__",
        json.dumps(runner_executable),
    )


def hf_llama_cpp_toolchain_runner_payload() -> bytes:
    """Return the exact bounded materialization runner."""

    script = """#!/usr/bin/env bash
set -euo pipefail
umask 077

EXPECTED_PACKET_TREE="${1:?expected packet tree is required}"
EXPECTED_MANIFEST_SHA256="${2:?expected manifest hash is required}"
OWNER_TOKEN="${3:?launch-claim owner token is required}"
PACKET_ROOT="/input/repo"
PROJECT_ROOT="$PACKET_ROOT/repo/ml/scriber_polishing"
WORKSPACE_ROOT="/tmp/scriber-llama-cpp-b10156-materialization"
REVIEWED_GIT_BIN="/tmp/scriber-reviewed-git-v19"
VENV_ROOT="/tmp/scriber-llama-cpp-runtime-v19"
OUTPUT_ROOT="__OUTPUT_ROOT__"

test "${SCRIBER_TRUSTED_PACKET_TREE_SHA256:-}" = "$EXPECTED_PACKET_TREE"
test "${SCRIBER_TRUSTED_MANIFEST_SHA256:-}" = "$EXPECTED_MANIFEST_SHA256"
test "${SCRIBER_V19_LAUNCH_CLAIM_OWNER:-}" = "$OWNER_TOKEN"
test ! -e "$WORKSPACE_ROOT"
test ! -e "$OUTPUT_ROOT"
test ! -e "$REVIEWED_GIT_BIN"
test ! -e "$VENV_ROOT"
cd "$PROJECT_ROOT"

unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -I -m venv "$VENV_ROOT"
PYTHON="$VENV_ROOT/bin/python"
"$PYTHON" -I - <<'PY'
import pathlib
import sys

prefix = pathlib.Path(sys.prefix).resolve()
if sys.version_info[:2] != (3, 12) or prefix == pathlib.Path(sys.base_prefix).resolve():
    raise SystemExit("isolated toolchain runtime is not CPython 3.12")
PY

"$PYTHON" -m pip install --isolated --no-cache-dir --no-index \
  --require-hashes \
  --force-reinstall \
  --ignore-installed \
  --no-deps \
  --find-links "$PACKET_ROOT/input/wheels" \
  -r "$PACKET_ROOT/input/offline-requirements.txt"

"$PYTHON" -I - <<'PY'
import importlib.metadata
import pathlib
import sys

expected = {
    "attrs": "24.3.0",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "referencing": "0.36.2",
    "rpds-py": "0.27.1",
    "typing-extensions": "4.15.0",
}
prefix = pathlib.Path(sys.prefix).resolve()
for name, version in expected.items():
    distribution = importlib.metadata.distribution(name)
    location = pathlib.Path(distribution.locate_file("")).resolve()
    if distribution.version != version or not location.is_relative_to(prefix):
        raise SystemExit(
            f"isolated dependency admission failed: {name}=={distribution.version} at {location}"
        )
PY

mkdir "$REVIEWED_GIT_BIN"
cat > "$REVIEWED_GIT_BIN/git" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$PROJECT_ROOT/scripts/reviewed_git_probe.py" "\\$@"
EOF
chmod 700 "$REVIEWED_GIT_BIN/git"
export PATH="$REVIEWED_GIT_BIN:$PATH"
export SCRIBER_EXPECTED_LLAMA_SOURCE_TREE_SHA256="$(
  "$PYTHON" -I -c 'import json; print(json.load(open("/input/repo/job-manifest.json", encoding="utf-8"))["source"]["source_inventory"]["tree_sha256"])'
)"

CUDA_RUNTIME_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cuda_runtime/lib"
CUDA_RUNTIME_LIBRARY="$CUDA_RUNTIME_LIBRARY_PATH/libcudart.so.12"
test -f "$CUDA_RUNTIME_LIBRARY"
CUDA_RUNTIME_RESOLVED="$(readlink -f "$CUDA_RUNTIME_LIBRARY")"
test "$(dirname "$CUDA_RUNTIME_RESOLVED")" = "$CUDA_RUNTIME_LIBRARY_PATH"
CUBLAS_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/cublas/lib"
CUBLAS_LIBRARY="$CUBLAS_LIBRARY_PATH/libcublas.so.12"
test -f "$CUBLAS_LIBRARY"
CUBLAS_RESOLVED="$(readlink -f "$CUBLAS_LIBRARY")"
test "$(dirname "$CUBLAS_RESOLVED")" = "$CUBLAS_LIBRARY_PATH"
NCCL_LIBRARY_PATH="/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib"
NCCL_LIBRARY="$NCCL_LIBRARY_PATH/libnccl.so.2"
test -f "$NCCL_LIBRARY"
NCCL_RESOLVED="$(readlink -f "$NCCL_LIBRARY")"
test "$(dirname "$NCCL_RESOLVED")" = "$NCCL_LIBRARY_PATH"
export LD_LIBRARY_PATH="$CUDA_RUNTIME_LIBRARY_PATH:$CUBLAS_LIBRARY_PATH:$NCCL_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

"$PYTHON" scripts/verify_hf_llama_cpp_toolchain.py \
  --mode materialize \
  --packet-root "$PACKET_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE" \
  --owner-token "$OWNER_TOKEN" \
  --workspace-root "$WORKSPACE_ROOT" \
  --output-root "$OUTPUT_ROOT"

"$PYTHON" scripts/verify_hf_llama_cpp_toolchain.py \
  --mode result \
  --bundle-root "$OUTPUT_ROOT" \
  --expected-packet-tree-sha256 "$EXPECTED_PACKET_TREE"

echo "llama_cpp_toolchain=complete version=b10156 training_steps_executed=0" >&2
"""
    return script.replace("__OUTPUT_ROOT__", TOOLCHAIN_OUTPUT_ROOT).replace("\r\n", "\n").encode("utf-8")


def build_hf_llama_cpp_toolchain_packet(
    *,
    source_root: str | Path,
    oci_cache_dir: str | Path,
    actual_cost_evidence_path: str | Path,
    offline_wheelhouse_dir: str | Path,
    output_dir: str | Path,
    flavor: str = TOOLCHAIN_FLAVOR,
) -> HfLlamaCppToolchainPacket:
    """Create, but never upload or execute, one immutable materialization packet."""

    source = Path(source_root).expanduser().resolve()
    oci_cache = Path(oci_cache_dir).expanduser().resolve()
    wheelhouse = Path(offline_wheelhouse_dir).expanduser().resolve()
    dependency_binding = _verify_offline_wheelhouse(wheelhouse)
    cost_evidence, cost_payload = _read_json_object(
        actual_cost_evidence_path,
        "HF actual-cost evidence",
    )
    cost_guard = build_hf_toolchain_cost_guard(
        cost_evidence,
        evidence_sha256=_sha256(cost_payload),
        flavor=flavor,
    )
    _verify_cost_freshness(str(cost_guard["actual_cost_as_of_utc"]))
    source_binding = _verify_source_checkout(source)
    oci_binding = _oci_binding(oci_cache)
    target = Path(output_dir).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise HfLlamaCppToolchainError(f"refusing to overwrite toolchain packet: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        packet_source = temporary / "input/source"
        _strict_copy_tree(source, packet_source)
        copied_source = _verify_source_checkout(packet_source)
        if copied_source != source_binding:
            raise HfLlamaCppToolchainError("packet source differs from its initial clean-checkout binding")
        packet_oci = temporary / "input/oci"
        packet_oci.mkdir(parents=True)
        for name in ("index.json", "manifest-amd64.json", "config.json"):
            shutil.copy2(oci_cache / name, packet_oci / name)
        for record in oci_binding["layers"]:
            filename = str(record["packet_filename"])
            shutil.copy2(oci_cache / filename, packet_oci / filename)
        if _oci_binding(packet_oci) != oci_binding:
            raise HfLlamaCppToolchainError("packet OCI cache differs from its reviewed binding")
        cost_target = temporary / "input/hf-actual-cost-evidence.json"
        cost_target.write_bytes(cost_payload)
        packet_wheels = temporary / "input/wheels"
        packet_wheels.mkdir()
        for filename, _size, _digest, _requirement in TOOLCHAIN_OFFLINE_WHEELS:
            shutil.copy2(wheelhouse / filename, packet_wheels / filename)
        (temporary / "input/offline-requirements.txt").write_bytes(
            _offline_requirements_payload()
        )
        if _verify_offline_wheelhouse(packet_wheels) != dependency_binding:
            raise HfLlamaCppToolchainError(
                "packet offline wheelhouse differs from its lock"
            )
        project = temporary / "repo/ml/scriber_polishing"
        (project / "src/scriber_polishing").mkdir(parents=True)
        (project / "scripts").mkdir()
        (project / "contracts").mkdir()
        for name in (
            "__init__.py",
            "hf_llama_cpp_toolchain.py",
            "llama_cpp_bundle.py",
            "oci_llama_cpp_bundle.py",
        ):
            shutil.copy2(_PROJECT_ROOT / "src/scriber_polishing" / name, project / "src/scriber_polishing" / name)
        for name in (
            "materialize_llama_cpp_bundle_lock.py",
            "reviewed_git_probe.py",
            "verify_hf_llama_cpp_toolchain.py",
        ):
            shutil.copy2(_PROJECT_ROOT / "scripts" / name, project / "scripts" / name)
        for name in (
            "hf_actual_cost_evidence_schema.json",
            "hf_llama_cpp_toolchain_job_manifest_schema.json",
            "hf_llama_cpp_toolchain_result_schema.json",
            "llama_cpp_bundle_lock_schema.json",
        ):
            shutil.copy2(_PROJECT_ROOT / "contracts" / name, project / "contracts" / name)
        runner = hf_llama_cpp_toolchain_runner_payload()
        (temporary / "run-hf-llama-cpp-toolchain.sh").write_bytes(runner)
        _, packet_inventory = _tree_inventory(temporary, excluded=_PACKET_EXCLUDED)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_llama_cpp_toolchain_job",
            "attempt": TOOLCHAIN_ATTEMPT,
            "source_git_head": _git_head(),
            "image": TOOLCHAIN_IMAGE,
            "flavor": flavor,
            "timeout_minutes": TOOLCHAIN_TIMEOUT_MINUTES,
            "source": source_binding,
            "oci": oci_binding,
            "build": _build_binding(),
            "dependencies": dependency_binding,
            "cost_guard": cost_guard,
            "output": {
                "bucket_root": TOOLCHAIN_OUTPUT_BUCKET,
                "mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
                "mount_container_path": "/outputs",
                "relative_path": TOOLCHAIN_OUTPUT_RELATIVE,
                "container_path": TOOLCHAIN_OUTPUT_ROOT,
                "staging_container_path": TOOLCHAIN_STAGING_ROOT,
                "remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
                "unique": True,
                "overwrite_allowed": False,
                "publication_protocol": "owner_token_stage_rename_noreplace_v1",
            },
            "side_effects": {
                "training_steps_executed": 0,
                "evaluation_reports_written": 0,
                "prediction_records_written": 0,
                "publication_allowed": False,
                "secrets_required": [],
            },
            "runner_sha256": _sha256(runner),
            "packet_tree_sha256": packet_inventory["tree_sha256"],
            "file_count": packet_inventory["entry_count"],
            "byte_count": packet_inventory["total_file_bytes"],
        }
        validate_hf_llama_cpp_toolchain_manifest(manifest)
        (temporary / "job-manifest.json").write_bytes(_canonical_bytes(manifest))
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verify_hf_llama_cpp_toolchain_packet(
        target,
        expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
    )
    return HfLlamaCppToolchainPacket(output_dir=target, manifest=manifest)


def verify_hf_llama_cpp_toolchain_packet(
    packet_root: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str | None = None,
    require_fresh_actual_cost: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Verify every packet byte and its exact source, OCI, runner, and cost bindings."""

    root = Path(packet_root).expanduser().resolve()
    manifest, manifest_payload = _read_json_object(root / "job-manifest.json", "toolchain job manifest")
    if manifest_payload != _canonical_bytes(manifest):
        raise HfLlamaCppToolchainError("toolchain job manifest is not canonical JSON")
    if (
        expected_manifest_sha256 is not None
        and _sha256(manifest_payload)
        != _prefixed_hash(expected_manifest_sha256, "toolchain manifest")
    ):
        raise HfLlamaCppToolchainError("toolchain job manifest hash differs")
    manifest = validate_hf_llama_cpp_toolchain_manifest(manifest)
    _, inventory = _tree_inventory(root, excluded=_PACKET_EXCLUDED)
    expected_tree = _prefixed_hash(expected_packet_tree_sha256, "expected packet tree")
    if (
        inventory["tree_sha256"] != expected_tree
        or manifest.get("packet_tree_sha256") != expected_tree
        or manifest.get("file_count") != inventory["entry_count"]
        or manifest.get("byte_count") != inventory["total_file_bytes"]
    ):
        raise HfLlamaCppToolchainError("toolchain packet differs from its exact launch binding")
    runner = root / "run-hf-llama-cpp-toolchain.sh"
    if manifest.get("runner_sha256") != _sha256_file(runner, "toolchain runner"):
        raise HfLlamaCppToolchainError("toolchain runner hash differs")
    # Packet creation already required a clean checkout. Remote verification
    # compares the complete checkout and payload inventories byte-for-byte;
    # repeating `git status` over a bucket mount adds no integrity signal and
    # can exceed the bounded job runtime because every path becomes a network
    # metadata lookup.
    source_binding = _verify_source_checkout(
        root / "input/source",
        require_clean_status=False,
    )
    if source_binding != manifest.get("source"):
        raise HfLlamaCppToolchainError("packet source differs from its manifest")
    if _oci_binding(root / "input/oci") != manifest.get("oci"):
        raise HfLlamaCppToolchainError("packet OCI cache differs from its manifest")
    dependency_binding = _verify_offline_wheelhouse(root / "input/wheels")
    requirements = root / "input/offline-requirements.txt"
    if (
        requirements.is_symlink()
        or not requirements.is_file()
        or requirements.read_bytes() != _offline_requirements_payload()
        or dependency_binding != manifest.get("dependencies")
        or dependency_binding.get("requirements_sha256")
        != _sha256_file(requirements, "offline requirements")
    ):
        raise HfLlamaCppToolchainError(
            "packet offline dependencies differ from the exact lock"
        )
    evidence, evidence_payload = _read_json_object(
        root / "input/hf-actual-cost-evidence.json",
        "packet HF actual-cost evidence",
    )
    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_sha256(evidence_payload),
        flavor=str(manifest["flavor"]),
    )
    if guard != manifest.get("cost_guard"):
        raise HfLlamaCppToolchainError("packet cost guard differs from its evidence")
    if require_fresh_actual_cost:
        _verify_cost_freshness(str(guard["actual_cost_as_of_utc"]), now_utc=now_utc)
    return manifest


def build_hf_llama_cpp_toolchain_launch_contract(
    packet_dir: str | Path,
    *,
    remote_packet_uri: str,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Build only the argv for the bounded private GPU materialization job."""

    packet = Path(packet_dir).expanduser().resolve()
    manifest, manifest_payload = _read_json_object(packet / "job-manifest.json", "toolchain job manifest")
    tree = str(manifest.get("packet_tree_sha256", ""))
    manifest_sha256 = _sha256(manifest_payload)
    verified = verify_hf_llama_cpp_toolchain_packet(
        packet,
        expected_packet_tree_sha256=tree,
        expected_manifest_sha256=manifest_sha256,
        require_fresh_actual_cost=True,
        now_utc=now_utc,
    )
    packet_uri = _private_bucket_uri(remote_packet_uri, "toolchain packet")
    validate_no_writable_read_only_uri_overlap(
        writable_uri=TOOLCHAIN_OUTPUT_MOUNT_URI,
        read_only_uris=(packet_uri,),
    )
    guard = _mapping(verified["cost_guard"], "toolchain cost guard")
    freshness = _verify_cost_freshness(str(guard["actual_cost_as_of_utc"]), now_utc=now_utc)
    packet_volume = f"{packet_uri}:/input/repo:ro"
    output_volume = f"{TOOLCHAIN_OUTPUT_MOUNT_URI}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        TOOLCHAIN_JOB_NAME,
        "--flavor",
        str(verified["flavor"]),
        "--timeout",
        f"{TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=llama-cpp-b10156-toolchain",
        "--label",
        TOOLCHAIN_CAMPAIGN_LABEL,
        "--label",
        TOOLCHAIN_ROLE_LABEL,
        "--label",
        TOOLCHAIN_ATTEMPT_LABEL,
        "--label",
        f"job-timeout={TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        f"output-prefix={TOOLCHAIN_OUTPUT_MOUNT_RELATIVE}",
        "--label",
        f"launch-claim={TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        output_volume,
        TOOLCHAIN_IMAGE,
        "python",
        "-I",
        "-c",
        hf_llama_cpp_toolchain_bootstrap_payload(),
        tree,
        str(verified["runner_sha256"]),
        manifest_sha256,
        TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_launch_contract",
        "flavor": verified["flavor"],
        "timeout": f"{TOOLCHAIN_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": guard["toolchain_maximum_cost_usd"],
        "maximum_authorized_new_spend_usd": guard[
            "maximum_authorized_new_spend_usd"
        ],
        "post_authorization_actual_spend_usd": guard[
            "post_authorization_actual_spend_usd"
        ],
        "fresh_campaign_inventory_required": True,
        "actual_cost_evidence_sha256": guard["actual_cost_evidence_sha256"],
        "accounted_campaign_job_ids": guard["accounted_campaign_job_ids"],
        "accounted_campaign_job_ids_sha256": guard[
            "accounted_campaign_job_ids_sha256"
        ],
        "actual_cost_evidence_expires_at_utc": freshness["expires_at_utc"],
        "packet_tree_sha256": tree,
        "runner_sha256": verified["runner_sha256"],
        "manifest_sha256": manifest_sha256,
        "source_git_head": str(verified["source_git_head"]),
        "image": TOOLCHAIN_IMAGE,
        "packet_remote_uri": packet_uri,
        "packet_volume": packet_volume,
        "output_volume": output_volume,
        "output_mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
        "output_root": TOOLCHAIN_OUTPUT_ROOT,
        "staging_root": TOOLCHAIN_STAGING_ROOT,
        "remote_result_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "remote_output_absence_required": True,
        "launch_claim_placeholder": TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
        "attempt": TOOLCHAIN_ATTEMPT,
        "argv": argv,
        "external_job_started": False,
        "publication_allowed": False,
        "secret_names": [],
    }


def build_refreshed_hf_llama_cpp_toolchain_launch_contract(
    packet_dir: str | Path,
    *,
    actual_cost_evidence_path: str | Path,
    remote_packet_uri: str,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Refresh only volatile launch authorization for an immutable uploaded packet.

    The compute packet and its tree hash remain unchanged. A new canonical cost
    snapshot may only preserve or increase every relevant accounted amount, so
    stale authorization can never be refreshed by omitting an earlier job.
    """

    packet = Path(packet_dir).expanduser().resolve()
    manifest, _ = _read_json_object(packet / "job-manifest.json", "toolchain job manifest")
    tree = str(manifest.get("packet_tree_sha256", ""))
    manifest_sha256 = _sha256(_canonical_bytes(manifest))
    verified = verify_hf_llama_cpp_toolchain_packet(
        packet,
        expected_packet_tree_sha256=tree,
        expected_manifest_sha256=manifest_sha256,
        require_fresh_actual_cost=False,
    )
    packet_guard = _mapping(verified["cost_guard"], "packet toolchain cost guard")
    evidence, evidence_payload = _read_json_object(
        Path(actual_cost_evidence_path).expanduser().resolve(),
        "refreshed HF actual-cost evidence",
    )
    guard = build_hf_toolchain_cost_guard(
        evidence,
        evidence_sha256=_sha256(evidence_payload),
        flavor=str(verified["flavor"]),
    )
    for field in (
        "actual_campaign_cost_usd",
        "accounted_campaign_cost_usd",
        "post_authorization_actual_spend_usd",
        "maximum_authorized_new_spend_usd",
    ):
        if Decimal(str(guard[field])) < Decimal(str(packet_guard[field])):
            raise HfLlamaCppToolchainError(
                f"refreshed launch cost guard rolls back packet field {field}"
            )
    packet_job_ids = packet_guard.get("accounted_campaign_job_ids")
    refreshed_job_ids = guard.get("accounted_campaign_job_ids")
    if (
        not isinstance(packet_job_ids, list)
        or not isinstance(refreshed_job_ids, list)
        or not set(packet_job_ids).issubset(refreshed_job_ids)
    ):
        raise HfLlamaCppToolchainError(
            "refreshed launch cost guard omits an earlier accounted campaign job"
        )
    freshness = _verify_cost_freshness(
        str(guard["actual_cost_as_of_utc"]),
        now_utc=now_utc,
    )
    packet_uri = _private_bucket_uri(remote_packet_uri, "toolchain packet")
    validate_no_writable_read_only_uri_overlap(
        writable_uri=TOOLCHAIN_OUTPUT_MOUNT_URI,
        read_only_uris=(packet_uri,),
    )
    packet_volume = f"{packet_uri}:/input/repo:ro"
    output_volume = f"{TOOLCHAIN_OUTPUT_MOUNT_URI}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        TOOLCHAIN_JOB_NAME,
        "--flavor",
        str(verified["flavor"]),
        "--timeout",
        f"{TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=llama-cpp-b10156-toolchain",
        "--label",
        TOOLCHAIN_CAMPAIGN_LABEL,
        "--label",
        TOOLCHAIN_ROLE_LABEL,
        "--label",
        TOOLCHAIN_ATTEMPT_LABEL,
        "--label",
        f"job-timeout={TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        f"output-prefix={TOOLCHAIN_OUTPUT_MOUNT_RELATIVE}",
        "--label",
        f"launch-claim={TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        output_volume,
        TOOLCHAIN_IMAGE,
        "python",
        "-I",
        "-c",
        hf_llama_cpp_toolchain_bootstrap_payload(),
        tree,
        str(verified["runner_sha256"]),
        manifest_sha256,
        TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_launch_contract",
        "flavor": verified["flavor"],
        "timeout": f"{TOOLCHAIN_TIMEOUT_MINUTES}m",
        "maximum_job_cost_usd": guard["toolchain_maximum_cost_usd"],
        "maximum_authorized_new_spend_usd": guard[
            "maximum_authorized_new_spend_usd"
        ],
        "post_authorization_actual_spend_usd": guard[
            "post_authorization_actual_spend_usd"
        ],
        "fresh_campaign_inventory_required": True,
        "actual_cost_evidence_sha256": guard["actual_cost_evidence_sha256"],
        "accounted_campaign_job_ids": guard["accounted_campaign_job_ids"],
        "accounted_campaign_job_ids_sha256": guard[
            "accounted_campaign_job_ids_sha256"
        ],
        "packet_actual_cost_evidence_sha256": packet_guard[
            "actual_cost_evidence_sha256"
        ],
        "actual_cost_evidence_expires_at_utc": freshness["expires_at_utc"],
        "launch_cost_guard_refreshed": True,
        "packet_tree_sha256": tree,
        "runner_sha256": verified["runner_sha256"],
        "manifest_sha256": manifest_sha256,
        "source_git_head": str(verified["source_git_head"]),
        "image": TOOLCHAIN_IMAGE,
        "packet_remote_uri": packet_uri,
        "packet_volume": packet_volume,
        "output_volume": output_volume,
        "output_mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
        "output_root": TOOLCHAIN_OUTPUT_ROOT,
        "staging_root": TOOLCHAIN_STAGING_ROOT,
        "remote_result_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "remote_output_absence_required": True,
        "launch_claim_placeholder": TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
        "attempt": TOOLCHAIN_ATTEMPT,
        "argv": argv,
        "external_job_started": False,
        "publication_allowed": False,
        "secret_names": [],
    }


def _tool_hashes(bundle: VerifiedLlamaCppBundle) -> dict[str, str]:
    return {
        name: _sha256_file(path, f"llama.cpp tool {name}")
        for name, path in sorted(
            {
                "convert_hf_to_gguf": bundle.convert_hf_to_gguf,
                "llama_cli": bundle.llama_cli,
                "llama_quantize": bundle.llama_quantize,
                "llama_server": bundle.llama_server,
            }.items()
        )
    }


def _result_from_bundle(
    bundle: VerifiedLlamaCppBundle,
    *,
    packet_tree_sha256: str,
    payload_inventory: Mapping[str, object],
    closure: Mapping[str, object],
    flavor: str,
) -> dict[str, object]:
    native_runtime_tree = _prefixed_hash(
        bundle.native_runtime.get("tree_sha256"),
        "native runtime tree",
    )
    closure_hash = _prefixed_hash(closure.get("ldd_closure_sha256"), "ldd closure")
    locked_closure = _mapping(
        bundle.native_runtime.get("ldd_closure"),
        "locked ldd closure",
    )
    required_host_drivers = locked_closure.get(
        "required_host_driver_sonames"
    )
    reported_required = closure.get("required_host_driver_sonames")
    reported_resolution = closure.get("host_driver_resolution")
    if (
        required_host_drivers != list(CUDA_HOST_DRIVER_SONAMES)
        or (
            reported_required is not None
            and reported_required != required_host_drivers
        )
        or (
            reported_resolution is not None
            and reported_resolution not in {"symbolic", "live"}
        )
    ):
        raise HfLlamaCppToolchainError(
            "llama.cpp closure does not bind the exact CUDA host-driver requirement"
        )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_result",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "complete",
        "platform": "linux_x86_64_cuda",
        "image": TOOLCHAIN_IMAGE,
        "flavor": flavor,
        "timeout_minutes": TOOLCHAIN_TIMEOUT_MINUTES,
        "packet_tree_sha256": _prefixed_hash(packet_tree_sha256, "packet tree"),
        "bundle_payload_inventory": dict(payload_inventory),
        "lock_filename": TOOLCHAIN_LOCK_FILENAME,
        "lock_sha256": bundle.lock_sha256,
        "commit": bundle.commit,
        "source_tree_sha256": bundle.source_tree_sha256,
        "build_identity_sha256": bundle.build_identity_sha256,
        "build": bundle.build,
        "distribution": bundle.distribution,
        "native_runtime_tree_sha256": native_runtime_tree,
        "ldd_closure_sha256": closure_hash,
        "required_host_driver_sonames": list(required_host_drivers),
        "runtime_library_directories": [
            path.relative_to(bundle.root).as_posix() for path in bundle.runtime_library_paths
        ],
        "tool_sha256": _tool_hashes(bundle),
        "runtime_closure_verified": True,
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
        "publication_allowed": False,
    }


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically commit one staged directory without replacing a winner."""

    if source.parent.parent != destination.parent:
        raise HfLlamaCppToolchainError(
            "toolchain stage and final output must share one mounted parent"
        )
    if os.name != "posix":
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise HfLlamaCppToolchainError(
                "toolchain final output already exists"
            ) from error
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise HfLlamaCppToolchainError(
            "renameat2(RENAME_NOREPLACE) is unavailable; refusing a non-atomic commit"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise HfLlamaCppToolchainError(
            "toolchain final output already exists"
        )
    raise HfLlamaCppToolchainError(
        "atomic toolchain output commit failed: "
        f"{os.strerror(error_number)} ({error_number})"
    )


def _publish_toolchain_no_clobber(
    staged: Path,
    output: Path,
    *,
    expected_packet_tree_sha256: str,
    expected_result: Mapping[str, object],
) -> dict[str, object]:
    if output.exists() or output.is_symlink():
        existing = verify_hf_llama_cpp_toolchain_result(
            output,
            expected_packet_tree_sha256=expected_packet_tree_sha256,
        )
        if existing != dict(expected_result):
            raise HfLlamaCppToolchainError(
                "existing toolchain output belongs to a different result"
            )
        raise HfLlamaCppToolchainError(
            "toolchain output collision preserved the existing verified winner"
        )
    try:
        _rename_directory_no_replace(staged, output)
    except HfLlamaCppToolchainError as error:
        if output.exists() and not output.is_symlink():
            existing = verify_hf_llama_cpp_toolchain_result(
                output,
                expected_packet_tree_sha256=expected_packet_tree_sha256,
            )
            if existing == dict(expected_result):
                raise HfLlamaCppToolchainError(
                    "toolchain publication race preserved the verified winner"
                ) from error
        raise
    verified = verify_hf_llama_cpp_toolchain_result(
        output,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    if verified != dict(expected_result):
        raise HfLlamaCppToolchainError(
            "committed toolchain result differs from the staged result"
        )
    return verified


def materialize_hf_llama_cpp_toolchain(
    packet_root: str | Path,
    *,
    expected_packet_tree_sha256: str,
    owner_token: str,
    workspace_root: str | Path,
    output_root: str | Path,
) -> dict[str, object]:
    """Materialize and publish the exact bundle once inside the dedicated cloud job."""

    packet = Path(packet_root).expanduser().resolve()
    if _OWNER_TOKEN.fullmatch(owner_token) is None:
        raise HfLlamaCppToolchainError("toolchain owner token must be 64 lowercase hex")
    manifest = verify_hf_llama_cpp_toolchain_packet(
        packet,
        expected_packet_tree_sha256=expected_packet_tree_sha256,
    )
    workspace = Path(workspace_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    if workspace.exists() or workspace.is_symlink():
        raise HfLlamaCppToolchainError("toolchain workspace must be absent")
    if output.exists() or output.is_symlink():
        verify_hf_llama_cpp_toolchain_result(
            output,
            expected_packet_tree_sha256=expected_packet_tree_sha256,
        )
        raise HfLlamaCppToolchainError(
            "toolchain output already exists and was preserved"
        )
    stage_parent = output.parent / ".staging"
    stage = stage_parent / (
        expected_packet_tree_sha256.removeprefix("sha256:") + "-" + owner_token
    )
    if stage.exists() or stage.is_symlink():
        raise HfLlamaCppToolchainError("owner-specific toolchain stage already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir()
    bundle_root = workspace / "bundle"
    try:
        source_destination = bundle_root / "source"
        source_destination.parent.mkdir()
        _strict_copy_tree(packet / "input/source", source_destination)
        layers: list[tuple[bytes, str]] = []
        oci = _mapping(manifest["oci"], "toolchain OCI binding")
        for row in oci["layers"]:
            record = _mapping(row, "toolchain OCI layer")
            layer_path = packet / "input/oci" / str(record["packet_filename"])
            if layer_path.is_symlink() or not layer_path.is_file():
                raise HfLlamaCppToolchainError(f"packet OCI layer is missing: {layer_path}")
            payload = layer_path.read_bytes()
            layers.append((payload, str(record["digest"])))
        try:
            extract_verified_oci_layers(layers, bundle_root / "runtime")
        except OciBundleError as error:
            raise HfLlamaCppToolchainError(f"OCI runtime extraction failed: {error}") from error
        runtime_library = (bundle_root / "runtime/app").resolve()
        previous_library_path = os.environ.get("LD_LIBRARY_PATH")
        os.environ["LD_LIBRARY_PATH"] = (
            str(runtime_library) if not previous_library_path else f"{runtime_library}:{previous_library_path}"
        )
        lock = bundle_root / TOOLCHAIN_LOCK_FILENAME
        flavor = str(manifest["flavor"])
        try:
            bundle = materialize_llama_cpp_bundle_lock(
                bundle_root,
                lock,
                expected_commit=TOOLCHAIN_COMMIT,
                llama_cpp_version=TOOLCHAIN_VERSION,
                compiler_id=COMPILER_ID,
                compiler_version=COMPILER_VERSION,
                cuda_enabled=True,
                cuda_version=CUDA_VERSION,
                generator=BUILD_GENERATOR,
                configuration=BUILD_CONFIGURATION,
                flags=list(BUILD_FLAGS),
                oci_repository=f"ghcr.io/{REPOSITORY}",
                oci_tag=TAG,
                oci_index_digest=INDEX_DIGEST,
                oci_manifest_digest=MANIFEST_DIGEST,
                oci_config_digest=CONFIG_DIGEST,
                oci_revision=TOOLCHAIN_COMMIT,
                oci_layers=[
                    {
                        "digest": str(record["digest"]),
                        "compressed_bytes": int(record["compressed_bytes"]),
                        "media_type": str(record["media_type"]),
                        "purpose": str(record["purpose"]),
                    }
                    for record in oci["layers"]
                ],
                runtime_image=TOOLCHAIN_IMAGE,
                native_runtime_root="runtime/app",
                required_host_driver_sonames=CUDA_HOST_DRIVER_SONAMES,
            )
            closure = verify_llama_cpp_runtime_closure(
                bundle,
                allow_unresolved_host_drivers=flavor == TOOLCHAIN_FLAVOR,
            )
        except LlamaCppBundleError as error:
            raise HfLlamaCppToolchainError(f"llama.cpp lock materialization failed: {error}") from error
        finally:
            if previous_library_path is None:
                os.environ.pop("LD_LIBRARY_PATH", None)
            else:
                os.environ["LD_LIBRARY_PATH"] = previous_library_path
        git_metadata = bundle_root / "source/.git"
        if git_metadata.is_symlink() or not git_metadata.is_dir():
            raise HfLlamaCppToolchainError("materialized source Git metadata is missing")
        shutil.rmtree(git_metadata)
        bundle = load_verified_llama_cpp_bundle(bundle_root, lock)
        _, payload_inventory = _tree_inventory(
            bundle_root,
            excluded=_RESULT_EXCLUDED,
            allow_safe_symlinks=True,
        )
        result = _result_from_bundle(
            bundle,
            packet_tree_sha256=str(manifest["packet_tree_sha256"]),
            payload_inventory=payload_inventory,
            closure=closure,
            flavor=flavor,
        )
        _validate_schema(result, validator=_result_validator(), label="toolchain materialization result")
        _write_immutable_json(bundle_root / TOOLCHAIN_RESULT_FILENAME, result)
        output.parent.mkdir(parents=True, exist_ok=True)
        stage_parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle_root, stage, symlinks=True)
        staged = verify_hf_llama_cpp_toolchain_result(
            stage,
            expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
        )
        if staged != result:
            raise HfLlamaCppToolchainError(
                "toolchain publication stage differs from the build result"
            )
        return _publish_toolchain_no_clobber(
            stage,
            output,
            expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
            expected_result=result,
        )
    except BaseException:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_hf_llama_cpp_toolchain_result(
    bundle_root: str | Path,
    *,
    expected_packet_tree_sha256: str | None = None,
) -> dict[str, object]:
    """Verify a materialized bundle, its payload inventory, and zero-side-effect result."""

    root = Path(bundle_root).expanduser().resolve()
    result, payload = _read_json_object(root / TOOLCHAIN_RESULT_FILENAME, "toolchain materialization result")
    if payload != _canonical_bytes(result):
        raise HfLlamaCppToolchainError("toolchain materialization result is not canonical JSON")
    _validate_schema(result, validator=_result_validator(), label="toolchain materialization result")
    if expected_packet_tree_sha256 is not None and result.get("packet_tree_sha256") != _prefixed_hash(
        expected_packet_tree_sha256,
        "expected packet tree",
    ):
        raise HfLlamaCppToolchainError("toolchain result packet tree differs")
    try:
        bundle = load_verified_llama_cpp_bundle(root, root / TOOLCHAIN_LOCK_FILENAME)
    except LlamaCppBundleError as error:
        raise HfLlamaCppToolchainError(f"materialized llama.cpp bundle verification failed: {error}") from error
    _, inventory = _tree_inventory(
        root,
        excluded=_RESULT_EXCLUDED,
        allow_safe_symlinks=True,
    )
    locked_closure = _mapping(bundle.native_runtime.get("ldd_closure"), "locked ldd closure")
    closure = {
        "ldd_closure_sha256": _sha256(
            json.dumps(
                locked_closure,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        )
    }
    expected = _result_from_bundle(
        bundle,
        packet_tree_sha256=str(result.get("packet_tree_sha256", "")),
        payload_inventory=inventory,
        closure=closure,
        flavor=str(result.get("flavor", "")),
    )
    if result != expected:
        raise HfLlamaCppToolchainError("toolchain result differs from the exact bundle payload")
    return result


def toolchain_materialization_binding(
    bundle_root: str | Path,
) -> dict[str, object]:
    """Return the narrow result binding consumed by the later quantization plan."""

    root = Path(bundle_root).expanduser().resolve()
    result = verify_hf_llama_cpp_toolchain_result(root)
    return {
        "schema_version": 1,
        "kind": "scriber_llama_cpp_toolchain_materialization_binding",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "status": "verified",
        "packet_tree_sha256": result["packet_tree_sha256"],
        "bundle_payload_tree_sha256": result["bundle_payload_inventory"]["tree_sha256"],
        "result_sha256": _sha256_file(root / TOOLCHAIN_RESULT_FILENAME, "toolchain result"),
        "flavor": result["flavor"],
        "timeout_minutes": TOOLCHAIN_TIMEOUT_MINUTES,
        "training_steps_executed": 0,
        "evaluation_reports_written": 0,
        "prediction_records_written": 0,
    }
