from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_media_preparation_smoke_script_covers_media_helper_paths() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_media_preparation.py").read_text(
        encoding="utf-8"
    )

    assert "_maybe_compress_audio_upload" in script
    assert "_extract_audio_from_video" in script
    assert "_ensure_audio_only_file" in script
    assert "prepared_azure_mai_audio_file" in script
    assert "azure_mai_content_type" in script
    assert "_probe_media_duration_seconds" in script
    assert "--media-tools-dir" in script
    assert "--require-ffprobe" in script


def test_windows_build_can_run_media_preparation_smoke_against_bundled_tools() -> None:
    build = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    assert "[switch]$RunMediaPreparationSmoke" in build
    assert "Media preparation smoke" in build
    assert "scripts\\smoke_media_preparation.py" in build
    assert "backend\\tools\\ffmpeg" in build
    assert "--media-tools-dir" in build
    assert "--require-ffprobe" in build
    assert "media-preparation-smoke.json" in build
    assert "$mediaPreparationSmoke[\"path\"] = $mediaPreparationSmokePath" in build
    assert "mediaPreparationSmoke = $mediaPreparationSmoke" in build


def test_installer_smoke_can_verify_installed_media_preparation_tools() -> None:
    installer = (REPO_ROOT / "scripts" / "smoke_windows_installer.ps1").read_text(
        encoding="utf-8"
    )
    build = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    assert "[switch]$VerifyMediaPreparation" in installer
    assert "[switch]$AllowMissingFfprobeForMediaPreparation" in installer
    assert "function Resolve-InstalledMediaToolsDir" in installer
    assert "backend\\tools\\ffmpeg" in installer
    assert "resources\\backend\\tools\\ffmpeg" in installer
    assert "function Invoke-InstalledMediaPreparationSmoke" in installer
    assert '[string]$PythonExecutable = ""' in installer
    assert 'venv\\Scripts\\python.exe' in installer
    assert "& $PythonExecutable @mediaSmokeArgs | Out-Host" in installer
    assert "python @mediaSmokeArgs" not in installer
    assert "scripts\\smoke_media_preparation.py" in installer
    assert "--media-tools-dir" in installer
    assert "--require-ffprobe" in installer
    assert "mediaPreparation = $mediaPreparation" in installer

    assert "[switch]$RunInstallerMediaPreparationSmoke" in build
    assert "$RunInstallerMediaPreparationSmoke" in build
    assert '"-VerifyMediaPreparation"' in build
    assert '"-AllowMissingFfprobeForMediaPreparation"' in build
    assert '"-PythonExecutable"' in build
    assert "$releasePython" in build
    assert "$SkipBundledFfprobe" in build


def test_installed_transcription_workflow_smoke_uses_real_backend_jobs_without_cli_token() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_installed_transcription_workflows.py").read_text(
        encoding="utf-8"
    )

    assert "/api/file/transcribe" in script
    assert "/api/youtube/transcribe" in script
    assert "/api/transcripts/" in script
    assert "Windows SAPI" in script
    assert "SCRIBER_SMOKE_SESSION_TOKEN" in script
    assert "--token-env" in script
    assert "--token\"" not in script
    assert "X-Scriber-Token" in script
    assert "DEFAULT_YOUTUBE_URL" in script
    assert "require_summary" in script


def test_media_preparation_smoke_script_writes_artifact(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not available on PATH")

    output_path = tmp_path / "media-preparation-smoke.json"
    cmd = [
        sys.executable,
        "scripts/smoke_media_preparation.py",
        "--output",
        str(output_path),
    ]
    if shutil.which("ffprobe"):
        cmd.append("--require-ffprobe")

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["apiVersion"] == "1"
    assert payload["ok"] is True
    assert payload["summary"]["failedChecks"] == 0
    assert payload["summary"]["passedChecks"] >= 4

    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["file_upload_compression"]["ok"] is True
    assert checks["video_upload_audio_extraction"]["ok"] is True
    assert checks["youtube_post_download_normalization"]["ok"] is True
    assert checks["azure_mai_audio_preparation"]["ok"] is True
    assert checks["file_upload_compression"]["output"]["suffix"] == ".webm"
    assert checks["azure_mai_audio_preparation"]["prepared"]["suffix"] == ".mp3"
    assert checks["azure_mai_audio_preparation"]["prepared"]["contentType"] == "audio/mpeg"
