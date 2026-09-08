from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from loguru import logger

from src.core import logging_setup


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    monkeypatch.setattr(logging_setup, "_LOGGING_ENABLED", True)
    monkeypatch.setattr(logging_setup, "_LAST_STDERR", False)
    monkeypatch.setattr(logging_setup, "_LAST_COMPONENT", "test")
    monkeypatch.setenv("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED", "1")
    yield tmp_path
    logger.remove()
    logger.enable("")
    logger.enable(None)
    logging.disable(logging.NOTSET)


def test_runtime_off_stops_files_and_lazy_payload_evaluation_and_on_resumes(isolated_logging):
    root = isolated_logging
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    logger.info("before-off-marker")
    sizes = {path.name: path.stat().st_size for path in root.iterdir()}
    logging_setup.set_diagnostic_logging_enabled(False)

    def forbidden():
        raise AssertionError("disabled logger evaluated expensive payload")

    logger.opt(lazy=True).info("{}", forbidden)
    logger.error("disabled-error-marker")
    logging_setup.emit_event(logger, "disabled-event-marker", meta={"status": "disabled"})
    # A provider library may re-enable its Loguru namespace when imported. Sink
    # gates still enforce the user's off preference even if that happens.
    logger.enable("")
    logger.error("disabled-library-marker")
    assert {path.name: path.stat().st_size for path in root.iterdir()} == sizes
    logging_setup.set_diagnostic_logging_enabled(True)
    logger.info("after-on-marker")
    contents = (root / "latest.log").read_text(encoding="utf-8")
    assert "before-off-marker" in contents and "after-on-marker" in contents
    assert "disabled" not in contents


def test_disabled_startup_preserves_existing_logs_and_later_enable_appends(monkeypatch, isolated_logging):
    root = isolated_logging
    (root / "latest.log").write_text("existing-log-marker\n", encoding="utf-8")
    monkeypatch.setenv("SCRIBER_DIAGNOSTIC_LOGGING_ENABLED", "0")
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    logger.error("disabled-startup-marker")
    assert sorted(path.name for path in root.iterdir()) == ["latest.log"]
    assert (root / "latest.log").read_text(encoding="utf-8") == "existing-log-marker\n"
    logging_setup.set_diagnostic_logging_enabled(True)
    logger.info("resumed-marker")
    contents = (root / "latest.log").read_text(encoding="utf-8")
    assert "existing-log-marker" in contents and "resumed-marker" in contents
    assert "disabled-startup-marker" not in contents


def test_off_waits_for_admitted_diagnostic_write_and_rejects_later_writes(isolated_logging):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    writes = []

    def write():
        entered.set()
        assert release.wait(5)
        writes.append("committed")

    def disable():
        logging_setup.set_diagnostic_logging_enabled(False)
        stopped.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_write = pool.submit(logging_setup.run_diagnostic_write, write)
        assert entered.wait(5)
        pending_off = pool.submit(disable)
        try:
            assert not stopped.wait(0.05), "off returned while a diagnostic write was active"
        finally:
            release.set()
        pending_write.result(timeout=5)
        pending_off.result(timeout=5)
    logging_setup.run_diagnostic_write(writes.append, "must-not-write")
    assert writes == ["committed"]
