from __future__ import annotations

import asyncio
import json

import pytest

from src.config import Config
from src.gladia_stt import (
    gladia_transcript_payload_to_text,
    transcribe_with_gladia_pre_recorded,
)
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


def test_gladia_payload_to_text_prefers_speaker_utterances():
    payload = {
        "result": {
            "transcription": {
                "full_transcript": "fallback transcript",
                "utterances": [
                    {"speaker": 0, "text": "Hallo"},
                    {"speaker": 1, "text": "Guten Tag"},
                ],
            }
        }
    }

    assert gladia_transcript_payload_to_text(payload, prefer_speaker_labels=True) == (
        "[Speaker 1]: Hallo\n\n"
        "[Speaker 2]: Guten Tag"
    )
    assert gladia_transcript_payload_to_text(payload, prefer_speaker_labels=False) == "fallback transcript"


@pytest.mark.asyncio
async def test_transcribe_with_gladia_pre_recorded_happy_path(monkeypatch):
    async def _no_sleep(_seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, payload={"audio_url": "https://audio.example/upload.wav"}),
            _FakeResponse(status=200, payload={"id": "job_123"}),
        ],
        get_responses=[
            _FakeResponse(status=200, payload={"status": "queued"}),
            _FakeResponse(
                status=200,
                payload={
                    "status": "done",
                    "result": {
                        "transcription": {
                            "full_transcript": "hello world",
                        }
                    },
                },
            ),
        ],
    )
    progress: list[str] = []

    payload = await transcribe_with_gladia_pre_recorded(
        session=session,
        api_key="test-key",
        audio_source=b"audio-bytes",
        filename="sample.wav",
        content_type="audio/wav",
        language="de",
        custom_vocab="Scriber, Pipecat",
        diarize=True,
        on_progress=progress.append,
    )

    assert payload["status"] == "done"
    assert session.post_calls[0][0].endswith("/v2/upload")
    assert session.post_calls[1][0].endswith("/v2/pre-recorded")
    submit_json = session.post_calls[1][1]["json"]
    assert submit_json["audio_url"] == "https://audio.example/upload.wav"
    assert submit_json["language_config"] == {"languages": ["de"], "code_switching": False}
    assert submit_json["custom_vocabulary_config"]["vocabulary"] == ["Scriber", "Pipecat"]
    assert session.delete_calls[0][0].endswith("/v2/pre-recorded/job_123")
    assert "Retrieving transcript..." in progress


@pytest.mark.asyncio
async def test_transcribe_with_gladia_pre_recorded_raises_on_provider_error(monkeypatch):
    async def _no_sleep(_seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    session = _FakeSession(
        post_responses=[
            _FakeResponse(status=200, payload={"audio_url": "https://audio.example/upload.wav"}),
            _FakeResponse(status=200, payload={"id": "job_123"}),
        ],
        get_responses=[
            _FakeResponse(status=200, payload={"status": "failed", "error_code": "audio_error"}),
        ],
    )

    with pytest.raises(RuntimeError, match="audio_error"):
        await transcribe_with_gladia_pre_recorded(
            session=session,
            api_key="test-key",
            audio_source=b"audio-bytes",
            filename="sample.wav",
            content_type="audio/wav",
            language="de",
        )


@pytest.mark.asyncio
async def test_pipeline_direct_upload_gladia_uses_pre_recorded_api(monkeypatch, tmp_path):
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"fake-audio")
    captured: list[tuple[str, bool]] = []
    progress: list[str] = []

    async def _fake_transcribe(**kwargs):
        assert kwargs["filename"] == "sample.wav"
        assert kwargs["content_type"] == "audio/wav"
        assert kwargs["language"] == "de"
        assert kwargs["custom_vocab"] == "Scriber,Pipecat"
        assert kwargs["diarize"] is True
        return {
            "status": "done",
            "result": {
                "transcription": {
                    "utterances": [{"speaker": 1, "text": "Hallo Gladia"}],
                    "full_transcript": "fallback",
                }
            },
        }

    monkeypatch.setattr("src.pipeline.transcribe_with_gladia_pre_recorded", _fake_transcribe)
    monkeypatch.setattr(Config, "GLADIA_API_KEY", "test-key")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber,Pipecat")
    monkeypatch.setattr(Config, "LANGUAGE", "de")

    pipeline = ScriberPipeline(
        service_name="gladia",
        on_transcription=lambda text, is_final: captured.append((text, is_final)),
        on_progress=lambda msg: progress.append(msg),
    )
    await pipeline.transcribe_file_direct(str(file_path))

    assert captured == [("[Speaker 1]: Hallo Gladia", True)]
    assert "Completed" in progress
