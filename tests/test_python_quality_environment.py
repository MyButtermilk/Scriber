from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.ci import prepare_python_quality_environment as setup


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for relative in setup.INPUTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test input\n", encoding="utf-8")
    (tmp_path / "requirements-test-constraints.txt").write_text("pip==26.1.2\npytest==9.0.1\n", encoding="utf-8")
    return tmp_path


def _inventory(identity: dict) -> dict:
    return {
        "prefix": identity["venv"],
        "base_prefix": identity["basePrefix"],
        "version": identity["pythonVersion"],
        "packages": [("pip", "26.1.2"), ("pytest", "9.0.1")],
    }


@pytest.mark.parametrize("relative", setup.INPUTS)
def test_each_setup_or_dependency_change_invalidates_the_cache(root: Path, relative: str) -> None:
    original = setup.environment_identity(root)
    (root / relative).write_text("changed input\n", encoding="utf-8")
    changed = setup.environment_identity(root)
    assert setup.cache_key(original) != setup.cache_key(changed)


@pytest.mark.parametrize(
    "field", ("interpreterSha256", "interpreter", "pythonVersion", "basePrefix", "architecture", "imageVersion", "venv")
)
def test_environment_or_location_drift_invalidates_the_cache(root: Path, field: str) -> None:
    identity = setup.environment_identity(root)
    changed = {**identity, field: "changed"}
    assert setup.cache_key(identity) != setup.cache_key(changed)
    assert setup.cache_key(identity).startswith("scriber-python-quality-venv-v1-")


@pytest.mark.parametrize(
    "packages",
    [
        [("pip", "26.1.2")],  # Missing distribution.
        [("pip", "26.1.2"), ("pytest", "9.0.0")],  # Wrong version.
        [("pip", "26.1.2"), ("pytest", "9.0.1"), ("extra", "1.0")],
        [("pip", "26.1.2"), ("pytest", "9.0.1"), ("PyTest", "9.0.1")],
    ],
)
def test_missing_changed_extra_and_duplicate_packages_are_rejected(root: Path, packages: list) -> None:
    identity = setup.environment_identity(root)
    inventory = {**_inventory(identity), "packages": packages}
    with pytest.raises(ValueError):
        setup.validate_inventory(inventory, identity, setup.pinned_packages(root))


@pytest.mark.parametrize("field", ("prefix", "base_prefix", "version"))
def test_restored_interpreter_must_match_the_current_runtime(root: Path, field: str) -> None:
    identity = setup.environment_identity(root)
    inventory = {**_inventory(identity), field: "different"}
    with pytest.raises(ValueError, match="interpreter identity"):
        setup.validate_inventory(inventory, identity, setup.pinned_packages(root))


def test_valid_inventory_accepts_normalized_distribution_names(root: Path) -> None:
    identity = setup.environment_identity(root)
    inventory = {**_inventory(identity), "packages": [("PIP", "26.1.2"), ("PyTest", "9.0.1")]}
    setup.validate_inventory(inventory, identity, setup.pinned_packages(root))


@pytest.mark.parametrize("constraint", ("pip>=26", "pip==26.*", "pip==26\nPIP==26", "", "-r other.txt"))
def test_ambiguous_constraint_files_fail_closed(root: Path, constraint: str) -> None:
    (root / "requirements-test-constraints.txt").write_text(constraint, encoding="utf-8")
    with pytest.raises(ValueError):
        setup.pinned_packages(root)


def test_mismatched_cache_stamp_is_rejected_before_running_cached_python(root: Path, monkeypatch) -> None:
    identity = setup.environment_identity(root)
    (root / "venv").mkdir()
    (root / "venv" / setup.STAMP).write_text("{}", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(setup, "_run", run)
    with pytest.raises(ValueError, match="stamp differs"):
        setup.validate_environment(root, identity)
    run.assert_not_called()


def test_cache_hit_validates_inventory_and_dependencies_without_installing(root: Path, monkeypatch) -> None:
    identity = setup.environment_identity(root)
    (root / "venv").mkdir()
    (root / "venv" / setup.STAMP).write_text(json.dumps(identity), encoding="utf-8")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(_inventory(identity))))
    monkeypatch.setattr(setup, "_run", run)
    assert setup.prepare_environment(root, identity) == "validated-cache"
    calls = [call.args[0] for call in run.call_args_list]
    assert len(calls) == 2
    assert calls[0][-1] == setup.INVENTORY_CODE
    assert calls[1][1:] == ["-I", "-m", "pip", "check"]


def test_invalid_cache_rebuilds_the_locked_environment_and_validates_again(root: Path, monkeypatch) -> None:
    identity = setup.environment_identity(root)
    (root / "venv").mkdir()
    (root / "venv" / "stale").write_text("old", encoding="utf-8")
    validation = Mock(side_effect=[ValueError("stale"), None])
    monkeypatch.setattr(setup, "validate_environment", validation)
    commands = []

    def run(command, _root, **_kwargs):
        commands.append(command)
        if command[1:] == ["-m", "venv", "venv"]:
            assert not (root / "venv").exists()
            (root / "venv").mkdir()

    monkeypatch.setattr(setup, "_run", run)
    assert setup.prepare_environment(root, identity) == "cold-install"
    assert validation.call_count == 2
    assert commands[1][1:] == ["-m", "pip", "install", "pip==26.1.2"]
    assert commands[2][1:] == [
        "-m",
        "pip",
        "install",
        "-c",
        "requirements-test-constraints.txt",
        "-c",
        "requirements-release-constraints.txt",
        "-r",
        "requirements-base.txt",
        "-r",
        "requirements-test.txt",
    ]
    assert json.loads((root / "venv" / setup.STAMP).read_text(encoding="utf-8")) == identity


def test_failed_fresh_validation_is_fatal(root: Path, monkeypatch) -> None:
    identity = setup.environment_identity(root)
    validation = Mock(side_effect=[ValueError("missing"), ValueError("bad dependency closure")])
    monkeypatch.setattr(setup, "validate_environment", validation)
    monkeypatch.setattr(setup, "_remove_environment", lambda _root: (root / "venv").mkdir())
    monkeypatch.setattr(setup, "_run", Mock())
    with pytest.raises(ValueError, match="bad dependency closure"):
        setup.prepare_environment(root, identity)


def test_cache_cleanup_keeps_neighboring_files(root: Path) -> None:
    (root / "venv").mkdir()
    (root / "venv" / "cached-file").write_text("cached", encoding="utf-8")
    before = (root / "requirements-test.txt").read_bytes()
    setup._remove_environment(root)
    assert not (root / "venv").exists()
    assert (root / "requirements-test.txt").read_bytes() == before


def test_cache_cleanup_rejects_junctions_before_deleting(root: Path, monkeypatch) -> None:
    (root / "venv").mkdir()
    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == "venv")
    with pytest.raises(ValueError, match="redirected"):
        setup._remove_environment(root)
    assert (root / "venv").exists()
