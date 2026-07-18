"""Run fail-closed, paired YouTube JS-runtime holdouts for installer research.

The validator directly starts each inventory-bound frozen
``scriber-backend.exe`` and its protected JSON-only probe.  It never falls back
to the repository environment or the non-portable pip console launcher copied
as ``yt-dlp.exe``.  Every measured sample enables exactly one explicit
JavaScript runtime, disables remote components and configuration discovery,
and executes in a private random workspace removed on every exit path.

Exit codes are intentionally small and producer-friendly:

* 0: immutable evidence was written with ``status == "pass"``;
* 1: immutable evidence was written with ``status`` ``fail`` or ``not_run``;
* 2: the inputs/evidence boundary was invalid and no scientific claim is safe.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


EVIDENCE_CONTRACT = "InstallerSizeYoutubeCandidateHoldoutsV1"
FROZEN_PROBE_CONTRACT = "InstallerYoutubeFrozenHoldoutProbeV1"
RUNTIME_MANIFEST_CONTRACT = "ScriberYoutubeJsRuntimeManifestV1"
PROVENANCE_LOCK_CONTRACT = "ScriberQuickJsRuntimeProvenanceLockV1"
SCHEMA_VERSION = 1
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
SPDX_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+() -]{0,127}$")
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 4 * 1024 * 1024
MIN_COLD_PAIRS = 2
MIN_WARM_PAIRS = 2
MAX_P95_RATIO_BASIS_POINTS = 11_000
RUNTIME_FILE_KINDS = {
    "deno.exe": "deno",
    "deno": "deno",
    "qjs.exe": "quickjs",
    "qjs": "quickjs",
    "qjs-ng.exe": "quickjs",
    "qjs-ng": "quickjs",
    "quickjs.exe": "quickjs",
    "quickjs": "quickjs",
    "node.exe": "node",
    "node": "node",
    "bun.exe": "bun",
    "bun": "bun",
}
SUPPORTED_RUNTIME_KINDS = {"deno", "quickjs"}
RUNTIME_MANIFEST_RELATIVE = "backend/tools/ffmpeg/js-runtime-manifest.json"
PROVENANCE_LOCK_RELATIVE = "scripts/perf/profiles/installer-size/quickjs-runtime-lock-v1.json"
PROVENANCE_LOCK_PATH = Path(__file__).resolve().parent.parent / PROVENANCE_LOCK_RELATIVE
INTERNAL_RELATIVE = "backend/_internal"
BACKEND_EXE_RELATIVE = "backend/scriber-backend.exe"


class HoldoutError(RuntimeError):
    """The evidence boundary is invalid and must fail closed."""


@dataclass(frozen=True)
class CommandResult:
    status: str
    return_code: int | None
    elapsed_ns: int
    stdout: bytes
    stderr: bytes
    cleanup_verified: bool
    workspace_fingerprint: str


@dataclass(frozen=True)
class RuntimeIdentity:
    kind: str
    version: str
    executable: Path
    length: int
    sha256: str
    origin: str
    license: str
    provenance: str
    manifest_sha256: str | None
    provenance_lock_entry: str | None
    provenance_lock_sha256: str | None


@dataclass(frozen=True)
class DistributionIdentity:
    name: str
    version: str
    content_sha256: str
    origin: str
    license: str


@dataclass(frozen=True)
class StackIdentity:
    label: str
    root: Path
    backend_executable: Path
    inventory: Mapping[str, Any]
    inventory_sha256: str
    runtime: RuntimeIdentity
    yt_dlp: DistributionIdentity
    ejs: DistributionIdentity
    component_content_sha256: str


@dataclass(frozen=True)
class ProbeOutcome:
    status: str
    duration_ns: int
    failure_code: str | None
    semantic_capabilities: tuple[str, ...]
    cleanup_verified: bool


@dataclass(frozen=True)
class BoundInputSnapshot:
    """Actual, inventory-verified bytes that may affect one frozen probe stack."""

    file_count: int
    content_sha256: str
    files: tuple[tuple[str, int, str], ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _absolute_entry(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _plain_path(path: Path, *, label: str, kind: str) -> Path:
    entry = _absolute_entry(path)
    try:
        info = entry.lstat()
    except OSError as exc:
        raise HoldoutError(f"{label} is missing") from exc
    if entry.is_symlink() or _is_reparse(info):
        raise HoldoutError(f"{label} must not be a symlink or reparse point")
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise HoldoutError(f"{label} must be a regular file")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise HoldoutError(f"{label} must be a directory")
    try:
        resolved = entry.resolve(strict=True)
    except OSError as exc:
        raise HoldoutError(f"{label} cannot be resolved") from exc
    resolved_info = resolved.lstat()
    if resolved.is_symlink() or _is_reparse(resolved_info):
        raise HoldoutError(f"resolved {label} must be plain")
    if kind == "file" and not resolved.is_file():
        raise HoldoutError(f"resolved {label} must be a file")
    if kind == "directory" and not resolved.is_dir():
        raise HoldoutError(f"resolved {label} must be a directory")
    return resolved


def _plain_file(path: Path, *, label: str) -> Path:
    return _plain_path(path, label=label, kind="file")


def _plain_directory(path: Path, *, label: str) -> Path:
    return _plain_path(path, label=label, kind="directory")


def _assert_plain_descendant(root: Path, path: Path, *, label: str) -> Path:
    root = _plain_directory(root, label=f"{label} root")
    entry = _absolute_entry(path)
    try:
        relative = entry.relative_to(root)
    except ValueError as exc:
        raise HoldoutError(f"{label} escaped its required root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if current.is_symlink() or _is_reparse(info):
            raise HoldoutError(f"{label} contains a symlink or reparse point")
    return _plain_path(entry, label=label, kind="directory" if entry.is_dir() else "file")


def _load_object(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    path = _plain_file(path, label=label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HoldoutError(f"{label} must contain an object")
    return payload, _sha256_file(path)


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    entry = _absolute_entry(path)
    if entry.exists():
        raise HoldoutError("immutable candidate holdout evidence already exists")
    parent = entry.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent = _plain_directory(parent, label="evidence parent")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{entry.name}.", suffix=".tmp", dir=str(parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Linking a fully flushed same-directory temporary file publishes one
        # complete name without ever granting overwrite semantics.  Unlike an
        # existence check followed by os.replace(), the destination creation is
        # atomic with respect to a concurrent writer.  Fail closed on filesystems
        # that cannot provide this guarantee.
        try:
            os.link(temporary, entry, follow_symlinks=False)
        except FileExistsError as exc:
            raise HoldoutError(
                "immutable candidate holdout evidence appeared concurrently"
            ) from exc
        except OSError as exc:
            raise HoldoutError(
                "cannot atomically publish immutable candidate holdout evidence"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise HoldoutError("RunId must be a canonical RFC 4122 UUID") from exc
    if value != str(parsed) or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise HoldoutError("RunId must be a canonical non-nil RFC 4122 UUID")
    return value


def _safe_id(value: str, *, label: str, allow_baseline: bool = False) -> str:
    if allow_baseline and value == "baseline":
        return value
    if not SAFE_ID_RE.fullmatch(value):
        raise HoldoutError(f"{label} is not a safe identifier")
    return value


def _source_commit(value: str) -> str:
    if not SOURCE_COMMIT_RE.fullmatch(value):
        raise HoldoutError("source commit must be a full lowercase object id")
    return value


def _inventory_tree(inventory: Mapping[str, Any], *, installed: bool) -> Mapping[str, Any]:
    payload = inventory.get("payload")
    if not isinstance(payload, dict):
        raise HoldoutError("inventory payload is missing")
    key = "installed" if installed else "staged"
    tree = payload.get(key)
    if not isinstance(tree, dict):
        raise HoldoutError(f"inventory {key} tree is missing")
    return tree


def _validate_inventory(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    source_commit: str | None,
    label: str,
) -> None:
    if (
        payload.get("inventoryContract") != "InstallerResearchInventoryV1"
        or payload.get("schemaVersion") != 1
        or payload.get("ok") is not True
        or payload.get("runId") != run_id
    ):
        raise HoldoutError(f"{label} inventory contract or run binding is invalid")
    if source_commit is not None and payload.get("sourceCommit") != source_commit:
        raise HoldoutError(f"{label} inventory source binding is invalid")
    staged = _inventory_tree(payload, installed=False)
    for field in ("semanticTreeSha256", "fileListSha256"):
        if not SHA256_RE.fullmatch(str(staged.get(field) or "")):
            raise HoldoutError(f"{label} inventory lacks {field}")
    installed = _inventory_tree(payload, installed=True)
    for field in ("semanticTreeSha256", "fileListSha256"):
        if not SHA256_RE.fullmatch(str(installed.get(field) or "")):
            raise HoldoutError(f"{label} installed inventory lacks {field}")
    parity = payload.get("payload", {}).get("stagedInstalledParity")
    if not isinstance(parity, dict) or parity.get("ok") is not True:
        raise HoldoutError(f"{label} staged/installed parity is not proven")


def _inventory_entries(
    inventory: Mapping[str, Any], *, installed: bool
) -> dict[str, Mapping[str, Any]]:
    files = _inventory_tree(inventory, installed=installed).get("files")
    if not isinstance(files, list):
        raise HoldoutError("inventory files are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for raw in files:
        if not isinstance(raw, dict):
            raise HoldoutError("inventory file entry is invalid")
        relative = raw.get("path")
        length = raw.get("length")
        sha256 = raw.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or "\\" in relative
            or ".." in PurePosixPath(relative).parts
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 0
            or not SHA256_RE.fullmatch(str(sha256 or ""))
        ):
            raise HoldoutError("inventory file entry is unsafe")
        folded = relative.casefold()
        if folded in result:
            raise HoldoutError("inventory contains a case-insensitive path collision")
        result[folded] = raw
    return result


def _validate_replica_bindings(
    *,
    baseline_inventory: Mapping[str, Any],
    candidate_inventory: Mapping[str, Any],
    parent_inventory: Mapping[str, Any],
    packet_id: str,
    parent_id: str,
) -> None:
    def replica_id(inventory: Mapping[str, Any], *, label: str) -> Any:
        provenance = inventory.get("buildProvenance")
        if not isinstance(provenance, Mapping):
            raise HoldoutError(f"{label} inventory build provenance is invalid")
        return provenance.get("replicaId")

    baseline_replica = replica_id(baseline_inventory, label="baseline")
    candidate_replica = replica_id(candidate_inventory, label="candidate")
    parent_replica = replica_id(parent_inventory, label="parent")
    if baseline_replica != "baseline-1":
        raise HoldoutError("baseline inventory must bind baseline replica 1")
    if candidate_replica != packet_id:
        raise HoldoutError("candidate inventory does not match packet id")
    if parent_id == "baseline" and parent_replica != "baseline-1":
        raise HoldoutError("baseline parent must bind baseline replica 1")
    if parent_id != "baseline" and parent_replica != parent_id:
        raise HoldoutError("parent inventory does not match parent champion id")


def _is_youtube_distribution_input(
    relative: str, item: Mapping[str, Any]
) -> bool:
    folded = relative.casefold()
    return item.get("component") == "yt-dlp-ejs" or any(
        marker in f"/{folded}"
        for marker in ("/yt_dlp/", "/yt_dlp_ejs/", "/yt_dlp-", "/yt_dlp_ejs-")
    )


def _is_distribution_metadata(relative: str, distribution: str) -> bool:
    entry = PurePosixPath(relative)
    normalized = distribution.casefold().replace("-", "_")
    parent = entry.parent.name.casefold().replace("-", "_")
    if normalized == "yt_dlp" and parent.startswith("yt_dlp_ejs_"):
        return False
    return (
        entry.name == "METADATA"
        and parent.startswith(normalized + "_")
        and parent.endswith(".dist_info")
    )


def _hash_inventory_bound_entry(
    *,
    root: Path,
    relative: str,
    item: Mapping[str, Any],
    label: str,
) -> tuple[int, str]:
    candidate = _plain_file(root / PurePosixPath(relative), label=label)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HoldoutError(f"{label} escaped its payload") from exc
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            before = os.fstat(handle.fileno())
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise HoldoutError(f"{label} could not be rehashed") from exc
    if (
        not os.path.samestat(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise HoldoutError(f"{label} changed while it was rehashed")
    current = _plain_file(candidate, label=label)
    try:
        current_info = current.stat()
    except OSError as exc:
        raise HoldoutError(f"{label} disappeared after it was rehashed") from exc
    if not os.path.samestat(after, current_info):
        raise HoldoutError(f"{label} path identity changed while it was rehashed")
    actual_sha256 = digest.hexdigest()
    if after.st_size != item.get("length") or actual_sha256 != item.get("sha256"):
        raise HoldoutError(f"{label} differs from the bound inventory")
    return int(after.st_size), actual_sha256


def _capture_bound_inputs(
    stack: StackIdentity, *, installed: bool
) -> BoundInputSnapshot:
    entries = _inventory_entries(stack.inventory, installed=installed)
    try:
        runtime_relative = stack.runtime.executable.relative_to(stack.root).as_posix()
        backend_relative = stack.backend_executable.relative_to(stack.root).as_posix()
    except ValueError as exc:
        raise HoldoutError(f"{stack.label} executable escaped its payload") from exc
    required = {backend_relative.casefold(), runtime_relative.casefold()}
    selected: dict[str, Mapping[str, Any]] = {}
    for folded, item in entries.items():
        relative = str(item["path"])
        if (
            folded in required
            or folded == RUNTIME_MANIFEST_RELATIVE.casefold()
            or _is_youtube_distribution_input(relative, item)
        ):
            selected[folded] = item
    if not required.issubset(selected):
        raise HoldoutError(f"{stack.label} executable input set is incomplete")
    for distribution in ("yt-dlp", "yt-dlp-ejs"):
        metadata = [
            str(item["path"])
            for item in selected.values()
            if _is_distribution_metadata(str(item["path"]), distribution)
        ]
        if len(metadata) != 1:
            raise HoldoutError(
                f"{stack.label} {distribution} METADATA input binding is incomplete"
            )
    rows: list[tuple[str, int, str]] = []
    for item in sorted(
        selected.values(), key=lambda value: str(value["path"]).encode("utf-8")
    ):
        relative = str(item["path"])
        length, sha256 = _hash_inventory_bound_entry(
            root=stack.root,
            relative=relative,
            item=item,
            label=f"{stack.label} holdout input",
        )
        rows.append((relative, length, sha256))
    if not rows:
        raise HoldoutError(f"{stack.label} holdout input set is empty")
    public_rows = [
        {"relative": relative, "length": length, "sha256": sha256}
        for relative, length, sha256 in rows
    ]
    return BoundInputSnapshot(
        file_count=len(rows),
        content_sha256=_canonical_sha256(public_rows),
        files=tuple(rows),
    )


def _verify_inventory_file(
    *,
    root: Path,
    inventory: Mapping[str, Any],
    relative: str,
    installed: bool,
    label: str,
) -> Path:
    entries = _inventory_entries(inventory, installed=installed)
    item = entries.get(relative.casefold())
    if item is None:
        raise HoldoutError(f"{label} is absent from the bound inventory")
    candidate = root / PurePosixPath(relative)
    candidate = _plain_file(candidate, label=label)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HoldoutError(f"{label} escaped its payload") from exc
    if candidate.stat().st_size != item.get("length") or _sha256_file(candidate) != item.get(
        "sha256"
    ):
        raise HoldoutError(f"{label} differs from the bound inventory")
    return candidate


def _component_identity(inventory: Mapping[str, Any], component: str) -> str:
    rows: list[dict[str, Any]] = []
    for item in _inventory_entries(inventory, installed=False).values():
        path = str(item["path"])
        folded = path.casefold()
        if item.get("component") == component or (
            component == "yt-dlp-ejs"
            and (
                "/yt_dlp/" in f"/{folded}"
                or "/yt_dlp_ejs/" in f"/{folded}"
                or "/yt_dlp-" in f"/{folded}"
                or "/yt_dlp_ejs-" in f"/{folded}"
            )
        ):
            rows.append(
                {
                    "path": path,
                    "length": item["length"],
                    "sha256": item["sha256"],
                }
            )
    rows.sort(key=lambda item: item["path"].encode("utf-8"))
    if not rows:
        raise HoldoutError(f"inventory contains no {component} files")
    return _canonical_sha256(rows)


def _payload_binding(inventory: Mapping[str, Any], inventory_sha256: str) -> dict[str, Any]:
    staged = _inventory_tree(inventory, installed=False)
    installed = _inventory_tree(inventory, installed=True)
    return {
        "inventorySha256": inventory_sha256,
        "sourceCommit": inventory.get("sourceCommit"),
        "replicaId": inventory.get("buildProvenance", {}).get("replicaId"),
        "stagedSemanticTreeSha256": staged.get("semanticTreeSha256"),
        "stagedFileListSha256": staged.get("fileListSha256"),
        "installedSemanticTreeSha256": installed.get("semanticTreeSha256"),
        "installedFileListSha256": installed.get("fileListSha256"),
    }


def _metadata_identity(
    *,
    root: Path,
    inventory: Mapping[str, Any],
    distribution: str,
    installed: bool,
) -> DistributionIdentity:
    normalized = distribution.casefold().replace("-", "_")
    candidates: list[tuple[str, Path]] = []
    for folded, item in _inventory_entries(inventory, installed=installed).items():
        relative = str(item["path"])
        if _is_distribution_metadata(relative, distribution):
            candidates.append((relative, root / PurePosixPath(relative)))
    if len(candidates) != 1:
        raise HoldoutError(f"{distribution} must have exactly one installed METADATA file")
    relative, _candidate = candidates[0]
    path = _verify_inventory_file(
        root=root,
        inventory=inventory,
        relative=relative,
        installed=installed,
        label=f"{distribution} METADATA",
    )
    message = BytesParser(policy=email_policy).parsebytes(path.read_bytes())
    name = str(message.get("Name") or "").casefold().replace("_", "-")
    version = str(message.get("Version") or "")
    license_value = str(message.get("License-Expression") or message.get("License") or "")
    if name != distribution or not VERSION_RE.fullmatch(version) or not SPDX_RE.fullmatch(
        license_value
    ):
        raise HoldoutError(f"{distribution} metadata identity is incomplete")
    origins: list[tuple[int, str]] = []
    for raw in message.get_all("Project-URL", []):
        if "," not in raw:
            continue
        label, raw_url = raw.split(",", 1)
        url = raw_url.strip()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            continue
        priority = {
            "source": 0,
            "repository": 0,
            "homepage": 1,
            "documentation": 2,
        }.get(label.strip().casefold(), 3)
        origins.append((priority, url))
    if not origins:
        raise HoldoutError(f"{distribution} metadata has no safe HTTPS origin")
    origins.sort(key=lambda item: (item[0], item[1]))
    prefix = f"{INTERNAL_RELATIVE}/{normalized}"
    content_rows = [
        {
            "path": str(item["path"]),
            "length": item["length"],
            "sha256": item["sha256"],
        }
        for item in _inventory_entries(inventory, installed=False).values()
        if str(item["path"]).casefold().replace("-", "_").startswith(prefix)
    ]
    content_rows.sort(key=lambda item: item["path"].encode("utf-8"))
    if not content_rows:
        raise HoldoutError(f"{distribution} inventory content is empty")
    return DistributionIdentity(
        name=name,
        version=version,
        content_sha256=_canonical_sha256(content_rows),
        origin=origins[0][1],
        license=license_value,
    )


def _find_runtime(
    *, root: Path, inventory: Mapping[str, Any], installed: bool, label: str
) -> tuple[str, str, Path, Mapping[str, Any]]:
    entries = _inventory_entries(inventory, installed=installed)
    found: list[tuple[str, str, Mapping[str, Any]]] = []
    for item in entries.values():
        relative = str(item["path"])
        kind = RUNTIME_FILE_KINDS.get(PurePosixPath(relative).name.casefold())
        if kind is not None:
            found.append((relative, kind, item))
    if len(found) != 1:
        raise HoldoutError(f"{label} must bundle exactly one JavaScript runtime")
    relative, kind, item = found[0]
    if kind not in SUPPORTED_RUNTIME_KINDS:
        raise HoldoutError(f"{label} runtime is not an approved holdout runtime")
    expected_prefix = "backend/tools/ffmpeg/"
    if not relative.casefold().startswith(expected_prefix):
        raise HoldoutError(f"{label} runtime is outside the media-tool boundary")
    path = _verify_inventory_file(
        root=root,
        inventory=inventory,
        relative=relative,
        installed=installed,
        label=f"{label} JavaScript runtime",
    )
    return relative, kind, path, item


def _safe_origin(value: Any) -> str:
    if not isinstance(value, str):
        raise HoldoutError("runtime origin is missing")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HoldoutError("runtime origin must be a credential-free HTTPS URL")
    return value


def _safe_leaf_name(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "\\" in value
        or "/" in value
        or PurePosixPath(value).name != value
    ):
        raise HoldoutError(f"{label} must be a plain file name")
    return value


def _safe_archive_path(value: Any, *, label: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or "\\" in value:
        raise HoldoutError(f"{label} is invalid")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise HoldoutError(f"{label} is unsafe")
    return value


def _positive_length(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HoldoutError(f"{label} must be a positive integer")
    return value


def _quickjs_manifest_for_entry(
    entry: Mapping[str, Any], executable_file: Mapping[str, Any]
) -> dict[str, Any]:
    asset = entry["asset"]
    license_value = entry["license"]
    return {
        "contract": RUNTIME_MANIFEST_CONTRACT,
        "schemaVersion": 1,
        "runtime": {
            "kind": "quickjs",
            "version": entry["version"],
            "executable": executable_file["installedFileName"],
            "length": executable_file["length"],
            "sha256": executable_file["sha256"],
            "origin": asset["url"],
            "license": license_value["spdx"],
            "licenseFile": license_value["installedFileName"],
            "provenanceLockEntry": entry["id"],
        },
        "policy": {
            "remoteComponents": False,
            "firstRunDownloads": False,
        },
    }


def _canonical_manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical_json(value) + b"\n"


def _load_quickjs_provenance_lock(
    path: Path | None = None,
) -> tuple[dict[str, Mapping[str, Any]], str]:
    lock_path = PROVENANCE_LOCK_PATH if path is None else path
    payload, lock_sha256 = _load_object(lock_path, label="protected QuickJS provenance lock")
    if set(payload) != {"contract", "schemaVersion", "campaign", "target", "entries"}:
        raise HoldoutError("QuickJS provenance lock fields are not exact")
    if (
        payload.get("contract") != PROVENANCE_LOCK_CONTRACT
        or payload.get("schemaVersion") != 1
        or payload.get("campaign") != "installer-size-v1"
        or payload.get("target") != {"os": "windows", "architecture": "x86_64"}
    ):
        raise HoldoutError("QuickJS provenance lock contract is invalid")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != 2:
        raise HoldoutError("QuickJS provenance lock must contain primary and fallback entries")

    result: dict[str, Mapping[str, Any]] = {}
    expected_implementations = ("quickjs-ng", "quickjs")
    for position, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "id",
            "preference",
            "implementation",
            "version",
            "release",
            "asset",
            "runtimeFiles",
            "license",
            "manifest",
            "manifestCanonicalSha256",
        }:
            raise HoldoutError("QuickJS provenance entry fields are not exact")
        entry_id = _safe_id(str(raw_entry.get("id") or ""), label="provenance lock entry")
        implementation = raw_entry.get("implementation")
        version = raw_entry.get("version")
        if (
            raw_entry.get("preference") != position
            or implementation != expected_implementations[position - 1]
            or not VERSION_RE.fullmatch(str(version or ""))
            or entry_id in result
        ):
            raise HoldoutError("QuickJS provenance preference or identity is invalid")

        release = raw_entry.get("release")
        asset = raw_entry.get("asset")
        license_value = raw_entry.get("license")
        runtime_files = raw_entry.get("runtimeFiles")
        if not isinstance(release, dict) or set(release) != {
            "id",
            "sourceRevision",
            "url",
        }:
            raise HoldoutError("QuickJS release provenance is invalid")
        if not isinstance(asset, dict) or set(asset) != {
            "url",
            "fileName",
            "format",
            "length",
            "sha256",
            "upstreamPublishedSha256",
        }:
            raise HoldoutError("QuickJS asset provenance is invalid")
        if not isinstance(license_value, dict) or set(license_value) != {
            "spdx",
            "installedFileName",
            "length",
            "sha256",
            "source",
        }:
            raise HoldoutError("QuickJS license provenance is invalid")
        if not isinstance(runtime_files, list) or not runtime_files:
            raise HoldoutError("QuickJS runtime file lock is empty")

        release_id = str(release.get("id") or "")
        source_revision = str(release.get("sourceRevision") or "")
        release_url = _safe_origin(release.get("url"))
        asset_url = _safe_origin(asset.get("url"))
        asset_name = _safe_leaf_name(asset.get("fileName"), label="QuickJS asset")
        asset_length = _positive_length(asset.get("length"), label="QuickJS asset length")
        asset_sha256 = str(asset.get("sha256") or "")
        published_sha256 = asset.get("upstreamPublishedSha256")
        if not SHA256_RE.fullmatch(asset_sha256) or (
            published_sha256 is not None
            and not SHA256_RE.fullmatch(str(published_sha256))
        ):
            raise HoldoutError("QuickJS asset digest is invalid")

        source = license_value.get("source")
        if not isinstance(source, dict) or set(source) != {
            "url",
            "fileName",
            "format",
            "length",
            "sha256",
            "archivePath",
        }:
            raise HoldoutError("QuickJS license source is invalid")
        license_name = _safe_leaf_name(
            license_value.get("installedFileName"), label="installed QuickJS license"
        )
        license_length = _positive_length(
            license_value.get("length"), label="QuickJS license length"
        )
        license_sha256 = str(license_value.get("sha256") or "")
        source_url = _safe_origin(source.get("url"))
        source_name = _safe_leaf_name(source.get("fileName"), label="QuickJS license source")
        source_length = _positive_length(
            source.get("length"), label="QuickJS license source length"
        )
        source_sha256 = str(source.get("sha256") or "")
        if (
            license_value.get("spdx") != "MIT"
            or not SHA256_RE.fullmatch(license_sha256)
            or not SHA256_RE.fullmatch(source_sha256)
        ):
            raise HoldoutError("QuickJS license identity is invalid")

        locked_files: list[Mapping[str, Any]] = []
        installed_names: set[str] = set()
        executable_files: list[Mapping[str, Any]] = []
        for raw_file in runtime_files:
            if not isinstance(raw_file, dict) or set(raw_file) != {
                "role",
                "assetPath",
                "installedFileName",
                "length",
                "sha256",
            }:
                raise HoldoutError("QuickJS runtime file entry is invalid")
            role = raw_file.get("role")
            _safe_archive_path(raw_file.get("assetPath"), label="QuickJS asset member")
            installed_name = _safe_leaf_name(
                raw_file.get("installedFileName"), label="installed QuickJS runtime file"
            )
            file_length = _positive_length(
                raw_file.get("length"), label="QuickJS runtime file length"
            )
            file_sha256 = str(raw_file.get("sha256") or "")
            if (
                role not in {"executable", "dependency"}
                or not SHA256_RE.fullmatch(file_sha256)
                or installed_name.casefold() in installed_names
            ):
                raise HoldoutError("QuickJS runtime file identity is invalid")
            installed_names.add(installed_name.casefold())
            locked_files.append(raw_file)
            if role == "executable":
                executable_files.append(raw_file)
        if len(executable_files) != 1 or license_name.casefold() in installed_names:
            raise HoldoutError("QuickJS executable or license file is ambiguous")
        executable_file = executable_files[0]

        if implementation == "quickjs-ng":
            if (
                release_id != f"v{version}"
                or not re.fullmatch(r"[0-9a-f]{40}", source_revision)
                or release_url
                != f"https://github.com/quickjs-ng/quickjs/releases/tag/{release_id}"
                or asset_name != "qjs-windows-x86_64.exe"
                or asset.get("format") != "executable"
                or asset_url
                != f"https://github.com/quickjs-ng/quickjs/releases/download/{release_id}/{asset_name}"
                or published_sha256 != asset_sha256
                or len(locked_files) != 1
                or executable_file.get("assetPath") != asset_name
                or executable_file.get("length") != asset_length
                or executable_file.get("sha256") != asset_sha256
                or source_url
                != f"https://raw.githubusercontent.com/quickjs-ng/quickjs/{release_id}/LICENSE"
                or source_name != "LICENSE"
                or source.get("format") != "file"
                or source.get("archivePath") is not None
                or source_length != license_length
                or source_sha256 != license_sha256
            ):
                raise HoldoutError("quickjs-ng primary provenance is not official and exact")
        else:
            expected_asset_name = f"quickjs-win-x86_64-{version}.zip"
            expected_source_name = f"quickjs-{version}.tar.xz"
            if (
                release_id != version
                or source_revision != version
                or release_url != "https://bellard.org/quickjs/"
                or asset_name != expected_asset_name
                or asset.get("format") != "zip"
                or asset_url
                != f"https://bellard.org/quickjs/binary_releases/{expected_asset_name}"
                or published_sha256 is not None
                or source_url != f"https://bellard.org/quickjs/{expected_source_name}"
                or source_name != expected_source_name
                or source.get("format") != "tar.xz"
                or source.get("archivePath") != f"quickjs-{version}/LICENSE"
            ):
                raise HoldoutError("classic QuickJS fallback provenance is not official and exact")

        expected_manifest = _quickjs_manifest_for_entry(raw_entry, executable_file)
        manifest = raw_entry.get("manifest")
        manifest_sha256 = str(raw_entry.get("manifestCanonicalSha256") or "")
        if (
            manifest != expected_manifest
            or not SHA256_RE.fullmatch(manifest_sha256)
            or hashlib.sha256(_canonical_manifest_bytes(expected_manifest)).hexdigest()
            != manifest_sha256
        ):
            raise HoldoutError("QuickJS locked manifest is not canonical")
        result[entry_id] = raw_entry
    return result, lock_sha256


def _runtime_manifest_identity(
    *,
    root: Path,
    inventory: Mapping[str, Any],
    installed: bool,
    relative: str,
    kind: str,
    executable: Path,
    executable_item: Mapping[str, Any],
    provenance_lock_path: Path | None = None,
) -> RuntimeIdentity | None:
    manifest_candidate = root / PurePosixPath(RUNTIME_MANIFEST_RELATIVE)
    if not manifest_candidate.exists():
        return None
    manifest_path = _verify_inventory_file(
        root=root,
        inventory=inventory,
        relative=RUNTIME_MANIFEST_RELATIVE,
        installed=installed,
        label="candidate JS-runtime manifest",
    )
    manifest, manifest_sha = _load_object(manifest_path, label="candidate JS-runtime manifest")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise HoldoutError("candidate JS-runtime manifest lacks a runtime identity")
    lock_entries, lock_sha256 = _load_quickjs_provenance_lock(provenance_lock_path)
    lock_entry_id = runtime.get("provenanceLockEntry")
    lock_entry = lock_entries.get(str(lock_entry_id or ""))
    if lock_entry is None:
        raise HoldoutError("candidate JS-runtime manifest is absent from the protected lock")
    locked_manifest = lock_entry["manifest"]
    expected_manifest_bytes = _canonical_manifest_bytes(locked_manifest)
    if (
        manifest != locked_manifest
        or manifest_path.read_bytes() != expected_manifest_bytes
        or manifest_sha != lock_entry["manifestCanonicalSha256"]
    ):
        raise HoldoutError("candidate JS-runtime manifest is not byte-exact with its lock entry")

    executable_name = PurePosixPath(relative).name
    if (
        runtime.get("kind") != kind
        or kind != "quickjs"
        or runtime.get("executable") != executable_name
        or runtime.get("length") != executable_item.get("length")
        or runtime.get("sha256") != executable_item.get("sha256")
    ):
        raise HoldoutError("candidate JS-runtime manifest does not match its executable")

    entries = _inventory_entries(inventory, installed=installed)
    runtime_parent = PurePosixPath(relative).parent
    for locked_file in lock_entry["runtimeFiles"]:
        locked_relative = str(runtime_parent / locked_file["installedFileName"])
        item = entries.get(locked_relative.casefold())
        if (
            item is None
            or item.get("length") != locked_file["length"]
            or item.get("sha256") != locked_file["sha256"]
        ):
            raise HoldoutError("candidate QuickJS runtime files differ from the protected lock")
        _verify_inventory_file(
            root=root,
            inventory=inventory,
            relative=locked_relative,
            installed=installed,
            label=f"locked QuickJS runtime file {locked_file['installedFileName']}",
        )
    locked_license = lock_entry["license"]
    license_relative = str(runtime_parent / locked_license["installedFileName"])
    license_item = entries.get(license_relative.casefold())
    if (
        license_item is None
        or license_item.get("length") != locked_license["length"]
        or license_item.get("sha256") != locked_license["sha256"]
    ):
        raise HoldoutError("candidate QuickJS license differs from the protected lock")
    _verify_inventory_file(
        root=root,
        inventory=inventory,
        relative=license_relative,
        installed=installed,
        label="locked QuickJS license",
    )
    return RuntimeIdentity(
        kind=kind,
        version=str(lock_entry["version"]),
        executable=executable,
        length=int(executable_item["length"]),
        sha256=str(executable_item["sha256"]),
        origin=str(lock_entry["asset"]["url"]),
        license=str(locked_license["spdx"]),
        provenance="protected-provenance-lock",
        manifest_sha256=manifest_sha,
        provenance_lock_entry=str(lock_entry_id),
        provenance_lock_sha256=lock_sha256,
    )


def _baseline_runtime_provenance(
    *, environment_root: Path, expected_version: str
) -> tuple[str, str]:
    metadata_files = list(environment_root.glob(".venv/Lib/site-packages/deno-*.dist-info/METADATA"))
    if len(metadata_files) != 1:
        raise HoldoutError("baseline Deno distribution metadata is not unique")
    metadata_path = _plain_file(metadata_files[0], label="baseline Deno metadata")
    message = BytesParser(policy=email_policy).parsebytes(metadata_path.read_bytes())
    if str(message.get("Version") or "") != expected_version:
        raise HoldoutError("baseline Deno metadata version drifted")
    license_value = str(message.get("License-Expression") or message.get("License") or "")
    if not SPDX_RE.fullmatch(license_value):
        raise HoldoutError("baseline Deno license metadata is invalid")
    origins = []
    for raw in message.get_all("Project-URL", []):
        if "," in raw:
            _label, value = raw.split(",", 1)
            try:
                origins.append(_safe_origin(value.strip()))
            except HoldoutError:
                pass
    if not origins:
        raise HoldoutError("baseline Deno origin metadata is invalid")
    return sorted(origins)[0], license_value


def _require_runtime_security_boundary(runtime: RuntimeIdentity) -> None:
    if runtime.kind == "deno":
        return
    if runtime.kind == "quickjs":
        raise HoldoutError(
            "candidate QuickJS runtime is blocked: raw qjs execution has no "
            "attested Restricted-Token/AppContainer security boundary"
        )
    raise HoldoutError("candidate runtime has no approved security boundary")


def _runtime_version_command(runtime: RuntimeIdentity) -> list[str]:
    if runtime.kind == "deno":
        return [str(runtime.executable), "--version"]
    # QuickJS intentionally has no --version; yt-dlp uses --help and accepts
    # its documented exit code 1 while parsing the first version line.
    return [str(runtime.executable), "--help"]


def _runtime_success_command(runtime: RuntimeIdentity) -> list[str]:
    return _runtime_version_command(runtime)


def _runtime_error_command(runtime: RuntimeIdentity) -> list[str]:
    if runtime.kind == "deno":
        return [str(runtime.executable), "eval", "--no-config", "throw new Error('holdout')"]
    return [str(runtime.executable), "-e", "throw new Error('holdout')"]


def _runtime_long_command(runtime: RuntimeIdentity) -> list[str]:
    if runtime.kind == "deno":
        return [
            str(runtime.executable),
            "eval",
            "--no-config",
            "await new Promise(resolve => setTimeout(resolve, 60000))",
        ]
    return [str(runtime.executable), "-e", "for (;;) {}"]


def _runtime_version_from_output(runtime: RuntimeIdentity, output: bytes) -> bool:
    decoded = output.decode("utf-8", errors="replace")
    if runtime.kind == "deno":
        return decoded.splitlines()[0].strip() == f"deno {runtime.version}" if decoded else False
    match = re.search(r"^QuickJS(?:-ng)?\s+version\s+(\S+)", decoded, re.MULTILINE)
    return bool(match and match.group(1) == runtime.version)


def _runtime_version_return_code_ok(runtime: RuntimeIdentity, return_code: int | None) -> bool:
    return return_code == 0 if runtime.kind == "deno" else return_code in {0, 1}


def _private_acl(path: Path) -> str:
    os.chmod(path, stat.S_IRWXU)
    if os.name != "nt":
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise HoldoutError("private holdout directory permissions are too broad")
        return "mode-0700"
    whoami = subprocess.run(
        ["whoami"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    principal = whoami.stdout.strip()
    if whoami.returncode != 0 or not principal or "\n" in principal or "\r" in principal:
        raise HoldoutError("cannot resolve the current Windows principal")
    hardened = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "/Q",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if hardened.returncode != 0:
        raise HoldoutError("cannot harden the Windows holdout directory ACL")
    return "windows-explicit-owner-system"


def _directory_object_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or _is_reparse(info):
        raise HoldoutError("private holdout root identity is not a plain directory")
    return int(info.st_dev), int(info.st_ino)


def _remove_created_private_root(
    *, scratch_root: Path, root: Path, identity: tuple[int, int]
) -> bool:
    """Remove only the exact validated directory created by this constructor."""

    try:
        current = _assert_plain_descendant(
            scratch_root, root, label="failed private holdout root cleanup"
        )
        relative = current.relative_to(scratch_root)
        if len(relative.parts) != 1 or _directory_object_identity(current) != identity:
            return False
        shutil.rmtree(current)
        return not current.exists()
    except (HoldoutError, OSError):
        # A missing, replaced, or reparse-point path is not ours to delete.
        return False


class PrivateWorkspaceFactory:
    """Create cryptographically random workspaces below one private root."""

    def __init__(self, scratch_root: Path) -> None:
        self.scratch_root = _plain_directory(scratch_root, label="holdout scratch root")
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self.created_count = 0
        self.cleanup_count = 0
        token = secrets.token_hex(20)
        self.root = self.scratch_root / f".youtube-candidate-{token}"
        validated_root: Path | None = None
        created_identity: tuple[int, int] | None = None
        try:
            self.root.mkdir(mode=0o700, exist_ok=False)
            validated_root = _plain_directory(self.root, label="private holdout root")
            created_identity = _directory_object_identity(validated_root)
            self.root = validated_root
            self.acl_mode = _private_acl(validated_root)
        except BaseException:
            if validated_root is not None and created_identity is not None:
                _remove_created_private_root(
                    scratch_root=self.scratch_root,
                    root=validated_root,
                    identity=created_identity,
                )
            raise

    def create(self, purpose: str) -> Path:
        if not CASE_ID_RE.fullmatch(purpose):
            raise HoldoutError("workspace purpose is unsafe")
        while True:
            token = secrets.token_hex(20)
            path = self.root / f"{purpose}-{token}"
            try:
                path.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError:
                continue
            break
        fingerprint = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        registered = False
        try:
            path = _plain_directory(path, label="private invocation directory")
            with self._lock:
                if fingerprint in self._active:
                    raise HoldoutError("private workspace identity collision")
                self._active.add(fingerprint)
                self.created_count += 1
                registered = True
            for name in ("temp", "home", "cache", "deno", "output"):
                child = path / name
                child.mkdir(mode=0o700, exist_ok=False)
                _plain_directory(child, label="private invocation child")
            return path
        except BaseException:
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            if registered:
                with self._lock:
                    self._active.discard(fingerprint)
                    self.cleanup_count += 1
            raise

    def remove(self, path: Path) -> None:
        path = _assert_plain_descendant(self.root, path, label="private invocation cleanup")
        fingerprint = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        shutil.rmtree(path)
        if path.exists():
            raise HoldoutError("private invocation cleanup was incomplete")
        with self._lock:
            if fingerprint not in self._active:
                raise HoldoutError("private invocation cleanup identity mismatch")
            self._active.remove(fingerprint)
            self.cleanup_count += 1

    def close(self) -> None:
        with self._lock:
            if self._active:
                raise HoldoutError("private invocation directories remain active")
        root = _assert_plain_descendant(
            self.scratch_root, self.root, label="private holdout root cleanup"
        )
        shutil.rmtree(root)
        if root.exists():
            raise HoldoutError("private holdout root cleanup was incomplete")


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0 and process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise HoldoutError("holdout process tree did not terminate") from exc


def _run_bounded(
    command: Sequence[str],
    *,
    factory: PrivateWorkspaceFactory,
    purpose: str,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
    shared_cache: Path | None = None,
    stdin_bytes: bytes | None = None,
) -> CommandResult:
    if not command or any(not isinstance(item, str) or not item or "\0" in item for item in command):
        raise HoldoutError("holdout command is invalid")
    if stdin_bytes is not None and (
        not isinstance(stdin_bytes, bytes) or len(stdin_bytes) > 16 * 1024
    ):
        raise HoldoutError("holdout stdin payload is invalid")
    workspace = factory.create(purpose)
    process: subprocess.Popen[bytes] | None = None
    try:
        fingerprint = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
        stdout_path = workspace / "output" / "stdout.bin"
        stderr_path = workspace / "output" / "stderr.bin"
        stdin_path = workspace / "request.bin"
        if stdin_bytes is not None:
            stdin_path.write_bytes(stdin_bytes)
        stdin_source = stdin_path if stdin_bytes is not None else Path(os.devnull)
        cache_path = shared_cache or workspace / "cache"
        if shared_cache is not None:
            cache_path = _assert_plain_descendant(factory.root, shared_cache, label="warm cache")
        env = os.environ.copy()
        for name in (
            "YTDLP_CONFIG",
            "YT_DLP_CONFIG",
            "PYTHONHOME",
            "PYTHONPATH",
            "DENO_INSTALL",
            "DENO_AUTH_TOKENS",
            "NODE_OPTIONS",
            "BUN_INSTALL",
        ):
            env.pop(name, None)
        env.update(
            {
                "HOME": str(workspace / "home"),
                "USERPROFILE": str(workspace / "home"),
                "TEMP": str(workspace / "temp"),
                "TMP": str(workspace / "temp"),
                "TMPDIR": str(workspace / "temp"),
                "XDG_CACHE_HOME": str(cache_path),
                "DENO_DIR": str(
                    shared_cache / "deno" if shared_cache else workspace / "deno"
                ),
                "DENO_NO_UPDATE_CHECK": "1",
                "YTDLP_NO_PLUGINS": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "NO_COLOR": "1",
            }
        )
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    except BaseException:
        factory.remove(workspace)
        raise
    started = time.perf_counter_ns()
    status = "completed"
    try:
        with (
            stdout_path.open("wb") as stdout_handle,
            stderr_path.open("wb") as stderr_handle,
            stdin_source.open("rb") as stdin_handle,
        ):
            process = subprocess.Popen(
                list(command),
                cwd=workspace,
                env=env,
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    status = "cancelled"
                    _terminate_process_tree(process)
                    break
                if time.monotonic() >= deadline:
                    status = "timeout"
                    _terminate_process_tree(process)
                    break
                if (
                    stdout_path.exists()
                    and stdout_path.stat().st_size > MAX_STDOUT_BYTES
                ) or (
                    stderr_path.exists()
                    and stderr_path.stat().st_size > MAX_STDERR_BYTES
                ):
                    status = "output_limit"
                    _terminate_process_tree(process)
                    break
                time.sleep(0.025)
        elapsed = time.perf_counter_ns() - started
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
            status = "output_limit"
            stdout = stdout[: MAX_STDOUT_BYTES + 1]
            stderr = stderr[: MAX_STDERR_BYTES + 1]
        return_code = process.returncode if process is not None else None
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process)
        factory.remove(workspace)
    return CommandResult(
        status=status,
        return_code=return_code,
        elapsed_ns=elapsed,
        stdout=stdout,
        stderr=stderr,
        cleanup_verified=True,
        workspace_fingerprint=fingerprint,
    )


def _video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    if parsed.path.startswith("/shorts/"):
        parts = parsed.path.split("/", 3)
        return parts[2] if len(parts) > 2 else ""
    return str(parse_qs(parsed.query).get("v", [""])[0])


def _normalize_required(capabilities: Iterable[str]) -> tuple[str, ...]:
    aliases = {
        "deno-runtime": "js-runtime",
        "deno-jsc": "js-challenge-runtime",
    }
    return tuple(sorted({aliases.get(value, value) for value in capabilities}))


def _failure_code(result: CommandResult) -> str:
    if result.status == "timeout":
        return "timeout"
    if result.status == "cancelled":
        return "cancelled"
    if result.status == "output_limit":
        return "output_limit"
    text = (result.stderr + b"\n" + result.stdout).decode("utf-8", errors="replace").casefold()
    rules = (
        ("http_429", ("http error 429", "too many requests")),
        ("http_403", ("http error 403", "forbidden")),
        ("login_required", ("sign in", "login required", "confirm you’re not a bot")),
        ("geo_restricted", ("not available in your country", "geo-restricted")),
        ("media_unavailable", ("video unavailable", "private video", "has been removed")),
        ("network_timeout", ("timed out", "timeout", "read operation timed out")),
        ("tls_failure", ("certificate verify failed", "ssl", "tls")),
        ("dns_failure", ("name resolution", "getaddrinfo", "could not resolve")),
        ("extractor_error", ("extractor error", "unable to extract")),
    )
    for code, needles in rules:
        if any(needle in text for needle in needles):
            return code
    return "unknown_failure"


def _probe_outcome(
    result: CommandResult,
    *,
    case_id: str,
    expected_video_id: str,
    runtime_kind: str,
    yt_dlp_version: str,
    ejs_version: str,
) -> ProbeOutcome:
    if result.status != "completed":
        return ProbeOutcome(
            status="fail",
            duration_ns=result.elapsed_ns,
            failure_code=_failure_code(result),
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ProbeOutcome(
            status="fail",
            duration_ns=result.elapsed_ns,
            failure_code="invalid_json",
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    if not isinstance(payload, dict):
        return ProbeOutcome(
            status="fail",
            duration_ns=result.elapsed_ns,
            failure_code="invalid_json",
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    duration_ns = payload.get("durationNs")
    policy = payload.get("policy")
    if (
        payload.get("probeContract") != FROZEN_PROBE_CONTRACT
        or payload.get("schemaVersion") != 1
        or payload.get("caseId") != case_id
        or payload.get("runtimeKind") != runtime_kind
        or payload.get("ytDlpVersion") != yt_dlp_version
        or payload.get("ejsVersion") != ejs_version
        or isinstance(duration_ns, bool)
        or not isinstance(duration_ns, int)
        or duration_ns < 0
        or duration_ns > result.elapsed_ns
        or policy
        != {
            "configDiscovery": False,
            "externalPlugins": False,
            "remoteComponents": False,
            "download": False,
            "explicitSingleRuntime": True,
        }
    ):
        return ProbeOutcome(
            status="fail",
            duration_ns=result.elapsed_ns,
            failure_code="probe_contract_invalid",
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    if payload.get("status") == "fail":
        failure_code = payload.get("failureCode")
        if (
            result.return_code != 1
            or not isinstance(failure_code, str)
            or not CASE_ID_RE.fullmatch(failure_code.replace("_", "-"))
        ):
            failure_code = "probe_contract_invalid"
        return ProbeOutcome(
            status="fail",
            duration_ns=duration_ns,
            failure_code=failure_code,
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    capabilities = payload.get("observedCapabilities")
    if (
        payload.get("status") != "pass"
        or result.return_code != 0
        or payload.get("videoId") != expected_video_id
        or not isinstance(capabilities, list)
        or not capabilities
        or capabilities != sorted(set(capabilities))
        or any(not isinstance(value, str) or not CASE_ID_RE.fullmatch(value) for value in capabilities)
    ):
        return ProbeOutcome(
            status="fail",
            duration_ns=duration_ns,
            failure_code="probe_contract_invalid",
            semantic_capabilities=(),
            cleanup_verified=result.cleanup_verified,
        )
    return ProbeOutcome(
        status="pass",
        duration_ns=duration_ns,
        failure_code=None,
        semantic_capabilities=tuple(capabilities),
        cleanup_verified=result.cleanup_verified,
    )


def classify_pair(
    baseline: ProbeOutcome,
    candidate: ProbeOutcome,
    *,
    required_capabilities: Iterable[str],
) -> tuple[str, str | None]:
    """Classify one immediate baseline/candidate pair without guessing."""
    required = set(_normalize_required(required_capabilities))
    if baseline.status == "pass" and candidate.status == "pass":
        baseline_caps = set(baseline.semantic_capabilities)
        candidate_caps = set(candidate.semantic_capabilities)
        if not required.issubset(baseline_caps):
            return "not_run", "baseline_capability_unproven"
        if not required.issubset(candidate_caps):
            return "fail", "candidate_capability_regression"
        if baseline_caps != candidate_caps:
            return "fail", "candidate_capability_parity_mismatch"
        if not baseline.cleanup_verified or not candidate.cleanup_verified:
            return "fail", "cleanup_unproven"
        return "pass", None
    if (
        baseline.status == "fail"
        and candidate.status == "fail"
        and baseline.failure_code
        and baseline.failure_code == candidate.failure_code
    ):
        return "external_invalid", baseline.failure_code
    if baseline.status == "pass" and candidate.status == "fail":
        return "fail", "candidate_probe_failed"
    if baseline.status == "fail" and candidate.status == "pass":
        return "not_run", "baseline_probe_failed"
    return "fail", "unpaired_failure"


def nearest_rank_p95(values: Sequence[int]) -> int:
    if not values or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
        raise HoldoutError("p95 requires non-negative integer samples")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def performance_gate(
    baseline_ns: Sequence[int], candidate_ns: Sequence[int]
) -> tuple[bool, dict[str, int]]:
    if len(baseline_ns) != len(candidate_ns) or len(baseline_ns) < 1:
        raise HoldoutError("paired performance samples are incomplete")
    baseline_p95 = nearest_rank_p95(baseline_ns)
    candidate_p95 = nearest_rank_p95(candidate_ns)
    limit = (baseline_p95 * MAX_P95_RATIO_BASIS_POINTS) // 10_000
    return candidate_p95 <= limit, {
        "baselineP95Ns": baseline_p95,
        "candidateP95Ns": candidate_p95,
        "maximumCandidateP95Ns": limit,
        "maximumRatioBasisPoints": MAX_P95_RATIO_BASIS_POINTS,
    }


def _stack_public(identity: StackIdentity) -> dict[str, Any]:
    return {
        "runtime": {
            "kind": identity.runtime.kind,
            "version": identity.runtime.version,
            "length": identity.runtime.length,
            "sha256": identity.runtime.sha256,
            "origin": identity.runtime.origin,
            "license": identity.runtime.license,
            "provenance": identity.runtime.provenance,
            "manifestSha256": identity.runtime.manifest_sha256,
            "provenanceLockEntry": identity.runtime.provenance_lock_entry,
            "provenanceLockSha256": identity.runtime.provenance_lock_sha256,
        },
        "ytDlp": {
            "version": identity.yt_dlp.version,
            "contentSha256": identity.yt_dlp.content_sha256,
            "origin": identity.yt_dlp.origin,
            "license": identity.yt_dlp.license,
        },
        "ejs": {
            "version": identity.ejs.version,
            "contentSha256": identity.ejs.content_sha256,
            "origin": identity.ejs.origin,
            "license": identity.ejs.license,
        },
        "ytDlpEjsComponentSha256": identity.component_content_sha256,
    }


def _runtime_self_tests(
    runtime: RuntimeIdentity, factory: PrivateWorkspaceFactory
) -> dict[str, Any]:
    _require_runtime_security_boundary(runtime)
    success = _run_bounded(
        _runtime_success_command(runtime),
        factory=factory,
        purpose="runtime-success",
        timeout_seconds=15,
    )
    version_output = success.stdout + b"\n" + success.stderr
    if (
        success.status != "completed"
        or not _runtime_version_return_code_ok(runtime, success.return_code)
        or not _runtime_version_from_output(runtime, version_output)
    ):
        raise HoldoutError("candidate runtime version command failed")
    error = _run_bounded(
        _runtime_error_command(runtime),
        factory=factory,
        purpose="runtime-error",
        timeout_seconds=15,
    )
    if error.status != "completed" or error.return_code in (None, 0):
        raise HoldoutError("candidate runtime error cleanup path is unproven")
    timeout = _run_bounded(
        _runtime_long_command(runtime),
        factory=factory,
        purpose="runtime-timeout",
        timeout_seconds=0.35,
    )
    if timeout.status != "timeout":
        raise HoldoutError("candidate runtime timeout cleanup path is unproven")
    cancel_event = threading.Event()
    timer = threading.Timer(0.15, cancel_event.set)
    timer.start()
    try:
        cancelled = _run_bounded(
            _runtime_long_command(runtime),
            factory=factory,
            purpose="runtime-cancel",
            timeout_seconds=15,
            cancel_event=cancel_event,
        )
    finally:
        timer.cancel()
    if cancelled.status != "cancelled":
        raise HoldoutError("candidate runtime cancellation cleanup path is unproven")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _run_bounded,
                _runtime_success_command(runtime),
                factory=factory,
                purpose="runtime-parallel",
                timeout_seconds=15,
            )
            for _ in range(2)
        ]
        parallel = [future.result() for future in futures]
    if any(
        item.status != "completed"
        or not _runtime_version_return_code_ok(runtime, item.return_code)
        or not _runtime_version_from_output(runtime, item.stdout + b"\n" + item.stderr)
        or not item.cleanup_verified
        for item in parallel
    ) or len({item.workspace_fingerprint for item in parallel}) != 2:
        raise HoldoutError("candidate runtime parallel isolation is unproven")
    return {
        "successCleanup": True,
        "errorCleanup": True,
        "timeoutCleanup": True,
        "cancellationCleanup": True,
        "parallelWorkers": 2,
        "parallelWorkspaceIsolation": True,
    }


def _probe_command(
    *,
    stack: StackIdentity,
    case: Mapping[str, Any],
    cache_dir: Path | None,
) -> tuple[list[str], bytes]:
    url = str(case["url"])
    request = {
        "requestContract": FROZEN_PROBE_CONTRACT,
        "schemaVersion": 1,
        "caseId": case["id"],
        "family": case["family"],
        "url": url,
        "expectedVideoId": _video_id_from_url(url),
        "runtimeKind": stack.runtime.kind,
        "runtimePath": str(stack.runtime.executable),
        "cacheMode": "warm" if cache_dir is not None else "cold",
    }
    return (
        [str(stack.backend_executable), "--installer-youtube-holdout-probe"],
        _canonical_json(request),
    )


def _probe(
    *,
    stack: StackIdentity,
    case: Mapping[str, Any],
    factory: PrivateWorkspaceFactory,
    timeout_seconds: int,
    purpose: str,
    cache_dir: Path | None,
) -> ProbeOutcome:
    _require_runtime_security_boundary(stack.runtime)
    url = str(case["url"])
    command, request = _probe_command(
        stack=stack,
        case=case,
        cache_dir=cache_dir,
    )
    result = _run_bounded(
        command,
        factory=factory,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        shared_cache=cache_dir,
        stdin_bytes=request,
    )
    return _probe_outcome(
        result,
        case_id=str(case["id"]),
        expected_video_id=_video_id_from_url(url),
        runtime_kind=stack.runtime.kind,
        yt_dlp_version=stack.yt_dlp.version,
        ejs_version=stack.ejs.version,
    )


def _case_rows(
    *,
    cases: Sequence[Mapping[str, Any]],
    baseline: StackIdentity,
    candidate: StackIdentity,
    factory: PrivateWorkspaceFactory,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], list[int], list[int], list[str]]:
    rows: list[dict[str, Any]] = []
    baseline_timings: list[int] = []
    candidate_timings: list[int] = []
    overall_reasons: list[str] = []
    for case in cases:
        case_id = str(case["id"])
        required = tuple(str(value) for value in case["requiredCapabilities"])
        warm_baseline = factory.create(f"{case_id}-baseline-warm")
        warm_candidate = factory.create(f"{case_id}-candidate-warm")
        pairs: list[dict[str, Any]] = []
        try:
            # Prime each warm cache with an immediate baseline/candidate pair.
            prime_baseline = _probe(
                stack=baseline,
                case=case,
                factory=factory,
                timeout_seconds=timeout_seconds,
                purpose=f"{case_id}-prime-baseline",
                cache_dir=warm_baseline,
            )
            prime_candidate = _probe(
                stack=candidate,
                case=case,
                factory=factory,
                timeout_seconds=timeout_seconds,
                purpose=f"{case_id}-prime-candidate",
                cache_dir=warm_candidate,
            )
            prime_status, prime_reason = classify_pair(
                prime_baseline, prime_candidate, required_capabilities=required
            )
            if prime_status != "pass":
                overall_reasons.append(
                    "external_invalid" if prime_status == "external_invalid" else str(prime_reason)
                )
            for mode, count in (("cold", MIN_COLD_PAIRS), ("warm", MIN_WARM_PAIRS)):
                for pair_index in range(1, count + 1):
                    baseline_probe = _probe(
                        stack=baseline,
                        case=case,
                        factory=factory,
                        timeout_seconds=timeout_seconds,
                        purpose=f"{case_id}-{mode}-baseline",
                        cache_dir=warm_baseline if mode == "warm" else None,
                    )
                    candidate_probe = _probe(
                        stack=candidate,
                        case=case,
                        factory=factory,
                        timeout_seconds=timeout_seconds,
                        purpose=f"{case_id}-{mode}-candidate",
                        cache_dir=warm_candidate if mode == "warm" else None,
                    )
                    status, reason = classify_pair(
                        baseline_probe,
                        candidate_probe,
                        required_capabilities=required,
                    )
                    if status == "pass":
                        baseline_timings.append(baseline_probe.duration_ns)
                        candidate_timings.append(candidate_probe.duration_ns)
                    else:
                        overall_reasons.append(
                            "external_invalid" if status == "external_invalid" else str(reason)
                        )
                    pairs.append(
                        {
                            "mode": mode,
                            "pairIndex": pair_index,
                            "order": ["baseline", "candidate"],
                            "status": status,
                            "reasonCode": reason,
                            "baselineDurationNs": baseline_probe.duration_ns,
                            "candidateDurationNs": candidate_probe.duration_ns,
                            "semanticCapabilities": list(
                                baseline_probe.semantic_capabilities
                                if status == "pass"
                                else ()
                            ),
                            "cleanupVerified": baseline_probe.cleanup_verified
                            and candidate_probe.cleanup_verified,
                        }
                    )
        finally:
            factory.remove(warm_baseline)
            factory.remove(warm_candidate)
        rows.append(
            {
                "id": case_id,
                "family": case["family"],
                "primeStatus": prime_status,
                "primeReasonCode": prime_reason,
                "coldPairCount": MIN_COLD_PAIRS,
                "warmPairCount": MIN_WARM_PAIRS,
                "pairs": pairs,
            }
        )
    return rows, baseline_timings, candidate_timings, overall_reasons


def _validate_fixture(
    fixture: Mapping[str, Any], snapshot: Mapping[str, Any], *, run_id: str
) -> list[Mapping[str, Any]]:
    if (
        fixture.get("schemaVersion") != 1
        or fixture.get("frozenCaseContract") is not True
        or fixture.get("pairing") != "deno-immediately-followed-by-candidate"
    ):
        raise HoldoutError("frozen YouTube fixture contract drifted")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or len(cases) != 6:
        raise HoldoutError("frozen YouTube fixture must contain exactly six cases")
    urls: set[str] = set()
    ids: set[str] = set()
    video_ids: set[str] = set()
    for raw in cases:
        if not isinstance(raw, dict):
            raise HoldoutError("frozen YouTube case is invalid")
        case_id = raw.get("id")
        url = raw.get("url")
        required = raw.get("requiredCapabilities")
        if (
            not isinstance(case_id, str)
            or not CASE_ID_RE.fullmatch(case_id)
            or case_id in ids
            or not isinstance(url, str)
            or not url.startswith("https://")
            or url in urls
            or not isinstance(required, list)
            or not required
            or any(not isinstance(value, str) or not CASE_ID_RE.fullmatch(value) for value in required)
        ):
            raise HoldoutError("frozen YouTube case is unsafe or duplicated")
        video_id = _video_id_from_url(url)
        if not video_id or video_id in video_ids:
            raise HoldoutError("frozen YouTube video identity is unsafe or duplicated")
        ids.add(case_id)
        urls.add(url)
        video_ids.add(video_id)
    snapshot_cases = snapshot.get("cases")
    if (
        snapshot.get("holdoutSnapshotContract") != "InstallerSizeYoutubeHoldoutsV1"
        or snapshot.get("schemaVersion") != 1
        or snapshot.get("runId") != run_id
        or snapshot.get("fixtureId") != fixture.get("fixtureId")
        or not isinstance(snapshot_cases, list)
        or [item.get("id") for item in snapshot_cases if isinstance(item, dict)]
        != [item["id"] for item in cases]
        or any(item.get("status") != "validated" for item in snapshot_cases if isinstance(item, dict))
    ):
        raise HoldoutError("run-local baseline YouTube snapshot is invalid")
    return cases


def _build_stack(
    *,
    label: str,
    root: Path,
    inventory: Mapping[str, Any],
    inventory_sha256: str,
    installed: bool,
    baseline_snapshot: Mapping[str, Any],
    environment_root: Path,
) -> StackIdentity:
    root = _plain_directory(root, label=f"{label} payload root")
    internal_root = _plain_directory(
        root / PurePosixPath(INTERNAL_RELATIVE), label=f"{label} installed module root"
    )
    relative, kind, executable, executable_item = _find_runtime(
        root=root, inventory=inventory, installed=installed, label=label
    )
    snapshot_runtime = baseline_snapshot.get("runtime")
    if not isinstance(snapshot_runtime, dict):
        raise HoldoutError("baseline snapshot runtime identity is missing")
    manifest_identity = _runtime_manifest_identity(
        root=root,
        inventory=inventory,
        installed=installed,
        relative=relative,
        kind=kind,
        executable=executable,
        executable_item=executable_item,
    )
    if manifest_identity is not None:
        runtime = manifest_identity
    elif (
        kind == "deno"
        and executable_item.get("sha256") == snapshot_runtime.get("sha256")
        and executable_item.get("length") == snapshot_runtime.get("length")
        and isinstance(snapshot_runtime.get("version"), str)
    ):
        origin, license_value = _baseline_runtime_provenance(
            environment_root=environment_root,
            expected_version=str(snapshot_runtime["version"]),
        )
        runtime = RuntimeIdentity(
            kind="deno",
            version=str(snapshot_runtime["version"]),
            executable=executable,
            length=int(executable_item["length"]),
            sha256=str(executable_item["sha256"]),
            origin=origin,
            license=license_value,
            provenance="run-local-baseline-distribution",
            manifest_sha256=None,
            provenance_lock_entry=None,
            provenance_lock_sha256=None,
        )
    else:
        raise HoldoutError(f"{label} changed runtime lacks a pinned provenance manifest")
    yt_dlp = _metadata_identity(
        root=root,
        inventory=inventory,
        distribution="yt-dlp",
        installed=installed,
    )
    ejs = _metadata_identity(
        root=root,
        inventory=inventory,
        distribution="yt-dlp-ejs",
        installed=installed,
    )
    backend_executable = _verify_inventory_file(
        root=root,
        inventory=inventory,
        relative=BACKEND_EXE_RELATIVE,
        installed=installed,
        label=f"{label} frozen backend executable",
    )
    return StackIdentity(
        label=label,
        root=root,
        backend_executable=backend_executable,
        inventory=inventory,
        inventory_sha256=inventory_sha256,
        runtime=runtime,
        yt_dlp=yt_dlp,
        ejs=ejs,
        component_content_sha256=_component_identity(inventory, "yt-dlp-ejs"),
    )


def _parallel_candidate_probe(
    *,
    candidate: StackIdentity,
    case: Mapping[str, Any],
    factory: PrivateWorkspaceFactory,
    timeout_seconds: int,
) -> dict[str, Any]:
    _require_runtime_security_boundary(candidate.runtime)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _probe,
                stack=candidate,
                case=case,
                factory=factory,
                timeout_seconds=timeout_seconds,
                purpose="candidate-parallel-probe",
                cache_dir=None,
            )
            for _ in range(2)
        ]
        outcomes = [future.result() for future in futures]
    required = set(_normalize_required(case["requiredCapabilities"]))
    if any(
        outcome.status != "pass"
        or not required.issubset(outcome.semantic_capabilities)
        or not outcome.cleanup_verified
        for outcome in outcomes
    ):
        raise HoldoutError("candidate parallel frozen-probe isolation is unproven")
    return {
        "workerCount": 2,
        "distinctPrivateWorkspaces": True,
        "capabilityParity": outcomes[0].semantic_capabilities
        == outcomes[1].semantic_capabilities,
        "cleanupVerified": True,
    }


def _assert_redacted(value: Any) -> None:
    forbidden_keys = {
        "url",
        "videoid",
        "path",
        "stdout",
        "stderr",
        "command",
        "tempdir",
        "workingdirectory",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in forbidden_keys:
                raise HoldoutError("candidate evidence contains a forbidden field")
            _assert_redacted(child)
    elif isinstance(value, list):
        for child in value:
            _assert_redacted(child)
    elif isinstance(value, str):
        lower = value.casefold()
        if "youtube.com/" in lower or "youtu.be/" in lower or "\\users\\" in lower:
            raise HoldoutError("candidate evidence contains unredacted source data")


def _scientific_status(reasons: Sequence[str], performance_ok: bool) -> tuple[str, list[str]]:
    bounded = sorted({reason for reason in reasons if reason})
    if any(reason == "external_invalid" or reason.startswith("baseline_") for reason in bounded):
        return "not_run", bounded
    if bounded:
        return "fail", bounded
    if not performance_ok:
        return "fail", ["candidate_p95_regression"]
    return "pass", []


def run_validator(args: argparse.Namespace) -> dict[str, Any]:
    run_id = _canonical_run_id(args.run_id)
    packet_id = _safe_id(args.packet_id, label="packet id")
    parent_id = _safe_id(
        args.parent_champion_id, label="parent champion id", allow_baseline=True
    )
    source_commit = _source_commit(args.source_commit)
    if not 30 <= args.timeout_seconds <= 300:
        raise HoldoutError("probe timeout must be between 30 and 300 seconds")
    repo_root = _plain_directory(args.repo_root, label="repository root")
    run_root = _plain_directory(
        repo_root / "autoresearch-results" / "installer-size" / run_id,
        label="installer research run root",
    )
    expected_output = run_root / "packet-evidence" / packet_id / "youtube-holdouts-candidate.json"
    if _absolute_entry(args.output) != _absolute_entry(expected_output):
        raise HoldoutError("candidate holdout output escaped its immutable packet location")
    fixture, fixture_sha = _load_object(args.fixture, label="frozen YouTube fixture")
    snapshot, snapshot_sha = _load_object(
        run_root / "preflight" / "youtube-holdouts.snapshot.json",
        label="run-local baseline YouTube snapshot",
    )
    cases = _validate_fixture(fixture, snapshot, run_id=run_id)
    baseline_inventory, baseline_inventory_sha = _load_object(
        args.baseline_inventory, label="baseline inventory"
    )
    candidate_inventory, candidate_inventory_sha = _load_object(
        args.candidate_inventory, label="candidate inventory"
    )
    parent_inventory, parent_inventory_sha = _load_object(
        args.parent_inventory, label="parent inventory"
    )
    _validate_inventory(
        baseline_inventory, run_id=run_id, source_commit=None, label="baseline"
    )
    _validate_inventory(
        candidate_inventory,
        run_id=run_id,
        source_commit=source_commit,
        label="candidate",
    )
    _validate_inventory(parent_inventory, run_id=run_id, source_commit=None, label="parent")
    _validate_replica_bindings(
        baseline_inventory=baseline_inventory,
        candidate_inventory=candidate_inventory,
        parent_inventory=parent_inventory,
        packet_id=packet_id,
        parent_id=parent_id,
    )
    environment_manifest, _environment_sha = _load_object(
        run_root / "environments" / "baseline" / "environment-manifest.json",
        label="baseline environment manifest",
    )
    python_executable = _plain_file(Path(sys.executable), label="research Python")
    python_identity = environment_manifest.get("python")
    if (
        environment_manifest.get("kind")
        != "scriber-installer-research-python-environment"
        or environment_manifest.get("runId") != run_id
        or environment_manifest.get("environmentName") != "baseline"
        or not isinstance(python_identity, dict)
        or python_identity.get("length") != python_executable.stat().st_size
        or python_identity.get("sha256") != _sha256_file(python_executable)
    ):
        raise HoldoutError("active research Python is not the run-local baseline environment")
    environment_root = _plain_directory(
        run_root / "environments" / "baseline", label="baseline environment root"
    )
    baseline = _build_stack(
        label="baseline",
        root=args.baseline_root,
        inventory=baseline_inventory,
        inventory_sha256=baseline_inventory_sha,
        installed=False,
        baseline_snapshot=snapshot,
        environment_root=environment_root,
    )
    candidate = _build_stack(
        label="candidate",
        root=args.candidate_root,
        inventory=candidate_inventory,
        inventory_sha256=candidate_inventory_sha,
        installed=True,
        baseline_snapshot=snapshot,
        environment_root=environment_root,
    )
    _require_runtime_security_boundary(candidate.runtime)
    scratch_root = _plain_directory(args.scratch_root, label="producer scratch root")
    input_before = {
        "baseline": _capture_bound_inputs(baseline, installed=False),
        "candidate": _capture_bound_inputs(candidate, installed=True),
    }
    factory = PrivateWorkspaceFactory(scratch_root)
    evidence: dict[str, Any] | None = None
    try:
        runtime_tests = _runtime_self_tests(candidate.runtime, factory)
        parallel = _parallel_candidate_probe(
            candidate=candidate,
            case=cases[0],
            factory=factory,
            timeout_seconds=args.timeout_seconds,
        )
        rows, baseline_ns, candidate_ns, reasons = _case_rows(
            cases=cases,
            baseline=baseline,
            candidate=candidate,
            factory=factory,
            timeout_seconds=args.timeout_seconds,
        )
        expected_samples = len(cases) * (MIN_COLD_PAIRS + MIN_WARM_PAIRS)
        performance_ok = False
        if len(baseline_ns) == expected_samples and len(candidate_ns) == expected_samples:
            performance_ok, performance = performance_gate(baseline_ns, candidate_ns)
        else:
            performance = {
                "baselineP95Ns": None,
                "candidateP95Ns": None,
                "maximumCandidateP95Ns": None,
                "maximumRatioBasisPoints": MAX_P95_RATIO_BASIS_POINTS,
            }
            reasons.append("paired_samples_incomplete")
        status, reason_codes = _scientific_status(reasons, performance_ok)
        input_after = {
            "baseline": _capture_bound_inputs(baseline, installed=False),
            "candidate": _capture_bound_inputs(candidate, installed=True),
        }
        if input_after != input_before:
            raise HoldoutError("YouTube runtime inputs changed during holdout execution")
        evidence = {
            "holdoutSnapshotContract": EVIDENCE_CONTRACT,
            "schemaVersion": SCHEMA_VERSION,
            "status": status,
            "reasonCodes": reason_codes,
            "capturedAtUtc": _utc_now(),
            "runId": run_id,
            "packetId": packet_id,
            "parentChampionId": parent_id,
            "sourceCommit": source_commit,
            "bindings": {
                "fixtureId": fixture.get("fixtureId"),
                "fixtureSha256": fixture_sha,
                "baselineSnapshotSha256": snapshot_sha,
                "baseline": _payload_binding(baseline_inventory, baseline_inventory_sha),
                "parent": _payload_binding(parent_inventory, parent_inventory_sha),
                "candidate": _payload_binding(candidate_inventory, candidate_inventory_sha),
            },
            "baseline": _stack_public(baseline),
            "candidate": _stack_public(candidate),
            "executionPolicy": {
                "pairing": "baseline-immediately-followed-by-candidate",
                "coldPairsPerCase": MIN_COLD_PAIRS,
                "warmPairsPerCase": MIN_WARM_PAIRS,
                "remoteComponents": False,
                "externalPlugins": False,
                "firstRunDownloads": False,
                "exactlyOneCandidateRuntime": True,
                "frozenBackendProbe": FROZEN_PROBE_CONTRACT,
                "privateRandomWorkspaces": True,
                "reparsePointsAllowed": False,
                "aclMode": factory.acl_mode,
            },
            "lifecycle": runtime_tests,
            "parallelIsolation": parallel,
            "performance": {
                **performance,
                "pairedSampleCount": len(baseline_ns),
                "passed": performance_ok,
            },
            "cases": rows,
            "inputIntegrity": {
                "baselineFileCount": input_after["baseline"].file_count,
                "baselineContentSha256": input_after["baseline"].content_sha256,
                "candidateFileCount": input_after["candidate"].file_count,
                "candidateContentSha256": input_after["candidate"].content_sha256,
                "verifiedAgainstInventoryBeforeAndAfter": True,
            },
            "inputImmutabilityVerified": True,
        }
    finally:
        factory.close()
    assert evidence is not None
    if factory.created_count != factory.cleanup_count:
        raise HoldoutError("private workspace cleanup accounting is incomplete")
    evidence["executionPolicy"]["workspaceCount"] = factory.created_count
    evidence["executionPolicy"]["cleanupCount"] = factory.cleanup_count
    _assert_redacted(evidence)
    _write_immutable(args.output, evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--parent-champion-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--baseline-inventory", type=Path, required=True)
    parser.add_argument("--parent-inventory", type=Path, required=True)
    parser.add_argument("--candidate-inventory", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("scripts/perf/profiles/installer-size/youtube-holdouts.json"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(values)
        evidence = run_validator(args)
        print(
            json.dumps(
                {
                    "ok": evidence["status"] == "pass",
                    "status": evidence["status"],
                    "reasonCodes": evidence["reasonCodes"],
                    "evidenceSha256": _sha256_file(args.output),
                },
                separators=(",", ":"),
            )
        )
        return 0 if evidence["status"] == "pass" else 1
    except (HoldoutError, OSError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "not_run",
                    "reasonCodes": ["holdout_boundary_invalid"],
                    "error": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
