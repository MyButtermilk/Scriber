from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.prepare_tauri_updater_config import DEFAULT_ENDPOINT
from scripts.validate_tauri_updater_metadata import DEFAULT_METADATA, sha256_file, validate_metadata


def fetch_published_metadata(url: str, *, timeout_sec: float) -> tuple[int, bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Scriber-release-readiness/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        status_code = int(response.getcode() or 0)
        final_url = response.geturl() or url
        return status_code, response.read(), final_url


def build_publication_report(
    *,
    url: str,
    status_code: int,
    body: bytes,
    local_metadata_path: Path,
    platform: str = "windows-x86_64",
    final_url: str = "",
) -> dict[str, Any]:
    failures: list[str] = []
    report: dict[str, Any] = {
        "ok": False,
        "url": url,
        "finalUrl": final_url or url,
        "statusCode": status_code,
        "requireSignatures": True,
        "platform": platform,
        "metadata": str(local_metadata_path),
        "metadataSha256": "",
        "localMetadataSha256": "",
        "metadataMatchesLocal": False,
        "downloadedBytes": len(body),
        "failures": failures,
    }

    if not is_https_url(url):
        failures.append("updater publication URL must be absolute HTTPS")
    if not is_https_url(report["finalUrl"]):
        failures.append("updater publication finalUrl must be absolute HTTPS")
    if status_code != 200:
        failures.append(f"updater publication status code must be 200, got {status_code}")

    downloaded_sha = sha256_bytes(body)
    report["metadataSha256"] = downloaded_sha

    try:
        downloaded_metadata = json.loads(body.decode("utf-8"))
    except Exception as exc:
        failures.append(f"published latest.json is not valid UTF-8 JSON: {exc}")
        downloaded_metadata = None

    if not isinstance(downloaded_metadata, dict):
        failures.append("published latest.json root must be a JSON object")
    else:
        try:
            validate_metadata(
                downloaded_metadata,
                platform=platform,
                require_signatures=True,
                allow_local_urls=False,
            )
        except Exception as exc:
            failures.append(str(exc))

    if not local_metadata_path.is_file():
        failures.append(f"local latest.json was not found: {local_metadata_path}")
    else:
        local_sha = sha256_file(local_metadata_path).lower()
        report["localMetadataSha256"] = local_sha
        report["metadataMatchesLocal"] = downloaded_sha == local_sha
        if downloaded_sha != local_sha:
            failures.append("published latest.json SHA256 does not match local latest.json")

    report["ok"] = not failures
    return report


def verify_publication(
    *,
    url: str,
    local_metadata_path: Path,
    platform: str = "windows-x86_64",
    timeout_sec: float = 20.0,
    attempts: int = 1,
    retry_delay_sec: float = 2.0,
) -> dict[str, Any]:
    normalized_attempts = max(1, attempts)
    last_report: dict[str, Any] | None = None
    for attempt in range(1, normalized_attempts + 1):
        report = verify_publication_once(
            url=url,
            local_metadata_path=local_metadata_path,
            platform=platform,
            timeout_sec=timeout_sec,
        )
        report["attempt"] = attempt
        report["attempts"] = normalized_attempts
        last_report = report
        if report["ok"] or attempt >= normalized_attempts:
            return report
        time.sleep(max(0.0, retry_delay_sec))
    return last_report or {}


def verify_publication_once(
    *,
    url: str,
    local_metadata_path: Path,
    platform: str,
    timeout_sec: float,
) -> dict[str, Any]:
    if not is_https_url(url):
        return build_publication_report(
            url=url,
            status_code=0,
            body=b"",
            local_metadata_path=local_metadata_path,
            platform=platform,
            final_url=url,
        )
    try:
        status_code, body, final_url = fetch_published_metadata(url, timeout_sec=timeout_sec)
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return build_publication_report(
            url=url,
            status_code=int(exc.code),
            body=body,
            local_metadata_path=local_metadata_path,
            platform=platform,
            final_url=exc.url or url,
        )
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "finalUrl": url,
            "statusCode": 0,
            "requireSignatures": True,
            "platform": platform,
            "metadata": str(local_metadata_path),
            "metadataSha256": "",
            "localMetadataSha256": sha256_file(local_metadata_path).lower() if local_metadata_path.is_file() else "",
            "metadataMatchesLocal": False,
            "downloadedBytes": 0,
            "failures": [str(exc)],
        }
    return build_publication_report(
        url=url,
        status_code=status_code,
        body=body,
        local_metadata_path=local_metadata_path,
        platform=platform,
        final_url=final_url,
    )


def sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def write_output(payload: dict[str, Any], path: str) -> None:
    if not path:
        return
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and verify the published signed Tauri updater latest.json.",
    )
    parser.add_argument("--url", default=DEFAULT_ENDPOINT)
    parser.add_argument("--metadata", default=str(DEFAULT_METADATA))
    parser.add_argument("--platform", default="windows-x86_64")
    parser.add_argument("--timeout-sec", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--retry-delay-sec", type=float, default=2.0)
    parser.add_argument("--output", default="")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    payload = verify_publication(
        url=args.url,
        local_metadata_path=Path(args.metadata).expanduser().resolve(),
        platform=args.platform,
        timeout_sec=args.timeout_sec,
        attempts=args.attempts,
        retry_delay_sec=args.retry_delay_sec,
    )
    write_output(payload, args.output)
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
