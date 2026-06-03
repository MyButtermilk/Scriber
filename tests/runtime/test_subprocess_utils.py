from __future__ import annotations

from src.runtime import subprocess_utils


class _StartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = None


def test_hidden_subprocess_kwargs_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")

    assert subprocess_utils.hidden_subprocess_kwargs(creationflags=1) == {}


def test_hidden_subprocess_kwargs_hides_windows_console(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.setattr(subprocess_utils.subprocess, "STARTUPINFO", _StartupInfo, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "STARTF_USESHOWWINDOW", 0x1, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = subprocess_utils.hidden_subprocess_kwargs(creationflags=0x2)

    assert kwargs["creationflags"] == 0x08000002
    assert kwargs["startupinfo"].dwFlags & 0x1
    assert kwargs["startupinfo"].wShowWindow == 0
