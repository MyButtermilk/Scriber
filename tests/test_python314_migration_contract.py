from __future__ import annotations

import json
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]


def _constraint_block(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]


def test_release_and_test_constraints_share_one_sorted_pin_block() -> None:
    release = _constraint_block(REPO_ROOT / "requirements-release-constraints.txt")
    test = _constraint_block(REPO_ROOT / "requirements-test-constraints.txt")

    assert release == test
    names = [canonicalize_name(Requirement(line).name) for line in release]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    for line in release:
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        assert requirement.url is None
        assert requirement.marker is None
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert "*" not in specifiers[0].version


def test_all_python_and_release_workflows_pin_exact_python3147() -> None:
    workflow_paths = (
        ".github/workflows/hybrid-pr-checks.yml",
        ".github/workflows/python-full-suite.yml",
        ".github/workflows/quality-gates.yml",
        ".github/workflows/release-cache-maintenance.yml",
        ".github/workflows/release-windows.yml",
    )
    for relative in workflow_paths:
        raw = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "3.13.14" not in raw
        assert "3.14.7" in raw

        workflow = yaml.safe_load(raw)
        if relative.endswith(("release-cache-maintenance.yml", "release-windows.yml")):
            assert workflow["env"]["SCRIBER_RELEASE_PYTHON_VERSION"] == "3.14.7"


def test_static_python_tooling_targets_python314() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'target-version = "py314"' in pyproject
    assert 'python_version = "3.14"' in pyproject
    assert "py313" not in pyproject


def test_product_numpy_and_runtime_locks_target_one_cp314_abi() -> None:
    numpy_lock = json.loads(
        (REPO_ROOT / "packaging" / "wheels" / "numpy-noblas-wheel-lock-v1.json").read_text(encoding="utf-8")
    )
    runtime_lock = json.loads(
        (REPO_ROOT / "packaging" / "cpython-windows-runtime-input-lock-v1.json").read_text(encoding="utf-8")
    )

    artifact = numpy_lock["artifact"]
    assert artifact["pythonTag"] == "cp314"
    assert artifact["abiTag"] == "cp314"
    assert artifact["platformTag"] == "win_amd64"
    assert artifact["relativePath"].endswith("-cp314-cp314-win_amd64.whl")
    assert runtime_lock["target"] == {
        "pythonVersion": "3.14.7",
        "cacheTag": "cpython-314",
        "architecture": "win_amd64",
        "cpythonTag": "v3.14.7",
    }
