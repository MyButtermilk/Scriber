"""Measure enabled/disabled diagnostic writers with synthetic workflow events."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from src.core import logging_setup


def measure(enabled: bool, *, events: int = 40, iterations: int = 40) -> dict:
    logging_setup.set_diagnostic_logging_enabled(enabled)
    samples = []
    for _ in range(iterations):
        start = perf_counter()
        for index in range(events):
            logging_setup.emit_event(
                logger,
                "Synthetic workflow boundary completed",
                event="benchmark.stage.completed",
                workflow="live_mic",
                stage="benchmark",
                duration_ms=index / 10,
                outcome="success",
                meta={"count": index, "sample_rate_hz": 48000},
            )
        samples.append((perf_counter() - start) * 1000)
    return {
        "enabled": enabled,
        "eventsPerSample": events,
        "samples": iterations,
        "medianMs": round(statistics.median(samples), 4),
        "p95Ms": round(sorted(samples)[37], 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="scriber-logging-benchmark-") as temp:
        root = Path(temp)
        with patch.object(logging_setup, "logs_dir", lambda: root):
            logging_setup.setup_logging(component="benchmark", force=True, add_stderr=False, enabled=True)
            on = measure(True)
            before_off = sum(path.stat().st_size for path in root.iterdir())
            off = measure(False)
            after_off = sum(path.stat().st_size for path in root.iterdir())
            result = {
                "fixture": "synthetic-events-two-real-file-sinks",
                "on": on,
                "off": off,
                "bytesWrittenWhileOff": after_off - before_off,
            }
            logger.remove()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
