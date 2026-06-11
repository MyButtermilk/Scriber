import json
import zipfile

from src.runtime import support_bundle
from src.runtime.log_clear_state import record_clear_state
from src.runtime.support_bundle import create_support_bundle, redact_mapping, redact_text


def test_redaction_helpers_hide_sensitive_values():
    assert "plain" in redact_text("mode=plain")
    pipe_name = r"\\.\pipe\scriber-shell-secret"
    escaped_pipe_name = pipe_name.replace("\\", "\\\\")
    redacted = redact_text(
        f"OPENAI_API_KEY=sk-abcdefghijklmnop Authorization: Bearer token-value "
        f"pipe={pipe_name} escaped={escaped_pipe_name}"
    )

    assert "sk-abcdefghijklmnop" not in redacted
    assert "token-value" not in redacted
    assert pipe_name not in redacted
    assert escaped_pipe_name not in redacted
    assert "scriber-shell-secret" not in redacted
    assert "[REDACTED]" in redacted
    assert "[REDACTED_PIPE]" in redacted

    mapping = redact_mapping(
        {
            "language": "en",
            "openaiApiKey": "secret-value",
            "nested": {"sessionToken": "token-value"},
        }
    )
    assert mapping["language"] == "en"
    assert mapping["openaiApiKey"] == "[REDACTED]"
    assert mapping["nested"]["sessionToken"] == "[REDACTED]"


def test_create_support_bundle_redacts_config_env_and_logs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir(parents=True)
    repo_dir.mkdir()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijklmnop")
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "session-secret-value")
    shell_pipe = r"\\.\pipe\scriber-shell-bundle"
    monkeypatch.setenv("SCRIBER_SHELL_IPC_PIPE", shell_pipe)
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo_dir)

    (data_dir / "settings.json").write_text(
        '{"language":"en","apiKeys":{"openaiApiKey":"settings-secret-value"}}',
        encoding="utf-8",
    )
    (data_dir / ".env").write_text(
        f"SONIOX_API_KEY=env-secret-value\nSCRIBER_MODE=toggle\nSCRIBER_SHELL_IPC_PIPE={shell_pipe}\n",
        encoding="utf-8",
    )
    (logs_dir / "latest.log").write_text(
        f"\x00\x00OPENAI_API_KEY=log-secret-value Authorization: Bearer bearer-secret pipe={shell_pipe}\n",
        encoding="utf-8",
    )

    bundle = create_support_bundle(
        runtime_info={
            "apiVersion": "1",
            "runtimeMode": "tauri-supervised",
            "launchKind": "sidecar",
            "pid": 123,
            "dataDir": str(data_dir),
        },
        app_state={
            "listening": False,
            "status": "Stopped",
            "recordingState": "idle",
            "transcribing": False,
            "current": {"content": "private transcript text"},
        },
    )

    assert bundle.is_file()
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "runtime.json" in names
        assert "state.redacted.json" in names
        assert "environment.redacted.json" in names
        assert "config/settings.redacted.json" in names
        assert "config/env.redacted.txt" in names
        assert "logs/latest.log" in names

        combined = "\n".join(zf.read(name).decode("utf-8", errors="replace") for name in names)

    assert "settings-secret-value" not in combined
    assert "env-secret-value" not in combined
    assert "log-secret-value" not in combined
    assert "session-secret-value" not in combined
    assert "sk-abcdefghijklmnop" not in combined
    assert "bearer-secret" not in combined
    assert shell_pipe not in combined
    assert "scriber-shell-bundle" not in combined
    assert "private transcript text" not in combined
    assert "\x00" not in combined
    assert "[REDACTED]" in combined
    assert "[REDACTED_PIPE]" in combined


def test_support_bundle_includes_redacted_audio_diagnostics(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir(parents=True)
    repo_dir.mkdir()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo_dir)

    bundle = create_support_bundle(
        runtime_info={
            "apiVersion": "1",
            "runtimeMode": "tauri-supervised",
            "launchKind": "sidecar",
            "pid": 123,
        },
        app_state={"recordingState": "idle"},
        audio_diagnostics={
            "apiVersion": "1",
            "watchdog": {
                "enabled": True,
                "lastWarning": {
                    "message": "Live microphone watchdog could not verify active capture",
                    "recordedAt": "2026-06-11T12:00:00Z",
                    "recordedAtUptimeSeconds": 42.5,
                    "diagnostics": {
                        "engine": "rust-prototype",
                        "frameSource": "rust-frame-pipe",
                        "lastHealthFailureReason": "staleCallbacks",
                        "healthRestartThrottleCount": 1,
                        "lastHealthRestartThrottledReason": "watchdog:staleCallbacks",
                        "sessionToken": "watchdog-secret-token",
                    },
                },
            },
            "textInjection": {
                "method": "tauri",
                "shellIpc": {
                    "available": True,
                    "lastCommand": "injectText",
                    "lastSuccess": False,
                    "lastErrorCode": "missingPasteMarker",
                    "lastFallbackReason": "missing paste marker",
                    "lastResponse": {
                        "success": False,
                        "errorCode": "missingPasteMarker",
                        "fallbackReason": "missing paste marker",
                        "payload": {
                            "method": "tauri",
                            "preDelayMode": "auto",
                            "requestedPreDelayMs": 80.0,
                            "markers": ["clipboard_set"],
                            "foregroundBefore": {
                                "available": True,
                                "titleHash": "title-hash",
                            },
                            "timingsMs": {"clipboardSet": 2.0},
                        },
                    },
                },
            },
            "microphone": {
                "nativeDeviceEvents": {
                    "source": "tauri",
                    "monitorKind": "wasapi-imm-notification",
                    "available": True,
                    "running": True,
                    "registered": True,
                    "comInitialized": True,
                    "callbackAlive": True,
                    "eventCount": 1,
                    "ignoredRenderCount": 0,
                    "debouncedEventCount": 0,
                    "lastEvent": {
                        "eventKind": "default_device_changed",
                        "flow": "capture",
                        "endpointId": "raw-native-endpoint-id",
                        "endpointIdHash": "event-hash",
                    },
                },
                "rustAudioFallbackCircuit": {
                    "available": True,
                    "open": True,
                    "reason": "pipeClosed",
                    "remainingSeconds": 12.5,
                    "cooldownSeconds": 60.0,
                    "sessionToken": "circuit-secret-token",
                },
                "activeCapture": {
                    "engine": "python",
                    "requestedEngine": "rust-prototype",
                    "frameSource": "sounddevice",
                    "engineFallbackReason": "rustPrototypeUnavailable",
                    "nativeEndpointIdHash": "abc123",
                    "sampleRate": 16000,
                    "targetChannels": 1,
                    "captureChannels": 2,
                    "blockSize": 512,
                    "prebufferMs": 400,
                    "droppedFrameCount": 3,
                    "lastCallbackAgoSeconds": 0.42,
                    "restartCount": 1,
                    "sessionToken": "audio-secret-token",
                }
            },
        },
    )

    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        payload = json.loads(zf.read("audio-diagnostics.redacted.json"))
        combined = "\n".join(zf.read(name).decode("utf-8", errors="replace") for name in names)

    active = payload["microphone"]["activeCapture"]
    native_events = payload["microphone"]["nativeDeviceEvents"]
    circuit = payload["microphone"]["rustAudioFallbackCircuit"]
    watchdog = payload["watchdog"]["lastWarning"]
    shell_ipc = payload["textInjection"]["shellIpc"]
    assert "audio-diagnostics.redacted.json" in names
    assert active["engine"] == "python"
    assert active["requestedEngine"] == "rust-prototype"
    assert active["frameSource"] == "sounddevice"
    assert active["nativeEndpointIdHash"] == "abc123"
    assert active["droppedFrameCount"] == 3
    assert active["sessionToken"] == "[REDACTED]"
    assert "audio-secret-token" not in combined
    assert watchdog["message"] == "Live microphone watchdog could not verify active capture"
    assert watchdog["diagnostics"]["lastHealthFailureReason"] == "staleCallbacks"
    assert watchdog["diagnostics"]["healthRestartThrottleCount"] == 1
    assert watchdog["diagnostics"]["sessionToken"] == "[REDACTED]"
    assert "watchdog-secret-token" not in combined
    assert circuit["open"] is True
    assert circuit["reason"] == "pipeClosed"
    assert circuit["sessionToken"] == "[REDACTED]"
    assert "circuit-secret-token" not in combined
    assert native_events["registered"] is True
    assert native_events["lastEvent"]["endpointId"] == "[REDACTED]"
    assert native_events["lastEvent"]["endpointIdHash"] == "event-hash"
    assert "raw-native-endpoint-id" not in combined
    assert shell_ipc["lastCommand"] == "injectText"
    assert shell_ipc["lastErrorCode"] == "missingPasteMarker"
    assert shell_ipc["lastResponse"]["payload"]["preDelayMode"] == "auto"
    assert shell_ipc["lastResponse"]["payload"]["requestedPreDelayMs"] == 80.0
    assert shell_ipc["lastResponse"]["payload"]["foregroundBefore"]["titleHash"] == "title-hash"


def test_support_bundle_respects_runtime_log_clear_marker(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir(parents=True)
    repo_dir.mkdir()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo_dir)

    log_path = logs_dir / "latest.log"
    log_path.write_text("before-clear\n", encoding="utf-8")
    cleared, failed = record_clear_state([log_path])
    assert cleared == ["latest.log"]
    assert failed == []
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("after-clear\n")

    bundle = create_support_bundle(
        runtime_info={"apiVersion": "1", "runtimeMode": "tauri-supervised", "launchKind": "sidecar", "pid": 123},
        app_state={"recordingState": "idle"},
    )

    with zipfile.ZipFile(bundle) as zf:
        log_text = zf.read("logs/latest.log").decode("utf-8", errors="replace")

    assert "before-clear" not in log_text
    assert "after-clear" in log_text
