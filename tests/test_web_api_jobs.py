import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src import web_api
from src.config import Config
from src.data.job_store import JobStatus, JobStore
from src.web_api import ScriberWebController, TranscriptRecord


@pytest.mark.asyncio
async def test_start_youtube_transcription_persists_job_lifecycle(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)

    async def _fake_run(rec, *, provider):
        rec.status = "completed"
        rec.step = "Completed"

    with (
        patch.object(ctl, "_run_youtube_transcription", new=AsyncMock(side_effect=_fake_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_youtube_transcription({"url": "https://youtube.com/watch?v=test123"})
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    job_id = ctl._job_ids_by_transcript[rec.id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.job_type.value == "youtube"


@pytest.mark.asyncio
async def test_cancel_transcript_marks_background_job_canceled(tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    ctl = ScriberWebController(loop, job_store=store)
    sample_file = tmp_path / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    async def _slow_run(_rec, _path, *, provider):
        await asyncio.sleep(10)

    with (
        patch.object(ctl, "_run_file_transcription", new=AsyncMock(side_effect=_slow_run)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        await asyncio.sleep(0)
        assert await ctl.cancel_transcript(rec.id) is True
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    job_id = ctl._job_ids_by_transcript[rec.id]
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CANCELED
    assert job.job_type.value == "file"


class _SyntheticPipeline:
    def __init__(self, *, on_transcription):
        self._on_transcription = on_transcription

    async def transcribe_file_direct(self, _path):
        self._on_transcription("Synthetic transcript text for summary failure.", True)


class _EmptyPipeline:
    async def transcribe_file_direct(self, _path):
        return None

    async def transcribe_file(self, _path):
        return None


def _completed_record(*, transcript_type: str, tmp_path) -> TranscriptRecord:
    now = datetime.now()
    return TranscriptRecord(
        id="summary-failure-record",
        title="Summary Failure",
        date="Today",
        duration="00:10",
        status="processing",
        type=transcript_type,
        language="auto",
        step="Queued",
        source_url="https://youtube.com/watch?v=summaryfailure" if transcript_type == "youtube" else "",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )


@pytest.mark.asyncio
async def test_youtube_auto_summary_failure_is_exposed_as_summary_state(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)

    async def _download_youtube_audio(*_args, **_kwargs):
        return audio_path

    async def _fail_summary(*_args, **_kwargs):
        raise RuntimeError("summary provider failed")

    def _create_pipeline(*_args, **kwargs):
        return _SyntheticPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "synthetic-summary-model")

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch("src.summarization.summarize_text", new=AsyncMock(side_effect=_fail_summary)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    assert rec.status == "completed"
    assert rec.summary == ""
    assert rec.summary_status == "failed"
    assert "summary provider failed" in rec.summary_error
    assert rec.to_public(include_content=True)["summaryStatus"] == "failed"
    assert save_mock.await_count >= 3


@pytest.mark.asyncio
async def test_file_transcription_empty_provider_result_fails_job(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        await ctl._run_file_transcription(rec, file_path, provider="gladia")

    assert rec.status == "failed"
    assert rec.step == "Failed"
    assert "provider returned no transcript text" in rec.content
    assert save_mock.await_count >= 1
    assert broadcast_mock.await_count >= 1


@pytest.mark.asyncio
async def test_youtube_transcription_empty_provider_result_fails_job(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="youtube", tmp_path=tmp_path)

    async def _download_youtube_audio(*_args, **_kwargs):
        return audio_path

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", return_value=_EmptyPipeline()),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()) as broadcast_mock,
    ):
        await ctl._run_youtube_transcription(rec, provider="gladia")

    assert rec.status == "failed"
    assert rec.step == "Failed"
    assert "provider returned no transcript text" in rec.content
    assert save_mock.await_count >= 1
    assert broadcast_mock.await_count >= 1


@pytest.mark.asyncio
async def test_update_settings_rejects_unavailable_local_stt_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBER_DISABLE_DEVICE_MONITOR", "1")
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "soniox")
    monkeypatch.setattr(
        web_api,
        "_provider_readiness_error",
        lambda provider: "Local ONNX transcription is unavailable in this Scriber build."
        if provider == "onnx_local"
        else None,
    )
    ctl = ScriberWebController(asyncio.get_running_loop())

    with pytest.raises(RuntimeError, match="Local ONNX transcription is unavailable"):
        await ctl.update_settings({"defaultSttService": "onnx_local"})

    assert Config.DEFAULT_STT_SERVICE == "soniox"
    ctl.shutdown()


@pytest.mark.asyncio
async def test_late_youtube_download_progress_cannot_overwrite_transcription_step(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    ctl._downloads_dir = tmp_path / "downloads"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    now = datetime.now()
    rec = TranscriptRecord(
        id="late-download-progress",
        title="Late Download Progress",
        date="Today",
        duration="00:10",
        status="processing",
        type="youtube",
        language="auto",
        step="Queued",
        source_url="https://youtube.com/watch?v=lateprogress",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    late_download_progress = {}

    async def _download_youtube_audio(*_args, **kwargs):
        callback = kwargs["on_progress"]
        late_download_progress["callback"] = callback
        callback(SimpleNamespace(status="finished", speed=None, eta=None, percent=100.0))
        return audio_path

    class _LateProgressPipeline:
        def __init__(self, *, on_transcription):
            self._on_transcription = on_transcription

        async def transcribe_file_direct(self, _path):
            late_download_progress["callback"](
                SimpleNamespace(status="finished", speed=None, eta=None, percent=100.0)
            )
            assert rec.step == "Transcribing..."
            self._on_transcription("Synthetic transcript after late progress.", True)

    def _create_pipeline(*_args, **kwargs):
        return _LateProgressPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_download_youtube_audio)),
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_youtube_transcription(rec, provider="soniox")

    assert rec.status == "completed"
    assert rec.step == "Completed"
    assert "Synthetic transcript after late progress." in rec.content


@pytest.mark.asyncio
async def test_file_auto_summary_failure_is_exposed_as_summary_state(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    ctl = ScriberWebController(loop)
    file_path = tmp_path / "upload.wav"
    file_path.write_bytes(b"RIFF....WAVEfmt ")
    rec = _completed_record(transcript_type="file", tmp_path=tmp_path)

    async def _fail_summary(*_args, **_kwargs):
        raise RuntimeError("summary provider failed")

    def _create_pipeline(*_args, **kwargs):
        return _SyntheticPipeline(on_transcription=kwargs["on_transcription"])

    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", True)
    monkeypatch.setattr(Config, "SUMMARIZATION_MODEL", "synthetic-summary-model")

    with (
        patch("src.web_api.supports_direct_file_upload", return_value=True),
        patch("src.web_api._create_scriber_pipeline", side_effect=_create_pipeline),
        patch("src.summarization.summarize_text", new=AsyncMock(side_effect=_fail_summary)),
        patch.object(ctl, "_save_transcript_to_db_async", new=AsyncMock()) as save_mock,
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        await ctl._run_file_transcription(rec, file_path, provider="soniox")

    assert rec.status == "completed"
    assert rec.summary == ""
    assert rec.summary_status == "failed"
    assert "summary provider failed" in rec.summary_error
    assert rec.to_public(include_content=True)["summaryStatus"] == "failed"
    assert save_mock.await_count >= 3
