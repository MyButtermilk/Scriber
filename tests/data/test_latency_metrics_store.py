import gc
import sqlite3
import weakref

import pytest

from src.data.latency_metrics_store import LatencyMetricsStore


def test_latency_metrics_store_default_uses_runtime_database_path(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SCRIBER_DATABASE_PATH", raising=False)

    store = LatencyMetricsStore()

    assert store._db_path == data_dir / "transcripts.db"
    assert (data_dir / "transcripts.db").is_file()


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


def test_latency_metrics_store_reuses_thread_local_connection(tmp_path):
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")

    first = store._connect()
    second = store._connect()

    assert first is second


def test_latency_metrics_store_finalizer_closes_cached_connections(tmp_path):
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    conn = store._connect()
    store_ref = weakref.ref(store)

    del store
    gc.collect()

    assert store_ref() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_latency_metrics_store_summary_percentiles(tmp_path):
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    for idx, value in enumerate([100.0, 120.0, 150.0, 200.0, 300.0], start=1):
        store.record(
            f"session-{idx}",
            {
                "hotkey_received_to_first_paste_ms": value,
            },
        )

    summary = store.summarize(limit=10)
    assert summary["count"] == 5
    assert summary["minMs"] == 100.0
    assert summary["p50Ms"] == 150.0
    assert summary["p95Ms"] == 300.0
    assert summary["maxMs"] == 300.0


def test_latency_metrics_store_prunes_to_configured_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRIBER_HOT_PATH_METRICS_RETENTION", "3")
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    for index in range(5):
        store.record(
            f"session-{index}",
            {"hotkey_received_to_first_paste_ms": float(index)},
        )

    assert [item.session_id for item in store.latest(limit=10)] == [
        "session-4",
        "session-3",
        "session-2",
    ]
    count = store._connect().execute("SELECT COUNT(*) FROM hot_path_metrics").fetchone()[0]
    assert count == 3


def test_latency_metrics_store_ignores_non_finite_or_corrupt_values(tmp_path):
    store = LatencyMetricsStore(db_path=tmp_path / "metrics.db")
    metric = store.record(
        "session-safe",
        {
            "valid": 12.5,
            "nan": float("nan"),
            "invalid": "not-a-number",  # type: ignore[dict-item]
        },
    )
    assert metric.segments == {"valid": 12.5}

    conn = store._connect()
    conn.execute(
        "UPDATE hot_path_metrics SET total_ms = ?, segments_json = ?",
        ("broken", '{"valid": 1, "bad": "nope", "infinite": Infinity}'),
    )
    conn.commit()

    loaded = store.latest(limit=1)[0]
    assert loaded.total_ms == 0.0
    assert loaded.segments == {"valid": 1.0}
