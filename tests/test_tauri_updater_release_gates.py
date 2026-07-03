from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_tauri_updater_metadata.py"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare_tauri_updater_config.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"


def run_script(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script_env = os.environ.copy()
    if env:
        script_env.update(env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO_ROOT,
        env=script_env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_manifest(path: Path, *, signature: str = "signed-update") -> None:
    path.write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "notes": "Release notes",
                "pub_date": "2026-06-01T13:00:00Z",
                "platforms": {
                    "windows-x86_64": {
                        "signature": signature,
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                    }
                },
                "artifacts": [
                    {
                        "name": "Scriber_0.1.0_x64-setup.exe",
                        "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
                        "sha256": "a" * 64,
                        "sizeBytes": 123,
                        "signature": signature,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def write_local_release_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    artifact_dir = tmp_path / "bundle"
    artifact_dir.mkdir()
    artifact = artifact_dir / "Scriber_0.1.0_x64-setup.exe"
    artifact.write_bytes(b"Scriber setup")
    checksum = sha256(artifact.read_bytes()).hexdigest()

    manifest = tmp_path / "latest.json"
    write_manifest(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["url"] = artifact.name
    data["artifacts"][0]["url"] = artifact.name
    data["artifacts"][0]["sha256"] = checksum
    data["artifacts"][0]["sizeBytes"] = artifact.stat().st_size
    manifest.write_text(json.dumps(data), encoding="utf-8")

    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{checksum}  {artifact.name}\n", encoding="utf-8")
    return manifest, artifact_dir, sums, checksum


def test_validate_tauri_updater_metadata_accepts_signed_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    write_manifest(manifest)

    result = run_script(VALIDATE_SCRIPT, "--metadata", str(manifest), "--require-signatures")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_validate_tauri_updater_metadata_rejects_missing_signature(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    write_manifest(manifest, signature="")

    result = run_script(VALIDATE_SCRIPT, "--metadata", str(manifest), "--require-signatures")

    assert result.returncode == 1
    assert "signature is required" in result.stderr


def test_validate_tauri_updater_metadata_allows_local_urls_only_when_not_required(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    write_manifest(manifest, signature="")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["url"] = "Scriber_0.1.0_x64-setup.exe"
    data["artifacts"][0]["url"] = "Scriber_0.1.0_x64-setup.exe"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    local_result = run_script(VALIDATE_SCRIPT, "--metadata", str(manifest), "--allow-local-urls")
    release_result = run_script(
        VALIDATE_SCRIPT,
        "--metadata",
        str(manifest),
        "--allow-local-urls",
        "--require-signatures",
    )

    assert local_result.returncode == 0, local_result.stderr
    assert release_result.returncode == 1
    assert "Updater URL must be absolute HTTPS" in release_result.stderr


def test_validate_tauri_updater_metadata_verifies_local_artifacts_and_sums(tmp_path: Path) -> None:
    manifest, artifact_dir, sums, _checksum = write_local_release_fixture(tmp_path)

    result = run_script(
        VALIDATE_SCRIPT,
        "--metadata",
        str(manifest),
        "--allow-local-urls",
        "--artifact-dir",
        str(artifact_dir),
        "--sha256sums",
        str(sums),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["localArtifactsVerified"] == 1


def test_validate_tauri_updater_metadata_rejects_local_artifact_checksum_mismatch(tmp_path: Path) -> None:
    manifest, artifact_dir, sums, _checksum = write_local_release_fixture(tmp_path)
    (artifact_dir / "Scriber_0.1.0_x64-setup.exe").write_bytes(b"tampered setup")

    result = run_script(
        VALIDATE_SCRIPT,
        "--metadata",
        str(manifest),
        "--allow-local-urls",
        "--artifact-dir",
        str(artifact_dir),
        "--sha256sums",
        str(sums),
    )

    assert result.returncode == 1
    assert "Artifact size mismatch" in result.stderr or "Artifact SHA256 mismatch" in result.stderr


def test_build_script_validates_release_metadata_against_local_artifacts() -> None:
    build_script = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    assert '"--artifact-dir",' in build_script
    assert "$bundleRoot" in build_script
    assert '"--sha256sums",' in build_script
    assert 'Join-Path $metadataDir "SHA256SUMS.txt"' in build_script


def test_build_script_prepares_sidecar_before_tauri_bundle() -> None:
    build_script = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")

    sidecar_index = build_script.index('Invoke-Checked -Label "Tauri sidecar preparation"')
    bundle_index = build_script.index('Invoke-Checked -Label "Tauri Windows bundle"')
    assert sidecar_index < bundle_index
    assert "Remove-TauriBeforeBundleCommand" in build_script
    assert '$config.build.PSObject.Properties.Remove("beforeBundleCommand")' in build_script
    assert "resources" in build_script


def test_prepare_tauri_updater_config_writes_signed_release_config(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    config.write_text(
        json.dumps(
            {
                "productName": "Scriber",
                "version": "0.1.0",
                "identifier": "app.scriber.desktop",
                "bundle": {"active": True},
            }
        ),
        encoding="utf-8",
    )

    result = run_script(
        PREPARE_SCRIPT,
        "--config",
        str(config),
        "--write",
        "--public-key",
        "PUBLIC_KEY",
        "--endpoint",
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        env={"TAURI_SIGNING_PRIVATE_KEY": "PRIVATE_KEY"},
    )

    assert result.returncode == 0, result.stderr
    updated = json.loads(config.read_text(encoding="utf-8"))
    assert updated["bundle"]["createUpdaterArtifacts"] is True
    assert updated["plugins"]["updater"]["pubkey"] == "PUBLIC_KEY"
    assert updated["plugins"]["updater"]["endpoints"] == [
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
    ]


def test_prepare_tauri_updater_config_requires_signing_key(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    config.write_text("{}", encoding="utf-8")
    env = {
        "TAURI_SIGNING_PRIVATE_KEY": "",
        "TAURI_SIGNING_PRIVATE_KEY_PATH": "",
    }

    result = run_script(
        PREPARE_SCRIPT,
        "--config",
        str(config),
        "--public-key",
        "PUBLIC_KEY",
        env=env,
    )

    assert result.returncode == 1
    assert "TAURI_SIGNING_PRIVATE_KEY" in result.stderr


def test_release_workflow_verifies_published_updater_metadata_after_release() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    publish_index = workflow.index("Publish GitHub release")
    verify_index = workflow.index("Verify published updater metadata")
    upload_index = workflow.index("Upload publication evidence")
    assert publish_index < verify_index < upload_index
    assert "scripts\\verify_tauri_updater_publication.py" in workflow
    assert "--output release-artifacts\\updater-publication.json" in workflow
    assert "--attempts 6" in workflow
    assert "--retry-delay-sec 10" in workflow
    assert "SCRIBER_TAURI_UPDATER_PUBLIC_KEY" in workflow
    assert "TAURI_SIGNING_PRIVATE_KEY" in workflow
    assert "TAURI_SIGNING_PRIVATE_KEY_PATH" in workflow
    assert "SCRIBER_TAURI_UPDATER_ENDPOINT" in workflow
    assert "latest/download/latest.json" in workflow
    assert "hashFiles('release-artifacts/updater-publication.json')" in workflow
