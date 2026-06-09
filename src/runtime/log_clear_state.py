from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any

from src.core.rest_contracts import REST_API_VERSION
from src.runtime.paths import logs_dir


CLEAR_STATE_FILENAME = "debug-log-clear-state.json"


def clear_state_path() -> Path:
    return logs_dir() / CLEAR_STATE_FILENAME


def load_clear_offsets() -> dict[str, int]:
    path = clear_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return {}

    offsets: dict[str, int] = {}
    for key, value in files.items():
        if not isinstance(value, dict):
            continue
        size = _safe_non_negative_int(value.get("sizeBytes"))
        if size is not None:
            offsets[str(key)] = size
    return offsets


def clear_offset_for_path(path: Path, offsets: dict[str, int]) -> int:
    offset = offsets.get(_path_key(path), 0)
    if offset <= 0:
        return 0
    try:
        current_size = path.stat().st_size
    except OSError:
        return 0
    if current_size < offset:
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
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
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
        "clearedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "files": files,
    }
    try:
        target = clear_state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f"{target.suffix}.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        failed.append({"source": CLEAR_STATE_FILENAME, "error": f"{type(exc).__name__}: {exc}"})

    return sorted(set(cleared)), failed


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _safe_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
