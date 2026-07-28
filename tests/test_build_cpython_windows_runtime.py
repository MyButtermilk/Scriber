from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "packaging" / "cpython-windows-runtime-input-lock-v1.json"
SCRIPT_PATH = ROOT / "scripts" / "build_cpython_windows_runtime.ps1"


def test_runtime_lock_binds_complete_matrix_and_exact_inputs() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["lockContract"] == "ScriberCPythonWindowsRuntimeInputLockV1"
    assert lock["target"] == {
        "pythonVersion": "3.14.6",
        "cacheTag": "cpython-314",
        "architecture": "win_amd64",
        "cpythonTag": "v3.14.6",
    }
    assert set(lock["buildMatrix"]) == {"A13", "O0", "O1", "C0", "C1", "T0", "T1", "K0"}
    assert lock["customBuildInputs"]["llvm"]["version"] == "19.1.7"
    assert lock["customBuildInputs"]["llvm"]["sizeBytes"] == 845236708
    assert re.fullmatch(r"[0-9a-f]{64}", lock["customBuildInputs"]["llvm"]["sha256"])
    visual_studio = lock["customBuildInputs"]["visualStudio"]
    assert visual_studio["installationVersion"] == "17.14.37516.0"
    assert visual_studio["bootstrapper"] == {
        "fileName": "vs_BuildTools-17.14.37.exe",
        "url": (
            "https://download.visualstudio.microsoft.com/download/pr/"
            "f7f5ecbc-83ca-4cf0-bdb2-aaf70efb6d97/"
            "e0b8ea16494b4a79c68da26773131562aefecc8d87f1923c24d579c7a72e0575/"
            "vs_BuildTools.exe"
        ),
        "sizeBytes": 4459960,
        "sha256": "e0b8ea16494b4a79c68da26773131562aefecc8d87f1923c24d579c7a72e0575",
        "signerSubject": "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US",
    }
    assert visual_studio["components"] == [
        "Microsoft.VisualStudio.Workload.VCTools",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "Microsoft.VisualStudio.Component.Windows11SDK.26100",
        "Microsoft.VisualStudio.Component.VC.Llvm.ClangToolset",
    ]
    assert lock["customBuildInputs"]["windowsSdk"]["directoryVersion"] == "10.0.26100.0"
    assert len(lock["customBuildInputs"]["hostPython"]["files"]) == 3
    assert lock["customBuildInputs"]["externals"]
    for external in lock["customBuildInputs"]["externals"]:
        assert external["fileCount"] > 0
        assert external["sizeBytes"] > 0
        assert re.fullmatch(r"[0-9a-f]{64}", external["treeSha256"])


def test_clang_runtime_commands_preserve_upstream_pgo_tail_and_jit_contract() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    clang = lock["customBuildCommands"]
    assert clang["C"][:7] == [
        r"PCbuild\build.bat",
        "-p",
        "x64",
        "--pgo",
        "--pgo-job",
        "-m test --pgo",
        "--experimental-jit-off",
    ]
    assert "--tail-call-interp" not in clang["C"]
    assert "--tail-call-interp" in clang["T"]
    assert "--pgo" not in clang["K"]
    assert "--experimental-jit-off" not in clang["K"]
    for arguments in clang.values():
        assert "-E" not in arguments
        assert "/p:PlatformToolset=ClangCL" in arguments
        assert "/p:LLVMToolsVersion=19.1.7" in arguments


def test_builder_is_offline_fail_closed_and_reuses_one_binary_family_per_jit_pair() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '$env:HTTP_PROXY = "http://127.0.0.1:9"' in source
    assert '$env:HTTPS_PROXY = "http://127.0.0.1:9"' in source
    assert '$env:PIP_NO_INDEX = "1"' in source
    assert '$env:IncludeLLVM = "false"' in source
    assert "get_externals.bat" not in source
    assert 'if ($lockedArguments -contains "-E")' in source
    assert "The offline CPython build attempted to fetch an input." in source
    assert '"PCbuild\\msbuild.rsp"' in source
    assert "[IO.File]::WriteAllLines" in source
    assert "msbuildResponseProperties = $msbuildProperties" in source
    assert '$gitIsolationShim = Join-Path $temporaryRoot "git-metadata-disabled.cmd"' in source
    assert '$effectiveBuildArguments += "/p:GIT=$gitIsolationShim"' in source
    assert "$env:GIT = $gitIsolationShim" in source
    assert '"IncludeLLVM", "GIT"' in source
    assert '"exit /b 1"' in source
    assert "Custom CPython identity probe did not emit valid JSON." in source
    assert "Custom CPython build identity is not the isolated v3.14.6 archive identity." in source
    assert '[string]$buildGitIdentity[0] -cne "CPython"' in source
    assert '[string]$buildGitIdentity[1] -cne ""' in source
    assert '[string]$buildGitIdentity[2] -cne ""' in source
    assert "gitMetadataIsolation = [ordered]@{" in source
    assert "buildIdentity = $buildIdentity" in source
    assert '$layoutScript = Join-Path $sourceRoot "PC\\layout"' in source
    assert '"--include-pip"' in source
    assert '"--include-venv"' in source
    assert '"--flat-dlls"' in source
    assert '"Lib\\encodings\\__init__.py"' in source
    assert "Copy-Item -LiteralPath $builtRoot -Destination $temporaryFamily -Recurse" not in source
    assert '"families\\" + [string]$variantLock.family' in source
    assert 'pythonJitEnvironment = if ($jitExpected) { "1" } else { "0" }' in source
    assert "runtimeLockSha256" in source
    assert "builderSha256" in source
    assert "Get-TreeIdentity" in source
    assert 'Join-Path $OutputRoot "failures"' in source
    assert 'Join-Path $failureRoot "$Variant-build.log"' in source
    assert "See retained log" in source


def test_toolchain_provisioner_is_exact_and_fail_closed() -> None:
    source = (ROOT / "scripts" / "provision_cpython_windows_toolchain.ps1").read_text(encoding="utf-8")
    assert "Save-LockedInput" in source
    assert "Get-AuthenticodeSignature" in source
    assert "SignerCertificate.Subject" in source
    assert '$arguments += "-requires"' in source
    assert "$arguments += @($VisualStudioLock.components)" in source
    assert (
        "Get-VisualStudioInstance -VsWhere $vswhere -VisualStudioLock $visualStudioLock -RequireComponents $false"
        in source
    )
    assert '"Microsoft Visual Studio\\Installer\\setup.exe"' in source
    assert '"--norestart"' in source
    assert '"--nocache"' in source
    assert "Windows Kits\\10\\bin\\$sdkVersion\\x64\\rc.exe" in source
