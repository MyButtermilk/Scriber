from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmark_lint import lint


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
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'scriber' } | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Depth 4"
    )
    result = run_capture(["powershell.exe", "-NoProfile", "-Command", ps], Path.cwd(), timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    parsed = json.loads(result.stdout)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


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
    if not desktop.is_file():
        findings.append({"level": "block", "code": "missing_desktop_binary", "message": f"Missing {desktop}"})
    if not backend.is_file():
        findings.append({"level": "block", "code": "missing_backend_binary", "message": f"Missing {backend}"})
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


def check_benchmark(repo_root: Path) -> list[dict[str, Any]]:
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
        findings.extend(check_benchmark(repo_root))

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
