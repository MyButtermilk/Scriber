import types

from src.config import Config
from src.mic_prewarm import RustAudioPrewarmManager
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
    assert snapshot["engine"] == "rust-wasapi"
    assert snapshot["activeCaptureAttached"] is True
    assert snapshot["adoptionCount"] == 1
    assert snapshot["lastAdoptedPrewarmIdHash"]
    assert "prewarm-1" not in str(snapshot)


def test_rust_audio_prewarm_manager_records_resume_gap_after_capture(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []
    start_ids = iter(["prewarm-capture", "prewarm-resume"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStop":
            return {
                "success": True,
                "payload": {"stopped": True, "prewarmId": payload["prewarmId"]},
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
    adopted = manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    )
    assert adopted is not None

    assert manager.detach_active_capture(None) is False
    assert manager.resume_after_active_capture() is True

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["active"] is True
    assert snapshot["activeCaptureResumeCount"] == 1
    assert snapshot["activeCaptureResumeReadyCount"] == 1
    assert snapshot["activeCaptureResumeFailedCount"] == 0
    assert snapshot["lastActiveCaptureResumeGapMs"] is not None
    assert snapshot["lastActiveCaptureResumeGapMs"] >= 0
    assert snapshot["lastActiveCaptureStopToReadyMs"] is not None
    assert snapshot["lastActiveCaptureStopToReadyMs"] >= 0
    assert snapshot["maxActiveCaptureStopToReadyMs"] == snapshot["lastActiveCaptureStopToReadyMs"]
    assert snapshot["lastActiveCaptureDetachAgoSeconds"] is not None
    assert snapshot["lastActiveCaptureResumeAttemptAgoSeconds"] is not None
    events = snapshot["recentEvents"]
    assert any(
        event["event"] == "started"
        and event.get("activeCaptureResumeGapMs") is not None
        and event.get("activeCaptureStopToReadyMs") is not None
        for event in events
    )
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStart",
    ]
    assert "prewarm-capture" not in str(snapshot)
    assert "prewarm-resume" not in str(snapshot)

    manager.stop()


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


def test_rust_audio_prewarm_manager_does_not_default_resolved_favorite_without_native_hash(
    monkeypatch,
):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, MME", raising=False)
    monkeypatch.setattr(
        mic_prewarm,
        "resolve_input_microphone_device",
        lambda *_args, **_kwargs: "1",
    )
    monkeypatch.setattr(
        mic_prewarm,
        "collect_native_capture_endpoint_inventory",
        lambda: [],
    )
    monkeypatch.setattr(
        mic_prewarm,
        "build_input_endpoint_mappings",
        lambda *_args, **_kwargs: [
            types.SimpleNamespace(
                portaudio_index=0,
                portaudio_name="Built-in Mic, MME",
                native_endpoint_id_hash="default-hash",
                is_default=True,
            )
        ],
    )

    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})

    payload = manager._device_selection_payload(
        "default",
        sample_rate=16000,
        channels=1,
    )

    assert payload["devicePreference"] == "1"
    assert payload["nativeEndpointIdHash"] is None
    assert payload["portAudioLabel"] == ""


def test_rust_audio_prewarm_manager_uses_shell_inventory_for_resolved_favorite(
    monkeypatch,
):
    _install_fake_sounddevice(monkeypatch)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic, MME", raising=False)
    monkeypatch.setattr(
        mic_prewarm,
        "resolve_input_microphone_device",
        lambda *_args, **_kwargs: "1",
    )
    monkeypatch.setattr(
        mic_prewarm,
        "collect_native_capture_endpoint_inventory",
        lambda: [],
    )
    monkeypatch.setattr(
        mic_prewarm,
        "build_input_endpoint_mappings",
        lambda *_args, **_kwargs: [
            types.SimpleNamespace(
                portaudio_index=0,
                portaudio_name="Built-in Mic, MME",
                native_endpoint_id_hash="default-hash",
                is_default=True,
            )
        ],
    )
    commands: list[str] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioEndpointInventory":
            return {
                "success": True,
                "payload": {
                    "available": True,
                    "endpoints": [
                        {
                            "endpointIdHash": "dock-hash",
                            "friendlyName": "Dock Mic",
                            "flow": "capture",
                            "isDefault": False,
                        }
                    ],
                },
            }
        return {}

    manager = RustAudioPrewarmManager(shell_call=shell_call)

    payload = manager._device_selection_payload(
        "default",
        sample_rate=16000,
        channels=1,
    )

    assert payload["devicePreference"] == "1"
    assert payload["portAudioLabel"] == "Dock Mic, MME"
    assert payload["nativeEndpointIdHash"] == "dock-hash"
    assert commands == ["audioEndpointInventory"]


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
    events = snapshot["recentEvents"]
    assert any(
        event["event"] == "health_restart"
        and event["healthError"] == "noActivePrewarm"
        for event in events
    )
    assert sum(1 for event in events if event["event"] == "started") == 2
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
