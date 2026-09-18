from __future__ import annotations

import ast
import itertools
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.ci import reuse_installer_smoke_environment as smoke

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("official", "refresh", "cold_product", "backend_sidecar", "backend_runtime", "quality_usable"),
    itertools.product((False, True), repeat=6),
)
def test_selection_preserves_full_fallback_and_never_uses_quality_to_build_a_missing_runtime(
    official, refresh, cold_product, backend_sidecar, backend_runtime, quality_usable
) -> None:
    selected = smoke.select_environment(
        official=official,
        refresh=refresh,
        cold_product=cold_product,
        backend_sidecar=backend_sidecar,
        backend_runtime=backend_runtime,
        quality_usable=quality_usable,
    )
    if refresh or not (cold_product or backend_sidecar or backend_runtime) or (official and not quality_usable):
        assert selected == "release"
    elif official:
        assert selected == "quality"
    else:
        assert selected == "base"


def test_media_smoke_production_imports_remain_in_the_validated_closure() -> None:
    tree = ast.parse((ROOT / "scripts/smoke_media_preparation.py").read_text(encoding="utf-8"))
    probe = ast.parse(smoke.SMOKE_IMPORT_PROBE)
    source_imports = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src.")
        for alias in node.names
    }
    validated_imports = {
        (node.module, alias.name)
        for node in ast.walk(probe)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert source_imports
    assert source_imports <= validated_imports


def test_quality_cache_contains_the_same_pinned_release_dependency_closure() -> None:
    pins = smoke.quality.pinned_packages(ROOT)
    release = {
        canonicalize_name(Requirement(line).name): next(iter(Requirement(line).specifier)).version
        for line in (ROOT / "requirements-release-constraints.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert pins == release
    for line in (ROOT / "requirements-build.txt").read_text(encoding="utf-8").splitlines():
        requirement = Requirement(line)
        assert pins[canonicalize_name(requirement.name)] in requirement.specifier


def test_valid_restore_runs_exact_inventory_validation_and_real_smoke_import_probe(tmp_path, monkeypatch) -> None:
    identity = {"identity": "fixture"}
    monkeypatch.setattr(smoke.quality, "environment_identity", Mock(return_value=identity))
    validate = Mock()
    monkeypatch.setattr(smoke.quality, "validate_environment", validate)
    run = Mock()
    monkeypatch.setattr(smoke.subprocess, "run", run)
    report = smoke.validate_restored_environment(tmp_path)
    assert report["usable"] is True
    validate.assert_called_once_with(tmp_path, identity)
    run.assert_called_once()
    assert run.call_args.args[0] == [
        str(tmp_path / "venv" / "Scripts" / "python.exe"),
        "-c",
        smoke.SMOKE_IMPORT_PROBE,
    ]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 120


@pytest.mark.parametrize("failure", (ValueError("different inventory"), FileNotFoundError("cache miss")))
def test_rejected_cache_is_removed_without_installing_packages(tmp_path, monkeypatch, failure) -> None:
    monkeypatch.setattr(smoke.quality, "environment_identity", Mock(return_value={}))
    monkeypatch.setattr(smoke.quality, "validate_environment", Mock(side_effect=failure))
    remove = Mock()
    monkeypatch.setattr(smoke.quality, "_remove_environment", remove)
    run = Mock()
    monkeypatch.setattr(smoke.subprocess, "run", run)
    assert smoke.validate_restored_environment(tmp_path)["usable"] is False
    remove.assert_called_once_with(tmp_path)
    run.assert_not_called()


def test_failed_smoke_import_selects_existing_release_environment_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(smoke.quality, "environment_identity", Mock(return_value={}))
    monkeypatch.setattr(smoke.quality, "validate_environment", Mock())
    monkeypatch.setattr(smoke.quality, "_remove_environment", Mock())
    monkeypatch.setattr(smoke.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "probe")))
    result = smoke.validate_restored_environment(tmp_path)
    assert result["usable"] is False
    assert (
        smoke.select_environment(
            official=True,
            refresh=False,
            cold_product=True,
            backend_sidecar=False,
            backend_runtime=False,
            quality_usable=result["usable"],
        )
        == "release"
    )


def _steps() -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8"))
    return {step["name"]: step for step in workflow["jobs"]["build-windows"]["steps"]}


def test_reuse_restore_is_exact_read_only_and_runs_before_cold_product_join() -> None:
    steps = _steps()
    restore = steps["Restore reusable installer smoke environment"]
    assert restore["uses"] == "actions/cache/restore@v6"
    assert restore["with"] == {"path": "venv", "key": "${{ steps.smoke-environment-key.outputs.cache-key }}"}
    validate = steps["Validate reusable installer smoke environment"]
    assert "reuse_installer_smoke_environment validate" in validate["run"]
    assert list(steps).index(validate["name"]) < list(steps).index("Download cold products in parallel")
    assert not any(
        step.get("uses") == "actions/cache/save@v6" and step["with"]["path"] == "venv" for step in steps.values()
    )


@pytest.mark.parametrize("quality_usable", ("true", "false", ""))
@pytest.mark.parametrize(
    "attested", ("cold-products", "backend-sidecar-validation", "backend-runtime-validation", "none")
)
@pytest.mark.parametrize("refresh", ("true", "false"))
def test_full_environment_gate_preserves_every_cache_miss_and_explicit_refresh(
    quality_usable, attested, refresh
) -> None:
    steps = _steps()
    values = {
        "needs.release-plan.outputs.official-release": "true",
        "env.SCRIBER_REFRESH_RELEASE_CACHE_ARTIFACTS": refresh,
        "steps.smoke-environment.outputs.usable": quality_usable,
        "steps.cold-products.outputs.usable": "false",
        "steps.backend-sidecar-validation.outputs.usable": "false",
        "steps.backend-runtime-validation.outputs.usable": "false",
        "steps.python-venv-cache.outputs.cache-hit": "false",
        "steps.python-venv-validation.outputs.usable": "false",
        "steps.python-wheelhouse-cache.outputs.cache-hit": "false",
        "steps.python-wheelhouse-artifact.outputs.restored": "false",
    }
    if attested != "none":
        values[f"steps.{attested}.outputs.usable"] = "true"
    expected = refresh == "true" or quality_usable != "true" or attested == "none"
    for name in (
        "Restore Python dependency cache",
        "Restore Python venv release artifact",
        "Validate restored Python environment",
        "Restore Python wheelhouse cache",
        "Restore Python wheelhouse release artifact",
        "Restore pip package store",
        "Install Python dependencies",
    ):
        expression = steps[name]["if"]
        for variable, value in values.items():
            expression = expression.replace(variable, json.dumps(value))
        expression = " ".join(expression.replace("&&", "and").replace("||", "or").split())
        tree = ast.parse(expression, mode="eval")
        assert all(
            isinstance(
                node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Constant)
            )
            for node in ast.walk(tree)
        ), expression
        assert eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}}) == expected, name


def test_both_build_and_downloaded_installer_smoke_use_selected_python_and_keep_media_checks() -> None:
    steps = _steps()
    selected = "${{ steps.installer-python.outputs.python }}"
    build = steps["Build Windows installer"]["run"]
    installed = steps["Smoke downloaded installer candidate"]["run"]
    assert selected in build
    assert '"-PythonExecutable"' in build
    assert '"-RunMediaPreparationSmoke"' in build
    assert selected in installed
    for switch in (
        "-SimulateUpgrade",
        "-VerifyFrontend",
        "-VerifyMediaPreparation",
        "-VerifySupportBundle",
        "-VerifyUninstall",
    ):
        assert switch in installed


def test_release_consolidates_startup_smoke_on_required_installed_candidate_gate() -> None:
    steps = _steps()
    names = list(steps)
    build = steps["Build Windows installer"]["run"]
    installed = steps["Smoke downloaded installer candidate"]
    publication = steps["Verify and publish exact GitHub release transaction"]
    assert '$buildArgs += "-SkipSmoke"' in build
    assert "if (-not $isOfficialRelease)" not in build
    assert installed["id"] == "installed-smoke"
    assert installed["if"] == publication["if"] == "needs.release-plan.outputs.official-release == 'true'"
    assert not installed.get("continue-on-error", False)
    assert (
        names.index("Cryptographically verify downloaded updater signatures")
        < names.index("Smoke downloaded installer candidate")
        < names.index("Verify and publish exact GitHub release transaction")
    )
    assert "scripts.ci.validate_installer_smoke_report" in installed["run"]
    assert "--installer (Join-Path $downloadRoot ([string]$installers[0].name))" in installed["run"]
    assert "if ($LASTEXITCODE -ne 0)" in installed["run"]
    assert "${{ steps.installed-smoke.outcome }}" in publication["run"]
    assert ' -ne "success"' in publication["run"]
    assert (
        "tmp/installer-smoke/downloaded-release-candidate.json"
        in steps["Upload draft verification evidence"]["with"]["path"]
    )
    evidence_condition = steps["Upload draft verification evidence"]["if"]
    assert "always()" in evidence_condition
    assert "steps.installed-smoke.outcome != 'skipped'" in evidence_condition
