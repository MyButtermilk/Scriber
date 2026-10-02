import subprocess
import wave

import pytest

from src import azure_mai_stt, openrouter_audio
from src.audio_prepare import probe_audio_input_file
from src.runtime.ffmpeg_commands import mp3_transcode_args
from src.runtime.media_tools import find_media_tool
from src.runtime.subprocess_utils import hidden_subprocess_kwargs


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
