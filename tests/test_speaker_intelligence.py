from __future__ import annotations

import hashlib
import wave

import numpy as np
import pytest

from src import speaker_intelligence
from src.speaker_intelligence import WeSpeakerModel


class FakeContent:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def iter_chunked(self, _size):
        yield self.payload[:3]
        yield self.payload[3:]


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.content = FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, payload: bytes):
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return FakeResponse(self.payload)


@pytest.mark.asyncio
async def test_model_download_is_atomic_and_checksum_verified(monkeypatch, tmp_path):
    payload = b"verified-model"
    monkeypatch.setattr(speaker_intelligence, "MODEL_SIZE", len(payload))
    monkeypatch.setattr(speaker_intelligence, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    model = WeSpeakerModel(tmp_path)
    status = await model.download(FakeSession(payload))
    assert status["installed"] is True
    assert model.path.read_bytes() == payload
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.asyncio
async def test_embedding_input_is_fixed_local_waveform_and_never_contains_text(monkeypatch, tmp_path):
    model = WeSpeakerModel(tmp_path / "model")
    model.root.mkdir(parents=True)
    model.path.write_bytes(b"model")
    monkeypatch.setattr(speaker_intelligence, "MODEL_SIZE", 5)

    class Runtime:
        def run(self, outputs, inputs):
            assert outputs == ["embedding"]
            assert inputs["waveform"].shape == (3, 160_000)
            assert inputs["mask"].shape == (3, 589)
            result = np.zeros((3, 256), dtype=np.float32)
            result[0, 0] = 1.0
            return [result]

    model._session = Runtime()
    audio = tmp_path / "speaker.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * 48_000)
    embedding = await model.extract(audio, 0, 3_000)
    assert len(embedding) == 256
    assert embedding[0] == 1.0
