"""Materialize the exact llama.cpp runtime used by local transcript polishing."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

LOCK_CONTRACT = "ScriberLocalPolishingLlamaCppRuntimeLockV1"
MANIFEST_CONTRACT = "ScriberLocalPolishingRuntimeManifestV1"
HASH_PREFIX = "sha256:"


class RuntimePreparationError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return HASH_PREFIX + digest.hexdigest()


def _validate_hash(value: object, label: str) -> str:
    text = str(value or "").lower()
    if len(text) != 71 or not text.startswith(HASH_PREFIX):
        raise RuntimePreparationError(f"{label} SHA-256 is invalid")
    try:
        int(text[len(HASH_PREFIX) :], 16)
    except ValueError as error:
        raise RuntimePreparationError(f"{label} SHA-256 is invalid") from error
    return text


def load_lock(path: str | Path) -> dict[str, Any]:
    lock_path = Path(path).resolve()
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimePreparationError("llama.cpp runtime lock is unreadable") from error
    if not isinstance(raw, dict):
        raise RuntimePreparationError("llama.cpp runtime lock must be an object")
    if raw.get("contract") != LOCK_CONTRACT or raw.get("schemaVersion") != 1:
        raise RuntimePreparationError("llama.cpp runtime lock contract is unsupported")
    if raw.get("status") != "locked":
        raise RuntimePreparationError("llama.cpp runtime lock is not materialized")
    platform = raw.get("platform")
    if not isinstance(platform, dict) or platform.get("os") != "windows" or platform.get("architecture") != "amd64":
        raise RuntimePreparationError("llama.cpp runtime lock platform is unsupported")
    for label in ("archive", "license"):
        entry = raw.get(label)
        if not isinstance(entry, dict):
            raise RuntimePreparationError(f"llama.cpp runtime {label} entry is missing")
        filename = entry.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise RuntimePreparationError(f"llama.cpp runtime {label} filename is invalid")
        if not str(entry.get("url") or "").startswith("https://"):
            raise RuntimePreparationError(f"llama.cpp runtime {label} URL is invalid")
        if isinstance(entry.get("bytes"), bool) or not isinstance(entry.get("bytes"), int) or entry["bytes"] < 1:
            raise RuntimePreparationError(f"llama.cpp runtime {label} byte count is invalid")
        _validate_hash(entry.get("sha256"), f"runtime {label}")
    exact = raw.get("requiredExactFiles")
    globs = raw.get("requiredFileGlobs")
    if not isinstance(exact, list) or not exact or not isinstance(globs, list) or not globs:
        raise RuntimePreparationError("llama.cpp runtime required file policy is empty")
    for name in [*exact, *globs]:
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            raise RuntimePreparationError("llama.cpp runtime required filename is invalid")
    if "llama-server.exe" not in exact:
        raise RuntimePreparationError("llama.cpp runtime server executable is not required")
    return raw


def _verify_locked_file(path: Path, entry: dict[str, Any], label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimePreparationError(f"{label} is not a regular file")
    if path.stat().st_size != entry["bytes"]:
        raise RuntimePreparationError(f"{label} byte count differs from lock")
    if _sha256_file(path) != str(entry["sha256"]).lower():
        raise RuntimePreparationError(f"{label} SHA-256 differs from lock")


def _download(entry: dict[str, Any], cache_dir: Path, label: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / entry["filename"]
    if target.exists():
        _verify_locked_file(target, entry, label)
        return target
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    request = urllib.request.Request(str(entry["url"]), headers={"User-Agent": "Scriber-runtime-builder/1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        _verify_locked_file(temporary, entry, label)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _safe_archive_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x1:
        raise RuntimePreparationError("encrypted llama.cpp archive entry is forbidden")
    path = PurePosixPath(info.filename.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimePreparationError("llama.cpp archive contains an unsafe path")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise RuntimePreparationError("llama.cpp archive symlinks are forbidden")
    if len(path.parts) != 1:
        raise RuntimePreparationError("llama.cpp archive must be flat")
    return path.name


def _selected_names(lock: dict[str, Any], archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    maximum_entries = int(lock.get("maximumArchiveEntries", 0))
    maximum_bytes = int(lock.get("maximumExtractedBytes", 0))
    if maximum_entries < 1 or len(infos) > maximum_entries:
        raise RuntimePreparationError("llama.cpp archive entry count exceeds lock")
    if maximum_bytes < 1 or sum(info.file_size for info in infos) > maximum_bytes:
        raise RuntimePreparationError("llama.cpp archive expanded size exceeds lock")
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = _safe_archive_name(info)
        if name.casefold() in {item.casefold() for item in by_name}:
            raise RuntimePreparationError("llama.cpp archive contains duplicate filenames")
        by_name[name] = info
    exact = set(lock["requiredExactFiles"])
    missing = sorted(exact.difference(by_name))
    if missing:
        raise RuntimePreparationError("llama.cpp archive is missing locked runtime files")
    selected = {name: by_name[name] for name in exact}
    for pattern in lock["requiredFileGlobs"]:
        matches = {name: info for name, info in by_name.items() if fnmatch.fnmatchcase(name, pattern)}
        if not matches:
            raise RuntimePreparationError("llama.cpp archive required runtime glob is empty")
        selected.update(matches)
    return dict(sorted(selected.items(), key=lambda item: item[0].casefold()))


def _expected_runtime_names(lock: dict[str, Any], directory: Path) -> set[str]:
    names = set(lock["requiredExactFiles"])
    for pattern in lock["requiredFileGlobs"]:
        matches = {path.name for path in directory.glob(pattern) if path.is_file() and not path.is_symlink()}
        if not matches:
            raise RuntimePreparationError("staged llama.cpp runtime glob is empty")
        names.update(matches)
    names.add(lock["license"]["filename"])
    return names


def write_manifest(
    lock: dict[str, Any],
    output_dir: str | Path,
    *,
    _allow_staging: bool = False,
) -> dict[str, Any]:
    root = Path(output_dir).resolve()
    valid_name = root.name == "local-polishing" or (
        _allow_staging and root.name.startswith(".local-polishing.") and root.name.endswith(".tmp")
    )
    if not valid_name or not root.is_dir():
        raise RuntimePreparationError("runtime output must be an existing local-polishing directory")
    expected = _expected_runtime_names(lock, root)
    if any(path.is_symlink() or not path.is_file() for path in root.iterdir()):
        raise RuntimePreparationError("staged llama.cpp runtime contains an unexpected non-file entry")
    actual = {path.name for path in root.iterdir() if path.is_file() and path.name != "runtime-manifest.json"}
    if actual != expected:
        raise RuntimePreparationError("staged llama.cpp runtime file set differs from lock")
    _verify_locked_file(root / lock["license"]["filename"], lock["license"], "runtime license")
    files = []
    for name in sorted(actual, key=str.casefold):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimePreparationError("staged llama.cpp runtime contains a non-regular file")
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": _sha256_file(path)})
    manifest = {
        "contract": MANIFEST_CONTRACT,
        "schemaVersion": 1,
        "runtime": "llama.cpp",
        "release": lock["release"],
        "platform": lock["platform"],
        "sourceArchive": {
            "filename": lock["archive"]["filename"],
            "bytes": lock["archive"]["bytes"],
            "sha256": lock["archive"]["sha256"],
        },
        "files": files,
    }
    manifest_path = root / "runtime-manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, manifest_path)
    return manifest


def verify_runtime(lock: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    root = Path(output_dir).resolve()
    if root.name != "local-polishing" or not root.is_dir():
        raise RuntimePreparationError("runtime output must be an existing local-polishing directory")
    manifest_path = root / "runtime-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimePreparationError("staged llama.cpp runtime manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise RuntimePreparationError("staged llama.cpp runtime manifest must be an object")
    if manifest.get("contract") != MANIFEST_CONTRACT or manifest.get("schemaVersion") != 1:
        raise RuntimePreparationError("staged llama.cpp runtime manifest contract is unsupported")
    if manifest.get("runtime") != "llama.cpp":
        raise RuntimePreparationError("staged llama.cpp runtime identifier differs from lock")
    if manifest.get("release") != lock["release"] or manifest.get("platform") != lock["platform"]:
        raise RuntimePreparationError("staged llama.cpp runtime identity differs from lock")
    if manifest.get("sourceArchive") != {
        "filename": lock["archive"]["filename"],
        "bytes": lock["archive"]["bytes"],
        "sha256": lock["archive"]["sha256"],
    }:
        raise RuntimePreparationError("staged llama.cpp source archive identity differs from lock")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimePreparationError("staged llama.cpp runtime manifest file list is empty")
    if any(path.is_symlink() or not path.is_file() for path in root.iterdir()):
        raise RuntimePreparationError("staged llama.cpp runtime contains an unexpected non-file entry")
    expected_names = _expected_runtime_names(lock, root)
    actual_names = {path.name for path in root.iterdir() if path.is_file() and path.name != manifest_path.name}
    if actual_names != expected_names:
        raise RuntimePreparationError("staged llama.cpp runtime file set differs from lock")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimePreparationError("staged llama.cpp runtime manifest file entry is invalid")
        name = entry.get("path")
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name or name in seen:
            raise RuntimePreparationError("staged llama.cpp runtime manifest filename is invalid")
        seen.add(name)
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimePreparationError("staged llama.cpp runtime contains a non-regular file")
        if isinstance(entry.get("bytes"), bool) or not isinstance(entry.get("bytes"), int):
            raise RuntimePreparationError("staged llama.cpp runtime manifest byte count is invalid")
        if path.stat().st_size != entry["bytes"]:
            raise RuntimePreparationError("staged llama.cpp runtime file byte count differs from manifest")
        if _sha256_file(path) != _validate_hash(entry.get("sha256"), "runtime file"):
            raise RuntimePreparationError("staged llama.cpp runtime file SHA-256 differs from manifest")
    if seen != expected_names:
        raise RuntimePreparationError("staged llama.cpp runtime manifest file set differs from lock")
    _verify_locked_file(root / lock["license"]["filename"], lock["license"], "runtime license")
    return manifest


def prepare(lock: dict[str, Any], cache_dir: str | Path, output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir).resolve()
    if output.name != "local-polishing" or output.parent == output or output == Path.home().resolve():
        raise RuntimePreparationError("runtime output path is unsafe")
    cache = Path(cache_dir).resolve()
    archive_path = _download(lock["archive"], cache, "runtime archive")
    license_path = _download(lock["license"], cache, "runtime license")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".local-polishing.", suffix=".tmp", dir=output.parent))
    backup: Path | None = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name, info in _selected_names(lock, archive).items():
                target = staging / name
                with archive.open(info) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
        shutil.copyfile(license_path, staging / lock["license"]["filename"])
        write_manifest(lock, staging, _allow_staging=True)
        if output.exists():
            existing_manifest = output / "runtime-manifest.json"
            if not existing_manifest.is_file():
                raise RuntimePreparationError("refusing to replace an unowned runtime directory")
            try:
                existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimePreparationError("existing runtime ownership manifest is invalid") from error
            if existing.get("contract") != MANIFEST_CONTRACT:
                raise RuntimePreparationError("refusing to replace a foreign runtime directory")
            backup = output.with_name(f".local-polishing.{uuid.uuid4().hex}.backup")
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except BaseException:
            if backup is not None and backup.exists() and not output.exists():
                os.replace(backup, output)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return json.loads((output / "runtime-manifest.json").read_text(encoding="utf-8"))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache")
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.refresh_manifest and args.verify:
        parser.error("--refresh-manifest and --verify are mutually exclusive")
    lock = load_lock(args.lock)
    if args.refresh_manifest:
        manifest = write_manifest(lock, args.output)
    elif args.verify:
        manifest = verify_runtime(lock, args.output)
    else:
        if not args.cache:
            parser.error("--cache is required unless --refresh-manifest or --verify is used")
        manifest = prepare(lock, args.cache, args.output)
    print(json.dumps({"ok": True, "output": str(Path(args.output).resolve()), "manifest": manifest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
