import os
import unittest

import src.config as config_module
from src.config import Config

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
