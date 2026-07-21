"""Bounded capture-time FFmpeg encoder used by buffered STT controls.

The authoritative PCM spool remains owned by the caller.  This helper only
maintains a best-effort encoded candidate: enqueueing never waits, overload or
local encoder failure invalidates the candidate, and the caller can still
encode the authoritative PCM after Stop before making a provider request.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections.abc import Sequence
from typing import BinaryIO

from src.runtime.subprocess_utils import hidden_subprocess_kwargs, read_stream_limited


class CaptureTimeEncoderError(RuntimeError):
    """The local capture-time candidate could not be completed safely."""


class CaptureTimeFfmpegEncoder:
    """Feed one FFmpeg encoder through a bounded, non-blocking PCM queue."""

    _SENTINEL = None

    def __init__(
        self,
        command: Sequence[str],
        *,
        sample_rate: int,
        channels: int,
        queue_capacity: int = 128,
        queued_pcm_limit: int = 4 * 1024 * 1024,
        output_memory_limit: int = 10 * 1024 * 1024,
        stderr_limit: int = 1024 * 1024,
        finish_timeout_seconds: float = 30.0,
    ) -> None:
        if not command:
            raise ValueError("capture-time encoder command must not be empty")
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("capture-time encoder PCM format must be positive")
        if queue_capacity <= 0 or queued_pcm_limit <= 0:
            raise ValueError("capture-time encoder queue bounds must be positive")

        self._command = tuple(str(part) for part in command)
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=int(queue_capacity)
        )
        self._queued_pcm_limit = int(queued_pcm_limit)
        self._queued_pcm_bytes = 0
        self._stderr_limit = max(0, int(stderr_limit))
        self._finish_timeout_seconds = min(
            120.0,
            max(1.0, float(finish_timeout_seconds)),
        )
        self._output: BinaryIO | None = tempfile.SpooledTemporaryFile(
            max_size=max(1, int(output_memory_limit)),
            mode="w+b",
        )
        self._process: asyncio.subprocess.Process | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._finished = False
        self._error_code: str | None = None

    @property
    def pcm_format(self) -> tuple[int, int]:
        return self._sample_rate, self._channels

    @property
    def valid(self) -> bool:
        return self._accepting and self._error_code is None

    @property
    def error_code(self) -> str | None:
        return self._error_code

    def offer(self, pcm: bytes, *, sample_rate: int, channels: int) -> bool:
        """Offer PCM without waiting; ``False`` leaves the caller's PCM intact."""

        if not self._accepting or self._finished or self._error_code is not None:
            return False
        if (int(sample_rate), int(channels)) != self.pcm_format:
            self._invalidate("pcmFormatChanged")
            return False
        if not pcm:
            return True
        if len(pcm) % (2 * self._channels) != 0:
            self._invalidate("pcmFrameMisaligned")
            return False
        if (
            self._queue.full()
            or self._queued_pcm_bytes + len(pcm) > self._queued_pcm_limit
        ):
            self._invalidate("boundedQueueOverflow")
            return False

        if self._runner_task is None:
            self._runner_task = asyncio.create_task(
                self._run(),
                name="capture-time-ffmpeg-encoder",
            )
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            self._invalidate("boundedQueueOverflow")
            return False
        self._queued_pcm_bytes += len(pcm)
        return True

    async def finish(self) -> BinaryIO:
        """Flush the small encoder tail and transfer the encoded spool."""

        self._accepting = False
        if self._error_code is not None:
            error_code = self._error_code
            await self.abort()
            raise CaptureTimeEncoderError(error_code)
        if self._finished:
            raise CaptureTimeEncoderError("capture-time encoder already finalized")
        if self._runner_task is None:
            await self.abort()
            raise CaptureTimeEncoderError("capture-time encoder received no PCM")

        async def finish_runner() -> None:
            await self._queue.put(self._SENTINEL)
            await asyncio.shield(self._runner_task)

        try:
            await asyncio.wait_for(
                finish_runner(),
                timeout=self._finish_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._error_code = "captureTimeEncoderFinishTimeout"
            await self.abort()
            raise CaptureTimeEncoderError(self._error_code) from None
        except asyncio.CancelledError:
            await self.abort()
            raise

        if self._error_code is not None:
            error_code = self._error_code
            await self.abort()
            raise CaptureTimeEncoderError(error_code)
        output = self._output
        if output is None:
            await self.abort()
            raise CaptureTimeEncoderError("capture-time encoder output unavailable")
        output.seek(0, 2)
        if output.tell() <= 0:
            await self.abort()
            raise CaptureTimeEncoderError("capture-time encoder output empty")
        output.seek(0)
        self._output = None
        self._finished = True
        return output

    async def abort(self) -> None:
        """Cancel, join, and close every candidate-owned resource."""

        self._accepting = False
        task = self._runner_task
        if task is not None and not task.done():
            task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        elif process is not None:
            with contextlib.suppress(Exception):
                await process.wait()
        self._close_output()
        self._finished = True

    def close_nowait(self) -> None:
        """Best-effort emergency cleanup for disposal paths without an await."""

        self._accepting = False
        task = self._runner_task
        if task is not None and not task.done():
            task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        self._close_output()
        self._finished = True

    def _invalidate(self, code: str) -> None:
        if self._error_code is None:
            self._error_code = code
        self.close_nowait()

    def _close_output(self) -> None:
        output = self._output
        self._output = None
        if output is not None:
            with contextlib.suppress(Exception):
                output.close()

    async def _run(self) -> None:
        stderr_task: asyncio.Task[bytes] | None = None
        capture_task: asyncio.Task[None] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **hidden_subprocess_kwargs(),
            )
            self._process = process
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise CaptureTimeEncoderError("capture-time encoder pipes unavailable")

            async def capture_output() -> None:
                assert process.stdout is not None
                while chunk := await process.stdout.read(64 * 1024):
                    output = self._output
                    if output is None:
                        raise CaptureTimeEncoderError(
                            "capture-time encoder output closed"
                        )
                    output.write(chunk)

            capture_task = asyncio.create_task(
                capture_output(),
                name="capture-time-ffmpeg-output",
            )
            stderr_task = asyncio.create_task(
                read_stream_limited(process.stderr, max_bytes=self._stderr_limit),
                name="capture-time-ffmpeg-stderr",
            )

            while True:
                chunk = await self._queue.get()
                if chunk is self._SENTINEL:
                    break
                self._queued_pcm_bytes = max(
                    0,
                    self._queued_pcm_bytes - len(chunk),
                )
                process.stdin.write(chunk)
                await process.stdin.drain()

            process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
            await capture_task
            _stderr = await stderr_task
            return_code = await process.wait()
            if return_code != 0:
                raise CaptureTimeEncoderError("capture-time encoder exited nonzero")
        except asyncio.CancelledError:
            await self._terminate_process()
        except Exception as exc:
            if self._error_code is None:
                self._error_code = type(exc).__name__
            await self._terminate_process()
        finally:
            for task in (capture_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            pending = [
                task
                for task in (capture_task, stderr_task)
                if task is not None
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
