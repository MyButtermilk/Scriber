import zipfile

from src.runtime.support_bundle import create_support_bundle, redact_mapping, redact_text


def test_redaction_helpers_hide_sensitive_values():
    assert "plain" in redact_text("mode=plain")
    redacted = redact_text("OPENAI_API_KEY=sk-abcdefghijklmnop Authorization: Bearer token-value")

    assert "sk-abcdefghijklmnop" not in redacted
    assert "token-value" not in redacted
    assert "[REDACTED]" in redacted

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
    logs_dir.mkdir(parents=True)
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abcdefghijklmnop")
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "session-secret-value")

    (data_dir / "settings.json").write_text(
        '{"language":"en","apiKeys":{"openaiApiKey":"settings-secret-value"}}',
        encoding="utf-8",
    )
    (data_dir / ".env").write_text(
        "SONIOX_API_KEY=env-secret-value\nSCRIBER_MODE=toggle\n",
        encoding="utf-8",
    )
    (logs_dir / "latest.log").write_text(
        "OPENAI_API_KEY=log-secret-value Authorization: Bearer bearer-secret\n",
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
    assert "private transcript text" not in combined
    assert "[REDACTED]" in combined
