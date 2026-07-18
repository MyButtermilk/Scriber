from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


CLI_PACKAGE = "node_modules/@tauri-apps/cli"
PLATFORM_PACKAGE = "node_modules/@tauri-apps/cli-win32-x64-msvc"
INTEGRITY_PATTERN = re.compile(r"^sha512-[A-Za-z0-9+/=]+$")


def read_contract(package_lock_path: Path) -> dict[str, str]:
    package_lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
    packages = package_lock["packages"]
    cli = packages[CLI_PACKAGE]
    platform = packages[PLATFORM_PACKAGE]
    version = str(cli.get("version", ""))
    platform_version = str(platform.get("version", ""))
    package_integrity = str(cli.get("integrity", ""))
    platform_integrity = str(platform.get("integrity", ""))
    if (
        not version
        or version != platform_version
        or not INTEGRITY_PATTERN.fullmatch(package_integrity)
        or not INTEGRITY_PATTERN.fullmatch(platform_integrity)
    ):
        raise ValueError("package-lock.json does not contain one exact Windows x64 Tauri CLI contract")
    return {
        "version": version,
        "packageIntegrity": package_integrity,
        "platformVersion": platform_version,
        "platformPackageIntegrity": platform_integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the exact Windows x64 Tauri CLI contract from package-lock.json."
    )
    parser.add_argument("--package-lock", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(read_contract(args.package_lock), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
