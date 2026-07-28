from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend_runtime.launcher import LayerValidationError, _validate_python_runtime
from backend_runtime.runtime_policy import (
    RuntimePolicyError,
    validate_packaged_runtime_policy,
    validate_runtime_policy_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _payload(root: Path, *, flavor: str = "Official", jit: bool = False) -> dict[str, object]:
    dll = root / "_internal" / "python314.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"locked-python-runtime")
    return {
        "schemaVersion": 1,
        "name": "scriber-python-runtime-policy",
        "python": {
            "version": "3.14.6",
            "cacheTag": "cpython-314",
            "dll": {
                "path": "_internal/python314.dll",
                "length": dll.stat().st_size,
                "sha256": hashlib.sha256(dll.read_bytes()).hexdigest(),
            },
        },
        "runtimeFlavor": flavor,
        "variantId": {
            ("Official", False): "O0",
            ("Official", True): "O1",
            ("ClangPgo", False): "C0",
            ("ClangPgo", True): "C1",
            ("ClangPgoTail", False): "T0",
            ("ClangPgoTail", True): "T1",
        }[(flavor, jit)],
        "compiler": "MSC v.1944 64 bit (AMD64)" if flavor == "Official" else "Clang 19.1.7",
        "tailCallInterpreter": flavor == "ClangPgoTail",
        "jit": {"available": True, "expected": jit, "active": jit},
        "runtimeLockSha256": "a" * 64,
    }


def test_runtime_policy_accepts_exact_official_cp314_with_disabled_jit(tmp_path: Path) -> None:
    payload = _payload(tmp_path)

    assert (
        validate_runtime_policy_payload(
            payload,
            tmp_path,
            python_version="3.14.6",
            cache_tag="cpython-314",
            compiler="MSC v.1944 64 bit (AMD64)",
            jit_available=True,
            jit_active=False,
        )
        is payload
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["python"].update(version="3.14.5"), "exactly CPython 3.14.6"),
        (lambda payload: payload.update(runtimeFlavor="ClangPgoTail"), "tail-call"),
        (lambda payload: payload["jit"].update(active=True), "JIT state"),
        (lambda payload: payload.update(runtimeLockSha256="bad"), "lock checksum"),
    ],
)
def test_runtime_policy_rejects_identity_drift(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    payload = _payload(tmp_path)
    mutation(payload)

    with pytest.raises(RuntimePolicyError, match=message):
        validate_runtime_policy_payload(
            payload,
            tmp_path,
            python_version="3.14.6",
            cache_tag="cpython-314",
            compiler="MSC v.1944 64 bit (AMD64)",
            jit_available=True,
            jit_active=False,
        )


def test_supervised_runtime_requires_clean_explicit_environment(tmp_path: Path, monkeypatch) -> None:
    payload = _payload(tmp_path)
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SCRIBER_PYTHON_RUNTIME_MANIFEST", str(manifest))

    with pytest.raises(RuntimePolicyError, match="PYTHON_JIT explicitly"):
        validate_runtime_policy_payload(
            payload,
            tmp_path,
            python_version="3.14.6",
            cache_tag="cpython-314",
            compiler="MSC v.1944 64 bit (AMD64)",
            jit_available=True,
            jit_active=False,
            environ={},
            require_supervisor_environment=True,
        )

    clean_environment = {
        "PYTHON_JIT": "0",
        "SCRIBER_PYTHON_RUNTIME_LOCK_SHA256": "a" * 64,
    }
    validate_runtime_policy_payload(
        payload,
        tmp_path,
        python_version="3.14.6",
        cache_tag="cpython-314",
        compiler="MSC v.1944 64 bit (AMD64)",
        jit_available=True,
        jit_active=False,
        environ=clean_environment,
        require_supervisor_environment=True,
    )
    with pytest.raises(RuntimePolicyError, match="PYTHON_GIL"):
        validate_runtime_policy_payload(
            payload,
            tmp_path,
            python_version="3.14.6",
            cache_tag="cpython-314",
            compiler="MSC v.1944 64 bit (AMD64)",
            jit_available=True,
            jit_active=False,
            environ={**clean_environment, "PYTHON_GIL": "1"},
            require_supervisor_environment=True,
        )


def test_packaged_policy_rejects_external_manifest_path(tmp_path: Path, monkeypatch) -> None:
    payload = _payload(tmp_path)
    manifest = tmp_path / "python-runtime-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    external = tmp_path / "external-python-runtime-manifest.json"
    external.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SCRIBER_PYTHON_RUNTIME_MANIFEST", str(external))

    with pytest.raises(RuntimePolicyError, match="packaged manifest"):
        validate_packaged_runtime_policy(tmp_path)


def test_launcher_binds_runtime_policy_bytes_to_frozen_layer(tmp_path: Path) -> None:
    policy = tmp_path / "python-runtime-manifest.json"
    policy.write_bytes(b"{}")
    runtime_manifest = {
        "content": {
            "files": [
                {
                    "path": "python-runtime-manifest.json",
                    "length": 2,
                    "sha256": "0" * 64,
                }
            ]
        }
    }

    with pytest.raises(LayerValidationError, match="frozen runtime layer"):
        _validate_python_runtime(tmp_path, runtime_manifest)


def test_windows_builds_expose_locked_runtime_flavor_and_jit_parameters() -> None:
    sidecar = (REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1").read_text(encoding="utf-8")
    release = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    for source in (sidecar, release):
        assert '[ValidateSet("Official", "ClangPgo", "ClangPgoTail")]' in source
        assert '[string]$PythonRuntimeFlavor = "Official"' in source
        assert '[ValidateSet("Disabled", "Enabled")]' in source
        assert '[string]$PythonJitMode = "Disabled"' in source
        assert "cpython-windows-runtime-input-lock-v1.json" in source
    assert '"-PythonRuntimeFlavor"' in release
    assert '"-PythonJitMode"' in release
    assert '"-PythonRuntimeLockPath"' in release
    assert '[string]$PythonRuntimeExperimentRoot = ""' in release
    assert '"-RuntimeCacheRoot", (Join-Path $resolvedPythonRuntimeExperimentRoot "runtime-cache")' in release
    assert '"-SidecarCacheRoot", (Join-Path $resolvedPythonRuntimeExperimentRoot "sidecar-cache")' in release
    assert "Use either -PythonRuntimeExperimentRoot or -ResearchBuildRoot, not both." in release
    assert "Resolve-LockedNumPyWheel -Root $Root" in sidecar
    assert "python-runtime-manifest.json" in sidecar


def test_release_workflows_fix_official_runtime_with_disabled_jit() -> None:
    for relative in (
        ".github/workflows/release-cache-maintenance.yml",
        ".github/workflows/release-windows.yml",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "SCRIBER_RELEASE_PYTHON_RUNTIME_FLAVOR: Official" in source
        assert "SCRIBER_RELEASE_PYTHON_JIT_MODE: Disabled" in source
        assert "-PythonRuntimeFlavor $env:SCRIBER_RELEASE_PYTHON_RUNTIME_FLAVOR" in source
        assert "-PythonJitMode $env:SCRIBER_RELEASE_PYTHON_JIT_MODE" in source


def test_python_runtime_selection_is_not_a_user_setting() -> None:
    user_configuration_sources = [
        *(REPO_ROOT / "src").rglob("*.py"),
        *(REPO_ROOT / "Frontend" / "client" / "src").rglob("*.ts"),
        *(REPO_ROOT / "Frontend" / "client" / "src").rglob("*.tsx"),
    ]
    forbidden = ("pythonruntimeflavor", "pythonjitmode", "python_jit")
    for path in user_configuration_sources:
        source = path.read_text(encoding="utf-8").lower()
        assert not any(token in source for token in forbidden), path


def test_tauri_supervisor_scrubs_python_runtime_controls_before_spawn() -> None:
    source = (REPO_ROOT / "Frontend" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    spawn = source.split("fn spawn_backend(", 1)[1].split("fn resolve_backend_command(", 1)[0]

    for name in (
        "PYTHON_JIT_ENV",
        "PYTHON_GIL_ENV",
        "PYTHON_TLBC_ENV",
        "PYTHON_RUNTIME_POLICY_ENV",
        "PYTHON_RUNTIME_LOCK_SHA256_ENV",
    ):
        assert name in spawn
    assert "command.env_remove(name);" in spawn
    assert "command.env(PYTHON_JIT_ENV, python_runtime.jit);" in spawn
