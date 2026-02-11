import asyncio
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
