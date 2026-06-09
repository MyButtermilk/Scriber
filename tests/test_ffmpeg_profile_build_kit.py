from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_KIT_PATH = REPO_ROOT / "scripts" / "ffmpeg" / "create_profile_b_build_kit.py"
VALIDATOR_PATH = REPO_ROOT / "scripts" / "ffmpeg" / "validate_ffmpeg_profile.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_kit = load_module(BUILD_KIT_PATH, "create_profile_b_build_kit")
validator = load_module(VALIDATOR_PATH, "validate_ffmpeg_profile")


def test_profile_b_configure_args_cover_validator_requirements() -> None:
    args = set(build_kit.PROFILE_B_CONFIGURE_ARGS)
    requirements = validator.PROFILE_REQUIREMENTS["B"]

    for encoder in requirements["encoders"]:
        assert f"--enable-encoder={encoder}" in args
    for decoder in requirements["decoders"]:
        assert f"--enable-decoder={decoder}" in args
    for demuxer in requirements["demuxers"]:
        configure_demuxer = "pcm_s16le" if demuxer == "s16le" else demuxer
        assert f"--enable-demuxer={configure_demuxer}" in args
    for muxer in requirements["muxers"]:
        configure_muxer = "pcm_s16le" if muxer == "s16le" else muxer
        assert f"--enable-muxer={configure_muxer}" in args
    for protocol in requirements["protocols"]:
        assert f"--enable-protocol={protocol}" in args
    for filter_name in requirements["filters"]:
        assert f"--enable-filter={filter_name}" in args

    assert "--enable-libopus" in args
    assert "--enable-libmp3lame" in args
    assert "--enable-parser=h264" in args
    assert "--enable-parser=hevc" in args
    assert "--disable-network" in args


def test_profile_b_configure_args_reject_disallowed_network_gpl_and_video_flags() -> None:
    args = set(build_kit.PROFILE_B_CONFIGURE_ARGS)

    assert not (args & build_kit.DISALLOWED_PROFILE_B_TOKENS)
    assert build_kit.validate_configure_args(list(args)) == []

    failures = build_kit.validate_configure_args([*args, "--enable-protocol=https", "--enable-gpl"])

    assert "disallowed configure flag: --enable-gpl" in failures
    assert "disallowed configure flag: --enable-protocol=https" in failures


def test_create_profile_b_build_kit_writes_reproducible_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "profile-b"

    payload = build_kit.create_build_kit(
        output_dir,
        source_url="https://example.invalid/ffmpeg.git",
        git_ref="test-ref",
    )

    args_file = output_dir / "configure-profile-b.args"
    script_file = output_dir / "configure-profile-b.sh"
    plan_file = output_dir / "profile-b-build-plan.json"

    assert payload["ok"] is True
    assert payload["profile"] == "B"
    assert payload["source"] == {
        "url": "https://example.invalid/ffmpeg.git",
        "gitRef": "test-ref",
    }
    assert args_file.exists()
    assert script_file.exists()
    assert plan_file.exists()

    args_text = args_file.read_text(encoding="utf-8")
    script_text = script_file.read_text(encoding="utf-8")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    assert "--enable-libmp3lame" in args_text
    assert "--enable-protocol=pipe" in args_text
    assert "--enable-protocol=https" not in args_text
    assert "./configure --prefix=\"$PREFIX_DIR\"" in script_text
    assert "+  --" not in script_text
    assert "--enable-demuxer=pcm_s16le" in script_text
    assert "build_profile_b_msys2.ps1" in json.dumps(plan)
    assert "-InstallDependencies" in json.dumps(plan)
    assert "validate_ffmpeg_profile.py" in json.dumps(plan)
    assert "--require-lgpl" in json.dumps(plan)
    assert "smoke_profile_b_fixtures.py" in json.dumps(plan)
    assert "smoke_media_preparation.py" in json.dumps(plan)
    assert "build_tauri_backend_sidecar.ps1" in json.dumps(plan)
