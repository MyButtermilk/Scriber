from src import native_overlay


def _response(
    *,
    success: bool,
    mode: str = "hidden",
    visible: bool = False,
    native_visible: bool | None = None,
    cursor_events_ignored: bool | None = None,
):
    if native_visible is None:
        native_visible = visible
    if cursor_events_ignored is None:
        cursor_events_ignored = not visible
    return {
        "success": success,
        "errorCode": None if success else "transportError",
        "payload": {
            "mode": mode,
            "visible": visible,
            "nativeVisible": native_visible,
            "cursorEventsIgnored": cursor_events_ignored,
        },
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


def test_hide_overlay_reconciles_state_after_lost_response(monkeypatch):
    responses = iter(
        [
            _response(success=False, mode="recording", visible=True),
            _response(success=True, mode="hidden", visible=False),
        ]
    )
    calls = []

    def fake_call(command, payload=None, **kwargs):
        calls.append((command, payload, kwargs))
        return next(responses)

    monkeypatch.setattr(native_overlay, "_call_overlay_response", fake_call)

    response = native_overlay._hide_overlay()

    assert response["success"] is True
    assert calls == [
        ("overlayHide", None, {"log_failure": False}),
        ("overlayStatus", None, {"log_failure": False}),
    ]


def test_hide_overlay_retries_when_native_state_is_still_visible(monkeypatch):
    responses = iter(
        [
            _response(success=False, mode="recording", visible=True),
            _response(success=True, mode="recording", visible=True),
            _response(success=True, mode="hidden", visible=False),
        ]
    )
    calls = []

    def fake_call(command, payload=None, **kwargs):
        calls.append((command, payload, kwargs))
        return next(responses)

    monkeypatch.setattr(native_overlay, "_call_overlay_response", fake_call)

    response = native_overlay._hide_overlay()

    assert response["success"] is True
    assert [call[0] for call in calls] == [
        "overlayHide",
        "overlayStatus",
        "overlayHide",
    ]


def test_hide_overlay_retries_when_logical_state_is_hidden_but_native_window_is_visible(
    monkeypatch,
):
    responses = iter(
        [
            _response(
                success=False,
                mode="hidden",
                visible=False,
                native_visible=True,
                cursor_events_ignored=True,
            ),
            _response(
                success=True,
                mode="hidden",
                visible=False,
                native_visible=True,
                cursor_events_ignored=True,
            ),
            _response(success=True, mode="hidden", visible=False),
        ]
    )
    calls = []

    def fake_call(command, payload=None, **kwargs):
        calls.append((command, payload, kwargs))
        return next(responses)

    monkeypatch.setattr(native_overlay, "_call_overlay_response", fake_call)

    response = native_overlay._hide_overlay()

    assert response["success"] is True
    assert response["payload"]["nativeVisible"] is False
    assert [call[0] for call in calls] == [
        "overlayHide",
        "overlayStatus",
        "overlayHide",
    ]


def test_show_overlay_retries_until_cursor_events_are_enabled(monkeypatch):
    responses = iter(
        [
            _response(success=False),
            _response(
                success=True,
                mode="recording",
                visible=True,
                native_visible=True,
                cursor_events_ignored=True,
            ),
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
        ("overlayShow", {"mode": "recording"}),
    ]


def test_overlay_audio_level_uses_nonblocking_native_pump(monkeypatch):
    published = []

    class FakePump:
        def publish(self, rms):
            published.append(rms)

    monkeypatch.setattr(native_overlay, "_tauri_overlay_enabled", lambda: True)
    monkeypatch.setattr(native_overlay, "_audio_level_pump", FakePump())

    native_overlay.RecordingOverlay().update_audio_level(0.125)

    assert published == [0.125]


def test_overlay_ipc_deadlines_cover_bounded_ui_dispatch(monkeypatch):
    calls = []

    def fake_call(command, payload, *, timeout_seconds):
        calls.append((command, timeout_seconds))
        return _response(success=True)

    monkeypatch.setattr(native_overlay, "call_shell_ipc", fake_call)

    for command in (
        "overlayPrepare",
        "overlayShow",
        "overlayHide",
        "overlayStatus",
        "overlayAudioLevel",
    ):
        native_overlay._call_overlay_response(command, log_failure=False)

    deadlines = dict(calls)
    assert deadlines["overlayPrepare"] > 2.0
    assert deadlines["overlayShow"] == deadlines["overlayPrepare"]
    assert deadlines["overlayHide"] == deadlines["overlayPrepare"]
    assert deadlines["overlayStatus"] < deadlines["overlayPrepare"]
    assert deadlines["overlayAudioLevel"] < deadlines["overlayStatus"]
