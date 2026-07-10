import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import database
from src.database import _build_fts_query


def _search_count(text: str, query: str) -> int:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE transcripts_fts USING fts5(content)")
        conn.execute("INSERT INTO transcripts_fts(content) VALUES (?)", (text,))
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM transcripts_fts WHERE transcripts_fts MATCH ?",
                (_build_fts_query(query),),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_fts_query_treats_hyphenated_terms_as_search_text():
    assert _build_fts_query("hello-world") == '"hello-world"*'
    assert _search_count("A hello-world example", "hello-world") == 1


def test_fts_query_preserves_unicode_words():
    assert _build_fts_query("München") == '"münchen"*'
    assert _search_count("Grüße aus München", "München") == 1


def test_save_transcript_propagates_storage_failure(monkeypatch):
    class _FailingConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(database, "_get_connection", lambda: _FailingConnection())

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        database.save_transcript({"id": "must-fail"})


def test_search_results_include_summary_lifecycle_metadata(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    try:
        database.init_database()
        database.save_transcript(
            {
                "id": "summary-failed",
                "title": "München review",
                "date": "Today",
                "duration": "00:10",
                "status": "completed",
                "type": "file",
                "language": "de",
                "content": "Grüße aus München",
                "createdAt": "2026-07-10T00:00:00",
                "updatedAt": "2026-07-10T00:00:00",
            }
        )
        assert database.update_transcript_summary_state(
            "summary-failed",
            status="failed",
            error="provider unavailable",
            step="Summary failed",
        )

        result = database.search_transcript_metadata("München")

        assert result["total"] == 1
        assert result["items"][0]["summaryStatus"] == "failed"
        assert result["items"][0]["summaryError"] == "provider unavailable"
        assert result["items"][0]["summaryUpdatedAt"]
        assert database.get_transcript("summary-failed")["step"] == "Summary failed"
    finally:
        database._close_all_connections()


def test_database_mutations_report_missing_transcripts(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    try:
        database.init_database()

        assert database.delete_transcript("missing") is False
        assert database.update_transcript_summary("missing", "summary") is False
        assert database.update_transcript_summary_state("missing", status="failed") is False
    finally:
        database._close_all_connections()


def test_database_cleanup_reopens_connections_owned_by_worker_threads(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")

    def _connection_identity() -> int:
        conn = database._get_connection()
        conn.execute("SELECT 1").fetchone()
        return id(conn)

    try:
        database.init_database()
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(_connection_identity).result(timeout=2.0)
            database._close_all_connections()
            second = executor.submit(_connection_identity).result(timeout=2.0)

        assert second != first
    finally:
        database._close_all_connections()


def test_metadata_page_filters_incomplete_rows_and_paginates_in_sql(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")

    def _record(record_id: str, *, created_at: str, status: str, transcript_type: str) -> dict:
        return {
            "id": record_id,
            "title": record_id,
            "date": "Today",
            "duration": "00:10",
            "status": status,
            "type": transcript_type,
            "language": "de",
            "content": f"content {record_id}",
            "createdAt": created_at,
            "updatedAt": created_at,
        }

    try:
        database.init_database()
        database.save_transcript(_record("mic-old", created_at="2026-01-01T00:00:00", status="completed", transcript_type="mic"))
        database.save_transcript(_record("file", created_at="2026-01-02T00:00:00", status="completed", transcript_type="file"))
        database.save_transcript(_record("mic-new", created_at="2026-01-03T00:00:00", status="completed", transcript_type="mic"))
        database.save_transcript(_record("mic-active", created_at="2026-01-04T00:00:00", status="processing", transcript_type="mic"))

        first = database.load_transcript_metadata_page(transcript_type="mic", limit=1)
        second = database.load_transcript_metadata_page(transcript_type="mic", offset=1, limit=1)
        count_only = database.load_transcript_metadata_page(transcript_type="mic", limit=0)
        with_incomplete = database.load_transcript_metadata_page(
            transcript_type="mic",
            limit=10,
            include_incomplete=True,
        )
        without_active_duplicate = database.load_transcript_metadata_page(
            transcript_type="mic",
            limit=10,
            include_incomplete=True,
            exclude_ids=("mic-active",),
        )
        indexes = {
            row[1]
            for row in database._get_connection().execute("PRAGMA index_list(transcripts)").fetchall()
        }

        assert first["total"] == 2
        assert first["items"][0]["id"] == "mic-new"
        assert first["hasMore"] is True
        assert second["items"][0]["id"] == "mic-old"
        assert second["hasMore"] is False
        assert count_only["items"] == []
        assert count_only["total"] == 2
        assert [item["id"] for item in with_incomplete["items"]] == [
            "mic-active",
            "mic-new",
            "mic-old",
        ]
        assert without_active_duplicate["total"] == 2
        assert all(item["id"] != "mic-active" for item in without_active_duplicate["items"])
        assert "idx_transcripts_type_created_at" in indexes
    finally:
        database._close_all_connections()


def test_transcript_upsert_keeps_rowid_stable(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    record = {
        "id": "stable-row",
        "title": "Before",
        "date": "Today",
        "duration": "00:10",
        "status": "completed",
        "type": "mic",
        "language": "de",
        "content": "before",
        "createdAt": "2026-01-01T00:00:00",
        "updatedAt": "2026-01-01T00:00:00",
    }
    try:
        database.init_database()
        database.save_transcript(record)
        first_rowid = database._get_connection().execute(
            "SELECT rowid FROM transcripts WHERE id = ?", (record["id"],)
        ).fetchone()[0]

        record.update({"title": "After", "content": "after"})
        database.save_transcript(record)
        second_rowid = database._get_connection().execute(
            "SELECT rowid FROM transcripts WHERE id = ?", (record["id"],)
        ).fetchone()[0]

        assert second_rowid == first_rowid
        assert database.get_transcript(record["id"])["title"] == "After"
        assert database.search_transcript_metadata("after")["total"] == 1
    finally:
        database._close_all_connections()


def test_database_init_repairs_equal_count_fts_rowid_corruption(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    record = {
        "id": "repair-fts",
        "title": "Repairable index",
        "date": "Today",
        "duration": "00:10",
        "status": "completed",
        "type": "mic",
        "language": "en",
        "content": "unique repair needle",
        "createdAt": "2026-01-01T00:00:00",
        "updatedAt": "2026-01-01T00:00:00",
    }
    try:
        database.init_database()
        database.save_transcript(record)
        conn = database._get_connection()
        rowid = conn.execute(
            "SELECT rowid FROM transcripts WHERE id = ?",
            (record["id"],),
        ).fetchone()[0]
        conn.execute("DELETE FROM transcripts_fts WHERE id = ?", (record["id"],))
        conn.execute(
            "INSERT INTO transcripts_fts(rowid, id, title, content, summary, channel) "
            "VALUES (?, ?, ?, ?, '', '')",
            (rowid + 1000, "orphan", "orphan", "wrong content"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM transcripts_fts").fetchone()[0] == 1
        assert database.search_transcript_metadata("needle")["total"] == 0

        database.init_database()

        result = database.search_transcript_metadata("needle")
        assert result["total"] == 1
        assert result["items"][0]["id"] == record["id"]
    finally:
        database._close_all_connections()


def test_metadata_page_order_is_stable_when_timestamps_match(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    try:
        database.init_database()
        for record_id in ("same-a", "same-b"):
            database.save_transcript(
                {
                    "id": record_id,
                    "title": record_id,
                    "date": "Today",
                    "duration": "00:10",
                    "status": "completed",
                    "type": "mic",
                    "language": "en",
                    "content": "same timestamp",
                    "createdAt": "2026-01-01T00:00:00",
                    "updatedAt": "2026-01-01T00:00:00",
                }
            )

        first = database.load_transcript_metadata_page(limit=1)
        second = database.load_transcript_metadata_page(offset=1, limit=1)

        assert [first["items"][0]["id"], second["items"][0]["id"]] == [
            "same-b",
            "same-a",
        ]
    finally:
        database._close_all_connections()


def test_existing_transcript_ids_chunks_large_input(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    transcript_ids = [f"bulk-{index:04d}" for index in range(1200)]
    try:
        database.init_database()
        conn = database._get_connection()
        conn.executemany(
            """
            INSERT INTO transcripts
                (id, title, date, duration, status, type, language, created_at, updated_at)
            VALUES (?, ?, 'Today', '00:01', 'completed', 'file', 'en', ?, ?)
            """,
            (
                (transcript_id, transcript_id, "2026-01-01T00:00:00", "2026-01-01T00:00:00")
                for transcript_id in transcript_ids
            ),
        )
        conn.commit()

        assert database.existing_transcript_ids(
            [*transcript_ids, "bulk-0001", "missing"]
        ) == set(transcript_ids)
    finally:
        database._close_all_connections()
