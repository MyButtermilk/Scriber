from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_installer_size_packet.ps1"
SOURCE = SCRIPT.read_text(encoding="utf-8")


MANDATORY_GATES = {
    "frozenRuntimeImports",
    "mediaPreparation",
    "youtubeWorkflow",
    "liveMic",
    "meetingCapture",
    "diarization",
    "pdfDocxExport",
    "desktopFrontend",
    "cleanInstallUpgradeUninstall",
    "licenseSupplyChain",
}


def _function_source(name: str) -> str:
    match = re.search(
        rf"(?ms)^function {re.escape(name)} \{{(?P<body>.*?)(?=^function |^\$canonicalRunId\s*=)",
        SOURCE,
    )
    assert match, f"missing PowerShell function {name}"
    return match.group("body")


def test_entrypoint_has_only_the_frozen_public_arguments() -> None:
    parameter_block = SOURCE.split("$ErrorActionPreference", 1)[0]
    names = re.findall(r"\[(?:string|switch)\]\$(\w+)", parameter_block)
    assert names == ["RunId", "Mode", "RunTiming"]
    assert (
        '[ValidateSet("baseline-1", "baseline-2", "candidate", "final-1", "final-2")]'
        in parameter_block
    )
    for forbidden in (
        "BuildRoot",
        "InstallRoot",
        "PythonExecutable",
        "PairCount",
        "Compression",
        "Threshold",
    ):
        assert f"]${forbidden}" not in parameter_block


def test_invalid_run_id_is_a_parse_and_safe_failure_smoke() -> None:
    if os.name != "nt":
        pytest.skip("The installer packet producer is Windows-only.")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-RunId",
            "invalid",
            "-Mode",
            "baseline-1",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    payload = json.loads((completed.stdout + completed.stderr).strip())
    assert payload == {
        "ok": False,
        "packetProducerContract": "InstallerSizePacketProducerV1",
        "schemaVersion": 1,
        "errorCode": "invalid_run_id",
    }
    rendered = json.dumps(payload)
    assert str(ROOT) not in rendered
    assert str(Path.home()) not in rendered


def test_captured_command_uses_native_exit_code_under_windows_powershell_51(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("The installer packet producer is Windows-only.")

    function_source = "function Invoke-CapturedCommand {" + _function_source(
        "Invoke-CapturedCommand"
    )
    probe = tmp_path / "captured-command-probe.ps1"
    probe.write_text(
        "\n".join(
            (
                "param([string]$Python, [string]$LogRoot)",
                '$ErrorActionPreference = "Stop"',
                "function Get-Sha256File {",
                "    param([string]$Path)",
                "    $bytes = [System.IO.File]::ReadAllBytes($Path)",
                "    $sha = [System.Security.Cryptography.SHA256]::Create()",
                "    try {",
                "        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()",
                "    } finally {",
                "        $sha.Dispose()",
                "    }",
                "}",
                "function Get-Sha256Text {",
                "    param([string]$Value)",
                "    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)",
                "    $sha = [System.Security.Cryptography.SHA256]::Create()",
                "    try {",
                "        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()",
                "    } finally {",
                "        $sha.Dispose()",
                "    }",
                "}",
                function_source,
                "$success = Invoke-CapturedCommand -LogPath (Join-Path $LogRoot 'success.log') -Command {",
                "    & $Python -c 'import sys;sys.stderr.write(chr(120)+chr(10))'",
                "}",
                "$nativeFailure = Invoke-CapturedCommand -LogPath (Join-Path $LogRoot 'native-failure.log') -Command {",
                "    & $Python -c 'import sys;sys.exit(7)'",
                "}",
                "$global:LASTEXITCODE = 23",
                "$powershellSuccess = Invoke-CapturedCommand -LogPath (Join-Path $LogRoot 'powershell-success.log') -Command {",
                "    $null = 1 + 1",
                "}",
                "$powershellFailure = Invoke-CapturedCommand -LogPath (Join-Path $LogRoot 'powershell-failure.log') -Command {",
                "    throw 'expected-probe-failure'",
                "}",
                "[ordered]@{",
                "    success = [int]$success.exitCode",
                "    nativeFailure = [int]$nativeFailure.exitCode",
                "    powershellSuccess = [int]$powershellSuccess.exitCode",
                "    powershellFailure = [int]$powershellFailure.exitCode",
                "} | ConvertTo-Json -Compress",
                "",
            )
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
            "-Python",
            sys.executable,
            "-LogRoot",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "x" in completed.stderr
    assert json.loads(completed.stdout.strip()) == {
        "success": 0,
        "nativeFailure": 7,
        "powershellSuccess": 0,
        "powershellFailure": 2,
    }

    captured = _function_source("Invoke-CapturedCommand")
    assert "& $Command > $LogPath" in captured
    assert "& $Command *> $LogPath" not in captured


def test_registry_cleanup_tolerates_only_a_disappearing_exact_entry(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("The installer packet producer is Windows-only.")

    function_source = (
        "function Remove-ExactUninstallRegistryEntries {"
        + _function_source("Remove-ExactUninstallRegistryEntries")
    )
    probe = tmp_path / "registry-cleanup-probe.ps1"
    probe.write_text(
        "\n".join(
            (
                '$ErrorActionPreference = "Stop"',
                "$script:mode = 'disappearing'",
                "$script:lookupCount = 0",
                "function Get-ExactUninstallRegistryEntries {",
                "    param([string]$InstallRoot)",
                "    $script:lookupCount += 1",
                "    if ($script:mode -eq 'persistent' -or $script:lookupCount -eq 1) {",
                "        return [pscustomobject]@{ PSPath = 'missing-test-key' }",
                "    }",
                "    return @()",
                "}",
                "function Remove-Item {",
                "    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force, [object]$ErrorAction)",
                "    if ([string]$ErrorAction -ne 'SilentlyContinue') {",
                "        throw 'registry-removal-was-not-race-safe'",
                "    }",
                "}",
                function_source,
                "$disappearing = 'not_run'",
                "try {",
                "    Remove-ExactUninstallRegistryEntries -InstallRoot 'C:\\scoped-test-install'",
                "    $disappearing = 'pass'",
                "} catch {",
                "    $disappearing = 'fail'",
                "}",
                "$script:mode = 'persistent'",
                "$script:lookupCount = 0",
                "$persistent = 'not_run'",
                "try {",
                "    Remove-ExactUninstallRegistryEntries -InstallRoot 'C:\\scoped-test-install'",
                "    $persistent = 'unexpected_pass'",
                "} catch {",
                "    $persistent = $_.Exception.Message",
                "}",
                "[ordered]@{ disappearing = $disappearing; persistent = $persistent } | ConvertTo-Json -Compress",
                "",
            )
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == {
        "disappearing": "pass",
        "persistent": "uninstall_registry_cleanup_failed",
    }


def test_registry_lookup_skips_entries_without_install_location_in_strict_mode(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("The installer packet producer is Windows-only.")

    convert_source = "function Convert-ToFullPath {" + _function_source(
        "Convert-ToFullPath"
    )
    lookup_source = "function Get-ExactUninstallRegistryEntries {" + _function_source(
        "Get-ExactUninstallRegistryEntries"
    )
    probe = tmp_path / "registry-lookup-probe.ps1"
    probe.write_text(
        "\n".join(
            (
                '$ErrorActionPreference = "Stop"',
                "Set-StrictMode -Version Latest",
                convert_source,
                lookup_source,
                "$matches = @(Get-ExactUninstallRegistryEntries -InstallRoot 'C:\\strict-mode-nonexistent-scriber-install')",
                "[ordered]@{ ok = $true; matches = $matches.Count } | ConvertTo-Json -Compress",
                "",
            )
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == {"ok": True, "matches": 0}


def test_full_payload_build_is_explicit_hermetic_and_unsigned() -> None:
    body = _function_source("Invoke-FullInstallerBuild")
    required = {
        "-Bundles @(\"nsis\")",
        "-NsisCompression \"bzip2\"",
        "-UseProfileBFfmpeg",
        "-ValidateSlimMediaTools",
        "-PythonExecutable $Python",
        "-ResearchBuildRoot $BuildRoot",
        "-ResearchToolchainManifest $ToolchainManifest",
    }
    for fragment in required:
        assert fragment in body
    assert "EnableTauriUpdater" not in body
    assert "SignUpdaterArtifacts" not in body
    assert "Authenticode" not in body
    assert "FastLocalInstaller" not in body
    assert "TAURI_SIGNING_PRIVATE_KEY" in SOURCE
    assert 'Remove-Item -LiteralPath "Env:$name"' in SOURCE


def test_compression_repack_never_mutates_the_shared_release_payload() -> None:
    body = _function_source("Invoke-CompressionRepack")
    assert 'Join-Path $BuildRoot "cargo-target"' in body
    assert "$env:CARGO_TARGET_DIR = $isolatedTarget" in body
    assert "$repackConfig.bundle.resources" in body
    assert "$isolatedBackendSource" in body
    assert "--remove-before-bundle-command" in body
    assert "--skip-updater-config" in body
    assert "canonical-backup" not in body
    assert "Move-Item" not in body
    assert "Frontend\\src-tauri\\target\\release" not in body
    before = body.index(
        'Assert-NsisTreeIdentity -Manifest $Toolchain.manifest -Code "repack_nsis_tree_drift"'
    )
    launch = body.index("& $node $tauri bundle")
    after = body.index(
        'Assert-NsisTreeIdentity -Manifest $Toolchain.manifest -Code "repack_nsis_tree_drift"',
        launch,
    )
    assert before < launch < after


def test_compression_lane_is_bound_to_the_parent_semantic_payload() -> None:
    assert '$comparisonKind -notin @("payload", "compression")' in SOURCE
    assert '$compression -notin @("bzip2", "zlib", "lzma")' in SOURCE
    assert 'throw "payload_candidate_must_use_bzip2"' in SOURCE
    assert '[string]$action.payloadTreeSha256 -ne [string]$parentInventory.payload.staged.semanticTreeSha256' in SOURCE
    assert "Assert-PayloadMatchesInventory -PayloadRoot $sourcePayload" in SOURCE
    assert "Assert-PayloadMatchesInventory -PayloadRoot $payloadRoot" in SOURCE


def test_reused_python_environment_is_fully_reattested() -> None:
    body = _function_source("Ensure-HermeticEnvironment")
    assert 'Join-Path $RunRoot "snapshots"' in body
    assert 'Join-Path $RunRoot "wheelhouse"' in body
    assert "write_installer_research_environment_manifest.py" in body
    assert "--verify $manifestPath" in body
    assert 'throw "environment_manifest_drift"' in body
    assert "-m pip check" in body
    assert '[switch]$VerifyOnly' in body
    assert 'throw "environment_missing_after_build"' in body


def test_build_output_is_accepted_only_after_environment_and_toolchain_recheck() -> None:
    build = SOURCE.index("$buildCommandEvidenceSha = Invoke-FullInstallerBuild")
    reattest = SOURCE.index("$postBuildEnvironment = Ensure-HermeticEnvironment")
    archive = SOURCE.index("$archivedInstaller = Join-Path $artifactRoot")
    assert build < reattest < archive
    reattest_block = SOURCE[reattest:archive]
    assert "-VerifyOnly" in reattest_block
    assert "$postBuildEnvironment.productDependenciesSha256" in reattest_block
    assert "$postBuildEnvironment.manifestSha256" in reattest_block
    assert 'throw "environment_identity_changed_after_build"' in reattest_block
    assert "$postBuildToolchain = Assert-ToolchainManifest" in reattest_block
    assert 'throw "toolchain_identity_changed_after_build"' in reattest_block


def test_toolchain_rehashes_complete_frontend_and_rust_tools() -> None:
    body = _function_source("Assert-ToolchainManifest")
    assert "frontendNodeModules" in body
    assert "Get-PlainTreeIdentity" in body
    assert "nativeTauriCli" in body
    assert "cli.win32-x64-msvc.node" in body
    assert "frontendPackageLock" in body
    assert "Assert-NsisTreeIdentity" in body
    nsis = _function_source("Assert-NsisTreeIdentity")
    assert '$Manifest.nsis.relativePath' in nsis
    assert '$Manifest.nsisTree' in nsis
    assert "Get-PlainTreeIdentity -Root $nsisRoot" in nsis
    assert "fileCount" in nsis
    assert "totalBytes" in nsis
    assert "treeSha256" in nsis
    assert "-Recurse" in nsis
    assert '$manifest.rustfmt' in body
    assert '$manifest.clippyDriver' in body
    assert 'executable = "rustfmt"' in body
    assert 'executable = "clippy-driver"' in body


def test_final_replica_uses_immutable_git_tree_binding_not_commit_equality() -> None:
    assert "$action.championSourceTreeOid" in SOURCE
    assert 'rev-parse "$sourceCommit^{tree}"' in SOURCE
    assert 'rev-parse "$championSourceCommit^{tree}"' in SOURCE
    assert "$currentTreeOid -ne $championTreeOid" in SOURCE
    assert "$championCommitTreeOid -ne $championTreeOid" in SOURCE
    assert '[string]$champion.sourceCommit -ne $sourceCommit' not in SOURCE
    assert "$parentChampionId = [string]$champion.packetId" in SOURCE


def test_every_mandatory_gate_has_retained_bounded_evidence() -> None:
    artifact = _function_source("Write-GateArtifact")
    assert "InstallerResearchGateArtifactV1" in artifact
    assert 'Join-Path $EvidenceRoot "gates\\$Gate.json"' in artifact
    assert "65536" in artifact
    assert "Get-Sha256File -Path $path" in artifact
    gate_block = SOURCE.split("$gateDefinitions = [ordered]@{", 1)[1].split(
        "$gates = [ordered]@{}", 1
    )[0]
    assert MANDATORY_GATES == set(
        re.findall(r"(?m)^\s{8}([A-Za-z][A-Za-z0-9]+)\s*=", gate_block)
    )
    assert "Write-GateArtifact" in SOURCE
    assert "-EvidenceSha256 $artifactSha" in SOURCE
    assert '$definition.Contains("detailEvidence")' in SOURCE
    assert "-DetailEvidence $definition.detailEvidence" not in SOURCE
    assert "$runtimeGateCommand.evidenceSha256" not in gate_block
    assert "Get-Sha256File -Path $installedSmokePath" not in gate_block
    retained = _function_source("Test-RetainedGateArtifact")
    assert "ExpectedParentChampionId" in retained
    assert "ExpectedSourceCommit" in retained
    assert "ExpectedSha256" in retained
    assert "gateArtifactContract" in retained
    assert "gate_artifact_status_check_mismatch" in artifact
    assert 'throw "mandatory_gate_set_drift"' in SOURCE
    assert 'throw "retained_gate_artifact_binding_failed"' in SOURCE


def test_candidate_and_final_upgrade_is_baseline_to_candidate() -> None:
    body = _function_source("Invoke-BaselineToCandidateUpgradeGate")
    assert "-InstallerPath $BaselineInstaller" in body
    assert "installer-research-upgrade-sentinel.txt" in body
    assert "Start-Process -FilePath $CandidateInstaller" in body
    assert "Assert-InstalledPayloadMatchesInventory" in body
    assert "smoke_tauri_desktop.ps1" in body
    assert "-RequireUninstaller" in body
    assert "Get-ExactInstalledProcesses" in body
    assert "Get-ExactUninstallRegistryEntries" in body
    assert SOURCE.count("-SimulateUpgrade") == 1
    assert "baseline-self-upgrade-uninstall-smoke.json" in SOURCE


def test_final_modes_require_all_external_gates() -> None:
    assert (
        '$expectedKind -in @("baseline-replica", "final-replica") -and '
        "-not $allExternalGatesPassed"
    ) in SOURCE
    assert 'throw "mandatory_functional_gate_failed"' in SOURCE


def test_every_candidate_and_final_runs_the_installed_paired_validator() -> None:
    evidence = _function_source("Test-CandidateHoldoutEvidence")
    assert "InstallerSizeYoutubeCandidateHoldoutsV1" in evidence
    assert 'ExpectedPacketId' in evidence
    assert 'ExpectedParentChampionId' in evidence
    assert 'ExpectedSourceCommit' in evidence
    assert 'pairedSampleCount -ne 24' in evidence
    assert 'workspaceCount -ne [int]$payload.executionPolicy.cleanupCount' in evidence
    assert 'Test-HoldoutInventoryBinding' in evidence

    assert 'validate_installer_youtube_candidate_holdouts.py' in SOURCE
    assert '--candidate-root $installRoot' in SOURCE
    assert '--baseline-root (Join-Path $runRoot "payloads\\baseline-1")' in SOURCE
    assert '--output $candidateHoldoutPath' in SOURCE
    assert '-ExpectedPacketId $packetId' in SOURCE
    assert '-ExpectedParentChampionId $parentChampionId' in SOURCE
    assert '$baselineEnvironment.python' in SOURCE
    assert 'packet-evidence\\$($champion.packetId)\\youtube-holdouts-candidate.json' not in SOURCE
    assert '$parentCandidateEvidence' not in SOURCE
    assert '$requiresCurrentHoldout = $expectedKind -in @("measure-candidate", "final-replica")' in SOURCE
    assert 'if (-not $requiresCurrentHoldout)' in SOURCE
    assert '$jsRuntimeChanged' not in SOURCE
    assert "currentJs" not in SOURCE
    assert "currentYtdlp" not in SOURCE
    assert 'baseline-prechange-stack' in SOURCE


def test_preexisting_candidate_holdout_is_never_accepted() -> None:
    assert '$preexisting.Count -ne 0' in SOURCE
    assert '$preexisting.Count -ne 1' not in SOURCE


def test_final_one_runs_the_exact_retained_full_suite_contract() -> None:
    body = _function_source("Invoke-FinalFullSuite")
    assert "InstallerResearchFullSuiteEvidenceV1" in body
    assert 'Join-Path $EvidenceRoot "full-suite-evidence.json"' in body
    assert "Write-FullSuiteGateArtifact" in body
    retained = _function_source("Write-FullSuiteGateArtifact")
    assert "InstallerResearchFullSuiteGateArtifactV1" in retained
    assert 'Join-Path $EvidenceRoot "full-suite\\$Gate.json"' in retained
    assert "& $Python -m pytest -q" in body
    assert "& $node $npm run check" in body
    assert '& $node $npm run "test:i18n"' in body
    assert "& $node $npm run build" in body
    assert "& $cargo test --locked" in body
    assert "& $cargo fmt --check" in body
    assert "& $cargo clippy --locked --all-targets --all-features -- -D warnings" in body
    assert "$Mode -eq \"final-1\"" in SOURCE


def test_timing_is_fixed_counterbalanced_and_final_two_is_mandatory() -> None:
    assert '$Mode -eq "final-2" -and -not $RunTiming' in SOURCE
    assert '-PairCount 20' in SOURCE
    assert '-WarmupPerVariant 1' in SOURCE
    assert '-RunId $RunId' in SOURCE
    assert '-PacketId $packetId' in SOURCE
    assert '-ParentChampionId $timingParentId' in SOURCE
    timing_call = SOURCE.split('"scripts\\measure_installer_research.ps1"', 1)[1].split(
        'if ($timingCommand.exitCode', 1
    )[0]
    assert timing_call.count('-OutputPath $timingPath') == 1
    assert timing_call.count('-SourceCommit $sourceCommit') == 1
    assert '$timingParentId = "baseline"' in SOURCE


def test_paths_and_cleanup_are_strictly_scoped() -> None:
    assert '"autoresearch-results\\installer-size"' in SOURCE
    assert 'Join-Path $ResearchNamespace $RunId' in SOURCE
    assert "Assert-StrictDescendant" in SOURCE
    assert "Assert-NoReparsePath" in SOURCE
    assert "Remove-ScopedTree" in SOURCE
    assert "Get-ExactInstalledProcesses" in SOURCE
    assert "Get-ExactUninstallRegistryEntries" in SOURCE
    assert (
        '$smokeNamespace = Assert-NoReparsePath -Root $RepoRoot '
        '-Path $InstallerSmokeNamespace -Code "unsafe_smoke_namespace"'
    ) in SOURCE
    cleanup = _function_source("Remove-ScopedTree")
    assert "Assert-NoReparsePath -Root $RepoRoot -Path $Root" in cleanup
    uninstaller = _function_source("Invoke-ExactUninstaller")
    assert "Assert-NoReparsePath -Root $RepoRoot -Path $InstallerSmokeNamespace" in uninstaller
    assert '-Root (Join-Path $RepoRoot "tmp\\installer-smoke")' not in SOURCE
    for forbidden in ("Invoke-Expression", "EncodedCommand", "Start-Job", "Remove-Item $HOME"):
        assert forbidden not in SOURCE


def test_build_evidence_is_retained_and_path_redacted_by_shape() -> None:
    assert "InstallerResearchBuildEvidenceV1" in SOURCE
    assert 'Join-Path $evidenceRoot "build-evidence.json"' in SOURCE
    block = SOURCE.split("$buildEvidence = [ordered]@{", 1)[1].split(
        "Write-JsonAtomic -Path $buildEvidencePath", 1
    )[0]
    assert "installerPath" not in block
    assert "buildRoot" not in block
    assert "treeSha256" in block
    assert "toolchain" in block
