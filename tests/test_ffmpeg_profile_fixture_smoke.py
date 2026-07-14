from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ffmpeg" / "smoke_profile_b_fixtures.py"


def test_profile_b_fixture_smoke_covers_documented_matrix() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    for fixture_name in (
        "mp3_cbr",
        "mp3_vbr",
        "wav_pcm_s16",
        "wav_pcm_s24",
        "wav_float",
        "mov_aac",
        "m4a_alac",
        "mp4_aac",
        "webm_opus",
        "mkv_video_audio",
        "ogg_opus",
        "flac",
        "yt_dlp_m4a",
        "yt_dlp_webm_opus",
        "yt_dlp_merged_mp4",
        "no_audio_video",
        "corrupted_input",
        "long_unicode_path_mp3",
    ):
        assert fixture_name in script

    assert "webm_opus_transcode_args" in script
    assert "mp3_transcode_args" in script
    assert "mp3_encode_pcm_pipe_args" in script
    assert "pcm_pipe_decode_args" in script
    assert "wav_pcm_transcode_args" in script
    assert "ffprobe_duration_args" in script
    assert "meeting_opus_playback_args" in script
    assert '"meeting_playback_microphone"' in script
    assert '"meeting_playback_system"' in script
    assert "timeline_origins_ms=[0, 120]" in script
    assert "--media-tools-dir" in script
    assert "--fixture-ffmpeg" in script
    assert "--require-ffprobe" in script
    assert "--timeout-sec" in script
    assert "--meeting-only" in script


def test_profile_b_fixture_smoke_missing_tool_resolution_can_fail(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_profile_b_fixtures", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.resolve_tool("ffmpeg", media_tools_dir=tmp_path, allow_path=False) is None


def test_profile_b_fixture_smoke_writes_artifact(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not available on PATH")

    output_path = tmp_path / "ffmpeg-profile-b-fixtures.json"
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--output",
        str(output_path),
        "--duration-sec",
        "0.25",
    ]
    if shutil.which("ffprobe"):
        cmd.append("--require-ffprobe")

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["apiVersion"] == "1"
    assert payload["ok"] is True
    assert payload["profile"] == "B"
    assert payload["summary"]["failedChecks"] == 0
    assert payload["summary"]["passedChecks"] >= 25

    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["mp3_cbr_to_webm_opus"]["ok"] is True
    assert checks["wav_pcm_s24_to_webm_opus"]["ok"] is True
    assert checks["m4a_alac_to_webm_opus"]["ok"] is True
    assert checks["yt_dlp_merged_mp4_to_webm_opus"]["ok"] is True
    assert checks["azure_mai_webm_opus_to_mp3"]["output"]["suffix"] == ".mp3"
    assert checks["webm_opus_to_pcm_pipe"]["stdoutBytes"] > 0
    assert checks["local_diarization_webm_to_wav_pcm"]["sampleRate"] == 16_000
    assert checks["local_diarization_webm_to_wav_pcm"]["channels"] == 1
    assert checks["local_diarization_webm_to_wav_pcm"]["sampleWidthBytes"] == 2
    assert checks["raw_pcm_pipe_to_mp3"]["stdoutBytes"] > 0
    assert checks["meeting_multitrack_flac"]["trackCount"] == 3
    assert checks["meeting_lossless_work_track"]["streamCopy"] is True
    assert checks["meeting_lossless_work_track"]["workTrack"]["suffix"] == ".flac"
    assert checks["meeting_playback_mix"]["output"]["suffix"] == ".opus"
    assert checks["meeting_playback_mix"]["timelineOriginsMs"] == [0, 120]
    assert checks["no_audio_video_fails"]["ok"] is True
    assert checks["corrupted_input_fails"]["ok"] is True
