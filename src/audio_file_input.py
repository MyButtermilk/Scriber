from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame, EndFrame
from pipecat.transports.base_transport import TransportParams

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
        await self._stop_ffmpeg()
        task, self._feed_task = self._feed_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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

    async def _feed_audio(self) -> None:
        try:
            ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found on PATH.")

            bytes_per_frame = max(1, self._block_size) * int(self._params.audio_in_channels) * 2
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                self._file_path,
                "-vn",
                "-ac",
                str(int(self._params.audio_in_channels)),
                "-ar",
                str(int(self._params.audio_in_sample_rate)),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-",
            ]

            self._ffmpeg = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert self._ffmpeg.stdout is not None
            assert self._ffmpeg.stderr is not None

            while True:
                chunk = await self._ffmpeg.stdout.read(bytes_per_frame)
                if not chunk:
                    break
                frame = InputAudioRawFrame(
                    audio=chunk,
                    sample_rate=int(self._params.audio_in_sample_rate),
                    num_channels=int(self._params.audio_in_channels),
                )
                await self.push_audio_frame(frame)
                await asyncio.sleep(0)

            stderr_b = await self._ffmpeg.stderr.read()
            rc = await self._ffmpeg.wait()
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

