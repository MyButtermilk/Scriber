from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_packaged_sidecar_metadata_redacts_absolute_build_paths() -> None:
    script = (
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")

    helper_start = script.index("function Test-IsRootedBuildMetadataPath")
    writer_start = script.index("function Write-SidecarBuildMetadata", helper_start)
    writer_end = script.index("function Resolve-PythonPath", writer_start)
    helper = script[helper_start:writer_start]
    writer = script[writer_start:writer_end]

    assert "[System.IO.Path]::IsPathRooted($Text)" in helper
    assert "Test-IsRootedBuildMetadataPath -Text $text" in helper
    assert "catch" in helper
    assert 'return "<redacted-absolute-path>"' in helper
    assert "System.Collections.IDictionary" in helper
    assert "System.Collections.IEnumerable" in helper
    assert "$redactedMetadata = Convert-ToPathRedactedBuildMetadataValue" in writer
    assert "$redactedMetadata | ConvertTo-Json" in writer
    assert "$metadata | ConvertTo-Json" not in writer


def test_packaged_sidecar_metadata_accepts_path_illegal_diagnostics() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")

    script = (
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    helper_start = script.index("function Test-IsRootedBuildMetadataPath")
    writer_start = script.index("function Write-SidecarBuildMetadata", helper_start)
    helper = script[helper_start:writer_start]
    probe = f"""
$ErrorActionPreference = 'Stop'
{helper}
$payload = [ordered]@{{
    relativeDiagnostic = 'plain|diagnostic'
    relativeAngleDiagnostic = 'plain<diagnostic>'
    malformedAbsolute = 'C:\\Users\\Example<bad>'
    malformedRootRelative = '\\Example|bad'
    url = 'https://example.invalid/a?b=c'
}}
Convert-ToPathRedactedBuildMetadataValue -Value $payload |
    ConvertTo-Json -Compress -Depth 10
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    redacted = json.loads(completed.stdout)
    assert redacted == {
        "relativeDiagnostic": "plain|diagnostic",
        "relativeAngleDiagnostic": "plain<diagnostic>",
        "malformedAbsolute": "<redacted-absolute-path>",
        "malformedRootRelative": "<redacted-absolute-path>",
        "url": "https://example.invalid/a?b=c",
    }


def test_packaged_sidecar_metadata_preserves_collection_shapes() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")

    script = (
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    helper_start = script.index("function Test-IsRootedBuildMetadataPath")
    writer_start = script.index("function Write-SidecarBuildMetadata", helper_start)
    helper = script[helper_start:writer_start]
    probe = f"""
$ErrorActionPreference = 'Stop'
{helper}
$payload = [ordered]@{{
    empty = @()
    single = @('one')
    multiple = @('one', 'two')
}}
Convert-ToPathRedactedBuildMetadataValue -Value $payload |
    ConvertTo-Json -Compress -Depth 10
"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "empty": [],
        "single": ["one"],
        "multiple": ["one", "two"],
    }


def test_research_builds_remove_volatile_packaged_sidecar_metadata() -> None:
    sidecar = (
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    windows_build = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$DeterministicResearchMetadata" in sidecar
    assert '"1970-01-01T00:00:00.0000000Z"' in sidecar
    assert "$metadataTotalDurationMs = [int64]0" in sidecar
    assert "$metadataPhases = @()" in sidecar
    assert '"cacheKey", "sha256", "length"' in sidecar
    assert '"-DeterministicResearchMetadata"' in windows_build
