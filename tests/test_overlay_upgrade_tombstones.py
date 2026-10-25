from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

import pytest

from scripts.validate_python314_upgrade_manifest import _validate_installer_hook_delete_safety

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "packaging" / "overlay-upgrade-from-v0.5.138.json"
HOOKS = REPO_ROOT / "Frontend" / "src-tauri" / "windows" / "installer-hooks.nsh"
RELEASE_CONSTRAINTS = REPO_ROOT / "requirements-release-constraints.txt"

_SAFE_PATH = re.compile(r"^_internal/[A-Za-z0-9_.+\-/]+$")
_DIST_INFO = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-/]+)\.dist-info$")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _nsis(path: str) -> str:
    return "$INSTDIR\\backend\\" + path.replace("/", "\\")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pinned_versions() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in RELEASE_CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        requirement = line.split("#", 1)[0].strip()
        if "==" in requirement:
            name, version = requirement.split("==", 1)
            pins[_normalized(name.split("[", 1)[0])] = version.split(";", 1)[0].strip()
    return pins


def _assert_safe(path: object) -> str:
    assert isinstance(path, str)
    assert _SAFE_PATH.match(path), path
    assert ".." not in PurePosixPath(path).parts
    return path


def test_overlay_upgrade_manifest_binds_the_public_v0_5_138_installer() -> None:
    manifest = _manifest()

    assert manifest["contract"] == "ScriberOverlayUpgradeTombstonesV1"
    assert manifest["schemaVersion"] == 1
    assert manifest["sourceRelease"] == {
        "repository": "MyButtermilk/Scriber",
        "releaseId": 418666404,
        "tag": "v0.5.138",
        "commitSha": "d80c9db3f3a2e348694135330c55deb4042e6994",
        "installer": {
            "assetId": 659971934,
            "name": "Scriber_0.5.138_x64-setup.exe",
            "length": 128890800,
            "sha256": "79b8c1d2867194b7103e7b58a955deaee0a897bca1e5039677589b552d26a362",
        },
    }
    assert len(manifest["obsoleteFiles"]) == 48
    assert len(manifest["obsoleteDirectories"]) == 17


def test_every_obsolete_file_has_exactly_one_exact_delete() -> None:
    manifest = _manifest()
    hooks = HOOKS.read_text(encoding="utf-8")
    _validate_installer_hook_delete_safety(hooks)

    files = [_assert_safe(path) for path in manifest["obsoleteFiles"]]
    assert files == sorted(set(files))
    for path in files:
        assert hooks.count(f'Delete "{_nsis(path)}"\n') == 1, path


def test_obsolete_directories_are_removed_child_first_and_non_recursively() -> None:
    manifest = _manifest()
    hooks = HOOKS.read_text(encoding="utf-8")
    files = manifest["obsoleteFiles"]
    directories = [_assert_safe(path) for path in manifest["obsoleteDirectories"]]
    assert len(directories) == len(set(directories))

    last_delete = max(hooks.index(f'Delete "{_nsis(path)}"') for path in files)
    positions: dict[str, int] = {}
    for directory in directories:
        line = f'RMDir "{_nsis(directory)}"\n'
        assert hooks.count(line) == 1, directory
        positions[directory] = hooks.index(line)
        assert positions[directory] > last_delete, directory
        assert any(path.startswith(directory + "/") for path in files), directory

    internal_rmdir = hooks.index('RMDir "$INSTDIR\\backend\\_internal"\n')
    for directory, position in positions.items():
        assert position < internal_rmdir, directory
        parent = str(PurePosixPath(directory).parent)
        if parent in positions:
            assert position < positions[parent], directory


def test_tombstones_never_target_the_currently_pinned_package_metadata() -> None:
    pins = _pinned_versions()
    retired = set()
    for path in _manifest()["obsoleteFiles"]:
        top = PurePosixPath(path).parts[1]
        match = _DIST_INFO.match(top)
        if match is None:
            retired.add(top)
            continue
        name = _normalized(match["name"])
        assert name in pins, top
        assert match["version"] != pins[name], top

    # Pipecat 1.14 replaced NLTK and its regex dependency; the frozen gate
    # rejects either if a dependency brings them back.
    assert retired == {"nltk", "nltk_data", "regex"}
    assert "nltk" not in pins
    assert "regex" not in pins


@pytest.mark.parametrize(
    "line",
    [
        '  Delete "$INSTDIR\\backend\\_internal\\pipecat_ai-*.dist-info\\RECORD"\n',
        '  RMDir /r "$INSTDIR\\backend\\_internal\\nltk_data"\n',
    ],
)
def test_overlay_upgrade_hook_rejects_wildcard_or_recursive_cleanup(line: str) -> None:
    hooks = HOOKS.read_text(encoding="utf-8").replace("!macroend", line + "!macroend")

    with pytest.raises(ValueError, match="wildcard deletion|recursive deletion"):
        _validate_installer_hook_delete_safety(hooks)
