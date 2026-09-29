import asyncio
import base64
import io
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import aiohttp
import pytest
from aiohttp import web

from src import cloud_async_stt
from src.core.provider_errors import ProviderTransportError


class ReplySession:
    """HTTP boundary double that consumes/closes files like aiohttp does."""

    def __init__(self, status=429, headers=None, message="Provider returned error", stalled=False):
        self.status = status
        self.headers = headers or {}
        self.message = message
        self.stalled = stalled
        self.bodies = []
        self.request_times = []
        self.timeouts = []
        self.closed_responses = 0

    def post(self, _url, **kwargs):
        self.bodies.append(kwargs["data"])
        self.request_times.append(datetime.now(UTC))
        self.timeouts.append(kwargs["timeout"].total)
        session = self

        class Reply:
            status = session.status
            headers = session.headers

            async def __aenter__(self):
                kwargs["data"].read()
                kwargs["data"].close()
                return self

            async def __aexit__(self, *_args):
                session.closed_responses += 1

            async def text(self):
                if session.stalled:
                    await asyncio.sleep(60)
                return '{"error":{"message":"' + session.message + '"}}'

        return Reply()


async def transcribe(session, **kwargs):
    return await cloud_async_stt.transcribe_with_openrouter_audio_transcription(
        session=session,
        api_key="local-test-only",
        audio_source=b"retained-mp3",
        filename="audio.mp3",
        content_type="audio/mpeg",
        language="de",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_openrouter_retries_confirmed_429_with_identical_audio_over_real_http(monkeypatch):
    requests = []

    async def transcribe(request):
        requests.append(await request.json())
        if len(requests) < 3:
            return web.json_response(
                {"error": {"code": 429, "message": "Provider returned error"}},
                status=429,
                headers={"Retry-After": "0"},
            )
        return web.json_response({"text": "Recovered dictation"})

    app = web.Application()
    app.router.add_post("/transcriptions", transcribe)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        cloud_async_stt,
        "openrouter_stt_url",
        lambda _region: f"http://127.0.0.1:{runner.addresses[0][1]}/transcriptions",
    )
    audio = io.BytesIO(b"prefix:retained-audio")
    audio.seek(7)
    try:
        async with aiohttp.ClientSession() as session:
            result = await cloud_async_stt.transcribe_with_openrouter_audio_transcription(
                session=session,
                api_key="local-test-only",
                audio_source=audio,
                filename="audio.mp3",
                content_type="audio/mpeg",
                language="de",
            )
        assert result == {"text": "Recovered dictation"}
        assert len(requests) == 3
        assert requests[0] == requests[1] == requests[2]
        assert base64.b64decode(requests[0]["input_audio"]["data"]) == b"retained-audio"
        assert not audio.closed
    finally:
        audio.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 403, 408, 413, 500, 503])
async def test_openrouter_does_not_replay_other_http_failures(status):
    session = ReplySession(status)
    with pytest.raises(ProviderTransportError) as caught:
        await transcribe(session)
    assert caught.value.status == status
    assert len(session.bodies) == session.closed_responses == 1
    assert session.bodies[0].closed


@pytest.mark.asyncio
async def test_openrouter_does_not_retry_an_explicit_credit_failure_returned_as_429():
    session = ReplySession(message="Insufficient credits", headers={"Retry-After": "0"})
    with pytest.raises(ProviderTransportError):
        await transcribe(session)
    assert len(session.bodies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["60", "9999999999999999999999"])
async def test_openrouter_never_shortens_server_wait_to_fit_retry_budget(hint):
    session = ReplySession(headers={"Retry-After": hint})
    with pytest.raises(ProviderTransportError) as caught:
        await transcribe(session)
    assert caught.value.status == 429
    assert len(session.bodies) == session.closed_responses == 1


@pytest.mark.asyncio
async def test_openrouter_repeated_429_is_bounded_to_three_attempts():
    session = ReplySession(headers={"Retry-After": "0"})
    with pytest.raises(ProviderTransportError) as caught:
        await transcribe(session)
    assert caught.value.status == 429
    assert len(session.bodies) == session.closed_responses == 3
    assert all(body.closed for body in session.bodies)
    assert session.timeouts[0] > session.timeouts[1] > session.timeouts[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", [None, "not a date"])
async def test_openrouter_missing_or_invalid_hint_uses_backoff(hint):
    session = ReplySession(headers={"Retry-After": hint})
    with pytest.raises(ProviderTransportError):
        await transcribe(session)
    assert len(session.request_times) == 3
    assert (session.request_times[1] - session.request_times[0]).total_seconds() >= 0.5
    assert (session.request_times[2] - session.request_times[1]).total_seconds() >= 1.0


@pytest.mark.asyncio
async def test_openrouter_honors_http_date_retry_after():
    retry_at = (datetime.now(UTC) + timedelta(seconds=2)).replace(microsecond=0)
    session = ReplySession(headers={"Retry-After": format_datetime(retry_at, usegmt=True)})
    with pytest.raises(ProviderTransportError):
        await transcribe(session)
    assert len(session.request_times) == 3
    assert session.request_times[1] >= retry_at


@pytest.mark.asyncio
async def test_openrouter_cancellation_during_backoff_never_starts_another_upload():
    waiting = asyncio.Event()
    session = ReplySession(headers={"Retry-After": "4"})

    def progress(message):
        if message == "Waiting before retrying transcription...":
            waiting.set()

    task = asyncio.create_task(transcribe(session, on_progress=progress))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(session.bodies) == session.closed_responses == 1
        assert session.bodies[0].closed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_openrouter_retry_hint_cannot_extend_the_original_timeout():
    session = ReplySession(headers={"Retry-After": "1"})
    with pytest.raises(ProviderTransportError):
        await transcribe(session, timeout_secs=0.1)
    assert len(session.bodies) == 1


@pytest.mark.asyncio
async def test_openrouter_ambiguous_response_timeout_does_not_replay_billable_work():
    session = ReplySession(status=200, stalled=True)
    with pytest.raises(TimeoutError):
        await transcribe(session, timeout_secs=0.05)
    assert len(session.bodies) == session.closed_responses == 1
    assert session.bodies[0].closed


@pytest.mark.asyncio
async def test_openrouter_non_seekable_audio_is_not_replayed():
    class OneShotAudio(io.BytesIO):
        def seekable(self):
            return False

    session = ReplySession(headers={"Retry-After": "0"})
    with OneShotAudio(b"retained-mp3") as audio:
        with pytest.raises(ProviderTransportError):
            await cloud_async_stt.transcribe_with_openrouter_audio_transcription(
                session=session,
                api_key="local-test-only",
                audio_source=audio,
                filename="audio.mp3",
                content_type="audio/mpeg",
                language="de",
            )
        assert not audio.closed
    assert len(session.bodies) == session.closed_responses == 1


@pytest.mark.asyncio
async def test_openrouter_canceled_preparation_closes_its_body_without_upload(monkeypatch):
    loop = asyncio.get_running_loop()
    prepared = asyncio.Event()
    release = threading.Event()
    bodies = []
    build = cloud_async_stt._build_openrouter_stt_json_body

    def blocked_build(*args, **kwargs):
        body = build(*args, **kwargs)
        bodies.append(body)
        loop.call_soon_threadsafe(prepared.set)
        release.wait(timeout=5)
        return body

    monkeypatch.setattr(cloud_async_stt, "_build_openrouter_stt_json_body", blocked_build)
    session = ReplySession()
    task = asyncio.create_task(transcribe(session))
    try:
        await asyncio.wait_for(prepared.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(bodies) == 1
    assert bodies[0].closed
    assert not session.bodies
