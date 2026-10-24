"""A foreign close must never free a SQLite connection under a running call."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from src import database
from src.data.job_store import JobStore
from src.data.latency_metrics_store import LatencyMetricsStore
from src.data.meeting_import_store import MeetingImportStore
from src.data.transcript_artifact_store import TranscriptArtifactStore
from src.runtime import sqlite_guard
from src.runtime.sqlite_guard import GuardedConnection, GuardedCursor


def _blocking_connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "guard.db", check_same_thread=False, factory=GuardedConnection)
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(value,) for value in range(3)])
    conn.commit()
    entered = threading.Event()
    release = threading.Event()

    def held(value):
        entered.set()
        release.wait(5)
        return value

    conn.create_function("held", 1, held)
    return conn, entered, release


def _close_in_thread(conn):
    closed = threading.Event()

    def close():
        conn.close()
        closed.set()

    thread = threading.Thread(target=close)
    thread.start()
    return thread, closed


@pytest.mark.parametrize("scope", ["statement", "transaction"])
def test_foreign_close_waits_for_the_running_native_step(tmp_path, scope):
    conn, entered, release = _blocking_connection(tmp_path)
    outcome: list[object] = []

    def owner():
        try:
            if scope == "transaction":
                with conn:
                    outcome.extend(conn.execute("SELECT held(x) FROM t ORDER BY x").fetchall())
            else:
                outcome.extend(conn.execute("SELECT held(x) FROM t ORDER BY x").fetchall())
        except sqlite3.ProgrammingError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=owner)
    worker.start()
    assert entered.wait(5)

    closer, closed = _close_in_thread(conn)
    # Without the guard this close freed the statement under the running step
    # (Windows access violation; segmentation fault on Linux).
    assert not closed.wait(0.2)
    release.set()
    worker.join(5)
    closer.join(5)

    assert closed.is_set()
    if scope == "transaction":
        assert outcome == [(0,), (1,), (2,)]
    else:
        # Between two statement calls the close may win; the owner then gets
        # SQLite's ordinary closed-database error instead of a native crash.
        assert outcome == [(0,), (1,), (2,)] or isinstance(outcome[-1], sqlite3.ProgrammingError)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_close_that_cannot_wait_leaves_the_handle_to_its_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_guard, "CLOSE_WAIT_SECONDS", 0.05)
    conn, entered, release = _blocking_connection(tmp_path)
    rows: list[tuple[int]] = []
    worker = threading.Thread(target=lambda: rows.extend(conn.execute("SELECT held(x) FROM t").fetchall()))
    worker.start()
    assert entered.wait(5)

    conn.close()
    release.set()
    worker.join(5)

    assert len(rows) == 3
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (3,)
    conn.close()


def test_guarded_connection_keeps_sqlite_cursor_semantics(tmp_path):
    conn = sqlite3.connect(tmp_path / "semantics.db", factory=GuardedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(x)")
    cursor = conn.executemany("INSERT INTO t VALUES (?)", [(value,) for value in range(4)])
    conn.commit()

    assert isinstance(cursor, GuardedCursor)
    assert [row["x"] for row in conn.execute("SELECT x FROM t ORDER BY x")] == [0, 1, 2, 3]
    assert len(conn.execute("SELECT x FROM t").fetchmany()) == 1
    assert len(conn.execute("SELECT x FROM t").fetchmany(3)) == 3
    with pytest.raises(RuntimeError), conn:
        conn.execute("INSERT INTO t VALUES (99)")
        raise RuntimeError("roll back")
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize(
    "store_factory",
    [JobStore, LatencyMetricsStore, MeetingImportStore, TranscriptArtifactStore],
)
def test_thread_local_stores_open_guarded_connections(tmp_path, store_factory):
    store = store_factory(tmp_path / "store.db")
    try:
        assert isinstance(store._connect(), GuardedConnection)
    finally:
        store.close()


def test_shared_transcript_database_opens_guarded_connections(tmp_path, monkeypatch):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    try:
        assert isinstance(database._get_connection(), GuardedConnection)
    finally:
        database._close_all_connections()
