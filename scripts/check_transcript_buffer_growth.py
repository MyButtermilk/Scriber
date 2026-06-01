from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def make_segment(index: int, chars: int) -> str:
    prefix = f"segment-{index:06d} "
    if len(prefix) >= chars:
        return prefix[:chars]
    return prefix + ("x" * (chars - len(prefix)))


def expected_content_chars(segments: int, segment_chars: int) -> int:
    if segments <= 0:
        return 0
    return (segments * segment_chars) + ((segments - 1) * len("\n\n"))


def build_record() -> Any:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        from src.web_api import TranscriptRecord

    return TranscriptRecord(
        id="synthetic-30-minute-live-session",
        title="Synthetic Live Mic",
        date="Today",
        duration="00:00",
        status="recording",
        type="mic",
        language="en",
    )


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    record = build_record()
    metadata_content_leaked = False
    metadata_reads = 0
    last_metadata: dict[str, Any] = {}

    tracemalloc.start()
    append_started = time.perf_counter()
    for index in range(1, args.segments + 1):
        record.append_final_text(make_segment(index, args.segment_chars))
        if index % args.metadata_read_interval == 0 or index == args.segments:
            metadata_reads += 1
            last_metadata = record.to_public(include_content=False)
            metadata_content_leaked = metadata_content_leaked or "content" in last_metadata
    append_ms = (time.perf_counter() - append_started) * 1000

    pre_materialize_content_chars = len(record.content)
    pending_before_materialize = len(record._pending_content_segments)
    _, peak_before_materialize_bytes = tracemalloc.get_traced_memory()

    materialize_started = time.perf_counter()
    materialized_content = record.content_text()
    materialize_ms = (time.perf_counter() - materialize_started) * 1000
    _, peak_after_materialize_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    expected_chars = expected_content_chars(args.segments, args.segment_chars)
    expected_pending = max(0, args.segments - 1)
    checks = {
        "metadataDoesNotExposeContent": not metadata_content_leaked,
        "appendDidNotMaterializePendingSegments": pending_before_materialize == expected_pending,
        "contentStayedAtFirstSegmentBeforeMaterialize": pre_materialize_content_chars
        == min(args.segment_chars, expected_chars),
        "materializedContentHasExpectedLength": len(materialized_content) == expected_chars,
        "pendingSegmentsClearedAfterMaterialize": len(record._pending_content_segments) == 0,
    }

    return {
        "schemaVersion": 1,
        "ok": all(checks.values()),
        "segments": args.segments,
        "segmentChars": args.segment_chars,
        "metadataReadInterval": args.metadata_read_interval,
        "metadataReads": metadata_reads,
        "appendMs": round(append_ms, 3),
        "materializeMs": round(materialize_ms, 3),
        "preMaterializeContentChars": pre_materialize_content_chars,
        "pendingBeforeMaterialize": pending_before_materialize,
        "materializedContentChars": len(materialized_content),
        "expectedContentChars": expected_chars,
        "peakBeforeMaterializeBytes": peak_before_materialize_bytes,
        "peakAfterMaterializeBytes": peak_after_materialize_bytes,
        "metadataContentLeaked": metadata_content_leaked,
        "lastPreview": last_metadata.get("preview", ""),
        "checks": checks,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synthetic guard for long live transcript buffering. The default "
            "shape approximates one final segment per second over 30 minutes."
        )
    )
    parser.add_argument("--segments", type=int, default=1800)
    parser.add_argument("--segment-chars", type=int, default=96)
    parser.add_argument("--metadata-read-interval", type=int, default=30)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    args.segments = max(1, int(args.segments))
    args.segment_chars = max(16, int(args.segment_chars))
    args.metadata_read_interval = max(1, int(args.metadata_read_interval))
    return args


def write_result(result: dict[str, Any], output_path: str) -> None:
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(output_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_check(args)
    write_result(result, args.output)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
