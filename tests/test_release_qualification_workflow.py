from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_pre_tag_dispatch_runs_the_exact_existing_release_quality_and_browser_gates() -> None:
    pre_tag = _workflow("qualify-release.yml")
    release = _workflow("release-windows.yml")
    quality = _workflow("quality-gates.yml")
    python = _workflow("python-full-suite.yml")
    # PyYAML's YAML 1.1 loader treats the GitHub Actions `on` key as True.
    trigger = pre_tag[True]
    assert set(trigger) == {"workflow_dispatch"}
    assert trigger["workflow_dispatch"]["inputs"]["candidate_sha"]["required"] is True
    assert set(pre_tag["jobs"]) == {"candidate", "quality-gates", "qualification"}
    gates = pre_tag["jobs"]["quality-gates"]
    assert gates["needs"] == "candidate"
    assert gates["name"] == release["jobs"]["quality-gates"]["name"]
    assert gates["uses"] == release["jobs"]["quality-gates"]["uses"]
    assert gates["uses"] == "./.github/workflows/quality-gates.yml"
    assert "with" not in gates  # No different SHA, reduced suite, or browser opt-out.
    assert quality["jobs"]["python-full-suite"]["uses"] == "./.github/workflows/python-full-suite.yml"
    assert set(python["jobs"]) == {"python-full-suite", "python-real-browser", "python-typecheck"}
    browser = python["jobs"]["python-real-browser"]
    assert any("smoke_real_file_upload_browser.py" in step.get("run", "") for step in browser["steps"])
    for workflow in (pre_tag, quality, python):
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["ref"] == "${{ github.sha }}"


def test_qualification_has_no_signing_publication_secrets_or_write_permissions() -> None:
    pre_tag = _workflow("qualify-release.yml")
    assert pre_tag["permissions"] == {"contents": "read"}
    assert pre_tag["concurrency"]["group"] == "qualify-release-${{ github.sha }}"
    assert pre_tag["concurrency"]["cancel-in-progress"] is False
    for name in ("qualify-release.yml", "quality-gates.yml", "python-full-suite.yml"):
        raw = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for prohibited in ("secrets.", "secrets: inherit", "contents: write", "actions: write", "id-token: write"):
            assert prohibited not in raw
        assert "build_windows.ps1" not in raw
        assert "publish_github_release" not in raw
    for job in pre_tag["jobs"].values():
        assert set(job.get("permissions", {}).values()) <= {"read"}
        assert "if" not in job
        assert "continue-on-error" not in job
    candidate = pre_tag["jobs"]["candidate"]
    admission = next(step for step in candidate["steps"] if "release_qualification candidate" in step.get("run", ""))
    assert admission["env"]["CANDIDATE_SHA"] == "${{ inputs.candidate_sha }}"
    assert '--candidate-sha "$CANDIDATE_SHA"' in admission["run"]
    assert "${{ inputs." not in admission["run"]
    certificate = pre_tag["jobs"]["qualification"]
    assert certificate["needs"] == ["candidate", "quality-gates"]
    assert certificate["permissions"] == {"contents": "read", "actions": "read"}
    certify = next(step for step in certificate["steps"] if "release_qualification certify" in step.get("run", ""))
    assert not {"if", "continue-on-error"} & certify.keys()


def test_tag_release_requires_qualification_before_all_product_preparation() -> None:
    release = _workflow("release-windows.yml")
    assert release[True]["push"]["tags"] == ["v*"]
    plan = release["jobs"]["release-plan"]
    names = [step["name"] for step in plan["steps"]]
    qualify = next(step for step in plan["steps"] if step.get("id") == "pre-tag-qualification")
    assert qualify["if"] == "steps.official-release.outputs.official-release == 'true'"
    assert "release_qualification select" in qualify["run"]
    assert "if ($LASTEXITCODE -ne 0)" in qualify["run"]
    assert "continue-on-error" not in qualify
    assert names.index(qualify["name"]) < names.index("Select exact-revision quality evidence")
    assert names.index(qualify["name"]) < names.index("Compute deterministic release cache keys")
    assert plan["outputs"]["qualification-run-id"] == "${{ steps.pre-tag-qualification.outputs.qualification-run-id }}"
    assert plan["outputs"]["qualification-run-attempt"] == (
        "${{ steps.pre-tag-qualification.outputs.qualification-run-attempt }}"
    )
    for job in release["jobs"].values():
        if job is not plan:
            needs = job.get("needs", [])
            assert "release-plan" in ([needs] if isinstance(needs, str) else needs)


def test_signing_rechecks_pinned_qualification_and_preserves_existing_fail_closed_barriers() -> None:
    release = _workflow("release-windows.yml")
    build = next(
        job
        for job in release["jobs"].values()
        if any(step["name"] == "Build Windows installer" for step in job.get("steps", []))
    )
    steps = {step["name"]: step for step in build["steps"]}
    names = list(steps)
    recheck = steps["Recheck pinned pre-tag qualification before signing"]
    assert recheck["if"] == "needs.release-plan.outputs.official-release == 'true'"
    assert "release_qualification verify" in recheck["run"]
    for binding in ("qualification-run-id", "qualification-run-attempt"):
        assert f"--{binding}" in recheck["run"]
        assert f"needs.release-plan.outputs.{binding}" in recheck["run"]
    assert "if ($LASTEXITCODE -ne 0)" in recheck["run"]
    assert "continue-on-error" not in recheck
    old_barrier = steps["Require successful release quality gates"]
    assert "release_quality_source wait" in old_barrier["run"]
    assert "if ($LASTEXITCODE -ne 0)" in old_barrier["run"]
    assert not {"if", "continue-on-error"} & old_barrier.keys()
    assert names.index(old_barrier["name"]) < names.index(recheck["name"]) < names.index("Build Windows installer")
    assert names.index("Smoke downloaded installer candidate") < names.index(
        "Verify and publish exact GitHub release transaction"
    )
    assert "--source-run-id" in old_barrier["run"]
    assert "--source-run-attempt" in old_barrier["run"]
