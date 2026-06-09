"""Shared subprocess helpers for repository scripts."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def process_creation_flags() -> int:
    """Creation flags that keep child console windows hidden on Windows."""
    if sys.platform.startswith("win"):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def terminate_process(proc: subprocess.Popen[Any] | None, *, timeout_sec: float = 2.0) -> None:
    """Terminate a child process gracefully, escalating to kill after timeout_sec."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_sec)
