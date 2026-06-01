from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class SyntheticUploadField:
    def __init__(self, total_bytes: int, pattern: bytes = b"scriber-upload-baseline\n") -> None:
        self.remaining = max(0, int(total_bytes))
        self.pattern = pattern or b"x"

    async def read_chunk(self, *, size: int) -> bytes:
        if self.remaining <= 0:
            return b""
        chunk_size = min(max(1, int(size)), self.remaining)
        self.remaining -= chunk_size
        repeats, remainder = divmod(chunk_size, len(self.pattern))
        return (self.pattern * repeats) + self.pattern[:remainder]


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(((pct / 100.0) * len(ordered) + 0.999999) - 1)))
    return float(ordered[idx])


def summarize_durations(durations_ms: list[float]) -> dict[str, float | int]:
    if not durations_ms:
        return {
            "count": 0,
            "totalMs": 0.0,
            "meanMs": 0.0,
            "p50Ms": 0.0,
            "p95Ms": 0.0,
            "maxMs": 0.0,
        }
    return {
        "count": len(durations_ms),
        "totalMs": round(sum(durations_ms), 3),
        "meanMs": round(statistics.fmean(durations_ms), 4),
        "p50Ms": round(percentile(durations_ms, 50.0), 4),
        "p95Ms": round(percentile(durations_ms, 95.0), 4),
        "maxMs": round(max(durations_ms), 4),
    }


async def measure_upload_streams(
    temp_dir: Path,
    *,
    file_count: int,
    file_size_bytes: int,
    chunk_size_bytes: int,
) -> dict[str, Any]:
    from src.web_api import _write_upload_stream_to_disk

    async def run_one(index: int) -> dict[str, Any]:
        field = SyntheticUploadField(file_size_bytes)
        target = temp_dir / f"upload-{index}.bin"
        started = time.perf_counter_ns()
        bytes_read, too_large = await _write_upload_stream_to_disk(
            field,
            target,
            max_bytes=file_size_bytes + chunk_size_bytes,
            chunk_size=chunk_size_bytes,
        )
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        written_bytes = target.stat().st_size if target.exists() else 0
        return {
            "index": index,
            "durationMs": round(duration_ms, 4),
            "bytesRead": bytes_read,
            "writtenBytes": written_bytes,
            "tooLarge": too_large,
            "ok": bytes_read == file_size_bytes and written_bytes == file_size_bytes and not too_large,
        }

    started = time.perf_counter_ns()
    results = await asyncio.gather(*(run_one(index) for index in range(file_count)))
    total_ms = (time.perf_counter_ns() - started) / 1_000_000
    durations = [float(result["durationMs"]) for result in results]
    total_bytes = sum(int(result["writtenBytes"]) for result in results)
    throughput_mbps = (total_bytes / (1024 * 1024)) / (total_ms / 1000.0) if total_ms else 0.0
    return {
        "fileCount": file_count,
        "fileSizeBytes": file_size_bytes,
        "chunkSizeBytes": chunk_size_bytes,
        "totalBytes": total_bytes,
        "totalMs": round(total_ms, 3),
        "throughputMBps": round(throughput_mbps, 2),
        "durations": summarize_durations(durations),
        "items": results,
        "ok": all(bool(result["ok"]) for result in results),
    }


def build_transcript(paragraphs: int) -> str:
    base = (
        "[Speaker 1]: Dies ist ein synthetischer Export-Benchmark-Absatz mit "
        "genug Text, um PDF- und DOCX-Layoutarbeit realistisch auszulösen. "
        "Er enthält normale Wörter, Satzzeichen und etwas wiederholte Struktur."
    )
    return "\n\n".join(f"{base} Absatz {index + 1}." for index in range(max(1, paragraphs)))


async def measure_exports(
    *,
    paragraphs: int,
    iterations: int,
    concurrency: int,
) -> dict[str, Any]:
    from src.web_api import _render_transcript_export_async

    formats = ("pdf", "docx")
    content = build_transcript(paragraphs)
    summary = "# Benchmark Summary\n- synthetic export load\n- pdf and docx rendering"
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(index: int, export_format: str) -> dict[str, Any]:
        async with semaphore:
            started = time.perf_counter_ns()
            data, content_type, ext = await _render_transcript_export_async(
                export_format=export_format,
                title=f"Upload Export Baseline {index}",
                content=content,
                summary=summary,
                date="2026-06-01",
                duration="10:00",
            )
            duration_ms = (time.perf_counter_ns() - started) / 1_000_000
            return {
                "index": index,
                "format": export_format,
                "durationMs": round(duration_ms, 4),
                "bytes": len(data),
                "contentType": content_type,
                "extension": ext,
                "ok": bool(data) and ext == export_format,
            }

    tasks = [
        run_one(index, export_format)
        for index in range(iterations)
        for export_format in formats
    ]
    started = time.perf_counter_ns()
    items = await asyncio.gather(*tasks)
    total_ms = (time.perf_counter_ns() - started) / 1_000_000
    by_format: dict[str, dict[str, Any]] = {}
    for export_format in formats:
        matching = [item for item in items if item["format"] == export_format]
        by_format[export_format] = {
            "durations": summarize_durations([float(item["durationMs"]) for item in matching]),
            "totalBytes": sum(int(item["bytes"]) for item in matching),
            "ok": all(bool(item["ok"]) for item in matching),
        }
    return {
        "iterationsPerFormat": iterations,
        "concurrency": concurrency,
        "paragraphs": paragraphs,
        "totalExports": len(items),
        "totalMs": round(total_ms, 3),
        "durations": summarize_durations([float(item["durationMs"]) for item in items]),
        "byFormat": by_format,
        "items": items,
        "ok": all(bool(item["ok"]) for item in items),
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    upload_file_size_bytes = int(args.upload_size_mb * 1024 * 1024)
    upload_chunk_size_bytes = int(args.upload_chunk_mb * 1024 * 1024)
    with tempfile.TemporaryDirectory(
        prefix="scriber-upload-export-baseline-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        managed_env = {
            "SCRIBER_DISABLE_DEVICE_MONITOR": "1",
            "SCRIBER_DISABLE_HOTKEYS": "1",
            "SCRIBER_DATA_DIR": temp_dir,
        }
        old_env = {name: os.environ.get(name) for name in managed_env}
        os.environ.update(managed_env)
        try:
            temp_path = Path(temp_dir)
            upload = await measure_upload_streams(
                temp_path,
                file_count=args.upload_files,
                file_size_bytes=upload_file_size_bytes,
                chunk_size_bytes=upload_chunk_size_bytes,
            )
            export = await measure_exports(
                paragraphs=args.export_paragraphs,
                iterations=args.export_iterations,
                concurrency=args.export_concurrency,
            )
        finally:
            for name, old_value in old_env.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "upload": upload,
            "export": export,
        },
        "ok": bool(upload["ok"]) and bool(export["ok"]),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure synthetic upload and export baseline under local load.")
    parser.add_argument("--upload-files", type=int, default=4)
    parser.add_argument("--upload-size-mb", type=float, default=4.0)
    parser.add_argument("--upload-chunk-mb", type=float, default=1.0)
    parser.add_argument("--export-iterations", type=int, default=2)
    parser.add_argument("--export-concurrency", type=int, default=2)
    parser.add_argument("--export-paragraphs", type=int, default=120)
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    args.upload_files = max(1, int(args.upload_files))
    args.upload_size_mb = max(0.001, float(args.upload_size_mb))
    args.upload_chunk_mb = max(0.001, float(args.upload_chunk_mb))
    args.export_iterations = max(1, int(args.export_iterations))
    args.export_concurrency = max(1, int(args.export_concurrency))
    args.export_paragraphs = max(1, int(args.export_paragraphs))
    result = asyncio.run(run_benchmark(args))
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
