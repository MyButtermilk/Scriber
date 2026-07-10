from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / "bundle"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / "release-metadata"
VERSION_FILE = REPO_ROOT / "src" / "version.py"


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not find __version__ in {VERSION_FILE}")
    return match.group(1)


def default_release_tag() -> str:
    ref_name = os.getenv("GITHUB_REF_NAME", "").strip()
    if os.getenv("GITHUB_REF_TYPE", "").strip().lower() == "tag":
        return ref_name
    if ref_name.startswith("v") and re.match(r"^v\d+\.\d+\.\d+", ref_name):
        return ref_name
    return ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_artifacts(root: Path = DEFAULT_ARTIFACT_DIR, *, version: str | None = None) -> list[Path]:
    if not root.exists():
        return []
    artifacts = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".exe", ".msi"}
    )
    if version:
        normalized_version = version[1:] if version.startswith("v") else version
        version_pattern = re.compile(
            rf"(?<![0-9]){re.escape(normalized_version)}(?![0-9])",
            re.IGNORECASE,
        )
        return [path for path in artifacts if version_pattern.search(path.name)]
    return artifacts


def primary_platform_artifact(artifacts: list[Path]) -> Path | None:
    if not artifacts:
        return None
    nsis = [
        path
        for path in artifacts
        if path.suffix.lower() == ".exe" and "setup" in path.name.lower()
    ]
    if nsis:
        return sorted(nsis)[0]
    return sorted(artifacts)[0]


def artifact_url(path: Path, *, base_url: str | None, repository: str | None, tag: str | None) -> str:
    filename = path.name
    if base_url:
        return f"{base_url.rstrip('/')}/{filename}"
    if repository and tag:
        return f"https://github.com/{repository}/releases/download/{tag}/{filename}"
    return filename


def read_signature(path: Path) -> str:
    sig_path = Path(f"{path}.sig")
    if not sig_path.exists():
        return ""
    return sig_path.read_text(encoding="utf-8").strip()


def build_latest_json(
    artifacts: list[Path],
    *,
    version: str,
    notes: str,
    pub_date: str,
    platform: str,
    base_url: str | None,
    repository: str | None,
    tag: str | None,
) -> dict:
    artifact_entries = []
    platforms: dict[str, dict[str, str]] = {}

    platform_artifact = primary_platform_artifact(artifacts)

    for path in artifacts:
        checksum = sha256_file(path)
        url = artifact_url(path, base_url=base_url, repository=repository, tag=tag)
        signature = read_signature(path)
        artifact_entries.append(
            {
                "name": path.name,
                "url": url,
                "sha256": checksum,
                "sizeBytes": path.stat().st_size,
                "signature": signature,
            }
        )

    if platform_artifact:
        signature = read_signature(platform_artifact)
        platforms[platform] = {
            "signature": signature,
            "url": artifact_url(platform_artifact, base_url=base_url, repository=repository, tag=tag),
        }

    return {
        "version": version,
        "notes": notes,
        "pub_date": pub_date,
        "platforms": platforms,
        "artifacts": artifact_entries,
    }


def write_metadata(
    artifacts: list[Path],
    *,
    output_dir: Path,
    version: str,
    notes: str,
    platform: str,
    base_url: str | None,
    repository: str | None,
    tag: str | None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    pub_date = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    checksums = []
    for path in artifacts:
        checksums.append(f"{sha256_file(path)}  {path.name}")

    checksums_path = output_dir / "SHA256SUMS.txt"
    checksums_path.write_text("\n".join(checksums) + ("\n" if checksums else ""), encoding="utf-8")

    latest = build_latest_json(
        artifacts,
        version=version,
        notes=notes,
        pub_date=pub_date,
        platform=platform,
        base_url=base_url,
        repository=repository,
        tag=tag,
    )
    latest_path = output_dir / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "version": version,
        "artifactCount": len(artifacts),
        "outputDir": str(output_dir),
        "latestJson": str(latest_path),
        "sha256Sums": str(checksums_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create Scriber release metadata.")
    parser.add_argument("--artifact", action="append", default=[], help="Release artifact path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--version", default=read_version())
    parser.add_argument("--notes", default="")
    parser.add_argument("--platform", default="windows-x86_64")
    parser.add_argument("--base-url", default=os.getenv("SCRIBER_RELEASE_BASE_URL", ""))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "MyButtermilk/Scriber"))
    parser.add_argument("--tag", default=default_release_tag())
    args = parser.parse_args(argv)

    artifacts = [Path(item).expanduser().resolve() for item in args.artifact]
    if not artifacts:
        artifacts = discover_artifacts(version=args.version)
    missing = [str(path) for path in artifacts if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing release artifacts: {', '.join(missing)}")
    if not artifacts:
        raise FileNotFoundError(f"No release artifacts found under {DEFAULT_ARTIFACT_DIR}")

    result = write_metadata(
        artifacts,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        version=args.version,
        notes=args.notes,
        platform=args.platform,
        base_url=args.base_url.strip() or None,
        repository=args.repository.strip() or None,
        tag=args.tag.strip() or None,
    )
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
