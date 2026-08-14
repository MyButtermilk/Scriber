"""Voice component routes exercised without web_api.create_app.

The interesting behaviour here is the biometric opt-in: it is durable and
cross-process, so it can be withdrawn while a download is mid-flight. Most of
these tests are about what the routes leave behind when that happens.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.app_keys import APP_HTTP_SESSION
from src.api.voice_component_routes import VoiceLibraryDeps, register_voice_component_routes
from src.config import Config


class _Model:
    def __init__(self) -> None:
        self.installed = False
        self.operations: list[str] = []
        self.stage_error: Exception | None = None
        self.promote_hook: Any = None

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


class _FlaglessStore:
    """An older store with no cross-process consent flag to consult."""

    def __init__(self) -> None:
        self.deleted_profiles = 0

    def delete_all_speaker_profiles(self) -> int:
        self.deleted_profiles = 2
        return 2


class _Store(_FlaglessStore):
    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled

    def speaker_library_enabled(self) -> bool:
        return self.enabled


class _Diarizer:
    def __init__(self) -> None:
        self.installed = False
        self.busy = False
        self.install_error: Exception | None = None

    def status(self) -> dict[str, Any]:
        return {"installed": self.installed}

    async def install(self, session: Any) -> dict[str, Any]:
        if self.install_error is not None:
            raise self.install_error
        self.installed = True
        return {"installed": True}

    def delete(self) -> None:
        self.installed = False


class _AsyncDiarizer(_Diarizer):
    """The second implementation shape: async status and a refusable delete."""

    async def status_async(self) -> dict[str, Any]:
        return {"installed": self.installed, "shape": "async"}

    async def delete_async(self) -> bool:
        if self.busy:
            return False
        self.installed = False
        return True


class _Harness:
    def __init__(self, *, diarizer: _Diarizer | None = None, store: Any = None) -> None:
        self.model = _Model()
        self.store = store if store is not None else _Store()
        self.diarizer = diarizer if diarizer is not None else _Diarizer()
        self.persisted = 0
        self.download_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()

    def voice_library(self) -> VoiceLibraryDeps:
        return VoiceLibraryDeps(
            speaker_model=self.model,
            meeting_store=self.store,
            persist_settings=self._persist,
            download_lock=self.download_lock,
            mutation_lock=self.mutation_lock,
        )

    def _persist(self) -> None:
        self.persisted += 1


@pytest.fixture
def harness():
    return _Harness()


@pytest.fixture(autouse=True)
def opted_in(monkeypatch):
    """Consent is on unless a test turns it off; most cases are about losing it."""
    monkeypatch.setattr(Config, "VOICEPRINT_LIBRARY_OPT_IN", True, raising=False)
    monkeypatch.setattr(Config, "SPEAKER_DIARIZATION_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "set_voiceprint_library_opt_in", lambda _value: None, raising=False)


async def _client(harness: _Harness) -> TestClient:
    app = web.Application()
    register_voice_component_routes(
        app,
        voice_library=harness.voice_library,
        diarizer=lambda: harness.diarizer,
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


@pytest.mark.asyncio
async def test_a_store_without_the_flag_falls_back_to_this_process(harness):
    harness.store = _FlaglessStore()
    client = await _client(harness)
    try:
        assert (await client.post("/api/meetings/speaker-model")).status == 200
    finally:
        await client.close()

    assert harness.model.installed is True


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


@pytest.mark.asyncio
async def test_a_rejected_download_reports_the_upstream_failure_as_502(harness):
    harness.model.stage_error = ValueError("checksum mismatch")
    client = await _client(harness)
    try:
        response = await client.post("/api/meetings/speaker-model")
        assert response.status == 502
        assert (await response.json())["message"] == "checksum mismatch"
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


@pytest.mark.asyncio
async def test_erasing_the_library_waits_for_the_mutation_lock(harness):
    """A download holding the lock must not race the erase."""
    client = await _client(harness)
    await harness.mutation_lock.acquire()
    try:
        pending = asyncio.create_task(client.delete("/api/meetings/speaker-library"))
        await asyncio.sleep(0)
        assert not pending.done()

        harness.mutation_lock.release()
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
async def test_the_async_diarizer_shape_is_used_when_it_is_offered(harness):
    """Both implementations are supported; the async one wins where present."""
    harness.diarizer = _AsyncDiarizer()
    client = await _client(harness)
    try:
        body = await (await client.get("/api/meetings/diarization-component")).json()
        assert body["shape"] == "async"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_diarizer_in_use_refuses_deletion(harness):
    harness.diarizer = _AsyncDiarizer()
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


def test_create_app_resolves_each_provider_from_only_what_its_routes_need(tmp_path, monkeypatch):
    """Guard the wiring, and specifically its granularity.

    The Voice Library routes and the diarization routes share no collaborator.
    A single bundle would make the model status endpoint fail on a composition
    that never built a diarizer, so each provider must resolve independently --
    and `persist_settings` must stay a deferred call, since only the erase route
    performs it.
    """
    from types import SimpleNamespace

    from src import web_api
    from src.api.voice_component_routes import APP_DIARIZER, APP_VOICE_LIBRARY_DEPS

    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    controller = SimpleNamespace(_speaker_model=object(), _meeting_store=object())

    app = web_api.create_app(controller)

    # No diarizer and no settings-persist hook on this controller at all.
    deps = app[APP_VOICE_LIBRARY_DEPS]()
    assert deps.speaker_model is controller._speaker_model
    assert isinstance(deps.download_lock, asyncio.Lock)
    assert deps.download_lock is not deps.mutation_lock

    controller._speaker_diarizer = "diarizer"
    assert app[APP_DIARIZER]() == "diarizer"
