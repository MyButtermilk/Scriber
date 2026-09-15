"""Exercise silence-stop settings through HTTP and the real controller."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import src.config as config_module
from src import database
from src.api.settings_routes import register_settings_routes
from src.config import Config
from src.web_api import ScriberWebController


@pytest_asyncio.fixture
async def settings_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC", "0")
    monkeypatch.setenv("SCRIBER_PREWARM_MODELS_ON_STARTUP", "0")
    monkeypatch.setenv("SCRIBER_PREWARM_STT_ON_STARTUP", "0")
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_ENABLED", False)
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_SILENCE_SECONDS", 5)
    monkeypatch.setattr(Config, "LANGUAGE", "de")
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False)
    monkeypatch.setattr(config_module, "_json_settings", {"micAutoStopEnabled": False, "micAutoStopSilenceSeconds": 5})
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_JSON_SETTINGS_PATH", settings_path)
    Config.persist_json_settings()
    persist = Mock(wraps=Config.persist_json_settings)
    # Keep the actual persistence scheduler and JSON write; this feature has no
    # .env fields and tests must never write the developer's environment file.
    monkeypatch.setattr(Config, "persist_settings_files", persist)
    controller = ScriberWebController(asyncio.get_running_loop())
    monkeypatch.setattr(controller, "list_microphones", lambda: [])
    monkeypatch.setattr(controller, "broadcast", AsyncMock())
    app = web.Application()
    register_settings_routes(app, controller=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield SimpleNamespace(client=client, controller=controller, path=settings_path, persist=persist)
    finally:
        await client.close()
        await controller.drain_background_tasks_for_shutdown(timeout_seconds=1)
        controller.shutdown()
        controller.close_persistence_stores()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, 11, -1, True, False, 5.0, 1.5, "5", "invalid", None])
async def test_invalid_timeout_is_http_400_without_partial_settings_update(settings_client, invalid):
    harness = settings_client
    before = harness.path.read_bytes()
    response = await harness.client.put(
        "/api/settings",
        json={"language": "en", "micAutoStopEnabled": True, "micAutoStopSilenceSeconds": invalid},
    )
    assert response.status == 400
    assert "integer from 1 to 10" in (await response.json())["message"]
    assert Config.LANGUAGE == "de"
    assert Config.MIC_AUTO_STOP_ENABLED is False
    assert Config.MIC_AUTO_STOP_SILENCE_SECONDS == 5
    assert harness.path.read_bytes() == before
    harness.persist.assert_not_called()
    harness.controller.broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, 1, "true", "false", 1.0, None, [], {}])
async def test_invalid_enabled_is_http_400_without_partial_settings_update(settings_client, invalid):
    harness = settings_client
    before = harness.path.read_bytes()
    response = await harness.client.put(
        "/api/settings",
        json={"language": "en", "micAutoStopEnabled": invalid, "micAutoStopSilenceSeconds": 10},
    )
    assert response.status == 400
    assert "boolean" in (await response.json())["message"]
    assert Config.LANGUAGE == "de"
    assert Config.MIC_AUTO_STOP_ENABLED is False
    assert Config.MIC_AUTO_STOP_SILENCE_SECONDS == 5
    assert harness.path.read_bytes() == before
    harness.persist.assert_not_called()


@pytest.mark.asyncio
async def test_silence_stop_http_roundtrip_and_persistence_at_both_bounds(settings_client):
    harness = settings_client
    initial = await (await harness.client.get("/api/settings")).json()
    assert initial["micAutoStopEnabled"] is False
    assert initial["micAutoStopSilenceSeconds"] == 5

    for enabled, seconds in [(True, 1), (True, 10), (False, 10)]:
        response = await harness.client.put(
            "/api/settings",
            json={"micAutoStopEnabled": enabled, "micAutoStopSilenceSeconds": seconds},
        )
        assert response.status == 200
        updated = await response.json()
        assert updated["micAutoStopEnabled"] is enabled
        assert updated["micAutoStopSilenceSeconds"] == seconds
        current = await (await harness.client.get("/api/settings")).json()
        assert current["micAutoStopEnabled"] is enabled
        assert current["micAutoStopSilenceSeconds"] == seconds
        task = harness.controller._settings_persist_task
        if task is not None:
            await asyncio.wait_for(task, timeout=2)
        persisted = json.loads(harness.path.read_text(encoding="utf-8"))
        assert persisted["micAutoStopEnabled"] is enabled
        assert persisted["micAutoStopSilenceSeconds"] == seconds
    assert harness.persist.call_count == 3
