from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "hybrid-pr-checks.yml"


def test_hybrid_pr_checks_workflow_exists_and_runs_on_pull_requests() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Hybrid PR Checks" in workflow
    assert "pull_request:" in workflow
    assert "branches:" in workflow
    assert "- main" in workflow
    assert "workflow_dispatch:" in workflow
    assert "permissions:" in workflow
    assert "contents: read" in workflow


def test_hybrid_pr_checks_cover_python_frontend_and_rust_gates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "python-gates:" in workflow
    assert "frontend-gates:" in workflow
    assert "rust-gates:" in workflow
    assert "tests\\test_tauri_security_gates.py" in workflow
    assert "tests\\test_validate_hybrid_release_readiness.py" in workflow
    assert "tests\\test_hybrid_release_readiness_runner.py" in workflow
    assert "tests\\test_verify_tauri_updater_publication.py" in workflow
    assert "tests\\test_windows_authenticode_gate.py" in workflow
    assert "tests\\perf\\test_frontend_browser_smoke_script.py" in workflow
    assert "npm ci --no-audit --no-fund" in workflow
    assert "npm run check" in workflow
    assert "npm run build" in workflow
    assert "Build frontend assets for Tauri tests" in workflow
    assert "Create placeholder backend resource directory for Tauri tests" in workflow
    assert "target\\release\\backend" in workflow
    assert "cargo test" in workflow


def test_hybrid_pr_checks_do_not_run_installer_release_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_windows.ps1" not in workflow
    assert "tauri build" not in workflow
    assert "choco install ffmpeg" not in workflow
