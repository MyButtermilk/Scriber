from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        pytest.skip("PowerShell is not available on this host")
    return executable


def _minimal_repo(root: Path) -> None:
    contract = root / "backend_runtime" / "contract.py"
    contract.parent.mkdir(parents=True)
    contract.write_text("RUNTIME_CONTRACT_REVISION = 4\n", encoding="utf-8")


def _run_sidecar(repo: Path, dist_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-RepoRoot",
            str(repo),
            "-PythonPath",
            sys.executable,
            "-DistRoot",
            str(dist_root),
        ],
        cwd=REPO_ROOT,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows sidecar build path semantics")
def test_sidecar_rejects_a_repo_prefix_sibling(tmp_path: Path) -> None:
    repo = tmp_path / "Scriber"
    escape = tmp_path / "Scriber-escape"
    repo.mkdir()
    _minimal_repo(repo)

    completed = _run_sidecar(repo, escape / "dist")

    assert completed.returncode != 0
    assert "DistRoot" in (completed.stdout + completed.stderr)
    assert not escape.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_sidecar_rejects_a_reparse_ancestor_before_writing(tmp_path: Path) -> None:
    repo = tmp_path / "Scriber"
    external = tmp_path / "external"
    repo.mkdir()
    external.mkdir()
    _minimal_repo(repo)
    _create_directory_reparse_point(repo / "build-link", external)

    completed = _run_sidecar(repo, repo / "build-link" / "dist")

    assert completed.returncode != 0
    assert "reparse point" in (completed.stdout + completed.stderr).lower()
    assert not (external / "dist").exists()


def test_sidecar_revalidates_recursive_targets_before_mutation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    boundary = source[
        source.index("function Assert-UnderRoot") : source.index(
            "function Invoke-TimedStep"
        )
    ]
    assert "$prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar" in boundary
    assert "StartsWith($rootFull," not in boundary
    assert "path must not contain a reparse point" in boundary
    assert "tree must not contain a reparse point" in boundary

    sync = source[
        source.index("function Sync-DirectoryContents") : source.index(
            "function Get-RustAudioSidecarInputManifest"
        )
    ]
    validation = sync.index("-Path $TargetDir -Label $TargetLabel -Recurse")
    creation = sync.index("New-Item -ItemType Directory")
    deletion = sync.index("Remove-Item -LiteralPath $targetFile.FullName")
    assert validation < creation < deletion

    for label in (
        "Stable media tools cleanup",
        "Rust diarization prestage cleanup",
        "Rust diarization prestage final cleanup",
        "Frozen backend runtime build path",
        "Rust diarization parallel cleanup root",
    ):
        assert f'-Label "{label}" -Recurse' in source
