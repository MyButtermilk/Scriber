import asyncio
import subprocess
import wave
from pathlib import Path

import pytest

from src import cloud_async_stt
from src.audio_prepare import probe_audio_input_file
from src.runtime.ffmpeg_commands import mp3_transcode_args
from src.runtime.media_tools import find_media_tool


@pytest.fixture
def mp3_recording(tmp_path):
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    wav = tmp_path / "source.wav"
    with wave.open(str(wav), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x01\x00" * 16_000 * 13)
    mp3 = tmp_path / "source.mp3"
    subprocess.run(mp3_transcode_args(ffmpeg, wav, mp3), check=True, capture_output=True, timeout=30)
    return mp3


@pytest.mark.asyncio
async def test_long_mp3_is_split_and_merged_in_order(monkeypatch, mp3_recording):
    from src import openrouter_audio

    monkeypatch.setattr(openrouter_audio, "OPENROUTER_STT_MAX_AUDIO_BYTES", 60_000)
    monkeypatch.setattr(openrouter_audio, "OPENROUTER_PART_TARGET_BYTES", 48_000)
    uploaded = []
    paths = []
    progress = []

    async def transcribe(**kwargs):
        path = kwargs["audio_source"].name
        paths.append(path)
        probe = probe_audio_input_file(path)
        assert probe.codec_name == "mp3"
        assert probe.byte_length <= 60_000
        uploaded.append(kwargs["audio_source"].read())
        return {"text": f"Part {len(uploaded)}.", "usage": {"seconds": probe.duration_ms / 1000}}

    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    result = await cloud_async_stt.transcribe_openrouter_file(
        session=object(),
        api_key="inert",
        path=mp3_recording,
        content_type="audio/mpeg",
        language="de",
        on_progress=progress.append,
    )
    assert result["text"] == "Part 1.\n\nPart 2.\n\nPart 3."
    # The shared edge audio is intentionally sent twice; usage is the actual
    # provider work, while transcript times remain on the original 13s clock.
    assert 13.5 < result["usage"]["seconds"] < 20
    assert len(uploaded) == 3
    assert mp3_recording.is_file()
    assert all(not Path(path).exists() for path in paths)
    assert "Transcribing part 3 of 3..." in progress


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "cancelled"])
async def test_later_part_failure_never_replays_completed_parts_and_cleans(monkeypatch, mp3_recording, outcome):
    from src import openrouter_audio
    from src.core.provider_errors import ProviderTransportError, provider_transport_error

    monkeypatch.setattr(openrouter_audio, "OPENROUTER_STT_MAX_AUDIO_BYTES", 60_000)
    monkeypatch.setattr(openrouter_audio, "OPENROUTER_PART_TARGET_BYTES", 48_000)
    paths = []

    async def transcribe(**kwargs):
        paths.append(Path(kwargs["audio_source"].name))
        if len(paths) == 2:
            if outcome == "cancelled":
                raise asyncio.CancelledError
            raise provider_transport_error("openrouter_stt", "transcription", status=503)
        return {"text": "First completed part"}

    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else ProviderTransportError):
        await cloud_async_stt.transcribe_openrouter_file(
            session=object(),
            api_key="inert",
            path=mp3_recording,
            content_type="audio/mpeg",
            language="de",
        )
    assert len(paths) == 2
    assert all(not path.exists() for path in paths)
    assert mp3_recording.exists()


@pytest.mark.asyncio
async def test_size_alone_triggers_automatic_splitting(monkeypatch, mp3_recording):
    from src import openrouter_audio

    monkeypatch.setattr(openrouter_audio, "OPENROUTER_STT_MAX_AUDIO_BYTES", 60_000)
    monkeypatch.setattr(openrouter_audio, "OPENROUTER_PART_TARGET_BYTES", 48_000)
    parts = []
    async with openrouter_audio.openrouter_audio_parts(mp3_recording) as iterator:
        async for part in iterator:
            assert 0 < part.path.stat().st_size <= 60_000
            parts.append(part)
    assert len(parts) == 3
    assert all(not part.path.exists() for part in parts)


@pytest.mark.asyncio
async def test_small_mp3_is_uploaded_unchanged(monkeypatch, mp3_recording):
    async def transcribe(**kwargs):
        assert Path(kwargs["audio_source"].name) == mp3_recording
        assert kwargs["audio_source"].read() == mp3_recording.read_bytes()
        return {"text": "Whole recording"}

    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    result = await cloud_async_stt.transcribe_openrouter_file(
        session=object(),
        api_key="inert",
        path=mp3_recording,
        content_type="audio/mpeg",
        language="de",
    )
    assert result["text"] == "Whole recording"
    assert result["_scriberChunkCount"] == 1
    assert mp3_recording.exists()


@pytest.mark.asyncio
async def test_direct_pipeline_emits_one_complete_transcript_after_all_parts(monkeypatch, mp3_recording):
    from src import openrouter_audio
    from src.config import Config
    from src.pipeline import ScriberPipeline
    from src.transcript_artifacts import freeze_provider_route

    monkeypatch.setattr(openrouter_audio, "OPENROUTER_STT_MAX_AUDIO_BYTES", 60_000)
    monkeypatch.setattr(openrouter_audio, "OPENROUTER_PART_TARGET_BYTES", 48_000)
    monkeypatch.setattr(Config, "get_api_key", lambda _provider: "inert")
    calls = []
    emitted = []

    class Transport:
        async def session_view(self, **_kwargs):
            return object()

    async def transcribe(**kwargs):
        assert kwargs["model"] == "microsoft/mai-transcribe-2"
        assert kwargs["content_type"] == "audio/mpeg"
        assert not emitted
        calls.append(kwargs["audio_source"].read())
        return {"text": f"Part {len(calls)}."}

    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    route = freeze_provider_route(workload="file", provider="openrouter_stt", language="de")
    pipeline = ScriberPipeline(
        service_name="openrouter_stt",
        execution_route=route.execution_route(),
        provider_http_transport=Transport(),
        on_transcription=lambda text, final: emitted.append((text, final)),
    )
    await pipeline.transcribe_file_direct(str(mp3_recording))
    assert len(calls) == 3
    assert emitted == [("Part 1.\n\nPart 2.\n\nPart 3.", True)]
    assert pipeline.last_structured_transcript_payload["_scriberChunkCount"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, "cancel"])
async def test_restart_reuses_paid_parts_and_only_resumes_known_rejection(
    monkeypatch, mp3_recording, tmp_path, failure
):
    from src import openrouter_audio
    from src.core.provider_errors import ProviderTransportError, provider_transport_error
    from src.data.job_store import JobStore
    from src.data.transcription_part_store import source_sha256

    monkeypatch.setattr(openrouter_audio, "OPENROUTER_STT_MAX_AUDIO_BYTES", 60_000)
    monkeypatch.setattr(openrouter_audio, "OPENROUTER_PART_TARGET_BYTES", 48_000)
    database = tmp_path / "parts.sqlite"
    store = JobStore(database)
    job = store.enqueue(transcript_id="durable-parts", job_type="file")
    assert store.mark_running(job.id)
    digest = source_sha256(mp3_recording)
    route = {"provider": "openrouter_stt", "model": "microsoft/mai-transcribe-2", "language": "de"}
    checkpoint = store.transcription_checkpoint(
        job.id, source_digest=digest, source_path=mp3_recording, execution_route=route
    )
    uploaded = []

    async def transcribe(**kwargs):
        if kwargs.get("before_request") is not None:
            await kwargs["before_request"]()
        uploaded.append(kwargs["audio_source"].read())
        if len(uploaded) == 2:
            if failure == "cancel":
                raise asyncio.CancelledError
            raise provider_transport_error("openrouter_stt", "transcription", status=failure)
        return {"text": f"Recognized request {len(uploaded)}."}

    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ProviderTransportError):
        await cloud_async_stt.transcribe_openrouter_file(
            session=object(),
            api_key="inert",
            path=mp3_recording,
            content_type="audio/mpeg",
            language="de",
            checkpoint=checkpoint,
        )
    assert store.mark_failed(job.id, last_error="test provider failure")
    restarted = JobStore(database)
    assert restarted.checkpoint_resume_available(job.id) is (failure == 429)
    accepted = restarted.queue_checkpoint_resume(job.id, source_digest=digest, expected_attempt=1)
    assert accepted is (failure == 429)
    if not accepted:
        assert len(uploaded) == 2
        return
    assert restarted.mark_running(job.id)
    resumed = restarted.transcription_checkpoint(
        job.id, source_digest=digest, source_path=mp3_recording, execution_route=route
    )
    result = await cloud_async_stt.transcribe_openrouter_file(
        session=object(),
        api_key="inert",
        path=mp3_recording,
        content_type="audio/mpeg",
        language="de",
        checkpoint=resumed,
    )
    assert len(uploaded) == 4
    assert uploaded[1] == uploaded[2]
    assert result["text"] == "Recognized request 1.\n\nRecognized request 3.\n\nRecognized request 4."
    assert result["_scriberChunkCount"] == 3
