"""Offline helpers for the isolated Scriber Issue #18 lossy-codec lab."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import struct
import subprocess
import sys
import time
import tomllib
from typing import Any, Iterable


LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAINS_PATH = LAB_ROOT / "toolchains.json"
ALLOWED_SAMPLE_RATES_HZ = (16_000, 48_000)
ALLOWED_DURATIONS_SECONDS = (5, 15, 30, 60)
CANDIDATES = (
    "mp3-lame",
    "mp3-ffmpeg",
    "opus-ruopus",
    "opus-libopus",
)
RUST_CANDIDATES = {"mp3-lame", "opus-ruopus"}
EXTENSIONS = {
    "mp3-lame": ".mp3",
    "mp3-ffmpeg": ".mp3",
    "opus-ruopus": ".opus",
    "opus-libopus": ".opus",
}
CANDIDATE_IDS = {
    "mp3-lame": "mp3_lame_in_process_v1",
    "mp3-ffmpeg": "mp3_ffmpeg_libmp3lame_current_control_v1",
    "opus-ruopus": "opus_ruopus_ogg_v1",
    "opus-libopus": "opus_ffmpeg_libopus_reference_control_v1",
}


def run_checked(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=LAB_ROOT,
        env=env,
        input=input_bytes,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_sha256() -> str:
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in LAB_ROOT.rglob("*")
        if path.is_file()
        and not any(part in {"artifacts", "target", "__pycache__"} for part in path.parts)
    )
    for path in paths:
        relative = path.relative_to(LAB_ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_matrix_value(sample_rate_hz: int, duration_seconds: int) -> None:
    if sample_rate_hz not in ALLOWED_SAMPLE_RATES_HZ:
        raise ValueError("sample rate must be exactly 16000 or 48000 Hz")
    if duration_seconds not in ALLOWED_DURATIONS_SECONDS:
        raise ValueError("duration must be exactly 5, 15, 30, or 60 seconds")


def deterministic_sample(index: int, sample_rate_hz: int) -> int:
    """Integer-only, speech-like fixture with an unambiguous alignment trace."""
    phase_a = (index * 197 * 65_536 // sample_rate_hz) & 0xFFFF
    phase_b = (index * 431 * 65_536 // sample_rate_hz) & 0xFFFF
    phase_c = (index * 733 * 65_536 // sample_rate_hz) & 0xFFFF
    triangle_a = 32_767 - abs(phase_a - 32_768) * 2
    triangle_b = 32_767 - abs(phase_b - 32_768) * 2
    triangle_c = 32_767 - abs(phase_c - 32_768) * 2
    envelope_slot = (index * 11 // sample_rate_hz) % 5
    envelope = (10, 7, 9, 5, 8)[envelope_slot]
    value = envelope * (triangle_a * 8 + triangle_b * 5 + triangle_c * 3) // 160
    return max(-32_768, min(32_767, value))


def write_fixture(path: Path, sample_rate_hz: int, duration_seconds: int) -> None:
    validate_matrix_value(sample_rate_hz, duration_seconds)
    expected_bytes = sample_rate_hz * duration_seconds * 2
    if path.is_file() and path.stat().st_size == expected_bytes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        buffer = bytearray()
        for index in range(sample_rate_hz * duration_seconds):
            buffer += struct.pack("<h", deterministic_sample(index, sample_rate_hz))
            if len(buffer) >= 1024 * 1024:
                stream.write(buffer)
                buffer.clear()
        stream.write(buffer)
    if path.stat().st_size != expected_bytes:
        raise RuntimeError("fixture writer produced an invalid byte count")


def pcm16_samples(path: Path) -> array[int]:
    values: array[int] = array("h")
    values.frombytes(path.read_bytes())
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _normalized_correlation(
    reference: array[int],
    decoded: array[int],
    lag: int,
    *,
    span: int,
    stride: int,
) -> float:
    ref_start = max(0, -lag)
    dec_start = max(0, lag)
    available = min(len(reference) - ref_start, len(decoded) - dec_start, span)
    if available <= stride * 16:
        return -1.0
    dot = 0.0
    ref_energy = 0.0
    dec_energy = 0.0
    for offset in range(0, available, stride):
        left = float(reference[ref_start + offset])
        right = float(decoded[dec_start + offset])
        dot += left * right
        ref_energy += left * left
        dec_energy += right * right
    if ref_energy <= 0.0 or dec_energy <= 0.0:
        return -1.0
    return dot / math.sqrt(ref_energy * dec_energy)


def find_alignment_lag(reference: array[int], decoded: array[int], sample_rate_hz: int) -> tuple[int, float]:
    max_lag = sample_rate_hz * 120 // 1000
    stride = max(1, sample_rate_hz // 4_000)
    span = min(len(reference), sample_rate_hz // 4)
    coarse_step = max(1, sample_rate_hz // 6_000)
    best_lag = 0
    best_correlation = -2.0
    for lag in range(-max_lag, max_lag + 1, coarse_step):
        correlation = _normalized_correlation(
            reference, decoded, lag, span=span, stride=stride
        )
        if correlation > best_correlation:
            best_lag = lag
            best_correlation = correlation
    low = max(-max_lag, best_lag - coarse_step)
    high = min(max_lag, best_lag + coarse_step)
    for lag in range(low, high + 1):
        correlation = _normalized_correlation(
            reference, decoded, lag, span=span, stride=stride
        )
        if correlation > best_correlation:
            best_lag = lag
            best_correlation = correlation
    return best_lag, best_correlation


def quality_metrics(reference_path: Path, decoded_path: Path, sample_rate_hz: int) -> dict[str, Any]:
    reference = pcm16_samples(reference_path)
    decoded = pcm16_samples(decoded_path)
    lag, alignment_correlation = find_alignment_lag(reference, decoded, sample_rate_hz)
    ref_start = max(0, -lag)
    dec_start = max(0, lag)
    overlap = min(len(reference) - ref_start, len(decoded) - dec_start)
    quality_stride = max(1, sample_rate_hz // 16_000)
    dot = 0.0
    ref_energy = 0.0
    dec_energy = 0.0
    raw_noise = 0.0
    for offset in range(0, overlap, quality_stride):
        left = float(reference[ref_start + offset])
        right = float(decoded[dec_start + offset])
        dot += left * right
        ref_energy += left * left
        dec_energy += right * right
        delta = right - left
        raw_noise += delta * delta
    if ref_energy <= 0.0 or dec_energy <= 0.0 or overlap <= 0:
        raise RuntimeError("quality window contains no measurable signal")
    scale = dot / ref_energy
    scaled_noise = 0.0
    for offset in range(0, overlap, quality_stride):
        left = float(reference[ref_start + offset])
        right = float(decoded[dec_start + offset])
        delta = right - scale * left
        scaled_noise += delta * delta
    raw_snr = 120.0 if raw_noise == 0.0 else 10.0 * math.log10(ref_energy / raw_noise)
    si_snr = 120.0 if scaled_noise == 0.0 else 10.0 * math.log10((scale * scale * ref_energy) / scaled_noise)
    normalized_correlation = dot / math.sqrt(ref_energy * dec_energy)
    decoded_tail = len(decoded) - dec_start - overlap
    source_tail_uncovered = len(reference) - ref_start - overlap
    duration_error = len(decoded) - len(reference)
    max_tail = sample_rate_hz * 200 // 1000
    quality_pass = (
        alignment_correlation >= 0.70
        and normalized_correlation >= 0.70
        and si_snr >= 3.0
    )
    tail_pass = (
        abs(duration_error) <= max_tail
        and abs(lag) <= sample_rate_hz * 120 // 1000
        and decoded_tail <= max_tail
        and source_tail_uncovered <= max_tail
    )
    return {
        "referenceSampleCount": len(reference),
        "decodedSampleCount": len(decoded),
        "alignmentLagSamples": lag,
        "alignmentLagMs": lag * 1000.0 / sample_rate_hz,
        "alignmentCorrelation": alignment_correlation,
        "alignedOverlapSamples": overlap,
        "decodedTailSamples": decoded_tail,
        "sourceTailUncoveredSamples": source_tail_uncovered,
        "durationErrorSamples": duration_error,
        "durationErrorMs": duration_error * 1000.0 / sample_rate_hz,
        "normalizedCorrelation": normalized_correlation,
        "rawSnrDb": raw_snr,
        "scaleInvariantSnrDb": si_snr,
        "leastSquaresGain": scale,
        "rmsRatio": math.sqrt(dec_energy / ref_energy),
        "qualityThreshold": {
            "minimumAlignmentCorrelation": 0.70,
            "minimumNormalizedCorrelation": 0.70,
            "minimumScaleInvariantSnrDb": 3.0,
        },
        "tailThreshold": {
            "maximumAbsoluteDurationErrorMs": 200,
            "maximumAbsoluteAlignmentLagMs": 120,
            "maximumTailMs": 200,
        },
        "qualityPass": quality_pass,
        "tailPass": tail_pass,
    }


def _decode_text(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


def _binary_path(target_dir: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return target_dir / "release" / f"scriber-lossy-codec-lab{suffix}"


def _dependency_lock() -> list[dict[str, Any]]:
    lock = tomllib.loads((LAB_ROOT / "Cargo.lock").read_text(encoding="utf-8"))
    return [
        {
            key: package[key]
            for key in ("name", "version", "source", "checksum")
            if key in package
        }
        for package in lock["package"]
    ]


@dataclass(frozen=True)
class Tooling:
    binary: Path
    ffmpeg: Path
    ffprobe: Path
    identity: dict[str, Any]
    system: dict[str, Any]
    dependency_lock: list[dict[str, Any]]
    shine_status: dict[str, Any]


def prepare_tooling(
    *,
    ffmpeg_arg: str | None = None,
    ffprobe_arg: str | None = None,
    skip_build: bool = False,
) -> Tooling:
    manifest = json.loads(TOOLCHAINS_PATH.read_text(encoding="utf-8"))
    target_dir = (LAB_ROOT / "target" / "stable").resolve()
    env_copy = os.environ.copy()
    env_copy["CARGO_TARGET_DIR"] = str(target_dir)
    env_copy["RUSTFLAGS"] = ""
    if not skip_build:
        run_checked(["cargo", "build", "--release", "--locked", "--offline"], env=env_copy)
    binary = _binary_path(target_dir)
    if not binary.is_file():
        raise RuntimeError(f"lab binary is missing: {binary}")

    ffmpeg_name = ffmpeg_arg or shutil.which("ffmpeg")
    ffprobe_name = ffprobe_arg or shutil.which("ffprobe")
    if not ffmpeg_name or not ffprobe_name:
        raise RuntimeError("ffmpeg and ffprobe must both be available")
    ffmpeg = Path(ffmpeg_name).resolve()
    ffprobe = Path(ffprobe_name).resolve()
    ffmpeg_version_text = _decode_text(run_checked([str(ffmpeg), "-version"]).stdout)
    ffprobe_version_text = _decode_text(run_checked([str(ffprobe), "-version"]).stdout)
    ffmpeg_first_line = ffmpeg_version_text.splitlines()[0]
    version_match = re.search(r"ffmpeg version\s+(\d+)", ffmpeg_first_line)
    if not version_match or int(version_match.group(1)) < manifest["externalControls"]["ffmpeg"]["minimumMajorVersion"]:
        raise RuntimeError("ffmpeg is older than the pinned minimum major version")
    encoders_text = _decode_text(run_checked([str(ffmpeg), "-hide_banner", "-encoders"]).stdout)
    for encoder in manifest["externalControls"]["ffmpeg"]["requiredEncoders"]:
        if not re.search(rf"\b{re.escape(encoder)}\b", encoders_text):
            raise RuntimeError(f"required ffmpeg encoder is missing: {encoder}")

    rustc = _decode_text(run_checked(["rustc", "--version", "--verbose"]).stdout)
    if f"release: {manifest['rust']['release']}" not in rustc:
        raise RuntimeError("active Rust release does not match toolchains.json")
    if f"commit-hash: {manifest['rust']['commit']}" not in rustc:
        raise RuntimeError("active Rust commit does not match toolchains.json")
    if f"host: {manifest['rust']['target']}" not in rustc:
        raise RuntimeError("active Rust host does not match toolchains.json")
    system = json.loads(_decode_text(run_checked([str(binary), "--system-json"]).stdout))
    identity = {
        "rustcVerbose": rustc.strip().splitlines(),
        "cargoVersion": _decode_text(run_checked(["cargo", "--version"]).stdout).strip(),
        "pythonVersion": platform.python_version(),
        "pythonImplementation": platform.python_implementation(),
        "candidateBinarySha256": sha256_file(binary),
        "ffmpegVersion": ffmpeg_first_line,
        "ffmpegExecutableSha256": sha256_file(ffmpeg),
        "ffprobeVersion": ffprobe_version_text.splitlines()[0],
        "ffprobeExecutableSha256": sha256_file(ffprobe),
        "ffmpegConfiguration": next(
            (line.removeprefix("configuration: ") for line in ffmpeg_version_text.splitlines() if line.startswith("configuration:")),
            "unknown",
        ),
        "cargoLockSha256": sha256_file(LAB_ROOT / "Cargo.lock"),
        "cargoManifestSha256": sha256_file(LAB_ROOT / "Cargo.toml"),
        "toolchainManifestSha256": sha256_file(TOOLCHAINS_PATH),
        "rustToolchainSha256": sha256_file(LAB_ROOT / "rust-toolchain.toml"),
        "labSourceTreeSha256": source_tree_sha256(),
    }
    shine_advertised = bool(re.search(r"\blibshine\b", encoders_text))
    shine_status = {
        "schemaVersion": 1,
        "candidateId": "shine_rs_or_shine_in_process_challenger",
        "status": "fail_closed_unavailable",
        "benchmarkEligible": False,
        "attempted": False,
        "windowsReproducibleBuildPinned": False,
        "ffmpegLibshineAdvertised": shine_advertised,
        "reasons": [
            "no reviewed and locked Shine-RS or Shine MSVC in-process build is present in this lab",
            "the external ffmpeg libshine encoder, when advertised, is not an in-process Rust challenger",
            "an external encoder advertisement is not proof of fixed-matrix 16 kHz output support",
        ],
        "licenseReview": "no Shine source is included; a future pin requires a separate source and redistribution review",
        "productionReady": False,
        "productionIntegrated": False,
        "productionPromoted": False,
    }
    return Tooling(
        binary=binary,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        identity=identity,
        system=system,
        dependency_lock=_dependency_lock(),
        shine_status=shine_status,
    )


def _external_encode(
    candidate: str,
    fixture: Path,
    sample_rate_hz: int,
    tooling: Tooling,
) -> tuple[bytes, dict[str, Any]]:
    pcm = fixture.read_bytes()
    common = [
        str(tooling.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate_hz),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-vn",
        "-map",
        "0:a:0",
    ]
    if candidate == "mp3-ffmpeg":
        codec_args = [
            "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "16000", "-ac", "1", "-f", "mp3", "pipe:1"
        ]
        configuration = {
            "bitrateBps": 64_000,
            "sourceSampleRateHz": sample_rate_hz,
            "encodedOutputSampleRateHz": 16_000,
            "channels": 1,
            "sampleFormat": "signed_16_le",
            "implementation": "current Scriber ffmpeg libmp3lame control",
            "processBoundary": "stdin/stdout pipes",
            "commandContract": "mp3_encode_pcm_pipe_args equivalent",
        }
    elif candidate == "opus-libopus":
        codec_args = [
            "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "1",
            "-application", "voip", "-frame_duration", "20", "-f", "opus", "pipe:1"
        ]
        configuration = {
            "bitrateBps": 64_000,
            "sourceSampleRateHz": sample_rate_hz,
            "encodedOutputSampleRateHz": 48_000,
            "channels": 1,
            "sampleFormat": "signed_16_le",
            "frameDurationMs": 20,
            "implementation": "FFmpeg libopus reference control",
            "libopusencUsed": False,
            "referenceControlSelection": "libopus through FFmpeg selected instead of an unpinned native libopusenc build",
            "processBoundary": "stdin/stdout pipes",
        }
    else:
        raise ValueError(f"not an external control: {candidate}")
    started = time.perf_counter_ns()
    completed = run_checked(common + codec_args, input_bytes=pcm)
    elapsed = time.perf_counter_ns() - started
    if not completed.stdout:
        raise RuntimeError(f"{candidate} produced empty output")
    return completed.stdout, {"runnerWallNanos": elapsed, "configuration": configuration}


def _probe_artifact(path: Path, candidate: str, duration_seconds: int, tooling: Tooling) -> dict[str, Any]:
    completed = run_checked(
        [
            str(tooling.ffprobe),
            "-v", "error", "-count_packets",
            "-show_entries", "format=format_name,duration:stream=codec_name,codec_type,sample_rate,channels,channel_layout,duration,nb_read_packets",
            "-of", "json", str(path),
        ]
    )
    probe = json.loads(_decode_text(completed.stdout))
    streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(streams) != 1:
        raise RuntimeError(f"{candidate} does not contain exactly one audio stream")
    stream = streams[0]
    expected_codec = "mp3" if candidate.startswith("mp3-") else "opus"
    expected_rate = 16_000 if candidate.startswith("mp3-") else 48_000
    format_name = str(probe.get("format", {}).get("format_name", ""))
    format_pass = "mp3" in format_name if expected_codec == "mp3" else "ogg" in format_name
    codec_pass = stream.get("codec_name") == expected_codec
    channel_pass = int(stream.get("channels", 0)) == 1
    rate_pass = int(stream.get("sample_rate", 0)) == expected_rate
    duration_value = stream.get("duration") or probe.get("format", {}).get("duration")
    duration = float(duration_value) if duration_value not in (None, "N/A") else None
    duration_error_ms = None if duration is None else (duration - duration_seconds) * 1000.0
    duration_pass = duration is not None and abs(duration_error_ms) <= 200.0
    packet_value = stream.get("nb_read_packets")
    packet_count = int(packet_value) if packet_value not in (None, "N/A") else None
    if expected_codec == "opus":
        expected_packets = duration_seconds * 50
        packet_pass = packet_count is not None and abs(packet_count - expected_packets) <= 2
        packet_bounds = [expected_packets - 2, expected_packets + 2]
    else:
        # MPEG-2 Layer III at 16 kHz carries 576 samples per audio frame. Both
        # locked LAME routes add exactly two delay/padding frames. Keeping this
        # exact catches an accidental insertion (rather than replacement) of
        # the LAME/Xing metadata frame.
        expected_packets = math.ceil(duration_seconds * 16_000 / 576) + 2
        packet_pass = packet_count == expected_packets
        packet_bounds = [expected_packets, expected_packets]
    return {
        "raw": probe,
        "expectedCodec": expected_codec,
        "expectedSampleRateHz": expected_rate,
        "expectedChannels": 1,
        "packetCount": packet_count,
        "packetCountBounds": packet_bounds,
        "durationSeconds": duration,
        "durationErrorMs": duration_error_ms,
        "formatPass": format_pass,
        "codecPass": codec_pass,
        "channelPass": channel_pass,
        "sampleRatePass": rate_pass,
        "packetCountPass": packet_pass,
        "durationPass": duration_pass,
        "structurePass": all((format_pass, codec_pass, channel_pass, rate_pass, packet_pass, duration_pass)),
    }


def run_candidate(
    *,
    candidate: str,
    fixture: Path,
    output_dir: Path,
    sample_rate_hz: int,
    duration_seconds: int,
    iteration: int,
    tooling: Tooling,
    sequence: int | None = None,
) -> dict[str, Any]:
    if candidate not in CANDIDATES:
        raise ValueError(f"unsupported candidate: {candidate}")
    validate_matrix_value(sample_rate_hz, duration_seconds)
    if iteration < 1:
        raise ValueError("iteration must be at least one")
    stem = f"{candidate}_{sample_rate_hz}hz_{duration_seconds}s_i{iteration}"
    if sequence is not None:
        stem = f"{sequence:03d}_{stem}"
    encoded = output_dir / "encoded" / f"{stem}{EXTENSIONS[candidate]}"
    decoded = output_dir / "decoded" / f"{stem}.pcm"
    attestation_path = output_dir / "attestations" / f"{stem}.json"
    encoded.parent.mkdir(parents=True, exist_ok=True)
    decoded.parent.mkdir(parents=True, exist_ok=True)
    attestation_path.parent.mkdir(parents=True, exist_ok=True)

    if candidate in RUST_CANDIDATES:
        rust_candidate = "mp3-lame" if candidate == "mp3-lame" else "opus-ruopus"
        started = time.perf_counter_ns()
        run_checked(
            [
                str(tooling.binary),
                "--candidate", rust_candidate,
                "--input-pcm", str(fixture),
                "--output", str(encoded),
                "--attestation", str(attestation_path),
                "--sample-rate-hz", str(sample_rate_hz),
                "--duration-seconds", str(duration_seconds),
                "--iteration", str(iteration),
            ]
        )
        runner_wall_nanos = time.perf_counter_ns() - started
        result = json.loads(attestation_path.read_text(encoding="utf-8"))
        result["timing"]["runnerWallNanos"] = runner_wall_nanos
    else:
        output, details = _external_encode(candidate, fixture, sample_rate_hz, tooling)
        encoded.write_bytes(output)
        result = {
            "schemaVersion": 1,
            "lab": "scriber-lossy-codec-lab",
            "issue": 18,
            "candidateId": CANDIDATE_IDS[candidate],
            "codec": "mp3" if candidate.startswith("mp3-") else "opus",
            "container": "mp3" if candidate.startswith("mp3-") else "ogg",
            "sampleRateHz": sample_rate_hz,
            "durationSeconds": duration_seconds,
            "iteration": iteration,
            "expectedMonoSamples": sample_rate_hz * duration_seconds,
            "input": {"sha256": sha256_file(fixture), "byteCount": fixture.stat().st_size},
            "output": {"sha256": sha256_file(encoded), "byteCount": encoded.stat().st_size},
            "timing": {"runnerWallNanos": details["runnerWallNanos"]},
            "configuration": details["configuration"],
            "compiler": None,
            "cpu": tooling.system["cpu"],
            "productionReady": False,
            "productionIntegrated": False,
            "productionPromoted": False,
        }

    probe = _probe_artifact(encoded, candidate, duration_seconds, tooling)
    run_checked(
        [
            str(tooling.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(encoded), "-vn", "-map", "0:a:0", "-ar", str(sample_rate_hz),
            "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", str(decoded),
        ]
    )
    quality = quality_metrics(fixture, decoded, sample_rate_hz)
    result["sequence"] = sequence
    result["containerProbe"] = probe
    result["independentDecode"] = {
        "decoder": "ffmpeg",
        "decoderVersion": tooling.identity["ffmpegVersion"],
        "decoderExecutableSha256": tooling.identity["ffmpegExecutableSha256"],
        "decodedSha256": sha256_file(decoded),
        "decodedByteCount": decoded.stat().st_size,
        "lossyByteEqualityClaimed": False,
        "encoderIndependentForRustCandidates": candidate in RUST_CANDIDATES,
    }
    result["quality"] = quality
    result["reproducibility"] = {
        **tooling.identity,
        "dependencyLock": tooling.dependency_lock,
    }
    result["validationPass"] = bool(
        probe["structurePass"] and quality["qualityPass"] and quality["tailPass"]
    )
    attestation_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not result["validationPass"]:
        raise RuntimeError(
            f"validation failed for {candidate} {sample_rate_hz} Hz {duration_seconds} s"
        )
    return result


def percentile_nearest_rank(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires values")
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize_series(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for result in results:
        key = (
            result["candidateId"],
            int(result["sampleRateHz"]),
            int(result["durationSeconds"]),
        )
        groups.setdefault(key, []).append(result)
    summaries = []
    for (candidate_id, sample_rate_hz, duration_seconds), rows in sorted(groups.items()):
        wall_ms = [row["timing"]["runnerWallNanos"] / 1_000_000.0 for row in rows]
        core_ms = [
            row["timing"]["codecAndContainerNanos"] / 1_000_000.0
            for row in rows
            if "codecAndContainerNanos" in row["timing"]
        ]
        summaries.append(
            {
                "candidateId": candidate_id,
                "sampleRateHz": sample_rate_hz,
                "durationSeconds": duration_seconds,
                "sampleCount": len(rows),
                "runnerWallMsP50": statistics.median(wall_ms),
                "runnerWallMsP95NearestRank": percentile_nearest_rank(wall_ms, 0.95),
                "codecAndContainerMsP50": statistics.median(core_ms) if core_ms else None,
                "minimumScaleInvariantSnrDb": min(
                    row["quality"]["scaleInvariantSnrDb"] for row in rows
                ),
                "maximumAbsoluteDurationErrorMs": max(
                    abs(row["quality"]["durationErrorMs"]) for row in rows
                ),
                "allValidationPassed": all(row["validationPass"] for row in rows),
            }
        )
    return summaries
