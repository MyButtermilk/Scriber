import asyncio
from unittest.mock import AsyncMock

import pytest
from src.data.latency_metrics_store import LatencyMetricsStore
from src.web_api import ScriberWebController


@pytest.mark.asyncio
async def test_hot_path_report_persisted_only_once(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-hot-path"

    ctl._start_hot_path_tracer(session_id)
    ctl._mark_hot_path(session_id, "first_paste")
    ctl._emit_hot_path_report_once(session_id)
    ctl._emit_hot_path_report_once(session_id)

    rows = metrics_store.latest(limit=10)
    assert len(rows) == 1
    assert rows[0].session_id == session_id
    assert rows[0].total_ms >= 0.0


@pytest.mark.asyncio
async def test_hot_path_audio_frame_marker_is_persisted(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-audio-frame"

    ctl._session_id = session_id
    ctl._start_hot_path_tracer(session_id)
    ctl._on_audio_level(0.5, session_id=session_id)
    ctl._mark_hot_path(session_id, "first_paste")
    ctl._emit_hot_path_report_once(session_id)

    rows = metrics_store.latest(limit=10)
    assert len(rows) == 1
    assert "hotkey_received_to_first_audio_frame_ms" in rows[0].segments
    assert "first_audio_frame_to_first_paste_ms" in rows[0].segments
    assert "hotkey_received_to_first_audible_audio_frame_ms" in rows[0].segments


@pytest.mark.asyncio
async def test_hot_path_silent_audio_frame_does_not_mark_audible_audio(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-silent-audio-frame"

    ctl._session_id = session_id
    ctl._start_hot_path_tracer(session_id)
    ctl._on_audio_level(0.0, session_id=session_id)
    ctl._mark_hot_path(session_id, "first_paste")
    ctl._emit_hot_path_report_once(session_id)

    rows = metrics_store.latest(limit=10)
    assert len(rows) == 1
    assert "hotkey_received_to_first_audio_frame_ms" in rows[0].segments
    assert "hotkey_received_to_first_audible_audio_frame_ms" not in rows[0].segments


@pytest.mark.asyncio
async def test_hot_path_partial_report_can_be_persisted_without_text_injection(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-partial-hot-path"

    ctl._start_hot_path_tracer(session_id)
    ctl._mark_hot_path(session_id, "mic_ready")
    ctl._mark_hot_path(session_id, "first_audio_frame")
    ctl._mark_hot_path(session_id, "stop_requested")
    ctl._mark_hot_path(session_id, "session_finished")

    assert ctl._emit_hot_path_report_once(session_id, required_marker=None) is True

    rows = metrics_store.latest(limit=10)
    assert len(rows) == 1
    assert "hotkey_received_to_mic_ready_ms" in rows[0].segments
    assert "hotkey_received_to_first_audio_frame_ms" in rows[0].segments
    assert "stop_requested_to_session_finished_ms" in rows[0].segments
    assert "stop_requested_to_first_paste_ms" not in rows[0].segments


@pytest.mark.asyncio
async def test_hot_path_marks_non_empty_final_transcript_only(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    ctl.broadcast = AsyncMock()
    session_id = "session-final-token"

    ctl._session_id = session_id
    ctl._start_hot_path_tracer(session_id)
    ctl._on_transcription("", True, session_id=session_id)
    ctl._mark_hot_path(session_id, "session_finished")
    assert ctl._emit_hot_path_report_once(session_id, required_marker=None) is True

    rows = metrics_store.latest(limit=10)
    assert "hotkey_received_to_first_final_token_ms" not in rows[0].segments

    second_id = "session-final-token-text"
    ctl._session_id = second_id
    ctl._start_hot_path_tracer(second_id)
    ctl._on_transcription("hello", True, session_id=second_id)
    ctl._mark_hot_path(second_id, "session_finished")
    assert ctl._emit_hot_path_report_once(second_id, required_marker=None) is True

    rows = metrics_store.latest(limit=10)
    latest = next(row for row in rows if row.session_id == second_id)
    assert "hotkey_received_to_first_final_token_ms" in latest.segments
    assert "hotkey_received_to_provider_final_received_ms" in latest.segments


@pytest.mark.asyncio
async def test_hot_path_persists_stop_to_injection_breakdown(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-stop-breakdown"

    ctl._start_hot_path_tracer(session_id)
    ctl._mark_hot_path(session_id, "stop_requested")
    ctl._mark_hot_path(session_id, "last_chunk_sent")
    ctl._mark_hot_path(session_id, "provider_final_received")
    ctl._mark_hot_path(session_id, "clipboard_set")
    ctl._mark_hot_path(session_id, "paste")
    ctl._mark_hot_path(session_id, "first_paste")

    assert ctl._emit_hot_path_report_once(session_id) is True

    rows = metrics_store.latest(limit=10)
    latest = next(row for row in rows if row.session_id == session_id)
    assert "stop_requested_to_last_chunk_sent_ms" in latest.segments
    assert "last_chunk_sent_to_provider_final_received_ms" in latest.segments
    assert "provider_final_received_to_clipboard_set_ms" in latest.segments
    assert "clipboard_set_to_paste_ms" in latest.segments
    assert "paste_to_first_paste_ms" in latest.segments


@pytest.mark.asyncio
async def test_hot_path_metrics_summary_exposed(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)

    metrics_store.record("a", {"hotkey_received_to_first_paste_ms": 100.0})
    metrics_store.record("b", {"hotkey_received_to_first_paste_ms": 220.0})

    out = ctl.get_hot_path_metrics(limit=10)
    assert out["summary"]["count"] == 2
    assert len(out["items"]) == 2
    assert out["items"][0]["sessionId"] in {"a", "b"}


@pytest.mark.asyncio
async def test_hot_path_metrics_can_include_active_readiness_snapshot(tmp_path):
    loop = asyncio.get_running_loop()
    metrics_store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    ctl = ScriberWebController(loop, latency_metrics_store=metrics_store)
    session_id = "session-active-readiness"

    ctl._start_hot_path_tracer(session_id)
    ctl._mark_hot_path(session_id, "mic_ready")
    ctl._mark_hot_path(session_id, "first_audio_frame")

    out = ctl.get_hot_path_metrics(limit=10, include_active=True)

    assert out["items"] == []
    assert out["includeActive"] is True
    active = next(item for item in out["activeItems"] if item["sessionId"] == session_id)
    assert active["active"] is True
    assert active["reportEmitted"] is False
    assert active["markerNames"] == ["hotkey_received", "mic_ready", "first_audio_frame"]
    assert "hotkey_received_to_mic_ready_ms" in active["segments"]
    assert "hotkey_received_to_first_audio_frame_ms" in active["segments"]
