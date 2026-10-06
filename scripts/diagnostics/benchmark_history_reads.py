"""Compare history reads on synthetic SQLite data; never opens user databases."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import database


def legacy_indexes(conn):
    for name, columns in (
        ("idx_transcripts_created_at", "created_at DESC"),
        ("idx_transcripts_type_created_at", "type, created_at DESC"),
    ):
        conn.execute(f"DROP INDEX {name}")
        conn.execute(f"CREATE INDEX {name} ON transcripts({columns})")


def measure(conn, run, iterations):
    samples = []
    result = run()
    for _ in range(iterations):
        start = time.perf_counter()
        assert run() == result
        samples.append((time.perf_counter() - start) * 1000)
    ticks = 0

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    conn.set_progress_handler(progress, 100)
    try:
        assert run() == result
    finally:
        conn.set_progress_handler(None, 0)
    return {"medianMs": round(statistics.median(samples), 3), "vmStepsApprox": ticks * 100}, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5_000)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if not 100 <= args.rows <= 20_000 or not 1 <= args.iterations <= 20:
        parser.error("rows must be 100..20000; iterations must be 1..20")
    original_path = database._DB_PATH
    database._close_all_connections()
    try:
        with tempfile.TemporaryDirectory(prefix="scriber-history-reads-") as root:
            database._DB_PATH = Path(root) / "history.db"
            database.init_database()
            conn = database._get_connection()
            summary = (
                "<section><h2>Review</h2>" + "<p>Project progress and milestones.</p>" * 8 + "<p>10%</p></section>"
            )
            conn.executemany(
                "INSERT INTO transcripts(id,title,date,duration,status,type,language,content,summary,summary_format,created_at,updated_at) "
                "VALUES (?,?,'','',?,'mic','en',?,?,'html','2026-10-05','2026-10-05')",
                [
                    (
                        f"row-{i:05}",
                        f"Row {i}",
                        "recording" if i % 5 == 0 else "completed",
                        "Synthetic speech. " * 100,
                        summary,
                    )
                    for i in range(args.rows)
                ],
            )
            conn.execute(
                "INSERT INTO transcripts_fts(rowid,id,title,content,summary,channel) "
                "SELECT rowid,id,title,content,scriber_summary_text(summary,summary_format),channel FROM transcripts"
            )
            conn.commit()

            def history():
                return database.load_transcript_metadata_page(transcript_type="mic", offset=10, limit=50)

            legacy_indexes(conn)
            before, expected = measure(conn, history, args.iterations)
            database._ensure_history_indexes(conn)
            after, actual = measure(conn, history, args.iterations)
            assert actual == expected

            # Replay the former punctuation predicate. Selecting just IDs makes
            # this a conservative baseline: production also projects metadata.
            predicate = (
                "FROM transcripts t WHERE (instr(LOWER(t.title), '%') > 0 OR instr(LOWER(t.content), '%') > 0 OR "
                "instr(LOWER(scriber_summary_text(t.summary,t.summary_format)), '%') > 0 OR instr(LOWER(t.channel), '%') > 0) "
                "AND t.type = 'mic' "
            )

            def old_search():
                total = conn.execute("SELECT COUNT(*) " + predicate).fetchone()[0]
                ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT t.id " + predicate + "ORDER BY t.created_at DESC,t.id DESC LIMIT 50"
                    )
                ]
                return total, ids

            def search():
                result = database.search_transcript_metadata("%", transcript_type="mic", limit=50)
                return result["total"], [item["id"] for item in result["items"]]

            search_before, expected = measure(conn, old_search, args.iterations)
            search_after, actual = measure(conn, search, args.iterations)
            assert actual == expected
            print(
                json.dumps(
                    {
                        "python": sys.version.split()[0],
                        "sqlite": sqlite3.sqlite_version,
                        "rows": args.rows,
                        "history": {"before": before, "after": after},
                        "punctuationSearch": {"before": search_before, "after": search_after},
                    },
                    indent=2,
                )
            )
    finally:
        database._close_all_connections()
        database._DB_PATH = original_path


if __name__ == "__main__":
    main()
