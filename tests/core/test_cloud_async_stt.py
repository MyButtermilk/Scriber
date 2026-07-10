import io
import threading
import wave

import pytest

from src.cloud_async_stt import (
    _delete_speechmatics_job,
    _pcm_stream_to_wav,
    _wait_for_gemini_file_active,
    deepgram_transcript_payload_to_text,
    openai_transcript_payload_to_text,
    speechmatics_transcript_payload_to_text,
    transcribe_with_gemini_audio,
)


def test_pcm_stream_to_wav_reads_source_in_bounded_chunks():
    class TrackingStream(io.BytesIO):
        read_sizes: list[int]

        def __init__(self, value: bytes):
            super().__init__(value)
            self.read_sizes = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return super().read(size)

    pcm = b"\x01\x02" * (1024 * 1024 + 17)
    source = TrackingStream(pcm)
    wav_source = _pcm_stream_to_wav(source, 16_000, 1)
    try:
        assert source.read_sizes
        assert all(size == 1024 * 1024 for size in source.read_sizes)
        with wave.open(wav_source, "rb") as reader:
            assert reader.getframerate() == 16_000
            assert reader.getnchannels() == 1
            assert reader.readframes(reader.getnframes()) == pcm
    finally:
        wav_source.close()


@pytest.mark.asyncio
async def test_speechmatics_job_cleanup_deletes_remote_job():
    class Response:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def __init__(self):
            self.calls = []

        def delete(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    await _delete_speechmatics_job(
        session=session,
        base_url="https://speechmatics.invalid/v2",
        api_key="secret",
        job_id="job-123",
    )

    assert session.calls[0][0].endswith("/jobs/job-123")
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_gemini_large_seekable_audio_streams_to_file_upload(monkeypatch):
    monkeypatch.setenv("SCRIBER_GEMINI_STT_INLINE_LIMIT_MB", "0.000001")

    class TrackingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if size < 0:
                raise AssertionError("large Gemini input must not be copied into one bytes object")
            return super().read(size)

    class Response:
        def __init__(self, *, text="", headers=None, status=200):
            self.status = status
            self._text = text
            self.headers = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return self._text

    class Session:
        def __init__(self):
            self.posts = []
            self.deletes = []
            self.responses = [
                Response(headers={"X-Goog-Upload-URL": "https://upload.invalid/file"}),
                Response(
                    text='{"file":{"name":"files/1","uri":"https://files.invalid/1","mimeType":"audio/wav","state":"ACTIVE"}}'
                ),
                Response(text='{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'),
            ]

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return self.responses.pop(0)

        def delete(self, url, **kwargs):
            self.deletes.append((url, kwargs))
            return Response(status=204)

    source = TrackingStream(b"large-audio-source")
    session = Session()
    payload = await transcribe_with_gemini_audio(
        session=session,
        api_key="key",
        audio_source=source,
        filename="audio.wav",
        content_type="audio/wav",
        language="en",
    )

    assert payload["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert session.posts[1][1]["data"] is source
    assert session.posts[1][1]["headers"]["Content-Length"] == str(len(b"large-audio-source"))
    assert session.deletes[0][0].endswith("/files/1")


@pytest.mark.asyncio
async def test_gemini_inline_audio_read_and_base64_run_off_event_loop(monkeypatch):
    monkeypatch.setenv("SCRIBER_GEMINI_STT_INLINE_LIMIT_MB", "1")
    event_loop_thread = threading.get_ident()
    read_threads: list[int] = []
    encode_threads: list[int] = []

    class TrackingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_threads.append(threading.get_ident())
            return super().read(size)

    class Response:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'

    class Session:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    from src import cloud_async_stt

    original_b64encode = cloud_async_stt.base64.b64encode

    def tracking_b64encode(value: bytes) -> bytes:
        encode_threads.append(threading.get_ident())
        return original_b64encode(value)

    monkeypatch.setattr(cloud_async_stt.base64, "b64encode", tracking_b64encode)
    session = Session()
    payload = await transcribe_with_gemini_audio(
        session=session,
        api_key="key",
        audio_source=TrackingStream(b"inline audio"),
        filename="audio.wav",
        content_type="audio/wav",
        language="en",
    )

    assert payload["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert read_threads and all(thread_id != event_loop_thread for thread_id in read_threads)
    assert encode_threads and all(thread_id != event_loop_thread for thread_id in encode_threads)


@pytest.mark.asyncio
async def test_gemini_file_upload_waits_until_remote_processing_is_active(monkeypatch):
    class Response:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"name":"files/1","uri":"https://files.invalid/1","state":"ACTIVE"}'

    class Session:
        def __init__(self):
            self.gets = []

        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            return Response()

    async def immediate_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("src.cloud_async_stt.asyncio.sleep", immediate_sleep)
    session = Session()
    result = await _wait_for_gemini_file_active(
        session=session,
        api_key="key",
        file_info={
            "name": "files/1",
            "uri": "https://files.invalid/1",
            "state": "PROCESSING",
        },
        timeout_secs=30,
    )

    assert result["state"] == "ACTIVE"
    assert session.gets[0][0].endswith("/files/1")
    assert session.gets[0][1]["params"] == {"key": "key"}


def test_deepgram_payload_formats_speaker_words():
    payload = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "hello there hi back",
                            "words": [
                                {"speaker": 0, "punctuated_word": "Hello"},
                                {"speaker": 0, "punctuated_word": "there."},
                                {"speaker": 1, "punctuated_word": "Hi"},
                                {"speaker": 1, "punctuated_word": "back."},
                            ],
                        }
                    ]
                }
            ]
        }
    }

    assert deepgram_transcript_payload_to_text(payload, prefer_speaker_labels=True) == (
        "[Speaker 1]: Hello there.\n\n[Speaker 2]: Hi back."
    )


def test_openai_payload_uses_text_fallback():
    assert (
        openai_transcript_payload_to_text({"text": "plain transcript"}, prefer_speaker_labels=True)
        == "plain transcript"
    )


def test_speechmatics_payload_builds_text_from_results():
    payload = {
        "results": [
            {"type": "word", "alternatives": [{"content": "Hello"}]},
            {"type": "word", "alternatives": [{"content": "world"}]},
            {"type": "punctuation", "alternatives": [{"content": "."}]},
        ]
    }

    assert speechmatics_transcript_payload_to_text(payload, prefer_speaker_labels=False) == "Hello world."
