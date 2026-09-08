"""Measure ordinary read endpoints of an explicitly isolated Scriber runtime.

No response content or credentials are included in the report. The token comes
from SCRIBER_SMOKE_SESSION_TOKEN. Never point this at the installed app's port.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from pathlib import Path

import aiohttp


async def measure(port: int, iterations: int) -> dict:
    results = {}
    headers = {"X-Scriber-Token": os.environ["SCRIBER_SMOKE_SESSION_TOKEN"]}
    async with aiohttp.ClientSession(headers=headers) as client:
        for route in (
            "/api/health",
            "/api/state",
            "/api/settings",
            "/api/transcripts",
            "/api/meetings",
            "/api/podcasts",
        ):
            samples = []
            sizes = []
            for _ in range(iterations + 1):
                started = time.perf_counter()
                async with client.get(
                    f"http://127.0.0.1:{port}{route}", timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    response.raise_for_status()
                    sizes.append(len(await response.read()))
                samples.append((time.perf_counter() - started) * 1000)
            warm = sorted(samples[1:])
            results[route] = {
                "coldMs": round(samples[0], 3),
                "medianMs": round(statistics.median(warm), 3),
                "p95Ms": round(warm[math.ceil(len(warm) * 0.95) - 1], 3),
                "responseBytes": sizes[-1],
            }
    return {"iterations": iterations, "endpoints": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.port == 8765 or not 1 <= args.port <= 65535 or not 3 <= args.iterations <= 100:
        parser.error("Use an isolated port and 3–100 iterations.")
    if not os.environ.get("SCRIBER_SMOKE_SESSION_TOKEN"):
        parser.error("SCRIBER_SMOKE_SESSION_TOKEN is required.")
    result = asyncio.run(measure(args.port, args.iterations))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
