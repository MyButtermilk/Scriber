"""Standalone backend worker entry point for packaged desktop runtimes."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


_SIMULATE_STARTUP_TIMEOUT_ONCE_ENV = "SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE"
_SIMULATE_STARTUP_TIMEOUT_MARKER_ENV = "SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER"


def run_runtime_import_check() -> int:
    from scripts.check_backend_runtime_imports import check_imports

    missing = check_imports()
    print(json.dumps({"ok": not missing, "missing": missing}, separators=(",", ":")))
    return 1 if missing else 0


def should_simulate_startup_timeout_once() -> bool:
    if os.getenv(_SIMULATE_STARTUP_TIMEOUT_ONCE_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False

    marker_raw = os.getenv(_SIMULATE_STARTUP_TIMEOUT_MARKER_ENV, "").strip()
    if not marker_raw:
        return False

    marker_path = Path(marker_raw)
    if marker_path.exists():
        return False

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def block_before_backend_start() -> None:
    while True:
        time.sleep(3600)


def main() -> int:
    if "--runtime-import-check" in sys.argv:
        return run_runtime_import_check()

    if should_simulate_startup_timeout_once():
        block_before_backend_start()

    from src.web_api import main as web_api_main

    web_api_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
