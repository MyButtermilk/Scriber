from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path


DEFAULT_TEXT = (
    "Dies ist ein automatischer Scriber Mikrofontest. "
    "Heute prüfen wir Aufnahme, Pause, Fortsetzen und Transkription. "
    "Die eindeutige Testmarke lautet Seestern siebenundvierzig. "
    "Das Meeting funktioniert vollständig."
)
SAMPLE_RATE = 48_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
MAX_FIXTURE_BYTES = 64 * 1024 * 1024


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a deterministic, raw 48 kHz mono PCM fixture with the local "
            "open-source Piper runtime."
        )
    )
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--voice-model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--result-json", type=Path)
    text_group = parser.add_mutually_exclusive_group()
    text_group.add_argument("--text", default=DEFAULT_TEXT)
    text_group.add_argument("--text-file", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--leading-silence-ms", type=int, default=600)
    parser.add_argument("--trailing-silence-ms", type=int, default=1_000)
    return parser.parse_args()


def _require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"{label} is missing or is not a file")
    return resolved


def _require_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeError(f"{label} is missing or is not a directory")
    return resolved


def _resolve_ffmpeg(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    located = shutil.which(value)
    if not located:
        raise RuntimeError("ffmpeg is unavailable")
    return Path(located).resolve()


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> None:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        input=stdin_text,
        text=stdin_text is not None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        executable = Path(command[0]).name
        raise RuntimeError(
            f"{executable} failed while generating the synthetic fixture "
            f"(exit code {completed.returncode})"
        )


def _read_text(args: argparse.Namespace) -> str:
    if args.text_file is not None:
        text_path = _require_file(args.text_file, "text input")
        text = text_path.read_text(encoding="utf-8")
    else:
        text = str(args.text or "")
    text = " ".join(text.split())
    if not text:
        raise RuntimeError("TTS input must not be empty")
    if len(text) > 4_000:
        raise RuntimeError("TTS input exceeds the 4,000 character fixture limit")
    return text


def _silence_bytes(duration_ms: int) -> bytes:
    if duration_ms < 0 or duration_ms > 30_000:
        raise RuntimeError("silence duration must be between 0 and 30,000 ms")
    sample_count = round(SAMPLE_RATE * duration_ms / 1_000)
    return bytes(sample_count * CHANNELS * SAMPLE_WIDTH_BYTES)


def _pcm_statistics(data: bytes) -> tuple[float, float]:
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0, 0.0
    peak = max(abs(int(value)) for value in samples) / 32_768.0
    square_sum = sum(int(value) * int(value) for value in samples)
    rms = math.sqrt(square_sum / len(samples)) / 32_768.0
    return peak, rms


def main() -> int:
    args = _parse_args()
    runtime_dir = _require_directory(args.runtime_dir, "Piper runtime directory")
    _require_file(runtime_dir / "piper" / "__main__.py", "Piper module")
    voice_model = _require_file(args.voice_model, "Piper voice model")
    _require_file(Path(f"{voice_model}.json"), "Piper voice configuration")
    ffmpeg = _resolve_ffmpeg(args.ffmpeg)
    text = _read_text(args)

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    piper_env = os.environ.copy()
    existing_pythonpath = piper_env.get("PYTHONPATH", "")
    piper_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(runtime_dir), existing_pythonpath) if part
    )

    with tempfile.TemporaryDirectory(prefix="scriber-meeting-tts-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        wav_path = temp_dir / "piper.wav"
        pcm_path = temp_dir / "resampled.pcm"
        _run_checked(
            [
                sys.executable,
                "-m",
                "piper",
                "--model",
                str(voice_model),
                "--output-file",
                str(wav_path),
                "--noise-scale",
                "0",
                "--noise-w-scale",
                "0",
                "--length-scale",
                "1",
            ],
            cwd=temp_dir,
            env=piper_env,
            stdin_text=text,
        )
        _require_file(wav_path, "Piper WAV output")
        _run_checked(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(wav_path),
                "-ac",
                str(CHANNELS),
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                str(pcm_path),
            ],
            cwd=temp_dir,
        )
        resampled = _require_file(pcm_path, "resampled PCM output").read_bytes()

    payload = (
        _silence_bytes(args.leading_silence_ms)
        + resampled
        + _silence_bytes(args.trailing_silence_ms)
    )
    if not payload or len(payload) % (CHANNELS * SAMPLE_WIDTH_BYTES) != 0:
        raise RuntimeError("generated fixture is empty or sample-unaligned")
    if len(payload) > MAX_FIXTURE_BYTES:
        raise RuntimeError("generated fixture exceeds the 64 MiB sidecar limit")

    temporary_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary_output.write_bytes(payload)
    os.replace(temporary_output, output)

    duration_ms = round(
        len(payload) * 1_000 / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES)
    )
    peak, rms = _pcm_statistics(payload)
    result = {
        "schemaVersion": 1,
        "engine": "piper",
        "voice": voice_model.stem,
        "format": "pcm_s16le_48000_mono",
        "sampleRate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sampleWidthBytes": SAMPLE_WIDTH_BYTES,
        "durationMs": duration_ms,
        "byteLength": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
    }
    if result["rms"] <= 0.0001:
        raise RuntimeError("generated fixture contains no meaningful signal")

    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True)
    if args.result_json is not None:
        result_path = args.result_json.expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_result = result_path.with_name(
            f".{result_path.name}.{os.getpid()}.tmp"
        )
        temporary_result.write_text(encoded + "\n", encoding="utf-8")
        os.replace(temporary_result, result_path)
    print(encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
