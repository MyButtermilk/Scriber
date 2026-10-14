"""No provider traffic: exercise handshake auth with doubles and loopback sockets."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from pipecat.utils.asyncio.task_manager import TaskManager
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.protocol import State

from src.meeting_live_stt import SonioxMeetingStream
from src.soniox_realtime import SonioxSTTService
from src.soniox_region import soniox_realtime_websocket_url
from src.soniox_websocket import soniox_connect_options

KEY = "synthetic-soniox-credential-must-not-be-logged"


def meeting(**kwargs):
    return SonioxMeetingStream(
        meeting_id="auth-test",
        source="system",
        api_key=KEY,
        model="stt-rt-v5",
        language="de",
        diarization=True,
        on_segment=AsyncMock(),
        on_gap=AsyncMock(),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us", "eu"])
@pytest.mark.parametrize("kind", ["meeting", "dictation"])
async def test_every_connection_and_reconnect_authenticates_before_config(monkeypatch, region, kind):
    calls = []
    sockets = []

    async def open_socket(url, *, additional_headers, logger):
        assert url == soniox_realtime_websocket_url(region)
        assert additional_headers == {"Authorization": f"Bearer {KEY}"}
        assert logger.disabled
        calls.append(url)
        socket = AsyncMock()
        socket.state = State.OPEN
        sockets.append(socket)
        return socket

    if kind == "meeting":
        service = meeting(connect_factory=open_socket, realtime_url=soniox_realtime_websocket_url(region))
        for _ in range(2):
            socket = await service._open_websocket()
            await socket.close()
    else:
        monkeypatch.setattr("src.soniox_realtime.websocket_connect", open_socket)
        service = SonioxSTTService(
            api_key=KEY, url=soniox_realtime_websocket_url(region), sample_rate=16000, vad_force_turn_endpoint=False
        )
        service._task_manager = TaskManager()
        service._call_event_handler = AsyncMock()
        await service._connect_websocket()
        await service._connect_websocket()  # Open connection is reused.
        assert len(calls) == 1
        assert await service._reconnect_websocket(1)
        await service._cancel_keepalive_task()
        await service._disconnect_websocket()
        assert service.__class__.__name__ == "SonioxSTTService"
    assert len(calls) == 2
    for socket in sockets:
        config = json.loads(socket.send.call_args.args[0])
        assert "api_key" not in config
        assert KEY not in socket.send.call_args.args[0]
        assert config["model"] == "stt-rt-v5"
        assert config["audio_format"] == "pcm_s16le"
        assert config["enable_endpoint_detection"] is True
        socket.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["meeting", "dictation"])
@pytest.mark.parametrize("failure_stage", ["handshake", "config", "cancel"])
async def test_failed_setup_does_not_expose_credentials_and_closes_socket(monkeypatch, kind, failure_stage):
    socket = AsyncMock()
    failure = asyncio.CancelledError() if failure_stage == "cancel" else RuntimeError(f"Authorization: Bearer {KEY}")
    socket.send.side_effect = failure
    connector = AsyncMock(side_effect=failure) if failure_stage == "handshake" else AsyncMock(return_value=socket)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    try:
        if kind == "meeting":
            service = meeting(connect_factory=connector)
            with pytest.raises(asyncio.CancelledError if failure_stage == "cancel" else RuntimeError) as caught:
                await service._open_websocket()
            assert KEY not in str(caught.value)
        else:
            monkeypatch.setattr("src.soniox_realtime.websocket_connect", connector)
            service = SonioxSTTService(api_key=KEY)
            service.push_error = AsyncMock()
            if failure_stage == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await service._connect_websocket()
            else:
                await service._connect_websocket()
                assert KEY not in str(service.push_error.call_args)
                assert "exception" not in service.push_error.call_args.kwargs
            assert service._websocket is None
        if failure_stage != "handshake":
            socket.close.assert_awaited_once()
        assert KEY not in "".join(messages)
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
async def test_real_handshake_and_frames_do_not_leak_key_with_debug_logging(caplog):
    # The server must not log the intentionally received credential either.
    server_logger = logging.Logger("test.soniox.server")
    server_logger.disabled = True
    received = []

    async def handler(socket):
        received.append(socket.request.headers.get("Authorization"))
        received.append(json.loads(await socket.recv()))
        await socket.send('{"finished":true}')

    caplog.set_level(logging.DEBUG)
    async with serve(handler, "127.0.0.1", 0, logger=server_logger) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", **soniox_connect_options(KEY)) as socket:
            await socket.send('{"model":"stt-rt-v5"}')
            assert json.loads(await socket.recv()) == {"finished": True}
    assert received == [f"Bearer {KEY}", {"model": "stt-rt-v5"}]
    assert KEY not in caplog.text
