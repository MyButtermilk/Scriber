"""Create run-local, real Deno/yt-dlp evidence for installer-size holdouts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse


SNAPSHOT_CONTRACT = "InstallerSizeYoutubeHoldoutsV1"
PROBE_CONTRACT = "InstallerSizeYoutubeHoldoutProbeV1"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROBE_ENVIRONMENT_REMOVALS = frozenset(
    {
        "BUN_INSTALL",
        "DENO_AUTH_TOKENS",
        "DENO_INSTALL",
        "NODE_OPTIONS",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "YTDLP_CONFIG",
        "YTDLP_NO_LAZY_EXTRACTORS",
        "YT_DLP_CONFIG",
    }
)


class HoldoutError(RuntimeError):
    """Raised when a holdout cannot be attested without guessing."""


def pinned_deno_version(stdout: str) -> str:
    """Normalize the semantic version from Deno's stable first line."""

    lines = str(stdout or "").splitlines()
    first_line = lines[0].strip() if lines else ""
    match = re.fullmatch(
        r"deno\s+(\d+\.\d+\.\d+)(?:\s+\([^\r\n]+\))?",
        first_line,
    )
    if match is None or match.group(1) != "2.9.2":
        raise HoldoutError("Deno runtime did not report the pinned 2.9.2 version")
    return match.group(1)


def require_baseline_environment_root(run_root: Path, active_prefix: Path) -> Path:
    """Bind the probe process to this run's exact baseline environment."""

    expected = (run_root / "environments" / "baseline" / ".venv").resolve(
        strict=True
    )
    active = Path(active_prefix).resolve(strict=True)
    if active != expected:
        raise HoldoutError(
            "holdout probe must run inside this RunId's baseline environment"
        )
    return active


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_file(path: Path, *, label: str) -> Path:
    # Inspect the directory entry before resolving it.  Resolving first would
    # make a symlink look like the plain file it targets and defeat this gate.
    candidate = Path(os.path.abspath(path))
    candidate_info = candidate.lstat()
    if candidate.is_symlink() or bool(
        getattr(candidate_info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise HoldoutError(f"{label} must be a plain file")
    resolved = candidate.resolve(strict=True)
    resolved_info = resolved.lstat()
    if not resolved.is_file() or resolved.is_symlink() or bool(
        getattr(resolved_info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise HoldoutError(f"{label} must be a plain file")
    return resolved


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HoldoutError(f"{label} must contain an object")
    return value


def _write_immutable(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise HoldoutError(f"immutable evidence already exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _distribution_identity(name: str, *, environment_root: Path) -> dict[str, Any]:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise HoldoutError(f"required distribution is unavailable: {name}") from exc
    entries: list[dict[str, Any]] = []
    for item in distribution.files or ():
        located = _plain_file(
            Path(distribution.locate_file(item)),
            label=f"distribution {name} file",
        )
        try:
            relative = located.relative_to(environment_root).as_posix()
        except ValueError as exc:
            raise HoldoutError(f"distribution {name} escaped the research environment") from exc
        entries.append(
            {
                "path": relative,
                "length": located.stat().st_size,
                "sha256": _sha256_file(located),
            }
        )
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    if not entries:
        raise HoldoutError(f"distribution {name} has no attestable files")
    return {
        "name": str(distribution.metadata.get("Name") or name).casefold().replace("_", "-"),
        "version": distribution.version,
        "fileCount": len(entries),
        "contentSha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
    }


def _video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    if parsed.path.startswith("/shorts/"):
        return parsed.path.split("/", 3)[2]
    return str(parse_qs(parsed.query).get("v", [""])[0])


def observed_capabilities(
    *,
    family: str,
    url: str,
    info: dict[str, Any],
    debug_log: str,
) -> tuple[list[str], dict[str, Any]]:
    formats = info.get("formats")
    if not isinstance(formats, list):
        formats = []
    audio_formats = [
        item
        for item in formats
        if isinstance(item, dict)
        and item.get("acodec") not in (None, "none")
        and isinstance(item.get("url"), str)
        and item["url"].startswith("https://")
    ]
    query_keys: set[str] = set()
    for item in formats:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        query_keys.update(parse_qs(urlparse(item["url"]).query))
    capabilities: set[str] = {"deno-runtime"}
    if isinstance(info.get("id"), str) and info.get("extractor_key") == "Youtube":
        capabilities.add("metadata")
    if audio_formats:
        capabilities.add("audio-format-url")
    lower_debug = debug_log.casefold()
    if "downloading player" in lower_debug or "forcing \"main\" player js" in lower_debug:
        capabilities.add("player-js")
    if "[jsc:deno] solving js challenges using deno" in lower_debug:
        capabilities.add("deno-jsc")
    if "sig" in query_keys or "signature" in query_keys:
        capabilities.add("signature")
    # A successful yt-dlp extraction normally returns the transformed media
    # URL, so a pre-transform `n` parameter is not reliable post-run evidence.
    # Bind the capability to the observable Deno JSC execution, player script,
    # and signed media result instead of inventing a challenge subtype.
    if {"deno-jsc", "player-js", "signature"}.issubset(capabilities):
        capabilities.add("js-challenge-solved")
    if family == "shorts" and urlparse(url).path.startswith("/shorts/"):
        capabilities.add("shorts-route")
    if family == "music" and urlparse(url).hostname == "music.youtube.com":
        capabilities.add("music-route")
    subtitle_count = len(info.get("subtitles") or {})
    automatic_caption_count = len(info.get("automatic_captions") or {})
    if subtitle_count or automatic_caption_count:
        capabilities.add("manual-or-automatic-captions")
    if info.get("live_status") == "was_live" and info.get("was_live") is True:
        capabilities.add("completed-live-replay-shape")
    observations = {
        "formatCount": len(formats),
        "audioFormatCount": len(audio_formats),
        "subtitleLanguageCount": subtitle_count,
        "automaticCaptionLanguageCount": automatic_caption_count,
        "hasSignatureQuery": "sig" in query_keys or "signature" in query_keys,
        "hasNQuery": "n" in query_keys,
        "denoJscObserved": "deno-jsc" in capabilities,
        "liveStatus": info.get("live_status"),
    }
    return sorted(capabilities), observations


def _sanitized_probe_environment(
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if inherited is None else inherited)
    for name in PROBE_ENVIRONMENT_REMOVALS:
        environment.pop(name, None)
    environment.update(
        {
            "DENO_NO_UPDATE_CHECK": "1",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "YTDLP_NO_PLUGINS": "1",
        }
    )
    return environment


def _probe_policy(
    *,
    command: list[str],
    environment: Mapping[str, str],
    deno_executable: Path,
) -> dict[str, bool]:
    required_switches = (
        "--no-config",
        "--no-plugin-dirs",
        "--no-cache-dir",
        "--no-js-runtimes",
        "--no-remote-components",
    )
    if any(command.count(switch) != 1 for switch in required_switches):
        raise HoldoutError("yt-dlp Deno probe policy switches are not exact")
    try:
        runtime_index = command.index("--js-runtimes")
        runtime_value = command[runtime_index + 1]
    except (ValueError, IndexError) as exc:
        raise HoldoutError("yt-dlp Deno runtime policy is missing") from exc
    if (
        command.count("--js-runtimes") != 1
        or runtime_value != f"deno:{deno_executable}"
    ):
        raise HoldoutError("yt-dlp Deno runtime policy is not exact")
    if any(name in environment for name in PROBE_ENVIRONMENT_REMOVALS):
        raise HoldoutError("yt-dlp Deno probe inherited an unsafe environment")
    required_environment = {
        "DENO_NO_UPDATE_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "YTDLP_NO_PLUGINS": "1",
    }
    if any(
        environment.get(name) != value
        for name, value in required_environment.items()
    ):
        raise HoldoutError("yt-dlp Deno probe environment policy is not exact")
    return {
        "configDiscovery": False,
        "download": False,
        "explicitSingleRuntime": True,
        "externalPlugins": False,
        "pythonPathInheritance": False,
        "pythonUserSite": False,
        "remoteComponents": False,
        "ytDlpCache": False,
    }


def _run_probe(
    *,
    url: str,
    deno_executable: Path,
    timeout_seconds: int,
    inherited_environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str, dict[str, bool]]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--verbose",
        "--no-config",
        "--no-plugin-dirs",
        "--no-playlist",
        "--no-cache-dir",
        "--no-js-runtimes",
        "--js-runtimes",
        f"deno:{deno_executable}",
        "--no-remote-components",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "-f",
        "bestaudio/best",
        "-J",
        url,
    ]
    environment = _sanitized_probe_environment(inherited_environment)
    policy = _probe_policy(
        command=command,
        environment=environment,
        deno_executable=deno_executable,
    )
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise HoldoutError("yt-dlp Deno probe failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HoldoutError("yt-dlp Deno probe did not emit one JSON object") from exc
    if not isinstance(payload, dict):
        raise HoldoutError("yt-dlp Deno probe payload is not an object")
    return payload, completed.stderr, policy


def _canonical_run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise HoldoutError("RunId must be a canonical RFC 4122 UUID") from exc
    if value != str(parsed) or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise HoldoutError("RunId must be a canonical non-nil RFC 4122 UUID")
    return str(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        run_id = _canonical_run_id(args.run_id)
        if not 30 <= args.timeout_seconds <= 300:
            raise HoldoutError("timeout must be between 30 and 300 seconds")
        repo_root = args.repo_root.resolve(strict=True)
        run_root = (repo_root / "autoresearch-results" / "installer-size" / run_id).resolve()
        expected_parent = (repo_root / "autoresearch-results" / "installer-size").resolve()
        if run_root.parent != expected_parent or not run_root.is_dir():
            raise HoldoutError("run root is missing or escaped its namespace")
        fixture_path = _plain_file(
            repo_root / "scripts" / "perf" / "profiles" / "installer-size" / "youtube-holdouts.json",
            label="holdout fixture",
        )
        fixture = _load_object(fixture_path, label="holdout fixture")
        environment_manifest_path = _plain_file(
            run_root / "environments" / "baseline" / "environment-manifest.json",
            label="baseline environment manifest",
        )
        environment_manifest = _load_object(
            environment_manifest_path, label="baseline environment manifest"
        )
        if (
            environment_manifest.get("kind")
            != "scriber-installer-research-python-environment"
            or environment_manifest.get("runId") != run_id
            or environment_manifest.get("environmentName") != "baseline"
        ):
            raise HoldoutError("baseline environment manifest identity mismatch")
        environment_root = require_baseline_environment_root(
            run_root,
            Path(sys.prefix),
        )
        python_executable = _plain_file(Path(sys.executable), label="research Python")
        python_identity = environment_manifest.get("python")
        if not isinstance(python_identity, dict) or (
            python_executable.stat().st_size != python_identity.get("length")
            or _sha256_file(python_executable) != python_identity.get("sha256")
        ):
            raise HoldoutError("active Python differs from the baseline environment manifest")
        try:
            import deno
        except ImportError as exc:
            raise HoldoutError("the locked Deno package is unavailable") from exc
        deno_executable = _plain_file(Path(deno.find_deno_bin()), label="Deno runtime")
        try:
            deno_executable.relative_to(environment_root)
        except ValueError as exc:
            raise HoldoutError("Deno runtime escaped the baseline environment") from exc
        deno_version = subprocess.run(
            [str(deno_executable), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if deno_version.returncode != 0:
            raise HoldoutError("Deno runtime did not report the pinned 2.9.2 version")
        normalized_deno_version = pinned_deno_version(deno_version.stdout)
        runtime_identity = {
            "name": "deno",
            "version": normalized_deno_version,
            "length": deno_executable.stat().st_size,
            "sha256": _sha256_file(deno_executable),
        }
        distribution_identities = {
            name: _distribution_identity(name, environment_root=environment_root)
            for name in ("deno", "yt-dlp", "yt-dlp-ejs")
        }
        rows = fixture.get("cases")
        if not isinstance(rows, list) or len(rows) != 6:
            raise HoldoutError("holdout fixture must contain exactly six cases")
        urls: set[str] = set()
        video_ids: set[str] = set()
        snapshot_cases: list[dict[str, Any]] = []
        pending_probes: list[tuple[str, dict[str, Any]]] = []
        snapshot_policy: dict[str, bool] | None = None
        captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        probes_dir = run_root / "preflight" / "youtube-holdout-probes"
        for row in rows:
            if not isinstance(row, dict):
                raise HoldoutError("holdout case must be an object")
            case_id = str(row.get("id") or "")
            family = str(row.get("family") or "")
            url = str(row.get("url") or "")
            required = row.get("requiredCapabilities")
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", case_id)
                or not url.startswith("https://")
                or not isinstance(required, list)
                or not required
                or url in urls
            ):
                raise HoldoutError(f"holdout fixture case is unsafe or duplicated: {case_id}")
            urls.add(url)
            expected_video_id = _video_id_from_url(url)
            if not expected_video_id or expected_video_id in video_ids:
                raise HoldoutError(f"holdout video id is missing or duplicated: {case_id}")
            info, debug_log, policy = _run_probe(
                url=url,
                deno_executable=deno_executable,
                timeout_seconds=args.timeout_seconds,
            )
            if snapshot_policy is None:
                snapshot_policy = policy
            elif policy != snapshot_policy:
                raise HoldoutError("yt-dlp Deno probe policy changed between cases")
            if info.get("id") != expected_video_id:
                raise HoldoutError(f"yt-dlp returned another video for {case_id}")
            video_ids.add(expected_video_id)
            observed, observations = observed_capabilities(
                family=family, url=url, info=info, debug_log=debug_log
            )
            missing = sorted(set(required) - set(observed))
            if missing:
                raise HoldoutError(
                    f"Deno probe lacks required capabilities for {case_id}: {', '.join(missing)}"
                )
            probe = {
                "probeContract": PROBE_CONTRACT,
                "schemaVersion": 1,
                "runId": run_id,
                "fixtureId": fixture.get("fixtureId"),
                "caseId": case_id,
                "family": family,
                "url": url,
                "videoId": expected_video_id,
                "capturedAtUtc": captured_at,
                "status": "pass",
                "observedCapabilities": observed,
                "observations": observations,
                "runtime": runtime_identity,
                "distributions": distribution_identities,
                "policy": policy,
            }
            pending_probes.append((case_id, probe))
            snapshot_cases.append(
                {
                    "id": case_id,
                    "family": family,
                    "url": url,
                    "videoId": expected_video_id,
                    "status": "validated",
                    "observedCapabilities": observed,
                    "denoProbe": "pass",
                }
            )
        if probes_dir.exists():
            raise HoldoutError("immutable holdout probe directory already exists")
        probes_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_probes = Path(
            tempfile.mkdtemp(prefix=".youtube-holdout-probes.", dir=str(probes_dir.parent))
        )
        try:
            for case_id, probe in pending_probes:
                _write_immutable(temporary_probes / f"{case_id}.json", probe)
            os.replace(temporary_probes, probes_dir)
        finally:
            if temporary_probes.exists():
                shutil.rmtree(temporary_probes)
        snapshot_by_id = {item["id"]: item for item in snapshot_cases}
        for case_id, _probe in pending_probes:
            snapshot_by_id[case_id]["probeEvidenceSha256"] = _sha256_file(
                probes_dir / f"{case_id}.json"
            )
        snapshot = {
            "holdoutSnapshotContract": SNAPSHOT_CONTRACT,
            "schemaVersion": 1,
            "runId": run_id,
            "fixtureId": fixture.get("fixtureId"),
            "fixtureSha256": _sha256_file(fixture_path),
            "capturedAtUtc": captured_at,
            "runtime": runtime_identity,
            "distributions": distribution_identities,
            "policy": snapshot_policy,
            "cases": snapshot_cases,
        }
        output = run_root / "preflight" / "youtube-holdouts.snapshot.json"
        _write_immutable(output, snapshot)
        print(json.dumps({"ok": True, "runId": run_id, "caseCount": len(snapshot_cases)}))
        return 0
    except (HoldoutError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
