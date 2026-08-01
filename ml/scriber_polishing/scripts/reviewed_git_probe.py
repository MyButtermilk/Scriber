"""Minimal, read-only Git identity probe for the verified HF v19 packet.

This intentionally implements only the three commands used by the llama.cpp
lock materializer.  It never consults a global Git configuration and it proves
the complete non-.git payload against the packet-bound source-tree hash before
reporting a clean status.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import sys
from pathlib import Path


class ProbeError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    records: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/") or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ProbeError(f"source contains a non-regular entry: {relative}")
        records.append((relative, path.stat().st_size, _sha256_file(path)))
    if not records:
        raise ProbeError("source payload is empty")
    digest = hashlib.sha256()
    for relative, size, content_hash in records:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _git_dir(root: Path) -> Path:
    candidate = root / ".git"
    if candidate.is_symlink() or not candidate.is_dir():
        raise ProbeError("checkout has no regular .git directory")
    return candidate


def _head_commit(git_dir: Path) -> str:
    head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    if head.startswith("ref: "):
        reference = head.removeprefix("ref: ")
        if reference.startswith("/") or ".." in Path(reference).parts:
            raise ProbeError("HEAD reference is unsafe")
        loose = git_dir / reference
        if loose.is_file() and not loose.is_symlink():
            head = loose.read_text(encoding="ascii").strip()
        else:
            packed = git_dir / "packed-refs"
            if packed.is_symlink() or not packed.is_file():
                raise ProbeError("HEAD reference is unresolved")
            matches = [
                line.split(" ", 1)[0]
                for line in packed.read_text(encoding="ascii").splitlines()
                if line and not line.startswith(("#", "^")) and line.endswith(" " + reference)
            ]
            if len(matches) != 1:
                raise ProbeError("HEAD reference is ambiguous or unresolved")
            head = matches[0]
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise ProbeError("HEAD is not a lowercase SHA-1 commit")
    return head


def _origin(git_dir: Path) -> str:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read(git_dir / "config", encoding="utf-8")
    section = 'remote "origin"'
    if not parser.has_option(section, "url"):
        raise ProbeError("origin URL is missing")
    return parser.get(section, "url").strip()


def _normalized_args(argv: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "-c":
            if index + 1 >= len(argv):
                raise ProbeError("incomplete -c option")
            index += 2
            continue
        result.append(argv[index])
        index += 1
    return result


def main() -> int:
    root = Path.cwd().resolve()
    git_dir = _git_dir(root)
    arguments = _normalized_args(sys.argv[1:])
    if arguments == ["rev-parse", "--verify", "HEAD"]:
        print(_head_commit(git_dir))
        return 0
    if arguments == ["remote", "get-url", "origin"]:
        print(_origin(git_dir))
        return 0
    if arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
        expected = os.environ.get("SCRIBER_EXPECTED_LLAMA_SOURCE_TREE_SHA256", "")
        actual = _source_tree_sha256(root)
        if not expected or actual != expected:
            raise ProbeError(
                f"source payload differs from packet binding: expected={expected!r}, actual={actual!r}"
            )
        return 0
    raise ProbeError(f"unsupported reviewed Git probe: {arguments!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, configparser.Error, ProbeError) as error:
        print(f"reviewed_git_probe: {error}", file=sys.stderr)
        raise SystemExit(1) from error
