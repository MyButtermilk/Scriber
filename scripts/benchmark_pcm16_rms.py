"""Deterministic microbenchmark for the shared PCM16 RMS hot path."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.runtime.pcm_audio import pcm16le_rms  # noqa: E402

BENCHMARK_ID = "pcm16le-rms-v1"


def deterministic_pcm16le_fixture(sample_count: int) -> bytes:
    """Build the same full-range signed-16 fixture on every run."""

    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    samples = [
        ((index * 7_919 + 12_345) & 0xFFFF) - 32_768
        for index in range(sample_count)
    ]
    return struct.pack(f"<{sample_count}h", *samples)


def run_benchmark(
    *,
    iterations: int,
    warmup: int,
    sample_count: int,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")

    pcm = deterministic_pcm16le_fixture(sample_count)
    expected_rms = pcm16le_rms(pcm)
    for _ in range(warmup):
        pcm16le_rms(pcm)

    checksum = 0
    started_ns = time.perf_counter_ns()
    for _ in range(iterations):
        checksum += pcm16le_rms(pcm)
    elapsed_ns = max(1, time.perf_counter_ns() - started_ns)

    return {
        "schemaVersion": 1,
        "benchmark": BENCHMARK_ID,
        "fixture": {
            "encoding": "pcm_s16le",
            "sampleCount": sample_count,
            "byteCount": len(pcm),
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "rms": expected_rms,
        },
        "iterations": iterations,
        "warmup": warmup,
        "checksum": checksum,
        "elapsedNs": elapsed_ns,
        "nsPerFrame": round(elapsed_ns / iterations, 3),
        "framesPerSecond": round(iterations * 1_000_000_000 / elapsed_ns, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100_000)
    parser.add_argument("--warmup", type=int, default=5_000)
    parser.add_argument("--sample-count", type=int, default=320)
    args = parser.parse_args()
    result = run_benchmark(
        iterations=args.iterations,
        warmup=args.warmup,
        sample_count=args.sample_count,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
