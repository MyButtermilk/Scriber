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


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent_count = 0
        self.sent_bytes = 0

    async def send_str(self, value: str) -> None:
        self.sent_count += 1
        self.sent_bytes += len(value.encode("utf-8"))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(((pct / 100.0) * len(ordered) + 0.999999) - 1)))
    return float(ordered[idx])


def summarize_durations(durations_ms: list[float]) -> dict[str, float | int]:
    if not durations_ms:
        return {
            "iterations": 0,
            "totalMs": 0.0,
            "eventsPerSecond": 0.0,
            "meanMs": 0.0,
            "p50Ms": 0.0,
            "p95Ms": 0.0,
            "maxMs": 0.0,
        }
    total_ms = sum(durations_ms)
    return {
        "iterations": len(durations_ms),
        "totalMs": round(total_ms, 3),
        "eventsPerSecond": round((len(durations_ms) / total_ms) * 1000.0, 2) if total_ms else 0.0,
        "meanMs": round(statistics.fmean(durations_ms), 4),
        "p50Ms": round(percentile(durations_ms, 50.0), 4),
        "p95Ms": round(percentile(durations_ms, 95.0), 4),
        "maxMs": round(max(durations_ms), 4),
    }


async def measure_broadcast(
    controller: Any,
    payload: dict[str, Any],
    *,
    iterations: int,
    warmup: int,
    client_count: int,
) -> dict[str, Any]:
    clients = [FakeWebSocket() for _ in range(max(0, client_count))]
    for client in clients:
        await controller.add_client(client)  # type: ignore[arg-type]

    for _ in range(warmup):
        await controller.broadcast(payload)

    durations_ms: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        await controller.broadcast(payload)
        durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    for client in clients:
        await controller.remove_client(client)  # type: ignore[arg-type]

    sent_count = sum(client.sent_count for client in clients)
    sent_bytes = sum(client.sent_bytes for client in clients)
    summary = summarize_durations(durations_ms)
    return {
        "clientCount": client_count,
        **summary,
        "sendCalls": sent_count,
        "bytesSent": sent_bytes,
        "bytesPerEvent": round(sent_bytes / sent_count, 2) if sent_count else 0.0,
    }


def measure_json_serialization(
    payload: dict[str, Any],
    *,
    iterations: int,
    warmup: int,
) -> dict[str, Any]:
    from src.core.ws_contracts import version_event_payload

    payload_to_send = version_event_payload(payload)
    for _ in range(warmup):
        json.dumps(payload_to_send, ensure_ascii=False)

    durations_ms: list[float] = []
    bytes_written = 0
    for _ in range(iterations):
        started = time.perf_counter_ns()
        serialized = json.dumps(payload_to_send, ensure_ascii=False)
        durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        bytes_written += len(serialized.encode("utf-8"))

    return {
        **summarize_durations(durations_ms),
        "bytesSerialized": bytes_written,
        "bytesPerEvent": round(bytes_written / iterations, 2) if iterations else 0.0,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="scriber-ws-baseline-",
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
            from src.data.job_store import JobStore
            from src.data.latency_metrics_store import LatencyMetricsStore
            from src.web_api import ScriberWebController

            loop = asyncio.get_running_loop()
            controller = ScriberWebController(
                loop,
                job_store=JobStore(db_path=Path(temp_dir) / "jobs.db"),
                latency_metrics_store=LatencyMetricsStore(db_path=Path(temp_dir) / "metrics.db"),
            )

            payload = {
                "type": "transcript",
                "text": "Dies ist ein repräsentativer Transcript-Chunk für die WebSocket-Baseline.",
                "final": False,
                "sessionId": "baseline-session",
                "recordingState": "recording",
                "audioLevel": 0.42,
                "metadata": {
                    "source": "baseline",
                    "provider": "synthetic",
                    "sequence": 1,
                },
            }
            client_counts = [int(value) for value in str(args.clients).split(",") if value.strip()]

            no_client = await measure_broadcast(
                controller,
                payload,
                iterations=args.iterations,
                warmup=args.warmup,
                client_count=0,
            )
            with_clients = [
                await measure_broadcast(
                    controller,
                    payload,
                    iterations=args.iterations,
                    warmup=args.warmup,
                    client_count=count,
                )
                for count in client_counts
            ]
            serialization = measure_json_serialization(
                payload,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        finally:
            try:
                from src import database as db

                db._close_all_connections()
            except Exception:
                pass
            for name, old_value in old_env.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value

    return {
        "schemaVersion": 1,
        "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "iterations": args.iterations,
        "warmup": args.warmup,
        "summary": {
            "jsonSerialize": serialization,
            "broadcastNoClients": no_client,
            "broadcastWithClients": with_clients,
        },
        "ok": True,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure WebSocket broadcast and JSON serialization baseline.")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--clients", default="1,5")
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    args.iterations = max(1, int(args.iterations))
    args.warmup = max(0, int(args.warmup))
    result = asyncio.run(run_benchmark(args))
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
