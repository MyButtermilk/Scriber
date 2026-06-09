from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


PROFILE_REQUIREMENTS: dict[str, dict[str, set[str]]] = {
    "A": {
        "encoders": {"libopus", "libmp3lame", "pcm_s16le"},
        "decoders": {
            "aac",
            "alac",
            "flac",
            "mp3",
            "opus",
            "pcm_f32le",
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_u8",
            "vorbis",
        },
        "demuxers": {"flac", "matroska", "mov", "mp3", "ogg", "s16le", "wav"},
        "muxers": {"mp3", "s16le", "webm"},
        "filters": {"aformat", "anull", "aresample", "pan"},
        "protocols": {"file", "pipe"},
    },
    "B": {
        "encoders": {"libopus", "libmp3lame", "pcm_s16le"},
        "decoders": {
            "aac",
            "alac",
            "flac",
            "mp3",
            "opus",
            "pcm_f32le",
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_u8",
            "vorbis",
        },
        "demuxers": {"flac", "matroska", "mov", "mp3", "ogg", "s16le", "wav"},
        "muxers": {"mp3", "s16le", "webm"},
        "filters": {"aformat", "anull", "aresample", "pan"},
        "protocols": {"file", "pipe"},
    },
}

PROFILE_PARSER_REQUIREMENTS: dict[str, set[str]] = {
    "A": {"aac", "flac", "mpegaudio", "opus", "vorbis"},
    "B": {"aac", "flac", "mpegaudio", "opus", "vorbis"},
}

PROFILE_ALLOWED_EXTRAS: dict[str, dict[str, set[str]]] = {
    "B": {
        "demuxers": {"concat", "mpegts"},
    }
}

SENSITIVE_GPL_FLAGS = {
    "--enable-gpl",
    "--enable-version3",
}
SENSITIVE_NONFREE_FLAGS = {
    "--enable-nonfree",
    "--enable-libfdk-aac",
}
EXCLUDED_FEATURE_MARKERS = {
    "--enable-ffplay",
    "--enable-libx264",
    "--enable-libx265",
    "--enable-cuda",
    "--enable-cuvid",
    "--enable-nvenc",
    "--enable-opencl",
    "--enable-vulkan",
}
NETWORK_PROTOCOLS = {"http", "https", "tls", "tcp", "udp", "rtmp", "rtmps"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_tool(names: list[str], *, media_tools_dir: Path | None = None, explicit: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if media_tools_dir:
        for name in names:
            candidates.append(media_tools_dir / name)
            if os.name == "nt" and not name.lower().endswith(".exe"):
                candidates.append(media_tools_dir / f"{name}.exe")
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def run_tool(path: Path, args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        [str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, "\n".join(part for part in (completed.stdout, completed.stderr) if part)


def file_info(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "exists": False}
    if not path.is_file():
        return {"path": str(path), "exists": False}
    data = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "sizeBytes": len(data),
        "sizeMiB": round(len(data) / (1024 * 1024), 2),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def parse_list_output(text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-") or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) == 1 and re.match(r"^[A-Za-z0-9_,.-]+$", parts[0]):
            names.add(parts[0])
            for alias in parts[0].split(","):
                if alias:
                    names.add(alias)
            continue
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if re.match(r"^[A-Za-z0-9_,.-]+$", name):
            names.add(name)
            for alias in name.split(","):
                if alias:
                    names.add(alias)
    return names


def parse_protocol_output(text: str) -> set[str]:
    names: set[str] = set()
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith(("input:", "output:")):
            in_section = True
            continue
        if not in_section:
            continue
        if re.match(r"^[A-Za-z0-9_.-]+$", line):
            names.add(line)
    return names


def parse_build_flags(buildconf: str) -> set[str]:
    flags: set[str] = set()
    for match in re.finditer(r"--[A-Za-z0-9][A-Za-z0-9_.=+-]*", buildconf):
        flags.add(match.group(0))
    return flags


def collect_capability_set(ffmpeg: Path, args: list[str], *, parser=parse_list_output) -> tuple[str, set[str]]:
    returncode, output = run_tool(ffmpeg, args)
    if returncode != 0:
        raise RuntimeError(f"{ffmpeg.name} {' '.join(args)} failed with exit code {returncode}: {output[:500]}")
    return output, parser(output)


def validate_requirements(profile: str, capabilities: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    requirements = PROFILE_REQUIREMENTS[profile]

    for category, required_names in requirements.items():
        present = capabilities.get(category, set())
        missing = sorted(required_names - present)
        if missing:
            failures.append(f"missing {category}: {', '.join(missing)}")

    protocols = capabilities.get("protocols", set())
    network_protocols = sorted(protocols & NETWORK_PROTOCOLS)
    if network_protocols:
        warnings.append(f"network protocols enabled: {', '.join(network_protocols)}")

    return failures, warnings


def build_manifest(
    *,
    ffmpeg: Path,
    ffprobe: Path | None,
    profile: str,
    require_lgpl: bool,
    strict_profile: bool,
) -> dict[str, Any]:
    ffmpeg_version_code, ffmpeg_version = run_tool(ffmpeg, ["-version"])
    ffmpeg_buildconf_code, ffmpeg_buildconf = run_tool(ffmpeg, ["-buildconf"])
    if ffmpeg_version_code != 0:
        raise RuntimeError(f"ffmpeg -version failed: {ffmpeg_version[:500]}")
    if ffmpeg_buildconf_code != 0:
        raise RuntimeError(f"ffmpeg -buildconf failed: {ffmpeg_buildconf[:500]}")

    ffprobe_version = ""
    ffprobe_version_code = None
    if ffprobe:
        ffprobe_version_code, ffprobe_version = run_tool(ffprobe, ["-version"])

    raw_outputs: dict[str, str] = {}
    capabilities: dict[str, set[str]] = {}
    for category, args, parser in (
        ("encoders", ["-hide_banner", "-v", "error", "-encoders"], parse_list_output),
        ("decoders", ["-hide_banner", "-v", "error", "-decoders"], parse_list_output),
        ("demuxers", ["-hide_banner", "-v", "error", "-demuxers"], parse_list_output),
        ("muxers", ["-hide_banner", "-v", "error", "-muxers"], parse_list_output),
        ("filters", ["-hide_banner", "-v", "error", "-filters"], parse_list_output),
        ("protocols", ["-hide_banner", "-v", "error", "-protocols"], parse_protocol_output),
    ):
        output, names = collect_capability_set(ffmpeg, args, parser=parser)
        raw_outputs[category] = output
        capabilities[category] = names

    failures, warnings = validate_requirements(profile, capabilities)
    build_flags = parse_build_flags(ffmpeg_buildconf)
    gpl_flags = sorted(build_flags & SENSITIVE_GPL_FLAGS)
    nonfree_flags = sorted(build_flags & SENSITIVE_NONFREE_FLAGS)
    excluded_markers = sorted(build_flags & EXCLUDED_FEATURE_MARKERS)

    if require_lgpl:
        if gpl_flags:
            failures.append(f"GPL/version3 flags present: {', '.join(gpl_flags)}")
        if nonfree_flags:
            failures.append(f"nonfree flags present: {', '.join(nonfree_flags)}")
    else:
        if gpl_flags:
            warnings.append(f"GPL/version3 flags present: {', '.join(gpl_flags)}")
        if nonfree_flags:
            warnings.append(f"nonfree flags present: {', '.join(nonfree_flags)}")

    if excluded_markers:
        warnings.append(f"excluded feature flags present: {', '.join(excluded_markers)}")

    if strict_profile:
        allowed_extras = PROFILE_ALLOWED_EXTRAS.get(profile, {})
        for category, present in capabilities.items():
            required = PROFILE_REQUIREMENTS[profile].get(category, set())
            allowed = allowed_extras.get(category, set())
            if category == "protocols":
                extras = present - required
                if extras:
                    failures.append(f"strict profile has extra protocols: {', '.join(sorted(extras))}")
            elif category in PROFILE_REQUIREMENTS[profile]:
                extras = present - required - allowed
                if extras:
                    warnings.append(f"strict profile has extra {category}: {', '.join(sorted(extras)[:30])}")

    ffmpeg_info = file_info(ffmpeg)
    ffprobe_info = file_info(ffprobe)
    total_bytes = int(ffmpeg_info.get("sizeBytes") or 0) + int(ffprobe_info.get("sizeBytes") or 0)

    return {
        "apiVersion": "1",
        "ok": not failures,
        "generatedAt": utc_now(),
        "profile": profile,
        "strictProfile": strict_profile,
        "requireLgpl": require_lgpl,
        "tools": {
            "ffmpeg": ffmpeg_info,
            "ffprobe": ffprobe_info,
            "totalSizeBytes": total_bytes,
            "totalSizeMiB": round(total_bytes / (1024 * 1024), 2),
        },
        "license": {
            "buildFlags": sorted(build_flags),
            "gplFlags": gpl_flags,
            "nonfreeFlags": nonfree_flags,
            "excludedFeatureFlags": excluded_markers,
            "requiresHumanReview": bool(gpl_flags or nonfree_flags),
        },
        "capabilities": {category: sorted(names) for category, names in capabilities.items()},
        "requirements": {category: sorted(names) for category, names in PROFILE_REQUIREMENTS[profile].items()},
        "parserRequirements": sorted(PROFILE_PARSER_REQUIREMENTS.get(profile, set())),
        "validationNotes": [
            "FFmpeg does not expose a portable parser-list CLI command; parser coverage is validated through configure flags and media smoke fixtures.",
            "PCM output remains stdout-only for file pipeline decoding and is not used as an upload artifact.",
        ],
        "failures": failures,
        "warnings": warnings,
        "raw": {
            "ffmpegVersion": ffmpeg_version,
            "ffmpegBuildconf": ffmpeg_buildconf,
            "ffprobeVersion": ffprobe_version,
            "ffprobeVersionExitCode": ffprobe_version_code,
            "capabilityOutputs": raw_outputs,
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and describe a Scriber FFmpeg profile candidate.")
    parser.add_argument("--media-tools-dir", type=Path, default=None)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ffprobe", type=Path, default=None)
    parser.add_argument("--profile", choices=sorted(PROFILE_REQUIREMENTS), default="B")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "tmp" / "ffmpeg-profile-manifest.json")
    parser.add_argument("--require-lgpl", action="store_true")
    parser.add_argument("--strict-profile", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args(argv)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_summary(payload: dict[str, Any], output_path: Path, *, print_json: bool = False) -> None:
    if print_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    status = "OK" if payload.get("ok") is True else "FAILED"
    profile = payload.get("profile", "?")
    print(f"FFmpeg profile {profile} {status}; manifest: {output_path}")
    tools = payload.get("tools")
    if isinstance(tools, dict):
        total_size = tools.get("totalSizeMiB")
        if total_size is not None:
            print(f"Media tools size: {total_size} MiB")
    for failure in payload.get("failures", []):
        print(f"failure: {failure}")
    for warning in payload.get("warnings", [])[:10]:
        print(f"warning: {warning}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    ffmpeg = resolve_tool(["ffmpeg"], media_tools_dir=args.media_tools_dir, explicit=args.ffmpeg)
    ffprobe = resolve_tool(["ffprobe"], media_tools_dir=args.media_tools_dir, explicit=args.ffprobe)

    if ffmpeg is None:
        payload = {
            "apiVersion": "1",
            "ok": False,
            "generatedAt": utc_now(),
            "profile": args.profile,
            "failures": ["ffmpeg was not found"],
            "warnings": [],
        }
        write_json(args.output, payload)
        print_summary(payload, args.output, print_json=args.print_json)
        return 1

    try:
        payload = build_manifest(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            profile=args.profile,
            require_lgpl=args.require_lgpl,
            strict_profile=args.strict_profile,
        )
    except Exception as exc:
        payload = {
            "apiVersion": "1",
            "ok": False,
            "generatedAt": utc_now(),
            "profile": args.profile,
            "tools": {
                "ffmpeg": file_info(ffmpeg),
                "ffprobe": file_info(ffprobe),
            },
            "failures": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
        }

    write_json(args.output, payload)
    print_summary(payload, args.output, print_json=args.print_json)
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
