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


def test_model_status_treats_concurrent_file_disappearance_as_not_installed(
    tmp_path,
):
    class VanishingPath:
        @staticmethod
        def is_file():
            return True

        @staticmethod
        def stat():
            raise FileNotFoundError("removed concurrently")

    model = WeSpeakerModel(tmp_path)
    model.path = VanishingPath()

    status = model.status()

    assert status["installed"] is False
    assert status["byteSize"] == 0


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


@pytest.mark.asyncio
async def test_in_memory_enrollment_uses_three_windows_and_returns_normalized_centroid(
    monkeypatch, tmp_path
):
    model = WeSpeakerModel(tmp_path / "model")
    model.root.mkdir(parents=True)
    model.path.write_bytes(b"model")
    monkeypatch.setattr(speaker_intelligence, "MODEL_SIZE", 5)

    class Runtime:
        def run(self, outputs, inputs):
            assert outputs == ["embedding"]
            waveform = inputs["waveform"]
            mask = inputs["mask"]
            assert waveform.shape == (3, 160_000)
            assert mask.shape == (3, 589)
            assert np.all(mask[:, :236] == 1.0)
            assert np.all(mask[:, 236:] == 0.0)
            # Eight seconds yields start, middle, and end four-second windows.
            assert waveform[0, 0] < waveform[1, 0] < waveform[2, 0]
            result = np.zeros((3, 256), dtype=np.float32)
            result[0, 0] = 1.0
            result[1, 1] = 1.0
            result[2, 0] = 1.0
            return [result]

    model._session = Runtime()
    samples = np.linspace(-12_000, 12_000, 128_000, dtype=np.int16)

    embedding = await model.extract_pcm16(samples.astype("<i2").tobytes())

    assert len(embedding) == 256
    assert np.linalg.norm(np.asarray(embedding)) == pytest.approx(1.0)
    assert embedding[0] == pytest.approx(2 / np.sqrt(5))
    assert embedding[1] == pytest.approx(1 / np.sqrt(5))
    assert all(value == pytest.approx(0.0) for value in embedding[2:])


@pytest.mark.asyncio
async def test_in_memory_enrollment_excludes_silent_embedding_window(
    monkeypatch, tmp_path
):
    model = WeSpeakerModel(tmp_path / "model")
    model.root.mkdir(parents=True)
    model.path.write_bytes(b"model")
    monkeypatch.setattr(speaker_intelligence, "MODEL_SIZE", 5)

    class Runtime:
        def run(self, _outputs, inputs):
            waveform = inputs["waveform"]
            mask = inputs["mask"]
            assert np.any(waveform[0])
            assert np.any(waveform[1])
            assert not np.any(waveform[2])
            assert np.any(mask[0]) and np.any(mask[1]) and not np.any(mask[2])
            result = np.zeros((3, 256), dtype=np.float32)
            result[0, 0] = 1.0
            result[1, 0] = 1.0
            # This arbitrary silent-window output must not enter the centroid.
            result[2, 1] = 1.0
            return [result]

    model._session = Runtime()
    time_axis = np.arange(64_000, dtype=np.float32) / 16_000
    voiced = (5_000 * np.sin(2 * np.pi * 190 * time_axis)).astype(np.int16)
    samples = np.concatenate([voiced, np.zeros(64_000, dtype=np.int16)])

    embedding = await model.extract_pcm16(samples.astype("<i2").tobytes())

    assert embedding[0] == pytest.approx(1.0)
    assert embedding[1] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_in_memory_enrollment_rejects_invalid_pcm_before_model_inference(tmp_path):
    model = WeSpeakerModel(tmp_path / "model")

    with pytest.raises(ValueError, match="16 kHz"):
        await model.extract_pcm16(b"\0\0" * 32_000, sample_rate=48_000)
    with pytest.raises(ValueError, match="complete PCM16"):
        await model.extract_pcm16(b"\0")
    with pytest.raises(ValueError, match="at least two seconds"):
        await model.extract_pcm16(b"\0\0" * 31_999)
