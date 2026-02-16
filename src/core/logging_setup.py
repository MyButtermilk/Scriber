from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRETTY_LOG_PATH = PROJECT_ROOT / "latest.log"
STRUCTURED_LOG_PATH = PROJECT_ROOT / "latest.structured.jsonl"


_CONFIGURED = False


def _normalize_level(level: str | None) -> str:
    raw = (level or "INFO").strip().upper()
    valid = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    if raw in valid:
        return raw
    return "INFO"


def setup_logging(
    *,
    component: str = "app",
    force: bool = False,
    add_stderr: bool = True,
) -> dict[str, str]:
    global _CONFIGURED

    if _CONFIGURED and not force:
        return {
            "pretty": str(PRETTY_LOG_PATH),
            "structured": str(STRUCTURED_LOG_PATH),
        }

    if force:
        logger.remove()

    PRETTY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    STRUCTURED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    logger.configure(extra={"component": component, "trace": "------", "stage": component})

    fmt = (
        "... {time:HH:mm:ss.SSS} {level:<5} "
        "[{extra[component]:<11}] "
        "[{extra[trace]:<6}] "
        "[{extra[stage]:<15}] "
        "{message}"
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
        )

    logger.add(
        PRETTY_LOG_PATH,
        level=level_name,
        format=fmt,
        colorize=False,
        enqueue=False,
        encoding="utf-8",
        mode="w",
        backtrace=False,
        diagnose=False,
    )

    logger.add(
        STRUCTURED_LOG_PATH,
        level=level_name,
        serialize=True,
        enqueue=False,
        encoding="utf-8",
        mode="w",
        backtrace=False,
        diagnose=False,
    )

    _CONFIGURED = True
    return {
        "pretty": str(PRETTY_LOG_PATH),
        "structured": str(STRUCTURED_LOG_PATH),
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

