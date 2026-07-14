from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import re
import socket
import threading
from types import SimpleNamespace
import wave

from aiohttp.test_utils import TestClient, TestServer
import pytest

from src import database, web_api
from src.data.meeting_store import MeetingCreate, MeetingStore
from src.data.meeting_import_store import MeetingImportStatus, MeetingImportStore
from src.data.transcript_artifact_store import SourceAssetState, TranscriptArtifactStore


def test_resume_connects_durable_readers_before_starting_live_stt():
    text = Path(web_api.__file__).read_text(encoding="utf-8")
    start = text.index("    async def _resume_interrupted_meeting_claimed")
    interrupted = text[start:text.index("    async def resume_meeting", start)]
    assert interrupted.index("recorder.start(sources)") < interrupted.index(
        "await _start_meeting_live_preview_best_effort"
    )


def test_initial_start_connects_durable_readers_before_starting_live_stt():
    text = Path(web_api.__file__).read_text(encoding="utf-8")
    start = text.index("    async def start_meeting(request")
    initial = text[start:text.index("    def _meeting_native_stop_snapshot", start)]
    assert initial.index("recorder.start(native_sources)") < initial.index(
        "await _start_meeting_live_preview_best_effort(ctl, meeting)"
    )


def test_default_device_reconnect_starts_durable_reader_before_live_preview():
    text = Path(web_api.__file__).read_text(encoding="utf-8")
    start = text.index("    async def _reconnect_meeting_after_device_change")
    reconnect = text[start:text.index("    def _emit_workflow_event", start)]
    assert reconnect.index("recorder.start(sources)") < reconnect.index(
        "await _start_meeting_live_preview_best_effort"
    )


def test_background_outlook_sync_uses_the_shared_bounded_http_timeout():
    text = Path(web_api.__file__).read_text(encoding="utf-8")
    start = text.index("    async def _meeting_maintenance_loop")
    maintenance = text[start:text.index(
        "    async def _resume_pending_meeting_pcm_purges", start
    )]
    assert "ClientSession(timeout=_OUTBOUND_HTTP_TIMEOUT)" in maintenance
    assert web_api._OUTBOUND_HTTP_TIMEOUT.total == 15


class FakeRecorder:
    def __init__(self, *_args, **_kwargs):
        self.sources = []
        self.expected_disconnect = False

    def start(self, sources):
        self.sources = sources
        self.expected_disconnect = False

    def prepare_for_expected_disconnect(self):
        self.expected_disconnect = True

    def cancel_expected_disconnect(self):
        self.expected_disconnect = False

    def stop(self, **_kwargs):
        self.expected_disconnect = False
        return {"microphone": {"chunks": 1}, "system": {"chunks": 1}}


class FakeLiveTranscriber:
    def enqueue_from_thread(self, _source, _pcm):
        pass

    async def stop(self):
        pass

    def snapshot(self):
        return {
            "streams": {},
            "droppedFrames": 0,
            "reconnectCount": 0,
            "reconnectAttempts": 0,
            "interimLatencySampleCount": 2,
            "interimLatencyP95Ms": 750,
        }


class AdmissionPipeline:
    service_name = "openai"

    def __init__(self):
        self.stop_gate = asyncio.Event()

    async def start(self):
        await self.stop_gate.wait()

    async def stop(self, **_kwargs):
        self.stop_gate.set()


class InactivePrewarm:
    is_active = False


class FakeDeviceProbe:
    def start(self, sources):
        self.sources = sources

    def stop(self):
        return {
            "microphone": {"frames": 10, "audioFrames": 1_600, "rms": 0.2, "peak": 0.5, "active": True, "errorCode": ""},
            "system": {"frames": 10, "audioFrames": 1_600, "rms": 0.3, "peak": 0.6, "active": True, "errorCode": ""},
            "mic_clean": {"frames": 10, "audioFrames": 1_600, "rms": 0.1, "peak": 0.4, "active": True, "errorCode": ""},
        }


class FakeWebhookResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeWebhookSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeWebhookResponse(self.statuses.pop(0))

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_meeting_capture_watchdog_stops_visibly_on_disk_full(monkeypatch):
    transitions = []
    events = []
    shell_calls = []

    class Store:
        def get(self, _meeting_id):
            return {"id": "meeting-disk", "state": "recording"}

        def transition(self, meeting_id, state, **kwargs):
            transitions.append((meeting_id, state, kwargs))
            return {"id": meeting_id, "state": state, **kwargs}

    class Recorder:
        def snapshot(self):
            return {"system": {"errorCode": "disk_full"}}

        def stop(self):
            return self.snapshot()

    async def no_wait(_seconds):
        return None

    controller = object.__new__(web_api.ScriberWebController)
    controller._shutting_down = False
    controller._meeting_store = Store()
    controller._meeting_recorders = {"meeting-disk": Recorder()}
    controller._meeting_live_transcribers = {"meeting-disk": FakeLiveTranscriber()}
    controller.broadcast = lambda payload: _append_event(events, payload)
    monkeypatch.setattr(web_api.asyncio, "sleep", no_wait)

    def shell_call(command, payload, **_kwargs):
        shell_calls.append((command, payload))
        return {
            "success": True,
            "payload": {
                "stopped": True,
                "sidecar": {
                    "framesProcessed": 250,
                    "bytesForwarded": 240_000,
                    "sidecarUptimeMs": 2_500,
                    "relayError": None,
                    "aecMetrics": {
                        "measurement": "render-active-raw-to-clean-energy-ratio",
                        "renderActiveFrames": 200,
                        "renderActiveDurationMs": 2_000,
                        "renderEnergy": 50_000.0,
                        "rawMicEnergy": 10_000.0,
                        "cleanMicEnergy": 1_000.0,
                        "echoReductionDb": 10.0,
                    },
                },
            },
        }

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    await web_api.ScriberWebController._meeting_capture_watchdog(
        controller, "meeting-disk", "capture-disk"
    )

    assert transitions[0][1] == "capture_failed"
    assert transitions[0][2]["error_code"] == "meeting_storage_full"
    assert "drive is full" in transitions[0][2]["error_message"]
    assert events[-1]["type"] == "meeting_state"
    assert shell_calls[-1][0] == "audioMeetingStop"


@pytest.mark.asyncio
async def test_meeting_capture_watchdog_fails_closed_after_audio_lease_loss(
    monkeypatch
):
    transitions = []
    shell_calls = []

    class Store:
        def get(self, meeting_id):
            return {"id": meeting_id, "state": "recording"}

        def transition(self, meeting_id, state, **kwargs):
            transitions.append((meeting_id, state, kwargs))
            return {"id": meeting_id, "state": state, **kwargs}

    class Recorder:
        def snapshot(self):
            return {"system": {"errorCode": ""}}

        def stop(self):
            return self.snapshot()

    async def no_wait(_seconds):
        return None

    controller = object.__new__(web_api.ScriberWebController)
    controller._shutting_down = False
    controller._meeting_store = Store()
    controller._meeting_recorders = {"meeting-lease": Recorder()}
    controller._meeting_live_transcribers = {
        "meeting-lease": FakeLiveTranscriber()
    }
    controller._audio_admission_lost_meetings = {"meeting-lease"}
    controller.broadcast = lambda payload: _append_event([], payload)
    monkeypatch.setattr(web_api.asyncio, "sleep", no_wait)

    def shell_call(command, payload, **_kwargs):
        shell_calls.append((command, payload))
        assert command == "audioMeetingStop"
        return {"success": True, "payload": {"stopped": True}}

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)

    await web_api.ScriberWebController._meeting_capture_watchdog(
        controller, "meeting-lease", "capture-lease"
    )

    assert transitions == [(
        "meeting-lease",
        "capture_failed",
        {
            "error_code": "audio_admission_lost",
            "error_message": (
                "Native audio ownership moved to another Scriber controller. "
                "Recording stopped and completed chunks were preserved."
            ),
        },
    )]
    assert shell_calls[0][0] == "audioMeetingStop"
    assert controller._audio_admission_lost_meetings == set()


async def _append_event(items, value):
    items.append(value)


class FakeController:
    def __init__(self, store):
        self._meeting_store = store
        self._meeting_recorders = {}
        self._meeting_live_transcribers = {}
        self._is_listening = False
        self._is_stopping = False
        self._meeting_device_test_active = False
        self.events = []
        self.scheduled = []
        self.analysis_scheduled = []
        self.capture_watchdogs = {}
        self.prewarm_paused = False

    async def broadcast(self, payload):
        self.events.append(payload)

    def schedule_meeting_finalization(self, meeting_id, **_kwargs):
        self.scheduled.append(meeting_id)
        return True

    def schedule_meeting_analysis(self, meeting_id, **_kwargs):
        self.analysis_scheduled.append(meeting_id)
        return True

    def start_meeting_capture_watchdog(self, meeting_id, capture_id):
        self.capture_watchdogs[meeting_id] = capture_id

    def stop_meeting_capture_watchdog(self, meeting_id):
        self.capture_watchdogs.pop(meeting_id, None)

    async def _pause_idle_mic_prewarm_for_capture(self):
        self.prewarm_paused = True

    def _resume_idle_mic_prewarm_after_capture(self):
        self.prewarm_paused = False

    async def start_meeting_live_transcription(self, meeting, **_kwargs):
        live = FakeLiveTranscriber()
        self._meeting_live_transcribers[meeting["id"]] = live
        return live


class _DirectRequest:
    def __init__(self, app, *, payload=None, meeting_id=""):
        self.app = app
        self.match_info = {"id": meeting_id} if meeting_id else {}
        self._payload = payload or {}
        self.query = {}

    async def json(self):
        return self._payload


def _route_handler(app, method, canonical):
    for route in app.router.routes():
        if route.method == method and route.resource.canonical == canonical:
            return route.handler
    raise AssertionError(f"Route not found: {method} {canonical}")


@pytest.mark.asyncio
async def test_speaker_attendee_confirmation_resolves_opaque_id_from_frozen_snapshot():
    captured = {}

    class Store:
        @staticmethod
        def detail(meeting_id):
            assert meeting_id == "meeting-participants"
            return {
                "captureMetadata": {
                    "calendarEvent": {
                        "organizer": None,
                        "participants": [{
                            "participantId": "opaque-participant",
                            "name": "Márta Example",
                            "address": "marta@example.com",
                            "type": "required",
                            "response": "accepted",
                        }],
                        "currentUser": None,
                    }
                },
                "speakers": [],
                "segments": [],
            }

        @staticmethod
        def assign_speaker_participant(
            meeting_id, speaker_id, participant, *, source
        ):
            captured.update(
                meeting_id=meeting_id,
                speaker_id=speaker_id,
                participant=participant,
                source=source,
            )
            return {
                "speakerId": speaker_id,
                "confirmedAttendee": {
                    "name": participant["name"],
                    "address": participant["address"],
                },
            }

    controller = SimpleNamespace(_meeting_store=Store())
    app = web_api.create_app(controller)
    handler = _route_handler(
        app, "PATCH", "/api/meetings/{id}/speakers/{speakerId}/attendee"
    )
    request = _DirectRequest(
        app,
        meeting_id="meeting-participants",
        payload={
            "participantId": "opaque-participant",
            "confirmed": True,
            "suggestionSource": "llm",
            # These untrusted values are ignored; the cache snapshot wins.
            "name": "Attacker",
            "address": "attacker@example.net",
        },
    )
    request.match_info["speakerId"] = "speaker-1"
    response = await handler(request)
    payload = json.loads(response.body)

    assert response.status == 200
    assert captured == {
        "meeting_id": "meeting-participants",
        "speaker_id": "speaker-1",
        "participant": {
            "participantId": "opaque-participant",
            "name": "Márta Example",
            "address": "marta@example.com",
            "type": "required",
            "response": "accepted",
            "isCurrentUser": False,
        },
        "source": "llm",
    }
    assert payload["assignment"]["confirmedAttendee"]["participantId"] == "opaque-participant"


@pytest.mark.asyncio
async def test_speaker_attendee_confirmation_rejects_unknown_opaque_id():
    class Store:
        @staticmethod
        def detail(_meeting_id):
            return {
                "captureMetadata": {"calendarEvent": {"participants": []}},
                "speakers": [],
                "segments": [],
            }

    app = web_api.create_app(SimpleNamespace(_meeting_store=Store()))
    handler = _route_handler(
        app, "PATCH", "/api/meetings/{id}/speakers/{speakerId}/attendee"
    )
    request = _DirectRequest(
        app,
        meeting_id="meeting-participants",
        payload={"participantId": "unknown", "confirmed": True},
    )
    request.match_info["speakerId"] = "speaker-1"
    response = await handler(request)
    assert response.status == 409


@pytest.mark.asyncio
async def test_legacy_calendar_snapshot_gets_opaque_id_that_patch_can_confirm():
    captured = {}
    legacy_event = {
        "id": "legacy-graph-event",
        "organizer": {
            "name": "Owner",
            "address": "owner@example.com",
        },
        "participants": [{
            "name": "Legacy Participant",
            "address": "legacy@example.com",
            "type": "required",
            "response": "accepted",
        }],
    }

    class Store:
        @staticmethod
        def detail(_meeting_id):
            return {
                "captureMetadata": {"calendarEvent": legacy_event},
                "speakers": [{
                    "id": "speaker-legacy",
                    "label": "Speaker 1",
                    "displayName": "Speaker 1",
                    "sourceHint": "system",
                }],
                "segments": [],
                "analysisModel": "gpt-5-mini",
            }

        @staticmethod
        def speaker_profiles():
            return []

        @staticmethod
        def assign_speaker_participant(
            meeting_id, speaker_id, participant, *, source
        ):
            captured.update(
                meeting_id=meeting_id,
                speaker_id=speaker_id,
                participant=participant,
                source=source,
            )
            return {"speakerId": speaker_id}

    app = web_api.create_app(SimpleNamespace(_meeting_store=Store()))
    get_handler = _route_handler(
        app, "GET", "/api/meetings/{id}/speaker-assignments"
    )
    get_response = await get_handler(
        _DirectRequest(app, meeting_id="meeting-legacy")
    )
    get_payload = json.loads(get_response.body)
    participant_id = get_payload["calendarEvent"]["participants"][0][
        "participantId"
    ]

    assert get_response.status == 200
    assert re.fullmatch(r"[0-9a-f]{20}", participant_id)
    assert participant_id != "legacy@example.com"

    patch_handler = _route_handler(
        app, "PATCH", "/api/meetings/{id}/speakers/{speakerId}/attendee"
    )
    patch_request = _DirectRequest(
        app,
        meeting_id="meeting-legacy",
        payload={"participantId": participant_id, "confirmed": True},
    )
    patch_request.match_info["speakerId"] = "speaker-legacy"
    patch_response = await patch_handler(patch_request)

    assert patch_response.status == 200
    assert captured["participant"]["participantId"] == participant_id
    assert captured["participant"]["address"] == "legacy@example.com"
    assert captured["source"] == "manual"


@pytest.mark.asyncio
async def test_meeting_audio_devices_falls_back_to_three_redacted_pycaw_captures(
    monkeypatch,
):
    app = web_api.create_app(SimpleNamespace())
    handler = _route_handler(app, "GET", "/api/meetings/audio-devices")
    raw_ids = [f"private-native-endpoint-{index}" for index in range(3)]
    fallback = [
        {
            "endpointId": raw_id,
            "endpointIdHash": hashlib.sha256(raw_id.encode()).hexdigest()[:16],
            "friendlyName": label,
            "flow": "capture",
            "isDefault": index == 1,
        }
        for index, (raw_id, label) in enumerate(
            zip(raw_ids, ("Jabra Engage 75", "Insta360 Link", "Realtek Array"))
        )
    ]

    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(
        web_api,
        "call_shell_ipc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private pipe path")),
    )
    monkeypatch.setattr(
        web_api,
        "collect_native_capture_endpoint_inventory",
        lambda: fallback,
    )

    response = await handler(_DirectRequest(app))
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload["available"] is True
    assert payload["source"] == "pycaw-fallback"
    assert payload["partial"] is True
    assert payload["reason"] == "shellIpcRequestFailed"
    assert payload["render"] == []
    assert [entry["friendlyName"] for entry in payload["capture"]] == [
        "Jabra Engage 75",
        "Insta360 Link",
        "Realtek Array",
    ]
    serialized = json.dumps(payload)
    assert all(raw_id not in serialized for raw_id in raw_ids)
    assert "private pipe path" not in serialized


@pytest.mark.asyncio
async def test_meeting_audio_devices_does_not_offer_native_selection_without_shell_ipc(
    monkeypatch,
):
    app = web_api.create_app(SimpleNamespace())
    handler = _route_handler(app, "GET", "/api/meetings/audio-devices")
    fallback_called = False

    def fallback_inventory():
        nonlocal fallback_called
        fallback_called = True
        return []

    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: False)
    monkeypatch.setattr(
        web_api,
        "collect_native_capture_endpoint_inventory",
        fallback_inventory,
    )

    response = await handler(_DirectRequest(app))
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload == {
        "apiVersion": web_api.REST_API_VERSION,
        "available": False,
        "capture": [],
        "render": [],
        "source": "unavailable",
        "partial": True,
        "reason": "shellIpcUnavailable",
    }
    assert fallback_called is False


@pytest.mark.asyncio
async def test_meeting_audio_devices_fills_empty_rust_capture_and_keeps_render(
    monkeypatch,
):
    app = web_api.create_app(SimpleNamespace())
    handler = _route_handler(app, "GET", "/api/meetings/audio-devices")
    microphone_hash = "a" * 16
    render_hash = "b" * 16

    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(
        web_api,
        "call_shell_ipc",
        lambda *_args, **_kwargs: {
            "success": True,
            "payload": {
                "available": True,
                "endpoints": [
                    {
                        "endpointIdHash": render_hash,
                        "friendlyName": "Desk Speakers",
                        "flow": "render",
                        "isDefault": True,
                        "defaultRoles": ["console"],
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(
        web_api,
        "collect_native_capture_endpoint_inventory",
        lambda: [
            {
                "endpointIdHash": microphone_hash,
                "friendlyName": "Fallback microphone",
                "flow": "capture",
                "isDefault": True,
            }
        ],
    )

    response = await handler(_DirectRequest(app))
    payload = json.loads(response.body)

    assert payload["available"] is True
    assert payload["source"] == "rust-wasapi+pycaw-fallback"
    assert payload["partial"] is True
    assert payload["reason"] == "captureInventoryEmpty"
    assert [entry["endpointIdHash"] for entry in payload["capture"]] == [
        microphone_hash
    ]
    assert [entry["endpointIdHash"] for entry in payload["render"]] == [render_hash]


@pytest.mark.asyncio
async def test_meeting_detail_exposes_the_immutable_final_route_snapshot():
    class Store:
        @staticmethod
        def detail(meeting_id, *, revision):
            assert meeting_id == "meeting-route"
            assert revision == "canonical"
            return {"id": meeting_id}

    class Artifacts:
        @staticmethod
        def get_head(_meeting_id):
            return SimpleNamespace(artifact_id="artifact")

        @staticmethod
        def get_artifact(_artifact_id):
            return SimpleNamespace(attempt_id="attempt")

        @staticmethod
        def get_route_snapshot(_attempt_id):
            return SimpleNamespace(
                provider="soniox_async",
                model="stt-async-v5",
                transport="webm_opus_task_derivative",
                language="de",
                timestamp_mode="word_or_segment",
                diarization_mode="native_if_evidenced_else_local",
            )

    controller = SimpleNamespace(
        _meeting_store=Store(),
        _transcript_artifacts=Artifacts(),
    )
    app = web_api.create_app(controller)
    handler = _route_handler(app, "GET", "/api/meetings/{id}")
    response = await handler(_DirectRequest(app, meeting_id="meeting-route"))
    payload = json.loads(response.body)

    assert payload["finalRoute"] == {
        "provider": "soniox_async",
        "model": "stt-async-v5",
        "transport": "webm_opus_task_derivative",
        "language": "de",
        "timestampMode": "word_or_segment",
        "diarizationMode": "native_if_evidenced_else_local",
    }


@pytest.mark.asyncio
async def test_meeting_detail_survives_unreadable_final_route_metadata():
    class Store:
        @staticmethod
        def detail(meeting_id, *, revision):
            return {"id": meeting_id, "revision": revision}

    class Artifacts:
        @staticmethod
        def get_head(_meeting_id):
            raise RuntimeError("corrupt artifact metadata")

    controller = SimpleNamespace(
        _meeting_store=Store(),
        _transcript_artifacts=Artifacts(),
    )
    app = web_api.create_app(controller)
    handler = _route_handler(app, "GET", "/api/meetings/{id}")
    response = await handler(_DirectRequest(app, meeting_id="meeting-corrupt-route"))
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload["id"] == "meeting-corrupt-route"
    assert payload["finalRoute"] is None


@pytest.mark.asyncio
async def test_meeting_capabilities_reports_verified_five_hour_storage(monkeypatch):
    class Store:
        def active(self):
            return None

    class Controller:
        _meeting_store = Store()
        _is_listening = False
        _is_stopping = False

    class DiskUsage:
        free = 7 * 1024 * 1024 * 1024

    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api.shutil, "disk_usage", lambda _path: DiskUsage())
    app = web_api.create_app(Controller())
    handler = _route_handler(app, "GET", "/api/meetings/capabilities")

    response = await handler(_DirectRequest(app))
    payload = json.loads(response.body)

    assert payload["nativeMeetingCapture"] is True
    assert payload["longSession"] == {
        "targetDurationSeconds": 18_000,
        "checkpointIntervalSeconds": 30,
        "requiredFreeBytes": 6 * 1024 * 1024 * 1024,
        "availableFreeBytes": 7 * 1024 * 1024 * 1024,
        "estimatedCaptureSeconds": (5 * 1024 * 1024 * 1024) // (16_000 * 2 * 3),
        "storageReady": True,
    }


@pytest.mark.asyncio
async def test_meeting_profile_treats_missing_live_key_as_non_blocking_preview_warning(
    monkeypatch
):
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    monkeypatch.setattr(web_api.Config, "MEETING_FINAL_PROVIDER", "onnx_local")
    monkeypatch.setattr(web_api.Config, "get_api_key", lambda _provider: "")

    app = web_api.create_app(object())
    handler = _route_handler(app, "GET", "/api/meeting-profiles")
    response = await handler(_DirectRequest(app))
    profile = json.loads(response.body)["profiles"][0]

    assert response.status == 200
    assert profile["finalProvider"] == "onnx_local"
    assert profile["available"] is True
    assert profile["unavailableReason"] == ""
    assert profile["livePreviewAvailable"] is False
    assert "Durable local recording" in profile["livePreviewWarning"]
    assert profile["name"] == "Live text + Local ONNX STT final"
    assert "Live captions are unavailable" in profile["description"]


@pytest.mark.asyncio
async def test_final_only_profile_disables_live_preview_and_reports_two_track_cost(monkeypatch):
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "final_only")
    monkeypatch.setattr(web_api.Config, "MEETING_FINAL_PROVIDER", "soniox_async")
    monkeypatch.setattr(web_api.Config, "get_api_key", lambda _provider: "configured")

    app = web_api.create_app(object())
    handler = _route_handler(app, "GET", "/api/meeting-profiles")
    response = await handler(_DirectRequest(app))
    profile = json.loads(response.body)["profiles"][0]

    assert response.status == 200
    assert profile["transcriptionMode"] == "final_only"
    assert profile["livePreviewAvailable"] is False
    assert profile["livePreviewWarning"] == ""
    assert profile["stages"][0]["provider"] == "Off"
    assert profile["costEstimate"]["audioTrackAssumption"] == 2
    assert profile["costEstimate"]["livePerMeetingHour"] == 0.0
    assert profile["costEstimate"]["finalPerMeetingHour"] == 0.2
    assert profile["costEstimate"]["singleTrackFinalPerAudioHour"] == 0.1
    assert profile["costEstimate"]["totalPerMeetingHour"] == 0.2


@pytest.mark.asyncio
async def test_live_and_final_profile_reports_both_soniox_passes(monkeypatch):
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    monkeypatch.setattr(web_api.Config, "MEETING_FINAL_PROVIDER", "soniox_async")
    monkeypatch.setattr(web_api.Config, "get_api_key", lambda _provider: "configured")

    app = web_api.create_app(object())
    handler = _route_handler(app, "GET", "/api/meeting-profiles")
    response = await handler(_DirectRequest(app))
    profile = json.loads(response.body)["profiles"][0]

    assert profile["transcriptionMode"] == "live_final"
    assert profile["costEstimate"]["livePerMeetingHour"] == 0.24
    assert profile["costEstimate"]["finalPerMeetingHour"] == 0.2
    assert profile["costEstimate"]["totalPerMeetingHour"] == 0.44


@pytest.mark.asyncio
async def test_final_only_mode_never_starts_a_live_preview_provider():
    class Controller:
        async def start_meeting_live_transcription(self, *_args, **_kwargs):
            raise AssertionError("final-only meetings must not open a live provider")

    live, degraded = await web_api._start_meeting_live_preview_best_effort(
        Controller(),
        {"id": "meeting-final-only", "transcriptionMode": "final_only"},
    )

    assert live is None
    assert degraded is False
    assert web_api._meeting_live_preview_metadata(
        {"transcriptionMode": "final_only"},
        degraded=False,
        error_code="live_stt_start_failed",
    ) == {
        "status": "disabled",
        "provider": "",
        "model": "",
        "errorCode": "",
    }


class TrackingRecorder:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.started = False
        self.start_count = 0
        self.stop_count = 0
        self.expected_disconnect = False
        self.on_pcm = _kwargs.get("on_pcm")
        type(self).instances.append(self)

    def start(self, _sources):
        self.started = True
        self.start_count += 1
        self.expected_disconnect = False

    def prepare_for_expected_disconnect(self):
        self.expected_disconnect = True

    def cancel_expected_disconnect(self):
        self.expected_disconnect = False

    def stop(self, **_kwargs):
        self.started = False
        self.stop_count += 1
        self.expected_disconnect = False
        return {"microphone": {"chunks": 1, "errorCode": ""}}


class TrackingLiveTranscriber(FakeLiveTranscriber):
    def __init__(self):
        self.stop_count = 0

    async def stop(self):
        self.stop_count += 1


def _capture_cancellation_controller(monkeypatch, tmp_path, db_name):
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / db_name)
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller.on_meeting_pcm = lambda *_args, **_kwargs: None
    TrackingRecorder.instances = []
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", TrackingRecorder)
    lives = []

    async def start_live(meeting, **_kwargs):
        live = TrackingLiveTranscriber()
        lives.append(live)
        controller._meeting_live_transcribers[meeting["id"]] = live
        return live

    controller.start_meeting_live_transcription = start_live
    return controller, store, lives


def _calendar_start_controller(monkeypatch, tmp_path, db_name, calendar):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / db_name)
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller._outlook_calendar = calendar
    controller.on_meeting_pcm = lambda *_args, **_kwargs: None
    TrackingRecorder.instances = []
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", TrackingRecorder)

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingStart":
            return {
                "success": True,
                "payload": {
                    "captureId": f"capture-{db_name}",
                    "sampleRate": 16_000,
                    "frameDurationMs": 10,
                    "aecActive": True,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    return controller, store


def _audio_race_controller(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "audio-admission.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", False)
    database.init_database()

    controller = web_api.ScriberWebController(asyncio.get_running_loop())
    controller._mic_prewarm = InactivePrewarm()
    controller._select_available_provider = lambda: "openai"
    controller._validate_live_provider_ready = lambda _provider: None
    controller._show_initializing_overlay_async = lambda **_kwargs: None
    controller._hide_recording_overlay_async = lambda **_kwargs: None
    controller._start_mic_watchdog = lambda: None
    controller.start_meeting_capture_watchdog = lambda *_args, **_kwargs: None
    controller._resume_idle_mic_prewarm_after_capture = lambda: None

    async def broadcast(_payload):
        return None

    async def start_live(meeting, **_kwargs):
        live = FakeLiveTranscriber()
        controller._meeting_live_transcribers[meeting["id"]] = live
        return live

    controller.broadcast = broadcast
    controller.start_meeting_live_transcription = start_live
    pipelines: list[AdmissionPipeline] = []

    def make_pipeline(**_kwargs):
        pipeline = AdmissionPipeline()
        pipelines.append(pipeline)
        return pipeline

    monkeypatch.setattr(web_api, "_create_scriber_pipeline", make_pipeline)
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", FakeRecorder)
    return controller, pipelines


@pytest.mark.asyncio
async def test_meeting_start_keeps_durable_capture_when_live_preview_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "degraded-live-preview.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller.on_meeting_pcm = lambda *_args, **_kwargs: None
    lifecycle: list[str] = []
    native_stops: list[str] = []

    class OrderedRecorder(TrackingRecorder):
        def start(self, sources):
            lifecycle.append("recorder")
            super().start(sources)

    TrackingRecorder.instances = []
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", OrderedRecorder)

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingStart":
            lifecycle.append("native")
            return {
                "success": True,
                "payload": {
                    "captureId": "capture-degraded-preview",
                    "sampleRate": 16_000,
                    "frameDurationMs": 10,
                    "aecActive": True,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            native_stops.append(command)
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    async def fail_live_preview(meeting, **_kwargs):
        lifecycle.append("live")
        assert controller._meeting_recorders[meeting["id"]].started is True
        raise RuntimeError("provider connection failed")

    controller.start_meeting_live_transcription = fail_live_preview
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings")

    response = await handler(
        _DirectRequest(app, payload={"title": "Durable without preview"})
    )
    payload = json.loads(response.body)

    assert response.status == 201
    assert lifecycle == ["native", "recorder", "live"]
    assert payload["state"] == "recording"
    assert payload["errorCode"] == "live_stt_start_failed"
    assert payload["errorMessage"] == (
        "Live transcription is unavailable. Durable local audio recording continues."
    )
    assert payload["captureMetadata"]["livePreview"] == {
        "status": "degraded",
        "provider": "soniox",
        "model": web_api.Config.SONIOX_RT_MODEL,
        "errorCode": "live_stt_start_failed",
    }
    assert controller._meeting_recorders[payload["id"]].started is True
    assert controller.capture_watchdogs[payload["id"]] == "capture-degraded-preview"
    assert native_stops == []
    assert [event["type"] for event in controller.events[-3:]] == [
        "meeting_state",
        "meeting_live_status",
        "meeting_live_status",
    ]
    assert {
        (event["source"], event["status"])
        for event in controller.events[-2:]
    } == {("microphone", "degraded"), ("system", "degraded")}
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_start_freezes_only_the_locally_resolved_calendar_event(
    monkeypatch, tmp_path
):
    frozen_event = {
        "id": "graph-event-1",
        "subject": "Customer planning",
        "start_at": "2026-07-14T08:00:00+00:00",
        "end_at": "2026-07-14T09:00:00+00:00",
        "organizer": {
            "participantId": "organizer-id",
            "name": "Olivia Owner",
            "address": "olivia@example.com",
            "isCurrentUser": False,
        },
        "participants": [{
            "participantId": "participant-id",
            "name": "Pat Participant",
            "address": "pat@example.com",
            "type": "required",
            "response": "accepted",
            "isCurrentUser": False,
        }],
        "currentUser": {
            "participantId": "self-id",
            "name": "Alex Example",
            "address": "alex@example.com",
            "isCurrentUser": True,
        },
        "calendarSyncedAt": "2026-07-14T07:55:00+00:00",
        "snapshotCreatedAt": "2026-07-14T07:59:00+00:00",
    }

    class Calendar:
        snapshot_calls: list[str] = []

        def event_snapshot(self, event_id):
            self.snapshot_calls.append(event_id)
            return json.loads(json.dumps(frozen_event))

        def current_event(self):
            raise AssertionError("explicit selection must not use current_event")

    calendar = Calendar()
    controller, _store = _calendar_start_controller(
        monkeypatch, tmp_path, "selected-calendar.db", calendar
    )
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings")
    response = await handler(_DirectRequest(app, payload={
        "calendarEventId": "graph-event-1",
        "transcriptionMode": "final_only",
        # Untrusted WebView context must be ignored entirely.
        "calendarEvent": {
            "subject": "Spoofed title",
            "participants": [{"address": "attacker@example.net"}],
        },
        "participants": [{"address": "attacker@example.net"}],
    }))
    payload = json.loads(response.body)

    assert response.status == 201
    assert calendar.snapshot_calls == ["graph-event-1"]
    assert payload["title"] == "Customer planning"
    assert payload["captureMetadata"]["calendarEventSelection"] == "explicit"
    assert payload["captureMetadata"]["calendarEvent"] == frozen_event
    assert "attacker@example.net" not in json.dumps(payload)
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_start_with_explicit_null_calendar_event_skips_legacy_lookup(
    monkeypatch, tmp_path
):
    class Calendar:
        def event_snapshot(self, _event_id):
            raise AssertionError("null selection must not resolve a snapshot")

        def current_event(self):
            raise AssertionError("null selection must not use current_event")

    controller, _store = _calendar_start_controller(
        monkeypatch, tmp_path, "no-calendar.db", Calendar()
    )
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings")
    response = await handler(_DirectRequest(app, payload={
        "title": "Unscheduled conversation",
        "calendarEventId": None,
        "transcriptionMode": "final_only",
    }))
    payload = json.loads(response.body)

    assert response.status == 201
    assert payload["captureMetadata"]["calendarEventSelection"] == "none"
    assert "calendarEvent" not in payload["captureMetadata"]
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_start_rejects_stale_selected_calendar_event_before_capture():
    class Calendar:
        def __init__(self):
            self.calls = []

        def event_snapshot(self, event_id):
            self.calls.append(event_id)
            return None

        def current_event(self):
            raise AssertionError("explicit selection must not use current_event")

    calendar = Calendar()
    app = web_api.create_app(SimpleNamespace(_outlook_calendar=calendar))
    handler = _route_handler(app, "POST", "/api/meetings")
    response = await handler(_DirectRequest(app, payload={
        "title": "Stale calendar item",
        "calendarEventId": "deleted-event",
    }))
    payload = json.loads(response.body)

    assert response.status == 409
    assert calendar.calls == ["deleted-event"]
    assert "no longer available" in payload["message"]


@pytest.mark.asyncio
async def test_final_only_start_skips_live_provider_but_records_durably(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "final-only-start.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    monkeypatch.setattr(web_api.Config, "get_api_key", lambda _provider: "")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller.on_meeting_pcm = lambda *_args, **_kwargs: None
    TrackingRecorder.instances = []
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", TrackingRecorder)

    async def unexpected_live_provider(*_args, **_kwargs):
        raise AssertionError("final-only capture must not open Soniox Realtime")

    controller.start_meeting_live_transcription = unexpected_live_provider

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingStart":
            return {
                "success": True,
                "payload": {
                    "captureId": "capture-final-only",
                    "sampleRate": 16_000,
                    "frameDurationMs": 10,
                    "aecActive": True,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings")

    response = await handler(
        _DirectRequest(
            app,
            payload={
                "title": "Quiet durable capture",
                "transcriptionMode": "final_only",
            },
        )
    )
    payload = json.loads(response.body)

    assert response.status == 201
    assert payload["state"] == "recording"
    assert payload["transcriptionMode"] == "final_only"
    assert payload["captureMetadata"]["livePreview"] == {
        "status": "disabled",
        "provider": "",
        "model": "",
        "errorCode": "",
    }
    assert controller._meeting_recorders[payload["id"]].started is True
    assert all(event["type"] != "meeting_live_status" for event in controller.events)
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_pcm_without_live_preview_still_emits_local_audio_level():
    controller = object.__new__(web_api.ScriberWebController)
    controller._loop = asyncio.get_running_loop()
    controller._meeting_last_level_broadcast = {}
    events: list[dict] = []
    controller._enqueue_control_broadcast = events.append

    web_api.ScriberWebController.on_meeting_pcm(
        controller,
        "meeting-local-only",
        None,
        "mic_clean",
        (2_000).to_bytes(2, "little", signed=True) * 160,
    )
    await asyncio.sleep(0)

    assert len(events) == 1
    assert events[0]["type"] == "meeting_audio_level"
    assert events[0]["source"] == "microphone"
    assert events[0]["rms"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", ["paused", "interrupted"])
async def test_final_only_resume_stays_final_only_when_global_setting_changes(
    monkeypatch, tmp_path, resume_state
):
    controller, store, lives = _capture_cancellation_controller(
        monkeypatch, tmp_path, f"final-only-resume-{resume_state}.db"
    )
    meeting = store.create(
        MeetingCreate(
            title=f"Final-only {resume_state}",
            transcription_mode="final_only",
        )
    )
    store.transition(
        meeting["id"],
        "recording",
        capture_metadata={"captureId": "capture-before-resume", "deviceSelection": {}},
    )
    if resume_state == "paused":
        store.transition(
            meeting["id"],
            "paused",
            capture_metadata={
                "captureId": "capture-before-resume",
                "deviceSelection": {},
                "pauseStartedAtMs": 0,
                "pauseStartedAtUtc": web_api.datetime.now(
                    web_api.timezone.utc
                ).isoformat(),
            },
        )
    else:
        store.transition(meeting["id"], "interrupted")

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingResume":
            return {
                "success": True,
                "payload": {
                    "captureId": f"capture-final-only-{resume_state}",
                    "sampleRate": 16_000,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/{id}/resume")

    response = await handler(_DirectRequest(app, meeting_id=meeting["id"]))
    payload = json.loads(response.body)

    assert response.status == 200
    assert payload["state"] == "recording"
    assert payload["transcriptionMode"] == "final_only"
    assert payload["captureMetadata"]["livePreview"]["status"] == "disabled"
    assert payload["errorCode"] == ""
    assert lives == []
    assert all(event["type"] != "meeting_live_status" for event in controller.events)
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", ["paused", "interrupted"])
async def test_meeting_resume_keeps_durable_capture_when_live_preview_fails(
    monkeypatch, tmp_path, resume_state
):
    controller, store, _lives = _capture_cancellation_controller(
        monkeypatch, tmp_path, f"degraded-{resume_state}-preview.db"
    )
    meeting = store.create(MeetingCreate(title=f"Resume {resume_state}"))
    store.transition(
        meeting["id"],
        "recording",
        capture_metadata={"captureId": "capture-before-resume", "deviceSelection": {}},
    )
    if resume_state == "paused":
        store.transition(
            meeting["id"],
            "paused",
            capture_metadata={
                "captureId": "capture-before-resume",
                "deviceSelection": {},
                "pauseStartedAtMs": 0,
                "pauseStartedAtUtc": web_api.datetime.now(
                    web_api.timezone.utc
                ).isoformat(),
            },
        )
    else:
        store.transition(meeting["id"], "interrupted")

    lifecycle: list[str] = []
    native_stops: list[str] = []

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingResume":
            lifecycle.append("native")
            return {
                "success": True,
                "payload": {
                    "captureId": f"capture-{resume_state}-degraded",
                    "sampleRate": 16_000,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            native_stops.append(command)
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    async def fail_live_preview(_meeting, **_kwargs):
        lifecycle.append("live")
        assert controller._meeting_recorders[meeting["id"]].started is True
        raise RuntimeError("provider unavailable")

    original_start = TrackingRecorder.start

    def track_start(self, sources):
        lifecycle.append("recorder")
        return original_start(self, sources)

    monkeypatch.setattr(TrackingRecorder, "start", track_start)
    controller.start_meeting_live_transcription = fail_live_preview
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/{id}/resume")

    response = await handler(_DirectRequest(app, meeting_id=meeting["id"]))
    payload = json.loads(response.body)

    assert response.status == 200
    assert lifecycle == ["native", "recorder", "live"]
    assert payload["state"] == "recording"
    assert payload["errorCode"] == "live_stt_resume_failed"
    assert payload["captureMetadata"]["livePreview"]["status"] == "degraded"
    assert controller._meeting_recorders[meeting["id"]].started is True
    assert native_stops == []
    assert [event["type"] for event in controller.events[-3:]] == [
        "meeting_state",
        "meeting_live_status",
        "meeting_live_status",
    ]
    await web_api._release_persistent_audio(controller)
    database._close_all_connections()


@pytest.mark.asyncio
async def test_default_device_reconnect_keeps_recorder_when_live_preview_fails(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "device-reconnect-preview.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller.on_meeting_pcm = lambda *_args, **_kwargs: None
    meeting = store.create(MeetingCreate(title="Default device reconnect"))
    meeting = store.transition(
        meeting["id"],
        "recording",
        capture_metadata={
            "captureId": "capture-before-device-change",
            "deviceSelection": {"microphoneMode": "default"},
        },
    )
    recorder = TrackingRecorder()
    recorder.started = True
    controller._meeting_recorders[meeting["id"]] = recorder
    commands: list[str] = []

    def shell_call(command, _payload, **_kwargs):
        commands.append(command)
        if command == "audioMeetingStop":
            return {"success": True, "payload": {"stopped": True}}
        if command == "audioMeetingResume":
            return {
                "success": True,
                "payload": {
                    "captureId": "capture-after-device-change",
                    "sampleRate": 16_000,
                    "sources": [],
                },
            }
        raise AssertionError(command)

    async def fail_live_preview(_meeting, **_kwargs):
        assert recorder.started is True
        raise RuntimeError("provider unavailable")

    controller.start_meeting_live_transcription = fail_live_preview
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)

    await web_api.ScriberWebController._reconnect_meeting_after_device_change(
        controller,
        meeting,
        reason="default-device-changed",
        auto_resume=True,
    )

    persisted = store.get(meeting["id"])
    assert persisted["state"] == "recording"
    assert persisted["errorCode"] == "live_stt_resume_failed"
    assert persisted["captureMetadata"]["livePreview"]["status"] == "degraded"
    assert recorder.started is True
    assert recorder.stop_count == 1
    assert recorder.start_count == 1
    assert commands == ["audioMeetingStop", "audioMeetingResume"]
    assert [event["type"] for event in controller.events[-3:]] == [
        "meeting_state",
        "meeting_live_status",
        "meeting_live_status",
    ]
    database._close_all_connections()


@pytest.mark.asyncio
async def test_persisted_audio_claim_blocks_a_second_controller_device_test(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "cross-controller-audio.db")
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    owner = FakeController(store)
    contender = FakeController(store)
    owner._audio_controller_id = "controller-owner"
    contender._audio_controller_id = "controller-contender"
    claim = await web_api._claim_persistent_audio(
        owner,
        owner_kind="live_mic",
        owner_id="live-session-owner",
        heartbeat=False,
    )
    shell_calls: list[str] = []
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(
        web_api,
        "call_shell_ipc",
        lambda command, *_args, **_kwargs: shell_calls.append(command),
    )
    client = TestClient(TestServer(web_api.create_app(contender)))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings/device-test", json={"durationMs": 500}
        )
        assert response.status == 409
        assert (await response.json())["message"] == (
            "Another Scriber controller owns native audio capture."
        )
        assert shell_calls == []
        active = contender._audio_admission_store.active()
        assert active is not None
        assert (active.owner_kind, active.owner_id, active.controller_id) == (
            "live_mic",
            "live-session-owner",
            "controller-owner",
        )
    finally:
        await web_api._release_persistent_audio(owner, claim)
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_live_mic_prewarm_claim_blocks_concurrent_meeting_start(monkeypatch, tmp_path):
    controller, pipelines = _audio_race_controller(monkeypatch, tmp_path)
    prewarm_entered = asyncio.Event()
    release_prewarm = asyncio.Event()
    native_starts: list[str] = []

    async def gated_prewarm_pause():
        prewarm_entered.set()
        await release_prewarm.wait()

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingStart":
            native_starts.append(command)
        return {"success": False, "fallbackReason": "must not start"}

    controller._pause_idle_mic_prewarm_for_capture = gated_prewarm_pause
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        live_task = asyncio.create_task(controller.start_listening())
        await asyncio.wait_for(prewarm_entered.wait(), timeout=1.0)
        meeting_task = asyncio.create_task(
            client.post("/api/meetings", json={"title": "Race"})
        )
        await asyncio.sleep(0.05)
        assert meeting_task.done() is False

        release_prewarm.set()
        assert await live_task is None
        response = await asyncio.wait_for(meeting_task, timeout=1.0)
        assert response.status == 409
        assert native_starts == []
        assert controller._is_listening is True
        assert len(pipelines) == 1
        await controller.stop_listening()
    finally:
        release_prewarm.set()
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_native_start_claim_blocks_live_mic_during_ipc_await(monkeypatch, tmp_path):
    controller, pipelines = _audio_race_controller(monkeypatch, tmp_path)
    controller._pause_idle_mic_prewarm_for_capture = lambda: asyncio.sleep(0)
    ipc_entered = threading.Event()
    release_ipc = threading.Event()

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingStart":
            ipc_entered.set()
            assert release_ipc.wait(timeout=2.0)
            return {
                "success": True,
                "payload": {"captureId": "race-start", "sampleRate": 16_000, "sources": []},
            }
        return {"success": True, "payload": {}}

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        meeting_task = asyncio.create_task(
            client.post("/api/meetings", json={"title": "Claim first"})
        )
        assert await asyncio.to_thread(ipc_entered.wait, 1.0)
        live_task = asyncio.create_task(controller.start_listening())
        await asyncio.sleep(0.05)
        assert live_task.done() is False

        release_ipc.set()
        response = await asyncio.wait_for(meeting_task, timeout=1.0)
        meeting = await response.json()
        assert response.status == 201
        info = await asyncio.wait_for(live_task, timeout=1.0)
        assert info is not None and info.code == "meeting_active"
        assert pipelines == []
        assert controller._is_listening is False
        controller._meeting_store.transition(meeting["id"], "capture_failed")
    finally:
        release_ipc.set()
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", ["paused", "interrupted"])
async def test_meeting_resume_claim_blocks_live_mic_until_recording_is_persisted(
    monkeypatch, tmp_path, resume_state
):
    controller, pipelines = _audio_race_controller(monkeypatch, tmp_path)
    controller._pause_idle_mic_prewarm_for_capture = lambda: asyncio.sleep(0)
    meeting = controller._meeting_store.create(MeetingCreate(title=f"Resume {resume_state}"))
    controller._meeting_store.transition(meeting["id"], "recording")
    controller._meeting_store.transition(meeting["id"], resume_state)
    ipc_entered = threading.Event()
    release_ipc = threading.Event()

    def shell_call(command, _payload, **_kwargs):
        if command == "audioMeetingResume":
            ipc_entered.set()
            assert release_ipc.wait(timeout=2.0)
            return {
                "success": True,
                "payload": {"captureId": f"resume-{resume_state}", "sampleRate": 16_000, "sources": []},
            }
        return {"success": True, "payload": {}}

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        resume_task = asyncio.create_task(
            client.post(f"/api/meetings/{meeting['id']}/resume")
        )
        assert await asyncio.to_thread(ipc_entered.wait, 1.0)
        live_task = asyncio.create_task(controller.start_listening())
        await asyncio.sleep(0.05)
        assert live_task.done() is False

        release_ipc.set()
        response = await asyncio.wait_for(resume_task, timeout=1.0)
        assert response.status == 200
        assert (await response.json())["state"] == "recording"
        info = await asyncio.wait_for(live_task, timeout=1.0)
        assert info is not None and info.code == "meeting_active"
        assert pipelines == []
        controller._meeting_store.transition(meeting["id"], "capture_failed")
    finally:
        release_ipc.set()
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_stage",
    ["after_create", "live_start", "native_start", "recording_commit", "broadcast"],
)
async def test_meeting_start_cancellation_releases_every_owned_stage(
    monkeypatch, tmp_path, cancel_stage
):
    controller, store, lives = _capture_cancellation_controller(
        monkeypatch, tmp_path, f"cancel-start-{cancel_stage}.db"
    )
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings")
    request = _DirectRequest(app, payload={"title": f"Cancel {cancel_stage}"})
    async_entered = asyncio.Event()
    async_release = asyncio.Event()
    thread_entered = threading.Event()
    thread_release = threading.Event()
    native_active = False
    native_stops = 0

    if cancel_stage == "after_create":
        async def pause_at_boundary():
            controller.prewarm_paused = True
            async_entered.set()
            await async_release.wait()

        controller._pause_idle_mic_prewarm_for_capture = pause_at_boundary

    if cancel_stage == "live_start":
        async def partially_start_live(meeting, **_kwargs):
            live = TrackingLiveTranscriber()
            lives.append(live)
            controller._meeting_live_transcribers[meeting["id"]] = live
            async_entered.set()
            await async_release.wait()
            return live

        controller.start_meeting_live_transcription = partially_start_live

    original_transition = store.transition
    if cancel_stage == "recording_commit":
        def transition_at_boundary(meeting_id, state, **kwargs):
            if state == "recording":
                thread_entered.set()
                assert thread_release.wait(timeout=2.0)
            return original_transition(meeting_id, state, **kwargs)

        store.transition = transition_at_boundary

    if cancel_stage == "broadcast":
        async def broadcast_at_boundary(payload):
            controller.events.append(payload)
            if (
                payload.get("type") == "meeting_state"
                and payload.get("meeting", {}).get("state") == "recording"
            ):
                async_entered.set()
                await async_release.wait()

        controller.broadcast = broadcast_at_boundary

    def shell_call(command, _payload, **_kwargs):
        nonlocal native_active, native_stops
        if command == "audioMeetingStart":
            if cancel_stage == "native_start":
                thread_entered.set()
                assert thread_release.wait(timeout=2.0)
            native_active = True
            return {
                "success": True,
                "payload": {
                    "captureId": f"capture-{cancel_stage}",
                    "sampleRate": 16_000,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            native_active = False
            native_stops += 1
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    task = asyncio.create_task(handler(request))
    try:
        if cancel_stage in {"after_create", "live_start", "broadcast"}:
            await asyncio.wait_for(async_entered.wait(), timeout=2.0)
        else:
            assert await asyncio.to_thread(thread_entered.wait, 2.0)
        task.cancel()
        thread_release.set()
        async_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3.0)

        meetings = store.list(limit=10)["items"]
        assert len(meetings) == 1
        assert meetings[0]["state"] == "capture_failed"
        assert meetings[0]["errorCode"] == "meeting_start_canceled"
        assert store.active() is None
        assert controller._meeting_recorders == {}
        assert controller._meeting_live_transcribers == {}
        assert controller.capture_watchdogs == {}
        assert controller.prewarm_paused is False
        assert native_active is False
        if cancel_stage in {"live_start", "native_start", "recording_commit", "broadcast"}:
            assert native_stops == 1
        else:
            assert native_stops == 0
        for recorder in TrackingRecorder.instances:
            assert recorder.started is False
            assert recorder.stop_count == 1
        for live in lives:
            assert live.stop_count == 1
    finally:
        thread_release.set()
        async_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        database._close_all_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", ["paused", "interrupted"])
@pytest.mark.parametrize("cancel_stage", ["native_start", "broadcast"])
async def test_meeting_resume_cancellation_returns_to_owned_interrupted_state(
    monkeypatch, tmp_path, resume_state, cancel_stage
):
    controller, store, lives = _capture_cancellation_controller(
        monkeypatch, tmp_path, f"cancel-resume-{resume_state}-{cancel_stage}.db"
    )
    meeting = store.create(MeetingCreate(title=f"Resume {resume_state}"))
    store.transition(meeting["id"], "recording")
    store.transition(meeting["id"], resume_state)
    controller.prewarm_paused = resume_state == "paused"
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/{id}/resume")
    request = _DirectRequest(app, meeting_id=meeting["id"])
    async_entered = asyncio.Event()
    async_release = asyncio.Event()
    thread_entered = threading.Event()
    thread_release = threading.Event()
    native_active = False
    native_stops = 0

    if cancel_stage == "broadcast":
        async def broadcast_at_boundary(payload):
            controller.events.append(payload)
            if (
                payload.get("type") == "meeting_state"
                and payload.get("meeting", {}).get("state") == "recording"
            ):
                async_entered.set()
                await async_release.wait()

        controller.broadcast = broadcast_at_boundary

    def shell_call(command, _payload, **_kwargs):
        nonlocal native_active, native_stops
        if command == "audioMeetingResume":
            if cancel_stage == "native_start":
                thread_entered.set()
                assert thread_release.wait(timeout=2.0)
            native_active = True
            return {
                "success": True,
                "payload": {
                    "captureId": f"resume-{resume_state}-{cancel_stage}",
                    "sampleRate": 16_000,
                    "sources": [],
                },
            }
        if command == "audioMeetingStop":
            native_active = False
            native_stops += 1
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    task = asyncio.create_task(handler(request))
    try:
        if cancel_stage == "native_start":
            assert await asyncio.to_thread(thread_entered.wait, 2.0)
        else:
            await asyncio.wait_for(async_entered.wait(), timeout=2.0)
        task.cancel()
        thread_release.set()
        async_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3.0)

        persisted = store.get(meeting["id"])
        assert persisted["state"] == "interrupted"
        assert persisted["errorCode"] == "meeting_resume_canceled"
        assert store.active() is None
        assert controller._meeting_recorders == {}
        assert controller._meeting_live_transcribers == {}
        assert controller.capture_watchdogs == {}
        assert controller.prewarm_paused is False
        assert native_active is False
        assert native_stops == 1
        if cancel_stage == "native_start":
            assert TrackingRecorder.instances == []
            assert lives == []
        else:
            assert len(TrackingRecorder.instances) == 1
            assert TrackingRecorder.instances[0].stop_count == 1
            assert TrackingRecorder.instances[0].started is False
            assert len(lives) == 1
            assert lives[0].stop_count == 1
    finally:
        thread_release.set()
        async_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        database._close_all_connections()


class FakeDiarizationComponent:
    installed = False
    busy = False

    def status(self):
        return {
            "available": True, "installed": self.installed,
            "engine": "sherpa-onnx", "version": "1.13.3",
            "segmentationModel": "pyannote-segmentation-3.0-int8",
            "embeddingModel": "3D-Speaker ERes2Net", "byteSize": 0,
            "license": "Apache-2.0",
        }

    async def install(self, _session):
        self.installed = True
        return self.status()

    def delete(self):
        self.installed = False

    async def delete_async(self):
        if self.busy:
            return False
        self.delete()
        return True


def _prepared_durable_import(
    tmp_path: Path,
    import_store: MeetingImportStore,
    *,
    import_id: str,
):
    record = import_store.create(
        import_id=import_id,
        source_filename="interview.wav",
        expected_bytes=4,
        profile_snapshot={
            "id": "test",
            "language": "de",
            "finalProvider": "openai_async",
            "analysisModel": "test-analysis",
            "audioRetentionDays": 0,
            "autoAnalyze": False,
        },
        metadata={"title": "Durable interview", "origin": "imported"},
    )
    import_store.begin_receiving(record.id)
    source_root = tmp_path / "meeting-imports" / record.id
    source_root.mkdir(parents=True)
    source_path = source_root / "source.wav"
    source_path.write_bytes(b"RIFF")
    import_store.mark_received(
        record.id,
        relative_path=source_path.relative_to(tmp_path).as_posix(),
        byte_count=4,
        sha256=web_api.MeetingFinalizer._sha256_file(source_path),
    )
    import_store.transition(record.id, MeetingImportStatus.PROBING)
    import_store.transition(
        record.id,
        MeetingImportStatus.PREPARING,
        probe={"durationMs": 1_250},
    )
    normalized_path = source_root / "system.wav"
    with wave.open(str(normalized_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * 20_000)
    return import_store.mark_prepared(
        record.id,
        relative_path=normalized_path.relative_to(tmp_path).as_posix(),
        byte_count=normalized_path.stat().st_size,
        sha256=web_api.MeetingFinalizer._sha256_file(normalized_path),
        probe={"durationMs": 1_250},
    )


def _durable_import_controller(meeting_store, import_store):
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._meeting_import_store = import_store
    controller._meeting_store = meeting_store
    controller._is_listening = False
    controller._is_stopping = False
    controller._shutting_down = False
    controller._listening_lock = asyncio.Lock()
    controller._meeting_tasks = {}
    controller._meeting_import_tasks = {}
    controller._meeting_import_upload_tasks = {}
    controller._speaker_model = None
    controller._speaker_diarizer = None
    controller.events = []
    controller.scheduled = []

    async def broadcast(payload):
        controller.events.append(payload)

    controller.broadcast = broadcast
    controller.schedule_meeting_finalization = (
        lambda meeting_id, **_kwargs: controller.scheduled.append(meeting_id) or True
    )
    controller.schedule_meeting_analysis = (
        lambda meeting_id, **_kwargs: controller.scheduled.append(meeting_id) or True
    )
    return controller


@pytest.mark.asyncio
async def test_legacy_multipart_meeting_import_is_retired_in_favor_of_durable_protocol(
    monkeypatch,
):
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    client = TestClient(TestServer(web_api.create_app(object())))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings/import",
            data=b"retired multipart payload",
            headers={"Content-Type": "multipart/form-data; boundary=retired"},
        )
        assert response.status == 410
        payload = await response.json()
        assert payload["apiVersion"] == web_api.REST_API_VERSION
        assert payload["createUrl"] == "/api/meeting-imports"
        assert "durable import" in payload["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_durable_meeting_import_commits_upload_before_background_processing(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meeting-import-api.db")
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api.Config, "MEETING_FINAL_PROVIDER", "openai_async")
    monkeypatch.setattr(web_api, "_validate_provider_ready", lambda _provider: None)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller._speaker_diarizer = FakeDiarizationComponent()
    controller._downloads_dir = tmp_path / "downloads"
    controller._meeting_import_store = MeetingImportStore(tmp_path / "meeting-import-api.db")
    controller._meeting_import_tasks = {}
    controller.scheduled_imports = []
    controller.schedule_meeting_import = lambda import_id: controller.scheduled_imports.append(import_id) or True

    async def broadcast_import(record, progress, status):
        controller.events.append({"record": record, "progress": progress, "status": status})

    controller._broadcast_meeting_import = broadcast_import
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        created = await client.post("/api/meeting-imports", json={
            "filename": "Customer interview.webm",
            "byteSize": 4096,
            "title": "Customer interview",
            "language": "de",
            "profileId": "soniox-balanced",
        })
        assert created.status == 201
        created_payload = await created.json()
        import_id = created_payload["id"]
        assert created_payload["state"] == "created"
        assert created_payload["uploadUrl"].endswith(f"/{import_id}/content")

        uploaded = await client.put(
            f"/api/meeting-imports/{import_id}/content",
            data=b"a" * 4096,
            headers={"Content-Type": "audio/webm"},
        )
        assert uploaded.status == 202
        uploaded_payload = await uploaded.json()
        assert uploaded_payload["state"] == "received"
        assert controller.scheduled_imports == [import_id]
        persisted = controller._meeting_import_store.require(import_id)
        assert persisted.status == MeetingImportStatus.RECEIVED
        assert persisted.original_bytes == 4096
        assert (tmp_path / persisted.original_relative_path).read_bytes() == b"a" * 4096

        replayed = await client.put(
            f"/api/meeting-imports/{import_id}/content",
            data=b"b" * 4096,
            headers={"Content-Type": "audio/webm"},
        )
        assert replayed.status == 202
        replayed_payload = await replayed.json()
        assert replayed_payload["state"] == "received"
        unchanged = controller._meeting_import_store.require(import_id)
        assert unchanged.status == MeetingImportStatus.RECEIVED
        assert unchanged.original_sha256 == persisted.original_sha256
        assert (tmp_path / unchanged.original_relative_path).read_bytes() == b"a" * 4096
    finally:
        await client.close()
        controller._meeting_import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_import_collection_recovers_jobs_without_exposing_staging_details(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    import_store = MeetingImportStore(tmp_path / "meeting-import-list.db")
    active = import_store.create(
        import_id="active-import",
        source_filename="Board review.wav",
        expected_bytes=4,
        profile_snapshot={"id": "balanced", "language": "de", "apiKey": "must-not-leak"},
        metadata={"title": "Board review", "origin": "imported"},
    )
    import_store.begin_receiving(active.id)
    import_store.mark_received(
        active.id,
        relative_path="meeting-imports/active-import/source.wav",
        byte_count=4,
        sha256="a" * 64,
    )
    failed = import_store.create(
        import_id="failed-import",
        source_filename="Interview.mp3",
        expected_bytes=8,
        profile_snapshot={"id": "balanced", "language": "en"},
        metadata={"title": "Interview", "origin": "imported"},
    )
    import_store.mark_failed(
        failed.id,
        error_code="decode_failed",
        error_message=r"C:\Users\private\recording.mp3 sk-test-secret",
    )
    retryable = import_store.create(
        import_id="retryable-import",
        source_filename="Planning.wav",
        expected_bytes=4,
        profile_snapshot={"id": "balanced", "language": "en"},
        metadata={"title": "Planning", "origin": "imported"},
    )
    import_store.begin_receiving(retryable.id)
    import_store.mark_received(
        retryable.id,
        relative_path="meeting-imports/retryable-import/source.wav",
        byte_count=4,
        sha256="b" * 64,
    )
    import_store.transition(retryable.id, MeetingImportStatus.PROBING)
    import_store.transition(retryable.id, MeetingImportStatus.PREPARING)
    import_store.mark_prepared(
        retryable.id,
        relative_path="meeting-imports/retryable-import/system.wav",
        byte_count=44,
        sha256="c" * 64,
        probe={"durationMs": 1_000},
    )
    import_store.transition(
        retryable.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-retryable"
    )
    import_store.mark_failed(
        retryable.id,
        error_code="finalization_failed",
        error_message="Final processing stopped.",
    )
    controller = object.__new__(web_api.ScriberWebController)
    controller._meeting_import_store = import_store
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.get("/api/meeting-imports?limit=10")
        assert response.status == 200
        payload = await response.json()
        assert payload["apiVersion"] == web_api.REST_API_VERSION
        assert [item["id"] for item in payload["items"]] == [
            active.id,
            retryable.id,
            failed.id,
        ]
        assert payload["items"][0]["state"] == "received"
        assert payload["items"][0]["canCancel"] is True
        assert payload["items"][1]["canRetry"] is True
        assert payload["items"][1]["meetingId"] == "meeting-retryable"
        assert payload["items"][2]["canRetry"] is False
        serialized = str(payload)
        assert "relativePath" not in serialized
        assert "sha256" not in serialized
        assert "profileSnapshot" not in serialized
        assert "C:\\Users\\private" not in serialized
        assert "sk-test-secret" not in serialized

        invalid = await client.get("/api/meeting-imports?limit=not-a-number")
        assert invalid.status == 400
    finally:
        await client.close()
        import_store.close()


@pytest.mark.asyncio
async def test_durable_import_worker_commits_workspace_and_enters_shared_finalizer(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "durable-worker.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api, "require_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 1.25)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "durable-worker.db")
    record = import_store.create(
        source_filename="interview.wav", expected_bytes=4,
        profile_snapshot={
            "id": "test", "language": "de", "finalProvider": "openai_async",
            "analysisModel": "test-analysis", "audioRetentionDays": 0, "autoAnalyze": False,
        },
        metadata={"title": "Durable interview", "origin": "imported"},
    )
    import_store.begin_receiving(record.id)
    source_root = tmp_path / "meeting-imports" / record.id
    source_root.mkdir(parents=True)
    source_path = source_root / "source.wav"
    source_path.write_bytes(b"RIFF")
    import_store.mark_received(
        record.id,
        relative_path=source_path.relative_to(tmp_path).as_posix(),
        byte_count=4,
        sha256=web_api.MeetingFinalizer._sha256_file(source_path),
    )

    class PreparedAudioProcess:
        returncode = 0

        def __init__(self, destination: Path):
            self.destination = destination

        async def communicate(self):
            with wave.open(str(self.destination), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\0\0" * 20_000)
            return b"", b""

        def kill(self):
            self.returncode = -1

        async def wait(self):
            return self.returncode

    async def fake_subprocess(*args, **_kwargs):
        return PreparedAudioProcess(Path(args[-1]))

    monkeypatch.setattr(web_api.asyncio, "create_subprocess_exec", fake_subprocess)
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._meeting_import_store = import_store
    controller._meeting_store = meeting_store
    controller._is_listening = False
    controller._is_stopping = False
    controller._meeting_tasks = {}
    controller._meeting_import_tasks = {}
    events = []
    scheduled = []

    async def broadcast(payload):
        events.append(payload)

    controller.broadcast = broadcast
    controller.schedule_meeting_finalization = lambda meeting_id, **_kwargs: scheduled.append(meeting_id) or True
    await controller._run_meeting_import(record.id)

    persisted = import_store.require(record.id)
    assert persisted.status == MeetingImportStatus.FINALIZING
    assert persisted.meeting_id in scheduled
    meeting = meeting_store.get(persisted.meeting_id)
    assert meeting["state"] == "finalizing"
    assert meeting["origin"] == "imported"
    assert meeting["transcriptionMode"] == "final_only"
    assert meeting["liveProvider"] == "file-import"
    assert meeting["consentConfirmed"] is False
    assert meeting["captureMetadata"]["importId"] == record.id
    chunks = meeting_store.audio_chunks(persisted.meeting_id, "system")
    assert len(chunks) == 1
    assert (tmp_path / "meetings" / chunks[0]["relativePath"]).is_file()
    assert any(event.get("type") == "meeting_import_progress" for event in events)
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_durable_import_rejects_audio_beyond_the_final_provider_duration(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "duration-limit-import.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api, "_probe_media_duration_seconds", lambda _path: 8_101.0)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "duration-limit-import.db")
    record = import_store.create(
        source_filename="long-interview.wav",
        expected_bytes=4,
        profile_snapshot={
            "id": "test",
            "language": "de",
            "finalProvider": "gladia_async",
            "analysisModel": "test-analysis",
            "audioRetentionDays": 0,
            "autoAnalyze": False,
        },
        metadata={"title": "Long interview", "origin": "imported"},
    )
    import_store.begin_receiving(record.id)
    source_root = tmp_path / "meeting-imports" / record.id
    source_root.mkdir(parents=True)
    source_path = source_root / "source.wav"
    source_path.write_bytes(b"RIFF")
    import_store.mark_received(
        record.id,
        relative_path=source_path.relative_to(tmp_path).as_posix(),
        byte_count=4,
        sha256=web_api.MeetingFinalizer._sha256_file(source_path),
    )
    controller = _durable_import_controller(meeting_store, import_store)

    await controller._run_meeting_import(record.id)

    persisted = import_store.require(record.id)
    assert persisted.status == MeetingImportStatus.FAILED
    assert "up to 135 minutes" in persisted.error_message
    assert controller.scheduled == []
    assert not source_root.exists()
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_durable_import_shutdown_keeps_waiting_job_recoverable(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "shutdown-import.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "shutdown-import.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="shutdown-waiting"
    )
    controller = _durable_import_controller(meeting_store, import_store)
    controller._is_listening = True
    entered_wait = asyncio.Event()

    async def broadcast(payload):
        controller.events.append(payload)
        if payload.get("type") == "meeting_import_progress":
            entered_wait.set()

    controller.broadcast = broadcast
    task = asyncio.create_task(controller._run_meeting_import(waiting.id))
    await asyncio.wait_for(entered_wait.wait(), timeout=2)
    controller._shutting_down = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    persisted = import_store.require(waiting.id)
    assert persisted.status == MeetingImportStatus.WAITING_FOR_WORKSPACE
    assert persisted.cancel_requested is False
    assert (tmp_path / persisted.original_relative_path).is_file()
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_durable_import_recovers_claim_before_meeting_creation(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "claim-recovery.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "claim-recovery.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="claim-recovery"
    )
    claimed_meeting_id = "1" * 32
    import_store.transition(
        waiting.id,
        MeetingImportStatus.COMMITTING,
        expected_status=MeetingImportStatus.WAITING_FOR_WORKSPACE,
        meeting_id=claimed_meeting_id,
    )
    controller = _durable_import_controller(meeting_store, import_store)

    await controller._run_meeting_import(waiting.id)

    persisted = import_store.require(waiting.id)
    assert persisted.status == MeetingImportStatus.FINALIZING
    assert persisted.meeting_id == claimed_meeting_id
    assert meeting_store.get(claimed_meeting_id)["state"] == "finalizing"
    assert meeting_store.list(limit=10)["total"] == 1
    assert not (tmp_path / "meeting-imports" / waiting.id).exists()
    assert (tmp_path / persisted.normalized_relative_path).is_file()
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_durable_import_recovers_directory_move_before_job_transition(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "move-recovery.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "move-recovery.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="move-recovery"
    )
    meeting_id = "2" * 32
    claimed = import_store.transition(
        waiting.id,
        MeetingImportStatus.COMMITTING,
        meeting_id=meeting_id,
    )
    meeting_store.create(
        MeetingCreate(
            title="Recovered import",
            origin="imported",
            capture_metadata={"importId": waiting.id},
        ),
        meeting_id=meeting_id,
    )
    destination = tmp_path / "meetings" / meeting_id / "import"
    destination.parent.mkdir(parents=True)
    (tmp_path / "meeting-imports" / waiting.id).replace(destination)
    assert meeting_store.recover_interrupted() == 1
    controller = _durable_import_controller(meeting_store, import_store)

    await controller._run_meeting_import(claimed.id)

    persisted = import_store.require(claimed.id)
    assert persisted.status == MeetingImportStatus.FINALIZING
    assert persisted.original_relative_path.startswith(f"meetings/{meeting_id}/import/")
    assert meeting_store.list(limit=10)["total"] == 1
    assert meeting_store.get(meeting_id)["state"] == "finalizing"
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_durable_import_pre_finalization_failure_removes_workspace(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "cleanup-failure.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "cleanup-failure.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="cleanup-failure"
    )
    claimed = import_store.transition(
        waiting.id,
        MeetingImportStatus.COMMITTING,
        meeting_id="3" * 32,
    )
    (tmp_path / claimed.normalized_relative_path).write_bytes(b"changed-after-hash")
    controller = _durable_import_controller(meeting_store, import_store)

    await controller._run_meeting_import(claimed.id)

    persisted = import_store.require(claimed.id)
    assert persisted.status == MeetingImportStatus.FAILED
    assert persisted.error_code == "ValueError"
    with pytest.raises(web_api.MeetingNotFound):
        meeting_store.get(claimed.meeting_id)
    assert not (tmp_path / "meetings" / claimed.meeting_id).exists()
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_finalizing_recovery_failure_preserves_workspace_for_discard(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "finalizing-recovery.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "finalizing-recovery.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="finalizing-recovery"
    )
    meeting_id = "6" * 32
    import_store.transition(
        waiting.id, MeetingImportStatus.COMMITTING, meeting_id=meeting_id
    )
    meeting_store.create(
        MeetingCreate(
            title="Broken finalizing import",
            origin="imported",
            capture_metadata={"importId": waiting.id},
        ),
        meeting_id=meeting_id,
    )
    meeting_store.transition(meeting_id, "finalizing")
    destination = tmp_path / "meetings" / meeting_id / "import"
    destination.parent.mkdir(parents=True)
    (tmp_path / "meeting-imports" / waiting.id).replace(destination)
    import_store.transition(
        waiting.id,
        MeetingImportStatus.FINALIZING,
        original_relative_path=(destination / "source.wav").relative_to(tmp_path).as_posix(),
        normalized_relative_path=(destination / "system.wav").relative_to(tmp_path).as_posix(),
    )
    controller = _durable_import_controller(meeting_store, import_store)

    await controller._run_meeting_import(waiting.id)

    assert import_store.require(waiting.id).status == MeetingImportStatus.FAILED
    assert meeting_store.get(meeting_id)["state"] == "finalization_failed"
    assert (destination / "source.wav").is_file()
    assert (destination / "system.wav").is_file()
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_cancel_after_workspace_claim_returns_meeting_handoff(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "cancel-claim.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "cancel-claim.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="cancel-claim"
    )
    claimed = import_store.transition(
        waiting.id,
        MeetingImportStatus.COMMITTING,
        meeting_id="4" * 32,
    )
    controller = _durable_import_controller(meeting_store, import_store)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.delete(f"/api/meeting-imports/{claimed.id}")
        payload = await response.json()
        assert response.status == 409
        assert payload["meetingId"] == claimed.meeting_id
        assert import_store.require(claimed.id).status == MeetingImportStatus.COMMITTING
        assert import_store.require(claimed.id).cancel_requested is False
    finally:
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_cancel_waits_for_active_upload_before_removing_staging(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "cancel-upload.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "cancel-upload.db")
    record = import_store.create(
        import_id="cancel-upload",
        source_filename="long.wav",
        expected_bytes=2 * 1024 * 1024,
        profile_snapshot={"id": "test", "finalProvider": "openai_async"},
    )
    controller = _durable_import_controller(meeting_store, import_store)
    receiving = asyncio.Event()

    async def broadcast(payload):
        controller.events.append(payload)
        if (
            payload.get("type") == "meeting_import_progress"
            and payload.get("phase") == "receiving"
        ):
            receiving.set()

    controller.broadcast = broadcast
    release_upload = asyncio.Event()

    async def slow_body():
        yield b"a" * (1024 * 1024)
        await release_upload.wait()
        yield b"b" * (1024 * 1024)

    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    upload_task = asyncio.create_task(
        client.put(
            f"/api/meeting-imports/{record.id}/content",
            data=slow_body(),
            headers={"Content-Type": "audio/wav"},
        )
    )
    try:
        await asyncio.wait_for(receiving.wait(), timeout=3)
        response = await client.delete(f"/api/meeting-imports/{record.id}")
        assert response.status == 200
        assert (await response.json())["state"] == "canceled"
        assert import_store.require(record.id).status == MeetingImportStatus.CANCELED
        assert not (tmp_path / "meeting-imports" / record.id).exists()
    finally:
        release_upload.set()
        await asyncio.gather(upload_task, return_exceptions=True)
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_shutdown_after_received_preserves_accepted_import_source(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "shutdown-received.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "shutdown-received.db")
    record = import_store.create(
        import_id="shutdown-received",
        source_filename="accepted.wav",
        expected_bytes=4,
        profile_snapshot={"id": "test", "finalProvider": "openai_async"},
    )
    controller = _durable_import_controller(meeting_store, import_store)
    controller.schedule_meeting_import = lambda _import_id: True
    accepted = asyncio.Event()
    hold_response = asyncio.Event()

    async def broadcast(payload):
        controller.events.append(payload)
        if (
            payload.get("type") == "meeting_import_progress"
            and payload.get("phase") == "received"
        ):
            accepted.set()
            await hold_response.wait()

    controller.broadcast = broadcast
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    request_task = asyncio.create_task(
        client.put(
            f"/api/meeting-imports/{record.id}/content",
            data=b"RIFF",
            headers={"Content-Type": "audio/wav"},
        )
    )
    try:
        await asyncio.wait_for(accepted.wait(), timeout=3)
        controller._shutting_down = True
        handler = controller._meeting_import_upload_tasks[record.id]
        handler.cancel()
        await asyncio.gather(request_task, return_exceptions=True)

        persisted = import_store.require(record.id)
        assert persisted.status == MeetingImportStatus.RECEIVED
        accepted_path = tmp_path / persisted.original_relative_path
        assert accepted_path.is_file()
        assert accepted_path.read_bytes() == b"RIFF"
    finally:
        hold_response.set()
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_post_commit_bookkeeping_failure_never_deletes_accepted_import(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "accepted-bookkeeping.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "accepted-bookkeeping.db")
    record = import_store.create(
        import_id="accepted-bookkeeping",
        source_filename="accepted.wav",
        expected_bytes=4,
        profile_snapshot={"id": "test", "finalProvider": "openai_async"},
    )
    controller = _durable_import_controller(meeting_store, import_store)

    def fail_schedule(_import_id):
        raise RuntimeError("synthetic scheduler bookkeeping failure")

    controller.schedule_meeting_import = fail_schedule
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.put(
            f"/api/meeting-imports/{record.id}/content",
            data=b"RIFF",
            headers={"Content-Type": "audio/wav"},
        )
        assert response.status == 202
        persisted = import_store.require(record.id)
        assert persisted.status == MeetingImportStatus.RECEIVED
        assert (tmp_path / persisted.original_relative_path).read_bytes() == b"RIFF"
    finally:
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_finalizer_failure_updates_durable_import_and_retry_reopens_it(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "finalizer-failure.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "finalizer-failure.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="finalizer-failure"
    )
    import_store.transition(
        waiting.id, MeetingImportStatus.COMMITTING, meeting_id="5" * 32
    )
    controller = _durable_import_controller(meeting_store, import_store)
    await controller._run_meeting_import(waiting.id)

    async def fail_finalizer(_self, _meeting_id, _progress):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(web_api.MeetingFinalizer, "run", fail_finalizer)
    await controller._run_meeting_finalization("5" * 32)
    failed = import_store.require(waiting.id)
    assert failed.status == MeetingImportStatus.FAILED
    assert failed.error_code == "RuntimeError"
    assert meeting_store.get("5" * 32)["state"] == "finalization_failed"

    controller.schedule_meeting_finalization = lambda _meeting_id, **_kwargs: True
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.post(f"/api/meetings/{'5' * 32}/retry")
        assert response.status == 202
        reopened = import_store.require(waiting.id)
        assert reopened.status == MeetingImportStatus.FINALIZING
        assert reopened.error_code == ""
    finally:
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_import_recovery_projects_analysis_failure_to_retryable_failed_job(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "analysis-crash-import.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    import_store = MeetingImportStore(tmp_path / "analysis-crash-import.db")
    waiting = _prepared_durable_import(
        tmp_path, import_store, import_id="analysis-crash-import"
    )
    meeting_id = "6" * 32
    import_store.transition(
        waiting.id, MeetingImportStatus.COMMITTING, meeting_id=meeting_id
    )
    controller = _durable_import_controller(meeting_store, import_store)
    await controller._run_meeting_import(waiting.id)
    assert import_store.require(waiting.id).status == MeetingImportStatus.FINALIZING
    meeting_store.transition(meeting_id, "analyzing")
    meeting_store.transition(
        meeting_id,
        "analysis_failed",
        error_code="process_interrupted_during_analysis",
        error_message="Canonical transcript is intact.",
    )

    await controller._run_meeting_import(waiting.id)

    failed = import_store.require(waiting.id)
    assert failed.status == MeetingImportStatus.FAILED
    assert failed.error_code == "meeting_analysis_failed"
    assert failed.meeting_id == meeting_id
    import_store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_retry_does_not_mutate_state_while_previous_task_is_still_owned(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "retry-owned.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    meeting_store = MeetingStore()
    meeting_store.initialize()
    meeting = meeting_store.create(MeetingCreate(title="Retry race"))
    meeting_store.transition(meeting["id"], "finalizing")
    meeting_store.transition(
        meeting["id"],
        "finalization_failed",
        error_code="provider_failed",
        error_message="Provider failed.",
    )
    import_store = MeetingImportStore(tmp_path / "retry-owned.db")
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._meeting_store = meeting_store
    controller._meeting_import_store = import_store
    controller._meeting_tasks = {}
    controller._loop = asyncio.get_running_loop()
    blocker = asyncio.Event()
    owned_task = asyncio.create_task(blocker.wait())
    controller._meeting_tasks[meeting["id"]] = owned_task

    async def broadcast(_payload):
        return None

    controller.broadcast = broadcast
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.post(f"/api/meetings/{meeting['id']}/retry")
        assert response.status == 409
        assert meeting_store.get(meeting["id"])["state"] == "finalization_failed"
        assert controller._meeting_tasks[meeting["id"]] is owned_task
    finally:
        owned_task.cancel()
        await asyncio.gather(owned_task, return_exceptions=True)
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_finalization_retry_can_switch_to_a_ready_compatible_provider(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "retry-provider.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api, "_provider_readiness_error", lambda _provider: None)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting = store.create(MeetingCreate(
        title="Switch final provider",
        final_provider="gladia_async",
    ))
    store.add_audio_chunk(
        meeting["id"],
        source="system",
        sequence=0,
        relative_path="retry-provider/system.wav",
        started_at_ms=0,
        ended_at_ms=9_000_000,
    )
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "finalization_failed")
    import_store = MeetingImportStore(tmp_path / "retry-provider.db")
    controller = FakeController(store)
    controller._meeting_import_store = import_store
    controller._meeting_tasks = {}
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        rejected = await client.post(
            f"/api/meetings/{meeting['id']}/retry",
            json={"finalProvider": "gladia_async"},
        )
        assert rejected.status == 409
        assert store.get(meeting["id"])["finalProvider"] == "gladia_async"
        assert store.get(meeting["id"])["state"] == "finalization_failed"

        accepted = await client.post(
            f"/api/meetings/{meeting['id']}/retry",
            json={"finalProvider": "deepgram_async"},
        )
        payload = await accepted.json()
        assert accepted.status == 202
        assert payload["state"] == "finalizing"
        assert payload["finalProvider"] == "deepgram_async"
        assert store.get(meeting["id"])["finalProvider"] == "deepgram_async"
    finally:
        await client.close()
        import_store.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_discard_rejects_workspace_owned_by_running_finalizer(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "discard-barrier.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting = store.create(MeetingCreate(title="Still processing"))
    store.transition(meeting["id"], "finalizing")
    workspace = tmp_path / "meetings" / meeting["id"]
    workspace.mkdir(parents=True)
    sentinel = workspace / "owned.wav"
    sentinel.write_bytes(b"owned")
    controller = FakeController(store)
    controller._meeting_tasks = {}
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.delete(f"/api/meetings/{meeting['id']}")
        assert response.status == 409
        assert sentinel.is_file()
        assert store.get(meeting["id"])["state"] == "finalizing"
    finally:
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_maintenance_finishes_crash_interrupted_discard(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "discard-recovery.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting = store.create(MeetingCreate(title="Discard tombstone"))
    store.transition(meeting["id"], "discarded")
    workspace = tmp_path / "meetings" / meeting["id"]
    workspace.mkdir(parents=True)
    (workspace / "leftover.wav").write_bytes(b"leftover")
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._meeting_store = store

    await controller._prune_discarded_meeting_workspaces()

    assert not workspace.exists()
    with pytest.raises(web_api.MeetingNotFound):
        store.get(meeting["id"])
    database._close_all_connections()


@pytest.mark.asyncio
async def test_maintenance_finishes_crash_interrupted_transcript_source_purge(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "source-purge-recovery.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    database.save_transcript({
        "id": "transcript-source-purge", "title": "Source purge", "date": "",
        "duration": "00:00", "status": "completed", "type": "file",
        "language": "auto", "step": "", "sourceUrl": "", "channel": "",
        "thumbnailUrl": "", "content": "Done", "createdAt": "", "updatedAt": "",
    })
    source = tmp_path / "downloads" / "files" / "job-1" / "source.wav"
    source.parent.mkdir(parents=True)
    payload = b"durable source"
    source.write_bytes(payload)
    store = TranscriptArtifactStore(tmp_path / "source-purge-recovery.db")
    asset = store.add_source_asset(
        transcript_id="transcript-source-purge",
        source_track="mix",
        asset_kind="uploaded_audio",
        purpose="processing_only",
        relative_path=source.relative_to(tmp_path).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        asset_id="pending-source",
    )
    pending = store.mark_source_asset_purge_pending(
        asset.id, expected_version=asset.state_version
    )
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._transcript_artifacts = store

    await controller._resume_pending_transcript_source_purges()

    assert not source.exists()
    recovered = store.get_source_asset(pending.id)
    assert recovered is not None
    assert recovered.state == SourceAssetState.PURGED
    assert recovered.relative_path == ""
    assert recovered.tombstone_reason == "startup_processing_source_purge_recovered"
    store.close()
    database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_api_runs_capture_lifecycle_without_fabricated_consent(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(web_api.Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    monkeypatch.setattr(web_api.Config, "MEETING_FINAL_PROVIDER", "soniox_async")
    monkeypatch.setattr(
        web_api.Config,
        "MEETING_ANALYSIS_MODEL",
        web_api.Config.SUMMARIZATION_MODEL or web_api.Config.DEFAULT_SUMMARIZATION_MODEL,
    )
    database.init_database()
    store = MeetingStore()
    store.initialize()
    controller = FakeController(store)
    controller._speaker_diarizer = FakeDiarizationComponent()
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api, "MeetingAudioRecorder", FakeRecorder)
    monkeypatch.setattr(web_api, "MeetingDeviceLevelProbe", FakeDeviceProbe)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    shell_calls = []

    def shell_call(command, payload, **_kwargs):
        shell_calls.append((command, dict(payload)))
        if command in {"audioMeetingPause", "audioMeetingStop"}:
            recorder = controller._meeting_recorders.get(str(payload.get("meetingId") or ""))
            if recorder is not None:
                assert recorder.expected_disconnect is True
        if command == "audioEndpointInventory":
            return {
                "success": True,
                "payload": {
                    "available": True,
                    "endpoints": [
                        {"endpointIdHash": "a" * 32, "friendlyName": "USB Mic", "flow": "capture", "isDefault": True, "defaultRoles": ["console"]},
                        {"endpointIdHash": "b" * 32, "friendlyName": "Desk Speakers", "flow": "render", "isDefault": True, "defaultRoles": ["console"]},
                        {"endpointIdHash": "not-a-hash", "friendlyName": "Rejected", "flow": "capture", "isDefault": False},
                    ],
                },
            }
        if command in {"audioMeetingStart", "audioMeetingResume"}:
            return {
                "success": True,
                "payload": {
                    "captureId": "mic.system",
                    "sampleRate": 16_000,
                    "frameDurationMs": 10,
                    "aecActive": False,
                    "sources": [
                        {"source": "microphone", "framePipe": "private-mic"},
                        {"source": "system", "framePipe": "private-system"},
                    ],
                },
            }
        return {
            "success": True,
            "payload": {
                "stopped": True,
                "sidecar": {
                    "relay": {
                        "framesProcessed": 250,
                        "bytesForwarded": 240_000,
                        "sidecarUptimeMs": 2_500,
                        "relayError": None,
                        "aecMetrics": {
                            "measurement": "render-active-raw-to-clean-energy-ratio",
                            "renderActiveFrames": 100,
                            "renderActiveDurationMs": 1_000,
                            "renderEnergy": 50_000.0,
                            "rawMicEnergy": 10_000.0,
                            "cleanMicEnergy": 1_000.0,
                            "echoReductionDb": 10.0,
                        },
                    },
                },
            },
        }

    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        component = await client.get("/api/meetings/diarization-component")
        assert component.status == 200
        assert (await component.json())["installed"] is False
        installed_component = await client.post("/api/meetings/diarization-component")
        assert installed_component.status == 200
        assert (await installed_component.json())["installed"] is True
        controller._speaker_diarizer.busy = True
        busy_delete = await client.delete("/api/meetings/diarization-component")
        assert busy_delete.status == 409
        assert (await busy_delete.json())["deleted"] is False
        assert controller._speaker_diarizer.installed is True
        controller._speaker_diarizer.busy = False
        deleted_component = await client.delete("/api/meetings/diarization-component")
        assert deleted_component.status == 200
        assert (await deleted_component.json())["installed"] is False

        hotkey = await client.post("/api/meetings/hotkey")
        assert hotkey.status == 202
        assert (await hotkey.json())["requiresConfirmation"] is True
        assert controller.events[-1]["type"] == "meeting_detected"

        profiles = await client.get("/api/meeting-profiles")
        assert profiles.status == 200
        profile_payload = await profiles.json()
        assert profile_payload["defaultProfileId"] == "soniox-balanced"
        assert profile_payload["providerCapabilities"]["soniox"]["live"] is True
        assert all(
            profile_payload["providerCapabilities"][provider]["batchDiarization"] is True
            for provider in ("soniox_async", "assemblyai", "mistral_async", "deepgram_async")
        )
        assert {
            "soniox_async", "assemblyai", "mistral_async", "deepgram_async",
            "openai_async", "gemini_stt", "azure_mai", "onnx_local", "groq",
        }.issubset({item["id"] for item in profile_payload["finalProviderOptions"]})
        assert profile_payload["providerCapabilities"]["openai_async"]["batchDiarization"] is False
        assert profile_payload["providerCapabilities"]["openai_async"]["localDiarizationFallback"] is True
        assert profile_payload["providerCapabilities"]["soniox_async"]["fiveHourSupported"] is True
        assert profile_payload["providerCapabilities"]["assemblyai"]["fiveHourSupported"] is True
        assert profile_payload["providerCapabilities"]["deepgram_async"]["fiveHourSupported"] is False
        assert profile_payload["providerCapabilities"]["azure_mai"]["fiveHourSupported"] is True
        assert profile_payload["providerCapabilities"]["onnx_local"]["fiveHourSupported"] is True
        assert profile_payload["providerCapabilities"]["openai_async"]["fiveHourSupported"] is False
        assert profile_payload["providerCapabilities"]["smallest_async"]["fiveHourSupported"] is False
        assert "not yet verified" in profile_payload["providerCapabilities"]["openai_async"]["fiveHourReason"]
        assert profile_payload["providerCapabilities"]["soniox_async"]["maxDurationSeconds"] == 18_000
        assert profile_payload["providerCapabilities"]["gladia_async"]["maxDurationSeconds"] == 8_100
        assert profile_payload["providerCapabilities"]["mistral_async"]["maxDurationSeconds"] == 10_800
        assert profile_payload["profiles"][0]["fiveHourSupported"] is True
        assert profile_payload["profiles"][0]["name"] == "Live text + Soniox Async final"
        assert [stage["model"] for stage in profile_payload["profiles"][0]["stages"]] == [
            web_api.Config.SONIOX_RT_MODEL,
            web_api.Config.SONIOX_ASYNC_MODEL,
            web_api.Config.SUMMARIZATION_MODEL or web_api.Config.DEFAULT_SUMMARIZATION_MODEL,
        ]

        devices = await client.get("/api/meetings/audio-devices")
        assert devices.status == 200
        device_payload = await devices.json()
        assert device_payload["available"] is True
        assert device_payload["source"] == "rust-wasapi"
        assert device_payload["partial"] is False
        assert device_payload["reason"] == ""
        assert device_payload["capture"] == [{
            "endpointIdHash": "a" * 32, "friendlyName": "USB Mic",
            "isDefault": True, "defaultRoles": ["console"],
        }]
        assert device_payload["render"] == [{
            "endpointIdHash": "b" * 32, "friendlyName": "Desk Speakers",
            "isDefault": True, "defaultRoles": ["console"],
        }]
        device_test = await client.post("/api/meetings/device-test", json={
            "microphoneNativeEndpointIdHash": "a" * 32,
            "renderNativeEndpointIdHash": "b" * 32,
            "durationMs": 500,
            "aecEnabled": True,
        })
        assert device_test.status == 200
        device_test_payload = await device_test.json()
        assert device_test_payload["audioPersisted"] is False
        assert device_test_payload["audioSentToProvider"] is False
        assert device_test_payload["sources"]["microphone"]["rms"] == 0.2
        assert device_test_payload["sources"]["system"]["active"] is True
        assert device_test_payload["testTonePlayed"] is False
        assert controller.prewarm_paused is False
        assert controller._audio_admission_store.active() is None
        device_start = next(
            payload for command, payload in shell_calls
            if command == "audioMeetingStart" and str(payload.get("meetingId", "")).startswith("device-test-")
        )
        assert device_start["microphoneNativeEndpointIdHash"] == "a" * 32
        assert device_start["renderNativeEndpointIdHash"] == "b" * 32
        preflight = await client.options(
            "/api/meetings/example/action-items/example",
            headers={"Origin": "http://localhost:5000", "Access-Control-Request-Method": "PATCH"},
        )
        assert preflight.status == 204
        assert "PATCH" in preflight.headers["Access-Control-Allow-Methods"]

        started = await client.post("/api/meetings", json={
            "title": "Call",
            "microphoneDeviceId": "usb-selected",
            "microphoneNativeEndpointIdHash": "mic-hash",
            "renderNativeEndpointIdHash": "render-hash",
        })
        assert started.status == 201
        meeting = await started.json()
        assert meeting["state"] == "recording"
        assert meeting["origin"] == "captured"
        assert meeting["consentConfirmed"] is False
        assert meeting["captureMetadata"]["sources"] == ["microphone", "system"]
        assert meeting["captureMetadata"]["captureStartLatencyMs"] >= 0
        assert meeting["captureMetadata"]["timelineOffsetMs"] == 0
        assert meeting["captureMetadata"]["timelineStartedAtUtc"]
        assert meeting["captureMetadata"]["deviceSelection"]["microphoneMode"] == "explicit"
        assert "framePipe" not in str(meeting)
        active_audio_claim = controller._audio_admission_store.active()
        assert active_audio_claim is not None
        assert (active_audio_claim.owner_kind, active_audio_claim.owner_id) == (
            "meeting", meeting["id"]
        )
        active_hotkey = await client.post("/api/meetings/hotkey")
        assert (await active_hotkey.json())["meetingId"] == meeting["id"]

        note = await client.post(f"/api/meetings/{meeting['id']}/notes", json={"body": "Ship Friday"})
        assert note.status == 201
        paused = await client.post(f"/api/meetings/{meeting['id']}/pause")
        assert (await paused.json())["state"] == "paused"
        assert controller._audio_admission_store.active().owner_id == meeting["id"]
        resumed = await client.post(f"/api/meetings/{meeting['id']}/resume")
        resumed_payload = await resumed.json()
        assert resumed_payload["state"] == "recording"
        assert resumed_payload["captureMetadata"]["timelineStartedAtUtc"]
        resume_payload = next(payload for command, payload in shell_calls if command == "audioMeetingResume")
        assert resume_payload["microphoneNativeEndpointIdHash"] == "mic-hash"
        assert resume_payload["renderNativeEndpointIdHash"] == "render-hash"
        stopped = await client.post(f"/api/meetings/{meeting['id']}/stop")
        assert stopped.status == 202
        assert (await stopped.json())["state"] == "finalizing"
        assert controller._audio_admission_store.active() is None
        assert controller.scheduled == [meeting["id"]]

        listing = await client.get("/api/meetings")
        payload = await listing.json()
        assert payload["total"] == 1
        detail = await client.get(f"/api/meetings/{meeting['id']}")
        detail_payload = await detail.json()
        assert detail_payload["notes"][0]["body"] == "Ship Friday"
        assert len(detail_payload["audioGaps"]) == 1
        assert detail_payload["audioGaps"][0]["reason"] == "pause"
        assert detail_payload["captureMetadata"]["timelineOffsetMs"] == detail_payload["audioGaps"][0]["endedAtMs"]
        assert len(detail_payload["captureMetadata"]["persistenceSessions"]) == 2
        assert len(detail_payload["captureMetadata"]["liveTranscriptionSessions"]) == 2
        assert detail_payload["captureMetadata"]["liveTranscriptionSessions"][-1]["interimLatencyP95Ms"] == 750
        assert len(detail_payload["captureMetadata"]["nativeStopSessions"]) == 2
        assert detail_payload["captureMetadata"]["nativeStopSessions"][-1]["relayHealthy"] is True
        assert detail_payload["captureMetadata"]["aecMetrics"]["echoReductionDb"] == 10.0
        assert "relayError" not in detail_payload["captureMetadata"]["nativeStopSessions"][-1]
        store.transition(
            meeting["id"], "interrupted",
            error_code="process_interrupted", error_message="Scriber restarted during capture.",
        )
        recovered = await client.post(f"/api/meetings/{meeting['id']}/resume")
        assert recovered.status == 200
        recovered_payload = await recovered.json()
        assert recovered_payload["state"] == "recording"
        assert recovered_payload["errorMessage"] == ""
        assert recovered_payload["captureMetadata"]["deviceSelection"]["microphoneNativeEndpointIdHash"] == "mic-hash"
        assert controller._audio_admission_store.active().owner_id == meeting["id"]
        recovered_detail = store.detail(meeting["id"])
        assert recovered_detail["audioGaps"][-1]["reason"] == "crash-recovery"
        assert recovered_detail["captureMetadata"]["timelineOffsetMs"] == recovered_detail["audioGaps"][-1]["endedAtMs"]
        assert recovered_detail["captureMetadata"]["timelineStartedAtUtc"]
        assert meeting["id"] in controller.capture_watchdogs
        store.transition(meeting["id"], "interrupted")
        insecure_preview = await client.post(
            f"/api/meetings/{meeting['id']}/deliveries/preview",
            json={"url": "http://127.0.0.1/hook"},
        )
        assert insecure_preview.status == 400
        unconfirmed = await client.post(
            f"/api/meetings/{meeting['id']}/deliveries",
            json={"url": "https://example.com/hook", "confirmed": False},
        )
        assert unconfirmed.status == 409

        async def public_dns(_loop, host, port, **_kwargs):
            assert host == "webhook.example"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        async def no_retry_delay(_seconds):
            return None

        monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", public_dns)
        monkeypatch.setattr(web_api.asyncio, "sleep", no_retry_delay)
        preview = await client.post(
            f"/api/meetings/{meeting['id']}/deliveries/preview",
            json={"url": "https://webhook.example/hook?token=not-stored"},
        )
        assert preview.status == 200
        preview_payload = await preview.json()
        assert preview_payload["target"] == "https://webhook.example/hook"
        assert "embedding" not in str(preview_payload).lower()

        fake_session = FakeWebhookSession([500, 429, 204])
        monkeypatch.setattr(
            web_api.ClientSession,
            "post",
            lambda _session, url, **kwargs: fake_session.post(url, **kwargs),
        )
        delivered = await client.post(
            f"/api/meetings/{meeting['id']}/deliveries",
            json={
                "url": "https://webhook.example/hook?token=not-stored",
                "confirmed": True,
                "previewHash": preview_payload["previewHash"],
                "secret": "webhook-signing-secret",
            },
        )
        assert delivered.status == 201
        delivery = (await delivered.json())["delivery"]
        assert delivery["status"] == "delivered"
        assert delivery["attemptCount"] == 3
        assert delivery["target"] == "https://webhook.example/hook"
        assert "not-stored" not in str(delivery)
        assert "webhook-signing-secret" not in str(delivery)
        assert len(fake_session.calls) == 3
        delivery_ids = set()
        for url, kwargs in fake_session.calls:
            assert url == "https://webhook.example/hook?token=not-stored"
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"]["X-Scriber-Signature"].startswith("sha256=")
            delivery_ids.add(kwargs["headers"]["Idempotency-Key"])
        assert delivery_ids == {delivery["id"]}

        deleted = await client.delete(f"/api/meetings/{meeting['id']}")
        assert deleted.status == 200
        assert (await deleted.json())["success"] is True
        missing = await client.get(f"/api/meetings/{meeting['id']}")
        assert missing.status == 404
        assert (await (await client.get("/api/meetings")).json())["total"] == 0
    finally:
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_transcript_correction_api_is_versioned_and_broadcasts(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting = store.create(MeetingCreate(title="Correction review", consent_confirmed=True))
    store.replace_segments(meeting["id"], "canonical", [{
        "id": "editable-segment", "revision": "canonical", "source": "system",
        "speakerLabel": "Remote", "startMs": 100, "endMs": 900,
        "text": "Launch on Thorsday", "isFinal": True,
    }])
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "ready")
    store.save_output(meeting["id"], kind="analysis", payload={"actionItems": []})
    controller = FakeController(store)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        edited = await client.patch(
            f"/api/meetings/{meeting['id']}/segments/editable-segment",
            json={"text": "Launch on Thursday", "expectedEditVersion": 0},
        )
        assert edited.status == 200
        payload = await edited.json()
        assert payload["segment"]["text"] == "Launch on Thursday"
        assert payload["transcriptEditVersion"] == 1
        assert payload["outputsStale"] is True
        assert controller.events[-1]["type"] == "meeting_transcript_edited"

        conflict = await client.patch(
            f"/api/meetings/{meeting['id']}/segments/editable-segment",
            json={"text": "Launch next Thursday", "expectedEditVersion": 0},
        )
        assert conflict.status == 409

        history = await client.get(
            f"/api/meetings/{meeting['id']}/segments/editable-segment/edits"
        )
        assert history.status == 200
        assert (await history.json())["items"][0]["text"] == "Launch on Thursday"

        undone = await client.post(
            f"/api/meetings/{meeting['id']}/segments/editable-segment/undo",
            json={"expectedEditVersion": 1},
        )
        assert undone.status == 200
        assert (await undone.json())["segment"]["text"] == "Launch on Thorsday"
    finally:
        await client.close()
        database._close_all_connections()


@pytest.mark.asyncio
async def test_meeting_audio_range_requires_auth_and_exports_exclude_voiceprints(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "meeting-secret-token")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    meeting = store.create(MeetingCreate(title="Secure meeting", consent_confirmed=True))
    meeting = store.transition(
        meeting["id"],
        "recording",
        capture_metadata={
            "calendarEvent": {
                "participants": [
                    {"name": "Morgan Example", "address": "morgan@example.com"},
                ]
            }
        },
    )
    segment = store.add_segments(meeting["id"], [{
        "id": "segment-secure", "revision": "canonical", "source": "system",
        "speakerLabel": "Remote", "startMs": 0, "endMs": 1000,
        "text": "Public transcript content", "isFinal": True,
    }])[0]
    store.register_speaker_embedding(
        meeting["id"], segment["speakerId"], segment["id"], [0.0] * 255 + [1.0]
    )
    final_dir = tmp_path / "meetings" / meeting["id"] / "final"
    final_dir.mkdir(parents=True)
    audio_bytes = b"OggS-secure-meeting-audio"
    (final_dir / "playback.opus").write_bytes(audio_bytes)
    controller = FakeController(store)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        denied = await client.get(f"/api/meetings/{meeting['id']}/audio")
        assert denied.status == 401

        search_denied = await client.get(
            f"/api/meetings/{meeting['id']}/search?q=Public"
        )
        assert search_denied.status == 401

        search = await client.get(
            f"/api/meetings/{meeting['id']}/search?q=Public",
            headers={"X-Scriber-Token": "meeting-secret-token"},
        )
        assert search.status == 200
        search_payload = await search.json()
        assert search_payload["query"] == "Public"
        assert search_payload["items"][0]["id"] == "segment-secure"
        assert search_payload["items"][0]["durationMs"] == 1000

        headers = {"X-Scriber-Token": "meeting-secret-token", "Range": "bytes=5-10"}
        ranged = await client.get(f"/api/meetings/{meeting['id']}/audio", headers=headers)
        assert ranged.status == 206
        assert await ranged.read() == audio_bytes[5:11]
        assert ranged.headers["Cache-Control"] == "private, no-store"
        assert ranged.headers["Accept-Ranges"] == "bytes"
        assert ranged.headers["Content-Type"].startswith("audio/")

        traversal = await client.get(
            f"/api/meetings/{meeting['id']}/audio/%2e%2e", headers={"X-Scriber-Token": "meeting-secret-token"}
        )
        assert traversal.status == 404

        exported = await client.get(
            f"/api/meetings/{meeting['id']}/export/json",
            headers={"X-Scriber-Token": "meeting-secret-token"},
        )
        assert exported.status == 200
        export_text = (await exported.read()).decode("utf-8")
        assert "Public transcript content" in export_text
        assert "embedding_blob" not in export_text
        assert "embeddingBlob" not in export_text
        assert "speaker_profile_observations" not in export_text

        email_preview = await client.get(
            f"/api/meetings/{meeting['id']}/email-preview",
            headers={"X-Scriber-Token": "meeting-secret-token"},
        )
        assert email_preview.status == 200
        preview_payload = await email_preview.json()
        assert preview_payload["recipients"] == [
            {"name": "Morgan Example", "address": "morgan@example.com"}
        ]
        assert preview_payload["subject"] == "Meeting follow-up: Secure meeting"
        assert "Public transcript content" not in preview_payload["body"]

        email_export = await client.get(
            f"/api/meetings/{meeting['id']}/export-email?attachment=md",
            headers={"X-Scriber-Token": "meeting-secret-token"},
        )
        assert email_export.status == 200
        assert email_export.headers["Content-Type"].startswith("message/rfc822")
        message = BytesParser(policy=policy.default).parsebytes(await email_export.read())
        assert "morgan@example.com" in str(message["To"])
        attachments = list(message.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == "Secure meeting.md"
        assert "Public transcript content" in attachments[0].get_content()
        assert "Attached: Secure meeting.md" in message.get_body(preferencelist=("plain",)).get_content()
    finally:
        await client.close()
        database._close_all_connections()


class _EnrollmentStore:
    def __init__(self):
        self.enrollments = []

    @staticmethod
    def active():
        return None

    @staticmethod
    def speaker_profiles():
        return []

    def enroll_speaker_profile(
        self, display_name, embedding, *, quality, profile_id=""
    ):
        self.enrollments.append(
            {
                "displayName": display_name,
                "embedding": embedding,
                "quality": quality,
                "profileId": profile_id,
            }
        )
        return {
            "id": "profile-enrolled",
            "displayName": display_name,
            "sampleCount": 1,
            "isNamed": True,
            "enrolled": True,
            "enrollmentSampleCount": 1,
            "enrolledAt": "2026-07-13T12:00:00+00:00",
            "createdAt": "2026-07-13T12:00:00+00:00",
            "updatedAt": "2026-07-13T12:00:00+00:00",
        }


class _EnrollmentModel:
    def __init__(self, *, installed=True, fail=False):
        self.installed = installed
        self.fail = fail
        self.samples = []

    def status(self):
        return {"installed": self.installed}

    async def extract_pcm16(self, pcm, *, sample_rate):
        self.samples.append((pcm, sample_rate))
        if self.fail:
            raise RuntimeError("inference failed")
        return [1.0] + [0.0] * 255


class _EnrollmentController:
    def __init__(self, *, installed=True, fail=False):
        self._meeting_store = _EnrollmentStore()
        self._speaker_model = _EnrollmentModel(installed=installed, fail=fail)
        self._meeting_device_test_active = False
        self._voice_enrollment_active = False
        self._is_listening = False
        self._is_stopping = False
        self.prewarm_paused = False
        self.state_broadcasts = []

    def get_state(self):
        return {
            "listening": self._is_listening,
            "voiceEnrollmentActive": self._voice_enrollment_active,
            "status": "Stopped",
            "inputWarning": "",
            "inputWarningCode": "",
            "inputWarningActions": [],
            "current": None,
            "sessionId": None,
            "backgroundProcessing": False,
            "recordingState": "idle",
            "transcribing": False,
        }

    async def broadcast(self, payload):
        self.state_broadcasts.append(payload)

    async def _pause_idle_mic_prewarm_for_capture(self):
        self.prewarm_paused = True

    def _resume_idle_mic_prewarm_after_capture(self):
        self.prewarm_paused = False


@pytest.mark.asyncio
async def test_voice_enrollment_api_gates_opt_in_model_and_active_audio(monkeypatch):
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)

    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    request = _DirectRequest(app, payload={"displayName": "Alice"})

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", False)
    response = await handler(request)
    assert response.status == 409
    assert "Turn on Voice Library" in json.loads(response.body)["message"]

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    controller._speaker_model.installed = False
    response = await handler(request)
    assert response.status == 409
    assert "Download the local voice recognition model" in json.loads(response.body)[
        "message"
    ]

    controller._speaker_model.installed = True
    controller._is_listening = True
    response = await handler(request)
    assert response.status == 409
    assert "Stop Live Mic" in json.loads(response.body)["message"]
    assert controller._voice_enrollment_active is False


@pytest.mark.asyncio
async def test_voice_enrollment_transport_failure_is_actionable_and_redacted(monkeypatch):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    claim = object()
    released = []

    async def claim_audio(_controller, **_kwargs):
        return claim

    async def release_audio(_controller, released_claim):
        released.append(released_claim)
        return True

    def shell_call(command, _payload, **_kwargs):
        assert command == "audioCaptureStart"
        return {
            "success": False,
            "errorCode": "transportError",
            "fallbackReason": "RuntimeError: OSError: [Errno 121] WaitNamedPipeW failed",
            "payload": {},
        }

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(
        _DirectRequest(app, payload={"displayName": "Alice"})
    )
    payload = json.loads(response.body)

    assert response.status == 503
    assert payload == {
        "message": (
            "Scriber's microphone service was temporarily busy. "
            "Wait a moment and try the sample again."
        )
    }
    assert "WaitNamedPipe" not in json.dumps(payload)
    assert released == [claim]
    assert controller._voice_enrollment_active is False
    assert controller.prewarm_paused is False


@pytest.mark.asyncio
async def test_voice_enrollment_api_is_local_private_and_cleans_up(monkeypatch):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    shell_calls = []
    released = []
    captures = []
    claim = object()

    class Capture:
        def __init__(self, *, sample_rate, max_duration_seconds):
            assert sample_rate == 16_000
            assert max_duration_seconds == 9.0
            self.started_with = ""
            self.cleared = False
            captures.append(self)

        def start(self, frame_pipe):
            self.started_with = frame_pipe

        @staticmethod
        def stop():
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0,
            }

        @staticmethod
        def pcm16():
            return b"private-pcm"

        def clear(self):
            self.cleared = True

    def shell_call(command, payload, **_kwargs):
        shell_calls.append((command, payload))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "private-stream",
                    "framePipe": "private-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        assert command == "audioCaptureStop"
        assert payload == {"streamId": "private-stream"}
        return {"success": True, "payload": {"stopped": True}}

    async def no_wait(duration_ms):
        assert duration_ms == 8_000

    async def claim_audio(_controller, **kwargs):
        assert kwargs["owner_kind"] == "voice_enrollment"
        assert kwargs["heartbeat"] is False
        return claim

    async def release_audio(_controller, released_claim):
        released.append(released_claim)
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "VoiceEnrollmentCapture", Capture)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_wait_for_voice_enrollment", no_wait)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(
        _DirectRequest(
            app,
            payload={
                "displayName": "  Alice   Example ",
                "durationMs": 8_000,
                "microphoneNativeEndpointIdHash": "abcdef0123456789",
            },
        )
    )
    payload = json.loads(response.body)

    assert response.status == 201
    assert payload["profile"]["displayName"] == "Alice Example"
    assert payload["capture"] == {
        "durationMs": 8_000,
        "rms": 0.1,
        "peak": 0.4,
        "quality": 0.956,
    }
    assert payload["audioPersisted"] is False
    assert payload["audioSentToProvider"] is False
    serialized = json.dumps(payload).lower()
    assert "private-pcm" not in serialized
    assert "private-pipe" not in serialized
    assert "private-stream" not in serialized
    assert "embedding" not in serialized
    assert shell_calls == [
        (
            "audioCaptureStart",
            {
                "sampleRate": 16_000,
                "channels": 1,
                "blockSize": 512,
                "devicePreference": "default",
                "nativeEndpointIdHash": "abcdef0123456789",
                "prebufferMs": 0,
            },
        ),
        ("audioCaptureStop", {"streamId": "private-stream"}),
    ]
    assert controller._speaker_model.samples == [(b"private-pcm", 16_000)]
    assert controller._meeting_store.enrollments[0]["displayName"] == "Alice Example"
    assert controller._meeting_store.enrollments[0]["embedding"] == [1.0] + [0.0] * 255
    assert captures[0].started_with == "private-pipe"
    assert captures[0].cleared is True
    assert released == [claim]
    assert controller._voice_enrollment_active is False
    assert controller.prewarm_paused is False
    assert [item["voiceEnrollmentActive"] for item in controller.state_broadcasts] == [
        True,
        False,
    ]
    assert all(item["type"] == "state" for item in controller.state_broadcasts)
    assert all(item["apiVersion"] == "1" for item in controller.state_broadcasts)


@pytest.mark.asyncio
async def test_voice_enrollment_cancellation_after_claim_releases_owned_lease(
    monkeypatch,
):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    claim_started = asyncio.Event()
    finish_claim = asyncio.Event()
    claim = object()
    released = []

    async def delayed_claim(_controller, **_kwargs):
        claim_started.set()
        await finish_claim.wait()
        return claim

    async def release_audio(_controller, released_claim):
        released.append(released_claim)
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", delayed_claim)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    task = asyncio.create_task(
        handler(_DirectRequest(app, payload={"displayName": "Alice"}))
    )
    await asyncio.wait_for(claim_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    finish_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert released == [claim]
    assert controller._voice_enrollment_active is False
    assert controller.prewarm_paused is False


@pytest.mark.asyncio
async def test_voice_enrollment_retains_ownership_when_shell_stop_is_unconfirmed(
    monkeypatch,
):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    stop_calls = []
    released = []
    captures = []
    claim = object()

    class Capture:
        def __init__(self, **_kwargs):
            self.cleared = False
            captures.append(self)

        @staticmethod
        def start(_frame_pipe):
            return None

        @staticmethod
        def stop():
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0,
            }

        def clear(self):
            self.cleared = True

    def shell_call(command, _payload, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "uncertain-stream",
                    "framePipe": "private-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        stop_calls.append(command)
        return {
            "success": False,
            "errorCode": "transportError",
            "payload": {},
        }

    async def no_wait(_duration_ms):
        return None

    async def claim_audio(_controller, **_kwargs):
        return claim

    async def release_audio(_controller, released_claim):
        released.append(released_claim)
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "VoiceEnrollmentCapture", Capture)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_wait_for_voice_enrollment", no_wait)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(
        _DirectRequest(app, payload={"displayName": "Alice"})
    )

    assert response.status == 503
    # The normal stop and the cleanup retry both failed at the IPC boundary.
    assert stop_calls == ["audioCaptureStop", "audioCaptureStop"]
    assert captures[0].cleared is True
    assert released == []
    assert controller._voice_enrollment_active is True
    assert controller.prewarm_paused is True


@pytest.mark.asyncio
async def test_voice_enrollment_rejects_unexpected_native_audio_format(monkeypatch):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    shell_calls = []

    def shell_call(command, payload, **_kwargs):
        shell_calls.append((command, payload))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "wrong-format-stream",
                    "framePipe": "private-pipe",
                    "sampleRate": 48_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        return {"success": True, "payload": {"stopped": True}}

    async def claim_audio(_controller, **_kwargs):
        return object()

    async def release_audio(_controller, _claim):
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(
        _DirectRequest(app, payload={"displayName": "Alice"})
    )

    assert response.status == 503
    assert "unsupported audio format" in json.loads(response.body)["message"]
    assert [item[0] for item in shell_calls] == [
        "audioCaptureStart",
        "audioCaptureStop",
    ]
    assert controller._meeting_store.enrollments == []
    assert controller._speaker_model.samples == []
    assert controller._voice_enrollment_active is False


@pytest.mark.asyncio
async def test_voice_enrollment_api_cleans_up_after_inference_failure(monkeypatch):
    controller = _EnrollmentController(fail=True)
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    stopped = []
    released = []
    captures = []

    class Capture:
        cleared = False

        def __init__(self, **_kwargs):
            captures.append(self)

        @staticmethod
        def start(_frame_pipe):
            pass

        @staticmethod
        def stop():
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0,
            }

        @staticmethod
        def pcm16():
            return b"private-pcm"

        def clear(self):
            self.cleared = True

    def shell_call(command, _payload, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream",
                    "framePipe": "pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        stopped.append(command)
        return {"success": True, "payload": {"stopped": True}}

    async def no_wait(_duration_ms):
        return None

    async def claim_audio(_controller, **_kwargs):
        return object()

    async def release_audio(_controller, released_claim):
        released.append(released_claim)
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "VoiceEnrollmentCapture", Capture)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_wait_for_voice_enrollment", no_wait)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(_DirectRequest(app, payload={"displayName": "Alice"}))

    assert response.status == 503
    assert json.loads(response.body) == {
        "message": "The voice sample could not be completed. Try again."
    }
    assert stopped == ["audioCaptureStop"]
    assert released
    assert captures[0].cleared is True
    assert controller._voice_enrollment_active is False
    assert controller.prewarm_paused is False
    assert [item["voiceEnrollmentActive"] for item in controller.state_broadcasts] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_voice_enrollment_does_not_recreate_data_after_opt_out(monkeypatch):
    controller = _EnrollmentController()
    app = web_api.create_app(controller)
    handler = _route_handler(app, "POST", "/api/meetings/speaker-profiles/enroll")
    stopped = []

    class Capture:
        def __init__(self, **_kwargs):
            self.cleared = False

        @staticmethod
        def start(_frame_pipe):
            return None

        @staticmethod
        def stop():
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0,
            }

        @staticmethod
        def pcm16():
            return b"private-pcm"

        def clear(self):
            self.cleared = True

    def shell_call(command, _payload, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream",
                    "framePipe": "pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        stopped.append(command)
        return {"success": True, "payload": {"stopped": True}}

    async def opt_out_while_recording(_duration_ms):
        monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", False)

    async def claim_audio(_controller, **_kwargs):
        return object()

    async def release_audio(_controller, _claim):
        return True

    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(web_api, "VoiceEnrollmentCapture", Capture)
    monkeypatch.setattr(web_api, "call_shell_ipc", shell_call)
    monkeypatch.setattr(web_api, "_wait_for_voice_enrollment", opt_out_while_recording)
    monkeypatch.setattr(web_api, "_claim_persistent_audio", claim_audio)
    monkeypatch.setattr(web_api, "_release_persistent_audio", release_audio)

    response = await handler(_DirectRequest(app, payload={"displayName": "Alice"}))

    assert response.status == 409
    assert "turned off" in json.loads(response.body)["message"]
    assert controller._speaker_model.samples == []
    assert controller._meeting_store.enrollments == []
    assert stopped == ["audioCaptureStop"]
    assert controller._voice_enrollment_active is False
    assert controller.prewarm_paused is False


@pytest.mark.asyncio
async def test_voice_library_delete_waits_for_profile_mutation_lock(monkeypatch):
    operations = []

    class Store(_EnrollmentStore):
        @staticmethod
        def delete_all_speaker_profiles():
            operations.append("profiles")
            return 2

    class Model(_EnrollmentModel):
        @staticmethod
        def delete():
            operations.append("model")

    controller = _EnrollmentController()
    controller._meeting_store = Store()
    controller._speaker_model = Model()
    controller._schedule_settings_persist = lambda: operations.append("settings")
    app = web_api.create_app(controller)
    handler = _route_handler(app, "DELETE", "/api/meetings/speaker-library")
    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)

    mutation_lock = web_api._voice_library_mutation_lock(controller)
    await mutation_lock.acquire()
    task = asyncio.create_task(handler(_DirectRequest(app)))
    await asyncio.sleep(0)
    assert task.done() is False

    mutation_lock.release()
    response = await task

    assert response.status == 200
    assert json.loads(response.body)["deletedProfiles"] == 2
    assert operations == ["profiles", "model", "settings"]
    assert web_api.Config.VOICEPRINT_LIBRARY_OPT_IN is False


@pytest.mark.asyncio
async def test_voice_model_download_cannot_restore_model_after_library_delete(
    monkeypatch,
):
    download_started = asyncio.Event()
    finish_download = asyncio.Event()
    operations = []

    class Store(_EnrollmentStore):
        enabled = True

        @classmethod
        def speaker_library_enabled(cls):
            return cls.enabled

        @classmethod
        def delete_all_speaker_profiles(cls):
            cls.enabled = False
            operations.append("profiles-deleted")
            return 0

    class Model(_EnrollmentModel):
        async def stage_download(self, _session):
            operations.append("download-started")
            download_started.set()
            await finish_download.wait()
            operations.append("download-staged")
            return object()

        @staticmethod
        def promote_staged(_staged):
            operations.append("promoted")
            return {"installed": True}

        @staticmethod
        def discard_staged(_staged):
            operations.append("staging-discarded")

        @staticmethod
        def delete():
            operations.append("model-deleted")

    controller = _EnrollmentController()
    controller._meeting_store = Store()
    controller._speaker_model = Model()
    controller._schedule_settings_persist = lambda: operations.append(
        "settings-scheduled"
    )
    app = web_api.create_app(controller)
    app[web_api.APP_HTTP_SESSION] = object()
    download_handler = _route_handler(
        app, "POST", "/api/meetings/speaker-model"
    )
    delete_handler = _route_handler(
        app, "DELETE", "/api/meetings/speaker-library"
    )
    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)

    download_task = asyncio.create_task(
        download_handler(_DirectRequest(app))
    )
    await asyncio.wait_for(download_started.wait(), timeout=2)
    delete_response = await asyncio.wait_for(
        delete_handler(_DirectRequest(app)), timeout=2
    )
    finish_download.set()
    download_response = await asyncio.wait_for(download_task, timeout=2)

    assert delete_response.status == 200
    assert download_response.status == 409
    assert "turned off" in json.loads(download_response.body)["message"]
    assert "promoted" not in operations
    assert operations == [
        "download-started",
        "profiles-deleted",
        "model-deleted",
        "settings-scheduled",
        "download-staged",
        "staging-discarded",
    ]


@pytest.mark.asyncio
async def test_voice_model_promotion_cancellation_finishes_durable_opt_out_cleanup(
    monkeypatch,
):
    promotion_started = threading.Event()
    finish_promotion = threading.Event()
    operations = []

    class Store(_EnrollmentStore):
        enabled = True

        @classmethod
        def speaker_library_enabled(cls):
            return cls.enabled

    class Model(_EnrollmentModel):
        installed = False

        async def stage_download(self, _session):
            operations.append("staged")
            return object()

        @classmethod
        def promote_staged(cls, _staged):
            operations.append("promotion-started")
            promotion_started.set()
            assert finish_promotion.wait(timeout=2)
            cls.installed = True
            operations.append("promoted")
            return {"installed": True}

        @staticmethod
        def discard_staged(_staged):
            operations.append("staging-discarded")

        @classmethod
        def delete(cls):
            cls.installed = False
            operations.append("model-deleted")

    controller = _EnrollmentController()
    controller._meeting_store = Store()
    controller._speaker_model = Model()
    app = web_api.create_app(controller)
    app[web_api.APP_HTTP_SESSION] = object()
    handler = _route_handler(app, "POST", "/api/meetings/speaker-model")
    monkeypatch.setattr(web_api.Config, "VOICEPRINT_LIBRARY_OPT_IN", True)

    task = asyncio.create_task(handler(_DirectRequest(app)))
    assert await asyncio.to_thread(promotion_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    # Simulate an opt-out committed by another process while the atomic model
    # replacement is still running in the executor.
    Store.enabled = False
    finish_promotion.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert Model.installed is False
    assert operations == [
        "staged",
        "promotion-started",
        "promoted",
        "model-deleted",
    ]
