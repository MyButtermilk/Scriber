from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import src.local_polishing.runtime as local_runtime
from src.local_polishing import (
    DEFAULT_CATALOG,
    ArtifactSpec,
    CatalogError,
    HuggingFaceArtifactDownloader,
    LlamaServerLaunchSpec,
    LlamaServerRuntimeFactory,
    LocalPolishing,
    LocalPolishingError,
    ModelCatalog,
    RuntimeBinary,
    VariantSpec,
    packaged_runtime_factories,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _catalog(
    payloads: dict[str, bytes],
    *,
    wrong_q8_hash: bool = False,
    requires_token: bool = True,
) -> ModelCatalog:
    variants = {}
    template_hash = _sha(json.dumps("fixture-template", separators=(",", ":")).encode())
    for variant in ("q8_0", "bf16"):
        model_path = f"variants/{variant}/model.gguf"
        policy_path = f"variants/{variant}/protection-policy.json"
        payloads.setdefault(model_path, f"gguf:{variant}".encode())
        payloads.setdefault(
            policy_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_id": "gemma_lexical_v1",
                    "max_spans": 32,
                    "markers": [{"marker_index": index, "marker": f"⟦KEEP_{chr(65 + index)}⟧"} for index in range(26)]
                    + [{"marker_index": 26 + index, "marker": f"⟦KEEP_A{chr(65 + index)}⟧"} for index in range(6)],
                },
                separators=(",", ":"),
            ).encode(),
        )
        model_sha = _sha(payloads[model_path])
        if variant == "q8_0" and wrong_q8_hash:
            model_sha = "0" * 64
        variants[variant] = VariantSpec(
            variant=variant,
            display_name=variant,
            description=f"fixture {variant}",
            artifacts=(
                ArtifactSpec(model_path, len(payloads[model_path]), model_sha),
                ArtifactSpec(policy_path, len(payloads[policy_path]), _sha(payloads[policy_path])),
            ),
            model_relative_path=model_path,
            protection_policy_relative_path=policy_path,
            chat_template_sha256=template_hash,
        )
    return ModelCatalog(
        schema_version=1,
        repository_id="fixture/private-model",
        revision="1" * 40,
        requires_token=requires_token,
        variants=variants,
    )


class FakeDownloader:
    def __init__(self, payloads: dict[str, bytes], *, expected_token: str | None = "read-token") -> None:
        self.payloads = payloads
        self.expected_token = expected_token
        self.calls: list[tuple[str, str]] = []

    async def download(self, *, filename, destination, token, progress, cancel_requested, **_kwargs):
        assert token == self.expected_token
        self.calls.append((filename, str(destination)))
        target = destination / Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payloads[filename])
        await progress(len(self.payloads[filename]), len(self.payloads[filename]))
        return target


class BlockingDownloader(FakeDownloader):
    def __init__(self, payloads: dict[str, bytes]) -> None:
        super().__init__(payloads)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().download(**kwargs)


class CancellableDownloader(FakeDownloader):
    def __init__(self, payloads: dict[str, bytes]) -> None:
        super().__init__(payloads)
        self.started = asyncio.Event()
        self.first = True
        self.saw_resume_metadata = False

    async def download(self, *, filename, destination, progress, cancel_requested, **kwargs):
        if self.first:
            self.first = False
            target = destination / Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payloads[filename][:2])
            metadata = destination / ".cache" / "resume.marker"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text("partial", encoding="utf-8")
            await progress(2, len(self.payloads[filename]))
            self.started.set()
            while not cancel_requested():
                await asyncio.sleep(0)
            raise asyncio.CancelledError
        self.saw_resume_metadata = (destination / ".cache" / "resume.marker").is_file()
        return await super().download(
            filename=filename,
            destination=destination,
            progress=progress,
            cancel_requested=cancel_requested,
            **kwargs,
        )


class FakeRuntime:
    backend_name = "fixture-cpu"

    def __init__(self, *, output: str = "[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]") -> None:
        self.output = output
        self.closed = False

    async def properties(self):
        return {"chat_template": "fixture-template"}

    async def apply_template(self, messages):
        assert messages[0]["role"] == "user"
        assert "Bereinige dieses Transkript konservativ" in messages[0]["content"]
        return "rendered prompt"

    async def complete(self, prompt, *, max_new_tokens):
        assert prompt == "rendered prompt"
        assert max_new_tokens > 0
        return self.output

    async def close(self):
        self.closed = True


class FakeRuntimeFactory:
    def __init__(self, runtime=None, *, fail=False, name="fixture") -> None:
        self.runtime = runtime or FakeRuntime()
        self.fail = fail
        self.name = name
        self.calls = 0

    async def create(self, *, model_path, model_sha256):
        self.calls += 1
        assert model_path.name == "model.gguf"
        assert len(model_sha256) == 64
        if self.fail:
            raise RuntimeError("fixture backend unavailable")
        return self.runtime


@pytest.mark.asyncio
async def test_hugging_face_stream_cancel_is_immediate_and_resumable(tmp_path: Path) -> None:
    payload = b"0123456789abcdef"
    first_chunk_written = asyncio.Event()
    release_first_request = asyncio.Event()
    requested_ranges: list[str | None] = []
    request_count = 0

    async def artifact(request: web.Request) -> web.StreamResponse:
        nonlocal request_count
        request_count += 1
        requested_range = request.headers.get("Range")
        requested_ranges.append(requested_range)
        offset = int(requested_range.removeprefix("bytes=").removesuffix("-")) if requested_range else 0
        response = web.StreamResponse(
            status=206 if requested_range else 200,
            headers={
                "Content-Length": str(len(payload) - offset),
                **({"Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"} if requested_range else {}),
            },
        )
        await response.prepare(request)
        try:
            await response.write(payload[offset : offset + 4])
            if request_count == 1:
                first_chunk_written.set()
                await release_first_request.wait()
            await response.write(payload[offset + 4 :])
        except ConnectionResetError:
            pass
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", artifact)
    server = TestServer(app)
    await server.start_server()
    destination = tmp_path / "download"
    downloader = HuggingFaceArtifactDownloader(
        endpoint=str(server.make_url("/")).rstrip("/"),
        chunk_size=4,
    )
    progress_values: list[int] = []

    async def progress(received: int, _total: int) -> None:
        progress_values.append(received)

    try:
        first = asyncio.create_task(
            downloader.download(
                repository_id="owner/repo",
                revision="1" * 40,
                filename="gguf/model.gguf",
                destination=destination,
                token=False,
                expected_size=len(payload),
                progress=progress,
                cancel_requested=lambda: False,
            )
        )
        await asyncio.wait_for(first_chunk_written.wait(), timeout=1.0)
        while not progress_values:
            await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=1.0)
        release_first_request.set()

        partial = destination / "gguf" / ".model.gguf.incomplete"
        assert partial.is_file()
        assert 0 < partial.stat().st_size < len(payload)

        completed = await downloader.download(
            repository_id="owner/repo",
            revision="1" * 40,
            filename="gguf/model.gguf",
            destination=destination,
            token=False,
            expected_size=len(payload),
            progress=progress,
            cancel_requested=lambda: False,
        )

        assert completed.read_bytes() == payload
        assert requested_ranges[0] is None
        assert requested_ranges[1] == f"bytes={4}-"
        assert not partial.exists()
    finally:
        release_first_request.set()
        await server.close()


@pytest.mark.asyncio
async def test_llama_runtime_factory_closes_partial_runtime_when_start_is_cancelled(
    monkeypatch,
    tmp_path,
):
    class BlockingRuntime:
        instance = None

        def __init__(self, **_kwargs):
            type(self).instance = self
            self.started = asyncio.Event()
            self.closed = False

        async def start(self):
            self.started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    monkeypatch.setattr(local_runtime, "LlamaServerRuntime", BlockingRuntime)
    binary = RuntimeBinary(
        "cpu",
        tmp_path / "llama-server.exe",
        "0" * 64,
        "none",
        "0",
    )
    factory = LlamaServerRuntimeFactory(binary)
    task = asyncio.create_task(factory.create(model_path=tmp_path / "model.gguf", model_sha256="1" * 64))
    while BlockingRuntime.instance is None:
        await asyncio.sleep(0)
    await BlockingRuntime.instance.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert BlockingRuntime.instance.closed is True


def test_catalog_rejects_artifact_path_traversal() -> None:
    with pytest.raises(CatalogError, match="safe relative POSIX path"):
        ArtifactSpec(
            relative_path="../model.gguf",
            byte_size=5,
            sha256=_sha(b"model"),
        )


def test_shipping_catalog_is_public_anonymous_and_materialized(tmp_path: Path) -> None:
    manager = LocalPolishing(root=tmp_path, catalog=DEFAULT_CATALOG, runtime_factories=())

    snapshot = manager.state().to_dict()

    assert DEFAULT_CATALOG.requires_token is False
    assert DEFAULT_CATALOG.revision == "65bae06a655fc209058d60064c65fe3e891e4a43"
    assert snapshot["available"] is True
    assert snapshot["message"] is None
    assert {model["status"] for model in snapshot["models"]} == {"not_installed"}
    for descriptor in DEFAULT_CATALOG.variants.values():
        paths = {artifact.relative_path for artifact in descriptor.artifacts}
        assert {"GEMMA_LICENSE.md", "MODIFICATIONS.md", "NOTICE"} <= paths
        assert descriptor.chat_template_sha256 == ("66317715a6a57b5e16bc1e5a4d17e296fa18cadfea46bfbdf07538dc48f72e65")


@pytest.mark.asyncio
async def test_public_catalog_download_does_not_read_a_credential(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, requires_token=False)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads, expected_token=None),
        token_provider=lambda: pytest.fail("anonymous download requested a credential"),
        runtime_factories=(),
    )

    operation_id = await manager.install("q8_0")

    assert (await manager.wait_for_operation(operation_id)).status == "ready"
    await manager.close()


@pytest.mark.asyncio
async def test_install_rejects_hash_mismatch_without_activating_model(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, wrong_q8_hash=True)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
    )

    operation_id = await manager.install("q8_0")
    operation = await manager.wait_for_operation(operation_id)

    assert operation.status == "error"
    assert operation.error_code == "artifact_hash_mismatch"
    snapshot = manager.state().to_dict()
    q8 = next(item for item in snapshot["models"] if item["variant"] == "q8_0")
    assert q8["installed"] is False
    assert not (tmp_path / "active-q8_0.json").exists()
    await manager.close()


@pytest.mark.asyncio
async def test_duplicate_install_requests_share_one_operation(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    downloader = BlockingDownloader(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=downloader,
        token_provider=lambda: "read-token",
    )

    first = await manager.install("q8_0")
    await downloader.started.wait()
    second = await manager.install("q8_0")
    downloader.release.set()
    result = await manager.wait_for_operation(first)

    assert first == second
    assert result.status == "ready"
    assert len(downloader.calls) == 2
    repeated = await manager.install("q8_0")
    assert repeated == first
    assert len(downloader.calls) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_successful_install_activates_only_after_all_artifacts_verify(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    downloader = FakeDownloader(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=downloader,
        token_provider=lambda: "read-token",
    )

    operation_id = await manager.install("q8_0")
    operation = await manager.wait_for_operation(operation_id)

    assert operation.status == "ready"
    pointer = json.loads((tmp_path / "active-q8_0.json").read_text(encoding="utf-8"))
    installation = (tmp_path / pointer["installation"]).resolve()
    assert installation.is_dir()
    assert (installation / "variants/q8_0/model.gguf").read_bytes() == payloads["variants/q8_0/model.gguf"]
    snapshot = manager.state().to_dict()
    q8 = next(item for item in snapshot["models"] if item["variant"] == "q8_0")
    assert q8["installed"] is True
    assert q8["progress"] == 100.0
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_install_preserves_staging_metadata_and_resumes(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    downloader = CancellableDownloader(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=downloader,
        token_provider=lambda: "read-token",
    )

    cancelled_id = await manager.install("q8_0")
    await downloader.started.wait()
    await manager.cancel(cancelled_id)
    cancelled = await manager.wait_for_operation(cancelled_id)
    resumed_id = await manager.install("q8_0")
    resumed = await manager.wait_for_operation(resumed_id)

    assert cancelled.status == "cancelled"
    assert resumed_id != cancelled_id
    assert resumed.status == "ready"
    assert downloader.saw_resume_metadata is True
    await manager.close()


def test_llama_server_launch_is_loopback_authenticated_and_offline(tmp_path: Path) -> None:
    binary = RuntimeBinary(
        name="vulkan",
        executable=tmp_path / "llama-server.exe",
        sha256="2" * 64,
        device="Vulkan0",
        gpu_layers="all",
    )
    command = LlamaServerLaunchSpec().command(
        binary=binary,
        model_path=tmp_path / "model.gguf",
        port=43123,
        api_key="secret-key",
    )

    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--api-key") + 1] == "secret-key"
    assert command[command.index("--device") + 1] == "Vulkan0"
    assert command[command.index("--n-gpu-layers") + 1] == "all"
    for required in ("--offline", "--no-webui", "--log-disable", "--jinja", "--sleep-idle-seconds"):
        assert required in command


def test_packaged_runtime_manifest_produces_vulkan_then_cpu_factories(tmp_path: Path) -> None:
    runtime_root = tmp_path / "local-polishing"
    runtime_root.mkdir()
    server = runtime_root / "llama-server.exe"
    server.write_bytes(b"server")
    library = runtime_root / "ggml.dll"
    library.write_bytes(b"library")
    files = [
        {"path": path.name, "bytes": path.stat().st_size, "sha256": f"sha256:{_sha(path.read_bytes())}"}
        for path in (server, library)
    ]
    (runtime_root / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "contract": "ScriberLocalPolishingRuntimeManifestV1",
                "schemaVersion": 1,
                "runtime": "llama.cpp",
                "platform": {"primaryBackend": "vulkan", "cpuFallback": True},
                "files": files,
            }
        ),
        encoding="utf-8",
    )

    factories = packaged_runtime_factories(runtime_root)

    assert [factory.name for factory in factories] == ["vulkan", "cpu"]
    assert factories[0].binary.device == "Vulkan0"
    assert factories[1].binary.device == "none"


@pytest.mark.asyncio
async def test_runtime_falls_back_to_second_backend_and_active_model_cannot_be_removed(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    failing = FakeRuntimeFactory(fail=True, name="vulkan")
    working_runtime = FakeRuntime()
    working = FakeRuntimeFactory(working_runtime, name="cpu")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(failing, working),
    )
    operation_id = await manager.install("q8_0")
    assert (await manager.wait_for_operation(operation_id)).status == "ready"
    installed_state = manager.state().to_dict()
    installed_q8 = next(item for item in installed_state["models"] if item["variant"] == "q8_0")
    assert installed_q8["runtimeReady"] is False
    assert installed_q8["runtimeError"] is None

    outcome = await manager.polish("Hallo welt.", "q8_0")

    assert outcome.text == "Hallo Welt."
    assert outcome.status == "accepted"
    assert outcome.runtime_backend == "fixture-cpu"
    assert failing.calls == 1
    assert working.calls == 1
    active_state = manager.state().to_dict()
    active_q8 = next(item for item in active_state["models"] if item["variant"] == "q8_0")
    assert active_q8["runtimeReady"] is True
    assert active_q8["runtimeError"] is None
    with pytest.raises(LocalPolishingError, match="currently loaded"):
        await manager.remove("q8_0")
    await manager.unload("q8_0")
    assert working_runtime.closed is True
    await manager.remove("q8_0")
    assert manager.state().to_dict()["models"][0]["installed"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_changed_protected_value_returns_original_text(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    runtime = FakeRuntime(output="[DOC]\n[P]Die Summe ist 6 Euro.[/P]\n[/DOC]")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Die Summe ist 5 Euro.", "q8_0")

    assert outcome.text == "Die Summe ist 5 Euro."
    assert outcome.status == "original_fallback"
    assert "damaged_placeholder" in outcome.reason_codes
    await manager.close()


@pytest.mark.asyncio
async def test_intact_protected_value_is_restored_before_return(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    runtime = FakeRuntime(output="[DOC]\n[P]Die Summe ist ⟦KEEP_A⟧ Euro.[/P]\n[/DOC]")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Die Summe ist 5 Euro.", "q8_0")

    assert outcome.text == "Die Summe ist 5 Euro."
    assert outcome.status == "accepted"
    await manager.close()
