"""Bounded, content-free exception locations for recording diagnostics.

Never format the exception or inspect frame locals/source lines: provider errors
and their traceback frames can retain credentials, audio paths and transcripts.
Only validated code identities from the recording stack cross this boundary.
"""

from __future__ import annotations

import re
from collections import deque
from types import TracebackType
from typing import Any

_MAX_STACK_FRAMES = 8
_MAX_TRACEBACK_WALK = 256
_MAX_CAUSES = 3
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,95}\Z")
_MODULE = re.compile(r"(?:src|pipecat|aiohttp|asyncio|websockets)(?:\.[A-Za-z_][A-Za-z_0-9]*)+\Z")


def _exception_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _IDENTIFIER.fullmatch(name) else "UnknownError"


def _code_stack(traceback: TracebackType | None) -> list[str]:
    frames: deque[str] = deque(maxlen=_MAX_STACK_FRAMES)
    remaining = _MAX_TRACEBACK_WALK
    while traceback is not None and remaining:
        remaining -= 1
        frame = traceback.tb_frame
        code = frame.f_code
        module = frame.f_globals.get("__name__")
        function = code.co_name
        if (
            isinstance(module, str)
            and len(module) <= 160
            and _MODULE.fullmatch(module)
            and (_IDENTIFIER.fullmatch(function) or function in {"<lambda>", "<module>"})
        ):
            # The globals alone are not evidence of a code location. Require a
            # matching module suffix while discarding the installation prefix.
            filename = "/" + code.co_filename.replace("\\", "/").casefold()
            module_path = module.replace(".", "/").casefold()
            if filename.endswith((f"/{module_path}.py", f"/{module_path}/__init__.py")):
                frames.append(f"{module}:{function}:{max(0, traceback.tb_lineno)}")
        traceback = traceback.tb_next
    return list(frames)


def _exception_entry(exc: BaseException) -> dict[str, Any]:
    details: dict[str, Any] = {"error_type": _exception_type(exc)}
    stack = _code_stack(exc.__traceback__)
    if stack:
        details["error_stack"] = stack
    return details


def exception_diagnostics(error: BaseException | str) -> dict[str, Any]:
    """Project an exception and up to three causes without exception content."""
    if not isinstance(error, BaseException):
        return {"error_type": "ProviderErrorFrame"}
    details = _exception_entry(error)
    seen = {id(error)}
    causes: list[dict[str, Any]] = []
    current = error
    for _ in range(_MAX_CAUSES):
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        causes.append(_exception_entry(cause))
        current = cause
    if causes:
        details["error_causes"] = causes
    return details
