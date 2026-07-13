"""Optional local WeSpeaker ONNX embeddings without Torch/Pyannote/SciPy."""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession

from src.runtime.paths import data_dir


MODEL_REVISION = "abea38bae76873d0842509a54f8fbe6c8b5b5fe6"
MODEL_URL = (
    "https://huggingface.co/talatapp/wespeaker-voxceleb-resnet34-LM-onnx/resolve/"
    f"{MODEL_REVISION}/wespeaker.onnx"
)
MODEL_SHA256 = "131700f06e0f4efa9283d66f504d84aaea8c279e81cba8c7784c14d180333c61"
MODEL_SIZE = 26_632_299
_ENROLLMENT_WINDOW_RMS_FLOOR = 0.006
_ENROLLMENT_WINDOW_PEAK_FLOOR = 0.018


@dataclass(frozen=True)
class StagedWeSpeakerDownload:
    model_path: Path


class WeSpeakerModel:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or data_dir() / "models" / "wespeaker-resnet34-lm"
        self.path = self.root / "wespeaker.onnx"
        self.verification_path = self.root / "wespeaker.onnx.sha256"
        self._session: Any = None
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            try:
                byte_size = self.path.stat().st_size if self.path.is_file() else 0
                installed = (
                    byte_size == MODEL_SIZE
                    and self.verification_path.is_file()
                    and self.verification_path.read_text(encoding="ascii").strip()
                    == MODEL_SHA256
                )
            except OSError:
                # A concurrent delete or cross-process opt-out is an ordinary
                # unavailable state, not a request-level server error.
                installed = False
                byte_size = 0
        return {
            "installed": installed,
            "model": "wespeaker-voxceleb-resnet34-LM",
            "revision": MODEL_REVISION,
            "byteSize": byte_size,
            "expectedByteSize": MODEL_SIZE,
            "sha256": MODEL_SHA256 if installed else "",
            "license": "Apache-2.0 model; upstream VoxCeleb dataset terms also apply",
        }

    async def stage_download(self, session: ClientSession) -> StagedWeSpeakerDownload:
        """Download and verify into a unique non-installed staging file."""

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / (
            f".wespeaker.{os.getpid()}.{uuid4().hex}.onnx.partial"
        )
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
            return StagedWeSpeakerDownload(model_path=temporary)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def promote_staged(self, staged: StagedWeSpeakerDownload) -> dict[str, Any]:
        """Atomically promote a verified staging file under the model lock."""

        verification_temporary = self.root / (
            f".wespeaker.{os.getpid()}.{uuid4().hex}.sha256.partial"
        )
        with self._lock:
            try:
                if (
                    not staged.model_path.is_file()
                    or staged.model_path.stat().st_size != MODEL_SIZE
                ):
                    raise ValueError("WeSpeaker staged model is unavailable.")
                staged.model_path.replace(self.path)
                verification_temporary.write_text(
                    MODEL_SHA256 + "\n", encoding="ascii"
                )
                verification_temporary.replace(self.verification_path)
                self._session = None
                return self.status()
            finally:
                staged.model_path.unlink(missing_ok=True)
                verification_temporary.unlink(missing_ok=True)

    @staticmethod
    def discard_staged(staged: StagedWeSpeakerDownload) -> None:
        staged.model_path.unlink(missing_ok=True)

    async def download(self, session: ClientSession) -> dict[str, Any]:
        """Compatibility helper for callers that do not need a promotion guard."""

        staged = await self.stage_download(session)
        try:
            return self.promote_staged(staged)
        finally:
            self.discard_staged(staged)

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

    async def extract_pcm16(self, pcm: bytes, *, sample_rate: int = 16_000) -> list[float]:
        """Extract a robust enrollment centroid from bounded mono PCM in memory."""
        if int(sample_rate) != 16_000:
            raise ValueError("WeSpeaker input must be 16 kHz PCM16 audio.")
        if not pcm or len(pcm) % 2:
            raise ValueError("WeSpeaker input must contain complete PCM16 samples.")
        return await asyncio.to_thread(self._extract_pcm16_sync, bytes(pcm))

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
        return self._extract_windows_sync([samples])

    def _extract_pcm16_sync(self, pcm: bytes) -> list[float]:
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < 32_000:
            raise ValueError("WeSpeaker needs at least two seconds of voice audio.")
        window_size = min(64_000, int(samples.size))
        final_start = max(0, int(samples.size) - window_size)
        middle_start = max(0, final_start // 2)
        starts = (0, middle_start, final_start)
        windows = []
        for start in starts:
            window = samples[start : start + window_size]
            if self._voice_window_is_usable(window):
                windows.append(window)
        if len(windows) < 2:
            raise ValueError(
                "WeSpeaker needs clear speech across at least two parts of the sample."
            )
        return self._extract_windows_sync(windows)

    @staticmethod
    def _voice_window_is_usable(samples: Any) -> bool:
        """Reject silent/noisy inference windows after the capture-level gate."""

        import numpy as np

        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size <= 0:
            return False
        centered = values - float(np.mean(values))
        rms = float(np.sqrt(np.mean(np.square(centered))))
        peak = float(np.max(np.abs(centered)))
        return bool(
            np.isfinite(rms)
            and np.isfinite(peak)
            and rms >= _ENROLLMENT_WINDOW_RMS_FLOOR
            and peak >= _ENROLLMENT_WINDOW_PEAK_FLOOR
        )

    def _extract_windows_sync(self, windows: list[Any]) -> list[float]:
        import numpy as np

        if not windows or len(windows) > 3:
            raise ValueError("WeSpeaker accepts between one and three voice windows.")
        waveform = np.zeros((3, 160_000), dtype=np.float32)
        mask = np.zeros((3, 589), dtype=np.float32)
        for index, window in enumerate(windows):
            samples = np.asarray(window, dtype=np.float32).reshape(-1)
            copy_count = min(int(samples.size), 160_000)
            if copy_count <= 0:
                raise ValueError("WeSpeaker received an empty voice window.")
            waveform[index, :copy_count] = samples[:copy_count]
            active_mask = max(1, min(589, round(copy_count / 160_000 * 589)))
            mask[index, :active_mask] = 1.0
        raw_output = self._runtime().run(
            ["embedding"], {"waveform": waveform, "mask": mask}
        )[0]
        output = np.asarray(raw_output, dtype=np.float32)
        if output.ndim == 1:
            output = output.reshape(1, -1)
        if output.ndim != 2 or output.shape[0] < len(windows) or output.shape[1] != 256:
            raise ValueError("WeSpeaker returned an invalid embedding.")
        normalized: list[Any] = []
        for vector in output[: len(windows)]:
            norm = float(np.linalg.norm(vector))
            if not norm or not np.isfinite(norm) or not np.all(np.isfinite(vector)):
                raise ValueError("WeSpeaker returned an invalid embedding.")
            normalized.append(vector / norm)
        centroid = np.mean(np.stack(normalized), axis=0)
        centroid_norm = float(np.linalg.norm(centroid))
        if not centroid_norm or not np.isfinite(centroid_norm):
            raise ValueError("WeSpeaker returned an invalid enrollment centroid.")
        return (centroid / centroid_norm).astype(np.float32).tolist()
