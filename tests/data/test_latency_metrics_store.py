from src.data.latency_metrics_store import LatencyMetricsStore


def test_latency_metrics_store_persists_and_reads_latest(tmp_path):
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    first = store.record(
        "session-1",
        {
            "hotkey_received_to_mic_ready_ms": 25.0,
            "hotkey_received_to_first_paste_ms": 110.0,
        },
    )
    second = store.record(
        "session-2",
        {
            "hotkey_received_to_mic_ready_ms": 20.0,
            "hotkey_received_to_first_paste_ms": 95.0,
        },
    )

    latest = store.latest(limit=1)
    assert len(latest) == 1
    assert latest[0].session_id == second.session_id
    assert latest[0].total_ms == 95.0
    assert latest[0].segments["hotkey_received_to_mic_ready_ms"] == 20.0
    assert first.total_ms == 110.0

