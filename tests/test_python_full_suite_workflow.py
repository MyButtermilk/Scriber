from __future__ import annotations

import re
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-full-suite.yml"


def _workflow() -> tuple[str, dict[str, object]]:
    raw = WORKFLOW.read_text(encoding="utf-8")
    return raw, yaml.safe_load(raw)


def _pwsh_logical_commands(script: str) -> list[str]:
    commands: list[str] = []
    pending = ""
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        continued = line.endswith("`")
        fragment = line[:-1].rstrip() if continued else line
        pending = " ".join(part for part in (pending, fragment) if part)
        if not continued:
            commands.append(" ".join(pending.split()))
            pending = ""
    assert not pending
    return commands


def test_python_full_suite_has_reproducible_test_dependency_closure() -> None:
    dev_requirements = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    test_requirements = (REPO_ROOT / "requirements-test.txt").read_text(encoding="utf-8").splitlines()

    assert dev_requirements == [
        "pytest==9.0.1",
        "pytest-asyncio==1.3.0",
        "pytest-mock==3.15.1",
        "PyYAML==6.0.3",
    ]
    assert test_requirements == [
        "-r requirements-dev.txt",
        "",
        "PyInstaller==6.20.0",
        "mypy==2.3.0",
        "pytest-xdist==3.8.0",
        "tzdata==2025.3",
    ]


def test_python_test_constraints_resolve_every_direct_requirement_exactly() -> None:
    constraints = [
        Requirement(line)
        for line in (REPO_ROOT / "requirements-test-constraints.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    constraint_names = [canonicalize_name(requirement.name) for requirement in constraints]
    assert len(constraint_names) == len(set(constraint_names))
    constraint_versions: dict[str, str] = {}
    for requirement in constraints:
        assert requirement.url is None
        assert requirement.marker is None
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version
        constraint_versions[canonicalize_name(requirement.name)] = specifiers[0].version

    direct_requirements = []
    for path in (
        "requirements-base.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
    ):
        for line in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "-r ")):
                direct_requirements.append(Requirement(line))

    assert {canonicalize_name(requirement.name) for requirement in direct_requirements}.issubset(constraint_versions)
    assert {
        name: constraint_versions[name]
        for name in (
            "ast-serialize",
            "librt",
            "mypy",
            "mypy-extensions",
            "pathspec",
            "typing-extensions",
        )
    } == {
        "ast-serialize": "0.6.0",
        "librt": "0.13.0",
        "mypy": "2.3.0",
        "mypy-extensions": "1.1.0",
        "pathspec": "1.1.1",
        "typing-extensions": "4.16.0",
    }


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
    job = workflow["jobs"]["python-full-suite"]
    ordered_steps = job["steps"]
    step_names = [step["name"] for step in ordered_steps]
    assert step_names == [
        "Checkout",
        "Set up Python",
        "Create isolated test environment",
        "Install Python test dependencies",
        "Restore locked FFmpeg test runtime",
        "Run complete Python test suite",
        "Typecheck extended Python tranche",
        "Upload Python test results",
    ]
    assert len(step_names) == len(set(step_names))
    steps = {step["name"]: step for step in ordered_steps}
    setup = steps["Set up Python"]
    create_environment = steps["Create isolated test environment"]
    install = steps["Install Python test dependencies"]["run"]
    restore_ffmpeg = steps["Restore locked FFmpeg test runtime"]
    run = steps["Run complete Python test suite"]["run"]
    typecheck = steps["Typecheck extended Python tranche"]
    upload = steps["Upload Python test results"]

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
    assert setup["uses"] == "actions/setup-python@v6"
    assert setup["with"]["python-version"] == "3.14.6"
    assert setup["with"]["cache"] == "pip"
    assert setup["with"]["cache-dependency-path"].splitlines() == [
        "requirements-base.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements-test-constraints.txt",
        "requirements-release-constraints.txt",
    ]
    assert create_environment["run"] == "python -m venv venv"
    assert ".\\venv\\Scripts\\python.exe -m pip install pip==26.1.2" in install
    assert "-c requirements-test-constraints.txt" in install
    assert "-r requirements-base.txt" in install
    assert "-r requirements-test.txt" in install
    assert ".\\venv\\Scripts\\python.exe -m pip check" in install
    assert restore_ffmpeg["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "scripts\\ffmpeg\\restore_profile_b_release_artifact.ps1" in restore_ffmpeg["run"]
    assert "-Tag ffmpeg-profile-b-n7.0-v4" in restore_ffmpeg["run"]
    assert "-AssetName scriber-ffmpeg-profile-b-n7.0-v4-Windows.zip" in restore_ffmpeg["run"]
    assert "validate_ffmpeg_profile.py" in restore_ffmpeg["run"]
    assert "SCRIBER_MEDIA_TOOLS_DIR=" in restore_ffmpeg["run"]
    assert "SCRIBER_FFMPEG_PATH=" not in restore_ffmpeg["run"]
    assert "SCRIBER_FFPROBE_PATH=" not in restore_ffmpeg["run"]
    assert _pwsh_logical_commands(run) == [
        "New-Item -ItemType Directory -Force build\\test-results | Out-Null",
        (
            ".\\venv\\Scripts\\python.exe -m pytest -n 4 --dist loadfile -ra "
            "--junitxml build\\test-results\\python-full-suite.xml"
        ),
    ]
    assert typecheck["shell"] == "pwsh"
    assert typecheck["run"].startswith(".\\venv\\Scripts\\python.exe -m mypy src\\api src\\core src\\runtime src\\data")
    assert "src\\native_overlay.py" in typecheck["run"]
    assert "src\\meeting_export.py" in typecheck["run"]
    assert step_names.index("Typecheck extended Python tranche") == (
        step_names.index("Run complete Python test suite") + 1
    )
    assert all(not {"if", "continue-on-error", "working-directory"} & step.keys() for step in ordered_steps[:-1])
    install_index = step_names.index("Install Python test dependencies")
    later_scripts = "\n".join(str(step.get("run", "")) for step in ordered_steps[install_index + 1 :])
    assert (
        re.search(
            r"(?i)(?:python\s+-m\s+)?pip\s+install\b",
            later_scripts,
        )
        is None
    )
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v7"
    assert upload["with"]["if-no-files-found"] == "warn"


def test_python_quality_docs_install_the_pinned_local_ruff_tool() -> None:
    install_command = "scripts\\project-python.cmd -m pip install ruff==0.15.22"
    check_command = "scripts\\project-python.cmd -m ruff check src tests scripts"
    for path in (
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "docs" / "TESTING_AND_RELEASE.md",
    ):
        documentation = path.read_text(encoding="utf-8")
        assert documentation.count(install_command) == 1
        assert documentation.index(install_command) < documentation.index(check_command)
