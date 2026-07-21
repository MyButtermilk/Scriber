#!/usr/bin/env python3
"""Build and validate the isolated flacenc candidate matrix.

The measured encode window is inside the Rust binary. Fixture generation and
independent ffmpeg decoding happen outside that window.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any


LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAINS_PATH = LAB_ROOT / "toolchains.json"
DURATIONS_SECONDS = (5, 15, 30, 60)
SAMPLE_RATES_HZ = (16_000, 48_000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_sha256() -> str:
    digest = hashlib.sha256()
    for relative in (
        "Cargo.lock",
        "Cargo.toml",
        "build.rs",
        "src/bin/rezin_candidate.rs",
        "src/main.rs",
        "toolchains.json",
        "vendor/rezin-flac-0.2.1/src/encode.rs",
    ):
        path = LAB_ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def deterministic_sample(index: int, sample_rate_hz: int) -> int:
    # Integer-only, speech-like gated harmonics. Fixture generation is not part
    # of the measured encoder interval and remains byte-identical across hosts.
    phase_a = (index * 197 * 65_536 // sample_rate_hz) & 0xFFFF
    phase_b = (index * 431 * 65_536 // sample_rate_hz) & 0xFFFF
    triangle_a = 32_767 - abs(phase_a - 32_768) * 2
    triangle_b = 32_767 - abs(phase_b - 32_768) * 2
    gate_period = max(1, sample_rate_hz // 4)
    gate = 0 if index % gate_period >= gate_period * 4 // 5 else 1
    value = gate * ((triangle_a * 7 + triangle_b * 3) // 20)
    return max(-32_768, min(32_767, value))


def write_fixture(path: Path, sample_rate_hz: int, duration_seconds: int) -> None:
    if sample_rate_hz not in SAMPLE_RATES_HZ:
        raise ValueError("sample rate outside fixed matrix")
    if duration_seconds not in DURATIONS_SECONDS:
        raise ValueError("duration outside fixed matrix")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        buffer = bytearray()
        for index in range(sample_rate_hz * duration_seconds):
            buffer += struct.pack("<h", deterministic_sample(index, sample_rate_hz))
            if len(buffer) >= 1024 * 1024:
                stream.write(buffer)
                buffer.clear()
        stream.write(buffer)


def run_checked(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=LAB_ROOT,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def toolchain_available(toolchain: str) -> bool:
    result = subprocess.run(
        ["rustup", "run", toolchain, "rustc", "--version"],
        cwd=LAB_ROOT,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def binary_path(target_dir: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return target_dir / "release" / f"scriber-codec-lab{suffix}"


def compare_exact(left: Path, right: Path) -> tuple[bool, int | None]:
    offset = 0
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(1024 * 1024)
            right_chunk = right_stream.read(1024 * 1024)
            if left_chunk == right_chunk:
                if not left_chunk:
                    return True, None
                offset += len(left_chunk)
                continue
            common = min(len(left_chunk), len(right_chunk))
            for index in range(common):
                if left_chunk[index] != right_chunk[index]:
                    return False, offset + index
            return False, offset + common


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("stable", "nightly-simd"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    toolchains = json.loads(TOOLCHAINS_PATH.read_text(encoding="utf-8"))
    toolchain = toolchains["stable" if args.profile == "stable" else "nightlySimd"]
    if not toolchain_available(toolchain):
        print(f"pinned toolchain is not installed: {toolchain}", file=sys.stderr)
        return 3
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg was not found on PATH", file=sys.stderr)
        return 4

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dir = (LAB_ROOT / "target" / args.profile).resolve()
    build_env = os.environ.copy()
    build_env["CARGO_TARGET_DIR"] = str(target_dir)
    # Attest an explicit empty override rather than silently inheriting a
    # machine-local RUSTFLAGS value.
    build_env["RUSTFLAGS"] = ""
    build = ["rustup", "run", toolchain, "cargo", "build", "--release", "--locked"]
    if args.profile == "nightly-simd":
        build += ["--features", "nightly-simd"]
    if not args.skip_build:
        run_checked(build, env=build_env)
    binary = binary_path(target_dir)
    if not binary.is_file():
        print(f"candidate binary is missing: {binary}", file=sys.stderr)
        return 5

    ffmpeg_version = run_checked([ffmpeg, "-version"]).stdout.splitlines()[0]
    results: list[dict[str, Any]] = []
    for sample_rate_hz in SAMPLE_RATES_HZ:
        for duration_seconds in DURATIONS_SECONDS:
            fixture = output_dir / "fixtures" / f"pcm16_{sample_rate_hz}hz_{duration_seconds}s.pcm"
            write_fixture(fixture, sample_rate_hz, duration_seconds)
            for iteration in range(1, args.iterations + 1):
                stem = f"{args.profile}_{sample_rate_hz}hz_{duration_seconds}s_i{iteration}"
                encoded = output_dir / "encoded" / f"{stem}.flac"
                decoded = output_dir / "decoded" / f"{stem}.pcm"
                attestation_path = output_dir / "attestations" / f"{stem}.json"
                for path in (encoded, decoded, attestation_path):
                    path.parent.mkdir(parents=True, exist_ok=True)

                run_checked(
                    [
                        str(binary),
                        "--input-pcm",
                        str(fixture),
                        "--output-flac",
                        str(encoded),
                        "--attestation",
                        str(attestation_path),
                        "--sample-rate-hz",
                        str(sample_rate_hz),
                        "--duration-seconds",
                        str(duration_seconds),
                        "--iteration",
                        str(iteration),
                    ]
                )
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
                exact, mismatch_offset = compare_exact(fixture, decoded)
                attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
                attestation["independentRoundTrip"] = {
                    "decoder": "ffmpeg",
                    "decoderVersion": ffmpeg_version,
                    "decoderExecutableSha256": sha256_file(Path(ffmpeg)),
                    "byteExact": exact,
                    "mismatchOffset": mismatch_offset,
                    "decodedByteCount": decoded.stat().st_size,
                    "decodedSha256": sha256_file(decoded),
                }
                attestation["reproducibility"] = {
                    "candidateBinarySha256": sha256_file(binary),
                    "cargoLockSha256": sha256_file(LAB_ROOT / "Cargo.lock"),
                    "cargoManifestSha256": sha256_file(LAB_ROOT / "Cargo.toml"),
                    "toolchainManifestSha256": sha256_file(TOOLCHAINS_PATH),
                    "labSourceTreeSha256": source_tree_sha256(),
                    "upstreamSourceTag": toolchains["crate"]["sourceTag"],
                    "upstreamSourceCommit": toolchains["crate"]["sourceCommit"],
                }
                attestation_path.write_text(
                    json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                if not exact:
                    raise RuntimeError(f"byte-exact roundtrip failed for {stem} at {mismatch_offset}")
                results.append(attestation)

    compiler_identities = {
        (
            result["compiler"]["release"],
            result["compiler"]["commitHash"],
            result["compiler"]["buildTarget"],
        )
        for result in results
    }
    candidate_ids = {result["candidateId"] for result in results}
    matrix = {
        "schemaVersion": 1,
        "lab": "scriber-codec-lab",
        "issue": 18,
        "profile": args.profile,
        "toolchainPin": toolchain,
        "cratePin": toolchains["crate"],
        "durationSeconds": list(DURATIONS_SECONDS),
        "sampleRatesHz": list(SAMPLE_RATES_HZ),
        "iterations": args.iterations,
        "candidateIds": sorted(candidate_ids),
        "compilerIdentities": [list(identity) for identity in sorted(compiler_identities)],
        "allRoundTripsByteExact": all(
            result["independentRoundTrip"]["byteExact"] for result in results
        ),
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
        "results": results,
    }
    matrix_path = output_dir / f"matrix-{args.profile}.json"
    matrix_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(matrix_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
