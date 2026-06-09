from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.runtime.ffmpeg_commands import (  # noqa: E402
    ffprobe_duration_args,
    mp3_encode_pcm_pipe_args,
    mp3_transcode_args,
    pcm_pipe_decode_args,
    webm_opus_transcode_args,
)
from src.runtime.subprocess_utils import hidden_subprocess_kwargs  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_tool(
    name: str,
    *,
    media_tools_dir: Path | None = None,
    explicit: Path | None = None,
    allow_path: bool = True,
) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if media_tools_dir:
        candidates.append(media_tools_dir / name)
        if os.name == "nt" and not name.lower().endswith(".exe"):
            candidates.append(media_tools_dir / f"{name}.exe")
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    if allow_path:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
        if os.name == "nt" and not name.lower().endswith(".exe"):
            found = shutil.which(f"{name}.exe")
            if found:
                return Path(found).resolve()
    return None


def file_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "sizeBytes": path.stat().st_size if path.exists() else 0,
        "suffix": path.suffix.lower(),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_sine_wav(
    path: Path,
    *,
    duration_sec: float,
    sample_rate: int = 16000,
    frequency_hz: float = 440.0,
) -> None:
    frame_count = max(1, int(duration_sec * sample_rate))
    amplitude = 0.25 * 32767
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for index in range(frame_count):
            sample = int(amplitude * math.sin(2.0 * math.pi * frequency_hz * index / sample_rate))
            wav_file.writeframesraw(struct.pack("<h", sample))


def sine_pcm_s16le_bytes(
    *,
    duration_sec: float,
    sample_rate: int = 16000,
    frequency_hz: float = 440.0,
) -> bytes:
    frame_count = max(1, int(duration_sec * sample_rate))
    amplitude = 0.25 * 32767
    chunks = bytearray()
    for index in range(frame_count):
        sample = int(amplitude * math.sin(2.0 * math.pi * frequency_hz * index / sample_rate))
        chunks.extend(struct.pack("<h", sample))
    return bytes(chunks)


def run_command(
    command: list[str],
    *,
    timeout_sec: float,
    input_bytes: bytes | None = None,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        command,
        input=input_bytes,
        stdin=None if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_sec,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    if expect_success and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{stderr[:1000]}")
    if not expect_success and completed.returncode == 0:
        raise RuntimeError(f"command unexpectedly succeeded: {' '.join(command)}")
    return completed


def generate_audio_fixture(
    generator_ffmpeg: Path,
    source_wav: Path,
    target: Path,
    codec_args: list[str],
    *,
    timeout_sec: float,
) -> Path:
    run_command(
        [
            str(generator_ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_wav),
            *codec_args,
            str(target),
        ],
        timeout_sec=timeout_sec,
    )
    assert_media_file(target)
    return target


def generate_video_audio_fixture(
    generator_ffmpeg: Path,
    source_wav: Path,
    target: Path,
    *,
    timeout_sec: float,
) -> tuple[Path, str]:
    attempts = [
        ("libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]),
        ("mpeg4", ["-c:v", "mpeg4"]),
    ]
    last_error = ""
    for codec_name, video_args in attempts:
        try:
            run_command(
                [
                    str(generator_ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=32x32:d=1",
                    "-i",
                    str(source_wav),
                    "-shortest",
                    *video_args,
                    "-c:a",
                    "aac",
                    str(target),
                ],
                timeout_sec=timeout_sec,
            )
            assert_media_file(target)
            return target, codec_name
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            target.unlink(missing_ok=True)
    raise RuntimeError(f"could not generate video/audio fixture: {last_error}")


def generate_no_audio_video(
    generator_ffmpeg: Path,
    target: Path,
    *,
    timeout_sec: float,
) -> tuple[Path, str]:
    attempts = [
        ("libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]),
        ("mpeg4", ["-c:v", "mpeg4"]),
    ]
    last_error = ""
    for codec_name, video_args in attempts:
        try:
            run_command(
                [
                    str(generator_ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=32x32:d=1",
                    *video_args,
                    "-an",
                    str(target),
                ],
                timeout_sec=timeout_sec,
            )
            assert_media_file(target)
            return target, codec_name
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            target.unlink(missing_ok=True)
    raise RuntimeError(f"could not generate no-audio video fixture: {last_error}")


def assert_media_file(path: Path) -> None:
    if not path.exists():
        raise AssertionError(f"missing media file: {path}")
    if path.stat().st_size <= 0:
        raise AssertionError(f"empty media file: {path}")


def check_transcode_to_webm(candidate_ffmpeg: Path, source: Path, target: Path, timeout_sec: float) -> dict[str, Any]:
    run_command(
        webm_opus_transcode_args(str(candidate_ffmpeg), source, target, bitrate="64k"),
        timeout_sec=timeout_sec,
    )
    assert_media_file(target)
    return {"input": file_info(source), "output": file_info(target)}


def check_transcode_to_mp3(candidate_ffmpeg: Path, source: Path, target: Path, timeout_sec: float) -> dict[str, Any]:
    run_command(
        mp3_transcode_args(str(candidate_ffmpeg), source, target, bitrate="64k"),
        timeout_sec=timeout_sec,
    )
    assert_media_file(target)
    return {"input": file_info(source), "output": file_info(target)}


def check_pcm_pipe_to_mp3(candidate_ffmpeg: Path, timeout_sec: float) -> dict[str, Any]:
    input_bytes = sine_pcm_s16le_bytes(duration_sec=1.0)
    completed = run_command(
        mp3_encode_pcm_pipe_args(
            str(candidate_ffmpeg),
            input_sample_rate=16000,
            input_channels=1,
            bitrate="64k",
        ),
        input_bytes=input_bytes,
        timeout_sec=timeout_sec,
    )
    if not completed.stdout:
        raise AssertionError("PCM-to-MP3 pipe produced no stdout.")
    return {
        "inputBytes": len(input_bytes),
        "stdoutBytes": len(completed.stdout),
    }


def check_pcm_pipe(candidate_ffmpeg: Path, source: Path, timeout_sec: float) -> dict[str, Any]:
    completed = run_command(
        pcm_pipe_decode_args(str(candidate_ffmpeg), source),
        timeout_sec=timeout_sec,
    )
    if not completed.stdout:
        raise AssertionError("PCM pipe produced no stdout.")
    return {
        "input": file_info(source),
        "stdoutBytes": len(completed.stdout),
    }


def check_expected_failure(
    candidate_ffmpeg: Path,
    source: Path,
    target: Path,
    timeout_sec: float,
) -> dict[str, Any]:
    completed = run_command(
        webm_opus_transcode_args(str(candidate_ffmpeg), source, target, bitrate="64k"),
        timeout_sec=timeout_sec,
        expect_success=False,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    return {
        "input": file_info(source),
        "returncode": completed.returncode,
        "stderrPreview": stderr[:500],
    }


def check_ffprobe_duration(candidate_ffprobe: Path, source: Path, timeout_sec: float) -> dict[str, Any]:
    completed = run_command(
        ffprobe_duration_args(str(candidate_ffprobe), source),
        timeout_sec=timeout_sec,
    )
    raw = completed.stdout.decode("utf-8", errors="replace").strip()
    duration = float(raw)
    if duration <= 0:
        raise AssertionError(f"ffprobe returned non-positive duration: {raw}")
    return {
        "input": file_info(source),
        "durationSeconds": duration,
    }


def run_check(checks: list[dict[str, Any]], name: str, func) -> None:
    started = time.perf_counter()
    try:
        details = func()
        checks.append(
            {
                "name": name,
                "ok": True,
                "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
                **details,
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": name,
                "ok": False,
                "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


def build_fixtures(
    *,
    generator_ffmpeg: Path,
    work_dir: Path,
    duration_sec: float,
    timeout_sec: float,
) -> tuple[dict[str, Path], dict[str, Any]]:
    source_wav = work_dir / "base-source.wav"
    write_sine_wav(source_wav, duration_sec=duration_sec)
    assert_media_file(source_wav)

    fixtures: dict[str, Path] = {
        "wav_pcm_s16": source_wav,
    }
    metadata: dict[str, Any] = {}

    fixture_specs = {
        "mp3_cbr": ("mp3-cbr.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]),
        "mp3_vbr": ("mp3-vbr.mp3", ["-c:a", "libmp3lame", "-q:a", "4"]),
        "wav_pcm_s24": ("wav-pcm-s24.wav", ["-c:a", "pcm_s24le"]),
        "wav_float": ("wav-float.wav", ["-c:a", "pcm_f32le"]),
        "mov_aac": ("mov-aac.mov", ["-c:a", "aac"]),
        "m4a_alac": ("m4a-alac.m4a", ["-c:a", "alac"]),
        "mp4_aac": ("mp4-aac.mp4", ["-c:a", "aac"]),
        "webm_opus": ("webm-opus.webm", ["-c:a", "libopus", "-b:a", "64k"]),
        "ogg_opus": ("ogg-opus.ogg", ["-c:a", "libopus", "-b:a", "64k"]),
        "flac": ("audio.flac", ["-c:a", "flac"]),
        "yt_dlp_m4a": ("yt-dlp-m4a.m4a", ["-c:a", "aac"]),
        "yt_dlp_webm_opus": ("yt-dlp-webm-opus.webm", ["-c:a", "libopus", "-b:a", "64k"]),
    }
    for name, (filename, codec_args) in fixture_specs.items():
        fixtures[name] = generate_audio_fixture(
            generator_ffmpeg,
            source_wav,
            work_dir / filename,
            codec_args,
            timeout_sec=timeout_sec,
        )

    fixtures["mkv_video_audio"], metadata["mkvVideoCodec"] = generate_video_audio_fixture(
        generator_ffmpeg,
        source_wav,
        work_dir / "mkv-video-audio.mkv",
        timeout_sec=timeout_sec,
    )
    fixtures["yt_dlp_merged_mp4"], metadata["mergedMp4VideoCodec"] = generate_video_audio_fixture(
        generator_ffmpeg,
        source_wav,
        work_dir / "yt-dlp-merged.mp4",
        timeout_sec=timeout_sec,
    )
    fixtures["no_audio_video"], metadata["noAudioVideoCodec"] = generate_no_audio_video(
        generator_ffmpeg,
        work_dir / "no-audio-video.mp4",
        timeout_sec=timeout_sec,
    )

    corrupt = work_dir / "corrupted-input.mp3"
    corrupt.write_bytes(b"not a valid media file")
    fixtures["corrupted_input"] = corrupt

    long_dir = work_dir / ("long-path-" + ("nested-" * 8))
    long_dir.mkdir(parents=True, exist_ok=True)
    unicode_path = long_dir / "deutsche-umlaute-ae-oe-ue-groesse-input.mp3"
    shutil.copyfile(fixtures["mp3_cbr"], unicode_path)
    fixtures["long_unicode_path_mp3"] = unicode_path

    return fixtures, metadata


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    candidate_ffmpeg = resolve_tool("ffmpeg", media_tools_dir=args.media_tools_dir, explicit=args.ffmpeg)
    candidate_ffprobe = resolve_tool("ffprobe", media_tools_dir=args.media_tools_dir, explicit=args.ffprobe)
    generator_ffmpeg = resolve_tool("ffmpeg", explicit=args.fixture_ffmpeg)

    failures: list[str] = []
    if not candidate_ffmpeg:
        failures.append("candidate ffmpeg was not found")
    if args.require_ffprobe and not candidate_ffprobe:
        failures.append("candidate ffprobe was not found")
    if not generator_ffmpeg:
        failures.append("fixture generator ffmpeg was not found")
    if failures:
        return {
            "apiVersion": "1",
            "ok": False,
            "generatedAt": utc_now(),
            "profile": "B",
            "failures": failures,
            "checks": [],
        }

    assert candidate_ffmpeg is not None
    assert generator_ffmpeg is not None

    temp_context: tempfile.TemporaryDirectory[str] | None = None
    if args.work_dir:
        work_dir = args.work_dir.expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="scriber-ffmpeg-profile-b-")
        work_dir = Path(temp_context.name)

    checks: list[dict[str, Any]] = []
    started = time.perf_counter()
    fixture_metadata: dict[str, Any] = {}

    try:
        fixtures, fixture_metadata = build_fixtures(
            generator_ffmpeg=generator_ffmpeg,
            work_dir=work_dir,
            duration_sec=args.duration_sec,
            timeout_sec=args.timeout_sec,
        )

        webm_cases = [
            "mp3_cbr",
            "mp3_vbr",
            "wav_pcm_s16",
            "wav_pcm_s24",
            "wav_float",
            "mov_aac",
            "m4a_alac",
            "mp4_aac",
            "webm_opus",
            "mkv_video_audio",
            "ogg_opus",
            "flac",
            "yt_dlp_m4a",
            "yt_dlp_webm_opus",
            "yt_dlp_merged_mp4",
            "long_unicode_path_mp3",
        ]
        for case_name in webm_cases:
            run_check(
                checks,
                f"{case_name}_to_webm_opus",
                lambda case_name=case_name: check_transcode_to_webm(
                    candidate_ffmpeg,
                    fixtures[case_name],
                    work_dir / f"{case_name}-out.webm",
                    args.timeout_sec,
                ),
            )

        azure_mp3_cases = ["wav_pcm_s16", "flac", "webm_opus", "yt_dlp_m4a", "yt_dlp_merged_mp4"]
        for case_name in azure_mp3_cases:
            run_check(
                checks,
                f"azure_mai_{case_name}_to_mp3",
                lambda case_name=case_name: check_transcode_to_mp3(
                    candidate_ffmpeg,
                    fixtures[case_name],
                    work_dir / f"azure-{case_name}.mp3",
                    args.timeout_sec,
                ),
            )

        run_check(
            checks,
            "webm_opus_to_pcm_pipe",
            lambda: check_pcm_pipe(candidate_ffmpeg, fixtures["webm_opus"], args.timeout_sec),
        )
        run_check(
            checks,
            "raw_pcm_pipe_to_mp3",
            lambda: check_pcm_pipe_to_mp3(candidate_ffmpeg, args.timeout_sec),
        )
        run_check(
            checks,
            "no_audio_video_fails",
            lambda: check_expected_failure(
                candidate_ffmpeg,
                fixtures["no_audio_video"],
                work_dir / "no-audio-video-out.webm",
                args.timeout_sec,
            ),
        )
        run_check(
            checks,
            "corrupted_input_fails",
            lambda: check_expected_failure(
                candidate_ffmpeg,
                fixtures["corrupted_input"],
                work_dir / "corrupted-input-out.webm",
                args.timeout_sec,
            ),
        )

        if candidate_ffprobe:
            run_check(
                checks,
                "ffprobe_duration_mp3",
                lambda: check_ffprobe_duration(candidate_ffprobe, fixtures["mp3_cbr"], args.timeout_sec),
            )

        ok = all(check["ok"] for check in checks)
        return {
            "apiVersion": "1",
            "ok": ok,
            "generatedAt": utc_now(),
            "profile": "B",
            "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
            "workDir": str(work_dir),
            "mediaTools": {
                "mediaToolsDir": str(args.media_tools_dir) if args.media_tools_dir else None,
                "candidateFfmpeg": str(candidate_ffmpeg),
                "candidateFfprobe": str(candidate_ffprobe) if candidate_ffprobe else None,
                "fixtureFfmpeg": str(generator_ffmpeg),
                "requireFfprobe": bool(args.require_ffprobe),
            },
            "fixtureMetadata": fixture_metadata,
            "summary": {
                "totalChecks": len(checks),
                "passedChecks": sum(1 for check in checks if check["ok"]),
                "failedChecks": sum(1 for check in checks if not check["ok"]),
            },
            "checks": checks,
        }
    finally:
        if temp_context and not args.keep_work_dir:
            temp_context.cleanup()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Scriber FFmpeg Profile B fixture matrix.")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "tmp" / "ffmpeg-profile-b-fixtures.json")
    parser.add_argument("--media-tools-dir", type=Path, default=None)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ffprobe", type=Path, default=None)
    parser.add_argument("--fixture-ffmpeg", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--require-ffprobe", action="store_true")
    parser.add_argument("--duration-sec", type=float, default=1.0)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        payload = run_smoke(args)
    except Exception as exc:
        payload = {
            "apiVersion": "1",
            "ok": False,
            "generatedAt": utc_now(),
            "profile": "B",
            "error": f"{type(exc).__name__}: {exc}",
        }
    write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
