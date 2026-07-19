from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_installer_size_packet.ps1"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    match = re.search(
        rf"(?ms)^function {re.escape(name)} \{{.*?(?=^function |\Z)",
        SOURCE,
    )
    assert match is not None, f"missing PowerShell function: {name}"
    return match.group(0)


def test_upgrade_uses_one_deadline_from_launcher_through_exact_inventory() -> None:
    body = _function_source("Invoke-BaselineToCandidateUpgradeGate")

    deadline = body.index("$upgradeDeadline = [DateTimeOffset]::UtcNow.AddSeconds(60)")
    start = body.index("$process = Start-Process")
    pid = body.index("$knownInstallerProcessIds.Add([int]$process.Id)")
    handle = body.index("$null = $process.Handle")
    wait = body.index("$process.WaitForExit($remainingLauncherMs)")
    exit_code = body.index("$candidateInstallerExitCode = [int]$process.ExitCode")
    barrier = body.index("Wait-CandidateUpgradeInstallStable")
    inventory = body.index("Assert-InstalledPayloadMatchesInventory", barrier)

    assert deadline < start < pid < handle < wait < exit_code < barrier < inventory
    assert "-PassThru" in body[start:pid]
    assert "-Wait" not in body[start:pid]
    assert body.count("[DateTimeOffset]::UtcNow.AddSeconds(60)") == 1
    assert "Get-RemainingDeadlineMilliseconds -Deadline $upgradeDeadline" in body
    assert "-InstallerPaths $installerPaths" in body
    assert "-Deadline $upgradeDeadline" in body
    assert "-RequiredStableSamples 3" in body
    assert "-ObservationIntervalMs 250" in body
    assert "-CompletionTimeoutSec" not in body
    assert "-PassThru -Wait" not in SOURCE


def test_completion_barrier_matches_timing_harness_safety_model() -> None:
    related = _function_source("Get-RelatedInstallerProcesses")
    exclusive = _function_source("Get-ExclusiveInstalledIdentity")
    barrier = _function_source("Wait-CandidateUpgradeInstallStable")

    assert "KnownProcessIds.Contains($parentProcessId)" in related
    assert "Test-IsScriberUpdaterCommand" in related
    assert "$commandLine.ToLowerInvariant().Contains($targetNeedle)" in related
    assert "$installerPathSet.Contains" in related
    assert "[System.IO.FileShare]::None" in exclusive
    assert "$algorithm.ComputeHash($stream)" in exclusive
    assert exclusive.index("$algorithm.ComputeHash($stream)") < exclusive.index(
        "$stream.Dispose()"
    )
    assert "Get-Sha256File -Path $Path" not in exclusive
    assert "[Parameter(Mandatory = $true)][DateTimeOffset]$Deadline" in barrier
    assert "AddSeconds" not in barrier
    assert "Get-RelatedInstallerProcesses" in barrier
    assert "Get-ExclusiveInstalledIdentity" in barrier
    assert "Get-CandidateInstalledAppIdentity" in barrier
    assert "[int64]$identity.length -eq [int64]$expectedIdentity.length" in barrier
    assert "[string]$identity.sha256 -eq [string]$expectedIdentity.sha256" in barrier
    assert "$stableCount -ge $RequiredStableSamples" in barrier
    assert "Get-RemainingDeadlineMilliseconds -Deadline $Deadline" in barrier
    assert "[Math]::Min($ObservationIntervalMs, $remainingMs)" in barrier
    assert 'throw "candidate_upgrade_completion_timeout"' in barrier


def test_exclusive_identity_hashes_a_real_file_under_its_open_stream(tmp_path: Path) -> None:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    sample = tmp_path / "identity.bin"
    content = (b"candidate-tree-identity\0" * 257) + b"end"
    sample.write_bytes(content)
    safe_path = str(sample).replace("'", "''")
    command = (
        _function_source("Get-ExclusiveInstalledIdentity")
        + f"\n$result = Get-ExclusiveInstalledIdentity -Path '{safe_path}'"
        + "\n$result | ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    identity = json.loads(completed.stdout.strip())

    assert identity["length"] == len(content)
    assert identity["sha256"] == hashlib.sha256(content).hexdigest()
    assert identity["key"] == f"{len(content)}|{hashlib.sha256(content).hexdigest()}"


def test_same_desktop_identity_cannot_bypass_full_candidate_tree_barrier() -> None:
    barrier = _function_source("Wait-CandidateUpgradeInstallStable")

    app_identity_match = barrier.index(
        "[string]$identity.sha256 -eq [string]$expectedIdentity.sha256"
    )
    full_tree_check = barrier.index("Assert-InstalledPayloadMatchesInventory")
    stable_sample = barrier.index("$stableCount += 1")
    success = barrier.index("return", stable_sample)

    assert app_identity_match < full_tree_check < stable_sample < success
    assert "-InstallRoot $InstallRoot" in barrier[full_tree_check:stable_sample]
    assert "-Inventory $CandidateInventory" in barrier[full_tree_check:stable_sample]


def test_failure_cleanup_is_scoped_bounded_and_precedes_tree_removal() -> None:
    stop = _function_source("Stop-ScopedProcessTree")
    wait = _function_source("Wait-ScopedProcessTreeExit")
    cleanup = _function_source("Invoke-ScopedInstallerProcessCleanup")
    gate = _function_source("Invoke-BaselineToCandidateUpgradeGate")
    uninstaller = _function_source("Invoke-ExactUninstaller")
    reasons = _function_source("Get-RedactedUpgradeGateReasonCode")

    assert "Get-RelatedInstallerProcesses" in stop
    assert "Stop-Process" in stop and "-Force" in stop
    assert "while ([DateTimeOffset]::UtcNow -lt $Deadline)" in wait
    assert "Get-RemainingDeadlineMilliseconds -Deadline $Deadline" in wait
    assert 'throw "upgrade_scoped_process_cleanup_timeout"' in wait
    assert cleanup.index("Stop-ScopedProcessTree") < cleanup.index(
        "Wait-ScopedProcessTreeExit"
    )
    assert "-TimeoutSec 10" in gate
    catch_cleanup = gate.index("Invoke-ScopedInstallerProcessCleanup", gate.index("} catch {"))
    diff = gate.index("Get-InstalledPayloadFirstDifference")
    assert catch_cleanup < diff
    final_cleanup = gate.index("Invoke-ScopedInstallerProcessCleanup", diff)
    final_uninstall = gate.index("Invoke-ExactUninstaller", final_cleanup)
    tree_removal = gate.index("Remove-ScopedTree", final_uninstall)
    assert final_cleanup < final_uninstall < tree_removal
    assert 'throw "upgrade_gate_cleanup_failed"' not in gate
    assert '"gate-cleanup" = "not_run"' in gate
    assert '$outcomes["gate-cleanup"] = "fail"' in gate
    assert '"upgrade_gate_cleanup_failed"' in reasons
    assert gate.index('$outcomes["gate-cleanup"] = "fail"') < gate.index(
        "Write-UpgradeGateDetailArtifact"
    )
    assert "WaitForExit($remainingMs)" in uninstaller
    assert "-Wait" not in uninstaller


def test_unexpected_reparse_difference_is_never_hashed() -> None:
    difference = _function_source("Get-InstalledPayloadFirstDifference")
    unexpected_start = difference.index("if (-not $hasExpected)")
    missing_start = difference.index("if (-not $hasActual)", unexpected_start)
    unexpected = difference[unexpected_start:missing_start]

    reparse = unexpected.index("[System.IO.FileAttributes]::ReparsePoint")
    guarded_hash = unexpected.index(
        '$actualSha256 = if ($actualIsReparse) { "" } else { Get-Sha256File'
    )
    evidence = unexpected.index("New-RedactedInstalledPayloadDifference")
    assert reparse < guarded_hash < evidence
    assert '$actualLength = if ($actualIsReparse) { [int64]-1 }' in unexpected


def test_failed_upgrade_retains_only_bounded_bound_first_difference() -> None:
    gate = _function_source("Invoke-BaselineToCandidateUpgradeGate")
    difference = _function_source("Get-InstalledPayloadFirstDifference")
    redaction = _function_source("New-RedactedInstalledPayloadDifference")
    writer = _function_source("Write-UpgradeGateDetailArtifact")
    validator = _function_source("Test-RetainedUpgradeGateDetail")

    assert "Get-RedactedUpgradeGateReasonCode" in gate
    assert "Get-InstalledPayloadFirstDifference" in gate
    assert "Write-UpgradeGateDetailArtifact" in gate
    assert 'if ($status -eq "fail")' in gate
    assert "firstDifference" in difference
    assert "SortedSet[string]" in difference
    assert '$safeRelativePath = "redacted"' in redaction
    assert "InstallerResearchUpgradeGateDetailV1" in writer
    for binding in ("runId", "packetId", "parentChampionId", "sourceCommit"):
        assert binding in writer
        assert binding in validator
    assert "65536" in writer
    assert "file://" in writer
    assert "same-version-upgrade-tree" in writer
    assert "installerExitCode" in writer
    assert "installerExitCode" in validator
    assert "Get-Sha256File -Path $path" in writer
    assert "InstallerResearchUpgradeGateDetailV1" in validator
    assert "Get-Sha256File -Path $detailPath" in validator
    assert "firstDifference" in validator
    assert ".Exception.ToString()" not in writer
    assert ".ScriptStackTrace" not in writer


def test_candidate_upgrade_gate_binds_detail_evidence_into_retained_gate() -> None:
    retained = _function_source("Test-RetainedGateArtifact")
    gate_definitions = SOURCE.split("$gateDefinitions = [ordered]@{", 1)[1].split(
        "$gates = [ordered]@{}", 1
    )[0]

    assert 'cleanInstallUpgradeUninstall = [ordered]@{' in gate_definitions
    assert '$upgradeGate.Contains("detailEvidence")' in gate_definitions
    assert 'ExpectedGate -eq "cleanInstallUpgradeUninstall"' in retained
    assert "Test-RetainedUpgradeGateDetail" in retained
    assert '$ExpectedStatus -ne "pass" -and $ExpectedPacketId -ne "baseline-1"' in retained
