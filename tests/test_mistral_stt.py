import wave
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import AudioRawFrame, EndFrame, ErrorFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from src.mistral_stt import (
    MistralAsyncProcessor,
    MistralRealtimeSTTService,
    _normalize_context_bias_terms,
    format_mistral_segments_with_speakers,
    transcribe_with_mistral,
)
from src.core.error_taxonomy import ErrorCategory
from src.core.provider_errors import provider_user_error


def test_mistral_realtime_initializes_complete_pipecat_1_5_settings():
    service = MistralRealtimeSTTService(
        api_key="test-key",
        model="voxtral-mini-transcribe-2507",
        language="auto",
    )

    assert service._settings.model == "voxtral-mini-transcribe-2507"
    assert service._settings.language is None


@pytest.mark.asyncio
async def test_mistral_realtime_transcribes_pcm_via_shared_wav_converter(monkeypatch):
    pcm = b"\x01\x02" * 160
    observed: dict[str, object] = {}

    async def _fake_transcribe(**kwargs):
        wav_source = kwargs["file_content"]
        observed["source"] = wav_source
        with wave.open(wav_source, "rb") as reader:
            observed["sample_rate"] = reader.getframerate()
            observed["channels"] = reader.getnchannels()
            observed["pcm"] = reader.readframes(reader.getnframes())
        return {"text": "Hallo von Mistral"}

    monkeypatch.setattr("src.mistral_stt.transcribe_with_mistral", _fake_transcribe)
    service = MistralRealtimeSTTService(
        api_key="test-key",
        model="voxtral-mini-transcribe-2507",
        language="de",
        aiohttp_session=object(),  # type: ignore[arg-type]
    )

    frames = [frame async for frame in service.run_stt(pcm)]

    assert len(frames) == 1
    assert isinstance(frames[0], TranscriptionFrame)
    assert frames[0].text == "Hallo von Mistral"
    assert observed["sample_rate"] == 16_000
    assert observed["channels"] == 1
    assert observed["pcm"] == pcm
    assert observed["source"].closed  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mistral_realtime_returns_error_frame_and_closes_wav(monkeypatch):
    observed: dict[str, object] = {}

    async def _fake_transcribe(**kwargs):
        observed["source"] = kwargs["file_content"]
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("src.mistral_stt.transcribe_with_mistral", _fake_transcribe)
    service = MistralRealtimeSTTService(
        api_key="test-key",
        model="voxtral-mini-transcribe-2507",
        aiohttp_session=object(),  # type: ignore[arg-type]
    )

    frames = [frame async for frame in service.run_stt(b"\x01\x02" * 160)]

    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert frames[0].error.startswith("mistral realtime error:")
    assert "provider unavailable" not in frames[0].error
    assert observed["source"].closed  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mistral_realtime_does_not_expose_provider_content(monkeypatch):
    private_marker = "PRIVATE_TRANSCRIPT_8fbe2d"

    async def _fake_transcribe(**_kwargs):
        raise RuntimeError(private_marker)

    monkeypatch.setattr("src.mistral_stt.transcribe_with_mistral", _fake_transcribe)
    service = MistralRealtimeSTTService(
        api_key="test-key",
        model="voxtral-mini-transcribe-2507",
        aiohttp_session=object(),  # type: ignore[arg-type]
    )

    frames = [frame async for frame in service.run_stt(b"\x01\x02" * 160)]

    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert private_marker not in frames[0].error


@pytest.mark.asyncio
async def test_mistral_http_error_discards_identifier_shaped_private_code():
    private_marker = "PRIVATE_TRANSCRIPT_91a5e7"

    class Response:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return (
                '{"code":"'
                + private_marker
                + '","message":"private provider response"}'
            )

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(RuntimeError) as caught:
        await transcribe_with_mistral(
            session=Session(),  # type: ignore[arg-type]
            api_key="test-key",
            model="voxtral-mini-transcribe-2507",
            file_content=b"wav",
            filename="audio.wav",
            content_type="audio/wav",
        )

    info = provider_user_error("mistral", caught.value)
    rendered = f"{caught.value!s} {caught.value!r}"
    assert private_marker not in rendered
    assert info.category is ErrorCategory.TRANSIENT_PROVIDER
    assert info.code == "500"


def test_context_bias_terms_strip_spaces_and_split_phrases():
    terms = _normalize_context_bias_terms("Scriber, Soniox, Bayerische Motoren Werke KGaA")
    assert "Scriber" in terms
    assert "Soniox" in terms
    assert "Bayerische" in terms
    assert "Motoren" in terms
    assert "Werke" in terms
    assert "KGaA" in terms


def test_context_bias_terms_deduplicate_case_insensitive():
    terms = _normalize_context_bias_terms("Scriber, scriber, SCRIBER")
    assert terms == ["Scriber"]


def test_format_mistral_segments_with_speakers_groups_contiguous_segments():
    text = format_mistral_segments_with_speakers(
        [
            {"speaker_id": "SPEAKER_00", "text": "Hallo"},
            {"speaker_id": "SPEAKER_00", "text": "weiter"},
            {"speaker_id": "SPEAKER_01", "text": "Guten Tag"},
        ]
    )

    assert text == (
        "[Speaker 1]: Hallo weiter\n\n"
        "[Speaker 2]: Guten Tag"
    )


def test_format_mistral_segments_preserves_numeric_speaker_zero():
    assert format_mistral_segments_with_speakers([
        {"speaker": 0, "text": "First"},
        {"speaker": 1, "text": "Second"},
    ]) == "[Speaker 1]: First\n\n[Speaker 2]: Second"


@pytest.mark.asyncio
async def test_mistral_buffer_finalizes_reserved_wav_in_place():
    processor = MistralAsyncProcessor(
        api_key="test-key",
        model="voxtral-mini-2602",
        session=object(),  # type: ignore[arg-type]
    )
    original_buffer = processor._buffer
    pcm = b"\x01\x02" * (16_000 * 5)
    observed: dict[str, object] = {}

    async def inspect_wav(wav_source):
        observed["sameBuffer"] = wav_source is original_buffer
        with wave.open(wav_source, "rb") as reader:
            observed["sampleRate"] = reader.getframerate()
            observed["channels"] = reader.getnchannels()
            observed["frames"] = reader.getnframes()
            observed["pcm"] = reader.readframes(reader.getnframes())
        return "done"

    processor._transcribe_wav = inspect_wav  # type: ignore[method-assign]
    processor.push_frame = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "src.mistral_stt.FrameProcessor.process_frame",
        new=AsyncMock(),
    ):
        await processor.process_frame(
            AudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert observed == {
        "sameBuffer": True,
        "sampleRate": 16_000,
        "channels": 1,
        "frames": 16_000 * 5,
        "pcm": pcm,
    }
    assert original_buffer.closed
    assert processor._buffer is not original_buffer
    assert processor._buffer.tell() == 44
    processor._buffer.close()


@pytest.mark.asyncio
async def test_mistral_async_processor_enables_diarization(monkeypatch):
    captured: dict = {}

    async def _fake_transcribe(**kwargs):
        captured.update(kwargs)
        return {
            "text": "plain fallback",
            "segments": [
                {"speaker_id": "SPEAKER_00", "text": "Hallo"},
                {"speaker_id": "SPEAKER_01", "text": "Antwort"},
            ],
        }

    monkeypatch.setattr("src.mistral_stt.transcribe_with_mistral", _fake_transcribe)

    processor = MistralAsyncProcessor(
        api_key="test-key",
        model="voxtral-mini-2602",
        language="de",
        custom_vocab="Scriber",
        session=object(),
        diarize=True,
    )

    text = await processor._transcribe_bytes(b"\0" * 320)

    assert captured["diarize"] is True
    assert captured["timestamp_granularities"] == ["segment"]
    assert captured["language"] == "de"
    assert text == "[Speaker 1]: Hallo\n\n[Speaker 2]: Antwort"
