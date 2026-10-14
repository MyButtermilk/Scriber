"""Connection authentication adapter for the pinned Pipecat 1.5 Soniox service."""

import asyncio
import json
from contextlib import suppress

from pipecat.services.settings import assert_given
from pipecat.services.soniox.stt import (
    SonioxContextObject,
    _prepare_language_hints,
)
from pipecat.services.soniox.stt import SonioxSTTService as PipecatSonioxSTTService
from websockets.protocol import State

from src.soniox_websocket import connect_soniox_websocket


class SonioxSTTService(PipecatSonioxSTTService):
    """Keep Pipecat's parser, endpointing, keepalive, and reconnect lifecycle."""

    async def _connect_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return
        websocket = None
        try:
            websocket = await connect_soniox_websocket(self._url, self._api_key)
            s = self._settings
            context = s.context
            if isinstance(context, SonioxContextObject):
                context = context.model_dump()
            # Match the pinned upstream configuration, without legacy api_key.
            config = {
                "model": s.model,
                "audio_format": self._audio_format,
                "num_channels": self._num_channels,
                "enable_endpoint_detection": not self._vad_force_turn_endpoint,
                "max_endpoint_delay_ms": s.max_endpoint_delay_ms,
                "endpoint_sensitivity": s.endpoint_sensitivity,
                "endpoint_latency_adjustment_level": s.endpoint_latency_adjustment_level,
                "sample_rate": self.sample_rate,
                "language_hints": _prepare_language_hints(assert_given(s.language_hints)),
                "language_hints_strict": s.language_hints_strict,
                "context": context,
                "enable_speaker_diarization": s.enable_speaker_diarization,
                "enable_language_identification": s.enable_language_identification,
                "client_reference_id": s.client_reference_id,
            }
            await websocket.send(json.dumps(config))
            self._websocket = websocket
            await self._call_event_handler("on_connected")
        except BaseException as exc:
            self._websocket = None
            if websocket is not None:
                with suppress(Exception):
                    await websocket.close()
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            await self.push_error(error_msg=f"Unable to connect to Soniox ({type(exc).__name__})")

    async def _get_websocket(self):
        # Authentication errors can arrive immediately after opening, without
        # `tokens`. Supply that field for the pinned parser and omit raw error
        # text, which may echo credentials. Successful token messages pass intact.
        async for message in super()._get_websocket():
            try:
                content = json.loads(message)
            except ValueError, TypeError:
                raise ConnectionError("Invalid Soniox realtime response") from None
            if not isinstance(content, dict):
                raise ConnectionError("Invalid Soniox realtime response")
            if content.get("error_code") or content.get("error_message"):
                code = content.get("error_code")
                content = {
                    "tokens": content.get("tokens", []),
                    "error_code": code if type(code) is int and 100 <= code <= 599 else 500,
                    "error_message": "Soniox realtime request rejected",
                    "finished": content.get("finished", False),
                }
                message = json.dumps(content)
            yield message
