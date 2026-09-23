"""Issue #47: a first paste is not proof that a session has no remaining text."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import TranscriptionFrame

from src.gemini_realtime_stt import GeminiTranscribeLiveSTTService

# Reuse the real-controller continuation harness rather than model FIFO with
# unrelated futures. Pytest's default prepend import mode exposes this module.
pytest_plugins = ["test_live_mic_continuation"]


@pytest.mark.asyncio
async def test_late_gemini_predecessor_final_cannot_be_overtaken_by_ready_successor(continuation):
    first, first_record = await continuation.start("")
    service = GeminiTranscribeLiveSTTService(
        api_key="test-only",
        final_quiet_seconds=0.01,
        final_timeout_seconds=15.0,
    )
    inferred_drain_started = asyncio.Event()
    release_late_final = asyncio.Event()
    continuation.cleanup_gates.append(release_late_final)

    async def forward(frame, _direction):
        if isinstance(frame, TranscriptionFrame):
            first.callbacks["on_transcription"](frame.text, True)
            if first.text_injection_enabled:
                continuation.injected.append(frame.text)
                first.callbacks["on_text_injected"](frame.text)

    service.push_frame = AsyncMock(side_effect=forward)
    wait_for_final = service._wait_for_final_drain

    async def observe_drain(generation):
        if service._drain_is_inferred:
            inferred_drain_started.set()
        return await wait_for_final(generation)

    service._wait_for_final_drain = observe_drain

    class WebSocket:
        closed = False

        async def send_str(self, value):
            if "audio" in json.loads(value).get("realtimeInput", {}):
                await service._handle_response(
                    json.dumps(
                        {
                            "serverContent": {
                                "interimInputTranscription": {"text": "Offen A erweitert"},
                                "inputTranscription": {"text": "Weiterer Satz im ersten Absatz."},
                                "generationComplete": True,
                            }
                        }
                    )
                )

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket
    await service._handle_response(
        json.dumps({"serverContent": {"inputTranscription": {"text": "Erster Absatz."}}})
    )
    await service._handle_response(
        json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}})
    )
    service._pcm_buffer.extend(bytes(3200))

    first_stop = continuation.stop()
    await asyncio.wait_for(first.capture_stopped.wait(), 2)

    async def drain():
        try:
            await service._close(wait_for_final=True)
        finally:
            first.provider_gate.set()

    async def deliver_late_final():
        await release_late_final.wait()
        assert not websocket.closed
        await service._handle_response(
            json.dumps({"serverContent": {"inputTranscription": {"text": "Spaeter finalisierter Nachsatz."}}})
        )

    draining = asyncio.create_task(drain())
    delivering = asyncio.create_task(deliver_late_final())
    try:
        await asyncio.wait_for(inferred_drain_started.wait(), 2)
        second, second_record = await continuation.start("Zweiter Absatz.")
        second_stop = continuation.stop()
        await asyncio.wait_for(second.capture_stopped.wait(), 2)
        second.provider_gate.set()
        await asyncio.wait_for(second.provider_completed.wait(), 2)

        # A per-chunk on_text_injected/inject_done cannot release B: A still
        # owes a real final. generationComplete is not a correlated input EOS.
        assert continuation.injected == ["Erster Absatz.", "Weiterer Satz im ersten Absatz."]
        assert not second_stop.done()
        assert not draining.done()

        release_late_final.set()
        await asyncio.wait_for(asyncio.gather(delivering, draining, first_stop, second_stop), 3)
        assert continuation.injected == [
            "Erster Absatz.",
            "Weiterer Satz im ersten Absatz.",
            "Spaeter finalisierter Nachsatz.",
            "Zweiter Absatz.",
        ]
        assert "Spaeter finalisierter Nachsatz." in first_record.content_text()
        assert second_record.content_text() == "Zweiter Absatz."
        assert not service._terminal_failure
        assert websocket.closed
    finally:
        release_late_final.set()
        for task in (delivering, draining):
            if not task.done():
                task.cancel()
        await asyncio.gather(delivering, draining, return_exceptions=True)
        await service._close(wait_for_final=False)
        first.provider_gate.set()
