import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import web_api
from src.web_api import ScriberWebController, TranscriptRecord


class _FakeMicPrewarmManager:
    instances: list["_FakeMicPrewarmManager"] = []

    def __init__(self):
        self.active = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.quiesce_calls = 0
        self.refresh_resume_calls = 0
        self.ensure_calls = 0
        type(self).instances.append(self)

    @property
    def is_active(self) -> bool:
        return self.active

    def pause_for_active_capture(self) -> None:
        self.pause_calls += 1
        self.active = False

    def resume_after_active_capture(self) -> bool:
        self.resume_calls += 1
        self.active = bool(web_api.Config.MIC_ALWAYS_ON)
        if not self.active:
            self.stop_calls += 1
        return self.active

    def quiesce_for_device_refresh(self) -> None:
        self.quiesce_calls += 1
        self.active = False

    def resume_after_device_refresh(self) -> bool:
        self.refresh_resume_calls += 1
        self.active = bool(web_api.Config.MIC_ALWAYS_ON)
        return self.active

    def ensure_healthy(self, *, reason: str = "watchdog", max_callback_gap_seconds=None) -> bool:
        self.ensure_calls += 1
        self.active = bool(web_api.Config.MIC_ALWAYS_ON)
        return self.active

    def diagnostic_snapshot(self) -> dict:
        return {
            "active": self.active,
            "ensureCalls": self.ensure_calls,
        }

    def attach_active_capture(self, *_args, **_kwargs):
        return None

    def detach_active_capture(self, *_args, **_kwargs):
        return self.active

    def stop(self) -> None:
        self.stop_calls += 1
        self.active = False


class _FakeRustMicPrewarmManager(_FakeMicPrewarmManager):
    engine = "rust-prototype"
    instances: list["_FakeRustMicPrewarmManager"] = []


def _install_fake_sounddevice_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: list[dict[str, object]],
    hostapis: list[dict[str, object]],
    default_input: int = 0,
) -> types.SimpleNamespace:
    module = types.SimpleNamespace()
    module.default = types.SimpleNamespace(device=(default_input, None), hostapi=0)

    def query_devices(device=None, kind=None):
        if device is None and kind is None:
            return devices
        idx = default_input if device is None else int(device)
        if idx < 0 or idx >= len(devices):
            raise ValueError("invalid device index")
        return devices[idx]

    module.query_devices = query_devices
    module.query_hostapis = lambda: hostapis
    module.check_input_settings = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


async def _wait_for_prewarm_task(ctl: ScriberWebController) -> None:
    for _ in range(100):
        task = ctl._mic_prewarm_task
        if task is None:
            return
        if task.done():
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("mic prewarm task did not finish")


def _make_record(session_id: str) -> TranscriptRecord:
    rec = TranscriptRecord(
        id=session_id,
        title="Live Mic",
        date="Today",
        duration="00:00",
        status="recording",
        type="mic",
        language="en",
    )
    rec.start()
    return rec


def test_transcript_record_buffers_final_segments_until_content_read():
    rec = _make_record("buffered-session")

    rec.append_final_text("first")
    rec.append_final_text("second")
    rec.append_final_text("third")

    assert rec.content == "first"
    assert rec._pending_content_segments == ["second", "third"]
    assert rec.content_text() == "first\n\nsecond\n\nthird"
    assert rec._pending_content_segments == []


class _ChunkUploadField:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read_chunk(self, *, size: int) -> bytes:
        del size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_write_upload_stream_to_disk_writes_chunks_off_hot_path(tmp_path):
    target = tmp_path / "upload.bin"
    field = _ChunkUploadField([b"abc", b"def"])

    bytes_read, too_large = await web_api._write_upload_stream_to_disk(
        field,
        target,
        max_bytes=16,
    )

    assert bytes_read == 6
    assert too_large is False
    assert target.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_write_upload_stream_to_disk_stops_before_oversized_chunk(tmp_path):
    target = tmp_path / "upload.bin"
    field = _ChunkUploadField([b"abc", b"def"])

    bytes_read, too_large = await web_api._write_upload_stream_to_disk(
        field,
        target,
        max_bytes=4,
    )

    assert bytes_read == 6
    assert too_large is True
    assert target.read_bytes() == b"abc"


@pytest.mark.asyncio
async def test_render_transcript_export_async_runs_renderer(monkeypatch):
    def fake_render(**kwargs):
        assert kwargs["export_format"] == "pdf"
        assert kwargs["title"] == "Title"
        return b"pdf", "application/pdf", "pdf"

    monkeypatch.setattr(web_api, "_render_transcript_export", fake_render)

    data, content_type, ext = await web_api._render_transcript_export_async(
        export_format="pdf",
        title="Title",
        content="Body",
        summary="Summary",
        date="Today",
        duration="00:01",
    )

    assert data == b"pdf"
    assert content_type == "application/pdf"
    assert ext == "pdf"


@pytest.mark.asyncio
async def test_update_settings_debounces_env_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC", "0.05")
    persist_mock = MagicMock()
    monkeypatch.setattr(web_api.Config, "persist_to_env_file", persist_mock)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    await ctl.update_settings({"language": "en"})
    await ctl.update_settings({"customVocab": "alpha beta"})

    assert persist_mock.call_count == 0

    await asyncio.sleep(0.08)

    assert persist_mock.call_count == 1
    ctl.shutdown()


@pytest.mark.asyncio
async def test_update_settings_flushes_pending_persist_on_shutdown(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC", "60")
    persist_mock = MagicMock()
    monkeypatch.setattr(web_api.Config, "persist_to_env_file", persist_mock)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    await ctl.update_settings({"language": "de"})

    assert persist_mock.call_count == 0

    ctl.shutdown()

    assert persist_mock.call_count == 1


@pytest.mark.asyncio
async def test_settings_round_trips_azure_mai_model(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC", "60")
    monkeypatch.setattr(web_api.Config, "AZURE_MAI_MODEL", "mai-transcribe-1.5", raising=False)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    settings = await ctl.update_settings({"apiKeys": {"azureMaiModel": "mai-transcribe-1"}})

    assert web_api.Config.AZURE_MAI_MODEL == "mai-transcribe-1"
    assert settings["apiKeys"]["azureMaiModel"] == "mai-transcribe-1"

    ctl.shutdown()


@pytest.mark.asyncio
async def test_controller_starts_idle_mic_prewarm_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(web_api, "MicrophonePrewarmManager", _FakeMicPrewarmManager)
    _FakeMicPrewarmManager.instances.clear()

    ctl = ScriberWebController(asyncio.get_running_loop())
    await _wait_for_prewarm_task(ctl)
    manager = _FakeMicPrewarmManager.instances[-1]

    assert manager.resume_calls == 1
    assert manager.active is True

    ctl.shutdown()

    assert manager.stop_calls >= 1
    assert manager.active is False


@pytest.mark.asyncio
async def test_controller_uses_rust_idle_prewarm_manager_for_rust_audio_engine(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-prototype")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(web_api, "RustAudioPrewarmManager", _FakeRustMicPrewarmManager)
    _FakeRustMicPrewarmManager.instances.clear()

    ctl = ScriberWebController(asyncio.get_running_loop())
    await _wait_for_prewarm_task(ctl)
    manager = _FakeRustMicPrewarmManager.instances[-1]

    assert manager.resume_calls == 1
    assert manager.active is True
    assert getattr(ctl._mic_prewarm, "engine") == "rust-prototype"

    ctl.shutdown()


@pytest.mark.asyncio
async def test_update_settings_toggles_idle_mic_prewarm(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", False, raising=False)
    monkeypatch.setattr(web_api.Config, "persist_to_env_file", MagicMock())
    monkeypatch.setattr(web_api, "MicrophonePrewarmManager", _FakeMicPrewarmManager)
    _FakeMicPrewarmManager.instances.clear()

    ctl = ScriberWebController(asyncio.get_running_loop())
    manager = _FakeMicPrewarmManager.instances[-1]

    await ctl.update_settings({"micAlwaysOn": True})

    assert web_api.Config.MIC_ALWAYS_ON is True
    assert manager.resume_calls == 1
    assert manager.active is True

    await ctl.update_settings({"micAlwaysOn": False})

    assert web_api.Config.MIC_ALWAYS_ON is False
    assert manager.resume_calls == 2
    assert manager.stop_calls >= 1
    assert manager.active is False

    ctl.shutdown()


@pytest.mark.asyncio
async def test_mic_watchdog_checks_idle_prewarm(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_MIC_WATCHDOG_INTERVAL_SEC", "0")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(web_api, "MicrophonePrewarmManager", _FakeMicPrewarmManager)
    _FakeMicPrewarmManager.instances.clear()

    ctl = ScriberWebController(asyncio.get_running_loop())
    await _wait_for_prewarm_task(ctl)
    manager = _FakeMicPrewarmManager.instances[-1]
    manager.active = False

    await ctl._run_mic_watchdog_check()

    assert manager.ensure_calls == 1
    assert manager.active is True
    assert ctl.get_audio_diagnostics()["microphone"]["prewarm"]["ensureCalls"] == 1

    ctl.shutdown()


@pytest.mark.asyncio
async def test_mic_watchdog_checks_active_pipeline(monkeypatch, tmp_path):
    class _FakePipeline:
        def __init__(self):
            self.health_calls = []

        def ensure_audio_health(self, **kwargs):
            self.health_calls.append(kwargs)
            return True

        def audio_diagnostics(self):
            return {"running": True, "streamActive": True}

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_MIC_WATCHDOG_INTERVAL_SEC", "0")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", False, raising=False)

    ctl = ScriberWebController(asyncio.get_running_loop())
    pipeline = _FakePipeline()
    ctl._pipeline = pipeline
    ctl._is_listening = True

    await ctl._run_mic_watchdog_check()

    assert pipeline.health_calls == [
        {"reason": "watchdog", "max_callback_gap_seconds": 15.0}
    ]
    assert ctl.get_audio_diagnostics()["microphone"]["activeCapture"]["streamActive"] is True

    ctl.shutdown()


@pytest.mark.asyncio
async def test_recording_state_transition_broadcasts_state_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_MIC_WATCHDOG_INTERVAL_SEC", "0")

    ctl = ScriberWebController(asyncio.get_running_loop())
    payloads = []

    async def fake_broadcast(payload):
        payloads.append(payload)

    ctl.broadcast = fake_broadcast
    ctl._set_recording_state(web_api.RecordingState.INITIALIZING, context="test")
    for _ in range(10):
        if payloads:
            break
        await asyncio.sleep(0.01)

    assert payloads
    assert payloads[-1]["type"] == "state"
    assert payloads[-1]["recordingState"] == "initializing"
    assert payloads[-1]["listening"] is False

    ctl.shutdown()


@pytest.mark.asyncio
async def test_on_pipeline_done_ignores_stale_task():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    stale_task = asyncio.create_task(asyncio.sleep(0))
    current_task = asyncio.create_task(asyncio.sleep(1))

    ctl._pipeline_task = current_task
    ctl._is_listening = True
    ctl._is_stopping = True
    ctl._session_id = "active-session"

    await stale_task
    ctl._on_pipeline_done(stale_task, session_id="old-session")
    await asyncio.sleep(0.01)

    assert ctl._pipeline_task is current_task
    assert ctl._is_listening is True
    assert ctl._is_stopping is True
    assert ctl._session_id == "active-session"

    current_task.cancel()
    await asyncio.gather(current_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_get_state_reports_background_processing_flag():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    pending = loop.create_future()
    ctl._running_tasks["bg-task"] = pending

    state = ctl.get_state()
    assert state["backgroundProcessing"] is True
    assert state["recordingState"] == "idle"

    pending.set_result(None)
    state = ctl.get_state()
    assert state["backgroundProcessing"] is False
    assert state["recordingState"] == "idle"


@pytest.mark.asyncio
async def test_runtime_and_health_contract_include_sidecar_fields():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    runtime = ctl.get_runtime_info()
    health = ctl.get_health()

    assert runtime["apiVersion"]
    assert runtime["version"] == web_api.app_version()
    assert runtime["workerVersion"]
    assert runtime["runtimeMode"] == "python-web"
    assert runtime["launchKind"] == "python-module"
    assert runtime["pid"] == health["pid"]
    assert runtime["host"] == "127.0.0.1"
    assert runtime["port"] == 8765
    assert runtime["startedAt"].endswith("Z")
    assert runtime["uptimeSeconds"] >= 0
    assert runtime["dataDir"]
    assert runtime["downloadsDir"]
    assert runtime["logsDir"]
    assert runtime["capabilities"]["rest"] is True
    assert runtime["capabilities"]["websocket"] is True
    assert runtime["capabilities"]["exports"] == ["pdf", "docx"]
    assert runtime["featureFlags"]["audioEngine"] == "python"
    assert runtime["featureFlags"]["requestedAudioEngine"] == "python"
    assert runtime["featureFlags"]["rustAudioRequested"] is False
    assert runtime["featureFlags"]["rustAudioAvailable"] is False
    assert runtime["featureFlags"]["nativeDeviceEvents"] == "auto"
    assert runtime["featureFlags"]["requestedNativeDeviceEvents"] == "auto"
    assert runtime["featureFlags"]["nativeDeviceEventsRequested"] is True
    assert runtime["featureFlags"]["sessionTokenRequired"] is False
    assert runtime["startup"]["deviceMonitor"] == "disabled"

    audio = ctl.get_audio_diagnostics()
    assert audio["apiVersion"] == runtime["apiVersion"]
    assert audio["runtimeMode"] == runtime["runtimeMode"]
    assert audio["pid"] == runtime["pid"]
    assert audio["featureFlags"]["audioEngine"] == runtime["featureFlags"]["audioEngine"]
    assert audio["provider"]["configured"]
    assert audio["provider"]["active"] is None
    assert audio["microphone"]["configuredDevice"]
    assert audio["microphone"]["prebufferMs"] >= 0
    assert audio["textInjection"]["method"]
    assert audio["textInjection"]["shellIpc"]["available"] is False
    assert audio["textInjection"]["shellIpc"]["pipeConfigured"] is False
    assert "onnxruntime" in audio["runtimeImports"]
    assert "pipecat.audio.vad.silero" in audio["runtimeImports"]

    assert health["ok"] is True
    assert health["ready"] is True
    assert health["version"] == runtime["version"]
    assert health["apiVersion"] == runtime["apiVersion"]
    assert health["host"] == runtime["host"]
    assert health["port"] == runtime["port"]
    assert health["startedAt"] == runtime["startedAt"]


@pytest.mark.asyncio
async def test_health_and_runtime_do_not_run_audio_import_diagnostics(monkeypatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    def fail_import_diagnostics():
        raise AssertionError("audio diagnostic imports must stay out of readiness paths")

    monkeypatch.setattr(web_api, "_audio_diagnostic_import_status", fail_import_diagnostics)

    runtime = ctl.get_runtime_info()
    health = ctl.get_health()

    assert runtime["apiVersion"]
    assert health["ok"] is True


@pytest.mark.asyncio
async def test_runtime_reports_rust_audio_as_requested_until_prototype_exists(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-prototype")
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    runtime = ctl.get_runtime_info()

    assert runtime["featureFlags"]["requestedAudioEngine"] == "rust-prototype"
    assert runtime["featureFlags"]["rustAudioRequested"] is True
    assert runtime["featureFlags"]["rustAudioAvailable"] is False
    assert runtime["featureFlags"]["audioEngine"] == "python"


@pytest.mark.asyncio
async def test_audio_diagnostics_include_private_native_endpoint_mapping(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: False)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        _install_fake_sounddevice_module(
            monkeypatch,
            devices=[
                {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
                {"name": "Dock Mic, MME", "max_input_channels": 1, "hostapi": 0},
            ],
            hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
            default_input=0,
        )
        monkeypatch.setattr(web_api.Config, "FAVORITE_MIC", "Mikrofon (2- Dock Mic)", raising=False)
        monkeypatch.setattr(
            web_api,
            "collect_native_capture_endpoint_inventory",
            lambda: [
                {
                    "endpointIdHash": "hashed-native-endpoint",
                    "friendlyName": "Dock Mic",
                    "flow": "capture",
                    "isDefault": True,
                }
            ],
        )

        audio = ctl.get_audio_diagnostics()
        mapping = audio["microphone"]["nativeEndpointMapping"]
        microphones = ctl.list_microphones()

        assert mapping["available"] is True
        assert mapping["nativeInventoryAvailable"] is True
        assert mapping["source"] == "pycaw"
        assert mapping["favoriteMicNormalized"] == "dock mic"
        assert mapping["mappings"][0]["nativeEndpointIdHash"] == "hashed-native-endpoint"
        assert "endpointId" not in mapping["mappings"][0]
        assert microphones[1]["deviceId"] == "Dock Mic, Windows WASAPI"
    finally:
        ctl.shutdown()


@pytest.mark.asyncio
async def test_audio_diagnostics_do_not_run_rust_probe_unless_requested(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.delenv("SCRIBER_AUDIO_ENGINE", raising=False)
    monkeypatch.delenv("SCRIBER_RUST_AUDIO_PROBE", raising=False)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: False)
    monkeypatch.setattr(web_api, "call_shell_ipc", MagicMock())
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        audio = ctl.get_audio_diagnostics()
    finally:
        ctl.shutdown()

    assert audio["microphone"]["rustAudioProbe"]["requested"] is False
    assert audio["microphone"]["rustAudioProbe"]["reason"] == "notRequested"
    web_api.call_shell_ipc.assert_not_called()


@pytest.mark.asyncio
async def test_audio_diagnostics_prefers_rust_native_endpoint_inventory_for_mapping(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    _install_fake_sounddevice_module(
        monkeypatch,
        devices=[
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
        ],
        hostapis=[{"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    monkeypatch.setattr(
        web_api,
        "collect_native_capture_endpoint_inventory",
        lambda: [
            {
                "endpointIdHash": "pycaw-hash",
                "friendlyName": "Dock Mic",
                "flow": "capture",
                "isDefault": True,
            }
        ],
    )
    def fake_shell_ipc(command, payload=None, **kwargs):
        if command == "nativeDeviceEventsStatus":
            return {
                "success": True,
                "payload": {
                    "source": "tauri",
                    "monitorKind": "wasapi-imm-notification",
                    "available": True,
                    "running": True,
                    "registered": True,
                    "effectiveMode": "observe-only",
                },
                "errorCode": None,
                "fallbackReason": None,
            }
        assert command == "audioEndpointInventory"
        return {
            "success": True,
            "payload": {
                "engine": "rust-prototype",
                "inventoryKind": "wasapi-capture-endpoints",
                "available": True,
                "source": "rust-wasapi",
                "endpoints": [
                    {
                        "endpointIdHash": "rust-hash",
                        "friendlyName": "Dock Mic",
                        "flow": "capture",
                        "isDefault": True,
                    }
                ],
            },
            "errorCode": None,
            "fallbackReason": None,
        }
    call_mock = MagicMock(side_effect=fake_shell_ipc)
    monkeypatch.setattr(web_api, "call_shell_ipc", call_mock)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        audio = ctl.get_audio_diagnostics()
    finally:
        ctl.shutdown()

    inventory = audio["microphone"]["rustNativeEndpointInventory"]
    native_events = audio["microphone"]["nativeDeviceEvents"]
    mapping = audio["microphone"]["nativeEndpointMapping"]
    calls = [(call.args[0], call.args[1]) for call in call_mock.call_args_list]
    assert ("audioEndpointInventory", {}) in calls
    assert ("nativeDeviceEventsStatus", {}) in calls
    assert inventory["available"] is True
    assert inventory["source"] == "rust-wasapi"
    assert native_events["registered"] is True
    assert native_events["effectiveMode"] == "observe-only"
    assert mapping["source"] == "rust-wasapi"
    assert mapping["rustInventoryAvailable"] is True
    assert mapping["mappings"][0]["nativeEndpointIdHash"] == "rust-hash"
    assert "pycaw-hash" not in str(mapping)
    assert "SWD\\MMDEVAPI" not in str(inventory)
    assert "endpointId" not in mapping["mappings"][0]


@pytest.mark.asyncio
async def test_audio_diagnostics_skip_rust_probe_without_shell_ipc(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_RUST_AUDIO_PROBE", "1")
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: False)
    monkeypatch.setattr(web_api, "call_shell_ipc", MagicMock())
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        audio = ctl.get_audio_diagnostics()
    finally:
        ctl.shutdown()

    assert audio["microphone"]["rustAudioProbe"]["requested"] is True
    assert audio["microphone"]["rustAudioProbe"]["available"] is False
    assert audio["microphone"]["rustAudioProbe"]["reason"] == "shellIpcUnavailable"
    web_api.call_shell_ipc.assert_not_called()


@pytest.mark.asyncio
async def test_audio_diagnostics_runs_rust_probe_when_requested(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-prototype")
    monkeypatch.setattr(web_api.Config, "MIC_BLOCK_SIZE", 640, raising=False)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    def fake_shell_ipc(command, payload=None, **kwargs):
        if command == "audioEndpointInventory":
            return {"success": True, "payload": {"available": False}, "errorCode": None, "fallbackReason": None}
        if command == "nativeDeviceEventsStatus":
            return {
                "success": True,
                "payload": {"available": True, "running": True, "registered": True},
                "errorCode": None,
                "fallbackReason": None,
            }
        assert command == "audioProbe"
        return {
            "success": True,
            "payload": {
                "engine": "rust-prototype",
                "probeKind": "wasapi-passive",
                "available": True,
                "endpointIdHash": "redacted-endpoint",
                "mixFormat": {"sampleRate": 48000, "channels": 2},
                "callbackCount": 0,
                "droppedFrameCount": 0,
                "closeStatus": "closed",
            },
            "errorCode": None,
            "fallbackReason": None,
        }
    call_mock = MagicMock(side_effect=fake_shell_ipc)
    monkeypatch.setattr(web_api, "call_shell_ipc", call_mock)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        audio = ctl.get_audio_diagnostics()
    finally:
        ctl.shutdown()

    probe = audio["microphone"]["rustAudioProbe"]
    assert probe["requested"] is True
    assert probe["available"] is True
    assert probe["ipcSuccess"] is True
    assert probe["endpointIdHash"] == "redacted-endpoint"
    assert probe["callbackCount"] == 0
    probe_calls = [call for call in call_mock.call_args_list if call.args[0] == "audioProbe"]
    assert len(probe_calls) == 1
    command, payload = probe_calls[0].args[:2]
    assert payload["sampleRate"] == 16000
    assert payload["channels"] == 1
    assert payload["blockSize"] == 640
    assert "endpointId" not in probe


@pytest.mark.asyncio
async def test_audio_diagnostics_rust_probe_sends_selected_native_endpoint_hash(monkeypatch):
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-prototype")
    monkeypatch.setattr(web_api.Config, "MIC_DEVICE", "Dock Mic, Windows WASAPI", raising=False)
    monkeypatch.setattr(web_api.Config, "FAVORITE_MIC", "Dock Mic, Windows WASAPI", raising=False)
    _install_fake_sounddevice_module(
        monkeypatch,
        devices=[
            {"name": "Built-in Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
            {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
        ],
        hostapis=[{"name": "MME"}, {"name": "Windows WASAPI"}],
        default_input=0,
    )
    monkeypatch.setattr(
        web_api,
        "collect_native_capture_endpoint_inventory",
        lambda: [
            {
                "endpointIdHash": "built-in-hash",
                "friendlyName": "Built-in Mic",
                "flow": "capture",
                "isDefault": True,
            },
            {
                "endpointIdHash": "dock-hash",
                "friendlyName": "Dock Mic",
                "flow": "capture",
                "isDefault": False,
            },
        ],
    )
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: True)
    def fake_shell_ipc(command, payload=None, **kwargs):
        if command == "audioEndpointInventory":
            return {"success": True, "payload": {"available": False}, "errorCode": None, "fallbackReason": None}
        if command == "nativeDeviceEventsStatus":
            return {
                "success": True,
                "payload": {"available": True, "running": True, "registered": True},
                "errorCode": None,
                "fallbackReason": None,
            }
        assert command == "audioProbe"
        return {
            "success": True,
            "payload": {
                "engine": "rust-prototype",
                "probeKind": "wasapi-passive",
                "available": True,
                "endpointIdHash": "dock-hash",
                "selection": "nativeEndpointHash",
                "endpointSelection": {
                    "mode": "nativeEndpointHash",
                    "selectedNativeEndpointIdHash": "dock-hash",
                },
                "callbackCount": 0,
                "droppedFrameCount": 0,
                "closeStatus": "closed",
            },
            "errorCode": None,
            "fallbackReason": None,
        }
    call_mock = MagicMock(side_effect=fake_shell_ipc)
    monkeypatch.setattr(web_api, "call_shell_ipc", call_mock)
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    try:
        audio = ctl.get_audio_diagnostics()
    finally:
        ctl.shutdown()

    probe = audio["microphone"]["rustAudioProbe"]
    probe_calls = [call for call in call_mock.call_args_list if call.args[0] == "audioProbe"]
    assert len(probe_calls) == 1
    command, payload = probe_calls[0].args[:2]
    assert payload["devicePreference"] == "Dock Mic, Windows WASAPI"
    assert payload["portAudioLabel"] == "Dock Mic, Windows WASAPI"
    assert payload["nativeEndpointIdHash"] == "dock-hash"
    assert payload["nativeEndpointMatchReason"] in {"name", "normalizedName"}
    assert probe["endpointSelection"]["mode"] == "nativeEndpointHash"
    assert "SWD\\MMDEVAPI" not in str(payload)
    assert "SWD\\MMDEVAPI" not in str(probe)


@pytest.mark.asyncio
async def test_runtime_reports_native_device_event_flag(monkeypatch):
    monkeypatch.setenv("SCRIBER_NATIVE_DEVICE_EVENTS", "0")
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    runtime = ctl.get_runtime_info()
    audio = ctl.get_audio_diagnostics()

    assert runtime["featureFlags"]["nativeDeviceEvents"] == "disabled"
    assert runtime["featureFlags"]["requestedNativeDeviceEvents"] == "0"
    assert runtime["featureFlags"]["nativeDeviceEventsRequested"] is False
    assert audio["featureFlags"]["nativeDeviceEvents"] == "disabled"


@pytest.mark.asyncio
async def test_register_hotkeys_can_be_disabled_for_runtime_smoke(monkeypatch):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    monkeypatch.setenv("SCRIBER_DISABLE_HOTKEYS", "1")
    ctl.register_hotkeys()

    assert ctl._keyboard is None


@pytest.mark.asyncio
async def test_low_input_warning_emits_and_clears():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._session_id = "s1"
    ctl._is_listening = True
    ctl._mic_low_rms_warn_after_secs = 0.01
    ctl._mic_low_rms_threshold = 0.001
    ctl._mic_low_rms_clear_threshold = 0.002

    with patch.object(ctl, "broadcast", new=AsyncMock()) as broadcast_mock:
        ctl._on_audio_level(0.0, session_id="s1")
        await asyncio.sleep(0.03)
        ctl._on_audio_level(0.0, session_id="s1")
        await asyncio.sleep(0.05)

        assert ctl.get_state()["inputWarning"]
        assert ctl.get_state()["inputWarningCode"] == "mic_level_very_low"
        assert ctl.get_state()["inputWarningActions"]
        active_payloads = [
            call.args[0]
            for call in broadcast_mock.await_args_list
            if call.args and isinstance(call.args[0], dict) and call.args[0].get("type") == "input_warning"
        ]
        assert any(payload.get("active") is True for payload in active_payloads)
        assert any(payload.get("code") == "mic_level_very_low" for payload in active_payloads)
        assert any(payload.get("actions") for payload in active_payloads)

        ctl._on_audio_level(0.02, session_id="s1")
        await asyncio.sleep(0.05)
        assert ctl.get_state()["inputWarning"] == ""
        assert ctl.get_state()["inputWarningCode"] == ""
        assert ctl.get_state()["inputWarningActions"] == []
        inactive_payloads = [
            call.args[0]
            for call in broadcast_mock.await_args_list
            if call.args and isinstance(call.args[0], dict) and call.args[0].get("type") == "input_warning"
        ]
        assert any(payload.get("active") is False for payload in inactive_payloads)


@pytest.mark.asyncio
async def test_broadcast_skips_serialization_without_clients():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    with patch.object(json, "dumps", side_effect=AssertionError("should not serialize")):
        await ctl.broadcast({"type": "status", "status": "Idle"})


@pytest.mark.asyncio
async def test_audio_level_skips_broadcast_work_without_clients_or_overlay():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._session_id = "s1"
    ctl._is_listening = True

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()) as broadcast_mock,
        patch.object(ctl._loop, "call_soon_threadsafe") as call_soon_mock,
        patch("src.web_api.update_overlay_audio") as overlay_mock,
    ):
        ctl._on_audio_level(0.02, session_id="s1")
        await asyncio.sleep(0)

    call_soon_mock.assert_not_called()
    broadcast_mock.assert_not_awaited()
    overlay_mock.assert_not_called()


@pytest.mark.asyncio
async def test_audio_level_broadcast_is_throttled_to_sixty_hz():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._session_id = "s1"
    ctl._client_count = 1

    with (
        patch.object(ctl, "_update_input_warning"),
        patch.object(ctl._loop, "call_soon_threadsafe") as call_soon_mock,
        patch("src.web_api.time.monotonic", side_effect=[100.0, 100.01, 100.02]),
    ):
        ctl._on_audio_level(0.02, session_id="s1")
        ctl._on_audio_level(0.03, session_id="s1")
        ctl._on_audio_level(0.04, session_id="s1")

    assert call_soon_mock.call_count == 2


@pytest.mark.asyncio
async def test_audio_level_updates_overlay_without_ws_clients():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._session_id = "s1"
    ctl._is_listening = True
    ctl._overlay_audio_enabled = True

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()) as broadcast_mock,
        patch.object(ctl._loop, "call_soon_threadsafe") as call_soon_mock,
        patch("src.web_api.update_overlay_audio") as overlay_mock,
    ):
        ctl._on_audio_level(0.02, session_id="s1")
        await asyncio.sleep(0)

    overlay_mock.assert_called_once_with(0.02)
    call_soon_mock.assert_not_called()
    broadcast_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_pipeline_done_persists_failed_live_session():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "failed-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True

    async def _boom():
        raise RuntimeError("boom")

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)
    ctl._pipeline_task = task

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch("src.web_api.hide_recording_overlay"),
    ):
        ctl._on_pipeline_done(task, session_id=session_id)
        await asyncio.sleep(0.05)

    assert rec.status == "failed"
    assert ctl._current is None
    assert ctl._session_id is None
    assert rec in ctl._history
    save_mock.assert_awaited_once_with(rec)


class _StopOkPipeline:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_emergency_stop_clears_state_and_stopping_flag():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "emergency-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True
    ctl._is_stopping = True

    pipeline = _StopOkPipeline()
    ctl._pipeline = pipeline
    ctl._pipeline_task = asyncio.create_task(asyncio.sleep(10))

    await ctl._emergency_stop_pipeline(session_id=session_id)

    assert pipeline.stopped is True
    assert ctl._is_listening is False
    assert ctl._is_stopping is False
    assert ctl._pipeline is None
    assert ctl._pipeline_task is None
    assert ctl._session_id is None
    assert ctl._current is None


class _StopFailPipeline:
    service_name = "openai"

    async def stop(self):
        raise RuntimeError("stop failed")


@pytest.mark.asyncio
async def test_stop_listening_marks_failed_when_stop_raises():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "stop-fail-session"
    rec = _make_record(session_id)
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True
    ctl._pipeline = _StopFailPipeline()
    ctl._pipeline_task = None

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
    ):
        await ctl.stop_listening()

    assert rec.status == "failed"
    assert "[Error] stop failed" in rec.content
    assert ctl._status == "Error"
    assert rec in ctl._history


class _SlowStopPipeline:
    service_name = "openai"

    def __init__(self):
        self.stop_gate = asyncio.Event()

    async def stop(self):
        await self.stop_gate.wait()


class _LateReadyPipeline:
    service_name = "openai"
    instances: list["_LateReadyPipeline"] = []

    def __init__(self, *_, on_mic_ready=None, **__):
        self.on_mic_ready = on_mic_ready
        self.allow_stop = asyncio.Event()
        self.stop_gate = asyncio.Event()
        type(self).instances.append(self)

    async def start(self):
        await self.stop_gate.wait()

    async def stop(self):
        await self.allow_stop.wait()
        self.stop_gate.set()


class _PrewarmAwarePipeline:
    service_name = "openai"
    instances: list["_PrewarmAwarePipeline"] = []

    def __init__(self, *_, mic_prewarm_manager=None, **__):
        self.mic_prewarm_manager = mic_prewarm_manager
        self.stop_gate = asyncio.Event()
        type(self).instances.append(self)

    async def start(self):
        await self.stop_gate.wait()

    async def stop(self):
        self.stop_gate.set()


@pytest.mark.asyncio
async def test_hotkey_toggle_is_deferred_while_stop_is_in_progress():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    session_id = "deferred-hotkey-session"
    rec = _make_record(session_id)
    pipeline = _SlowStopPipeline()
    ctl._current = rec
    ctl._session_id = session_id
    ctl._is_listening = True
    ctl._pipeline = pipeline
    ctl._pipeline_task = None

    with (
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "start_listening", new=AsyncMock()) as start_mock,
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
    ):
        stop_task = asyncio.create_task(ctl.stop_listening())

        for _ in range(50):
            if ctl._is_stopping:
                break
            await asyncio.sleep(0.01)
        assert ctl._is_stopping is True

        await ctl._handle_hotkey_toggle()
        assert ctl._pending_hotkey_toggle is True

        pipeline.stop_gate.set()
        await stop_task

    start_mock.assert_awaited_once()
    assert ctl._pending_hotkey_toggle is False


@pytest.mark.asyncio
async def test_late_mic_ready_is_ignored_while_stop_is_in_progress():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    _LateReadyPipeline.instances.clear()

    with (
        patch("src.web_api.ScriberPipeline", _LateReadyPipeline),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay") as show_recording_mock,
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
    ):
        await ctl.start_listening()
        pipeline = _LateReadyPipeline.instances[-1]

        stop_task = asyncio.create_task(ctl.stop_listening())
        for _ in range(50):
            if ctl._is_stopping:
                break
            await asyncio.sleep(0.01)
        assert ctl._is_stopping is True

        pipeline.on_mic_ready()
        await asyncio.sleep(0.05)

        assert ctl.get_state()["recordingState"] == "finalizing"
        show_recording_mock.assert_not_called()

        pipeline.allow_stop.set()
        await stop_task

    assert ctl.get_state()["recordingState"] == "idle"


@pytest.mark.asyncio
async def test_start_listening_passes_idle_prewarm_without_closing_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(web_api.Config, "MIC_ALWAYS_ON", True, raising=False)
    monkeypatch.setattr(web_api, "MicrophonePrewarmManager", _FakeMicPrewarmManager)
    _FakeMicPrewarmManager.instances.clear()
    _PrewarmAwarePipeline.instances.clear()

    ctl = ScriberWebController(asyncio.get_running_loop())
    await _wait_for_prewarm_task(ctl)
    manager = _FakeMicPrewarmManager.instances[-1]

    with (
        patch("src.web_api.ScriberPipeline", _PrewarmAwarePipeline),
        patch.object(ctl, "_select_available_provider", return_value="openai"),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
    ):
        await ctl.start_listening()

        pipeline = _PrewarmAwarePipeline.instances[-1]

        assert pipeline.mic_prewarm_manager is manager
        assert manager.pause_calls == 0

        pipeline.stop_gate.set()
        await asyncio.wait_for(ctl._pipeline_task, timeout=1.0)

    ctl.shutdown()


@pytest.mark.asyncio
async def test_dispatch_hotkey_toggle_debounces_rapid_events():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._loop = MagicMock()
    ctl._hotkey_dispatch_debounce_seconds = 0.5

    with patch("src.web_api.time.monotonic", side_effect=[10.0, 10.1, 10.8]):
        ctl._dispatch_hotkey_toggle()
        ctl._dispatch_hotkey_toggle()
        ctl._dispatch_hotkey_toggle()

    assert ctl._loop.call_soon_threadsafe.call_count == 2


@pytest.mark.asyncio
async def test_toggle_hotkey_poll_loop_triggers_only_on_rising_edge():
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)

    states = [False, True, True, False, False]
    idx = {"value": 0}

    class _KeyboardStub:
        def is_pressed(self, _hotkey: str) -> bool:
            current = states[idx["value"]]
            if idx["value"] < len(states) - 1:
                idx["value"] += 1
            return current

    ctl._keyboard = _KeyboardStub()

    with patch.object(ctl, "_dispatch_hotkey_toggle") as dispatch_mock:
        task = asyncio.create_task(ctl._toggle_hotkey_poll_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    dispatch_mock.assert_called_once()
