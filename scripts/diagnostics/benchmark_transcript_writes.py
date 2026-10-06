"""Measure progress-only saves on synthetic transcripts in a disposable database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import database


def reference_sync(conn: sqlite3.Connection, transcript_id: str) -> None:
    # init_database repairs legacy rowid drift before any controller writes.
    # FTS5's id column is UNINDEXED: deleting by that value reads every stored
    # document. The shared rowid permits one indexed parent lookup and one FTS row.
    conn.execute(
        "DELETE FROM transcripts_fts WHERE rowid = (SELECT rowid FROM transcripts WHERE id = ?)",
        (transcript_id,),
    )
    conn.execute(
        """
        INSERT INTO transcripts_fts(rowid, id, title, content, summary, channel)
        SELECT rowid, id, title, content,
               scriber_summary_text(summary, summary_format), channel
        FROM transcripts
        WHERE id = ?
        """,
        (transcript_id,),
    )


def measure(record, run_sync, iterations, updates):
    samples = []
    changes = []
    with patch.object(database, "_sync_fts_row", run_sync):
        for _ in range(iterations):
            before = database._get_connection().total_changes
            start = time.perf_counter()
            for index in range(updates):
                database.save_transcript({**record, "step": f"Progress {index}"})
            samples.append((time.perf_counter() - start) * 1000)
            changes.append(database._get_connection().total_changes - before)
            assert database.get_transcript(record["id"])["step"] == f"Progress {updates - 1}"
            assert database.search_transcript_metadata("contentneedle")["total"] == 1
            assert database.search_transcript_metadata("summaryneedle")["total"] == 1
    return {"medianMs": round(statistics.median(samples), 3), "sqliteChanges": changes[-1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--words", type=int, default=20_000)
    parser.add_argument("--updates", type=int, default=25)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if not 100 <= args.words <= 100_000 or not 1 <= args.updates <= 100 or not 1 <= args.iterations <= 20:
        parser.error("words must be 100..100000, updates 1..100, iterations 1..20")
    record = {
        "id": "synthetic",
        "type": "file",
        "status": "processing",
        "title": "Synthetic title",
        "content": "contentneedle " + " ".join(f"word{index}" for index in range(args.words)),
        "summary": "<section><h2>summaryneedle</h2>" + "<p>Visible summary text.</p>" * 100 + "</section>",
        "summaryFormat": "html",
        "createdAt": "2026-10-06",
        "updatedAt": "2026-10-06",
    }
    database._close_all_connections()
    with (
        tempfile.TemporaryDirectory(prefix="scriber-write-benchmark-") as directory,
        patch.object(database, "_DB_PATH", Path(directory) / "synthetic.db"),
    ):
        try:
            database.init_database()
            database.save_transcript(record)
            current_sync = database._sync_fts_row
            before = measure(record, reference_sync, args.iterations, args.updates)
            after = measure(record, current_sync, args.iterations, args.updates)
            print(
                json.dumps({"words": args.words, "updates": args.updates, "before": before, "after": after}, indent=2)
            )
        finally:
            database._close_all_connections()


if __name__ == "__main__":
    main()
