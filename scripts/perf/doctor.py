from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from ctypes import wintypes

try:
    from .benchmark_lint import lint
    from .runtime_attestation import read_windows_file_version, verify_attestation
except ImportError:
    from benchmark_lint import lint
    from runtime_attestation import read_windows_file_version, verify_attestation


REQUIRED_ROOT_FILES = [
    "GOAL.md",
    "autoresearch.ps1",
    "autoresearch.checks.ps1",
    "autoresearch.config.json",
    "autoresearch.md",
    "autoresearch.jsonl",
    "autoresearch.ideas.md",
]

PROTECTED_PATHS = [
    "autoresearch.ps1",
    "autoresearch.checks.ps1",
    "benchmarks/windows",
    "benchmarks/fixtures",
    "benchmarks/oracles",
    "scripts/perf/evaluator",
]

STATE_SCHEMA_VERSION = 1
ACTIVE_BENCHMARK_SEGMENT_PREFIX = "B7-"
HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
EVALUATOR_PATHS = (
    "scripts/perf/run.ps1",
    "scripts/perf/benchmark_lint.py",
    "scripts/perf/doctor.py",
    "scripts/perf/runtime_attestation.py",
    "benchmarks/windows/profile.ps1",
    "benchmarks/windows/endpoint_probe.py",
    "benchmarks/windows/app_ux_collector.py",
    "benchmarks/windows/app_ux_lifecycle_import.schema.json",
    "benchmarks/windows/app_action.ps1",
    "benchmarks/windows/app_observer.ps1",
    "benchmarks/windows/trace_collector.py",
    "scripts/perf/evaluator/local_wux.py",
)
B7_REQUIRED_BASELINE_METRICS = tuple(
    f"{scenario}_{percentile}_ms"
    for scenario in (
        "overlay_warm",
        "overlay_cold",
        "microsoft_local_tail",
        "soniox_local_tail",
        "app_ux",
    )
    for percentile in ("p50", "p95")
)


def run_capture(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_evaluator_hash(repo_root: Path) -> tuple[str, list[str]]:
    """Mirror the evaluator fingerprint emitted by benchmarks/windows/profile.ps1."""

    missing: list[str] = []
    hashes: list[str] = []
    for relative in EVALUATOR_PATHS:
        path = repo_root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        hashes.append(file_sha256(path).upper())
    if missing:
        return "", missing
    source = "|".join(hashes)
    serialized = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), []


def _normalized_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if HASH_PATTERN.fullmatch(text) else ""


def _text(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _identity_values_match(field: str, actual: str, expected: str) -> bool:
    if not actual or not expected:
        return False
    if field.endswith("Hash") or field.endswith("Sha256"):
        return actual.casefold() == expected.casefold()
    return actual == expected


def _is_explicitly_historical(payload: dict[str, Any]) -> bool:
    historical_states = {"historical", "superseded", "invalidated", "archived"}
    return (
        payload.get("active") is False
        or str(payload.get("status", "")).strip().lower() in historical_states
        or str(payload.get("evidenceLevel", "")).strip().lower() in historical_states
    )


def _baseline_is_accepted(config: dict[str, Any]) -> tuple[bool, bool]:
    baseline = config.get("baseline") if isinstance(config.get("baseline"), dict) else {}
    accepted_flag = baseline.get("accepted") is True
    accepted_status = str(baseline.get("status", "")).strip().lower() == "accepted"
    return accepted_flag and accepted_status, accepted_flag == accepted_status


def _state_document(
    path: Path,
    *,
    label: str,
    findings: list[dict[str, Any]],
    required: bool,
) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_state_missing",
                    "message": f"Missing active AutoResearch {label}: {path}",
                    "artifact": label,
                }
            )
        return None
    try:
        return load_json(path)
    except Exception as exc:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_state_invalid_json",
                "message": f"AutoResearch {label} is not a valid JSON object: {exc}",
                "artifact": label,
            }
        )
        return None


def _check_schema(
    payload: dict[str, Any],
    *,
    artifact: str,
    findings: list[dict[str, Any]],
) -> None:
    if payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_schema_mismatch",
                "message": (
                    f"AutoResearch {artifact} schemaVersion must be {STATE_SCHEMA_VERSION}; "
                    f"found {payload.get('schemaVersion')!r}."
                ),
                "artifact": artifact,
                "expected": STATE_SCHEMA_VERSION,
                "actual": payload.get("schemaVersion"),
            }
        )


def _check_identity(
    payload: dict[str, Any],
    *,
    artifact: str,
    segment: str,
    baseline_id: str,
    baseline_sha256: str,
    profile_id: str,
    scorer_hash: str,
    evaluator_hash: str,
    findings: list[dict[str, Any]],
    check_segment: bool = True,
) -> None:
    expected_fields = {
        "baselineId": baseline_id,
        "baselineSha256": baseline_sha256,
        "profileId": profile_id,
        "scorerHash": scorer_hash,
        "evaluatorHash": evaluator_hash,
    }
    actual_fields = {
        "baselineId": _text(payload, "baselineId"),
        "baselineSha256": _normalized_hash(payload.get("baselineSha256")),
        "profileId": _text(payload, "profileId", "profile_id"),
        "scorerHash": _normalized_hash(payload.get("scorerHash")),
        "evaluatorHash": _normalized_hash(payload.get("evaluatorHash")),
    }
    if check_segment and _text(payload, "segment") != segment:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_segment_mismatch",
                "message": f"AutoResearch {artifact} does not belong to active segment {segment}.",
                "artifact": artifact,
                "expected": segment,
                "actual": _text(payload, "segment") or "missing",
            }
        )
    for name, expected in expected_fields.items():
        actual = actual_fields[name]
        if not actual:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_identity_missing",
                    "message": f"AutoResearch {artifact} is missing {name}.",
                    "artifact": artifact,
                    "field": name,
                }
            )
        elif not _identity_values_match(name, actual, expected):
            findings.append(
                {
                    "level": "block",
                    "code": f"autoresearch_{name.replace('Id', '_id').replace('Sha256', '_sha').replace('Hash', '_hash').lower()}_mismatch",
                    "message": f"AutoResearch {artifact} {name} does not match the active B7 contract.",
                    "artifact": artifact,
                    "field": name,
                    "expected": expected or "missing-active-value",
                    "actual": actual,
                }
            )


def check_autoresearch_state(repo_root: Path) -> list[dict[str, Any]]:
    """Cross-check the active AutoResearch pointers without running a benchmark.

    B4/B6 artifacts may remain in the repository as explicit historical evidence,
    but none may satisfy an active B7 baseline, champion, or resume pointer.
    """

    findings: list[dict[str, Any]] = []
    config = _state_document(
        repo_root / "autoresearch.config.json",
        label="config",
        findings=findings,
        required=True,
    )
    if config is None:
        return findings
    _check_schema(config, artifact="config", findings=findings)

    segment = _text(config, "segment")
    is_b7 = segment.startswith(ACTIVE_BENCHMARK_SEGMENT_PREFIX)
    if not is_b7:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_active_segment_outdated",
                "message": (
                    f"The active evaluator implements B7, but autoresearch.config.json names "
                    f"{segment or 'no segment'}. B4/B6 evidence is historical only."
                ),
                "expectedPrefix": ACTIVE_BENCHMARK_SEGMENT_PREFIX,
                "actual": segment or "missing",
            }
        )

    accepted, acceptance_coherent = _baseline_is_accepted(config)
    if not acceptance_coherent:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_baseline_acceptance_incoherent",
                "message": "Config baseline.accepted and baseline.status disagree.",
            }
        )
    if is_b7 and not accepted:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_b7_baseline_not_accepted",
                "message": "No measured and explicitly accepted B7 baseline is active.",
            }
        )

    scorer_path = repo_root / "scripts" / "perf" / "evaluator" / "local_wux.py"
    scorer_hash = file_sha256(scorer_path) if scorer_path.is_file() else ""
    evaluator_hash, missing_evaluator_files = current_evaluator_hash(repo_root)
    if not scorer_hash:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_scorer_missing",
                "message": f"Missing B7 scorer: {scorer_path}",
            }
        )
    if missing_evaluator_files:
        findings.append(
            {
                "level": "block",
                "code": "autoresearch_evaluator_surface_incomplete",
                "message": "The B7 evaluator fingerprint cannot be calculated.",
                "missing": missing_evaluator_files,
            }
        )

    baseline_path = repo_root / "benchmarks" / "results" / "baseline.json"
    baseline = _state_document(
        baseline_path,
        label="baseline",
        findings=findings,
        required=True,
    )
    baseline_id = ""
    baseline_sha256 = file_sha256(baseline_path) if baseline_path.is_file() else ""
    profile_id = ""
    active_baseline = False
    if baseline is not None:
        baseline_segment = _text(baseline, "segment")
        historical = _is_explicitly_historical(baseline)
        active_baseline = accepted and not historical and baseline_segment == segment
        if baseline_segment != segment and not (historical and not accepted):
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_baseline_segment_mismatch",
                    "message": (
                        f"baseline.json belongs to {baseline_segment or 'an unlabelled legacy segment'}, "
                        f"not active segment {segment or 'missing'}."
                    ),
                    "expected": segment or "missing",
                    "actual": baseline_segment or "missing",
                }
            )
        if historical and accepted:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_historical_baseline_active",
                    "message": "An explicitly historical baseline cannot be accepted as the active baseline.",
                }
            )
        if active_baseline:
            _check_schema(baseline, artifact="baseline", findings=findings)
            baseline_id = _text(baseline, "baselineId")
            profile_id = _text(baseline, "profileId", "profile_id")
            config_baseline = config.get("baseline") if isinstance(config.get("baseline"), dict) else {}
            config_identity = {
                "baselineId": _text(config_baseline, "baselineId"),
                "baselineSha256": _normalized_hash(
                    config_baseline.get("baselineSha256") or config_baseline.get("sha256")
                ),
                "profileId": _text(config_baseline, "profileId", "profile_id"),
                "scorerHash": _normalized_hash(config_baseline.get("scorerHash")),
                "evaluatorHash": _normalized_hash(config_baseline.get("evaluatorHash")),
            }
            expected_identity = {
                "baselineId": baseline_id,
                "baselineSha256": baseline_sha256,
                "profileId": profile_id,
                "scorerHash": scorer_hash,
                "evaluatorHash": evaluator_hash,
            }
            baseline_identity = {
                "baselineId": baseline_id,
                "baselineSha256": baseline_sha256,
                "profileId": profile_id,
                "scorerHash": _normalized_hash(baseline.get("scorerHash")),
                "evaluatorHash": _normalized_hash(baseline.get("evaluatorHash")),
            }
            for artifact, identity in (("config baseline", config_identity), ("baseline", baseline_identity)):
                for field, expected in expected_identity.items():
                    actual = identity[field]
                    if not actual:
                        findings.append(
                            {
                                "level": "block",
                                "code": "autoresearch_identity_missing",
                                "message": f"AutoResearch {artifact} is missing {field}.",
                                "artifact": artifact,
                                "field": field,
                            }
                        )
                    elif not _identity_values_match(field, actual, expected):
                        code_field = field.replace("Id", "_id").replace("Sha256", "_sha").replace("Hash", "_hash").lower()
                        findings.append(
                            {
                                "level": "block",
                                "code": f"autoresearch_{code_field}_mismatch",
                                "message": f"AutoResearch {artifact} {field} does not match the active B7 contract.",
                                "artifact": artifact,
                                "field": field,
                                "expected": expected or "missing-active-value",
                                "actual": actual,
                            }
                        )
            if is_b7:
                metrics = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}
                invalid_metrics = []
                for name in B7_REQUIRED_BASELINE_METRICS:
                    value = metrics.get(name)
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        invalid_metrics.append(name)
                        continue
                    parsed = float(value)
                    if not math.isfinite(parsed) or parsed <= 0:
                        invalid_metrics.append(name)
                local_wux = metrics.get("local_wux")
                if isinstance(local_wux, bool) or not isinstance(local_wux, (int, float)) or float(local_wux) != 1.0:
                    invalid_metrics.append("local_wux=1.0")
                if invalid_metrics:
                    findings.append(
                        {
                            "level": "block",
                            "code": "autoresearch_b7_baseline_metrics_incomplete",
                            "message": "The active baseline does not implement the complete B7 p50/p95 score contract.",
                            "invalid": invalid_metrics,
                        }
                    )

    require_active_state = bool(is_b7 and active_baseline)
    profile = _state_document(
        repo_root / "benchmarks" / "results" / "profile.json",
        label="profile",
        findings=findings,
        required=require_active_state,
    )
    if profile is not None and require_active_state:
        _check_schema(profile, artifact="profile", findings=findings)
        _check_identity(
            profile,
            artifact="profile",
            segment=segment,
            baseline_id=baseline_id,
            baseline_sha256=baseline_sha256,
            profile_id=profile_id,
            scorer_hash=scorer_hash,
            evaluator_hash=evaluator_hash,
            findings=findings,
            check_segment=False,
        )

    champion = _state_document(
        repo_root / "benchmarks" / "results" / "champion.json",
        label="champion",
        findings=findings,
        required=False,
    )
    active_champion_id = ""
    if champion is not None:
        champion_segment = _text(champion, "segment")
        champion_historical = _is_explicitly_historical(champion)
        if champion_segment != segment:
            if not champion_historical:
                findings.append(
                    {
                        "level": "block",
                        "code": "autoresearch_champion_segment_mismatch",
                        "message": (
                            f"champion.json belongs to {champion_segment or 'an unlabelled legacy segment'} "
                            f"and is not explicitly historical."
                        ),
                        "expected": segment or "missing",
                        "actual": champion_segment or "missing",
                    }
                )
        elif not champion_historical and require_active_state:
            _check_schema(champion, artifact="champion", findings=findings)
            active_champion_id = _text(champion, "championId")
            if not active_champion_id:
                findings.append(
                    {
                        "level": "block",
                        "code": "autoresearch_champion_id_missing",
                        "message": "The active B7 champion has no championId.",
                    }
                )
            _check_identity(
                champion,
                artifact="champion",
                segment=segment,
                baseline_id=baseline_id,
                baseline_sha256=baseline_sha256,
                profile_id=profile_id,
                scorer_hash=scorer_hash,
                evaluator_hash=evaluator_hash,
                findings=findings,
            )

    progress = _state_document(
        repo_root / ".git" / "autoresearch" / "progress.json",
        label="progress",
        findings=findings,
        required=require_active_state,
    )
    last_run_path = repo_root / ".git" / "autoresearch" / "last-run.json"
    last_run = _state_document(
        last_run_path,
        label="last-run",
        findings=findings,
        required=require_active_state,
    )
    for artifact, payload in (("progress", progress), ("last-run", last_run)):
        if payload is None:
            continue
        if require_active_state:
            _check_schema(payload, artifact=artifact, findings=findings)
            _check_identity(
                payload,
                artifact=artifact,
                segment=segment,
                baseline_id=baseline_id,
                baseline_sha256=baseline_sha256,
                profile_id=profile_id,
                scorer_hash=scorer_hash,
                evaluator_hash=evaluator_hash,
                findings=findings,
            )
        else:
            resume_segment = _text(payload, "segment")
            if not resume_segment:
                findings.append(
                    {
                        "level": "block",
                        "code": "autoresearch_resume_segment_missing",
                        "message": f"AutoResearch {artifact} is an unbound legacy resume artifact.",
                        "artifact": artifact,
                        "expected": segment or "missing",
                    }
                )
            elif resume_segment != segment:
                findings.append(
                    {
                        "level": "block",
                        "code": "autoresearch_resume_segment_mismatch",
                        "message": f"AutoResearch {artifact} belongs to a different segment.",
                        "artifact": artifact,
                        "expected": segment or "missing",
                        "actual": resume_segment,
                    }
                )
    if progress is not None and require_active_state:
        if progress.get("baselineAccepted") is not True:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_resume_acceptance_mismatch",
                    "message": "progress.json does not confirm the active accepted B7 baseline.",
                }
            )
        progress_champion_id = _text(progress, "championId")
        if progress_champion_id and progress_champion_id != active_champion_id:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_resume_champion_mismatch",
                    "message": "progress.json championId does not match the active champion pointer.",
                    "expected": active_champion_id or "no-active-champion",
                    "actual": progress_champion_id,
                }
            )
        expected_last_run_sha = _normalized_hash(progress.get("lastRunSha256"))
        actual_last_run_sha = file_sha256(last_run_path) if last_run_path.is_file() else ""
        if not expected_last_run_sha:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_resume_binding_missing",
                    "message": "progress.json is not bound to last-run.json by lastRunSha256.",
                }
            )
        elif expected_last_run_sha != actual_last_run_sha:
            findings.append(
                {
                    "level": "block",
                    "code": "autoresearch_resume_last_run_sha_mismatch",
                    "message": "progress.json lastRunSha256 does not match last-run.json.",
                    "expected": actual_last_run_sha,
                    "actual": expected_last_run_sha,
                }
            )
    return findings


def list_scriber_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    processes: list[dict[str, Any]] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            name = str(entry.szExeFile)
            if "scriber" in name.casefold():
                executable_path = ""
                process = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        capacity = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(capacity.value)
                        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(capacity)):
                            executable_path = buffer.value
                    finally:
                        kernel32.CloseHandle(process)
                processes.append(
                    {
                        "ProcessId": int(entry.th32ProcessID),
                        "Name": name,
                        "ExecutablePath": executable_path,
                    }
                )
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def normalize_path(path: str) -> str:
    return str(Path(path).resolve()).casefold()


def detect_foreign_scriber_instances(repo_root: Path, install_root: Path) -> list[dict[str, Any]]:
    allowed_roots = [normalize_path(str(install_root))]
    release_root = repo_root / "Frontend" / "src-tauri" / "target" / "release"
    if release_root.exists():
        allowed_roots.append(normalize_path(str(release_root)))
    foreign = []
    for proc in list_scriber_processes():
        exe = str(proc.get("ExecutablePath") or "")
        if not exe:
            # A Scriber-named process whose image cannot be queried cannot be
            # proven to belong to the selected benchmark runtime.
            foreign.append(proc)
            continue
        normalized = normalize_path(exe)
        if not any(normalized.startswith(root) for root in allowed_roots):
            foreign.append(proc)
    return foreign


def default_install_root(repo_root: Path) -> Path:
    release_root = repo_root / "Frontend" / "src-tauri" / "target" / "release"
    if (release_root / "scriber-desktop.exe").is_file() and (release_root / "backend" / "scriber-backend.exe").is_file():
        return release_root
    return repo_root / "Scriber Install"


def check_static(repo_root: Path, install_root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rel in REQUIRED_ROOT_FILES:
        path = repo_root / rel
        if not path.exists():
            findings.append({"level": "block", "code": "missing_file", "message": f"Missing {rel}"})
    for rel in PROTECTED_PATHS:
        path = repo_root / rel
        if not path.exists():
            findings.append({"level": "block", "code": "missing_protected_path", "message": f"Missing protected path {rel}"})
    desktop = install_root / "scriber-desktop.exe"
    backend = install_root / "backend" / "scriber-backend.exe"
    audio_sidecar = install_root / "scriber-audio-sidecar.exe"
    if not desktop.is_file():
        findings.append({"level": "block", "code": "missing_desktop_binary", "message": f"Missing {desktop}"})
    if not backend.is_file():
        findings.append({"level": "block", "code": "missing_backend_binary", "message": f"Missing {backend}"})
    if not audio_sidecar.is_file():
        findings.append({"level": "block", "code": "missing_audio_sidecar_binary", "message": f"Missing {audio_sidecar}"})
    package_json = repo_root / "Frontend" / "package.json"
    if desktop.is_file() and package_json.is_file() and os.name == "nt":
        expected_version = str(load_json(package_json).get("version") or "")
        actual_version = read_windows_file_version(desktop)
        if not actual_version:
            findings.append(
                {
                    "level": "block",
                    "code": "unreadable_desktop_version",
                    "message": f"Could not read the PE file version from {desktop}.",
                }
            )
        elif actual_version != expected_version:
            findings.append(
                {
                    "level": "block",
                    "code": "binary_version_mismatch",
                    "message": (
                        f"Benchmark desktop is version {actual_version}, but the current source version is "
                        f"{expected_version}. Rebuild or select the matching release before measuring."
                    ),
                    "expectedVersion": expected_version,
                    "actualVersion": actual_version,
                }
            )
    attestation = verify_attestation(repo_root, install_root)
    if not attestation.get("ok"):
        findings.append(
            {
                "level": "block",
                "code": "runtime_attestation_invalid",
                "message": "The benchmark runtime does not match its explicit post-build attestation.",
                "manifestPresent": bool(attestation.get("manifestPresent")),
                "manifestSha256": str(attestation.get("manifestSha256") or ""),
                "errors": attestation.get("errors", []),
            }
        )
    if os.name != "nt":
        findings.append({"level": "block", "code": "not_windows", "message": "Windows benchmark contract requires native Windows."})
    foreign = detect_foreign_scriber_instances(repo_root, install_root)
    if foreign:
        findings.append(
            {
                "level": "block",
                "code": "foreign_scriber_instance",
                "message": "A running Scriber instance does not match the benchmark install root.",
                "processes": foreign,
            }
        )
    return findings


def check_benchmark(repo_root: Path, install_root: Path) -> list[dict[str, Any]]:
    attestation = verify_attestation(repo_root, install_root)
    if not attestation.get("ok"):
        return [
            {
                "level": "block",
                "code": "runtime_attestation_invalid",
                "message": "FastLocal was not started because the runtime attestation is invalid.",
                "errors": attestation.get("errors", []),
            }
        ]
    result = run_capture(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "autoresearch.ps1"),
            "-Suite",
            "FastLocal",
            "-InstallRoot",
            str(install_root),
        ],
        repo_root,
        timeout=180,
    )
    errors = lint(result.stdout, allow_unknown=False)
    if result.returncode != 0:
        errors.append(f"benchmark exited with code {result.returncode}")
    if errors:
        return [
            {
                "level": "block",
                "code": "benchmark_untrusted",
                "message": "FastLocal benchmark did not produce a trustworthy finite METRIC package.",
                "errors": errors,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        ]
    return [{"level": "ok", "code": "benchmark_contract", "message": "FastLocal benchmark output is finite and complete."}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Scriber Windows autoresearch readiness.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--check-benchmark", action="store_true")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    install_root = Path(args.install_root).resolve() if args.install_root else default_install_root(repo_root)
    findings = check_static(repo_root, install_root)
    findings.extend(check_autoresearch_state(repo_root))
    if args.check_benchmark:
        if any(item.get("level") == "block" for item in findings):
            findings.append(
                {
                    "level": "block",
                    "code": "benchmark_skipped_untrusted_state",
                    "message": "FastLocal was not started because static or AutoResearch state checks failed.",
                }
            )
        else:
            findings.extend(check_benchmark(repo_root, install_root))

    blocked = [item for item in findings if item.get("level") == "block"]
    payload = {
        "ok": not blocked,
        "blocked": bool(blocked),
        "repoRoot": str(repo_root),
        "installRoot": str(install_root),
        "findings": findings or [{"level": "ok", "code": "static_contract", "message": "No blocking static issues detected."}],
    }
    if args.explain:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("doctor: ok" if payload["ok"] else "doctor: blocked")
        for item in findings:
            print(f"- {item.get('level')}: {item.get('code')} - {item.get('message')}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
