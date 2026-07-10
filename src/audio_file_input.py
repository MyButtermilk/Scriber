from __future__ import annotations

import asyncio
import math
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame, EndFrame
from pipecat.transports.base_transport import TransportParams

from src.runtime.ffmpeg_commands import pcm_pipe_decode_args
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import hidden_subprocess_kwargs, read_stream_limited

try:
    from pipecat.transports.base_input import BaseInputTransport
except ImportError as exc:  # pragma: no cover - defensive fallback
    raise ImportError(
        "FfmpegAudioFileInput requires pipecat.transports.base_input.BaseInputTransport. "
        "Upgrade pipecat to a version that includes BaseInputTransport."
    ) from exc


class FfmpegAudioFileInput(BaseInputTransport):
    def __init__(
        self,
        file_path: str | Path,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        block_size: int = 1024,
        max_queued_audio_secs: float = 60.0,
    ):
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=int(sample_rate),
            audio_in_channels=int(channels),
            audio_in_passthrough=True,
        )
        super().__init__(params=params)
        self._file_path = str(Path(file_path).expanduser().resolve())
        self._block_size = int(block_size)
        self._max_queued_frames = max(
            1,
            int(
                math.ceil(
                    max(1.0, float(max_queued_audio_secs))
                    * max(1, int(sample_rate))
                    / max(1, self._block_size)
                )
            ),
        )
        self._done = asyncio.Event()
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._feed_task: asyncio.Task | None = None
        self._error: str | None = None

    @property
    def done(self) -> asyncio.Event:
        return self._done

    @property
    def error(self) -> str | None:
        return self._error

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

        if self._feed_task and not self._feed_task.done():
            return

        self._feed_task = asyncio.create_task(self._feed_audio(), name="ffmpeg_audio_file_feed")

    async def stop(self, frame: EndFrame):
        task, self._feed_task = self._feed_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_ffmpeg()
        await super().stop(frame)

    async def _stop_ffmpeg(self) -> None:
        proc, self._ffmpeg = self._ffmpeg, None
        if not proc:
            return
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except Exception:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass

    async def _wait_for_audio_queue_capacity(self) -> None:
        """Bound decoded PCM queued ahead of slower provider/model work."""
        queue = getattr(self, "_audio_in_queue", None)
        qsize = getattr(queue, "qsize", None)
        if not callable(qsize):
            return
        while qsize() >= self._max_queued_frames:
            audio_task = getattr(self, "_audio_task", None)
            if audio_task is not None and audio_task.done():
                await audio_task
                raise RuntimeError("Audio input processing stopped before the file was consumed.")
            await asyncio.sleep(0.01)

    async def _feed_audio(self) -> None:
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            ffmpeg = require_media_tool("ffmpeg")

            bytes_per_frame = max(1, self._block_size) * int(self._params.audio_in_channels) * 2
            cmd = pcm_pipe_decode_args(
                ffmpeg,
                self._file_path,
                sample_rate=int(self._params.audio_in_sample_rate),
                channels=int(self._params.audio_in_channels),
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **hidden_subprocess_kwargs(),
            )
            self._ffmpeg = proc
            assert proc.stdout is not None
            assert proc.stderr is not None
            stderr_task = asyncio.create_task(
                read_stream_limited(proc.stderr),
                name="ffmpeg_audio_file_stderr",
            )

            pcm_sample_width = max(1, int(self._params.audio_in_channels)) * 2
            pending_pcm = bytearray()
            while True:
                read_size = max(1, bytes_per_frame - len(pending_pcm))
                decoded = await proc.stdout.read(read_size)
                if decoded:
                    pending_pcm.extend(decoded)
                if decoded and len(pending_pcm) < bytes_per_frame:
                    # Let the concurrent stderr drainer run even when the pipe
                    # has several immediately available short reads.
                    await asyncio.sleep(0)
                    continue
                if not pending_pcm:
                    break
                if len(pending_pcm) % pcm_sample_width:
                    raise RuntimeError("ffmpeg produced a truncated PCM sample")
                chunk = bytes(pending_pcm)
                pending_pcm.clear()
                frame = InputAudioRawFrame(
                    audio=chunk,
                    sample_rate=int(self._params.audio_in_sample_rate),
                    num_channels=int(self._params.audio_in_channels),
                )
                await self.push_audio_frame(frame)
                await self._wait_for_audio_queue_capacity()
                await asyncio.sleep(0)
                if not decoded:
                    break

            stderr_b = await stderr_task
            rc = await proc.wait()
            if rc != 0:
                err = (stderr_b or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(err or f"ffmpeg exited with code {rc}")

            # Wait for all pushed audio frames to be processed by the base audio task.
            try:
                await asyncio.wait_for(self._audio_in_queue.join(), timeout=60 * 60)
            except Exception:
                # If join fails, still allow the caller to end the pipeline.
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = str(exc)
            logger.error(f"Audio file feed failed: {exc}")
        finally:
            self._done.set()
            try:
                await self._stop_ffmpeg()
            except Exception:
                pass
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)

