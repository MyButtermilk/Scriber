"""Optional local WeSpeaker ONNX embeddings without Torch/Pyannote/SciPy."""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import wave
from pathlib import Path
from typing import Any

from aiohttp import ClientSession

from src.runtime.paths import data_dir


MODEL_REVISION = "abea38bae76873d0842509a54f8fbe6c8b5b5fe6"
MODEL_URL = (
    "https://huggingface.co/talatapp/wespeaker-voxceleb-resnet34-LM-onnx/resolve/"
    f"{MODEL_REVISION}/wespeaker.onnx"
)
MODEL_SHA256 = "131700f06e0f4efa9283d66f504d84aaea8c279e81cba8c7784c14d180333c61"
MODEL_SIZE = 26_632_299


class WeSpeakerModel:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or data_dir() / "models" / "wespeaker-resnet34-lm"
        self.path = self.root / "wespeaker.onnx"
        self.verification_path = self.root / "wespeaker.onnx.sha256"
        self._session: Any = None
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        installed = (
            self.path.is_file()
            and self.path.stat().st_size == MODEL_SIZE
            and self.verification_path.is_file()
            and self.verification_path.read_text(encoding="ascii").strip() == MODEL_SHA256
        )
        return {
            "installed": installed,
            "model": "wespeaker-voxceleb-resnet34-LM",
            "revision": MODEL_REVISION,
            "byteSize": self.path.stat().st_size if self.path.is_file() else 0,
            "expectedByteSize": MODEL_SIZE,
            "sha256": MODEL_SHA256 if installed else "",
            "license": "Apache-2.0 model; upstream VoxCeleb dataset terms also apply",
        }

    async def download(self, session: ClientSession) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / ".wespeaker.onnx.partial"
        verification_temporary = self.root / ".wespeaker.onnx.sha256.partial"
        digest = hashlib.sha256()
        written = 0
        try:
            async with session.get(MODEL_URL, allow_redirects=True) as response:
                if response.status != 200:
                    raise ValueError(f"WeSpeaker download failed (HTTP {response.status}).")
                with temporary.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        written += len(chunk)
                        if written > MODEL_SIZE:
                            raise ValueError("WeSpeaker download exceeded its manifest size.")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if written != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
                raise ValueError("WeSpeaker model failed manifest checksum verification.")
            temporary.replace(self.path)
            verification_temporary.write_text(MODEL_SHA256 + "\n", encoding="ascii")
            verification_temporary.replace(self.verification_path)
            self._session = None
            return self.status()
        finally:
            temporary.unlink(missing_ok=True)
            verification_temporary.unlink(missing_ok=True)

    def delete(self) -> None:
        with self._lock:
            self._session = None
            self.path.unlink(missing_ok=True)
            self.verification_path.unlink(missing_ok=True)

    def _runtime(self):
        with self._lock:
            if self._session is None:
                if not self.status()["installed"]:
                    raise ValueError("The optional WeSpeaker model is not installed.")
                digest = hashlib.sha256()
                with self.path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != MODEL_SHA256:
                    self.verification_path.unlink(missing_ok=True)
                    raise ValueError("The optional WeSpeaker model checksum is invalid.")
                import onnxruntime as ort

                self._session = ort.InferenceSession(
                    str(self.path), providers=["CPUExecutionProvider"]
                )
            return self._session

    async def extract(self, path: Path, start_ms: int, end_ms: int) -> list[float]:
        return await asyncio.to_thread(self._extract_sync, path, start_ms, end_ms)

    def _extract_sync(self, path: Path, start_ms: int, end_ms: int) -> list[float]:
        import numpy as np

        with wave.open(str(path), "rb") as source:
            if source.getframerate() != 16_000 or source.getsampwidth() != 2:
                raise ValueError("WeSpeaker input must be 16 kHz PCM16 WAV.")
            start_frame = max(0, round(start_ms * 16_000 / 1000))
            requested_frames = min(160_000, max(32_000, round((end_ms - start_ms) * 16_000 / 1000)))
            source.setpos(min(start_frame, source.getnframes()))
            raw = source.readframes(requested_frames)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        waveform = np.zeros((3, 160_000), dtype=np.float32)
        copy_count = min(samples.size, 160_000)
        waveform[0, :copy_count] = samples[:copy_count]
        mask = np.zeros((3, 589), dtype=np.float32)
        active_mask = max(1, min(589, round(copy_count / 160_000 * 589)))
        mask[0, :active_mask] = 1.0
        output = self._runtime().run(["embedding"], {"waveform": waveform, "mask": mask})[0][0]
        norm = float(np.linalg.norm(output))
        if not norm or not np.isfinite(norm):
            raise ValueError("WeSpeaker returned an invalid embedding.")
        return (output / norm).astype(np.float32).tolist()
