import json
import zipfile
import pytest

from src.runtime import support_bundle
from src.runtime.log_clear_state import record_clear_state
from src.runtime.support_bundle import create_support_bundle, redact_mapping, redact_text


def test_redaction_helpers_hide_sensitive_values():
    assert "plain" in redact_text("mode=plain")
    pipe_name = r"\\.\pipe\scriber-shell-secret"
    escaped_pipe_name = pipe_name.replace("\\", "\\\\")
    endpoint_id = r"SWD\MMDEVAPI\{0.0.1.00000000}.{secret-capture-device}"
    escaped_endpoint_id = endpoint_id.replace("\\", "\\\\")
    groq_key = "gsk_" + "a" * 32
    google_key = "AIza" + "b" * 32
    redacted = redact_text(
        f"OPENAI_API_KEY=sk-abcdefghijklmnop Authorization: Bearer token-value "
        f"rawGroq={groq_key} "
        f"url=https://example.test/v1?key={google_key}&access_token=query-token "
        f"pipe={pipe_name} escaped={escaped_pipe_name} "
        f"endpoint={endpoint_id} escapedEndpoint={escaped_endpoint_id}"
    )

    assert "sk-abcdefghijklmnop" not in redacted
    assert groq_key not in redacted
    assert google_key not in redacted
    assert "query-token" not in redacted
    assert "token-value" not in redacted
    assert pipe_name not in redacted
    assert escaped_pipe_name not in redacted
    assert "scriber-shell-secret" not in redacted
    assert endpoint_id not in redacted
    assert escaped_endpoint_id not in redacted
    assert "SWD" not in redacted
    assert "secret-capture-device" not in redacted
    assert "[REDACTED]" in redacted
    assert "[REDACTED_PIPE]" in redacted
    assert "[REDACTED_ENDPOINT_ID]" in redacted

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


def test_redaction_handles_very_long_non_secret_token():
    raw = "x" * (support_bundle._MAX_SETTINGS_BYTES + 1)

    assert redact_text(raw) == raw


def test_support_bundle_limits_log_file_count(monkeypatch, tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    repo = tmp_path / "repo"
    logs.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo)
    for index in range(support_bundle._MAX_LOG_FILES + 5):
        (logs / f"log-{index:03d}.log").write_text("entry\n", encoding="utf-8")

    bundle = create_support_bundle(
        runtime_info={"apiVersion": "1", "runtimeMode": "test"},
        app_state={"listening": False},
    )

    with zipfile.ZipFile(bundle) as zf:
        log_names = [name for name in zf.namelist() if name.startswith("logs/")]
    assert len(log_names) == support_bundle._MAX_LOG_FILES


def test_create_support_bundle_redacts_config_env_and_logs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    logs_dir = data_dir / "logs"
    repo_dir = tmp_path / "repo"
    logs_dir.mkdir(parents=True)
    repo_dir.mkdir()
    groq_key = "gsk_" + "b" * 32
    groq_log_key = "gsk_" + "c" * 32
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijklmnop")
    monkeypatch.setenv("GROQ_API_KEY", groq_key)
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "session-secret-value")
    shell_pipe = r"\\.\pipe\scriber-shell-bundle"
    raw_endpoint_id = r"SWD\MMDEVAPI\{0.0.1.00000000}.{support-bundle-capture}"
    monkeypatch.setenv("SCRIBER_SHELL_IPC_PIPE", shell_pipe)
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo_dir)

    (data_dir / "settings.json").write_text(
        '{"language":"en","apiKeys":{"openaiApiKey":"settings-secret-value","groq":"settings-groq-secret"}}',
        encoding="utf-8",
    )
    (data_dir / ".env").write_text(
        f"SONIOX_API_KEY=env-secret-value\nGROQ_API_KEY={groq_key}\nSCRIBER_MODE=toggle\nSCRIBER_SHELL_IPC_PIPE={shell_pipe}\n",
        encoding="utf-8",
    )
    (logs_dir / "latest.log").write_text(
        f"\x00\x00OPENAI_API_KEY=log-secret-value Authorization: Bearer bearer-secret "
        f"raw_groq={groq_log_key} pipe={shell_pipe} endpoint={raw_endpoint_id}\n",
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
    assert "settings-groq-secret" not in combined
    assert "env-secret-value" not in combined
    assert "log-secret-value" not in combined
    assert groq_key not in combined
    assert groq_log_key not in combined
    assert "session-secret-value" not in combined
    assert "sk-abcdefghijklmnop" not in combined
    assert "bearer-secret" not in combined
    assert shell_pipe not in combined
    assert "scriber-shell-bundle" not in combined
    assert raw_endpoint_id not in combined
    assert "support-bundle-capture" not in combined
    assert "private transcript text" not in combined
    assert "\x00" not in combined
    assert "[REDACTED]" in combined
    assert "[REDACTED_PIPE]" in combined
    assert "[REDACTED_ENDPOINT_ID]" in combined


def test_support_bundle_does_not_load_oversized_settings_as_json(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    data_dir.mkdir()
    repo_dir.mkdir()
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo_dir)
    (data_dir / "settings.json").write_text(
        "x" * (support_bundle._MAX_SETTINGS_BYTES + 1),
        encoding="utf-8",
    )

    bundle = create_support_bundle(runtime_info={}, app_state={})

    with zipfile.ZipFile(bundle) as zf:
        assert "config/settings.redacted.txt" in zf.namelist()
        assert len(zf.read("config/settings.redacted.txt")) < support_bundle._MAX_SETTINGS_BYTES


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
                        "engine": "rust-wasapi",
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
                            "deadlineMs": 2000.0,
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
                "prewarm": {
                    "engine": "rust-wasapi",
                    "active": True,
                    "prewarmId": "raw-prewarm-id",
                    "prewarmIdHash": "prewarm-hash",
                    "lastActiveCaptureResumeGapMs": 12.0,
                    "lastActiveCaptureStopToReadyMs": 18.0,
                    "maxActiveCaptureStopToReadyMs": 18.0,
                    "lastStatus": {
                        "active": True,
                        "prewarm_id": "raw-prewarm-id-2",
                        "prewarmIdHash": "status-hash",
                    },
                    "recentEvents": [
                        {
                            "event": "started",
                            "reason": "start",
                            "prewarmId": "raw-prewarm-id-3",
                            "prewarmIdHash": "event-hash",
                        }
                    ],
                },
                "activeCapture": {
                    "engine": "python",
                    "requestedEngine": "rust-wasapi",
                    "frameSource": "sounddevice",
                    "engineFallbackReason": "rustWasapiUnavailable",
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
    prewarm = payload["microphone"]["prewarm"]
    watchdog = payload["watchdog"]["lastWarning"]
    shell_ipc = payload["textInjection"]["shellIpc"]
    assert "audio-diagnostics.redacted.json" in names
    assert active["engine"] == "python"
    assert active["requestedEngine"] == "rust-wasapi"
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
    assert prewarm["active"] is True
    assert prewarm["prewarmId"] == "[REDACTED]"
    assert prewarm["prewarmIdHash"] == "prewarm-hash"
    assert prewarm["lastStatus"]["prewarm_id"] == "[REDACTED]"
    assert prewarm["recentEvents"][0]["prewarmId"] == "[REDACTED]"
    assert prewarm["lastActiveCaptureStopToReadyMs"] == 18.0
    assert "raw-prewarm-id" not in combined
    assert native_events["registered"] is True
    assert native_events["lastEvent"]["endpointId"] == "[REDACTED]"
    assert native_events["lastEvent"]["endpointIdHash"] == "event-hash"
    assert "raw-native-endpoint-id" not in combined
    assert shell_ipc["lastCommand"] == "injectText"
    assert shell_ipc["lastErrorCode"] == "missingPasteMarker"
    assert shell_ipc["lastResponse"]["payload"]["preDelayMode"] == "auto"
    assert shell_ipc["lastResponse"]["payload"]["requestedPreDelayMs"] == 80.0
    assert shell_ipc["lastResponse"]["payload"]["deadlineMs"] == 2000.0
    assert shell_ipc["lastResponse"]["payload"]["foregroundBefore"]["titleHash"] == "title-hash"


def test_support_bundle_includes_post_processing_diagnostics(monkeypatch, tmp_path):
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
        post_processing_diagnostics={
            "apiVersion": "1",
            "items": [
                {
                    "sessionIdPrefix": "abcdef12",
                    "model": "google/gemini-2.5-flash-lite:nitro",
                    "status": "failure",
                    "rawChars": 123,
                    "promptChars": 1800,
                    "error": "OPENAI_API_KEY=secret-value failed",
                }
            ],
        },
    )

    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        payload = json.loads(zf.read("post-processing-diagnostics.redacted.json"))
        combined = "\n".join(zf.read(name).decode("utf-8", errors="replace") for name in names)

    assert "post-processing-diagnostics.redacted.json" in names
    assert payload["items"][0]["model"] == "google/gemini-2.5-flash-lite:nitro"
    assert payload["items"][0]["error"] == "OPENAI_API_KEY=[REDACTED] failed"
    assert "secret-value" not in combined


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


def test_support_bundle_names_are_unique_and_retention_is_bounded(monkeypatch, tmp_path):
    output_dir = tmp_path / "bundles"
    monkeypatch.setattr(support_bundle, "_MAX_SUPPORT_BUNDLES", 3)
    bundles = [
        create_support_bundle(
            runtime_info={"apiVersion": "1"},
            app_state={"recordingState": "idle"},
            output_dir=output_dir,
        )
        for _ in range(5)
    ]

    assert len({path.name for path in bundles}) == 5
    assert bundles[-1].is_file()
    assert len(list(output_dir.glob("scriber-support-*.zip"))) == 3
    assert list(output_dir.glob("*.tmp")) == []


def test_support_bundle_does_not_follow_log_symlink_outside_root(monkeypatch, tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    repo = tmp_path / "repo"
    logs.mkdir(parents=True)
    repo.mkdir()
    outside = tmp_path / "outside-secret.log"
    outside.write_text("outside secret\n", encoding="utf-8")
    try:
        (logs / "linked.log").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data))
    monkeypatch.setattr(support_bundle, "repo_root", lambda: repo)
    bundle = create_support_bundle(
        runtime_info={"apiVersion": "1"},
        app_state={"recordingState": "idle"},
    )

    with zipfile.ZipFile(bundle) as zf:
        assert "logs/linked.log" not in zf.namelist()
