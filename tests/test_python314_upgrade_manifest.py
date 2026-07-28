from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_python314_upgrade_manifest import validate_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "packaging" / "python-314-upgrade-from-v0.5.47.json"
HOOKS = REPO_ROOT / "Frontend" / "src-tauri" / "windows" / "installer-hooks.nsh"


def test_python314_upgrade_manifest_and_exact_tombstones_are_valid() -> None:
    result = validate_manifest(MANIFEST, HOOKS)

    assert result["sourceRepository"] == "MyButtermilk/Scriber"
    assert result["sourceReleaseId"] == 360523309
    assert result["sourceTag"] == "v0.5.47"
    assert result["sourceCommitSha"] == "aa918d3d111dab834e05e5b588c24da503f9176d"
    assert result["sourceAssetId"] == 491618831
    assert result["sourceInstallerName"] == "Scriber_0.5.47_x64-setup.exe"
    assert result["sourceInstallerLength"] == 79787381
    assert result["sourceInstallerSha256"] == ("a4c242935e2eb26d6ab2517a38538b4942f204477cb745ef6c7909312efdd8f5")
    assert result["obsoleteFileCount"] == 33
    assert result["obsoleteRuntimeFileCount"] == 43
    assert result["obsoleteRuntimeDirectoryCount"] == 1
    assert result["obsoleteBundledToolFileCount"] == 2
    assert result["installerVerified"] is False


def test_python314_upgrade_manifest_rejects_wildcard_substitution(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            'Delete "$INSTDIR\\backend\\_internal\\python313.dll"',
            'Delete "$INSTDIR\\backend\\_internal\\python*.dll"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wildcard deletion"):
        validate_manifest(MANIFEST, hooks)


def test_python314_upgrade_manifest_rejects_additional_wildcard_delete(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            "!macroend",
            '  Delete "$INSTDIR\\backend\\_internal\\*.cp313-win_amd64.pyd"\n!macroend',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wildcard deletion"):
        validate_manifest(MANIFEST, hooks)


def test_python314_upgrade_manifest_rejects_additional_recursive_rmdir(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            "!macroend",
            '  RMDir /r "$INSTDIR\\backend\\_internal"\n!macroend',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="recursive deletion"):
        validate_manifest(MANIFEST, hooks)


def test_python314_upgrade_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["obsoletePython313Files"][0] = "_internal/../python313.dll"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe obsolete path"):
        validate_manifest(manifest, HOOKS)


def test_python314_upgrade_manifest_rejects_backslash_path_traversal(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["obsoletePython313Files"][0] = "_internal/subdir\\..\\python313.dll"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe obsolete path"):
        validate_manifest(manifest, HOOKS)


def test_python314_upgrade_manifest_binds_runtime_tombstones(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            'Delete "$INSTDIR\\backend\\_internal\\ucrtbase.dll"',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing exact runtime tombstone"):
        validate_manifest(MANIFEST, hooks)


def test_python314_upgrade_manifest_binds_runtime_directory_tombstone(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            '  RMDir "$INSTDIR\\backend\\_internal\\websockets"\n',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing exact runtime directory tombstone"):
        validate_manifest(MANIFEST, hooks)


def test_python314_upgrade_manifest_binds_removed_yt_dlp_launcher(tmp_path: Path) -> None:
    hooks = tmp_path / "installer-hooks.nsh"
    source = HOOKS.read_text(encoding="utf-8")
    hooks.write_text(
        source.replace(
            '  Delete "$INSTDIR\\backend\\tools\\ffmpeg\\yt-dlp.exe"\n',
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing exact bundled tool tombstone"):
        validate_manifest(MANIFEST, hooks)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("sourceRelease", "repository"), "foreign/Scriber"),
        (("sourceRelease", "releaseId"), 1),
        (("sourceRelease", "tag"), "v0.5.46"),
        (("sourceRelease", "commitSha"), "0" * 40),
        (("sourceRelease", "installer", "assetId"), 1),
        (("sourceRelease", "installer", "name"), "other.exe"),
        (("sourceRelease", "installer", "length"), 1),
        (("sourceRelease", "installer", "sha256"), "0" * 64),
    ],
)
def test_python314_upgrade_manifest_rejects_nonpublic_baseline_identity(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: str | int,
) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="public v0.5.47"):
        validate_manifest(manifest, HOOKS)
