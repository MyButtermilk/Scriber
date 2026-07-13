from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SELECT_BACKEND_CACHE = REPO_ROOT / "scripts" / "ci" / "select_backend_sidecar_cache_entry.ps1"


def test_backend_sidecar_cache_selection_keeps_only_attested_entry() -> None:
    fixture_root = REPO_ROOT / "build" / f"cache-selection-test-{uuid.uuid4().hex}"
    cache_root = fixture_root / "cache"
    metadata_path = fixture_root / "sidecar-build-metadata.json"
    selected_key = "a" * 64
    stale_key = "b" * 64
    selected = cache_root / selected_key
    stale = cache_root / stale_key
    try:
        (selected / "scriber-backend").mkdir(parents=True)
        selected_exe = selected / "scriber-backend" / "scriber-backend.exe"
        selected_exe.write_bytes(b"selected")
        (selected / "cache-manifest.json").write_text(
            json.dumps(
                {
                    "cacheKey": selected_key,
                    "sidecarSha256": hashlib.sha256(b"selected").hexdigest(),
                    "sidecarLength": selected_exe.stat().st_size,
                }
            ),
            encoding="utf-8",
        )
        stale.mkdir(parents=True)
        (stale / "stale.bin").write_bytes(b"stale")
        metadata_path.write_text(
            json.dumps({"cache": {"key": selected_key}}), encoding="utf-8"
        )

        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SELECT_BACKEND_CACHE),
                "-CacheRoot",
                str(cache_root.relative_to(REPO_ROOT)),
                "-MetadataPath",
                str(metadata_path.relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert selected.is_dir()
        assert not stale.exists()
        assert [path.name for path in cache_root.iterdir()] == [selected_key]
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)
