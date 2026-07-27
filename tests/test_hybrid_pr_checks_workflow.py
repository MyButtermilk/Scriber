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
        "python-ruff",
        "python-full-suite",
        "python-gates",
        "frontend-gates",
        "rust-gates",
        "workflow-lint",
    }.issubset(jobs)
    assert jobs["python-full-suite"]["uses"] == "./.github/workflows/python-full-suite.yml"
    assert "tests\\test_tauri_security_gates.py" in workflow
    assert "tests\\test_validate_hybrid_release_readiness.py" in workflow
    assert "tests\\test_hybrid_release_readiness_runner.py" in workflow
    assert "tests\\test_verify_tauri_updater_publication.py" in workflow
    assert "tests\\test_windows_authenticode_gate.py" in workflow
    assert "tests\\test_tauri_stability_smoke_gates.py" in workflow
    assert "tests\\perf\\test_media_preparation_smoke_script.py" in workflow
    assert "tests\\perf\\test_frontend_browser_smoke_script.py" in workflow
    assert "npm ci --no-audit --no-fund" in workflow
    assert "npm run check" in workflow
    assert "npm test" in workflow
    assert "npm run build" in workflow
    assert workflow.count("cache-dependency-path: Frontend/package-lock.json") == 2
    assert "Build frontend assets for Tauri tests" in workflow
    assert "Create placeholder backend resource directory for Tauri tests" in workflow
    assert "target\\release\\backend" in workflow
    assert "cargo test" in workflow


def test_hybrid_pr_checks_run_exact_scoped_ruff_gate_without_product_dependencies() -> None:
    jobs = _workflow_jobs()
    job = jobs["python-ruff"]
    ordered_steps = job["steps"]
    step_names = [step["name"] for step in ordered_steps]
    assert step_names == [
        "Checkout",
        "Set up Python",
        "Install Ruff",
        "Lint scoped Python modules",
        "Check scoped Python formatting",
    ]
    assert len(step_names) == len(set(step_names))
    steps = {step["name"]: step for step in ordered_steps}

    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert (
        not {
            "if",
            "continue-on-error",
            "working-directory",
            "container",
            "defaults",
        }
        & job.keys()
    )
    assert steps["Set up Python"]["with"]["python-version"] == "3.13.14"
    assert steps["Install Ruff"]["run"] == "python -m pip install ruff==0.15.22"
    assert steps["Lint scoped Python modules"]["run"] == (
        "python -m ruff check src/core src/runtime src/data"
    )
    assert steps["Check scoped Python formatting"]["run"] == (
        "python -m ruff format --check src/core src/runtime src/data"
    )
    assert all(
        not {"if", "continue-on-error", "working-directory"} & step.keys()
        for step in ordered_steps
    )
    pip_install_commands = [
        str(step.get("run", ""))
        for step in ordered_steps
        if re.search(
            r"(?m)^\s*python -m pip install(?:\s|$)",
            str(step.get("run", "")),
        )
    ]
    assert pip_install_commands == ["python -m pip install ruff==0.15.22"]


def test_hybrid_pr_checks_pin_workflow_lint_and_frontend_test_commands() -> None:
    jobs = _workflow_jobs()
    frontend_steps = {
        step["name"]: step
        for step in jobs["frontend-gates"]["steps"]
    }
    lint_steps = jobs["workflow-lint"]["steps"]

    assert frontend_steps["Typecheck frontend"]["run"] == "npm run check"
    assert frontend_steps["Test frontend"]["run"] == "npm test"
    assert frontend_steps["Build frontend"]["run"] == "npm run build"
    assert any(
        "go run github.com/rhysd/actionlint/cmd/actionlint@"
        "914e7df21a07ef503a81201c76d2b11c789d3fca"
        in step.get("run", "")
        for step in lint_steps
    )
    setup_go = next(
        step for step in lint_steps if step["name"] == "Set up Go"
    )
    assert setup_go["with"]["go-version"] == "1.26.4"
    actionlint = next(
        step["run"]
        for step in lint_steps
        if step["name"] == "Lint GitHub Actions workflows"
    )
    assert actionlint.count("-ignore") == 1
    assert (
        'unexpected key "queue" for "concurrency" section'
        in actionlint
    )
    python_install = next(
        step["run"]
        for step in jobs["python-gates"]["steps"]
        if step["name"] == "Install Python gate dependencies"
    )
    assert "python -m pip install pip==26.1.2" in python_install
    assert "-c requirements-test-constraints.txt" in python_install
    assert "python -m pip check" in python_install


def test_ci_and_release_third_party_actions_are_commit_pinned() -> None:
    workflows = [
        yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")),
        yaml.safe_load(
            (
                REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
            ).read_text(encoding="utf-8")
        ),
    ]
    third_party_uses: list[str] = []
    for workflow in workflows:
        for job in workflow["jobs"].values():
            candidates = [job.get("uses")]
            candidates.extend(step.get("uses") for step in job.get("steps", []))
            for uses in candidates:
                if (
                    isinstance(uses, str)
                    and not uses.startswith(("./", "actions/"))
                ):
                    third_party_uses.append(uses)

    assert third_party_uses
    assert all(
        re.fullmatch(r"[^/@]+/[^/@]+@[0-9a-f]{40}", uses)
        for uses in third_party_uses
    )


def test_hybrid_pr_checks_do_not_run_installer_release_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "build_windows.ps1" not in workflow
    assert "tauri build" not in workflow
    assert "choco install ffmpeg" not in workflow
