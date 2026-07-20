from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend_runtime.contract import (
    APPLICATION_EXTERNAL_IMPORT_ROOTS,
    APPLICATION_OPTIONAL_IMPORT_EXEMPTIONS,
    RUNTIME_CONTRACT_NAME,
    RUNTIME_CONTRACT_REVISION,
    RUNTIME_REQUIRED_IMPORTS,
)
from backend_runtime.launcher import (
    LayerValidationError,
    validate_application_layer,
    validate_runtime_layer,
)
from scripts.stage_backend_application_layer import stage_application_layer
from scripts.stage_backend_application_layer import validate_staged_application_layer


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_KEY = "a" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_runtime_manifest(runtime_root: Path, executable: Path) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "name": "scriber-backend-runtime-layer",
        "cacheKey": RUNTIME_KEY,
        "runtimeContract": {
            "name": RUNTIME_CONTRACT_NAME,
            "revision": RUNTIME_CONTRACT_REVISION,
        },
        "executable": {
            "sha256": _sha256(executable),
            "length": executable.stat().st_size,
        },
        "content": {
            "fileCount": 1,
            "treeSha256": "c" * 64,
            "files": [
                {
                    "path": executable.name,
                    "length": executable.stat().st_size,
                    "sha256": _sha256(executable),
                }
            ],
        },
    }
    (runtime_root / "runtime-layer-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest


def _layered_runtime(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    runtime_root = tmp_path / "scriber-backend"
    runtime_root.mkdir()
    executable = runtime_root / "scriber-backend.exe"
    executable.write_bytes(b"stable-frozen-runtime")
    runtime_manifest = _write_runtime_manifest(runtime_root, executable)
    stage_application_layer(REPO_ROOT, runtime_root, runtime_cache_key=RUNTIME_KEY)
    return runtime_root, executable, runtime_manifest


def test_application_layer_is_physical_complete_and_bound_to_runtime(tmp_path: Path) -> None:
    runtime_root, executable, expected_runtime = _layered_runtime(tmp_path)

    runtime_manifest = validate_runtime_layer(
        runtime_root, executable_path=executable
    )
    app_manifest = validate_application_layer(runtime_root, runtime_manifest)

    assert runtime_manifest == expected_runtime
    assert app_manifest["runtimeCacheKey"] == RUNTIME_KEY
    assert app_manifest["entryPoint"] == "src.backend_worker:main"
    assert (runtime_root / "app" / "src" / "web_api.py").is_file()
    assert (runtime_root / "app" / "scripts" / "check_backend_runtime_imports.py").is_file()
    assert not any("__pycache__" in path.parts for path in (runtime_root / "app").rglob("*"))
    assert not any(path.suffix in {".pyc", ".pyo"} for path in (runtime_root / "app").rglob("*"))


def test_application_layer_rejects_tampering_and_unlisted_files(tmp_path: Path) -> None:
    runtime_root, executable, _runtime_manifest = _layered_runtime(tmp_path)
    runtime_manifest = validate_runtime_layer(runtime_root, executable_path=executable)
    web_api = runtime_root / "app" / "src" / "web_api.py"
    web_api.write_text(web_api.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    with pytest.raises(LayerValidationError, match="checksum|length"):
        validate_application_layer(runtime_root, runtime_manifest)

    stage_application_layer(REPO_ROOT, runtime_root, runtime_cache_key=RUNTIME_KEY)
    (runtime_root / "app" / "src" / "unlisted_shadow.py").write_text(
        "raise RuntimeError('must never load')\n", encoding="utf-8"
    )
    with pytest.raises(LayerValidationError, match="unlisted"):
        validate_application_layer(runtime_root, runtime_manifest)


def test_application_layer_rejects_another_runtime_generation(tmp_path: Path) -> None:
    runtime_root, executable, _runtime_manifest = _layered_runtime(tmp_path)
    runtime_manifest = validate_runtime_layer(runtime_root, executable_path=executable)
    stage_application_layer(REPO_ROOT, runtime_root, runtime_cache_key="b" * 64)

    with pytest.raises(LayerValidationError, match="do not match"):
        validate_application_layer(runtime_root, runtime_manifest)


def test_physical_application_import_check_is_repeatable_without_bytecode(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "scriber-backend"
    runtime_root.mkdir()
    _write_runtime_manifest(runtime_root, Path(sys.executable))
    stage_application_layer(REPO_ROOT, runtime_root, runtime_cache_key=RUNTIME_KEY)
    command = (
        "import sys; from pathlib import Path; "
        "from backend_runtime.launcher import launch_application; "
        "sys.argv=['scriber-backend','--runtime-import-check']; "
        "raise SystemExit(launch_application(Path(sys.argv_orig[1])))"
    )
    # Keep the runtime root out of the application argv that backend_worker sees.
    command = command.replace(
        "import sys;", "import sys; sys.argv_orig=list(sys.argv);"
    )
    env = os.environ.copy()
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    for _attempt in range(2):
        result = subprocess.run(
            [sys.executable, "-c", command, str(runtime_root)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        validate_staged_application_layer(
            runtime_root,
            runtime_cache_key=RUNTIME_KEY,
        )

    assert not any(
        path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
        for path in (runtime_root / "app").rglob("*")
    )


def test_src_external_imports_are_explicit_runtime_or_optional_exclusions() -> None:
    roots: set[str] = set()
    for source in (REPO_ROOT / "src").rglob("*.py"):
        source_relative = source.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (source_relative, alias.name) not in APPLICATION_OPTIONAL_IMPORT_EXEMPTIONS:
                        roots.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if (source_relative, node.module) not in APPLICATION_OPTIONAL_IMPORT_EXEMPTIONS:
                    roots.add(node.module.split(".", 1)[0])

    external = roots - set(sys.stdlib_module_names) - {"src", "scripts"}
    declared = set(APPLICATION_EXTERNAL_IMPORT_ROOTS)
    assert external <= declared, f"Undeclared application imports: {sorted(external - declared)}"
    assert (
        "src/gemini_transcribe.py",
        "google.generativeai",
    ) in APPLICATION_OPTIONAL_IMPORT_EXEMPTIONS
    assert all(root != "google" for root in APPLICATION_EXTERNAL_IMPORT_ROOTS)


def test_direct_pipecat_imports_use_exact_frozen_runtime_modules() -> None:
    """Every direct Pipecat module import must exist in the frozen boundary.

    The broader external-root test intentionally sees only ``pipecat``.  This
    exact-module parity check prevents a valid application import such as
    ``pipecat.transports.base_input`` from being absent in the PyInstaller
    runtime while a nearby module happens to be frozen.
    """

    direct_modules: set[str] = set()
    for source in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                direct_modules.update(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("pipecat.")
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                and node.module.startswith("pipecat.")
            ):
                direct_modules.add(node.module)

    frozen_modules = {module for module, _reason in RUNTIME_REQUIRED_IMPORTS}
    missing = direct_modules - frozen_modules
    assert not missing, (
        f"Direct Pipecat imports absent from frozen runtime: {sorted(missing)}"
    )
    assert "pipecat.transports.base_input" in direct_modules


def test_application_layer_stages_only_tracked_files(tmp_path: Path) -> None:
    untracked = REPO_ROOT / "src" / "local-release-secret.env"
    assert not untracked.exists()
    untracked.write_text("SECRET=must-not-ship\n", encoding="utf-8")
    try:
        runtime_root = tmp_path / "scriber-backend"
        runtime_root.mkdir()
        manifest = stage_application_layer(
            REPO_ROOT,
            runtime_root,
            runtime_cache_key=RUNTIME_KEY,
        )
    finally:
        untracked.unlink(missing_ok=True)

    paths = {entry["path"] for entry in manifest["files"]}
    assert "src/local-release-secret.env" not in paths
    assert not (runtime_root / "app" / "src" / "local-release-secret.env").exists()


def test_pyinstaller_spec_freezes_launcher_and_not_first_party_application() -> None:
    spec = (REPO_ROOT / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )
    excludes = spec.split("excludes=[", 1)[1].split("],\n    noarchive", 1)[0]

    assert '[str(repo_root / "backend_runtime" / "launcher.py")]' in spec
    assert "RUNTIME_REQUIRED_IMPORTS" in spec
    assert '"src",' in excludes
    assert '"scripts",' in excludes
    assert 'repo_root / "src" / "backend_worker.py"' not in spec
    assert '"src.' not in spec


def test_numpy_product_overlay_is_validated_safe_fail_closed_and_pyinstaller_scoped() -> None:
    builder = (REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1").read_text(
        encoding="utf-8"
    )
    spec = (REPO_ROOT / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )
    overlay_function = builder.split(
        "function New-ValidatedNumPyPyInstallerOverlay", 1
    )[1].split("function Invoke-TimedStep", 1)[0]
    pyinstaller_block = builder.split(
        'Invoke-TimedStep -Label "pyinstaller-build"', 1
    )[1].split('Invoke-TimedStep -Label "frozen-runtime-layer-check"', 1)[0]

    assert "scripts\\validate_numpy_noblas_wheel.py" in overlay_function
    assert "--lock $lockPath" in overlay_function
    assert "--wheel $wheelPath" in overlay_function
    assert "--extract-to $stagingRoot" in overlay_function
    assert "-Root $BuildWorkRoot" in overlay_function
    assert "$safeExtractProgram" not in overlay_function

    for variable in (
        "SCRIBER_NUMPY_WHEEL_PATH",
        "SCRIBER_NUMPY_OVERLAY_ROOT",
        "PYTHONPATH",
    ):
        assert f"$env:{variable} =" in pyinstaller_block
        assert f"Remove-Item Env:{variable}" in pyinstaller_block
    assert "$env:PYTHONPATH = $script:NumPyPyInstallerOverlay.OverlayRoot" in pyinstaller_block
    assert "-m pip install" not in overlay_function

    assert 'numpy_version = "2.4.6+scriber.noblas.1"' in spec
    assert 'os.environ.get("SCRIBER_NUMPY_WHEEL_PATH")' in spec
    assert 'os.environ.get("SCRIBER_NUMPY_OVERLAY_ROOT")' in spec
    assert "numpy_wheel_path != expected_numpy_wheel" in spec
    assert "numpy_origin.is_relative_to(numpy_overlay_root)" in spec
    assert "numpy_distribution.version != numpy_version" in spec
    assert "numpy_distribution_root.is_relative_to(numpy_overlay_root)" in spec
    assert 'numpy_metadata_datas = copy_metadata("numpy")' in spec
    assert "Path(numpy_metadata_source).resolve() != expected_numpy_dist_info" in spec
    assert 'numpy_metadata_prefix + "METADATA" not in analysis_data_destinations' in spec
    assert 'destination.startswith(numpy_metadata_prefix + "licenses/")' in spec
    assert 'destination.startswith("numpy-2.4.6.dist-info/")' in spec
    assert "pathex=[str(numpy_overlay_root)," in spec


def test_runtime_cache_key_source_excludes_application_code() -> None:
    script = (REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1").read_text(
        encoding="utf-8"
    )
    runtime_block = script.split("$backendRuntimeEntries = New-EntryList", 1)[1].split(
        'Write-KeyFile -Name "backend-runtime.txt"', 1
    )[0]
    application_block = script.split("$backendEntries = New-EntryList", 1)[1]

    assert '"backend_runtime"' in runtime_block
    assert '"requirements-base.txt"' in runtime_block
    assert (
        '"packaging/wheels/numpy-2.4.6+scriber.noblas.1-cp313-cp313-win_amd64.whl"'
        in runtime_block
    )
    assert '"packaging/wheels/numpy-noblas-wheel-lock-v1.json"' in runtime_block
    assert '"scripts/validate_numpy_noblas_wheel.py"' in runtime_block
    assert '"src"' not in runtime_block
    assert 'Add-GitTrackedEntries -Entries $backendEntries -Paths @("src")' in application_block
    assert '"scripts/stage_backend_application_layer.py"' in application_block
