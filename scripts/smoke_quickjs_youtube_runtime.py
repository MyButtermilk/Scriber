"""Run the frozen YouTube probe against an explicit QuickJS candidate payload.

This is a focused product smoke gate, not an AutoResearch comparison.  It runs
the six frozen public route families once with a cold yt-dlp cache and writes a
small, redacted JSON report.  URLs, video identifiers, paths, commands, and raw
child-process output are deliberately excluded from the report.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse


EVIDENCE_CONTRACT = "ScriberQuickJsYoutubeRuntimeSmokeV1"
FROZEN_PROBE_CONTRACT = "InstallerYoutubeFrozenHoldoutProbeV1"
FIXTURE_ID = "youtube-runtime-holdouts-v1"
SCHEMA_VERSION = 1
EXPECTED_CASE_COUNT = 6
MAX_JSON_INPUT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_STDOUT_BYTES = 32 * 1024
MAX_STDERR_BYTES = 64 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{6,32}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
MANIFEST_CONTRACT_RE = re.compile(r"^ScriberYoutubeJsRuntimeManifestV([3-9][0-9]*)$")

PROBE_POLICY = {
    "configDiscovery": False,
    "externalPlugins": False,
    "remoteComponents": False,
    "download": False,
    "explicitSingleRuntime": True,
}

PROBE_FAILURE_CODES = frozenset(
    {
        "http_429",
        "http_403",
        "login_required",
        "geo_restricted",
        "media_unavailable",
        "network_timeout",
        "tls_failure",
        "dns_failure",
        "extractor_error",
        "unknown_failure",
        "probe_boundary_invalid",
        "probe_response_limit",
    }
)

PUBLIC_FAILURE_CODES = PROBE_FAILURE_CODES | {
    "timeout",
    "output_limit",
    "process_failed",
    "invalid_json",
    "probe_contract_invalid",
    "candidate_capability_missing",
    "cleanup_unproven",
    "input_changed",
}


class SmokeError(RuntimeError):
    """The smoke input or execution boundary is invalid."""

    def __init__(self, code: str) -> None:
        if not CASE_ID_RE.fullmatch(code):
            code = "smoke-boundary-invalid"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileIdentity:
    length: int
    sha256: str

    def public(self) -> dict[str, Any]:
        return {"length": self.length, "sha256": self.sha256}


@dataclass(frozen=True)
class CandidateBundle:
    root: Path
    backend: Path
    runtime: Path
    engine: Path
    license_file: Path
    manifest_file: Path
    manifest: dict[str, Any]
    identities: dict[str, FileIdentity]


@dataclass(frozen=True)
class CommandResult:
    status: str
    return_code: int | None
    elapsed_ns: int
    stdout: bytes
    stderr: bytes
    cleanup_verified: bool


CommandRunner = Callable[..., CommandResult]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> FileIdentity:
    return FileIdentity(length=path.stat().st_size, sha256=_sha256_file(path))


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise SmokeError("probe-request-invalid")
    return encoded


def _load_json_object(path: Path, *, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SmokeError(code) from exc
    if not raw or len(raw) > MAX_JSON_INPUT_BYTES:
        raise SmokeError(code)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(code) from exc
    if not isinstance(value, dict):
        raise SmokeError(code)
    return value, raw


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def _plain_directory(path: Path, *, code: str) -> Path:
    entry = Path(os.path.abspath(path))
    try:
        if _is_reparse(entry) or not entry.is_dir():
            raise SmokeError(code)
        return entry.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SmokeError(code) from exc


def _plain_child(root: Path, relative: Path, *, directory: bool, code: str) -> Path:
    current = root
    try:
        for part in relative.parts:
            current = current / part
            if _is_reparse(current):
                raise SmokeError(code)
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SmokeError(code) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SmokeError(code) from exc
    if directory and not resolved.is_dir():
        raise SmokeError(code)
    if not directory and not resolved.is_file():
        raise SmokeError(code)
    return resolved


def _same_entry(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _parse_video_id(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise SmokeError("fixture-invalid")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {"www.youtube.com", "youtube.com", "music.youtube.com", "youtu.be"}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SmokeError("fixture-invalid")
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    elif parsed.path.startswith("/shorts/"):
        parts = parsed.path.split("/", 3)
        video_id = parts[2] if len(parts) > 2 else ""
    else:
        video_id = str(parse_qs(parsed.query).get("v", [""])[0])
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise SmokeError("fixture-invalid")
    return video_id


def _normalize_required(values: Iterable[str]) -> tuple[str, ...]:
    aliases = {
        "deno-runtime": "js-runtime",
        "deno-jsc": "js-challenge-runtime",
    }
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not CASE_ID_RE.fullmatch(value):
            raise SmokeError("fixture-invalid")
        normalized.add(aliases.get(value, value))
    if not normalized:
        raise SmokeError("fixture-invalid")
    return tuple(sorted(normalized))


def _load_cases(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixture, raw = _load_json_object(path, code="fixture-invalid")
    cases = fixture.get("cases")
    if (
        fixture.get("schemaVersion") != 1
        or fixture.get("fixtureId") != FIXTURE_ID
        or fixture.get("frozenCaseContract") is not True
        or not isinstance(cases, list)
        or len(cases) != EXPECTED_CASE_COUNT
    ):
        raise SmokeError("fixture-invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise SmokeError("fixture-invalid")
        case_id = raw_case.get("id")
        family = raw_case.get("family")
        url = raw_case.get("url")
        required = raw_case.get("requiredCapabilities")
        if (
            not isinstance(case_id, str)
            or not CASE_ID_RE.fullmatch(case_id)
            or case_id in seen
            or not isinstance(family, str)
            or not CASE_ID_RE.fullmatch(family)
            or not isinstance(required, list)
        ):
            raise SmokeError("fixture-invalid")
        seen.add(case_id)
        normalized.append(
            {
                "caseId": case_id,
                "family": family,
                "url": url,
                "expectedVideoId": _parse_video_id(str(url)),
                "requiredCapabilities": _normalize_required(required),
            }
        )
    return normalized, {
        "fixtureId": FIXTURE_ID,
        "sha256": _sha256_bytes(raw),
        "caseCount": len(normalized),
    }


def _manifest_runtime_files(
    payload_root: Path, explicit_runtime: Path
) -> CandidateBundle:
    backend = _plain_child(
        payload_root,
        Path("backend/scriber-backend.exe"),
        directory=False,
        code="candidate-backend-invalid",
    )
    runtime = _plain_child(
        payload_root,
        Path("backend/tools/ffmpeg/qjs.exe"),
        directory=False,
        code="candidate-runtime-invalid",
    )
    if not _same_entry(explicit_runtime, runtime):
        raise SmokeError("candidate-runtime-not-explicit-payload-runtime")
    engine = _plain_child(
        payload_root,
        Path("backend/tools/ffmpeg/qjs-engine.exe"),
        directory=False,
        code="candidate-runtime-invalid",
    )
    license_file = _plain_child(
        payload_root,
        Path("backend/tools/ffmpeg/LICENSE.quickjs-ng.txt"),
        directory=False,
        code="candidate-runtime-invalid",
    )
    manifest_file = _plain_child(
        payload_root,
        Path("backend/tools/ffmpeg/js-runtime-manifest.json"),
        directory=False,
        code="candidate-manifest-invalid",
    )
    manifest, _raw = _load_json_object(manifest_file, code="candidate-manifest-invalid")
    contract = manifest.get("contract")
    match = MANIFEST_CONTRACT_RE.fullmatch(str(contract))
    runtime_manifest = manifest.get("runtime")
    policy = manifest.get("policy")
    if (
        not match
        or manifest.get("schemaVersion") != int(match.group(1))
        or not isinstance(runtime_manifest, dict)
        or not isinstance(policy, dict)
        or runtime_manifest.get("kind") != "quickjs"
        or runtime_manifest.get("implementation") != "bounded-quickjs-wrapper"
        or runtime_manifest.get("executable") != "qjs.exe"
        or runtime_manifest.get("engine") != "qjs-engine.exe"
        or runtime_manifest.get("licenseFile") != "LICENSE.quickjs-ng.txt"
        or policy.get("remoteComponents") is not False
        or policy.get("firstRunDownloads") is not False
        or policy.get("exactArgumentProtocol") is not True
        or policy.get("engineHashVerified") is not True
        or policy.get("killOnJobClose") is not True
    ):
        raise SmokeError("candidate-manifest-invalid")
    identities = {
        "backend": _file_identity(backend),
        "wrapper": _file_identity(runtime),
        "engine": _file_identity(engine),
        "license": _file_identity(license_file),
        "manifest": _file_identity(manifest_file),
    }
    if (
        runtime_manifest.get("length") != identities["wrapper"].length
        or runtime_manifest.get("sha256") != identities["wrapper"].sha256
        or runtime_manifest.get("engineLength") != identities["engine"].length
        or runtime_manifest.get("engineSha256") != identities["engine"].sha256
        or not isinstance(runtime_manifest.get("version"), str)
        or not VERSION_RE.fullmatch(str(runtime_manifest["version"]))
        or not isinstance(runtime_manifest.get("protocol"), str)
        or not VERSION_RE.fullmatch(str(runtime_manifest["protocol"]))
    ):
        raise SmokeError("candidate-manifest-invalid")
    for name in ("maximumStdoutBytes", "maximumStderrBytes", "timeoutMilliseconds"):
        value = policy.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SmokeError("candidate-manifest-invalid")
    return CandidateBundle(
        root=payload_root,
        backend=backend,
        runtime=runtime,
        engine=engine,
        license_file=license_file,
        manifest_file=manifest_file,
        manifest=manifest,
        identities=identities,
    )


def _candidate_bundle(candidate_payload: Path, explicit_runtime: Path) -> CandidateBundle:
    root = _plain_directory(candidate_payload, code="candidate-payload-invalid")
    return _manifest_runtime_files(root, explicit_runtime)


def _attach_windows_kill_on_close_job(
    process: subprocess.Popen[bytes],
) -> tuple[int, Any] | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    try:
        process_handle = wintypes.HANDLE(process._handle)
    except AttributeError as exc:
        kernel32.CloseHandle(job)
        raise OSError("child process has no assignable Windows handle") from exc
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return int(job), kernel32.CloseHandle


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None and os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if completed.returncode != 0 and process.poll() is None:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    elif process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise SmokeError("process-cleanup-failed") from exc


def _close_process_scope(
    process: subprocess.Popen[bytes], windows_job: tuple[int, Any] | None
) -> bool:
    if windows_job is not None:
        handle, close_handle = windows_job
        closed = bool(close_handle(handle))
        if process.poll() is None:
            _terminate_process_tree(process)
        return closed and process.poll() is not None
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        _terminate_process_tree(process)
    return process.poll() is not None


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_bytes: bytes,
    timeout_seconds: float,
) -> CommandResult:
    if (
        not command
        or any(not isinstance(value, str) or not value or "\0" in value for value in command)
        or not isinstance(stdin_bytes, bytes)
        or len(stdin_bytes) > MAX_REQUEST_BYTES
        or timeout_seconds <= 0
    ):
        raise SmokeError("process-boundary-invalid")
    stdin_path = cwd / "request.bin"
    stdout_path = cwd / "stdout.bin"
    stderr_path = cwd / "stderr.bin"
    stdin_path.write_bytes(stdin_bytes)
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    process: subprocess.Popen[bytes] | None = None
    windows_job: tuple[int, Any] | None = None
    status = "completed"
    started = time.perf_counter_ns()
    try:
        with (
            stdin_path.open("rb") as stdin_handle,
            stdout_path.open("wb") as stdout_handle,
            stderr_path.open("wb") as stderr_handle,
        ):
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                env=dict(env),
                stdin=stdin_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
            try:
                windows_job = _attach_windows_kill_on_close_job(process)
            except OSError as exc:
                _terminate_process_tree(process)
                raise SmokeError("process-scope-failed") from exc
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    status = "timeout"
                    _terminate_process_tree(process)
                    break
                if (
                    stdout_path.stat().st_size > MAX_STDOUT_BYTES
                    or stderr_path.stat().st_size > MAX_STDERR_BYTES
                ):
                    status = "output_limit"
                    _terminate_process_tree(process)
                    break
                time.sleep(0.025)
        elapsed_ns = time.perf_counter_ns() - started
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        if len(stdout) > MAX_STDOUT_BYTES or len(stderr) > MAX_STDERR_BYTES:
            status = "output_limit"
        stdout = stdout[: MAX_STDOUT_BYTES + 1]
        stderr = stderr[: MAX_STDERR_BYTES + 1]
        return_code = process.returncode
    finally:
        cleanup_verified = (
            _close_process_scope(process, windows_job) if process is not None else False
        )
    return CommandResult(
        status=status,
        return_code=return_code,
        elapsed_ns=elapsed_ns,
        stdout=stdout,
        stderr=stderr,
        cleanup_verified=cleanup_verified,
    )


def _probe_environment(workspace: Path) -> dict[str, str]:
    home = workspace / "home"
    temp = workspace / "temp"
    cache = workspace / "cache"
    for child in (home, temp, cache):
        child.mkdir(mode=0o700, exist_ok=False)
    env = os.environ.copy()
    for name in (
        "YTDLP_CONFIG",
        "YT_DLP_CONFIG",
        "PYTHONHOME",
        "PYTHONPATH",
        "DENO_INSTALL",
        "DENO_AUTH_TOKENS",
        "NODE_OPTIONS",
        "BUN_INSTALL",
    ):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
            "XDG_CACHE_HOME": str(cache),
            "YTDLP_NO_PLUGINS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "NO_COLOR": "1",
        }
    )
    return env


def _probe_request(case: Mapping[str, Any], runtime: Path) -> bytes:
    return _canonical_json_bytes(
        {
            "requestContract": FROZEN_PROBE_CONTRACT,
            "schemaVersion": 1,
            "caseId": case["caseId"],
            "family": case["family"],
            "url": case["url"],
            "expectedVideoId": case["expectedVideoId"],
            "runtimeKind": "quickjs",
            "runtimePath": str(runtime),
            "cacheMode": "cold",
        }
    )


def _failure_row(
    case: Mapping[str, Any],
    *,
    failure_code: str,
    elapsed_ns: int,
    cleanup_verified: bool,
) -> dict[str, Any]:
    if failure_code not in PUBLIC_FAILURE_CODES:
        failure_code = "probe_contract_invalid"
    return {
        "caseId": case["caseId"],
        "family": case["family"],
        "status": "fail",
        "failureCode": failure_code,
        "durationNs": None,
        "elapsedNs": max(0, elapsed_ns),
        "requiredCapabilities": list(case["requiredCapabilities"]),
        "observedCapabilities": [],
        "missingRequiredCapabilities": list(case["requiredCapabilities"]),
        "cleanupVerified": cleanup_verified,
    }


def _parse_probe_result(
    case: Mapping[str, Any], result: CommandResult
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    if not result.cleanup_verified:
        return (
            _failure_row(
                case,
                failure_code="cleanup_unproven",
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=False,
            ),
            None,
        )
    if result.status != "completed":
        return (
            _failure_row(
                case,
                failure_code=result.status,
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=True,
            ),
            None,
        )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return (
            _failure_row(
                case,
                failure_code="invalid_json",
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=True,
            ),
            None,
        )
    if not isinstance(payload, dict):
        return (
            _failure_row(
                case,
                failure_code="invalid_json",
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=True,
            ),
            None,
        )
    duration_ns = payload.get("durationNs")
    common_valid = (
        payload.get("probeContract") == FROZEN_PROBE_CONTRACT
        and payload.get("schemaVersion") == 1
        and payload.get("caseId") == case["caseId"]
        and payload.get("runtimeKind") == "quickjs"
        and payload.get("policy") == PROBE_POLICY
        and isinstance(duration_ns, int)
        and not isinstance(duration_ns, bool)
        and 0 <= duration_ns <= result.elapsed_ns
        and isinstance(payload.get("ytDlpVersion"), str)
        and VERSION_RE.fullmatch(str(payload["ytDlpVersion"])) is not None
        and isinstance(payload.get("ejsVersion"), str)
        and VERSION_RE.fullmatch(str(payload["ejsVersion"])) is not None
    )
    if payload.get("status") == "fail" and common_valid:
        failure_code = payload.get("failureCode")
        if result.return_code != 1 or failure_code not in PROBE_FAILURE_CODES:
            failure_code = "probe_contract_invalid"
        return (
            _failure_row(
                case,
                failure_code=str(failure_code),
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=True,
            ),
            (str(payload["ytDlpVersion"]), str(payload["ejsVersion"])),
        )
    capabilities = payload.get("observedCapabilities")
    if (
        not common_valid
        or payload.get("status") != "pass"
        or result.return_code != 0
        or payload.get("videoId") != case["expectedVideoId"]
        or not isinstance(capabilities, list)
        or not capabilities
        or capabilities != sorted(set(capabilities))
        or any(
            not isinstance(value, str) or not CASE_ID_RE.fullmatch(value)
            for value in capabilities
        )
    ):
        return (
            _failure_row(
                case,
                failure_code="probe_contract_invalid",
                elapsed_ns=result.elapsed_ns,
                cleanup_verified=True,
            ),
            None,
        )
    missing = sorted(set(case["requiredCapabilities"]) - set(capabilities))
    status = "pass" if not missing else "fail"
    return (
        {
            "caseId": case["caseId"],
            "family": case["family"],
            "status": status,
            "failureCode": None if status == "pass" else "candidate_capability_missing",
            "durationNs": duration_ns,
            "elapsedNs": result.elapsed_ns,
            "requiredCapabilities": list(case["requiredCapabilities"]),
            "observedCapabilities": capabilities,
            "missingRequiredCapabilities": missing,
            "cleanupVerified": True,
        },
        (str(payload["ytDlpVersion"]), str(payload["ejsVersion"])),
    )


def _refresh_identities(bundle: CandidateBundle) -> dict[str, FileIdentity]:
    return {
        "backend": _file_identity(bundle.backend),
        "wrapper": _file_identity(bundle.runtime),
        "engine": _file_identity(bundle.engine),
        "license": _file_identity(bundle.license_file),
        "manifest": _file_identity(bundle.manifest_file),
    }


def _assert_redacted(value: Any) -> None:
    forbidden_keys = {
        "url",
        "videoid",
        "path",
        "stdout",
        "stderr",
        "command",
        "workingdirectory",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            folded = str(key).casefold()
            if folded in forbidden_keys:
                raise SmokeError("evidence-redaction-failed")
            if folded.endswith("failurecode") and child is not None:
                if not isinstance(child, str) or child not in PUBLIC_FAILURE_CODES:
                    raise SmokeError("evidence-redaction-failed")
            _assert_redacted(child)
    elif isinstance(value, list):
        for child in value:
            _assert_redacted(child)
    elif isinstance(value, str):
        lowered = value.casefold()
        if (
            "youtube.com/" in lowered
            or "youtu.be/" in lowered
            or "\\users\\" in lowered
            or re.search(r"[a-z]:\\", lowered)
        ):
            raise SmokeError("evidence-redaction-failed")


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    path = Path(os.path.abspath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_smoke(args: argparse.Namespace, *, runner: CommandRunner | None = None) -> dict[str, Any]:
    if not 5 <= args.timeout_seconds <= 300:
        raise SmokeError("timeout-invalid")
    cases, fixture_binding = _load_cases(args.fixture)
    bundle = _candidate_bundle(args.candidate_payload, args.runtime)
    output = Path(os.path.abspath(args.output))
    try:
        output.relative_to(bundle.root)
    except ValueError:
        pass
    else:
        raise SmokeError("output-inside-candidate-payload")
    scratch_parent: Path | None = None
    if args.scratch_root is not None:
        scratch_parent = _plain_directory(args.scratch_root, code="scratch-root-invalid")
        try:
            scratch_parent.relative_to(bundle.root)
        except ValueError:
            pass
        else:
            raise SmokeError("scratch-inside-candidate-payload")
    command_runner = runner or _run_bounded
    rows: list[dict[str, Any]] = []
    versions: set[tuple[str, str]] = set()
    with tempfile.TemporaryDirectory(
        prefix="scriber-quickjs-smoke-",
        dir=str(scratch_parent) if scratch_parent is not None else None,
    ) as temporary_root:
        root = Path(temporary_root)
        for index, case in enumerate(cases):
            workspace = root / f"case-{index + 1:02d}-{case['caseId']}"
            workspace.mkdir(mode=0o700, exist_ok=False)
            env = _probe_environment(workspace)
            result = command_runner(
                [str(bundle.backend), "--installer-youtube-holdout-probe"],
                cwd=workspace,
                env=env,
                stdin_bytes=_probe_request(case, bundle.runtime),
                timeout_seconds=float(args.timeout_seconds),
            )
            row, version = _parse_probe_result(case, result)
            rows.append(row)
            if version is not None:
                versions.add(version)
    after = _refresh_identities(bundle)
    inputs_unchanged = after == bundle.identities
    if not inputs_unchanged:
        for row in rows:
            row["status"] = "fail"
            row["failureCode"] = "input_changed"
    if len(versions) > 1:
        for row in rows:
            if row["status"] == "pass":
                row["status"] = "fail"
                row["failureCode"] = "probe_contract_invalid"
    reason_codes = sorted(
        {
            str(row["failureCode"])
            for row in rows
            if row["failureCode"] is not None
        }
    )
    status = "pass" if not reason_codes and len(rows) == EXPECTED_CASE_COUNT else "fail"
    runtime_manifest = bundle.manifest["runtime"]
    evidence: dict[str, Any] = {
        "contract": EVIDENCE_CONTRACT,
        "schemaVersion": SCHEMA_VERSION,
        "status": status,
        "reasonCodes": reason_codes,
        "capturedAtUtc": _utc_now(),
        "bindings": {
            "fixture": fixture_binding,
            "candidateBackend": bundle.identities["backend"].public(),
            "quickJsWrapper": bundle.identities["wrapper"].public(),
            "quickJsEngine": bundle.identities["engine"].public(),
            "quickJsLicense": bundle.identities["license"].public(),
            "runtimeManifest": {
                **bundle.identities["manifest"].public(),
                "contract": bundle.manifest["contract"],
                "runtimeVersion": runtime_manifest["version"],
                "protocol": runtime_manifest["protocol"],
            },
        },
        "executionPolicy": {
            "frozenBackendProbe": FROZEN_PROBE_CONTRACT,
            "runtimeKind": "quickjs",
            "caseCount": EXPECTED_CASE_COUNT,
            "cacheMode": "cold",
            "sequential": True,
            "remoteComponents": False,
            "externalPlugins": False,
            "download": False,
            "timeoutSecondsPerCase": args.timeout_seconds,
            "maximumStdoutBytes": MAX_STDOUT_BYTES,
            "maximumStderrBytes": MAX_STDERR_BYTES,
            "processTreeCleanup": True,
        },
        "toolVersions": (
            {
                "ytDlp": next(iter(versions))[0],
                "ejs": next(iter(versions))[1],
            }
            if len(versions) == 1
            else None
        ),
        "cases": rows,
        "inputImmutabilityVerified": inputs_unchanged,
    }
    _assert_redacted(evidence)
    _write_atomic(output, evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-payload", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("scripts/perf/profiles/installer-size/youtube-holdouts.json"),
    )
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        evidence = run_smoke(args)
        print(
            json.dumps(
                {
                    "ok": evidence["status"] == "pass",
                    "status": evidence["status"],
                    "reasonCodes": evidence["reasonCodes"],
                    "evidenceSha256": _sha256_file(args.output),
                },
                separators=(",", ":"),
            )
        )
        return 0 if evidence["status"] == "pass" else 1
    except (SmokeError, OSError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, SmokeError) else "smoke-execution-failed"
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "not_run",
                    "reasonCodes": [code],
                    "errorType": type(exc).__name__,
                },
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
