#!/usr/bin/env python3
"""Run a temporally balanced Stable/Nightly FLAC lab comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
from typing import Any


LAB_ROOT = Path(__file__).resolve().parents[1]
MATRIX_RUNNER = LAB_ROOT / "scripts" / "run_matrix.py"
RUN_ORDER = (
    "stable",
    "nightly-simd",
    "nightly-simd",
    "stable",
    "nightly-simd",
    "stable",
    "stable",
    "nightly-simd",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def point_key(result: dict[str, Any]) -> tuple[int, int]:
    return result["input"]["sampleRateHz"], result["input"]["durationSeconds"]


def run_profile(profile: str, output_dir: Path, *, skip_build: bool) -> Path:
    command = [
        sys.executable,
        str(MATRIX_RUNNER),
        "--profile",
        profile,
        "--output-dir",
        str(output_dir),
        "--iterations",
        "1",
    ]
    if skip_build:
        command.append("--skip-build")
    completed = subprocess.run(
        command,
        cwd=LAB_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    matrix_path = Path(completed.stdout.strip().splitlines()[-1])
    if not matrix_path.is_file():
        raise RuntimeError(f"matrix runner did not produce {matrix_path}")
    return matrix_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build and warm each binary before entering the measured counterbalanced
    # sequence. Warmup results are retained but excluded from the comparison.
    for profile in ("stable", "nightly-simd"):
        print(f"warmup {profile}", flush=True)
        run_profile(profile, output_dir / "warmup" / profile, skip_build=False)

    suffix = ".exe" if os.name == "nt" else ""
    candidate_artifacts: dict[str, dict[str, Any]] = {}
    for profile in ("stable", "nightly-simd"):
        source = LAB_ROOT / "target" / profile / "release" / f"scriber-codec-lab{suffix}"
        retained = output_dir / "candidates" / f"scriber-codec-lab-{profile}{suffix}"
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, retained)
        candidate_artifacts[profile] = {
            "fileName": retained.name,
            "sha256": sha256_file(retained),
            "byteCount": retained.stat().st_size,
        }

    point_results: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}
    pass_records: list[dict[str, Any]] = []
    for pass_number, profile in enumerate(RUN_ORDER, start=1):
        print(f"pass {pass_number}/{len(RUN_ORDER)} {profile}", flush=True)
        pass_dir = output_dir / "passes" / f"{pass_number:02d}-{profile}"
        matrix_path = run_profile(profile, pass_dir, skip_build=True)
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        if not matrix["allRoundTripsByteExact"] or matrix["iterations"] != 1:
            raise RuntimeError(f"invalid matrix evidence in {matrix_path}")
        observed_binary_hashes = {
            result["reproducibility"]["candidateBinarySha256"]
            for result in matrix["results"]
        }
        expected_binary_hash = candidate_artifacts[profile]["sha256"]
        if observed_binary_hashes != {expected_binary_hash}:
            raise RuntimeError(f"candidate binary changed during pass {pass_number}")
        pass_records.append(
            {
                "pass": pass_number,
                "profile": profile,
                "matrixSha256": sha256_file(matrix_path),
                "candidateIds": matrix["candidateIds"],
                "compilerIdentities": matrix["compilerIdentities"],
            }
        )
        for result in matrix["results"]:
            profiles = point_results.setdefault(
                point_key(result), {"stable": [], "nightly-simd": []}
            )
            profiles[profile].append(result)

    comparisons: list[dict[str, Any]] = []
    for (sample_rate_hz, duration_seconds), profiles in sorted(point_results.items()):
        stable = profiles["stable"]
        nightly = profiles["nightly-simd"]
        if len(stable) != 4 or len(nightly) != 4:
            raise RuntimeError("counterbalanced matrix must contain four samples per profile")
        stable_ms = [float(result["timingsMs"]["coreEncode"]) for result in stable]
        nightly_ms = [float(result["timingsMs"]["coreEncode"]) for result in nightly]
        stable_p50 = statistics.median(stable_ms)
        nightly_p50 = statistics.median(nightly_ms)
        gain_percent = (stable_p50 - nightly_p50) / stable_p50 * 100.0
        all_results = stable + nightly
        comparisons.append(
            {
                "sampleRateHz": sample_rate_hz,
                "durationSeconds": duration_seconds,
                "stableCoreEncodeMs": stable_ms,
                "nightlySimdCoreEncodeMs": nightly_ms,
                "stableCoreEncodeP50Ms": stable_p50,
                "nightlySimdCoreEncodeP50Ms": nightly_p50,
                "nightlySimdGainPercent": gain_percent,
                "nightlySimdSpeedup": stable_p50 / nightly_p50,
                "allRoundTripsByteExact": all(
                    result["independentRoundTrip"]["byteExact"] for result in all_results
                ),
                "identicalInputAcrossProfiles": len(
                    {result["input"]["sha256"] for result in all_results}
                )
                == 1,
                "identicalFlacAcrossProfiles": len(
                    {result["output"]["sha256"] for result in all_results}
                )
                == 1,
            }
        )

    report = {
        "schemaVersion": 1,
        "lab": "scriber-codec-lab",
        "issue": 18,
        "design": "counterbalanced_ABBA_BAAB",
        "runOrder": list(RUN_ORDER),
        "warmupsExcluded": True,
        "samplesPerProfileAndSeries": 4,
        "candidateArtifacts": candidate_artifacts,
        "durationSeconds": [5, 15, 30, 60],
        "sampleRatesHz": [16_000, 48_000],
        "allRoundTripsByteExact": all(
            comparison["allRoundTripsByteExact"] for comparison in comparisons
        ),
        "allFlacOutputsIdenticalAcrossProfiles": all(
            comparison["identicalFlacAcrossProfiles"] for comparison in comparisons
        ),
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
        "claimBoundary": "codec-core lab only; no installed or provider latency claim",
        "passes": pass_records,
        "comparisons": comparisons,
    }
    report_path = output_dir / "counterbalanced-comparison.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
