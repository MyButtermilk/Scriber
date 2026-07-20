from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_numpy_noblas_wheel.py"
LOCK_PATH = REPO_ROOT / "packaging" / "wheels" / "numpy-noblas-wheel-lock-v1.json"
WHEEL_PATH = (
    REPO_ROOT
    / "packaging"
    / "wheels"
    / "numpy-2.4.6+scriber.noblas.1-cp313-cp313-win_amd64.whl"
)


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_numpy_noblas_wheel", VALIDATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def _write_lock(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "numpy-noblas-wheel-lock-v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_repository_numpy_noblas_wheel_matches_lock() -> None:
    summary = validator.validate(LOCK_PATH, WHEEL_PATH)

    assert summary["ok"] is True
    assert summary["version"] == "2.4.6+scriber.noblas.1"
    assert summary["sha256"] == (
        "e28ee25278e91bbb99153e9e7bcdbb0d8e88d1d4401f76218e0cd71707e0151c"
    )
    assert summary["blas"] == "none"
    assert summary["lapack"] == "none"


def test_validated_wheel_extracts_to_empty_overlay(tmp_path: Path) -> None:
    destination = tmp_path / "overlay"
    destination.mkdir()

    summary = validator.validate(LOCK_PATH, WHEEL_PATH)
    extracted = validator.extract_validated_wheel(WHEEL_PATH, destination)

    assert summary["ok"] is True
    assert extracted == 930
    assert (destination / "numpy" / "__init__.py").is_file()
    assert (
        destination
        / "numpy-2.4.6+scriber.noblas.1.dist-info"
        / "METADATA"
    ).is_file()
    assert not list(destination.rglob("*openblas*"))
    assert not list(destination.rglob("*.dll"))


def test_validated_wheel_refuses_nonempty_overlay(tmp_path: Path) -> None:
    destination = tmp_path / "overlay"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(validator.ValidationError, match="must be empty"):
        validator.extract_validated_wheel(WHEEL_PATH, destination)


def test_validator_rejects_lock_sha_mismatch(tmp_path: Path) -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    lock["artifact"]["sha256"] = "0" * 64

    with pytest.raises(validator.ValidationError, match="SHA-256"):
        validator.validate(_write_lock(tmp_path, lock), WHEEL_PATH)


def test_static_runtime_version_must_match_metadata() -> None:
    with zipfile.ZipFile(WHEEL_PATH) as archive:
        source = archive.read("numpy/version.py")
    changed = source.replace(
        b'version = "2.4.6+scriber.noblas.1"', b'version = "2.4.6"', 1
    )
    assert changed != source

    with pytest.raises(validator.ValidationError, match="runtime version"):
        validator._validate_static_runtime_version(
            changed, expected_version="2.4.6+scriber.noblas.1"
        )


def test_validator_rejects_dll_archive_member() -> None:
    with pytest.raises(validator.ValidationError, match="binary payload"):
        validator._validate_no_forbidden_archive_entries(
            ["numpy.libs/openblas.dll"],
            suffixes=[".dll"],
            path_markers=["numpy.libs/"],
        )


def test_pe_import_policy_rejects_openblas() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    validation = lock["validation"]
    replacement_source = b"MSVCP140.dll"
    replacement_target = b"OPENBLAS.dll"
    assert len(replacement_source) == len(replacement_target)

    mutated_pyd: bytes | None = None
    mutated_name = ""
    with zipfile.ZipFile(WHEEL_PATH) as archive:
        for name in archive.namelist():
            if not name.casefold().endswith(".pyd"):
                continue
            data = archive.read(name)
            if replacement_source in data:
                mutated_pyd = data.replace(replacement_source, replacement_target, 1)
                mutated_name = name
                break
    assert mutated_pyd is not None

    imports = validator._pe_imports(mutated_pyd, label=mutated_name)
    assert "OPENBLAS.dll" in imports
    with pytest.raises(validator.ValidationError, match="numerical runtime"):
        validator._validate_pe_import_policy(
            imports,
            forbidden_markers=validation["forbiddenPeImportMarkers"],
            allowed_imports=validation["allowedPeImports"],
        )


def test_validator_rejects_changed_license_inventory(tmp_path: Path) -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    lock["validation"]["licenseFiles"].pop()

    with pytest.raises(validator.ValidationError, match="license members"):
        validator.validate(_write_lock(tmp_path, lock), WHEEL_PATH)
