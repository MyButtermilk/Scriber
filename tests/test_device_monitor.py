import types

import src.device_monitor as device_monitor


class _RecordingLock:
    def __init__(self) -> None:
        self.held = False
        self.enter_count = 0

    def __enter__(self):
        self.held = True
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        return False


def test_enumerate_microphones_queries_portaudio_under_guard(monkeypatch):
    guard = _RecordingLock()
    query_checked_guard = False

    class _FakeSoundDevice:
        default = types.SimpleNamespace(device=(0, None), hostapi=0)

        def query_devices(self):
            nonlocal query_checked_guard
            query_checked_guard = True
            assert guard.held
            return [{"name": "USB Mic, MME", "max_input_channels": 1, "hostapi": 0}]

        def query_hostapis(self):
            return [{"name": "MME"}]

        def check_input_settings(self, **kwargs):
            return None

    monkeypatch.setattr(device_monitor, "_DEVICE_GUARD_LOCK", guard)
    monkeypatch.setattr(device_monitor, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(device_monitor, "sd", _FakeSoundDevice())

    devices = device_monitor._enumerate_microphones()

    assert query_checked_guard is True
    assert guard.enter_count == 1
    assert devices == [
        {"deviceId": "default", "label": "Default"},
        {"deviceId": "USB Mic, MME", "label": "USB Mic, MME (Default)"},
    ]


def test_schedule_refresh_logs_only_new_pending_refresh(monkeypatch):
    monitor = device_monitor.DeviceMonitor(debounce_seconds=1.0)
    logs: list[str] = []

    monkeypatch.setattr(device_monitor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        device_monitor,
        "logger",
        types.SimpleNamespace(debug=lambda msg: logs.append(msg)),
    )

    monitor._schedule_refresh(reason="device_state_changed", immediate=False)
    monitor._schedule_refresh(reason="device_state_changed", immediate=False)
    monitor._schedule_refresh(reason="manual", immediate=True)

    assert logs == [
        "[DeviceMonitor] refresh scheduled (device_state_changed, portaudio)",
        "[DeviceMonitor] refresh scheduled (manual, portaudio)",
    ]


def test_refresh_deferred_while_stream_active_is_not_rescheduled(monkeypatch):
    monitor = device_monitor.DeviceMonitor(debounce_seconds=1.0)
    logs: list[str] = []

    monkeypatch.setattr(device_monitor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", lambda: (False, True))
    monkeypatch.setattr(
        device_monitor,
        "logger",
        types.SimpleNamespace(debug=lambda msg: logs.append(msg)),
    )

    monitor._refresh_devices(trigger="event", force=False)
    monitor._refresh_devices(trigger="event", force=False)
    monitor._schedule_refresh(reason="device_added", immediate=False)

    assert monitor._pending_refresh_at == 0.0
    assert logs == [
        "[DeviceMonitor] refresh deferred until active stream stops (event)",
    ]


def test_refresh_quiesces_and_resumes_idle_streams(monkeypatch):
    monitor = device_monitor.DeviceMonitor()
    calls: list[str] = []

    monitor.on_portaudio_refresh_quiesce(
        lambda: calls.append("pause"),
        lambda: calls.append("resume"),
    )
    monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", lambda: (True, False))
    monkeypatch.setattr(
        device_monitor,
        "_enumerate_microphones",
        lambda **_kwargs: [{"deviceId": "default", "label": "Default"}],
    )

    monitor._refresh_devices(trigger="manual", force=True)

    assert calls == ["pause", "resume"]


def test_poll_refresh_does_not_quiesce_or_refresh_portaudio(monkeypatch):
    monitor = device_monitor.DeviceMonitor()
    calls: list[str] = []

    monitor.on_portaudio_refresh_quiesce(
        lambda: calls.append("pause"),
        lambda: calls.append("resume"),
    )

    def fail_refresh():
        raise AssertionError("poll refresh should not restart PortAudio")

    monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", fail_refresh)
    monkeypatch.setattr(
        device_monitor,
        "_enumerate_microphones",
        lambda **_kwargs: [{"deviceId": "default", "label": "Default"}],
    )

    monitor._refresh_devices(trigger="poll", force=False, refresh_portaudio=False)

    assert calls == []


def test_poll_interval_is_sparse_when_native_events_are_active():
    monitor = device_monitor.DeviceMonitor()
    monitor._poll_seconds_override = None
    monitor._native_notifications_active = False

    assert monitor._current_poll_seconds() == device_monitor._FALLBACK_POLL_SECONDS
    assert monitor._poll_mode() == "fallback"

    monitor._native_notifications_active = True

    assert monitor._current_poll_seconds() == device_monitor._NATIVE_EVENT_SAFETY_POLL_SECONDS
    assert monitor._poll_mode() == "native-event-safety"


def test_explicit_poll_interval_override_wins_over_native_event_mode():
    monitor = device_monitor.DeviceMonitor(poll_seconds=7.5)
    monitor._native_notifications_active = True

    assert monitor._current_poll_seconds() == 7.5
    assert monitor._poll_mode() == "override"


def test_non_invasive_poll_is_quiet_when_devices_are_unchanged(monkeypatch):
    monitor = device_monitor.DeviceMonitor()
    devices = [{"deviceId": "default", "label": "Default"}]
    logs: dict[str, list[str]] = {"debug": [], "info": [], "warning": []}

    class _FakeLogger:
        def debug(self, msg):
            logs["debug"].append(msg)

        def info(self, msg):
            logs["info"].append(msg)

        def warning(self, msg):
            logs["warning"].append(msg)

    monitor._devices = devices
    monitor._signature = monitor._signature_for(devices)
    monkeypatch.setattr(device_monitor, "logger", _FakeLogger())
    monkeypatch.setattr(
        device_monitor,
        "_enumerate_microphones",
        lambda **_kwargs: [dict(item) for item in devices],
    )

    monitor._refresh_devices(trigger="poll", force=False, refresh_portaudio=False)

    assert logs == {"debug": [], "info": [], "warning": []}
    diagnostics = monitor.diagnostic_snapshot()
    assert diagnostics["pollRefreshCount"] == 1
    assert diagnostics["lastPollRefreshAgoSeconds"] is not None


def test_refresh_does_not_resume_idle_stream_when_real_stream_is_active(monkeypatch):
    monitor = device_monitor.DeviceMonitor()
    calls: list[str] = []

    monitor.on_portaudio_refresh_quiesce(
        lambda: calls.append("pause"),
        lambda: calls.append("resume"),
    )
    monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", lambda: (False, True))

    monitor._refresh_devices(trigger="manual", force=True)

    assert calls == ["pause"]


def test_deferred_refresh_waits_for_active_stream_to_stop(monkeypatch):
    monitor = device_monitor.DeviceMonitor()

    monitor._defer_refresh_until_idle(trigger="event")

    monkeypatch.setattr(device_monitor, "get_active_stream_count", lambda: 1)
    assert monitor._take_deferred_refresh_trigger_if_idle() is None

    monkeypatch.setattr(device_monitor, "get_active_stream_count", lambda: 0)
    assert monitor._take_deferred_refresh_trigger_if_idle() == "event_stream_idle"
    assert monitor._take_deferred_refresh_trigger_if_idle() is None


def test_endpoint_refresh_ignores_render_endpoint():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []

    monitor._pycaw_audio_utilities = types.SimpleNamespace(
        GetEndpointDataFlow=lambda device_id, outputType=0: device_monitor._E_RENDER
    )
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    monitor._schedule_endpoint_refresh(
        reason="device_state_changed",
        device_id="{0.0.0.00000000}.{render}",
        immediate=False,
    )

    assert scheduled == []


def test_endpoint_refresh_schedules_capture_endpoint():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []

    monitor._pycaw_audio_utilities = types.SimpleNamespace(
        GetEndpointDataFlow=lambda device_id, outputType=0: device_monitor._E_CAPTURE
    )
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    monitor._schedule_endpoint_refresh(
        reason="device_state_changed",
        device_id="{0.0.1.00000000}.{capture}",
        immediate=False,
    )

    assert scheduled == [
        {
            "reason": "device_state_changed",
            "immediate": False,
            "force_portaudio_refresh": True,
        }
    ]


def test_endpoint_refresh_uses_endpoint_id_hint_when_flow_lookup_is_unavailable():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []

    monitor._pycaw_audio_utilities = types.SimpleNamespace()
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    monitor._schedule_endpoint_refresh(
        reason="device_state_changed",
        device_id=r"SWD\MMDEVAPI\{0.0.0.00000000}.{render}",
        immediate=False,
    )
    monitor._schedule_endpoint_refresh(
        reason="device_state_changed",
        device_id=r"SWD\MMDEVAPI\{0.0.1.00000000}.{capture}",
        immediate=False,
    )

    assert scheduled == [
        {
            "reason": "device_state_changed",
            "immediate": False,
            "force_portaudio_refresh": True,
        }
    ]


def test_default_device_refresh_ignores_render_flow():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    monitor._schedule_flow_refresh(
        reason="default_device_changed",
        flow=device_monitor._E_RENDER,
        immediate=False,
    )
    monitor._schedule_flow_refresh(
        reason="default_device_changed",
        flow=device_monitor._E_CAPTURE,
        immediate=False,
    )

    assert scheduled == [
        {
            "reason": "default_device_changed",
            "immediate": False,
            "force_portaudio_refresh": True,
        }
    ]
