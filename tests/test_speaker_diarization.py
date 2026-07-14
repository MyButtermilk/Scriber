from __future__ import annotations

import asyncio
import json
import hashlib
import sys
import tarfile
import wave
from pathlib import Path

import pytest

from src import web_api
from src.speaker_diarization import (
    COMPONENT_SCHEMA,
    COMPONENT_NAME,
    COMPONENT_SOURCES,
    DiarizationIneligibleError,
    EMBEDDING_SHA256,
    SHERPA_VERSION,
    WORKER_FILE,
    WORKER_MANIFEST_FILE,
    WORKER_MANIFEST_SCHEMA,
    WORKER_NAME,
    WORKER_PROTOCOL_SCHEMA,
    WORKER_VERSION,
    MAX_EXTRACTED_MODEL_BYTES,
    DiarizationTurn,
    SherpaOnnxDiarizer,
    WorkerDescriptor,
    _safe_extract,
    align_words_to_speakers,
    distribute_text_over_turns,
    format_speaker_transcript,
    normalize_turn_speakers,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _worker_fixture(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "bundle" / WORKER_FILE
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"static-worker-fixture")
    manifest_path = executable.with_name(WORKER_MANIFEST_FILE)
    manifest_path.write_text(json.dumps({
        "schemaVersion": WORKER_MANIFEST_SCHEMA,
        "distribution": "bundled",
        "worker": {
            "name": WORKER_NAME,
            "fileName": WORKER_FILE,
            "version": WORKER_VERSION,
            "protocolSchemaVersion": WORKER_PROTOCOL_SCHEMA,
            "sherpaOnnxVersion": SHERPA_VERSION,
            "linkMode": "static",
            "sha256": _sha(executable.read_bytes()),
            "byteSize": executable.stat().st_size,
        },
    }), encoding="utf-8")
    return executable, manifest_path


def _write_component_fixture(manager: SherpaOnnxDiarizer) -> None:
    artifacts = {
        "segmentation-model": ("models/pyannote-segmentation-3.0.int8.onnx", b"segmentation"),
        "embedding-model": (
            "models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
            b"embedding",
        ),
        "segmentation-license": ("licenses/PYANNOTE_SEGMENTATION_LICENSE.txt", b"mit"),
        "embedding-license": ("licenses/APACHE-2.0.txt", b"apache"),
        "embedding-provenance": ("licenses/ERES2NET_MODEL_NOTICE.txt", b"provenance"),
        "worker-license": ("licenses/SCRIBER_DIARIZATION_WORKER_LICENSE.txt", b"mit-worker"),
    }
    records = []
    for role, (relative, content) in artifacts.items():
        path = manager.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append({
            "role": role,
            "relativePath": relative,
            "sha256": _sha(content),
            "byteSize": len(content),
        })
    worker = manager._descriptor_from_worker_manifest(  # noqa: SLF001 - contract fixture
        manager._worker_override, manager._worker_manifest_override
    )
    manager.manifest_path.write_text(json.dumps({
        "schemaVersion": COMPONENT_SCHEMA,
        "component": COMPONENT_NAME,
        "sherpaOnnxVersion": SHERPA_VERSION,
        "worker": {
            "name": WORKER_NAME,
            "version": WORKER_VERSION,
            "protocolSchemaVersion": WORKER_PROTOCOL_SCHEMA,
            "sha256": worker.sha256,
            "byteSize": worker.byte_size,
            "distribution": "bundled",
        },
        "artifacts": records,
        "sources": COMPONENT_SOURCES,
    }), encoding="utf-8")


def test_aligns_exact_provider_words_to_local_speaker_turns():
    words = [
        {"text": "Hello", "startMs": 100, "endMs": 400, "speaker": "", "confidence": 0.9},
        {"text": "there.", "startMs": 450, "endMs": 900, "speaker": "", "confidence": 0.8},
        {"text": "Agreed.", "startMs": 1_200, "endMs": 1_700, "speaker": "", "confidence": 0.95},
    ]
    turns = [DiarizationTurn(0, 1_000, 0), DiarizationTurn(1_050, 2_000, 1)]

    segments = align_words_to_speakers(words, turns)

    assert [item["speakerLabel"] for item in segments] == ["Speaker 1", "Speaker 2"]
    assert segments[0]["startMs"] == 100
    assert segments[0]["endMs"] == 900
    assert segments[1]["text"] == "Agreed."
    assert {item["alignmentQuality"] for item in segments} == {"exact_word"}


def test_plain_text_is_distributed_over_real_speech_turns():
    turns = [DiarizationTurn(200, 1_200, 0), DiarizationTurn(1_500, 2_500, 1)]
    segments = distribute_text_over_turns("One two three four five six", turns)

    assert len(segments) == 2
    assert [item["speakerLabel"] for item in segments] == ["Speaker 1", "Speaker 2"]
    assert segments[0]["startMs"] == 200
    assert segments[1]["endMs"] == 2_500
    assert "One" in format_speaker_transcript(segments)
    assert "[Speaker 2]:" in format_speaker_transcript(segments)
    assert {item["alignmentQuality"] for item in segments} == {"estimated"}


def test_sherpa_cluster_ids_are_stable_by_first_chronological_appearance():
    turns = [
        DiarizationTurn(2_000, 3_000, 7),
        DiarizationTurn(0, 1_000, 42),
        DiarizationTurn(1_100, 1_900, 7),
    ]

    normalized = normalize_turn_speakers(turns)

    assert [(item.start_ms, item.speaker) for item in normalized] == [
        (0, 0), (1_100, 1), (2_000, 1),
    ]


@pytest.mark.asyncio
async def test_component_status_requires_worker_models_and_licenses_to_match_manifest(
    tmp_path: Path, monkeypatch
):
    executable, worker_manifest = _worker_fixture(tmp_path)
    manager = SherpaOnnxDiarizer(
        tmp_path / "component",
        worker_executable=executable,
        worker_manifest_path=worker_manifest,
    )
    monkeypatch.setattr(manager, "_probe_worker_sync", lambda _worker: None)
    _write_component_fixture(manager)

    verified = await manager.status_async()
    assert verified["installed"] is True
    assert verified["workerReady"] is True
    assert verified["verificationState"] == "verified"

    manager.embedding_model.write_bytes(b"tampering!")
    rejected = await manager.status_async()
    assert rejected["installed"] is False
    assert rejected["reason"] == "component_hash_mismatch"


@pytest.mark.asyncio
async def test_status_caches_hashes_until_an_artifact_fingerprint_changes(tmp_path: Path, monkeypatch):
    executable, worker_manifest = _worker_fixture(tmp_path)
    manager = SherpaOnnxDiarizer(
        tmp_path / "component",
        worker_executable=executable,
        worker_manifest_path=worker_manifest,
    )
    monkeypatch.setattr(manager, "_probe_worker_sync", lambda _worker: None)
    _write_component_fixture(manager)
    from src import speaker_diarization as module

    real_sha = module._sha256
    calls: list[Path] = []

    def counting_sha(path: Path) -> str:
        calls.append(path)
        return real_sha(path)

    monkeypatch.setattr(module, "_sha256", counting_sha)
    assert (await manager.status_async())["installed"] is True
    first_count = len(calls)
    assert first_count >= 6
    assert (await manager.status_async())["installed"] is True
    assert len(calls) == first_count


@pytest.mark.asyncio
async def test_production_worker_without_build_manifest_fails_closed(tmp_path: Path, monkeypatch):
    backend_root = tmp_path / "resources" / "backend"
    worker = backend_root / "tools" / "diarization" / WORKER_FILE
    worker.parent.mkdir(parents=True)
    worker.write_bytes(b"unattested-worker")
    monkeypatch.setattr("src.speaker_diarization.is_frozen", lambda: True)
    monkeypatch.setattr("src.speaker_diarization.app_root", lambda: backend_root)
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    monkeypatch.setattr(manager, "_probe_worker_sync", lambda _worker: None)

    status = await manager.status_async()

    assert status["installed"] is False
    assert status["workerReady"] is False
    assert status["reason"] == "worker_manifest_missing"


def test_worker_response_contract_is_strict_and_uses_millisecond_turns():
    job_id = "local-diarization:fixture"
    payload = {
        "schemaVersion": WORKER_PROTOCOL_SCHEMA,
        "jobId": job_id,
        "ok": True,
        "worker": {"name": WORKER_NAME, "version": WORKER_VERSION},
        "engine": {"name": "sherpa-onnx", "version": SHERPA_VERSION, "linkMode": "static"},
        "models": {
            "segmentation": "pyannote-segmentation-3.0-int8",
            "embedding": "3d-speaker-eres2net-base-16k",
        },
        "sampleRate": 16_000,
        "durationMs": 2_000,
        "speakerCount": 2,
        "turns": [
            {"startMs": 100, "endMs": 800, "speaker": 0},
            {"startMs": 900, "endMs": 1_800, "speaker": 1},
        ],
    }

    turns = SherpaOnnxDiarizer._turns_from_worker_payload(payload, job_id)

    assert turns == [DiarizationTurn(100, 800, 0), DiarizationTurn(900, 1_800, 1)]
    payload["turns"].reverse()
    with pytest.raises(RuntimeError, match="unsorted"):
        SherpaOnnxDiarizer._turns_from_worker_payload(payload, job_id)


@pytest.mark.asyncio
async def test_worker_client_drains_large_stdout_concurrently_without_pipe_deadlock(
    tmp_path: Path, monkeypatch
):
    original_create = asyncio.create_subprocess_exec
    child_code = (
        "import json,sys;"
        "request=json.loads(sys.stdin.readline());"
        "payload={'schemaVersion':1,'jobId':request['jobId'],'ok':True,'padding':'x'*1048576};"
        "sys.stdout.write(json.dumps(payload)+'\\n');sys.stdout.flush()"
    )
    child = await original_create(
        sys.executable,
        "-c",
        child_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def use_child(*_args, **_kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", use_child)
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    descriptor = WorkerDescriptor(Path("unused.exe"), "0" * 64, 1, "test", WORKER_VERSION)
    request = {"schemaVersion": 1, "jobId": "large-output"}

    result = await asyncio.wait_for(
        manager._run_worker_request(descriptor, request, job_root=tmp_path), timeout=10
    )

    assert result["jobId"] == "large-output"
    assert len(result["padding"]) == 1_048_576
    assert child.returncode == 0


@pytest.mark.asyncio
async def test_worker_timeout_kills_and_reaps_child_process(tmp_path: Path, monkeypatch):
    original_create = asyncio.create_subprocess_exec
    child = await original_create(
        sys.executable,
        "-c",
        "import sys,time;sys.stdin.readline();time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def use_child(*_args, **_kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", use_child)
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    monkeypatch.setattr(manager, "_worker_timeout_seconds", lambda: 0.05)
    descriptor = WorkerDescriptor(Path("unused.exe"), "0" * 64, 1, "test", WORKER_VERSION)

    with pytest.raises(RuntimeError, match="timed out"):
        await manager._run_worker_request(
            descriptor,
            {"schemaVersion": 1, "jobId": "timeout"},
            job_root=tmp_path,
        )

    assert child.returncode is not None


@pytest.mark.asyncio
async def test_audio_preparation_produces_worker_pcm_contract(tmp_path: Path):
    source = tmp_path / "source.wav"
    prepared = tmp_path / "prepared.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48_000)
        writer.writeframes(b"\0\0" * 2 * 48_000)

    await SherpaOnnxDiarizer._prepare_audio(source, prepared)

    with wave.open(str(prepared), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16_000
        assert reader.getnframes() == 16_000


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [5, None])
async def test_component_download_enforces_declared_and_streamed_byte_limits(
    tmp_path: Path, declared_length: int | None
):
    class Content:
        async def iter_chunked(self, _size):
            yield b"abc"
            yield b"def"

    class Response:
        content_length = declared_length
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    destination = tmp_path / "artifact.bin"
    manager = SherpaOnnxDiarizer(tmp_path / "component")

    with pytest.raises(ValueError, match="size limit"):
        await manager._download(
            Session(), "https://example.invalid/artifact", destination, "0" * 64, max_bytes=4
        )

    assert not destination.exists()


@pytest.mark.asyncio
async def test_component_delete_refuses_while_diarization_owns_models_and_scratch(
    tmp_path: Path, monkeypatch
):
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    manager.root.mkdir(parents=True)
    worker = WorkerDescriptor(Path("unused.exe"), "0" * 64, 1, "test", WORKER_VERSION)
    manager._verified_worker = worker
    started = asyncio.Event()
    release = asyncio.Event()

    async def installed(*, force=False):
        return {"installed": True}

    async def prepare(_source: Path, prepared: Path):
        prepared.write_bytes(b"wav")

    async def run(_worker, request, *, job_root):
        assert job_root.is_dir()
        assert request["clustering"]["numSpeakers"] == 4
        assert request["limits"]["maxDurationMs"] == 2 * 60 * 60 * 1000
        started.set()
        await release.wait()
        return {
            "schemaVersion": 1,
            "jobId": request["jobId"],
            "ok": True,
            "worker": {"name": WORKER_NAME, "version": WORKER_VERSION},
            "engine": {"name": "sherpa-onnx", "version": SHERPA_VERSION, "linkMode": "static"},
            "models": {
                "segmentation": "pyannote-segmentation-3.0-int8",
                "embedding": "3d-speaker-eres2net-base-16k",
            },
            "sampleRate": 16_000,
            "durationMs": 1_000,
            "speakerCount": 1,
            "turns": [{"startMs": 0, "endMs": 900, "speaker": 0}],
        }

    monkeypatch.setattr(manager, "status_async", installed)
    monkeypatch.setattr(manager, "_prepare_audio", prepare)
    monkeypatch.setattr(manager, "_wave_duration_ms", lambda _path: 1_000)
    monkeypatch.setattr(manager, "_run_worker_request", run)
    task = asyncio.create_task(manager.diarize(tmp_path / "source.wav", num_speakers=4))
    await asyncio.wait_for(started.wait(), timeout=2)

    assert await manager.delete_async() is False
    assert manager.root.is_dir()
    release.set()
    assert await task == [DiarizationTurn(0, 900, 0)]
    assert await manager.delete_async() is True
    assert not manager.root.exists()


def test_product_duration_gate_keeps_worker_hard_limit_separate():
    assert SherpaOnnxDiarizer.is_duration_eligible(60 * 60 * 1000) is True
    assert SherpaOnnxDiarizer.is_duration_eligible(60 * 60 * 1000 + 1) is False
    assert SherpaOnnxDiarizer.is_duration_eligible(0) is False


def test_embedding_provenance_and_complete_apache_license_are_packaged():
    assert COMPONENT_SOURCES["embedding"]["modelRevision"] == "v1.0.1"
    assert COMPONENT_SOURCES["embedding"]["repositoryCommit"] == (
        "46215101b5c2ca4443163c8ced56147cc6f01908"
    )
    licenses = Path(__file__).resolve().parents[1] / "src" / "assets" / "licenses"
    apache = (licenses / "APACHE-2.0.txt").read_text(encoding="utf-8")
    provenance = (licenses / "ERES2NET_MODEL_NOTICE.txt").read_text(encoding="utf-8")
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in apache
    assert "approximately 10,000 speakers" in provenance
    assert "46215101b5c2ca4443163c8ced56147cc6f01908" in provenance


def test_component_archive_rejects_expansion_beyond_disk_budget(tmp_path: Path, monkeypatch):
    huge = tarfile.TarInfo("model.onnx")
    huge.size = MAX_EXTRACTED_MODEL_BYTES + 1

    class Bundle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getmembers(self):
            return [huge]

        def extractall(self, *_args, **_kwargs):
            raise AssertionError("oversized archive must be rejected before extraction")

    monkeypatch.setattr(tarfile, "open", lambda *_args, **_kwargs: Bundle())
    with pytest.raises(ValueError, match="size limit"):
        _safe_extract(tmp_path / "archive.tar.bz2", tmp_path / "out")


@pytest.mark.asyncio
async def test_over_product_limit_skips_before_worker_without_leaking_active_ownership(
    tmp_path: Path, monkeypatch
):
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    manager.root.mkdir(parents=True)
    manager._verified_worker = WorkerDescriptor(
        Path("unused.exe"), "0" * 64, 1, "test", WORKER_VERSION
    )

    async def installed(*, force=False):
        return {"installed": True}

    async def prepare(_source: Path, prepared: Path):
        prepared.write_bytes(b"wav")

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("worker must not start above the product limit")

    monkeypatch.setattr(manager, "status_async", installed)
    monkeypatch.setattr(manager, "_prepare_audio", prepare)
    monkeypatch.setattr(manager, "_wave_duration_ms", lambda _path: 60 * 60 * 1000 + 1)
    monkeypatch.setattr(manager, "_run_worker_request", must_not_run)

    with pytest.raises(DiarizationIneligibleError, match="native diarization"):
        await manager.diarize(tmp_path / "long.wav")

    assert manager.status()["activeJobs"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, 0, 65, 4.5, "4"])
async def test_known_speaker_count_is_strictly_bounded(tmp_path: Path, invalid):
    manager = SherpaOnnxDiarizer(tmp_path / "component")
    with pytest.raises(ValueError, match="between 1 and 64"):
        await manager.diarize(tmp_path / "audio.wav", num_speakers=invalid)


@pytest.mark.asyncio
async def test_fallback_prefers_provider_word_timestamps(monkeypatch, tmp_path: Path):
    manager = SherpaOnnxDiarizer(tmp_path / "component")

    async def fake_diarize(_path, **_kwargs):
        return [DiarizationTurn(0, 800, 0), DiarizationTurn(800, 1_800, 1)]

    monkeypatch.setattr(manager, "diarize", fake_diarize)
    payload = {
        "words": [
            {"text": "First", "start": 0.1, "end": 0.5},
            {"text": "Second", "start": 1.0, "end": 1.5},
        ]
    }
    segments, turns = await manager.transcribe_with_fallback_speakers(
        audio_path=tmp_path / "audio.wav",
        provider="openai_async",
        payload=payload,
        text="First Second",
    )

    assert len(turns) == 2
    assert [item["speakerLabel"] for item in segments] == ["Speaker 1", "Speaker 2"]
    assert segments[1]["startMs"] == 1_000
    assert {item["alignmentQuality"] for item in segments} == {"exact_word"}


@pytest.mark.asyncio
async def test_file_and_youtube_postprocess_uses_response_evidence_not_provider_marketing(
    monkeypatch, tmp_path: Path
):
    class FakeDiarizer:
        calls = 0

        def status(self):
            return {"installed": True}

        async def transcribe_with_fallback_speakers(self, **_kwargs):
            self.calls += 1
            return ([{
                "speakerLabel": "Speaker 1", "text": "Hello there.",
                "startMs": 0, "endMs": 900,
            }, {
                "speakerLabel": "Speaker 2", "text": "Thanks.",
                "startMs": 1_000, "endMs": 1_500,
            }], [])

    controller = object.__new__(web_api.ScriberWebController)
    controller._speaker_diarizer = FakeDiarizer()

    async def no_broadcast(**_kwargs):
        return None

    controller._broadcast_history_updated = no_broadcast
    monkeypatch.setattr(web_api.Config, "SPEAKER_DIARIZATION_FALLBACK_ENABLED", True)
    record = web_api.TranscriptRecord(
        id="fallback", title="Interview", date="Today", duration="00:02",
        status="processing", type="file", language="auto", content="Hello there. Thanks.",
    )
    pipeline = type("Pipeline", (), {"last_structured_transcript_payload": None})()

    segments = await web_api.ScriberWebController._apply_speaker_diarization_fallback(
        controller,
        record,
        provider="openai_async",
        pipeline=pipeline,
        audio_path=tmp_path / "audio.wav",
    )
    assert len(segments) == 2
    assert record.content_text() == "[Speaker 1]: Hello there.\n[Speaker 2]: Thanks."
    assert controller._speaker_diarizer.calls == 1

    claimed_native_record = web_api.TranscriptRecord(
        id="claimed-native", title="Claimed native", date="Today", duration="00:02",
        status="processing", type="youtube", language="auto", content="Native transcript",
    )
    claimed_native = await web_api.ScriberWebController._apply_speaker_diarization_fallback(
        controller,
        claimed_native_record,
        provider="assemblyai",
        pipeline=pipeline,
        audio_path=tmp_path / "audio.wav",
    )
    assert len(claimed_native) == 2
    assert controller._speaker_diarizer.calls == 2

    native_record = web_api.TranscriptRecord(
        id="native", title="Native", date="Today", duration="00:02",
        status="processing", type="youtube", language="auto",
        content="[Speaker A]: Native transcript",
    )
    native_pipeline = type("Pipeline", (), {"last_structured_transcript_payload": {
        "utterances": [{
            "speaker": "A", "text": "Native transcript", "start": 0, "end": 1_500,
        }],
    }})()
    native = await web_api.ScriberWebController._apply_speaker_diarization_fallback(
        controller,
        native_record,
        provider="assemblyai",
        pipeline=native_pipeline,
        audio_path=tmp_path / "audio.wav",
    )
    assert native == []
    assert controller._speaker_diarizer.calls == 2

    async def ineligible(**_kwargs):
        raise DiarizationIneligibleError(
            "Local speaker separation currently supports recordings up to 60 minutes; "
            "choose an STT model with native diarization for longer recordings."
        )

    monkeypatch.setattr(
        controller._speaker_diarizer,
        "transcribe_with_fallback_speakers",
        ineligible,
    )
    long_record = web_api.TranscriptRecord(
        id="long", title="Long", date="Today", duration="61:00",
        status="processing", type="file", language="auto", content="Keep this STT result.",
    )
    skipped = await web_api.ScriberWebController._apply_speaker_diarization_fallback(
        controller,
        long_record,
        provider="openai_async",
        pipeline=pipeline,
        audio_path=tmp_path / "long.wav",
    )
    assert skipped == []
    assert long_record.content_text() == "Keep this STT result."

    async def broken_preparation(**_kwargs):
        raise RuntimeError("Audio preparation for local speaker separation failed.")

    monkeypatch.setattr(
        controller._speaker_diarizer,
        "transcribe_with_fallback_speakers",
        broken_preparation,
    )
    completed_provider_record = web_api.TranscriptRecord(
        id="provider-complete", title="Provider complete", date="Today", duration="08:56",
        status="processing", type="youtube", language="auto",
        content="Keep the completed provider transcript.",
    )
    degraded = await web_api.ScriberWebController._apply_speaker_diarization_fallback(
        controller,
        completed_provider_record,
        provider="azure_mai",
        pipeline=pipeline,
        audio_path=tmp_path / "youtube.webm",
    )
    assert degraded == []
    assert completed_provider_record.content_text() == "Keep the completed provider transcript."
