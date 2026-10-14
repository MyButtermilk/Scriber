from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from websockets.asyncio.server import serve
from websockets.protocol import State

from src import soniox_websocket
from src.config import Config
from src.pipeline import ScriberPipeline
from src.soniox_realtime import SonioxSTTService
from src.soniox_region import soniox_realtime_websocket_url

KEY = "synthetic-soniox-credential-must-never-be-logged"


class Socket:
    def __init__(self, messages=()):
        self.state = State.OPEN
        self.send = AsyncMock()
        self.close = AsyncMock()
        self.ping = AsyncMock()
        self.messages = messages

    async def __aiter__(self):
        for message in self.messages:
            if isinstance(message, BaseException):
                raise message
            yield message


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_API_KEY", KEY)
    monkeypatch.setattr(Config, "SONIOX_MODE", "realtime")
    monkeypatch.setattr(Config, "LANGUAGE", "de-DE")
    monkeypatch.setattr(Config, "CUSTOM_VOCAB", "Scriber, Pipecat")
    result = ScriberPipeline(service_name="soniox")._create_stt_service(object())
    assert isinstance(result, SonioxSTTService)
    result._sample_rate = 16_000
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["us", "eu"])
async def test_pipecat_start_and_reconnect_authenticate_and_preserve_config(service, monkeypatch, region):
    from pipecat.clocks.system_clock import SystemClock
    from pipecat.processors.frame_processor import FrameProcessorSetup
    from pipecat.services.soniox import stt as upstream
    from pipecat.utils.asyncio.task_manager import TaskManager

    await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
    service._url = soniox_realtime_websocket_url(region)
    first, second, reference_socket = Socket(), Socket(), Socket()
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr("src.soniox_realtime.websocket_connect", connect)
    connected = AsyncMock()
    service._call_event_handler = connected
    try:
        await service._connect_websocket()
        # An already-open connection must not authenticate or configure twice.
        await service._connect_websocket()
        assert connect.await_count == 1
        first.state = State.CLOSED
        assert await service._reconnect_websocket(1)
        first.close.assert_awaited_once()
        second.ping.assert_awaited_once()
        assert connect.await_count == 2
        for call in connect.await_args_list:
            assert call.args == (service._url,)
            assert call.kwargs["additional_headers"] == {"Authorization": f"Bearer {KEY}"}
            assert not call.kwargs["logger"].isEnabledFor(logging.DEBUG)
        assert [call.args[0] for call in connected.await_args_list] == [
            "on_connected",
            "on_disconnected",
            "on_connected",
        ]

        # Pin parity with the installed 1.5.0 configuration, not a partial mock.
        monkeypatch.setattr(upstream, "websocket_connect", AsyncMock(return_value=reference_socket))
        reference = upstream.SonioxSTTService(
            api_key=KEY, sample_rate=16_000, settings=service._settings, vad_force_turn_endpoint=False
        )
        reference._sample_rate = 16_000
        try:
            await reference._connect_websocket()
            expected = json.loads(reference_socket.send.await_args.args[0])
            assert expected.pop("api_key") == KEY
            for socket in (first, second):
                socket.send.assert_awaited_once()
                actual = json.loads(socket.send.await_args.args[0])
                assert actual == expected
                assert KEY not in socket.send.await_args.args[0]
        finally:
            await reference.cleanup()
    finally:
        await service._disconnect()
        await service.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["handshake", "config", "cancel"])
async def test_pipecat_connection_failure_is_private_and_cleans_up(service, monkeypatch, failure):
    socket = Socket()
    error = RuntimeError(f"Authorization: Bearer {KEY}")
    connect = AsyncMock(return_value=socket)
    if failure == "handshake":
        connect.side_effect = error
    else:
        socket.send.side_effect = asyncio.CancelledError() if failure == "cancel" else error
    monkeypatch.setattr("src.soniox_realtime.websocket_connect", connect)
    frames = []
    service.push_frame = AsyncMock(side_effect=lambda frame, *_args, **_kwargs: frames.append(frame))
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="DEBUG")
    try:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await service._connect_websocket()
            assert not frames
        else:
            await service._connect_websocket()
            assert len(frames) == 1
            assert "Unable to connect to Soniox" in frames[0].error
            assert frames[0].exception is None
        assert service._websocket is None
        assert socket.close.await_count == (failure != "handshake")
        assert KEY not in "".join(logs)
        assert KEY not in repr(frames)
    finally:
        logger.remove(sink)
        await service.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tokens", [False, True])
async def test_pipecat_auth_error_frame_is_reported_without_raw_credentials(service, with_tokens):
    payload = {"error_code": 401, "error_type": "unauthenticated", "error_message": f"Rejected {KEY}"}
    if with_tokens:
        payload["tokens"] = []
    service._websocket = Socket([json.dumps(payload)])
    frames = []
    service.push_frame = AsyncMock(side_effect=lambda frame, *_args, **_kwargs: frames.append(frame))
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="DEBUG")
    try:
        await service._receive_messages()
        assert len(frames) == 1
        assert "401" in frames[0].error
        assert KEY not in repr(frames)
        assert KEY not in "".join(logs)
    finally:
        logger.remove(sink)
        await service.cleanup()


@pytest.mark.asyncio
async def test_error_response_preserves_final_tokens_and_suppresses_raw_error(service):
    from pipecat.frames.frames import ErrorFrame, TranscriptionFrame

    service._websocket = Socket(
        [
            json.dumps({"tokens": [{"text": "Vorher ", "is_final": True}]}),
            json.dumps(
                {
                    "tokens": [{"text": "fertig.", "is_final": True}],
                    "error_code": 500,
                    "error_message": KEY,
                }
            ),
        ]
    )
    frames = []
    service.push_frame = AsyncMock(side_effect=lambda frame, *_args, **_kwargs: frames.append(frame))
    service._user_turn_started = AsyncMock()
    service._user_turn_stopped = AsyncMock()
    try:
        await service._receive_messages()
        assert [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)] == ["Vorher fertig."]
        assert len([frame for frame in frames if isinstance(frame, ErrorFrame)]) == 1
        assert KEY not in repr(frames)
    finally:
        await service.cleanup()


@pytest.mark.asyncio
async def test_real_upgrade_sends_bearer_header_without_debug_log_leak(caplog):
    requests = []

    async def handler(websocket):
        requests.append(websocket.request)
        await websocket.wait_closed()

    # Capture all application DEBUG logs; the test server must not itself log
    # the received synthetic credential as though it were client diagnostics.
    server_logger = logging.Logger("test.soniox.server", level=logging.CRITICAL + 1)
    server_logger.propagate = False
    with caplog.at_level(logging.DEBUG):
        async with serve(handler, "127.0.0.1", 0, logger=server_logger) as server:
            port = server.sockets[0].getsockname()[1]
            websocket = await soniox_websocket.connect_soniox_websocket(
                f"ws://127.0.0.1:{port}/transcribe-websocket", KEY
            )
            await websocket.close()
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {KEY}"
    assert "Sec-WebSocket-Protocol" not in requests[0].headers
    assert KEY not in requests[0].path
    assert KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "ping", "close", "receive"])
@pytest.mark.parametrize("code", [1000, 1008])
async def test_transport_errors_preserve_close_class_and_code_without_reason(operation, code):
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
    from websockets.frames import Close

    error_type = ConnectionClosedOK if code == 1000 else ConnectionClosedError
    error = error_type(Close(code, KEY), Close(code, KEY), True)
    socket = Socket([error])
    if operation != "receive":
        getattr(socket, operation).side_effect = error
    websocket = await soniox_websocket.connect_soniox_websocket(
        "wss://synthetic.invalid", KEY, connect_factory=AsyncMock(return_value=socket)
    )
    with pytest.raises(error_type) as caught:
        if operation == "receive":
            await anext(websocket)
        elif operation == "send":
            await websocket.send(b"audio")
        else:
            await getattr(websocket, operation)()
    assert caught.value.rcvd.code == code
    assert caught.value.sent.code == code
    assert caught.value.rcvd_then_sent is True
    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)


@pytest.mark.asyncio
async def test_real_server_close_reason_never_reaches_pipecat_logs_or_errors(service):
    from websockets.exceptions import ConnectionClosedError

    closed = asyncio.Event()

    async def handler(websocket):
        await websocket.recv()
        await websocket.close(code=1008, reason=f"Invalid API key {KEY}")
        closed.set()

    server_logger = logging.Logger("test.soniox.server", level=logging.CRITICAL + 1)
    server_logger.propagate = False
    frames = []
    service.push_frame = AsyncMock(side_effect=lambda frame, *_args, **_kwargs: frames.append(frame))
    service._reconnect_on_error = False
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="DEBUG")
    try:
        async with serve(handler, "127.0.0.1", 0, logger=server_logger) as server:
            service._url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            await service._connect_websocket()
            await asyncio.wait_for(closed.wait(), timeout=2)
            with pytest.raises(ConnectionClosedError):
                await service._websocket.send(b"audio")
            assert not await service._verify_connection()
            await service._receive_task_handler(service._report_error)
        assert frames
        assert any("1008" in frame.error for frame in frames)
        assert KEY not in "".join(logs)
        assert KEY not in repr(frames)
    finally:
        logger.remove(sink)
        await service.cleanup()


@pytest.mark.asyncio
async def test_meeting_handshake_failure_never_exposes_credentials():
    from src.meeting_live_stt import SonioxMeetingStream

    stream = SonioxMeetingStream(
        meeting_id="auth-test",
        source="system",
        api_key=KEY,
        model="stt-rt-v5",
        language="de",
        diarization=True,
        on_segment=AsyncMock(),
        on_gap=AsyncMock(),
        connect_factory=AsyncMock(side_effect=RuntimeError(f"request Authorization: Bearer {KEY}")),
    )
    with pytest.raises(RuntimeError) as caught:
        await stream.start()
    assert KEY not in str(caught.value)
    assert stream.websocket is None
    assert stream.supervisor_task is None
    await stream.stop()
