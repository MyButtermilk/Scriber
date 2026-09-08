"""The supervisor's frequent health poll must not resolve paths or touch disk."""

from __future__ import annotations

import time
from types import SimpleNamespace

from src import web_api


def test_health_uses_only_live_status_and_never_builds_diagnostic_inventory(monkeypatch):
    controller = web_api.ScriberWebController.__new__(web_api.ScriberWebController)
    controller._started_at_iso = "2026-09-08T00:00:00+02:00"
    controller._started_at_monotonic = time.monotonic() - 2
    controller._session_id = "active-session"
    controller._recording_state_machine = SimpleNamespace(state=SimpleNamespace(value="recording"))

    def forbidden():
        raise AssertionError("health touched diagnostic paths or feature inventory")

    monkeypatch.setattr(web_api, "data_dir", forbidden)
    monkeypatch.setattr(web_api, "logs_dir", forbidden)
    monkeypatch.setattr(web_api, "_runtime_feature_flags", forbidden)
    payload = controller.get_health()
    assert payload["ok"] is True and payload["ready"] is True
    assert payload["activeSession"] == "active-session"
    assert payload["recordingState"] == "recording"
    assert 1 <= payload["uptimeSeconds"] < 10
    assert "dataDir" not in payload and "featureFlags" not in payload
    controller._session_id = None
    controller._recording_state_machine.state.value = "idle"
    assert controller.get_health()["recordingState"] == "idle"
    assert controller.get_health()["activeSession"] is None
