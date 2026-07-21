import asyncio
import json
import wave
from unittest.mock import AsyncMock, patch

import pytest

from pipecat.frames.frames import AudioRawFrame, EndFrame
from pipecat.processors.frame_processor import FrameDirection

from src.assemblyai_async_stt import (
    AssemblyAIUniversal35ProAsyncProcessor,
    assemblyai_transcript_payload_to_text,
    build_keyterms_from_vocab,
    build_u3pro_language_fields,
    format_assemblyai_utterances_to_scriber_text,
    transcribe_with_assemblyai_pre_recorded,
)
from src.config import Config
from src.pipeline import ScriberPipeline


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text or json.dumps(self._payload)


class _FakeSession:
    def __init__(
        self,
        *,
        post_responses: list[_FakeResponse],
        get_responses: list[_FakeResponse],
        delete_responses: list[_FakeResponse] | None = None,
    ):
        self._post_responses = list(post_responses)
        self._get_responses = list(get_responses)
        self._delete_responses = list(delete_responses or [_FakeResponse(status=204)])
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self._post_responses:
            raise AssertionError("Unexpected POST call")
        return self._post_responses.pop(0)

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self._get_responses:
            raise AssertionError("Unexpected GET call")
        return self._get_responses.pop(0)

    def delete(self, url: str, **kwargs):
        self.delete_calls.append((url, kwargs))
        if not self._delete_responses:
            raise AssertionError("Unexpected DELETE call")
        return self._delete_responses.pop(0)


@pytest.mark.asyncio
async def test_assemblyai_buffer_finalizes_reserved_wav_in_place():
    processor = AssemblyAIUniversal35ProAsyncProcessor(
        api_key="test-key",
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
        "src.assemblyai_async_stt.FrameProcessor.process_frame",
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


def test_build_keyterms_from_vocab_sanitizes_and_limits():
    keyterms = build_keyterms_from_vocab(
        " Foo , bar,\nfoo\none two three four five six\none two three four five six seven "
    )
    assert keyterms == ["Foo", "bar", "one two three four five six"]

    large_vocab = ",".join(f"term{i}" for i in range(0, 1200))
    assert len(build_keyterms_from_vocab(large_vocab)) == 1000


def test_build_u3pro_language_fields_rules():
    assert build_u3pro_language_fields("auto") == {"language_detection": True}
    assert build_u3pro_language_fields("de-DE") == {"language_code": "de"}
    assert build_u3pro_language_fields("nl") == {"language_code": "nl"}
    assert build_u3pro_language_fields("pl") == {"language_detection": True}


def test_format_assemblyai_utterances_to_scriber_text_maps_speakers():
    text = format_assemblyai_utterances_to_scriber_text(
        [
            {"speaker": "A", "text": "Hallo"},
            {"speaker": "B", "text": "Hi"},
            {"speaker": "A", "text": "Weiter"},
        ]
    )
    assert text == (
        "[Speaker 1]: Hallo\n\n"
        "[Speaker 2]: Hi\n\n"
        "[Speaker 1]: Weiter"
    )


def test_format_assemblyai_utterances_preserves_numeric_speaker_zero():
    assert format_assemblyai_utterances_to_scriber_text([
        {"speaker": 0, "text": "First"},
        {"speaker": 1, "text": "Second"},
    ]) == "[Speaker 1]: First\n\n[Speaker 2]: Second"


def test_payload_to_text_prefers_diarized_output_when_requested():
    payload = {
        "text": "fallback plain text",
        "utterances": [{"speaker": "A", "text": "segment text"}],
    }
    diarized = assemblyai_transcript_payload_to_text(payload, prefer_speaker_labels=True)
    plain = assemblyai_transcript_payload_to_text(payload, prefer_speaker_labels=False)
    assert diarized == "[Speaker 1]: segment text"
    assert plain == "fallback plain text"


@pytest.mark.asyncio
async def test_transcribe_with_assemblyai_pre_recorded_happy_path(monkeypatch):
    async def _no_sleep(_seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, payload={"upload_url": "https://cdn.example/uploaded.wav"}),
            _FakeResponse(status=200, payload={"id": "tr_123"}),
        ],
        get_responses=[
            _FakeResponse(status=200, payload={"status": "processing"}),
            _FakeResponse(
                status=200,
                payload={
                    "status": "completed",
                    "text": "hello world",
                    "utterances": [{"speaker": "A", "text": "hello world"}],
                },
            ),
        ],
    )

    payload = await transcribe_with_assemblyai_pre_recorded(
        session=session,
        api_key="test-key",
        audio_source=b"audio-bytes",
        language="nl",
        custom_vocab="Alpha, alpha, this entry has way too many words in it",
        speaker_labels=True,
    )

    assert payload.get("status") == "completed"
    assert len(session.post_calls) == 2
    submit_json = session.post_calls[1][1]["json"]
    assert submit_json["speech_models"] == ["universal-3-5-pro"]
    assert submit_json["speaker_labels"] is True
    assert submit_json["language_code"] == "nl"
    assert submit_json["keyterms_prompt"] == ["Alpha"]
    assert session.post_calls[0][1]["timeout"].total == 300.0
    assert session.delete_calls[0][0].endswith("/transcript/tr_123")


@pytest.mark.asyncio
async def test_assemblyai_long_file_uses_explicit_upload_timeout():
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, payload={"upload_url": "https://cdn.example/long.flac"}),
            _FakeResponse(status=200, payload={"id": "tr_long"}),
        ],
        get_responses=[
            _FakeResponse(status=200, payload={"status": "completed", "text": "complete"}),
        ],
    )

    await transcribe_with_assemblyai_pre_recorded(
        session=session,
        api_key="test-key",
        audio_source=b"long-audio-placeholder",
        language="en",
        timeout_secs=9_300.0,
        upload_timeout_secs=1_620.0,
    )

    assert session.post_calls[0][1]["timeout"].total == 1_620.0


@pytest.mark.asyncio
async def test_transcribe_with_assemblyai_pre_recorded_raises_on_error_status():
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, payload={"upload_url": "https://cdn.example/uploaded.wav"}),
            _FakeResponse(status=200, payload={"id": "tr_123"}),
        ],
        get_responses=[
            _FakeResponse(status=200, payload={"status": "error", "error": "transcription failed"}),
        ],
    )

    with pytest.raises(RuntimeError, match="transcription failed"):
        await transcribe_with_assemblyai_pre_recorded(
            session=session,
            api_key="test-key",
            audio_source=b"audio-bytes",
            language="de",
            custom_vocab="",
            speaker_labels=True,
        )
    assert session.delete_calls[0][0].endswith("/transcript/tr_123")


@pytest.mark.asyncio
async def test_pipeline_direct_upload_assemblyai_uses_diarized_speaker_output(
    monkeypatch,
    tmp_path,
):
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"fake-audio")

    captured: list[tuple[str, bool]] = []
    progress: list[str] = []

    async def _fake_transcribe(**kwargs):
        assert kwargs["speaker_labels"] is True
        assert kwargs["language"] == "de"
        assert kwargs["custom_vocab"] == "Scriber,Pipecat"
        assert kwargs["model"] == "universal-3-5-pro"
        assert kwargs["upload_timeout_secs"] == 300.0
        return {
            "status": "completed",
            "utterances": [
                {"speaker": "A", "text": "Hallo"},
                {"speaker": "B", "text": "Guten Tag"},
            ],
            "text": "fallback",
        }

    monkeypatch.setattr("src.pipeline.transcribe_with_assemblyai_pre_recorded", _fake_transcribe)
    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "test-key")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber,Pipecat")
    monkeypatch.setattr(Config, "LANGUAGE", "de")

    pipeline = ScriberPipeline(
        service_name="assemblyai",
        on_transcription=lambda text, is_final: captured.append((text, is_final)),
        on_progress=lambda msg: progress.append(msg),
    )
    await pipeline.transcribe_file_direct(str(file_path))

    assert captured == [
        ("[Speaker 1]: Hallo\n\n[Speaker 2]: Guten Tag", True),
    ]
    assert "Completed" in progress


@pytest.mark.asyncio
async def test_pipeline_direct_upload_uses_frozen_execution_route_after_settings_change(
    monkeypatch,
    tmp_path,
):
    file_path = tmp_path / "queued.wav"
    file_path.write_bytes(b"fake-audio")

    async def _fake_transcribe(**kwargs):
        assert kwargs["language"] == "de-DE"
        assert kwargs["custom_vocab"] == "Frozen term"
        assert kwargs["model"] == "frozen-model"
        assert kwargs["speaker_labels"] is False
        assert kwargs["timeout_secs"] == 9_300.0
        assert kwargs["upload_timeout_secs"] == 1_620.0
        return {"status": "completed", "text": "Ergebnis"}

    monkeypatch.setattr("src.pipeline.transcribe_with_assemblyai_pre_recorded", _fake_transcribe)
    monkeypatch.setattr(Config, "ASSEMBLYAI_API_KEY", "test-key")
    pipeline = ScriberPipeline(
        service_name="assemblyai",
        direct_file_speaker_diarization=False,
        direct_file_expected_duration_seconds=5 * 60 * 60,
        execution_route={
            "language": "de-DE",
            "custom_vocab": "Frozen term",
            "model": "frozen-model",
        },
    )
    monkeypatch.setattr(Config, "LANGUAGE", "en-US")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Changed term")
    monkeypatch.setattr(Config, "ASSEMBLYAI_ASYNC_MODEL", "changed-model")

    await pipeline.transcribe_file_direct(str(file_path))
