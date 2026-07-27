from __future__ import annotations

from pathlib import Path

import yaml
from packaging.requirements import Requirement


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-full-suite.yml"


def _workflow() -> tuple[str, dict[str, object]]:
    raw = WORKFLOW.read_text(encoding="utf-8")
    return raw, yaml.safe_load(raw)


def test_python_full_suite_has_reproducible_test_dependency_closure() -> None:
    dev_requirements = (REPO_ROOT / "requirements-dev.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    test_requirements = (REPO_ROOT / "requirements-test.txt").read_text(
        encoding="utf-8"
    ).splitlines()

    assert dev_requirements == [
        "pytest==9.0.1",
        "pytest-asyncio==1.3.0",
        "pytest-mock==3.15.1",
    ]
    assert test_requirements == [
        "-r requirements-dev.txt",
        "",
        "PyYAML==6.0.3",
        "PyInstaller==6.20.0",
        "pytest-xdist==3.8.0",
        "tzdata==2025.3",
    ]


def test_python_test_constraints_resolve_every_direct_requirement_exactly() -> None:
    constraints = [
        Requirement(line)
        for line in (
            REPO_ROOT / "requirements-test-constraints.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    constrained_names = {requirement.name.casefold() for requirement in constraints}
    direct_requirements = []
    for path in (
        "requirements-base.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
    ):
        for line in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "-r ")):
                direct_requirements.append(Requirement(line))

    assert all(str(requirement.specifier).startswith("==") for requirement in constraints)
    assert {
        requirement.name.casefold() for requirement in direct_requirements
    }.issubset(constrained_names)


def test_python_full_suite_is_bounded_read_only_and_reusable() -> None:
    raw, workflow = _workflow()
    job = workflow["jobs"]["python-full-suite"]

    assert "workflow_call:" in raw
    assert workflow["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 30
    assert "secrets:" not in raw


def test_python_full_suite_installs_checks_runs_and_uploads_results() -> None:
    _, workflow = _workflow()
    steps = {
        step["name"]: step
        for step in workflow["jobs"]["python-full-suite"]["steps"]
    }
    setup = steps["Set up Python"]
    install = steps["Install Python test dependencies"]["run"]
    run = steps["Run complete Python test suite"]["run"]
    upload = steps["Upload Python test results"]

    assert setup["uses"] == "actions/setup-python@v6"
    assert setup["with"]["python-version"] == "3.13.14"
    assert setup["with"]["cache"] == "pip"
    assert setup["with"]["cache-dependency-path"].splitlines() == [
        "requirements-base.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements-test-constraints.txt",
    ]
    assert "python -m pip install pip==26.1.2" in install
    assert "-c requirements-test-constraints.txt" in install
    assert "-r requirements-base.txt" in install
    assert "-r requirements-test.txt" in install
    assert "python -m pip check" in install
    assert "python -m pytest -n 4 --dist loadfile -ra" in run
    assert "--junitxml build\\test-results\\python-full-suite.xml" in run
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v7"
    assert upload["with"]["if-no-files-found"] == "warn"
