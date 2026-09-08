from __future__ import annotations

import logging

import pytest
from loguru import logger

from src.config import Config
from src.core import logging_setup
from src.runtime import diagnostic_preferences as preferences


@pytest.fixture
def owners(monkeypatch):
    monkeypatch.setattr(Config, "DIAGNOSTIC_LOGGING_ENABLED", True)
    monkeypatch.setenv("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED", "1")
    monkeypatch.setattr(preferences.shell_ipc, "available", lambda: True)
    operations = []

    def shell(command, payload, **_kwargs):
        assert command == "setDiagnosticLoggingEnabled"
        operations.append(("shell", payload["enabled"]))
        return {"success": True, "payload": payload}

    monkeypatch.setattr(preferences.shell_ipc, "call_shell_ipc", shell)
    monkeypatch.setattr(
        Config, "persist_to_env_file", lambda: operations.append(("persist", Config.DIAGNOSTIC_LOGGING_ENABLED))
    )
    monkeypatch.setattr(
        preferences, "set_diagnostic_logging_enabled", lambda enabled: operations.append(("python", enabled))
    )
    return operations


def test_preference_confirms_shell_persists_and_applies_python_before_returning(owners):
    preferences.apply_diagnostic_logging_preference(False)
    assert owners == [("shell", False), ("python", False), ("persist", False)]
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is False


def test_source_mode_does_not_require_shell(monkeypatch, owners):
    monkeypatch.setattr(preferences.shell_ipc, "available", lambda: False)
    preferences.apply_diagnostic_logging_preference(False)
    assert owners == [("python", False), ("persist", False)]


def test_failed_shell_confirmation_does_not_persist_a_false_effective_state(monkeypatch, owners):
    monkeypatch.setattr(preferences.shell_ipc, "call_shell_ipc", lambda *_args, **_kwargs: {"success": False})
    with pytest.raises(RuntimeError, match="could not be applied"):
        preferences.apply_diagnostic_logging_preference(False)
    assert owners == []
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is True


def test_persistence_failure_restores_native_and_config_owners(monkeypatch, owners):
    def fail():
        raise OSError("test disk unavailable")

    monkeypatch.setattr(Config, "persist_to_env_file", fail)
    with pytest.raises(OSError):
        preferences.apply_diagnostic_logging_preference(False)
    assert owners == [("shell", False), ("python", False), ("python", True), ("shell", True)]
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is True


def test_lost_native_acknowledgement_reconciles_to_previous_preference(monkeypatch, owners):
    native_preferences = []

    def lose_off_ack(_command, payload, **_kwargs):
        native_preferences.append(payload["enabled"])
        return {"success": True, "payload": payload} if payload["enabled"] else {"success": False}

    monkeypatch.setattr(preferences.shell_ipc, "call_shell_ipc", lose_off_ack)
    with pytest.raises(RuntimeError):
        preferences.apply_diagnostic_logging_preference(False)
    assert native_preferences == [False, False, True]
    assert owners == []
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is True


def test_preference_commit_does_not_depend_on_unrelated_json_settings(monkeypatch, owners):
    def json_failure():
        raise OSError("unrelated settings.json is unavailable")

    monkeypatch.setattr(Config, "persist_json_settings", json_failure)
    preferences.apply_diagnostic_logging_preference(False)
    assert owners == [("shell", False), ("python", False), ("persist", False)]


@pytest.mark.parametrize("invalid", [None, "false", 0, 1, []])
def test_logging_preference_requires_an_actual_boolean(invalid, owners):
    with pytest.raises(ValueError):
        preferences.apply_diagnostic_logging_preference(invalid)
    assert owners == []


@pytest.fixture
def actual_sinks(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    monkeypatch.setattr(logging_setup, "_LOGGING_ENABLED", False)
    monkeypatch.setattr(logging_setup, "_LAST_STDERR", False)
    monkeypatch.setattr(logging_setup, "_LAST_COMPONENT", "test")
    monkeypatch.setattr(Config, "DIAGNOSTIC_LOGGING_ENABLED", False)
    monkeypatch.setenv("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED", "0")
    monkeypatch.setattr(preferences.shell_ipc, "available", lambda: True)
    native = {"enabled": False, "calls": []}

    def shell(_command, payload, **_kwargs):
        native["enabled"] = payload["enabled"]
        native["calls"].append(payload["enabled"])
        return {"success": True, "payload": payload}

    monkeypatch.setattr(preferences.shell_ipc, "call_shell_ipc", shell)
    durable = tmp_path / "fixture.env"
    durable.write_text("0", encoding="utf-8")
    monkeypatch.setattr(
        Config,
        "persist_to_env_file",
        lambda: durable.write_text("1" if Config.DIAGNOSTIC_LOGGING_ENABLED else "0", encoding="utf-8"),
    )
    logging_setup.set_diagnostic_logging_enabled(False)
    yield tmp_path / "logs", durable, native
    logger.remove()
    logger.enable("")
    logger.enable(None)
    logging.disable(logging.NOTSET)


def test_actual_partial_sink_admission_failure_keeps_durable_and_live_owners_off(actual_sinks):
    root, durable, native = actual_sinks
    blocked_sink = root / "latest.structured.jsonl"
    blocked_sink.mkdir(parents=True)
    (root / "latest.log").write_text("kept-history\n", encoding="utf-8")

    with pytest.raises(OSError):
        preferences.apply_diagnostic_logging_preference(True)

    assert native == {"enabled": False, "calls": [True, False]}
    assert durable.read_text(encoding="utf-8") == "0"
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is False
    assert logging_setup.diagnostic_logging_enabled() is False
    assert logging_setup._CONFIGURED is False
    logger.error("must-not-be-persisted")
    writes = []
    logging_setup.run_diagnostic_write(writes.append, "must-not-be-persisted")
    assert writes == []
    assert (root / "latest.log").read_text(encoding="utf-8") == "kept-history\n"

    # The partially opened pretty sink was released and retry can admit all
    # sinks once the external file collision has been resolved.
    blocked_sink.rmdir()
    preferences.apply_diagnostic_logging_preference(True)
    logger.info("successful-retry")
    assert native["enabled"] is True
    assert durable.read_text(encoding="utf-8") == "1"
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is True
    assert logging_setup.diagnostic_logging_enabled() is True
    assert "kept-history" in (root / "latest.log").read_text(encoding="utf-8")
    assert "successful-retry" in (root / "latest.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("previous", [False, True])
def test_persistence_failure_rolls_back_actual_sinks_native_and_config(monkeypatch, actual_sinks, previous):
    root, durable, native = actual_sinks
    if previous:
        preferences.apply_diagnostic_logging_preference(True)
        logger.info("before-failed-disable")
    native["calls"].clear()

    def fail():
        raise OSError("synthetic atomic commit failure")

    monkeypatch.setattr(Config, "persist_to_env_file", fail)
    with pytest.raises(OSError, match="synthetic atomic commit failure"):
        preferences.apply_diagnostic_logging_preference(not previous)

    assert native == {"enabled": previous, "calls": [not previous, previous]}
    assert durable.read_text(encoding="utf-8") == ("1" if previous else "0")
    assert Config.DIAGNOSTIC_LOGGING_ENABLED is previous
    assert logging_setup.diagnostic_logging_enabled() is previous
    logger.info("after-failed-mutation")
    contents = (root / "latest.log").read_text(encoding="utf-8")
    assert ("after-failed-mutation" in contents) is previous
    if previous:
        assert "before-failed-disable" in contents
