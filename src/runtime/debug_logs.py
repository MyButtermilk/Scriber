from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.rest_contracts import REST_API_VERSION
from src.runtime.log_clear_state import clear_offset_for_path, load_clear_offsets, record_clear_state
from src.runtime.paths import data_dir, logs_dir, repo_root
from src.runtime.support_bundle import redact_text


_LOG_PATTERNS = ("*.log", "*.jsonl", "*crash*.json", "*crash*.jsonl")
_MAX_BYTES_PER_FILE = 512_000
_MAX_FILES = 24
_MAX_LIMIT = 2_000
_DEFAULT_LIMIT = 500
_LEVEL_RE = re.compile(r"\b(CRITICAL|FATAL|ERROR|ERR|WARNING|WARN|SUCCESS|INFO|DEBUG|TRACE)\b", re.IGNORECASE)
_PRETTY_LOG_RE = re.compile(r"^\.\.\.\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+(?P<level>[A-Z]+)\s+(?P<message>.*)$")
_SHELL_LOG_RE = re.compile(r"^(?P<timestamp_ms>\d{12,})\s+(?P<message>.*)$")


@dataclass(frozen=True)
class DebugLogEntry:
    source: str
    line: int
    level: str
    message: str
    timestamp: str | None = None
    timestamp_ms: int | None = None
    component: str | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "line": self.line,
            "level": self.level,
            "message": self.message,
            "timestamp": self.timestamp,
            "timestampMs": self.timestamp_ms,
            "component": self.component,
        }


def collect_debug_logs(*, limit: int = _DEFAULT_LIMIT) -> dict[str, Any]:
    limit = _clamp_limit(limit)
    ranked_entries: list[tuple[float, int, DebugLogEntry]] = []
    sources: list[str] = []
    truncated = False
    clear_offsets = load_clear_offsets()

    for source_index, path in enumerate(_candidate_log_files()):
        source = path.name
        sources.append(source)
        try:
            text, file_truncated = _read_tail(path, start_offset=clear_offset_for_path(path, clear_offsets))
            fallback_timestamp_ms = path.stat().st_mtime * 1000.0
        except OSError:
            continue
        truncated = truncated or file_truncated
        lines = text.splitlines()
        if len(lines) > limit:
            truncated = True
            start_line = max(1, len(lines) - limit + 1)
            selected = lines[-limit:]
        else:
            start_line = 1
            selected = lines
        for offset, line in enumerate(selected):
            entry = _parse_log_line(line, source=source, line_number=start_line + offset)
            if entry is not None:
                ranked_entries.append(
                    (
                        _entry_sort_value(entry, fallback_timestamp_ms),
                        source_index,
                        entry,
                    )
                )

    if len(ranked_entries) > limit:
        truncated = True
        ranked_entries = sorted(
            ranked_entries,
            key=lambda item: (item[0], item[1], item[2].line),
        )[-limit:]
    else:
        ranked_entries.sort(key=lambda item: (item[0], item[1], item[2].line))
    entries = [entry for _, _, entry in ranked_entries]

    return {
        "apiVersion": REST_API_VERSION,
        "items": [entry.to_public() for entry in entries],
        "sources": sorted(set(sources)),
        "limit": limit,
        "truncated": truncated,
    }


def clear_debug_logs() -> dict[str, Any]:
    cleared, failed = record_clear_state(_candidate_log_files())

    return {
        "apiVersion": REST_API_VERSION,
        "ok": not failed,
        "cleared": len(cleared),
        "failed": len(failed),
        "clearedSources": sorted(cleared),
        "failures": failed,
    }


def _clamp_limit(limit: int) -> int:
    if limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _candidate_log_files() -> list[Path]:
    candidates: list[Path] = []
    for directory in (logs_dir(), data_dir() / "logs", repo_root()):
        if not directory.exists():
            continue
        try:
            directory_root = directory.resolve()
        except OSError:
            continue
        for pattern in _LOG_PATTERNS:
            for candidate in directory.glob(pattern):
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.is_file() and resolved.is_relative_to(directory_root):
                    candidates.append(resolved)

    resolved: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(candidates, key=lambda item: (item.name, str(item.parent))):
        try:
            resolved_path = path.resolve()
        except OSError:
            continue
        if resolved_path in seen or not resolved_path.is_file():
            continue
        seen.add(resolved_path)
        resolved.append(resolved_path)
        if len(resolved) >= _MAX_FILES:
            break
    return resolved


def _read_tail(path: Path, *, start_offset: int = 0) -> tuple[str, bool]:
    size = path.stat().st_size
    start_offset = max(0, min(start_offset, size))
    readable_size = size - start_offset
    truncated = readable_size > _MAX_BYTES_PER_FILE
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - _MAX_BYTES_PER_FILE)
        elif start_offset:
            handle.seek(start_offset)
        raw = handle.read(_MAX_BYTES_PER_FILE if truncated else readable_size)
    return raw.decode("utf-8", errors="replace"), truncated


def _parse_log_line(line: str, *, source: str, line_number: int) -> DebugLogEntry | None:
    message = redact_text(line).strip()
    if not message:
        return None

    if source.endswith(".jsonl") or message.startswith("{"):
        parsed = _parse_json_log_line(message, source=source, line_number=line_number)
        if parsed is not None:
            return parsed

    pretty = _PRETTY_LOG_RE.match(message)
    if pretty:
        level = _normalize_level(pretty.group("level"))
        return DebugLogEntry(
            source=source,
            line=line_number,
            level=level,
            timestamp=pretty.group("time"),
            message=pretty.group("message").strip(),
        )

    shell = _SHELL_LOG_RE.match(message)
    if shell:
        text = shell.group("message").strip()
        return DebugLogEntry(
            source=source,
            line=line_number,
            level=_infer_level(text),
            timestamp_ms=_safe_int(shell.group("timestamp_ms")),
            message=text,
        )

    return DebugLogEntry(
        source=source,
        line=line_number,
        level=_infer_level(message),
        message=message,
    )


def _parse_json_log_line(message: str, *, source: str, line_number: int) -> DebugLogEntry | None:
    try:
        payload = json.loads(message)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    record = payload.get("record")
    if isinstance(record, dict):
        text = str(record.get("message") or "").strip()
        level_data = record.get("level")
        level_name = ""
        if isinstance(level_data, dict):
            level_name = str(level_data.get("name") or "")
        time_data = record.get("time")
        timestamp = None
        if isinstance(time_data, dict):
            timestamp = str(time_data.get("repr") or time_data.get("timestamp") or "") or None
        extra = record.get("extra")
        component = str(extra.get("component") or "") if isinstance(extra, dict) else ""
        return DebugLogEntry(
            source=source,
            line=line_number,
            level=_normalize_level(level_name or _infer_level(text)),
            timestamp=timestamp,
            component=component or None,
            message=text or message,
        )

    text = str(payload.get("message") or payload.get("event") or message).strip()
    return DebugLogEntry(
        source=source,
        line=line_number,
        level=_normalize_level(str(payload.get("level") or _infer_level(text))),
        timestamp=str(payload.get("timestamp") or "") or None,
        timestamp_ms=_safe_int(payload.get("timestampMs")),
        component=str(payload.get("component") or "") or None,
        message=text,
    )


def _infer_level(message: str) -> str:
    match = _LEVEL_RE.search(message)
    if not match:
        lowered = message.lower()
        if "failed" in lowered or "exception" in lowered:
            return "ERROR"
        if "skipped" in lowered or "timeout" in lowered:
            return "WARNING"
        return "INFO"
    return _normalize_level(match.group(1))


def _normalize_level(level: str) -> str:
    normalized = (level or "").strip().upper()
    if normalized == "WARN":
        return "WARNING"
    if normalized == "ERR":
        return "ERROR"
    if normalized == "FATAL":
        return "CRITICAL"
    if normalized in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        return normalized
    return "INFO"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _entry_sort_value(entry: DebugLogEntry, fallback_timestamp_ms: float) -> float:
    """Return a comparable timestamp without trusting source filename order."""
    if entry.timestamp_ms is not None:
        return float(entry.timestamp_ms)

    timestamp = str(entry.timestamp or "").strip()
    if timestamp:
        normalized = timestamp.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).timestamp() * 1000.0
        except ValueError:
            pass

        time_match = re.match(
            r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.(?P<fraction>\d+))?)?",
            timestamp,
        )
        if time_match:
            fallback = datetime.fromtimestamp(fallback_timestamp_ms / 1000.0)
            fraction = (time_match.group("fraction") or "")[:6].ljust(6, "0")
            parsed = fallback.replace(
                hour=int(time_match.group("hour")),
                minute=int(time_match.group("minute")),
                second=int(time_match.group("second") or 0),
                microsecond=int(fraction or 0),
            )
            return parsed.timestamp() * 1000.0

    # Preserve file-local order for unstructured lines while using mtime as the
    # best available cross-file approximation.
    return fallback_timestamp_ms + (entry.line / 1_000_000.0)
