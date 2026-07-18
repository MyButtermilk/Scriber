from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_windows.ps1"
TIMING_SCRIPT = REPO_ROOT / "scripts" / "measure_installer_research.ps1"
SIDECAR_SCRIPT = REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}")
    end = source.index(f"function {next_name}", start)
    return source[start:end]


def test_windows_build_accepts_an_explicit_fail_closed_python_at_the_end() -> None:
    build = _read(BUILD_SCRIPT)
    param_block = build[build.index("param(") : build.index(")\n\n$ErrorActionPreference")]

    assert '[string]$PythonExecutable = ""' in param_block
    assert param_block.rfind('[string]$PythonExecutable = ""') > param_block.rfind(
        "[switch]$RunMediaPreparationSmoke"
    )
    assert '[string]$ResearchBuildRoot = ""' in param_block
    assert param_block.rfind('[string]$ResearchBuildRoot = ""') > param_block.rfind(
        '[string]$PythonExecutable = ""'
    )
    assert '[string]$ResearchToolchainManifest = ""' in param_block
    assert param_block.rfind('[string]$ResearchToolchainManifest = ""') > param_block.rfind(
        '[string]$ResearchBuildRoot = ""'
    )
    assert "$pythonExecutableWasExplicit" in build
    assert "[System.IO.Path]::IsPathRooted($PythonExecutable)" in build
    assert 'Test-Path -LiteralPath $pythonCandidate -PathType Leaf' in build
    assert 'throw "Explicit Python executable was not found: $pythonCandidate"' in build

    # Omitting the new final positional parameter preserves the established
    # local behavior, while an explicit research interpreter cannot fall back.
    assert 'Join-Path $RepoRoot "venv\\Scripts\\python.exe"' in build
    assert "Get-Command python -ErrorAction Stop" in build


def test_windows_build_forwards_the_resolved_python_to_every_sidecar_call() -> None:
    build = _read(BUILD_SCRIPT)
    sidecar_arguments = _function(
        build, "New-SidecarBuildScriptArguments", "Write-BuildTimingReport"
    )

    assert '"scripts\\build_tauri_backend_sidecar.ps1"' in sidecar_arguments
    assert '"-PythonPath",\n        $releasePython' in sidecar_arguments
    assert build.count("New-SidecarBuildScriptArguments") >= 4
    assert "pythonExecutableExplicit = $pythonExecutableWasExplicit" in build


def test_research_sidecar_fails_closed_instead_of_installing_pyinstaller() -> None:
    build = _read(BUILD_SCRIPT)
    sidecar_arguments = _function(
        build, "New-SidecarBuildScriptArguments", "Write-BuildTimingReport"
    )
    initial_arguments = sidecar_arguments.split(
        "if (-not $resolvedResearchBuildRoot)", 1
    )[0]
    assert '"-InstallPyInstaller"' not in initial_arguments
    assert 'if (-not $resolvedResearchBuildRoot)' in sidecar_arguments
    assert '$sidecarArgs += "-InstallPyInstaller"' in sidecar_arguments
    assert '$env:PIP_NO_INDEX = "1"' in build
    assert "Remove-Item Env:PIP_NO_INDEX" in build

    sidecar = _read(SIDECAR_SCRIPT)
    guard = sidecar.index(
        "if ($DeterministicResearchMetadata -and $InstallPyInstaller)"
    )
    resolution = sidecar.index("$RepoRoot = (Resolve-Path $RepoRoot).Path")
    assert guard < resolution
    assert "network installation is forbidden" in sidecar


def test_windows_build_isolates_research_python_layers_under_one_root() -> None:
    build = _read(BUILD_SCRIPT)
    sidecar_arguments = _function(
        build, "New-SidecarBuildScriptArguments", "Write-BuildTimingReport"
    )

    assert 'throw "-ResearchBuildRoot requires an explicit -PythonExecutable."' in build
    assert 'throw "-ResearchBuildRoot requires an explicit -ResearchToolchainManifest."' in build
    assert "ResearchBuildRoot must be a strict descendant of RepoRoot" in build
    assert "ResearchBuildRoot must not be a reparse point" in build
    assert "Assert-NoResearchReparsePath" in build
    assert "Explicit Python executable must not be a reparse point" in build
    assert '"-DistRoot", (Join-Path $resolvedResearchBuildRoot "dist")' in sidecar_arguments
    assert '"-WorkRoot", (Join-Path $resolvedResearchBuildRoot "work")' in sidecar_arguments
    assert (
        '"-SidecarCacheRoot", (Join-Path $resolvedResearchBuildRoot "sidecar-cache")'
        in sidecar_arguments
    )
    assert (
        '"-RuntimeCacheRoot", (Join-Path $resolvedResearchBuildRoot "runtime-cache")'
        in sidecar_arguments
    )
    assert "researchBuildIsolated = [bool]$resolvedResearchBuildRoot" in build
    assert "Research Node version differs from .node-version" in build
    assert '$env:RUSTUP_TOOLCHAIN = [string]$toolchain.rustToolchain' in build
    assert '$env:PATH = "$researchNodeRoot$([System.IO.Path]::PathSeparator)$priorResearchPath"' in build
    assert "Assert-ResearchToolFileIdentity" in build
    assert "researchToolchainHash = $researchToolchainHash" in build
    assert "ResearchBuildRoot and ResearchToolchainManifest must belong to the same run" in build


def test_windows_build_binds_and_rechecks_the_complete_nsis_tree() -> None:
    build = _read(BUILD_SCRIPT)

    assert "Get-ResearchPlainTreeIdentity" in build
    assert "Assert-ResearchTreeIdentity" in build
    assert '$researchNsisRoot = Join-Path $env:LOCALAPPDATA "tauri\\NSIS"' in build
    assert '$toolchain.nsis.relativePath' in build
    assert '$toolchain.nsisTree' in build
    bundle = build.split('Invoke-Checked -Label "Tauri Windows bundle"', 1)[1]
    before = bundle.index('Label "Pinned NSIS tree before bundle"')
    launch = bundle.index("cmd.exe /d /s /c $tauriCommand")
    after = bundle.index('Label "Pinned NSIS tree after bundle"')
    assert before < launch < after


def test_research_build_stages_an_exact_payload_tree() -> None:
    build = _read(BUILD_SCRIPT)

    assert 'Join-Path $resolvedResearchBuildRoot "payload"' in build
    assert "scripts\\stage_installer_research_payload.py" in build
    assert "researchPayloadRoot = $researchPayloadRoot" in build


def test_installer_timing_defaults_to_twenty_counterbalanced_pairs() -> None:
    timing = _read(TIMING_SCRIPT)

    for parameter in ("RunId", "PacketId", "ParentChampionId", "SourceCommit"):
        assert f"[string]${parameter}" in timing
    assert "RunId must be a canonical non-nil RFC 4122 UUID" in timing
    assert "SourceCommit must be a full lowercase SHA-1 Git commit" in timing
    assert "runId = $RunId" in timing
    assert "packetId = $PacketId" in timing
    assert "parentChampionId = $ParentChampionId" in timing
    assert "sourceCommit = $SourceCommit" in timing
    assert "[int]$PairCount = 20" in timing
    assert "[ValidateRange(20, 100)]" in timing
    assert "[int]$WarmupPerVariant = 1" in timing
    assert "[int]$StableSamples = 3" in timing
    assert "[int]$SampleIntervalMs = 750" in timing
    assert '@("baseline", "candidate")' in timing
    assert '@("candidate", "baseline")' in timing
    assert 'if (($pair % 2) -eq 1) { "AB" } else { "BA" }' in timing
    assert "warmup = $true" in timing
    assert "warmup = $false" in timing
    assert "Where-Object { -not $_.warmup" in timing


def test_installer_timing_starts_qpc_before_create_process_and_waits_for_completion() -> None:
    timing = _read(TIMING_SCRIPT)
    measurement = _function(
        timing, "Invoke-InstallerMeasurement", "Get-TimingStatistics"
    )

    qpc_index = measurement.index("[System.Diagnostics.Stopwatch]::GetTimestamp()")
    stopwatch_index = measurement.index(
        "$watch = [System.Diagnostics.Stopwatch]::StartNew()"
    )
    process_index = measurement.index("$process = Start-Process")
    assert qpc_index < stopwatch_index < process_index

    assert "$process.WaitForExit($CompletionTimeoutSec * 1000)" in measurement
    assert "$launcherExitCode -ne 0" in measurement
    assert "Get-RelatedInstallerProcesses" in measurement
    assert "$related.Count -eq 0 -and $installedExe" in measurement
    assert "Get-ExclusiveInstalledIdentity" in measurement
    assert "$stableCount -ge $RequiredStableSamples" in measurement
    assert "launcherExitMs" in measurement
    assert "stableInstallMs" in measurement
    assert "postExitCompletionMs" in measurement


def test_installer_timing_stability_tuple_is_exclusive_and_complete() -> None:
    timing = _read(TIMING_SCRIPT)
    identity = _function(
        timing, "Get-ExclusiveInstalledIdentity", "Get-InstalledTreeInventory"
    )

    assert "[System.IO.FileShare]::None" in identity
    assert "Get-NormalizedFileVersion" in identity
    assert "Get-Sha256Hex" in identity
    assert "length = $length" in identity
    assert 'key = "$version|$length|$sha256"' in identity


def test_installer_timing_tracks_children_and_scopes_destructive_cleanup() -> None:
    timing = _read(TIMING_SCRIPT)
    process_discovery = _function(
        timing, "Get-RelatedInstallerProcesses", "Stop-ScopedProcessTree"
    )
    cleanup = _function(timing, "Invoke-CleanInstallState", "Get-ExclusiveInstalledIdentity")

    assert "$KnownProcessIds.Contains($parentProcessId)" in process_discovery
    assert "$KnownProcessIds.Contains($processId)" in process_discovery
    assert "$installerPathSet.Contains" in process_discovery
    assert "$installerNameSet" not in process_discovery
    assert ".Contains($name)" not in process_discovery
    assert "$commandLine.ToLowerInvariant().Contains($targetNeedle)" in process_discovery
    assert "$scopedUpdater" in process_discovery
    assert "$commandTargetsRoot -and" in process_discovery
    assert "Test-IsScriberUpdaterCommand" in process_discovery
    assert "Assert-SafeInstallRoot" in cleanup
    assert "Resolve-InstalledUninstaller" in cleanup
    assert "Wait-ScopedProcessTreeExit" in cleanup
    assert "Remove-TargetRegistryEntries" in cleanup
    assert "Remove-Item -LiteralPath $TargetRoot -Recurse -Force" in cleanup
    assert "InstallRoot must be a strict descendant" in timing
    assert "must not be a reparse point" in timing
    assert "Assert-NoReparsePath -Root $RepoRoot -Path $ScratchRoot" in timing
    assert "-Root $verifiedScratch -Path $TargetRoot" in timing

    # Cleanup is explicitly called before the function that starts its own
    # stopwatch. It is not part of stableInstallMs.
    main = timing[timing.index("$lockPath =") :]
    first_cleanup = main.index("Invoke-CleanInstallState")
    first_measurement = main.index("Invoke-InstallerMeasurement")
    assert first_cleanup < first_measurement
    assert "outsideTimedIntervals = $true" in timing


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


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_timing_rejects_a_reparse_ancestor_before_creating_scratch(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        pytest.skip("PowerShell is not available on this host")
    fake_repo = tmp_path / "repo"
    external = tmp_path / "external"
    fake_repo.mkdir()
    external.mkdir()
    _create_directory_reparse_point(fake_repo / "tmp", external)

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(TIMING_SCRIPT),
            "-RunId",
            "12345678-1234-4234-8234-123456789abc",
            "-PacketId",
            "packet-1",
            "-ParentChampionId",
            "baseline",
            "-SourceCommit",
            "0" * 40,
            "-BaselineInstallerPath",
            "missing-baseline.exe",
            "-CandidateInstallerPath",
            "missing-candidate.exe",
            "-RepoRoot",
            str(fake_repo),
            "-ExpectedVersion",
            "1.0.0",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "reparse point" in (completed.stdout + completed.stderr).lower()
    assert not (external / "installer-research-timing").exists()


def test_process_scope_rejects_foreign_same_name_and_foreign_updater() -> None:
    timing = _read(TIMING_SCRIPT)
    process_discovery = _function(
        timing, "Get-RelatedInstallerProcesses", "Stop-ScopedProcessTree"
    )

    def is_related(
        *,
        pid: int,
        parent_pid: int,
        executable: str,
        command: str,
        known: set[int],
        target_root: str,
        attested_installers: set[str],
    ) -> bool:
        executable_folded = executable.casefold()
        command_folded = command.casefold()
        target_folded = target_root.casefold().rstrip("\\/")
        exact_installer = executable_folded in {
            value.casefold() for value in attested_installers
        } or any(value.casefold() in command_folded for value in attested_installers)
        return (
            pid in known
            or parent_pid in known
            or executable_folded.startswith(target_folded + "\\")
            or target_folded in command_folded
            or exact_installer
        )

    target = r"C:\repo\tmp\installer-research-timing\install"
    attested = {r"C:\repo\artifacts\Scriber_0.5.38_x64-setup.exe"}
    assert not is_related(
        pid=41,
        parent_pid=7,
        executable=r"C:\other\Scriber_0.5.38_x64-setup.exe",
        command=r'"C:\other\Scriber_0.5.38_x64-setup.exe" /S',
        known={100},
        target_root=target,
        attested_installers=attested,
    )
    assert not is_related(
        pid=42,
        parent_pid=7,
        executable=r"C:\Users\Else\AppData\Local\Temp\Scriber.exe",
        command=r'"C:\Users\Else\AppData\Local\Temp\Scriber.exe" /UPDATE /ARGS other',
        known={100},
        target_root=target,
        attested_installers=attested,
    )
    assert is_related(
        pid=43,
        parent_pid=100,
        executable=r"C:\Users\Else\AppData\Local\Temp\Scriber.exe",
        command=r'"C:\Users\Else\AppData\Local\Temp\Scriber.exe" /UPDATE',
        known={100},
        target_root=target,
        attested_installers=attested,
    )
    assert "Test-IsScriberUpdaterCommand" in process_discovery
    assert "|ARGS" in timing


def test_installer_timing_uses_untrimmed_median_and_nearest_rank_p95() -> None:
    timing = _read(TIMING_SCRIPT)
    statistics = _function(timing, "Get-TimingStatistics", "Write-JsonAtomic")

    assert "Sort-Object" in statistics
    assert "[Math]::Floor($values.Count / 2)" in statistics
    assert "[Math]::Ceiling(0.95 * $values.Count)" in statistics
    assert "p95Ms = [int64]$values[$p95Rank - 1]" in statistics
    assert "Trim" not in statistics
    assert "RemoveAt" not in statistics


def test_installed_tree_inventory_runs_after_timing_and_is_consistency_gated() -> None:
    timing = _read(TIMING_SCRIPT)
    inventory = _function(
        timing, "Get-InstalledTreeInventory", "Assert-VariantInventoryConsistent"
    )

    assert "SortedDictionary[string, object]" in inventory
    assert "[System.StringComparer]::Ordinal" in inventory
    assert "Get-Sha256Hex -Path $file.FullName" in inventory
    assert "[void]$canonical.Append([char]0)" in inventory
    assert "treeSha256 = $treeSha256" in inventory
    assert "inventoryDurationMs" in inventory

    first_measurement = timing.index("$measurement = Invoke-InstallerMeasurement")
    first_inventory = timing.index(
        "$inventory = Get-InstalledTreeInventory", first_measurement
    )
    first_cleanup_after = timing.index("Invoke-CleanInstallState", first_inventory)
    assert first_measurement < first_inventory < first_cleanup_after
    assert "Assert-VariantInventoryConsistent" in timing
    assert 'throw "$Variant installed tree changed across timing samples."' in timing


def test_installer_timing_report_is_atomic_machine_readable_evidence() -> None:
    timing = _read(TIMING_SCRIPT)

    for field in (
        'apiVersion = "1"',
        'kind = "installer-ab-timing"',
        "pairCount = $PairCount",
        "variants = $variants",
        "samples = @($samples)",
        "statistics = [ordered]@{",
        "launcherExitMs",
        "stableInstallMs",
        "postExitCompletionMs",
        "installedVersion",
        "installedLength",
        "installedSha256",
        "installedFileCount",
        "installedTotalBytes",
        "installedTreeSha256",
        "inventoryDurationMs",
        "inventoryConsistency",
    ):
        assert field in timing
    assert "Write-JsonAtomic -Path $OutputPath" in timing
    assert "Move-Item -LiteralPath $temporaryPath -Destination $Path -Force" in timing
    assert "osFileCacheFlushed = $false" in timing
    assert not re.search(r"EmptyStandbyList|RAMMap|Clear-SystemFileCache", timing, re.I)


def test_installer_timing_json_redacts_personal_absolute_paths() -> None:
    timing = _read(TIMING_SCRIPT)

    assert "installerName = [System.IO.Path]::GetFileName" in timing
    assert "installerPath = $BaselineInstallerPath" not in timing
    assert "installerPath = $CandidateInstallerPath" not in timing
    assert "installRoot = Convert-ToRelativePath -Root $RepoRoot -Path $InstallRoot" in timing
    assert '$measurement["installedExe"] = Convert-ToRelativePath' in timing
    assert "Get-RedactedErrorMessage" in timing


def test_installer_timing_script_avoids_dynamic_powershell_execution() -> None:
    timing = _read(TIMING_SCRIPT)

    forbidden = (
        "Invoke-" + "Expression",
        "-Encoded" + "Command",
        "FromBase64" + "String",
    )
    for literal in forbidden:
        assert literal not in timing
    assert not re.search(
        r"\b(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b[^\r\n]{0,240}-Command\b",
        timing,
        flags=re.IGNORECASE,
    )


def test_changed_powershell_scripts_parse_without_execution() -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not available on this host")

    for relative_path in (
        "scripts\\build_windows.ps1",
        "scripts\\build_tauri_backend_sidecar.ps1",
        "scripts\\measure_installer_research.ps1",
    ):
        command = (
            "$tokens = $null; $errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path '{relative_path}'), "
            "[ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
