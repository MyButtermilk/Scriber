from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import ErrorFrame

from src.core.error_taxonomy import ErrorCategory, classify_error_message, user_message_for_category
from src.core.provider_errors import provider_user_error
from src.pipeline import ConnectionErrorHandlerProcessor, ScriberPipeline


@pytest.mark.asyncio
async def test_pipeline_error_keeps_exception_origin_before_notifying_controller():
    from src.core.error_diagnostics import exception_diagnostics

    observed = []
    pipe = ScriberPipeline(service_name="azure_mai", on_error=lambda _: observed.append(pipe.last_error_diagnostics()))
    error = RuntimeError("Microphone initialization failed: private details")
    error.__cause__ = TimeoutError("private endpoint and credentials")
    frame = ErrorFrame(error="Error processing frame: microphone startup failed", exception=error)
    task = AsyncMock()

    await pipe._handle_pipeline_error(task, frame)

    assert observed == [exception_diagnostics(error)]
    assert "private" not in str(observed)
    assert pipe._terminal_error == frame.error
    task.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_audio_diagnostics_cannot_suppress_pipeline_error_notification():
    from types import SimpleNamespace
    from unittest.mock import Mock

    observed = []
    pipe = ScriberPipeline(service_name="azure_mai", on_error=observed.append)
    pipe.audio_input = SimpleNamespace(startup_diagnostics=Mock(side_effect=RuntimeError("snapshot unavailable")))
    frame = ErrorFrame(error="microphone startup failed", exception=TimeoutError())

    await pipe._handle_pipeline_error(AsyncMock(), frame)

    assert observed == [frame.error]
    assert pipe.last_error_diagnostics() == {"error_type": "TimeoutError"}


@pytest.mark.asyncio
async def test_audio_start_diagnostics_survive_failed_capture_cleanup():
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.config import Config

    pipe = ScriberPipeline(service_name="azure_mai")
    pipe._record_terminal_error("microphone startup failed")
    pipe.audio_input = SimpleNamespace(
        startup_diagnostics=lambda: {"firstLiveFrameWaitMs": 750.0, "sidecarStopConfirmed": True},
        stop=AsyncMock(),
        native_audio_stop_confirmed=lambda: True,
    )

    with patch.object(Config, "MIC_ALWAYS_ON", False):
        await pipe._cleanup_audio_input()

    assert pipe.audio_input is None
    assert pipe.last_error_diagnostics()["audio_start"] == {
        "firstLiveFrameWaitMs": 750.0,
        "sidecarStopConfirmed": True,
    }


@pytest.mark.asyncio
async def test_provider_error_frame_is_terminal_for_stop():
    pipe = ScriberPipeline(service_name="azure_mai")
    pipe._record_terminal_error(
        "azure mai error: Azure MAI transcription failed (503): ServiceUnavailable - no healthy upstream"
    )

    with pytest.raises(RuntimeError) as exc_info:
        await pipe.stop()

    assert "Azure MAI transcription failed (503)" in str(exc_info.value)


@pytest.mark.asyncio
async def test_native_microphone_error_retains_local_classification_after_pipeline_stop():
    pipe = ScriberPipeline(service_name="azure_mai")
    pipe._record_terminal_error(
        "Error processing frame: Microphone initialization failed: "
        "Rust audio capture did not deliver a first live frame in time"
    )

    with pytest.raises(RuntimeError) as exc_info:
        await pipe.stop()

    info = provider_user_error("azure_mai", exc_info.value)
    assert info.provider == "microphone"
    assert info.category is ErrorCategory.DEVICE_UNAVAILABLE
    assert info.code == "rustFirstLiveFrameTimeout"


@pytest.mark.asyncio
async def test_stop_timeout_is_terminal_error():
    pipe = ScriberPipeline(service_name="azure_mai")
    pipe.is_active = True
    pipe._start_done.clear()

    with pytest.raises(RuntimeError) as exc_info:
        await pipe.stop(timeout_secs=0.01)

    assert "Transcription did not finish within" in str(exc_info.value)


@pytest.mark.asyncio
async def test_error_handler_records_non_connection_provider_errors():
    recorded: list[str] = []
    user_errors: list[str] = []
    cleanup_calls = 0

    def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    processor = ConnectionErrorHandlerProcessor(
        on_error=user_errors.append,
        cleanup_callback=cleanup,
        on_provider_error=recorded.append,
    )
    processor.push_frame = AsyncMock()

    await processor.process_frame(
        ErrorFrame(
            error=("azure mai error: Azure MAI transcription failed (503): ServiceUnavailable - no healthy upstream")
        ),
        MagicMock(),
    )

    assert recorded == [
        "azure mai error: Azure MAI transcription failed (503): ServiceUnavailable - no healthy upstream"
    ]
    assert user_errors == []
    assert cleanup_calls == 0


@pytest.mark.asyncio
async def test_error_handler_treats_aiohttp_cannot_connect_as_terminal_network_error():
    recorded: list[str] = []
    user_errors: list[str] = []
    cleanup_calls = 0

    def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    processor = ConnectionErrorHandlerProcessor(
        on_error=user_errors.append,
        cleanup_callback=cleanup,
        on_provider_error=recorded.append,
    )
    processor.push_frame = AsyncMock()
    error = "modulate realtime error: Cannot connect to host modulate-developer-apis.com:443 ssl:default"

    await processor.process_frame(ErrorFrame(error=error), MagicMock())
    await processor.process_frame(ErrorFrame(error=error), MagicMock())

    assert recorded == [error]
    assert user_errors == [error]
    assert cleanup_calls == 1
    processor.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_handler_stops_capture_for_fatal_gemini_provider_error():
    recorded: list[str] = []
    user_errors: list[str] = []
    cleanup_calls = 0

    def cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    processor = ConnectionErrorHandlerProcessor(
        on_error=user_errors.append,
        cleanup_callback=cleanup,
        on_provider_error=recorded.append,
    )
    processor.push_frame = AsyncMock()
    error = "Gemini live transcription provider failed: quota exhausted"

    await processor.process_frame(ErrorFrame(error=error, fatal=True), MagicMock())
    await processor.process_frame(ErrorFrame(error=error, fatal=True), MagicMock())

    assert recorded == [error]
    assert user_errors == [error]
    assert cleanup_calls == 1
    processor.push_frame.assert_not_awaited()


def test_azure_mai_no_healthy_upstream_gets_friendly_provider_message():
    category = classify_error_message(
        "azure mai error: Azure MAI transcription failed (503): "
        "MAI service returned an error: ServiceUnavailable - no healthy upstream"
    )

    assert category is ErrorCategory.TRANSIENT_PROVIDER
    assert user_message_for_category(category) == "STT service is temporarily unavailable. Please try again shortly."
