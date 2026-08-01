#!/usr/bin/env python3
"""Verify the staged/installed local-polishing runtime and no-model contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_CONTRACT = "ScriberLocalPolishingRuntimeManifestV1"
EXPECTED_TAG = "b10158"
EXPECTED_COMMIT = "f87067841bac583bc089a225382248d857791ca8"
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class LocalPolishingRuntimeSmokeError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _resolve_runtime(root: Path) -> Path:
    candidates = (
        root / "tools" / "local-polishing",
        root / "backend" / "tools" / "local-polishing",
        root / "resources" / "backend" / "tools" / "local-polishing",
    )
    matches = [candidate.resolve() for candidate in candidates if candidate.is_dir()]
    if len(matches) != 1:
        raise LocalPolishingRuntimeSmokeError("expected exactly one packaged local-polishing runtime")
    return matches[0]


def _load_manifest(runtime_root: Path) -> dict[str, Any]:
    manifest_path = runtime_root / "runtime-manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalPolishingRuntimeSmokeError("runtime manifest is unreadable") from error
    if not isinstance(value, dict):
        raise LocalPolishingRuntimeSmokeError("runtime manifest must contain an object")
    if value.get("contract") != MANIFEST_CONTRACT or value.get("schemaVersion") != 1:
        raise LocalPolishingRuntimeSmokeError("runtime manifest contract is unsupported")
    if value.get("runtime") != "llama.cpp":
        raise LocalPolishingRuntimeSmokeError("runtime manifest has the wrong engine")
    release = value.get("release")
    if not isinstance(release, dict) or release.get("tag") != EXPECTED_TAG or release.get("commit") != EXPECTED_COMMIT:
        raise LocalPolishingRuntimeSmokeError("runtime release differs from pinned b10158")
    version_markers = release.get("versionOutputContains")
    if (
        not isinstance(version_markers, list)
        or not version_markers
        or not all(isinstance(marker, str) and marker for marker in version_markers)
    ):
        raise LocalPolishingRuntimeSmokeError("runtime version markers are invalid")
    platform = value.get("platform")
    if (
        not isinstance(platform, dict)
        or platform.get("os") != "windows"
        or platform.get("architecture") != "amd64"
        or platform.get("primaryBackend") != "vulkan"
        or platform.get("cpuFallback") is not True
    ):
        raise LocalPolishingRuntimeSmokeError("runtime platform lacks Vulkan with CPU fallback")
    return value


def _verify_files(runtime_root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise LocalPolishingRuntimeSmokeError("runtime manifest contains no files")
    declared: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise LocalPolishingRuntimeSmokeError("runtime file entry is invalid")
        name = entry["path"]
        relative = PurePosixPath(name)
        byte_size = entry.get("bytes")
        digest = entry.get("sha256")
        if relative.is_absolute() or len(relative.parts) != 1 or "\\" in name or ".." in relative.parts:
            raise LocalPolishingRuntimeSmokeError("runtime manifest path is unsafe")
        if name in declared:
            raise LocalPolishingRuntimeSmokeError("runtime manifest path is duplicated")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
            raise LocalPolishingRuntimeSmokeError("runtime file size is invalid")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise LocalPolishingRuntimeSmokeError("runtime file digest is invalid")
        declared[name] = (byte_size, digest)
    observed_files = {
        path.name for path in runtime_root.iterdir() if path.is_file() and path.name != "runtime-manifest.json"
    }
    observed_directories = [path.name for path in runtime_root.iterdir() if path.is_dir()]
    if observed_directories or observed_files != set(declared):
        raise LocalPolishingRuntimeSmokeError("runtime file set differs from its manifest")
    verified: dict[str, str] = {}
    for name, (byte_size, digest) in declared.items():
        path = (runtime_root / name).resolve()
        if runtime_root not in path.parents or path.is_symlink() or not path.is_file():
            raise LocalPolishingRuntimeSmokeError("runtime file escaped its directory")
        if path.stat().st_size != byte_size or _sha256_file(path) != digest:
            raise LocalPolishingRuntimeSmokeError("runtime file differs from its manifest")
        verified[name] = digest
    if "llama-server.exe" not in verified:
        raise LocalPolishingRuntimeSmokeError("runtime is missing llama-server.exe")
    return verified


def _verify_version(runtime_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    executable = runtime_root / "llama-server.exe"
    completed = subprocess.run(
        [str(executable), "--version"],
        cwd=runtime_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = completed.stdout[: 64 * 1024]
    text = output.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise LocalPolishingRuntimeSmokeError("llama-server --version failed")
    if not all(marker in text for marker in manifest["release"]["versionOutputContains"]):
        raise LocalPolishingRuntimeSmokeError("llama-server version output differs from b10158")
    return {
        "exitCode": completed.returncode,
        "outputBytes": len(output),
        "outputSha256": f"sha256:{hashlib.sha256(output).hexdigest()}",
    }


def validate_resource(root: str | Path, *, run_version: bool = True) -> dict[str, Any]:
    install_root = Path(root).resolve()
    if not install_root.is_dir():
        raise LocalPolishingRuntimeSmokeError("resource root does not exist")
    runtime_root = _resolve_runtime(install_root)
    manifest = _load_manifest(runtime_root)
    verified = _verify_files(runtime_root, manifest)
    bundled_models = sorted(
        path.relative_to(install_root).as_posix()
        for path in install_root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".gguf"
    )
    if bundled_models:
        raise LocalPolishingRuntimeSmokeError("base package unexpectedly contains GGUF model weights")
    version = _verify_version(runtime_root, manifest) if run_version else None
    return {
        "ok": True,
        "contract": "ScriberLocalPolishingInstalledRuntimeSmokeV1",
        "releaseTag": manifest["release"]["tag"],
        "releaseCommit": manifest["release"]["commit"],
        "primaryBackend": manifest["platform"]["primaryBackend"],
        "cpuFallback": manifest["platform"]["cpuFallback"],
        "verifiedFileCount": len(verified),
        "llamaServerSha256": verified["llama-server.exe"],
        "version": version,
        "bundledGgufCount": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = validate_resource(args.root)
    except (LocalPolishingRuntimeSmokeError, OSError, subprocess.SubprocessError) as error:
        payload = {
            "ok": False,
            "contract": "ScriberLocalPolishingInstalledRuntimeSmokeV1",
            "error": str(error),
        }
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
