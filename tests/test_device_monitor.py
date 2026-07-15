from pathlib import Path
import subprocess
import sys
import types
import threading
import time

import src.device_monitor as device_monitor


def test_importing_web_api_does_not_import_sounddevice_in_fresh_process():
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys\n"
        "assert 'sounddevice' not in sys.modules\n"
        "import src.web_api\n"
        "assert 'sounddevice' not in sys.modules, "
        "'src.web_api imported sounddevice eagerly'\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_concurrent_enumeration_imports_sounddevice_once(monkeypatch):
    import_calls = 0
    import_started = threading.Event()
    release_import = threading.Event()

    class _FakeSoundDevice:
        default = types.SimpleNamespace(device=(0, None), hostapi=0)

        @staticmethod
        def query_devices():
            return [
                {
                    "name": "USB Mic, MME",
                    "max_input_channels": 1,
                    "hostapi": 0,
                }
            ]

        @staticmethod
        def query_hostapis():
            return [{"name": "MME"}]

        @staticmethod
        def check_input_settings(**_kwargs):
            return None

    fake_sounddevice = _FakeSoundDevice()

    def import_sounddevice(name):
        nonlocal import_calls
        assert name == "sounddevice"
        import_calls += 1
        import_started.set()
        assert release_import.wait(timeout=2)
        return fake_sounddevice

    monkeypatch.setattr(device_monitor, "sd", None)
    monkeypatch.setattr(device_monitor, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(device_monitor, "_SOUNDDEVICE_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(device_monitor, "import_module", import_sounddevice)

    results: list[list[dict[str, str]]] = []
    workers = [
        threading.Thread(target=lambda: results.append(device_monitor._enumerate_microphones()))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    assert import_started.wait(timeout=2)
    release_import.set()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    assert import_calls == 1
    assert len(results) == 4
    assert all(result[1]["deviceId"] == "USB Mic, MME" for result in results)


def test_unavailable_sounddevice_keeps_default_only_without_import(monkeypatch):
    monkeypatch.setattr(device_monitor, "sd", None)
    monkeypatch.setattr(device_monitor, "HAS_SOUNDDEVICE", False)
    monkeypatch.setattr(device_monitor, "_SOUNDDEVICE_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(
        device_monitor,
        "import_module",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not import")),
    )

    assert device_monitor._enumerate_microphones() == [
        {"deviceId": "default", "label": "Default"}
    ]


def test_device_monitor_loads_sounddevice_on_its_worker_thread(monkeypatch):
    imported_on: list[str] = []
    refreshed = threading.Event()

    fake_sounddevice = types.SimpleNamespace(
        default=types.SimpleNamespace(device=(0, None), hostapi=0),
        query_devices=lambda: [
            {
                "name": "Worker Mic, MME",
                "max_input_channels": 1,
                "hostapi": 0,
            }
        ],
        query_hostapis=lambda: [{"name": "MME"}],
        check_input_settings=lambda **_kwargs: None,
    )

    def import_sounddevice(_name):
        imported_on.append(threading.current_thread().name)
        return fake_sounddevice

    monkeypatch.setattr(device_monitor, "sd", None)
    monkeypatch.setattr(device_monitor, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(device_monitor, "_SOUNDDEVICE_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(device_monitor, "import_module", import_sounddevice)
    monitor = device_monitor.DeviceMonitor(poll_seconds=60)
    monitor.on_devices_changed(lambda _devices: refreshed.set())

    monitor.start()
    try:
        assert refreshed.wait(timeout=2)
    finally:
        monitor.stop()

    assert imported_on == ["device-monitor"]


def test_concurrent_monitor_start_creates_only_one_thread(monkeypatch):
    monitor = device_monitor.DeviceMonitor(poll_seconds=60)
    real_thread = threading.Thread
    factory_entered = threading.Event()
    release_factory = threading.Event()
    factory_calls = 0

    class FakeMonitorThread:
        def __init__(self):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, *, timeout):
            self.alive = False

    def thread_factory(**_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        factory_entered.set()
        release_factory.wait(timeout=1)
        return FakeMonitorThread()

    monkeypatch.setattr(device_monitor.threading, "Thread", thread_factory)
    first = real_thread(target=monitor.start)
    second = real_thread(target=monitor.start)
    first.start()
    assert factory_entered.wait(timeout=1)
    second.start()
    time.sleep(0.03)
    assert factory_calls == 1
    release_factory.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert factory_calls == 1
    monitor.stop()


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


def test_device_state_changed_burst_uses_trailing_debounce(monkeypatch):
    monitor = device_monitor.DeviceMonitor(debounce_seconds=0.5)
    logs: list[str] = []
    times = iter([100.0, 100.2, 100.6, 101.1])

    monkeypatch.setattr(device_monitor.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        device_monitor,
        "logger",
        types.SimpleNamespace(debug=lambda msg: logs.append(msg)),
    )

    monitor._schedule_refresh(reason="device_state_changed", immediate=False)
    monitor._schedule_refresh(reason="device_state_changed", immediate=False)
    monitor._schedule_refresh(reason="device_state_changed", immediate=False)
    monitor._schedule_refresh(reason="device_state_changed", immediate=False)

    assert logs == [
        "[DeviceMonitor] refresh scheduled (device_state_changed, portaudio)",
    ]
    expected_due_at = 101.1 + device_monitor._DEVICE_STATE_CHANGED_DEBOUNCE_SECONDS
    assert abs(monitor._pending_refresh_at - expected_due_at) < 0.001


def test_due_refresh_take_preserves_newer_trailing_deadline():
    monitor = device_monitor.DeviceMonitor()
    monitor._pending_refresh_at = 101.0
    monitor._pending_refresh_reason = "device_state_changed"
    monitor._pending_refresh_requires_portaudio = True

    assert monitor._take_due_refresh(100.9) is None
    assert monitor._pending_refresh_at == 101.0
    assert monitor._take_due_refresh(101.0) == ("device_state_changed", True)
    assert monitor._pending_refresh_at == 0.0


def test_stop_retains_reference_when_monitor_thread_does_not_exit(monkeypatch):
    monitor = device_monitor.DeviceMonitor()
    warnings: list[str] = []

    class StuckThread:
        def is_alive(self):
            return True

        def join(self, *, timeout):
            assert timeout == 2.0

    thread = StuckThread()
    monitor._thread = thread  # type: ignore[assignment]
    monkeypatch.setattr(
        device_monitor,
        "logger",
        types.SimpleNamespace(
            warning=lambda message: warnings.append(message),
            info=lambda _message: None,
        ),
    )

    monitor.stop()

    assert monitor._thread is thread
    assert warnings == ["[DeviceMonitor] stop timed out; monitor thread is still running"]


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


def test_native_refresh_hint_ignores_render_flow():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    result = monitor.request_native_refresh(
        {
            "source": "tauri",
            "eventKind": "device_added",
            "flow": "render",
            "endpointIdHash": "abc123",
        }
    )

    assert scheduled == []
    assert result == {
        "scheduled": False,
        "ignored": True,
        "reason": "render-flow",
        "deviceMonitor": "running",
    }
    diagnostics = monitor.diagnostic_snapshot()
    assert diagnostics["nativeHintCount"] == 1
    assert diagnostics["nativeHintIgnoredCount"] == 1
    assert diagnostics["lastNativeHint"]["endpointIdHash"] == "abc123"


def test_native_refresh_hint_schedules_capture_with_portaudio():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    result = monitor.request_native_refresh(
        {
            "source": "tauri",
            "eventKind": "default_device_changed",
            "flow": "capture",
            "role": "communications",
            "forcePortAudioRefresh": True,
        }
    )

    assert result == {
        "scheduled": True,
        "ignored": False,
        "deviceMonitor": "running",
        "forcePortAudioRefresh": True,
    }
    assert scheduled == [
        {
            "reason": "native_default_device_changed",
            "immediate": False,
            "force_portaudio_refresh": True,
        }
    ]
    diagnostics = monitor.diagnostic_snapshot()
    assert diagnostics["nativeHintCount"] == 1
    assert diagnostics["nativeHintIgnoredCount"] == 0
    assert diagnostics["nativeHintPortAudioCount"] == 1


def test_native_refresh_hint_can_be_non_invasive():
    monitor = device_monitor.DeviceMonitor()
    scheduled: list[dict[str, object]] = []
    monitor._schedule_refresh = lambda **kwargs: scheduled.append(kwargs)  # type: ignore[method-assign]

    monitor.request_native_refresh(
        {
            "source": "tauri",
            "eventKind": "property_value_changed",
            "flow": "unknown",
            "forcePortAudioRefresh": False,
        }
    )

    assert scheduled == [
        {
            "reason": "native_property_value_changed",
            "immediate": False,
            "force_portaudio_refresh": False,
        }
    ]
    assert monitor.diagnostic_snapshot()["nativeHintPortAudioCount"] == 0
