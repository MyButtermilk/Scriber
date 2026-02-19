import asyncio

import pytest

from src.assemblyai_async_stt import (
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
        return self._text


class _FakeSession:
    def __init__(self, *, post_responses: list[_FakeResponse], get_responses: list[_FakeResponse]):
        self._post_responses = list(post_responses)
        self._get_responses = list(get_responses)
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

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
    assert build_u3pro_language_fields("nl") == {"language_detection": True}


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
    assert submit_json["speech_models"] == ["universal-3-pro"]
    assert submit_json["speaker_labels"] is True
    assert submit_json["language_detection"] is True
    assert submit_json["keyterms_prompt"] == ["Alpha"]


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
