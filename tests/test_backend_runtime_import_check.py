from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_backend_runtime_imports import REQUIRED_IMPORTS, check_imports


def test_backend_worker_startup_timeout_simulation_is_once(monkeypatch, tmp_path):
    from src import backend_worker

    marker_path = tmp_path / "startup-timeout.marker"
    monkeypatch.setenv("SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE", "1")
    monkeypatch.setenv("SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER", str(marker_path))

    assert backend_worker.should_simulate_startup_timeout_once() is True
    assert marker_path.exists()
    assert backend_worker.should_simulate_startup_timeout_once() is False


def test_backend_runtime_import_check_covers_scipy_startup_dependency():
    required_modules = {module for module, _reason in REQUIRED_IMPORTS}

    assert "scipy" in required_modules
    assert "scipy.signal" in required_modules
    assert "pyloudnorm" in required_modules
    assert "src.web_api" in required_modules


def test_standard_requirements_include_scipy():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements-base.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "scipy" in requirements


def test_backend_worker_import_does_not_eagerly_import_web_api():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.backend_worker; print('src.web_api' in sys.modules)",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_backend_worker_runtime_import_check_entrypoint():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "src.backend_worker", "--runtime-import-check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"ok": True, "missing": []}


def test_sidecar_build_runs_frozen_runtime_import_check():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (
        repo_root / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    spec = (repo_root / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )

    assert "Invoke-FrozenBackendRuntimeImportCheck" in build_script
    assert "--runtime-import-check" in build_script
    assert "scripts.check_backend_runtime_imports" in spec


def test_backend_runtime_import_check_reports_missing_modules():
    def fake_import(module_name: str) -> object:
        if module_name == "missing_module":
            raise ModuleNotFoundError("No module named 'missing_module'")
        return object()

    missing = check_imports(
        [
            ("present_module", "test present"),
            ("missing_module", "test missing"),
        ],
        import_module=fake_import,
    )

    assert missing == [
        {
            "module": "missing_module",
            "reason": "test missing",
            "error": "ModuleNotFoundError: No module named 'missing_module'",
        }
    ]
