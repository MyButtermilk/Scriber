from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs(*, creationflags: int = 0) -> dict[str, Any]:
    """Return subprocess kwargs that keep child console windows hidden on Windows."""
    if sys.platform != "win32":
        return {}

    kwargs: dict[str, Any] = {}
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    flags = creationflags | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags

    return kwargs
