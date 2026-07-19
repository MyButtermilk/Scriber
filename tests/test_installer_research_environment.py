from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts import write_installer_research_environment_manifest as environment_manifest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "12345678-1234-4234-8234-123456789abc"


def test_environment_preparer_is_scoped_and_has_no_unsafe_powershell() -> None:
    script = (REPO_ROOT / "scripts/prepare_installer_research_environment.ps1").read_text(
        encoding="utf-8"
    )

    assert "Resolve-CanonicalUuid" in script
    assert "Assert-UnderRoot" in script
    assert "Assert-NoReparsePath" in script
    assert "Resolve-PlainFileUnderRoot" in script
    assert "baseline environment requires the canonical repository requirements files" in script
    assert 'Join-Path $runRoot "snapshots"' in script
    assert '"requirements-base.txt"' in script
    assert "immutable baseline requirement snapshot" in script
    assert "-Recurse" in script
    assert '"autoresearch-results\\installer-size\\$canonicalRunId"' in script
    assert "--no-index" in script
    assert "pip check" in script
    assert "Seeding the deterministic comtypes generated package" in script
    assert "import comtypes.client, comtypes.gen" in script
    assert script.index("import comtypes.client, comtypes.gen") < script.index(
        "write_installer_research_environment_manifest.py"
    )
    assert "Invoke-Expression" not in script
    assert "EncodedCommand" not in script
    assert "ExecutionPolicy Bypass" not in script
    assert "Get-Command python" not in script


def test_toolchain_preparer_pins_and_verifies_downloaded_inputs() -> None:
    script = (REPO_ROOT / "scripts/prepare_installer_research_toolchain.ps1").read_text(
        encoding="utf-8"
    )

    assert "Resolve-CanonicalUuid" in script
    assert "Assert-UnderRoot" in script
    assert "Assert-NoReparsePath" in script
    assert "-Recurse" in script
    assert "SHASUMS256.txt" in script
    assert "Get-FileHash" in script
    assert "rustup toolchain install $RustToolchain --profile minimal" in script
    assert "rustup component add --toolchain $RustToolchain rustfmt clippy" in script
    assert "rustup which --toolchain $RustToolchain rustc" in script
    assert "@tauri-apps\\cli\\tauri.js" in script
    assert "$nodeExecutable $npmCli ci --no-audit --no-fund --prefer-offline" in script
    assert "frontendPackageLock" in script
    assert "frontendNodeModules" in script
    assert "nativeTauriCli" in script
    assert "cli.win32-x64-msvc.node" in script
    assert 'Join-Path $frontendNodeModules ".vite-temp"' in script
    assert "Vite temporary config directory is not empty after npm ci" in script
    assert "Get-PlainTreeIdentity" in script
    assert "$env:RUSTUP_TOOLCHAIN = $RustToolchain" in script
    assert 'Join-Path $nsisRoot "Bin\\makensis.exe"' in script
    assert '$nsisRoot = Join-Path $env:LOCALAPPDATA "tauri\\NSIS"' in script
    assert '-Path $nsisRoot `\n    -Label "Tauri NSIS toolchain" `\n    -Recurse' in script
    assert "$nsisTreeIdentity = Get-PlainTreeIdentity -Root $nsisRoot" in script
    assert 'nsisTree = $nsisTreeIdentity' in script
    assert '$nsisIdentity["relativePath"] = $makensisRelativePath' in script
    assert "Invoke-Expression" not in script
    assert "EncodedCommand" not in script
    assert "ExecutionPolicy Bypass" not in script


def test_environment_manifest_is_path_redacted_and_stable(
    tmp_path: Path, monkeypatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(b"wheel")
    requirement = tmp_path / "requirements.txt"
    requirement.write_text("example==1.0\n", encoding="utf-8")
    distributions = [
        {
            "name": "example",
            "version": "1.0",
            "recordSha256": "1" * 64,
            "fileCount": 1,
            "installedBytes": 5,
            "contentSha256": "2" * 64,
        }
    ]
    monkeypatch.setattr(
        environment_manifest,
        "_distribution_entries",
        lambda *, environment_root: distributions,
    )
    generated_trees = [
        {
            "id": "comtypes.gen",
            "fileCount": 1,
            "installedBytes": 56,
            "contentSha256": "3" * 64,
        }
    ]
    monkeypatch.setattr(
        environment_manifest,
        "_generated_tree_entries",
        lambda *, environment_root: generated_trees,
    )
    arguments = {
        "run_id": RUN_ID,
        "environment_name": "baseline",
        "wheelhouse": wheelhouse,
        "requirements": [requirement],
        "python_executable": Path(sys.executable),
    }
    first_payload = environment_manifest.build_manifest(**arguments)
    second_payload = environment_manifest.build_manifest(**arguments)
    assert first_payload == second_payload
    assert first_payload["runId"] == RUN_ID
    assert first_payload["environmentName"] == "baseline"
    serialized = json.dumps(first_payload, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert str(Path(sys.executable).parent) not in serialized
    assert first_payload["wheelhouse"][0]["sha256"]
    assert first_payload["requirements"][0]["sha256"]
    assert first_payload["productDependenciesSha256"]
    assert all(entry["contentSha256"] for entry in first_payload["distributions"])
    assert all(entry["fileCount"] > 0 for entry in first_payload["distributions"])
    assert first_payload["generatedTrees"] == generated_trees


def test_generated_comtypes_tree_identity_detects_unrecorded_modules(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "environment"
    generated_root = environment_root / "Lib" / "site-packages" / "comtypes" / "gen"
    generated_root.mkdir(parents=True)
    (generated_root / "__init__.py").write_text(
        "# deterministic generated package\n", encoding="utf-8"
    )
    pycache = generated_root / "__pycache__"
    pycache.mkdir()
    (pycache / "__init__.pyc").write_bytes(b"volatile bytecode")

    first = environment_manifest._generated_tree_identity(
        generated_root,
        tree_id="comtypes.gen",
        environment_root=environment_root.resolve(),
    )
    (generated_root / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    second = environment_manifest._generated_tree_identity(
        generated_root,
        tree_id="comtypes.gen",
        environment_root=environment_root.resolve(),
    )

    assert first["fileCount"] == 1
    assert second["fileCount"] == 2
    assert first["contentSha256"] != second["contentSha256"]
