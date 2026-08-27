from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src import web_api
from src.config import Config
from src.local_polishing import (
    ArtifactSpec,
    LocalPolishing,
    ModelCatalog,
    PolishOutcome,
    VariantSpec,
)
from src.web_api import ScriberWebController, TranscriptRecord


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_post_processing_replay_injection_reuses_bound_target_and_markers(
    monkeypatch,
    tmp_path,
):
    class ReplayExecution:
        injection_target_guard = object()

        def __init__(self):
            self.markers = []

        def marker(self, name):
            self.markers.append(name)

        def fail(self, _code):
            raise AssertionError("the bound target did not change")

    captured = {}

    def inject_once(text, **kwargs):
        captured.update({"text": text, **kwargs})
        kwargs["on_injection_marker"]("clipboard_set", 10)
        kwargs["on_injection_marker"]("paste", 20)
        kwargs["on_injected"](text)
        return True

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr("src.injector.inject_text_once", inject_once)
    controller = ScriberWebController(asyncio.get_running_loop())
    record = TranscriptRecord(
        id="replay-post-processing",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Bereinigter Text",
    )
    replay = ReplayExecution()

    injected = await controller._inject_live_transcript_text(
        record.content_text(),
        record=record,
        session_id=record.id,
        provider="azure_mai",
        post_processed=True,
        provider_replay_execution=replay,  # type: ignore[arg-type]
    )

    assert injected is True
    assert captured["text"] == "Bereinigter Text "
    assert captured["target_guard"] is replay.injection_target_guard
    assert captured["injection_method"] == "paste"
    assert replay.markers == ["clipboard_set", "paste", "injection_callback_completed"]
    controller.shutdown()


def _catalog(payloads: dict[str, bytes]) -> ModelCatalog:
    variants = {}
    template_hash = _sha(json.dumps("fixture-template", separators=(",", ":")).encode())
    for variant in ("q8_0", "bf16"):
        model_path = f"variants/{variant}/model.gguf"
        policy_path = f"variants/{variant}/protection-policy.json"
        payloads[model_path] = f"gguf:{variant}".encode()
        payloads[policy_path] = b'{"schema_version":1,"markers":[]}'
        variants[variant] = VariantSpec(
            variant=variant,
            display_name=variant,
            description=f"fixture {variant}",
            artifacts=(
                ArtifactSpec(model_path, len(payloads[model_path]), _sha(payloads[model_path])),
                ArtifactSpec(policy_path, len(payloads[policy_path]), _sha(payloads[policy_path])),
            ),
            model_relative_path=model_path,
            protection_policy_relative_path=policy_path,
            chat_template_sha256=template_hash,
        )
    return ModelCatalog(
        schema_version=1,
        repository_id="fixture/public-model",
        revision="1" * 40,
        requires_token=False,
        variants=variants,
    )


class _AnonymousDownloader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []
        self.tokens: list[str | None] = []

    async def download(self, *, filename, destination, token, progress, **_kwargs):
        self.calls.append(filename)
        self.tokens.append(token)
        target = destination / Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payloads[filename])
        await progress(len(self.payloads[filename]), len(self.payloads[filename]))
        return target


class _CancellableDownloader(_AnonymousDownloader):
    def __init__(self, payloads: dict[str, bytes]) -> None:
        super().__init__(payloads)
        self.started = asyncio.Event()

    async def download(self, *, cancel_requested, **kwargs):
        self.started.set()
        while not cancel_requested():
            await asyncio.sleep(0)
        raise asyncio.CancelledError


class _Runtime:
    backend_name = "fixture-cpu"

    async def properties(self):
        return {"chat_template": "fixture-template"}

    async def apply_template(self, _messages):
        return "fixture prompt"

    async def complete(self, _prompt, *, max_new_tokens):
        assert max_new_tokens > 0
        return "[DOC]\n[P]Bereinigter Text.[/P]\n[/DOC]"

    async def close(self):
        return None


class _RuntimeFactory:
    async def create(self, *, model_path, model_sha256):
        assert model_path.suffix == ".gguf"
        assert len(model_sha256) == 64
        return _Runtime()


def _manager(tmp_path: Path, downloader: _AnonymousDownloader) -> LocalPolishing:
    payloads = downloader.payloads
    return LocalPolishing(
        root=tmp_path / "local-polishing",
        catalog=_catalog(payloads),
        downloader=downloader,
        runtime_factories=(_RuntimeFactory(),),
    )


async def _close(client: TestClient, controller: ScriberWebController) -> None:
    await client.close()
    await controller.drain_background_tasks_for_shutdown(timeout_seconds=0.5)
    controller.shutdown()


@pytest.mark.asyncio
async def test_local_polishing_routes_are_token_protected_and_do_not_expose_credentials(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    payloads: dict[str, bytes] = {}
    downloader = _AnonymousDownloader(payloads)
    manager = _manager(tmp_path, downloader)
    controller = ScriberWebController(asyncio.get_running_loop(), local_polisher=manager)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        responses = [
            await client.get("/api/local-polishing/models"),
            await client.post("/api/local-polishing/models/q8_0/install"),
            await client.delete("/api/local-polishing/model-operations/unknown"),
            await client.delete("/api/local-polishing/models/q8_0"),
        ]
        assert {response.status for response in responses} == {401}

        response = await client.get(
            "/api/local-polishing/models",
            headers={"X-Scriber-Token": "secret"},
        )
        public_payload = await response.json()
        assert response.status == 200
        assert public_payload["available"] is True
        assert {item["variant"] for item in public_payload["models"]} == {"q8_0", "bf16"}
        assert "credential" not in json.dumps(public_payload).lower()
        assert "token" not in json.dumps(public_payload).lower()
    finally:
        await _close(client, controller)


@pytest.mark.asyncio
async def test_install_is_idempotent_anonymous_and_broadcasts_authoritative_progress(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "secret")
    payloads: dict[str, bytes] = {}
    downloader = _AnonymousDownloader(payloads)
    manager = _manager(tmp_path, downloader)
    controller = ScriberWebController(asyncio.get_running_loop(), local_polisher=manager)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    headers = {"X-Scriber-Token": "secret"}
    websocket = await client.ws_connect("/ws?scriberToken=secret")
    try:
        assert (await websocket.receive_json())["type"] == "state"
        first = await client.post("/api/local-polishing/models/q8_0/install", headers=headers)
        second = await client.post("/api/local-polishing/models/q8_0/install", headers=headers)
        first_payload = await first.json()
        second_payload = await second.json()

        assert first.status == second.status == 202
        assert first_payload["operationId"] == second_payload["operationId"]
        operation = await manager.wait_for_operation(first_payload["operationId"])
        assert operation.status == "ready"

        progress_event = None
        for _ in range(10):
            candidate = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
            if candidate.get("type") == "local_polishing_model_progress" and candidate.get("status") == "ready":
                progress_event = candidate
                break
        assert progress_event is not None
        assert progress_event["variant"] == "q8_0"
        assert progress_event["progress"] == 100.0

        status_response = await client.get("/api/local-polishing/models", headers=headers)
        status_payload = await status_response.json()
        q8 = next(item for item in status_payload["models"] if item["variant"] == "q8_0")
        assert q8["status"] == "ready"
        assert q8["installed"] is True
        assert len(downloader.calls) == 2
        assert downloader.tokens == [None, None]
    finally:
        await websocket.close()
        await _close(client, controller)


@pytest.mark.asyncio
async def test_install_operation_can_be_cancelled_without_starting_a_duplicate(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    payloads: dict[str, bytes] = {}
    downloader = _CancellableDownloader(payloads)
    manager = _manager(tmp_path, downloader)
    controller = ScriberWebController(asyncio.get_running_loop(), local_polisher=manager)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        install = await client.post("/api/local-polishing/models/bf16/install")
        operation_id = (await install.json())["operationId"]
        await asyncio.wait_for(downloader.started.wait(), timeout=1.0)

        cancelled = await client.delete(f"/api/local-polishing/model-operations/{operation_id}")
        cancelled_payload = await cancelled.json()
        assert cancelled.status == 202
        assert cancelled_payload["operationId"] == operation_id
        final = await manager.wait_for_operation(operation_id)
        assert final.status == "cancelled"

        unknown = await client.delete("/api/local-polishing/model-operations/unknown")
        assert unknown.status == 404
    finally:
        await _close(client, controller)


@pytest.mark.asyncio
async def test_uninstalled_model_cannot_be_selected_and_selected_model_cannot_be_deleted(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_ENGINE", "cloud")
    monkeypatch.setenv("SCRIBER_LOCAL_POLISHING_VARIANT", "q8_0")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "q8_0", raising=False)
    payloads: dict[str, bytes] = {}
    downloader = _AnonymousDownloader(payloads)
    manager = _manager(tmp_path, downloader)
    controller = ScriberWebController(asyncio.get_running_loop(), local_polisher=manager)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        rejected = await client.put(
            "/api/settings",
            json={"postProcessingEngine": "local", "localPolishingVariant": "q8_0"},
        )
        assert rejected.status == 400
        assert Config.POST_PROCESSING_ENGINE == "cloud"

        install = await client.post("/api/local-polishing/models/q8_0/install")
        operation_id = (await install.json())["operationId"]
        assert (await manager.wait_for_operation(operation_id)).status == "ready"

        selected = await client.put(
            "/api/settings",
            json={"postProcessingEngine": "local", "localPolishingVariant": "q8_0"},
        )
        assert selected.status == 200
        assert Config.POST_PROCESSING_ENGINE == "local"

        active_delete = await client.delete("/api/local-polishing/models/q8_0")
        assert active_delete.status == 409
        assert (await active_delete.json())["code"] == "selected_model"

        cloud = await client.put("/api/settings", json={"postProcessingEngine": "cloud"})
        assert cloud.status == 200
        removed = await client.delete("/api/local-polishing/models/q8_0")
        assert removed.status == 200
        state = manager.state().to_dict()
        q8 = next(item for item in state["models"] if item["variant"] == "q8_0")
        assert q8["installed"] is False
    finally:
        await _close(client, controller)


@pytest.mark.asyncio
async def test_local_variant_switch_replaces_runtime_and_releases_previous_model(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_ENGINE", "cloud")
    monkeypatch.setenv("SCRIBER_LOCAL_POLISHING_VARIANT", "q8_0")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "q8_0", raising=False)
    payloads: dict[str, bytes] = {}
    downloader = _AnonymousDownloader(payloads)
    manager = _manager(tmp_path, downloader)
    controller = ScriberWebController(asyncio.get_running_loop(), local_polisher=manager)
    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        for variant in ("q8_0", "bf16"):
            install = await client.post(f"/api/local-polishing/models/{variant}/install")
            operation_id = (await install.json())["operationId"]
            assert (await manager.wait_for_operation(operation_id)).status == "ready"

        q8_selection = await client.put(
            "/api/settings",
            json={"postProcessingEngine": "local", "localPolishingVariant": "q8_0"},
        )
        assert q8_selection.status == 200
        for _ in range(50):
            if manager.state().current_variant == "q8_0":
                break
            await asyncio.sleep(0.01)
        assert manager.state().current_variant == "q8_0"

        bf16_selection = await client.put(
            "/api/settings",
            json={"localPolishingVariant": "bf16"},
        )
        assert bf16_selection.status == 200
        for _ in range(50):
            if manager.state().current_variant == "bf16":
                break
            await asyncio.sleep(0.01)
        assert manager.state().current_variant == "bf16"

        previous_model_delete = await client.delete("/api/local-polishing/models/q8_0")
        assert previous_model_delete.status == 200
        assert Config.LOCAL_POLISHING_VARIANT == "bf16"
    finally:
        await _close(client, controller)


@pytest.mark.asyncio
async def test_local_selection_waits_for_verified_runtime_before_saving(
    monkeypatch,
    tmp_path,
):
    class Snapshot:
        def to_dict(self):
            return {
                "available": True,
                "message": None,
                "currentVariant": None,
                "models": [
                    {
                        "variant": "q8_0",
                        "status": "ready",
                        "installed": True,
                        "progress": 100.0,
                    }
                ],
            }

    class BlockingPrewarmPolisher:
        def __init__(self):
            self.prewarm_started = asyncio.Event()
            self.release_prewarm = asyncio.Event()

        def state(self):
            return Snapshot()

        async def prewarm(self, variant):
            assert variant == "q8_0"
            self.prewarm_started.set()
            await self.release_prewarm.wait()
            return True

        async def unload(self):
            return None

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_ENGINE", "cloud")
    monkeypatch.setenv("SCRIBER_LOCAL_POLISHING_VARIANT", "q8_0")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "q8_0", raising=False)
    polisher = BlockingPrewarmPolisher()
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=polisher,  # type: ignore[arg-type]
    )
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]

    update_task = asyncio.create_task(
        controller.update_settings({"postProcessingEngine": "local", "localPolishingVariant": "q8_0"})
    )
    await asyncio.wait_for(polisher.prewarm_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert not update_task.done()
    assert Config.POST_PROCESSING_ENGINE == "cloud"
    assert not polisher.release_prewarm.is_set()
    polisher.release_prewarm.set()
    updated = await asyncio.wait_for(update_task, timeout=2.0)

    assert updated["postProcessingEngine"] == "local"
    controller.shutdown()


@pytest.mark.asyncio
async def test_local_selection_fails_closed_when_runtime_cannot_start(monkeypatch, tmp_path):
    class Snapshot:
        def to_dict(self):
            return {
                "available": True,
                "message": None,
                "currentVariant": None,
                "models": [
                    {
                        "variant": "q8_0",
                        "status": "ready",
                        "installed": True,
                        "runtimeReady": False,
                        "runtimeError": "runtime_unavailable",
                    }
                ],
            }

    class UnavailableRuntimePolisher:
        def state(self):
            return Snapshot()

        async def prewarm(self, variant):
            assert variant == "q8_0"
            return False

        async def unload(self):
            return None

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_ENGINE", "cloud")
    monkeypatch.setenv("SCRIBER_LOCAL_POLISHING_VARIANT", "q8_0")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "q8_0", raising=False)
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=UnavailableRuntimePolisher(),  # type: ignore[arg-type]
    )
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="verified local polishing runtime"):
        await controller.update_settings({"postProcessingEngine": "local", "localPolishingVariant": "q8_0"})

    assert Config.POST_PROCESSING_ENGINE == "cloud"
    controller.shutdown()


@pytest.mark.asyncio
async def test_rapid_variant_changes_cancel_stale_background_prewarms(monkeypatch, tmp_path):
    class Snapshot:
        def to_dict(self):
            return {
                "available": True,
                "message": None,
                "currentVariant": None,
                "models": [{"variant": variant, "status": "ready", "installed": True} for variant in ("q8_0", "bf16")],
            }

    class SwitchingPolisher:
        def __init__(self):
            self.started: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
            self.cancelled: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
            self.gates: list[asyncio.Event] = []

        def state(self):
            return Snapshot()

        async def prewarm(self, variant):
            index = len(self.gates)
            gate = asyncio.Event()
            self.gates.append(gate)
            await self.started.put((index, variant))
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await self.cancelled.put((index, variant))
                raise
            return True

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    polisher = SwitchingPolisher()
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=polisher,  # type: ignore[arg-type]
    )
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]

    controller._schedule_local_polishing_prewarm("q8_0")
    assert await asyncio.wait_for(polisher.started.get(), timeout=1.0) == (0, "q8_0")
    controller._schedule_local_polishing_prewarm("bf16")
    assert await asyncio.wait_for(polisher.cancelled.get(), timeout=1.0) == (0, "q8_0")
    assert await asyncio.wait_for(polisher.started.get(), timeout=1.0) == (1, "bf16")
    controller._schedule_local_polishing_prewarm("q8_0")
    assert await asyncio.wait_for(polisher.cancelled.get(), timeout=1.0) == (1, "bf16")
    assert await asyncio.wait_for(polisher.started.get(), timeout=1.0) == (2, "q8_0")

    polisher.gates[2].set()
    active = controller._local_polishing_prewarm_tasks["q8_0"]
    await asyncio.wait_for(active, timeout=1.0)
    assert all(task.done() for task in controller._local_polishing_prewarm_tasks.values())
    controller.shutdown()


@pytest.mark.asyncio
async def test_live_controller_passes_local_engine_and_keeps_raw_fallback(
    monkeypatch,
    tmp_path,
):
    class RejectingPolisher:
        async def polish(self, transcript, variant):
            assert transcript == "Unveränderter Rohtext"
            assert variant == "bf16"
            return PolishOutcome(
                text="Nicht freigegebene Ausgabe",
                variant="bf16",
                status="original_fallback",
                reason_codes=("protected_value_mismatch",),
                duration_ms=4.0,
                runtime_backend="fixture-cpu",
            )

    async def cloud_must_not_run(*_args, **_kwargs):
        raise AssertionError("the controller must not fall through to cloud post-processing")

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "local", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "bf16", raising=False)
    monkeypatch.setattr("src.post_processing.generate_live_mic_text", cloud_must_not_run)
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=RejectingPolisher(),  # type: ignore[arg-type]
    )
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]
    controller._inject_live_transcript_text = AsyncMock(return_value=True)  # type: ignore[method-assign]
    record = TranscriptRecord(
        id="local-fallback",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Unveränderter Rohtext",
    )

    await controller._post_process_and_inject_live_transcript(
        record,
        session_id="local-fallback",
        provider="soniox",
    )

    assert record.content_text() == "Unveränderter Rohtext"
    controller._inject_live_transcript_text.assert_awaited_once()
    assert controller._inject_live_transcript_text.await_args.kwargs["post_processed"] is False
    diagnostic = controller.get_post_processing_diagnostics()["latest"]
    assert diagnostic["engine"] == "local"
    assert diagnostic["model"] == "local:bf16"
    assert diagnostic["status"] == "original_fallback"
    assert diagnostic["fallbackToRaw"] is True
    assert diagnostic["reasonCodes"] == ["protected_value_mismatch"]
    assert all(
        call.args[0].get("type") != "post_processing_fallback_used" for call in controller.broadcast.await_args_list
    )
    controller.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_notification_accepted", [True, False])
async def test_live_controller_broadcasts_successful_cloud_fallback_once(
    monkeypatch,
    tmp_path,
    native_notification_accepted,
):
    async def successful_fallback(
        _raw_text,
        *,
        model,
        engine,
        local_polisher,
        local_variant,
        diagnostics,
        provider_http_transport,
    ):
        del local_polisher, local_variant, provider_http_transport
        assert model == "cerebras/gemma-4-31b"
        assert engine == "cloud"
        diagnostics.update(
            {
                "status": "completed",
                "engine": "cloud",
                "fallbackToRaw": False,
                "primaryModel": "cerebras/gemma-4-31b",
                "configuredFallbackModel": "minimax/minimax-m3:nitro",
                "usedModel": "minimax/minimax-m3",
                "fallbackModel": "minimax/minimax-m3",
                "fallbackUsed": True,
                "fallbackReason": "quota_exceeded",
                "primaryFailureStatus": 402,
            }
        )
        return "Bereinigter Text"

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "POST_PROCESSING_MODEL", "cerebras/gemma-4-31b", raising=False)
    monkeypatch.setattr("src.post_processing.post_process_live_transcript", successful_fallback)
    desktop_notice = AsyncMock(return_value=native_notification_accepted)
    monkeypatch.setattr("src.web_api.request_post_processing_fallback_notification", desktop_notice)
    controller = ScriberWebController(asyncio.get_running_loop())
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]
    controller._inject_live_transcript_text = AsyncMock(return_value=True)  # type: ignore[method-assign]
    record = TranscriptRecord(
        id="cloud-fallback",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Roher Text",
    )

    await controller._post_process_and_inject_live_transcript(
        record,
        session_id="cloud-fallback",
        provider="soniox",
    )

    assert record.content_text() == "Bereinigter Text"
    events = [call.args[0] for call in controller.broadcast.await_args_list]
    fallback_events = [event for event in events if event.get("type") == "post_processing_fallback_used"]
    assert fallback_events == [
        {
            "apiVersion": "1",
            "type": "post_processing_fallback_used",
            "eventId": "post-processing-fallback:cloud-fallback",
            "primaryModel": "cerebras/gemma-4-31b",
            "fallbackModel": "minimax/minimax-m3",
            "desktopNotificationAccepted": native_notification_accepted,
            "reason": "quota_exceeded",
            "reasonCategory": "quota_or_payment",
            "primaryFailureStatus": 402,
            "sessionId": "cloud-fallback",
        }
    ]
    diagnostic = controller.get_post_processing_diagnostics()["latest"]
    assert diagnostic["fallbackUsed"] is True
    assert diagnostic["usedModel"] == "minimax/minimax-m3"
    pending_fallback = controller.get_state()["pendingPostProcessingFallback"]
    if native_notification_accepted:
        assert pending_fallback is None
    else:
        assert pending_fallback == fallback_events[0]
        controller._pending_post_processing_fallback_event = (
            time.monotonic() - 1.0,
            pending_fallback,
        )
        assert controller.get_state()["pendingPostProcessingFallback"] is None
    desktop_notice.assert_awaited_once_with(
        "cerebras/gemma-4-31b",
        "minimax/minimax-m3",
        "post-processing-fallback:cloud-fallback",
    )
    controller.shutdown()


@pytest.mark.asyncio
async def test_live_controller_finishes_fallback_event_before_delivering_notification_cancellation(
    monkeypatch,
    tmp_path,
):
    async def successful_fallback(
        _raw_text,
        *,
        model,
        engine,
        local_polisher,
        local_variant,
        diagnostics,
        provider_http_transport,
    ):
        del model, engine, local_polisher, local_variant, provider_http_transport
        diagnostics.update(
            {
                "status": "completed",
                "engine": "cloud",
                "fallbackToRaw": False,
                "primaryModel": "cerebras/gemma-4-31b",
                "usedModel": "minimax/minimax-m3",
                "fallbackModel": "minimax/minimax-m3",
                "fallbackUsed": True,
                "fallbackReason": "quota_exceeded",
            }
        )
        return "Bereinigter Text"

    notification_started = asyncio.Event()
    release_notification = asyncio.Event()

    async def blocked_notification(_primary_model, _fallback_model, _event_id):
        notification_started.set()
        await release_notification.wait()
        return False

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "POST_PROCESSING_MODEL", "cerebras/gemma-4-31b", raising=False)
    monkeypatch.setattr("src.post_processing.post_process_live_transcript", successful_fallback)
    monkeypatch.setattr(
        "src.web_api.request_post_processing_fallback_notification",
        blocked_notification,
    )
    controller = ScriberWebController(asyncio.get_running_loop())
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]
    controller._inject_live_transcript_text = AsyncMock(return_value=True)  # type: ignore[method-assign]
    record = TranscriptRecord(
        id="cloud-fallback-cancel",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Roher Text",
    )

    task = asyncio.create_task(
        controller._post_process_and_inject_live_transcript(
            record,
            session_id=record.id,
            provider="soniox",
        )
    )
    await asyncio.wait_for(notification_started.wait(), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_notification.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    fallback_events = [
        call.args[0]
        for call in controller.broadcast.await_args_list
        if call.args[0].get("type") == "post_processing_fallback_used"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0]["eventId"] == "post-processing-fallback:cloud-fallback-cancel"
    assert fallback_events[0]["desktopNotificationAccepted"] is False
    controller.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_to_raw", "fallback_used", "processed_text"),
    [
        (True, True, "Roher Text"),
        (False, False, "Bereinigter Primärtext"),
    ],
)
async def test_live_controller_does_not_notify_without_accepted_fallback_output(
    monkeypatch,
    tmp_path,
    fallback_to_raw,
    fallback_used,
    processed_text,
):
    async def post_process(
        _raw_text,
        *,
        model,
        engine,
        local_polisher,
        local_variant,
        diagnostics,
        provider_http_transport,
    ):
        del model, engine, local_polisher, local_variant, provider_http_transport
        diagnostics.update(
            {
                "status": "original_fallback" if fallback_to_raw else "completed",
                "engine": "cloud",
                "fallbackToRaw": fallback_to_raw,
                "primaryModel": "cerebras/gemma-4-31b",
                "usedModel": "minimax/minimax-m3" if fallback_used else "cerebras/gemma-4-31b",
                "fallbackModel": "minimax/minimax-m3" if fallback_used else None,
                "fallbackUsed": fallback_used,
            }
        )
        return processed_text

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "POST_PROCESSING_MODEL", "cerebras/gemma-4-31b", raising=False)
    monkeypatch.setattr("src.post_processing.post_process_live_transcript", post_process)
    desktop_notice = AsyncMock(return_value=True)
    monkeypatch.setattr("src.web_api.request_post_processing_fallback_notification", desktop_notice)
    controller = ScriberWebController(asyncio.get_running_loop())
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]
    controller._inject_live_transcript_text = AsyncMock(return_value=True)  # type: ignore[method-assign]
    record = TranscriptRecord(
        id=f"no-notice-{fallback_to_raw}-{fallback_used}",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Roher Text",
    )

    await controller._post_process_and_inject_live_transcript(
        record,
        session_id=record.id,
        provider="soniox",
    )

    desktop_notice.assert_not_awaited()
    assert all(
        call.args[0].get("type") != "post_processing_fallback_used" for call in controller.broadcast.await_args_list
    )
    controller.shutdown()


@pytest.mark.asyncio
async def test_live_controller_cloud_deadline_injects_raw_before_failure_broadcast(
    monkeypatch,
    tmp_path,
):
    cancellation_observed = asyncio.Event()

    async def hanging_generation(
        _prompt,
        _model,
        *,
        max_output_tokens,
        openrouter_reasoning_effort,
        openrouter_max_semantic_attempts,
        routing_diagnostics,
        provider_http_transport,
    ):
        assert max_output_tokens >= 768
        assert openrouter_reasoning_effort == "low"
        assert openrouter_max_semantic_attempts == 1
        assert isinstance(routing_diagnostics, dict)
        assert provider_http_transport is controller._provider_http_transport
        try:
            await asyncio.Event().wait()
        finally:
            cancellation_observed.set()

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setenv("SCRIBER_LIVE_POST_PROCESSING_TIMEOUT_SEC", "0.05")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(Config, "POST_PROCESSING_MODEL", "cerebras/gemma-4-31b", raising=False)
    monkeypatch.setattr("src.post_processing.generate_live_mic_text", hanging_generation)
    controller = ScriberWebController(asyncio.get_running_loop())
    actions: list[tuple[str, str]] = []

    async def recording_broadcast(event):
        actions.append(("broadcast", str(event)))

    async def recording_injection(text, **_kwargs):
        actions.append(("inject", text))
        return True

    controller.broadcast = recording_broadcast  # type: ignore[method-assign]
    controller._inject_live_transcript_text = recording_injection  # type: ignore[method-assign]
    record = TranscriptRecord(
        id="cloud-deadline",
        title="Live Mic",
        date="Today",
        duration="0:01",
        status="completed",
        type="mic",
        language="de",
        content="Unveränderter Rohtext",
    )

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(
        controller._post_process_and_inject_live_transcript(
            record,
            session_id="cloud-deadline",
            provider="soniox",
        ),
        timeout=0.5,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5
    assert cancellation_observed.is_set()
    assert record.content_text() == "Unveränderter Rohtext"
    assert [kind for kind, _value in actions] == ["broadcast", "inject", "broadcast"]
    assert actions[1] == ("inject", "Unveränderter Rohtext")
    diagnostic = controller.get_post_processing_diagnostics()["latest"]
    assert diagnostic["status"] == "failure"
    assert diagnostic["fallbackToRaw"] is True
    assert diagnostic["deadlineMs"] == 50.0
    assert diagnostic["fallbackPolicy"] == "bounded_cross_provider"
    assert diagnostic["reasonCodes"] == ["deadline_exceeded"]
    assert "PRIVATE_TRANSCRIPT_SENTINEL" not in controller._post_processing_error_summary(
        RuntimeError("PRIVATE_TRANSCRIPT_SENTINEL")
    )
    controller.shutdown()


@pytest.mark.asyncio
async def test_post_processing_start_schedules_local_prewarm_without_blocking_provider_rejection(
    monkeypatch,
    tmp_path,
):
    class Snapshot:
        def to_dict(self):
            return {
                "available": True,
                "message": None,
                "currentVariant": "q8_0",
                "models": [
                    {
                        "variant": "q8_0",
                        "status": "ready",
                        "installed": True,
                        "progress": 100.0,
                    }
                ],
            }

    class RecordingPolisher:
        def __init__(self):
            self.prewarm_calls: list[str] = []

        def state(self):
            return Snapshot()

        async def prewarm(self, variant):
            self.prewarm_calls.append(variant)
            return True

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "POST_PROCESSING_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "POST_PROCESSING_ENGINE", "local", raising=False)
    monkeypatch.setattr(Config, "LOCAL_POLISHING_VARIANT", "q8_0", raising=False)
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "soniox", raising=False)
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "", raising=False)
    polisher = RecordingPolisher()
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=polisher,  # type: ignore[arg-type]
    )
    controller.broadcast = AsyncMock()  # type: ignore[method-assign]

    rejection = await controller.start_listening(post_process=True)
    await asyncio.sleep(0)

    assert rejection is not None
    assert rejection.code == "missing_api_key"
    assert polisher.prewarm_calls == ["q8_0"]
    controller.shutdown()


@pytest.mark.asyncio
async def test_shutdown_bounds_local_runtime_cleanup(monkeypatch, tmp_path):
    class SlowClosePolisher:
        def __init__(self):
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def close(self):
            self.close_started.set()
            await self.release_close.wait()

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    polisher = SlowClosePolisher()
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        local_polisher=polisher,  # type: ignore[arg-type]
    )

    started = asyncio.get_running_loop().time()
    pending = await controller.drain_background_tasks_for_shutdown(timeout_seconds=0.01)
    elapsed = asyncio.get_running_loop().time() - started

    assert polisher.close_started.is_set()
    assert pending >= 1
    assert elapsed < 0.5
    polisher.release_close.set()
    await asyncio.wait_for(controller._local_polishing_close_task, timeout=1.0)
    controller.shutdown()
