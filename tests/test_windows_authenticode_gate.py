from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHENTICODE_SCRIPT = REPO_ROOT / "scripts" / "validate_windows_authenticode.ps1"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_windows.ps1"
RELEASE_CACHE_KEY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1"
RESTORE_RELEASE_CACHE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "restore_release_cache_artifact.ps1"
RESTORE_COMPONENT_CACHES_SCRIPT = REPO_ROOT / "scripts" / "ci" / "restore_component_cache_artifacts_parallel.ps1"
RESTORE_FFMPEG_PROFILE_SCRIPT = REPO_ROOT / "scripts" / "ffmpeg" / "restore_profile_b_release_artifact.ps1"
PUBLISH_FFMPEG_PROFILE_SCRIPT = REPO_ROOT / "scripts" / "ffmpeg" / "publish_profile_b_release_artifact.ps1"
PUBLISH_RELEASE_CACHE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "publish_release_cache_artifact.ps1"
PRUNE_RELEASE_CACHES_SCRIPT = REPO_ROOT / "scripts" / "ci" / "prune_obsolete_release_caches.ps1"
TAG_RELEASE_PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate_tag_release_preflight.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"


def powershell_exe() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is required for Windows release signing gates.")
    return exe


def run_powershell(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script_env = os.environ.copy()
    if env:
        script_env.update(env)
    return subprocess.run(
        [powershell_exe(), *args],
        cwd=REPO_ROOT,
        env=script_env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "script",
    [
        AUTHENTICODE_SCRIPT,
        BUILD_SCRIPT,
        RELEASE_CACHE_KEY_SCRIPT,
        RESTORE_RELEASE_CACHE_SCRIPT,
        RESTORE_COMPONENT_CACHES_SCRIPT,
        RESTORE_FFMPEG_PROFILE_SCRIPT,
        PUBLISH_FFMPEG_PROFILE_SCRIPT,
        PUBLISH_RELEASE_CACHE_SCRIPT,
        PRUNE_RELEASE_CACHES_SCRIPT,
        TAG_RELEASE_PREFLIGHT_SCRIPT,
    ],
)
def test_release_powershell_scripts_parse(script: Path) -> None:
    command = (
        "$tokens = $null; "
        "$errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseFile($env:SCRIPT_TO_PARSE, [ref]$tokens, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }; "
        "Write-Host 'OK'"
    )

    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
        env={"SCRIPT_TO_PARSE": str(script)},
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_release_cache_restore_treats_missing_artifact_as_cache_miss(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if os.name == "nt":
        fake_gh = fake_bin / "gh.cmd"
        fake_gh.write_text(
            "@echo off\r\necho release not found 1^>^&2\r\nexit /b 1\r\n",
            encoding="utf-8",
        )
    else:
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            "#!/bin/sh\necho 'release not found' >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)

    github_output = tmp_path / "github-output.txt"
    destination = tmp_path / "restored"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RESTORE_RELEASE_CACHE_SCRIPT),
        "-Repo",
        "example/missing",
        "-Tag",
        "missing-cache-generation",
        "-AssetName",
        "missing.zip",
        "-DestinationPath",
        str(destination),
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GITHUB_OUTPUT": str(github_output),
        },
    )

    assert result.returncode == 0, result.stderr
    assert not destination.exists()
    outputs = github_output.read_text(encoding="utf-8-sig")
    assert "restored=false" in outputs
    assert "source=none" in outputs


@pytest.mark.parametrize(
    ("script", "extra_args", "expected"),
    [
        (
            PUBLISH_RELEASE_CACHE_SCRIPT,
            ["-SourcePath", "missing-cache-source", "-Title", "Invalid"],
            "Refusing cache publication for non-cache release tag",
        ),
        (
            PUBLISH_FFMPEG_PROFILE_SCRIPT,
            ["-BuildRoot", "missing-ffmpeg-cache"],
            "Refusing FFmpeg cache publication for non-cache release tag",
        ),
    ],
)
def test_cache_publishers_refuse_public_app_release_tags(
    script: Path, extra_args: list[str], expected: str
) -> None:
    result = run_powershell(
        "-NoProfile",
        "-File",
        str(script),
        "-Repo",
        "MyButtermilk/Scriber",
        "-Tag",
        "v0.5.13",
        "-AssetName",
        "must-never-upload.zip",
        *extra_args,
    )

    assert result.returncode != 0
    assert expected in (result.stdout + result.stderr)


def test_authenticode_gate_rejects_unsigned_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "unsigned.exe"
    artifact.write_bytes(b"MZ")
    report = tmp_path / "authenticode.json"

    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(AUTHENTICODE_SCRIPT),
        "-Path",
        str(artifact),
        "-OutputPath",
        str(report),
    )

    assert result.returncode == 1
    assert "Authenticode signature" in result.stderr
    assert not report.exists()


def test_build_script_wires_authenticode_gate() -> None:
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$RequireAuthenticodeSignature" in build_script
    assert "[string]$ExpectedAuthenticodePublisher" in build_script
    assert "[switch]$RequireAuthenticodeTimestamp" in build_script
    assert '$releaseExe = Join-Path $targetRelease "scriber-desktop.exe"' in build_script
    assert "$authenticodeTargets" in build_script
    assert "scripts\\validate_windows_authenticode.ps1" in build_script
    assert '$authenticodeReportPath = Join-Path $metadataDir "authenticode.json"' in build_script
    assert '"-OutputPath", $authenticodeReportPath' in build_script
    assert "Authenticode signature validation" in build_script


def test_authenticode_gate_supports_json_output_path() -> None:
    script = AUTHENTICODE_SCRIPT.read_text(encoding="utf-8")

    assert '[string]$OutputPath = ""' in script
    assert "function Write-Utf8NoBomJson" in script
    assert "Write-Utf8NoBomJson -Path $resolvedOutputPath -Json $json" in script
    assert "$json" in script


def test_release_workflow_exposes_authenticode_gate_switches() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE" in workflow
    assert "SCRIBER_AUTHENTICODE_PUBLISHER" in workflow
    assert "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP" in workflow
    assert "-RequireAuthenticodeSignature" in workflow
    assert "-RequireAuthenticodeTimestamp" in workflow


def tag_release_preflight_env(**overrides: str) -> dict[str, str]:
    values = {
        "SCRIBER_TAURI_UPDATER_PUBLIC_KEY": "",
        "SCRIBER_TAURI_UPDATER_ENDPOINT": "",
        "TAURI_SIGNING_PRIVATE_KEY": "",
        "TAURI_SIGNING_PRIVATE_KEY_PATH": "",
        "SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE": "",
        "SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE": "",
        "SCRIBER_AUTHENTICODE_PUBLISHER": "",
        "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP": "",
    }
    values.update(overrides)
    return values


def test_tag_release_preflight_accepts_signed_updater_without_logging_keys() -> None:
    public_key = "PUBLIC_KEY_MUST_NOT_BE_LOGGED"
    private_key = "PRIVATE_KEY_MUST_NOT_BE_LOGGED"
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TAG_RELEASE_PREFLIGHT_SCRIPT),
        env=tag_release_preflight_env(
            SCRIBER_TAURI_UPDATER_PUBLIC_KEY=public_key,
            TAURI_SIGNING_PRIVATE_KEY=private_key,
            SCRIBER_TAURI_UPDATER_ENDPOINT=(
                "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
            ),
            SCRIBER_REQUIRE_AUTHENTICODE_SIGNATURE="1",
            SCRIBER_AUTHENTICODE_PUBLISHER="Trusted Publisher",
            SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP="1",
        ),
    )

    combined_output = result.stdout + result.stderr
    assert result.returncode == 0, combined_output
    assert "Tag release preflight passed" in result.stdout
    assert public_key not in combined_output
    assert private_key not in combined_output


def test_tag_release_preflight_rejects_missing_updater_signing_without_override() -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TAG_RELEASE_PREFLIGHT_SCRIPT),
        env=tag_release_preflight_env(),
    )

    assert result.returncode == 1
    assert "Signed v* releases require SCRIBER_TAURI_UPDATER_PUBLIC_KEY" in result.stderr


def test_tag_release_preflight_allows_explicit_unsigned_override() -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TAG_RELEASE_PREFLIGHT_SCRIPT),
        env=tag_release_preflight_env(SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE="1"),
    )

    assert result.returncode == 0, result.stderr
    assert "Intentional unsigned v* release override is enabled" in result.stdout


def test_tag_release_preflight_rejects_non_https_updater_endpoint() -> None:
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TAG_RELEASE_PREFLIGHT_SCRIPT),
        env=tag_release_preflight_env(
            SCRIBER_TAURI_UPDATER_PUBLIC_KEY="PUBLIC_KEY",
            TAURI_SIGNING_PRIVATE_KEY="PRIVATE_KEY",
            SCRIBER_TAURI_UPDATER_ENDPOINT="http://example.test/latest.json",
        ),
    )

    assert result.returncode == 1
    assert "must be an absolute HTTPS URL" in result.stderr


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        (
            {"SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP": "1"},
            "SCRIBER_REQUIRE_AUTHENTICODE_TIMESTAMP=1 requires",
        ),
        (
            {"SCRIBER_AUTHENTICODE_PUBLISHER": "Unused Publisher"},
            "SCRIBER_AUTHENTICODE_PUBLISHER is only valid",
        ),
        (
            {"SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE": "true"},
            "SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE must be unset, '0', or '1'",
        ),
    ],
)
def test_tag_release_preflight_rejects_inconsistent_policy(
    overrides: dict[str, str], expected_error: str
) -> None:
    policy = {"SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE": "1"}
    policy.update(overrides)
    result = run_powershell(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(TAG_RELEASE_PREFLIGHT_SCRIPT),
        env=tag_release_preflight_env(**policy),
    )

    assert result.returncode == 1
    assert expected_error in result.stderr
