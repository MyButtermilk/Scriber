import threading
import time
import types

import pytest

import src.mic_prewarm as mic_prewarm
from src.config import Config
from src.mic_prewarm import RustAudioPrewarmManager


class _FakeInputStream:
    instances: list[_FakeInputStream] = []

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
        InputStream=_FakeInputStream,
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


def test_default_device_selection_does_not_load_sounddevice(monkeypatch):
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    monkeypatch.setattr(mic_prewarm, "sd", None)
    monkeypatch.setattr(mic_prewarm, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(mic_prewarm, "_SOUNDDEVICE_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(
        mic_prewarm,
        "import_module",
        lambda _name: (_ for _ in ()).throw(AssertionError("must stay lazy")),
    )
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})

    payload = manager._device_selection_payload(
        "default",
        sample_rate=16000,
        channels=1,
    )

    assert payload == {
        "devicePreference": "default",
        "portAudioLabel": "",
        "nativeEndpointIdHash": None,
    }
    assert mic_prewarm._SOUNDDEVICE_IMPORT_ATTEMPTED is False


def test_concurrent_favorite_selection_imports_sounddevice_once(monkeypatch):
    import_calls = 0
    import_started = threading.Event()
    fake_sounddevice = types.SimpleNamespace()

    def import_sounddevice(name):
        nonlocal import_calls
        assert name == "sounddevice"
        import_calls += 1
        import_started.set()
        time.sleep(0.03)
        return fake_sounddevice

    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Dock Mic", raising=False)
    monkeypatch.setattr(mic_prewarm, "sd", None)
    monkeypatch.setattr(mic_prewarm, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(mic_prewarm, "_SOUNDDEVICE_IMPORT_ATTEMPTED", False)
    monkeypatch.setattr(mic_prewarm, "import_module", import_sounddevice)
    monkeypatch.setattr(
        mic_prewarm,
        "resolve_input_microphone_device",
        lambda module, **_kwargs: "7" if module is fake_sounddevice else None,
    )
    monkeypatch.setattr(
        mic_prewarm,
        "build_input_endpoint_mappings",
        lambda module, **_kwargs: [] if module is fake_sounddevice else None,
    )
    monkeypatch.setattr(
        mic_prewarm,
        "collect_native_capture_endpoint_inventory",
        lambda: [],
    )
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})
    results: list[dict] = []
    workers = [
        threading.Thread(
            target=lambda: results.append(
                manager._device_selection_payload(
                    "default",
                    sample_rate=16000,
                    channels=1,
                )
            )
        )
        for _ in range(4)
    ]

    for worker in workers:
        worker.start()
    assert import_started.wait(timeout=2)
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    assert import_calls == 1
    assert len(results) == 4
    assert all(result["devicePreference"] == "7" for result in results)


def test_rust_audio_prewarm_manager_adopts_session_without_stopping(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    commands: list[tuple[str, dict]] = []
    selection_calls: list[str] = []

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

    def selection(device_preference, **_kwargs):
        selection_calls.append(str(device_preference))
        return {
            "devicePreference": "default",
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        }

    monkeypatch.setattr(manager, "_device_selection_payload", selection)

    assert manager.start_if_enabled() is True
    assert selection_calls == ["default"]
    assert (
        manager.leased_capture_device_preference(
            sample_rate=16000,
            target_channels=1,
            block_size=160,
        )
        == "default"
    )
    prewarm_start_payload = next(payload for command, payload in commands if command == "audioPrewarmStart")
    assert "_configuredDevice" not in prewarm_start_payload
    assert "_favoriteMic" not in prewarm_start_payload
    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("warm adoption must not resolve devices")),
    )
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
    assert adopted["captureRoute"] == {
        "devicePreference": "default",
        "portAudioLabel": "Default Mic, Windows WASAPI",
        "nativeEndpointIdHash": "endpoint-hash",
    }
    assert adopted["routeGeneration"] == 1
    assert selection_calls == ["default"]
    assert manager.is_active is True
    assert manager.commit_active_capture("prewarm-1") is True
    assert manager.is_active is False
    assert [command for command, _payload in commands] == ["audioPrewarmStart"]
    assert commands[0][1]["prebufferMs"] == 400
    assert commands[0][1]["nativeEndpointIdHash"] == "endpoint-hash"
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["engine"] == "rust-wasapi"
    assert snapshot["activeCaptureAttached"] is True
    assert snapshot["adoptionCount"] == 1
    assert snapshot["adoptionCommitCount"] == 1
    assert snapshot["adoptionPending"] is False
    assert snapshot["lastAdoptedPrewarmIdHash"]
    assert "prewarm-1" not in str(snapshot)


def test_rust_audio_prewarm_warm_route_hint_rejects_stale_config(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic A", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "Favorite A", raising=False)
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})
    with manager._lock:
        manager._prewarm_id = "prewarm-route-hint"
        manager._stream_signature = {
            "sample_rate": 16000,
            "target_channels": 1,
            "block_size": 160,
            "device_preference": "7",
            "configured_device": "Mic A",
            "favorite_mic": "Favorite A",
        }

    assert (
        manager.leased_capture_device_preference(
            sample_rate=16000,
            target_channels=1,
            block_size=160,
        )
        == "7"
    )

    monkeypatch.setattr(Config, "FAVORITE_MIC", "Favorite B", raising=False)
    assert (
        manager.leased_capture_device_preference(
            sample_rate=16000,
            target_channels=1,
            block_size=160,
        )
        is None
    )


def test_rust_audio_prewarm_rejects_changed_device_then_restarts_with_new_identity(
    monkeypatch,
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic A", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    commands: list[tuple[str, dict]] = []
    start_ids = iter(["prewarm-mic-a", "prewarm-mic-b"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {
                "success": True,
                "payload": {
                    "prewarmId": next(start_ids),
                    "source": "wasapi-prewarm",
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

    def selection(device_preference, **_kwargs):
        if str(device_preference) in {"Mic A", "1"}:
            return {
                "devicePreference": "1",
                "portAudioLabel": "Mic A",
                "nativeEndpointIdHash": "endpoint-hash-a",
            }
        if str(device_preference) in {"Mic B", "2"}:
            return {
                "devicePreference": "2",
                "portAudioLabel": "Mic B",
                "nativeEndpointIdHash": "endpoint-hash-b",
            }
        raise AssertionError(device_preference)

    monkeypatch.setattr(manager, "_device_selection_payload", selection)

    assert manager.start_if_enabled() is True
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic B", raising=False)

    # The pipeline resolved Mic B while the cold-import prewarm still owns Mic A.
    # That stale audio must never be leased to the new capture.
    assert (
        manager.attach_active_capture(
            None,
            sample_rate=16000,
            target_channels=1,
            block_size=160,
            device="2",
        )
        is None
    )

    rejected = manager.diagnostic_snapshot()
    assert rejected["active"] is True
    assert rejected["activeCaptureAttached"] is False
    assert rejected["adoptionCount"] == 0
    assert rejected["adoptionDeviceIdentityRejectionCount"] == 1
    assert rejected["lastAdoptionRejectionReason"] == "device_route_generation_mismatch"
    assert rejected["recentEvents"][-1] == {
        "event": "adoption_rejected",
        "reason": "device_route_generation_mismatch",
        "prewarmIdHash": mic_prewarm._hash_diagnostic_hint("prewarm-mic-a"),
        "storedNativeEndpointIdHash": "endpoint-hash-a",
        "routeGeneration": 1,
        "ageSeconds": rejected["recentEvents"][-1]["ageSeconds"],
    }
    assert "prewarm-mic-a" not in str(rejected)

    # MicrophoneInput's existing non-adoption path pauses/stops the stale
    # session.  Resuming after capture creates a new prewarm for Mic B.
    manager.pause_for_active_capture()
    assert manager.resume_after_active_capture() is True
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
        "audioPrewarmStart",
    ]
    assert commands[0][1]["nativeEndpointIdHash"] == "endpoint-hash-a"
    assert commands[2][1]["nativeEndpointIdHash"] == "endpoint-hash-b"

    adopted = manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="2",
    )
    assert adopted is not None
    assert adopted["prewarmId"] == "prewarm-mic-b"
    assert manager.commit_active_capture("prewarm-mic-b") is True


def test_rust_audio_prewarm_rejects_stale_portaudio_route_until_device_refresh(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic A", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)

    def shell_call(command, _payload=None, **_kwargs):
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-index-shift"}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)

    def selection(device_preference, **_kwargs):
        return {
            "devicePreference": "4" if str(device_preference) == "4" else "1",
            "portAudioLabel": "Mic A",
            "nativeEndpointIdHash": "stable-endpoint-hash",
        }

    monkeypatch.setattr(manager, "_device_selection_payload", selection)
    assert manager.start_if_enabled() is True

    adopted = manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="4",
    )
    assert adopted is None
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["adoptionDeviceIdentityRejectionCount"] == 1
    assert snapshot["lastAdoptionRejectionReason"] == "device_route_generation_mismatch"


def test_rust_audio_prewarm_rejects_hashless_non_default_identity():
    assert RustAudioPrewarmManager._normalized_device_preference(0) == "0"
    assert (
        RustAudioPrewarmManager._device_identity_matches(
            {
                "device_preference": "1",
                "native_endpoint_id_hash": "",
            },
            {
                "devicePreference": "1",
                "nativeEndpointIdHash": None,
            },
        )
        is False
    )
    assert (
        RustAudioPrewarmManager._device_identity_matches(
            {
                "device_preference": "default",
                "native_endpoint_id_hash": "",
            },
            {
                "devicePreference": "default",
                "nativeEndpointIdHash": None,
            },
        )
        is True
    )


def test_rust_audio_prewarm_rejects_config_route_change_without_resolving_devices(
    monkeypatch,
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic B", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})
    with manager._lock:
        manager._prewarm_id = "prewarm-stale-route"
        manager._stream_signature = {
            "sample_rate": 16000,
            "target_channels": 1,
            "block_size": 160,
            "device_preference": "1",
            "native_endpoint_id_hash": "endpoint-a",
            "configured_device": "Mic A",
            "favorite_mic": "",
            "route_generation": 4,
        }

    monkeypatch.setattr(
        manager,
        "_device_selection_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("route mismatch must not resolve devices")),
    )
    assert (
        manager.attach_active_capture(
            None,
            sample_rate=16000,
            target_channels=1,
            block_size=160,
            device="1",
        )
        is None
    )

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["activeCaptureAttached"] is False
    assert snapshot["adoptionCount"] == 0
    assert snapshot["adoptionDeviceIdentityRejectionCount"] == 1
    assert snapshot["lastAdoptionRejectionReason"] == "device_route_generation_mismatch"
    assert "prewarm-stale-route" not in str(snapshot)


def test_rust_audio_prewarm_manager_adopts_temporary_session_without_always_on(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
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
                    "prewarmId": "prewarm-temporary",
                    "source": "wasapi-prewarm",
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

    assert manager.start_if_enabled(temporary=True, prebuffer_ms=999_999) is True
    assert commands[0][1]["prebufferMs"] == 6000
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["active"] is True
    assert snapshot["configured"] is False
    assert snapshot["temporaryIdlePrewarm"] is True

    adopted = manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    )

    assert adopted is not None
    assert adopted["prewarmId"] == "prewarm-temporary"
    assert manager.is_active is True
    assert manager.commit_active_capture("prewarm-temporary") is True
    assert manager.is_active is False
    assert [command for command, _payload in commands] == ["audioPrewarmStart"]
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["temporaryIdlePrewarm"] is False
    assert snapshot["adoptionCount"] == 1
    assert snapshot["adoptionCommitCount"] == 1
    assert "prewarm-temporary" not in str(snapshot)


def test_rust_audio_prewarm_manager_rolls_back_failed_capture_without_losing_session(
    monkeypatch,
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-rollback"}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "prewarmId": payload["prewarmId"],
                    "reason": "active",
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 160,
            "devicePreference": "default",
        },
    )
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
    assert (
        manager.attach_active_capture(
            None,
            sample_rate=16000,
            target_channels=1,
            block_size=160,
            device="default",
        )
        is not None
    )
    assert manager.rollback_active_capture("prewarm-rollback") is True

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["active"] is True
    assert snapshot["activeCaptureAttached"] is False
    assert snapshot["pausedForActiveCapture"] is False
    assert snapshot["adoptionPending"] is False
    assert snapshot["adoptionRollbackCount"] == 1
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStatus",
    ]
    assert "prewarm-rollback" not in str(snapshot)


def test_rust_audio_prewarm_manager_rejects_non_temporary_session_without_always_on(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})
    with manager._lock:
        manager._prewarm_id = "prewarm-stale"
        manager._stream_signature = {
            "sample_rate": 16000,
            "target_channels": 1,
            "block_size": 160,
        }
        manager._temporary_idle_prewarm = False

    assert (
        manager.attach_active_capture(
            None,
            sample_rate=16000,
            target_channels=1,
            block_size=160,
            device="default",
        )
        is None
    )


def test_rust_audio_prewarm_start_if_enabled_stops_disabled_paused_session(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
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
    with manager._lock:
        manager._prewarm_id = "prewarm-disabled"
        manager._prewarm_payload = {"prewarmId": "prewarm-disabled"}
        manager._stream_signature = {"sample_rate": 16000}
        manager._paused_for_active_capture = True
        manager._active_capture_attached = True

    assert manager.start_if_enabled() is False
    assert manager.is_active is False
    assert commands == [("audioPrewarmStop", {"prewarmId": "prewarm-disabled"})]

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["pausedForActiveCapture"] is False
    assert snapshot["activeCaptureAttached"] is False
    assert snapshot["lastStopReason"] == "disabled"
    assert snapshot["lastStopSuccess"] is True
    assert "prewarm-disabled" not in str(snapshot)


def test_rust_audio_prewarm_discards_late_start_after_disable(monkeypatch):
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
            monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False, raising=False)
            return {
                "success": True,
                "payload": {
                    "prewarmId": "prewarm-race",
                    "source": "wasapi-prewarm",
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

    assert manager.start_if_enabled() is False
    assert manager.is_active is False
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
    ]
    assert commands[1][1] == {"prewarmId": "prewarm-race"}

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["streamStartCount"] == 0
    assert snapshot["streamCloseCount"] == 1
    assert snapshot["lastStartSuccess"] is False
    assert snapshot["lastStopReason"] == "disabled_after_start"
    assert snapshot["lastStopSuccess"] is True
    assert snapshot["active"] is False
    assert "prewarm-race" not in str(snapshot)


def test_rust_audio_prewarm_coalesces_concurrent_start_attempts(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    entered = threading.Event()
    release = threading.Event()
    commands: list[str] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            entered.set()
            assert release.wait(timeout=2.0)
            return {"success": True, "payload": {"prewarmId": "prewarm-one"}}
        if command == "audioPrewarmStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 160,
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(manager.start_if_enabled()))
    thread.start()
    assert entered.wait(timeout=1.0)

    assert manager.start_if_enabled() is False
    release.set()
    thread.join(timeout=2.0)

    assert result == [True]
    assert commands == ["audioPrewarmStart"]
    assert manager.diagnostic_snapshot()["startInProgress"] is False
    manager.stop()


@pytest.mark.parametrize("shutdown_while_waiting", [False, True])
def test_route_restart_waits_for_invalidated_start_before_replacing(monkeypatch, shutdown_while_waiting):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "old-device")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "favorite")
    entered = threading.Event()
    release = threading.Event()
    invalidated = threading.Event()
    commands = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, dict(payload or {})))
        if command == "audioPrewarmStart":
            if payload["devicePreference"] == "old-device":
                entered.set()
                assert release.wait(timeout=2)
            return {"success": True, "payload": {"prewarmId": payload["devicePreference"]}}
        assert command == "audioPrewarmStop"
        return {"success": True, "payload": {"stopped": True}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": Config.MIC_DEVICE,
            "_configuredDevice": Config.MIC_DEVICE,
            "_favoriteMic": Config.FAVORITE_MIC,
            "nativeEndpointIdHash": Config.MIC_DEVICE,
        },
    )
    original_stop = manager.stop

    def observed_stop(*, reason="stop"):
        original_stop(reason=reason)
        invalidated.set()

    monkeypatch.setattr(manager, "stop", observed_stop)
    initial = []
    restarted = []
    start_thread = threading.Thread(target=lambda: initial.append(manager.start_if_enabled()))
    start_thread.start()
    assert entered.wait(timeout=1)
    monkeypatch.setattr(Config, "MIC_DEVICE", "new-device")
    restart_thread = threading.Thread(target=lambda: restarted.append(manager.restart_for_route_change()))
    restart_thread.start()
    try:
        assert invalidated.wait(timeout=1)
        assert restart_thread.is_alive()
        if shutdown_while_waiting:
            manager.stop(reason="shutdown")
    finally:
        release.set()
        start_thread.join(timeout=2)
        restart_thread.join(timeout=2)
    assert initial == [False]
    assert restarted == [not shutdown_while_waiting]
    if shutdown_while_waiting:
        assert [command for command, _ in commands] == ["audioPrewarmStart", "audioPrewarmStop"]
        assert manager.is_active is False
        return
    assert [command for command, _ in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
        "audioPrewarmStart",
    ]
    assert (
        manager.leased_capture_device_preference(
            sample_rate=16000,
            target_channels=1,
            block_size=256,
        )
        == "new-device"
    )
    assert (
        manager.attach_active_capture(
            sample_rate=16000,
            target_channels=1,
            block_size=256,
            device="old-device",
        )
        is None
    )
    assert (
        manager.attach_active_capture(
            sample_rate=16000,
            target_channels=1,
            block_size=256,
            device="new-device",
        )["prewarmId"]
        == "new-device"
    )
    manager.stop()


def test_invalidated_start_keeps_late_producer_owned_when_stop_is_unknown(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    commands = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            manager.stop(reason="settings_route_changed")
            return {"success": True, "payload": {"prewarmId": "late-producer"}}
        assert command == "audioPrewarmStop"
        return {"success": False, "errorCode": "transportError"}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": "default",
        },
    )
    assert manager.start_if_enabled() is False
    assert manager.is_active is True
    assert manager.diagnostic_snapshot()["stopPending"] is True
    assert manager.start_if_enabled() is False
    assert commands == ["audioPrewarmStart", "audioPrewarmStop"]


def test_stop_reserves_native_ownership_before_releasing_state_lock(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    entered_stop = threading.Event()
    release_stop = threading.Event()
    commands = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "owned-producer"}}
        assert command == "audioPrewarmStop"
        return {"success": True, "payload": {"stopped": True}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {"sampleRate": 16000, "channels": 1, "blockSize": 256, "devicePreference": "default"},
    )
    assert manager.start_if_enabled()
    original_stop_owned = manager._stop_owned_prewarm

    def delayed_stop(prewarm_id, *, reason):
        entered_stop.set()
        assert release_stop.wait(timeout=2)
        return original_stop_owned(prewarm_id, reason=reason)

    monkeypatch.setattr(manager, "_stop_owned_prewarm", delayed_stop)
    stop_thread = threading.Thread(target=manager.stop)
    stop_thread.start()
    try:
        assert entered_stop.wait(timeout=1)
        assert manager.is_active
        assert manager.diagnostic_snapshot()["stopPending"] is True
        assert manager.start_if_enabled() is False
        assert commands == ["audioPrewarmStart"]
    finally:
        release_stop.set()
        stop_thread.join(timeout=2)
    assert manager.is_active is False
    assert manager.diagnostic_snapshot()["stopPending"] is False
    assert commands == ["audioPrewarmStart", "audioPrewarmStop"]


def test_route_restart_retains_unconfirmed_native_stop_without_replacement(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    commands = []
    stop_confirmed = False

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "owned-prewarm"}}
        assert command == "audioPrewarmStop"
        return {"success": stop_confirmed, "payload": {"stopped": stop_confirmed}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": "default",
        },
    )
    assert manager.start_if_enabled()
    assert manager.restart_for_route_change() is False
    assert manager.is_active
    assert manager.start_if_enabled() is False
    assert commands.count("audioPrewarmStart") == 1
    stop_confirmed = True
    assert manager.restart_for_route_change() is True
    assert commands.count("audioPrewarmStart") == 2
    manager.stop()


def test_queued_route_restart_does_not_resurrect_after_shutdown(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    queued = threading.Event()
    gate = threading.Lock()
    commands = []

    class ObservedLock:
        def __enter__(self):
            queued.set()
            gate.acquire()

        def __exit__(self, *_args):
            gate.release()

    manager = RustAudioPrewarmManager(shell_call=lambda command, *_args, **_kwargs: commands.append(command))
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": "default",
        },
    )
    manager._route_restart_lock = ObservedLock()
    result = []
    gate.acquire()
    worker = threading.Thread(target=lambda: result.append(manager.restart_for_route_change()))
    worker.start()
    try:
        assert queued.wait(timeout=1)
        manager.stop(reason="shutdown")
    finally:
        gate.release()
        worker.join(timeout=2)
    assert result == [False]
    assert commands == []
    assert manager.is_active is False


def test_queued_route_restart_preserves_capture_admitted_before_worker_runs(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "old-route")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "favorite")
    queued = threading.Event()
    gate = threading.Lock()
    commands = []

    class ObservedLock:
        def __enter__(self):
            queued.set()
            gate.acquire()

        def __exit__(self, *_args):
            gate.release()

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        return {"success": True, "payload": {"prewarmId": "admitted-prewarm", "stopped": True}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": Config.MIC_DEVICE,
            "_configuredDevice": Config.MIC_DEVICE,
            "_favoriteMic": Config.FAVORITE_MIC,
        },
    )
    assert manager.start_if_enabled()
    manager._route_restart_lock = ObservedLock()
    result = []
    gate.acquire()
    worker = threading.Thread(target=lambda: result.append(manager.restart_for_route_change()))
    worker.start()
    try:
        assert queued.wait(timeout=1)
        adopted = manager.attach_active_capture(
            sample_rate=16000,
            target_channels=1,
            block_size=256,
            device="old-route",
        )
        assert adopted is not None
        monkeypatch.setattr(Config, "MIC_DEVICE", "new-route")
    finally:
        gate.release()
        worker.join(timeout=2)
    assert result == [False]
    assert manager.commit_active_capture(adopted["prewarmId"]) is True
    assert commands == ["audioPrewarmStart"]


def test_stale_stop_helper_cannot_erase_newer_unconfirmed_producer(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    entered_stale = threading.Event()
    release_stale = threading.Event()
    starts = 0
    commands = []

    def shell_call(command, payload=None, **_kwargs):
        nonlocal starts
        commands.append((command, dict(payload or {})))
        if command == "audioPrewarmStart":
            starts += 1
            return {"success": True, "payload": {"prewarmId": f"producer-{starts}"}}
        assert command == "audioPrewarmStop"
        confirmed = payload["prewarmId"] != "producer-2"
        return {"success": confirmed, "payload": {"stopped": confirmed}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": "default",
        },
    )
    assert manager.start_if_enabled()
    original_stop_owned = manager._stop_owned_prewarm

    def delayed_stop(prewarm_id, *, reason):
        if threading.current_thread().name == "stale-stop":
            entered_stale.set()
            assert release_stale.wait(timeout=2)
        return original_stop_owned(prewarm_id, reason=reason)

    monkeypatch.setattr(manager, "_stop_owned_prewarm", delayed_stop)
    stale = threading.Thread(target=manager.stop, name="stale-stop")
    stale.start()
    try:
        assert entered_stale.wait(timeout=1)
        manager.stop()
        assert manager.start_if_enabled()
        manager.stop()
        assert manager.diagnostic_snapshot()["stopPending"] is True
    finally:
        release_stale.set()
        stale.join(timeout=2)
    assert manager.is_active
    assert manager.start_if_enabled() is False
    assert starts == 2
    assert [payload.get("prewarmId") for command, payload in commands if command == "audioPrewarmStop"] == [
        "producer-1",
        "producer-2",
    ]


def test_queued_route_change_replaces_prior_restart_with_latest_route(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "first-route")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "favorite")
    first_started = threading.Event()
    release_first = threading.Event()
    second_queued = threading.Event()
    gate = threading.Lock()
    entries = 0
    commands = []

    class ObservedLock:
        def __enter__(self):
            nonlocal entries
            entries += 1
            if entries == 2:
                second_queued.set()
            gate.acquire()

        def __exit__(self, *_args):
            gate.release()

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, dict(payload or {})))
        if command == "audioPrewarmStart":
            if payload["devicePreference"] == "first-route":
                first_started.set()
                assert release_first.wait(timeout=2)
            return {"success": True, "payload": {"prewarmId": payload["devicePreference"]}}
        assert command == "audioPrewarmStop"
        return {"success": True, "payload": {"stopped": True}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    manager._route_restart_lock = ObservedLock()
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 256,
            "devicePreference": Config.MIC_DEVICE,
            "_configuredDevice": Config.MIC_DEVICE,
            "_favoriteMic": Config.FAVORITE_MIC,
            "nativeEndpointIdHash": Config.MIC_DEVICE,
        },
    )
    results = []
    first = threading.Thread(target=lambda: results.append(manager.restart_for_route_change()))
    second = threading.Thread(target=lambda: results.append(manager.restart_for_route_change()))
    first.start()
    assert first_started.wait(timeout=1)
    monkeypatch.setattr(Config, "MIC_DEVICE", "latest-route")
    second.start()
    try:
        assert second_queued.wait(timeout=1)
    finally:
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
    assert results == [True, True]
    assert (
        manager.leased_capture_device_preference(
            sample_rate=16000,
            target_channels=1,
            block_size=256,
        )
        == "latest-route"
    )
    assert [command for command, _ in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
        "audioPrewarmStart",
    ]
    manager.stop()


def test_rust_audio_prewarm_stop_invalidates_late_start_response(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    entered = threading.Event()
    release = threading.Event()
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            entered.set()
            assert release.wait(timeout=2.0)
            return {"success": True, "payload": {"prewarmId": "prewarm-late"}}
        if command == "audioPrewarmStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 160,
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(manager.start_if_enabled()))
    thread.start()
    assert entered.wait(timeout=1.0)

    manager.stop(reason="shutdown")
    release.set()
    thread.join(timeout=2.0)

    assert result == [False]
    assert manager.is_active is False
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
    ]
    assert commands[-1][1] == {"prewarmId": "prewarm-late"}
    assert manager.diagnostic_snapshot()["startInProgress"] is False


def test_rust_audio_prewarm_closes_sidecar_if_post_start_commit_fails(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-malformed"}}
        if command == "audioPrewarmStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            # Deliberately omit sampleRate so commit validation raises after
            # the sidecar already returned a live prewarm session.
            "channels": 1,
            "blockSize": 160,
            "devicePreference": "default",
        },
    )

    assert manager.start_if_enabled() is False
    assert manager.is_active is False
    assert [command for command, _payload in commands] == [
        "audioPrewarmStart",
        "audioPrewarmStop",
    ]
    assert commands[-1][1] == {"prewarmId": "prewarm-malformed"}


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
    assert manager.commit_active_capture("prewarm-capture") is True

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


def test_deferred_device_refresh_retry_preserves_prewarm_resume(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    commands: list[str] = []
    start_ids = iter(["before-undock", "after-redock"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "prewarmId": payload["prewarmId"],
                    "reason": "device_refresh",
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

    # DeviceMonitor quiesces before attempting its guarded PortAudio refresh.
    # If that attempt is deferred, its retry quiesces again before resuming.
    manager.quiesce_for_device_refresh()
    assert manager.is_active is False
    manager.quiesce_for_device_refresh()

    assert manager.resume_after_device_refresh() is True
    assert manager.is_active is True
    assert commands == [
        "audioPrewarmStart",
        "audioPrewarmStop",
        "audioPrewarmStart",
    ]
    assert manager.diagnostic_snapshot()["deviceRefreshPauseCount"] == 1
    assert manager.diagnostic_snapshot()["deviceRefreshResumeCount"] == 1


def test_rust_audio_prewarm_stop_does_not_report_failed_transport_as_stopped(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)

    def shell_call(command, payload=None, **_kwargs):
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-stop-failure"}}
        if command == "audioPrewarmStop":
            return {
                "success": False,
                "errorCode": "transportError",
                "fallbackReason": "pipe busy",
                "payload": {},
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monkeypatch.setattr(
        manager,
        "_build_start_payload",
        lambda: {
            "sampleRate": 16000,
            "channels": 1,
            "blockSize": 160,
            "devicePreference": "default",
        },
    )

    assert manager.start_if_enabled() is True
    manager.stop(reason="test")

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["lastStopSuccess"] is False
    assert snapshot["lastStopError"] == "pipe busy"
    assert snapshot["lastTransition"] == "close_error"
    assert snapshot["recentEvents"][-1]["event"] == "stop_failed"


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


@pytest.mark.parametrize(
    ("observed_frames", "last_frame_age_ms", "uptime_ms", "stalled"),
    [
        (0, None, 1_000, False),
        (0, None, 20_000, True),
        (6_400, 1, 20_000, False),
        (6_400, 20_000, 30_000, True),
    ],
)
@pytest.mark.parametrize("prebuffer_ms", [0, 400])
def test_prewarm_watchdog_requires_recent_audio_frames(
    monkeypatch, observed_frames, last_frame_age_ms, uptime_ms, stalled, prebuffer_ms
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", prebuffer_ms, raising=False)
    commands: list[str] = []
    start_ids = iter(["before-redock", "recovered"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "workerFinished": False,
                    "reason": "active",
                    "prewarmId": payload["prewarmId"],
                    "totalAudioFramesObserved": observed_frames,
                    "lastAudioFrameAgoMs": last_frame_age_ms,
                    "sidecarUptimeMs": uptime_ms,
                    "bufferedBlocks": 40 if prebuffer_ms and observed_frames else 0,
                },
            }
        if command == "audioPrewarmStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(max_callback_gap_seconds=15.0) is True
    assert commands == (
        ["audioPrewarmStart", "audioPrewarmStatus", "audioPrewarmStop", "audioPrewarmStart"]
        if stalled
        else ["audioPrewarmStart", "audioPrewarmStatus"]
    )
    assert manager.diagnostic_snapshot()["healthRestartCount"] == int(stalled)
    if stalled:
        assert manager.diagnostic_snapshot()["lastHealthError"] == "prewarmAudioStalled"


def test_stalled_prewarm_waits_for_confirmed_native_stop_before_replacement(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    commands: list[str] = []
    stop_confirmed = False
    start_ids = iter(["stalled", "recovered"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "totalAudioFramesObserved": 0,
                    "lastAudioFrameAgoMs": None,
                    "sidecarUptimeMs": 20_000,
                },
            }
        if command == "audioPrewarmStop":
            return {"success": stop_confirmed, "payload": {"stopped": stop_confirmed}}
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(max_callback_gap_seconds=15.0) is False
    assert manager.diagnostic_snapshot()["stopPending"] is True
    assert commands == ["audioPrewarmStart", "audioPrewarmStatus", "audioPrewarmStop"]

    stop_confirmed = True
    assert manager.ensure_healthy(max_callback_gap_seconds=15.0) is True
    assert commands[-2:] == ["audioPrewarmStop", "audioPrewarmStart"]
    assert manager.diagnostic_snapshot()["stopPending"] is False


def test_native_status_pending_stop_retains_owner_until_confirmed(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "")
    commands = []
    stop_confirmed = False

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": f"producer-{commands.count(command)}"}}
        assert payload["prewarmId"] == "producer-1"
        if command == "audioPrewarmStatus":
            # Native status retired this entry but still owns a process whose
            # termination could not be confirmed. Inactive does not mean free.
            return {
                "success": False,
                "errorCode": "audioPrewarmStopPending",
                "payload": {"active": False, "reason": "prewarmStopPending"},
            }
        assert command == "audioPrewarmStop"
        return {"success": True, "payload": {"stopped": stop_confirmed}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy() is False
    assert manager.diagnostic_snapshot()["stopPending"] is True
    assert manager.start_if_enabled() is False
    assert manager.attach_active_capture(sample_rate=16000, target_channels=1, block_size=160, device="default") is None
    assert commands == ["audioPrewarmStart", "audioPrewarmStatus", "audioPrewarmStop"]

    stop_confirmed = True
    assert manager.ensure_healthy() is True
    assert commands[-2:] == ["audioPrewarmStop", "audioPrewarmStart"]
    assert commands.count("audioPrewarmStart") == 2
    assert manager.diagnostic_snapshot()["stopPending"] is False


@pytest.mark.parametrize(
    "retry_response",
    [
        {"success": True, "payload": {"stopped": False, "reason": "noActivePrewarm"}},
        {"success": True, "payload": {}},
        {"success": True, "payload": {"stopped": "true"}},
        {"success": True, "payload": {"stopped": 1}},
        {"success": False, "payload": {"stopped": True}},
    ],
)
def test_timed_out_prewarm_stop_requires_explicit_completion_before_retry(monkeypatch, retry_response):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "")
    commands = []
    stop_attempts = 0
    stop_confirmed = False

    def shell_call(command, payload=None, **_kwargs):
        nonlocal stop_attempts
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": f"producer-{commands.count(command)}"}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "totalAudioFramesObserved": 0,
                    "sidecarUptimeMs": 20_000,
                },
            }
        assert command == "audioPrewarmStop"
        assert payload["prewarmId"] == "producer-1"
        stop_attempts += 1
        if stop_attempts == 1:
            # The IPC timeout does not cancel the native worker's cleanup.
            raise TimeoutError("native stop still running")
        if stop_confirmed:
            return {"success": True, "payload": {"stopped": True}}
        # A removed session-map entry is not evidence that its process exited.
        return retry_response

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(max_callback_gap_seconds=15.0) is False
    assert manager.diagnostic_snapshot()["stopPending"] is True

    for _ in range(2):
        assert manager.ensure_healthy() is False
        assert manager.start_if_enabled() is False
        assert (
            manager.attach_active_capture(sample_rate=16000, target_channels=1, block_size=160, device="default")
            is None
        )
        snapshot = manager.diagnostic_snapshot()
        assert snapshot["stopPending"] is True
        assert snapshot["lastStopSuccess"] is False
        assert snapshot["lastStopError"]
        assert commands.count("audioPrewarmStart") == 1

    stop_confirmed = True
    assert manager.ensure_healthy() is True
    assert commands[-2:] == ["audioPrewarmStop", "audioPrewarmStart"]
    assert commands.count("audioPrewarmStart") == 2
    assert manager.diagnostic_snapshot()["stopPending"] is False


@pytest.mark.parametrize("capture_before_resume", [False, True])
def test_refresh_during_stalled_stop_preserves_resume_and_capture_owner(monkeypatch, capture_before_resume):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "")
    first_stop_entered = threading.Event()
    finish_first_stop = threading.Event()
    commands = []
    stop_attempts = 0

    def shell_call(command, payload=None, **_kwargs):
        nonlocal stop_attempts
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": f"producer-{commands.count(command)}"}}
        if command == "audioPrewarmStatus":
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "totalAudioFramesObserved": 0,
                    "sidecarUptimeMs": 20_000,
                },
            }
        assert command == "audioPrewarmStop"
        assert payload["prewarmId"] == "producer-1"
        stop_attempts += 1
        if stop_attempts == 1:
            first_stop_entered.set()
            assert finish_first_stop.wait(timeout=5)
        return {"success": True, "payload": {"stopped": True}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    health_results = []
    watchdog = threading.Thread(target=lambda: health_results.append(manager.ensure_healthy()))
    watchdog.start()
    try:
        assert first_stop_entered.wait(timeout=2)
        assert manager.diagnostic_snapshot()["stopPending"] is True
        assert manager.diagnostic_snapshot()["active"] is False
        manager.quiesce_for_device_refresh()
        manager.quiesce_for_device_refresh()
        assert (
            manager.attach_active_capture(sample_rate=16000, target_channels=1, block_size=160, device="default")
            is None
        )
        if capture_before_resume:
            # A non-adopting hotkey owns capture while refresh completion waits.
            manager.pause_for_active_capture()
    finally:
        finish_first_stop.set()
        watchdog.join(timeout=5)
    assert not watchdog.is_alive()
    assert health_results == [False]
    assert commands.count("audioPrewarmStart") == 1
    assert manager.resume_after_device_refresh() is (not capture_before_resume)
    assert manager.diagnostic_snapshot()["deviceRefreshPauseCount"] == 1
    assert manager.diagnostic_snapshot()["deviceRefreshResumeCount"] == 1
    assert manager.resume_after_device_refresh() is False

    if capture_before_resume:
        assert manager.ensure_healthy() is False
        assert commands.count("audioPrewarmStart") == 1
        assert manager.diagnostic_snapshot()["pausedForActiveCapture"] is True
        assert manager.resume_after_active_capture() is True
    assert commands.count("audioPrewarmStart") == 2
    assert manager.is_active is True


@pytest.mark.parametrize("transition", ["adopt", "commit", "replace", "shutdown"])
def test_stale_prewarm_health_response_cannot_interrupt_new_owner(monkeypatch, transition):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(Config, "FAVORITE_MIC", "", raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    commands: list[str] = []
    start_ids = iter(["old-route", "new-route"])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": next(start_ids)}}
        if command == "audioPrewarmStop":
            return {"success": True, "payload": {"stopped": True}}
        if command == "audioPrewarmStatus":
            if transition in {"adopt", "commit"}:
                assert manager.attach_active_capture(
                    None, sample_rate=16000, target_channels=1, block_size=160, device="default"
                )
                if transition == "commit":
                    assert manager.commit_active_capture("old-route") is True
            else:
                manager.stop(reason="shutdown" if transition == "shutdown" else "settings_route_changed")
                if transition == "replace":
                    assert manager.start_if_enabled() is True
            return {
                "success": True,
                "payload": {
                    "active": True,
                    "totalAudioFramesObserved": 0,
                    "lastAudioFrameAgoMs": None,
                    "sidecarUptimeMs": 20_000,
                },
            }
        raise AssertionError(command)

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    assert manager.start_if_enabled() is True
    assert manager.ensure_healthy(max_callback_gap_seconds=15.0) is False
    assert manager.diagnostic_snapshot()["healthRestartCount"] == 0
    expected = ["audioPrewarmStart", "audioPrewarmStatus"]
    if transition not in {"adopt", "commit"}:
        expected.append("audioPrewarmStop")
    if transition == "replace":
        expected.append("audioPrewarmStart")
    assert commands == expected
    if transition in {"adopt", "commit"}:
        assert manager.diagnostic_snapshot()["activeCaptureAttached"] is True
        assert manager.ensure_healthy() is False
        assert commands == expected
    if transition == "commit":
        import src.device_monitor as device_monitor

        monitor = device_monitor.DeviceMonitor()
        monitor.on_portaudio_refresh_quiesce(
            manager.quiesce_for_device_refresh,
            manager.resume_after_device_refresh,
        )
        monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", lambda: (False, True))
        monkeypatch.setattr(device_monitor, "get_active_stream_count", lambda: 1)
        monkeypatch.setattr(device_monitor, "_enumerate_microphones", lambda **_kwargs: [])
        monitor._refresh_devices(trigger="device_removed", force=False)
        monitor._refresh_devices(trigger="device_added", force=False)
        monitor._refresh_devices(trigger="poll", force=False, refresh_portaudio=False)
        assert monitor.diagnostic_snapshot()["refreshDeferredUntilIdle"] is True
        assert monitor._take_deferred_refresh_trigger_if_idle() is None
        assert manager.diagnostic_snapshot()["activeCaptureAttached"] is True
        assert manager.ensure_healthy() is False
        assert commands == expected


def test_device_refresh_preserves_native_adoption_until_python_commit(monkeypatch):
    import src.device_monitor as device_monitor

    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default")
    monkeypatch.setattr(Config, "FAVORITE_MIC", "")
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000)
    monkeypatch.setattr(Config, "CHANNELS", 1)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160)
    commands = []
    stream_active = True

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": f"producer-{commands.count(command)}"}}
        assert command == "audioPrewarmStop"
        # Native capture has consumed the producer, but the first live frame
        # has not yet allowed Python to commit that provisional adoption.
        return {"success": True, "payload": {"stopped": False, "reason": "noActivePrewarm"}}

    manager = RustAudioPrewarmManager(shell_call=shell_call)
    monitor = device_monitor.DeviceMonitor()
    monitor.on_portaudio_refresh_quiesce(
        manager.quiesce_for_device_refresh,
        manager.resume_after_device_refresh,
    )
    monkeypatch.setattr(device_monitor, "_refresh_portaudio_cache", lambda: (not stream_active, stream_active))
    monkeypatch.setattr(device_monitor, "get_active_stream_count", lambda: int(stream_active))
    monkeypatch.setattr(device_monitor, "_enumerate_microphones", lambda **_kwargs: [])

    assert manager.start_if_enabled() is True
    adopted = manager.attach_active_capture(sample_rate=16000, target_channels=1, block_size=160, device="default")
    assert adopted is not None
    monitor._refresh_devices(trigger="device_removed", force=False)
    monitor._refresh_devices(trigger="device_added", force=False)
    monitor._refresh_devices(trigger="poll", force=False, refresh_portaudio=False)
    assert monitor.diagnostic_snapshot()["refreshDeferredUntilIdle"] is True
    assert monitor._take_deferred_refresh_trigger_if_idle() is None

    assert manager.commit_active_capture(adopted["prewarmId"]) is True
    assert manager.diagnostic_snapshot()["stopPending"] is False
    assert manager.diagnostic_snapshot()["activeCaptureAttached"] is True
    assert manager.ensure_healthy() is False
    assert commands == ["audioPrewarmStart"]

    stream_active = False
    trigger = monitor._take_deferred_refresh_trigger_if_idle()
    assert trigger is not None
    monitor._refresh_devices(trigger=trigger, force=False)
    assert manager.resume_after_active_capture() is True
    assert commands == ["audioPrewarmStart", "audioPrewarmStart"]
    assert manager.diagnostic_snapshot()["stopPending"] is False


def test_rust_audio_prewarm_watchdog_keeps_session_on_transport_error(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "default", raising=False)
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioPrewarmStart":
            return {"success": True, "payload": {"prewarmId": "prewarm-transport"}}
        if command == "audioPrewarmStatus":
            return {
                "success": False,
                "errorCode": "transportError",
                "fallbackReason": "RuntimeError: OSError: [Errno 22] Invalid argument",
                "payload": {},
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
    assert manager.is_active is True
    snapshot = manager.diagnostic_snapshot()
    assert snapshot["healthRestartCount"] == 0
    assert snapshot["streamStartCount"] == 1
    assert snapshot["lastHealthCheckActive"] is True
    assert "Invalid argument" in snapshot["lastHealthError"]
    assert any(
        event["event"] == "health_status_unknown" and event["errorCode"] == "transportError"
        for event in snapshot["recentEvents"]
    )
    assert "prewarm-transport" not in str(snapshot)


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
    assert any(event["event"] == "health_restart" and event["healthError"] == "noActivePrewarm" for event in events)
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
