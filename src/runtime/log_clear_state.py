from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.core.rest_contracts import REST_API_VERSION
from src.runtime.paths import logs_dir

CLEAR_STATE_FILENAME = "debug-log-clear-state.json"
_MAX_CLEAR_STATE_BYTES = 1024 * 1024
_CLEAR_FINGERPRINT_BYTES = 256
_ROTATION_SUFFIX_RE = re.compile(r"\.(?:[0-9]+|[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9_.-]+)$")


def clear_state_path() -> Path:
    return logs_dir() / CLEAR_STATE_FILENAME


def load_clear_offsets() -> dict[str, dict[str, Any]]:
    path = clear_state_path()
    try:
        if path.stat().st_size > _MAX_CLEAR_STATE_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return {}

    offsets: dict[str, dict[str, Any]] = {}
    for key, value in files.items():
        if not isinstance(value, dict):
            continue
        size = _safe_non_negative_int(value.get("sizeBytes"))
        if size is not None:
            offsets[str(key)] = {
                "sizeBytes": size,
                "tailSha256": str(value.get("tailSha256") or ""),
            }
    return offsets


def clear_offset_for_path(path: Path, offsets: dict[str, Any]) -> int:
    path_key = _path_key(path)
    offset = _matching_clear_offset(path, offsets.get(path_key, 0))
    if offset:
        return offset
    # Native numbered slots shift, and Loguru renames current files with a
    # timestamp. A clear boundary follows the bytes only within the same log
    # family and only when its persisted fingerprint still matches.
    family = _rotation_family(Path(path_key))
    if family == Path(path_key):
        # A newly opened current file must not inherit a boundary from an older
        # generation merely because repeated diagnostic bytes happen to match.
        return 0
    for recorded_path, entry in offsets.items():
        if recorded_path == path_key or not isinstance(entry, dict) or not entry.get("tailSha256"):
            continue
        if _rotation_family(Path(recorded_path)) == family:
            offset = max(offset, _matching_clear_offset(path, entry))
    return offset


def _rotation_family(path: Path) -> Path:
    return path.with_name(_ROTATION_SUFFIX_RE.sub("", path.stem) + path.suffix)


def _matching_clear_offset(path: Path, entry: Any) -> int:
    if isinstance(entry, dict):
        offset = _safe_non_negative_int(entry.get("sizeBytes")) or 0
        expected_fingerprint = str(entry.get("tailSha256") or "")
    else:
        # Backward-compatible support for in-memory callers using the original
        # path-to-integer mapping.
        offset = _safe_non_negative_int(entry) or 0
        expected_fingerprint = ""
    if offset <= 0:
        return 0
    try:
        current_size = path.stat().st_size
    except OSError:
        return 0
    if current_size < offset:
        return 0
    if expected_fingerprint:
        try:
            current_fingerprint = _tail_fingerprint(path, end_offset=offset)
        except OSError:
            return 0
        if not current_fingerprint or current_fingerprint != expected_fingerprint:
            return 0
    return offset


def record_clear_state(paths: Iterable[Path]) -> tuple[list[str], list[dict[str, str]]]:
    cleared: list[str] = []
    failed: list[dict[str, str]] = []
    files: dict[str, dict[str, Any]] = {}

    for path in paths:
        source = path.name
        try:
            stat = path.stat()
            if not path.is_file():
                continue
            files[_path_key(path)] = {
                "source": source,
                "sizeBytes": stat.st_size,
                "tailSha256": _tail_fingerprint(path, end_offset=stat.st_size),
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        except OSError as exc:
            failed.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})
        else:
            cleared.append(source)

    state = {
        "apiVersion": REST_API_VERSION,
        "clearedAt": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "files": files,
    }
    target = clear_state_path()
    tmp = target.with_suffix(f"{target.suffix}.{uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        failed.append({"source": CLEAR_STATE_FILENAME, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        tmp.unlink(missing_ok=True)

    return sorted(set(cleared)), failed


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _safe_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed if parsed >= 0 else None


def _tail_fingerprint(path: Path, *, end_offset: int) -> str:
    end = max(0, int(end_offset))
    start = max(0, end - _CLEAR_FINGERPRINT_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        payload = handle.read(end - start)
    return hashlib.sha256(payload).hexdigest()
