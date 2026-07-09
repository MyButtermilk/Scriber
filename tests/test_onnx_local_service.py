from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import STTUpdateSettingsFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.transcriptions.language import Language

from src import onnx_local_service


def test_onnx_services_initialize_complete_pipecat_1_5_settings(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)

    segmented = onnx_local_service.OnnxLocalSTTService(
        model_name="test-model",
        language="auto",
    )
    buffered = onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="test-model",
        language="de",
    )

    assert segmented._settings.model == "test-model"
    assert segmented._settings.language is None
    assert buffered._settings.model == "test-model"
    assert buffered._settings.language == "de"


@pytest.mark.asyncio
async def test_buffered_onnx_service_consumes_pipecat_1_5_settings_delta(monkeypatch):
    monkeypatch.setattr(onnx_local_service, "is_onnx_available", lambda: True)
    service = onnx_local_service.OnnxLocalBufferedSTTService(
        model_name="initial-model",
        language="auto",
    )
    service._update_settings = AsyncMock(return_value={})
    service.push_frame = AsyncMock()
    delta = STTSettings(model="replacement-model", language=Language.DE)

    await service.process_frame(
        STTUpdateSettingsFrame(delta=delta, service=service),
        FrameDirection.DOWNSTREAM,
    )

    service._update_settings.assert_awaited_once_with(delta)
    service.push_frame.assert_not_awaited()
    assert service._model_name == "replacement-model"
    assert service._language == "de"
    assert service._local_settings == {
        "model": "replacement-model",
        "language": "de",
        "quantization": "int8",
    }
