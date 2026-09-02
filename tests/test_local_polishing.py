from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import src.local_polishing.runtime as local_runtime
from src.local_polishing import (
    DEFAULT_CATALOG,
    ArtifactSpec,
    CatalogError,
    CompletionResult,
    HuggingFaceArtifactDownloader,
    LlamaServerLaunchSpec,
    LlamaServerRuntimeFactory,
    LocalPolishing,
    LocalPolishingError,
    ModelCatalog,
    RuntimeBinary,
    VariantSpec,
    catalog_identity,
    packaged_runtime_factories,
)
from src.local_polishing.safety import (
    SafetyError,
    load_policy_markers,
    repair_unambiguous_numeric_anchors,
    validate_plain_text_content,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _catalog(
    payloads: dict[str, bytes],
    *,
    wrong_q8_hash: bool = False,
    requires_token: bool = True,
    plain_completion: bool = False,
    generation_max_new_tokens: int | None = 384,
    repository_id: str = "fixture/private-model",
    revision: str = "1" * 40,
) -> ModelCatalog:
    variants = {}
    template_hash = _sha(json.dumps("fixture-template", separators=(",", ":")).encode())
    prompt_template = "Student-Regel\n\nTranskript:\n${transcript}\n\nBereinigte Fassung:\n"
    prompt_hash = _sha(prompt_template.encode("utf-8"))
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
            chat_template_sha256=None if plain_completion else template_hash,
            prompt_contract="plain_completion_v1" if plain_completion else "chat_template_v1",
            output_contract="plain_text_v1" if plain_completion else "sst_v1",
            prompt_template=prompt_template if plain_completion else None,
            prompt_template_sha256=prompt_hash if plain_completion else None,
            generation_max_new_tokens=generation_max_new_tokens if plain_completion else None,
        )
    return ModelCatalog(
        schema_version=3 if plain_completion else 1,
        repository_id=repository_id,
        revision=revision,
        requires_token=requires_token,
        variants=variants,
    )


def _qad_plain_catalog(payloads: dict[str, bytes]) -> ModelCatalog:
    fixture = _catalog(payloads, plain_completion=True)
    descriptor = replace(
        fixture.variants["q8_0"],
        variant="qad_q4_0",
        display_name="LFM2.5 350M · QAD Q4_0",
    )
    return ModelCatalog(
        schema_version=3,
        repository_id=fixture.repository_id,
        revision=fixture.revision,
        requires_token=fixture.requires_token,
        variants={"qad_q4_0": descriptor},
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

    def __init__(
        self,
        *,
        output: str = "[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]",
        finish_reason: str = "eos",
        prompt_truncated: bool = False,
        exhaust_budget: bool = False,
    ) -> None:
        self.output = output
        self.finish_reason = finish_reason
        self.prompt_truncated = prompt_truncated
        self.exhaust_budget = exhaust_budget
        self.closed = False
        self.applied_templates = 0
        self.prompts: list[str] = []
        self.max_new_token_requests: list[int] = []

    async def properties(self):
        return {"chat_template": "fixture-template"}

    async def apply_template(self, messages):
        self.applied_templates += 1
        assert messages[0]["role"] == "user"
        assert "Bereinige dieses Transkript konservativ" in messages[0]["content"]
        return "rendered prompt"

    async def complete(self, prompt, *, max_new_tokens):
        self.prompts.append(prompt)
        self.max_new_token_requests.append(max_new_tokens)
        assert max_new_tokens > 0
        return CompletionResult(
            content=self.output,
            finish_reason=self.finish_reason,
            tokens_predicted=max_new_tokens if self.exhaust_budget else min(12, max_new_tokens),
            prompt_truncated=self.prompt_truncated,
        )

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


def test_shipping_catalog_exposes_only_the_public_qad_winner(tmp_path: Path) -> None:
    manager = LocalPolishing(root=tmp_path, catalog=DEFAULT_CATALOG, runtime_factories=())

    snapshot = manager.state().to_dict()

    assert DEFAULT_CATALOG.requires_token is False
    assert DEFAULT_CATALOG.repository_id == "Buttermilk03/scriber-lfm2.5-350m-polishing-de-qad-v1"
    assert DEFAULT_CATALOG.revision == "d64f8a14a09b2916000d969edd18bc411745e53a"
    assert DEFAULT_CATALOG.materialized is True
    assert snapshot["available"] is True
    assert snapshot["message"] is None
    assert [model["variant"] for model in snapshot["models"]] == ["qad_q4_0"]
    assert {model["status"] for model in snapshot["models"]} == {"not_installed"}
    assert snapshot["models"][0]["name"] == "LFM2.5 350M · QAD Q4_0"
    assert snapshot["models"][0]["sizeBytes"] == 218_347_894
    descriptor = DEFAULT_CATALOG.variants["qad_q4_0"]
    assert descriptor.description == (
        "Fast local German dictation cleanup, trained with Praxist and quantized only with QAD Q4_0."
    )
    assert {(artifact.relative_path, artifact.byte_size, artifact.sha256) for artifact in descriptor.artifacts} == {
        (
            "gguf/qad_q4_0/Scriber-LFM2.5-350M-Production-QAD-Q4_0.gguf",
            218_328_640,
            "e1ca3391d896db64df91c5ed5a02e16f5b6bbec5de81667ec99535eb7b1c0486",
        ),
        (
            "gguf/qad_q4_0/scriber-protection-policy.json",
            2_408,
            "03c3f0fa422d10e8585e3367b8cdd73226ef9b246b9f438dfafb0563dffa823e",
        ),
        (
            "gguf/qad_q4_0/variant-artifact-manifest.json",
            3_912,
            "771f8945f6cef07095100453b90551635205acb4b815b5655aaf2b00e78cf4ef",
        ),
        (
            "LICENSE",
            10_574,
            "4d28ca14dedc0b3d0fcc2b3339f0e79931faa33874f3d24f522183a8fc70068c",
        ),
        (
            "MODIFICATIONS.md",
            2_360,
            "575f42fca15ea0782b81061b03968f179801a767d8a394ce46afddf5379b5a58",
        ),
    }
    assert descriptor.chat_template_sha256 is None
    assert descriptor.prompt_contract == "plain_completion_v1"
    assert descriptor.output_contract == "plain_text_v1"
    assert descriptor.generation_max_new_tokens == 384
    assert descriptor.prompt_template_sha256 == ("e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8")
    rendered = descriptor.render_plain_completion_prompt("Hallo welt.")
    assert "${transcript}" not in rendered
    assert "Transkript:\nHallo welt.\n\nBereinigte Fassung:\n\n" in rendered


def test_plain_completion_catalog_binds_and_renders_one_deterministic_prompt() -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    descriptor = catalog.variants["q8_0"]

    prompt = descriptor.render_plain_completion_prompt("Hallo welt.")

    assert prompt == "Student-Regel\n\nTranskript:\nHallo welt.\n\nBereinigte Fassung:\n"
    assert descriptor.prompt_template_sha256 == _sha(descriptor.prompt_template.encode("utf-8"))
    assert descriptor.chat_template_sha256 is None
    assert descriptor.generation_max_new_tokens == 384
    assert catalog.schema_version == 3


def test_schema_three_requires_a_bounded_plain_completion_generation_cap() -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    variants = {
        variant: replace(descriptor, generation_max_new_tokens=None) for variant, descriptor in catalog.variants.items()
    }

    with pytest.raises(CatalogError, match="requires a bounded generation cap"):
        ModelCatalog(
            schema_version=3,
            repository_id=catalog.repository_id,
            revision=catalog.revision,
            requires_token=catalog.requires_token,
            variants=variants,
        )


def test_lfm_qad_policy_is_exactly_bound_to_raw_plain_completion(tmp_path: Path) -> None:
    labels = [chr(65 + index) for index in range(26)] + [f"A{chr(65 + index)}" for index in range(6)]
    policy = {
        "schema_version": 2,
        "policy_id": "lfm2_qad_lexical_v1",
        "model_binding": {
            "base_repository_id": "LiquidAI/LFM2.5-350M-Base",
            "base_revision": "9960764e30892e01f29a6dc23df2533fcd8bd5ae",
            "tokenizer_json_sha256": "4905ab82b2cfc25e0c88adc8f4eeffe759c57c5626312b30b0aaeaf8ad3379bc",
            "vocabulary_size": 65_536,
            "prompt_contract": "plain_completion_v1",
            "catalog_prompt_sha256": "e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8",
            "training_prompt_sha256": "372f879803334a68e310fe2e658c11678600baf0f4ef72834e4acd409f747dd6",
            "output_contract": "plain_text_v1",
            "generation_max_new_tokens": 384,
            "runtime_input": "raw_transcript_no_keep_markers",
        },
        "max_spans": 32,
        "markers": [{"marker_index": index, "marker": f"⟦KEEP_{label}⟧"} for index, label in enumerate(labels)],
    }
    policy_path = tmp_path / "scriber-protection-policy.json"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

    assert load_policy_markers(policy_path) == tuple(f"⟦KEEP_{label}⟧" for label in labels)

    policy["model_binding"]["runtime_input"] = "protected_markers"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SafetyError, match="invalid_protection_policy"):
        load_policy_markers(policy_path)


def test_schema_two_cannot_carry_an_identity_unbound_generation_cap() -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)

    with pytest.raises(CatalogError, match="require catalog schema 3"):
        ModelCatalog(
            schema_version=2,
            repository_id=catalog.repository_id,
            revision=catalog.revision,
            requires_token=catalog.requires_token,
            variants=catalog.variants,
        )


@pytest.mark.parametrize("generation_max_new_tokens", (True, 0, 4097, 384.0))
def test_plain_completion_rejects_an_invalid_generation_cap(
    generation_max_new_tokens: object,
) -> None:
    payloads: dict[str, bytes] = {}

    with pytest.raises(CatalogError, match="integer from 1 through 4096"):
        _catalog(
            payloads,
            plain_completion=True,
            generation_max_new_tokens=generation_max_new_tokens,  # type: ignore[arg-type]
        )


def test_schema_three_identity_binds_the_exact_generation_cap() -> None:
    payloads: dict[str, bytes] = {}
    cap_384 = _catalog(payloads, plain_completion=True, generation_max_new_tokens=384)
    cap_383 = _catalog(payloads, plain_completion=True, generation_max_new_tokens=383)

    assert catalog_identity(cap_384, "q8_0") != catalog_identity(cap_383, "q8_0")


def test_catalog_schema_one_rejects_new_prompt_contract() -> None:
    payloads: dict[str, bytes] = {}
    plain = _catalog(payloads, plain_completion=True)

    with pytest.raises(CatalogError, match="schema 1"):
        ModelCatalog(
            schema_version=1,
            repository_id=plain.repository_id,
            revision=plain.revision,
            requires_token=plain.requires_token,
            variants=plain.variants,
        )


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


def test_loopback_port_remains_reserved_until_the_spawn_boundary() -> None:
    reservation = local_runtime._reserve_loopback_port()
    host, port = reservation.getsockname()
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            competitor.bind((host, port))
    finally:
        competitor.close()
        reservation.close()

    successor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        successor.bind((host, port))
    finally:
        successor.close()


@pytest.mark.asyncio
async def test_vulkan_selector_isolates_the_only_nvidia_device_in_a_hybrid_host(tmp_path: Path) -> None:
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"server")
    binary = RuntimeBinary("vulkan", server, _sha(server.read_bytes()), "Vulkan0", "all")
    calls: list[dict[str, str]] = []

    async def probe(_binary, overrides):
        calls.append(dict(overrides))
        if not overrides:
            return local_runtime.VulkanDeviceProbeResult(
                stdout=(
                    "Available devices:\r\n"
                    "  Vulkan0: AMD Radeon(TM) 890M Graphics (16444 MiB, 15622 MiB free)\r\n"
                    "  Vulkan1: NVIDIA GeForce RTX 4070 Laptop GPU (7948 MiB, 7180 MiB free)\r\n"
                ),
                stderr="",
                returncode=0,
            )
        return local_runtime.VulkanDeviceProbeResult(
            stdout=(
                "Available devices:\r\n  Vulkan0: NVIDIA GeForce RTX 4070 Laptop GPU (7948 MiB, 7100 MiB free)\r\n"
            ),
            stderr="",
            returncode=0,
        )

    selected = await local_runtime.select_preferred_vulkan_device(binary, probe=probe)

    assert selected == 1
    assert calls == [{}, {"GGML_VK_VISIBLE_DEVICES": "1"}]


@pytest.mark.asyncio
async def test_vulkan_selector_reverifies_a_single_nvidia_device_as_logical_vulkan_zero(tmp_path: Path) -> None:
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"server")
    binary = RuntimeBinary("vulkan", server, _sha(server.read_bytes()), "Vulkan0", "all")
    calls: list[dict[str, str]] = []
    result = local_runtime.VulkanDeviceProbeResult(
        stdout=("Available devices:\n  Vulkan0: NVIDIA GeForce RTX 4070 Laptop GPU (7948 MiB, 7180 MiB free)\n"),
        stderr="",
        returncode=0,
    )

    async def probe(_binary, overrides):
        calls.append(dict(overrides))
        return result

    selected = await local_runtime.select_preferred_vulkan_device(binary, probe=probe)

    assert selected == 0
    assert calls == [{}, {"GGML_VK_VISIBLE_DEVICES": "0"}]


@pytest.mark.parametrize(
    "payload",
    (
        "Available devices:\n",
        "wrong header\n  (none)\n",
        "Available devices:\n  Vulkan1: NVIDIA GPU (8 MiB, 7 MiB free)\n",
        "Available devices:\n  Vulkan0: NVIDIA GPU (8 MiB, 9 MiB free)\n",
        "Available devices:\n  Vulkan0: NVIDIA\tGPU (8 MiB, 7 MiB free)\n",
        "Available devices:\n  Vulkan0: NVIDIA GPU (8 MiB, 7 MiB free)\nextra\n",
    ),
)
def test_vulkan_device_parser_rejects_malformed_or_unsafe_evidence(payload: str) -> None:
    with pytest.raises(local_runtime.LlamaRuntimeError):
        local_runtime.parse_vulkan_device_list(payload)


@pytest.mark.asyncio
async def test_vulkan_selector_rejects_ambiguous_nvidia_devices_without_isolation(tmp_path: Path) -> None:
    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"server")
    binary = RuntimeBinary("vulkan", server, _sha(server.read_bytes()), "Vulkan0", "all")
    calls: list[dict[str, str]] = []

    async def probe(_binary, overrides):
        calls.append(dict(overrides))
        return local_runtime.VulkanDeviceProbeResult(
            stdout=(
                "Available devices:\n"
                "  Vulkan0: NVIDIA GeForce RTX 4070 (7948 MiB, 7180 MiB free)\n"
                "  Vulkan1: NVIDIA RTX A2000 (4096 MiB, 3000 MiB free)\n"
            ),
            stderr="",
            returncode=0,
        )

    assert await local_runtime.select_preferred_vulkan_device(binary, probe=probe) is None
    assert calls == [{}]


def test_llama_child_environment_is_isolated_and_scrubs_parent_runtime_selection(monkeypatch) -> None:
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "9")
    monkeypatch.setenv("GGML_VK_DISABLE_BFLOAT16", "parent-value")
    monkeypatch.setenv("LLAMA_ARG_DEVICE", "parent-device")
    monkeypatch.setenv("LLAMA_ARG_MODEL", "parent-model")
    monkeypatch.setenv("GGML_BACKEND_PATH", "C:/decoy-backends")
    monkeypatch.setenv("GGML_CUDA_FORCE_MMQ", "1")
    monkeypatch.setenv("VK_ICD_FILENAMES", "C:/decoy-vulkan.json")
    monkeypatch.setenv("VK_LAYER_PATH", "C:/decoy-vulkan-layers")
    monkeypatch.setenv("CUDA_PATH", "C:/decoy-cuda")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    monkeypatch.setenv("PATH", "C:/decoy-dll-search")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/decoy.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/decoy.dylib")
    monkeypatch.setenv("SCRIBER_PARENT_SECRET", "must-not-cross")
    before = {name: os.environ.get(name) for name in local_runtime._RUNTIME_ENVIRONMENT_KEYS}

    child = local_runtime._child_environment({"GGML_VK_VISIBLE_DEVICES": "1"})

    assert {name: os.environ.get(name) for name in local_runtime._RUNTIME_ENVIRONMENT_KEYS} == before
    assert child["GGML_VK_VISIBLE_DEVICES"] == "1"
    assert "GGML_VK_DISABLE_BFLOAT16" not in child
    assert "LLAMA_ARG_DEVICE" not in child
    assert "LLAMA_ARG_MODEL" not in child
    assert "GGML_BACKEND_PATH" not in child
    assert "GGML_CUDA_FORCE_MMQ" not in child
    assert "VK_ICD_FILENAMES" not in child
    assert "VK_LAYER_PATH" not in child
    assert "CUDA_PATH" not in child
    assert "CUDA_VISIBLE_DEVICES" not in child
    assert "LD_PRELOAD" not in child
    assert "DYLD_INSERT_LIBRARIES" not in child
    assert "SCRIBER_PARENT_SECRET" not in child
    assert child["PATH"] != "C:/decoy-dll-search"
    allowed = {
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "NO_PROXY",
        "GGML_VK_VISIBLE_DEVICES",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "TEMP",
        "TMP",
        "HOME",
        "TMPDIR",
    }
    assert set(child) <= allowed
    with pytest.raises(local_runtime.LlamaRuntimeError, match="BF16"):
        local_runtime._child_environment({"GGML_VK_DISABLE_BFLOAT16": "0"})


@pytest.mark.asyncio
async def test_llama_runtime_launch_uses_private_verified_runtime_snapshot_as_cwd(monkeypatch, tmp_path: Path) -> None:
    server = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    server.write_bytes(b"server")
    model.write_bytes(b"model")
    captured: dict[str, object] = {}

    class ExitedProcess:
        returncode = 1

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ExitedProcess()

    monkeypatch.setenv("GGML_BACKEND_PATH", str(tmp_path / "decoy"))
    monkeypatch.setattr(local_runtime.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runtime = local_runtime.LlamaServerRuntime(
        binary=RuntimeBinary("cpu", server, _sha(server.read_bytes()), "none", "0"),
        model_path=model,
        model_sha256=_sha(model.read_bytes()),
        launch_spec=LlamaServerLaunchSpec(),
        startup_timeout_seconds=0.1,
    )

    with pytest.raises(local_runtime.LlamaRuntimeError, match="exited during startup"):
        await runtime.start()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    launched_executable = Path(captured["args"][0])
    launched_cwd = Path(kwargs["cwd"])
    assert launched_executable.name == server.name
    assert launched_executable.parent == launched_cwd
    assert launched_cwd != server.parent.resolve()
    assert not launched_cwd.exists()
    assert "GGML_BACKEND_PATH" not in kwargs["env"]


@pytest.mark.asyncio
async def test_llama_runtime_rejects_health_from_a_listener_not_owned_by_its_child(
    monkeypatch,
    tmp_path: Path,
) -> None:
    server = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    server.write_bytes(b"server")
    model.write_bytes(b"model")

    class RunningProcess:
        pid = 4242

        def __init__(self) -> None:
            self.returncode = None
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    wrong_listener: socket.socket | None = None

    async def fake_create_subprocess_exec(*args, **_kwargs):
        nonlocal wrong_listener
        port = int(args[args.index("--port") + 1])
        wrong_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        wrong_listener.bind(("127.0.0.1", port))
        wrong_listener.listen(1)
        return RunningProcess()

    monkeypatch.setattr(local_runtime.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(local_runtime, "_process_owns_loopback_listener", lambda *_args: False)
    runtime = local_runtime.LlamaServerRuntime(
        binary=RuntimeBinary("cpu", server, _sha(server.read_bytes()), "none", "0"),
        model_path=model,
        model_sha256=_sha(model.read_bytes()),
        launch_spec=LlamaServerLaunchSpec(),
        startup_timeout_seconds=0.1,
    )

    health_called = False

    async def health(*_args, **_kwargs):
        nonlocal health_called
        health_called = True
        return {"status": "ok"}

    monkeypatch.setattr(runtime, "_request", health)
    try:
        with pytest.raises(local_runtime.LlamaRuntimeError, match="listener ownership"):
            await runtime.start()
        assert health_called is False
    finally:
        if wrong_listener is not None:
            wrong_listener.close()


@pytest.mark.asyncio
async def test_llama_runtime_checks_owned_listener_before_authenticated_health(
    monkeypatch,
    tmp_path: Path,
) -> None:
    server = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    server.write_bytes(b"server")
    model.write_bytes(b"model")

    class RunningProcess:
        pid = 4242

        def __init__(self) -> None:
            self.returncode = None
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return RunningProcess()

    ownership_checks = 0

    def owns_listener(*_args) -> bool:
        nonlocal ownership_checks
        ownership_checks += 1
        return True

    monkeypatch.setattr(local_runtime.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(local_runtime, "_process_owns_loopback_listener", owns_listener)
    runtime = local_runtime.LlamaServerRuntime(
        binary=RuntimeBinary("cpu", server, _sha(server.read_bytes()), "none", "0"),
        model_path=model,
        model_sha256=_sha(model.read_bytes()),
        launch_spec=LlamaServerLaunchSpec(),
        startup_timeout_seconds=0.1,
    )
    health_calls = 0

    async def health(*_args, **_kwargs):
        nonlocal health_calls
        health_calls += 1
        assert ownership_checks >= 1
        return {"status": "ok"}

    monkeypatch.setattr(runtime, "_request", health)
    await runtime.start()

    assert health_calls == 1
    assert ownership_checks == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_authenticated_runtime_request_sink_rejects_unowned_listener(
    monkeypatch,
    tmp_path: Path,
) -> None:
    server = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    server.write_bytes(b"server")
    model.write_bytes(b"model")

    class RunningProcess:
        pid = 4242
        returncode = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    class RejectingSession:
        request_calls = 0

        def request(self, *_args, **_kwargs):
            self.request_calls += 1
            raise AssertionError("unowned listener must be rejected before HTTP")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(local_runtime, "_process_owns_loopback_listener", lambda *_args: False)
    runtime = local_runtime.LlamaServerRuntime(
        binary=RuntimeBinary("cpu", server, _sha(server.read_bytes()), "none", "0"),
        model_path=model,
        model_sha256=_sha(model.read_bytes()),
        launch_spec=LlamaServerLaunchSpec(),
    )
    process = RunningProcess()
    session = RejectingSession()
    runtime._process = process
    runtime._session = session

    with pytest.raises(local_runtime.LlamaRuntimeError, match="listener ownership"):
        await runtime._request("GET", "/health")
    assert session.request_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_vulkan_factory_retries_exact_bfloat16_failure_with_a_new_process(monkeypatch, tmp_path: Path) -> None:
    class TrackingRuntime:
        instances: list[TrackingRuntime] = []

        def __init__(self, **kwargs):
            self.environment = dict(kwargs["environment_overrides"])
            self.backend_name = kwargs["backend_name"]
            self.closed = False
            type(self).instances.append(self)

        async def start(self):
            if len(type(self).instances) == 1:
                raise local_runtime.VulkanBfloat16ExtensionError("extension unavailable")

        async def close(self):
            self.closed = True

    async def select_device(_binary):
        return 1

    monkeypatch.setattr(local_runtime, "LlamaServerRuntime", TrackingRuntime)
    binary = RuntimeBinary("vulkan", tmp_path / "llama-server.exe", "0" * 64, "Vulkan0", "all")
    factory = LlamaServerRuntimeFactory(
        binary,
        vulkan_device_selector=select_device,
        allow_bfloat16_compatibility_retry=True,
    )

    runtime = await factory.create(model_path=tmp_path / "model.gguf", model_sha256="1" * 64)

    assert len(TrackingRuntime.instances) == 2
    assert TrackingRuntime.instances[0].closed is True
    assert TrackingRuntime.instances[0].environment == {"GGML_VK_VISIBLE_DEVICES": "1"}
    assert runtime is TrackingRuntime.instances[1]
    assert runtime.backend_name == "vulkan_compat"
    assert TrackingRuntime.instances[1].environment == {
        "GGML_VK_VISIBLE_DEVICES": "1",
        "GGML_VK_DISABLE_BFLOAT16": "1",
    }


@pytest.mark.asyncio
async def test_vulkan_factory_does_not_retry_an_unclassified_startup_failure(monkeypatch, tmp_path: Path) -> None:
    class FailingRuntime:
        instances: list[FailingRuntime] = []

        def __init__(self, **_kwargs):
            self.closed = False
            type(self).instances.append(self)

        async def start(self):
            raise local_runtime.LlamaRuntimeError("generic startup failure")

        async def close(self):
            self.closed = True

    async def select_device(_binary):
        return 1

    monkeypatch.setattr(local_runtime, "LlamaServerRuntime", FailingRuntime)
    binary = RuntimeBinary("vulkan", tmp_path / "llama-server.exe", "0" * 64, "Vulkan0", "all")
    factory = LlamaServerRuntimeFactory(
        binary,
        vulkan_device_selector=select_device,
        allow_bfloat16_compatibility_retry=True,
    )

    with pytest.raises(local_runtime.LlamaRuntimeError, match="generic startup failure"):
        await factory.create(model_path=tmp_path / "model.gguf", model_sha256="1" * 64)

    assert len(FailingRuntime.instances) == 1
    assert FailingRuntime.instances[0].closed is True


@pytest.mark.asyncio
async def test_packaged_vulkan_contract_maps_physical_vulkan1_and_disables_bfloat16_on_first_launch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class TrackingRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

        async def close(self):
            return None

    async def select_device(_binary):
        return 1

    monkeypatch.setattr(local_runtime, "LlamaServerRuntime", TrackingRuntime)
    binary = RuntimeBinary("vulkan", tmp_path / "llama-server.exe", "0" * 64, "Vulkan0", "all")
    factory = LlamaServerRuntimeFactory(
        binary,
        vulkan_device_selector=select_device,
        allow_bfloat16_compatibility_retry=True,
        disable_bfloat16_on_first_launch=True,
    )

    await factory.create(model_path=tmp_path / "model.gguf", model_sha256="1" * 64)

    assert captured["environment_overrides"] == {
        "GGML_VK_VISIBLE_DEVICES": "1",
        "GGML_VK_DISABLE_BFLOAT16": "1",
    }
    assert captured["backend_name"] == "vulkan_compat"


def test_bfloat16_compatibility_retry_requires_exact_extension_evidence() -> None:
    assert local_runtime._is_bfloat16_extension_failure(b"vk::PhysicalDevice::createDevice: ErrorExtensionNotPresent")
    assert not local_runtime._is_bfloat16_extension_failure(b"ErrorExtensionNotPresent")
    assert not local_runtime._is_bfloat16_extension_failure(b"vk::PhysicalDevice::createDevice: ErrorFeatureNotPresent")


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
    assert {item.name for item in factories[0].binary.runtime_files} == {"llama-server.exe", "ggml.dll"}
    assert factories[0].allow_bfloat16_compatibility_retry is True
    assert factories[0].disable_bfloat16_on_first_launch is True
    assert factories[1].binary.device == "none"
    assert factories[1].allow_bfloat16_compatibility_retry is False
    assert factories[1].disable_bfloat16_on_first_launch is False


@pytest.mark.asyncio
async def test_runtime_dll_is_reverified_before_any_spawn(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "local-polishing"
    runtime_root.mkdir()
    server = runtime_root / "llama-server.exe"
    library = runtime_root / "ggml.dll"
    model = tmp_path / "model.gguf"
    server.write_bytes(b"server")
    library.write_bytes(b"library")
    model.write_bytes(b"model")
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
    factory = packaged_runtime_factories(runtime_root)[1]
    library.write_bytes(b"tampered library")
    spawned = False

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("spawn must not occur")

    monkeypatch.setattr(local_runtime.asyncio, "create_subprocess_exec", forbidden_spawn)
    runtime = local_runtime.LlamaServerRuntime(
        binary=factory.binary,
        model_path=model,
        model_sha256=_sha(model.read_bytes()),
        launch_spec=LlamaServerLaunchSpec(),
    )
    with pytest.raises(local_runtime.LlamaRuntimeError, match="runtime file hash mismatch"):
        await runtime.start()
    assert spawned is False


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
async def test_plain_completion_bypasses_chat_template_and_returns_plain_text(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    runtime = FakeRuntime(output="Bitte überweise 1.250 €.")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    assert (await manager.wait_for_operation(operation_id)).status == "ready"

    outcome = await manager.polish("Bitte überweise 1.250 €.", "q8_0")

    assert outcome.text == "Bitte überweise 1.250 €."
    assert outcome.status == "accepted"
    assert runtime.applied_templates == 0
    assert runtime.prompts == ["Student-Regel\n\nTranskript:\nBitte überweise 1.250 €.\n\nBereinigte Fassung:\n"]
    assert "⟦KEEP_" not in runtime.prompts[0]
    assert runtime.max_new_token_requests == [384]
    await manager.close()


@pytest.mark.asyncio
async def test_schema_one_chat_completion_keeps_the_legacy_dynamic_generation_budget(
    tmp_path: Path,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    runtime = FakeRuntime()
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    assert (await manager.wait_for_operation(operation_id)).status == "ready"

    outcome = await manager.polish("Hallo welt.", "q8_0")

    assert outcome.status == "accepted"
    assert runtime.max_new_token_requests == [133]
    await manager.close()


@pytest.mark.asyncio
async def test_plain_completion_without_a_catalog_bound_cap_fails_closed(
    tmp_path: Path,
) -> None:
    payloads: dict[str, bytes] = {}
    schema_three = _catalog(payloads, plain_completion=True)
    schema_two = ModelCatalog(
        schema_version=2,
        repository_id=schema_three.repository_id,
        revision=schema_three.revision,
        requires_token=schema_three.requires_token,
        variants={
            variant: replace(descriptor, generation_max_new_tokens=None)
            for variant, descriptor in schema_three.variants.items()
        },
    )
    runtime = FakeRuntime(output="Hallo Welt.")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=schema_two,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    assert (await manager.wait_for_operation(operation_id)).status == "ready"

    outcome = await manager.polish("Hallo welt.", "q8_0")

    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("prompt_contract_error",)
    assert runtime.max_new_token_requests == []
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "finish_reason", "prompt_truncated", "reason"),
    (
        ("", "eos", False, "empty_output"),
        ("Hallo Welt.", "unknown", False, "generation_incomplete"),
        ("Hallo Welt.", "limit", False, "token_budget_exhausted"),
        ("Hallo Welt.", "eos", True, "prompt_truncated"),
        ("Hallo\x00 Welt.", "eos", False, "control_character_leak"),
        ("Hallo <|assistant|>Welt.", "eos", False, "control_markup_leak"),
        ("Hallo ${transcript} Welt.", "eos", False, "control_markup_leak"),
        ("[DOC]\n[P]Hallo Welt.[/P]\n[/DOC]", "eos", False, "control_markup_leak"),
        ("Bereinigte Fassung: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("**Bereinigte Fassung:** Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Transkript: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Ergebnis:\nHallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("- Ergebnis: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("[Ergebnis] Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Output: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Hier ist der bereinigte Text: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Hier ist deine bereinigte Fassung: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("Hier ist Ihr bereinigter Text: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("The cleaned text is: Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ('"Hallo Welt."', "eos", False, "unsafe_plain_structure"),
        ("`Hallo Welt.`", "eos", False, "unsafe_plain_structure"),
        ("**Hallo Welt.**", "eos", False, "unsafe_plain_structure"),
        ("„Hallo Welt.“", "eos", False, "unsafe_plain_structure"),
        ("> Hallo Welt.", "eos", False, "unsafe_plain_structure"),
        ('{"result":"Hallo Welt."}', "eos", False, "unsafe_plain_structure"),
        ("<div>Hallo Welt.</div>", "eos", False, "control_markup_leak"),
        ("<result>Hallo Welt.</result>", "eos", False, "unsafe_plain_structure"),
        (
            "Student-Regel\n\nTranskript:\nHallo welt.\n\nBereinigte Fassung:\nHallo Welt.",
            "eos",
            False,
            "unsafe_plain_structure",
        ),
        ("```\nHallo Welt.", "eos", False, "unsafe_plain_structure"),
        ("<assistant>Hallo Welt.</assistant>", "eos", False, "control_markup_leak"),
        ("Hallo ⟦KEEP_Z⟧ Welt.", "eos", False, "marker_leak"),
    ),
)
async def test_plain_text_contract_fails_closed(
    tmp_path: Path,
    output: str,
    finish_reason: str,
    prompt_truncated: bool,
    reason: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    runtime = FakeRuntime(
        output=output,
        finish_reason=finish_reason,
        prompt_truncated=prompt_truncated,
    )
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Hallo welt.", "q8_0")

    assert outcome.text == "Hallo welt."
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == (reason,)
    await manager.close()


@pytest.mark.asyncio
async def test_plain_text_contract_rejects_budget_boundary_even_with_eos(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    runtime = FakeRuntime(output="Hallo Welt.", finish_reason="eos", exhaust_budget=True)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Hallo welt.", "q8_0")

    assert outcome.text == "Hallo welt."
    assert outcome.reason_codes == ("token_budget_exhausted",)
    await manager.close()


@pytest.mark.asyncio
async def test_plain_text_contract_rejects_short_source_content_addition(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    runtime = FakeRuntime(output="Hallo. Morgen fliegen wir nach Tokio und kaufen dort ein schönes Haus am Meer.")
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(runtime),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Hallo.", "q8_0")

    assert outcome.text == "Hallo."
    assert outcome.reason_codes == ("content_addition",)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "output"),
    (
        ("Die Quote beträgt fünfzehn Prozent.", "Die Quote beträgt 15 %."),
        ("Wir treffen uns um vierzehn Uhr dreißig.", "Wir treffen uns um 14:30 Uhr."),
        (
            "Die Zahlung beträgt zweitausend fünfhundert Euro.",
            "Die Zahlung beträgt 2.500 €.",
        ),
        (
            "Der Termin ist am dritten vierten zwanzig vierundzwanzig.",
            "Der Termin ist am 03.04.2024.",
        ),
        (
            "Der Termin ist am dritten April zweitausendvierundzwanzig.",
            "Der Termin ist am 03.04.2024.",
        ),
    ),
)
async def test_plain_text_contract_accepts_equivalent_german_number_formatting(
    tmp_path: Path,
    source: str,
    output: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "output", "reason"),
    (
        (
            "Die Kündigung wurde abgelehnt.",
            "Die Kündigung wurde akzeptiert.",
            "changed_semantic_anchor",
        ),
        (
            "Herr Müller genehmigt den Antrag.",
            "Herr Schmidt genehmigt den Antrag.",
            "changed_named_anchor",
        ),
        (
            "Heute besprechen wir den Vertrag und senden danach die Unterlagen an die Verwaltung.",
            "Heute besprechen wir den Vertrag und senden danach die Unterlagen an die Verwaltung. "
            "Zusätzlich wurde die Kündigung verbindlich akzeptiert und der gesamte Vorgang ist endgültig abgeschlossen.",
            "content_addition",
        ),
    ),
)
async def test_plain_text_contract_rejects_semantic_replacement_or_long_source_invention(
    tmp_path: Path,
    source: str,
    output: str,
    reason: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == (reason,)
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_falls_back_when_a_similar_word_changes_meaning(tmp_path: Path) -> None:
    source = "Der Vertrag wurde beschlossen."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output="Der Vertrag wurde geschlossen.")),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("content_loss",)
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_falls_back_when_correction_signal_would_delete_content(
    tmp_path: Path,
) -> None:
    source = "Der Vertrag gilt, ich meine, das ernst."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output="Der Vertrag das ernst.")),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("content_loss",)
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_name_capitalization_after_a_role(tmp_path: Path) -> None:
    source = "Sehr geehrte frau berger, bitte senden Sie die Unterlagen."
    output = "Sehr geehrte Frau Berger, bitte senden Sie die Unterlagen."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_unambiguous_spoken_punctuation_commands(tmp_path: Path) -> None:
    source = "Hallo komma wie geht es fragezeichen"
    output = "Hallo, wie geht es?"
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_an_unambiguous_spoken_line_break(tmp_path: Path) -> None:
    source = "Bitte senden Sie die Unterlagen neue Zeile den Grundriss komma den Energieausweis punkt"
    output = "Bitte senden Sie die Unterlagen:\nden Grundriss, den Energieausweis."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_grammatical_polarity_inflection(tmp_path: Path) -> None:
    source = "Wir haben keine Termin."
    output = "Wir haben keinen Termin."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_spoken_paragraph_formatting(tmp_path: Path) -> None:
    source = "Nach Paragraph fünfhundertfünfunddreißig gilt der Mietvertrag."
    output = "Nach § 535 gilt der Mietvertrag."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_preserves_decimal_comma_as_a_number_phrase(tmp_path: Path) -> None:
    source = "Die Miete beträgt eins Komma fünfzig Euro."
    output = "Die Miete beträgt 1,50 €."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_requires_each_bound_spoken_punctuation_command(tmp_path: Path) -> None:
    source = "Hallo komma wie geht es fragezeichen"
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output="Hallo wie geht es")),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("changed_format_command",)
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_an_unambiguous_spoken_paragraph_break(tmp_path: Path) -> None:
    source = "Erster Gedanke neuer Absatz Zweiter Gedanke"
    output = "Erster Gedanke\n\nZweiter Gedanke."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_accepts_a_compact_spoken_amount_phrase(tmp_path: Path) -> None:
    source = "zweitausend fünfhundert Euro"
    output = "2.500 €"
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_qad_plain_text_preserves_a_german_grouped_decimal_amount(tmp_path: Path) -> None:
    source = "Die Rechnung beträgt 12.500,00 €"
    output = "Die Rechnung beträgt 12.500,00 €."
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "output", "reason"),
    (
        (
            "Alexander bestätigt den Termin.",
            "Alexandra bestätigt den Termin.",
            "content_loss",
        ),
        (
            "Müller bestätigt den Termin.",
            "Möller bestätigt den Termin.",
            "content_loss",
        ),
        (
            "Schmidt bestätigt den Termin.",
            "Schmitt bestätigt den Termin.",
            "content_loss",
        ),
        (
            "Sehr geehrte frau berger, bitte senden Sie die Unterlagen.",
            "Sehr geehrte Frau Bergner, bitte senden Sie die Unterlagen.",
            "changed_named_anchor",
        ),
        (
            "Bitte senden Sie die Unterlagen neue Zeile den Grundriss.",
            "Bitte senden Sie die Unterlagen den Grundriss.",
            "changed_format_command",
        ),
        (
            "Das Komma ist wichtig.",
            "Das ist wichtig.",
            "content_loss",
        ),
        (
            "Nach § 535 Absatz 3 gilt der Mietvertrag.",
            "Nach § 535 3 gilt der Mietvertrag.",
            "changed_format_command",
        ),
        (
            "Wir haben keinen Termin vereinbart.",
            "Wir haben einen Termin vereinbart.",
            "changed_polarity",
        ),
        (
            "Nach § 535 BGB gilt der Mietvertrag.",
            "Nach § 536 BGB gilt der Mietvertrag.",
            "changed_legal_reference",
        ),
        (
            "Dieser Paragraph erklärt die Regel.",
            "Dieser erklärt die Regel.",
            "content_loss",
        ),
        (
            "Der Mieter bestätigt den vollständigen Mietvertrag heute schriftlich.",
            "Der Mieter bestätigt den Mietvertrag.",
            "content_loss",
        ),
    ),
)
async def test_qad_plain_text_falls_back_for_unsafe_review_cases(
    tmp_path: Path,
    source: str,
    output: str,
    reason: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == (reason,)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "am dritten vierten zwanzig vierundzwanzig",
            "am 03.04.2024",
        ),
        (
            "fünfzig Kilowattstunden pro Quadratmeter und Jahr",
            "50 kWh/m²a",
        ),
    ),
)
async def test_qad_plain_text_accepts_short_compact_date_and_unit_formatting(
    tmp_path: Path,
    source: str,
    output: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _qad_plain_catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=output)),),
    )
    operation_id = await manager.install("qad_q4_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "qad_q4_0")

    assert outcome.text == output
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.parametrize(
    ("source", "candidate", "reason"),
    (
        (
            "Die Miete beträgt 1.000 € und die Kaution beträgt 2.000 €.",
            "Die Miete beträgt 2.000 € und die Kaution beträgt 1.000 €.",
            "changed_number",
        ),
        (
            "Herr Müller zahlt nicht an Frau Schmidt.",
            "Herr Schmidt zahlt nicht an Frau Müller.",
            "changed_named_anchor",
        ),
        (
            "Herr Müller zahlt nicht pünktlich.",
            "Herr Müller zahlt pünktlich nicht.",
            "changed_polarity_position",
        ),
        (
            "Der Mieter bestätigt den vollständigen Mietvertrag heute schriftlich.",
            "Der Mieter bestätigt den Mietvertrag.",
            "content_loss",
        ),
        (
            "Die Miete beträgt eintausend Euro, die Kaution zweitausend Euro.",
            "Die Miete beträgt 2.000 €, die Kaution 1.000 €.",
            "changed_number",
        ),
        (
            "Wir treffen uns um 14 Uhr.",
            "Wir treffen uns 14 Uhr.",
            "content_loss",
        ),
        (
            "Also folgt Abschnitt B.",
            "Folgt Abschnitt B.",
            "content_loss",
        ),
        (
            "You know the answer.",
            "Know the answer.",
            "content_loss",
        ),
        (
            "Der Mietvertrag ist gültig, ich meine das ernst.",
            "Der Mietvertrag das ernst.",
            "content_loss",
        ),
    ),
)
def test_plain_text_safety_binds_roles_positions_and_meaningful_deletions(
    source: str,
    candidate: str,
    reason: str,
) -> None:
    with pytest.raises(SafetyError, match=reason):
        validate_plain_text_content(source, candidate)


@pytest.mark.parametrize(
    ("source", "candidate"),
    (
        ("Der Mietvertag beginnt heute.", "Der Mietvertrag beginnt heute."),
        ("Ich, ähm, sende den Mietvertrag heute.", "Ich sende den Mietvertrag heute."),
        ("Ich ich sende den Mietvertrag heute.", "Ich sende den Mietvertrag heute."),
        ("Wir treffen uns am Montag, nein, am Dienstag.", "Wir treffen uns am Dienstag."),
        (
            "Der Verbrauch beträgt fünfzig Kilowattstunden pro Quadratmeter und Jahr.",
            "Der Verbrauch beträgt 50 kWh/m²a.",
        ),
        ("Die Vergütung beträgt zehn Euro pro Quadratmeter.", "Die Vergütung beträgt 10 €/m²."),
    ),
)
def test_plain_text_safety_accepts_only_reviewed_cleanup_classes(source: str, candidate: str) -> None:
    validate_plain_text_content(source, candidate)


def test_plain_text_safety_preserves_an_english_grouped_decimal_amount() -> None:
    validate_plain_text_content(
        "The invoice total is $12,500.00",
        "The invoice total is $12,500.00.",
    )


@pytest.mark.parametrize(
    ("source", "candidate"),
    (
        ("Die Rechnung beträgt 12.500,00 €.", "Die Rechnung beträgt 12.500,01 €."),
        ("The invoice total is $12,500.00.", "The invoice total is $12,500.01."),
    ),
)
def test_plain_text_safety_rejects_changed_grouped_decimal_amount(
    source: str,
    candidate: str,
) -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(source, candidate)


@pytest.mark.parametrize(
    ("source", "candidate"),
    (
        ("Die Folge lautet 1,2,3.", "Die Folge lautet 1,2,3."),
        ("Die Folge lautet 1,2,3.", "Die Folge lautet 1,2,30."),
        ("Die Folge lautet 1.2.3.", "Die Folge lautet 1.2.3."),
        ("Die Folge lautet 1.2.3.", "Die Folge lautet 1.2.30."),
    ),
)
def test_plain_text_safety_rejects_malformed_repeated_decimal_separators(
    source: str,
    candidate: str,
) -> None:
    with pytest.raises(SafetyError, match="changed_number"):
        validate_plain_text_content(source, candidate)


def test_plain_text_safety_preserves_an_english_grouped_integer() -> None:
    validate_plain_text_content(
        "The population is 1,234,567",
        "The population is 1,234,567.",
    )


@pytest.mark.parametrize(
    ("source", "candidate", "expected"),
    (
        (
            "Die Quote beträgt fünfzehn Prozent.",
            "Die Quote beträgt 16 %.",
            "Die Quote beträgt 15 %.",
        ),
        (
            "Die Zahlung beträgt zweitausend fünfhundert Euro.",
            "Die Zahlung beträgt 2.600 €.",
            "Die Zahlung beträgt 2.500 €.",
        ),
        (
            "Der Faktor beträgt eins Komma fünfzig.",
            "Der Faktor beträgt 1,60.",
            "Der Faktor beträgt 1,50.",
        ),
        (
            "Der Termin ist am dritten April zweitausendvierundzwanzig.",
            "Der Termin ist am 04.04.2025.",
            "Der Termin ist am 03.04.2024.",
        ),
        (
            "Wir treffen uns um vierzehn Uhr dreißig.",
            "Wir treffen uns um 15:31 Uhr.",
            "Wir treffen uns um 14:30 Uhr.",
        ),
        (
            "Wir treffen uns um vierzehn Uhr dreißig.",
            "Wir treffen uns um 15 Uhr 31.",
            "Wir treffen uns um 14 Uhr 30.",
        ),
        (
            "Die Miete beträgt eintausend Euro, die Kaution zweitausend Euro.",
            "Die Miete beträgt 1.100 €, die Kaution 2.100 €.",
            "Die Miete beträgt 1.000 €, die Kaution 2.000 €.",
        ),
    ),
)
def test_numeric_anchor_repair_replaces_unambiguous_wrong_digit_values(
    source: str,
    candidate: str,
    expected: str,
) -> None:
    assert repair_unambiguous_numeric_anchors(source, candidate) == expected


@pytest.mark.parametrize(
    ("source", "candidate", "reason"),
    (
        (
            "Der Wert beträgt fünf Euro. Der Wert beträgt sechs Euro.",
            "Der Wert beträgt 7 €. Der Wert beträgt 8 €.",
            "changed_number_role",
        ),
        ("Die Quote beträgt fünfzehn Prozent.", "Die Quote beträgt 16 €.", "changed_unit"),
        (
            "Die Quote beträgt fünfzehn Prozent.",
            "Die Quote beträgt 16 % und die Zahlung 2 €.",
            "changed_number",
        ),
        (
            "Die Miete beträgt eintausend Euro, die Kaution zweitausend Euro.",
            "Die Kaution beträgt 2.100 €, die Miete 1.100 €.",
            "changed_number_role",
        ),
        ("Die Quote beträgt fünfzehn Prozent.", "Die Quote beträgt sechzehn Prozent.", "changed_number"),
        (
            "Der Termin ist am dritten April zweitausendvierundzwanzig.",
            "Der Termin ist am 31.02.2025.",
            "changed_number",
        ),
        ("Die Telefonnummer lautet eins zwei drei.", "Die Telefonnummer lautet 124.", "changed_number"),
        (
            "Die erste Rate beträgt 5 Euro, die zweite Rate zehn Euro.",
            "Die erste Rate beträgt 5 Euro, die zweite Rate 11 Euro.",
            "changed_number",
        ),
        (
            "Die Quote beträgt fünfzehn Prozent, die Zahlung zehn Euro.",
            "Die Quote beträgt 16 %, die Zahlung elf Euro.",
            "changed_number",
        ),
    ),
)
def test_numeric_anchor_repair_fails_closed_for_ambiguous_or_non_digit_mismatches(
    source: str,
    candidate: str,
    reason: str,
) -> None:
    with pytest.raises(SafetyError, match=reason):
        repair_unambiguous_numeric_anchors(source, candidate)


def test_numeric_anchor_repair_does_not_treat_an_ambiguous_amount_as_a_split_year() -> None:
    for source, candidate in (
        ("Die Zahlung beträgt zwanzig fünf Euro.", "Die Zahlung beträgt 2.006 €."),
        ("Im Jahr kostet es zwanzig fünf Euro.", "Im Jahr kostet es 2.006 €."),
    ):
        with pytest.raises(SafetyError, match="changed_number"):
            repair_unambiguous_numeric_anchors(source, candidate)


@pytest.mark.parametrize(
    ("source", "candidate", "expected"),
    (
        ("Das Jahr ist zwanzig fünf.", "Das Jahr ist 2006.", "Das Jahr ist 2005."),
        (
            "Der Termin ist im April zwanzig fünf.",
            "Der Termin ist im April 2006.",
            "Der Termin ist im April 2005.",
        ),
    ),
)
def test_numeric_anchor_repair_allows_split_years_only_in_explicit_context(
    source: str,
    candidate: str,
    expected: str,
) -> None:
    assert repair_unambiguous_numeric_anchors(source, candidate) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plain_completion", "generated"),
    (
        (True, "Die Quote beträgt 16 %."),
        (False, "[DOC]\n[P]Die Quote beträgt 16 %.[/P]\n[/DOC]"),
    ),
)
async def test_polish_repairs_unambiguous_numeric_anchor_after_numeric_rejection(
    tmp_path: Path,
    plain_completion: bool,
    generated: str,
) -> None:
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=plain_completion)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=generated)),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish("Die Quote beträgt fünfzehn Prozent.", "q8_0")

    assert outcome.text == "Die Quote beträgt 15 %."
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plain_completion", "generated"),
    (
        (True, "Die Miete beträgt 20 €."),
        (False, "[DOC]\n[P]Die Miete beträgt 20 €.[/P]\n[/DOC]"),
    ),
)
async def test_polish_accepts_a_valid_numeric_self_correction_before_repair(
    tmp_path: Path,
    plain_completion: bool,
    generated: str,
) -> None:
    source = "Die Miete beträgt zehn, nein, zwanzig Euro."
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=plain_completion)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output=generated)),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == "Die Miete beträgt 20 €."
    assert outcome.status == "accepted"
    assert outcome.reason_codes == ()
    await manager.close()


@pytest.mark.asyncio
async def test_polish_sst_revalidates_full_semantics_after_numeric_repair(tmp_path: Path) -> None:
    source = "Wir vereinbaren heute eine monatliche Miete von fünfzehn Prozent."
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(
            FakeRuntimeFactory(
                FakeRuntime(output=("[DOC]\n[P]Wir kündigen heute eine monatliche Miete von 16 %.[/P]\n[/DOC]"))
            ),
        ),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("changed_semantic_anchor",)
    await manager.close()


@pytest.mark.asyncio
async def test_polish_numeric_anchor_repair_fails_closed_without_partial_output(tmp_path: Path) -> None:
    source = "Die Quote beträgt fünfzehn Prozent, die Zahlung zehn Euro."
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads, plain_completion=True)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(FakeRuntimeFactory(FakeRuntime(output="Die Quote beträgt 16 %, die Zahlung elf Euro.")),),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("changed_number",)
    await manager.close()


@pytest.mark.asyncio
async def test_polish_does_not_mix_anchor_repair_with_restored_numeric_placeholders(tmp_path: Path) -> None:
    source = "Die erste Rate beträgt 5 Euro, die zweite Rate zehn Euro."
    payloads: dict[str, bytes] = {}
    catalog = _catalog(payloads)
    manager = LocalPolishing(
        root=tmp_path,
        catalog=catalog,
        downloader=FakeDownloader(payloads),
        token_provider=lambda: "read-token",
        runtime_factories=(
            FakeRuntimeFactory(
                FakeRuntime(output=("[DOC]\n[P]Die erste Rate beträgt 11 Euro, die zweite Rate ⟦KEEP_A⟧.[/P]\n[/DOC]"))
            ),
        ),
    )
    operation_id = await manager.install("q8_0")
    await manager.wait_for_operation(operation_id)

    outcome = await manager.polish(source, "q8_0")

    assert outcome.text == source
    assert outcome.status == "original_fallback"
    assert outcome.reason_codes == ("changed_number",)
    await manager.close()


def test_compound_units_emit_only_the_longest_non_overlapping_anchor() -> None:
    from src.local_polishing.safety import _unit_anchors

    assert _unit_anchors("50 kWh/m²a und 10 €/m²") == {"kwh/m2a": 1, "eur/m2": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_type", "finish_reason", "stop_reached"),
    (
        ("eos", "eos", True),
        ("word", "stop", True),
        ("limit", "limit", False),
        ("none", "unknown", False),
    ),
)
async def test_llama_completion_preserves_pinned_termination_evidence(
    monkeypatch,
    tmp_path: Path,
    stop_type: str,
    finish_reason: str,
    stop_reached: bool,
) -> None:
    binary = RuntimeBinary(
        name="cpu",
        executable=tmp_path / "llama-server.exe",
        sha256="2" * 64,
        device="none",
        gpu_layers="0",
    )
    runtime = local_runtime.LlamaServerRuntime(
        binary=binary,
        model_path=tmp_path / "model.gguf",
        model_sha256="3" * 64,
        launch_spec=LlamaServerLaunchSpec(),
    )
    request = AsyncMock(
        return_value={
            "content": "Hallo Welt.",
            "tokens_predicted": 3,
            "stop": True,
            "stop_type": stop_type,
            "truncated": False,
        }
    )
    monkeypatch.setattr(runtime, "_request", request)

    result = await runtime.complete("prompt", max_new_tokens=32)

    assert result == CompletionResult("Hallo Welt.", finish_reason, 3, False)
    assert result.stop_reached is stop_reached
    assert request.await_args.args == ("POST", "/completion")
    assert request.await_args.kwargs["body"]["n_predict"] == 32


@pytest.mark.asyncio
async def test_llama_completion_rejects_unpinned_termination_shape(monkeypatch, tmp_path: Path) -> None:
    binary = RuntimeBinary(
        name="cpu",
        executable=tmp_path / "llama-server.exe",
        sha256="2" * 64,
        device="none",
        gpu_layers="0",
    )
    runtime = local_runtime.LlamaServerRuntime(
        binary=binary,
        model_path=tmp_path / "model.gguf",
        model_sha256="3" * 64,
        launch_spec=LlamaServerLaunchSpec(),
    )
    monkeypatch.setattr(
        runtime,
        "_request",
        AsyncMock(
            return_value={
                "content": "Hallo Welt.",
                "tokens_predicted": 3,
                "stop": True,
                "stopped_eos": True,
                "truncated": False,
            }
        ),
    )

    with pytest.raises(local_runtime.LlamaRuntimeError, match="stop type"):
        await runtime.complete("prompt", max_new_tokens=32)


def test_legacy_promotion_surface_is_removed() -> None:
    assert not hasattr(LocalPolishing, "remove_legacy_revision")


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
