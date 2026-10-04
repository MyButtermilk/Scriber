from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.runtime.paths import logs_dir

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRETTY_LOG_PATH = PROJECT_ROOT / "latest.log"
STRUCTURED_LOG_PATH = PROJECT_ROOT / "latest.structured.jsonl"


_CONFIGURED = False
_LOGGING_ENABLED = True
_LAST_COMPONENT = "app"
_LAST_STDERR = True
_DIAGNOSTIC_WRITE_LOCK = threading.RLock()
# Each sink keeps its current file plus three uncompressed generations. Their
# .log/.jsonl suffixes remain discoverable by the Debug Console/support bundle.
_LOG_ROTATION_BYTES = 5 * 1024 * 1024
_LOG_RETENTION_FILES = 3


class _BoundedDiagnosticSink:
    """A synchronous Loguru sink whose I/O failures never expose its record.

    Loguru serializes calls to each sink, and remove() drains those calls. Open
    handles only for the append itself so our own writer cannot block Windows
    rotation. A locked reader may delay rotation, but never grow a file beyond
    twice the normal limit. Counters retain no original message or exception.
    """

    def __init__(self, path: Path, *, append: bool, structured: bool) -> None:
        self.path = path
        self.structured = structured
        self.limit = _LOG_ROTATION_BYTES
        self.retention = _LOG_RETENTION_FILES
        self._blocked = False
        self._notice_written = False
        self._dropped_records = 0
        self._dropped_bytes = 0
        self._reason = "io_unavailable"
        self._archive_pattern = re.compile(
            rf"{re.escape(path.stem)}\.(?:[0-9]+|[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}_[0-9_.-]+)"
            rf"{re.escape(path.suffix)}"
        )
        # Fail closed during initial admission, including a partially successful
        # setup. Resume is append-only; explicit fresh-start requests truncate.
        with path.open("ab" if append else "wb"):
            pass

    def _rotate(self) -> None:
        archives = [
            path for path in self.path.parent.iterdir() if self._archive_pattern.fullmatch(path.name) and path.is_file()
        ]
        target: Path | None
        if len(archives) >= self.retention:
            target = min(archives, key=lambda path: (path.stat().st_mtime_ns, path.name))
        else:
            target = next(
                (
                    candidate
                    for index in range(1, self.retention + 1)
                    if not (candidate := self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}")).exists()
                ),
                None,
            )
            if target is None:
                raise OSError("No diagnostic archive slot available")
        # Replace the oldest archive atomically, rather than shifting/deleting
        # archives before learning that the active file is locked. Legacy Loguru
        # timestamp archives participate in the same retention family.
        os.replace(self.path, target)
        # An older installation may have retained more generations. Prune only
        # after the active file has been safely archived, and never add another
        # generation while a locked legacy archive prevents cleanup.
        surplus = len(archives) - self.retention
        if surplus > 0:
            oldest = sorted(
                (path for path in archives if path != target),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
            for path in oldest[:surplus]:
                path.unlink()

    def _append(self, data: bytes, *, limit: int) -> bool:
        with self.path.open("ab") as stream:
            if stream.tell() + len(data) > limit:
                return False
            stream.write(data)
        return True

    def _health_line(self, *, recovered: bool) -> bytes:
        now = datetime.now(UTC)
        event = "diagnostic.rotation.recovered" if recovered else "diagnostic.rotation.blocked"
        message = (
            "Diagnostic file rotation recovered."
            if recovered
            else "Diagnostic file rotation unavailable; bounded append enabled."
        )
        meta = {
            "reason": self._reason,
            "dropped_records": self._dropped_records,
            "dropped_bytes": self._dropped_bytes,
        }
        if self.structured:
            return (
                json.dumps(
                    {
                        "timestamp": now.isoformat(),
                        "level": "WARNING",
                        "component": "logging",
                        "event": event,
                        "message": message,
                        "meta": meta,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        timestamp = now.strftime("%H:%M:%S.%f")[:-3]
        return (
            f"... {timestamp} WARNING [logging    ] [------] [rotation       ] "
            f"{message} event={event} reason={self._reason} "
            f"dropped_records={self._dropped_records} dropped_bytes={self._dropped_bytes}\n"
        ).encode()

    def _block(self, reason: str = "io_unavailable") -> None:
        if not self._blocked:
            self._reason = reason
        self._blocked = True

    def _drop(self, size: int) -> None:
        self._dropped_records += 1
        self._dropped_bytes += size

    def _blocked_notice(self, *, reserve: int = 0) -> None:
        if not self._notice_written:
            self._notice_written = self._append(self._health_line(recovered=False), limit=2 * self.limit - reserve)

    def write(self, message: str) -> None:
        data = message.encode("utf-8", errors="replace")
        if len(data) > self.limit:
            self._block("record_too_large")
            self._drop(len(data))
            # The record is already counted; keep the notice pending.
            with suppress(OSError):
                self._blocked_notice()
            return
        try:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            rotation_failed = False
            recovered = False
            if size and (size + len(data) > self.limit or self._blocked):
                try:
                    self._rotate()
                except OSError:
                    self._block()
                    rotation_failed = True
            if self._blocked:
                if rotation_failed:
                    self._blocked_notice(reserve=len(data))
                elif self._append(self._health_line(recovered=True), limit=2 * self.limit):
                    recovered = True
                    self._blocked = False
                    self._notice_written = False
                    self._dropped_records = 0
                    self._dropped_bytes = 0
            if not self._append(data, limit=2 * self.limit if self._blocked or recovered else self.limit):
                self._block("capacity")
                self._drop(len(data))
        except OSError:
            # Do not let Loguru's default handler print "Record was:" and all
            # extras to stderr. No recursive logger calls from this sink.
            self._block()
            self._drop(len(data))


def diagnostic_logging_enabled() -> bool:
    return _LOGGING_ENABLED


def set_diagnostic_logging_enabled(enabled: bool) -> None:
    """Stop message construction/sinks immediately; resume without truncating logs."""
    global _LOGGING_ENABLED, _CONFIGURED
    with _DIAGNOSTIC_WRITE_LOCK:
        if not enabled:
            _LOGGING_ENABLED = False
            logger.disable("")
            logger.disable(None)
            logging.disable(logging.CRITICAL)
            # Removing synchronous sinks waits for a writer that already passed
            # its filter. The returned preference is therefore a write barrier,
            # including concurrent metrics workers guarded below.
            logger.remove()
            _CONFIGURED = False
            return
        if not _CONFIGURED:
            setup_logging(component=_LAST_COMPONENT, force=True, add_stderr=_LAST_STDERR, append=True, enabled=True)
        else:
            _LOGGING_ENABLED = True
            logger.enable("")
            logger.enable(None)
            logging.disable(logging.NOTSET)


def run_diagnostic_write(write: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Admit optional persisted diagnostics behind the same off barrier."""
    with _DIAGNOSTIC_WRITE_LOCK:
        if not _LOGGING_ENABLED:
            return None
        return write(*args, **kwargs)


def _normalize_level(level: str | None) -> str:
    raw = (level or "INFO").strip().upper()
    valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    if raw in valid:
        return raw
    return "INFO"


def _log_paths() -> tuple[Path, Path]:
    base_dir = logs_dir()
    return base_dir / "latest.log", base_dir / "latest.structured.jsonl"


def setup_logging(
    *,
    component: str = "app",
    force: bool = False,
    add_stderr: bool = True,
    append: bool = True,
    enabled: bool | None = None,
) -> dict[str, str]:
    with _DIAGNOSTIC_WRITE_LOCK:
        try:
            return _setup_logging(
                component=component, force=force, add_stderr=add_stderr, append=append, enabled=enabled
            )
        except Exception:
            # A file may become unwritable while logging is off. Do not leave a
            # successful stderr/pretty sink, or report enabled, when another
            # sink failed to open. The preference owner can now roll back safely.
            set_diagnostic_logging_enabled(False)
            raise


def _setup_logging(
    *,
    component: str,
    force: bool,
    add_stderr: bool,
    append: bool,
    enabled: bool | None,
) -> dict[str, str]:
    global _CONFIGURED, _LOGGING_ENABLED, _LAST_COMPONENT, _LAST_STDERR
    _LAST_COMPONENT, _LAST_STDERR = component, add_stderr
    pretty_log_path, structured_log_path = _log_paths()

    effective_enabled = (
        enabled
        if enabled is not None
        else os.getenv("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
    )
    if not effective_enabled:
        if force:
            logger.remove()
            _CONFIGURED = False
        set_diagnostic_logging_enabled(False)
        return {"pretty": str(pretty_log_path), "structured": str(structured_log_path)}
    if _CONFIGURED and not force:
        _LOGGING_ENABLED = True
        logger.enable("")
        logger.enable(None)
        logging.disable(logging.NOTSET)
        return {
            "pretty": str(pretty_log_path),
            "structured": str(structured_log_path),
        }

    # Keep admission closed until every sink is ready, including during a
    # resume that fails after opening only one of the files.
    _LOGGING_ENABLED = False
    logger.disable("")
    logger.disable(None)
    logging.disable(logging.CRITICAL)
    if force:
        logger.remove()
    _CONFIGURED = False

    pretty_log_path.parent.mkdir(parents=True, exist_ok=True)
    structured_log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.configure(extra={"component": component, "trace": "------", "stage": component})

    fmt = (
        "... {time:HH:mm:ss.SSS} {level:<5} [{extra[component]:<11}] [{extra[trace]:<6}] [{extra[stage]:<15}] {message}"
    )

    level_name = _normalize_level(os.getenv("SCRIBER_LOG_LEVEL", "DEBUG"))

    if add_stderr:
        logger.add(
            sys.stderr,
            level=level_name,
            format=fmt,
            colorize=False,
            enqueue=False,
            backtrace=False,
            diagnose=False,
            filter=lambda _record: _LOGGING_ENABLED,
        )

    logger.add(
        _BoundedDiagnosticSink(pretty_log_path, append=append, structured=False),
        level=level_name,
        format=fmt,
        colorize=False,
        enqueue=False,
        backtrace=False,
        diagnose=False,
        filter=lambda _record: _LOGGING_ENABLED,
    )

    logger.add(
        _BoundedDiagnosticSink(structured_log_path, append=append, structured=True),
        level=level_name,
        serialize=True,
        enqueue=False,
        backtrace=False,
        diagnose=False,
        filter=lambda _record: _LOGGING_ENABLED,
    )

    _CONFIGURED = True
    _LOGGING_ENABLED = True
    logger.enable("")
    logger.enable(None)
    logging.disable(logging.NOTSET)
    return {
        "pretty": str(pretty_log_path),
        "structured": str(structured_log_path),
    }


def emit_event(
    bound_logger: Any,
    message: str,
    *,
    level: str = "INFO",
    event: str | None = None,
    workflow: str | None = None,
    stage: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
    transcript_id: str | None = None,
    job_id: str | None = None,
    provider: str | None = None,
    duration_ms: int | float | None = None,
    outcome: str | None = None,
    milestone: bool | None = None,
    error_category: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    if not _LOGGING_ENABLED:
        return
    extras: dict[str, Any] = {}
    if event is not None:
        extras["event"] = event
    if workflow is not None:
        extras["workflow"] = workflow
    if stage is not None:
        extras["stage"] = stage
    if trace_id is not None:
        extras["trace_id"] = trace_id
        extras["trace"] = trace_id[-6:] if len(trace_id) >= 6 else trace_id
    if session_id is not None:
        extras["session_id"] = session_id
    if transcript_id is not None:
        extras["transcript_id"] = transcript_id
    if job_id is not None:
        extras["job_id"] = job_id
    if provider is not None:
        extras["provider"] = provider
    if duration_ms is not None:
        extras["duration_ms"] = duration_ms
    if outcome is not None:
        extras["outcome"] = outcome
    if milestone is not None:
        extras["milestone"] = milestone
    if error_category is not None:
        extras["error_category"] = error_category
    if meta is not None:
        extras["meta"] = meta

    logger_obj = bound_logger.bind(**extras) if extras else bound_logger
    logger_obj.log(_normalize_level(level), message)
