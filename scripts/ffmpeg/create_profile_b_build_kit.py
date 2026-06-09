from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]

PROFILE_B_CONFIGURE_ARGS = [
    "--enable-small",
    "--disable-everything",
    "--disable-autodetect",
    "--disable-debug",
    "--disable-doc",
    "--disable-network",
    "--enable-ffmpeg",
    "--enable-ffprobe",
    "--disable-ffplay",
    "--enable-protocol=file",
    "--enable-protocol=pipe",
    "--enable-demuxer=mp3",
    "--enable-demuxer=wav",
    "--enable-demuxer=mov",
    "--enable-demuxer=matroska",
    "--enable-demuxer=ogg",
    "--enable-demuxer=flac",
    "--enable-demuxer=pcm_s16le",
    "--enable-demuxer=mpegts",
    "--enable-demuxer=concat",
    "--enable-muxer=webm",
    "--enable-muxer=mp3",
    "--enable-muxer=pcm_s16le",
    "--enable-decoder=mp3",
    "--enable-decoder=aac",
    "--enable-decoder=opus",
    "--enable-decoder=vorbis",
    "--enable-decoder=flac",
    "--enable-decoder=alac",
    "--enable-decoder=pcm_s16le",
    "--enable-decoder=pcm_s24le",
    "--enable-decoder=pcm_s32le",
    "--enable-decoder=pcm_f32le",
    "--enable-decoder=pcm_u8",
    "--enable-libopus",
    "--enable-encoder=libopus",
    "--enable-libmp3lame",
    "--enable-encoder=libmp3lame",
    "--enable-encoder=pcm_s16le",
    "--enable-parser=mpegaudio",
    "--enable-parser=aac",
    "--enable-parser=opus",
    "--enable-parser=vorbis",
    "--enable-parser=flac",
    "--enable-parser=h264",
    "--enable-parser=hevc",
    "--enable-filter=aresample",
    "--enable-filter=aformat",
    "--enable-filter=anull",
    "--enable-filter=pan",
]

DISALLOWED_PROFILE_B_TOKENS = {
    "--enable-gpl",
    "--enable-version3",
    "--enable-nonfree",
    "--enable-protocol=http",
    "--enable-protocol=https",
    "--enable-protocol=tcp",
    "--enable-protocol=tls",
    "--enable-protocol=udp",
    "--enable-libx264",
    "--enable-libx265",
    "--enable-cuda",
    "--enable-cuvid",
    "--enable-nvenc",
    "--enable-opencl",
    "--enable-vulkan",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def validate_configure_args(args: list[str]) -> list[str]:
    failures: list[str] = []
    seen = set(args)
    for token in sorted(DISALLOWED_PROFILE_B_TOKENS & seen):
        failures.append(f"disallowed configure flag: {token}")

    required = {
        "--disable-network",
        "--disable-ffplay",
        "--enable-ffmpeg",
        "--enable-ffprobe",
        "--enable-protocol=file",
        "--enable-protocol=pipe",
        "--enable-demuxer=pcm_s16le",
        "--enable-libopus",
        "--enable-libmp3lame",
        "--enable-encoder=libopus",
        "--enable-encoder=libmp3lame",
        "--enable-encoder=pcm_s16le",
        "--enable-muxer=pcm_s16le",
        "--enable-parser=h264",
        "--enable-parser=hevc",
        "--enable-demuxer=mpegts",
        "--enable-demuxer=concat",
    }
    for token in sorted(required - seen):
        failures.append(f"missing required configure flag: {token}")
    return failures


def shell_script(configure_args: list[str]) -> str:
    line_continuation = " \\\n  "
    quoted_args = line_continuation.join(shlex.quote(arg) for arg in configure_args)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${{1:-.}}"
PREFIX_DIR="${{2:-$PWD/dist/scriber-ffmpeg-profile-b}}"
JOBS="${{JOBS:-$(nproc 2>/dev/null || echo 2)}}"

cd "$SOURCE_DIR"
./configure --prefix="$PREFIX_DIR" \\
  {quoted_args}
make -j"$JOBS"
make install

echo "Profile B FFmpeg install completed: $PREFIX_DIR"
echo "Run the Scriber validator against: $PREFIX_DIR/bin"
"""


def build_plan(
    *,
    output_dir: Path,
    source_url: str,
    git_ref: str,
    configure_args: list[str],
) -> dict[str, Any]:
    media_tools_placeholder = "<profile-b-prefix>/bin"
    return {
        "apiVersion": "1",
        "ok": True,
        "generatedAt": utc_now(),
        "profile": "B",
        "source": {
            "url": source_url,
            "gitRef": git_ref,
        },
        "outputDir": str(output_dir.resolve()),
        "configureArgsFile": repo_relative(output_dir / "configure-profile-b.args"),
        "configureScript": repo_relative(output_dir / "configure-profile-b.sh"),
        "configureArgs": configure_args,
        "buildRunner": {
            "command": [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts/ffmpeg/build_profile_b_msys2.ps1",
                "-BuildRoot",
                "build/ffmpeg-profile-b-msys2",
                "-SourceUrl",
                source_url,
                "-GitRef",
                git_ref,
                "-InstallDependencies",
            ],
        },
        "postBuildValidation": [
            {
                "name": "profile_manifest",
                "command": [
                    "python",
                    "scripts/ffmpeg/validate_ffmpeg_profile.py",
                    "--media-tools-dir",
                    media_tools_placeholder,
                    "--profile",
                    "B",
                    "--require-lgpl",
                    "--output",
                    "tmp/ffmpeg-profile-b-manifest.json",
                ],
            },
            {
                "name": "profile_b_fixture_smoke",
                "command": [
                    "python",
                    "scripts/ffmpeg/smoke_profile_b_fixtures.py",
                    "--media-tools-dir",
                    media_tools_placeholder,
                    "--require-ffprobe",
                    "--output",
                    "tmp/ffmpeg-profile-b-fixtures.json",
                ],
            },
            {
                "name": "media_preparation_smoke",
                "command": [
                    "python",
                    "scripts/smoke_media_preparation.py",
                    "--media-tools-dir",
                    media_tools_placeholder,
                    "--require-ffprobe",
                    "--output",
                    "tmp/media-preparation-smoke-profile-b.json",
                ],
            },
            {
                "name": "sidecar_candidate_gate",
                "command": [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    "scripts/build_tauri_backend_sidecar.ps1",
                    "-SkipFrontendBuild",
                    "-BundleMediaTools",
                    "-ValidateSlimMediaTools",
                    "-MediaToolsDir",
                    media_tools_placeholder,
                    "-CopyToTauriRelease",
                ],
            },
        ],
        "notes": [
            "The helper generates a deterministic Profile B build kit; it does not download FFmpeg sources or vendor binaries.",
            "Run configure-profile-b.sh from an FFmpeg source checkout with libopus and libmp3lame development dependencies available.",
            "Do not accept a produced binary for release until the profile manifest, Profile B fixture smoke, media-preparation smoke, sidecar gate, and installed-package smokes pass.",
        ],
    }


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def create_build_kit(output_dir: Path, *, source_url: str, git_ref: str) -> dict[str, Any]:
    configure_args = list(PROFILE_B_CONFIGURE_ARGS)
    failures = validate_configure_args(configure_args)
    if failures:
        raise RuntimeError("; ".join(failures))

    output_dir.mkdir(parents=True, exist_ok=True)
    write_text(output_dir / "configure-profile-b.args", "\n".join(configure_args) + "\n")
    write_text(output_dir / "configure-profile-b.sh", shell_script(configure_args))
    payload = build_plan(
        output_dir=output_dir,
        source_url=source_url,
        git_ref=git_ref,
        configure_args=configure_args,
    )
    write_json(output_dir / "profile-b-build-plan.json", payload)
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a reproducible Scriber FFmpeg Profile B build kit.")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "build" / "ffmpeg-profile-b")
    parser.add_argument("--source-url", default="https://git.ffmpeg.org/ffmpeg.git")
    parser.add_argument("--git-ref", default="n7.0")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        payload = create_build_kit(
            args.output_dir,
            source_url=args.source_url,
            git_ref=args.git_ref,
        )
    except Exception as exc:
        print(f"FFmpeg Profile B build kit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"FFmpeg Profile B build kit written: {args.output_dir}")
        print(f"Build plan: {args.output_dir / 'profile-b-build-plan.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
