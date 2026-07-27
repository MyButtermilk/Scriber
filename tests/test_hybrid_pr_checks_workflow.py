from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "hybrid-pr-checks.yml"


def _workflow_jobs() -> dict[str, object]:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return payload["jobs"]


def test_hybrid_pr_checks_workflow_exists_and_runs_on_pull_requests() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Hybrid PR Checks" in workflow
    assert "pull_request:" in workflow
    assert "branches:" in workflow
    assert "- main" in workflow
    assert "push:\n    branches:\n      - main" in workflow
    assert "codex/hybrid-tauri-performance" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "permissions:" in workflow
    assert "contents: read" in workflow


def test_hybrid_pr_checks_cover_python_frontend_and_rust_gates() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = _workflow_jobs()

    assert {
        "quality-gates",
        "python-gates",
        "workflow-lint",
    }.issubset(jobs)
    assert jobs["quality-gates"]["uses"] == "./.github/workflows/quality-gates.yml"
    assert "tests\\test_tauri_security_gates.py" in workflow
    assert "tests\\test_validate_hybrid_release_readiness.py" in workflow
    assert "tests\\test_hybrid_release_readiness_runner.py" in workflow
    assert "tests\\test_verify_tauri_updater_publication.py" in workflow
    assert "tests\\test_windows_authenticode_gate.py" in workflow
    assert "tests\\test_tauri_stability_smoke_gates.py" in workflow
    assert "tests\\perf\\test_media_preparation_smoke_script.py" in workflow
    assert "tests\\perf\\test_frontend_browser_smoke_script.py" in workflow
    assert "npm run lint" not in workflow
    assert "cargo clippy" not in workflow


def test_hybrid_pr_checks_reuse_exact_revision_quality_gates() -> None:
    job = _workflow_jobs()["quality-gates"]

    assert job == {
        "name": "Reusable quality gates",
        "uses": "./.github/workflows/quality-gates.yml",
        "permissions": {"contents": "read"},
    }


def test_hybrid_pr_checks_pin_workflow_lint_and_python_gate_dependencies() -> None:
    jobs = _workflow_jobs()
    lint_steps = jobs["workflow-lint"]["steps"]

    assert any(
        "go run github.com/rhysd/actionlint/cmd/actionlint@"
        "914e7df21a07ef503a81201c76d2b11c789d3fca" in step.get("run", "")
        for step in lint_steps
    )
    setup_go = next(step for step in lint_steps if step["name"] == "Set up Go")
    assert setup_go["with"]["go-version"] == "1.26.4"
    actionlint = next(step["run"] for step in lint_steps if step["name"] == "Lint GitHub Actions workflows")
    assert actionlint.count("-ignore") == 1
    assert 'unexpected key "queue" for "concurrency" section' in actionlint
    python_install = next(
        step["run"] for step in jobs["python-gates"]["steps"] if step["name"] == "Install Python gate dependencies"
    )
    assert "python -m pip install pip==26.1.2" in python_install
    assert "-c requirements-test-constraints.txt" in python_install
    assert "python -m pip check" in python_install


def test_ci_and_release_third_party_actions_are_commit_pinned() -> None:
    workflows = [
        yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")),
        yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release-windows.yml").read_text(encoding="utf-8")),
    ]
    third_party_uses: list[str] = []
    for workflow in workflows:
        for job in workflow["jobs"].values():
            candidates = [job.get("uses")]
            candidates.extend(step.get("uses") for step in job.get("steps", []))
            for uses in candidates:
                if isinstance(uses, str) and not uses.startswith(("./", "actions/")):
                    third_party_uses.append(uses)

    assert third_party_uses
    assert all(re.fullmatch(r"[^/@]+/[^/@]+@[0-9a-f]{40}", uses) for uses in third_party_uses)


def test_hybrid_pr_checks_do_not_run_installer_release_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_windows.ps1" not in workflow
    assert "tauri build" not in workflow
    assert "choco install ffmpeg" not in workflow
