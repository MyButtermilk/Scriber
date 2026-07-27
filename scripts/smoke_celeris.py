from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Application imports intentionally follow the sys.path bootstrap above.
from src.celeris import celeris_chat_completion  # noqa: E402


async def main() -> None:
    key = os.getenv("CELERIS_API_KEY", "").strip()
    if not key:
        print("CELERIS_API_KEY is not configured; skipping live smoke test.")
        return
    result = await celeris_chat_completion(
        "Reply with exactly: Celeris ready",
        max_output_tokens=256,
        timeout_seconds=60,
        api_key=key,
    )
    if result.strip().casefold() != "celeris ready":
        raise SystemExit("Celeris live smoke returned an unexpected response.")
    print("Celeris live smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
