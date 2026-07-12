"""Optional local speaker diarization through Scriber's isolated Rust worker.

The statically linked worker is versioned with the signed Scriber application.
Only checksum-pinned models and their license notices are downloaded into
``SCRIBER_DATA_DIR``.  Python never loads Sherpa, ONNX Runtime, PyTorch, or
Pyannote for this feature.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from aiohttp import ClientSession

from src.provider_transcript import group_provider_words, normalize_provider_words
from src.runtime.paths import app_root, data_dir, is_frozen, repo_root
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs


SHERPA_VERSION = "1.13.3"
WORKER_NAME = "scriber-diarization-sidecar"
WORKER_FILE = f"{WORKER_NAME}.exe"
WORKER_MANIFEST_FILE = f"{WORKER_NAME}.manifest.json"
WORKER_VERSION = "0.1.0"
WORKER_PROTOCOL_SCHEMA = 1
WORKER_MANIFEST_SCHEMA = 1
SEGMENTATION_MODEL_ID = "pyannote-segmentation-3.0-int8"
EMBEDDING_MODEL_ID = "3d-speaker-eres2net-base-16k"
SEGMENTATION_ARCHIVE = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"speaker-segmentation-models/{SEGMENTATION_ARCHIVE}"
)
SEGMENTATION_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
EMBEDDING_FILE = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    f"speaker-recongition-models/{EMBEDDING_FILE}"
)
EMBEDDING_SHA256 = "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b"
COMPONENT_SCHEMA = 2
COMPONENT_NAME = "scriber-local-speaker-diarization"
COMPONENT_SOURCES = {
    "segmentation": {
        "url": SEGMENTATION_URL,
        "archiveSha256": SEGMENTATION_SHA256,
        "license": "MIT",
    },
    "embedding": {
        "artifactUrl": EMBEDDING_URL,
        "artifactSha256": EMBEDDING_SHA256,
        "modelCardUrl": (
            "https://modelscope.cn/models/iic/"
            "speech_eres2net_base_sv_zh-cn_3dspeaker_16k"
        ),
        "modelRevision": "v1.0.1",
        "repositoryCommit": "46215101b5c2ca4443163c8ced56147cc6f01908",
        "declaredLicense": "Apache-2.0",
        "declaredTraining": "3D-Speaker; approximately 10,000 speakers; 16 kHz Chinese audio",
    },
}
MAX_DURATION_MS = 2 * 60 * 60 * 1000
PRODUCT_MAX_DURATION_MS = 60 * 60 * 1000
MAX_RESIDENT_BYTES = 1024 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_WORKER_DIAGNOSTIC_BYTES = 64 * 1024
MAX_WORKER_TURNS = 100_000
MAX_SEGMENTATION_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_EMBEDDING_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_MODEL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64

_SEGMENTATION_LICENSE_NAME = "PYANNOTE_SEGMENTATION_LICENSE.txt"
_APACHE_LICENSE_NAME = "APACHE-2.0.txt"
_EMBEDDING_NOTICE_NAME = "ERES2NET_MODEL_NOTICE.txt"
_WORKER_LICENSE_NAME = "SCRIBER_DIARIZATION_WORKER_LICENSE.txt"
_EXPECTED_ARTIFACT_PATHS = {
    "segmentation-model": "models/pyannote-segmentation-3.0.int8.onnx",
    "embedding-model": f"models/{EMBEDDING_FILE}",
    "segmentation-license": f"licenses/{_SEGMENTATION_LICENSE_NAME}",
    "embedding-license": f"licenses/{_APACHE_LICENSE_NAME}",
    "embedding-provenance": f"licenses/{_EMBEDDING_NOTICE_NAME}",
    "worker-license": f"licenses/{_WORKER_LICENSE_NAME}",
}


@dataclass(frozen=True)
class DiarizationTurn:
    start_ms: int
    end_ms: int
    speaker: int


@dataclass(frozen=True)
class WorkerDescriptor:
    executable: Path
    sha256: str
    byte_size: int
    source: str
    version: str


def normalize_turn_speakers(turns: Iterable[DiarizationTurn]) -> list[DiarizationTurn]:
    """Renumber anonymous clusters by chronological first appearance."""
    ordered = sorted(turns, key=lambda item: (item.start_ms, item.end_ms, item.speaker))
    labels: dict[int, int] = {}
    return [
        DiarizationTurn(
            item.start_ms,
            item.end_ms,
            labels.setdefault(item.speaker, len(labels)),
        )
        for item in ordered
    ]


ProgressCallback = Callable[[str, float], Awaitable[None] | None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(archive, "r:bz2") as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Sherpa archive contains too many entries.")
        expanded_bytes = 0
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination_resolved and destination_resolved not in target.parents:
                raise ValueError("Sherpa archive contains an unsafe path.")
            if member.issym() or member.islnk():
                raise ValueError("Sherpa archive contains an unsupported link.")
            if not (member.isfile() or member.isdir()):
                raise ValueError("Sherpa archive contains an unsupported entry.")
            if member.isfile():
                if member.size < 0 or member.size > MAX_EXTRACTED_MODEL_BYTES:
                    raise ValueError("Sherpa archive entry exceeds the size limit.")
                expanded_bytes += member.size
                if expanded_bytes > MAX_EXTRACTED_MODEL_BYTES:
                    raise ValueError("Sherpa archive exceeds the extracted size limit.")
        bundle.extractall(destination, members=members)


def _best_turn(start_ms: int, end_ms: int, turns: Iterable[DiarizationTurn]) -> DiarizationTurn | None:
    midpoint = start_ms + max(0, end_ms - start_ms) // 2
    best: tuple[int, int, DiarizationTurn] | None = None
    for turn in turns:
        overlap = max(0, min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms))
        distance = 0 if turn.start_ms <= midpoint <= turn.end_ms else min(
            abs(midpoint - turn.start_ms), abs(midpoint - turn.end_ms)
        )
        candidate = (overlap, -distance, turn)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best is not None else None


def align_words_to_speakers(
    words: list[dict[str, Any]], turns: list[DiarizationTurn], *, source: str = "system"
) -> list[dict[str, Any]]:
    """Attach local speaker turns to exact provider-timed words."""
    aligned: list[dict[str, Any]] = []
    for word in words:
        item = dict(word)
        turn = _best_turn(int(item.get("startMs", 0)), int(item.get("endMs", 0)), turns)
        item["speaker"] = f"Speaker {(turn.speaker if turn else 0) + 1}"
        aligned.append(item)
    concatenate = bool(aligned and aligned[0].get("concatenate"))
    return group_provider_words(aligned, source, 0, concatenate=concatenate)


def distribute_text_over_turns(
    text: str, turns: list[DiarizationTurn], *, source: str = "system"
) -> list[dict[str, Any]]:
    """Fallback for STT providers that return text without word timestamps."""
    tokens = re.findall(r"\S+", text)
    usable = [turn for turn in turns if turn.end_ms > turn.start_ms]
    if not tokens or not usable:
        return []
    total_duration = sum(turn.end_ms - turn.start_ms for turn in usable)
    cursor = 0
    segments: list[dict[str, Any]] = []
    for index, turn in enumerate(usable):
        remaining_turns = len(usable) - index
        if index == len(usable) - 1:
            take = len(tokens) - cursor
        else:
            proportional = round(len(tokens) * (turn.end_ms - turn.start_ms) / total_duration)
            take = max(1, min(len(tokens) - cursor - (remaining_turns - 1), proportional))
        if take <= 0:
            continue
        block = " ".join(tokens[cursor:cursor + take]).strip()
        cursor += take
        if not block:
            continue
        segments.append({
            "revision": "canonical",
            "source": source,
            "providerSegmentId": f"local-diarization-{index}",
            "speakerLabel": f"Speaker {turn.speaker + 1}",
            "startMs": turn.start_ms,
            "endMs": turn.end_ms,
            "text": block,
            "confidence": None,
            "alignmentQuality": "estimated",
            "isFinal": True,
        })
        if cursor >= len(tokens):
            break
    return segments


def format_speaker_transcript(segments: list[dict[str, Any]]) -> str:
    blocks: list[tuple[str, list[str]]] = []
    for segment in segments:
        label = str(segment.get("speakerLabel") or "Speaker 1")
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        if blocks and blocks[-1][0] == label:
            blocks[-1][1].append(text)
        else:
            blocks.append((label, [text]))
    return "\n".join(f"[{label}]: {' '.join(parts)}" for label, parts in blocks)


class _ComponentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiarizationIneligibleError(RuntimeError):
    """The local route is safely skipped; STT output must remain usable."""


class SherpaOnnxDiarizer:
    """Manifest-verified optional models plus a bundled static Rust worker."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        worker_executable: Path | None = None,
        worker_manifest_path: Path | None = None,
    ) -> None:
        self.root = root or data_dir() / "models" / f"sherpa-onnx-diarization-{SHERPA_VERSION}"
        self.model_dir = self.root / "models"
        self.license_dir = self.root / "licenses"
        self.segmentation_model = self.model_dir / "pyannote-segmentation-3.0.int8.onnx"
        self.embedding_model = self.model_dir / EMBEDDING_FILE
        self.manifest_path = self.root / "component.json"
        self._worker_override = Path(worker_executable).resolve() if worker_executable else None
        self._worker_manifest_override = (
            Path(worker_manifest_path).resolve() if worker_manifest_path else None
        )
        self._install_lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._activity_lock = asyncio.Lock()
        self._active_jobs = 0
        self._deleting = False
        self._verified_signature: tuple[Any, ...] | None = None
        self._verified_worker: WorkerDescriptor | None = None
        self._cached_status = self._base_status(
            installed=False,
            verification_state="pending",
            reason="verification_pending",
        )

    def _base_status(
        self,
        *,
        installed: bool,
        verification_state: str,
        reason: str | None,
        byte_size: int = 0,
        worker: WorkerDescriptor | None = None,
    ) -> dict[str, Any]:
        return {
            "available": os.name == "nt",
            "installed": installed,
            "verificationState": verification_state,
            "reason": reason,
            "engine": "sherpa-onnx",
            "version": SHERPA_VERSION,
            "worker": WORKER_NAME,
            "workerVersion": worker.version if worker else WORKER_VERSION,
            "workerReady": worker is not None,
            "workerSource": worker.source if worker else None,
            "workerByteSize": worker.byte_size if worker else 0,
            "segmentationModel": SEGMENTATION_MODEL_ID,
            "embeddingModel": "3D-Speaker ERes2Net",
            "byteSize": byte_size,
            "license": "Model and worker license notices are stored with the component",
            "distribution": "bundled-worker-and-optional-models",
            "activeJobs": self._active_jobs,
            "maxEligibleDurationMs": PRODUCT_MAX_DURATION_MS,
        }

    def status(self) -> dict[str, Any]:
        """Return the last verified snapshot without hashing on the caller thread."""
        status = dict(self._cached_status)
        status["activeJobs"] = self._active_jobs
        return status

    async def status_async(self, *, force: bool = False) -> dict[str, Any]:
        """Verify worker, models, and licenses off the aiohttp event loop."""
        async with self._status_lock:
            status = await asyncio.to_thread(self._verify_status_sync, force)
            status["activeJobs"] = self._active_jobs
            return status

    async def is_installed(self) -> bool:
        return bool((await self.status_async()).get("installed"))

    def _verify_status_sync(self, force: bool = False) -> dict[str, Any]:
        signature = self._filesystem_signature()
        if not force and signature == self._verified_signature:
            return dict(self._cached_status)
        worker: WorkerDescriptor | None = None
        try:
            worker = self._resolve_worker_sync()
            self._verified_worker = worker
            if not self.manifest_path.is_file():
                status = self._base_status(
                    installed=False,
                    verification_state="verified",
                    reason="models_not_installed",
                    worker=worker,
                )
            else:
                byte_size = self._verify_component_manifest_sync(worker)
                status = self._base_status(
                    installed=True,
                    verification_state="verified",
                    reason=None,
                    byte_size=byte_size,
                    worker=worker,
                )
        except _ComponentError as exc:
            self._verified_worker = None
            status = self._base_status(
                installed=False,
                verification_state="failed",
                reason=exc.code,
                worker=worker,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            self._verified_worker = None
            status = self._base_status(
                installed=False,
                verification_state="failed",
                reason="component_verification_failed",
                worker=worker,
            )
        self._verified_signature = signature
        self._cached_status = status
        return dict(status)

    @staticmethod
    def _stat_signature(path: Path) -> tuple[Any, ...]:
        try:
            info = path.stat()
            return (str(path), info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0))
        except OSError:
            return (str(path), None)

    def _filesystem_signature(self) -> tuple[Any, ...]:
        paths = [
            self.manifest_path,
            *[self.root / relative for relative in _EXPECTED_ARTIFACT_PATHS.values()],
        ]
        for executable, manifest in self._worker_candidates():
            paths.extend((executable, manifest))
        return tuple(self._stat_signature(path) for path in paths)

    def _worker_candidates(self) -> list[tuple[Path, Path]]:
        candidates: list[tuple[Path, Path]] = []

        def add(executable: Path, manifest: Path | None = None) -> None:
            executable = executable.expanduser().resolve()
            manifest = (manifest or executable.with_name(WORKER_MANIFEST_FILE)).expanduser().resolve()
            pair = (executable, manifest)
            if pair not in candidates:
                candidates.append(pair)

        if self._worker_override is not None:
            add(self._worker_override, self._worker_manifest_override)
            return candidates
        if is_frozen():
            # Tauri maps target/release/backend to resources/backend. The worker
            # is a backend tool, never a Tauri external binary or linked crate.
            add(app_root() / "tools" / "diarization" / WORKER_FILE)
            return candidates
        explicit = os.getenv("SCRIBER_DIARIZATION_WORKER_EXE", "").strip()
        if explicit:
            candidate = Path(explicit).expanduser()
            if candidate.is_absolute() and candidate.name.casefold() == WORKER_FILE.casefold():
                add(candidate)
        crate_root = repo_root() / "native" / WORKER_NAME
        add(crate_root / "target" / "release" / WORKER_FILE)
        add(crate_root / "target" / "debug" / WORKER_FILE)
        return candidates

    def _resolve_worker_sync(self) -> WorkerDescriptor:
        saw_candidate = False
        last_error: _ComponentError | None = None
        for executable, manifest_path in self._worker_candidates():
            if not executable.is_file():
                continue
            saw_candidate = True
            try:
                if is_frozen() and self._worker_override is None and not manifest_path.is_file():
                    raise _ComponentError(
                        "worker_manifest_missing",
                        "The bundled diarization worker manifest is missing.",
                    )
                if manifest_path.is_file():
                    descriptor = self._descriptor_from_worker_manifest(executable, manifest_path)
                elif not is_frozen():
                    descriptor = WorkerDescriptor(
                        executable=executable,
                        sha256=_sha256(executable),
                        byte_size=executable.stat().st_size,
                        source="local-dev",
                        version=WORKER_VERSION,
                    )
                else:
                    raise _ComponentError(
                        "worker_manifest_missing",
                        "The bundled diarization worker manifest is missing.",
                    )
                self._probe_worker_sync(descriptor)
                return descriptor
            except (OSError, ValueError, json.JSONDecodeError, _ComponentError) as exc:
                last_error = exc if isinstance(exc, _ComponentError) else _ComponentError(
                    "worker_verification_failed",
                    "The bundled diarization worker could not be verified.",
                )
        if last_error is not None:
            raise last_error
        raise _ComponentError(
            "worker_unavailable" if not saw_candidate else "worker_verification_failed",
            "The bundled diarization worker is unavailable.",
        )

    def _descriptor_from_worker_manifest(
        self, executable: Path, manifest_path: Path
    ) -> WorkerDescriptor:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise _ComponentError(
                "worker_manifest_invalid", "The bundled diarization worker manifest is invalid."
            )
        worker = manifest.get("worker")
        distribution = manifest.get("distribution")
        if (
            manifest.get("schemaVersion") != WORKER_MANIFEST_SCHEMA
            or not isinstance(worker, dict)
            or worker.get("name") != WORKER_NAME
            or worker.get("fileName") != WORKER_FILE
            or worker.get("version") != WORKER_VERSION
            or worker.get("protocolSchemaVersion") != WORKER_PROTOCOL_SCHEMA
            or worker.get("sherpaOnnxVersion") != SHERPA_VERSION
            or worker.get("linkMode") != "static"
            or (
                is_frozen()
                and self._worker_override is None
                and distribution != "bundled-signed-scriber-resource"
            )
        ):
            raise _ComponentError(
                "worker_manifest_invalid", "The bundled diarization worker manifest is invalid."
            )
        digest = str(worker.get("sha256") or "").lower()
        byte_size = worker.get("byteSize")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(byte_size, int):
            raise _ComponentError(
                "worker_manifest_invalid", "The bundled diarization worker manifest is invalid."
            )
        if byte_size <= 0 or executable.stat().st_size != byte_size or _sha256(executable) != digest:
            raise _ComponentError(
                "worker_hash_mismatch", "The bundled diarization worker failed verification."
            )
        return WorkerDescriptor(
            executable=executable,
            sha256=digest,
            byte_size=byte_size,
            source=str(distribution or "bundled"),
            version=WORKER_VERSION,
        )

    def _probe_worker_sync(self, descriptor: WorkerDescriptor) -> None:
        def control(argument: str) -> dict[str, Any]:
            try:
                process = subprocess.run(
                    [str(descriptor.executable), argument],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise _ComponentError(
                    "worker_self_test_failed", "The bundled diarization worker did not start."
                ) from exc
            if (
                process.returncode != 0
                or len(process.stdout) > MAX_WORKER_DIAGNOSTIC_BYTES
                or len(process.stderr) > MAX_WORKER_DIAGNOSTIC_BYTES
            ):
                raise _ComponentError(
                    "worker_self_test_failed", "The bundled diarization worker self-test failed."
                )
            try:
                payload = json.loads(process.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _ComponentError(
                    "worker_self_test_failed", "The bundled diarization worker self-test failed."
                ) from exc
            if not isinstance(payload, dict):
                raise _ComponentError(
                    "worker_self_test_failed", "The bundled diarization worker self-test failed."
                )
            return payload

        version = control("--version")
        if (
            version.get("schemaVersion") != WORKER_PROTOCOL_SCHEMA
            or version.get("ok") is not True
            or version.get("worker", {}).get("name") != WORKER_NAME
            or version.get("worker", {}).get("version") != WORKER_VERSION
            or version.get("engine", {}).get("name") != "sherpa-onnx"
            or version.get("engine", {}).get("version") != SHERPA_VERSION
            or version.get("engine", {}).get("linkMode") != "static"
        ):
            raise _ComponentError(
                "worker_self_test_failed", "The bundled diarization worker self-test failed."
            )
        self_test = control("--self-test")
        if (
            self_test.get("schemaVersion") != WORKER_PROTOCOL_SCHEMA
            or self_test.get("ok") is not True
            or self_test.get("loadsUserAudio") is not False
            or self_test.get("loadsModels") is not False
            or self_test.get("platform", {}).get("windows") is not True
            or self_test.get("platform", {}).get("memoryLimit") != "jobObject"
        ):
            raise _ComponentError(
                "worker_self_test_failed", "The bundled diarization worker self-test failed."
            )

    def _verify_component_manifest_sync(self, worker: WorkerDescriptor) -> int:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _ComponentError(
                "component_manifest_invalid", "The diarization component manifest is invalid."
            ) from exc
        if not isinstance(manifest, dict):
            raise _ComponentError(
                "component_manifest_invalid", "The diarization component manifest is invalid."
            )
        worker_record = manifest.get("worker")
        artifacts = manifest.get("artifacts")
        if (
            manifest.get("schemaVersion") != COMPONENT_SCHEMA
            or manifest.get("component") != COMPONENT_NAME
            or manifest.get("sherpaOnnxVersion") != SHERPA_VERSION
            or not isinstance(worker_record, dict)
            or worker_record.get("name") != WORKER_NAME
            or worker_record.get("version") != worker.version
            or worker_record.get("protocolSchemaVersion") != WORKER_PROTOCOL_SCHEMA
            or worker_record.get("sha256") != worker.sha256
            or worker_record.get("byteSize") != worker.byte_size
            or worker_record.get("distribution") != worker.source
            or manifest.get("sources") != COMPONENT_SOURCES
            or not isinstance(artifacts, list)
        ):
            raise _ComponentError(
                "component_manifest_invalid", "The diarization component manifest is invalid."
            )
        by_role = {
            item.get("role"): item
            for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        }
        if set(by_role) != set(_EXPECTED_ARTIFACT_PATHS) or len(artifacts) != len(by_role):
            raise _ComponentError(
                "component_manifest_invalid", "The diarization component manifest is invalid."
            )
        root = self.root.resolve()
        total = self.manifest_path.stat().st_size
        for role, expected_relative in _EXPECTED_ARTIFACT_PATHS.items():
            record = by_role[role]
            relative = record.get("relativePath")
            digest = str(record.get("sha256") or "").lower()
            byte_size = record.get("byteSize")
            if (
                relative != expected_relative
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(byte_size, int)
                or byte_size <= 0
            ):
                raise _ComponentError(
                    "component_manifest_invalid", "The diarization component manifest is invalid."
                )
            path = (self.root / relative).resolve()
            if path == root or root not in path.parents or not path.is_file():
                raise _ComponentError(
                    "component_artifact_missing", "A diarization component artifact is missing."
                )
            if path.stat().st_size != byte_size or _sha256(path) != digest:
                raise _ComponentError(
                    "component_hash_mismatch", "A diarization component artifact failed verification."
                )
            total += byte_size
        return total

    async def _report(self, callback: ProgressCallback | None, label: str, amount: float) -> None:
        if callback is None:
            return
        result = callback(label, amount)
        if asyncio.iscoroutine(result):
            await result

    async def _download(
        self,
        session: ClientSession,
        url: str,
        destination: Path,
        expected_sha256: str,
        *,
        max_bytes: int,
    ) -> None:
        digest = hashlib.sha256()
        received = 0
        try:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                content_length = response.content_length
                if content_length is not None and (content_length < 0 or content_length > max_bytes):
                    raise ValueError("Downloaded diarization artifact exceeds the size limit.")
                with destination.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        received += len(chunk)
                        if received > max_bytes:
                            raise ValueError("Downloaded diarization artifact exceeds the size limit.")
                        handle.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("Downloaded diarization artifact failed SHA-256 verification.")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _license_asset(name: str) -> Path:
        return Path(__file__).resolve().parent / "assets" / "licenses" / name

    @staticmethod
    def _artifact_entry(root: Path, role: str, relative_path: str) -> dict[str, Any]:
        path = root / relative_path
        return {
            "role": role,
            "relativePath": relative_path,
            "sha256": _sha256(path),
            "byteSize": path.stat().st_size,
        }

    def _assemble_component_sync(
        self, staging: Path, extracted: Path, downloads: Path, worker: WorkerDescriptor
    ) -> None:
        models_out = staging / "models"
        licenses_out = staging / "licenses"
        models_out.mkdir(parents=True, exist_ok=True)
        licenses_out.mkdir(parents=True, exist_ok=True)
        segmentation_source = next(
            (path for path in extracted.rglob("model.int8.onnx") if path.is_file()), None
        )
        segmentation_license = next(
            (path for path in extracted.rglob("LICENSE") if path.is_file()), None
        )
        if segmentation_source is None or segmentation_license is None:
            raise ValueError("The segmentation model archive is incomplete.")
        shutil.copy2(segmentation_source, models_out / self.segmentation_model.name)
        shutil.copy2(downloads / EMBEDDING_FILE, models_out / EMBEDDING_FILE)
        shutil.copy2(segmentation_license, licenses_out / _SEGMENTATION_LICENSE_NAME)
        for name in (_APACHE_LICENSE_NAME, _EMBEDDING_NOTICE_NAME, _WORKER_LICENSE_NAME):
            source = self._license_asset(name)
            if not source.is_file():
                raise ValueError("A required diarization license notice is unavailable.")
            shutil.copy2(source, licenses_out / name)
        manifest = {
            "schemaVersion": COMPONENT_SCHEMA,
            "component": COMPONENT_NAME,
            "sherpaOnnxVersion": SHERPA_VERSION,
            "worker": {
                "name": WORKER_NAME,
                "version": worker.version,
                "protocolSchemaVersion": WORKER_PROTOCOL_SCHEMA,
                "sha256": worker.sha256,
                "byteSize": worker.byte_size,
                "distribution": worker.source,
            },
            "artifacts": [
                self._artifact_entry(staging, role, relative)
                for role, relative in _EXPECTED_ARTIFACT_PATHS.items()
            ],
            "sources": COMPONENT_SOURCES,
        }
        (staging / "component.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    async def install(
        self, session: ClientSession, progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        if os.name != "nt":
            raise RuntimeError("Local Sherpa diarization is currently Windows-only.")
        async with self._install_lock:
            current = await self.status_async()
            if current["installed"]:
                return current
            async with self._activity_lock:
                if self._active_jobs or self._deleting:
                    raise RuntimeError("Local speaker separation is currently in use.")
            worker = await asyncio.to_thread(self._resolve_worker_sync)
            await asyncio.to_thread(self.root.parent.mkdir, parents=True, exist_ok=True)
            staging = self.root.with_name(f".{self.root.name}.installing")
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging, True)
            await asyncio.to_thread(staging.mkdir, parents=True)
            try:
                downloads = staging / "downloads"
                extracted = staging / "extracted"
                await asyncio.to_thread(downloads.mkdir, parents=True)
                await asyncio.to_thread(extracted.mkdir, parents=True)
                artifacts = (
                    (
                        SEGMENTATION_URL,
                        downloads / SEGMENTATION_ARCHIVE,
                        SEGMENTATION_SHA256,
                        "Downloading segmentation model",
                        0.16,
                        MAX_SEGMENTATION_DOWNLOAD_BYTES,
                    ),
                    (
                        EMBEDDING_URL,
                        downloads / EMBEDDING_FILE,
                        EMBEDDING_SHA256,
                        "Downloading speaker embedding model",
                        0.54,
                        MAX_EMBEDDING_DOWNLOAD_BYTES,
                    ),
                )
                for url, path, digest, label, amount, max_bytes in artifacts:
                    await self._report(progress, label, amount)
                    await self._download(session, url, path, digest, max_bytes=max_bytes)
                await self._report(progress, "Verifying local speaker separation", 0.82)
                await asyncio.to_thread(_safe_extract, downloads / SEGMENTATION_ARCHIVE, extracted)
                await asyncio.to_thread(
                    self._assemble_component_sync, staging, extracted, downloads, worker
                )
                await asyncio.to_thread(shutil.rmtree, downloads)
                await asyncio.to_thread(shutil.rmtree, extracted)
                if self.root.exists():
                    await asyncio.to_thread(shutil.rmtree, self.root)
                await asyncio.to_thread(staging.replace, self.root)
                self._verified_signature = None
                await self._report(progress, "Local speaker separation ready", 1.0)
                return await self.status_async(force=True)
            except BaseException:
                if staging.exists():
                    await asyncio.to_thread(shutil.rmtree, staging, True)
                raise

    def _delete_sync(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self._verified_signature = None
        self._verified_worker = None
        self._cached_status = self._base_status(
            installed=False,
            verification_state="pending",
            reason="verification_pending",
        )

    def delete(self) -> None:
        """Synchronous test/maintenance path; REST callers use ``delete_async``."""
        if self._active_jobs or self._deleting:
            raise RuntimeError("Local speaker separation is currently in use.")
        self._delete_sync()

    async def delete_async(self) -> bool:
        """Delete only after atomically proving no diarization job owns files."""
        async with self._install_lock:
            async with self._status_lock:
                async with self._activity_lock:
                    if self._active_jobs or self._deleting:
                        return False
                    self._deleting = True
                deletion = asyncio.create_task(asyncio.to_thread(self._delete_sync))
                try:
                    await asyncio.shield(deletion)
                except asyncio.CancelledError:
                    # Keep the ownership barrier closed until the filesystem
                    # worker has actually finished; to_thread itself is not
                    # cancellable once scheduled.
                    await deletion
                    raise
                finally:
                    async with self._activity_lock:
                        self._deleting = False
                return True

    async def _enter_job(self) -> None:
        async with self._activity_lock:
            if self._deleting:
                raise RuntimeError("Local speaker separation is being removed.")
            self._active_jobs += 1

    async def _leave_job(self) -> None:
        async with self._activity_lock:
            self._active_jobs = max(0, self._active_jobs - 1)

    @staticmethod
    async def _prepare_audio(audio_path: Path, prepared: Path) -> None:
        ffmpeg = require_media_tool("ffmpeg")
        conversion = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav",
            str(prepared),
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                communicate_or_kill_on_cancel(
                    conversion,
                    max_stderr_bytes=MAX_WORKER_DIAGNOSTIC_BYTES + 1,
                ),
                timeout=600,
            )
        except TimeoutError as exc:
            raise RuntimeError("Audio preparation for local speaker separation timed out.") from exc
        if (
            conversion.returncode != 0
            or len(stderr or b"") > MAX_WORKER_DIAGNOSTIC_BYTES
            or not prepared.is_file()
        ):
            raise RuntimeError("Audio preparation for local speaker separation failed.")

    @staticmethod
    def _worker_timeout_seconds() -> float:
        try:
            configured = float(os.getenv("SCRIBER_DIARIZATION_WORKER_TIMEOUT_SECONDS", "3600"))
        except ValueError:
            configured = 3600.0
        return min(7200.0, max(60.0, configured))

    @staticmethod
    def is_duration_eligible(duration_ms: int) -> bool:
        return 0 < int(duration_ms) <= PRODUCT_MAX_DURATION_MS

    @staticmethod
    def _wave_duration_ms(path: Path) -> int:
        with wave.open(str(path), "rb") as reader:
            sample_rate = reader.getframerate()
            if sample_rate <= 0:
                raise RuntimeError("Prepared audio has an invalid sample rate.")
            return round(reader.getnframes() * 1000 / sample_rate)

    async def _run_worker_request(
        self,
        worker: WorkerDescriptor,
        request: dict[str, Any],
        *,
        job_root: Path,
    ) -> dict[str, Any]:
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise RuntimeError("Local speaker separation request is too large.")
        environment = os.environ.copy()
        environment["SCRIBER_DIARIZATION_JOB_ROOT"] = str(job_root.resolve())
        environment["SCRIBER_DIARIZATION_COMPONENT_ROOT"] = str(self.root.resolve())
        process = await asyncio.create_subprocess_exec(
            str(worker.executable),
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **hidden_subprocess_kwargs(),
        )

        async def communicate() -> tuple[bytes | None, bytes | None]:
            try:
                if process.stdin is None:
                    raise RuntimeError("Local speaker separation could not start.")
                process.stdin.write(encoded)
                await process.stdin.drain()
                process.stdin.close()
                return await communicate_or_kill_on_cancel(
                    process,
                    max_stdout_bytes=MAX_WORKER_OUTPUT_BYTES + 1,
                    max_stderr_bytes=MAX_WORKER_DIAGNOSTIC_BYTES + 1,
                )
            except BaseException:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await process.wait()
                except Exception:
                    pass
                raise

        try:
            stdout, stderr = await asyncio.wait_for(
                communicate(), timeout=self._worker_timeout_seconds()
            )
        except TimeoutError as exc:
            raise RuntimeError("Local speaker separation timed out.") from exc
        stdout = stdout or b""
        stderr = stderr or b""
        if len(stdout) > MAX_WORKER_OUTPUT_BYTES or len(stderr) > MAX_WORKER_DIAGNOSTIC_BYTES:
            raise RuntimeError("Local speaker separation returned too much data.")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise RuntimeError("Local speaker separation returned an invalid response.")
        try:
            payload = json.loads(lines[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Local speaker separation returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Local speaker separation returned an invalid response.")
        if process.returncode != 0 or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else "worker_failed"
            if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
                code = "worker_failed"
            raise RuntimeError(f"Local speaker separation failed ({code}).")
        return payload

    @staticmethod
    def _turns_from_worker_payload(payload: dict[str, Any], job_id: str) -> list[DiarizationTurn]:
        if (
            payload.get("schemaVersion") != WORKER_PROTOCOL_SCHEMA
            or payload.get("jobId") != job_id
            or payload.get("worker", {}).get("name") != WORKER_NAME
            or payload.get("worker", {}).get("version") != WORKER_VERSION
            or payload.get("engine", {}).get("name") != "sherpa-onnx"
            or payload.get("engine", {}).get("version") != SHERPA_VERSION
            or payload.get("engine", {}).get("linkMode") != "static"
            or payload.get("models", {}).get("segmentation") != SEGMENTATION_MODEL_ID
            or payload.get("models", {}).get("embedding") != EMBEDDING_MODEL_ID
            or payload.get("sampleRate") != 16_000
        ):
            raise RuntimeError("Local speaker separation returned an incompatible response.")
        duration = payload.get("durationMs")
        speaker_count = payload.get("speakerCount")
        raw_turns = payload.get("turns")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
            or duration > MAX_DURATION_MS
            or not isinstance(speaker_count, int)
            or isinstance(speaker_count, bool)
            or not 1 <= speaker_count <= 64
            or not isinstance(raw_turns, list)
            or len(raw_turns) > MAX_WORKER_TURNS
        ):
            raise RuntimeError("Local speaker separation returned an invalid response.")
        turns: list[DiarizationTurn] = []
        for raw in raw_turns:
            if not isinstance(raw, dict):
                raise RuntimeError("Local speaker separation returned an invalid response.")
            values = (raw.get("startMs"), raw.get("endMs"), raw.get("speaker"))
            if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
                raise RuntimeError("Local speaker separation returned an invalid response.")
            start_ms, end_ms, speaker = values
            if start_ms < 0 or end_ms <= start_ms or end_ms > duration or speaker < 0:
                raise RuntimeError("Local speaker separation returned an invalid response.")
            turns.append(DiarizationTurn(start_ms, end_ms, speaker))
        if {turn.speaker for turn in turns} != set(range(speaker_count)):
            raise RuntimeError("Local speaker separation returned inconsistent speakers.")
        normalized = normalize_turn_speakers(turns)
        if turns != sorted(turns, key=lambda item: (item.start_ms, item.end_ms, item.speaker)):
            raise RuntimeError("Local speaker separation returned unsorted turns.")
        if not normalized:
            raise RuntimeError("Local speaker separation returned no speech turns.")
        return normalized

    async def diarize(
        self,
        audio_path: Path,
        *,
        cluster_threshold: float = 0.9,
        num_speakers: int | None = None,
    ) -> list[DiarizationTurn]:
        if num_speakers is not None and (
            isinstance(num_speakers, bool)
            or not isinstance(num_speakers, int)
            or not 1 <= num_speakers <= 64
        ):
            raise ValueError("Known speaker count must be between 1 and 64.")
        threshold = float(cluster_threshold)
        if not math.isfinite(threshold) or not 0.1 <= threshold <= 1.0:
            raise ValueError("Clustering threshold must be between 0.1 and 1.0.")
        await self._enter_job()
        try:
            status = await self.status_async()
            if not status["installed"] or self._verified_worker is None:
                raise RuntimeError("The optional local speaker separation component is not installed.")
            worker = self._verified_worker
            scratch_root = self.root / "scratch"
            await asyncio.to_thread(scratch_root.mkdir, parents=True, exist_ok=True)
            directory = Path(
                await asyncio.to_thread(tempfile.mkdtemp, prefix="job-", dir=scratch_root)
            )
            try:
                prepared = directory / "audio.wav"
                await self._prepare_audio(Path(audio_path), prepared)
                duration_ms = await asyncio.to_thread(self._wave_duration_ms, prepared)
                if not self.is_duration_eligible(duration_ms):
                    raise DiarizationIneligibleError(
                        "Local speaker separation currently supports recordings up to 60 minutes; "
                        "choose an STT model with native diarization for longer recordings."
                    )
                job_id = f"local-diarization:{uuid4().hex}"
                request = {
                    "schemaVersion": WORKER_PROTOCOL_SCHEMA,
                    "jobId": job_id,
                    "audioPath": str(prepared.resolve()),
                    "segmentationModelPath": str(self.segmentation_model.resolve()),
                    "embeddingModelPath": str(self.embedding_model.resolve()),
                    "clustering": {
                        "numSpeakers": int(num_speakers) if num_speakers is not None else None,
                        "threshold": threshold,
                    },
                    "limits": {
                        "maxDurationMs": MAX_DURATION_MS,
                        "maxResidentBytes": MAX_RESIDENT_BYTES,
                    },
                }
                payload = await self._run_worker_request(worker, request, job_root=directory)
                return self._turns_from_worker_payload(payload, job_id)
            finally:
                await asyncio.to_thread(shutil.rmtree, directory, True)
        finally:
            await self._leave_job()

    async def transcribe_with_fallback_speakers(
        self,
        *,
        audio_path: Path,
        provider: str,
        payload: Any,
        text: str,
        source: str = "system",
        timeline_origin_ms: int = 0,
        num_speakers: int | None = None,
        normalized_words: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[DiarizationTurn]]:
        turns = await self.diarize(audio_path, num_speakers=num_speakers)
        if timeline_origin_ms:
            turns = [
                DiarizationTurn(
                    item.start_ms + timeline_origin_ms,
                    item.end_ms + timeline_origin_ms,
                    item.speaker,
                )
                for item in turns
            ]
        words = (
            [dict(item) for item in normalized_words]
            if normalized_words is not None
            else normalize_provider_words(provider, payload, timeline_origin_ms)
        )
        segments = align_words_to_speakers(words, turns, source=source) if words else []
        if not segments:
            segments = distribute_text_over_turns(text, turns, source=source)
        return segments, turns


async def diarization_component_installed(component: Any) -> bool:
    """Check component readiness without forcing synchronous hashing.

    The small compatibility branch keeps test doubles and older callers usable;
    the production manager always exposes ``status_async``.
    """
    status_async = getattr(component, "status_async", None)
    if callable(status_async):
        status = status_async()
        if asyncio.iscoroutine(status):
            status = await status
        return bool(status.get("installed")) if isinstance(status, dict) else False
    status = component.status()
    return bool(status.get("installed")) if isinstance(status, dict) else False


def turns_as_dicts(turns: list[DiarizationTurn]) -> list[dict[str, int]]:
    return [asdict(turn) for turn in turns]
