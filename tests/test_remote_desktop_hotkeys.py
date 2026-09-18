import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.config as config_module
from src import web_api
from src.config import Config
from src.web_api import ScriberWebController


def fresh_enabled(tmp_path):
    env = {**os.environ, "SCRIBER_DATA_DIR": str(tmp_path), "SCRIBER_SKIP_LEGACY_DATA_MIGRATION": "1"}
    result = subprocess.run(
        [sys.executable, "-c", "from src.config import Config; print(Config.REMOTE_DESKTOP_HOTKEYS)"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "True"


def test_remote_desktop_hotkeys_default_off_and_persist_across_restart(tmp_path, monkeypatch):
    assert not fresh_enabled(tmp_path)
    monkeypatch.setattr(config_module, "_json_settings", {})
    monkeypatch.setattr(config_module, "_JSON_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(Config, "REMOTE_DESKTOP_HOTKEYS", False)
    for enabled in (True, False):
        Config.set_remote_desktop_hotkeys(enabled)
        Config.persist_json_settings()
        assert fresh_enabled(tmp_path) is enabled


@pytest.mark.asyncio
async def test_settings_update_round_trips_the_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SETTINGS_PERSIST_DEBOUNCE_SEC", "60")
    monkeypatch.setattr(web_api.db, "_DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(Config, "REMOTE_DESKTOP_HOTKEYS", False)
    monkeypatch.setattr(Config, "persist_settings_files", Mock())
    monkeypatch.setattr(Config, "persist_json_settings", Mock())
    monkeypatch.setattr(config_module, "_json_settings", dict(config_module._json_settings))
    controller = ScriberWebController(asyncio.get_running_loop())
    try:
        assert controller.get_settings()["remoteDesktopHotkeys"] is False
        for enabled in (True, False):
            updated = await controller.update_settings({"remoteDesktopHotkeys": enabled})
            assert updated["remoteDesktopHotkeys"] is enabled
            assert config_module._json_settings["remoteDesktopHotkeys"] is enabled
    finally:
        controller.shutdown()


@pytest.mark.parametrize("invalid", ["true", "false", 1, None, []])
def test_non_boolean_stored_value_never_enables_keyboard_hook(tmp_path, invalid):
    (tmp_path / "settings.json").write_text(json.dumps({"remoteDesktopHotkeys": invalid}), encoding="utf-8")
    assert not fresh_enabled(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["true", 1, None, []])
async def test_api_rejects_invalid_opt_in_before_any_mutation(monkeypatch, invalid):
    monkeypatch.setattr(Config, "REMOTE_DESKTOP_HOTKEYS", False)
    monkeypatch.setattr(Config, "HOTKEY", "ctrl+space")
    controller = ScriberWebController.__new__(ScriberWebController)
    controller._settings_update_lock = asyncio.Lock()
    with pytest.raises(ValueError, match="must be a boolean"):
        await controller.update_settings({"remoteDesktopHotkeys": invalid, "hotkey": "ctrl+a"})
    assert Config.REMOTE_DESKTOP_HOTKEYS is False
    assert Config.HOTKEY == "ctrl+space"
