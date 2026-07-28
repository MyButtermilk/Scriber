#!/usr/bin/env python3
"""Validate the exact CPython 3.13 -> 3.14 installer transition contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_CONTRACT = "ScriberPython314UpgradeManifestV1"
EXPECTED_REPOSITORY = "MyButtermilk/Scriber"
EXPECTED_RELEASE_ID = 360523309
EXPECTED_SOURCE_TAG = "v0.5.47"
EXPECTED_SOURCE_SHA = "aa918d3d111dab834e05e5b588c24da503f9176d"
EXPECTED_ASSET_ID = 491618831
EXPECTED_INSTALLER_NAME = "Scriber_0.5.47_x64-setup.exe"
EXPECTED_INSTALLER_LENGTH = 79787381
EXPECTED_INSTALLER_SHA256 = "a4c242935e2eb26d6ab2517a38538b4942f204477cb745ef6c7909312efdd8f5"
PY313_ABI_RE = re.compile(r"(?:python313\.dll|\.cp313-win_amd64\.pyd)\Z")
PY313_RUNTIME_RE = re.compile(r"(?:api-ms-win-(?:core|crt)-[a-z0-9-]+|ucrtbase)\.dll\Z")
SAFE_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_safe_internal_path(raw_path: Any, *, label: str) -> PurePosixPath:
    if not isinstance(raw_path, str):
        raise ValueError(f"{label} must be a string")
    path = PurePosixPath(raw_path)
    if (
        "\\" in raw_path
        or raw_path != path.as_posix()
        or path.is_absolute()
        or not raw_path.startswith("_internal/")
        or any(part in {"", ".", ".."} or not SAFE_PATH_SEGMENT_RE.fullmatch(part) for part in path.parts)
    ):
        raise ValueError(f"unsafe {label}: {raw_path!r}")
    return path


def _validate_installer_hook_delete_safety(hooks: str) -> None:
    for line_number, raw_line in enumerate(hooks.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if not re.match(r"(?i)^(?:Delete|RMDir)\b", line):
            continue
        if "*" in line or "?" in line:
            raise ValueError(f"installer hook contains wildcard deletion at line {line_number}")
        if re.match(r"(?i)^RMDir\s+/r(?:\s|$)", line):
            raise ValueError(f"installer hook contains recursive deletion at line {line_number}")


def validate_manifest(
    manifest_path: Path,
    installer_hooks_path: Path,
    *,
    installer_path: Path | None = None,
) -> dict[str, Any]:
    payload = _require_dict(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    if payload.get("contract") != EXPECTED_CONTRACT or payload.get("schemaVersion") != 1:
        raise ValueError("unexpected Python 3.14 upgrade manifest contract")

    source = _require_dict(payload.get("sourceRelease"), "sourceRelease")
    installer = _require_dict(source.get("installer"), "sourceRelease.installer")
    if (
        source.get("repository") != EXPECTED_REPOSITORY
        or source.get("releaseId") != EXPECTED_RELEASE_ID
        or source.get("tag") != EXPECTED_SOURCE_TAG
        or source.get("commitSha") != EXPECTED_SOURCE_SHA
    ):
        raise ValueError("upgrade source release identity is not the public v0.5.47 anchor")
    if (
        installer.get("assetId") != EXPECTED_ASSET_ID
        or installer.get("name") != EXPECTED_INSTALLER_NAME
        or installer.get("length") != EXPECTED_INSTALLER_LENGTH
        or installer.get("sha256") != EXPECTED_INSTALLER_SHA256
    ):
        raise ValueError("upgrade source installer identity is not the public v0.5.47 asset")

    paths = payload.get("obsoletePython313Files")
    if not isinstance(paths, list) or len(paths) != 33:
        raise ValueError("obsoletePython313Files must contain exactly 33 paths")
    if paths != sorted(set(paths), key=paths.index):
        raise ValueError("obsoletePython313Files must be unique")

    hooks = installer_hooks_path.read_text(encoding="utf-8")
    _validate_installer_hook_delete_safety(hooks)
    for raw_path in paths:
        path = _require_safe_internal_path(raw_path, label="obsolete path")
        if not PY313_ABI_RE.search(path.name):
            raise ValueError(f"path is not a CPython 3.13 ABI artifact: {raw_path!r}")
        nsis_path = raw_path.replace("/", "\\")
        exact_delete = f'Delete "$INSTDIR\\backend\\{nsis_path}"'
        if hooks.count(exact_delete) != 1:
            raise ValueError(f"installer hook is missing exact tombstone: {raw_path}")

    runtime_paths = payload.get("obsoletePython313RuntimeFiles")
    if not isinstance(runtime_paths, list) or len(runtime_paths) != 43:
        raise ValueError("obsoletePython313RuntimeFiles must contain exactly 43 paths")
    if runtime_paths != sorted(set(runtime_paths), key=runtime_paths.index):
        raise ValueError("obsoletePython313RuntimeFiles must be unique")
    if set(paths) & set(runtime_paths):
        raise ValueError("obsolete Python 3.13 file groups must not overlap")
    for raw_path in runtime_paths:
        path = _require_safe_internal_path(raw_path, label="obsolete runtime path")
        if not PY313_RUNTIME_RE.fullmatch(path.name):
            raise ValueError(f"path is not an allowlisted CPython 3.13 runtime artifact: {raw_path!r}")
        nsis_path = raw_path.replace("/", "\\")
        exact_delete = f'Delete "$INSTDIR\\backend\\{nsis_path}"'
        if hooks.count(exact_delete) != 1:
            raise ValueError(f"installer hook is missing exact runtime tombstone: {raw_path}")

    runtime_directories = payload.get("obsoletePython313RuntimeDirectories")
    if runtime_directories != ["_internal/websockets"]:
        raise ValueError("obsoletePython313RuntimeDirectories must bind the exact known empty directory")
    for raw_path in runtime_directories:
        _require_safe_internal_path(raw_path, label="obsolete runtime directory")
        nsis_path = raw_path.replace("/", "\\")
        exact_rmdir = f'RMDir "$INSTDIR\\backend\\{nsis_path}"'
        if hooks.count(exact_rmdir) != 1:
            raise ValueError(f"installer hook is missing exact runtime directory tombstone: {raw_path}")

    obsolete_tools = payload.get("obsoleteBundledToolFiles")
    if obsolete_tools != ["tools/ffmpeg/deno.exe", "tools/ffmpeg/yt-dlp.exe"]:
        raise ValueError("obsoleteBundledToolFiles must bind the exact known superseded tools")
    for raw_path in obsolete_tools:
        path = PurePosixPath(raw_path)
        if (
            "\\" in raw_path
            or raw_path != path.as_posix()
            or path.parts[:2] != ("tools", "ffmpeg")
            or len(path.parts) != 3
            or any(part in {"", ".", ".."} or not SAFE_PATH_SEGMENT_RE.fullmatch(part) for part in path.parts)
        ):
            raise ValueError(f"unsafe obsolete bundled tool path: {raw_path!r}")
        nsis_path = raw_path.replace("/", "\\")
        exact_delete = f'Delete "$INSTDIR\\backend\\{nsis_path}"'
        if hooks.count(exact_delete) != 1:
            raise ValueError(f"installer hook is missing exact bundled tool tombstone: {raw_path}")

    if installer_path is not None:
        if installer_path.stat().st_size != installer["length"]:
            raise ValueError("source installer length does not match the manifest")
        digest = hashlib.sha256(installer_path.read_bytes()).hexdigest()
        if digest != installer["sha256"]:
            raise ValueError("source installer SHA-256 does not match the manifest")

    return {
        "contract": EXPECTED_CONTRACT,
        "sourceRepository": source["repository"],
        "sourceReleaseId": source["releaseId"],
        "sourceTag": source["tag"],
        "sourceCommitSha": source["commitSha"],
        "sourceAssetId": installer["assetId"],
        "sourceInstallerName": installer["name"],
        "sourceInstallerLength": installer["length"],
        "sourceInstallerSha256": installer["sha256"],
        "obsoleteFileCount": len(paths),
        "obsoleteRuntimeFileCount": len(runtime_paths),
        "obsoleteRuntimeDirectoryCount": len(runtime_directories),
        "obsoleteBundledToolFileCount": len(obsolete_tools),
        "installerVerified": installer_path is not None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("packaging/python-314-upgrade-from-v0.5.47.json"),
    )
    parser.add_argument(
        "--installer-hooks",
        type=Path,
        default=Path("Frontend/src-tauri/windows/installer-hooks.nsh"),
    )
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = validate_manifest(
        args.manifest.resolve(),
        args.installer_hooks.resolve(),
        installer_path=args.installer.resolve() if args.installer else None,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
