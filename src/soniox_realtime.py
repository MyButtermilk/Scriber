# Connection configuration adapted from pipecat-ai 1.5.0.
# Copyright (c) 2024-2026, Daily. SPDX-License-Identifier: BSD-2-Clause

"""Handshake authentication adapter for the pinned Pipecat 1.5 Soniox client.

Connection setup and error privacy differ; token parsing, endpointing, keepalive
and reconnect orchestration remain inherited. Retain the class name because the
pipeline's manual-finalization and cleanup paths identify Soniox by name.
"""

import asyncio
import json
from contextlib import suppress

from loguru import logger
from pipecat.services.settings import assert_given
from pipecat.services.soniox.stt import (
    SonioxContextObject,
    _prepare_language_hints,
)
from pipecat.services.soniox.stt import (
    SonioxSTTService as PipecatSonioxSTTService,
)
from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State

from src.soniox_websocket import connect_soniox_websocket


class SonioxSTTService(PipecatSonioxSTTService):
    async def _connect_websocket(self):
        """Establish the websocket connection to Soniox."""
        websocket = None
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to Soniox STT")

            websocket = await connect_soniox_websocket(self._url, self._api_key, connect_factory=websocket_connect)
            self._websocket = websocket

            if not self._websocket:
                raise ConnectionError("Soniox transport unavailable")

            # If vad_force_turn_endpoint is not enabled, we need to enable endpoint detection.
            # Either one or the other is required.
            enable_endpoint_detection = not self._vad_force_turn_endpoint

            s = self._settings

            context = s.context
            if isinstance(context, SonioxContextObject):
                context = context.model_dump()

            # Send the initial configuration message.
            config = {
                "model": s.model,
                "audio_format": self._audio_format,
                "num_channels": self._num_channels,
                "enable_endpoint_detection": enable_endpoint_detection,
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

            # Send the configuration message.
            await self._websocket.send(json.dumps(config))

            await self._call_event_handler("on_connected")
            logger.debug("Connected to Soniox STT")
        except BaseException as exc:
            self._websocket = None
            if websocket is not None:
                with suppress(Exception):
                    await websocket.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            # Never forward transport exception text/objects: they can retain
            # the upgrade request, including its Authorization header.
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
