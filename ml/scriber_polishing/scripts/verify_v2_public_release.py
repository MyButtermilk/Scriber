#!/usr/bin/env python3
"""Verify the exact public V2 commit anonymously with HEAD and 1-byte Range requests."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.v2_q8_bf16_release import (  # noqa: E402
    AnonymousPublicProbe,
    V2ReleaseError,
    canonical_json_bytes,
    verify_v2_public_release,
)

_SHA256_ETAG = re.compile(r"^[0-9a-f]{64}$")
_GIT_BLOB_ETAG = re.compile(r"^[0-9a-f]{40}$")


def _load_canonical_object(path: Path, label: str) -> dict[str, object]:
    source = path.expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise V2ReleaseError(f"{label} must be a regular file: {source}")
    payload = source.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2ReleaseError(f"{label} is not UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise V2ReleaseError(f"{label} must be a JSON object")
    materialized = dict(value)
    if payload != canonical_json_bytes(materialized):
        raise V2ReleaseError(f"{label} bytes are not canonical")
    return materialized


def _write_immutable(path: Path, payload: bytes) -> None:
    destination = path.expanduser().resolve()
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != payload:
            raise V2ReleaseError(f"refusing to replace V2 public verification: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)


def _sha256_from_etag(value: object) -> str | None:
    normalized = str(value or "").strip().strip('"')
    return f"sha256:{normalized}" if _SHA256_ETAG.fullmatch(normalized) else None


def _git_blob_from_etag(value: object) -> str | None:
    normalized = str(value or "").strip().strip('"')
    return normalized if _GIT_BLOB_ETAG.fullmatch(normalized) else None


class AnonymousHuggingFaceProbe:
    """Hugging Face reader that explicitly disables cached-token use."""

    def __init__(self) -> None:
        try:
            from huggingface_hub import HfApi
        except ImportError as error:  # pragma: no cover - runtime dependency boundary
            raise V2ReleaseError("huggingface-hub is required for anonymous verification") from error
        self._api = HfApi(token=False)
        self._head_commits: dict[tuple[str, str, str], str] = {}

    def repository_info(self, repository_id: str, revision: str) -> dict[str, object]:
        info = self._api.model_info(
            repository_id,
            revision=revision,
            files_metadata=False,
            token=False,
        )
        return {
            "sha": str(info.sha),
            "private": bool(info.private),
            "gated": info.gated,
        }

    def list_files(self, repository_id: str, revision: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for entry in self._api.list_repo_tree(
            repository_id,
            revision=revision,
            repo_type="model",
            recursive=True,
            expand=True,
            token=False,
        ):
            path = getattr(entry, "path", None)
            size = getattr(entry, "size", None)
            if not isinstance(path, str) or not isinstance(size, int):
                continue
            lfs = getattr(entry, "lfs", None)
            lfs_sha = getattr(lfs, "sha256", None)
            if lfs_sha is None and isinstance(lfs, Mapping):
                lfs_sha = lfs.get("sha256")
            rows.append(
                {
                    "path": path,
                    "bytes": size,
                    "sha256": _sha256_from_etag(lfs_sha),
                    "git_blob_oid": _git_blob_from_etag(
                        getattr(entry, "blob_id", None)
                    ),
                }
            )
        return rows

    def head(self, repository_id: str, revision: str, path: str) -> dict[str, object]:
        try:
            from huggingface_hub import get_hf_file_metadata, hf_hub_url
        except ImportError as error:  # pragma: no cover - initialized above
            raise V2ReleaseError("huggingface-hub is required for anonymous verification") from error
        url = hf_hub_url(repository_id, filename=path, revision=revision)
        metadata = get_hf_file_metadata(url, token=False)
        commit = str(metadata.commit_hash)
        self._head_commits[(repository_id, revision, path)] = commit
        return {
            "status": 200,
            "content_length": int(metadata.size),
            "resolved_commit": commit,
            "sha256": _sha256_from_etag(metadata.etag),
            "git_blob_oid": _git_blob_from_etag(metadata.etag),
        }

    def range_get(
        self,
        repository_id: str,
        revision: str,
        path: str,
        *,
        range_header: str,
    ) -> dict[str, object]:
        try:
            from huggingface_hub import hf_hub_url
        except ImportError as error:  # pragma: no cover - initialized above
            raise V2ReleaseError("huggingface-hub is required for anonymous verification") from error
        request = urllib.request.Request(
            hf_hub_url(repository_id, filename=path, revision=revision),
            headers={
                "Range": range_header,
                "User-Agent": "scriber-v2-anonymous-range-verifier/1",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HF URL
            status = int(response.status)
            content_range = str(response.headers.get("Content-Range", ""))
            content_length = int(response.headers.get("Content-Length", "0"))
            payload = response.read(2)
        if status != 206 or len(payload) != 1:
            raise V2ReleaseError("anonymous Range endpoint did not honor the one-byte request")
        return {
            "status": status,
            "content_length": content_length,
            "content_range": content_range,
            "resolved_commit": self._head_commits.get(
                (repository_id, revision, path), ""
            ),
            "payload": payload,
        }


def main(
    argv: list[str] | None = None,
    *,
    probe: AnonymousPublicProbe | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--staged-root", type=Path, required=True)
    parser.add_argument("--publication-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verification = verify_v2_public_release(
            _load_canonical_object(args.plan, "V2 release plan"),
            inventory=_load_canonical_object(args.inventory, "V2 public inventory"),
            staged_root=args.staged_root,
            publication_receipt=_load_canonical_object(
                args.publication_receipt, "V2 publication receipt"
            ),
            probe=probe or AnonymousHuggingFaceProbe(),
        )
        _write_immutable(args.output, canonical_json_bytes(verification))
    except (OSError, V2ReleaseError, ValueError) as error:
        print(f"v2_public_verification=failed error={error}", file=sys.stderr)
        return 2
    print(
        "v2_public_verification=passed "
        f"repository={verification['repository_id']} commit={verification['public_commit']} "
        "full_model_redownload=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
