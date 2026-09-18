from __future__ import annotations

from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-full-suite.yml"
SETUP = REPO_ROOT / ".github" / "actions" / "setup-python-quality" / "action.yml"


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


def test_all_python_quality_lanes_run_independently_and_remain_required() -> None:
    raw, workflow = _workflow()
    assert "workflow_call:" in raw
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets:" not in raw
    expected = {
        "python-full-suite": ("Python full suite", 30),
        "python-real-browser": ("Python real-browser integration", 20),
        "python-typecheck": ("Python typecheck", 15),
    }
    assert set(workflow["jobs"]) == expected.keys()
    for job_id, (name, timeout) in expected.items():
        job = workflow["jobs"][job_id]
        assert job["name"] == name
        assert job["runs-on"] == "windows-latest"
        assert job["timeout-minutes"] == timeout
        assert not {"needs", "if", "continue-on-error", "container", "defaults"} & job.keys()
        assert job["steps"][0]["with"]["ref"] == "${{ github.sha }}"
        setup = job["steps"][1]
        assert setup["uses"] == "./.github/actions/setup-python-quality"
        assert setup["with"]["python-version"] == "3.14.7"
        assert setup["with"].get("publish-cache", "false") == ("true" if job_id == "python-full-suite" else "false")
        for step in job["steps"]:
            assert "continue-on-error" not in step
            if "if" in step:
                assert step["if"] == "always()"
                assert step["uses"] == "actions/upload-artifact@v7"


def test_pytest_lane_keeps_all_tests_and_file_isolation_without_browser_setup() -> None:
    _, workflow = _workflow()
    job = workflow["jobs"]["python-full-suite"]
    steps = {step["name"]: step for step in job["steps"]}
    assert _pwsh_logical_commands(steps["Run complete Python test suite"]["run"]) == [
        "New-Item -ItemType Directory -Force build\\test-results | Out-Null",
        (
            ".\\venv\\Scripts\\python.exe -m pytest -n 4 --dist loadfile -ra "
            "--junitxml build\\test-results\\python-full-suite.xml"
        ),
    ]
    assert all("setup-node" not in step.get("uses", "") for step in job["steps"])
    assert all("smoke_real_file_upload_browser" not in step.get("run", "") for step in job["steps"])
    upload = steps["Upload Python test results"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "build/test-results/python-full-suite.xml"
    assert upload["with"]["if-no-files-found"] == "warn"


def test_browser_lane_preserves_real_production_composition_and_own_artifact() -> None:
    _, workflow = _workflow()
    job = workflow["jobs"]["python-real-browser"]
    steps = {step["name"]: step for step in job["steps"]}
    setup_node = steps["Set up pinned Node for the real-browser File smoke"]
    install_frontend = steps["Install frontend dependencies for the real-browser File smoke"]
    browser_smoke = steps["Run real-browser File upload against production composition"]
    assert setup_node["uses"] == "actions/setup-node@v6"
    assert setup_node["with"]["node-version-file"] == ".node-version"
    assert setup_node["with"]["cache-dependency-path"] == "Frontend/package-lock.json"
    assert install_frontend["working-directory"] == "Frontend"
    assert install_frontend["run"] == "npm ci --no-audit --no-fund"
    assert browser_smoke["shell"] == "pwsh"
    assert "scripts\\smoke_real_file_upload_browser.py" in browser_smoke["run"]
    assert "build\\test-results\\real-file-browser-smoke.json" in browser_smoke["run"]
    assert all("pytest" not in step.get("run", "") for step in job["steps"])
    upload = steps["Upload browser integration result"]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "build/test-results/real-file-browser-smoke.json"
    assert upload["with"]["name"].startswith("python-real-browser-")


def test_media_lanes_both_use_the_verified_locked_ffmpeg_runtime() -> None:
    _, workflow = _workflow()
    for job_id in ("python-full-suite", "python-real-browser"):
        steps = {step["name"]: step for step in workflow["jobs"][job_id]["steps"]}
        restore = steps["Restore locked FFmpeg test runtime"]
        assert restore["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert "scripts\\ffmpeg\\restore_profile_b_release_artifact.ps1" in restore["run"]
        assert "-Tag ffmpeg-profile-b-n7.0-v4" in restore["run"]
        assert "-AssetName scriber-ffmpeg-profile-b-n7.0-v4-Windows.zip" in restore["run"]
        assert "validate_ffmpeg_profile.py" in restore["run"]
        assert "SCRIBER_MEDIA_TOOLS_DIR=" in restore["run"]
        assert "SCRIBER_FFMPEG_PATH=" not in restore["run"]
        assert "SCRIBER_FFPROBE_PATH=" not in restore["run"]


def test_typecheck_lane_retains_the_extended_python_tranche() -> None:
    _, workflow = _workflow()
    job = workflow["jobs"]["python-typecheck"]
    typecheck = job["steps"][-1]
    assert typecheck["shell"] == "pwsh"
    assert typecheck["run"].startswith(".\\venv\\Scripts\\python.exe -m mypy src\\api src\\core src\\runtime src\\data")
    assert "src\\native_overlay.py" in typecheck["run"]
    assert "src\\meeting_export.py" in typecheck["run"]
    assert all("npm " not in step.get("run", "") for step in job["steps"])


def test_quality_environment_cache_is_exact_validated_and_only_saved_from_main() -> None:
    action = yaml.safe_load(SETUP.read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in action["runs"]["steps"]}
    setup = steps["Set up Python"]
    assert setup["uses"] == "actions/setup-python@v6"
    assert setup["with"]["python-version"] == "${{ inputs.python-version }}"
    assert setup["with"]["cache"] == "pip"
    assert setup["with"]["cache-dependency-path"].splitlines() == [
        "requirements-base.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements-test-constraints.txt",
        "requirements-release-constraints.txt",
    ]
    restore = steps["Restore exact test environment"]
    save = steps["Save trusted test environment"]
    assert restore["uses"] == "actions/cache/restore@v6"
    assert "restore-keys" not in restore["with"]
    assert restore["with"] == save["with"]
    assert restore["with"]["path"] == "venv"
    assert save["uses"] == "actions/cache/save@v6"
    assert save["if"] == (
        "inputs.publish-cache == 'true' && github.repository == 'MyButtermilk/Scriber' "
        "&& github.event_name == 'push' && github.ref == 'refs/heads/main' && steps.cache.outputs.cache-hit != 'true'"
    )
    validate = steps["Validate or create isolated test environment"]
    assert "prepare_python_quality_environment.py prepare" in validate["run"]
    assert "if" not in validate
    assert list(steps).index("Validate or create isolated test environment") < list(steps).index(
        "Save trusted test environment"
    )


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
