import asyncio
import io
from pathlib import Path

import pytest
from pipecat.frames.frames import CancelFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService

from src.azure_mai_stt import (
    AzureMaiTranscribeSTTService,
    _pcm_stream_to_mp3,
    azure_mai_content_type,
    azure_mai_language_locales,
    azure_mai_model,
    azure_mai_phrase_list,
    azure_mai_region,
    azure_mai_transcript_payload_to_text,
    build_azure_mai_definition,
    prepared_azure_mai_audio_file,
    transcribe_with_azure_mai,
    validate_azure_mai_region,
)
from src.config import Config
from src.core.provider_audio_formats import ProviderAudioCapabilityError
from src.pipeline import ScriberPipeline
from src.runtime.capture_time_encoder import CaptureTimeEncoderError
from src.runtime.capture_time_encoder import CaptureTimeFfmpegEncoder
from src.runtime.ffmpeg_commands import mp3_encode_pcm_pipe_args
from src.runtime.media_tools import find_media_tool


def test_azure_mai_region_defaults_to_northeurope():
    assert azure_mai_region("") == "northeurope"
    assert azure_mai_region(None) == "northeurope"
    assert azure_mai_region("NorthEurope") == "northeurope"


def test_azure_mai_service_initializes_complete_pipecat_1_5_settings():
    service = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
        language="auto",
    )

    assert service._settings.model == "mai-transcribe-1.5"
    assert service._settings.language is None


def test_azure_mai_capture_time_candidate_defaults_off(monkeypatch):
    monkeypatch.delenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", raising=False)

    service = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
    )

    assert service._capture_encoder_enabled is False


def test_azure_mai_capture_time_candidate_is_frozen_per_service(monkeypatch):
    monkeypatch.setenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "0")
    service = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
    )
    monkeypatch.setenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "1")

    service._offer_capture_encoded_frame(b"\0\0")

    assert service._capture_encoder_enabled is False
    assert service._capture_encoder is None


def test_validate_azure_mai_region_rejects_unsupported():
    assert validate_azure_mai_region("northeurope") == "northeurope"
    try:
        validate_azure_mai_region("westeurope")
    except ValueError as exc:
        assert "not supported" in str(exc)
        assert "northeurope" in str(exc)
    else:
        raise AssertionError("Expected unsupported MAI region to fail")


def test_build_azure_mai_definition_sets_model_and_optional_locale():
    assert build_azure_mai_definition("auto", custom_vocab="") == {
        "enhancedMode": {
            "enabled": True,
            "model": "mai-transcribe-1.5",
        }
    }
    assert build_azure_mai_definition("de", custom_vocab="") == {
        "enhancedMode": {
            "enabled": True,
            "model": "mai-transcribe-1.5",
        },
        "locales": ["de"],
    }


def test_azure_mai_model_can_be_overridden(monkeypatch):
    assert azure_mai_model("") == "mai-transcribe-1.5"

    monkeypatch.setattr("src.azure_mai_stt.Config.AZURE_MAI_MODEL", "mai-transcribe-1")
    assert (
        build_azure_mai_definition("en", custom_vocab="")["enhancedMode"]["model"]
        == "mai-transcribe-1"
    )
    assert (
        build_azure_mai_definition("en", model="custom-model", custom_vocab="")["enhancedMode"]["model"]
        == "custom-model"
    )


def test_azure_mai_phrase_list_uses_custom_vocab_for_transcribe_15():
    assert azure_mai_phrase_list("Contoso, Jessie, , Rehaan") == ["Contoso", "Jessie", "Rehaan"]

    definition = build_azure_mai_definition("en", custom_vocab="Contoso, Jessie")
    assert definition["phraseList"] == {"phrases": ["Contoso", "Jessie"]}

    old_model_definition = build_azure_mai_definition(
        "en",
        model="mai-transcribe-1",
        custom_vocab="Contoso, Jessie",
    )
    assert "phraseList" not in old_model_definition


@pytest.mark.asyncio
async def test_azure_mai_request_uses_explicit_frozen_model_and_vocab(monkeypatch):
    monkeypatch.setattr(Config, "AZURE_MAI_MODEL", "changed-after-route-freeze")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed term")

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"combinedPhrases":[{"text":"done"}]}'

    class Session:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    session = Session()
    payload = await transcribe_with_azure_mai(
        session=session,
        speech_key="key",
        region="northeurope",
        audio_source=b"audio",
        filename="audio.mp3",
        content_type="audio/mpeg",
        language="de-DE",
        model="mai-transcribe-1.5",
        custom_vocab="Frozen term",
    )

    fields = {
        str(field[0].get("name")): field[2]
        for field in session.posts[0][1]["data"]._fields
    }
    assert payload["combinedPhrases"][0]["text"] == "done"
    assert '"model": "mai-transcribe-1.5"' in fields["definition"]
    assert '"phrases": ["Frozen term"]' in fields["definition"]


@pytest.mark.asyncio
async def test_azure_mai_raw_transport_marks_completion_before_json_parse(monkeypatch):
    events: list[str] = []

    async def raw_transport(**kwargs):
        events.append("transport_complete")
        assert kwargs["filename"] == "audio.mp3"
        assert kwargs["content_type"] == "audio/mpeg"
        assert kwargs["definition"]["enhancedMode"]["model"] == "mai-transcribe-1.5"
        return 200, '{"combinedPhrases":[{"text":"done"}]}'

    original_loads = __import__("json").loads

    def observed_loads(raw):
        events.append("json_parse")
        return original_loads(raw)

    monkeypatch.setattr("src.azure_mai_stt.json.loads", observed_loads)
    payload = await transcribe_with_azure_mai(
        session=object(),
        speech_key="benchmark-only-key",
        region="northeurope",
        audio_source=b"mp3",
        filename="audio.mp3",
        content_type="audio/mpeg",
        language="de-DE",
        model="mai-transcribe-1.5",
        raw_transport=raw_transport,
        on_response_complete=lambda: events.append("response_complete_marker"),
    )

    assert payload["combinedPhrases"][0]["text"] == "done"
    assert events == [
        "transport_complete",
        "response_complete_marker",
        "json_parse",
    ]


def test_pipeline_wires_local_azure_transport_without_cloud_credential(monkeypatch):
    async def raw_transport(**_kwargs):
        return 200, '{"combinedPhrases":[{"text":"done"}]}'

    marker = lambda: None
    audio_marker = lambda _name: None
    monkeypatch.setattr(
        Config,
        "AZURE_MAI_SPEECH_KEY",
        "configured-secret-must-not-reach-local-replay",
    )
    pipeline = ScriberPipeline(
        service_name="azure_mai",
        azure_mai_raw_transport=raw_transport,
        on_provider_response_complete=marker,
        on_audio_start_marker=audio_marker,
    )

    service = pipeline._create_stt_service(object())

    assert isinstance(service, AzureMaiTranscribeSTTService)
    assert service._speech_key == "local-replay"
    assert service._raw_transport is raw_transport
    assert service._on_response_complete is marker
    assert service._on_encoder_marker is audio_marker


def test_pipeline_azure_live_fails_closed_for_unknown_model(monkeypatch):
    monkeypatch.setattr(Config, "AZURE_MAI_SPEECH_KEY", "configured")
    monkeypatch.setattr(Config, "AZURE_MAI_MODEL", "unknown-model")
    pipeline = ScriberPipeline(service_name="azure_mai")

    with pytest.raises(ProviderAudioCapabilityError):
        pipeline._create_stt_service(object())


def test_azure_mai_transcript_payload_to_text_prefers_combined_phrases():
    payload = {
        "combinedPhrases": [{"text": "Hello world"}],
        "phrases": [{"text": "fallback"}],
    }

    assert azure_mai_transcript_payload_to_text(payload) == "Hello world"


def test_azure_mai_transcript_payload_to_text_falls_back_to_phrases():
    assert azure_mai_transcript_payload_to_text({"phrases": [{"text": "Hello"}, {"displayText": "world"}]}) == (
        "Hello world"
    )


def test_azure_mai_content_type_by_extension():
    assert azure_mai_content_type(Path("audio.wav")) == "audio/wav"
    assert azure_mai_content_type(Path("audio.flac")) == "audio/flac"
    assert azure_mai_content_type(Path("audio.webm")) == "audio/mpeg"


def test_azure_mai_language_locales_skips_auto():
    assert azure_mai_language_locales("auto") == []
    assert azure_mai_language_locales("en") == ["en"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".webm", ".wav", ".flac"])
async def test_prepared_azure_mai_audio_file_transcodes_non_mp3_input_to_encoded_mp3(
    monkeypatch,
    tmp_path,
    suffix,
):
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"source")

    async def fake_transcode(source_path: Path, target_path: Path):
        assert source_path == source
        assert target_path.suffix == ".mp3"
        target_path.write_bytes(b"mp3")
        return target_path

    monkeypatch.setattr("src.azure_mai_stt._transcode_to_mp3", fake_transcode)

    async with prepared_azure_mai_audio_file(source) as prepared:
        assert prepared.suffix == ".mp3"
        assert azure_mai_content_type(prepared) == "audio/mpeg"
        assert prepared.exists()

    assert not prepared.exists()


@pytest.mark.asyncio
async def test_prepared_azure_mai_audio_file_keeps_existing_mp3(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"mp3")

    async def fail_transcode(source_path: Path, target_path: Path):
        raise AssertionError("existing mp3 should not be transcoded")

    monkeypatch.setattr("src.azure_mai_stt._transcode_to_mp3", fail_transcode)

    async with prepared_azure_mai_audio_file(source) as prepared:
        assert prepared == source
        assert azure_mai_content_type(prepared) == "audio/mpeg"
        assert prepared.exists()

    assert source.exists()


@pytest.mark.asyncio
async def test_azure_mai_live_buffer_uploads_mp3_not_wav(monkeypatch):
    captured = {}

    async def fake_pcm_to_mp3(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
        assert audio_bytes == b"\x01\x02"
        assert sample_rate == 16000
        assert channels == 1
        return b"mp3-bytes"

    async def fake_transcribe_with_azure_mai(**kwargs):
        captured.update(kwargs)
        return {"combinedPhrases": [{"text": "hello"}]}

    monkeypatch.setattr("src.azure_mai_stt._pcm_to_mp3", fake_pcm_to_mp3)
    monkeypatch.setattr("src.azure_mai_stt.transcribe_with_azure_mai", fake_transcribe_with_azure_mai)

    processor = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
        language="de",
    )

    assert isinstance(processor, STTService)

    text = await processor._transcribe_bytes(b"\x01\x02")

    assert text == "hello"
    assert captured["audio_source"] == b"mp3-bytes"
    assert captured["filename"] == "audio.mp3"
    assert captured["content_type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_azure_mai_flush_uses_capture_time_mp3_without_post_stop_encode(
    monkeypatch,
):
    monkeypatch.setenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "1")

    class CaptureEncoder:
        async def finish(self):
            return io.BytesIO(b"capture-time-mp3")

    async def fail_post_stop_encode(*_args, **_kwargs):
        raise AssertionError("capture-time candidate should avoid post-Stop encode")

    uploaded = []

    async def capture_upload(source):
        uploaded.append(source.read())
        return "done"

    async def capture_push(*_args, **_kwargs):
        return None

    markers = []
    monkeypatch.setattr(
        "src.azure_mai_stt._pcm_stream_to_mp3",
        fail_post_stop_encode,
    )
    processor = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
        language="de",
        on_encoder_marker=markers.append,
    )
    processor._buffer.write(b"\0\0" * 160)
    processor._buffer_size = 320
    processor._capture_encoder = CaptureEncoder()
    processor._transcribe_mp3 = capture_upload
    processor.push_frame = capture_push

    await processor._flush_transcription(None)

    assert uploaded == [b"capture-time-mp3"]
    assert markers == ["encoder_tail_started", "encoder_tail_completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture_enabled", "expected_implementation"),
    [
        (False, "post_stop_ffmpeg_mp3_v1"),
        (True, "capture_time_ffmpeg_mp3_v1"),
    ],
)
async def test_azure_mai_raw_transport_receives_actual_preparation(
    monkeypatch,
    capture_enabled,
    expected_implementation,
):
    captured = {}

    async def raw_transport(**kwargs):
        captured.update(kwargs)
        return 200, '{"combinedPhrases":[{"text":"done"}]}'

    async def fallback_encode(*_args, **_kwargs):
        return io.BytesIO(b"post-stop-mp3")

    class CaptureEncoder:
        async def finish(self):
            return io.BytesIO(b"capture-time-mp3")

    monkeypatch.setattr(
        "src.azure_mai_stt._pcm_stream_to_mp3",
        fallback_encode,
    )
    processor = AzureMaiTranscribeSTTService(
        speech_key="local-replay",
        region="northeurope",
        language="en-US",
        session=object(),
        raw_transport=raw_transport,
        capture_time_mp3_enabled=capture_enabled,
    )
    processor._buffer.write(b"\0\0" * 160)
    processor._buffer_size = 320
    if capture_enabled:
        processor._capture_encoder = CaptureEncoder()

    async def capture_push(*_args, **_kwargs):
        return None

    processor.push_frame = capture_push
    await processor._flush_transcription(None)

    assert captured["audio_preparation_implementation"] == (
        expected_implementation
    )


@pytest.mark.asyncio
async def test_azure_mai_capture_time_failure_falls_back_before_single_upload(
    monkeypatch,
):
    monkeypatch.setenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "1")

    class FailedCaptureEncoder:
        async def finish(self):
            raise CaptureTimeEncoderError("boundedQueueOverflow")

    fallback_calls = 0

    async def fallback_encode(*_args, **_kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return io.BytesIO(b"fallback-mp3")

    uploaded = []

    async def capture_upload(source):
        uploaded.append(source.read())
        return "done"

    async def capture_push(*_args, **_kwargs):
        return None

    monkeypatch.setattr("src.azure_mai_stt._pcm_stream_to_mp3", fallback_encode)
    processor = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
        language="de",
    )
    processor._buffer.write(b"\0\0" * 160)
    processor._buffer_size = 320
    processor._capture_encoder = FailedCaptureEncoder()
    processor._transcribe_mp3 = capture_upload
    processor.push_frame = capture_push

    await processor._flush_transcription(None)

    assert fallback_calls == 1
    assert uploaded == [b"fallback-mp3"]


@pytest.mark.asyncio
async def test_azure_mai_cancel_aborts_encoder_without_provider_request(monkeypatch):
    class CaptureEncoder:
        def __init__(self):
            self.aborted = False

        async def abort(self):
            self.aborted = True

    async def base_process_frame(*_args, **_kwargs):
        return None

    async def fail_flush(*_args, **_kwargs):
        raise AssertionError("CancelFrame must not begin a provider request")

    pushed = []

    async def capture_push(frame, direction):
        pushed.append((frame, direction))

    monkeypatch.setattr(
        "src.azure_mai_stt.AIService.process_frame",
        base_process_frame,
    )
    processor = AzureMaiTranscribeSTTService(
        speech_key="key",
        region="northeurope",
    )
    encoder = CaptureEncoder()
    processor._capture_encoder = encoder
    processor._buffer.write(b"\0\0" * 160)
    processor._buffer_size = 320
    processor._flush_transcription = fail_flush
    processor.push_frame = capture_push
    terminal = CancelFrame()

    await processor.process_frame(terminal, FrameDirection.DOWNSTREAM)

    assert encoder.aborted is True
    assert processor._buffer_size == 0
    assert pushed == [(terminal, FrameDirection.DOWNSTREAM)]


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_seconds", [5, 15, 30, 60])
async def test_azure_mai_capture_time_mp3_matches_post_stop_control(
    duration_seconds,
):
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is unavailable")
    pcm = b"\0\0" * (16_000 * duration_seconds)
    baseline_stream = await _pcm_stream_to_mp3(
        io.BytesIO(pcm),
        sample_rate=16_000,
        channels=1,
    )
    try:
        baseline = baseline_stream.read()
    finally:
        baseline_stream.close()

    encoder = CaptureTimeFfmpegEncoder(
        mp3_encode_pcm_pipe_args(
            ffmpeg,
            input_sample_rate=16_000,
            input_channels=1,
            bitrate="64k",
        ),
        sample_rate=16_000,
        channels=1,
    )
    for offset in range(0, len(pcm), 32 * 1024):
        assert encoder.offer(
            pcm[offset : offset + 32 * 1024],
            sample_rate=16_000,
            channels=1,
        )
        await asyncio.sleep(0)
    candidate_stream = await encoder.finish()
    try:
        candidate = candidate_stream.read()
    finally:
        candidate_stream.close()

    assert candidate == baseline
