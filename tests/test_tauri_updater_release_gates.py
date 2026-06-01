from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_tauri_updater_metadata.py"
PREPARE_SCRIPT = REPO_ROOT / "scripts" / "prepare_tauri_updater_config.py"


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
