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
TAG_RELEASE_PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "ci" / "validate_tag_release_preflight.ps1"


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


def test_validate_tauri_updater_metadata_links_platform_to_artifact(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    write_manifest(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["url"] = (
        "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/other.exe"
    )
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = run_script(VALIDATE_SCRIPT, "--metadata", str(manifest), "--require-signatures")

    assert result.returncode == 1
    assert "must match exactly one" in result.stderr


def test_validate_tauri_updater_metadata_links_platform_signature_to_artifact(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    write_manifest(manifest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["platforms"]["windows-x86_64"]["signature"] = "different-signature"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    result = run_script(VALIDATE_SCRIPT, "--metadata", str(manifest), "--require-signatures")

    assert result.returncode == 1
    assert "must match the selected artifact signature" in result.stderr


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

    config_index = build_script.index('Invoke-Checked -Label "Prepare Tauri build config"')
    sidecar_index = build_script.index('Invoke-Checked -Label "Tauri sidecar preparation"')
    bundle_index = build_script.index('Invoke-Checked -Label "Tauri Windows bundle"')
    assert config_index < sidecar_index < bundle_index
    assert "scripts\\prepare_tauri_updater_config.py" in build_script
    assert "$tauriBuildConfigPath" in build_script
    assert '"--output"' in build_script
    assert '"--version"' in build_script
    assert '"--remove-before-bundle-command"' in build_script
    assert '"--skip-updater-config"' in build_script
    assert "Remove-TauriBeforeBundleCommand" not in build_script
    assert '$config.build.PSObject.Properties.Remove("beforeBundleCommand")' not in build_script
    assert "Set-Utf8NoBomContent" not in build_script
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


def test_prepare_tauri_updater_config_writes_release_overlay_without_mutating_source(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    output = tmp_path / "tauri.generated.conf.json"
    source_payload = {
        "productName": "Scriber",
        "version": "../package.json",
        "identifier": "app.scriber.desktop",
        "build": {"beforeBundleCommand": "powershell -File scripts/build_tauri_backend_sidecar.ps1"},
        "bundle": {"active": True, "windows": {"nsis": {}}},
        "plugins": {"updater": {"pubkey": "", "endpoints": []}},
    }
    config.write_text(json.dumps(source_payload), encoding="utf-8")

    result = run_script(
        PREPARE_SCRIPT,
        "--config",
        str(config),
        "--output",
        str(output),
        "--version",
        "0.4.20",
        "--remove-before-bundle-command",
        "--skip-updater-config",
        "--nsis-compression",
        "zlib",
        env={"TAURI_SIGNING_PRIVATE_KEY": "", "TAURI_SIGNING_PRIVATE_KEY_PATH": ""},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text(encoding="utf-8")) == source_payload
    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated == {
        "version": "0.4.20",
        "bundle": {"windows": {"nsis": {"compression": "zlib"}}},
        "build": {"beforeBundleCommand": None},
    }


def test_prepare_tauri_updater_config_empty_env_endpoint_uses_default(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    output = tmp_path / "tauri.generated.conf.json"
    config.write_text(
        json.dumps(
            {
                "productName": "Scriber",
                "version": "../package.json",
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
        "--output",
        str(output),
        "--version",
        "0.4.20",
        "--public-key",
        "PUBLIC_KEY",
        env={
            "TAURI_SIGNING_PRIVATE_KEY": "PRIVATE_KEY",
            "SCRIBER_TAURI_UPDATER_ENDPOINT": "",
        },
    )

    assert result.returncode == 0, result.stderr
    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["plugins"]["updater"]["endpoints"] == [
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
    ]
    assert generated["bundle"]["createUpdaterArtifacts"] is True


def test_prepare_tauri_updater_config_can_embed_runtime_without_artifacts(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    output = tmp_path / "tauri.generated.conf.json"
    config.write_text("{}", encoding="utf-8")

    result = run_script(
        PREPARE_SCRIPT,
        "--config",
        str(config),
        "--output",
        str(output),
        "--version",
        "0.5.3",
        "--public-key",
        "PUBLIC_KEY",
        "--endpoint",
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json",
        "--skip-signing-key-check",
        "--skip-updater-artifacts",
        env={"TAURI_SIGNING_PRIVATE_KEY": "", "TAURI_SIGNING_PRIVATE_KEY_PATH": ""},
    )

    assert result.returncode == 0, result.stderr
    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["plugins"]["updater"]["pubkey"] == "PUBLIC_KEY"
    assert "bundle" not in generated or "createUpdaterArtifacts" not in generated["bundle"]


def test_prepare_tauri_updater_config_reads_public_key_from_env(tmp_path: Path) -> None:
    config = tmp_path / "tauri.conf.json"
    output = tmp_path / "tauri.generated.conf.json"
    config.write_text(
        json.dumps(
            {
                "productName": "Scriber",
                "version": "../package.json",
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
        "--output",
        str(output),
        "--version",
        "0.4.20",
        env={
            "TAURI_SIGNING_PRIVATE_KEY": "PRIVATE_KEY",
            "SCRIBER_TAURI_UPDATER_PUBLIC_KEY": "ENV_PUBLIC_KEY",
            "SCRIBER_TAURI_UPDATER_ENDPOINT": "",
        },
    )

    assert result.returncode == 0, result.stderr
    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["plugins"]["updater"]["pubkey"] == "ENV_PUBLIC_KEY"
    assert generated["plugins"]["updater"]["endpoints"] == [
        "https://github.com/MyButtermilk/Scriber/releases/latest/download/latest.json"
    ]
    assert generated["bundle"]["createUpdaterArtifacts"] is True


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
    preflight = TAG_RELEASE_PREFLIGHT_SCRIPT.read_text(encoding="utf-8")

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
    assert "SCRIBER_ALLOW_UNSIGNED_TAG_RELEASE" in workflow
    assert "Validate tag release signing preflight" in workflow
    assert "Signed v* releases require SCRIBER_TAURI_UPDATER_PUBLIC_KEY" in preflight


def test_release_workflow_requires_signature_file_for_signed_updater_artifacts() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    collect_index = workflow.index("Collect release artifacts")
    summary_index = workflow.index("Summarize release artifact timing")
    upload_index = workflow.index("Upload build artifacts")
    assert collect_index < summary_index < upload_index
    assert "$artifactEntries = @($metadata.artifacts)" in workflow
    assert "latest.json contains a release artifact without a name" in workflow
    assert '$signaturePath = "$($matches[0].FullName).sig"' in workflow
    assert "Copy-Item -LiteralPath $signaturePath -Destination release-artifacts\\" in workflow
    assert "Signed updater artifact '$artifactName' is missing sibling signature file" in workflow
    assert "-not [string]::IsNullOrWhiteSpace([string]$artifact.signature)" in workflow
    assert "release-artifact-summary.json" in workflow
