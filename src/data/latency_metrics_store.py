import json
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any

from src.runtime.paths import database_path


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
        self._db_path = db_path or database_path()
        self._lock = threading.Lock()
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._connection_generation = 0
        try:
            self._retention_rows = max(
                1,
                min(
                    1_000_000,
                    int(os.getenv("SCRIBER_HOT_PATH_METRICS_RETENTION", "5000") or 5000),
                ),
            )
        except (TypeError, ValueError):
            self._retention_rows = 5000
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._thread_local, "conn", None)
        if (
            conn is None
            or getattr(self._thread_local, "connection_generation", -1) != self._connection_generation
        ):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._thread_local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
                self._thread_local.connection_generation = self._connection_generation
        return conn

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
            self._connection_generation += 1
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass
        self._thread_local.conn = None
        self._thread_local.connection_generation = self._connection_generation

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
        payload: dict[str, float] = {}
        for key, value in (segments or {}).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                payload[str(key)] = number
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
                conn.execute(
                    """
                    DELETE FROM hot_path_metrics
                    WHERE id <= COALESCE(
                        (
                            SELECT id FROM hot_path_metrics
                            ORDER BY id DESC
                            LIMIT 1 OFFSET ?
                        ),
                        -1
                    )
                    """,
                    (self._retention_rows,),
                )
                conn.commit()
        return metric

    def latest(self, *, limit: int = 50) -> list[HotPathMetric]:
        query_limit = max(1, min(1000, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, total_ms, segments_json, created_at
                FROM hot_path_metrics
                ORDER BY created_at DESC, id DESC
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
            clean_segments: dict[str, float] = {}
            for key, value in segments.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    clean_segments[str(key)] = number
            try:
                total_ms = float(row["total_ms"] or 0.0)
            except (TypeError, ValueError):
                total_ms = 0.0
            if not math.isfinite(total_ms):
                total_ms = 0.0
            metrics.append(
                HotPathMetric(
                    session_id=row["session_id"],
                    total_ms=total_ms,
                    segments=clean_segments,
                    created_at=row["created_at"] or "",
                )
            )
        return metrics

    def summarize(self, *, limit: int = 200) -> dict[str, float | int]:
        metrics = self.latest(limit=limit)
        return self._summarize_metrics(metrics)

    def snapshot(
        self,
        *,
        limit: int = 200,
    ) -> tuple[dict[str, float | int], list[HotPathMetric]]:
        metrics = self.latest(limit=limit)
        return self._summarize_metrics(metrics), metrics

    @staticmethod
    def _summarize_metrics(metrics: list[HotPathMetric]) -> dict[str, float | int]:
        values = sorted([m.total_ms for m in metrics if m.total_ms >= 0.0])
        if not values:
            return {
                "count": 0,
                "minMs": 0.0,
                "p50Ms": 0.0,
                "p95Ms": 0.0,
                "p99Ms": 0.0,
                "maxMs": 0.0,
            }

        def percentile(pct: float) -> float:
            if not values:
                return 0.0
            idx = max(0, min(len(values) - 1, int(ceil((pct / 100.0) * len(values)) - 1)))
            return float(values[idx])

        return {
            "count": len(values),
            "minMs": float(values[0]),
            "p50Ms": percentile(50.0),
            "p95Ms": percentile(95.0),
            "p99Ms": percentile(99.0),
            "maxMs": float(values[-1]),
        }
