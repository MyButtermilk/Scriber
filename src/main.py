from __future__ import annotations

import sys


def main() -> int:
    print(
        "The legacy Python desktop UI has been removed. "
        "Start Scriber through the Tauri desktop shell instead:\n"
        "  cd Frontend\n"
        "  npm run tauri:dev",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
