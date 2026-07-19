from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.installer_research.comparator import (
    MANDATORY_EXTERNAL_GATES,
    MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS,
    MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR,
    validate_install_measurements,
)
from scripts.installer_research.inventory import build_root_identity_sha256
from scripts.perf.autoresearch_profiles import ProfileError, resolve_profile_context
from scripts.perf.installer_size.doctor import current_installer_evaluator_hash, run_doctor
from scripts.perf.installer_size.evaluator import load_result, validate_result
from scripts.perf.installer_size.state import (
    StateError,
    abandon_pending_packet,
    accepted_baseline_replica_packet_id,
    acquire_dispatch_lock,
    append_ledger,
    arm_research_clock,
    clear_pending_packet,
    effective_finalization_reserve,
    file_sha256,
    format_utc,
    git_snapshot,
    git_parent_oids,
    git_tree_oid,
    initialize_run,
    load_json_object,
    load_manifest,
    load_progress,
    packet_completed_ledger_payload,
    pending_packet_requires_resume,
    paths_for,
    remaining_seconds,
    safe_packet_id,
    safe_lane_id,
    store_immutable_json,
    summarize,
    utc_now,
    validate_packet,
    write_json_atomic,
    write_preflight,
)


POWERSHELL_PACKET_ENTRYPOINT = "scripts/run_installer_size_packet.ps1"
MAX_CAPTURE_CHARS = 4096
MIN_PACKET_TIMEOUT_SECONDS = 30
MAX_PACKET_TIMEOUT_SECONDS = 14_400
REQUIRED_KEEP_PASS_GATES = {
    "bindings",
    "installerReduction",
    "stagedPayloadNonGrowth",
    "componentPartition",
    "pyinstallerPartition",
    "installedPayloadNonGrowth",
    "installTimingRegression",
    "frozenRuntimeImports",
    "mediaPreparation",
    "youtubeWorkflow",
    "liveMic",
    "meetingCapture",
    "diarization",
    "pdfDocxExport",
    "desktopFrontend",
    "cleanInstallUpgradeUninstall",
    "licenseSupplyChain",
}
FINAL_EXTERNAL_GATES = frozenset(MANDATORY_EXTERNAL_GATES)
FINAL_FULL_SUITE_GATES = frozenset(
    {
        "pythonPytest",
        "frontendCheck",
        "frontendI18n",
        "frontendBuild",
        "rustCargoTest",
        "rustFmt",
        "rustClippy",
    }
)


def _print_json(payload: dict[str, Any], *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def _redact_output(value: str | None, repo_root: Path) -> str:
    lines = [line for line in (value or "").splitlines() if line.strip()]
    redacted = "\n".join(lines[-8:]).replace(str(repo_root), "<repo>")
    redacted = re.sub(
        r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\"']+",
        "<redacted-path>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(?:authorization|api[-_]?key|token|secret|password)\b\s*[:=]\s*[^\s,;]+",
        "<redacted-credential>",
        redacted,
    )
    redacted = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", redacted)
    if len(redacted) > MAX_CAPTURE_CHARS:
        return redacted[-MAX_CAPTURE_CHARS:]
    return redacted


def _safe_relative_entrypoint(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or ".." in Path(text).parts:
        raise StateError("packet entrypoint must be a safe repository-relative path")
    return text


def _safe_arguments(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise StateError("packet dispatch arguments must be an array with at most 128 entries")
    arguments: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 4096 or "\0" in item or "\n" in item or "\r" in item:
            raise StateError("packet dispatch contains an unsafe argument")
        arguments.append(item)
    return arguments


def _option_pairs(arguments: list[str], *, command: str) -> dict[str, str]:
    if not arguments or arguments[0] != command:
        raise StateError(f"packet evaluator command must be {command}")
    remaining = arguments[1:]
    if len(remaining) % 2:
        raise StateError("packet evaluator arguments must be explicit --option value pairs")
    options: dict[str, str] = {}
    for index in range(0, len(remaining), 2):
        name, value = remaining[index : index + 2]
        if not name.startswith("--") or name == "--" or value.startswith("--"):
            raise StateError("packet evaluator arguments must be explicit --option value pairs")
        if name in options:
            raise StateError(f"packet evaluator option is duplicated: {name}")
        options[name] = value
    return options


def _require_exact_options(
    options: dict[str, str],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(options))
    unknown = sorted(set(options) - required - optional)
    if missing:
        raise StateError("packet evaluator options are missing: " + ", ".join(missing))
    if unknown:
        raise StateError("packet evaluator options are not allowlisted: " + ", ".join(unknown))


def _resolve_argument_path(context, value: str, *, label: str) -> Path:
    raw = Path(value)
    resolved = (raw if raw.is_absolute() else context.repo_root / raw).resolve()
    if not resolved.is_relative_to(context.repo_root.resolve()):
        raise StateError(f"{label} must stay under the repository root")
    return resolved


def _require_run_artifact(context, value: str, *, label: str) -> Path:
    resolved = _resolve_argument_path(context, value, label=label)
    run_root = paths_for(context).root.resolve()
    if not resolved.is_relative_to(run_root):
        raise StateError(f"{label} must stay under the immutable RunId root")
    if not resolved.exists():
        raise StateError(f"{label} does not exist")
    return resolved


def _require_exact_path(context, value: str, expected: Path, *, label: str) -> None:
    if _resolve_argument_path(context, value, label=label) != expected.resolve():
        raise StateError(f"{label} does not match the harness-owned path")


def _validate_inventory_dispatch(
    context,
    packet: dict[str, Any],
    arguments: list[str],
    result_path: Path,
) -> None:
    options = _option_pairs(arguments, command="inventory")
    required = {
        "--staged-root",
        "--backend-exe",
        "--component-map",
        "--installed-root",
        "--product-version",
        "--compression",
        "--toolchain-hash",
        "--run-id",
        "--source-commit",
        "--replica-id",
        "--build-root-sha256",
        "--output",
    }
    _require_exact_options(
        options,
        required=required,
        optional={"--installer", "--artifact-dir"},
    )
    if ("--installer" in options) == ("--artifact-dir" in options):
        raise StateError("inventory dispatch requires exactly one installer source")
    for name in (
        "--staged-root",
        "--backend-exe",
        "--installed-root",
        "--installer" if "--installer" in options else "--artifact-dir",
    ):
        _require_run_artifact(context, options[name], label=name)
    _require_exact_path(
        context,
        options["--component-map"],
        context.repo_root / "packaging" / "installer-component-map-v1.json",
        label="--component-map",
    )
    _require_exact_path(context, options["--output"], result_path, label="--output")
    paths = paths_for(context)
    if options["--toolchain-hash"] != file_sha256(paths.toolchain_manifest):
        raise StateError("inventory --toolchain-hash differs from the pinned manifest")
    if options["--run-id"] != context.run_id:
        raise StateError("inventory --run-id differs from the active RunId")
    if options["--source-commit"] != packet["sourceCommit"]:
        raise StateError("inventory --source-commit differs from the packet")
    if options["--replica-id"] != packet["packetId"]:
        raise StateError("inventory --replica-id must equal packetId")
    build_root_sha = options["--build-root-sha256"]
    if len(build_root_sha) != 64 or any(character not in "0123456789abcdef" for character in build_root_sha):
        raise StateError("inventory --build-root-sha256 must be a lowercase SHA-256")
    staged_root = _require_run_artifact(context, options["--staged-root"], label="--staged-root")
    if build_root_sha != build_root_identity_sha256(staged_root):
        raise StateError("inventory --build-root-sha256 differs from the staged build-root identity")
    if packet["action"]["kind"] == "baseline-replica" and options["--compression"] != "bzip2":
        raise StateError("baseline replicas require explicit bzip2 compression")


def _expected_parent_champion_id(context) -> str:
    champion_id = load_progress(context).get("championId")
    return str(champion_id) if champion_id else "baseline"


def _champion_inventory_path(context, champion_id: str) -> Path:
    paths = paths_for(context)
    packet = load_json_object(paths.packet(champion_id))
    dispatch = packet.get("action", {}).get("dispatch", {})
    if dispatch.get("driver") == "powershell-file":
        candidate = paths.root / "packet-evidence" / champion_id / "inventory.json"
    else:
        options = _option_pairs(
            _safe_arguments(dispatch.get("arguments")),
            command="evaluate",
        )
        candidate = _resolve_argument_path(
            context,
            options.get("--candidate-inventory", ""),
            label="champion --candidate-inventory",
        )
    if not candidate.is_file() or not candidate.resolve().is_relative_to(paths.root.resolve()):
        raise StateError("champion inventory provenance is missing from the RunId root")
    return candidate.resolve()


def _validate_candidate_dispatch(
    context,
    packet: dict[str, Any],
    arguments: list[str],
    result_path: Path,
) -> None:
    options = _option_pairs(arguments, command="evaluate")
    required = {
        "--baseline",
        "--candidate-inventory",
        "--run-id",
        "--packet-id",
        "--parent-champion-id",
        "--hypothesis",
        "--source-commit",
        "--comparison-kind",
        "--gate-results",
        "--install-measurements",
        "--min-absolute-reduction-bytes",
        "--min-relative-basis-points",
        "--output",
    }
    _require_exact_options(options, required=required, optional={"--parent-inventory"})
    paths = paths_for(context)
    _require_exact_path(context, options["--baseline"], paths.baseline, label="--baseline")
    _require_exact_path(context, options["--output"], result_path, label="--output")
    for name in ("--candidate-inventory", "--gate-results", "--install-measurements"):
        _require_run_artifact(context, options[name], label=name)
    expected_parent = _expected_parent_champion_id(context)
    if expected_parent == "baseline":
        if "--parent-inventory" in options:
            raise StateError("baseline parent must use the embedded accepted baseline inventory")
    else:
        if "--parent-inventory" not in options:
            raise StateError("kept parent requires an explicit champion inventory")
        parent_inventory = _require_run_artifact(
            context,
            options["--parent-inventory"],
            label="--parent-inventory",
        )
        if parent_inventory != _champion_inventory_path(context, expected_parent):
            raise StateError("--parent-inventory differs from the kept champion inventory")
    expected_values = {
        "--run-id": str(context.run_id),
        "--packet-id": str(packet["packetId"]),
        "--parent-champion-id": expected_parent,
        "--hypothesis": str(packet["hypothesis"]["statement"]),
        "--source-commit": str(packet["sourceCommit"]),
        "--comparison-kind": str(packet["action"]["comparisonKind"]),
        "--min-absolute-reduction-bytes": str(context.config["minimumInstallerReduction"]["bytes"]),
        "--min-relative-basis-points": str(
            round(float(context.config["minimumInstallerReduction"]["fraction"]) * 10_000)
        ),
    }
    for name, expected in expected_values.items():
        if options[name] != expected:
            raise StateError(f"candidate {name} differs from the frozen packet/run binding")
    if packet.get("parentChampionId") != expected_parent:
        raise StateError("packet parentChampionId is stale")


def _validate_dispatch_policy(
    context,
    packet: dict[str, Any],
    *,
    driver: Any,
    entrypoint: str,
    arguments: list[str],
) -> None:
    if driver == "powershell-file" and entrypoint == POWERSHELL_PACKET_ENTRYPOINT:
        kind = packet["action"]["kind"]
        if kind == "baseline-replica":
            mode = f"baseline-{packet['action']['replica']}"
            run_timing = False
        elif kind == "final-replica":
            mode = f"final-{packet['action']['replica']}"
            run_timing = packet["action"]["replica"] == 2
        else:
            mode = "candidate"
            run_timing = "-RunTiming" in arguments
        expected_arguments = ["-RunId", str(context.run_id), "-Mode", mode]
        if run_timing:
            expected_arguments.append("-RunTiming")
        if arguments != expected_arguments:
            raise StateError("packet producer arguments differ from the exact RunId/mode/timing policy")
        return
    raise StateError(
        "packet dispatch must use only the frozen installer-size packet producer"
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.poll() is None:
        process.kill()


def _attach_windows_kill_on_close_job(
    process: subprocess.Popen[str],
) -> tuple[int, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
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
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "SetInformationJobObject failed")
    try:
        process_handle = wintypes.HANDLE(process._handle)
    except AttributeError as exc:
        kernel32.CloseHandle(job)
        raise OSError("producer process has no assignable Windows handle") from exc
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "AssignProcessToJobObject failed")
    return int(job), kernel32.CloseHandle


def _run_bounded_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "text": True,
        # Packet producers emit an ASCII JSON authority line, but nested
        # Windows tools may also write OEM or otherwise malformed diagnostic
        # bytes.  Never let a background TextIO reader die and turn captured
        # stdout/stderr into None; ASCII backslash escapes preserve the JSON
        # boundary and keep untrusted diagnostics non-authoritative and safe
        # for a legacy Windows console to print.
        "encoding": "utf-8",
        "errors": "backslashreplace",
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    executable_name = str(command[0]).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable_name == "powershell.exe":
        # A PowerShell 7 parent prepends its own module directories before
        # launching arbitrary children.  Windows PowerShell 5.1 can then find
        # an incompatible Microsoft.PowerShell.Utility module and lose core
        # commands such as Get-FileHash.  Omitting the cross-edition value lets
        # powershell.exe rebuild its native default PSModulePath at startup.
        child_environment = os.environ.copy()
        for name in list(child_environment):
            if name.casefold() == "psmodulepath":
                child_environment.pop(name)
        popen_kwargs["env"] = child_environment
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    job_handle: int | None = None
    close_job_handle = None
    try:
        job_binding = _attach_windows_kill_on_close_job(process)
    except OSError:
        _terminate_process_tree(process)
        raise
    if job_binding is not None:
        job_handle, close_job_handle = job_binding
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=stdout,
            stderr=stderr,
        ) from exc
    finally:
        if job_handle is not None and close_job_handle is not None:
            close_job_handle(job_handle)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _finding(code: str, message: str) -> dict[str, Any]:
    return {"level": "block", "code": code, "message": message}


def _gate_status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or "")
    return str(value or "")


def _lower_sha256(value: Any) -> bool:
    text = str(value or "")
    return bool(
        len(text) == 64
        and text == text.casefold()
        and all(character in "0123456789abcdef" for character in text)
    )


def _validate_exact_pass_gates(
    gates: Any,
    *,
    expected: frozenset[str],
    code_prefix: str,
) -> list[dict[str, Any]]:
    if not isinstance(gates, dict) or set(gates) != expected:
        return [
            _finding(
                f"{code_prefix}_gate_set_mismatch",
                "evidence does not contain exactly the frozen mandatory gate set",
            )
        ]
    findings: list[dict[str, Any]] = []
    for name in sorted(expected):
        gate = gates.get(name)
        if (
            not isinstance(gate, dict)
            or set(gate) != {"status", "evidenceSha256"}
            or gate.get("status") != "pass"
            or not _lower_sha256(gate.get("evidenceSha256"))
        ):
            findings.append(
                _finding(
                    f"{code_prefix}_gate_not_passed",
                    f"frozen gate {name} lacks an exact pass status and evidence hash",
                )
            )
    return findings


def _evidence_value_is_path_redacted(value: Any, repo_root: Path) -> bool:
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and _evidence_value_is_path_redacted(item, repo_root)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_evidence_value_is_path_redacted(item, repo_root) for item in value)
    if not isinstance(value, str):
        return True
    text = value.strip()
    return not bool(
        re.match(r"^[A-Za-z]:[\\/]", text)
        or text.startswith("\\\\")
        or text.casefold().startswith("file://")
        or str(repo_root).casefold() in text.casefold()
    )


def _validate_youtube_detail_evidence(
    context,
    packet: dict[str, Any],
    *,
    expected_parent_champion_id: str,
    detail: Any,
) -> list[dict[str, Any]]:
    if not isinstance(detail, dict) or set(detail) != {
        "kind",
        "relativePath",
        "sha256",
    }:
        return [
            _finding(
                "youtube_detail_evidence_shape_mismatch",
                "YouTube gate must bind one exact retained detail artifact",
            )
        ]
    kind = detail.get("kind")
    relative = detail.get("relativePath")
    expected_sha = detail.get("sha256")
    packet_id = str(packet.get("packetId") or "")
    expected_relative = {
        "baseline-youtube-holdout": "preflight/youtube-holdouts.snapshot.json",
        "candidate-youtube-holdout": (
            f"packet-evidence/{packet_id}/youtube-holdouts-candidate.json"
        ),
    }.get(kind)
    if (
        expected_relative is None
        or relative != expected_relative
        or not _lower_sha256(expected_sha)
    ):
        return [
            _finding(
                "youtube_detail_evidence_binding_mismatch",
                "YouTube detail evidence path, kind, or hash is not canonical",
            )
        ]
    parts = relative.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        return [
            _finding(
                "youtube_detail_evidence_path_invalid",
                "YouTube detail evidence escaped its run namespace",
            )
        ]
    detail_path = paths_for(context).root.joinpath(*parts)
    current = paths_for(context).root
    try:
        for part in parts:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or bool(
                getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise OSError("reparse point")
    except OSError:
        return [
            _finding(
                "youtube_detail_evidence_missing_or_reparse",
                "YouTube detail evidence is missing or contains a reparse point",
            )
        ]
    if not detail_path.is_file() or detail_path.stat().st_size > 1_048_576:
        return [
            _finding(
                "youtube_detail_evidence_missing_or_oversized",
                "YouTube detail evidence is missing or oversized",
            )
        ]
    if file_sha256(detail_path) != expected_sha:
        return [
            _finding(
                "youtube_detail_evidence_hash_mismatch",
                "YouTube detail evidence differs from its retained gate artifact",
            )
        ]
    try:
        payload = load_json_object(detail_path)
    except StateError as exc:
        return [_finding("youtube_detail_evidence_invalid", str(exc))]
    findings: list[dict[str, Any]] = []
    if kind == "candidate-youtube-holdout":
        bindings = {
            "holdoutSnapshotContract": "InstallerSizeYoutubeCandidateHoldoutsV1",
            "schemaVersion": 1,
            "status": "pass",
            "runId": context.run_id,
            "packetId": packet_id,
            "parentChampionId": expected_parent_champion_id,
            "sourceCommit": packet.get("sourceCommit"),
            "inputImmutabilityVerified": True,
        }
        if any(payload.get(field) != expected for field, expected in bindings.items()):
            findings.append(
                _finding(
                    "youtube_detail_evidence_provenance_mismatch",
                    "candidate YouTube evidence differs from this packet",
                )
            )
        if payload.get("reasonCodes") != [] or len(payload.get("cases", [])) != 6:
            findings.append(
                _finding(
                    "youtube_detail_evidence_cases_invalid",
                    "candidate YouTube evidence is not a complete passing six-case matrix",
                )
            )
    else:
        if (
            payload.get("holdoutSnapshotContract")
            != "InstallerSizeYoutubeHoldoutsV1"
            or payload.get("schemaVersion") != 1
            or payload.get("runId") != context.run_id
        ):
            findings.append(
                _finding(
                    "youtube_baseline_snapshot_binding_mismatch",
                    "baseline YouTube snapshot differs from this run",
                )
            )
        cases = payload.get("cases")
        if not isinstance(cases, list) or len(cases) != 6:
            findings.append(
                _finding(
                    "youtube_baseline_snapshot_cases_invalid",
                    "baseline YouTube snapshot is not a complete six-case matrix",
                )
            )
        else:
            seen: set[str] = set()
            for case in cases:
                case_id = case.get("id") if isinstance(case, dict) else None
                probe_sha = (
                    case.get("probeEvidenceSha256")
                    if isinstance(case, dict)
                    else None
                )
                if (
                    not isinstance(case_id, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", case_id)
                    or case_id in seen
                    or case.get("status") != "validated"
                    or not _lower_sha256(probe_sha)
                ):
                    findings.append(
                        _finding(
                            "youtube_baseline_probe_binding_invalid",
                            "baseline YouTube snapshot contains an invalid probe binding",
                        )
                    )
                    continue
                seen.add(case_id)
                probe_path = (
                    paths_for(context).root
                    / "preflight"
                    / "youtube-holdout-probes"
                    / f"{case_id}.json"
                )
                if (
                    not probe_path.is_file()
                    or probe_path.is_symlink()
                    or probe_path.stat().st_size > 262_144
                    or file_sha256(probe_path) != probe_sha
                ):
                    findings.append(
                        _finding(
                            "youtube_baseline_probe_hash_mismatch",
                            f"baseline YouTube probe {case_id} is missing or drifted",
                        )
                    )
    return findings


def _validate_packet_gate_evidence(
    context,
    packet: dict[str, Any],
    *,
    expected_parent_champion_id: str,
    result_gates: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    paths = paths_for(context)
    packet_id = str(packet.get("packetId") or "")
    evidence_path = paths.root / "packet-evidence" / packet_id / "gate-evidence.json"
    if not evidence_path.is_file():
        return [
            _finding(
                "final_gate_evidence_missing",
                "final replica requires fresh packet-local functional gate evidence",
            )
        ], None
    evidence_sha = file_sha256(evidence_path)
    try:
        evidence = load_json_object(evidence_path)
    except StateError as exc:
        return [_finding("final_gate_evidence_invalid", str(exc))], evidence_sha
    expected_fields = {
        "gateEvidenceContract",
        "schemaVersion",
        "runId",
        "packetId",
        "parentChampionId",
        "sourceCommit",
        "gates",
    }
    findings: list[dict[str, Any]] = []
    if set(evidence) != expected_fields:
        findings.append(
            _finding(
                "final_gate_evidence_shape_mismatch",
                "final functional evidence contains unsupported or missing fields",
            )
        )
    bindings = {
        "gateEvidenceContract": "InstallerResearchGateEvidenceV1",
        "schemaVersion": 1,
        "runId": context.run_id,
        "packetId": packet_id,
        "parentChampionId": expected_parent_champion_id,
        "sourceCommit": packet.get("sourceCommit"),
    }
    for field, expected in bindings.items():
        if evidence.get(field) != expected:
            findings.append(
                _finding(
                    "final_gate_evidence_binding_mismatch",
                    f"final functional evidence differs from the packet at {field}",
                )
            )
    gates = evidence.get("gates")
    findings.extend(
        _validate_exact_pass_gates(
            gates,
            expected=FINAL_EXTERNAL_GATES,
            code_prefix="final_external",
        )
    )
    if isinstance(gates, dict):
        for name in sorted(FINAL_EXTERNAL_GATES):
            gate = gates.get(name)
            expected_sha = gate.get("evidenceSha256") if isinstance(gate, dict) else None
            artifact_path = (
                paths.root
                / "packet-evidence"
                / packet_id
                / "gates"
                / f"{name}.json"
            )
            if not artifact_path.is_file() or artifact_path.stat().st_size > 65_536:
                findings.append(
                    _finding(
                        "gate_artifact_missing_or_oversized",
                        f"retained gate artifact {name} is missing or oversized",
                    )
                )
                continue
            if not _lower_sha256(expected_sha) or file_sha256(artifact_path) != expected_sha:
                findings.append(
                    _finding(
                        "gate_artifact_hash_mismatch",
                        f"retained gate artifact {name} differs from gate evidence",
                    )
                )
            try:
                artifact = load_json_object(artifact_path)
            except StateError as exc:
                findings.append(_finding("gate_artifact_invalid", str(exc)))
                continue
            artifact_fields = {
                "gateArtifactContract",
                "schemaVersion",
                "runId",
                "packetId",
                "parentChampionId",
                "sourceCommit",
                "gate",
                "status",
                "checks",
                "detailEvidence",
            }
            artifact_bindings = {
                "gateArtifactContract": "InstallerResearchGateArtifactV1",
                "schemaVersion": 1,
                "runId": context.run_id,
                "packetId": packet_id,
                "parentChampionId": expected_parent_champion_id,
                "sourceCommit": packet.get("sourceCommit"),
                "gate": name,
                "status": "pass",
            }
            if set(artifact) != artifact_fields or any(
                artifact.get(field) != expected
                for field, expected in artifact_bindings.items()
            ):
                findings.append(
                    _finding(
                        "gate_artifact_binding_mismatch",
                        f"retained gate artifact {name} is not bound to this packet",
                    )
                )
            checks = artifact.get("checks")
            if (
                not isinstance(checks, list)
                or not checks
                or any(
                    not isinstance(check, dict)
                    or set(check) != {"name", "status"}
                    or not isinstance(check.get("name"), str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}",
                        check["name"],
                    )
                    or check.get("status") != "pass"
                    for check in checks
                )
            ):
                findings.append(
                    _finding(
                        "gate_artifact_checks_invalid",
                        f"retained gate artifact {name} has no exact passing checks",
                    )
                )
            if not _evidence_value_is_path_redacted(artifact, context.repo_root):
                findings.append(
                    _finding(
                        "gate_artifact_contains_path",
                        f"retained gate artifact {name} contains a local path",
                    )
                )
            if name == "youtubeWorkflow":
                findings.extend(
                    _validate_youtube_detail_evidence(
                        context,
                        packet,
                        expected_parent_champion_id=expected_parent_champion_id,
                        detail=artifact.get("detailEvidence"),
                    )
                )
            elif artifact.get("detailEvidence") is not None:
                findings.append(
                    _finding(
                        "unexpected_gate_detail_evidence",
                        f"retained gate artifact {name} has unexpected detail evidence",
                    )
                )
            if result_gates is not None:
                result_gate = result_gates.get(name)
                if (
                    not isinstance(result_gate, dict)
                    or result_gate.get("status") != "pass"
                    or result_gate.get("evidenceSha256") != expected_sha
                ):
                    findings.append(
                        _finding(
                            "result_gate_artifact_binding_mismatch",
                            f"candidate result gate {name} differs from retained evidence",
                        )
                    )
    return findings, evidence_sha


def _validate_final_gate_evidence(
    context,
    packet: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        champion = load_json_object(paths_for(context).champion)
    except StateError as exc:
        return [_finding("final_gate_evidence_invalid", str(exc))], None
    return _validate_packet_gate_evidence(
        context,
        packet,
        expected_parent_champion_id=str(champion.get("packetId") or ""),
    )


def _validate_final_full_suite_evidence(
    context,
    packet: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    if packet.get("action", {}).get("replica") != 1:
        return [], None
    paths = paths_for(context)
    packet_id = str(packet.get("packetId") or "")
    evidence_path = (
        paths.root / "packet-evidence" / packet_id / "full-suite-evidence.json"
    )
    if not evidence_path.is_file():
        return [
            _finding(
                "final_full_suite_evidence_missing",
                "final replica 1 requires a fresh full local test-suite attestation",
            )
        ], None
    evidence_sha = file_sha256(evidence_path)
    try:
        evidence = load_json_object(evidence_path)
    except StateError as exc:
        return [_finding("final_full_suite_evidence_invalid", str(exc))], evidence_sha
    expected_fields = {
        "fullSuiteEvidenceContract",
        "schemaVersion",
        "runId",
        "packetId",
        "sourceCommit",
        "championSha256",
        "championSourceTreeOid",
        "gates",
    }
    findings: list[dict[str, Any]] = []
    if set(evidence) != expected_fields:
        findings.append(
            _finding(
                "final_full_suite_shape_mismatch",
                "full-suite evidence contains unsupported or missing fields",
            )
        )
    bindings = {
        "fullSuiteEvidenceContract": "InstallerResearchFullSuiteEvidenceV1",
        "schemaVersion": 1,
        "runId": context.run_id,
        "packetId": packet_id,
        "sourceCommit": packet.get("sourceCommit"),
        "championSha256": packet.get("action", {}).get("championSha256"),
        "championSourceTreeOid": packet.get("action", {}).get(
            "championSourceTreeOid"
        ),
    }
    for field, expected in bindings.items():
        if evidence.get(field) != expected:
            findings.append(
                _finding(
                    "final_full_suite_binding_mismatch",
                    f"full-suite evidence differs from the packet at {field}",
                )
            )
    findings.extend(
        _validate_exact_pass_gates(
            evidence.get("gates"),
            expected=FINAL_FULL_SUITE_GATES,
            code_prefix="final_full_suite",
        )
    )
    gates = evidence.get("gates")
    if isinstance(gates, dict):
        for name in sorted(FINAL_FULL_SUITE_GATES):
            gate = gates.get(name)
            expected_sha = gate.get("evidenceSha256") if isinstance(gate, dict) else None
            artifact_path = (
                paths.root
                / "packet-evidence"
                / packet_id
                / "full-suite"
                / f"{name}.json"
            )
            if not artifact_path.is_file() or artifact_path.stat().st_size > 65_536:
                findings.append(
                    _finding(
                        "final_full_suite_artifact_missing_or_oversized",
                        f"retained full-suite artifact {name} is missing or oversized",
                    )
                )
                continue
            if not _lower_sha256(expected_sha) or file_sha256(artifact_path) != expected_sha:
                findings.append(
                    _finding(
                        "final_full_suite_artifact_hash_mismatch",
                        f"retained full-suite artifact {name} differs from its summary",
                    )
                )
            try:
                artifact = load_json_object(artifact_path)
            except StateError as exc:
                findings.append(
                    _finding("final_full_suite_artifact_invalid", str(exc))
                )
                continue
            expected_artifact = {
                "fullSuiteGateArtifactContract": "InstallerResearchFullSuiteGateArtifactV1",
                "schemaVersion": 1,
                "runId": context.run_id,
                "packetId": packet_id,
                "sourceCommit": packet.get("sourceCommit"),
                "gate": name,
                "status": "pass",
            }
            if set(artifact) != {*expected_artifact, "checks"} or any(
                artifact.get(field) != expected
                for field, expected in expected_artifact.items()
            ):
                findings.append(
                    _finding(
                        "final_full_suite_artifact_binding_mismatch",
                        f"retained full-suite artifact {name} is not bound to this packet",
                    )
                )
            checks = artifact.get("checks")
            if (
                not isinstance(checks, list)
                or not checks
                or any(
                    not isinstance(check, dict)
                    or set(check) != {"name", "status"}
                    or not isinstance(check.get("name"), str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}",
                        check["name"],
                    )
                    or check.get("status") != "pass"
                    for check in checks
                )
            ):
                findings.append(
                    _finding(
                        "final_full_suite_artifact_checks_invalid",
                        f"retained full-suite artifact {name} has no exact passing checks",
                    )
                )
            if not _evidence_value_is_path_redacted(artifact, context.repo_root):
                findings.append(
                    _finding(
                        "final_full_suite_artifact_contains_path",
                        f"retained full-suite artifact {name} contains a local path",
                    )
                )
    if (
        evidence_path.stat().st_size > 65_536
        or not _evidence_value_is_path_redacted(evidence, context.repo_root)
    ):
        findings.append(
            _finding(
                "final_full_suite_evidence_unsafe",
                "full-suite evidence is oversized or contains a local path",
            )
        )
    return findings, evidence_sha


def _validate_result_bindings(
    context,
    packet: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    findings = validate_result(payload, expected_run_id=str(context.run_id))
    if payload.get("packetId") != packet.get("packetId"):
        findings.append(_finding("result_packet_id_mismatch", "result packetId differs from the immutable packet"))
    if payload.get("sourceCommit") != packet.get("sourceCommit"):
        findings.append(_finding("result_source_commit_mismatch", "result sourceCommit differs from the immutable packet"))
    expected_parent = str(packet.get("parentChampionId") or "")
    if payload.get("parentChampionId") != expected_parent:
        findings.append(_finding("result_parent_champion_mismatch", "result parentChampionId is stale"))
    if payload.get("hypothesis") != packet.get("hypothesis", {}).get("statement"):
        findings.append(_finding("result_hypothesis_mismatch", "result hypothesis differs from the immutable packet"))
    action = packet.get("action", {})
    if payload.get("comparisonKind") != action.get("comparisonKind"):
        findings.append(_finding("result_comparison_kind_mismatch", "result comparisonKind differs from the immutable packet"))
    if payload.get("compression") != action.get("compression"):
        findings.append(_finding("result_compression_mismatch", "result compression differs from the immutable packet"))
    if (
        action.get("comparisonKind") == "compression"
        and payload.get("payload", {}).get("semanticTreeSha256") != action.get("payloadTreeSha256")
    ):
        findings.append(_finding("result_compression_payload_mismatch", "compression result changed the frozen champion payload tree"))
    manifest = load_manifest(context)
    bindings = manifest["bindings"]
    if payload.get("evaluatorHash") != bindings.get("evaluatorHash"):
        findings.append(_finding("result_evaluator_hash_mismatch", "result evaluatorHash differs from the baseline binding"))
    if payload.get("toolchainHash") != bindings.get("toolchainHash"):
        findings.append(_finding("result_toolchain_hash_mismatch", "result toolchainHash differs from the baseline binding"))
    attribution = payload.get("attribution")
    if not isinstance(attribution, dict) or attribution.get("componentMapSha256") != bindings.get("componentMapSha256"):
        findings.append(_finding("result_component_map_mismatch", "result component map differs from the baseline binding"))
    gates = payload.get("gates")
    if payload.get("decision") == "keep":
        if not isinstance(gates, dict):
            findings.append(_finding("keep_gates_missing", "kept result has no gate object"))
        else:
            missing = sorted(REQUIRED_KEEP_PASS_GATES - set(gates))
            if missing:
                findings.append(_finding("keep_mandatory_gates_missing", "kept result omits mandatory gates: " + ", ".join(missing)))
            incomplete = sorted(name for name in REQUIRED_KEEP_PASS_GATES if _gate_status(gates.get(name)) != "pass")
            if incomplete:
                findings.append(_finding("keep_gate_not_passed", "kept result contains incomplete or failed gates: " + ", ".join(incomplete)))
            comparison_kind = payload.get("comparisonKind")
            comparison_statuses = (
                ("pass", "not_applicable")
                if comparison_kind == "payload"
                else ("not_applicable", "pass")
            )
            if (
                _gate_status(gates.get("compressionBinding")) != comparison_statuses[0]
                or _gate_status(gates.get("semanticPayloadIdentity")) != comparison_statuses[1]
            ):
                findings.append(_finding("keep_comparison_gate_mismatch", "kept result has invalid comparison-kind gate statuses"))
            expected_combined_status = "not_applicable" if comparison_kind == "payload" else "pass"
            if _gate_status(gates.get("combinedInstall50")) != expected_combined_status:
                findings.append(_finding("keep_combined_metric_gate_mismatch", "kept result has an invalid 50-Mbit combined metric gate"))
            gate_findings, _gate_evidence_sha = _validate_packet_gate_evidence(
                context,
                packet,
                expected_parent_champion_id=expected_parent,
                result_gates=gates,
            )
            findings.extend(gate_findings)
        if payload.get("reasonCodes"):
            findings.append(_finding("keep_reason_codes_present", "kept result must not retain failure reason codes"))
    return findings


def _validate_inventory_result(
    context,
    packet: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if payload.get("inventoryContract") != "InstallerResearchInventoryV1" or payload.get("schemaVersion") != 1:
        return [_finding("inventory_contract_mismatch", "packet did not produce InstallerResearchInventoryV1")]
    if payload.get("ok") is not True:
        findings.append(_finding("inventory_not_ok", "inventory reported a failed attribution or parity check"))
    expected = {
        "runId": context.run_id,
        "sourceCommit": packet.get("sourceCommit"),
        "evaluatorHash": current_installer_evaluator_hash(context.repo_root),
        "toolchainHash": file_sha256(paths_for(context).toolchain_manifest),
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            findings.append(_finding(f"inventory_{field}_mismatch", f"inventory {field} differs from the frozen packet/run binding"))
    provenance = payload.get("buildProvenance")
    if not isinstance(provenance, dict) or provenance.get("replicaId") != packet.get("packetId"):
        findings.append(_finding("inventory_replicaId_mismatch", "inventory replicaId differs from packetId"))
        provenance = provenance if isinstance(provenance, dict) else {}
    build_root_sha = str(provenance.get("buildRootSha256") or "")
    if len(build_root_sha) != 64 or any(character not in "0123456789abcdef" for character in build_root_sha):
        findings.append(_finding("inventory_build_root_provenance_missing", "inventory has no valid buildRootSha256"))
    component_map = payload.get("componentMap")
    expected_component_map = file_sha256(context.repo_root / "packaging" / "installer-component-map-v1.json")
    if not isinstance(component_map, dict) or component_map.get("sha256") != expected_component_map:
        findings.append(_finding("inventory_component_map_mismatch", "inventory component map differs from the frozen map"))
    if packet["action"]["kind"] == "baseline-replica" and payload.get("compression") != "bzip2":
        findings.append(_finding("baseline_compression_mismatch", "baseline inventory is not bzip2"))
    installed = payload.get("payload", {}).get("installed") if isinstance(payload.get("payload"), dict) else None
    if not isinstance(installed, dict) or not isinstance(installed.get("totalBytes"), int) or installed.get("totalBytes", 0) <= 0:
        findings.append(_finding("inventory_installed_payload_missing", "inventory lacks a positive installed payload"))
    return findings


def _validate_final_replica_against_champion(
    context,
    packet: dict[str, Any],
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    findings = _validate_inventory_result(context, packet, inventory)
    paths = paths_for(context)
    champion = load_json_object(paths.champion)
    staged = inventory.get("payload", {}).get("staged", {})
    installed = inventory.get("payload", {}).get("installed", {})
    backend = inventory.get("backendExecutable", {})
    pyz = backend.get("pyzDiagnostics", {}) if isinstance(backend, dict) else {}
    comparisons = {
        "installer.length": (inventory.get("installer", {}).get("length"), champion.get("installer", {}).get("length")),
        "payload.stagedBytes": (staged.get("totalBytes"), champion.get("payload", {}).get("stagedBytes")),
        "payload.installedBytes": (installed.get("totalBytes"), champion.get("payload", {}).get("installedBytes")),
        "payload.semanticTreeSha256": (staged.get("semanticTreeSha256"), champion.get("payload", {}).get("semanticTreeSha256")),
        "payload.fileListSha256": (staged.get("fileListSha256"), champion.get("payload", {}).get("fileListSha256")),
        "attribution.pyzInventorySha256": (pyz.get("inventorySha256"), champion.get("attribution", {}).get("pyzInventorySha256")),
        "compression": (inventory.get("compression"), champion.get("compression")),
    }
    for field, (actual, expected) in comparisons.items():
        if actual != expected:
            findings.append(_finding("final_replica_champion_mismatch", f"final replica differs from champion at {field}"))
    return findings


def _validate_final_timing(
    context,
    packet: dict[str, Any],
    inventory: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    if packet.get("action", {}).get("replica") != 2:
        return [], None, None
    paths = paths_for(context)
    evidence_path = paths.root / "packet-evidence" / str(packet["packetId"]) / "install-timing.json"
    if not evidence_path.is_file():
        return (
            [_finding("final_timing_evidence_missing", "final replica 2 requires fresh counterbalanced timing evidence")],
            None,
            None,
        )
    evidence_sha = file_sha256(evidence_path)
    try:
        evidence = load_json_object(evidence_path)
        baseline = load_json_object(paths.baseline)
        baseline_inventory = baseline.get("inventory")
        if not isinstance(baseline_inventory, dict):
            raise StateError("accepted baseline has no embedded inventory")
        summary, gate, invalid = validate_install_measurements(
            evidence,
            evidence_sha256=evidence_sha,
            run_id=str(context.run_id),
            packet_id=str(packet["packetId"]),
            parent_champion_id="baseline",
            source_commit=str(packet["sourceCommit"]),
            parent=baseline_inventory,
            candidate=inventory,
        )
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        return [_finding("final_timing_evidence_invalid", str(exc))], None, evidence_sha
    findings: list[dict[str, Any]] = []
    if invalid or gate.get("status") != "pass":
        findings.append(_finding("final_timing_regression_gate_failed", "final timing evidence is invalid or exceeds the five-percent p50/p95 regression gate"))
    baseline_total = summary.get("baseline", {}).get("totalInstallNanoseconds50P50")
    candidate_total = summary.get("candidate", {}).get("totalInstallNanoseconds50P50")
    if (
        isinstance(baseline_total, bool)
        or not isinstance(baseline_total, int)
        or isinstance(candidate_total, bool)
        or not isinstance(candidate_total, int)
    ):
        findings.append(_finding("final_combined_timing_missing", "final timing summary has no combined 50-Mbit/s p50 values"))
    else:
        required = max(
            MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS,
            (
                int(baseline_total)
                + MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR
                - 1
            )
            // MINIMUM_COMBINED_IMPROVEMENT_PERCENT_DENOMINATOR,
        )
        actual = int(baseline_total) - int(candidate_total)
        if actual < required:
            findings.append(
                _finding(
                    "final_combined_improvement_insufficient",
                    "final 50-Mbit/s download-plus-install p50 improvement is below max(0.5 seconds, 1 percent)",
                )
            )
    return findings, summary, evidence_sha


def _validate_final_protocol(
    context,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any] | None,
    str | None,
    str | None,
    dict[str, str],
]:
    paths = paths_for(context)
    progress = load_progress(context)
    findings: list[dict[str, Any]] = []
    champion_id = progress.get("championId")
    if not isinstance(champion_id, str) or not champion_id or not paths.champion.is_file():
        return [_finding("final_champion_missing", "final protocol requires a kept champion")], None, None, None, {}
    try:
        champion_packet = load_json_object(paths.packet(champion_id))
        champion_result = load_json_object(paths.packet_result(champion_id))
        champion = load_json_object(paths.champion)
    except StateError as exc:
        return [_finding("final_champion_provenance_missing", str(exc))], None, None, None, {}
    if champion != champion_result:
        findings.append(_finding("final_champion_copy_mismatch", "champion.json differs from its immutable packet result"))
    findings.extend(_validate_result_bindings(context, champion_packet, champion))
    if champion.get("decision") != "keep":
        findings.append(_finding("final_champion_decision_invalid", "final champion is not a kept result"))
    try:
        current_source = git_snapshot(context.repo_root)
        current_tree = git_tree_oid(
            context.repo_root,
            str(current_source.get("sourceCommit") or ""),
        )
        champion_tree = git_tree_oid(
            context.repo_root,
            str(champion.get("sourceCommit") or ""),
        )
    except StateError as exc:
        findings.append(_finding("final_current_tree_unavailable", str(exc)))
    else:
        if current_tree != champion_tree:
            findings.append(
                _finding(
                    "final_current_tree_differs_from_champion",
                    "current committed source tree differs from the kept champion tree",
                )
            )

    accepted_ids = progress.get("finalReplicaPacketIds")
    if not isinstance(accepted_ids, list) or len(accepted_ids) != 2 or len(set(accepted_ids)) != 2:
        findings.append(_finding("final_replica_count_invalid", "final protocol requires exactly two accepted fresh replicas"))
        return findings, None, None, None, {}
    replica_ordinals: set[int] = set()
    build_root_hashes: set[str] = set()
    final_timing_summary: dict[str, Any] | None = None
    final_timing_sha256: str | None = None
    final_full_suite_sha256: str | None = None
    final_gate_evidence_sha256s: dict[str, str] = {}
    for packet_id in accepted_ids:
        try:
            packet = load_json_object(paths.packet(str(packet_id)))
            inventory = load_json_object(paths.packet_result(str(packet_id)))
        except StateError as exc:
            findings.append(_finding("final_replica_provenance_missing", str(exc)))
            continue
        if packet.get("action", {}).get("kind") != "final-replica":
            findings.append(_finding("final_replica_packet_kind_invalid", "accepted final evidence is not a final-replica packet"))
            continue
        if packet.get("action", {}).get("championSha256") != file_sha256(paths.champion):
            findings.append(_finding("final_replica_champion_binding_mismatch", "final replica is bound to another champion"))
        champion_tree_binding = packet.get("action", {}).get(
            "championSourceTreeOid"
        )
        try:
            packet_tree = git_tree_oid(
                context.repo_root,
                str(packet.get("sourceCommit") or ""),
            )
            champion_tree = git_tree_oid(
                context.repo_root,
                str(champion.get("sourceCommit") or ""),
            )
        except StateError as exc:
            findings.append(_finding("final_source_tree_unavailable", str(exc)))
        else:
            if (
                champion_tree_binding != packet_tree
                or champion_tree_binding != champion_tree
            ):
                findings.append(
                    _finding(
                        "final_source_tree_binding_mismatch",
                        "final replica source tree differs from the kept champion tree",
                    )
                )
        replica_ordinals.add(packet["action"].get("replica"))
        build_root_hash = str(inventory.get("buildProvenance", {}).get("buildRootSha256") or "")
        if build_root_hash:
            build_root_hashes.add(build_root_hash)
        findings.extend(_validate_final_replica_against_champion(context, packet, inventory))
        gate_findings, gate_sha = _validate_final_gate_evidence(context, packet)
        findings.extend(gate_findings)
        if gate_sha is not None:
            final_gate_evidence_sha256s[str(packet_id)] = gate_sha
        suite_findings, suite_sha = _validate_final_full_suite_evidence(
            context,
            packet,
        )
        findings.extend(suite_findings)
        if suite_sha is not None:
            final_full_suite_sha256 = suite_sha
        timing_findings, timing_summary, timing_sha = _validate_final_timing(
            context,
            packet,
            inventory,
        )
        findings.extend(timing_findings)
        if timing_summary is not None:
            final_timing_summary = timing_summary
            final_timing_sha256 = timing_sha
    if replica_ordinals != {1, 2}:
        findings.append(_finding("final_replica_ordinals_invalid", "final protocol requires replica ordinals 1 and 2"))
    if len(build_root_hashes) != 2:
        findings.append(_finding("final_replica_build_roots_reused", "final replicas must come from distinct fresh build roots"))
    if progress.get("activePacketId") or paths.pending_packet.is_file():
        findings.append(_finding("final_packet_still_active", "final protocol cannot close with an active or pending packet"))
    return (
        findings,
        final_timing_summary,
        final_timing_sha256,
        final_full_suite_sha256,
        dict(sorted(final_gate_evidence_sha256s.items())),
    )


def _record_packet_learning(
    context,
    progress: dict[str, Any],
    packet: dict[str, Any],
    *,
    decision: str,
    result: dict[str, Any] | None,
    duration_seconds: float,
) -> dict[str, Any]:
    policy = context.config.get("lanePolicy")
    if not isinstance(policy, dict):
        raise StateError("lane learning policy is missing")
    lane_id = safe_lane_id(packet.get("lane"))
    learning = progress.setdefault("laneLearning", {})
    if not isinstance(learning, dict):
        raise StateError("lane learning state is invalid")
    lane = learning.setdefault(
        lane_id,
        {
            "alpha": float(policy["betaPriorAlpha"]),
            "beta": float(policy["betaPriorBeta"]),
            "experiments": 0,
            "keeps": 0,
            "validDiscards": 0,
            "durationEwmaSeconds": None,
            "locked": False,
            "reductionObservations": [],
            "validDiscardReasons": [],
        },
    )
    ewma_alpha = float(policy["ewmaAlpha"])
    prior_duration = lane.get("durationEwmaSeconds")
    lane["durationEwmaSeconds"] = round(
        max(0.0, duration_seconds)
        if prior_duration is None
        else ewma_alpha * max(0.0, duration_seconds)
        + (1.0 - ewma_alpha) * float(prior_duration),
        6,
    )
    lane["experiments"] = int(lane.get("experiments", 0)) + 1
    lane["lastDecision"] = decision
    if packet["action"]["kind"] == "measure-candidate" and isinstance(result, dict):
        expected = packet.get("hypothesis", {}).get("expectedReductionBytes")
        delta = result.get("installer", {}).get("deltaBytes")
        actual = -delta if isinstance(delta, int) and not isinstance(delta, bool) else None
        observation = {
            "packetId": packet["packetId"],
            "expectedReductionBytes": expected,
            "actualReductionBytes": actual,
            "decision": decision,
        }
        lane.setdefault("reductionObservations", []).append(observation)
        lane["lastExpectedReductionBytes"] = expected
        lane["lastActualReductionBytes"] = actual
        if isinstance(expected, int) and isinstance(actual, int):
            lane["lastReductionErrorBytes"] = actual - expected
        if decision == "keep":
            lane["alpha"] = float(lane.get("alpha", 0)) + 1.0
            lane["keeps"] = int(lane.get("keeps", 0)) + 1
            progress["validDiscardsWithoutEvidence"] = 0
        elif decision == "discard":
            lane["beta"] = float(lane.get("beta", 0)) + 1.0
            lane["validDiscards"] = int(lane.get("validDiscards", 0)) + 1
            reason_codes = [
                item
                for item in result.get("reasonCodes", [])
                if isinstance(item, str)
            ]
            lane.setdefault("validDiscardReasons", []).append(
                {"packetId": packet["packetId"], "reasonCodes": reason_codes}
            )
            progress["validDiscardsWithoutEvidence"] = int(
                progress.get("validDiscardsWithoutEvidence", 0)
            ) + 1
            if lane["validDiscards"] >= int(policy["lockAfterValidDiscards"]):
                lane["locked"] = True
            if progress["validDiscardsWithoutEvidence"] >= int(
                policy["plateauAfterValidDiscards"]
            ):
                progress["phase"] = "plateau"
    beta_total = float(lane.get("alpha", 0)) + float(lane.get("beta", 0))
    lane["posteriorKeepProbability"] = round(
        float(lane.get("alpha", 0)) / beta_total if beta_total > 0 else 0.0,
        6,
    )
    return {
        "lane": lane_id,
        "alpha": lane["alpha"],
        "beta": lane["beta"],
        "posteriorKeepProbability": lane["posteriorKeepProbability"],
        "durationEwmaSeconds": lane["durationEwmaSeconds"],
        "validDiscards": lane["validDiscards"],
        "locked": lane["locked"],
        "validDiscardsWithoutEvidence": progress.get("validDiscardsWithoutEvidence", 0),
        "plateau": progress.get("phase") == "plateau",
    }


def _result_path(context, packet: dict[str, Any]) -> Path:
    paths = paths_for(context)
    action = packet["action"]
    kind = action["kind"]
    if kind == "baseline-replica":
        replica = action.get("replica")
        if replica not in (1, 2):
            raise StateError("baseline-replica action requires replica 1 or 2")
        expected = paths.baseline_replica(replica)
    else:
        expected = paths.packet_result(str(packet["packetId"]))
    requested = str(action.get("resultRelativePath") or "").replace("\\", "/")
    expected_relative = expected.relative_to(paths.root).as_posix()
    if requested != expected_relative:
        raise StateError(f"packet resultRelativePath must be {expected_relative}")
    resolved = (paths.root / Path(requested)).resolve()
    if not resolved.is_relative_to(paths.root.resolve()):
        raise StateError("packet result path escaped the run root")
    return resolved


def _dispatch_command(context, packet: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    action = packet["action"]
    dispatch = action.get("dispatch")
    if not isinstance(dispatch, dict):
        raise StateError("packet action requires a dispatch object")
    driver = dispatch.get("driver")
    entrypoint = _safe_relative_entrypoint(dispatch.get("entrypoint"))
    arguments = _safe_arguments(dispatch.get("arguments"))
    path = context.repo_root.joinpath(*entrypoint.split("/"))
    if not path.is_file():
        raise StateError(f"packet entrypoint does not exist: {entrypoint}")
    _validate_dispatch_policy(
        context,
        packet,
        driver=driver,
        entrypoint=entrypoint,
        arguments=arguments,
    )
    if driver == "powershell-file":
        command = ["powershell.exe", "-NoProfile", "-File", str(path), *arguments]
    else:
        launcher = context.repo_root / "scripts" / "project-python.cmd"
        command = [str(launcher), str(path), *arguments]
    timeout_seconds = packet["action"]["timeoutSeconds"]
    return _run_bounded_command(
        command,
        cwd=context.repo_root,
        timeout_seconds=timeout_seconds,
    )


def _preflight_required_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    codes = {str(item.get("code")) for item in report.get("findings", [])}
    actions: list[dict[str, Any]] = []
    if codes & {"wheelhouse_manifest_missing_or_invalid", "environment_manifest_missing_or_invalid"}:
        actions.append(
            {
                "id": "prepare-hermetic-python",
                "command": (
                    "powershell.exe -NoProfile -File .\\scripts\\prepare_installer_research_environment.ps1 "
                    "-RunId <run-id> -BasePython <explicit-python.exe>"
                ),
            }
        )
    if "toolchain_manifest_missing_or_invalid" in codes:
        actions.append(
            {
                "id": "prepare-pinned-toolchain",
                "command": (
                    "powershell.exe -NoProfile -File .\\scripts\\prepare_installer_research_toolchain.ps1 "
                    "-RunId <run-id>"
                ),
            }
        )
    if codes & {
        "youtube_holdout_snapshot_missing_or_invalid",
        "youtube_holdout_snapshot_incomplete",
        "youtube_holdout_snapshot_case_unvalidated",
    }:
        actions.append(
            {
                "id": "validate-youtube-holdouts",
                "instruction": (
                    "Create one hashed InstallerSizeYoutubeHoldoutProbeV1 Deno report per distinct, current "
                    "regular, signature, Shorts, Music, caption, and completed-live-replay case, then bind "
                    "them in preflight/youtube-holdouts.snapshot.json."
                ),
            }
        )
    if "preexisting_scriber_process" in codes:
        actions.append(
            {
                "id": "stop-scoped-stale-processes",
                "instruction": "Stop only the exact Scriber processes listed by the doctor, then rerun with -Resume.",
            }
        )
    return actions


def _start_session_locked(
    context,
    *,
    resume: bool,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    paths, _ = initialize_run(
        context,
        resume=resume,
        now=now,
        _lock_held=True,
    )
    progress = load_progress(context)
    if not progress.get("researchStartedAtUtc"):
        if not paths.preflight.is_file():
            report = run_doctor(context, phase="prepare", now=now)
            write_json_atomic(paths.preflight_dir / "prepare-doctor-latest.json", report)
            if report["ok"]:
                write_preflight(
                    context,
                    findings=report["findings"],
                    accepted=True,
                    evidence_hashes=report["evidenceHashes"],
                    now=now,
                )
            else:
                payload = {
                    "sessionContract": "InstallerSizeSessionEntryV1",
                    "schemaVersion": 1,
                    "profile": "installer-size",
                    "runId": context.run_id,
                    "initialized": True,
                    "resumed": resume,
                    "researchClockStarted": False,
                    "phase": "prepare",
                    "doctor": report,
                    "requiredActions": _preflight_required_actions(report),
                }
                return 2, payload
        if (
            accepted_baseline_replica_packet_id(context, 1)
            and accepted_baseline_replica_packet_id(context, 2)
        ) or (
            paths.baseline.is_file()
            and load_json_object(paths.baseline).get("accepted") is False
        ):
            baseline_acceptance = _accept_baseline(context)
            if not paths.baseline.is_file():
                return 2, {
                    "sessionContract": "InstallerSizeSessionEntryV1",
                    "schemaVersion": 1,
                    "profile": "installer-size",
                    "runId": context.run_id,
                    "initialized": True,
                    "resumed": resume,
                    "researchClockStarted": False,
                    "phase": "baseline-validation",
                    "baselineAcceptance": _baseline_acceptance_process_payload(
                        baseline_acceptance,
                        context.repo_root,
                    ),
                    "requiredActions": [
                        {
                            "id": "retry-baseline-pair-acceptance",
                            "instruction": "Resume the same RunId; no baseline artifact was committed.",
                        }
                    ],
                }
            baseline = _load_baseline_pair(context)
            if baseline.get("accepted") is not True:
                return 2, {
                    "sessionContract": "InstallerSizeSessionEntryV1",
                    "schemaVersion": 1,
                    "profile": "installer-size",
                    "runId": context.run_id,
                    "initialized": True,
                    "resumed": resume,
                    "researchClockStarted": False,
                    "phase": "baseline-rejected",
                    "baselineAcceptance": {
                        **_baseline_acceptance_process_payload(
                            baseline_acceptance,
                            context.repo_root,
                        ),
                        "accepted": False,
                        "fatalForRunId": True,
                    },
                    "requiredActions": [
                        {
                            "id": "start-new-run-id",
                            "instruction": "The immutable baseline pair was rejected; start a new RunId after correcting the cause.",
                        }
                    ],
                }
            report = run_doctor(
                context,
                phase="run",
                now=now,
                allow_unarmed_run=True,
            )
            write_json_atomic(paths.preflight_dir / "pre-arm-doctor.json", report)
            if report["ok"]:
                arm_now = now or utc_now()
                arm_research_clock(
                    context,
                    baseline=baseline,
                    doctor_report=report,
                    now=arm_now,
                )
            else:
                return 2, {
                    "sessionContract": "InstallerSizeSessionEntryV1",
                    "schemaVersion": 1,
                    "profile": "installer-size",
                    "runId": context.run_id,
                    "initialized": True,
                    "resumed": resume,
                    "researchClockStarted": False,
                    "phase": "baseline-validation",
                    "doctor": report,
                    "requiredActions": [],
                }
    state = summarize(context, now=now)
    state["sessionContract"] = "InstallerSizeSessionEntryV1"
    state["schemaVersion"] = 1
    state["initialized"] = True
    state["resumed"] = resume
    state["researchClockStarted"] = bool(state.get("researchStartedAtUtc"))
    state["safeNextStep"] = recommend_next(context, now=now)["safeNextStep"]
    return 0, state


def start_session(
    context,
    *,
    resume: bool,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    with acquire_dispatch_lock(context):
        return _start_session_locked(context, resume=resume, now=now)


def recommend_next(context, *, now: datetime | None = None) -> dict[str, Any]:
    paths = paths_for(context)
    state = summarize(context, now=now)
    if pending_packet_requires_resume(context):
        safe = "resume-run-to-reconcile-pending-tail"
    elif not state["preflightAccepted"]:
        safe = "complete-preflight"
    elif state["pendingPacket"]:
        pending = load_json_object(paths.pending_packet)
        remaining = state.get("remainingSeconds")
        action = pending.get("action") if isinstance(pending, dict) else None
        timeout_seconds = (
            action.get("timeoutSeconds") if isinstance(action, dict) else None
        )
        if remaining == 0:
            safe = "abandon-pending-deadline-expired"
        elif (
            isinstance(action, dict)
            and action.get("kind") == "measure-candidate"
            and isinstance(remaining, int)
            and isinstance(timeout_seconds, int)
            and remaining
            <= int(state["effectiveFinalizationReserveSeconds"])
            + timeout_seconds
        ):
            safe = "abandon-pending-for-finalization-reserve"
        else:
            safe = "dispatch-existing-packet"
    elif not accepted_baseline_replica_packet_id(context, 1):
        safe = "formulate-baseline-replica-1-packet"
    elif not accepted_baseline_replica_packet_id(context, 2):
        safe = "formulate-baseline-replica-2-packet"
    elif (
        paths.baseline.is_file()
        and load_json_object(paths.baseline).get("accepted") is False
    ):
        safe = "start-new-run-id-after-baseline-rejection"
    elif not paths.baseline.is_file() or not state["researchStartedAtUtc"]:
        safe = "accept-baseline-and-arm-fixed-deadline"
    elif state["remainingSeconds"] == 0:
        safe = "finalize-research"
    elif state["phase"] == "complete":
        safe = "wait-for-immutable-research-deadline"
    elif state["phase"] == "finalizing":
        accepted = state.get("finalReplicaPacketIds", [])
        safe = f"formulate-final-replica-{len(accepted) + 1}-packet"
    elif state["phase"] == "plateau":
        safe = "begin-finalization-after-plateau"
    else:
        reserve = int(state["effectiveFinalizationReserveSeconds"])
        lane_policy = context.config["lanePolicy"]
        exploration_due = (
            (int(state.get("packetSequence", 0)) + 1)
            % int(lane_policy["explorationEveryPackets"])
            == 0
        )
        if int(state["remainingSeconds"] or 0) <= reserve:
            safe = "begin-finalization"
        elif exploration_due:
            safe = "formulate-high-potential-exploration-packet"
        else:
            safe = "formulate-next-hypothesis-packet"
    lane_policy = context.config["lanePolicy"]
    exploration_due = (
        (int(state.get("packetSequence", 0)) + 1)
        % int(lane_policy["explorationEveryPackets"])
        == 0
    )
    return {
        "recommendationContract": "InstallerSizeRecommendationV1",
        "schemaVersion": 1,
        "profile": "installer-size",
        "runId": context.run_id,
        "safeNextStep": safe,
        "learningPolicy": {
            "lockedLanes": state.get("lockedLanes", []),
            "plateau": state.get("phase") == "plateau",
            "validDiscardsWithoutEvidence": state.get("validDiscardsWithoutEvidence", 0),
            "explorationDue": exploration_due,
            "explorationEveryPackets": lane_policy["explorationEveryPackets"],
            "minimumExplorationPotentialBytes": lane_policy["minimumExplorationPotentialBytes"],
            "effectiveFinalizationReserveSeconds": state["effectiveFinalizationReserveSeconds"],
        },
        "state": state,
    }


def _load_baseline_pair(context) -> dict[str, Any]:
    baseline = load_json_object(paths_for(context).baseline)
    if (
        baseline.get("baselineContract") != "InstallerResearchBaselineV1"
        or baseline.get("runId") != context.run_id
        or not isinstance(baseline.get("accepted"), bool)
    ):
        raise StateError("baseline pair artifact has invalid identity or status")
    return baseline


def _baseline_acceptance_process_payload(
    result: subprocess.CompletedProcess[str] | None,
    repo_root: Path,
) -> dict[str, Any]:
    if result is None:
        return {"attempted": False, "exitCode": None, "stdout": "", "stderr": ""}
    return {
        "attempted": True,
        "exitCode": result.returncode,
        "stdout": _redact_output(result.stdout, repo_root),
        "stderr": _redact_output(result.stderr, repo_root),
    }


def _accept_baseline(context) -> subprocess.CompletedProcess[str] | None:
    paths = paths_for(context)
    if paths.baseline.is_file():
        return None
    launcher = context.repo_root / "scripts" / "project-python.cmd"
    evaluator = context.repo_root / "scripts" / "installer_research.py"
    command = [
            str(launcher),
            str(evaluator),
            "accept-baseline",
            "--first-inventory",
            str(paths.baseline_replica(1)),
            "--second-inventory",
            str(paths.baseline_replica(2)),
            "--output",
            str(paths.baseline),
        ]
    try:
        return subprocess.run(
            command,
            cwd=str(context.repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=stdout,
            stderr="baseline acceptance timed out",
        )


def _assert_packet_doctor_ready(
    context,
    packet: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    phase = "prepare" if packet["action"]["kind"] == "baseline-replica" else "run"
    report = run_doctor(context, phase=phase, now=now)
    if report.get("ok") is not True:
        codes = sorted(
            {
                str(item.get("code") or "doctor_blocked")
                for item in report.get("findings", [])
                if isinstance(item, dict) and item.get("level") == "block"
            }
        )
        detail = ", ".join(codes[:8]) or "doctor_blocked"
        raise StateError(f"packet dispatch Doctor failed: {detail}")


def _check_packet_phase(context, packet: dict[str, Any], *, now: datetime | None = None) -> None:
    paths = paths_for(context)
    progress = load_progress(context)
    kind = packet["action"]["kind"]
    timeout_seconds = packet["action"]["timeoutSeconds"]
    if not paths.preflight.is_file() or load_json_object(paths.preflight).get("accepted") is not True:
        raise StateError("packets cannot run before accepted preflight")
    source = git_snapshot(context.repo_root)
    if source["dirtyEntries"]:
        raise StateError("packet dispatch requires a clean Git worktree")
    if source["sourceCommit"] != packet["sourceCommit"]:
        raise StateError("packet sourceCommit differs from the current Git HEAD")
    if kind == "baseline-replica":
        if progress.get("researchStartedAtUtc"):
            raise StateError("baseline replicas cannot run after the research clock started")
        session_source = load_json_object(paths.session_init).get("sourceCommit")
        if packet["sourceCommit"] != session_source:
            raise StateError("baseline replica sourceCommit differs from session initialization")
    else:
        remaining = remaining_seconds(context, now=now)
        if remaining is None:
            raise StateError("candidate packets cannot run before the research clock starts")
        if remaining <= 0:
            raise StateError("candidate packet cannot start after the immutable deadline")
        if kind == "measure-candidate":
            if progress.get("phase") != "run":
                raise StateError("new hypotheses cannot run after finalization starts")
            reserve = effective_finalization_reserve(context, progress=progress)
            if remaining <= reserve + timeout_seconds:
                raise StateError("candidate packet cannot consume the finalization reserve")
            lane_id = safe_lane_id(packet.get("lane"))
            learning = progress.get("laneLearning", {})
            lane_state = learning.get(lane_id) if isinstance(learning, dict) else None
            if isinstance(lane_state, dict) and lane_state.get("locked") is True:
                raise StateError("candidate packet lane is locked after three valid discards")
            if packet.get("exploration") is True:
                lane_policy = context.config["lanePolicy"]
                next_sequence = int(progress.get("packetSequence", 0)) + 1
                every = int(lane_policy["explorationEveryPackets"])
                expected = packet.get("hypothesis", {}).get("expectedReductionBytes")
                if next_sequence % every != 0:
                    raise StateError("exploration packet is allowed only at the frozen cadence")
                if lane_state is not None:
                    raise StateError("exploration packet must use an untested lane")
                if not isinstance(expected, int) or expected < int(
                    lane_policy["minimumExplorationPotentialBytes"]
                ):
                    raise StateError("exploration packet potential is below the frozen minimum")
            expected_parent = str(progress.get("championId") or "baseline")
            if packet.get("parentChampionId") != expected_parent:
                raise StateError("candidate packet parentChampionId is stale")
            if packet["action"].get("comparisonKind") == "payload":
                parent_source = (
                    load_json_object(paths.champion).get("sourceCommit")
                    if progress.get("championId")
                    else load_manifest(context).get("sourceCommit")
                )
                expected_parent_tree = git_tree_oid(
                    context.repo_root,
                    str(parent_source or ""),
                )
                if packet.get("parentSourceTreeOid") != expected_parent_tree:
                    raise StateError(
                        "candidate packet parentSourceTreeOid is stale"
                    )
                parents = git_parent_oids(
                    context.repo_root,
                    str(packet["sourceCommit"]),
                )
                if len(parents) != 1:
                    raise StateError(
                        "payload candidate sourceCommit must be a non-merge commit"
                    )
                actual_parent_tree = git_tree_oid(
                    context.repo_root,
                    parents[0],
                )
                if actual_parent_tree != expected_parent_tree:
                    raise StateError(
                        "payload candidate is not based on the current champion source tree"
                    )
            else:
                parent = (
                    load_json_object(paths.champion)
                    if progress.get("championId")
                    else load_json_object(paths.baseline)
                )
                parent_tree = (
                    parent.get("payload", {}).get("semanticTreeSha256")
                    if progress.get("championId")
                    else parent.get("semanticTreeSha256")
                )
                parent_source = (
                    parent.get("sourceCommit")
                    if progress.get("championId")
                    else load_manifest(context).get("sourceCommit")
                )
                if packet["action"].get("payloadTreeSha256") != parent_tree:
                    raise StateError("compression packet is not bound to the attested champion payload tree")
                if packet.get("sourceCommit") != parent_source:
                    raise StateError("compression packet must reuse the champion source without a rebuild commit")
        else:
            reserve = effective_finalization_reserve(context, progress=progress)
            if progress.get("phase") not in {"finalizing", "plateau"} and remaining > reserve:
                raise StateError("final replicas cannot start before the finalization reserve")
            if timeout_seconds >= remaining:
                raise StateError("final replica timeout does not fit before the immutable deadline")
            champion_id = progress.get("championId")
            if not champion_id or not paths.champion.is_file():
                raise StateError("final replicas require a kept champion")
            champion = load_json_object(paths.champion)
            expected_tree = packet["action"].get("championSourceTreeOid")
            packet_tree = git_tree_oid(context.repo_root, packet["sourceCommit"])
            champion_tree = git_tree_oid(
                context.repo_root,
                str(champion.get("sourceCommit") or ""),
            )
            if expected_tree != packet_tree or expected_tree != champion_tree:
                raise StateError(
                    "final replica source tree differs from the kept champion tree"
                )
            if packet["action"]["championSha256"] != file_sha256(paths.champion):
                raise StateError("final replica championSha256 is stale")
            accepted = progress.get("finalReplicaPacketIds", [])
            if not isinstance(accepted, list):
                raise StateError("final replica progress is invalid")
            replica = packet["action"]["replica"]
            accepted_ordinals = {
                load_json_object(paths.packet(packet_id))["action"]["replica"]
                for packet_id in accepted
            }
            if replica in accepted_ordinals:
                raise StateError(f"final replica {replica} is already accepted")
            if replica == 2 and 1 not in accepted_ordinals:
                raise StateError("final replica 1 must be accepted before replica 2")


def _dispatch_next_locked(
    context,
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    paths = paths_for(context)
    if not paths.pending_packet.is_file():
        raise StateError("next requires one existing pending-packet.json")
    packet = load_json_object(paths.pending_packet)
    validate_packet(packet, run_id=str(context.run_id))
    _assert_packet_doctor_ready(context, packet, now=now)
    _check_packet_phase(context, packet, now=now)
    packet_id = safe_packet_id(packet["packetId"])
    immutable_packet = paths.packet(packet_id)
    store_immutable_json(immutable_packet, packet)
    result_path = _result_path(context, packet)
    if result_path.exists():
        raise StateError("packet result already exists; packet dispatch is not repeatable")
    progress = load_progress(context)
    if progress.get("activePacketId"):
        raise StateError("another packet is already active")
    prior_champion_id = progress.get("championId")
    prior_champion_sha256: str | None = None
    if prior_champion_id:
        if not paths.champion.is_file():
            raise StateError("durable champion progress has no champion artifact")
        prior_champion_sha256 = file_sha256(paths.champion)
    elif paths.champion.is_file():
        raise StateError("champion artifact exists without durable champion progress")
    started = now or utc_now()
    started_monotonic = time.monotonic()
    progress["activePacketId"] = packet_id
    progress["packetSequence"] = int(progress.get("packetSequence", 0)) + 1
    if packet["action"]["kind"] == "final-replica":
        progress["phase"] = "finalizing"
    progress["updatedAtUtc"] = format_utc(started)
    write_json_atomic(paths.progress, progress)
    append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet_id,
            "packetSha256": file_sha256(immutable_packet),
            "actionKind": packet["action"]["kind"],
        },
        now=started,
    )

    dispatch_error = ""
    try:
        result = _dispatch_command(context, packet)
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except (OSError, subprocess.TimeoutExpired, StateError) as exc:
        exit_code = 2
        stdout = ""
        stderr = ""
        dispatch_error = type(exc).__name__
    finished = utc_now()
    decision = "crash"
    result_sha256 = ""
    result_contract = ""
    result_findings: list[dict[str, Any]] = []
    result_payload: dict[str, Any] | None = None
    final_timing_summary: dict[str, Any] | None = None
    final_timing_sha256: str | None = None
    final_gate_evidence_sha256: str | None = None
    final_full_suite_sha256: str | None = None
    packet_gate_evidence_path = (
        paths.root / "packet-evidence" / packet_id / "gate-evidence.json"
    )
    packet_gate_evidence_sha256 = (
        file_sha256(packet_gate_evidence_path)
        if packet_gate_evidence_path.is_file()
        else None
    )
    if exit_code in (0, 1) and result_path.is_file():
        payload = load_result(result_path)
        result_payload = payload
        result_contract = str(payload.get("resultContract") or payload.get("inventoryContract") or "")
        action_kind = packet["action"]["kind"]
        if action_kind == "baseline-replica":
            result_findings = _validate_inventory_result(context, packet, payload)
            if not result_findings:
                decision = "baseline_accept"
        elif action_kind == "measure-candidate":
            result_findings = _validate_result_bindings(context, packet, payload)
            if not result_findings:
                decision = str(payload["decision"])
                if decision not in {
                    "keep",
                    "discard",
                    "checks_failed",
                    "invalid_measurement",
                    "measure_only",
                }:
                    result_findings.append(_finding("candidate_decision_invalid", "candidate evaluator returned an invalid terminal decision"))
                    decision = "crash"
                if decision == "keep":
                    progress["championId"] = packet_id
        else:
            result_findings = _validate_final_replica_against_champion(context, packet, payload)
            gate_findings, final_gate_evidence_sha256 = (
                _validate_final_gate_evidence(context, packet)
            )
            result_findings.extend(gate_findings)
            suite_findings, final_full_suite_sha256 = (
                _validate_final_full_suite_evidence(context, packet)
            )
            result_findings.extend(suite_findings)
            timing_findings, final_timing_summary, final_timing_sha256 = _validate_final_timing(
                context,
                packet,
                payload,
            )
            result_findings.extend(timing_findings)
            if not result_findings:
                decision = "final_replica_accept"
                accepted_ids = progress.setdefault("finalReplicaPacketIds", [])
                accepted_ids.append(packet_id)
                if len(accepted_ids) == 2:
                    progress["phase"] = "complete"
        if not result_findings:
            result_sha256 = file_sha256(result_path)
    failed_result: dict[str, str] | None = None
    if decision == "crash" and result_path.is_file() and not result_sha256:
        failed_path = paths.failed_result(packet_id)
        if failed_path.exists():
            raise StateError("failed packet result quarantine already exists")
        failed_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(result_path, failed_path)
        failed_result = {
            "relativePath": failed_path.relative_to(paths.root).as_posix(),
            "sha256": file_sha256(failed_path),
        }
    last_run = {
        "lastRunContract": "InstallerSizeLastRunV1",
        "schemaVersion": 1,
        "runId": context.run_id,
        "packetId": packet_id,
        "packetSha256": file_sha256(immutable_packet),
        "priorChampionId": prior_champion_id,
        "priorChampionSha256": prior_champion_sha256,
        "startedAtUtc": format_utc(started),
        "finishedAtUtc": format_utc(finished),
        "exitCode": exit_code,
        "dispatchError": dispatch_error or None,
        "stdout": _redact_output(stdout, context.repo_root),
        "stderr": _redact_output(stderr, context.repo_root),
        "resultContract": result_contract,
        "resultSha256": result_sha256 or None,
        "resultFindings": result_findings,
        "failedResult": failed_result,
        "gateEvidenceSha256": packet_gate_evidence_sha256,
        "finalGateEvidenceSha256": final_gate_evidence_sha256,
        "finalFullSuiteSha256": final_full_suite_sha256,
        "finalTimingSha256": final_timing_sha256,
        "finalTimingSummary": final_timing_summary,
        "decision": decision,
    }
    learning_update = _record_packet_learning(
        context,
        progress,
        packet,
        decision=decision,
        result=result_payload,
        duration_seconds=time.monotonic() - started_monotonic,
    )
    last_run["learningUpdate"] = learning_update
    write_json_atomic(paths.last_run, last_run)
    progress["activePacketId"] = None
    if packet["action"]["kind"] == "baseline-replica" and decision == "baseline_accept":
        progress["baselineReplicasAccepted"] = sum(
            1 for index in (1, 2) if paths.baseline_replica(index).is_file()
        )
    progress["updatedAtUtc"] = format_utc(finished)
    write_json_atomic(paths.progress, progress)
    if decision == "keep" and result_payload is not None:
        write_json_atomic(paths.champion, result_payload)
    append_ledger(
        paths.ledger,
        event="packet_completed",
        payload=packet_completed_ledger_payload(
            packet_id=packet_id,
            last_run=last_run,
            last_run_sha256=file_sha256(paths.last_run),
        ),
        now=finished,
    )
    clear_pending_packet(paths, expected_packet_id=packet_id)

    baseline_acceptance: dict[str, Any] | None = None
    baseline_pair_ready = True
    if (
        packet["action"]["kind"] == "baseline-replica"
        and decision == "baseline_accept"
        and paths.baseline_replica(1).is_file()
        and paths.baseline_replica(2).is_file()
    ):
        accepted = _accept_baseline(context)
        baseline_acceptance = _baseline_acceptance_process_payload(
            accepted,
            context.repo_root,
        )
        if accepted is not None and accepted.returncode != 0:
            baseline_pair_ready = False
        if paths.baseline.is_file():
            baseline_pair = _load_baseline_pair(context)
            baseline_acceptance["accepted"] = baseline_pair.get("accepted") is True
            if baseline_pair.get("accepted") is not True:
                baseline_pair_ready = False
                baseline_acceptance["fatalForRunId"] = True
            if baseline_pair_ready:
                pre_arm_now = now or utc_now()
                report = run_doctor(
                    context,
                    phase="run",
                    now=pre_arm_now,
                    allow_unarmed_run=True,
                )
                write_json_atomic(paths.preflight_dir / "pre-arm-doctor.json", report)
                baseline_acceptance["doctorOk"] = report.get("ok") is True
                if report.get("ok") is not True:
                    baseline_pair_ready = False
            if baseline_pair_ready:
                arm_now = now or utc_now()
                arm_research_clock(
                    context,
                    baseline=baseline_pair,
                    doctor_report=report,
                    now=arm_now,
                )
        else:
            baseline_pair_ready = False
            baseline_acceptance["accepted"] = None
            baseline_acceptance["retryable"] = True
    response = {
        "dispatchContract": "InstallerSizePacketDispatchV1",
        "schemaVersion": 1,
        "profile": "installer-size",
        "runId": context.run_id,
        "packetId": packet_id,
        "decision": decision,
        "exitCode": exit_code,
        "lastRunSha256": file_sha256(paths.last_run),
        "baselineAcceptance": baseline_acceptance,
        "state": summarize(context),
    }
    return (
        0
        if (
            decision
            in {"baseline_accept", "final_replica_accept", "keep", "discard", "measure_only"}
            and baseline_pair_ready
        )
        else 2,
        response,
    )


def dispatch_next(context, *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    with acquire_dispatch_lock(context):
        return _dispatch_next_locked(context, now=now)


def finalize_preview(context, *, now: datetime | None = None) -> dict[str, Any]:
    paths = paths_for(context)
    state = summarize(context, now=now)
    report = run_doctor(context, phase="finalize", now=now)
    (
        final_findings,
        final_timing_summary,
        final_timing_sha256,
        final_full_suite_sha256,
        final_gate_evidence_sha256s,
    ) = _validate_final_protocol(context)
    research_complete = bool(state.get("remainingSeconds") == 0)
    champion_ready = bool(report["ok"] and research_complete and not final_findings)
    payload = {
        "finalizeContract": "InstallerSizeFinalizePreviewV1",
        "schemaVersion": 1,
        "profile": "installer-size",
        "runId": context.run_id,
        "researchComplete": research_complete,
        "researchChampionReady": champion_ready,
        "releaseReady": False,
        "releaseBlockers": [
            "signed-tag-release-not-run",
            "authenticode-and-updater-signing-not-proven",
            "provider-secret-e2e-gates-not-proven",
            "publication-and-updater-discoverability-not-proven",
        ],
        "finalProtocol": {
            "ok": not final_findings,
            "findings": final_findings,
            "acceptedReplicaPacketIds": state.get("finalReplicaPacketIds", []),
            "fullSuiteEvidenceSha256": final_full_suite_sha256,
            "gateEvidenceSha256s": final_gate_evidence_sha256s,
            "timingEvidenceSha256": final_timing_sha256,
            "timingSummary": final_timing_summary,
        },
        "state": state,
        "doctor": report,
    }
    write_json_atomic(paths.finalize_preview, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scriber installer-size AutoResearch session harness.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration-seconds", type=int)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--resume", action="store_true")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--phase", choices=("prepare", "run", "finalize"), default="prepare")
    doctor_parser.add_argument("--explain", action="store_true")
    state_parser = subparsers.add_parser("state")
    state_parser.add_argument("--compact", action="store_true")
    rec = subparsers.add_parser("recommend-next")
    rec.add_argument("--compact", action="store_true")
    onboarding = subparsers.add_parser("onboarding-packet")
    onboarding.add_argument("--compact", action="store_true")
    subparsers.add_parser("next")
    abandon = subparsers.add_parser("abandon-pending")
    abandon.add_argument(
        "--reason",
        required=True,
        choices=(
            "deadline_expired",
            "finalization_reserve",
            "source_superseded",
            "operator_canceled",
        ),
    )
    subparsers.add_parser("finalize-preview")
    args = parser.parse_args(argv)
    try:
        context = resolve_profile_context(
            Path(args.repo_root),
            profile="installer-size",
            run_id=args.run_id,
            duration_seconds=args.duration_seconds,
            require_run_id=True,
        )
        if args.command == "start":
            exit_code, payload = start_session(context, resume=args.resume)
        elif args.command == "doctor":
            payload = run_doctor(context, phase=args.phase)
            exit_code = 0 if payload["ok"] else 1
        elif args.command == "state":
            payload = summarize(context)
            exit_code = 0
        elif args.command == "recommend-next":
            payload = recommend_next(context)
            exit_code = 0
        elif args.command == "onboarding-packet":
            payload = {
                "profile": "installer-size",
                "runId": context.run_id,
                "goal": "scripts/perf/profiles/installer-size/GOAL.md",
                "config": "scripts/perf/profiles/installer-size/config.json",
                "state": summarize(context),
                "recommendation": recommend_next(context),
            }
            exit_code = 0
        elif args.command == "next":
            exit_code, payload = dispatch_next(context)
        elif args.command == "abandon-pending":
            tombstone = abandon_pending_packet(context, reason=args.reason)
            payload = {
                "abandonContract": "InstallerSizePacketAbandonmentDispatchV1",
                "schemaVersion": 1,
                "profile": "installer-size",
                "runId": context.run_id,
                "tombstone": tombstone,
                "state": summarize(context),
            }
            exit_code = 0
        else:
            payload = finalize_preview(context)
            exit_code = 0 if payload["researchChampionReady"] else 1
    except (ProfileError, StateError, ValueError, OSError) as exc:
        payload = {
            "profile": "installer-size",
            "runId": args.run_id,
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }
        exit_code = 2
    compact = bool(getattr(args, "compact", False))
    _print_json(payload, compact=compact)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
