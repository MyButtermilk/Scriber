"""Live Mic can capture another thought while earlier text is finishing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src import database, native_overlay, web_api
from src.api.live_mic_routes import LiveMicStartCommand, register_live_mic_routes
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
        if self.provider_completed.is_set():
            if self.failure is not None:
                raise self.failure
            return
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
@pytest.mark.parametrize("cancel_successor", [False, True], ids=["old-finalizer", "successor-cancel"])
async def test_old_finalizer_hide_is_owner_bound_while_successor_is_still_initializing(
    continuation, monkeypatch, cancel_successor
):
    ctl = continuation.controller
    loop = asyncio.get_running_loop()
    calls = []
    old_hide_dispatched = asyncio.Event()
    old_session_id = None

    def record_shell_command(command, payload=None, **_kwargs):
        calls.append((command, payload))
        if command == "overlayHide" and old_session_id is not None:
            loop.call_soon_threadsafe(old_hide_dispatched.set)
        return {"success": True}

    monkeypatch.setattr(native_overlay, "_tauri_overlay_enabled", lambda: True)
    monkeypatch.setattr(native_overlay, "_overlay", native_overlay.RecordingOverlay())
    monkeypatch.setattr(native_overlay, "_call_overlay_response", record_shell_command)
    for method in (
        "_show_initializing_overlay_async",
        "_show_recording_overlay_async",
        "_show_transcribing_overlay_async",
        "_hide_recording_overlay_async",
    ):
        monkeypatch.setattr(ctl, method, getattr(web_api.ScriberWebController, method).__get__(ctl))

    first, first_record = await continuation.start("Erster Gedanke.")
    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)
    await asyncio.gather(*list(ctl._overlay_tasks))
    old_session_id = first_record.id

    successor_preparing = asyncio.Event()
    successor_release = asyncio.Event()
    continuation.cleanup_gates.append(successor_release)

    async def pause_successor_setup():
        successor_preparing.set()
        await successor_release.wait()

    monkeypatch.setattr(ctl, "_pause_idle_mic_prewarm_for_capture", pause_successor_setup)
    successor_start = asyncio.create_task(ctl.start_listening())
    try:
        await asyncio.wait_for(successor_preparing.wait(), 2)
        await asyncio.gather(*list(ctl._overlay_tasks))
        successor_show = calls[-1]
        assert successor_show[0] == "overlayShow"
        assert successor_show[1]["mode"] == "initializing"
        assert successor_show[1]["ownerId"] != old_session_id
        # The old session is still current until the successor pipeline exists.
        assert ctl._session_id == old_session_id
        calls_before_completion = len(calls)
        if cancel_successor:
            successor_stop = continuation.stop()
            successor_release.set()
            await asyncio.wait_for(successor_start, 2)
            await asyncio.wait_for(successor_stop, 2)
            await asyncio.gather(*list(ctl._overlay_tasks))
            assert ctl._session_id == old_session_id
            assert len(continuation.pipelines) == 1
            assert ("overlayHide", {"expectedOwnerId": successor_show[1]["ownerId"]}) in calls[calls_before_completion:]
        else:
            first.provider_gate.set()
            await asyncio.wait_for(old_hide_dispatched.wait(), 2)
            assert ("overlayHide", {"expectedOwnerId": old_session_id}) in calls[calls_before_completion:]
    finally:
        successor_release.set()
        first.provider_gate.set()
        await asyncio.wait_for(successor_start, 2)
        await asyncio.wait_for(first_stop, 2)
        await asyncio.gather(*list(ctl._overlay_tasks))


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
async def test_gemini_ambiguous_revision_releases_ready_successor_after_safe_deadline(continuation):
    first, _ = await continuation.start("")
    first.callbacks["on_transcription"]("Erster Absatz.", True)
    continuation.injected.append("Erster Absatz.")
    first.callbacks["on_text_injected"]("Erster Absatz.")
    service = GeminiTranscribeLiveSTTService(api_key="secret", final_quiet_seconds=0.01, final_timeout_seconds=0.1)
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

    # No correlated turn ID exists, so even a ready successor must wait for
    # the ambiguous predecessor's full deadline before inserting in order.
    draining = asyncio.create_task(drain())
    await asyncio.sleep(0.04)
    assert not draining.done()
    assert not second_stop.done()
    await asyncio.wait_for(draining, 1)
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
@pytest.mark.parametrize("confirmation", [False, None, True])
async def test_pipeline_exit_keeps_native_owner_until_confirmed_stop_retry(continuation, confirmation):
    ctl = continuation.controller
    first, first_record = await continuation.start("")
    first.native_confirmation = confirmation
    first.native_stopped = True
    task = ctl._pipeline_task
    first.finished.set()
    await asyncio.wait_for(task, 2)
    await ctl._wait_for_detached_tasks()

    if confirmation is not True:
        assert ctl.get_state()["nativeCleanupPending"] is True
        assert ctl._persistent_audio_claim is not None
        assert ctl._persistent_audio_claim.owner_id == first_record.id
        error = await ctl.start_listening()
        assert error is not None and error.code == "recording_cleanup_pending"
        assert len(continuation.pipelines) == 1

        first.native_confirmation = True
        first.provider_gate.set()
        outcome = ctl.request_async_stop_listening()
        assert outcome["stopScheduled"] is True
        await asyncio.wait_for(ctl._background_stop_task, 2)

    assert ctl._persistent_audio_claim is None
    assert ctl.get_state()["nativeCleanupPending"] is False
    second, second_record = await continuation.start("Nachtrag.")
    assert second is not first
    assert ctl._persistent_audio_claim.owner_id == second_record.id


@pytest.mark.asyncio
async def test_failed_stop_retries_exact_native_owner_without_republishing_transcript(continuation, monkeypatch):
    ctl = continuation.controller
    first, first_record = await continuation.start("Erster Gedanke.")
    first.native_confirmation = False
    first.failure = RuntimeError("Native microphone stop was not confirmed")
    first.provider_gate.set()
    await asyncio.wait_for(continuation.stop(), 2)
    assert ctl._persistent_audio_claim.owner_id == first_record.id
    assert ctl._pending_live_mic_native_cleanup[1] is first
    saved_before_retry = ctl._save_transcript_to_db_async.await_count

    outcome = await ctl.stop_live_mic()
    assert outcome.status == 400
    assert outcome.payload["code"] == "recording_cleanup_pending"
    assert ctl._persistent_audio_claim.owner_id == first_record.id

    first.native_confirmation = True
    store = web_api._audio_admission_owner(ctl).store
    original_release = store.release

    def fail_release(_claim):
        raise OSError("temporary admission storage failure")

    monkeypatch.setattr(store, "release", fail_release)
    assert (await ctl.stop_live_mic()).status == 400
    assert ctl._persistent_audio_claim.owner_id == first_record.id
    monkeypatch.setattr(store, "release", original_release)
    # Downstream failure stays sticky even after native cleanup succeeds.
    assert (await ctl.stop_live_mic()).status == 200
    assert ctl._persistent_audio_claim is None
    assert ctl._save_transcript_to_db_async.await_count == saved_before_retry
    assert continuation.injected == ["Erster Gedanke."]
    second, second_record = await continuation.start("Nachtrag.")
    assert second is not first
    assert ctl._persistent_audio_claim.owner_id == second_record.id
    assert await ctl._emergency_stop_pipeline(session_id=first_record.id) is False
    assert ctl._persistent_audio_claim.owner_id == second_record.id


@pytest.mark.asyncio
async def test_audio_lease_loss_retries_failed_emergency_cleanup(continuation):
    ctl = continuation.controller
    first, first_record = await continuation.start("")
    first.native_confirmation = None
    first.provider_gate.set()
    assert await ctl._emergency_stop_pipeline(session_id=first_record.id) is False
    owner = web_api._audio_admission_owner(ctl)
    claim = owner.current
    assert claim.owner_id == first_record.id

    first.native_confirmation = True
    await owner.note_loss(claim, reason="test_lease_expired")
    assert owner.current is None
    assert ctl._pending_live_mic_native_cleanup is None
    assert ctl._is_stopping is False


@pytest.mark.asyncio
async def test_shutdown_retries_retained_native_cleanup_after_pipeline_exit(continuation):
    ctl = continuation.controller
    first, _first_record = await continuation.start("")
    first.native_confirmation = False
    first.native_stopped = True
    task = ctl._pipeline_task
    first.finished.set()
    await task
    await ctl._wait_for_detached_tasks()
    assert ctl._persistent_audio_claim is not None

    first.native_confirmation = True
    first.provider_gate.set()
    await ctl.drain_background_tasks_for_shutdown(timeout_seconds=2)
    assert ctl._persistent_audio_claim is None
    assert ctl._pending_live_mic_native_cleanup is None


@pytest.mark.asyncio
async def test_temporary_prewarm_stop_timeout_retains_claim_after_pipeline_exit(continuation, monkeypatch):
    ctl = continuation.controller
    first, record = await continuation.start("")
    monkeypatch.setattr(web_api.Config, "MIC_POST_RECORDING_PREWARM_SECONDS", 0.0)
    manager = ctl._mic_prewarm
    manager._pending_stop_prewarm_id = "still-stopping"
    monkeypatch.setattr(manager, "stop", lambda **_kwargs: None)
    task = ctl._pipeline_task
    first.native_stopped = True
    first.finished.set()
    await task
    await ctl._wait_for_detached_tasks()
    assert ctl._persistent_audio_claim.owner_id == record.id

    def confirm_stop(**_kwargs):
        manager._pending_stop_prewarm_id = None

    monkeypatch.setattr(manager, "stop", confirm_stop)
    first.provider_gate.set()
    assert (await ctl.stop_live_mic()).status == 200
    assert ctl._persistent_audio_claim is None
    assert not manager.is_active


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
@pytest.mark.parametrize("cancel_successor", [False, True])
async def test_new_start_queued_behind_cancelled_start_is_eventually_admitted(
    continuation, monkeypatch, cancel_successor
):
    ctl = continuation.controller
    starting = asyncio.Event()
    finish_setup = asyncio.Event()
    continuation.cleanup_gates.append(finish_setup)
    continuation_ready = asyncio.Event()
    allow_continuation = asyncio.Event()
    continuation.cleanup_gates.append(allow_continuation)
    original_continue = ctl._start_pending_live_mic

    async def hold_continuation():
        continuation_ready.set()
        await allow_continuation.wait()
        await original_continue()

    if cancel_successor:
        monkeypatch.setattr(ctl, "_start_pending_live_mic", hold_continuation)

    async def pause():
        starting.set()
        await finish_setup.wait()

    monkeypatch.setattr(ctl, "_pause_idle_mic_prewarm_for_capture", pause)
    first_start = asyncio.create_task(ctl.start_listening())
    await asyncio.wait_for(starting.wait(), 2)
    stop = continuation.stop()
    second_start = asyncio.create_task(ctl.start_listening(post_process=True))
    # The stop wrapper admits its own finalizer on the next event-loop turn;
    # this later start can reach the shared lock before that finalizer.
    await asyncio.sleep(0)
    finish_setup.set()
    await asyncio.wait_for(asyncio.gather(first_start, second_start, stop), 2)
    if cancel_successor:
        await asyncio.wait_for(continuation_ready.wait(), 2)
        ctl.request_async_stop_listening()
        allow_continuation.set()
    await ctl._wait_for_detached_tasks()
    assert ctl._pending_live_mic_start is None
    if cancel_successor:
        assert not continuation.pipelines
        assert not ctl._is_listening
    else:
        assert len(continuation.pipelines) == 1
        assert ctl._is_listening
        assert ctl._session_id in ctl._post_processing_session_ids


@pytest.mark.asyncio
async def test_stop_revokes_dispatched_continuation_waiting_for_admission(continuation, monkeypatch):
    ctl = continuation.controller
    dispatched = asyncio.Event()
    original = ctl._execute_live_mic_start

    async def execute(command, **kwargs):
        dispatched.set()
        return await original(command, **kwargs)

    monkeypatch.setattr(ctl, "_execute_live_mic_start", execute)
    ctl._queue_live_mic_start()
    background = asyncio.create_task(asyncio.sleep(0))
    await background
    ctl._background_stop_task = background
    async with ctl._listening_lock:
        ctl._on_background_stop_done(background)
        await asyncio.wait_for(dispatched.wait(), 2)
        ctl.request_async_stop_listening()
    await ctl._wait_for_detached_tasks()

    assert not continuation.pipelines
    assert ctl._pending_live_mic_start is None
    assert web_api._audio_admission_owner(ctl).current is None


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
@pytest.mark.parametrize("post_process", [False, True], ids=["dictation-hotkey", "post-processing-hotkey"])
async def test_queued_hotkey_start_begins_at_native_release(continuation, post_process):
    ctl = continuation.controller
    first, _record = await continuation.start("Eins.")
    first.native_gate.clear()
    stop_task = continuation.stop()
    await asyncio.wait_for(first.stop_started.wait(), 2)
    if post_process:
        await ctl._handle_post_processing_hotkey_toggle()
    else:
        await ctl._handle_hotkey_toggle()
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
    assert (ctl._session_id in ctl._post_processing_session_ids) is post_process
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


@pytest.mark.asyncio
async def test_push_to_talk_release_before_delayed_press_cannot_start_capture(continuation):
    ctl = continuation.controller
    app = web.Application()
    register_live_mic_routes(app, control=ctl, replay=MagicMock(pending_activation=False))
    gesture = str(uuid4())
    async with TestClient(TestServer(app)) as client:
        released = await client.post(
            "/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": gesture, "revision": 2}}
        )
        assert released.status == 202
        pressed = await client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": gesture, "revision": 1}})
        assert pressed.status == 200
        assert not continuation.pipelines
        assert web_api._audio_admission_owner(ctl).current is None


@pytest.mark.asyncio
async def test_push_to_talk_late_release_cannot_stop_successor(continuation):
    ctl = continuation.controller
    app = web.Application()
    register_live_mic_routes(app, control=ctl, replay=MagicMock(pending_activation=False))
    old_gesture, new_gesture = str(uuid4()), str(uuid4())
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": old_gesture, "revision": 2}})
        await client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": new_gesture, "revision": 3}})
        pipeline = continuation.pipelines[-1]
        pipeline.provider_gate.set()
        await asyncio.wait_for(pipeline.ready.wait(), 2)
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": old_gesture, "revision": 2}})
        assert ctl._background_stop_task is None
        assert ctl._is_listening
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": new_gesture, "revision": 4}})
        await asyncio.wait_for(pipeline.stop_started.wait(), 2)
        assert ctl._background_stop_task is not None
        await ctl._background_stop_task
        assert not ctl._is_listening
        ui_pipeline, _ = await continuation.start("UI recording")
        pressed = await client.post(
            "/api/live-mic/start", json={"pushToTalk": {"gestureId": old_gesture, "revision": 5}}
        )
        assert (await pressed.json())["pushToTalkIgnored"] is True
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": old_gesture, "revision": 6}})
        assert ctl._is_listening
        assert not ui_pipeline.stop_started.is_set()


@pytest.mark.asyncio
async def test_push_to_talk_release_cancels_press_waiting_for_audio_admission(continuation, monkeypatch):
    ctl = continuation.controller
    app = web.Application()
    register_live_mic_routes(app, control=ctl, replay=MagicMock(pending_activation=False))
    gesture = str(uuid4())
    entered = asyncio.Event()
    original = ctl.start_listening

    async def waiting_start(**kwargs):
        entered.set()
        return await original(**kwargs)

    monkeypatch.setattr(ctl, "start_listening", waiting_start)
    async with TestClient(TestServer(app)) as client:
        async with ctl._listening_lock:
            press = asyncio.create_task(
                client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": gesture, "revision": 1}})
            )
            await asyncio.wait_for(entered.wait(), 2)
            release = await client.post(
                "/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": gesture, "revision": 2}}
            )
            assert release.status == 202
        response = await asyncio.wait_for(press, 2)
        assert (await response.json())["pushToTalkIgnored"] is True
        assert not continuation.pipelines


@pytest.mark.asyncio
async def test_push_to_talk_queued_press_keeps_ownership_through_capture_release(continuation):
    ctl = continuation.controller
    old, _ = await continuation.start("old")
    old.native_gate.clear()
    old_stop = continuation.stop()
    await asyncio.wait_for(old.stop_started.wait(), 2)
    app = web.Application()
    register_live_mic_routes(app, control=ctl, replay=MagicMock(pending_activation=False))
    gesture = str(uuid4())
    successor_gesture = str(uuid4())
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": gesture, "revision": 1}})
        assert response.status == 202
        response = await client.post(
            "/api/live-mic/start", json={"pushToTalk": {"gestureId": successor_gesture, "revision": 3}}
        )
        assert response.status == 202
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": gesture, "revision": 2}})
        continuation.pipeline_created.clear()
        old.native_gate.set()
        await asyncio.wait_for(continuation.pipeline_created.wait(), 2)
        new = continuation.pipelines[-1]
        assert new is not old
        new.provider_gate.set()
        await asyncio.wait_for(new.ready.wait(), 2)
        await client.post(
            "/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": successor_gesture, "revision": 4}}
        )
        await asyncio.wait_for(new.stop_started.wait(), 2)
        old.provider_gate.set()
        await old_stop


@pytest.mark.asyncio
async def test_push_to_talk_new_press_survives_cancelled_start_cleanup(continuation, monkeypatch):
    ctl = continuation.controller
    entered = asyncio.Event()
    release = asyncio.Event()
    continuation.cleanup_gates.append(release)
    original = web_api._live_mic_audio_conflict

    async def conflict(controller):
        entered.set()
        await release.wait()
        return await original(controller)

    monkeypatch.setattr(web_api, "_live_mic_audio_conflict", conflict)
    app = web.Application()
    register_live_mic_routes(app, control=ctl, replay=MagicMock(pending_activation=False))
    first, second = str(uuid4()), str(uuid4())
    accepted_b = asyncio.Event()
    original_accept = ctl._accept_push_to_talk_start

    def accept(marker):
        result = original_accept(marker)
        if marker.gesture_id == second:
            accepted_b.set()
        return result

    monkeypatch.setattr(ctl, "_accept_push_to_talk_start", accept)
    async with TestClient(TestServer(app)) as client:
        start_a = asyncio.create_task(
            client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": first, "revision": 1}})
        )
        await asyncio.wait_for(entered.wait(), 2)
        await client.post("/api/live-mic/stop-request", json={"pushToTalk": {"gestureId": first, "revision": 2}})
        start_b = asyncio.create_task(
            client.post("/api/live-mic/start", json={"pushToTalk": {"gestureId": second, "revision": 3}})
        )
        # The canceled prior start still holds admission; the new gesture must queue/wait, not be rejected as UI-owned.
        await asyncio.wait_for(accepted_b.wait(), 2)
        release.set()
        await asyncio.wait_for(start_a, 2)
        result = await asyncio.wait_for(start_b, 2)
        assert (await result.json()).get("pushToTalkIgnored") is not True
        await asyncio.wait_for(continuation.pipeline_created.wait(), 2)
