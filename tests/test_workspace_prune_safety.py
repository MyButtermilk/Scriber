from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "prune_local_dev_workspace.ps1"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is required for the Windows workspace-prune contract")
    return executable


def _fake_scriber_workspace(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "web_api.py").write_text("# fixture\n", encoding="utf-8")
    (root / "Frontend" / "src-tauri").mkdir(parents=True)
    (root / "Frontend" / "src-tauri" / "tauri.conf.json").write_text(
        '{"productName":"Scriber"}\n', encoding="utf-8"
    )
    fixture_script = root / "scripts" / SCRIPT.name
    shutil.copyfile(SCRIPT, fixture_script)
    return fixture_script


def _run_dry_run(
    script: Path, *additional_arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *additional_arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        pass

    cmd = shutil.which("cmd.exe")
    if not cmd:
        pytest.skip("directory symlinks and Windows junctions are unavailable")
    result = subprocess.run(
        [cmd, "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(f"directory symlinks and junctions are unavailable: {result.stderr}")


def test_workspace_prune_is_dry_run_by_default(tmp_path: Path) -> None:
    workspace = tmp_path / "Scriber"
    script = _fake_scriber_workspace(workspace)
    payload = workspace / "tmp" / "nested" / "payload.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"rebuildable")

    result = _run_dry_run(script)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry_run"
    assert report["bytesRemoved"] == 0
    assert report["targetCount"] == 1
    assert report["targets"][0]["bytes"] == len(b"rebuildable")
    assert report["targets"][0]["action"] == "would_remove"
    assert payload.read_bytes() == b"rebuildable"


def test_workspace_prune_dry_run_never_follows_nested_reparse_points(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "Scriber"
    script = _fake_scriber_workspace(workspace)
    local_payload = workspace / "tmp" / "local.bin"
    local_payload.parent.mkdir(parents=True)
    local_payload.write_bytes(b"local")
    external = tmp_path / "outside"
    external.mkdir()
    external_payload = external / "must-not-be-counted.bin"
    external_payload.write_bytes(b"outside-payload")
    link = workspace / "tmp" / "outside-link"
    _create_directory_reparse_point(link, external)

    result = _run_dry_run(script)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["targets"][0]["bytes"] == len(b"local")
    assert external_payload.read_bytes() == b"outside-payload"
    assert link.exists()


def test_workspace_prune_refuses_a_reparse_point_build_root(tmp_path: Path) -> None:
    workspace = tmp_path / "Scriber"
    script = _fake_scriber_workspace(workspace)
    external_build = tmp_path / "outside-build"
    external_build.mkdir()
    external_payload = external_build / "must-not-be-targeted.bin"
    external_payload.write_bytes(b"outside")
    _create_directory_reparse_point(workspace / "build", external_build)

    result = _run_dry_run(script)

    assert result.returncode != 0
    assert "Refusing to traverse a reparse point" in (result.stderr + result.stdout)
    assert external_payload.read_bytes() == b"outside"


def test_unused_repository_local_model_is_explicitly_opt_in(tmp_path: Path) -> None:
    workspace = tmp_path / "Scriber"
    script = _fake_scriber_workspace(workspace)
    model = workspace / "sherpa-onnx-parakeet-primeline-de-int8" / "encoder.int8.onnx"
    model.parent.mkdir()
    model.write_bytes(b"reproducible-model-fixture")

    default_result = _run_dry_run(script)
    opted_in_result = _run_dry_run(script, "-IncludeUnusedLocalModels")

    assert default_result.returncode == 0, default_result.stderr
    assert json.loads(default_result.stdout)["targetCount"] == 0
    assert opted_in_result.returncode == 0, opted_in_result.stderr
    opted_in_report = json.loads(opted_in_result.stdout)
    assert opted_in_report["includeUnusedLocalModels"] is True
    assert opted_in_report["targetCount"] == 1
    assert opted_in_report["targets"][0]["bytes"] == len(b"reproducible-model-fixture")
    assert model.read_bytes() == b"reproducible-model-fixture"


def test_workspace_prune_has_no_recursive_delete_primitive() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "function Remove-SafeWorkspaceTree" in source
    assert "function Remove-WorkspaceReparsePoint" in source
    assert "Assert-ScriberWorkspace -WorkspaceRoot $repoRoot" in source
    assert "Get-ChildItem -LiteralPath $target -Recurse" not in source
    assert "Remove-Item -LiteralPath $target -Recurse" not in source
    assert "[System.IO.Directory]::Delete($safeDirectory, $false)" in source
    assert '"dist\\tauri-sidecar"' in source
    assert '\n    "dist",' not in source
    assert '"Frontend\\tmp"' not in source
    assert "if ($IncludeUnusedLocalModels)" in source
