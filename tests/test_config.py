import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

import src.config as config_module
from src.config import Config


def _read_fresh_shortcut_config(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SCRIBER_DATA_DIR"] = str(tmp_path)
    env["SCRIBER_SKIP_LEGACY_DATA_MIGRATION"] = "1"
    for key in (
        "SCRIBER_HOTKEY",
        "SCRIBER_POST_PROCESSING_HOTKEY",
        "SCRIBER_MEETING_HOTKEY",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from src.config import Config; "
                "print(json.dumps({'live': Config.HOTKEY, "
                "'post': Config.POST_PROCESSING_HOTKEY, "
                "'meeting': Config.MEETING_HOTKEY}))"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


def _read_fresh_summary_prompt_config(
    tmp_path: Path,
    *,
    persist: bool,
) -> dict[str, object]:
    env = os.environ.copy()
    env["SCRIBER_DATA_DIR"] = str(tmp_path)
    env["SCRIBER_SKIP_LEGACY_DATA_MIGRATION"] = "1"
    code = (
        "import json; import src.config as module; "
        "pending_before = module.Config.json_settings_migration_pending(); "
        + ("module.Config.persist_json_settings(); " if persist else "")
        + "print(json.dumps({"
        "'prompt': module.Config.SUMMARIZATION_PROMPT, "
        "'pendingBefore': pending_before, "
        "'pendingAfter': module.Config.json_settings_migration_pending()}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


def test_fresh_install_shortcut_defaults(tmp_path):
    assert _read_fresh_shortcut_config(tmp_path) == {
        "live": "ctrl+shift+d",
        "post": "ctrl+shift+f",
        "meeting": "ctrl+shift+m",
    }


def test_existing_dotenv_shortcuts_override_defaults(tmp_path):
    (tmp_path / ".env").write_text(
        "SCRIBER_HOTKEY=f8\n"
        "SCRIBER_POST_PROCESSING_HOTKEY=ctrl+alt+p\n"
        "SCRIBER_MEETING_HOTKEY=ctrl+alt+m\n",
        encoding="utf-8",
    )

    assert _read_fresh_shortcut_config(tmp_path) == {
        "live": "f8",
        "post": "ctrl+alt+p",
        "meeting": "ctrl+alt+m",
    }


def test_numeric_env_helpers_fall_back_and_clamp(monkeypatch):
    monkeypatch.setenv("TEST_SCRIBER_INT", "broken")
    monkeypatch.setenv("TEST_SCRIBER_FLOAT", "nan")
    assert config_module._env_int("TEST_SCRIBER_INT", 12, minimum=1, maximum=20) == 12
    assert config_module._env_float("TEST_SCRIBER_FLOAT", 2.5, minimum=0.0, maximum=5.0) == 2.5

    monkeypatch.setenv("TEST_SCRIBER_INT", "999")
    monkeypatch.setenv("TEST_SCRIBER_FLOAT", "-4")
    assert config_module._env_int("TEST_SCRIBER_INT", 12, minimum=1, maximum=20) == 20
    assert config_module._env_float("TEST_SCRIBER_FLOAT", 2.5, minimum=0.0, maximum=5.0) == 0.0


def test_older_install_summary_prompt_is_replaced_exactly_once():
    old_custom_prompt = "Mein alter eigener Prompt mit Markdown-Ausgabe."
    settings = {"summarizationPrompt": old_custom_prompt, "unrelated": True}

    assert config_module._migrate_summarization_prompt_once(settings) is True
    assert settings["summarizationPrompt"] == config_module._CURRENT_SUMMARIZATION_PROMPT
    assert settings[config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY] == (
        config_module._SUMMARIZATION_PROMPT_MIGRATION_VERSION
    )
    assert settings["unrelated"] is True

    settings["summarizationPrompt"] = "Spätere bewusste Nutzeränderung"
    assert config_module._migrate_summarization_prompt_once(settings) is False
    assert settings["summarizationPrompt"] == "Spätere bewusste Nutzeränderung"


def test_fresh_install_marks_prompt_migration_without_storing_a_prompt():
    settings = {}

    assert config_module._migrate_summarization_prompt_once(settings) is True
    assert "summarizationPrompt" not in settings
    assert settings[config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY] == 1


@pytest.mark.parametrize("stored_version", [1, "1", 2, 99])
def test_completed_summary_prompt_migration_preserves_user_prompt(stored_version):
    settings = {
        "summarizationPrompt": "Spätere bewusste Nutzeränderung",
        config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY: stored_version,
    }

    assert config_module._migrate_summarization_prompt_once(settings) is False
    assert settings["summarizationPrompt"] == "Spätere bewusste Nutzeränderung"


@pytest.mark.parametrize("invalid_marker", [None, True, -1, "invalid"])
def test_invalid_summary_prompt_migration_marker_is_treated_as_not_completed(
    invalid_marker,
):
    settings = {
        "summarizationPrompt": "Alter Prompt",
        config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY: invalid_marker,
    }

    assert config_module._migrate_summarization_prompt_once(settings) is True
    assert settings["summarizationPrompt"] == config_module._CURRENT_SUMMARIZATION_PROMPT
    assert settings[config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY] == 1


def test_user_prompt_edit_keeps_completed_migration_marker(monkeypatch):
    marker_key = config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY
    monkeypatch.setattr(
        config_module,
        "_json_settings",
        {marker_key: config_module._SUMMARIZATION_PROMPT_MIGRATION_VERSION},
    )
    monkeypatch.setattr(config_module, "_json_settings_migration_pending", False)
    monkeypatch.setattr(
        Config,
        "SUMMARIZATION_PROMPT",
        config_module._CURRENT_SUMMARIZATION_PROMPT,
    )

    Config.set_summarization_prompt("Mein später angepasster Prompt")

    assert config_module._json_settings["summarizationPrompt"] == (
        "Mein später angepasster Prompt"
    )
    assert config_module._json_settings[marker_key] == 1
    assert Config.json_settings_migration_pending() is False


def test_prompt_upgrade_migrates_once_across_real_process_starts(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"summarizationPrompt": "Alter installierter Prompt"}),
        encoding="utf-8",
    )

    first_start = _read_fresh_summary_prompt_config(tmp_path, persist=True)

    assert first_start == {
        "prompt": config_module._CURRENT_SUMMARIZATION_PROMPT,
        "pendingBefore": True,
        "pendingAfter": False,
    }
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["summarizationPrompt"] == config_module._CURRENT_SUMMARIZATION_PROMPT
    assert persisted[config_module._SUMMARIZATION_PROMPT_MIGRATION_KEY] == 1

    persisted["summarizationPrompt"] = "Später vom Nutzer angepasster Prompt"
    settings_path.write_text(json.dumps(persisted), encoding="utf-8")

    second_start = _read_fresh_summary_prompt_config(tmp_path, persist=False)

    assert second_start == {
        "prompt": "Später vom Nutzer angepasster Prompt",
        "pendingBefore": False,
        "pendingAfter": False,
    }


def test_versioned_model_env_upgrades_legacy_dotenv_default(monkeypatch):
    monkeypatch.setattr(config_module, "_PROCESS_ENV_KEYS_BEFORE_DOTENV", frozenset())
    monkeypatch.setenv("TEST_SCRIBER_MODEL", "stt-rt-v3")

    resolved = config_module._versioned_model_env(
        "TEST_SCRIBER_MODEL",
        "stt-rt-v5",
        legacy_dotenv_defaults={"stt-rt-v3", "stt-rt-v4"},
    )

    assert resolved == "stt-rt-v5"
    assert os.environ["TEST_SCRIBER_MODEL"] == "stt-rt-v5"


def test_versioned_model_env_preserves_explicit_process_override(monkeypatch):
    monkeypatch.setattr(
        config_module,
        "_PROCESS_ENV_KEYS_BEFORE_DOTENV",
        frozenset({"TEST_SCRIBER_MODEL"}),
    )
    monkeypatch.setenv("TEST_SCRIBER_MODEL", "stt-rt-v3")

    resolved = config_module._versioned_model_env(
        "TEST_SCRIBER_MODEL",
        "stt-rt-v5",
        legacy_dotenv_defaults={"stt-rt-v3", "stt-rt-v4"},
    )

    assert resolved == "stt-rt-v3"


def test_versioned_model_env_uses_default_for_blank_value(monkeypatch):
    monkeypatch.setattr(config_module, "_PROCESS_ENV_KEYS_BEFORE_DOTENV", frozenset())
    monkeypatch.setenv("TEST_SCRIBER_MODEL", "  ")

    resolved = config_module._versioned_model_env(
        "TEST_SCRIBER_MODEL",
        "stt-rt-v5",
        legacy_dotenv_defaults={"stt-rt-v3", "stt-rt-v4"},
    )

    assert resolved == "stt-rt-v5"


def test_json_settings_loader_rejects_oversized_or_non_object_payload(monkeypatch, tmp_path):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_JSON_SETTINGS_PATH", target)
    target.write_bytes(b"x" * (config_module._MAX_JSON_SETTINGS_BYTES + 1))
    assert config_module._load_json_settings() == {}
    assert config_module._load_json_settings_with_status() == ({}, False)

    target.write_text('["not", "an", "object"]', encoding="utf-8")
    assert config_module._load_json_settings() == {}
    assert config_module._load_json_settings_with_status() == ({}, False)


def test_missing_or_valid_json_settings_allow_automatic_migration(monkeypatch, tmp_path):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_JSON_SETTINGS_PATH", target)
    assert config_module._load_json_settings_with_status() == ({}, True)

    target.write_text('{"summarizationPrompt": "old"}', encoding="utf-8")
    assert config_module._load_json_settings_with_status() == (
        {"summarizationPrompt": "old"},
        True,
    )


class TestConfig(unittest.TestCase):
    def test_default_values(self):
        # Assuming env vars are not set or set to defaults during test
        # We can check if keys exist in class
        self.assertTrue(hasattr(Config, 'SONIOX_API_KEY'))
        self.assertTrue(hasattr(Config, 'HOTKEY'))

    def test_hotkey_config(self):
        # Verify we can override
        os.environ['SCRIBER_HOTKEY'] = 'f9'
        # Reload module to pick up env change?
        # Config class loads at import time.
        # So we might need to reload or access os.getenv directly in methods.
        # But for this simple test, just checking the structure is enough.
        pass

    def test_mistral_service_mapping_exists(self):
        self.assertIn("mistral", Config.SERVICE_API_KEY_MAP)
        self.assertIn("mistral_async", Config.SERVICE_API_KEY_MAP)
        self.assertIn("mistral", Config.SERVICE_LABELS)
        self.assertIn("mistral_async", Config.SERVICE_LABELS)
        self.assertEqual(Config.SERVICE_LABELS["mistral"], "Mistral (Segmented)")

    def test_smallest_service_mapping_exists(self):
        self.assertIn("smallest", Config.SERVICE_API_KEY_MAP)
        self.assertIn("smallest_async", Config.SERVICE_API_KEY_MAP)
        self.assertIn("smallest", Config.SERVICE_LABELS)
        self.assertIn("smallest_async", Config.SERVICE_LABELS)

    def test_azure_mai_service_mapping_exists(self):
        self.assertIn("azure_mai", Config.SERVICE_API_KEY_MAP)
        self.assertIn("azure_mai", Config.SERVICE_LABELS)

    def test_assemblyai_service_mapping_exists(self):
        self.assertIn("assemblyai", Config.SERVICE_API_KEY_MAP)
        self.assertIn("assemblyai_realtime", Config.SERVICE_API_KEY_MAP)
        self.assertIn("assemblyai", Config.SERVICE_LABELS)
        self.assertIn("assemblyai_realtime", Config.SERVICE_LABELS)

    def test_openrouter_service_mapping_exists(self):
        self.assertIn("openrouter", Config.SERVICE_API_KEY_MAP)
        self.assertNotIn("openrouter", Config.SERVICE_LABELS)

    def test_aws_transcribe_is_not_supported(self):
        self.assertNotIn("aws", Config.SERVICE_API_KEY_MAP)
        self.assertNotIn("aws", Config.SERVICE_LABELS)

    def test_soniox_async_default_model_is_v5(self):
        self.assertEqual(Config.DEFAULT_SONIOX_ASYNC_MODEL, "stt-async-v5")

    def test_soniox_realtime_default_model_is_v5(self):
        self.assertEqual(Config.DEFAULT_SONIOX_RT_MODEL, "stt-rt-v5")

    def test_soniox_default_region_is_us(self):
        self.assertEqual(Config.DEFAULT_SONIOX_REGION, "us")

    def test_historical_soniox_models_are_registered_for_default_migration(self):
        self.assertIn("stt-async-preview", Config._LEGACY_DEFAULT_SONIOX_ASYNC_MODELS)
        self.assertIn("stt-async-v4", Config._LEGACY_DEFAULT_SONIOX_ASYNC_MODELS)
        self.assertIn("stt-rt-v3", Config._LEGACY_DEFAULT_SONIOX_RT_MODELS)
        self.assertIn("stt-rt-v4", Config._LEGACY_DEFAULT_SONIOX_RT_MODELS)

    def test_post_processing_default_model_is_cerebras_gemma(self):
        self.assertEqual(Config.DEFAULT_POST_PROCESSING_MODEL, "cerebras/gemma-4-31b")
        self.assertIn("google/gemini-2.5-flash-lite:nitro", Config._LEGACY_DEFAULT_POST_PROCESSING_MODELS)
        self.assertIn("openai/gpt-oss-120b", Config._LEGACY_DEFAULT_POST_PROCESSING_MODELS)


def test_bootstrap_runtime_env_reads_only_path_keys(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    (tmp_path / ".env").write_text(
        f"SCRIBER_DATA_DIR={data_dir}\n"
        "SCRIBER_LEGACY_DATA_DIR=C:\\Legacy\\Scriber\n"
        "SONIOX_API_KEY=should-not-bootstrap\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "repo_root", lambda: tmp_path)
    monkeypatch.delenv("SCRIBER_DATA_DIR", raising=False)
    monkeypatch.delenv("SCRIBER_LEGACY_DATA_DIR", raising=False)
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)

    config_module._bootstrap_runtime_env()

    assert os.environ["SCRIBER_DATA_DIR"] == str(data_dir)
    assert os.environ["SCRIBER_LEGACY_DATA_DIR"] == "C:\\Legacy\\Scriber"
    assert "SONIOX_API_KEY" not in os.environ


def test_bootstrap_runtime_env_keeps_process_values(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SCRIBER_DATA_DIR=C:\\Old\\Scriber\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("SCRIBER_DATA_DIR", "C:\\Process\\Scriber")

    config_module._bootstrap_runtime_env()

    assert os.environ["SCRIBER_DATA_DIR"] == "C:\\Process\\Scriber"


def test_persist_to_env_file_includes_text_injection_disable(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", True)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_DISABLE_TEXT_INJECTION=1" in target.read_text(encoding="utf-8")


def test_persist_to_env_file_includes_azure_mai_model(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "AZURE_MAI_MODEL", "mai-transcribe-1.5")

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_AZURE_MAI_MODEL=mai-transcribe-1.5" in target.read_text(encoding="utf-8")


def test_persist_to_env_file_includes_openrouter_api_key(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "openrouter-secret", raising=False)

    Config.persist_to_env_file(str(target))

    assert "OPENROUTER_API_KEY=openrouter-secret" in target.read_text(encoding="utf-8")


def test_persist_to_env_file_includes_modulate_api_key(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "MODULATE_API_KEY", "modulate-secret", raising=False)

    Config.persist_to_env_file(str(target))

    contents = target.read_text(encoding="utf-8")
    assert contents.count("MODULATE_API_KEY=") == 1
    assert "MODULATE_API_KEY=modulate-secret" in contents


def test_persist_to_env_file_includes_soniox_async_v5_default(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "SONIOX_ASYNC_MODEL", Config.DEFAULT_SONIOX_ASYNC_MODEL)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_SONIOX_ASYNC_MODEL=stt-async-v5" in target.read_text(encoding="utf-8")


def test_persist_to_env_file_includes_soniox_realtime_v5_default(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "SONIOX_RT_MODEL", Config.DEFAULT_SONIOX_RT_MODEL)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_SONIOX_RT_MODEL=stt-rt-v5" in target.read_text(encoding="utf-8")


def test_persist_to_env_file_includes_soniox_region(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "SONIOX_REGION", "eu", raising=False)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_SONIOX_REGION=eu" in target.read_text(encoding="utf-8")


def test_meeting_transcription_mode_is_validated_and_persisted(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "MEETING_TRANSCRIPTION_MODE", "live_final")
    monkeypatch.setattr(config_module, "_json_settings", dict(config_module._json_settings))
    monkeypatch.setenv("SCRIBER_MEETING_TRANSCRIPTION_MODE", "live_final")

    Config.set_meeting_transcription_mode(" FINAL_ONLY ")
    Config.persist_to_env_file(str(target))

    assert Config.MEETING_TRANSCRIPTION_MODE == "final_only"
    assert config_module._json_settings["meetingTranscriptionMode"] == "final_only"
    assert "SCRIBER_MEETING_TRANSCRIPTION_MODE=final_only" in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="live_final or final_only"):
        Config.set_meeting_transcription_mode("minute_chunks")


def test_persist_to_env_file_includes_assemblyai_models(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "ASSEMBLYAI_ASYNC_MODEL", Config.DEFAULT_ASSEMBLYAI_ASYNC_MODEL)
    monkeypatch.setattr(Config, "ASSEMBLYAI_RT_MODEL", Config.DEFAULT_ASSEMBLYAI_RT_MODEL)

    Config.persist_to_env_file(str(target))

    contents = target.read_text(encoding="utf-8")
    assert "SCRIBER_ASSEMBLYAI_ASYNC_MODEL=universal-3-5-pro" in contents
    assert "SCRIBER_ASSEMBLYAI_RT_MODEL=universal-3-5-pro" in contents


def test_persist_to_env_file_includes_post_processing_settings(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "POST_PROCESSING_ENABLED", True)
    monkeypatch.setattr(Config, "POST_PROCESSING_HOTKEY", "ctrl+shift+p")
    monkeypatch.setattr(Config, "POST_PROCESSING_MODEL", "gemini-flash-latest")

    Config.persist_to_env_file(str(target))

    contents = target.read_text(encoding="utf-8")
    assert "SCRIBER_POST_PROCESSING_ENABLED=1" in contents
    assert "SCRIBER_POST_PROCESSING_HOTKEY=ctrl+shift+p" in contents
    assert "SCRIBER_POST_PROCESSING_MODEL=gemini-flash-latest" in contents


def test_persist_to_env_file_includes_vad_segmentation_setting(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_SEGMENT_SPEECH_WITH_VAD=1" in target.read_text(encoding="utf-8")


def test_transcription_provider_models_expose_effective_provider_names(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_RT_MODEL", "stt-rt-custom")
    monkeypatch.setattr(Config, "SONIOX_ASYNC_MODEL", "stt-async-custom")
    monkeypatch.setattr(
        Config,
        "MISTRAL_RT_MODEL",
        "voxtral-mini-transcribe-realtime-2602",
    )
    monkeypatch.setattr(Config, "MISTRAL_ASYNC_MODEL", "voxtral-mini-2602")
    monkeypatch.setattr(Config, "OPENAI_REALTIME_STT_MODEL", "realtime-custom")

    models = Config.transcription_provider_models()

    assert models["soniox-realtime"] == "stt-rt-custom"
    assert models["soniox-async"] == "stt-async-custom"
    assert models["modulate-realtime"] == "velma-2-stt-streaming"
    assert models["modulate-async"] == "velma-2-stt-batch"
    assert models["mistral-realtime"] == "voxtral-mini-2602"
    assert models["openai"] == "realtime-custom"
    assert models["google"] == "latest_long"
    assert models["groq"] == "whisper-large-v3-turbo"
    assert models["speechmatics"] == "enhanced"
    assert models["gladia-async"] == "provider default (Gladia Pre-recorded API v2)"


def test_persist_to_env_file_includes_youtube_caption_preference(monkeypatch, tmp_path):
    target = tmp_path / ".env"
    monkeypatch.setattr(Config, "YOUTUBE_PREFER_CAPTIONS", False)

    Config.persist_to_env_file(str(target))

    assert "SCRIBER_YOUTUBE_PREFER_CAPTIONS=0" in target.read_text(encoding="utf-8")


def test_json_setting_setters_are_batched_until_explicit_persist(monkeypatch, tmp_path):
    target = tmp_path / "settings.json"
    writes = []
    monkeypatch.setattr(config_module, "_JSON_SETTINGS_PATH", target)
    monkeypatch.setattr(
        config_module,
        "_atomic_write_text",
        lambda path, content: writes.append((Path(path), content)),
    )
    monkeypatch.setattr(config_module, "_json_settings_migration_pending", True)

    Config.set_post_processing_enabled(False)
    Config.set_post_processing_model("test/model")
    Config.set_segment_speech_with_vad(True)
    Config.set_youtube_prefer_captions(False)

    assert writes == []
    assert Config.json_settings_migration_pending() is True
    Config.persist_json_settings()
    assert len(writes) == 1
    assert Config.json_settings_migration_pending() is False
    assert writes[0][0] == target
    assert '"postProcessingModel": "test/model"' in writes[0][1]
    assert '"youtubePreferCaptions": false' in writes[0][1]


def test_atomic_write_cleans_unique_temporary_file_after_replace_failure(monkeypatch, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")

    def _fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", _fail_replace)

    try:
        config_module._atomic_write_text(target, "new")
    except OSError as exc:
        assert str(exc) == "replace failed"
    else:
        raise AssertionError("replace failure must propagate")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []
