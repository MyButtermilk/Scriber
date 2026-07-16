from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend_runtime.contract import (
    APPLICATION_DIRECTORY_NAME,
    APPLICATION_ENTRY_POINT,
    APPLICATION_LAYER_SCHEMA_VERSION,
    APPLICATION_MANIFEST_NAME,
    RUNTIME_CONTRACT_NAME,
    RUNTIME_CONTRACT_REVISION,
)
from backend_runtime.launcher import validate_application_layer


_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _application_version(repo_root: Path) -> str:
    match = _VERSION_PATTERN.search((repo_root / "src" / "version.py").read_text(encoding="utf-8"))
    if not match:
        raise ValueError("src/version.py does not contain a canonical __version__ assignment")
    return match.group(1)


def _source_files(repo_root: Path) -> list[tuple[Path, Path]]:
    requested = (
        "src",
        "scripts/__init__.py",
        "scripts/check_backend_runtime_imports.py",
    )
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--", *requested],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("Could not enumerate the tracked backend application files.")
    relatives = sorted({Path(line) for line in result.stdout.splitlines() if line.strip()})
    required = {Path("scripts/__init__.py"), Path("scripts/check_backend_runtime_imports.py")}
    if not required.issubset(relatives):
        missing = ", ".join(sorted(path.as_posix() for path in required - set(relatives)))
        raise FileNotFoundError(f"Required tracked application-layer file is missing: {missing}")

    files: list[tuple[Path, Path]] = []
    resolved_repo = repo_root.resolve()
    for relative in relatives:
        source = (repo_root / relative).resolve()
        try:
            source.relative_to(resolved_repo)
        except ValueError as exc:
            raise ValueError(f"Tracked application path escapes the repository: {relative}") from exc
        if not source.is_file():
            raise FileNotFoundError(f"Tracked application-layer file is missing: {relative.as_posix()}")
        if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
            raise ValueError(f"Bytecode must not be tracked in the application layer: {relative.as_posix()}")
        files.append((source, relative))
    return files


def stage_application_layer(
    repo_root: Path,
    backend_root: Path,
    *,
    runtime_cache_key: str,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    backend_root = backend_root.resolve()
    if not re.fullmatch(r"[0-9a-f]{64}", runtime_cache_key):
        raise ValueError("runtime_cache_key must be a lowercase SHA-256 digest")

    app_root = backend_root / APPLICATION_DIRECTORY_NAME
    staging = backend_root / f".{APPLICATION_DIRECTORY_NAME}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for source, relative in _source_files(repo_root):
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        entries: list[dict[str, Any]] = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            entries.append(
                {
                    "path": path.relative_to(staging).as_posix(),
                    "length": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        manifest: dict[str, Any] = {
            "schemaVersion": APPLICATION_LAYER_SCHEMA_VERSION,
            "name": "scriber-backend-application-layer",
            "applicationVersion": _application_version(repo_root),
            "entryPoint": APPLICATION_ENTRY_POINT,
            "runtimeContract": {
                "name": RUNTIME_CONTRACT_NAME,
                "revision": RUNTIME_CONTRACT_REVISION,
            },
            "runtimeCacheKey": runtime_cache_key,
            "files": entries,
        }
        (staging / APPLICATION_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if app_root.exists():
            shutil.rmtree(app_root)
        staging.replace(app_root)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def validate_staged_application_layer(
    backend_root: Path,
    *,
    runtime_cache_key: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", runtime_cache_key):
        raise ValueError("runtime_cache_key must be a lowercase SHA-256 digest")
    return validate_application_layer(
        backend_root.resolve(),
        {"cacheKey": runtime_cache_key},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--backend-root", required=True, type=Path)
    parser.add_argument("--runtime-cache-key", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    if args.validate_only:
        manifest = validate_staged_application_layer(
            args.backend_root,
            runtime_cache_key=args.runtime_cache_key,
        )
    else:
        if args.repo_root is None:
            parser.error("--repo-root is required unless --validate-only is used")
        manifest = stage_application_layer(
            args.repo_root,
            args.backend_root,
            runtime_cache_key=args.runtime_cache_key,
        )
    payload = {
        "ok": True,
        "applicationVersion": manifest["applicationVersion"],
        "runtimeCacheKey": manifest["runtimeCacheKey"],
        "fileCount": len(manifest["files"]),
        "validatedOnly": args.validate_only,
    }
    rendered = json.dumps(payload, separators=(",", ":"))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
