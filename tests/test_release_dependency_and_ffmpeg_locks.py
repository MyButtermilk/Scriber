from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_CONSTRAINTS = REPO_ROOT / "requirements-release-constraints.txt"
TEST_CONSTRAINTS = REPO_ROOT / "requirements-test-constraints.txt"
FFMPEG_LOCK = REPO_ROOT / "packaging" / "ffmpeg-profile-b-release-lock-v1.json"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "ffmpeg" / "restore_profile_b_release_artifact.ps1"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"


def _requirements(path: Path) -> dict[str, Requirement]:
    result: dict[str, Requirement] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        requirement = Requirement(line)
        result[canonicalize_name(requirement.name)] = requirement
    return result


def _pins(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, requirement in _requirements(path).items():
        exact = [item.version for item in requirement.specifier if item.operator == "=="]
        assert len(exact) == 1, f"{name} must have one exact pin in {path.name}"
        result[name] = exact[0]
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_constraints_match_validated_test_graph() -> None:
    assert _pins(RELEASE_CONSTRAINTS) == _pins(TEST_CONSTRAINTS)


def test_all_direct_release_requirements_are_locked() -> None:
    constraints = _pins(RELEASE_CONSTRAINTS)
    for requirements_name in ("requirements-base.txt", "requirements-build.txt"):
        for name, requirement in _requirements(REPO_ROOT / requirements_name).items():
            assert name in constraints, f"{name} is missing from release constraints"
            assert constraints[name] in requirement.specifier

    base = _requirements(REPO_ROOT / "requirements-base.txt")
    assert str(base["audioop-lts"].specifier) == "==0.2.2"


def test_release_workflow_binds_installs_and_caches_to_constraints() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    dependency_hash = (
        "hashFiles('requirements-base.txt', 'requirements-build.txt', 'requirements-release-constraints.txt')"
    )

    assert dependency_hash in workflow
    assert "hashFiles('requirements-base.txt', 'requirements-build.txt')" not in workflow
    assert "pip install --upgrade pip" not in workflow
    assert workflow.count("--constraint requirements-release-constraints.txt") >= 7
    assert "hashFiles('packaging/ffmpeg-profile-b-release-lock-v1.json')" in workflow


def test_ffmpeg_release_lock_matches_current_release_asset() -> None:
    lock = json.loads(FFMPEG_LOCK.read_text(encoding="utf-8"))
    assert lock["schemaVersion"] == 1
    assert lock["repository"] == "MyButtermilk/Scriber"
    assert lock["release"]["tag"] == "ffmpeg-profile-b-n7.0-v4"
    assert lock["release"]["asset"]["name"] == "scriber-ffmpeg-profile-b-n7.0-v4-Windows.zip"
    assert lock["release"]["asset"]["sizeBytes"] == 5_139_991
    assert lock["release"]["asset"]["sha256"] == "b029f1e941d3a89a32f033e0398ba03776ac59a882902a7a935839e7825997d1"
    locked_files = {entry["path"]: entry for entry in lock["files"]}
    assert locked_files["dist/scriber-ffmpeg-profile-b/bin/ffmpeg.exe"] == {
        "path": "dist/scriber-ffmpeg-profile-b/bin/ffmpeg.exe",
        "sizeBytes": 2_779_648,
        "sha256": "d2c9fe4af2a3227d0dd191fc41e4e6cba9cbb8c8b1c14f2619dcb5d0c39f2cfb",
    }
    assert locked_files["dist/scriber-ffmpeg-profile-b/bin/ffprobe.exe"] == {
        "path": "dist/scriber-ffmpeg-profile-b/bin/ffprobe.exe",
        "sizeBytes": 2_657_792,
        "sha256": "066c2db8c0982a1e5a702d77b38c2f703eba84d3f6293102c833f9b6200c3f18",
    }


@pytest.mark.skipif(os.name != "nt", reason="PowerShell release helper is Windows-only")
def test_ffmpeg_restore_rejects_tampering_before_replacing_cache(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell

    archive = tmp_path / "profile.zip"
    payloads = {
        "dist/scriber-ffmpeg-profile-b/bin/ffmpeg.exe": b"locked ffmpeg",
        "dist/scriber-ffmpeg-profile-b/bin/ffprobe.exe": b"locked ffprobe",
    }
    report = json.dumps(
        {
            "apiVersion": "1",
            "ok": True,
            "sourceUrl": "https://example.invalid/ffmpeg.git",
            "gitRef": "locked-ref",
        }
    ).encode()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in payloads.items():
            bundle.writestr(name, payload)
        bundle.writestr("profile-b-msys2-build-report.json", report)

    lock = {
        "schemaVersion": 1,
        "profile": "profile-b",
        "repository": "example/repo",
        "release": {
            "tag": "locked-tag",
            "asset": {
                "name": archive.name,
                "sizeBytes": archive.stat().st_size,
                "sha256": _sha256(archive),
            },
        },
        "source": {
            "url": "https://example.invalid/ffmpeg.git",
            "gitRef": "locked-ref",
        },
        "files": [
            {
                "path": name,
                "sizeBytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in payloads.items()
        ],
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh.ps1"
    fake_gh.write_text(
        """
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Remaining)
$dirIndex = [Array]::IndexOf($Remaining, "--dir")
if ($dirIndex -lt 0) { exit 2 }
Copy-Item -LiteralPath $env:FAKE_FFMPEG_ARCHIVE -Destination (
    Join-Path $Remaining[$dirIndex + 1] $env:FAKE_FFMPEG_ASSET
) -Force
exit 0
""".strip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_FFMPEG_ARCHIVE"] = str(archive)
    env["FAKE_FFMPEG_ASSET"] = archive.name
    build_root = tmp_path / "restored"

    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RESTORE_SCRIPT),
        "-Repo",
        "example/repo",
        "-Tag",
        "locked-tag",
        "-AssetName",
        archive.name,
        "-BuildRoot",
        str(build_root),
        "-LockPath",
        str(lock_path),
    ]
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)
    assert (build_root / "dist/scriber-ffmpeg-profile-b/bin/ffmpeg.exe").read_bytes() == (
        payloads["dist/scriber-ffmpeg-profile-b/bin/ffmpeg.exe"]
    )

    sentinel = build_root / "sentinel.txt"
    sentinel.write_text("keep existing verified cache", encoding="utf-8")
    archive.write_bytes(archive.read_bytes() + b"tampered")
    failed = subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "mismatch" in (failed.stdout + failed.stderr)
    assert sentinel.read_text(encoding="utf-8") == "keep existing verified cache"
