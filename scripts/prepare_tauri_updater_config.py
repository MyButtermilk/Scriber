from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "Frontend" / "src-tauri" / "tauri.conf.json"
DEFAULT_ENDPOINT = "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"


def parse_endpoint(value: str) -> str:
    endpoint = value.strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"Updater endpoint must be an absolute HTTPS URL: {value}")
    return endpoint


def configure_tauri_updater(config: dict, *, public_key: str, endpoints: list[str]) -> dict:
    if not public_key.strip():
        raise ValueError("Tauri updater public key is required.")
    if not endpoints:
        raise ValueError("At least one updater endpoint is required.")

    bundle = config.setdefault("bundle", {})
    bundle["createUpdaterArtifacts"] = True

    plugins = config.setdefault("plugins", {})
    updater = plugins.setdefault("updater", {})
    updater["pubkey"] = public_key.strip()
    updater["endpoints"] = endpoints
    updater.setdefault("windows", {})["installMode"] = "passive"
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare tauri.conf.json for signed Tauri updater builds.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--write", action="store_true", help="Write the modified config back to disk.")
    parser.add_argument(
        "--public-key",
        default=os.getenv("SCRIBER_TAURI_UPDATER_PUBLIC_KEY", ""),
        help="Tauri updater public key content. Defaults to SCRIBER_TAURI_UPDATER_PUBLIC_KEY.",
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        default=[],
        help="HTTPS latest.json endpoint. Can be passed multiple times.",
    )
    parser.add_argument(
        "--skip-signing-key-check",
        action="store_true",
        help="Do not require TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH.",
    )
    args = parser.parse_args(argv)

    signing_key = os.getenv("TAURI_SIGNING_PRIVATE_KEY", "").strip()
    signing_key_path = os.getenv("TAURI_SIGNING_PRIVATE_KEY_PATH", "").strip()
    if not args.skip_signing_key_check and not signing_key and not signing_key_path:
        raise RuntimeError(
            "TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH is required for updater artifacts."
        )

    endpoints = args.endpoint or [os.getenv("SCRIBER_TAURI_UPDATER_ENDPOINT", DEFAULT_ENDPOINT)]
    parsed_endpoints = [parse_endpoint(endpoint) for endpoint in endpoints]

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    configure_tauri_updater(config, public_key=args.public_key, endpoints=parsed_endpoints)

    if args.write:
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "config": str(config_path),
                "written": args.write,
                "endpointCount": len(parsed_endpoints),
                "createUpdaterArtifacts": config.get("bundle", {}).get("createUpdaterArtifacts") is True,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        sys.exit(1)
