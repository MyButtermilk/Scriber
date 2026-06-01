from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / "release-metadata" / "latest.json"
SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_https_url(url: str, *, allow_local_urls: bool) -> None:
    parsed = urlparse(url)
    if allow_local_urls and not parsed.scheme and not parsed.netloc and url.strip():
        return
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"Updater URL must be absolute HTTPS: {url}")


def validate_metadata(
    data: dict,
    *,
    platform: str,
    require_signatures: bool,
    allow_local_urls: bool,
) -> None:
    version = data.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        raise ValueError("latest.json version must be SemVer, optionally prefixed with v.")

    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        raise ValueError("latest.json platforms must be an object.")

    platform_entry = platforms.get(platform)
    if not isinstance(platform_entry, dict):
        raise ValueError(f"latest.json is missing platforms.{platform}.")

    url = platform_entry.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"platforms.{platform}.url is required.")
    validate_https_url(url, allow_local_urls=allow_local_urls)

    signature = platform_entry.get("signature")
    if not isinstance(signature, str):
        raise ValueError(f"platforms.{platform}.signature must be a string.")
    if require_signatures and not signature.strip():
        raise ValueError(f"platforms.{platform}.signature is required for updater releases.")

    artifacts = data.get("artifacts", [])
    if artifacts is None:
        artifacts = []
    if not isinstance(artifacts, list):
        raise ValueError("latest.json artifacts must be a list when present.")

    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ValueError(f"artifacts[{index}] must be an object.")
        artifact_url = artifact.get("url")
        if not isinstance(artifact_url, str) or not artifact_url.strip():
            raise ValueError(f"artifacts[{index}].url is required.")
        validate_https_url(artifact_url, allow_local_urls=allow_local_urls)

        checksum = artifact.get("sha256")
        if not isinstance(checksum, str) or not SHA256_RE.match(checksum):
            raise ValueError(f"artifacts[{index}].sha256 must be a 64-character SHA256 hex digest.")

        size_bytes = artifact.get("sizeBytes")
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise ValueError(f"artifacts[{index}].sizeBytes must be a positive integer.")

        artifact_signature = artifact.get("signature", "")
        if not isinstance(artifact_signature, str):
            raise ValueError(f"artifacts[{index}].signature must be a string.")
        if require_signatures and not artifact_signature.strip():
            raise ValueError(f"artifacts[{index}].signature is required for updater releases.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Scriber latest.json for Tauri updater compatibility.")
    parser.add_argument("--metadata", default=str(DEFAULT_METADATA))
    parser.add_argument("--platform", default="windows-x86_64")
    parser.add_argument("--require-signatures", action="store_true")
    parser.add_argument(
        "--allow-local-urls",
        action="store_true",
        help="Permit filename-only artifact URLs for local non-updater builds.",
    )
    args = parser.parse_args(argv)

    metadata_path = Path(args.metadata).expanduser().resolve()
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    validate_metadata(
        data,
        platform=args.platform,
        require_signatures=args.require_signatures,
        allow_local_urls=args.allow_local_urls and not args.require_signatures,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "metadata": str(metadata_path),
                "platform": args.platform,
                "requireSignatures": args.require_signatures,
                "allowLocalUrls": args.allow_local_urls and not args.require_signatures,
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
