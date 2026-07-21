#!/usr/bin/env python3
"""Run the fixed 5/15/30/60-second lossy-codec evidence matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from lab_common import (
    ALLOWED_DURATIONS_SECONDS,
    ALLOWED_SAMPLE_RATES_HZ,
    CANDIDATES,
    prepare_tooling,
    run_candidate,
    summarize_series,
    write_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=("all", *CANDIDATES), default="all")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least one")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tooling = prepare_tooling(
        ffmpeg_arg=args.ffmpeg,
        ffprobe_arg=args.ffprobe,
        skip_build=args.skip_build,
    )
    candidates = CANDIDATES if args.candidate == "all" else (args.candidate,)
    results = []
    sequence = 0
    for candidate in candidates:
        for sample_rate_hz in ALLOWED_SAMPLE_RATES_HZ:
            for duration_seconds in ALLOWED_DURATIONS_SECONDS:
                fixture = output_dir / "fixtures" / f"pcm16_{sample_rate_hz}hz_{duration_seconds}s.pcm"
                write_fixture(fixture, sample_rate_hz, duration_seconds)
                for iteration in range(1, args.iterations + 1):
                    sequence += 1
                    results.append(
                        run_candidate(
                            candidate=candidate,
                            fixture=fixture,
                            output_dir=output_dir,
                            sample_rate_hz=sample_rate_hz,
                            duration_seconds=duration_seconds,
                            iteration=iteration,
                            tooling=tooling,
                            sequence=sequence,
                        )
                    )
    shine_path = output_dir / "shine-challenger-status.json"
    shine_path.write_text(
        json.dumps(tooling.shine_status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    matrix = {
        "schemaVersion": 1,
        "lab": "scriber-lossy-codec-lab",
        "issue": 18,
        "candidateSelection": list(candidates),
        "durationSeconds": list(ALLOWED_DURATIONS_SECONDS),
        "sampleRatesHz": list(ALLOWED_SAMPLE_RATES_HZ),
        "iterations": args.iterations,
        "lossyByteEqualityClaimed": False,
        "qualityMetric": "aligned raw SNR plus scale-invariant SNR and normalized correlation",
        "shineChallenger": tooling.shine_status,
        "opusReferenceControl": {
            "implementation": "FFmpeg libopus",
            "libopusencUsed": False,
            "selection": "reference control permitted by Issue #18; avoids an unpinned native libopusenc build",
        },
        "tooling": tooling.identity,
        "system": tooling.system,
        "series": summarize_series(results),
        "allValidationPassed": all(result["validationPass"] for result in results),
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
        "results": results,
    }
    matrix_path = output_dir / "matrix.json"
    matrix_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(matrix_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"lossy codec matrix failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
