from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import ErrorFrame

from src.core.error_taxonomy import ErrorCategory, classify_error_message, user_message_for_category
from src.pipeline import ConnectionErrorHandlerProcessor, ScriberPipeline


@pytest.mark.asyncio
async def test_provider_error_frame_is_terminal_for_stop():
    pipe = ScriberPipeline(service_name="azure_mai")
    pipe._record_terminal_error(
        "azure mai error: Azure MAI transcription failed (503): "
        "ServiceUnavailable - no healthy upstream"
    )

    with pytest.raises(RuntimeError) as exc_info:
        await pipe.stop()

    assert "Azure MAI transcription failed (503)" in str(exc_info.value)


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

    await processor.process_frame(
        ErrorFrame(
            error=(
                "azure mai error: Azure MAI transcription failed (503): "
                "ServiceUnavailable - no healthy upstream"
            )
        ),
        MagicMock(),
    )

    assert recorded == [
        "azure mai error: Azure MAI transcription failed (503): "
        "ServiceUnavailable - no healthy upstream"
    ]
    assert user_errors == []
    assert cleanup_calls == 0


def test_azure_mai_no_healthy_upstream_gets_friendly_provider_message():
    category = classify_error_message(
        "azure mai error: Azure MAI transcription failed (503): "
        "MAI service returned an error: ServiceUnavailable - no healthy upstream"
    )

    assert category is ErrorCategory.TRANSIENT_PROVIDER
    assert (
        user_message_for_category(category)
        == "STT service is temporarily unavailable. Please try again shortly."
    )
