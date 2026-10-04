from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ctypes import wintypes

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


def test_restart_preserves_previous_diagnostics_by_default(isolated_logging):
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    logger.info("before-restart-marker")
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    logger.info("after-restart-marker")

    for filename in ("latest.log", "latest.structured.jsonl"):
        contents = (isolated_logging / filename).read_text(encoding="utf-8")
        assert "before-restart-marker" in contents
        assert "after-restart-marker" in contents


def test_python_diagnostic_files_rotate_with_bounded_retention(monkeypatch, isolated_logging):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 2048)
    monkeypatch.setattr(logging_setup, "_LOG_RETENTION_FILES", 2)
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    for number in range(40):
        logger.info("rotation-marker-{:03d} {}", number, "x" * 128)

    for extension in ("log", "jsonl"):
        files = list(isolated_logging.glob(f"*.{extension}"))
        assert 1 < len(files) <= 3
        assert all(path.stat().st_size <= 2048 for path in files)
        contents = "\n".join(path.read_text(encoding="utf-8") for path in files)
        assert "rotation-marker-039" in contents
        assert "rotation-marker-000" not in contents


def test_rotation_permission_error_is_bounded_recovers_and_never_dumps_raw_record(monkeypatch, isolated_logging, capfd):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 4096)
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    original_replace = logging_setup.os.replace

    def locked_replace(*_args):
        raise PermissionError("synthetic private path and secret exception payload")

    monkeypatch.setattr(logging_setup.os, "replace", locked_replace)
    for number in range(30):
        logger.bind(api_key="private-record-extra").info("locked-record-{:03d} {}", number, "x" * 250)
    for name in ("latest.log", "latest.structured.jsonl"):
        path = isolated_logging / name
        assert path.stat().st_size <= 8192
        contents = path.read_text(encoding="utf-8")
        assert "locked-record-000" in contents
        assert "locked-record-029" not in contents
        assert contents.count("diagnostic.rotation.blocked") == 1
        assert "secret exception payload" not in contents
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""

    monkeypatch.setattr(logging_setup.os, "replace", original_replace)
    logger.info("after-lock-recovery")
    for name in ("latest.log", "latest.structured.jsonl"):
        contents = (isolated_logging / name).read_text(encoding="utf-8")
        assert "after-lock-recovery" in contents
        assert contents.count("diagnostic.rotation.recovered") == 1
    structured = [
        json.loads(line)
        for line in (isolated_logging / "latest.structured.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    recovery = next(row for row in structured if row.get("event") == "diagnostic.rotation.recovered")
    assert recovery["meta"]["dropped_records"] > 0
    assert recovery["meta"]["dropped_bytes"] > 0
    assert "private-record-extra" not in json.dumps(recovery)
    assert capfd.readouterr().err == ""


@contextmanager
def _windows_reader_without_delete_sharing(path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, OPEN_EXISTING.
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        assert close_handle(handle), ctypes.get_last_error()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reader sharing semantics")
@pytest.mark.parametrize("locked_target", ["active", "oldest_archive"])
def test_real_windows_lock_preserves_archives_caps_append_and_reports_exact_losses(
    monkeypatch, tmp_path, locked_target, capfd
):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 1024)
    path = tmp_path / "latest.log"
    sink = logging_setup._BoundedDiagnosticSink(path, append=True, structured=False)
    archives = [tmp_path / f"latest.{index}.log" for index in range(1, 4)]
    for index, archive in enumerate(archives, 1):
        archive.write_text(f"archive-{index}\n", encoding="utf-8")
        os.utime(archive, (index, index))
    archived_bytes = {archive.name: archive.read_bytes() for archive in archives}
    sink.write("existing-before-lock " + "x" * 930 + "\n")
    records = [f"locked-{index:02d} " + "y" * 90 + "\n" for index in range(20)]
    target = path if locked_target == "active" else archives[0]
    with _windows_reader_without_delete_sharing(target):
        for record in records:
            sink.write(record)
        during_lock = path.read_text(encoding="utf-8")
        assert path.stat().st_size <= 2048
        assert "existing-before-lock" in during_lock
        assert "locked-00" in during_lock
        assert during_lock.count("diagnostic.rotation.blocked") == 1
        assert {archive.name: archive.read_bytes() for archive in archives} == archived_bytes
    dropped = [record for record in records if record not in during_lock]
    assert dropped
    sink.write("after-release\n")
    recovered = path.read_text(encoding="utf-8")
    assert "after-release" in recovered
    assert recovered.count("diagnostic.rotation.recovered") == 1
    assert f"dropped_records={len(dropped)} " in recovered
    assert f"dropped_bytes={sum(len(record.encode('utf-8')) for record in dropped)}\n" in recovered
    assert archives[0].read_text(encoding="utf-8") == during_lock
    assert len(list(tmp_path.glob("*.log"))) == 4
    assert all(file.stat().st_size <= 2048 for file in tmp_path.glob("*.log"))
    assert capfd.readouterr().err == ""


def test_oversized_utf8_record_is_counted_once_and_structured_recovery_remains_valid(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 1024)
    path = tmp_path / "latest.structured.jsonl"
    sink = logging_setup._BoundedDiagnosticSink(path, append=True, structured=True)
    oversized = "private-oversized-" + "ä" * 1024
    sink.write(oversized)
    sink.write(json.dumps({"message": "accepted-after-oversized"}) + "\n")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    recovery = next(row for row in rows if row.get("event") == "diagnostic.rotation.recovered")
    assert recovery["meta"] == {
        "reason": "record_too_large",
        "dropped_records": 1,
        "dropped_bytes": len(oversized.encode("utf-8")),
    }
    assert rows[-1]["message"] == "accepted-after-oversized"
    assert all(file.stat().st_size <= 2048 for file in tmp_path.glob("*.jsonl"))
    assert "private-oversized-" not in "".join(file.read_text(encoding="utf-8") for file in tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("legacy_count", [3, 5])
def test_rotation_reuses_legacy_timestamp_archives_without_adding_generations(monkeypatch, tmp_path, legacy_count):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 1024)
    path = tmp_path / "latest.structured.jsonl"
    archives = [tmp_path / f"latest.structured.2026-10-01_12-00-0{index}_000000.jsonl" for index in range(legacy_count)]
    for index, archive in enumerate(archives, 1):
        archive.write_text(f"legacy-{index}\n", encoding="utf-8")
        os.utime(archive, (index, index))
    sink = logging_setup._BoundedDiagnosticSink(path, append=True, structured=True)
    sink.write("a" * 1000 + "\n")
    sink.write("new-current\n" * 10)
    assert len(list(tmp_path.glob("*.jsonl"))) == 4
    assert archives[0].read_text(encoding="utf-8") == "a" * 1000 + "\n"
    assert "new-current" in path.read_text(encoding="utf-8")


def test_recovery_notice_cannot_displace_a_record_at_the_normal_size_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 1024)
    path = tmp_path / "latest.log"
    sink = logging_setup._BoundedDiagnosticSink(path, append=True, structured=False)
    sink.write("before-lock\n" * 80)
    original_replace = logging_setup.os.replace

    def locked_replace(*_args):
        raise PermissionError("synthetic sharing violation")

    monkeypatch.setattr(logging_setup.os, "replace", locked_replace)
    sink.write("during-lock\n" * 20)
    monkeypatch.setattr(logging_setup.os, "replace", original_replace)
    at_limit = "legitimate-large-record " + "x" * (1024 - len("legitimate-large-record ") - 1) + "\n"
    sink.write(at_limit)
    contents = path.read_text(encoding="utf-8")
    assert at_limit in contents
    assert contents.count("diagnostic.rotation.recovered") == 1
    assert "dropped_records=0 " in contents
    assert path.stat().st_size <= 2048


def test_blocked_notice_reserves_space_for_the_original_record(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 1024)
    path = tmp_path / "latest.log"
    sink = logging_setup._BoundedDiagnosticSink(path, append=True, structured=False)
    sink.write("x" * 999 + "\n")
    original_replace = logging_setup.os.replace

    def locked_replace(*_args):
        raise PermissionError("synthetic sharing violation")

    monkeypatch.setattr(logging_setup.os, "replace", locked_replace)
    original = "must-fit-original " + "y" * (950 - len("must-fit-original ") - 1) + "\n"
    sink.write(original)
    assert original in path.read_text(encoding="utf-8")
    assert path.stat().st_size == 1950
    monkeypatch.setattr(logging_setup.os, "replace", original_replace)
    sink.write("recovered\n")
    recovered = path.read_text(encoding="utf-8")
    assert "diagnostic.rotation.recovered" in recovered
    assert "dropped_records=0 " in recovered


def test_append_failure_does_not_leak_record_or_exception_and_counts_recovery(monkeypatch, isolated_logging, capfd):
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    original_append = logging_setup._BoundedDiagnosticSink._append

    def locked_append(*_args, **_kwargs):
        raise PermissionError("private-failure-detail")

    monkeypatch.setattr(logging_setup._BoundedDiagnosticSink, "_append", locked_append)
    logger.bind(api_key="private-extra").info("first-unwritten-record")
    logger.bind(api_key="private-extra").info("second-unwritten-record")
    assert capfd.readouterr().err == ""
    monkeypatch.setattr(logging_setup._BoundedDiagnosticSink, "_append", original_append)
    logger.info("success-after-write-denial")
    rows = [
        json.loads(line)
        for line in (isolated_logging / "latest.structured.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    recovery = next(row for row in rows if row.get("event") == "diagnostic.rotation.recovered")
    assert recovery["meta"]["dropped_records"] == 2
    assert recovery["meta"]["dropped_bytes"] > 0
    assert "private" not in json.dumps(rows)
    assert capfd.readouterr().err == ""


def test_archive_slot_directory_collisions_cannot_escape_to_loguru_stderr(monkeypatch, isolated_logging, capfd):
    monkeypatch.setattr(logging_setup, "_LOG_ROTATION_BYTES", 4096)
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    collisions = []
    for name in ("latest.log", "latest.structured.jsonl"):
        active = isolated_logging / name
        for index in range(1, 4):
            collision = active.with_name(f"{active.stem}.{index}{active.suffix}")
            collision.mkdir()
            collisions.append(collision)
    for index in range(30):
        logger.bind(api_key="collision-private-extra").info("slot-collision-{} {}", index, "x" * 250)
    for name in ("latest.log", "latest.structured.jsonl"):
        active = isolated_logging / name
        assert active.stat().st_size <= 8192
        assert active.read_text(encoding="utf-8").count("diagnostic.rotation.blocked") == 1
    assert capfd.readouterr().err == ""
    for collision in collisions:
        collision.rmdir()
    logger.info("after-slot-recovery")
    for name in ("latest.log", "latest.structured.jsonl"):
        contents = (isolated_logging / name).read_text(encoding="utf-8")
        assert "after-slot-recovery" in contents
        assert contents.count("diagnostic.rotation.recovered") == 1
    assert capfd.readouterr().err == ""


def test_explicit_non_append_setup_still_truncates_current_logs(isolated_logging):
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    logger.info("before-explicit-truncate")
    logging_setup.setup_logging(component="test", force=True, add_stderr=False, append=False)
    logger.info("after-explicit-truncate")
    for name in ("latest.log", "latest.structured.jsonl"):
        contents = (isolated_logging / name).read_text(encoding="utf-8")
        assert "before-explicit-truncate" not in contents
        assert "after-explicit-truncate" in contents


def test_off_waits_for_an_admitted_synchronous_file_sink(monkeypatch, isolated_logging):
    logging_setup.setup_logging(component="test", force=True, add_stderr=False)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    append = logging_setup._BoundedDiagnosticSink._append

    def blocked_append(self, data, *, limit):
        if b"admitted-file-record" in data:
            entered.set()
            assert release.wait(5)
        return append(self, data, limit=limit)

    def disable():
        logging_setup.set_diagnostic_logging_enabled(False)
        stopped.set()

    monkeypatch.setattr(logging_setup._BoundedDiagnosticSink, "_append", blocked_append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(logger.info, "admitted-file-record")
        assert entered.wait(5)
        off = pool.submit(disable)
        try:
            assert not stopped.wait(0.05)
        finally:
            release.set()
        writer.result(timeout=5)
        off.result(timeout=5)
    before = {path.name: path.read_bytes() for path in isolated_logging.iterdir()}
    assert b"admitted-file-record" in before["latest.log"]
    logger.enable("")
    logger.error("after-off-forbidden")
    assert {path.name: path.read_bytes() for path in isolated_logging.iterdir()} == before
