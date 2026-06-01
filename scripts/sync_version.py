from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "src" / "version.py"
TAURI_CONF = REPO_ROOT / "Frontend" / "src-tauri" / "tauri.conf.json"
CARGO_TOML = REPO_ROOT / "Frontend" / "src-tauri" / "Cargo.toml"
PACKAGE_JSON = REPO_ROOT / "Frontend" / "package.json"
PACKAGE_LOCK = REPO_ROOT / "Frontend" / "package-lock.json"


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not find __version__ in {VERSION_FILE}")
    return match.group(1)


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def update_tauri_conf(version: str) -> bool:
    data = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    changed = data.get("version") != version
    data["version"] = version
    if changed:
        write_json(TAURI_CONF, data)
    return changed


def update_package_json(version: str) -> bool:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    changed = data.get("version") != version
    data["version"] = version
    if changed:
        write_json(PACKAGE_JSON, data)
    return changed


def update_package_lock(version: str) -> bool:
    data = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    changed = data.get("version") != version
    data["version"] = version
    root = data.get("packages", {}).get("")
    if isinstance(root, dict) and root.get("version") != version:
        root["version"] = version
        changed = True
    if changed:
        write_json(PACKAGE_LOCK, data)
    return changed


def update_cargo_toml(version: str) -> bool:
    text = CARGO_TOML.read_text(encoding="utf-8")
    updated = re.sub(
        r'(?m)^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{version}"',
        text,
        count=1,
    )
    changed = updated != text
    if changed:
        CARGO_TOML.write_text(updated, encoding="utf-8")
    return changed


def main() -> int:
    version = read_version()
    changed = {
        "tauriConf": update_tauri_conf(version),
        "cargoToml": update_cargo_toml(version),
        "packageJson": update_package_json(version),
        "packageLock": update_package_lock(version),
    }
    print(json.dumps({"version": version, "changed": changed}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
