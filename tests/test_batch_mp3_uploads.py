"""Provider-independent compact upload behavior at selection and live boundaries."""

import io
import wave
from pathlib import Path

import pytest

from src import audio_prepare
from src.assemblyai_async_stt import AssemblyAIUniversal35ProAsyncProcessor
from src.cloud_async_stt import (
    DeepgramAsyncProcessor,
    GeminiAsyncProcessor,
    GladiaAsyncProcessor,
    OpenAIAsyncProcessor,
    OpenRouterSTTProcessor,
    SpeechmaticsAsyncProcessor,
)
from src.core.provider_audio_formats import (
    PROVIDER_AUDIO_CAPABILITY_MATRIX,
    AudioInputFormat,
    AudioInputSelection,
    AudioSelectionMode,
    ProviderAudioRouteKind,
    resolve_batch_provider_audio_capabilities,
    select_audio_input_format,
)
from src.mistral_stt import MistralAsyncProcessor
from src.modulate_stt import ModulateAsyncProcessor
from src.runtime.media_tools import find_media_tool
from src.smallest_stt import SmallestAsyncProcessor


@pytest.mark.parametrize(
    "capability",
    [c for c in PROVIDER_AUDIO_CAPABILITY_MATRIX if c.active and c.route_kind == ProviderAudioRouteKind.BATCH],
    ids=lambda c: c.capability_id,
)
def test_batch_wav_uses_mp3_only_where_exact_route_accepts_it(capability):
    selected = select_audio_input_format(
        capability, route_kind=ProviderAudioRouteKind.BATCH, original_format=AudioInputFormat.WAV_PCM16
    )
    if AudioInputFormat.MP3 in capability.batch_formats:
        assert selected.audio_format == AudioInputFormat.MP3
        assert selected.mode == AudioSelectionMode.GENERATED
    else:
        assert selected.audio_format == AudioInputFormat.WAV_PCM16
        assert selected.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


@pytest.mark.parametrize(
    "provider,model,original",
    [
        ("soniox_async", "stt-async-v5", AudioInputFormat.WEBM_OPUS),
        ("soniox_async", "stt-async-v5", AudioInputFormat.MP3),
        ("smallest_async", "pulse", AudioInputFormat.OGG_OPUS),
        ("deepgram_async", "nova-3", AudioInputFormat.OGG_OPUS),
        ("openai_async", "gpt-4o-mini-transcribe-2025-12-15", AudioInputFormat.M4A_AAC),
        ("speechmatics_async", "enhanced", AudioInputFormat.MP3),
    ],
)
def test_accepted_lossy_original_avoids_reencoding(provider, model, original):
    selected = select_audio_input_format(
        resolve_batch_provider_audio_capabilities(provider, model),
        route_kind=ProviderAudioRouteKind.BATCH,
        original_format=original,
    )
    assert selected.audio_format == original
    assert selected.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_format", [AudioInputFormat.WAV_PCM16, AudioInputFormat.FLAC])
async def test_frozen_legacy_lossless_upload_keeps_its_exact_bytes(monkeypatch, tmp_path, audio_format):
    capability = resolve_batch_provider_audio_capabilities("deepgram_async", "nova-3")
    source = tmp_path / ("original.wav" if audio_format == AudioInputFormat.WAV_PCM16 else "original.flac")
    source.write_bytes(b"frozen original")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda _path: audio_prepare.ProbedAudioInput(
            audio_format,
            audio_format.container.value,
            audio_format.codec.value,
            16_000,
            1,
            1_000,
            source.stat().st_size,
        ),
    )
    frozen = AudioInputSelection(
        audio_format, AudioSelectionMode.ORIGINAL_PASSTHROUGH, capability.capability_id, capability.revision
    )
    async with audio_prepare.prepare_provider_audio_file(
        source, provider="deepgram_async", model="nova-3", frozen_selection=frozen
    ) as prepared:
        assert prepared.path == source
        assert prepared.path.read_bytes() == b"frozen original"
        assert prepared.selected_format == audio_format
        assert not prepared.generated
    assert source.read_bytes() == b"frozen original"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "processor_type,options,transport_name",
    [
        (DeepgramAsyncProcessor, {}, "src.cloud_async_stt.transcribe_with_deepgram_pre_recorded"),
        (
            OpenAIAsyncProcessor,
            {"model": "gpt-4o-mini-transcribe-2025-12-15"},
            "src.cloud_async_stt.transcribe_with_openai_audio_transcription",
        ),
        (OpenRouterSTTProcessor, {}, "src.cloud_async_stt.transcribe_with_openrouter_audio_transcription"),
        (GeminiAsyncProcessor, {}, "src.cloud_async_stt.transcribe_with_gemini_audio"),
        (GladiaAsyncProcessor, {}, "src.cloud_async_stt.transcribe_with_gladia_pre_recorded"),
        (SpeechmaticsAsyncProcessor, {}, "src.cloud_async_stt.transcribe_with_speechmatics_batch"),
        (
            AssemblyAIUniversal35ProAsyncProcessor,
            {},
            "src.assemblyai_async_stt.transcribe_with_assemblyai_pre_recorded",
        ),
        (MistralAsyncProcessor, {"model": "voxtral-mini-2602"}, "src.mistral_stt.transcribe_with_mistral"),
        (ModulateAsyncProcessor, {}, "src.modulate_stt.transcribe_with_modulate_multilingual"),
        (SmallestAsyncProcessor, {}, "src.smallest_stt.transcribe_with_smallest_pre_recorded"),
    ],
    ids=lambda value: value.__name__ if isinstance(value, type) else None,
)
async def test_buffered_provider_passes_real_mp3_to_transport_and_cleans_it(
    monkeypatch, processor_type, options, transport_name
):
    if not find_media_tool("ffmpeg") or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are unavailable")
    monkeypatch.delenv("SCRIBER_SPEECHMATICS_BATCH_BASE_URL", raising=False)
    source = io.BytesIO()
    with wave.open(source, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x01\x00" * 16_000)
    original = source.getvalue()
    uploaded = []

    async def transcribe(**kwargs):
        stream = kwargs.get("audio_source", kwargs.get("file_content"))
        path = Path(stream.name)
        probe = audio_prepare.probe_audio_input_file(path)
        assert probe.audio_format == AudioInputFormat.MP3
        assert probe.channels == 1
        assert 950 <= probe.duration_ms <= 1_200
        assert probe.byte_length < len(original)
        if "filename" in kwargs:
            assert kwargs["filename"] == "audio.mp3"
            assert kwargs["content_type"] == "audio/mpeg"
        uploaded.append((stream, path))
        return {
            "text": "Hallo",
            "transcript": "Hallo",
            "output_text": "Hallo",
            "results": {"channels": [{"alternatives": [{"transcript": "Hallo"}]}]},
            "result": {"transcription": {"full_transcript": "Hallo"}},
        }

    monkeypatch.setattr(transport_name, transcribe)
    processor = processor_type(api_key="inert-key", language="de", session=object(), **options)
    try:
        assert await processor._transcribe_wav(source) == "Hallo"
        assert len(uploaded) == 1
        stream, path = uploaded[0]
        assert stream.closed
        assert not path.parent.exists()
        assert not source.closed
        assert source.getvalue() == original
    finally:
        source.close()
        processor._buffer.close()
