from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality-gates.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
HYBRID_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "hybrid-pr-checks.yml"


def test_release_and_pr_use_the_same_reusable_quality_workflow() -> None:
    quality = yaml.safe_load(QUALITY_WORKFLOW.read_text(encoding="utf-8"))
    release = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    hybrid = yaml.safe_load(HYBRID_WORKFLOW.read_text(encoding="utf-8"))

    assert release["jobs"]["quality-gates"]["uses"] == ("./.github/workflows/quality-gates.yml")
    assert hybrid["jobs"]["quality-gates"]["uses"] == ("./.github/workflows/quality-gates.yml")
    assert set(quality["jobs"]) == {
        "python-ruff",
        "python-full-suite",
        "frontend",
        "rust",
    }


def test_quality_workflow_runs_every_requested_exact_revision_gate() -> None:
    raw = QUALITY_WORKFLOW.read_text(encoding="utf-8")

    assert raw.count("ref: ${{ github.sha }}") == 3
    assert "python -m ruff check src tests scripts" in raw
    assert "python -m ruff format --check src tests scripts" in raw
    assert "uses: ./.github/workflows/python-full-suite.yml" in raw
    for command in ("npm run check", "npm run lint", "npm test", "npm run build"):
        assert command in raw
    assert "cargo fmt --all -- --check" in raw
    assert "cargo clippy --locked --all-targets -- -D warnings" in raw
    assert "cargo test --locked --all-targets" in raw
