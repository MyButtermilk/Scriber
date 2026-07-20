"""Prepare the locked English/German NLTK Punkt data for a frozen build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from typing import Any, Sequence
import urllib.request
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "packaging" / "nltk-punkt-tab-lock-v1.json"
EXPECTED_CONTRACT = "ScriberNltkPunktTabLockV1"
ARCHIVE_PREFIX = PurePosixPath("punkt_tab")
OUTPUT_PREFIX = Path("tokenizers") / "punkt_tab"


class PunktDataError(RuntimeError):
    """Raised when the locked archive or selected payload is invalid."""


def _load_lock(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PunktDataError("Punkt lock must be a JSON object")
    if value.get("schemaVersion") != 1 or value.get("contract") != EXPECTED_CONTRACT:
        raise PunktDataError("Punkt lock contract is unsupported")
    archive = value.get("archive")
    assets = value.get("assets")
    url = value.get("url")
    if (
        not isinstance(archive, dict)
        or not isinstance(archive.get("length"), int)
        or archive["length"] <= 0
        or not isinstance(archive.get("sha256"), str)
        or len(archive["sha256"]) != 64
        or not isinstance(url, str)
        or not url.startswith("https://")
        or not isinstance(assets, list)
        or not assets
        or any(not isinstance(asset, str) or not asset for asset in assets)
        or len(assets) != len(set(assets))
    ):
        raise PunktDataError("Punkt lock fields are invalid")
    return value


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(part in ("", ".") or ":" in part for part in path.parts)
    ):
        raise PunktDataError(f"Punkt archive contains an unsafe member: {name!r}")
    return path


def _verify_archive(path: Path, lock: dict[str, Any]) -> None:
    expected = lock["archive"]
    actual_length = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if actual_length != expected["length"] or digest.hexdigest() != expected["sha256"]:
        raise PunktDataError("Punkt archive identity does not match the lock")


def prepare(
    lock_path: Path,
    output_root: Path,
    *,
    archive_path: Path | None = None,
) -> dict[str, Any]:
    lock_path = lock_path.resolve(strict=True)
    lock = _load_lock(lock_path)
    output_root = output_root.resolve()
    if output_root.exists():
        if not output_root.is_dir() or output_root.is_symlink() or any(output_root.iterdir()):
            raise PunktDataError("Punkt output must be a new or empty physical directory")
    else:
        output_root.mkdir(parents=True)

    temporary_archive: Path | None = None
    try:
        if archive_path is None:
            handle, raw_path = tempfile.mkstemp(prefix="scriber-punkt-", suffix=".zip")
            os.close(handle)
            temporary_archive = Path(raw_path)
            request = urllib.request.Request(
                lock["url"],
                headers={"User-Agent": "Scriber-release-builder/1"},
            )
            with urllib.request.urlopen(request, timeout=60) as source, temporary_archive.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            archive_path = temporary_archive
        else:
            archive_path = archive_path.resolve(strict=True)
        _verify_archive(archive_path, lock)

        selected_members = {
            (ARCHIVE_PREFIX / PurePosixPath(asset)).as_posix(): asset
            for asset in lock["assets"]
        }
        extracted: list[str] = []
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names: set[str] = set()
            for info in infos:
                member = _safe_member_name(info.filename)
                if info.filename in names:
                    raise PunktDataError("Punkt archive contains a duplicate member")
                names.add(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode):
                    raise PunktDataError("Punkt archive symlinks are forbidden")
                if member.parts and member.parts[0] != ARCHIVE_PREFIX.parts[0]:
                    raise PunktDataError("Punkt archive contains an unexpected root")
            missing = sorted(set(selected_members) - names)
            if missing:
                raise PunktDataError("Punkt archive is missing locked assets")

            for member_name, relative in selected_members.items():
                target = (output_root / OUTPUT_PREFIX / Path(*PurePosixPath(relative).parts)).resolve()
                if os.path.commonpath((str(output_root), str(target))) != str(output_root):
                    raise PunktDataError("Punkt output path escapes its root")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member_name, "r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted.append((OUTPUT_PREFIX / relative).as_posix())

        return {
            "ok": True,
            "contract": lock["contract"],
            "archiveSha256": lock["archive"]["sha256"],
            "fileCount": len(extracted),
            "files": sorted(extracted),
        }
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.lock, args.output, archive_path=args.archive)
    except (OSError, PunktDataError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"NLTK Punkt preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
