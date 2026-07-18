"""Bounded YouTube capability probe executed by the frozen backend launcher.

This module is deliberately independent of Scriber application code.  It is
frozen into ``scriber-backend.exe`` and is used only by installer-size
AutoResearch.  Requests arrive as one bounded JSON object on stdin; responses
never include the requested URL, media URLs, player URLs, paths, logs, or raw
provider errors.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse


PROBE_CONTRACT = "InstallerYoutubeFrozenHoldoutProbeV1"
SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_LOG_MESSAGES = 256
MAX_LOG_CHARACTERS = 256 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{6,32}$")
RUNTIME_NAMES = {
    "deno": {"deno", "deno.exe"},
    "quickjs": {"qjs", "qjs.exe", "qjs-ng", "qjs-ng.exe", "quickjs", "quickjs.exe"},
}


class ProbeBoundaryError(RuntimeError):
    """A request cannot be executed without weakening the probe boundary."""


class _BoundedLogger:
    def __init__(self) -> None:
        self._messages: list[str] = []
        self._characters = 0

    def _append(self, value: object) -> None:
        if len(self._messages) >= MAX_LOG_MESSAGES or self._characters >= MAX_LOG_CHARACTERS:
            return
        text = str(value)[:2048]
        remaining = MAX_LOG_CHARACTERS - self._characters
        text = text[:remaining]
        self._messages.append(text)
        self._characters += len(text)

    def debug(self, value: object) -> None:
        self._append(value)

    def warning(self, value: object) -> None:
        self._append(value)

    def error(self, value: object) -> None:
        self._append(value)

    @property
    def text(self) -> str:
        return "\n".join(self._messages)


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def _plain_file_below(root: Path, raw_path: object, *, runtime_kind: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\0" in raw_path:
        raise ProbeBoundaryError("runtime path is invalid")
    root_entry = Path(os.path.abspath(root))
    if _is_reparse(root_entry) or not root_entry.is_dir():
        raise ProbeBoundaryError("runtime root is not plain")
    root = root_entry.resolve(strict=True)
    candidate_entry = Path(os.path.abspath(raw_path))
    try:
        relative = candidate_entry.relative_to(root)
    except ValueError as exc:
        raise ProbeBoundaryError("runtime escaped the frozen backend root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise ProbeBoundaryError("runtime path contains a reparse point")
    candidate = candidate_entry.resolve(strict=True)
    expected_parent = (root / "tools" / "ffmpeg").resolve(strict=True)
    if candidate.parent != expected_parent or not candidate.is_file():
        raise ProbeBoundaryError("runtime is outside the media-tool directory")
    if candidate.name.casefold() not in RUNTIME_NAMES[runtime_kind]:
        raise ProbeBoundaryError("runtime filename does not match its kind")
    return candidate


def _load_request(stream: Any) -> dict[str, Any]:
    payload = stream.read(MAX_REQUEST_BYTES + 1)
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_REQUEST_BYTES:
        raise ProbeBoundaryError("probe request size is invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeBoundaryError("probe request is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProbeBoundaryError("probe request must be an object")
    return value


def _validate_request(value: Mapping[str, Any], runtime_root: Path) -> dict[str, Any]:
    allowed = {
        "requestContract",
        "schemaVersion",
        "caseId",
        "family",
        "url",
        "expectedVideoId",
        "runtimeKind",
        "runtimePath",
        "cacheMode",
    }
    if set(value) != allowed:
        raise ProbeBoundaryError("probe request fields are not exact")
    case_id = value.get("caseId")
    family = value.get("family")
    url = value.get("url")
    video_id = value.get("expectedVideoId")
    runtime_kind = value.get("runtimeKind")
    cache_mode = value.get("cacheMode")
    if (
        value.get("requestContract") != PROBE_CONTRACT
        or value.get("schemaVersion") != SCHEMA_VERSION
        or not isinstance(case_id, str)
        or not CASE_ID_RE.fullmatch(case_id)
        or not isinstance(family, str)
        or not CASE_ID_RE.fullmatch(family)
        or not isinstance(video_id, str)
        or not VIDEO_ID_RE.fullmatch(video_id)
        or runtime_kind not in RUNTIME_NAMES
        or cache_mode not in {"cold", "warm"}
    ):
        raise ProbeBoundaryError("probe request identity is invalid")
    if not isinstance(url, str) or len(url) > 2048:
        raise ProbeBoundaryError("probe URL is invalid")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.youtube.com", "youtube.com", "music.youtube.com", "youtu.be"}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ProbeBoundaryError("probe URL is outside the frozen YouTube holdout scope")
    runtime_path = _plain_file_below(
        runtime_root, value.get("runtimePath"), runtime_kind=str(runtime_kind)
    )
    cache_root: Path | None = None
    raw_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
    if cache_mode == "warm":
        if not raw_cache:
            raise ProbeBoundaryError("warm probe cache is missing")
        cache_root = Path(os.path.abspath(raw_cache))
        if _is_reparse(cache_root) or not cache_root.is_dir():
            raise ProbeBoundaryError("warm probe cache is not a plain directory")
        cache_root = cache_root.resolve(strict=True)
    return {
        "caseId": case_id,
        "family": family,
        "url": url,
        "expectedVideoId": video_id,
        "runtimeKind": runtime_kind,
        "runtimePath": runtime_path,
        "cacheMode": cache_mode,
        "cacheRoot": cache_root,
    }


def _failure_code(value: object) -> str:
    text = str(value).casefold()[:8192]
    rules = (
        ("http_429", ("http error 429", "too many requests")),
        ("http_403", ("http error 403", "forbidden")),
        ("login_required", ("sign in", "login required", "confirm you’re not a bot")),
        ("geo_restricted", ("not available in your country", "geo-restricted")),
        ("media_unavailable", ("video unavailable", "private video", "has been removed")),
        ("network_timeout", ("timed out", "timeout", "read operation timed out")),
        ("tls_failure", ("certificate verify failed", "ssl", "tls")),
        ("dns_failure", ("name resolution", "getaddrinfo", "could not resolve")),
        ("extractor_error", ("extractor error", "unable to extract")),
    )
    for code, markers in rules:
        if any(marker in text for marker in markers):
            return code
    return "unknown_failure"


def _capabilities(
    *, request: Mapping[str, Any], info: Mapping[str, Any], debug_log: str
) -> list[str]:
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
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            query_keys.update(parse_qs(urlparse(item["url"]).query))
    capabilities = {"js-runtime"}
    if isinstance(info.get("id"), str) and info.get("extractor_key") == "Youtube":
        capabilities.add("metadata")
    if audio_formats:
        capabilities.add("audio-format-url")
    lower_debug = debug_log.casefold()
    if "downloading player" in lower_debug or 'forcing "main" player js' in lower_debug:
        capabilities.add("player-js")
    runtime_kind = str(request["runtimeKind"])
    if f"solving js challenges using {runtime_kind}" in lower_debug:
        capabilities.add("js-challenge-runtime")
    if "sig" in query_keys or "signature" in query_keys:
        capabilities.add("signature")
    if {"js-challenge-runtime", "player-js", "signature"}.issubset(capabilities):
        capabilities.add("js-challenge-solved")
    parsed_url = urlparse(str(request["url"]))
    if request["family"] == "shorts" and parsed_url.path.startswith("/shorts/"):
        capabilities.add("shorts-route")
    if request["family"] == "music" and parsed_url.hostname == "music.youtube.com":
        capabilities.add("music-route")
    if len(info.get("subtitles") or {}) or len(info.get("automatic_captions") or {}):
        capabilities.add("manual-or-automatic-captions")
    if info.get("live_status") == "was_live" and info.get("was_live") is True:
        capabilities.add("completed-live-replay-shape")
    return sorted(capabilities)


def _response_base(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "probeContract": PROBE_CONTRACT,
        "schemaVersion": SCHEMA_VERSION,
        "caseId": request["caseId"],
        "runtimeKind": request["runtimeKind"],
        "ytDlpVersion": importlib.metadata.version("yt-dlp"),
        "ejsVersion": importlib.metadata.version("yt-dlp-ejs"),
        "policy": {
            "configDiscovery": False,
            "externalPlugins": False,
            "remoteComponents": False,
            "download": False,
            "explicitSingleRuntime": True,
        },
    }


def execute_probe(
    request: Mapping[str, Any],
    *,
    runtime_root: Path,
    parse_options: Callable[[list[str]], Any] | None = None,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = _validate_request(request, runtime_root)
    if os.environ.get("YTDLP_NO_PLUGINS") != "1":
        raise ProbeBoundaryError("external yt-dlp plugins are not disabled")
    # This import is part of the probe: metadata alone would not prove that the
    # EJS package was actually frozen and importable in this backend binary.
    import yt_dlp_ejs  # noqa: F401

    if parse_options is None or ydl_factory is None:
        import yt_dlp

        parse_options = parse_options or yt_dlp.parse_options
        ydl_factory = ydl_factory or yt_dlp.YoutubeDL
    logger = _BoundedLogger()
    cli_args = [
        "--no-config",
        "--no-playlist",
        "--simulate",
        "--no-plugin-dirs",
        "--no-js-runtimes",
        "--js-runtimes",
        f"{request['runtimeKind']}:{request['runtimePath']}",
        "--no-remote-components",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "-f",
        "bestaudio/best",
    ]
    if request["cacheMode"] == "cold":
        cli_args.append("--no-cache-dir")
    else:
        cli_args.extend(("--cache-dir", str(request["cacheRoot"])))
    cli_args.append(str(request["url"]))
    parsed = parse_options(cli_args)
    options = dict(parsed.ydl_opts)
    expected_runtime = {
        str(request["runtimeKind"]): {"path": str(request["runtimePath"])}
    }
    parsed_options = getattr(parsed, "options", None)
    if (
        options.get("js_runtimes") != expected_runtime
        or options.get("remote_components") != []
        or getattr(parsed_options, "plugin_dirs", None) != []
    ):
        raise ProbeBoundaryError("frozen yt-dlp CLI runtime policy did not parse exactly")
    if request["cacheMode"] == "cold" and options.get("cachedir") is not False:
        raise ProbeBoundaryError("cold yt-dlp cache policy did not parse exactly")
    if request["cacheMode"] == "warm" and Path(str(options.get("cachedir"))).resolve() != request[
        "cacheRoot"
    ]:
        raise ProbeBoundaryError("warm yt-dlp cache policy did not parse exactly")
    options.update(
        {
            "logger": logger,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "simulate": True,
            "verbose": True,
        }
    )
    # ``parse_options`` deliberately does not update yt-dlp's process-global
    # plugin lookup (the normal CLI does that later in ``main``).  Bind both
    # layers so a user/system/PYTHONPATH plugin cannot replace the extractor or
    # JS challenge implementation used by this measurement.
    from yt_dlp.globals import plugin_dirs

    previous_plugin_dirs = list(plugin_dirs.value)
    plugin_dirs.value = []
    started = time.perf_counter_ns()
    try:
        if plugin_dirs.value != []:
            raise ProbeBoundaryError("yt-dlp plugin policy is not empty")
        try:
            with ydl_factory(options) as ydl:
                info = ydl.extract_info(str(request["url"]), download=False)
            duration_ns = time.perf_counter_ns() - started
            if not isinstance(info, dict) or info.get("id") != request["expectedVideoId"]:
                raise ProbeBoundaryError("yt-dlp returned another video identity")
            response = _response_base(request)
            response.update(
                {
                    "status": "pass",
                    "videoId": request["expectedVideoId"],
                    "durationNs": duration_ns,
                    "observedCapabilities": _capabilities(
                        request=request, info=info, debug_log=logger.text
                    ),
                }
            )
            return 0, response
        except ProbeBoundaryError:
            raise
        except Exception as exc:
            duration_ns = time.perf_counter_ns() - started
            response = _response_base(request)
            response.update(
                {
                    "status": "fail",
                    "failureCode": _failure_code(exc),
                    "durationNs": duration_ns,
                    "observedCapabilities": [],
                }
            )
            return 1, response
    finally:
        plugin_dirs.value = previous_plugin_dirs


def run_frozen_probe(runtime_root: Path) -> int:
    try:
        request = _load_request(sys.stdin.buffer)
        exit_code, response = execute_probe(request, runtime_root=runtime_root)
    except (ProbeBoundaryError, OSError, ValueError) as exc:
        response = {
            "probeContract": PROBE_CONTRACT,
            "schemaVersion": SCHEMA_VERSION,
            "status": "fail",
            "failureCode": "probe_boundary_invalid",
            "errorType": type(exc).__name__,
        }
        exit_code = 2
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        encoded = json.dumps(
            {
                "probeContract": PROBE_CONTRACT,
                "schemaVersion": SCHEMA_VERSION,
                "status": "fail",
                "failureCode": "probe_response_limit",
            },
            separators=(",", ":"),
        )
        exit_code = 2
    print(encoded)
    return exit_code
