"""Live Mic can capture another thought while earlier text is finishing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src import database, web_api
from src.api.live_mic_routes import LiveMicStartCommand
from src.gemini_realtime_stt import GeminiTranscribeLiveSTTService


class _ContinuationPipeline:
    """Separate physical capture and provider completion at the real boundary."""

    def __init__(self, *, injected: list[str], **kwargs):
        self.service_name = kwargs["service_name"]
        self.callbacks = kwargs
        self.text_injection_enabled = kwargs["text_injection_enabled"]
        self.injected = injected
        self.text = ""
        self.failure: Exception | None = None
        self.native_confirmation: bool | None = True
        self.native_stopped = False
        self.native_gate = asyncio.Event()
        self.native_gate.set()
        self.stop_started = asyncio.Event()
        self.capture_stopped = asyncio.Event()
        self.provider_gate = asyncio.Event()
        self.provider_completed = asyncio.Event()
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()

    async def start(self):
        self.callbacks["on_mic_ready"]()
        self.callbacks["on_status_change"]("Listening")
        self.ready.set()
        await self.finished.wait()

    def native_audio_stop_confirmed(self):
        return self.native_confirmation if self.native_stopped else False

    def audio_diagnostics(self):
        return {"audioLevelSampleCount": 5, "maxObservedRms": 0.5, "speechObserved": True}

    async def stop(self, timeout_secs=None):
        self.stop_started.set()
        await self.native_gate.wait()
        self.native_stopped = True
        await self.callbacks["on_capture_stopped"]()
        self.capture_stopped.set()
        await self.provider_gate.wait()
        if self.text:
            self.callbacks["on_transcription"](self.text, True)
            if self.text_injection_enabled:
                self.injected.append(self.text)
                self.callbacks["on_text_injected"](self.text)
        self.callbacks["on_status_change"]("Stopped")
        self.provider_completed.set()
        self.finished.set()
        if self.failure is not None:
            raise self.failure


@dataclass
class _Harness:
    controller: web_api.ScriberWebController
    pipelines: list[_ContinuationPipeline]
    injected: list[str]
    pipeline_created: asyncio.Event
    stop_tasks: list[asyncio.Task] = field(default_factory=list)
    cleanup_gates: list[asyncio.Event] = field(default_factory=list)

    async def start(self, text: str, *, post_process: bool = False):
        assert await self.controller.start_listening(post_process=post_process) is None
        pipeline = self.pipelines[-1]
        pipeline.text = text
        await asyncio.wait_for(pipeline.ready.wait(), 2)
        return pipeline, self.controller._current

    def stop(self):
        outcome = self.controller.request_async_stop_listening()
        assert outcome["stopScheduled"] is True
        task = self.controller._background_stop_task
        assert task is not None
        self.stop_tasks.append(task)
        return task


@pytest_asyncio.fixture
async def continuation(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    monkeypatch.setenv("SCRIBER_MIC_WATCHDOG_INTERVAL_SEC", "0")
    monkeypatch.setenv("SCRIBER_PREWARM_MODELS_ON_STARTUP", "0")
    monkeypatch.setenv("SCRIBER_PREWARM_STT_ON_STARTUP", "0")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", False)
    monkeypatch.setattr(web_api.Config, "POST_PROCESSING_ENABLED", True)
    monkeypatch.setattr(web_api.Config, "POST_PROCESSING_ENGINE", "cloud")
    pipelines: list[_ContinuationPipeline] = []
    injected: list[str] = []
    pipeline_created = asyncio.Event()

    def construct(**kwargs):
        pipeline = _ContinuationPipeline(injected=injected, **kwargs)
        pipelines.append(pipeline)
        pipeline_created.set()
        return pipeline

    monkeypatch.setattr(web_api, "ScriberPipeline", construct)
    controller = web_api.ScriberWebController(asyncio.get_running_loop())
    controller._live_toggle_start_grace_seconds = 0
    monkeypatch.setattr(controller, "_select_available_provider", lambda: "azure_mai")
    monkeypatch.setattr(controller, "_validate_live_provider_ready", lambda _provider: None)
    monkeypatch.setattr(controller, "_get_overlay", lambda: None)
    monkeypatch.setattr(controller, "_pause_idle_mic_prewarm_for_capture", AsyncMock())
    monkeypatch.setattr(controller, "_resume_idle_mic_prewarm_after_capture", MagicMock())
    monkeypatch.setattr(controller, "_start_mic_watchdog", MagicMock())
    monkeypatch.setattr(controller, "_save_transcript_to_db_async", AsyncMock())
    monkeypatch.setattr(controller, "_broadcast_history_updated", AsyncMock())
    monkeypatch.setattr(controller, "broadcast", AsyncMock())
    for method in (
        "_show_initializing_overlay_async",
        "_show_recording_overlay_async",
        "_show_transcribing_overlay_async",
        "_hide_recording_overlay_async",
    ):
        monkeypatch.setattr(controller, method, MagicMock())

    async def inject(text, **_kwargs):
        injected.append(text)
        return True

    monkeypatch.setattr(controller, "_inject_live_transcript_text", inject)
    harness = _Harness(controller, pipelines, injected, pipeline_created)
    try:
        yield harness
    finally:
        for gate in harness.cleanup_gates:
            gate.set()
        for pipeline in pipelines:
            pipeline.native_confirmation = True
            pipeline.native_gate.set()
            pipeline.provider_gate.set()
        if controller._is_listening:
            harness.stop_tasks.append(asyncio.create_task(controller.stop_listening()))
        if harness.stop_tasks:
            await asyncio.wait_for(asyncio.gather(*harness.stop_tasks, return_exceptions=True), 5)
        claim = web_api._audio_admission_owner(controller).current
        if claim is not None:
            await web_api._release_persistent_audio(controller, claim)
        await controller.drain_background_tasks_for_shutdown(timeout_seconds=1)
        controller.shutdown()
        controller.close_persistence_stores()


@pytest.mark.asyncio
async def test_capture_restarts_before_old_provider_finishes_and_late_text_stays_separate(continuation):
    ctl = continuation.controller
    first, first_record = await continuation.start("Erster Gedanke.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    assert not first_stop.done()

    second, second_record = await continuation.start("Nachtrag.")
    assert second is not first
    assert second_record.id != first_record.id
    assert ctl._persistent_audio_claim.owner_id == second_record.id
    assert second.text_injection_enabled is False
    prior_hide_count = len(ctl._hide_recording_overlay_async.call_args_list)
    first.provider_gate.set()
    await asyncio.wait_for(first_stop, 2)

    assert first_record.content_text() == "Erster Gedanke."
    assert second_record.content_text() == ""
    assert ctl._current is second_record
    assert ctl._session_id == second_record.id
    assert ctl._is_listening is True
    assert ctl._active_provider == "azure_mai"
    assert ctl.get_state()["recordingState"] == "recording"
    assert ctl.get_state()["transcribing"] is False
    assert ctl._persistent_audio_claim.owner_id == second_record.id
    assert not any(
        call.kwargs.get("session_id") == first_record.id
        for call in ctl._hide_recording_overlay_async.call_args_list[prior_hide_count:]
    )


@pytest.mark.asyncio
async def test_successor_stop_is_accepted_and_injection_waits_for_prior_text(continuation):
    first, first_record = await continuation.start("Eins.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, second_record = await continuation.start("Zwei.")
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)
    assert continuation.injected == []
    assert not second_stop.done()

    first.provider_gate.set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)
    assert continuation.injected == ["Eins.", "Zwei."]
    assert first_record.content_text() == "Eins."
    assert second_record.content_text() == "Zwei."
    assert first_record.status == second_record.status == "completed"
    assert continuation.controller._persistent_audio_claim is None


@pytest.mark.asyncio
async def test_gemini_completed_revision_releases_ready_successor_without_provider_timeout(continuation):
    first, _ = await continuation.start("")
    first.callbacks["on_transcription"]("Erster Absatz.", True)
    continuation.injected.append("Erster Absatz.")
    first.callbacks["on_text_injected"]("Erster Absatz.")
    service = GeminiTranscribeLiveSTTService(api_key="secret", final_quiet_seconds=0.01)
    service.push_frame = AsyncMock()

    class WebSocket:
        closed = False

        async def send_str(self, value):
            if "audio" in json.loads(value).get("realtimeInput", {}):
                await service._handle_response(
                    json.dumps(
                        {
                            "serverContent": {
                                "interimInputTranscription": {"text": "Offen A erweitert"},
                                "inputTranscription": {"text": "Final A"},
                                "generationComplete": True,
                            }
                        }
                    )
                )

        async def close(self):
            self.closed = True

    service._ws = WebSocket()
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}}))
    service._pcm_buffer.extend(bytes(3200))
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, _ = await continuation.start("Nachtrag.")
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)
    assert continuation.injected == ["Erster Absatz."]
    assert not second_stop.done()

    async def drain():
        try:
            await service._close(wait_for_final=True)
        finally:
            first.provider_gate.set()

    # A completed SMART revision used to keep the predecessor open for the
    # default 15 seconds, blocking a successor whose provider was already done.
    await asyncio.wait_for(drain(), 1)
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)
    assert continuation.injected == ["Erster Absatz.", "Nachtrag."]
    assert not service._terminal_failure


@pytest.mark.asyncio
async def test_three_overlapping_dictations_keep_capture_order(continuation):
    sessions = []
    for text in ("Eins.", "Zwei.", "Drei."):
        pipeline, record = await continuation.start(text)
        stop_task = continuation.stop()
        await asyncio.wait_for(pipeline.capture_stopped.wait(), 2)
        sessions.append((pipeline, record, stop_task))
    for pipeline, _record, _task in reversed(sessions[1:]):
        pipeline.provider_gate.set()
        await asyncio.wait_for(pipeline.provider_completed.wait(), 2)
    assert continuation.injected == []

    sessions[0][0].provider_gate.set()
    await asyncio.wait_for(asyncio.gather(*(session[2] for session in sessions)), 2)
    assert continuation.injected == ["Eins.", "Zwei.", "Drei."]
    assert [session[1].content_text() for session in sessions] == continuation.injected


@pytest.mark.asyncio
async def test_prior_provider_failure_does_not_block_successor_completion(continuation):
    first, first_record = await continuation.start("")
    first.failure = RuntimeError("synthetic provider failure")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, second_record = await continuation.start("Nachtrag bleibt erhalten.")
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)
    first.provider_gate.set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)

    assert first_record.status == "failed"
    assert second_record.status == "completed"
    assert continuation.injected == ["Nachtrag bleibt erhalten."]


@pytest.mark.asyncio
async def test_early_lease_release_failure_preserves_pending_provider_text(continuation, monkeypatch):
    ctl = continuation.controller
    first, first_record = await continuation.start("Dieser Text darf nicht verloren gehen.")
    release = web_api._release_persistent_audio
    attempts = 0

    async def transient_release_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary admission store failure")
        return await release(*args, **kwargs)

    monkeypatch.setattr(web_api, "_release_persistent_audio", transient_release_failure)
    stop_task = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    assert not stop_task.done()
    assert ctl._is_stopping is True
    assert ctl._persistent_audio_claim.owner_id == first_record.id
    first.provider_gate.set()
    await asyncio.wait_for(stop_task, 2)

    assert first_record.status == "completed"
    assert first_record.content_text() == "Dieser Text darf nicht verloren gehen."
    assert continuation.injected == ["Dieser Text darf nicht verloren gehen."]
    assert ctl._persistent_audio_claim is None


@pytest.mark.asyncio
async def test_capture_during_old_polishing_preserves_both_post_processing_modes(continuation, monkeypatch):
    ctl = continuation.controller
    polishing_started = asyncio.Event()
    polishing_gate = asyncio.Event()
    continuation.cleanup_gates.append(polishing_gate)
    polished_ids = []

    async def polish(record, **kwargs):
        polished_ids.append(record.id)
        if len(polished_ids) == 1:
            polishing_started.set()
            await polishing_gate.wait()
        await ctl._inject_live_transcript_text("Poliert: " + record.content_text(), **kwargs)

    monkeypatch.setattr(ctl, "_post_process_and_inject_live_transcript", polish)
    first, first_record = await continuation.start("Eins.", post_process=True)
    first_stop = continuation.stop()
    first.provider_gate.set()
    await asyncio.wait_for(polishing_started.wait(), 2)
    second, second_record = await continuation.start("Zwei.", post_process=True)
    assert ctl._is_listening is True
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)
    assert continuation.injected == []
    polishing_gate.set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)

    assert polished_ids == [first_record.id, second_record.id]
    assert continuation.injected == ["Poliert: Eins.", "Poliert: Zwei."]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, None])
async def test_unconfirmed_native_stop_keeps_capture_exclusive(continuation, confirmation):
    ctl = continuation.controller
    first, first_record = await continuation.start("Eins.")
    first.native_confirmation = confirmation
    continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    assert ctl._persistent_audio_claim.owner_id == first_record.id
    assert ctl._is_stopping is True

    command = LiveMicStartCommand(post_process=True, tauri_hotkey_marker=None, provider_replay_activation=False)
    await ctl.start_live_mic(command)
    assert len(continuation.pipelines) == 1
    assert ctl._persistent_audio_claim.owner_id == first_record.id
    ctl.request_async_stop_listening()


@pytest.mark.asyncio
async def test_stop_cancels_start_queued_during_native_stop(continuation):
    ctl = continuation.controller
    first, _record = await continuation.start("Eins.")
    first.native_gate.clear()
    stop_task = continuation.stop()
    await asyncio.wait_for(first.stop_started.wait(), 2)
    command = LiveMicStartCommand(post_process=True, tauri_hotkey_marker=None, provider_replay_activation=False)
    await ctl.start_live_mic(command)
    assert ctl.get_state()["micStartPending"] is True
    outcome = ctl.request_async_stop_listening()
    assert outcome["stopAccepted"] is True
    assert ctl.get_state()["micStartPending"] is False
    first.native_gate.set()
    first.provider_gate.set()
    await asyncio.wait_for(stop_task, 2)
    assert len(continuation.pipelines) == 1


@pytest.mark.asyncio
async def test_start_immediately_after_stop_request_is_retained_before_finalizer_runs(continuation):
    ctl = continuation.controller
    first, _record = await continuation.start("Eins.")
    first.native_gate.clear()
    continuation.stop()
    command = LiveMicStartCommand(post_process=True, tauri_hotkey_marker=None, provider_replay_activation=False)
    await ctl.start_live_mic(command)

    assert ctl.get_state()["micStartPending"] is True
    assert len(continuation.pipelines) == 1
    ctl.request_async_stop_listening()
    assert ctl.get_state()["micStartPending"] is False


@pytest.mark.asyncio
async def test_queued_post_processing_start_begins_at_native_release(continuation):
    ctl = continuation.controller
    first, _record = await continuation.start("Eins.")
    first.native_gate.clear()
    stop_task = continuation.stop()
    await asyncio.wait_for(first.stop_started.wait(), 2)
    await ctl._handle_post_processing_hotkey_toggle()
    assert ctl.get_state()["micStartPending"] is True
    continuation.pipeline_created.clear()
    first.native_gate.set()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    await asyncio.wait_for(continuation.pipeline_created.wait(), 2)
    assert len(continuation.pipelines) == 2
    second = continuation.pipelines[-1]
    await asyncio.wait_for(second.ready.wait(), 2)

    assert not stop_task.done()
    assert ctl._is_listening is True
    assert ctl._session_id in ctl._post_processing_session_ids
    assert second.text_injection_enabled is False
    assert ctl.get_state()["micStartPending"] is False


@pytest.mark.asyncio
async def test_silence_timeout_only_stops_its_current_capture_once(continuation):
    ctl = continuation.controller
    first, _first_record = await continuation.start("Eins.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, second_record = await continuation.start("Zwei.")
    first.callbacks["on_silence_timeout"]()
    assert ctl._is_listening is True
    assert ctl._session_id == second_record.id
    assert ctl._background_stop_task is None

    second.callbacks["on_silence_timeout"]()
    second_stop = ctl._background_stop_task
    assert second_stop is not None
    continuation.stop_tasks.append(second_stop)
    second.callbacks["on_silence_timeout"]()
    assert ctl._background_stop_task is second_stop
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    assert ctl.get_state()["micStartPending"] is False
    first.provider_gate.set()
    second.provider_gate.set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 2)
    assert continuation.injected == ["Eins.", "Zwei."]


@pytest.mark.asyncio
async def test_shutdown_drains_every_finalizer_before_closing_polisher(continuation, monkeypatch):
    ctl = continuation.controller
    first, _first_record = await continuation.start("Eins.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, _second_record = await continuation.start("Zwei.")
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)

    async def close_polisher():
        continuation.injected.append("polisher closed")

    close = AsyncMock(side_effect=close_polisher)
    monkeypatch.setattr(ctl._local_polisher, "close", close)
    drain = asyncio.create_task(ctl.drain_background_tasks_for_shutdown(timeout_seconds=2))
    continuation.stop_tasks.append(drain)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(drain), 0.05)
    close.assert_not_awaited()
    assert not first_stop.cancelled()
    assert not second_stop.cancelled()

    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)
    first.provider_gate.set()
    assert await asyncio.wait_for(drain, 3) == 0
    close.assert_awaited_once()
    assert continuation.injected == ["Eins.", "Zwei.", "polisher closed"]
    assert ctl._live_mic_finalizer_tasks == set()
    assert ctl._live_mic_finalization_done == {}


@pytest.mark.asyncio
async def test_cancelled_stop_caller_cannot_release_successor_before_old_text_finishes(continuation):
    first, first_record = await continuation.start("Eins bleibt erhalten.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    second, _second_record = await continuation.start("Zwei folgt danach.")
    second_stop = continuation.stop()
    await asyncio.wait_for(second.capture_stopped.wait(), 2)
    second.provider_gate.set()
    await asyncio.wait_for(second.provider_completed.wait(), 2)

    first_stop.cancel()
    done, _pending = await asyncio.wait({second_stop}, timeout=0.05)
    assert not done
    assert continuation.injected == []
    first.provider_gate.set()
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop, return_exceptions=True), 2)

    assert first_record.status == "completed"
    assert first_record.content_text() == "Eins bleibt erhalten."
    assert continuation.injected == ["Eins bleibt erhalten.", "Zwei folgt danach."]


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_cancel_transcript_finalization(continuation):
    ctl = continuation.controller
    first, first_record = await continuation.start("Spätes Ergebnis bleibt erhalten.")
    stop_task = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)

    pending = await ctl.drain_background_tasks_for_shutdown(timeout_seconds=0.01)
    assert pending > 0
    assert ctl._live_mic_finalizer_tasks
    assert not stop_task.done()
    first.provider_gate.set()
    await asyncio.wait_for(stop_task, 2)

    assert first_record.content_text() == "Spätes Ergebnis bleibt erhalten."
    assert first_record.status == "completed"
    assert continuation.injected == ["Spätes Ergebnis bleibt erhalten."]


@pytest.mark.asyncio
async def test_shutdown_waits_for_admitted_finalizer_blocked_on_capture_lock(continuation, monkeypatch):
    ctl = continuation.controller
    pipeline, _record = await continuation.start("Noch glätten.", post_process=True)
    admitted = asyncio.Event()
    spawn = ctl._live_mic_finalizer_supervisor.spawn

    def observe_admission(*args, **kwargs):
        task = spawn(*args, **kwargs)
        admitted.set()
        return task

    close = AsyncMock(side_effect=lambda: continuation.injected.append("polisher closed"))

    async def polish(record, **_kwargs):
        close.assert_not_awaited()
        continuation.injected.append("Poliert: " + record.content_text())

    monkeypatch.setattr(ctl._live_mic_finalizer_supervisor, "spawn", observe_admission)
    monkeypatch.setattr(ctl._local_polisher, "close", close)
    monkeypatch.setattr(ctl, "_post_process_and_inject_live_transcript", polish)
    await ctl._listening_lock.acquire()
    lock_held_by_test = True
    try:
        stop_task = continuation.stop()
        await asyncio.wait_for(admitted.wait(), 2)
        assert not pipeline.stop_started.is_set()
        drain = asyncio.create_task(ctl.drain_background_tasks_for_shutdown(timeout_seconds=2))
        continuation.stop_tasks.append(drain)
        done, _pending = await asyncio.wait({drain}, timeout=0.05)
        assert not done
        close.assert_not_awaited()
        ctl._listening_lock.release()
        lock_held_by_test = False
        await asyncio.wait_for(pipeline.capture_stopped.wait(), 2)
        close.assert_not_awaited()
        pipeline.provider_gate.set()
        await asyncio.wait_for(stop_task, 2)
        assert await asyncio.wait_for(drain, 3) == 0
    finally:
        if lock_held_by_test:
            ctl._listening_lock.release()

    assert continuation.injected == ["Poliert: Noch glätten.", "polisher closed"]
    assert ctl._live_mic_finalizer_tasks == set()


@pytest.mark.asyncio
async def test_shutdown_admits_queued_stop_before_sealing_polisher_dependencies(continuation, monkeypatch):
    ctl = continuation.controller
    pipeline, record = await continuation.start("Vorgemerkter Stopp.", post_process=True)
    close = AsyncMock(side_effect=lambda: continuation.injected.append("polisher closed"))

    async def polish(record, **_kwargs):
        close.assert_not_awaited()
        continuation.injected.append("Poliert: " + record.content_text())

    monkeypatch.setattr(ctl._local_polisher, "close", close)
    monkeypatch.setattr(ctl, "_post_process_and_inject_live_transcript", polish)
    stop_task = continuation.stop()
    pipeline.provider_gate.set()
    # Enter shutdown before the scheduled stop-request waiter gets a turn.
    assert await ctl.drain_background_tasks_for_shutdown(timeout_seconds=2) == 0
    await asyncio.wait_for(stop_task, 2)

    assert record.status == "completed"
    assert continuation.injected == ["Poliert: Vorgemerkter Stopp.", "polisher closed"]
    assert ctl._live_mic_finalizer_supervisor.sealed


@pytest.mark.asyncio
async def test_shutdown_seals_finalizer_admission_before_polisher_close(continuation, monkeypatch):
    ctl = continuation.controller
    closing = asyncio.Event()
    allow_close = asyncio.Event()
    continuation.cleanup_gates.append(allow_close)

    async def close_polisher():
        closing.set()
        await allow_close.wait()

    monkeypatch.setattr(ctl._local_polisher, "close", close_polisher)
    finalize = AsyncMock()
    monkeypatch.setattr(ctl, "_stop_listening_session", finalize)
    drain = asyncio.create_task(ctl.drain_background_tasks_for_shutdown(timeout_seconds=2))
    continuation.stop_tasks.append(drain)
    await asyncio.wait_for(closing.wait(), 2)
    assert ctl._live_mic_finalizer_supervisor.sealed
    assert await ctl.stop_listening() is None
    finalize.assert_not_awaited()
    allow_close.set()
    assert await asyncio.wait_for(drain, 3) == 0
