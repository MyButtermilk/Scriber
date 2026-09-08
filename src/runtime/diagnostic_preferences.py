"""Apply one logging preference across persistence, shell and Python writers."""

from __future__ import annotations

from contextlib import suppress

from src.config import Config
from src.core.logging_setup import set_diagnostic_logging_enabled
from src.runtime import shell_ipc


def apply_diagnostic_logging_preference(enabled: bool) -> None:
    """Run behind the settings lock and one thread cancellation barrier.

    A successful return means the shell acknowledged, the preference is durable,
    and Python stopped/resumed its sinks. Unavailable source-mode shells are not
    involved. Sink admission must succeed before the atomic persistence commit;
    an admission or persistence failure restores both live owners.
    """
    if not isinstance(enabled, bool):
        raise ValueError("Diagnostic logging must be a boolean.")
    previous = Config.DIAGNOSTIC_LOGGING_ENABLED
    native = shell_ipc.available()
    if native:
        try:
            _set_shell_logging(enabled)
        except Exception:
            # A lost acknowledgement can hide an applied native command. Restore
            # the prior preference before reporting the failed transaction.
            with suppress(Exception):
                _set_shell_logging(previous)
            raise
    try:
        set_diagnostic_logging_enabled(enabled)
        Config.set_diagnostic_logging_enabled(enabled)
        # This preference lives only in .env. Its atomic replacement is the
        # commit point; an unrelated settings.json failure must not create a
        # false rollback with a different preference on the next launch.
        Config.persist_to_env_file()
    except Exception as exc:
        Config.set_diagnostic_logging_enabled(previous)
        try:
            set_diagnostic_logging_enabled(previous)
        except Exception as rollback_error:
            exc.add_note(f"Python logging rollback failed: {type(rollback_error).__name__}")
        if native:
            try:
                _set_shell_logging(previous)
            except Exception as rollback_error:
                exc.add_note(f"Desktop logging rollback failed: {type(rollback_error).__name__}")
        raise


def _set_shell_logging(enabled: bool) -> None:
    # Idempotent retry also reconciles a command whose acknowledgement was lost.
    for _ in range(2):
        response = shell_ipc.call_shell_ipc("setDiagnosticLoggingEnabled", {"enabled": enabled}, timeout_seconds=1.0)
        payload = response.get("payload")
        if response.get("success") is True and isinstance(payload, dict) and payload.get("enabled") is enabled:
            return
    raise RuntimeError("The desktop logging setting could not be applied. Please try again.")
