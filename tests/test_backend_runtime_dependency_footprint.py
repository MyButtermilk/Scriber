from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.analyze_backend_runtime_dependencies import build_report


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_fake_sidecar(root: Path) -> Path:
    internal = root / "_internal"
    files = {
        "scipy/signal/_sigtools.pyd": b"s" * 1024,
        "scipy.libs/libopenblas.dll": b"l" * 2048,
        "onnxruntime/capi/onnxruntime.dll": b"o" * 4096,
        "onnxruntime/capi/onnxruntime_pybind11_state.pyd": b"p" * 4096,
    }
    for relative, content in files.items():
        path = internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_runtime_dependency_footprint_reports_required_groups(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")

    report = build_report(sidecar)

    assert report["apiVersion"] == "1"
    assert report["ok"] is True
    assert report["summary"]["totalMb"] > 0
    assert report["summary"]["missingRequiredPaths"] == []
    assert set(report["dependencies"]) == {"scipy", "onnxruntime"}
    assert report["dependencies"]["scipy"]["missingRequiredPaths"] == []
    assert report["dependencies"]["onnxruntime"]["missingRequiredPaths"] == []
    assert report["dependencies"]["onnxruntime"]["topFiles"][0]["path"].startswith(
        "onnxruntime"
    )


def test_runtime_dependency_footprint_fails_when_budget_is_exceeded(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")

    report = build_report(sidecar, max_total_mb=0.001)

    assert report["ok"] is False
    assert "total" in report["summary"]["budgetFailures"]


def test_runtime_dependency_footprint_cli_writes_report(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    output = tmp_path / "runtime-dependency-footprint.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/analyze_backend_runtime_dependencies.py",
            "--sidecar-dir",
            str(sidecar),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout = json.loads(completed.stdout)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert stdout["ok"] is True
    assert stdout["output"] == str(output.resolve())
    assert written["ok"] is True


def test_windows_build_and_release_workflow_can_emit_runtime_dependency_footprint() -> None:
    build = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "[switch]$RunRuntimeDependencyFootprint" in build
    assert "[double]$MaxScipyRuntimeDependencyMB = 0" in build
    assert "[double]$MaxOnnxRuntimeDependencyMB = 0" in build
    assert "[double]$MaxPythonRuntimeDependencyMB = 0" in build
    assert "scripts\\analyze_backend_runtime_dependencies.py" in build
    assert "runtime-dependency-footprint.json" in build
    assert "--max-scipy-mb" in build
    assert "--max-onnxruntime-mb" in build
    assert "--max-total-mb" in build
    assert "runtimeDependencyFootprint = $runtimeDependencyFootprintPath" in build

    assert '"-RunRuntimeDependencyFootprint"' in workflow
