"""Passively transfer release-producer dependencies to the default-branch cache.

Only cache misses are exported, after the ready application artifact is uploaded.
The detached maintenance workflow validates source identity and recomputed keys,
then saves the restored paths with actions/cache. No payload file is executed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST = "build-dependencies-manifest.json"
ROOTS = {
    "rust": (
        ".cargo/registry/index",
        ".cargo/registry/cache",
        ".cargo/git/db",
        "Frontend/src-tauri/target/release/.fingerprint",
        "Frontend/src-tauri/target/release/build",
        "Frontend/src-tauri/target/release/deps",
        "Frontend/src-tauri/target/release/incremental",
    ),
    "frontend": ("Frontend/node_modules",),
}
KEY_PATTERNS = {
    "rust": re.compile(r"scriber-rust-dependencies-v1-Windows-[0-9a-f]{64}"),
    "frontend": re.compile(r"scriber-frontend-node-modules-Windows-node-[0-9a-f]{64}-[0-9a-f]{64}"),
}
APP_EXCLUSIONS = {
    ".fingerprint": ("scriber-desktop-*",),
    "build": ("scriber-desktop-*",),
    "deps": ("scriber_desktop*", "libscriber_desktop*", "scriber_audio_sidecar*"),
    "incremental": ("scriber_desktop*", "scriber_audio_sidecar*"),
}
MAX_FILES = 250_000
MAX_BYTES = 16 * 1024**3


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _regular_path(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Dependency snapshots cannot contain links or reparse points")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("Expected a dependency directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected a regular dependency file")


def _directory_chain(root: Path, path: Path) -> None:
    while path != root:
        if path.exists() or path.is_symlink():
            _regular_path(path, directory=True)
        path = path.parent


def _safe_member(name: str, kind: str) -> bool:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or any(part in {".", ".."} for part in path.parts)
        or any(character in name for character in "\\:\x00")
        or any(part.endswith((".", " ")) for part in path.parts)
        or any(re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts)
    ):
        return False
    if not any(name.startswith(root + "/") for root in ROOTS[kind]):
        return False
    if kind == "rust":
        prefix = "Frontend/src-tauri/target/release/"
        if name.startswith(prefix):
            parts = name[len(prefix) :].split("/")
            if len(parts) >= 2 and any(
                fnmatch.fnmatchcase(parts[1].casefold(), item) for item in APP_EXCLUSIONS.get(parts[0], ())
            ):
                return False
    return True


def _identity(repository: str, run_id: int, attempt: int, sha: str, ref: str) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or run_id < 1
        or attempt < 1
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
        or not re.fullmatch(r"refs/(?:tags/v[0-9]+\.[0-9]+\.[0-9]+|heads/main)", ref)
    ):
        raise ValueError("Invalid dependency snapshot source identity")
    return {
        "repository": repository,
        "sourceRunId": run_id,
        "sourceRunAttempt": attempt,
        "sourceSha": sha,
        "sourceRef": ref,
    }


def _validate_keys(keys: dict[str, str]) -> None:
    if not keys or not set(keys) <= ROOTS.keys():
        raise ValueError("No supported dependency component was selected")
    for kind, key in keys.items():
        if not KEY_PATTERNS[kind].fullmatch(key):
            raise ValueError("Invalid dependency cache key")


def _export_component(repo_root: Path, output: Path, kind: str, key: str) -> dict[str, Any]:
    started = time.monotonic()
    name = f"{kind}-dependencies.tar.zst"
    archive_path = output / name
    count = 0
    total = 0
    roots = []
    with tarfile.open(archive_path, "w:zst", level=1, dereference=True) as archive:
        for root in ROOTS[kind]:
            source = repo_root / root
            if not source.exists():
                continue
            _directory_chain(repo_root, source)
            roots.append(root)
            for directory, directories, files in os.walk(source, followlinks=False):
                current = Path(directory)
                for child in directories[:]:
                    path = current / child
                    _regular_path(path, directory=True)
                    # Prune app-specific trees without copying or deleting the
                    # compiler output used to produce the shipping binary.
                    probe = path.relative_to(repo_root).as_posix() + "/probe"
                    if not _safe_member(probe, kind):
                        directories.remove(child)
                for child in sorted(files):
                    path = current / child
                    relative = path.relative_to(repo_root).as_posix()
                    if not _safe_member(relative, kind):
                        continue
                    _regular_path(path)
                    size = path.stat().st_size
                    count += 1
                    total += size
                    if count > MAX_FILES or total > MAX_BYTES:
                        raise ValueError("Dependency snapshot exceeds its bounded inventory")
                    archive.add(path, arcname=relative, recursive=False)
    if count == 0 or (kind == "rust" and not any(root.endswith("/deps") for root in roots)):
        raise ValueError("Required compiled dependencies are missing")
    if kind == "frontend" and not (repo_root / "Frontend/node_modules/.package-lock.json").is_file():
        raise ValueError("Frontend dependencies are missing npm install metadata")
    return {
        "key": key,
        "archive": name,
        "sha256": _sha256(archive_path),
        "length": archive_path.stat().st_size,
        "fileCount": count,
        "unpackedBytes": total,
        "roots": roots,
        "exportSeconds": round(time.monotonic() - started, 3),
    }


def export_snapshot(*, repo_root: Path, output: Path, identity: dict[str, Any], keys: dict[str, str]) -> dict[str, Any]:
    _validate_keys(keys)
    repo_root = repo_root.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {kind: executor.submit(_export_component, repo_root, output, kind, key) for kind, key in keys.items()}
        components = {kind: future.result() for kind, future in futures.items()}
    manifest = {"apiVersion": 1, **identity, "components": components}
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _read_manifest(snapshot: Path, identity: dict[str, Any], expected_keys: dict[str, str]) -> dict[str, Any]:
    _validate_keys(expected_keys)
    _regular_path(snapshot, directory=True)
    manifest_path = snapshot / MANIFEST
    _regular_path(manifest_path)
    if manifest_path.stat().st_size > 64 * 1024:
        raise ValueError("Dependency manifest is too large")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != 1:
        raise ValueError("Invalid dependency snapshot manifest")
    for name, value in identity.items():
        actual = manifest.get(name)
        # A failed-job rerun can retain a successful producer artifact. Its
        # immutable source and keys must match; no cross-run reuse is permitted.
        if name == "sourceRunAttempt":
            if type(actual) is not int or not 1 <= actual <= value:
                raise ValueError("Dependency snapshot attempt does not match")
        elif actual != value:
            raise ValueError("Dependency snapshot source identity does not match")
    components = manifest.get("components")
    if not isinstance(components, dict) or not components or not set(components) <= expected_keys.keys():
        raise ValueError("Invalid dependency snapshot components")
    expected_files = {MANIFEST}
    for kind, entry in components.items():
        if (
            not isinstance(entry, dict)
            or entry.get("key") != expected_keys[kind]
            or entry.get("archive") != f"{kind}-dependencies.tar.zst"
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or type(entry.get("length")) is not int
            or not 0 < entry["length"] <= MAX_BYTES
            or type(entry.get("fileCount")) is not int
            or not 0 < entry["fileCount"] <= MAX_FILES
            or type(entry.get("unpackedBytes")) is not int
            or not 0 <= entry["unpackedBytes"] <= MAX_BYTES
            or not isinstance(entry.get("roots"), list)
            or not entry["roots"]
            or not all(isinstance(root, str) and root in ROOTS[kind] for root in entry["roots"])
            or len(entry["roots"]) != len(set(entry["roots"]))
            or (kind == "rust" and "Frontend/src-tauri/target/release/deps" not in entry["roots"])
        ):
            raise ValueError("Dependency snapshot cache identity is invalid")
        expected_files.add(entry["archive"])
        archive = snapshot / entry["archive"]
        _regular_path(archive)
        if archive.stat().st_size != entry["length"] or _sha256(archive) != entry["sha256"]:
            raise ValueError("Dependency snapshot archive checksum does not match")
    if {child.name for child in snapshot.iterdir()} != expected_files:
        raise ValueError("Dependency snapshot contains unexpected files")
    return manifest


def _extract_component(snapshot: Path, staging: Path, kind: str, entry: dict[str, Any]) -> None:
    seen = set()
    total = 0
    with tarfile.open(snapshot / entry["archive"], "r|zst") as archive:
        for member in archive:
            folded = member.name.casefold()
            if (
                not member.isfile()
                or member.issparse()
                or not _safe_member(member.name, kind)
                or not any(member.name.startswith(root + "/") for root in entry["roots"])
                or folded in seen
                or member.size < 0
            ):
                raise ValueError("Unsafe dependency archive member")
            seen.add(folded)
            total += member.size
            if len(seen) > entry["fileCount"] or total > entry["unpackedBytes"]:
                raise ValueError("Dependency archive exceeds its declared inventory")
            # Fresh staging, a strict path allowlist, no links/special files,
            # and Python's data filter keep downloaded content passive.
            archive.extract(member, staging, filter="data")
    if len(seen) != entry["fileCount"] or total != entry["unpackedBytes"]:
        raise ValueError("Dependency archive inventory does not match")
    if kind == "frontend" and not (staging / "Frontend/node_modules/.package-lock.json").is_file():
        raise ValueError("Frontend archive is missing npm install metadata")


def import_snapshot(
    *, repo_root: Path, snapshot: Path, identity: dict[str, Any], expected_keys: dict[str, str]
) -> dict[str, Any]:
    repo_root = repo_root.resolve(strict=True)
    manifest = _read_manifest(snapshot, identity, expected_keys)
    # Never overwrite existing dependency state or follow a workspace junction.
    for entry in manifest["components"].values():
        for root in entry["roots"]:
            destination = repo_root / root
            if destination.exists() or destination.is_symlink():
                raise ValueError("Dependency destination already exists")
            _directory_chain(repo_root, destination.parent)
    with tempfile.TemporaryDirectory(prefix="scriber-dependency-import-", dir=repo_root) as temporary:
        staging = Path(temporary)
        for kind, entry in manifest["components"].items():
            _extract_component(snapshot, staging, kind, entry)
        # Commit only after every archive validates; a corrupt second component
        # must never leave a publishable first component in the workspace.
        for entry in manifest["components"].values():
            for root in entry["roots"]:
                source = staging / root
                if source.exists():
                    destination = repo_root / root
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(source, destination)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "import"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-run-id", type=int, required=True)
    parser.add_argument("--source-run-attempt", type=int, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--rust-key")
    parser.add_argument("--frontend-key")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    identity = _identity(args.repository, args.source_run_id, args.source_run_attempt, args.source_sha, args.source_ref)
    keys = {kind: value for kind, value in (("rust", args.rust_key), ("frontend", args.frontend_key)) if value}
    if args.mode == "export":
        result = export_snapshot(repo_root=args.repo_root, output=args.snapshot, identity=identity, keys=keys)
    else:
        result = import_snapshot(
            repo_root=args.repo_root, snapshot=args.snapshot, identity=identity, expected_keys=keys
        )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for kind in ROOTS:
                output.write(f"{kind}-ready={str(kind in result['components']).lower()}\n")
    for kind, entry in result["components"].items():
        print(f"{args.mode}: {kind}, {entry['fileCount']} files, {entry['length']} compressed bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
