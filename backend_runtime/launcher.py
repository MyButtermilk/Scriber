"""Frozen launcher for the separately staged Scriber application layer."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from backend_runtime.contract import (
    APPLICATION_DIRECTORY_NAME,
    APPLICATION_ENTRY_POINT,
    APPLICATION_LAYER_SCHEMA_VERSION,
    APPLICATION_MANIFEST_NAME,
    REQUIRED_PACKAGE_VERSIONS,
    RUNTIME_CONTRACT_NAME,
    RUNTIME_CONTRACT_REVISION,
    RUNTIME_LAYER_SCHEMA_VERSION,
    RUNTIME_MANIFEST_NAME,
    RUNTIME_REQUIRED_IMPORTS,
)


class LayerValidationError(RuntimeError):
    """Raised when installed runtime and application layers do not match."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise LayerValidationError(f"{label} is missing or invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise LayerValidationError(f"{label} must be a JSON object: {path.name}")
    return value


def _contract(payload: dict[str, Any], label: str) -> dict[str, Any]:
    value = payload.get("runtimeContract")
    if not isinstance(value, dict):
        raise LayerValidationError(f"{label} has no runtime contract.")
    if value.get("name") != RUNTIME_CONTRACT_NAME:
        raise LayerValidationError(f"{label} targets an incompatible runtime.")
    if value.get("revision") != RUNTIME_CONTRACT_REVISION:
        raise LayerValidationError(f"{label} targets an incompatible runtime revision.")
    return value


def validate_runtime_layer(
    runtime_root: Path,
    *,
    executable_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the frozen runtime identity before loading external code."""

    manifest = _load_object(runtime_root / RUNTIME_MANIFEST_NAME, "Runtime manifest")
    if manifest.get("schemaVersion") != RUNTIME_LAYER_SCHEMA_VERSION:
        raise LayerValidationError("Runtime manifest schema is unsupported.")
    if manifest.get("name") != "scriber-backend-runtime-layer":
        raise LayerValidationError("Runtime manifest name is unsupported.")
    _contract(manifest, "Runtime manifest")

    cache_key = manifest.get("cacheKey")
    if not isinstance(cache_key, str) or len(cache_key) != 64:
        raise LayerValidationError("Runtime cache identity is invalid.")
    try:
        int(cache_key, 16)
    except ValueError as exc:
        raise LayerValidationError("Runtime cache identity is invalid.") from exc

    executable = (executable_path or Path(sys.executable)).resolve()
    identity = manifest.get("executable")
    if not isinstance(identity, dict):
        raise LayerValidationError("Runtime executable identity is missing.")
    if identity.get("length") != executable.stat().st_size:
        raise LayerValidationError("Runtime executable length does not match its manifest.")
    if identity.get("sha256") != _sha256(executable):
        raise LayerValidationError("Runtime executable checksum does not match its manifest.")
    content = manifest.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("fileCount"), int):
        raise LayerValidationError("Runtime content identity is missing.")
    files = content.get("files")
    if (
        not isinstance(files, list)
        or not files
        or len(files) != content["fileCount"]
        or len(files) > 32768
    ):
        raise LayerValidationError("Runtime file identity list is missing or invalid.")
    seen_runtime_paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise LayerValidationError("Runtime file identity entry is invalid.")
        raw_path = entry.get("path")
        length = entry.get("length")
        checksum = entry.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or Path(raw_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(raw_path).parts)
            or raw_path in seen_runtime_paths
            or not isinstance(length, int)
            or length < 0
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            raise LayerValidationError("Runtime file identity entry is invalid.")
        try:
            int(checksum, 16)
        except ValueError as exc:
            raise LayerValidationError("Runtime file identity entry is invalid.") from exc
        seen_runtime_paths.add(raw_path)
    tree_sha256 = content.get("treeSha256")
    if not isinstance(tree_sha256, str) or len(tree_sha256) != 64:
        raise LayerValidationError("Runtime content identity is invalid.")
    try:
        int(tree_sha256, 16)
    except ValueError as exc:
        raise LayerValidationError("Runtime content identity is invalid.") from exc
    return manifest


def _safe_application_path(app_root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise LayerValidationError("Application manifest contains an invalid path.")
    relative = Path(raw_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise LayerValidationError("Application manifest contains an unsafe path.")
    candidate = (app_root / relative).resolve()
    try:
        candidate.relative_to(app_root.resolve())
    except ValueError as exc:
        raise LayerValidationError("Application manifest path escapes its layer.") from exc
    return candidate


def validate_application_layer(
    runtime_root: Path,
    runtime_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Validate every first-party application file and runtime binding."""

    app_root = runtime_root / APPLICATION_DIRECTORY_NAME
    manifest = _load_object(app_root / APPLICATION_MANIFEST_NAME, "Application manifest")
    if manifest.get("schemaVersion") != APPLICATION_LAYER_SCHEMA_VERSION:
        raise LayerValidationError("Application manifest schema is unsupported.")
    if manifest.get("name") != "scriber-backend-application-layer":
        raise LayerValidationError("Application manifest name is unsupported.")
    if manifest.get("entryPoint") != APPLICATION_ENTRY_POINT:
        raise LayerValidationError("Application entry point is unsupported.")
    _contract(manifest, "Application manifest")
    if manifest.get("runtimeCacheKey") != runtime_manifest.get("cacheKey"):
        raise LayerValidationError("Application and frozen runtime layers do not match.")
    version = manifest.get("applicationVersion")
    if not isinstance(version, str) or not version.strip():
        raise LayerValidationError("Application version is missing.")

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > 4096:
        raise LayerValidationError("Application file manifest is invalid.")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise LayerValidationError("Application file entry is invalid.")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            raise LayerValidationError("Application file entry path is invalid.")
        if raw_path in seen:
            raise LayerValidationError("Application file manifest contains duplicates.")
        seen.add(raw_path)
        path = _safe_application_path(app_root, raw_path)
        if not path.is_file():
            raise LayerValidationError(f"Application file is missing: {raw_path}")
        if entry.get("length") != path.stat().st_size:
            raise LayerValidationError(f"Application file length mismatch: {raw_path}")
        if entry.get("sha256") != _sha256(path):
            raise LayerValidationError(f"Application file checksum mismatch: {raw_path}")

    actual = {
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*")
        if path.is_file()
        and path.relative_to(app_root).as_posix() != APPLICATION_MANIFEST_NAME
    }
    if actual != seen:
        raise LayerValidationError("Application layer contains unlisted or missing files.")
    return manifest


def check_imports(
    imports: Iterable[tuple[str, str]] = RUNTIME_REQUIRED_IMPORTS,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for module_name, reason in imports:
        try:
            import_module(module_name)
        except Exception as exc:
            missing.append(
                {
                    "module": module_name,
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return missing


def check_package_versions(
    requirements: Iterable[tuple[str, str]] = REQUIRED_PACKAGE_VERSIONS,
    version_for: Callable[[str], str] = importlib.metadata.version,
) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for package_name, expected_version in requirements:
        try:
            installed_version = version_for(package_name)
        except Exception as exc:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if installed_version != expected_version:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"VersionMismatch: installed {installed_version}",
                }
            )
    return mismatches


def run_runtime_layer_check() -> int:
    missing = [*check_imports(), *check_package_versions()]
    print(json.dumps({"ok": not missing, "missing": missing}, separators=(",", ":")))
    return 1 if missing else 0


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def launch_application(runtime_root: Path | None = None) -> int:
    # The application layer is an exact, checksummed file set.  Never let a
    # physical source import add unlisted ``__pycache__`` files beside it.
    sys.dont_write_bytecode = True
    root = (runtime_root or _runtime_root()).resolve()
    runtime_manifest = validate_runtime_layer(root)
    app_manifest = validate_application_layer(root, runtime_manifest)
    app_root = root / APPLICATION_DIRECTORY_NAME
    app_root_text = os.fspath(app_root)
    if app_root_text not in sys.path:
        sys.path.insert(0, app_root_text)

    version_module = importlib.import_module("src.version")
    if getattr(version_module, "__version__", None) != app_manifest["applicationVersion"]:
        raise LayerValidationError("Application code version does not match its manifest.")
    worker = importlib.import_module("src.backend_worker")
    main = getattr(worker, "main", None)
    if not callable(main):
        raise LayerValidationError("Application entry point is not callable.")
    return int(main())


def main() -> int:
    if "--runtime-layer-check" in sys.argv:
        return run_runtime_layer_check()
    try:
        return launch_application()
    except LayerValidationError as exc:
        print(
            json.dumps(
                {"ok": False, "error": "backend_layer_validation_failed", "message": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
