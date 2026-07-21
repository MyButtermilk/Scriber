#!/usr/bin/env python3
"""Warmup-excluded ABBA/BAAB comparison for both lossy codec pairs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from lab_common import (
    ALLOWED_DURATIONS_SECONDS,
    ALLOWED_SAMPLE_RATES_HZ,
    prepare_tooling,
    run_candidate,
    summarize_series,
    write_fixture,
)


PAIR_ORDERS = {
    "mp3": (
        "mp3-lame", "mp3-ffmpeg", "mp3-ffmpeg", "mp3-lame",
        "mp3-ffmpeg", "mp3-lame", "mp3-lame", "mp3-ffmpeg",
    ),
    "opus": (
        "opus-ruopus", "opus-libopus", "opus-libopus", "opus-ruopus",
        "opus-libopus", "opus-ruopus", "opus-ruopus", "opus-libopus",
    ),
}


def comparison_rows(series: list[dict]) -> list[dict]:
    by_key = {
        (row["candidateId"], row["sampleRateHz"], row["durationSeconds"]): row
        for row in series
    }
    pairs = (
        ("mp3_lame_in_process_v1", "mp3_ffmpeg_libmp3lame_current_control_v1", "mp3"),
        ("opus_ruopus_ogg_v1", "opus_ffmpeg_libopus_reference_control_v1", "opus"),
    )
    rows = []
    for candidate_id, control_id, codec in pairs:
        for sample_rate_hz in ALLOWED_SAMPLE_RATES_HZ:
            for duration_seconds in ALLOWED_DURATIONS_SECONDS:
                candidate = by_key[(candidate_id, sample_rate_hz, duration_seconds)]
                control = by_key[(control_id, sample_rate_hz, duration_seconds)]
                candidate_ms = candidate["runnerWallMsP50"]
                control_ms = control["runnerWallMsP50"]
                rows.append(
                    {
                        "codec": codec,
                        "sampleRateHz": sample_rate_hz,
                        "durationSeconds": duration_seconds,
                        "candidateId": candidate_id,
                        "controlId": control_id,
                        "candidateRunnerWallMsP50": candidate_ms,
                        "controlRunnerWallMsP50": control_ms,
                        "runnerWallSavedMs": control_ms - candidate_ms,
                        "runnerWallPercentChange": (control_ms - candidate_ms) * 100.0 / control_ms,
                        "comparisonBoundary": "separate-process lab wall time; not installed or in-process product latency",
                    }
                )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tooling = prepare_tooling(
        ffmpeg_arg=args.ffmpeg,
        ffprobe_arg=args.ffprobe,
        skip_build=args.skip_build,
    )

    fixtures = {}
    for sample_rate_hz in ALLOWED_SAMPLE_RATES_HZ:
        for duration_seconds in ALLOWED_DURATIONS_SECONDS:
            fixture = output_dir / "fixtures" / f"pcm16_{sample_rate_hz}hz_{duration_seconds}s.pcm"
            write_fixture(fixture, sample_rate_hz, duration_seconds)
            fixtures[(sample_rate_hz, duration_seconds)] = fixture

    warmups = []
    warmup_dir = output_dir / "warmup-excluded"
    for sequence, candidate in enumerate(
        ("mp3-lame", "mp3-ffmpeg", "opus-ruopus", "opus-libopus"), start=1
    ):
        warmups.append(
            run_candidate(
                candidate=candidate,
                fixture=fixtures[(16_000, 5)],
                output_dir=warmup_dir,
                sample_rate_hz=16_000,
                duration_seconds=5,
                iteration=1,
                tooling=tooling,
                sequence=sequence,
            )
        )

    measured = []
    execution = []
    sequence = 0
    for codec, order in PAIR_ORDERS.items():
        for pass_number, candidate in enumerate(order, start=1):
            combinations = [
                (sample_rate_hz, duration_seconds)
                for sample_rate_hz in ALLOWED_SAMPLE_RATES_HZ
                for duration_seconds in ALLOWED_DURATIONS_SECONDS
            ]
            if pass_number % 2 == 0:
                combinations.reverse()
            for sample_rate_hz, duration_seconds in combinations:
                sequence += 1
                result = run_candidate(
                    candidate=candidate,
                    fixture=fixtures[(sample_rate_hz, duration_seconds)],
                    output_dir=output_dir / "measured",
                    sample_rate_hz=sample_rate_hz,
                    duration_seconds=duration_seconds,
                    iteration=pass_number,
                    tooling=tooling,
                    sequence=sequence,
                )
                measured.append(result)
                execution.append(
                    {
                        "sequence": sequence,
                        "codec": codec,
                        "pass": pass_number,
                        "candidateId": result["candidateId"],
                        "sampleRateHz": sample_rate_hz,
                        "durationSeconds": duration_seconds,
                    }
                )

    series = summarize_series(measured)
    summary = {
        "schemaVersion": 1,
        "lab": "scriber-lossy-codec-lab",
        "issue": 18,
        "design": "per-codec ABBA/BAAB with alternating forward/reverse matrix traversal",
        "pairOrders": {key: list(value) for key, value in PAIR_ORDERS.items()},
        "warmupExcluded": True,
        "warmupRuns": [
            {
                "candidateId": row["candidateId"],
                "sampleRateHz": row["sampleRateHz"],
                "durationSeconds": row["durationSeconds"],
                "validationPass": row["validationPass"],
            }
            for row in warmups
        ],
        "durationSeconds": list(ALLOWED_DURATIONS_SECONDS),
        "sampleRatesHz": list(ALLOWED_SAMPLE_RATES_HZ),
        "samplesPerCandidateSeries": 4,
        "lossyByteEqualityClaimed": False,
        "tooling": tooling.identity,
        "system": tooling.system,
        "shineChallenger": tooling.shine_status,
        "execution": execution,
        "series": series,
        "comparisons": comparison_rows(series),
        "allValidationPassed": all(row["validationPass"] for row in measured),
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
    }
    status_path = output_dir / "shine-challenger-status.json"
    status_path.write_text(
        json.dumps(tooling.shine_status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary_path = output_dir / "counterbalanced-comparison.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(summary_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"counterbalanced lossy codec comparison failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
