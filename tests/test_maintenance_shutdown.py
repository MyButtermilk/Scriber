"""Shutdown must join maintenance SQLite work before closing its connections."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src import database, web_api
from src.config import Config
from src.data.transcript_artifact_store import SourceAssetState, TranscriptArtifactStore


@pytest_asyncio.fixture
async def controller_factory(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "maintenance.sqlite")
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "MIC_ALWAYS_ON", False)
    monkeypatch.setattr(web_api, "shell_ipc_available", lambda: False)
    controllers = []

    def create_controller():
        controller = web_api.ScriberWebController(asyncio.get_running_loop())
        monkeypatch.setattr(controller._outlook_calendar, "status", AsyncMock(return_value={"configured": False}))
        controllers.append(controller)
        return controller

    try:
        yield create_controller
    finally:
        for controller in controllers:
            await controller.drain_background_tasks_for_shutdown(timeout_seconds=1)
            controller.shutdown()
            controller.close_persistence_stores()
        database._close_all_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_mode", ["timeout", "maintenance_cancel", "caller_cancel", "shutdown_first"])
async def test_shutdown_joins_exact_source_scan_before_store_close(monkeypatch, controller_factory, shutdown_mode):
    loop = asyncio.get_running_loop()
    read_entered = asyncio.Event()
    read_finished = asyncio.Event()
    bounded_wait_entered = asyncio.Event()
    release_read = threading.Event()
    completed_read = threading.Event()
    scan = TranscriptArtifactStore.list_source_assets_by_state

    def blocked_scan(store, state, **kwargs):
        assert state == SourceAssetState.PURGE_PENDING
        assert kwargs == {"purpose": "processing_only"}
        loop.call_soon_threadsafe(read_entered.set)
        assert release_read.wait(5), "The test must release the maintenance scan"
        try:
            return scan(store, state, **kwargs)
        finally:
            completed_read.set()
            loop.call_soon_threadsafe(read_finished.set)

    monkeypatch.setattr(TranscriptArtifactStore, "list_source_assets_by_state", blocked_scan)
    controller = controller_factory()
    maintenance = controller._meeting_retention_task
    assert maintenance is not None
    closed = []
    close_artifacts = controller._transcript_artifacts.close
    close_database = database._close_all_connections

    def observe_close(name, close):
        def guarded_close():
            closed.append((name, completed_read.is_set()))
            # Never intentionally close SQLite while the test worker owns it,
            # even if the production ordering regresses.
            if completed_read.is_set():
                close()

        return guarded_close

    monkeypatch.setattr(controller._transcript_artifacts, "close", observe_close("artifacts", close_artifacts))
    monkeypatch.setattr(database, "_close_all_connections", observe_close("database", close_database))
    wait = asyncio.wait

    async def observe_wait(futures, **kwargs):
        if maintenance in futures and "timeout" in kwargs:
            bounded_wait_entered.set()
        return await wait(futures, **kwargs)

    monkeypatch.setattr(asyncio, "wait", observe_wait)
    shutdown = None

    async def drain_then_close():
        try:
            return await controller.drain_background_tasks_for_shutdown(
                timeout_seconds=5 if shutdown_mode == "caller_cancel" else 0
            )
        finally:
            controller.shutdown()
            controller.close_persistence_stores()

    try:
        await asyncio.wait_for(read_entered.wait(), timeout=2)
        if shutdown_mode == "shutdown_first":
            controller.shutdown()
            assert controller._meeting_retention_task is maintenance
        shutdown = asyncio.create_task(drain_then_close())
        await asyncio.wait_for(bounded_wait_entered.wait(), timeout=2)
        # Cross the bounded wait and enter its cleanup; the worker remains
        # blocked regardless of how fast this machine runs.
        await asyncio.sleep(0)
        if shutdown_mode == "maintenance_cancel":
            maintenance.cancel()
            await asyncio.sleep(0)
            maintenance.cancel()
        elif shutdown_mode == "caller_cancel":
            shutdown.cancel()
            await asyncio.sleep(0)
            shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert not completed_read.is_set()
        assert closed == []

        release_read.set()
        if shutdown_mode == "caller_cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(shutdown, timeout=2)
        else:
            await asyncio.wait_for(shutdown, timeout=2)
        assert maintenance.done()
        assert completed_read.is_set()
        assert closed == [("artifacts", True), ("database", True)]
    finally:
        release_read.set()
        await asyncio.wait_for(read_finished.wait(), timeout=5)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_maintenance_shutdown_finishes_promptly(controller_factory):
    controller = controller_factory()
    maintenance = controller._meeting_retention_task
    assert maintenance is not None
    await asyncio.wait_for(controller.drain_background_tasks_for_shutdown(timeout_seconds=0), timeout=1)
    assert maintenance.done()
    controller.shutdown()
    assert controller._meeting_retention_task is None
    controller.close_persistence_stores()
