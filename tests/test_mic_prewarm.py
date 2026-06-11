import types

import numpy as np

from src.config import Config
from src.device_monitor import get_active_stream_count
from src.mic_prewarm import MicrophonePrewarmManager, RustAudioPrewarmManager
import src.mic_prewarm as mic_prewarm


class _FakeInputStream:
    instances: list["_FakeInputStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        _FakeInputStream.instances.append(self)

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


def _install_fake_sounddevice(monkeypatch):
    _FakeInputStream.instances.clear()
    fake_sd = types.SimpleNamespace(
        default=types.SimpleNamespace(device=(0, None), hostapi=0),
        InputStream=lambda **kwargs: _FakeInputStream(**kwargs),
        query_hostapis=lambda: [{"name": "MME"}],
        query_devices=lambda device=None, kind=None: (
            [
                {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0},
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
            ]
            if device is None and kind is None
            else {"name": "Built-in Mic, MME", "max_input_channels": 1, "hostapi": 0}
        ),
        check_input_settings=lambda **_kwargs: None,
    )
    monkeypatch.setattr(mic_prewarm, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(mic_prewarm, "sd", fake_sd)
    return fake_sd


def test_mic_prewarm_starts_and_stops_idle_stream(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    before_count = get_active_stream_count()

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    assert manager.is_active is True
    assert get_active_stream_count() == before_count + 1
    assert _FakeInputStream.instances[-1].kwargs["device"] == 0

    manager.stop()

    assert manager.is_active is False
    assert _FakeInputStream.instances[-1].closed is True
    assert get_active_stream_count() == before_count


def test_mic_prewarm_stays_paused_during_active_capture(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()
    assert manager.start_if_enabled() is True
    manager.pause_for_active_capture()

    assert manager.is_active is False
    assert manager.start_if_enabled() is False
    assert len(_FakeInputStream.instances) == 1

    assert manager.resume_after_active_capture() is True
    assert manager.is_active is True
    assert len(_FakeInputStream.instances) == 2

    manager.stop()


def test_mic_prewarm_can_route_active_capture_without_reopening(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()
    received = []

    assert manager.start_if_enabled() is True
    stream = _FakeInputStream.instances[-1]

    attached = manager.attach_active_capture(
        lambda indata, frames, time_info, status: received.append(
            (indata, frames, time_info, status)
        ),
        sample_rate=16000,
        target_channels=1,
        block_size=stream.kwargs["blocksize"],
        device="0",
    )

    assert attached is not None
    assert len(_FakeInputStream.instances) == 1

    data = np.zeros((512, 1), dtype=np.int16)
    stream.kwargs["callback"](data, 512, {"t": 1}, None)

    assert received == [(data, 512, {"t": 1}, None)]

    assert manager.detach_active_capture() is True
    stream.kwargs["callback"](data, 512, {"t": 2}, None)

    assert len(received) == 1

    manager.stop()


def test_mic_prewarm_returns_recent_prebuffer_when_attached(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 32, raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    stream = _FakeInputStream.instances[-1]

    old_data = np.full((512, 1), 1, dtype=np.int16)
    recent_data = np.full((512, 1), 2, dtype=np.int16)
    stream.kwargs["callback"](old_data, 512, {"t": 1}, None)
    stream.kwargs["callback"](recent_data, 512, {"t": 2}, None)

    attached = manager.attach_active_capture(
        lambda *_args: None,
        sample_rate=16000,
        target_channels=1,
        block_size=stream.kwargs["blocksize"],
        device="0",
    )

    assert attached is not None
    assert attached["prebuffer_ms"] == 32.0
    assert len(attached["prebuffer_frames"]) == 1
    buffered_data, buffered_frames, buffered_time_info, buffered_status = attached["prebuffer_frames"][0]
    assert np.array_equal(buffered_data, recent_data)
    assert buffered_frames == 512
    assert buffered_time_info == {"t": 2}
    assert buffered_status is None

    second = manager.attach_active_capture(
        lambda *_args: None,
        sample_rate=16000,
        target_channels=1,
        block_size=stream.kwargs["blocksize"],
        device="0",
    )
    assert second is None

    manager.stop()


def test_mic_prewarm_rejects_active_capture_signature_mismatch(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True

    attached = manager.attach_active_capture(
        lambda *_args: None,
        sample_rate=48000,
        target_channels=1,
        block_size=512,
        device="0",
    )

    assert attached is None
    assert manager.is_active is True

    manager.stop()


def test_mic_prewarm_stays_paused_during_device_refresh(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()
    assert manager.start_if_enabled() is True
    manager.quiesce_for_device_refresh()

    assert manager.is_active is False
    assert manager.start_if_enabled() is False
    assert len(_FakeInputStream.instances) == 1

    assert manager.resume_after_device_refresh() is True
    assert manager.is_active is True
    assert len(_FakeInputStream.instances) == 2

    manager.stop()


def test_mic_prewarm_honors_disabled_setting(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is False
    assert manager.is_active is False
    assert _FakeInputStream.instances == []


def test_mic_prewarm_watchdog_restarts_inactive_idle_stream(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    first_stream = _FakeInputStream.instances[-1]
    first_stream.active = False

    assert manager.ensure_healthy(reason="test") is True

    assert len(_FakeInputStream.instances) == 2
    assert first_stream.closed is True
    assert _FakeInputStream.instances[-1].active is True

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["active"] is True
    assert snapshot["hasStream"] is True
    assert snapshot["lastHealthCheckReason"] == "test"
    assert snapshot["lastHealthFailureReason"] == "inactiveStream"

    manager.stop()


def test_mic_prewarm_watchdog_records_missing_idle_stream(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    first_stream = _FakeInputStream.instances[-1]
    with manager._lock:
        manager._release_stream()
        first_stream.stop()
        first_stream.close()
        manager._stream = None
        manager._stream_signature = {}

    assert manager.ensure_healthy(reason="test-missing") is True

    assert len(_FakeInputStream.instances) == 2
    assert _FakeInputStream.instances[-1].active is True
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["healthRestartCount"] == 1
    assert snapshot["lastHealthCheckReason"] == "test-missing"
    assert snapshot["lastHealthFailureReason"] == "missingPrewarmStream"
    assert snapshot["lastStatus"] == "missingPrewarmStream"

    manager.stop()


def test_mic_prewarm_watchdog_restarts_idle_stream_when_callbacks_never_arrive(monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    manager = MicrophonePrewarmManager()

    assert manager.start_if_enabled() is True
    first_stream = _FakeInputStream.instances[-1]
    manager._stream_started_at = 100.0
    manager._last_callback_at = 0.0
    monkeypatch.setattr(mic_prewarm.time, "monotonic", lambda: 120.0)

    assert manager.ensure_healthy(reason="test", max_callback_gap_seconds=10.0) is True

    assert len(_FakeInputStream.instances) == 2
    assert first_stream.closed is True
    assert _FakeInputStream.instances[-1].active is True

    manager.stop()


def test_rust_audio_prewarm_manager_adopts_session_without_stopping(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {
                "success": True,
                "payload": {
                    "prewarmId": "prewarm-1",
                    "source": "wasapi-prewarm",
                    "prebufferFrameTarget": 40,
                },
            }
        if command == "audioPrewarmStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "prewarmId": payload["prewarmId"],
                    "reason": "prewarmStop",
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    assert manager.start_if_enabled() is True
    assert manager.is_active is True
    adopted = manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    )

    assert adopted is not None
    assert adopted["prewarmId"] == "prewarm-1"
    assert manager.is_active is False
    assert [command for command, _payload in commands] == ["audioPrewarmStart"]
    assert commands[0][1]["prebufferMs"] == 400
    assert commands[0][1]["nativeEndpointIdHash"] == "endpoint-hash"
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["engine"] == "rust-prototype"
    assert snapshot["activeCaptureAttached"] is True
    assert snapshot["adoptionCount"] == 1
    assert snapshot["lastAdoptedPrewarmIdHash"]
    assert "prewarm-1" not in str(snapshot)


def test_rust_audio_prewarm_manager_keeps_default_when_native_mapping_unavailable(
    monkeypatch,
):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    monkeypatch.setattr(
        mic_prewarm,
        "collect_native_capture_endpoint_inventory",
        lambda: [],
    )

    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})

    payload = manager._device_selection_payload(
        "default",
        sample_rate=16000,
        channels=1,
    )

    assert payload["devicePreference"] == "default"
    assert payload["nativeEndpointIdHash"] is None


def test_rust_audio_prewarm_manager_pause_stops_sidecar_session(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-2"}}
        if command == "audioPrewarmStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "prewarmId": payload["prewarmId"],
                    "reason": "active_capture",
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    assert manager.start_if_enabled() is True
    manager.pause_for_active_capture()

    assert manager.is_active is False
    snapshot = manager.diagnostic_snapshot()
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
    ]
    assert commands[-1][1]["prewarmId"] == "prewarm-2"
    assert snapshot["lastStop"]["prewarmIdHash"]
    assert "prewarm-2" not in str(snapshot)


def test_rust_audio_prewarm_watchdog_queries_sidecar_status(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-status-ok"}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "prewarmId": payload["prewarmId"],
                    "reason": "active",
                    "bufferedBlocks": 4,
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(reason="test-watchdog") is True

    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStatus",
    ]
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["lastHealthCheckReason"] == "test-watchdog"
    assert snapshot["lastHealthCheckActive"] is True
    assert snapshot["lastHealthResponseMs"] is not None
    assert snapshot["lastStatus"]["active"] is True
    assert snapshot["lastStatus"]["prewarmIdHash"]
    assert "prewarm-status-ok" not in str(snapshot)


def test_rust_audio_prewarm_watchdog_restarts_missing_sidecar_session(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []
    start_ids = iter(["prewarm-old", "prewarm-new"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": False,
                    "prewarmId": payload["prewarmId"],
                    "reason": "noActivePrewarm",
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(reason="test-watchdog") is True

    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStatus",
        "audioPrewarmStart",
    ]
    assert manager.is_active is True
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["healthRestartCount"] == 1
    assert snapshot["streamStartCount"] == 2
    assert snapshot["lastHealthCheckActive"] is False
    assert snapshot["lastHealthError"] == "noActivePrewarm"
    assert snapshot["lastStatus"]["prewarmIdHash"]
    assert "prewarm-old" not in str(snapshot)
    assert "prewarm-new" not in str(snapshot)


def test_rust_audio_prewarm_watchdog_records_missing_cached_session(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []
    start_ids = iter(["prewarm-old", "prewarm-new"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    assert manager.start_if_enabled() is True
    with manager._lock:
        manager._prewarm_id = ""
        manager._prewarm_payload = {}
        manager._stream_signature = {}

    assert manager.ensure_healthy(reason="test-missing") is True

    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStart",
    ]
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["healthRestartCount"] == 1
    assert snapshot["lastHealthCheckReason"] == "test-missing"
    assert snapshot["lastHealthCheckActive"] is False
    assert snapshot["lastHealthError"] == "missingPrewarmSession"
    assert snapshot["active"] is True
    assert snapshot["prewarmIdHash"]
    assert "prewarm-old" not in str(snapshot)
    assert "prewarm-new" not in str(snapshot)
