"""One adapter reuses File admission and the complete Transcript summary lifecycle."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from src.api.file_transcription_routes import FileUploadPlan, PublicRecordPort, maybe_compress_audio_upload
from src.api.transcript_routes import SummaryOutcome, TranscriptView
from src.podcasts.feeds import PodcastError
from src.runtime.cancellation import (
    await_with_delayed_cancellation,
    remove_tree_if_exists,
    to_thread_cancellation_barrier,
)


class PodcastControllerPort(Protocol):
    @property
    def file_upload_root(self) -> Path: ...

    def plan_file_upload(self, *, source_is_video: bool) -> FileUploadPlan: ...

    async def start_file_transcription(
        self, file_path: Path, original_filename: str, *, plan: FileUploadPlan, transcript_id: str | None = None
    ) -> PublicRecordPort: ...

    async def transcript_view(self, transcript_id: str) -> TranscriptView | None: ...

    async def summarize_transcript(self, transcript_id: str) -> SummaryOutcome: ...


class PodcastProcessor:
    def __init__(self, controller: PodcastControllerPort, *, poll_seconds: float = 3) -> None:
        self._controller = controller
        self._poll_seconds = poll_seconds

    def plan(self) -> FileUploadPlan:
        try:
            return self._controller.plan_file_upload(source_is_video=False)
        except Exception as exc:
            raise PodcastError(
                "Configure a transcription provider in Settings before processing podcast episodes."
            ) from exc

    async def view(self, transcript_id: str) -> TranscriptView | None:
        return await self._controller.transcript_view(transcript_id)

    async def process(
        self,
        source: Path,
        title: str,
        transcript_id: str,
        plan: FileUploadPlan | None,
        stage: Callable[[str], Awaitable[None]],
    ) -> None:
        view = await self.view(transcript_id)
        if view is None:
            if plan is None:
                plan = self.plan()
            workspace = self._controller.file_upload_root / f"podcast-{transcript_id}"
            target = workspace / f"episode{source.suffix}"
            adopted = False
            try:
                await to_thread_cancellation_barrier(workspace.mkdir, parents=True, exist_ok=True)
                await to_thread_cancellation_barrier(shutil.copyfile, source, target)
                target = await maybe_compress_audio_upload(target, max_bytes=plan.final_audio_max_bytes)
                if target.stat().st_size > plan.final_audio_max_bytes:
                    raise PodcastError("This episode exceeds the selected provider's audio size limit.")
                await stage("admitting")
                # The controller owns the copy at entry, including failed or canceled
                # enqueue. The podcast keeps its original downloaded audio.
                adopted = True
                _, pending_cancel = await await_with_delayed_cancellation(
                    self._controller.start_file_transcription(target, title, plan=plan, transcript_id=transcript_id)
                )
                if pending_cancel is not None:
                    raise pending_cancel
            finally:
                if not adopted:
                    await remove_tree_if_exists(workspace)
        await stage("transcribing")
        async with asyncio.timeout(6 * 60 * 60):
            while True:
                view = await self.view(transcript_id)
                if view is None:
                    raise PodcastError("The linked transcript is unavailable. Retry this episode to create it again.")
                if view.status == "completed":
                    break
                if view.status in {"failed", "canceled", "cancelled"}:
                    raise PodcastError(
                        "Podcast transcription failed. Open the transcript for details or retry the episode."
                    )
                await asyncio.sleep(self._poll_seconds)
            if view.summary.strip():
                return
            await stage("summarizing")
            while True:
                outcome = await self._controller.summarize_transcript(transcript_id)
                if outcome.kind == "completed":
                    return
                if outcome.kind != "already_running":
                    raise PodcastError(
                        "The transcript is ready, but its summary failed. Check the summary model and retry."
                    )
                await asyncio.sleep(self._poll_seconds)
                view = await self.view(transcript_id)
                if view is not None and view.summary.strip():
                    return
