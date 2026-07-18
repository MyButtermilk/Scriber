"""Create the exact, allowlisted payload tree consumed by the NSIS size evaluator."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import tempfile
from pathlib import Path


REQUIRED_FILES = (
    "scriber-desktop.exe",
    "scriber-audio-sidecar.exe",
    "THIRD_PARTY_NOTICES.md",
)
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class StagingError(RuntimeError):
    """Raised when a release tree cannot be staged without ambiguity."""


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def _validate_plain_tree(root: Path, *, label: str) -> None:
    if not root.is_dir() or _is_reparse(root):
        raise StagingError(f"{label} must be a plain directory")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            entry = current_path / name
            if _is_reparse(entry):
                raise StagingError(f"{label} contains a reparse point")


def _resolve_plain(path: Path, *, label: str, directory: bool) -> Path:
    candidate = Path(path.absolute())
    if not candidate.exists() or _is_reparse(candidate):
        raise StagingError(f"{label} must be a plain {'directory' if directory else 'file'}")
    resolved = candidate.resolve(strict=True)
    expected_type = resolved.is_dir() if directory else resolved.is_file()
    if not expected_type or _is_reparse(resolved):
        raise StagingError(f"{label} must be a plain {'directory' if directory else 'file'}")
    return resolved


def _reject_reparse_ancestors(path: Path, *, label: str) -> None:
    parts = path.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        if not current.exists():
            continue
        if _is_reparse(current):
            raise StagingError(f"{label} contains a reparse-point ancestor")


def stage_payload(*, release_root: Path, notices: Path, output: Path) -> None:
    release_root = _resolve_plain(release_root, label="release root", directory=True)
    notices = _resolve_plain(notices, label="THIRD_PARTY_NOTICES.md", directory=False)
    output = Path(output.absolute())
    if output.exists():
        raise StagingError("output already exists; use a fresh research build root")
    if output == release_root or release_root in output.parents:
        raise StagingError("output must stay outside the Cargo release tree")
    _validate_plain_tree(release_root / "backend", label="backend payload")
    _reject_reparse_ancestors(output.parent, label="output parent")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.parent.is_dir() or _is_reparse(output.parent):
        raise StagingError("output parent must be a plain directory")
    for name in REQUIRED_FILES[:2]:
        source = release_root / name
        if not source.is_file() or _is_reparse(source):
            raise StagingError(f"required release payload is missing: {name}")

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
    )
    try:
        shutil.copy2(release_root / "scriber-desktop.exe", temporary)
        shutil.copy2(release_root / "scriber-audio-sidecar.exe", temporary)
        shutil.copy2(notices, temporary / "THIRD_PARTY_NOTICES.md")
        shutil.copytree(release_root / "backend", temporary / "backend")
        _validate_plain_tree(temporary, label="staged installer payload")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--notices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    stage_payload(
        release_root=args.release_root,
        notices=args.notices,
        output=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
