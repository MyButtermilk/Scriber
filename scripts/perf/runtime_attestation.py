from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable


MANIFEST_NAME = "scriber-autoresearch-runtime-attestation.json"
MANIFEST_KIND = "scriber-autoresearch-runtime-attestation"
SCHEMA_VERSION = 1

COMPONENT_PATHS = {
    "desktop": "scriber-desktop.exe",
    "backend": "backend/scriber-backend.exe",
    "audioSidecar": "scriber-audio-sidecar.exe",
}

# Build products and benchmark output must not make the source identity change
# after a measurement. All tracked and non-ignored untracked source files not
# covered by these explicit exclusions participate in the digest.
EXCLUDED_PATH_PREFIXES = ("benchmarks/results/",)
EXCLUDED_PATH_SEGMENTS = frozenset(
    {"build", "tmp", "target", "targets", "venv", ".venv", "node_modules"}
)


class AttestationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _VsFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("signature", ctypes.c_uint32),
        ("struct_version", ctypes.c_uint32),
        ("file_version_ms", ctypes.c_uint32),
        ("file_version_ls", ctypes.c_uint32),
        ("product_version_ms", ctypes.c_uint32),
        ("product_version_ls", ctypes.c_uint32),
        ("file_flags_mask", ctypes.c_uint32),
        ("file_flags", ctypes.c_uint32),
        ("file_os", ctypes.c_uint32),
        ("file_type", ctypes.c_uint32),
        ("file_subtype", ctypes.c_uint32),
        ("file_date_ms", ctypes.c_uint32),
        ("file_date_ls", ctypes.c_uint32),
    ]


def read_windows_file_version(path: Path) -> str:
    """Read the native numeric PE version without spawning PowerShell."""
    if os.name != "nt" or not path.is_file():
        return ""
    version_dll = ctypes.windll.version
    ignored = ctypes.c_uint32(0)
    size = version_dll.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ""
    value = ctypes.c_void_p()
    value_size = ctypes.c_uint32(0)
    if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(value_size)):
        return ""
    if value_size.value < ctypes.sizeof(_VsFixedFileInfo):
        return ""
    info = ctypes.cast(value, ctypes.POINTER(_VsFixedFileInfo)).contents
    parts = [
        info.file_version_ms >> 16,
        info.file_version_ms & 0xFFFF,
        info.file_version_ls >> 16,
        info.file_version_ls & 0xFFFF,
    ]
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(part) for part in parts)


def _run_git(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError("git_failed", message or f"git {' '.join(args)} failed")
    return result.stdout


def _is_excluded_source_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").lstrip("./")
    lowered = normalized.casefold()
    if lowered == MANIFEST_NAME.casefold():
        return True
    if any(lowered.startswith(prefix.casefold()) for prefix in EXCLUDED_PATH_PREFIXES):
        return True
    return any(part.casefold() in EXCLUDED_PATH_SEGMENTS for part in PurePosixPath(normalized).parts)


def _hash_length_prefixed(hasher: Any, value: bytes) -> None:
    hasher.update(struct.pack(">Q", len(value)))
    hasher.update(value)


def source_identity(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    head = _run_git(repo_root, "rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    if len(head) != 40:
        raise AttestationError("invalid_git_head", f"Unexpected Git HEAD: {head!r}")

    listed = _run_git(
        repo_root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    relative_paths = sorted(
        {
            raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            for raw in listed.split(b"\0")
            if raw
        },
        key=lambda value: value.encode("utf-8", errors="surrogateescape"),
    )

    digest = hashlib.sha256()
    digest.update(b"scriber-source-content-v1\0")
    included = 0
    for relative_path in relative_paths:
        if _is_excluded_source_path(relative_path):
            continue
        parts = PurePosixPath(relative_path).parts
        if not parts or ".." in parts or PurePosixPath(relative_path).is_absolute():
            raise AttestationError("unsafe_source_path", f"Unsafe Git path: {relative_path!r}")
        path = repo_root.joinpath(*parts)
        encoded_path = relative_path.encode("utf-8", errors="surrogateescape")
        _hash_length_prefixed(digest, encoded_path)
        if path.is_symlink():
            digest.update(b"L")
            _hash_length_prefixed(digest, os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            digest.update(b"F")
            file_digest = hashlib.sha256()
            file_size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    file_digest.update(chunk)
                    file_size += len(chunk)
            digest.update(struct.pack(">Q", file_size))
            digest.update(file_digest.digest())
        else:
            # Tracked deletions are part of the working-tree identity too.
            digest.update(b"M")
        included += 1

    return {
        "head": head,
        "contentSha256": digest.hexdigest(),
        "fileCount": included,
        "algorithm": "git-worktree-sha256-v1",
        "exclusions": {
            "prefixes": list(EXCLUDED_PATH_PREFIXES),
            "segments": sorted(EXCLUDED_PATH_SEGMENTS),
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _application_version(repo_root: Path) -> str:
    path = repo_root / "Frontend" / "package.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = str(payload["version"]).strip()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AttestationError("invalid_application_version", f"Could not read {path}: {exc}") from exc
    if not version:
        raise AttestationError("invalid_application_version", f"Empty application version in {path}")
    return version


def _component_snapshot(
    install_root: Path,
    name: str,
    relative_path: str,
    version_reader: Callable[[Path], str],
) -> dict[str, Any]:
    path = install_root.joinpath(*PurePosixPath(relative_path).parts)
    if not path.is_file():
        raise AttestationError("missing_component", f"Missing {name} component: {relative_path}")
    before = path.stat()
    sha256 = _sha256_file(path)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise AttestationError("component_changed_during_hash", f"{name} changed while it was being hashed")
    return {
        "relativePath": relative_path,
        "sha256": sha256,
        "sizeBytes": after.st_size,
        "peVersion": version_reader(path),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_attestation(
    repo_root: Path,
    install_root: Path,
    *,
    version_reader: Callable[[Path], str] = read_windows_file_version,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    install_root = install_root.resolve()
    if not install_root.is_dir():
        raise AttestationError("missing_install_root", f"Install root does not exist: {install_root}")

    expected_version = _application_version(repo_root)
    source = source_identity(repo_root)
    components = {
        name: _component_snapshot(install_root, name, relative_path, version_reader)
        for name, relative_path in COMPONENT_PATHS.items()
    }
    for name in ("desktop", "audioSidecar"):
        actual_version = components[name]["peVersion"]
        if os.name == "nt" and not actual_version:
            raise AttestationError("missing_component_version", f"Could not read the PE version for {name}")
        if actual_version and actual_version != expected_version:
            raise AttestationError(
                "component_version_mismatch",
                f"{name} version {actual_version} does not match source version {expected_version}",
            )

    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "createdAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "explicit-post-build-snapshot",
        "applicationVersion": expected_version,
        "source": source,
        "components": components,
    }
    payload["attestationId"] = _canonical_hash(payload)
    manifest_path = install_root / MANIFEST_NAME
    _atomic_write_json(manifest_path, payload)
    return {
        "ok": True,
        "manifestPath": str(manifest_path),
        "manifestSha256": _sha256_file(manifest_path),
        "attestationId": payload["attestationId"],
        "source": source,
        "components": components,
    }


def _error(code: str, message: str, *, component: str = "") -> dict[str, str]:
    value = {"code": code, "message": message}
    if component:
        value["component"] = component
    return value


def verify_attestation(
    repo_root: Path,
    install_root: Path,
    *,
    version_reader: Callable[[Path], str] = read_windows_file_version,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    install_root = install_root.resolve()
    manifest_path = install_root / MANIFEST_NAME
    errors: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "ok": False,
        "manifestPresent": manifest_path.is_file(),
        "manifestPath": str(manifest_path),
        "manifestSha256": "",
        "attestationId": "",
        "sourceContentSha256": "",
        "errors": errors,
    }
    if not manifest_path.is_file():
        errors.append(_error("missing_attestation", f"Missing runtime attestation: {MANIFEST_NAME}"))
        return result

    try:
        raw = manifest_path.read_bytes()
        result["manifestSha256"] = hashlib.sha256(raw).hexdigest()
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(_error("invalid_attestation_json", str(exc)))
        return result
    if not isinstance(manifest, dict):
        errors.append(_error("invalid_attestation_shape", "Runtime attestation must be a JSON object"))
        return result

    if manifest.get("schemaVersion") != SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
        errors.append(_error("invalid_attestation_contract", "Unsupported runtime attestation contract"))
    attestation_id = str(manifest.get("attestationId") or "")
    result["attestationId"] = attestation_id
    unsigned = dict(manifest)
    unsigned.pop("attestationId", None)
    if not attestation_id or _canonical_hash(unsigned) != attestation_id:
        errors.append(_error("attestation_integrity_mismatch", "Runtime attestation payload hash does not match"))

    try:
        current_source = source_identity(repo_root)
        expected_version = _application_version(repo_root)
    except AttestationError as exc:
        errors.append(_error(exc.code, str(exc)))
        return result
    result["sourceContentSha256"] = current_source["contentSha256"]
    recorded_source = manifest.get("source")
    if not isinstance(recorded_source, dict):
        errors.append(_error("missing_source_identity", "Runtime attestation has no source identity"))
    else:
        if recorded_source.get("head") != current_source["head"]:
            errors.append(_error("source_head_mismatch", "Git HEAD differs from the attested build source"))
        if recorded_source.get("contentSha256") != current_source["contentSha256"]:
            errors.append(_error("source_content_mismatch", "Working-tree content differs from the attested build source"))
        if recorded_source.get("fileCount") != current_source["fileCount"]:
            errors.append(_error("source_file_set_mismatch", "Working-tree file set differs from the attested build source"))
    if manifest.get("applicationVersion") != expected_version:
        errors.append(_error("application_version_mismatch", "Source application version differs from the attestation"))

    recorded_components = manifest.get("components")
    if not isinstance(recorded_components, dict):
        errors.append(_error("missing_components", "Runtime attestation has no component map"))
        recorded_components = {}
    if set(recorded_components) != set(COMPONENT_PATHS):
        errors.append(_error("component_set_mismatch", "Runtime attestation component set is incomplete or unexpected"))

    for name, relative_path in COMPONENT_PATHS.items():
        recorded = recorded_components.get(name)
        if not isinstance(recorded, dict) or recorded.get("relativePath") != relative_path:
            errors.append(_error("invalid_component_entry", f"Invalid attestation entry for {name}", component=name))
            continue
        path = install_root.joinpath(*PurePosixPath(relative_path).parts)
        if not path.is_file():
            errors.append(_error("missing_component", f"Missing {name} component: {relative_path}", component=name))
            continue
        actual_sha256 = _sha256_file(path)
        if recorded.get("sha256") != actual_sha256:
            errors.append(_error("component_hash_mismatch", f"{name} SHA-256 differs from the attestation", component=name))
        if recorded.get("sizeBytes") != path.stat().st_size:
            errors.append(_error("component_size_mismatch", f"{name} size differs from the attestation", component=name))
        actual_version = version_reader(path)
        if recorded.get("peVersion") != actual_version:
            errors.append(_error("component_version_changed", f"{name} PE version differs from the attestation", component=name))
        if name in {"desktop", "audioSidecar"} and actual_version and actual_version != expected_version:
            errors.append(_error("component_version_mismatch", f"{name} version differs from source", component=name))

    result["ok"] = not errors
    return result


def _cli_payload(operation: Callable[[], dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    try:
        payload = operation()
        return (0 if payload.get("ok") else 2), payload
    except AttestationError as exc:
        return 2, {"ok": False, "errors": [_error(exc.code, str(exc))]}
    except Exception as exc:  # Fail closed, but keep the CLI result machine-readable.
        return 2, {"ok": False, "errors": [_error("attestation_internal_error", str(exc))]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Write or verify the explicit post-build runtime snapshot required by local Scriber AutoResearch. "
            "Writing is a deliberate build/staging step; benchmark execution only verifies it."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("write", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo-root", required=True)
        child.add_argument("--install-root", required=True)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    install_root = Path(args.install_root)
    if args.command == "write":
        code, payload = _cli_payload(lambda: write_attestation(repo_root, install_root))
    else:
        code, payload = _cli_payload(lambda: verify_attestation(repo_root, install_root))
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
