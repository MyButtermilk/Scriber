import argparse
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import smoke_real_file_upload_browser as smoke


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("startup timeout"), asyncio.CancelledError()])
async def test_browser_startup_retries_only_setup_and_cleans_failed_process(monkeypatch, tmp_path, failure):
    primary = tmp_path / "chrome.exe"
    fallback = tmp_path / "edge.exe"
    primary.touch()
    fallback.touch()
    monkeypatch.setattr(smoke, "resolve_browser_path", lambda _: str(primary))
    monkeypatch.setattr(smoke, "browser_candidates", lambda: [str(primary), str(fallback)])
    started = []
    stopped = []

    def start(executable, port, profile, **kwargs):
        process = SimpleNamespace(poll=lambda: None)
        started.append((executable, profile, process))
        return process

    monkeypatch.setattr(smoke, "start_browser", start)
    monkeypatch.setattr(smoke, "terminate_process_tree", stopped.append)
    connected = object()
    connect = AsyncMock(side_effect=[failure, connected])
    monkeypatch.setattr(smoke, "connect_to_browser", connect)
    args = argparse.Namespace(browser="", headed=False, startup_timeout_sec=45.0)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await smoke._start_test_browser(args, tmp_path)
        assert len(started) == 1
    else:
        process, cdp = await smoke._start_test_browser(args, tmp_path)
        assert cdp is connected
        assert process is started[1][2]
        assert started[1][0] == str(fallback)
        assert started[0][1] != started[1][1]
        assert connect.await_count == 2
        assert connect.call_args.kwargs == {"timeout_sec": 45.0}
    assert stopped == [started[0][2]]
