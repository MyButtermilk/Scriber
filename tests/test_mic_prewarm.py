import types
import threading
import time

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


def test_rust_audio_prewarm_rejects_changed_device_then_restarts_with_new_identity(
    monkeypatch,
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic A", raising=False)
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
    assert manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="2",
    ) is None

    rejected = manager.diagnostic_snapshot()
    assert rejected["active"] is True
    assert rejected["activeCaptureAttached"] is False
    assert rejected["adoptionCount"] == 0
    assert rejected["adoptionDeviceIdentityRejectionCount"] == 1
    assert rejected["lastAdoptionRejectionReason"] == "device_identity_mismatch"
    assert rejected["recentEvents"][-1] == {
        "event": "adoption_rejected",
        "reason": "device_identity_mismatch",
        "prewarmIdHash": manager._hash_hint("prewarm-mic-a"),
        "storedNativeEndpointIdHash": "endpoint-hash-a",
        "requestedNativeEndpointIdHash": "endpoint-hash-b",
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


def test_rust_audio_prewarm_identity_uses_endpoint_hash_not_portaudio_index(monkeypatch):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(Config, "SAMPLE_RATE", 16000, raising=False)
    monkeypatch.setattr(Config, "CHANNELS", 1, raising=False)
    monkeypatch.setattr(Config, "MIC_BLOCK_SIZE", 160, raising=False)
    monkeypatch.setattr(Config, "MIC_DEVICE", "Mic A", raising=False)

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
    assert adopted is not None
    assert adopted["prewarmId"] == "prewarm-index-shift"
    assert manager.diagnostic_snapshot()["adoptionDeviceIdentityRejectionCount"] == 0


def test_rust_audio_prewarm_rejects_hashless_non_default_identity():
    assert RustAudioPrewarmManager._normalized_device_preference(0) == "0"
    assert RustAudioPrewarmManager._device_identity_matches(
        {
            "device_preference": "1",
            "native_endpoint_id_hash": "",
        },
        {
            "devicePreference": "1",
            "nativeEndpointIdHash": None,
        },
    ) is False
    assert RustAudioPrewarmManager._device_identity_matches(
        {
            "device_preference": "default",
            "native_endpoint_id_hash": "",
        },
        {
            "devicePreference": "default",
            "nativeEndpointIdHash": None,
        },
    ) is True


def test_rust_audio_prewarm_rejects_adoption_when_device_identity_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", True, raising=False)
    manager = RustAudioPrewarmManager(shell_call=lambda *_args, **_kwargs: {})
    with manager._lock:
        manager._prewarm_id = "prewarm-unresolved-device"
        manager._stream_signature = {
            "sample_rate": 16000,
            "target_channels": 1,
            "block_size": 160,
            "device_preference": "default",
            "native_endpoint_id_hash": "",
        }

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(manager, "_device_selection_payload", unavailable)
    assert manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    ) is None

    snapshot = manager.diagnostic_snapshot()
    assert snapshot["activeCaptureAttached"] is False
    assert snapshot["adoptionCount"] == 0
    assert snapshot["adoptionDeviceIdentityRejectionCount"] == 1
    assert snapshot["lastAdoptionRejectionReason"] == "device_identity_unavailable"
    assert "inventory unavailable" not in str(snapshot)
    assert "prewarm-unresolved-device" not in str(snapshot)


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
    assert manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    ) is not None
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

    assert manager.attach_active_capture(
        None,
        sample_rate=16000,
        target_channels=1,
        block_size=160,
        device="default",
    ) is None


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
        event["event"] == "health_status_unknown"
        and event["errorCode"] == "transportError"
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
