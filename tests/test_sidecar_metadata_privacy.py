from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_packaged_sidecar_metadata_redacts_absolute_build_paths() -> None:
    script = (
        REPO_ROOT / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")

    helper_start = script.index("function Convert-ToPathRedactedBuildMetadataValue")
    writer_start = script.index("function Write-SidecarBuildMetadata", helper_start)
    writer_end = script.index("function Resolve-PythonPath", writer_start)
    helper = script[helper_start:writer_start]
    writer = script[writer_start:writer_end]

    assert "[System.IO.Path]::IsPathRooted($text)" in helper
    assert 'return "<redacted-absolute-path>"' in helper
    assert "System.Collections.IDictionary" in helper
    assert "System.Collections.IEnumerable" in helper
    assert "$redactedMetadata = Convert-ToPathRedactedBuildMetadataValue" in writer
    assert "$redactedMetadata | ConvertTo-Json" in writer
    assert "$metadata | ConvertTo-Json" not in writer


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
