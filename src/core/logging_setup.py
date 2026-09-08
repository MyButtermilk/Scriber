from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
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
    append: bool = False,
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
        pretty_log_path,
        level=level_name,
        format=fmt,
        colorize=False,
        enqueue=False,
        encoding="utf-8",
        mode="a" if append else "w",
        backtrace=False,
        diagnose=False,
        filter=lambda _record: _LOGGING_ENABLED,
    )

    logger.add(
        structured_log_path,
        level=level_name,
        serialize=True,
        enqueue=False,
        encoding="utf-8",
        mode="a" if append else "w",
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
