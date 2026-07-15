from __future__ import annotations

import argparse
import ctypes
import json
import os
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
    if (repo_root / "autoresearch.config.json").exists():
        try:
            load_json(repo_root / "autoresearch.config.json")
        except Exception as exc:
            findings.append({"level": "block", "code": "invalid_config", "message": str(exc)})
    if args.check_benchmark:
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
