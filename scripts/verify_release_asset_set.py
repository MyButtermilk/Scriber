"""Verify that a GitHub API round-trip preserved the exact release asset set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_tauri_updater_metadata import (
    sha256_file,
    validate_local_artifacts,
    validate_metadata,
)


def _files_by_name(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Release asset directory was not found: {root}")
    files = list(root.iterdir())
    if any(not item.is_file() for item in files):
        raise ValueError(f"Release asset directory must be flat: {root}")
    return {item.name: item for item in files}


def verify_release_asset_set(
    *,
    expected_dir: Path,
    downloaded_dir: Path,
    platform: str = "windows-x86_64",
) -> dict[str, Any]:
    expected = _files_by_name(expected_dir)
    downloaded = _files_by_name(downloaded_dir)
    if expected.keys() != downloaded.keys():
        missing = sorted(expected.keys() - downloaded.keys())
        unexpected = sorted(downloaded.keys() - expected.keys())
        raise ValueError(f"Downloaded release asset set differs: missing={missing}, unexpected={unexpected}")

    identities: list[dict[str, Any]] = []
    for name in sorted(expected):
        expected_path = expected[name]
        downloaded_path = downloaded[name]
        expected_size = expected_path.stat().st_size
        downloaded_size = downloaded_path.stat().st_size
        expected_sha = sha256_file(expected_path)
        downloaded_sha = sha256_file(downloaded_path)
        if downloaded_size != expected_size:
            raise ValueError(
                f"Downloaded release asset size mismatch for {name}: expected {expected_size}, found {downloaded_size}"
            )
        if downloaded_sha != expected_sha:
            raise ValueError(
                f"Downloaded release asset SHA256 mismatch for {name}: expected {expected_sha}, found {downloaded_sha}"
            )
        identities.append({"name": name, "sizeBytes": downloaded_size, "sha256": downloaded_sha})

    metadata_path = downloaded_dir / "latest.json"
    sums_path = downloaded_dir / "SHA256SUMS.txt"
    if not metadata_path.is_file() or not sums_path.is_file():
        raise ValueError("Downloaded assets must include latest.json and SHA256SUMS.txt")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    validate_metadata(
        metadata,
        platform=platform,
        require_signatures=True,
        allow_local_urls=False,
    )
    validated_artifacts = validate_local_artifacts(
        metadata,
        artifact_dir=downloaded_dir,
        sha256sums_path=sums_path,
    )
    for artifact in metadata["artifacts"]:
        name = artifact["name"]
        signature_path = downloaded_dir / f"{name}.sig"
        if not signature_path.is_file():
            raise ValueError(f"Downloaded signed updater artifact is missing {name}.sig")
        signature_text = signature_path.read_text(encoding="utf-8").strip()
        if signature_text != artifact["signature"].strip():
            raise ValueError(f"Downloaded signature file does not match latest.json for {name}")

    return {
        "schemaVersion": 1,
        "ok": True,
        "expectedAssetCount": len(expected),
        "downloadedAssetCount": len(downloaded),
        "updaterArtifactsValidated": validated_artifacts,
        "assets": identities,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--downloaded-dir", type=Path, required=True)
    parser.add_argument("--platform", default="windows-x86_64")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify_release_asset_set(
        expected_dir=args.expected_dir.resolve(),
        downloaded_dir=args.downloaded_dir.resolve(),
        platform=args.platform,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
