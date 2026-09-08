"""Measure the real redacted log collector with deterministic, content-free logs.

Run through scripts/project-python.cmd. Reports contain timings and counts only;
--live reads the configured log roots without exporting their contents.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.runtime import debug_logs


def measure(iterations: int) -> dict:
    durations: list[float] = []
    reads = 0
    original_read = debug_logs._read_tail

    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original_read(*args, **kwargs)

    with patch.object(debug_logs, "_read_tail", read):
        for _ in range(iterations + 1):
            start = time.perf_counter()
            payload = debug_logs.collect_debug_logs(limit=1200)
            durations.append((time.perf_counter() - start) * 1000)
    warm = sorted(durations[1:])
    return {
        "iterations": iterations,
        "coldMs": round(durations[0], 3),
        "warmMedianMs": round(statistics.median(warm), 3),
        "warmP95Ms": round(warm[math.ceil(len(warm) * 0.95) - 1], 3),
        "tailReads": reads,
        "sources": len(payload["sources"]),
        "entries": len(payload["items"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not 3 <= args.iterations <= 100:
        parser.error("iterations must be between 3 and 100")
    if args.live:
        result = measure(args.iterations)
        result["fixture"] = "configured-local-logs-read-only"
    else:
        with tempfile.TemporaryDirectory(prefix="scriber-log-benchmark-") as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            for source in range(6):
                lines = []
                for index in range(1600):
                    lines.append(
                        json.dumps(
                            {
                                "timestamp": f"2026-09-08T00:{source:02d}:{index % 60:02d}+02:00",
                                "message": "Workflow stage completed",
                                "event": "operation.completed",
                                "workflow": ["live_mic", "meetings", "youtube", "file", "voice", "podcasts"][source],
                                "level": "WARNING" if index % 40 == 0 else "INFO",
                                "duration_ms": index / 10,
                                "meta": {"status": "success", "count": index, "sample_rate_hz": 48000},
                            }
                        )
                    )
                (logs / f"workflow-{source}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            with (
                patch.object(debug_logs, "logs_dir", lambda: logs),
                patch.object(debug_logs, "data_dir", lambda: root),
                patch.object(debug_logs, "repo_root", lambda: root),
                patch.object(debug_logs, "load_clear_offsets", lambda: {}),
            ):
                result = measure(args.iterations)
                result["fixture"] = "six-workflows-9600-events"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
