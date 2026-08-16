from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_python314_upgrade_acceptance.ps1"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_gate_binds_exact_public_v0547_installer_identity() -> None:
    source = _source()

    for expected in (
        'BaselineRepository = "MyButtermilk/Scriber"',
        "BaselineReleaseId = 360523309",
        'BaselineTag = "v0.5.47"',
        'BaselineCommit = "aa918d3d111dab834e05e5b588c24da503f9176d"',
        "BaselineAssetId = 491618831",
        'BaselineInstallerName = "Scriber_0.5.47_x64-setup.exe"',
        "BaselineInstallerLength = [int64]79787381",
        'BaselineInstallerSha256 = "a4c242935e2eb26d6ab2517a38538b4942f204477cb745ef6c7909312efdd8f5"',
    ):
        assert expected in source

    assert "validate_python314_upgrade_manifest.py" in source
    assert "--installer $BaselineInstallerPath" in source


def test_gate_requires_candidate_and_source_tree_binding() -> None:
    source = _source()

    for parameter in (
        "BaselineInstallerPath",
        "CandidateInstallerPath",
        "CandidateRuntimeAttestationPath",
        "PythonExecutable",
        "SourceCommit",
        "SourceTreeSha256",
    ):
        position = source.index(f"${parameter}")
        assert "[Parameter(Mandatory = $true)]" in source[max(0, position - 180) : position]

    assert "scripts\\perf\\runtime_attestation.py" in source
    assert "git-worktree-sha256-v1" in source
    assert "source.contentSha256 -ceq $SourceTreeSha256" in source
    assert "source.head -ceq $SourceCommit" in source


def test_gate_has_no_dynamic_or_encoded_powershell_execution() -> None:
    source = _source()
    lowered = source.casefold()

    forbidden = (
        "invoke-expression",
        "scriptblock]::create",
        "scriptblock::create",
        "-encodedcommand",
        "-enc ",
        "frombase64string",
        "powershell.exe -command",
        "pwsh.exe -command",
    )
    for marker in forbidden:
        assert marker not in lowered
    assert not re.search(r"(?im)(?:^|[;&|]\s*)iex(?:\s|$)", source)
    assert "SimulateUpgrade" not in source


def test_gate_performs_real_two_installer_clean_upgrade_parity() -> None:
    source = _source()
    baseline_smoke = source.index("-InstallerPath $BaselineInstallerPath")
    sentinel = source.index("python314-upgrade-user-data-sentinel-v1")
    candidate_overlay = source.index("-FilePath $CandidateInstallerPath", sentinel)

    assert baseline_smoke < sentinel < candidate_overlay
    assert 'ArgumentList @("/S", "/D=$upgradeInstallRoot")' in source
    assert "Wait-CandidateUpgradeInstallStable" in source
    assert "$stableMatches -ge 3" in source
    assert "Compare-InstalledTreeInventory" in source
    assert "[string]$left.path -cne [string]$right.path" in source
    assert "[int64]$left.length -ne [int64]$right.length" in source
    assert "[string]$left.sha256 -cne [string]$right.sha256" in source


def test_gate_enforces_official_o0_and_zero_cp313_payload() -> None:
    source = _source()

    assert '[string]$policy.python.version -cne "3.14.7"' in source
    assert 'StartsWith("3.14.7"' not in source
    for marker in (
        '"3.14.7"',
        '"cpython-314"',
        '"Official"',
        '"O0"',
        "tailCallInterpreter",
        "jit.expected",
        "jit.active",
        '"python313.dll"',
        r'"\.cp313-win_amd64\.pyd$"',
        "python314.dll",
    ):
        assert marker in source


def test_gate_preserves_user_data_and_strictly_checks_uninstall_cleanup() -> None:
    source = _source()

    assert "Invoke-StrictUninstall" in source
    assert "installRootRemoved" in source
    assert "Get-ScopedScriberProcesses" in source
    assert "Get-ScopedUninstallEntries" in source
    assert "dataSentinelPreserved" in source
    assert "strictUninstallAndDataPreservation" in source
    assert "Remove-Item -LiteralPath $upgradeDataRoot" not in source
    assert "Remove-Item -Recurse" not in source


def test_gate_writes_one_atomic_bounded_evidence_contract() -> None:
    source = _source()

    assert "[AllowEmptyCollection()]" in source
    assert 'Contract = "ScriberPython314UpgradeAcceptanceV1"' in source
    assert "Write-AtomicJson" in source
    assert "Move-Item -LiteralPath $temporary -Destination $Path -Force" in source
    assert "clean = $null" in source
    assert "upgrade = $null" in source
    assert "inventory = $cleanInventory" in source
    assert "inventory = $upgradeInventory" in source
    assert "output_path_overlaps_install_or_data" in source
    assert "Test-StrictDescendantPath -Path $WorkRoot -Parent $allowedScratchRoot" in source
