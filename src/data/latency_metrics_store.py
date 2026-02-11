import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now().isoformat()


@dataclass(frozen=True)
class HotPathMetric:
    session_id: str
    total_ms: float
    segments: dict[str, float]
    created_at: str


class LatencyMetricsStore:
    """Persisted hot-path latency reports for local RALPH loops."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or (Path(__file__).resolve().parents[2] / "transcripts.db")
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hot_path_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        total_ms REAL NOT NULL,
                        segments_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_hot_path_metrics_created_at
                    ON hot_path_metrics(created_at DESC)
                    """
                )
                conn.commit()

    def record(self, session_id: str, segments: dict[str, float]) -> HotPathMetric:
        payload = {k: float(v) for k, v in (segments or {}).items()}
        total_ms = payload.get("hotkey_received_to_first_paste_ms")
        if total_ms is None:
            total_ms = max(payload.values(), default=0.0)
        metric = HotPathMetric(
            session_id=session_id,
            total_ms=float(total_ms),
            segments=payload,
            created_at=_now_iso(),
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO hot_path_metrics (session_id, total_ms, segments_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        metric.session_id,
                        metric.total_ms,
                        json.dumps(metric.segments, ensure_ascii=False),
                        metric.created_at,
                    ),
                )
                conn.commit()
        return metric

    def latest(self, *, limit: int = 50) -> list[HotPathMetric]:
        query_limit = max(1, min(1000, int(limit)))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT session_id, total_ms, segments_json, created_at
                    FROM hot_path_metrics
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (query_limit,),
                ).fetchall()

        metrics: list[HotPathMetric] = []
        for row in rows:
            segments: dict[str, Any]
            try:
                parsed = json.loads(row["segments_json"] or "{}")
                segments = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                segments = {}
            metrics.append(
                HotPathMetric(
                    session_id=row["session_id"],
                    total_ms=float(row["total_ms"] or 0.0),
                    segments={str(k): float(v) for k, v in segments.items()},
                    created_at=row["created_at"] or "",
                )
            )
        return metrics

