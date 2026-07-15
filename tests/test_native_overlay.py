from src import native_overlay


def _response(*, success: bool, mode: str = "hidden", visible: bool = False):
    return {
        "success": success,
        "errorCode": None if success else "transportError",
        "payload": {"mode": mode, "visible": visible},
    }


def test_show_overlay_reconciles_state_after_lost_response(monkeypatch):
    responses = iter(
        [
            _response(success=False),
            _response(success=True, mode="recording", visible=True),
        ]
    )
    calls = []

    def fake_call(command, payload=None, **_kwargs):
        calls.append((command, payload))
        return next(responses)

    monkeypatch.setattr(native_overlay, "_call_overlay_response", fake_call)

    response = native_overlay._show_overlay_mode("recording")

    assert response["success"] is True
    assert calls == [
        ("overlayShow", {"mode": "recording"}),
        ("overlayStatus", None),
    ]


def test_show_overlay_retries_when_native_state_was_not_applied(monkeypatch):
    responses = iter(
        [
            _response(success=False),
            _response(success=True, mode="hidden", visible=False),
            _response(success=True, mode="recording", visible=True),
        ]
    )
    calls = []

    def fake_call(command, payload=None, **_kwargs):
        calls.append((command, payload))
        return next(responses)

    monkeypatch.setattr(native_overlay, "_call_overlay_response", fake_call)

    response = native_overlay._show_overlay_mode("recording")

    assert response["success"] is True
    assert calls[-1] == ("overlayShow", {"mode": "recording"})
    assert len(calls) == 3


def test_overlay_audio_level_uses_nonblocking_native_pump(monkeypatch):
    published = []

    class FakePump:
        def publish(self, rms):
            published.append(rms)

    monkeypatch.setattr(native_overlay, "_tauri_overlay_enabled", lambda: True)
    monkeypatch.setattr(native_overlay, "_audio_level_pump", FakePump())

    native_overlay.RecordingOverlay().update_audio_level(0.125)

    assert published == [0.125]
