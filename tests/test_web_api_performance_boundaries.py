import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from src import web_api


class _UnstartedPipeline:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


def _audio_claim() -> web_api.AudioAdmissionClaim:
    return web_api.AudioAdmissionClaim(
        owner_kind="live_mic",
        owner_id="performance-boundary-session",
        controller_id="performance-boundary-controller",
        state_version=1,
        lease_expires_at="2099-01-01T00:00:00Z",
        updated_at="2026-07-15T00:00:00Z",
    )


def _shutdown_controller(store) -> web_api.ScriberWebController:
    controller = web_api.ScriberWebController.__new__(
        web_api.ScriberWebController
    )
    controller._retry_scheduler = MagicMock()
    controller._audio_admission_store = store
    controller._audio_controller_id = "performance-boundary-controller"
    controller._audio_admission_heartbeat_task = None
    controller._persistent_audio_claim = _audio_claim()
    controller._shutdown_audio_release_task = None
    controller._shutdown_audio_release_thread = None
    controller._live_mic_start_in_progress_generation = None
    controller._live_mic_cancel_start_generation = None
    return controller


@pytest.mark.asyncio
async def test_file_pipeline_construction_keeps_event_loop_responsive(
    monkeypatch,
):
    build_started = threading.Event()
    finish_build = threading.Event()
    build_threads: list[int] = []
    event_loop_thread = threading.get_ident()
    pipeline = _UnstartedPipeline()

    def blocking_builder(*_args, **_kwargs):
        build_threads.append(threading.get_ident())
        build_started.set()
        assert finish_build.wait(timeout=1.0)
        return pipeline

    monkeypatch.setattr(web_api, "_create_scriber_pipeline", blocking_builder)

    build_task = asyncio.create_task(
        web_api._create_scriber_pipeline_off_loop(service_name="test")
    )
    assert await asyncio.wait_for(
        asyncio.to_thread(build_started.wait, 1.0), timeout=1.5
    )

    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    assert not build_task.done()

    finish_build.set()
    assert await asyncio.wait_for(build_task, timeout=1.0) is pipeline
    assert build_threads == [build_threads[0]]
    assert build_threads[0] != event_loop_thread
    assert pipeline.stop_calls == 0


@pytest.mark.asyncio
async def test_cancelled_file_pipeline_construction_waits_and_cleans_up(
    monkeypatch,
):
    build_started = threading.Event()
    finish_build = threading.Event()
    pipeline = _UnstartedPipeline()

    def blocking_builder(*_args, **_kwargs):
        build_started.set()
        assert finish_build.wait(timeout=1.0)
        return pipeline

    monkeypatch.setattr(web_api, "_create_scriber_pipeline", blocking_builder)

    build_task = asyncio.create_task(
        web_api._create_scriber_pipeline_off_loop(service_name="test")
    )
    assert await asyncio.wait_for(
        asyncio.to_thread(build_started.wait, 1.0), timeout=1.5
    )
    build_task.cancel()
    await asyncio.sleep(0)
    assert not build_task.done()

    finish_build.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(build_task, timeout=1.0)
    assert pipeline.stop_calls == 1


@pytest.mark.asyncio
async def test_shutdown_audio_release_does_not_stall_event_loop():
    release_started = threading.Event()
    finish_release = threading.Event()

    class _BlockingStore:
        def __init__(self) -> None:
            self.released: list[web_api.AudioAdmissionClaim] = []

        def release(self, claim):
            release_started.set()
            assert finish_release.wait(timeout=1.0)
            self.released.append(claim)
            return True

    store = _BlockingStore()
    controller = _shutdown_controller(store)
    started = time.perf_counter()

    controller.begin_shutdown()

    begin_shutdown_ms = (time.perf_counter() - started) * 1_000
    release_task = controller._shutdown_audio_release_task
    assert release_task is not None
    assert begin_shutdown_ms < 50.0
    assert await asyncio.wait_for(
        asyncio.to_thread(release_started.wait, 1.0), timeout=1.5
    )

    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    assert not release_task.done()

    finish_release.set()
    assert await asyncio.wait_for(release_task, timeout=1.0) is True
    assert store.released == [_audio_claim()]


@pytest.mark.asyncio
async def test_cancelled_shutdown_audio_release_observes_cleanup_boundary():
    release_started = threading.Event()
    finish_release = threading.Event()

    class _BlockingStore:
        def __init__(self) -> None:
            self.released: list[web_api.AudioAdmissionClaim] = []

        def release(self, claim):
            release_started.set()
            assert finish_release.wait(timeout=1.0)
            self.released.append(claim)
            return True

    store = _BlockingStore()
    controller = _shutdown_controller(store)
    controller.begin_shutdown()
    release_task = controller._shutdown_audio_release_task
    assert release_task is not None
    assert await asyncio.wait_for(
        asyncio.to_thread(release_started.wait, 1.0), timeout=1.5
    )

    release_task.cancel()
    await asyncio.sleep(0)
    assert not release_task.done()

    finish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(release_task, timeout=1.0)
    assert store.released == [_audio_claim()]
