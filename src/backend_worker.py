"""Standalone backend worker entry point for packaged desktop runtimes."""

from __future__ import annotations

import json
import sys


def run_runtime_import_check() -> int:
    from scripts.check_backend_runtime_imports import check_imports

    missing = check_imports()
    print(json.dumps({"ok": not missing, "missing": missing}, separators=(",", ":")))
    return 1 if missing else 0


def main() -> int:
    if "--runtime-import-check" in sys.argv:
        return run_runtime_import_check()

    from src.web_api import main as web_api_main

    web_api_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
