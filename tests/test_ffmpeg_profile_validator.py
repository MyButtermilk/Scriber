from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "ffmpeg" / "validate_ffmpeg_profile.py"


spec = importlib.util.spec_from_file_location("validate_ffmpeg_profile", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def test_parse_list_output_expands_ffmpeg_alias_groups() -> None:
    output = """
 Demuxers:
 D  matroska,webm  Matroska / WebM
 D  mov,mp4,m4a,3gp,3g2,mj2 QuickTime / MOV
 D  mp3            MP3
"""

    parsed = validator.parse_list_output(output)

    assert "matroska,webm" in parsed
    assert "matroska" in parsed
    assert "webm" in parsed
    assert "mov" in parsed
    assert "mp4" in parsed
    assert "m4a" in parsed
    assert "mp3" in parsed


def test_validate_requirements_accepts_profile_b_audio_set() -> None:
    requirements = validator.PROFILE_REQUIREMENTS["B"]
    capabilities = {category: set(values) for category, values in requirements.items()}

    failures, warnings = validator.validate_requirements("B", capabilities)

    assert failures == []
    assert warnings == []
    assert "libmp3lame" in requirements["encoders"]
    assert "pcm_s16le" in requirements["encoders"]
    assert "s16le" in requirements["demuxers"]
    assert "s16le" in requirements["muxers"]
    assert "pipe" in requirements["protocols"]
    assert "adelay" in requirements["filters"]


def test_validate_requirements_reports_missing_encoder_and_network_warning() -> None:
    requirements = validator.PROFILE_REQUIREMENTS["B"]
    capabilities = {category: set(values) for category, values in requirements.items()}
    capabilities["encoders"].remove("libmp3lame")
    capabilities["protocols"].add("https")

    failures, warnings = validator.validate_requirements("B", capabilities)

    assert "missing encoders: libmp3lame" in failures
    assert "network protocols enabled: https" in warnings


def test_parse_build_flags_captures_license_flags() -> None:
    buildconf = """
configuration:
    --enable-gpl
    --enable-libopus --enable-libmp3lame
    --disable-network
"""

    flags = validator.parse_build_flags(buildconf)

    assert "--enable-gpl" in flags
    assert "--enable-libopus" in flags
    assert "--disable-network" in flags
