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
NSIS_COMPRESSIONS = {"lzma", "zlib", "bzip2", "none"}


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


def parse_endpoints(explicit_endpoints: list[str]) -> list[str]:
    endpoints = explicit_endpoints or [os.getenv("SCRIBER_TAURI_UPDATER_ENDPOINT", "").strip() or DEFAULT_ENDPOINT]
    return [parse_endpoint(endpoint) for endpoint in endpoints]


def configure_app_version(config: dict, version: str) -> dict:
    clean_version = version.strip()
    if clean_version:
        config["version"] = clean_version
    return config


def configure_nsis_compression(config: dict, compression: str) -> dict:
    clean_compression = compression.strip()
    if not clean_compression:
        return config
    if clean_compression not in NSIS_COMPRESSIONS:
        raise ValueError(
            f"Unsupported NSIS compression {compression!r}; expected one of {sorted(NSIS_COMPRESSIONS)}"
        )
    bundle = config.setdefault("bundle", {})
    windows = bundle.setdefault("windows", {})
    nsis = windows.setdefault("nsis", {})
    nsis["compression"] = clean_compression
    return config


def build_release_overlay(
    *,
    version: str,
    nsis_compression: str,
    remove_before_bundle: bool,
    updater_public_key: str,
    updater_endpoints: list[str],
    skip_updater_config: bool,
) -> dict:
    overlay: dict = {}
    configure_app_version(overlay, version)
    configure_nsis_compression(overlay, nsis_compression)
    if remove_before_bundle:
        remove_before_bundle_command(overlay)
    if not skip_updater_config:
        configure_tauri_updater(overlay, public_key=updater_public_key, endpoints=updater_endpoints)
    return overlay


def remove_before_bundle_command(config: dict) -> dict:
    build = config.setdefault("build", {})
    if isinstance(build, dict):
        build["beforeBundleCommand"] = None
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a Tauri config for Scriber release builds.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default="", help="Write the modified config to this path instead of --config.")
    parser.add_argument("--write", action="store_true", help="Write the modified config back to disk.")
    parser.add_argument("--version", default="", help="Concrete app version to write into the generated build config.")
    parser.add_argument(
        "--nsis-compression",
        choices=sorted(NSIS_COMPRESSIONS),
        default="",
        help="Optional NSIS compression override for the generated build config.",
    )
    parser.add_argument(
        "--remove-before-bundle-command",
        action="store_true",
        help="Disable build.beforeBundleCommand in the generated build config.",
    )
    parser.add_argument(
        "--skip-updater-config",
        action="store_true",
        help="Do not require signing keys or write updater plugin settings.",
    )
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
    if (
        not args.skip_updater_config
        and not args.skip_signing_key_check
        and not signing_key
        and not signing_key_path
    ):
        raise RuntimeError(
            "TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH is required for updater artifacts."
        )

    parsed_endpoints: list[str] = []
    if not args.skip_updater_config:
        parsed_endpoints = parse_endpoints(args.endpoint)

    config_path = Path(args.config).expanduser().resolve()
    base_config = json.loads(config_path.read_text(encoding="utf-8"))
    if args.output:
        config = build_release_overlay(
            version=args.version,
            nsis_compression=args.nsis_compression,
            remove_before_bundle=args.remove_before_bundle_command,
            updater_public_key=args.public_key,
            updater_endpoints=parsed_endpoints,
            skip_updater_config=args.skip_updater_config,
        )
    else:
        config = base_config
        configure_app_version(config, args.version)
        configure_nsis_compression(config, args.nsis_compression)
        if args.remove_before_bundle_command:
            remove_before_bundle_command(config)
        if not args.skip_updater_config:
            configure_tauri_updater(config, public_key=args.public_key, endpoints=parsed_endpoints)

    output_path = Path(args.output).expanduser().resolve() if args.output else config_path
    written = args.write or bool(args.output)
    if written:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "config": str(config_path),
                "output": str(output_path) if written else "",
                "written": written,
                "updaterConfigured": not args.skip_updater_config,
                "endpointCount": len(parsed_endpoints),
                "version": config.get("version"),
                "nsisCompression": config.get("bundle", {})
                .get("windows", {})
                .get("nsis", {})
                .get("compression", ""),
                "beforeBundleCommandRemoved": config.get("build", {}).get("beforeBundleCommand") is None,
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
