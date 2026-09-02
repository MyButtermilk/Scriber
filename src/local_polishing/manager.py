"""Lifecycle owner for immutable local-polishing model installations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from src.runtime.paths import data_dir

from .catalog import (
    DEFAULT_CATALOG,
    ArtifactSpec,
    CatalogError,
    CatalogVariant,
    ModelCatalog,
    catalog_identity,
)
from .hub import ArtifactDownloader, HuggingFaceArtifactDownloader
from .runtime import CompletionResult, GenerationRuntime, GenerationRuntimeFactory, packaged_runtime_factories
from .safety import (
    SafetyError,
    load_policy_markers,
    protect_transcript,
    render_plain_sst,
    render_plain_text,
    repair_unambiguous_numeric_anchors,
    restore_protected_values,
    validate_plain_text_content,
)

OperationStatus = Literal[
    "downloading",
    "verifying",
    "cancelling",
    "cancelled",
    "ready",
    "error",
]
TokenProvider = Callable[[], str | None | Awaitable[str | None]]
POLISHING_INSTRUCTION = (
    "Bereinige dieses Transkript konservativ. Gib ausschließlich gültiges SST v1 aus; "
    "erhalte Bedeutung, Reihenfolge und geschützte Platzhalter exakt.\n\nTranskript:\n"
)


def _validate_or_repair_numeric_anchors(source: str, candidate: str) -> str:
    """Accept a proven candidate first; repair only a numeric mismatch."""

    try:
        validate_plain_text_content(source, candidate)
    except SafetyError as error:
        if error.code != "changed_number":
            raise
    else:
        return candidate
    repaired = repair_unambiguous_numeric_anchors(source, candidate)
    validate_plain_text_content(source, repaired)
    return repaired


class LocalPolishingError(RuntimeError):
    """A bounded, machine-actionable local-polishing failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InstallOperationSnapshot:
    operation_id: str
    variant: CatalogVariant
    status: OperationStatus
    bytes_received: int
    bytes_total: int
    progress: float
    eta_seconds: float | None
    error_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "variant": self.variant,
            "status": self.status,
            "bytesReceived": self.bytes_received,
            "bytesTotal": self.bytes_total,
            "progress": self.progress,
            "etaSeconds": self.eta_seconds,
            "errorCode": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class LocalPolishingSnapshot:
    available: bool
    message: str | None
    current_variant: CatalogVariant | None
    models: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "message": self.message,
            "currentVariant": self.current_variant,
            "models": [dict(item) for item in self.models],
        }


@dataclass(frozen=True, slots=True)
class PolishOutcome:
    text: str
    variant: CatalogVariant | None
    status: Literal["accepted", "original_fallback"]
    reason_codes: tuple[str, ...]
    duration_ms: float
    runtime_backend: str | None


@dataclass(slots=True)
class _InstallOperation:
    operation_id: str
    variant: CatalogVariant
    status: OperationStatus
    bytes_received: int
    bytes_total: int
    started_at: float
    cancel_event: asyncio.Event
    task: asyncio.Task[None] | None = None
    error_code: str | None = None

    def snapshot(self, now: float) -> InstallOperationSnapshot:
        progress = 0.0 if self.bytes_total < 1 else min(100.0, self.bytes_received * 100.0 / self.bytes_total)
        eta: float | None = None
        elapsed = max(0.0, now - self.started_at)
        if self.status in {"downloading", "verifying", "cancelling"} and self.bytes_received > 0 and elapsed > 0:
            rate = self.bytes_received / elapsed
            if rate > 0:
                eta = max(0.0, (self.bytes_total - self.bytes_received) / rate)
        if self.status == "ready":
            progress = 100.0
            eta = 0.0
        return InstallOperationSnapshot(
            operation_id=self.operation_id,
            variant=self.variant,
            status=self.status,
            bytes_received=self.bytes_received,
            bytes_total=self.bytes_total,
            progress=progress,
            eta_seconds=eta,
            error_code=self.error_code,
        )


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise LocalPolishingError("artifact_not_regular", "downloaded artifact is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _verify_file(path: Path, artifact: ArtifactSpec) -> None:
    if path.is_symlink() or not path.is_file():
        raise LocalPolishingError("artifact_not_regular", "downloaded artifact is not a regular file")
    if path.stat().st_size != artifact.byte_size:
        raise LocalPolishingError("artifact_size_mismatch", "downloaded artifact size differs from the catalog")
    if _sha256_file(path) != artifact.sha256:
        raise LocalPolishingError("artifact_hash_mismatch", "downloaded artifact hash differs from the catalog")


def _safe_artifact_path(base: Path, relative_path: str) -> Path:
    resolved_base = base.resolve()
    candidate = base / Path(relative_path)
    resolved_candidate = candidate.resolve()
    if resolved_candidate == resolved_base or resolved_base not in resolved_candidate.parents:
        raise LocalPolishingError("unsafe_artifact_path", "artifact escaped its managed directory")
    return candidate


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_canonical_json(value))
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


async def _close_runtime_resilient(runtime: GenerationRuntime) -> None:
    """Finish runtime teardown before delivering a concurrent cancellation."""

    close_task = asyncio.create_task(runtime.close())
    pending_cancel: asyncio.CancelledError | None = None
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as error:
            pending_cancel = error
        except Exception:
            break
    with suppress(Exception):
        close_task.result()
    if pending_cancel is not None:
        raise pending_cancel


class LocalPolishing:
    """Deep module hiding downloads, activation, native runtime and safety details."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        catalog: ModelCatalog = DEFAULT_CATALOG,
        downloader: ArtifactDownloader | None = None,
        token_provider: TokenProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
        runtime_factories: tuple[GenerationRuntimeFactory, ...] | None = None,
    ) -> None:
        self.root = (root or (data_dir() / "models" / "local-polishing")).resolve()
        self.catalog = catalog
        self._variants: tuple[CatalogVariant, ...] = tuple(catalog.variants)
        self._downloader = downloader or HuggingFaceArtifactDownloader()
        self._token_provider = token_provider or (lambda: None)
        self._clock = clock
        self._runtime_factories = runtime_factories if runtime_factories is not None else packaged_runtime_factories()
        self._operation_lock = asyncio.Lock()
        self._operations: dict[str, _InstallOperation] = {}
        self._active_operation_by_variant: dict[CatalogVariant, str] = {}
        self._runtime_variant: CatalogVariant | None = None
        self._runtime: GenerationRuntime | None = None
        self._runtime_lock = asyncio.Lock()
        self._runtime_ready_by_variant: dict[CatalogVariant, bool] = {variant: False for variant in self._variants}
        self._runtime_error_by_variant: dict[CatalogVariant, str | None] = {
            variant: (None if self._runtime_factories else "runtime_unavailable") for variant in self._variants
        }
        self._inference_count = 0
        self._closed = False

    def _require_variant(self, variant: str) -> CatalogVariant:
        if variant not in self._variants:
            raise LocalPolishingError("unknown_variant", f"unsupported local-polishing variant: {variant!r}")
        return variant

    def _stage_dir(self, variant: CatalogVariant) -> Path:
        assert self.catalog.revision is not None
        return self.root / ".staging" / self.catalog.revision / variant

    def _activation_path(self, variant: CatalogVariant) -> Path:
        return self.root / f"active-{variant}.json"

    def _catalog_identity(self, variant: CatalogVariant) -> str:
        return catalog_identity(self.catalog, variant)

    def _installed_dir(self, variant: CatalogVariant) -> Path | None:
        pointer = self._activation_path(variant)
        try:
            value = json.loads(pointer.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        if (
            value.get("schemaVersion") != 1
            or value.get("variant") != variant
            or value.get("repositoryId") != self.catalog.repository_id
            or value.get("revision") != self.catalog.revision
            or value.get("catalogIdentity") != self._catalog_identity(variant)
        ):
            return None
        relative = value.get("installation")
        if not isinstance(relative, str):
            return None
        installation_root = (self.root / "installations").resolve()
        candidate = (self.root / relative).resolve()
        if candidate == installation_root or installation_root not in candidate.parents:
            return None
        manifest_path = candidate / "installation.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, json.JSONDecodeError:
            return None
        if not isinstance(manifest, dict) or manifest.get("catalogIdentity") != self._catalog_identity(variant):
            return None
        descriptor = self.catalog.variants[variant]
        for artifact in descriptor.artifacts:
            try:
                path = _safe_artifact_path(candidate, artifact.relative_path)
            except LocalPolishingError:
                return None
            if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact.byte_size:
                return None
        return candidate

    def _verify_installation(self, variant: CatalogVariant, installation: Path) -> None:
        for artifact in self.catalog.variants[variant].artifacts:
            _verify_file(_safe_artifact_path(installation, artifact.relative_path), artifact)

    def state(self) -> LocalPolishingSnapshot:
        available = self.catalog.materialized
        message = None if available else "catalog_not_materialized"
        models: list[Mapping[str, object]] = []
        now = self._clock()
        for variant in self._variants:
            descriptor = self.catalog.variants[variant]
            installed = available and self._installed_dir(variant) is not None
            operation_id = self._active_operation_by_variant.get(variant)
            operation = self._operations.get(operation_id) if operation_id else None
            if operation is None:
                status = "ready" if installed else ("not_installed" if available else "unavailable")
                progress = 100.0 if installed else 0.0
                bytes_received = descriptor.total_bytes if installed else 0
                eta = None
                error_code = None
            else:
                operation_snapshot = operation.snapshot(now)
                status = operation_snapshot.status
                progress = operation_snapshot.progress
                bytes_received = operation_snapshot.bytes_received
                eta = operation_snapshot.eta_seconds
                error_code = operation_snapshot.error_code
            models.append(
                {
                    "variant": variant,
                    "name": descriptor.display_name,
                    "description": descriptor.description,
                    "status": status,
                    "installed": installed,
                    "active": self._runtime_variant == variant,
                    "runtimeReady": self._runtime_ready_by_variant[variant],
                    "runtimeError": self._runtime_error_by_variant[variant],
                    "operationId": operation.operation_id if operation else None,
                    "progress": progress,
                    "bytesReceived": bytes_received,
                    "bytesTotal": descriptor.total_bytes,
                    "etaSeconds": eta,
                    "message": error_code,
                    "errorCode": error_code,
                    "sizeBytes": descriptor.total_bytes,
                }
            )
        return LocalPolishingSnapshot(
            available=available,
            message=message,
            current_variant=self._runtime_variant,
            models=tuple(models),
        )

    async def install(self, variant: str) -> str:
        selected = self._require_variant(variant)
        self.catalog.require_materialized()
        if self._closed:
            raise LocalPolishingError("closed", "local polishing is closed")
        async with self._operation_lock:
            installed = self._installed_dir(selected)
            if installed is not None:
                try:
                    await asyncio.to_thread(self._verify_installation, selected, installed)
                except LocalPolishingError:
                    installed = None
            active_id = self._active_operation_by_variant.get(selected)
            if active_id is not None:
                active = self._operations[active_id]
                if (active.task is not None and not active.task.done()) or (
                    active.status == "ready" and installed is not None
                ):
                    return active_id
            if installed is not None:
                operation = _InstallOperation(
                    operation_id=uuid4().hex,
                    variant=selected,
                    status="ready",
                    bytes_received=self.catalog.variants[selected].total_bytes,
                    bytes_total=self.catalog.variants[selected].total_bytes,
                    started_at=self._clock(),
                    cancel_event=asyncio.Event(),
                )
                self._operations[operation.operation_id] = operation
                self._active_operation_by_variant[selected] = operation.operation_id
                return operation.operation_id
            operation = _InstallOperation(
                operation_id=uuid4().hex,
                variant=selected,
                status="downloading",
                bytes_received=0,
                bytes_total=self.catalog.variants[selected].total_bytes,
                started_at=self._clock(),
                cancel_event=asyncio.Event(),
            )
            self._operations[operation.operation_id] = operation
            self._active_operation_by_variant[selected] = operation.operation_id
            operation.task = asyncio.create_task(self._run_install(operation))
            return operation.operation_id

    async def _token(self) -> str | None:
        value = self._token_provider()
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    async def _run_install(self, operation: _InstallOperation) -> None:
        descriptor = self.catalog.variants[operation.variant]
        stage = self._stage_dir(operation.variant)
        completed = 0
        try:
            token = await self._token() if self.catalog.requires_token else None
            if self.catalog.requires_token and token is None:
                raise LocalPolishingError("credential_required", "a Hugging Face read credential is required")
            stage.mkdir(parents=True, exist_ok=True)
            staging_root = (self.root / ".staging").resolve()
            resolved_stage = stage.resolve()
            if stage.is_symlink() or staging_root not in resolved_stage.parents:
                raise LocalPolishingError("unsafe_staging_path", "model staging escaped its managed root")
            for artifact in descriptor.artifacts:
                if operation.cancel_event.is_set():
                    raise asyncio.CancelledError
                destination = _safe_artifact_path(stage, artifact.relative_path)
                if destination.exists():
                    try:
                        await asyncio.to_thread(_verify_file, destination, artifact)
                    except LocalPolishingError:
                        with suppress(OSError):
                            destination.unlink()
                    else:
                        completed += artifact.byte_size
                        operation.bytes_received = completed
                        continue

                async def _progress(received: int, _total: int, base: int = completed) -> None:
                    operation.bytes_received = min(operation.bytes_total, base + max(0, received))

                downloaded = await self._downloader.download(
                    repository_id=self.catalog.repository_id,
                    revision=self.catalog.revision or "",
                    filename=artifact.relative_path,
                    destination=stage,
                    token=token,
                    expected_size=artifact.byte_size,
                    progress=_progress,
                    cancel_requested=operation.cancel_event.is_set,
                )
                expected_path = destination.resolve()
                if downloaded.resolve() != expected_path:
                    raise LocalPolishingError("unsafe_download_path", "downloaded artifact escaped its staging path")
                await asyncio.to_thread(_verify_file, destination, artifact)
                completed += artifact.byte_size
                operation.bytes_received = completed

            if operation.cancel_event.is_set():
                raise asyncio.CancelledError
            operation.status = "verifying"
            for artifact in descriptor.artifacts:
                await asyncio.to_thread(
                    _verify_file,
                    _safe_artifact_path(stage, artifact.relative_path),
                    artifact,
                )
            metadata_dir = stage / ".cache"
            if metadata_dir.exists():
                if metadata_dir.is_symlink() or not metadata_dir.is_dir():
                    raise LocalPolishingError(
                        "unsafe_download_metadata", "download metadata is not a regular directory"
                    )
                await asyncio.to_thread(shutil.rmtree, metadata_dir)
            expected_files = {item.relative_path for item in descriptor.artifacts}
            observed_files: set[str] = set()
            for path in stage.rglob("*"):
                if path.is_symlink():
                    raise LocalPolishingError("artifact_not_regular", "model staging contains a symbolic link")
                if path.is_file():
                    observed_files.add(path.relative_to(stage).as_posix())
            if observed_files != expected_files:
                raise LocalPolishingError("unexpected_artifact", "model staging contains a file outside the catalog")
            identity = self._catalog_identity(operation.variant)
            _atomic_json(
                stage / "installation.json",
                {
                    "schemaVersion": 1,
                    "catalogIdentity": identity,
                    "repositoryId": self.catalog.repository_id,
                    "revision": self.catalog.revision,
                    "variant": operation.variant,
                },
            )
            if operation.cancel_event.is_set():
                raise asyncio.CancelledError

            final = (
                self.root
                / "installations"
                / (self.catalog.revision or "missing")
                / operation.variant
                / operation.operation_id
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            installations_root = (self.root / "installations").resolve()
            if installations_root not in final.resolve().parents:
                raise LocalPolishingError("unsafe_installation_path", "model installation escaped its managed root")
            os.replace(stage, final)
            relative_final = final.relative_to(self.root).as_posix()
            _atomic_json(
                self._activation_path(operation.variant),
                {
                    "schemaVersion": 1,
                    "catalogIdentity": identity,
                    "repositoryId": self.catalog.repository_id,
                    "revision": self.catalog.revision,
                    "variant": operation.variant,
                    "installation": relative_final,
                },
            )
            self._runtime_ready_by_variant[operation.variant] = False
            self._runtime_error_by_variant[operation.variant] = (
                None if self._runtime_factories else "runtime_unavailable"
            )
            operation.bytes_received = operation.bytes_total
            operation.status = "ready"
        except asyncio.CancelledError:
            operation.status = "cancelled"
            operation.error_code = "cancelled"
        except LocalPolishingError as error:
            operation.status = "error"
            operation.error_code = error.code
        except Exception:
            operation.status = "error"
            operation.error_code = "download_failed"

    async def wait_for_operation(self, operation_id: str) -> InstallOperationSnapshot:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise LocalPolishingError("unknown_operation", "local-polishing operation does not exist")
        if operation.task is not None:
            await asyncio.shield(operation.task)
        return operation.snapshot(self._clock())

    async def cancel(self, operation_id: str) -> InstallOperationSnapshot:
        async with self._operation_lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise LocalPolishingError("unknown_operation", "local-polishing operation does not exist")
            task = operation.task
            if task is not None and not task.done():
                operation.status = "cancelling"
                operation.cancel_event.set()
                # All shipping downloads are native async streams. Cancelling
                # their owning task therefore closes the socket immediately;
                # no detached worker continues after this method reports back.
                task.cancel()
        if task is not None and not task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        return operation.snapshot(self._clock())

    async def _close_runtime_locked(self) -> None:
        runtime = self._runtime
        self._runtime = None
        self._runtime_variant = None
        if runtime is not None:
            with suppress(Exception):
                await _close_runtime_resilient(runtime)

    async def _ensure_runtime_locked(self, variant: CatalogVariant, installation: Path) -> GenerationRuntime:
        if self._runtime is not None and self._runtime_variant == variant:
            return self._runtime
        await self._close_runtime_locked()
        descriptor = self.catalog.variants[variant]
        assert descriptor.model_relative_path is not None
        model_artifact = descriptor.artifact(descriptor.model_relative_path)
        model_path = _safe_artifact_path(installation, descriptor.model_relative_path)
        await asyncio.to_thread(_verify_file, model_path, model_artifact)
        if not self._runtime_factories:
            self._runtime_ready_by_variant[variant] = False
            self._runtime_error_by_variant[variant] = "runtime_unavailable"
            raise LocalPolishingError("runtime_unavailable", "no verified local-polishing runtime is installed")
        for factory in self._runtime_factories:
            candidate: GenerationRuntime | None = None
            try:
                candidate = await factory.create(model_path=model_path, model_sha256=model_artifact.sha256)
                if descriptor.prompt_contract == "chat_template_v1":
                    properties = await candidate.properties()
                    chat_template = properties.get("chat_template")
                    template_payload = json.dumps(
                        chat_template,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    if hashlib.sha256(template_payload).hexdigest() != descriptor.chat_template_sha256:
                        await _close_runtime_resilient(candidate)
                        raise LocalPolishingError(
                            "template_hash_mismatch", "GGUF chat template differs from the catalog"
                        )
                self._runtime = candidate
                self._runtime_variant = variant
                self._runtime_ready_by_variant[variant] = True
                self._runtime_error_by_variant[variant] = None
                return candidate
            except asyncio.CancelledError:
                self._runtime_ready_by_variant[variant] = False
                self._runtime_error_by_variant[variant] = None
                if candidate is not None:
                    with suppress(Exception):
                        await _close_runtime_resilient(candidate)
                raise
            except LocalPolishingError as error:
                self._runtime_ready_by_variant[variant] = False
                self._runtime_error_by_variant[variant] = error.code
                raise
            except Exception:
                if candidate is not None:
                    with suppress(Exception):
                        await _close_runtime_resilient(candidate)
                continue
        self._runtime_ready_by_variant[variant] = False
        self._runtime_error_by_variant[variant] = "runtime_unavailable"
        raise LocalPolishingError("runtime_unavailable", "all verified local-polishing runtimes failed")

    async def prewarm(self, variant: str) -> bool:
        selected = self._require_variant(variant)
        installation = self._installed_dir(selected)
        if installation is None:
            return False
        async with self._runtime_lock:
            try:
                await self._ensure_runtime_locked(selected, installation)
            except LocalPolishingError as error:
                self._runtime_ready_by_variant[selected] = False
                self._runtime_error_by_variant[selected] = error.code
                return False
            except Exception:
                self._runtime_ready_by_variant[selected] = False
                self._runtime_error_by_variant[selected] = "runtime_unavailable"
                return False
        return True

    async def polish(self, transcript: str, variant: str) -> PolishOutcome:
        started = self._clock()
        selected: CatalogVariant | None = None
        backend: str | None = None

        def _fallback(code: str) -> PolishOutcome:
            return PolishOutcome(
                text=transcript,
                variant=selected,
                status="original_fallback",
                reason_codes=(code,),
                duration_ms=max(0.0, (self._clock() - started) * 1000),
                runtime_backend=backend,
            )

        if not transcript.strip():
            return _fallback("empty_transcript")
        try:
            selected = self._require_variant(variant)
            self.catalog.require_materialized()
            installation = self._installed_dir(selected)
            if installation is None:
                return _fallback("model_not_installed")
            descriptor = self.catalog.variants[selected]
            assert descriptor.protection_policy_relative_path is not None
            policy_artifact = descriptor.artifact(descriptor.protection_policy_relative_path)
            policy_path = _safe_artifact_path(installation, descriptor.protection_policy_relative_path)
            await asyncio.to_thread(_verify_file, policy_path, policy_artifact)
            markers = await asyncio.to_thread(load_policy_markers, policy_path)
            # The production LFM was trained and evaluated on the raw source
            # transcript.  KEEP markers belong only to the retired SST/chat
            # contract; injecting them into plain_completion_v1 would create
            # an untrained prompt distribution and change the exact task input.
            protected = (
                protect_transcript(transcript, markers) if descriptor.prompt_contract == "chat_template_v1" else None
            )
        except SafetyError as error:
            return _fallback(error.code)
        except LocalPolishingError as error:
            return _fallback(error.code)
        except Exception:
            return _fallback("preflight_failed")

        async with self._runtime_lock:
            self._inference_count += 1
            try:
                completion: CompletionResult | None = None
                if descriptor.prompt_contract == "plain_completion_v1":
                    if descriptor.generation_max_new_tokens is None:
                        return _fallback("prompt_contract_error")
                    max_new_tokens = descriptor.generation_max_new_tokens
                else:
                    # Preserve schema-1 Gemma's existing length-derived budget.
                    max_new_tokens = min(4096, max(128, len(transcript) // 2 + 128))
                for attempt in range(2):
                    try:
                        runtime = await self._ensure_runtime_locked(selected, installation)
                        backend = runtime.backend_name
                        if descriptor.prompt_contract == "chat_template_v1":
                            if protected is None:
                                return _fallback("prompt_contract_error")
                            prompt = await runtime.apply_template(
                                [{"role": "user", "content": f"{POLISHING_INSTRUCTION}{protected.text}"}]
                            )
                        else:
                            try:
                                prompt = descriptor.render_plain_completion_prompt(transcript)
                            except CatalogError:
                                return _fallback("prompt_contract_error")
                        completion = await runtime.complete(
                            prompt,
                            max_new_tokens=max_new_tokens,
                        )
                        break
                    except LocalPolishingError as error:
                        self._runtime_ready_by_variant[selected] = False
                        self._runtime_error_by_variant[selected] = error.code
                        return _fallback(error.code)
                    except Exception:
                        await self._close_runtime_locked()
                        if attempt == 1:
                            self._runtime_ready_by_variant[selected] = False
                            self._runtime_error_by_variant[selected] = "generation_error"
                            return _fallback("generation_error")
                if completion is None:
                    return _fallback("generation_error")
                if not isinstance(completion, CompletionResult):
                    return _fallback("generation_error")
                if completion.prompt_truncated:
                    return _fallback("prompt_truncated")
                if completion.finish_reason == "limit" or completion.tokens_predicted >= max_new_tokens:
                    return _fallback("token_budget_exhausted")
                if not completion.stop_reached:
                    return _fallback("generation_incomplete")
                try:
                    # Preserve the legacy runtime's outer-whitespace behavior;
                    # plain_text_v1 still performs its own structural checks.
                    if descriptor.output_contract == "plain_text_v1":
                        output = render_plain_text(completion.content.strip())
                    else:
                        if protected is None:
                            return _fallback("prompt_contract_error")
                        restored = restore_protected_values(protected, completion.content.strip())
                        output = render_plain_sst(restored)
                    output = _validate_or_repair_numeric_anchors(transcript, output)
                except SafetyError as error:
                    return _fallback(error.code)
                return PolishOutcome(
                    text=output,
                    variant=selected,
                    status="accepted",
                    reason_codes=(),
                    duration_ms=max(0.0, (self._clock() - started) * 1000),
                    runtime_backend=backend,
                )
            finally:
                self._inference_count -= 1

    async def unload(self, variant: str | None = None) -> None:
        selected = self._require_variant(variant) if variant is not None else None
        async with self._runtime_lock:
            if selected is None or self._runtime_variant == selected:
                await self._close_runtime_locked()

    async def remove(self, variant: str) -> None:
        selected = self._require_variant(variant)
        async with self._operation_lock:
            active_id = self._active_operation_by_variant.get(selected)
            operation = self._operations.get(active_id) if active_id else None
            if operation is not None and operation.task is not None and not operation.task.done():
                raise LocalPolishingError("model_busy", "model installation is currently active")
            async with self._runtime_lock:
                if self._runtime_variant == selected or self._inference_count > 0:
                    raise LocalPolishingError("model_in_use", "model is currently loaded or in use")
                installation = self._installed_dir(selected)
                pointer = self._activation_path(selected)
                if installation is not None:
                    install_root = (self.root / "installations").resolve()
                    resolved = installation.resolve()
                    if install_root not in resolved.parents:
                        raise LocalPolishingError(
                            "unsafe_installation_path", "model installation escaped its managed root"
                        )
                    await asyncio.to_thread(shutil.rmtree, resolved)
                pointer.unlink(missing_ok=True)
                self._active_operation_by_variant.pop(selected, None)
                self._runtime_ready_by_variant[selected] = False
                self._runtime_error_by_variant[selected] = None if self._runtime_factories else "runtime_unavailable"
                if self.catalog.revision is not None:
                    stage = self._stage_dir(selected)
                    if stage.exists():
                        stage_root = (self.root / ".staging").resolve()
                        resolved_stage = stage.resolve()
                        if stage_root not in resolved_stage.parents:
                            raise LocalPolishingError("unsafe_staging_path", "model staging escaped its managed root")
                        await asyncio.to_thread(shutil.rmtree, resolved_stage)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        for operation in self._operations.values():
            if operation.task is not None and not operation.task.done():
                operation.cancel_event.set()
                operation.task.cancel()
                tasks.append(operation.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._runtime_lock:
            await self._close_runtime_locked()
