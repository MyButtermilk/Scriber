import asyncio
from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src import web_api
from src.config import Config
from src.injector import TextInjector
from src.web_api import ScriberWebController


@dataclass
class _PipelineStats:
    start_calls: int = 0
    stop_calls: int = 0
    injected: bool = False
    final_text: str = ""


class _StressFakePipeline:
    """Minimal pipeline stub for lifecycle stress tests."""

    instances: ClassVar[list["_StressFakePipeline"]] = []
    created: ClassVar[int] = 0

    def __init__(
        self,
        service_name: str = "openai",
        on_status_change=None,
        on_audio_level=None,
        on_transcription=None,
        on_progress=None,
        on_text_injected=None,
        on_mic_ready=None,
        on_error=None,
        **_kwargs,
    ):
        type(self).created += 1
        self.instance_id = type(self).created
        self.stats = _PipelineStats(final_text=f"stress transcript {self.instance_id}")
        self.service_name = service_name
        self.on_status_change = on_status_change
        self.on_audio_level = on_audio_level
        self.on_transcription = on_transcription
        self.on_progress = on_progress
        self.on_text_injected = on_text_injected
        self.on_mic_ready = on_mic_ready
        self.on_error = on_error
        self._stop_event = asyncio.Event()
        type(self).instances.append(self)

    async def start(self):
        self.stats.start_calls += 1
        if self.on_status_change:
            self.on_status_change("Listening")
        if self.on_mic_ready:
            self.on_mic_ready()
        await self._stop_event.wait()

    async def stop(self):
        self.stats.stop_calls += 1
        if not self.stats.injected:
            if self.on_transcription:
                self.on_transcription(self.stats.final_text, True)
            TextInjector(inject_immediately=False)._inject_text(self.stats.final_text + " ")
            if self.on_text_injected:
                self.on_text_injected(self.stats.final_text + " ")
            self.stats.injected = True
        self._stop_event.set()


class _FakeKeyboard:
    def __init__(self):
        self.pressed = False

    def is_pressed(self, hotkey: str) -> bool:
        return self.pressed


@pytest.fixture(autouse=True)
def _fake_live_provider_readiness(monkeypatch):
    """Keep lifecycle stress tests independent of local provider credentials."""
    monkeypatch.setattr(
        ScriberWebController, "_select_available_provider", lambda _self: "openai"
    )
    monkeypatch.setattr(
        ScriberWebController,
        "_validate_live_provider_ready",
        lambda _self, _provider: None,
    )


def _assert_controller_clean(ctl: ScriberWebController) -> None:
    assert ctl._is_listening is False
    assert ctl._is_stopping is False
    assert ctl._pipeline is None
    assert ctl._pipeline_task is None
    assert ctl._session_id is None


def _assert_pipeline_invariants() -> None:
    assert _StressFakePipeline.instances
    assert all(p.stats.start_calls <= 1 for p in _StressFakePipeline.instances)
    assert all(
        p.stats.start_calls == 1 or p.stats.stop_calls == 1
        for p in _StressFakePipeline.instances
    )
    assert all(p.stats.stop_calls <= 1 for p in _StressFakePipeline.instances)
    assert all(p.stats.injected for p in _StressFakePipeline.instances)


@pytest.mark.asyncio
async def test_hotkey_toggle_burst_stress_end_to_end(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    ctl = ScriberWebController(loop)
    _StressFakePipeline.instances.clear()
    _StressFakePipeline.created = 0

    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")

    with (
        patch("src.web_api.ScriberPipeline", _StressFakePipeline),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
    ):
        burst = []
        for _ in range(40):
            burst.append(asyncio.create_task(ctl.toggle_listening()))
            await asyncio.sleep(0.001)
        await asyncio.gather(*burst)

        # Force deterministic cleanup after burst.
        await ctl.stop_listening()
        await asyncio.sleep(0.05)

    _assert_controller_clean(ctl)
    _assert_pipeline_invariants()
    assert ctl._history
    assert all(rec.status == "completed" for rec in ctl._history)
    assert all(rec.content.strip().startswith("stress transcript") for rec in ctl._history)
    assert paste_mock.call_count == len(ctl._history)


@pytest.mark.asyncio
async def test_tauri_hotkey_start_spam_during_initializing_creates_single_recording(
    monkeypatch,
    tmp_path,
):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")

    ctl = ScriberWebController(loop)
    _StressFakePipeline.instances.clear()
    _StressFakePipeline.created = 0

    pause_entered = asyncio.Event()
    release_start = asyncio.Event()

    async def slow_prewarm_pause():
        pause_entered.set()
        await release_start.wait()

    with (
        patch("src.web_api.ScriberPipeline", _StressFakePipeline),
        patch.object(ctl, "_pause_idle_mic_prewarm_for_capture", side_effect=slow_prewarm_pause),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
    ):
        app = web_api.create_app(ctl)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            first_response_task = asyncio.create_task(client.post("/api/live-mic/toggle"))
            await asyncio.wait_for(pause_entered.wait(), timeout=1.0)

            burst_response_tasks = [
                asyncio.create_task(client.post("/api/live-mic/toggle"))
                for _ in range(24)
            ]
            await asyncio.sleep(0.01)
            release_start.set()

            responses = await asyncio.gather(first_response_task, *burst_response_tasks)
            payloads = [await response.json() for response in responses]
        finally:
            await client.close()

        assert [response.status for response in responses] == [200] * 25
        assert len(_StressFakePipeline.instances) == 1
        pipeline = _StressFakePipeline.instances[0]
        assert pipeline.stats.start_calls == 1
        assert pipeline.stats.stop_calls == 0
        assert ctl._pending_hotkey_toggle is False
        assert ctl.get_state()["listening"] is True
        assert ctl.get_state()["recordingState"] in {"initializing", "recording"}
        assert all(payload["listening"] is True for payload in payloads)

        await ctl.stop_listening()
        await asyncio.sleep(0.05)

    _assert_controller_clean(ctl)
    _assert_pipeline_invariants()
    assert len(ctl._history) == 1
    assert ctl._history[0].status == "completed"
    assert paste_mock.call_count == 1


@pytest.mark.asyncio
async def test_hotkey_ptt_press_release_burst_stress_end_to_end(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    ctl = ScriberWebController(loop)
    _StressFakePipeline.instances.clear()
    _StressFakePipeline.created = 0

    fake_keyboard = _FakeKeyboard()
    ctl._keyboard = fake_keyboard

    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")

    with (
        patch("src.web_api.ScriberPipeline", _StressFakePipeline),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=True) as paste_mock,
    ):
        ptt_task = asyncio.create_task(ctl._ptt_loop())
        try:
            for _ in range(8):
                fake_keyboard.pressed = True
                await asyncio.sleep(0.07)
                fake_keyboard.pressed = False
                await asyncio.sleep(0.07)
        finally:
            fake_keyboard.pressed = False
            await ctl.stop_listening()
            ptt_task.cancel()
            await asyncio.gather(ptt_task, return_exceptions=True)
            await asyncio.sleep(0.05)

    _assert_controller_clean(ctl)
    _assert_pipeline_invariants()
    assert len(ctl._history) >= 3
    assert all(rec.status == "completed" for rec in ctl._history)
    assert all(rec.content.strip().startswith("stress transcript") for rec in ctl._history)
    assert paste_mock.call_count == len(ctl._history)


@pytest.mark.asyncio
async def test_cancelling_ptt_poller_does_not_cancel_active_finalization(
    monkeypatch,
    tmp_path,
):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    ctl = ScriberWebController(loop)
    fake_keyboard = _FakeKeyboard()
    ctl._keyboard = fake_keyboard
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class _SlowStopPipeline(_StressFakePipeline):
        async def stop(self, **_kwargs):
            stop_entered.set()
            await release_stop.wait()
            await super().stop()

    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")

    with (
        patch("src.web_api.ScriberPipeline", _SlowStopPipeline),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch("src.injector.HAS_GUI", True),
        patch("src.injector._paste_text", return_value=True),
    ):
        ptt_task = asyncio.create_task(ctl._ptt_loop())
        fake_keyboard.pressed = True
        await asyncio.sleep(0.07)
        fake_keyboard.pressed = False
        await asyncio.wait_for(stop_entered.wait(), timeout=1.0)

        background_stop = ctl._background_stop_task
        assert background_stop is not None
        ptt_task.cancel()
        await asyncio.gather(ptt_task, return_exceptions=True)
        assert background_stop.cancelled() is False
        assert ctl._is_stopping is True

        release_stop.set()
        await asyncio.wait_for(asyncio.shield(background_stop), timeout=1.0)

    _assert_controller_clean(ctl)
    assert len(ctl._history) == 1
    assert ctl._history[0].status == "completed"
