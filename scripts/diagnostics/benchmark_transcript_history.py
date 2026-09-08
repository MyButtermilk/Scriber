"""Compare bounded synthetic history writes with the former unindexed FTS delete.

Uses production save, summary, list and search operations against a disposable
SQLite WAL database. Only the old FTS row-delete statement is replayed for the
baseline; no application settings or user data are read or changed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import database


def _baseline_sync(connection, transcript_id: str) -> None:
    connection.execute("DELETE FROM transcripts_fts WHERE id = ?", (transcript_id,))
    connection.execute(
        """INSERT INTO transcripts_fts(rowid, id, title, content, summary, channel)
           SELECT rowid, id, title, content, scriber_summary_text(summary, summary_format), channel
           FROM transcripts WHERE id = ?""",
        (transcript_id,),
    )


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "medianMs": round(statistics.median(samples), 3),
        "p95Ms": round(sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
    }


def _measure(count: int, iterations: int) -> dict[str, Any]:
    original_path = database._DB_PATH
    production_sync = database._sync_fts_row
    database._close_all_connections()
    content = "A synthetic conversation about design and engineering. " * 200
    try:
        with tempfile.TemporaryDirectory(prefix="scriber-history-benchmark-") as root:
            database._DB_PATH = Path(root) / "fixture.db"
            database.init_database()
            connection = database._get_connection()
            connection.executemany(
                """INSERT INTO transcripts
                   (id,title,date,duration,status,type,language,content,created_at,updated_at,preview)
                   VALUES (?,?,'2026-09-07','02:00','completed',?,'en',?,?,'2026-09-07','Synthetic preview')""",
                (
                    (
                        f"synthetic-{index:06}",
                        f"Episode {index}",
                        "mic" if index % 2 else "file",
                        content,
                        f"2026-09-07T{index // 3600:02}:{index // 60 % 60:02}:{index % 60:02}",
                    )
                    for index in range(count)
                ),
            )
            connection.execute(
                """INSERT INTO transcripts_fts(rowid,id,title,content,summary,channel)
                   SELECT rowid,id,title,content,summary,channel FROM transcripts"""
            )
            connection.commit()
            target = f"synthetic-{count // 2:06}"
            record = database.get_transcript(target)
            assert record is not None
            record["content"] = content + " Uniquechangedneedle"
            samples: dict[str, dict[str, list[float]]] = {
                name: {operation: [] for operation in ("save", "summary", "list", "search")}
                for name in ("baseline", "optimized")
            }
            # Reverse every other block to reduce thermal/cache/order bias.
            for block in range(iterations + 3):
                order = ("baseline", "optimized") if block % 2 == 0 else ("optimized", "baseline")
                for variant in order:
                    database._sync_fts_row = _baseline_sync if variant == "baseline" else production_sync
                    operations = (
                        ("save", lambda: database.save_transcript(record)),
                        ("summary", lambda: database.update_transcript_summary(target, "<p>Uniquechangedsummary</p>")),
                        (
                            "list",
                            lambda: database.load_transcript_metadata_page(
                                transcript_type="file", include_incomplete=True
                            ),
                        ),
                        ("search", lambda: database.search_transcript_metadata("Uniquechangedneedle")),
                    )
                    for operation, action in operations:
                        started = time.perf_counter_ns()
                        result = action()
                        elapsed = (time.perf_counter_ns() - started) / 1_000_000
                        if block >= 3:
                            samples[variant][operation].append(elapsed)
                        if operation == "list":
                            assert result["total"] == (count + 1) // 2 and len(result["items"]) == 50
                            assert all(not item["content"] for item in result["items"])
                        if operation == "search":
                            assert result["total"] == 1 and result["items"][0]["id"] == target
            plans = {
                name: [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, (target,))]
                for name, sql in {
                    "baseline": "DELETE FROM transcripts_fts WHERE id=?",
                    "optimized": "DELETE FROM transcripts_fts WHERE rowid=(SELECT rowid FROM transcripts WHERE id=?)",
                }.items()
            }
            read_statements: list[str] = []
            connection.set_trace_callback(read_statements.append)
            database.load_transcript_metadata_page(transcript_type="file", include_incomplete=True)
            connection.set_trace_callback(None)
            list_plans = [
                {
                    "statement": statement,
                    "plan": [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement)],
                }
                for statement in read_statements
                if statement.startswith("SELECT")
            ]
            database._close_all_connections()
            return {
                "transcripts": count,
                "contentBytesEach": len(content.encode()),
                "iterationsPerVariant": iterations,
                "queryPlans": plans,
                "listQueryPlans": list_plans,
                "results": {
                    variant: {operation: _summary(values) for operation, values in operations.items()}
                    for variant, operations in samples.items()
                },
            }
    finally:
        database._close_all_connections()
        database._DB_PATH = original_path
        database._sync_fts_row = production_sync


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", nargs="+", type=int, default=[2000, 10000])
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if any(not 100 <= count <= 20_000 for count in args.records) or not 5 <= args.iterations <= 50:
        parser.error("Use 100–20000 synthetic records and 5–50 iterations.")
    report = {"schema": "scriber-history-benchmark-v1", "fixtures": []}
    for count in args.records:
        print(f"Measuring {count} synthetic transcripts...", flush=True)
        report["fixtures"].append(_measure(count, args.iterations))
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
