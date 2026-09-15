"""A rejected replay activation must never become an ordinary queued capture."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import web_api
from src.api.live_mic_routes import LiveMicStartCommand
from src.runtime.provider_replay import ProviderReplayConflict, ProviderReplayExecution


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_state", ["native_stop", "scheduled_stop"])
@pytest.mark.parametrize("existing_start", [False, True])
async def test_replay_activation_during_stop_never_enters_ordinary_queue(
    monkeypatch, tmp_path, stop_state, existing_start
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_PREWARM_MODELS_ON_STARTUP", "0")
    monkeypatch.setenv("SCRIBER_PREWARM_STT_ON_STARTUP", "0")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", False)
    controller = web_api.ScriberWebController(asyncio.get_running_loop())
    start_live_mic = AsyncMock()
    monkeypatch.setattr(controller, "start_live_mic", start_live_mic)
    monkeypatch.setattr(controller, "_schedule_state_snapshot_broadcast", MagicMock())
    ordinary = (
        LiveMicStartCommand(post_process=True, tauri_hotkey_marker=None, provider_replay_activation=False)
        if existing_start
        else None
    )
    controller._pending_live_mic_start = ordinary
    controller._pending_hotkey_toggle = existing_start
    stop_gate = asyncio.Event()
    stop_task = asyncio.create_task(stop_gate.wait()) if stop_state == "scheduled_stop" else None
    controller._is_stopping = stop_state == "native_stop"
    controller._background_stop_task = stop_task
    replay = MagicMock(spec=ProviderReplayExecution)

    try:
        with pytest.raises(ProviderReplayConflict, match="stop is pending"):
            await controller.start_listening(
                post_process=False,
                tauri_hotkey_marker={"requestId": "replay-activation"},
                provider_replay_execution=replay,
            )

        assert controller._pending_live_mic_start is ordinary
        assert controller._pending_hotkey_toggle is existing_start
        assert controller._provider_replay_execution is None
        assert replay.mock_calls == []

        # Releasing the earlier stop cannot resurrect the rejected activation.
        controller._is_stopping = False
        stop_gate.set()
        if stop_task is not None:
            await stop_task
        controller._background_stop_task = None
        await controller._start_pending_live_mic()
        if ordinary is None:
            start_live_mic.assert_not_awaited()
        else:
            start_live_mic.assert_awaited_once_with(ordinary)
        assert controller._pending_live_mic_start is None
    finally:
        controller._is_stopping = False
        stop_gate.set()
        if stop_task is not None:
            await stop_task
        controller._background_stop_task = None
        controller._cancel_pending_live_mic_start()
        await controller.drain_background_tasks_for_shutdown(timeout_seconds=1)
        controller.shutdown()
