#!/usr/bin/env python3
"""Compare FFmpeg FLAC-fast with pinned Rezin Stable build variants."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import struct
import subprocess
import sys
import time
from typing import Any


LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAINS_PATH = LAB_ROOT / "toolchains.json"
BASE_RUNNER_PATH = LAB_ROOT / "scripts" / "run_matrix.py"
SPEC = importlib.util.spec_from_file_location("codec_base_runner", BASE_RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

DURATIONS_SECONDS = (5, 15, 30, 60)
SAMPLE_RATES_HZ = (16_000, 48_000)
PROFILES = ("ffmpeg-fast", "rezin-default", "rezin-lto")
RUN_ORDER = (
    "ffmpeg-fast",
    "rezin-default",
    "rezin-lto",
    "rezin-default",
    "rezin-lto",
    "ffmpeg-fast",
    "rezin-lto",
    "ffmpeg-fast",
    "rezin-default",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_wav_fixture(pcm_path: Path, wav_path: Path, sample_rate_hz: int) -> None:
    pcm_size = pcm_path.stat().st_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate_hz,
        sample_rate_hz * 2,
        2,
        16,
        b"data",
        pcm_size,
    )
    assert len(header) == 44
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wav_path.open("wb") as output, pcm_path.open("rb") as pcm:
        output.write(header)
        shutil.copyfileobj(pcm, output)


def run_checked(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=LAB_ROOT,
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"command failed ({error.returncode}): {command[0]}\n{error.stderr}"
        ) from error


def build_rezin_candidates(toolchain: str, output_dir: Path) -> dict[str, dict[str, Any]]:
    suffix = ".exe" if os.name == "nt" else ""
    variants = {
        "rezin-default": {
            "target": LAB_ROOT / "target" / "rezin-default",
            "cargoProfile": "lab-release-default",
            "cargoArgs": ["--profile", "lab-release-default"],
            "variant": "stable_release_default",
            "lto": "off",
            "codegenUnits": 16,
        },
        "rezin-lto": {
            "target": LAB_ROOT / "target" / "rezin-lto",
            "cargoProfile": "release",
            "cargoArgs": ["--release"],
            "variant": "stable_release_lto_thin_cgu1",
            "lto": "thin",
            "codegenUnits": 1,
        },
    }
    records: dict[str, dict[str, Any]] = {}
    for profile, config in variants.items():
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(config["target"])
        env["RUSTFLAGS"] = ""
        env["SCRIBER_CODEC_LAB_REZIN_BUILD_VARIANT"] = config["variant"]
        command = [
            "rustup",
            "run",
            toolchain,
            "cargo",
            "build",
            "--locked",
            "--features",
            "rezin-candidate",
            "--bin",
            "scriber-rezin-candidate",
            *config["cargoArgs"],
        ]
        run_checked(command, env=env)
        built = (
            Path(config["target"])
            / config["cargoProfile"]
            / f"scriber-rezin-candidate{suffix}"
        )
        retained = output_dir / "candidates" / f"scriber-rezin-candidate-{profile}{suffix}"
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, retained)
        records[profile] = {
            "path": retained,
            "fileName": retained.name,
            "sha256": sha256_file(retained),
            "byteCount": retained.stat().st_size,
            "toolchain": toolchain,
            "cargoProfile": config["cargoProfile"],
            "variant": config["variant"],
            "lto": config["lto"],
            "codegenUnits": config["codegenUnits"],
        }
    return records


def decode_and_verify(
    ffmpeg: str,
    encoded: Path,
    expected_pcm: Path,
    decoded: Path,
    sample_rate_hz: int,
) -> dict[str, Any]:
    decoded.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(encoded),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(sample_rate_hz),
            "-ac",
            "1",
            str(decoded),
        ]
    )
    exact, mismatch_offset = BASE.compare_exact(expected_pcm, decoded)
    if not exact:
        raise RuntimeError(f"byte-exact decode failed at offset {mismatch_offset}")
    return {
        "decoder": "ffmpeg",
        "byteExact": exact,
        "mismatchOffset": mismatch_offset,
        "decodedByteCount": decoded.stat().st_size,
        "decodedSha256": sha256_file(decoded),
    }


def run_ffmpeg_fast(
    ffmpeg: str,
    ffmpeg_record: dict[str, Any],
    wav: Path,
    pcm: Path,
    output: Path,
    decoded: Path,
    sample_rate_hz: int,
    duration_seconds: int,
    iteration: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(wav),
        "-map_metadata",
        "-1",
        "-c:a",
        "flac",
        "-compression_level",
        "0",
        str(output),
    ]
    started = time.perf_counter_ns()
    run_checked(command)
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    output_bytes = output.read_bytes()
    attestation = {
        "schemaVersion": 1,
        "candidateId": "ffmpeg_7_0_flac_fast_level_0",
        "codec": "flac",
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
        "input": {
            "container": "wav",
            "codec": "pcm_s16le",
            "sampleRateHz": sample_rate_hz,
            "channels": 1,
            "bitsPerSample": 16,
            "durationSeconds": duration_seconds,
            "pcmByteCount": pcm.stat().st_size,
            "pcmSha256": sha256_file(pcm),
            "wavSha256": sha256_file(wav),
        },
        "output": {
            "container": "native_flac",
            "codec": "flac",
            "byteCount": len(output_bytes),
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
            "encodedToPcmRatio": len(output_bytes) / pcm.stat().st_size,
        },
        "encoder": {
            "implementation": "ffmpeg",
            "version": ffmpeg_record["version"],
            "executableSha256": ffmpeg_record["sha256"],
            "codec": "flac",
            "compressionLevel": 0,
            "commandContract": [
                "-map_metadata -1",
                "-c:a flac",
                "-compression_level 0",
            ],
        },
        "build": {"variant": "prebuilt_ffmpeg_control"},
        "timingsMs": {"processWall": wall_ms},
        "iteration": iteration,
    }
    attestation["independentRoundTrip"] = decode_and_verify(
        ffmpeg, output, pcm, decoded, sample_rate_hz
    )
    return attestation


def run_rezin(
    profile: str,
    candidate: dict[str, Any],
    ffmpeg: str,
    wav: Path,
    pcm: Path,
    output: Path,
    decoded: Path,
    attestation_path: Path,
    sample_rate_hz: int,
    duration_seconds: int,
    iteration: int,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    attestation_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(candidate["path"]),
        "--input-wav",
        str(wav),
        "--output-flac",
        str(output),
        "--attestation",
        str(attestation_path),
        "--sample-rate-hz",
        str(sample_rate_hz),
        "--duration-seconds",
        str(duration_seconds),
        "--iteration",
        str(iteration),
        "--build-variant",
        candidate["variant"],
    ]
    started = time.perf_counter_ns()
    run_checked(command)
    wall_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    if attestation["build"]["lto"] != candidate["lto"]:
        raise RuntimeError(f"{profile} LTO attestation mismatch")
    attestation["input"]["pcmSha256"] = sha256_file(pcm)
    attestation["timingsMs"]["processWall"] = wall_ms
    attestation["reproducibility"] = {
        "candidateBinarySha256": candidate["sha256"],
        "cargoLockSha256": sha256_file(LAB_ROOT / "Cargo.lock"),
        "cargoManifestSha256": sha256_file(LAB_ROOT / "Cargo.toml"),
        "toolchainManifestSha256": sha256_file(TOOLCHAINS_PATH),
        "labSourceTreeSha256": BASE.source_tree_sha256(),
        "rezinSourceCommit": candidate["sourceCommit"],
        "rezinPatchedEncodeSha256": candidate["patchedEncodeSha256"],
    }
    attestation["independentRoundTrip"] = decode_and_verify(
        ffmpeg, output, pcm, decoded, sample_rate_hz
    )
    attestation_path.write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return attestation


def run_profile_matrix(
    profile: str,
    pass_number: int,
    output_dir: Path,
    ffmpeg: str,
    ffmpeg_record: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    results = []
    for sample_rate_hz in SAMPLE_RATES_HZ:
        for duration_seconds in DURATIONS_SECONDS:
            fixture_stem = f"pcm16_{sample_rate_hz}hz_{duration_seconds}s"
            pcm = output_dir / "fixtures" / f"{fixture_stem}.pcm"
            wav = output_dir / "fixtures" / f"{fixture_stem}.wav"
            if not pcm.is_file():
                BASE.write_fixture(pcm, sample_rate_hz, duration_seconds)
                write_wav_fixture(pcm, wav, sample_rate_hz)
            stem = f"p{pass_number:02d}_{profile}_{sample_rate_hz}hz_{duration_seconds}s"
            output = output_dir / "encoded" / f"{stem}.flac"
            decoded = output_dir / "decoded" / f"{stem}.pcm"
            attestation_path = output_dir / "attestations" / f"{stem}.json"
            if profile == "ffmpeg-fast":
                result = run_ffmpeg_fast(
                    ffmpeg,
                    ffmpeg_record,
                    wav,
                    pcm,
                    output,
                    decoded,
                    sample_rate_hz,
                    duration_seconds,
                    pass_number,
                )
                attestation_path.parent.mkdir(parents=True, exist_ok=True)
                attestation_path.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            else:
                result = run_rezin(
                    profile,
                    candidates[profile],
                    ffmpeg,
                    wav,
                    pcm,
                    output,
                    decoded,
                    attestation_path,
                    sample_rate_hz,
                    duration_seconds,
                    pass_number,
                )
            results.append(result)
    return results


def pgo_assessment(toolchain: str) -> dict[str, Any]:
    sysroot = Path(
        run_checked(["rustup", "run", toolchain, "rustc", "--print", "sysroot"])
        .stdout.strip()
    )
    matches = list(sysroot.rglob("llvm-profdata*"))
    return {
        "status": "eligible_not_run" if matches else "fail_closed_not_run",
        "toolchain": toolchain,
        "llvmProfdataAvailable": bool(matches),
        "reason": (
            "bounded PGO may be run explicitly with the pinned llvm-tools component"
            if matches
            else "pinned toolchain has no llvm-tools-preview/llvm-profdata; no implicit tool download or unverifiable PGO build"
        ),
        "productionReady": False,
        "productionPromoted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    toolchains = json.loads(TOOLCHAINS_PATH.read_text(encoding="utf-8"))
    rezin_toolchain = toolchains["rezinStable"]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg was not found on PATH", file=sys.stderr)
        return 4
    ffmpeg_record = {
        "version": run_checked([ffmpeg, "-version"]).stdout.splitlines()[0],
        "sha256": sha256_file(Path(ffmpeg)),
        "byteCount": Path(ffmpeg).stat().st_size,
    }
    print("building pinned Rezin candidates", flush=True)
    candidates = build_rezin_candidates(rezin_toolchain, output_dir)
    for candidate in candidates.values():
        candidate["sourceCommit"] = toolchains["rezinCrate"]["sourceCommit"]
        candidate["patchedEncodeSha256"] = toolchains["rezinCrate"]["labPatch"][
            "patchedEncodeSha256"
        ]

    # One allowed 5-second warmup per candidate. These records are intentionally
    # excluded from every comparison statistic.
    for warmup_number, profile in enumerate(PROFILES, start=1):
        print(f"warmup {profile}", flush=True)
        warm_dir = output_dir / "warmup" / profile
        warm_pcm = warm_dir / "fixture.pcm"
        warm_wav = warm_dir / "fixture.wav"
        BASE.write_fixture(warm_pcm, 16_000, 5)
        write_wav_fixture(warm_pcm, warm_wav, 16_000)
        output = warm_dir / "output.flac"
        decoded = warm_dir / "decoded.pcm"
        if profile == "ffmpeg-fast":
            run_ffmpeg_fast(
                ffmpeg,
                ffmpeg_record,
                warm_wav,
                warm_pcm,
                output,
                decoded,
                16_000,
                5,
                warmup_number,
            )
        else:
            run_rezin(
                profile,
                candidates[profile],
                ffmpeg,
                warm_wav,
                warm_pcm,
                output,
                decoded,
                warm_dir / "attestation.json",
                16_000,
                5,
                warmup_number,
            )

    by_series: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}
    pass_records = []
    for pass_number, profile in enumerate(RUN_ORDER, start=1):
        print(f"pass {pass_number}/{len(RUN_ORDER)} {profile}", flush=True)
        results = run_profile_matrix(
            profile, pass_number, output_dir, ffmpeg, ffmpeg_record, candidates
        )
        pass_records.append(
            {
                "pass": pass_number,
                "profile": profile,
                "allRoundTripsByteExact": all(
                    result["independentRoundTrip"]["byteExact"] for result in results
                ),
            }
        )
        for result in results:
            key = (result["input"]["sampleRateHz"], result["input"]["durationSeconds"])
            profiles = by_series.setdefault(key, {name: [] for name in PROFILES})
            profiles[profile].append(result)

    comparisons = []
    for (sample_rate_hz, duration_seconds), profiles in sorted(by_series.items()):
        wall = {
            profile: [result["timingsMs"]["processWall"] for result in profiles[profile]]
            for profile in PROFILES
        }
        p50 = {profile: statistics.median(values) for profile, values in wall.items()}
        sizes = {
            profile: statistics.median(
                [result["output"]["byteCount"] for result in profiles[profile]]
            )
            for profile in PROFILES
        }
        all_results = [result for profile in PROFILES for result in profiles[profile]]
        comparisons.append(
            {
                "sampleRateHz": sample_rate_hz,
                "durationSeconds": duration_seconds,
                "processWallMs": wall,
                "processWallP50Ms": p50,
                "outputByteCount": sizes,
                "rezinDefaultVsFfmpegGainPercent":
                    (p50["ffmpeg-fast"] - p50["rezin-default"])
                    / p50["ffmpeg-fast"]
                    * 100.0,
                "rezinLtoVsFfmpegGainPercent":
                    (p50["ffmpeg-fast"] - p50["rezin-lto"])
                    / p50["ffmpeg-fast"]
                    * 100.0,
                "rezinLtoVsDefaultGainPercent":
                    (p50["rezin-default"] - p50["rezin-lto"])
                    / p50["rezin-default"]
                    * 100.0,
                "allRoundTripsByteExact": all(
                    result["independentRoundTrip"]["byteExact"] for result in all_results
                ),
                "identicalDecodedPcm": len(
                    {
                        result["independentRoundTrip"]["decodedSha256"]
                        for result in all_results
                    }
                )
                == 1,
            }
        )

    public_candidates = {
        profile: {key: value for key, value in record.items() if key != "path"}
        for profile, record in candidates.items()
    }
    report = {
        "schemaVersion": 1,
        "lab": "scriber-codec-lab",
        "issue": 18,
        "design": "three_profile_balanced_latin_order",
        "runOrder": list(RUN_ORDER),
        "warmupsExcluded": True,
        "samplesPerProfileAndSeries": 3,
        "durationSeconds": list(DURATIONS_SECONDS),
        "sampleRatesHz": list(SAMPLE_RATES_HZ),
        "ffmpegControl": ffmpeg_record,
        "rezinCandidates": public_candidates,
        "rezinToolchainPin": rezin_toolchain,
        "rezinCratePin": toolchains["rezinCrate"],
        "pgoAssessment": pgo_assessment(rezin_toolchain),
        "allRoundTripsByteExact": all(
            comparison["allRoundTripsByteExact"] for comparison in comparisons
        ),
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
        "claimBoundary": "isolated codec process-wall lab only; no installed or provider latency claim",
        "passes": pass_records,
        "comparisons": comparisons,
    }
    report_path = output_dir / "challenger-comparison.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
