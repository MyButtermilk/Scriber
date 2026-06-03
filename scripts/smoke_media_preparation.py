from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


StepFunc = Callable[[], Awaitable[dict[str, Any]]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_text(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path))


def _file_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "sizeBytes": path.stat().st_size if path.exists() else 0,
        "suffix": path.suffix.lower(),
    }


def _write_sine_wav(
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _assert_media_output(path: Path, *, suffix: str | None = None) -> None:
    if not path.exists():
        raise AssertionError(f"Expected media output does not exist: {path}")
    if path.stat().st_size <= 0:
        raise AssertionError(f"Expected media output is empty: {path}")
    if suffix and path.suffix.lower() != suffix:
        raise AssertionError(f"Expected {suffix} output, got {path.suffix}: {path}")


async def _run_step(checks: list[dict[str, Any]], name: str, func: StepFunc) -> None:
    started = time.perf_counter()
    try:
        details = await func()
    except Exception as exc:
        checks.append(
            {
                "name": name,
                "ok": False,
                "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return

    checks.append(
        {
            "name": name,
            "ok": True,
            "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
            **details,
        }
    )


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.media_tools_dir:
        os.environ["SCRIBER_MEDIA_TOOLS_DIR"] = str(Path(args.media_tools_dir).expanduser().resolve())

    from src.azure_mai_stt import azure_mai_content_type, prepared_azure_mai_audio_file
    from src.runtime.media_tools import find_media_tool, require_media_tool
    from src.web_api import (
        _extract_audio_from_video,
        _maybe_compress_audio_upload,
        _probe_media_duration_seconds,
    )
    from src.youtube_download import _ensure_audio_only_file

    ffmpeg = require_media_tool("ffmpeg")
    ffprobe = find_media_tool("ffprobe")
    if args.require_ffprobe and not ffprobe:
        raise RuntimeError("ffprobe is required for this smoke but was not resolved.")

    temp_context: tempfile.TemporaryDirectory[str] | None = None
    if args.work_dir:
        work_dir = Path(args.work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="scriber-media-smoke-")
        work_dir = Path(temp_context.name)

    checks: list[dict[str, Any]] = []
    started = time.perf_counter()

    try:
        source_wav = work_dir / "source.wav"
        _write_sine_wav(source_wav, duration_sec=args.duration_sec)
        _assert_media_output(source_wav, suffix=".wav")

        async def file_upload_compression() -> dict[str, Any]:
            upload_path = work_dir / "file-upload.wav"
            shutil.copyfile(source_wav, upload_path)
            original_size = upload_path.stat().st_size
            compressed_path = await _maybe_compress_audio_upload(
                upload_path,
                max_bytes=args.force_compression_max_bytes,
            )
            _assert_media_output(compressed_path, suffix=".webm")
            if compressed_path.stat().st_size >= original_size:
                raise AssertionError("Compressed upload is not smaller than the source WAV.")
            return {
                "sourceSizeBytes": original_size,
                "output": _file_info(compressed_path),
            }

        async def video_upload_extraction() -> dict[str, Any]:
            uploaded_media = work_dir / "uploaded-media.wav"
            shutil.copyfile(source_wav, uploaded_media)
            extracted_path = await _extract_audio_from_video(uploaded_media, work_dir)
            _assert_media_output(extracted_path, suffix=".webm")
            return {
                "input": _file_info(uploaded_media),
                "output": _file_info(extracted_path),
            }

        async def youtube_post_download_normalization() -> dict[str, Any]:
            downloaded = work_dir / "downloaded-audio.wav"
            shutil.copyfile(source_wav, downloaded)
            normalized_path = await _ensure_audio_only_file(downloaded)
            _assert_media_output(normalized_path, suffix=".webm")
            return {
                "inputRemoved": not downloaded.exists(),
                "output": _file_info(normalized_path),
            }

        async def azure_mai_preparation() -> dict[str, Any]:
            source_webm = work_dir / "azure-source.webm"
            shutil.copyfile(source_wav, work_dir / "azure-source.wav")
            source_webm = await _extract_audio_from_video(work_dir / "azure-source.wav", work_dir)
            _assert_media_output(source_webm, suffix=".webm")
            prepared_info: dict[str, Any]
            async with prepared_azure_mai_audio_file(source_webm) as prepared_path:
                _assert_media_output(prepared_path, suffix=".mp3")
                prepared_info = {
                    "path": str(prepared_path),
                    "suffix": prepared_path.suffix.lower(),
                    "sizeBytes": prepared_path.stat().st_size,
                    "contentType": azure_mai_content_type(prepared_path),
                }
            return {
                "input": _file_info(source_webm),
                "prepared": prepared_info,
                "temporaryFileCleaned": not Path(prepared_info["path"]).exists(),
            }

        async def ffprobe_duration_probe() -> dict[str, Any]:
            duration = await asyncio.to_thread(_probe_media_duration_seconds, source_wav)
            if args.require_ffprobe and duration is None:
                raise AssertionError("ffprobe duration probe returned no duration.")
            return {
                "ffprobeAvailable": bool(ffprobe),
                "durationSeconds": duration,
            }

        await _run_step(checks, "file_upload_compression", file_upload_compression)
        await _run_step(checks, "video_upload_audio_extraction", video_upload_extraction)
        await _run_step(checks, "youtube_post_download_normalization", youtube_post_download_normalization)
        await _run_step(checks, "azure_mai_audio_preparation", azure_mai_preparation)
        await _run_step(checks, "ffprobe_duration_probe", ffprobe_duration_probe)

        ok = all(check["ok"] for check in checks)
        return {
            "apiVersion": "1",
            "ok": ok,
            "generatedAt": _utc_now(),
            "durationMs": round((time.perf_counter() - started) * 1000.0, 3),
            "workDir": str(work_dir),
            "mediaTools": {
                "mediaToolsDir": _path_text(args.media_tools_dir),
                "ffmpeg": ffmpeg,
                "ffprobe": ffprobe,
                "requireFfprobe": bool(args.require_ffprobe),
            },
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
    parser = argparse.ArgumentParser(
        description=(
            "Smoke-test Scriber media preparation helpers with the resolved ffmpeg "
            "and optional ffprobe binaries."
        )
    )
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "tmp" / "media-preparation-smoke.json")
    parser.add_argument("--media-tools-dir", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--require-ffprobe", action="store_true")
    parser.add_argument("--duration-sec", type=float, default=4.0)
    parser.add_argument("--force-compression-max-bytes", type=int, default=4096)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        payload = asyncio.run(run_smoke(args))
    except Exception as exc:
        payload = {
            "apiVersion": "1",
            "ok": False,
            "generatedAt": _utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_json(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    _write_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
