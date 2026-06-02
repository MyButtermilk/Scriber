from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = REPO_ROOT / "Frontend" / "src-tauri" / "target" / "release" / "release-metadata" / "latest.json"
SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sha256sums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not SHA256_RE.match(parts[0]):
            raise ValueError(f"Invalid SHA256SUMS.txt line {line_no}.")
        name = parts[1].strip()
        if name.startswith("*"):
            name = name[1:]
        if not name:
            raise ValueError(f"Invalid SHA256SUMS.txt line {line_no}: missing artifact name.")
        checksums[name] = parts[0].lower()
    return checksums


def resolve_artifact_path(artifact_dir: Path, name: str) -> Path:
    if not name or Path(name).name != name:
        raise ValueError(f"Artifact name must be a filename without path separators: {name!r}")
    direct_path = (artifact_dir / name).resolve()
    if direct_path.is_file():
        return direct_path

    matches = [path.resolve() for path in artifact_dir.rglob(name) if path.is_file()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        formatted = ", ".join(str(path) for path in matches)
        raise ValueError(f"Artifact name is ambiguous under {artifact_dir}: {name} matched {formatted}")
    return direct_path


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


def validate_local_artifacts(
    data: dict,
    *,
    artifact_dir: Path,
    sha256sums_path: Path | None,
) -> int:
    artifacts = data.get("artifacts", [])
    if not artifacts:
        raise ValueError("latest.json artifacts are required for local artifact validation.")

    checksum_manifest = parse_sha256sums(sha256sums_path) if sha256sums_path else {}
    checked = 0

    for index, artifact in enumerate(artifacts):
        name = artifact.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"artifacts[{index}].name is required for local artifact validation.")

        artifact_path = resolve_artifact_path(artifact_dir, name.strip())
        if not artifact_path.is_file():
            raise FileNotFoundError(f"Artifact listed in latest.json was not found: {artifact_path}")

        expected_size = artifact["sizeBytes"]
        actual_size = artifact_path.stat().st_size
        if actual_size != expected_size:
            raise ValueError(
                f"Artifact size mismatch for {name}: latest.json has {expected_size}, file has {actual_size}."
            )

        expected_sha = artifact["sha256"].lower()
        actual_sha = sha256_file(artifact_path).lower()
        if actual_sha != expected_sha:
            raise ValueError(
                f"Artifact SHA256 mismatch for {name}: latest.json has {expected_sha}, file has {actual_sha}."
            )

        if sha256sums_path:
            sums_sha = checksum_manifest.get(name)
            if not sums_sha:
                raise ValueError(f"SHA256SUMS.txt is missing artifact {name}.")
            if sums_sha != expected_sha:
                raise ValueError(
                    f"SHA256SUMS.txt mismatch for {name}: sums has {sums_sha}, latest.json has {expected_sha}."
                )

        checked += 1

    return checked


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
    parser.add_argument(
        "--artifact-dir",
        default="",
        help="Optional directory containing release artifacts listed by latest.json.",
    )
    parser.add_argument(
        "--sha256sums",
        default="",
        help="Optional SHA256SUMS.txt path to cross-check against latest.json and local artifacts.",
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
    local_artifact_count = 0
    artifact_dir = None
    sha256sums_path = None
    if args.artifact_dir:
        artifact_dir = Path(args.artifact_dir).expanduser().resolve()
        sha256sums_path = Path(args.sha256sums).expanduser().resolve() if args.sha256sums else None
        if sha256sums_path and not sha256sums_path.is_file():
            raise FileNotFoundError(f"SHA256SUMS.txt was not found: {sha256sums_path}")
        local_artifact_count = validate_local_artifacts(
            data,
            artifact_dir=artifact_dir,
            sha256sums_path=sha256sums_path,
        )
    print(
        json.dumps(
            {
                "ok": True,
                "metadata": str(metadata_path),
                "platform": args.platform,
                "requireSignatures": args.require_signatures,
                "allowLocalUrls": args.allow_local_urls and not args.require_signatures,
                "localArtifactsVerified": local_artifact_count,
                "artifactDir": str(artifact_dir) if artifact_dir else "",
                "sha256Sums": str(sha256sums_path) if sha256sums_path else "",
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
