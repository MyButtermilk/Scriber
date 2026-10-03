import os
import subprocess
import wave
from dataclasses import replace

import pytest

from src import azure_mai_stt, openrouter_audio
from src.audio_prepare import PreparedProviderAudio, ProviderAudioPreparationError, probe_audio_input_file
from src.core.provider_audio_formats import AudioInputFormat, AudioSelectionMode
from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import TranscriptionPartOutcomeUnknown, source_sha256
from src.runtime.ffmpeg_commands import flac_transcode_args, mp3_transcode_args
from src.runtime.media_tools import find_media_tool
from src.runtime.subprocess_utils import hidden_subprocess_kwargs
from src.transcript_artifacts import freeze_provider_route


@pytest.fixture
def mp3_recording(tmp_path):
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x01\x00" * 16_000 * 13)
    target = tmp_path / "source.mp3"
    subprocess.run(
        mp3_transcode_args(ffmpeg, source, target),
        check=True,
        capture_output=True,
        timeout=30,
        **hidden_subprocess_kwargs(),
    )
    return target


def observe_manifest(monkeypatch):
    manifests = []
    plan = openrouter_audio.plan_mp3_audio_parts

    async def observed(*args, **kwargs):
        manifest = await plan(*args, **kwargs)
        manifests.append(manifest)
        return manifest

    monkeypatch.setattr(openrouter_audio, "plan_mp3_audio_parts", observed)
    return manifests


async def transcribe_file(path, **kwargs):
    return await azure_mai_stt.transcribe_azure_mai_file_parts(
        audio_path=path,
        session=object(),
        speech_key="inert-test-key",
        region="northeurope",
        content_type="audio/mpeg",
        language="de-DE",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "diarize", "requests_words"),
    [("MAI-Transcribe-2", False, True), ("MAI-Transcribe-2", True, True), ("mai-transcribe-1.5", False, False)],
)
async def test_azure_whole_mp3_unchanged_at_both_limits(monkeypatch, mp3_recording, model, diarize, requests_words):
    original = mp3_recording.read_bytes()
    probe = probe_audio_input_file(mp3_recording)
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_BYTES", len(original))
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_DURATION_MS", probe.duration_ms)
    manifests = observe_manifest(monkeypatch)
    calls = []
    payload = {"combinedPhrases": [{"text": "Whole recording"}], "durationMilliseconds": probe.duration_ms}

    async def provider(**kwargs):
        calls.append(kwargs)
        assert kwargs["audio_path"] == mp3_recording
        assert kwargs["audio_path"].read_bytes() == original
        assert kwargs["request_word_timestamps"] is requests_words
        assert kwargs["diarize"] is diarize
        assert kwargs["model"] == model
        assert kwargs["content_type"] == "audio/mpeg"
        return payload.copy()

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    result = await transcribe_file(mp3_recording, model=model, diarize=diarize)
    assert result == {**payload, "_scriberChunkCount": 1}
    assert len(calls) == 1
    assert len(manifests) == 1
    assert manifests[0].passthrough
    assert mp3_recording.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_trigger", ["duration", "one_byte_over"])
async def test_azure_real_parts_obey_limits_and_merge_native_words_on_source_clock(
    monkeypatch, mp3_recording, limit_trigger
):
    original = mp3_recording.read_bytes()
    source_probe = probe_audio_input_file(mp3_recording)
    max_bytes = len(original) * 2 if limit_trigger == "duration" else len(original) - 1
    max_duration = 6_000 if limit_trigger == "duration" else source_probe.duration_ms + 1
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_BYTES", max_bytes)
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_DURATION_MS", max_duration)
    manifests = observe_manifest(monkeypatch)
    source_words = [(f"Wort{index + 1}", 500 + 1000 * index, 700 + 1000 * index) for index in range(13)]
    uploaded = []
    billed_ms = []
    progress = []

    async def provider(**kwargs):
        assert len(manifests) == 1  # Every boundary is fixed before the first request.
        window = manifests[0].parts[len(uploaded)]
        path = kwargs["audio_path"]
        assert path != mp3_recording
        assert kwargs["request_word_timestamps"] is True
        assert kwargs["diarize"] is False
        assert kwargs["model"] == "MAI-Transcribe-2"
        probe = probe_audio_input_file(path)
        assert probe.codec_name == "mp3"
        assert 0 < probe.byte_length <= max_bytes
        assert 0 < probe.duration_ms <= max_duration
        assert list(path.parent.iterdir()) == [path]
        uploaded.append(path)
        billed_ms.append(probe.duration_ms)
        kwargs["on_progress"]("Transcribing...")
        words = [
            {"text": text, "offsetMilliseconds": start - window.start_ms, "durationMilliseconds": end - start}
            for text, start, end in source_words
            if window.start_ms <= start < end <= window.end_ms
        ]
        text = " ".join(word["text"] for word in words)
        return {
            "combinedPhrases": [{"text": text}],
            "phrases": [{"text": text, "words": words}],
            "durationMilliseconds": probe.duration_ms,
            "usage": {"seconds": probe.duration_ms / 1000},
        }

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    result = await transcribe_file(mp3_recording, model="MAI-Transcribe-2", on_progress=progress.append)
    manifest = manifests[0]
    assert not manifest.passthrough
    assert len(uploaded) == len(manifest.parts) == result["_scriberChunkCount"]
    assert len(uploaded) > 1
    for left, right in zip(manifest.parts, manifest.parts[1:], strict=False):
        assert left.core_end_ms == right.core_start_ms
        assert left.end_ms > right.start_ms
    assert result["text"] == " ".join(word[0] for word in source_words)
    merged = result["_scriberMerged"]
    assert merged["wordTimingComplete"] is True
    assert merged["alignmentQuality"] == "exact_word"
    assert merged["audioBoundaryToleranceMs"] == 250
    assert [(word["text"], word["startMs"], word["endMs"]) for word in merged["words"]] == source_words
    assert result["usage"]["seconds"] == pytest.approx(sum(billed_ms) / 1000)
    assert f"Transcribing part {len(uploaded)} of {len(uploaded)}..." in progress
    assert all(not path.exists() and not path.parent.exists() for path in uploaded)
    assert mp3_recording.read_bytes() == original


@pytest.mark.asyncio
async def test_azure_text_only_parts_keep_text_and_report_missing_word_timing(monkeypatch, mp3_recording):
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_DURATION_MS", 6_000)
    paths = []

    async def provider(**kwargs):
        assert kwargs["request_word_timestamps"] is True
        assert kwargs["diarize"] is False
        paths.append(kwargs["audio_path"])
        return {"combinedPhrases": [{"text": f"Part {len(paths)}."}]}

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    result = await transcribe_file(mp3_recording, model="MAI-Transcribe-2")
    assert len(paths) > 1
    assert result["_scriberChunkCount"] == len(paths)
    assert result["text"] == "\n\n".join(f"Part {index}." for index in range(1, len(paths) + 1))
    merged = result["_scriberMerged"]
    assert merged["wordTimingComplete"] is False
    assert merged["alignmentQuality"] == "estimated"
    assert merged["words"] == []
    assert len(merged["boundaryWarnings"]) == len(paths) - 1
    assert {warning["code"] for warning in merged["boundaryWarnings"]} == {"overlap_missing_word_timestamps"}
    assert all(not path.exists() for path in paths)
    assert mp3_recording.exists()


@pytest.fixture(params=[AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC])
def frozen_prepared_recording(tmp_path, request):
    if not find_media_tool("ffprobe"):
        pytest.skip("ffprobe is unavailable")
    source = tmp_path / "frozen.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x01\x00" * 16_000)
    audio_format = request.param
    if audio_format == AudioInputFormat.FLAC:
        ffmpeg = find_media_tool("ffmpeg")
        if not ffmpeg:
            pytest.skip("FFmpeg is unavailable")
        target = source.with_suffix(".flac")
        subprocess.run(
            flac_transcode_args(ffmpeg, source, target),
            check=True,
            capture_output=True,
            timeout=30,
            **hidden_subprocess_kwargs(),
        )
        source = target
    probe = probe_audio_input_file(source)
    route = freeze_provider_route(
        workload="file", provider="azure_mai", audio_input_format=audio_format
    ).execution_route()
    prepared = PreparedProviderAudio(
        path=source,
        source_format=AudioInputFormat.WAV_PCM16,
        selected_format=audio_format,
        selection_mode=AudioSelectionMode.GENERATED,
        implementation="ffmpeg_wav_pcm16_control"
        if audio_format == AudioInputFormat.WAV_PCM16
        else "ffmpeg_flac_fast_control",
        content_type="audio/wav" if audio_format == AudioInputFormat.WAV_PCM16 else "audio/flac",
        capability_id=route["provider_audio_capability_id"],
        capability_revision=route["provider_audio_capability_revision"],
        byte_length=probe.byte_length,
        generated=True,
        duration_ms=probe.duration_ms,
        verified_mtime_ns=source.stat().st_mtime_ns,
    )
    return prepared, route


async def transcribe_prepared(prepared, **kwargs):
    return await azure_mai_stt.transcribe_azure_mai_file_parts(
        audio_path=prepared.path,
        session=object(),
        speech_key="inert-test-key",
        region="northeurope",
        content_type=prepared.content_type,
        language="de-DE",
        prepared_audio=prepared,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["bytes", "duration"])
@pytest.mark.parametrize("limit_delta", [0, -1])
async def test_azure_frozen_non_mp3_obeys_both_real_file_bounds(
    monkeypatch, frozen_prepared_recording, limit_kind, limit_delta
):
    prepared, _route = frozen_prepared_recording
    original = prepared.path.read_bytes()
    monkeypatch.setattr(
        azure_mai_stt, "AZURE_MAI_MAX_AUDIO_BYTES", len(original) + (limit_delta if limit_kind == "bytes" else 0)
    )
    monkeypatch.setattr(
        azure_mai_stt,
        "AZURE_MAI_MAX_AUDIO_DURATION_MS",
        prepared.duration_ms + (limit_delta if limit_kind == "duration" else 0),
    )
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        assert kwargs["audio_path"] == prepared.path
        assert kwargs["audio_path"].read_bytes() == original
        assert kwargs["content_type"] == prepared.content_type
        return {"combinedPhrases": [{"text": "Frozen recording"}]}

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    if limit_delta:
        with pytest.raises(ProviderAudioPreparationError, match="byte or duration limit"):
            await transcribe_prepared(prepared)
        assert calls == []
    else:
        result = await transcribe_prepared(prepared)
        assert result["_scriberChunkCount"] == 1
        assert len(calls) == 1
    assert prepared.path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_result", ["valid", "unknown_duration", "wrong_format", "unbound_duration"])
async def test_azure_frozen_non_mp3_missing_duration_requires_exact_probe(
    monkeypatch, frozen_prepared_recording, probe_result
):
    prepared, _route = frozen_prepared_recording
    verified_probe = probe_audio_input_file(prepared.path)
    if probe_result == "unknown_duration":
        verified_probe = replace(verified_probe, duration_ms=None)
    elif probe_result == "wrong_format":
        verified_probe = replace(verified_probe, audio_format=AudioInputFormat.MP3)
    prepared = (
        replace(prepared, verified_mtime_ns=None)
        if probe_result == "unbound_duration"
        else replace(prepared, duration_ms=None)
    )
    probes = []
    calls = []

    def probe(path):
        probes.append(path)
        return verified_probe

    async def provider(**kwargs):
        calls.append(kwargs)
        return {"combinedPhrases": [{"text": "Verified recording"}]}

    monkeypatch.setattr(openrouter_audio, "probe_audio_input_file", probe)
    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    if probe_result in {"valid", "unbound_duration"}:
        await transcribe_prepared(prepared)
        assert len(calls) == 1
    else:
        with pytest.raises(ProviderAudioPreparationError, match="duration|verified format"):
            await transcribe_prepared(prepared)
        assert calls == []
    assert probes and all(path == prepared.path for path in probes)
    assert prepared.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown"])
async def test_azure_frozen_non_mp3_retains_paid_checkpoint_and_no_replay_fence(
    monkeypatch, tmp_path, frozen_prepared_recording, outcome
):
    prepared, route = frozen_prepared_recording
    store = JobStore(tmp_path / "frozen-checkpoint.sqlite")
    job = store.enqueue(transcript_id="frozen-provider-test", job_type=JobType.FILE)
    assert store.mark_running(job.id)
    checkpoint = store.transcription_checkpoint(
        job.id,
        source_digest=source_sha256(prepared.path),
        source_path=prepared.path,
        execution_route=route,
    )
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        if outcome == "unknown":
            raise TimeoutError("synthetic lost response")
        return {"combinedPhrases": [{"text": "Paid recording"}]}

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", provider)
    try:
        if outcome == "success":
            first = await transcribe_prepared(prepared, checkpoint=checkpoint)
            assert await transcribe_prepared(prepared, checkpoint=checkpoint) == first
        else:
            with pytest.raises(TimeoutError, match="lost response"):
                await transcribe_prepared(prepared, checkpoint=checkpoint)
            with pytest.raises(TranscriptionPartOutcomeUnknown):
                await transcribe_prepared(prepared, checkpoint=checkpoint)
        assert len(calls) == 1
        with pytest.raises(ProviderAudioPreparationError, match="saved part manifest"):
            await transcribe_prepared(replace(prepared, duration_ms=prepared.duration_ms + 1), checkpoint=checkpoint)
        assert prepared.path.exists()
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_duration", [True, 0, -1, 1.0])
async def test_azure_frozen_non_mp3_rejects_invalid_duration_evidence_before_http(
    monkeypatch, frozen_prepared_recording, invalid_duration
):
    prepared, _route = frozen_prepared_recording

    async def unexpected_provider(**_kwargs):
        pytest.fail("Invalid preparation evidence must fail before provider HTTP")

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", unexpected_provider)
    with pytest.raises(ProviderAudioPreparationError, match="positive recording duration"):
        await transcribe_prepared(replace(prepared, duration_ms=invalid_duration))
    assert prepared.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_phase", ["after_preparation", "after_plan"])
@pytest.mark.parametrize("frozen_prepared_recording", [AudioInputFormat.WAV_PCM16], indirect=True)
async def test_azure_frozen_wav_duration_evidence_rejects_same_size_header_drift(
    monkeypatch, frozen_prepared_recording, change_phase
):
    prepared, _route = frozen_prepared_recording
    original_size = prepared.path.stat().st_size
    monkeypatch.setattr(azure_mai_stt, "AZURE_MAI_MAX_AUDIO_DURATION_MS", prepared.duration_ms)

    def change_duration():
        changed = bytearray(prepared.path.read_bytes())
        changed[24:28] = (8_000).to_bytes(4, "little")
        changed[28:32] = (16_000).to_bytes(4, "little")
        prepared.path.write_bytes(changed)
        os.utime(prepared.path, ns=(prepared.verified_mtime_ns, prepared.verified_mtime_ns + 1_000_000_000))
        assert prepared.path.stat().st_size == original_size
        assert probe_audio_input_file(prepared.path).duration_ms == prepared.duration_ms * 2

    async def unexpected_provider(**_kwargs):
        pytest.fail("Changed WAV duration evidence must fail before provider HTTP")

    monkeypatch.setattr(azure_mai_stt, "transcribe_azure_mai_file", unexpected_provider)
    if change_phase == "after_preparation":
        change_duration()
    else:
        plan = openrouter_audio.plan_mp3_audio_parts

        async def changed_after_plan(*args, **kwargs):
            manifest = await plan(*args, **kwargs)
            change_duration()
            return manifest

        monkeypatch.setattr(openrouter_audio, "plan_mp3_audio_parts", changed_after_plan)
    with pytest.raises(ProviderAudioPreparationError, match="verified file"):
        await transcribe_prepared(prepared)
    assert prepared.path.exists()
