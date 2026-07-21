import asyncio
import io
import threading
import wave

import pytest
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from src.cloud_async_stt import (
    SpeechmaticsAsyncProcessor,
    _delete_speechmatics_job,
    _pcm_stream_to_wav,
    _wait_for_gemini_file_active,
    deepgram_transcript_payload_to_text,
    openai_transcript_payload_to_text,
    speechmatics_transcript_payload_to_text,
    transcribe_with_deepgram_pre_recorded,
    transcribe_with_gemini_audio,
    transcribe_with_openai_audio_transcription,
)
from src.config import Config
from src.microphone import RustCaptureWavArtifact
from src.runtime.audio_spool import create_pcm_spool


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


def test_reserved_pcm_spool_becomes_wav_without_copying_payload():
    pcm = b"\x01\x02" * 32_000
    source = create_pcm_spool(reserve_wav_header=True)
    source.write(pcm)

    wav_source = _pcm_stream_to_wav(
        source,
        16_000,
        1,
        reserved_wav_header=True,
        pcm_size=len(pcm),
    )
    try:
        assert wav_source is source
        with wave.open(wav_source, "rb") as reader:
            assert reader.getframerate() == 16_000
            assert reader.getnchannels() == 1
            assert reader.getnframes() == 32_000
            assert reader.readframes(reader.getnframes()) == pcm
    finally:
        wav_source.close()


@pytest.mark.asyncio
async def test_speechmatics_async_uses_rust_capture_wav_lease_and_releases_it():
    pcm = b"\x01\x00" * 160
    local_wav = io.BytesIO()
    with wave.open(local_wav, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(pcm)
    wav_bytes = local_wav.getvalue()

    class Artifact:
        released = False
        opened = False

        def matches_pcm(self, *, sample_rate, channels, pcm_bytes):
            return (sample_rate, channels, pcm_bytes) == (16_000, 1, len(pcm))

        def open(self):
            self.opened = True
            return io.BytesIO(wav_bytes)

        async def open_async(self):
            return self.open()

        async def release_async(self):
            self.released = True
            return True

    artifact = Artifact()
    processor = SpeechmaticsAsyncProcessor(
        api_key="test-key",
        language="en",
        session=object(),
    )
    captured = []

    async def push(frame, direction):
        captured.append((frame, direction))

    async def transcribe(wav_source):
        assert wav_source.read() == wav_bytes
        assert processor._audio_preparation_implementation == "wav_pcm16_file_v1"
        return "leased transcript"

    processor.push_frame = push
    processor._transcribe_wav = transcribe
    assert processor.adopt_capture_wav_artifact(artifact) is True

    await processor.process_frame(
        InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert artifact.opened is True
    assert artifact.released is True
    assert any(
        isinstance(frame, TranscriptionFrame) and frame.text == "leased transcript"
        for frame, _direction in captured
    )
    assert processor.adopt_capture_wav_artifact(Artifact()) is False


@pytest.mark.asyncio
async def test_speechmatics_async_cancel_during_rust_wav_open_closes_before_release(
    tmp_path,
    monkeypatch,
):
    pcm = b"\x01\x00" * 160
    lease_id = "323456781234423481234567890abcde"
    path = tmp_path / f"{lease_id}.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(pcm)

    open_started = threading.Event()
    allow_open = threading.Event()
    opened_handle = None
    release_calls = []

    def shell_call(command, payload, **_kwargs):
        assert opened_handle is not None
        assert opened_handle.closed is True
        path.unlink()
        release_calls.append((command, payload))
        return {"success": True, "payload": {"released": True}}

    artifact = RustCaptureWavArtifact(
        {
            "schemaVersion": "1",
            "leaseId": lease_id,
            "path": str(path),
            "format": "wav_pcm16",
            "contentType": "audio/wav",
            "owner": "pythonBackendLease",
            "cleanupCommand": "audioCaptureArtifactRelease",
            "byteLength": len(pcm) + 44,
            "pcmBytes": len(pcm),
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
        },
        shell_call=shell_call,
    )
    original_open = artifact.open

    def blocking_open():
        nonlocal opened_handle
        open_started.set()
        if not allow_open.wait(timeout=2.0):
            raise TimeoutError("test did not release artifact open")
        opened_handle = original_open()
        return opened_handle

    monkeypatch.setattr(artifact, "open", blocking_open)
    processor = SpeechmaticsAsyncProcessor(
        api_key="test-key",
        language="en",
        session=object(),
    )

    async def push(_frame, _direction):
        return None

    async def unexpected_transcribe(_wav_source):
        raise AssertionError("canceled artifact open must not start provider work")

    processor.push_frame = push
    processor._transcribe_wav = unexpected_transcribe
    assert processor.adopt_capture_wav_artifact(artifact) is True
    await processor.process_frame(
        InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    terminal_task = asyncio.create_task(
        processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    )
    try:
        assert await asyncio.to_thread(open_started.wait, 1.0)
        terminal_task.cancel()
        await asyncio.sleep(0)
        assert terminal_task.done() is False
    finally:
        allow_open.set()

    with pytest.raises(asyncio.CancelledError):
        await terminal_task

    assert opened_handle is not None
    assert opened_handle.closed is True
    assert artifact.released is True
    assert path.exists() is False
    assert release_calls == [
        ("audioCaptureArtifactRelease", {"leaseId": lease_id})
    ]


@pytest.mark.asyncio
async def test_speechmatics_async_rust_wav_open_error_uses_pcm_spool_fallback(
    tmp_path,
    monkeypatch,
):
    pcm = b"\x03\x00" * 160
    lease_id = "423456781234423481234567890abcde"
    path = tmp_path / f"{lease_id}.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(pcm)

    release_calls = []

    def shell_call(command, payload, **_kwargs):
        path.unlink()
        release_calls.append((command, payload))
        return {"success": True, "payload": {"released": True}}

    artifact = RustCaptureWavArtifact(
        {
            "schemaVersion": "1",
            "leaseId": lease_id,
            "path": str(path),
            "format": "wav_pcm16",
            "contentType": "audio/wav",
            "owner": "pythonBackendLease",
            "cleanupCommand": "audioCaptureArtifactRelease",
            "byteLength": len(pcm) + 44,
            "pcmBytes": len(pcm),
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
        },
        shell_call=shell_call,
    )
    open_calls = 0

    def failing_open():
        nonlocal open_calls
        open_calls += 1
        raise RuntimeError("normal Rust WAV open failure")

    monkeypatch.setattr(artifact, "open", failing_open)
    with pytest.raises(RuntimeError, match="normal Rust WAV open failure"):
        await artifact.open_async()

    processor = SpeechmaticsAsyncProcessor(
        api_key="test-key",
        language="en",
        session=object(),
    )
    captured = []

    async def push(frame, direction):
        captured.append((frame, direction))

    async def transcribe(wav_source):
        assert processor._audio_preparation_implementation == (
            "python_reserved_wav_header_v1"
        )
        with wave.open(wav_source, "rb") as reader:
            assert reader.getframerate() == 16_000
            assert reader.getnchannels() == 1
            assert reader.readframes(reader.getnframes()) == pcm
        return "PCM spool fallback transcript"

    processor.push_frame = push
    processor._transcribe_wav = transcribe
    assert processor.adopt_capture_wav_artifact(artifact) is True
    await processor.process_frame(
        InputAudioRawFrame(audio=pcm, sample_rate=16_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    assert open_calls == 2
    assert artifact.released is True
    assert path.exists() is False
    assert release_calls == [
        ("audioCaptureArtifactRelease", {"leaseId": lease_id})
    ]
    assert any(
        isinstance(frame, TranscriptionFrame)
        and frame.text == "PCM spool fallback transcript"
        for frame, _direction in captured
    )


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
    monkeypatch.setattr(Config, "GEMINI_STT_MODEL", "changed-after-route-freeze")
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
        model="frozen-gemini-model",
        language="en",
    )

    assert payload["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert session.posts[0][0].endswith(
        "/models/frozen-gemini-model:generateContent"
    )
    assert read_threads and all(thread_id != event_loop_thread for thread_id in read_threads)
    assert encode_threads and all(thread_id != event_loop_thread for thread_id in encode_threads)


@pytest.mark.asyncio
async def test_deepgram_request_uses_explicit_frozen_model(monkeypatch):
    monkeypatch.setattr(Config, "DEEPGRAM_MODEL", "changed-after-route-freeze")

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"results":{"channels":[]}}'

    class Session:
        def __init__(self):
            self.posts = []

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return Response()

    session = Session()
    await transcribe_with_deepgram_pre_recorded(
        session=session,
        api_key="key",
        audio_source=b"audio",
        filename="audio.wav",
        content_type="audio/wav",
        model="frozen-deepgram-model",
        language="en",
    )

    assert session.posts[0][0] == "https://api.deepgram.com/v1/listen"
    assert ("model", "frozen-deepgram-model") in session.posts[0][1]["params"]


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


@pytest.mark.asyncio
async def test_openai_batch_requests_word_timestamps_for_local_speaker_alignment():
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"text":"hello","words":[{"word":"hello","start":0,"end":0.4}]}'

    class Session:
        fields: dict[str, object]

        def post(self, _url, **kwargs):
            data = kwargs["data"]
            self.fields = {
                str(field[0].get("name")): field[2]
                for field in data._fields
            }
            return Response()

    session = Session()
    payload = await transcribe_with_openai_audio_transcription(
        session=session,
        api_key="secret",
        audio_source=b"audio",
        filename="meeting.wav",
        content_type="audio/wav",
        model="gpt-4o-mini-transcribe-2025-12-15",
        language="en",
    )

    assert session.fields["response_format"] == "verbose_json"
    assert session.fields["timestamp_granularities[]"] == "word"
    assert payload["words"][0]["start"] == 0


def test_speechmatics_payload_builds_text_from_results():
    payload = {
        "results": [
            {"type": "word", "alternatives": [{"content": "Hello"}]},
            {"type": "word", "alternatives": [{"content": "world"}]},
            {"type": "punctuation", "alternatives": [{"content": "."}]},
        ]
    }

    assert speechmatics_transcript_payload_to_text(payload, prefer_speaker_labels=False) == "Hello world."


def test_speechmatics_payload_preserves_numeric_speaker_zero():
    payload = {
        "results": [
            {"type": "word", "alternatives": [{"content": "First", "speaker": 0}]},
            {"type": "word", "alternatives": [{"content": "Second", "speaker": 1}]},
        ]
    }

    assert speechmatics_transcript_payload_to_text(
        payload, prefer_speaker_labels=True
    ) == "[Speaker 1]: First\n\n[Speaker 2]: Second"
