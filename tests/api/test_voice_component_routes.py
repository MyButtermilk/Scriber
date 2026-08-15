"""Voice component routes exercised without web_api.create_app.

The interesting behaviour here is the biometric opt-in: it is durable and
cross-process, so it can be withdrawn while a download is mid-flight. Most of
these tests are about what the routes leave behind when that happens.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src import database
from src.api import voice_component_routes
from src.api.app_keys import APP_HTTP_SESSION
from src.api.voice_component_routes import (
    APP_VOICE_COMPONENT_STATE,
    DiarizerPort,
    SpeakerModelPort,
    SpeakerProfilePreviewGrant,
    VoiceCaptureRuntime,
    VoiceEnrollmentAdmission,
    VoiceEnrollmentAdmissionPort,
    VoiceEnrollmentLossHandler,
    VoiceLibraryDeps,
    VoiceLibraryStorePort,
    download_speaker_model,
    patch_speaker_profile,
    register_voice_component_routes,
)
from src.config import Config
from src.data.meeting_store import MeetingCreate, MeetingNotFound, MeetingStore, VoiceLibraryDisabled


class _Model:
    def __init__(self) -> None:
        self.installed = False
        self.operations: list[str] = []
        self.stage_error: Exception | None = None
        self.promote_hook: Any = None
        self.samples: list[tuple[bytes, int]] = []

    def status(self) -> dict[str, Any]:
        return {"installed": self.installed}

    async def stage_download(self, session: Any) -> object:
        if self.stage_error is not None:
            raise self.stage_error
        self.operations.append("staged")
        return object()

    def promote_staged(self, _staged: object) -> dict[str, Any]:
        if self.promote_hook is not None:
            self.promote_hook()
        self.installed = True
        self.operations.append("promoted")
        return {"installed": True}

    def discard_staged(self, _staged: object) -> None:
        self.operations.append("staging-discarded")

    def delete(self) -> None:
        self.installed = False
        self.operations.append("model-deleted")

    async def extract_pcm16(self, pcm: bytes, *, sample_rate: int = 16_000) -> list[float]:
        self.samples.append((pcm, sample_rate))
        return [1.0] + [0.0] * 255


class _Store:
    def __init__(self, *, enabled: bool = True) -> None:
        self.deleted_profiles = 0
        self.enabled = enabled
        self.profiles = [
            {
                "id": "profile-alice",
                "displayName": "Alice",
                "sampleCount": 2,
                "isNamed": True,
            }
        ]
        self.enrollments: list[dict[str, Any]] = []
        self.merges: list[tuple[str, str]] = []
        self.splits: list[tuple[str, str]] = []
        self.preview_candidates: dict[str, dict[str, Any]] = {}
        self.stored_previews: dict[str, dict[str, Any]] = {}
        self.saved_previews: list[tuple[str, bytes]] = []

    def delete_all_speaker_profiles(self) -> int:
        self.deleted_profiles = 2
        return 2

    def speaker_library_enabled(self) -> bool:
        return self.enabled

    def speaker_profiles(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.profiles]

    def speaker_profile_preview_candidates(self) -> dict[str, dict[str, Any]]:
        return dict(self.preview_candidates)

    def speaker_profile_previews(self) -> dict[str, dict[str, Any]]:
        return dict(self.stored_previews)

    def speaker_profile_preview(self, profile_id: str) -> dict[str, Any] | None:
        preview = self.stored_previews.get(profile_id)
        return dict(preview) if preview is not None else None

    def save_speaker_profile_preview(
        self,
        profile_id: str,
        audio: bytes,
        *,
        duration_ms: int,
        source: str,
        replace: bool = False,
    ) -> bool:
        del duration_ms, source, replace
        if not self.enabled:
            raise VoiceLibraryDisabled("Voice Library is turned off.")
        self.saved_previews.append((profile_id, audio))
        return True

    def enroll_speaker_profile(
        self,
        display_name: str,
        embedding: list[float],
        *,
        quality: float,
        profile_id: str = "",
        preview_audio: bytes | None = None,
        preview_duration_ms: int = 0,
        preview_source: str = "",
    ) -> dict[str, Any]:
        self.enrollments.append(
            {
                "displayName": display_name,
                "embedding": embedding,
                "quality": quality,
                "profileId": profile_id,
                "previewAudio": preview_audio,
                "previewDurationMs": preview_duration_ms,
                "previewSource": preview_source,
            }
        )
        return {
            "id": "profile-enrolled",
            "displayName": display_name,
            "embedding": embedding,
        }

    def delete_speaker_profile(self, profile_id: str) -> bool:
        before = len(self.profiles)
        self.profiles = [item for item in self.profiles if item["id"] != profile_id]
        return len(self.profiles) != before

    def rename_speaker_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        name = " ".join(display_name.split()).strip()
        for item in self.profiles:
            if item["id"] == profile_id:
                item["displayName"] = name
                return {"id": profile_id, "displayName": name}
        raise MeetingNotFound("Speaker profile not found")

    def merge_speaker_profiles(self, target_profile_id: str, source_profile_id: str) -> dict[str, Any]:
        self.merges.append((target_profile_id, source_profile_id))
        return {"targetProfileId": target_profile_id, "sourceProfileId": source_profile_id}

    def split_speaker_profile(self, meeting_id: str, speaker_id: str) -> dict[str, Any]:
        self.splits.append((meeting_id, speaker_id))
        return {"meetingId": meeting_id, "speakerId": speaker_id, "split": True}


class _Diarizer:
    def __init__(self) -> None:
        self.installed = False
        self.busy = False
        self.install_error: Exception | None = None

    async def status_async(self, *, force: bool = False) -> dict[str, Any]:
        assert force is False
        return {"installed": self.installed}

    async def install(self, session: Any) -> dict[str, Any]:
        if self.install_error is not None:
            raise self.install_error
        self.installed = True
        return {"installed": True}

    async def delete_async(self) -> bool:
        if self.busy:
            return False
        self.installed = False
        return True


class _EnrollmentAdmission:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.prepared = 0
        self.released: list[tuple[object, bool]] = []
        self.loss_handler: VoiceEnrollmentLossHandler | None = None
        self.events: list[str] = []

    async def acquire(
        self,
        *,
        owner_id: str,
        loss_handler: VoiceEnrollmentLossHandler,
    ) -> VoiceEnrollmentAdmission:
        self.acquired.append(owner_id)
        self.loss_handler = loss_handler
        return VoiceEnrollmentAdmission(claim="local-test-claim")  # type: ignore[arg-type]

    async def lose(self, reason: str = "lease-expired") -> None:
        assert self.loss_handler is not None
        await self.loss_handler(reason)

    async def prepare_capture(self) -> None:
        self.prepared += 1

    async def release(
        self,
        admission: VoiceEnrollmentAdmission,
        *,
        native_capture_released: bool,
    ) -> None:
        self.events.append("admission-release")
        self.released.append((admission.claim, native_capture_released))


class _Harness:
    def __init__(self, *, diarizer: _Diarizer | None = None, store: Any = None) -> None:
        self.model = _Model()
        self.store = store if store is not None else _Store()
        self.diarizer = diarizer if diarizer is not None else _Diarizer()
        self.enrollment = _EnrollmentAdmission()
        self.persisted = 0

    def voice_library(self) -> VoiceLibraryDeps:
        return VoiceLibraryDeps(
            speaker_model=self.model,
            meeting_store=self.store,
            persist_settings=self._persist,
        )

    def _persist(self) -> None:
        self.persisted += 1


@pytest.fixture
def harness():
    return _Harness()


@pytest.fixture
def sqlite_voice_store(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "voice-routes.db")
    database.init_database()
    store = MeetingStore()
    store.initialize()
    yield store
    database._close_all_connections()


@pytest.fixture(autouse=True)
def opted_in(monkeypatch):
    """Consent is on unless a test turns it off; most cases are about losing it."""
    monkeypatch.setattr(Config, "VOICEPRINT_LIBRARY_OPT_IN", True, raising=False)
    monkeypatch.setattr(Config, "SPEAKER_DIARIZATION_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "set_voiceprint_library_opt_in", lambda _value: None, raising=False)


async def _client(
    harness: _Harness,
    *,
    capture_runtime: VoiceCaptureRuntime | None = None,
) -> TestClient:
    app = web.Application()
    register_voice_component_routes(
        app,
        voice_library=harness.voice_library,
        enrollment=lambda: harness.enrollment,
        diarizer=lambda: harness.diarizer,
        capture_runtime=capture_runtime,
    )
    app[APP_HTTP_SESSION] = object()
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_model_status_reports_the_opt_in_beside_the_install_state(harness):
    client = await _client(harness)
    try:
        body = await (await client.get("/api/meetings/speaker-model")).json()
        assert body["optedIn"] is True
        assert body["installed"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_model_status_reports_the_durable_cross_process_opt_out(harness):
    harness.store.enabled = False
    client = await _client(harness)
    try:
        body = await (await client.get("/api/meetings/speaker-model")).json()
        assert body["optedIn"] is False
    finally:
        await client.close()


@pytest.mark.parametrize("store_failure_type", [OSError, sqlite3.OperationalError])
@pytest.mark.asyncio
async def test_model_status_redacts_a_durable_gate_read_failure(
    sqlite_voice_store,
    monkeypatch,
    store_failure_type,
):
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise store_failure_type("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as gate_failure:
            gate_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.get("/api/meetings/speaker-model")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library consent could not be confirmed."
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/meetings/speaker-model", None),
        ("get", "/api/meetings/speaker-profiles", None),
        (
            "post",
            "/api/meetings/speaker-profiles/enroll",
            {"displayName": "Alice", "durationMs": 8_000},
        ),
    ],
)
@pytest.mark.asyncio
async def test_voice_routes_redact_an_unknown_model_status_failure(harness, monkeypatch, method, path, payload):
    def fail_model_status():
        raise RuntimeError("C:\\private\\speaker-model.bin")

    monkeypatch.setattr(harness.model, "status", fail_model_status)
    client = await _client(harness)
    try:
        response = (
            await getattr(client, method)(path, json=payload)
            if payload is not None
            else await getattr(client, method)(path)
        )
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library is temporarily unavailable."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_collection_returns_no_profiles_or_preview_grants_after_durable_opt_out(harness):
    harness.model.installed = True
    harness.store.enabled = False
    harness.store.stored_previews["profile-alice"] = {
        "source": "enrollment",
        "durationMs": 2_000,
    }
    client = await _client(harness)
    try:
        response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 200
        body = await response.json()
        assert body["items"] == []
        assert body["enabled"] is False
        assert client.app[APP_VOICE_COMPONENT_STATE].preview_grants == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_collection_redacts_a_durable_gate_read_failure(sqlite_voice_store, monkeypatch):
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as gate_failure:
            gate_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library consent could not be confirmed."
    finally:
        await client.close()


@pytest.mark.parametrize(
    "store_method",
    ["speaker_profiles", "speaker_profile_preview_candidates", "speaker_profile_previews"],
)
@pytest.mark.asyncio
async def test_profile_collection_redacts_unknown_store_reads(harness, monkeypatch, store_method):
    def fail_store_read():
        raise OSError("C:\\private\\voice-library.db")

    monkeypatch.setattr(harness.store, store_method, fail_store_read)
    client = await _client(harness)
    try:
        response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library profile data could not be read."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_collection_discards_new_grants_when_opt_out_lands_during_the_read(harness):
    checks = 0

    def consent_state() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    harness.store.speaker_library_enabled = consent_state
    harness.store.stored_previews["profile-alice"] = {
        "source": "enrollment",
        "durationMs": 2_000,
    }
    client = await _client(harness)
    try:
        response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 200
        body = await response.json()
        assert body["items"] == []
        assert body["enabled"] is False
        assert client.app[APP_VOICE_COMPONENT_STATE].preview_grants == {}
    finally:
        await client.close()

    assert checks == 2


@pytest.mark.asyncio
async def test_profile_collection_redacts_a_failed_durable_gate_recheck(sqlite_voice_store, monkeypatch):
    checks = 0
    durable_gate = sqlite_voice_store.speaker_library_enabled

    def fail_second_gate_read():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise OSError("C:\\private\\voice-library.db")
        return durable_gate()

    monkeypatch.setattr(sqlite_voice_store, "speaker_library_enabled", fail_second_gate_read)
    client = await _client(_Harness(store=sqlite_voice_store))
    try:
        response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library consent could not be confirmed."
        assert client.app[APP_VOICE_COMPONENT_STATE].preview_grants == {}
    finally:
        await client.close()

    assert checks == 2


@pytest.mark.asyncio
async def test_profile_collection_never_returns_an_evicted_preview_capability(harness):
    harness.store.profiles = [
        {
            "id": f"profile-{index:03d}",
            "displayName": f"Speaker {index:03d}",
            "sampleCount": 1,
            "isNamed": False,
        }
        for index in range(300)
    ]
    previews = {
        item["id"]: {
            "source": "enrollment",
            "durationMs": 2_000,
        }
        for item in harness.store.profiles
    }
    harness.store.speaker_profile_previews = lambda: previews
    client = await _client(harness)
    try:
        response = await client.get("/api/meetings/speaker-profiles")
        assert response.status == 200
        body = await response.json()
        returned_tokens = {item["preview"]["token"] for item in body["items"] if item.get("preview") is not None}
        retained_tokens = set(client.app[APP_VOICE_COMPONENT_STATE].preview_grants)
    finally:
        await client.close()

    assert len(returned_tokens) == 256
    assert returned_tokens == retained_tokens


@pytest.mark.asyncio
async def test_stored_preview_rechecks_durable_consent_after_reading_the_blob(harness):
    token = "e" * 32

    def read_then_opt_out(_profile_id: str) -> dict[str, Any]:
        harness.store.enabled = False
        return {"audio": b"RIFF" + (b"\0" * 100)}

    harness.store.speaker_profile_preview = read_then_opt_out
    client = await _client(harness)
    client.app[APP_VOICE_COMPONENT_STATE].preview_grants[token] = SpeakerProfilePreviewGrant(
        profile_id="profile-alice",
        duration_ms=2_000,
        expires_at=float("inf"),
        source="enrollment",
    )
    try:
        response = await client.get(f"/api/meetings/speaker-profile-preview/{token}")
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preview_backfill_rechecks_durable_consent_before_rendering(harness, monkeypatch):
    token = "a" * 32
    profile_id = "profile-alice"
    harness.store.enabled = False
    harness.store.preview_candidates[profile_id] = {
        "profileId": profile_id,
        "meetingId": "b" * 32,
        "source": "system",
        "startMs": 0,
        "durationMs": 4_000,
    }
    rendered = 0

    async def render(_grant: SpeakerProfilePreviewGrant) -> bytes:
        nonlocal rendered
        rendered += 1
        return b"RIFF" + (b"\0" * 100)

    monkeypatch.setattr(voice_component_routes, "_render_speaker_profile_preview", render)
    client = await _client(harness)
    client.app[APP_VOICE_COMPONENT_STATE].preview_grants[token] = SpeakerProfilePreviewGrant(
        profile_id=profile_id,
        duration_ms=4_000,
        expires_at=float("inf"),
        source="system",
        meeting_id="b" * 32,
    )
    try:
        response = await client.get(f"/api/meetings/speaker-profile-preview/{token}")
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()

    assert rendered == 0
    assert harness.store.saved_previews == []


@pytest.mark.asyncio
async def test_preview_backfill_refuses_audio_when_consent_is_withdrawn_during_render(harness, monkeypatch):
    token = "c" * 32
    profile_id = "profile-alice"
    harness.store.preview_candidates[profile_id] = {
        "profileId": profile_id,
        "meetingId": "d" * 32,
        "source": "system",
        "startMs": 0,
        "durationMs": 4_000,
    }

    async def render(_grant: SpeakerProfilePreviewGrant) -> bytes:
        harness.store.enabled = False
        return b"RIFF" + (b"\0" * 100)

    monkeypatch.setattr(voice_component_routes, "_render_speaker_profile_preview", render)
    client = await _client(harness)
    client.app[APP_VOICE_COMPONENT_STATE].preview_grants[token] = SpeakerProfilePreviewGrant(
        profile_id=profile_id,
        duration_ms=4_000,
        expires_at=float("inf"),
        source="system",
        meeting_id="d" * 32,
    )
    try:
        response = await client.get(f"/api/meetings/speaker-profile-preview/{token}")
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.store.saved_previews == []


@pytest.mark.asyncio
async def test_preview_backfill_never_returns_audio_when_the_atomic_consent_check_fails(harness, monkeypatch):
    token = "9" * 32
    profile_id = "profile-alice"
    harness.store.preview_candidates[profile_id] = {
        "profileId": profile_id,
        "meetingId": "8" * 32,
        "source": "system",
        "startMs": 0,
        "durationMs": 4_000,
    }

    async def render(_grant: SpeakerProfilePreviewGrant) -> bytes:
        return b"RIFF" + (b"\0" * 100)

    def fail_save(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("database unavailable")

    monkeypatch.setattr(voice_component_routes, "_render_speaker_profile_preview", render)
    harness.store.save_speaker_profile_preview = fail_save
    client = await _client(harness)
    client.app[APP_VOICE_COMPONENT_STATE].preview_grants[token] = SpeakerProfilePreviewGrant(
        profile_id=profile_id,
        duration_ms=4_000,
        expires_at=float("inf"),
        source="system",
        meeting_id="8" * 32,
    )
    try:
        response = await client.get(f"/api/meetings/speaker-profile-preview/{token}")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert "database unavailable" not in (await response.json())["message"]
        assert (await client.get(f"/api/meetings/speaker-profile-preview/{token}")).status == 404
    finally:
        await client.close()


@pytest.mark.parametrize(
    "failure_point",
    [
        "initial_gate",
        "stored_preview_read",
        "stored_preview_gate_recheck",
        "candidate_read",
        "candidate_gate_recheck",
    ],
)
@pytest.mark.asyncio
async def test_speaker_preview_redacts_unknown_gate_and_store_reads(harness, monkeypatch, failure_point):
    token = "7" * 32
    profile_id = "profile-alice"
    gate_checks = 0
    failed_gate_check = {
        "initial_gate": 1,
        "stored_preview_gate_recheck": 2,
        "candidate_gate_recheck": 2,
    }.get(failure_point)

    def durable_gate():
        nonlocal gate_checks
        gate_checks += 1
        if gate_checks == failed_gate_check:
            raise OSError("C:\\private\\voice-library.db")
        return True

    def fail_store_read(*_args):
        raise OSError("C:\\private\\voice-library.db")

    harness.store.speaker_library_enabled = durable_gate
    if failure_point == "stored_preview_read":
        monkeypatch.setattr(harness.store, "speaker_profile_preview", fail_store_read)
    elif failure_point == "stored_preview_gate_recheck":
        harness.store.stored_previews[profile_id] = {
            "audio": b"RIFF" + (b"\0" * 100),
            "source": "enrollment",
            "durationMs": 4_000,
        }
    elif failure_point == "candidate_read":
        monkeypatch.setattr(harness.store, "speaker_profile_preview_candidates", fail_store_read)
    elif failure_point == "candidate_gate_recheck":
        harness.store.preview_candidates[profile_id] = {
            "profileId": profile_id,
            "meetingId": "8" * 32,
            "source": "system",
            "startMs": 0,
            "durationMs": 4_000,
        }

    client = await _client(harness)
    client.app[APP_VOICE_COMPONENT_STATE].preview_grants[token] = SpeakerProfilePreviewGrant(
        profile_id=profile_id,
        duration_ms=4_000,
        expires_at=float("inf"),
        source="system",
        meeting_id="8" * 32,
    )
    try:
        response = await client.get(f"/api/meetings/speaker-profile-preview/{token}")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())[
            "message"
        ] == "Voice Library consent could not be confirmed for this speaker preview."
        assert token not in client.app[APP_VOICE_COMPONENT_STATE].preview_grants
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_voice_enrollment_runs_end_to_end_through_the_domain_http_boundary(harness):
    """One HTTP request owns capture, persistence, privacy, and lease release."""
    harness.model.installed = True
    shell_calls: list[tuple[str, dict[str, Any]]] = []
    captures: list[Any] = []

    class Capture:
        def __init__(self, *, sample_rate: int, max_duration_seconds: float) -> None:
            assert sample_rate == 16_000
            assert max_duration_seconds == 9.0
            self.cleared = False
            captures.append(self)

        @staticmethod
        def start(frame_pipe: str) -> None:
            assert frame_pipe == "local-frame-pipe"

        @staticmethod
        def stop() -> dict[str, Any]:
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0.0,
            }

        @staticmethod
        def pcm16() -> bytes:
            return b"private-pcm"

        def clear(self) -> None:
            self.cleared = True

    def call_shell(command: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        shell_calls.append((command, payload))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "local-stream",
                    "framePipe": "local-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        return {"success": True, "payload": {"stopped": True}}

    async def no_wait(duration_ms: int) -> None:
        assert duration_ms == 8_000

    runtime = VoiceCaptureRuntime(
        available=lambda: True,
        call=call_shell,
        capture_factory=Capture,
        wait=no_wait,
        reference_wav=lambda pcm, **_kwargs: (
            (b"RIFF" + b"\0" * 60, 4_000)
            if pcm == b"private-pcm"
            else (_ for _ in ()).throw(AssertionError("unexpected PCM"))
        ),
    )
    client = await _client(harness, capture_runtime=runtime)
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "  Alice   Example ", "durationMs": 8_000},
        )
        assert response.status == 201
        body = await response.json()
    finally:
        await client.close()

    assert body["profile"] == {
        "id": "profile-enrolled",
        "displayName": "Alice Example",
        "sampleCount": 0,
        "isNamed": False,
        "enrolled": False,
        "enrollmentSampleCount": 0,
        "enrolledAt": "",
        "createdAt": "",
        "updatedAt": "",
    }
    assert body["audioPersisted"] is True
    assert body["audioSentToProvider"] is False
    serialized = str(body).lower()
    assert "private-pcm" not in serialized
    assert "embedding" not in serialized
    assert len(harness.enrollment.acquired) == 1
    assert harness.enrollment.prepared == 1
    assert harness.enrollment.released == [("local-test-claim", True)]
    assert harness.store.enrollments[0]["displayName"] == "Alice Example"
    assert harness.store.enrollments[0]["previewSource"] == "enrollment"
    assert captures[0].cleared is True
    assert [name for name, _payload in shell_calls] == ["audioCaptureStart", "audioCaptureStop"]


@pytest.mark.asyncio
async def test_voice_enrollment_loss_stops_capture_before_releasing_admission(harness):
    """Lease loss owns remote stop before request cleanup may release admission."""
    harness.model.installed = True
    reader_stop_expected = threading.Event()
    loss_completed = False

    class Capture:
        @staticmethod
        def start(_frame_pipe: str) -> None:
            return None

        @staticmethod
        def stop() -> dict[str, Any]:
            return {"active": False, "durationMs": 0}

        @staticmethod
        def expect_native_stop() -> None:
            if not reader_stop_expected.is_set():
                harness.enrollment.events.append("reader-expects-stop")
                reader_stop_expected.set()

        @staticmethod
        def clear() -> None:
            return None

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if command == "audioCaptureStart":
            harness.enrollment.events.append("native-start")
            return {
                "success": True,
                "payload": {
                    "streamId": "loss-stream",
                    "framePipe": "loss-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        assert command == "audioCaptureStop"
        assert reader_stop_expected.is_set()
        harness.enrollment.events.append("native-stop")
        return {"success": True, "payload": {"stopped": True}}

    async def lose_during_capture(_duration_ms: int) -> None:
        nonlocal loss_completed
        await harness.enrollment.lose()
        loss_completed = True

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
            capture_factory=lambda **_kwargs: Capture(),
            wait=lose_during_capture,
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 503
    finally:
        await client.close()

    assert harness.store.enrollments == []
    assert loss_completed is True
    assert harness.enrollment.events == [
        "native-start",
        "reader-expects-stop",
        "native-stop",
        "admission-release",
    ]
    assert harness.enrollment.released == [("local-test-claim", True)]


@pytest.mark.asyncio
async def test_voice_enrollment_loss_after_capture_blocks_biometric_persistence(harness):
    """Ownership lost after remote stop still invalidates enrollment commit."""
    harness.model.installed = True
    shell_calls: list[str] = []

    class Capture:
        @staticmethod
        def start(_frame_pipe: str) -> None:
            return None

        @staticmethod
        def stop() -> dict[str, Any]:
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0.0,
            }

        @staticmethod
        def pcm16() -> bytes:
            return b"private-pcm"

        @staticmethod
        def clear() -> None:
            return None

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        shell_calls.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "lost-after-capture",
                    "framePipe": "local-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        return {"success": True, "payload": {"stopped": True}}

    async def lose_before_embedding_commit(_pcm: bytes, *, sample_rate: int = 16_000) -> list[float]:
        assert sample_rate == 16_000
        await harness.enrollment.lose("lease-rebound")
        return [1.0] + [0.0] * 255

    harness.model.extract_pcm16 = lose_before_embedding_commit
    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
            capture_factory=lambda **_kwargs: Capture(),
            wait=lambda _duration_ms: asyncio.sleep(0),
            reference_wav=lambda _pcm, **_kwargs: (b"RIFF" + b"\0" * 60, 4_000),
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 503
    finally:
        await client.close()

    assert harness.store.enrollments == []
    assert shell_calls == ["audioCaptureStart", "audioCaptureStop"]
    assert harness.enrollment.released == [("local-test-claim", True)]


@pytest.mark.asyncio
async def test_voice_enrollment_loss_waits_for_in_flight_shell_start_before_stopping(harness):
    """Loss cannot release ownership while an uninterruptible start may succeed."""
    harness.model.installed = True
    start_entered = threading.Event()
    finish_start = threading.Event()

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if command == "audioCaptureStart":
            harness.enrollment.events.append("start-entered")
            start_entered.set()
            assert finish_start.wait(timeout=2)
            harness.enrollment.events.append("start-finished")
            return {
                "success": True,
                "payload": {
                    "streamId": "late-start-stream",
                    "framePipe": "late-start-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        harness.enrollment.events.append("native-stop")
        return {"success": True, "payload": {"stopped": True}}

    def unexpected_capture(**_kwargs: Any) -> Any:
        raise AssertionError("reader must not start after native-audio ownership was lost")

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
            capture_factory=unexpected_capture,
        ),
    )
    request_task = asyncio.create_task(
        client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(start_entered.wait), timeout=2)
        loss_task = asyncio.create_task(harness.enrollment.lose("shutdown"))
        await asyncio.sleep(0)
        assert not loss_task.done()
        finish_start.set()
        await asyncio.wait_for(loss_task, timeout=2)
        response = await asyncio.wait_for(request_task, timeout=2)
        assert response.status == 503
    finally:
        finish_start.set()
        if not request_task.done():
            request_task.cancel()
        await client.close()

    assert harness.enrollment.events == [
        "start-entered",
        "start-finished",
        "native-stop",
        "admission-release",
    ]
    assert harness.enrollment.released == [("local-test-claim", True)]


@pytest.mark.asyncio
async def test_voice_enrollment_loss_retains_admission_when_shell_stop_is_unconfirmed(harness):
    """A failed loss handler must keep cross-process native ownership fail-closed."""
    harness.model.installed = True

    class Capture:
        @staticmethod
        def start(_frame_pipe: str) -> None:
            return None

        @staticmethod
        def stop() -> dict[str, Any]:
            return {"active": False, "durationMs": 0}

        @staticmethod
        def clear() -> None:
            return None

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "unconfirmed-stop-stream",
                    "framePipe": "local-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        harness.enrollment.events.append("unconfirmed-native-stop")
        return {"success": False, "errorCode": "transportError"}

    async def lose_during_capture(_duration_ms: int) -> None:
        with pytest.raises(RuntimeError, match="proven stopped"):
            await harness.enrollment.lose("lease-expired")

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
            capture_factory=lambda **_kwargs: Capture(),
            wait=lose_during_capture,
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 503
    finally:
        await client.close()

    assert harness.enrollment.events[-1] == "admission-release"
    assert "unconfirmed-native-stop" in harness.enrollment.events[:-1]
    assert harness.enrollment.released == [("local-test-claim", False)]


@pytest.mark.asyncio
async def test_enrollment_retains_audio_ownership_when_a_started_capture_has_no_stream_id(harness):
    """An unaddressable native capture cannot be proven stopped."""
    harness.model.installed = True
    shell_calls: list[str] = []

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        shell_calls.append(command)
        assert command == "audioCaptureStart"
        return {
            "success": True,
            "payload": {
                "framePipe": "local-frame-pipe",
                "sampleRate": 16_000,
                "channels": 1,
                "sampleFormat": "pcm_i16_le",
            },
        }

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 503
        assert "incomplete" in (await response.json())["message"]
    finally:
        await client.close()

    assert shell_calls == ["audioCaptureStart"]
    assert harness.enrollment.released == [("local-test-claim", False)]


@pytest.mark.asyncio
async def test_durable_opt_out_refuses_enrollment_before_native_capture(harness):
    """Another process can withdraw biometric consent before this request."""
    harness.model.installed = True
    harness.store.enabled = False

    def unexpected_capture(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("native capture must not start after durable opt-out")

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=unexpected_capture,
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.enrollment.acquired == []
    assert harness.model.samples == []
    assert harness.store.enrollments == []


@pytest.mark.asyncio
async def test_enrollment_redacts_a_durable_gate_read_failure(sqlite_voice_store, monkeypatch):
    harness = _Harness(store=sqlite_voice_store)
    harness.model.installed = True
    client = await _client(harness)

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as gate_failure:
            gate_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.post(
                "/api/meetings/speaker-profiles/enroll",
                json={"displayName": "Alice", "durationMs": 8_000},
            )
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library consent could not be confirmed."
    finally:
        await client.close()

    assert harness.enrollment.acquired == []


@pytest.mark.asyncio
async def test_enrollment_redacts_an_unknown_profile_read(harness, monkeypatch):
    harness.model.installed = True

    def fail_profile_read():
        raise OSError("C:\\private\\voice-library.db")

    monkeypatch.setattr(harness.store, "speaker_profiles", fail_profile_read)
    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(available=lambda: True),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "profileId": "profile-alice", "durationMs": 8_000},
        )
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library profile data could not be read."
    finally:
        await client.close()

    assert harness.enrollment.acquired == []


@pytest.mark.asyncio
async def test_durable_opt_out_during_capture_refuses_embedding_and_persistence(harness):
    """Consent is checked again after the owned-remote capture completes."""
    harness.model.installed = True

    class Capture:
        @staticmethod
        def start(_frame_pipe: str) -> None:
            return None

        @staticmethod
        def stop() -> dict[str, Any]:
            return {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.4,
                "clippingRatio": 0.0,
            }

        @staticmethod
        def pcm16() -> bytes:
            return b"private-pcm"

        @staticmethod
        def clear() -> None:
            return None

    def call_shell(command: str, _payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "local-stream",
                    "framePipe": "local-frame-pipe",
                    "sampleRate": 16_000,
                    "channels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        return {"success": True, "payload": {"stopped": True}}

    async def withdraw_consent(_duration_ms: int) -> None:
        harness.store.enabled = False

    client = await _client(
        harness,
        capture_runtime=VoiceCaptureRuntime(
            available=lambda: True,
            call=call_shell,
            capture_factory=lambda **_kwargs: Capture(),
            wait=withdraw_consent,
            reference_wav=lambda _pcm, **_kwargs: (b"RIFF" + b"\0" * 60, 4_000),
        ),
    )
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/enroll",
            json={"displayName": "Alice", "durationMs": 8_000},
        )
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.model.samples == []
    assert harness.store.enrollments == []
    assert harness.enrollment.released == [("local-test-claim", True)]


@pytest.mark.asyncio
async def test_profile_mutations_are_owned_and_serialized_by_the_voice_domain(harness):
    client = await _client(harness)
    try:
        renamed = await client.patch(
            "/api/meetings/speaker-profiles/profile-alice",
            json={"displayName": "  Alicia  "},
        )
        assert renamed.status == 200
        assert (await renamed.json())["displayName"] == "Alicia"

        merged = await client.post(
            "/api/meetings/speaker-profiles/merge",
            json={"targetProfileId": "profile-alice", "sourceProfileId": "profile-bob"},
        )
        assert merged.status == 200

        split = await client.post("/api/meetings/meeting-1/speakers/speaker-2/split-profile")
        assert split.status == 200

        deleted = await client.delete("/api/meetings/speaker-profiles/profile-alice")
        assert deleted.status == 200
        missing = await client.delete("/api/meetings/speaker-profiles/profile-alice")
        assert missing.status == 404
    finally:
        await client.close()

    assert harness.store.merges == [("profile-alice", "profile-bob")]
    assert harness.store.splits == [("meeting-1", "speaker-2")]


@pytest.mark.asyncio
async def test_profile_delete_remains_available_after_cross_process_opt_out(sqlite_voice_store):
    profile = sqlite_voice_store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    sqlite_voice_store.set_speaker_library_enabled(False)
    client = await _client(_Harness(store=sqlite_voice_store))
    try:
        response = await client.delete(f"/api/meetings/speaker-profiles/{profile['id']}")
        assert response.status == 200
        assert response.content_type == "application/json"
    finally:
        await client.close()

    assert sqlite_voice_store.speaker_profiles() == []


@pytest.mark.asyncio
async def test_profile_delete_redacts_a_store_failure(sqlite_voice_store, monkeypatch):
    profile = sqlite_voice_store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as store_failure:
            store_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.delete(f"/api/meetings/speaker-profiles/{profile['id']}")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "The speaker profile could not be deleted."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_rename_maps_a_cross_process_opt_out_to_conflict(sqlite_voice_store):
    profile = sqlite_voice_store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    sqlite_voice_store.set_speaker_library_enabled(False)
    client = await _client(_Harness(store=sqlite_voice_store))
    try:
        response = await client.patch(
            f"/api/meetings/speaker-profiles/{profile['id']}",
            json={"displayName": "Alicia"},
        )
        assert response.status == 409
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library is turned off."
    finally:
        await client.close()

    assert sqlite_voice_store.speaker_profiles()[0]["displayName"] == "Alice"


@pytest.mark.asyncio
async def test_profile_rename_redacts_a_store_failure(sqlite_voice_store, monkeypatch):
    profile = sqlite_voice_store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as store_failure:
            store_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.patch(
                f"/api/meetings/speaker-profiles/{profile['id']}",
                json={"displayName": "Alicia"},
            )
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "The speaker profile could not be updated."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_merge_profiles_maps_a_disabled_voice_library_to_conflict(harness):
    def disabled_merge(_target_profile_id: str, _source_profile_id: str) -> dict[str, Any]:
        raise VoiceLibraryDisabled("Voice Library is turned off.")

    harness.store.merge_speaker_profiles = disabled_merge
    client = await _client(harness)
    try:
        response = await client.post(
            "/api/meetings/speaker-profiles/merge",
            json={"targetProfileId": "profile-alice", "sourceProfileId": "profile-bob"},
        )
        assert response.status == 409
        assert (await response.json())["message"] == "Voice Library is turned off."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_merge_redacts_a_store_failure(sqlite_voice_store, monkeypatch):
    target = sqlite_voice_store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    source = sqlite_voice_store.enroll_speaker_profile("Alicia", [0.0, 1.0] + [0.0] * 254)
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as store_failure:
            store_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.post(
                "/api/meetings/speaker-profiles/merge",
                json={"targetProfileId": target["id"], "sourceProfileId": source["id"]},
            )
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "The speaker profiles could not be merged."
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("method", "path", "payload", "store_method", "expected_message"),
    [
        (
            "patch",
            "/api/meetings/speaker-profiles/profile-alice",
            {"displayName": "Alicia"},
            "rename_speaker_profile",
            "The speaker profile could not be updated.",
        ),
        (
            "post",
            "/api/meetings/speaker-profiles/merge",
            {"targetProfileId": "profile-alice", "sourceProfileId": "profile-bob"},
            "merge_speaker_profiles",
            "The speaker profiles could not be merged.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_profile_mutations_redact_unknown_store_runtime_failures(
    harness,
    monkeypatch,
    method,
    path,
    payload,
    store_method,
    expected_message,
):
    def fail_store_write(*_args):
        raise RuntimeError("C:\\private\\voice-library.db")

    monkeypatch.setattr(harness.store, store_method, fail_store_write)
    client = await _client(harness)
    try:
        response = await getattr(client, method)(path, json=payload)
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == expected_message
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_split_profile_maps_a_disabled_voice_library_to_conflict(harness):
    def disabled_split(_meeting_id: str, _speaker_id: str) -> dict[str, Any]:
        raise VoiceLibraryDisabled("Voice Library is turned off.")

    harness.store.split_speaker_profile = disabled_split
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/meeting-1/speakers/speaker-2/split-profile")
        assert response.status == 409
        assert "turned off" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_split_redacts_a_store_failure(sqlite_voice_store, monkeypatch):
    meeting = sqlite_voice_store.create(MeetingCreate(title="Voice split", consent_confirmed=True))
    sqlite_voice_store.add_segments(
        meeting["id"],
        [
            {
                "id": "voice-split-segment",
                "revision": "canonical",
                "source": "system",
                "sequence": 0,
                "speakerLabel": "Remote 1",
                "startMs": 0,
                "endMs": 3_000,
                "text": "Split this speaker.",
            }
        ],
    )
    speaker = sqlite_voice_store.detail(meeting["id"])["speakers"][0]
    sqlite_voice_store.register_speaker_embedding(
        meeting["id"],
        speaker["id"],
        "voice-split-segment",
        [1.0] + [0.0] * 255,
    )
    client = await _client(_Harness(store=sqlite_voice_store))

    def fail_connection():
        raise OSError("C:\\private\\voice-library.db")

    try:
        with monkeypatch.context() as store_failure:
            store_failure.setattr(database, "_get_connection", fail_connection)
            response = await client.post(f"/api/meetings/{meeting['id']}/speakers/{speaker['id']}/split-profile")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "The speaker profile could not be split."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_profile_mutation_keeps_its_lock_until_the_store_commit_finishes(harness):
    started = threading.Event()
    finish = threading.Event()
    original_rename = harness.store.rename_speaker_profile

    def blocked_rename(profile_id: str, display_name: str) -> dict[str, Any]:
        started.set()
        if not finish.wait(timeout=2.0):
            raise TimeoutError("test did not release the profile mutation")
        return original_rename(profile_id, display_name)

    harness.store.rename_speaker_profile = blocked_rename
    client = await _client(harness)
    state = client.server.app[APP_VOICE_COMPONENT_STATE]

    class Request(SimpleNamespace):
        async def json(self) -> dict[str, str]:
            return {"displayName": "Alicia"}

    task = asyncio.create_task(
        patch_speaker_profile(
            Request(
                app=client.server.app,
                match_info={"profileId": "profile-alice"},
            )
        )
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0) is True
        task.cancel()
        await asyncio.sleep(0)

        assert task.done() is False
        assert state.mutation_lock.locked() is True
    finally:
        finish.set()
        await client.close()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.mutation_lock.locked() is False


@pytest.mark.asyncio
async def test_a_download_without_consent_is_refused_before_anything_is_staged(harness, monkeypatch):
    monkeypatch.setattr(Config, "VOICEPRINT_LIBRARY_OPT_IN", False, raising=False)
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 409
        assert "opt-in" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.model.operations == []


@pytest.mark.asyncio
async def test_a_durable_opt_out_refuses_the_download_even_when_this_process_still_agrees(harness):
    """The store's flag is cross-process; Config alone is only this window."""
    harness.store.enabled = False
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 409
        assert "before the download started" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.model.operations == []


@pytest.mark.parametrize(
    ("failed_check", "expected_operations"),
    [(1, []), (2, ["staged", "staging-discarded"])],
)
@pytest.mark.asyncio
async def test_download_redacts_an_unknown_durable_gate(
    harness,
    failed_check,
    expected_operations,
):
    checks = 0

    def fail_selected_gate_read():
        nonlocal checks
        checks += 1
        if checks == failed_check:
            raise OSError("C:\\private\\voice-library.db")
        return True

    harness.store.speaker_library_enabled = fail_selected_gate_read
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())[
            "message"
        ] == "Voice Library consent could not be confirmed for the local download."
    finally:
        await client.close()

    assert harness.model.installed is False
    assert harness.model.operations == expected_operations


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_staged_model_discard(harness):
    checks = 0
    discard_started = threading.Event()
    release_discard = threading.Event()
    discard_finished = threading.Event()

    def opt_out_after_staging():
        nonlocal checks
        checks += 1
        return checks == 1

    def blocking_discard(_staged: object) -> None:
        discard_started.set()
        assert release_discard.wait(timeout=5.0)
        harness.model.operations.append("staging-discarded")
        discard_finished.set()

    harness.store.speaker_library_enabled = opt_out_after_staging
    harness.model.discard_staged = blocking_discard
    client = await _client(harness)
    task = asyncio.create_task(download_speaker_model(SimpleNamespace(app=client.server.app)))
    result: list[Any] = []
    try:
        assert await asyncio.to_thread(discard_started.wait, 2.0)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)

        assert task.done() is False
        assert discard_finished.is_set() is False
    finally:
        release_discard.set()
        result = await asyncio.gather(task, return_exceptions=True)
        await client.close()

    assert isinstance(result[0], asyncio.CancelledError)
    assert discard_finished.is_set() is True
    assert harness.model.operations == ["staged", "staging-discarded"]


@pytest.mark.asyncio
async def test_a_download_installs_the_model_and_reports_its_status(harness):
    client = await _client(harness)
    try:
        body = await (await client.post("/api/meetings/speaker-model")).json()
        assert body["installed"] is True
    finally:
        await client.close()

    assert harness.model.operations == ["staged", "promoted"]


@pytest.mark.asyncio
async def test_an_opt_out_during_the_atomic_replace_deletes_what_was_just_installed(harness):
    """The replace runs in an executor that cancellation cannot interrupt.

    Another Scriber process can withdraw consent inside that window, so the
    route has to re-check afterwards and remove the model it just promoted.
    """
    harness.model.promote_hook = lambda: setattr(harness.store, "enabled", False)
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 409
        assert "while the local download was finishing" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.model.installed is False
    assert harness.model.operations == ["staged", "promoted", "model-deleted"]


@pytest.mark.asyncio
async def test_an_unknown_post_promotion_consent_state_deletes_the_installed_model(harness):
    checks = 0

    def consent_state() -> bool:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise OSError("database unavailable")
        return True

    harness.store.speaker_library_enabled = consent_state
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 503
        assert "could not be confirmed" in (await response.json())["message"]
    finally:
        await client.close()

    assert harness.model.installed is False
    assert harness.model.operations == ["staged", "promoted", "model-deleted"]


@pytest.mark.asyncio
async def test_cancellation_during_a_failed_post_promotion_consent_check_still_deletes_the_model(harness):
    consent_check_started = threading.Event()
    release_consent_check = threading.Event()
    checks = 0

    def consent_state() -> bool:
        nonlocal checks
        checks += 1
        if checks == 3:
            consent_check_started.set()
            if not release_consent_check.wait(timeout=3.0):
                raise TimeoutError("test did not release the consent check")
            raise OSError("database unavailable")
        return True

    harness.store.speaker_library_enabled = consent_state
    client = await _client(harness)
    task = asyncio.create_task(download_speaker_model(SimpleNamespace(app=client.server.app)))
    try:
        assert await asyncio.to_thread(consent_check_started.wait, 2.0)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
    finally:
        release_consent_check.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()

    assert harness.model.installed is False
    assert harness.model.operations == ["staged", "promoted", "model-deleted"]


@pytest.mark.asyncio
async def test_an_opt_out_between_staging_and_promotion_never_promotes(harness):
    client = await _client(harness)
    harness.model.stage_error = None

    original_stage = harness.model.stage_download

    async def stage_then_opt_out(session: Any) -> object:
        staged = await original_stage(session)
        harness.store.enabled = False
        return staged

    harness.model.stage_download = stage_then_opt_out
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 409
        assert "while the local download was running" in (await response.json())["message"]
    finally:
        await client.close()

    # Staging is discarded rather than promoted, so nothing is installed.
    assert harness.model.operations == ["staged", "staging-discarded"]
    assert harness.model.installed is False


@pytest.mark.parametrize(
    ("stage_error", "expected_status", "expected_message"),
    [
        (
            ValueError("C:\\private\\invalid-speaker-model.bin"),
            502,
            "Local Voice Library model validation failed.",
        ),
        (
            sqlite3.OperationalError("C:\\private\\voice-library.db"),
            503,
            "Voice Library is temporarily unavailable.",
        ),
        (
            RuntimeError("C:\\private\\speaker-model.bin"),
            503,
            "Voice Library is temporarily unavailable.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejected_download_redacts_internal_failures(
    harness,
    stage_error,
    expected_status,
    expected_message,
):
    harness.model.stage_error = stage_error
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == expected_status
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == expected_message
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_erasing_the_library_removes_profiles_model_and_consent(harness):
    harness.model.installed = True
    client = await _client(harness)
    try:
        body = await (await client.delete("/api/meetings/speaker-library")).json()
        assert body["deleted"] is True
        assert body["deletedProfiles"] == 2
    finally:
        await client.close()

    assert harness.store.deleted_profiles == 2
    assert harness.model.installed is False
    assert harness.persisted == 1


@pytest.mark.parametrize(
    ("failure_point", "failure_type"),
    [
        ("profile_store", OSError),
        ("profile_store", sqlite3.OperationalError),
        ("profile_store", RuntimeError),
        ("speaker_model", OSError),
        ("speaker_model", RuntimeError),
    ],
)
@pytest.mark.asyncio
async def test_erasing_the_library_redacts_unknown_store_and_model_failures(
    harness,
    monkeypatch,
    failure_point,
    failure_type,
):
    harness.model.installed = True

    def fail_delete():
        raise failure_type("C:\\private\\voice-library.bin")

    if failure_point == "profile_store":
        monkeypatch.setattr(harness.store, "delete_all_speaker_profiles", fail_delete)
    else:
        monkeypatch.setattr(harness.model, "delete", fail_delete)

    client = await _client(harness)
    try:
        response = await client.delete("/api/meetings/speaker-library")
        assert response.status == 503
        assert response.content_type == "application/json"
        assert (await response.json())["message"] == "Voice Library data could not be deleted."
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_erasing_the_library_waits_for_the_mutation_lock(harness):
    """A download holding the lock must not race the erase."""
    client = await _client(harness)
    mutation_lock = client.app[APP_VOICE_COMPONENT_STATE].mutation_lock
    await mutation_lock.acquire()
    try:
        pending = asyncio.create_task(client.delete("/api/meetings/speaker-library"))
        await asyncio.sleep(0)
        assert not pending.done()

        mutation_lock.release()
        assert (await pending).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_diarization_status_and_install_round_trip(harness):
    client = await _client(harness)
    try:
        before = await (await client.get("/api/meetings/diarization-component")).json()
        assert before["installed"] is False
        assert before["enabled"] is True

        installed = await (await client.post("/api/meetings/diarization-component")).json()
        assert installed["installed"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_failed_diarization_install_is_reported_without_the_raw_error(harness):
    harness.diarizer.install_error = RuntimeError("connection to 10.0.0.4 refused")
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/diarization-component")
        assert response.status == 502
        assert (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_diarizer_in_use_refuses_deletion(harness):
    harness.diarizer.installed = True
    harness.diarizer.busy = True
    client = await _client(harness)
    try:
        response = await client.delete("/api/meetings/diarization-component")
        assert response.status == 409
        body = await response.json()
        assert body["deleted"] is False
        assert "in use" in body["message"]
    finally:
        await client.close()

    assert harness.diarizer.installed is True


@pytest.mark.asyncio
async def test_deleting_the_diarizer_reports_the_refreshed_status(harness):
    harness.diarizer.installed = True
    client = await _client(harness)
    try:
        body = await (await client.delete("/api/meetings/diarization-component")).json()
        assert body["deleted"] is True
        assert body["installed"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_app_resolves_each_provider_from_only_what_its_routes_need(tmp_path, monkeypatch):
    """Guard the wiring, and specifically its granularity.

    The Voice Library routes and the diarization routes share no collaborator.
    A single bundle would make the model status endpoint fail on a composition
    that never built a diarizer, so each provider must resolve independently --
    and `persist_settings` must stay a deferred call, since only the erase route
    performs it.
    """
    from types import SimpleNamespace

    from src import web_api
    from src.api.voice_component_routes import (
        APP_DIARIZER,
        APP_VOICE_ENROLLMENT,
        APP_VOICE_LIBRARY_DEPS,
    )

    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)

    class Model:
        @staticmethod
        def status() -> dict[str, Any]:
            return {"installed": False}

    class Store:
        @staticmethod
        def speaker_library_enabled() -> bool:
            return True

    controller = SimpleNamespace(_speaker_model=Model(), _meeting_store=Store())

    app = web_api.create_app(controller)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/meetings/speaker-model")
        assert response.status == 200
    finally:
        await client.close()

    assert not hasattr(controller, "_voice_enrollment_admission")
    assert not hasattr(controller, "_speaker_diarizer")

    # No diarizer and no settings-persist hook on this controller at all.
    deps = app[APP_VOICE_LIBRARY_DEPS]()
    assert deps.speaker_model is controller._speaker_model
    assert app[APP_VOICE_ENROLLMENT] is not None

    controller._speaker_diarizer = "diarizer"
    assert app[APP_DIARIZER]() == "diarizer"


def test_voice_domain_pins_each_real_collaborator_locally(assert_protocol_contract):
    from src import web_api
    from src.data.meeting_store import MeetingStore
    from src.speaker_diarization import SherpaOnnxDiarizer
    from src.speaker_intelligence import WeSpeakerModel

    assert_protocol_contract(
        SpeakerModelPort,
        WeSpeakerModel,
        methods={
            "status",
            "stage_download",
            "promote_staged",
            "discard_staged",
            "delete",
            "extract_pcm16",
        },
    )
    assert_protocol_contract(
        VoiceLibraryStorePort,
        MeetingStore,
        methods={
            "speaker_library_enabled",
            "speaker_profiles",
            "speaker_profile_preview_candidates",
            "speaker_profile_previews",
            "speaker_profile_preview",
            "save_speaker_profile_preview",
            "enroll_speaker_profile",
            "delete_speaker_profile",
            "rename_speaker_profile",
            "delete_all_speaker_profiles",
            "merge_speaker_profiles",
            "split_speaker_profile",
        },
        returns={
            "speaker_library_enabled": bool,
            "speaker_profiles": list[dict[str, Any]],
            "speaker_profile_preview_candidates": dict[str, dict[str, Any]],
            "speaker_profile_previews": dict[str, dict[str, Any]],
            "speaker_profile_preview": dict[str, Any] | None,
            "save_speaker_profile_preview": bool,
            "enroll_speaker_profile": dict[str, Any],
            "delete_speaker_profile": bool,
            "rename_speaker_profile": dict[str, Any],
            "delete_all_speaker_profiles": int,
            "merge_speaker_profiles": dict[str, Any],
            "split_speaker_profile": dict[str, Any],
        },
    )
    assert_protocol_contract(
        DiarizerPort,
        SherpaOnnxDiarizer,
        methods={"status_async", "install", "delete_async"},
    )
    assert_protocol_contract(
        VoiceEnrollmentAdmissionPort,
        web_api._ControllerVoiceEnrollmentAdmission,
        methods={"acquire", "prepare_capture", "release"},
        returns={"acquire": VoiceEnrollmentAdmission},
    )
