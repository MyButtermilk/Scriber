from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.installer_research.inventory import (
    InventoryError,
    _build_tree_inventory,
    _normalize_sidecar_build_metadata,
    _normalize_tauri_bundle_type,
    build_root_identity_sha256,
    build_inventory,
    inspect_pyinstaller_executable,
    load_component_map,
    select_installer,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_MAP = REPO_ROOT / "packaging" / "installer-component-map-v1.json"
TOOLCHAIN_HASH = "1" * 64
EVALUATOR_HASH = "2" * 64
RUN_ID = "12345678-1234-4234-8234-123456789abc"
SOURCE_COMMIT = "a" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar_metadata(
    backend_exe: Path,
    *,
    generated_at: str = "2026-07-18T10:00:00Z",
    root: str = "C:/build/one",
    cache_hit: bool = False,
    stable_marker: str = "same",
) -> dict:
    return {
        "apiVersion": "1",
        "generatedAt": generated_at,
        "sidecarDir": f"{root}/backend",
        "sidecarExe": f"{root}/backend/scriber-backend.exe",
        "sidecar": {
            "sha256": _sha256(backend_exe),
            "length": backend_exe.stat().st_size,
        },
        "copiedToTauriRelease": f"{root}/target/release/backend",
        "targetCurrent": cache_hit,
        "cache": {"enabled": True, "hit": cache_hit, "key": "3" * 64},
        "runtimeLayer": {"cacheHit": cache_hit, "cacheKey": "4" * 64},
        "mediaTools": {"files": []},
        "applicationLayer": {"applicationVersion": "9.8.7"},
        "flags": {"stableMarker": stable_marker},
        "preparedMediaTools": {"temporaryPath": f"{root}/tmp"},
        "mediaToolsCopied": [f"{root}/ffmpeg.exe"],
        "rustAudioSidecarCopied": None,
        "rustDiarizationSidecarCopied": None,
        "totalDurationMs": 1234,
        "phases": [
            {"label": "freeze", "durationMs": 1234, "ok": True},
        ],
    }


@pytest.fixture(scope="module")
def minimal_pyinstaller_payload(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("PyInstaller")
    root = tmp_path_factory.mktemp("installer-research-pyinstaller")
    package = root / "overlap_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .sub import VALUE\n", encoding="utf-8")
    (package / "sub.py").write_text("VALUE = 'fixture'\n", encoding="utf-8")
    source = root / "entry.py"
    source.write_text(
        "import json\n"
        "import urllib.parse\n"
        "import overlap_pkg.sub\n"
        "print(json.dumps({'path': urllib.parse.urlparse('https://example.test').path, "
        "'value': overlap_pkg.sub.VALUE}))\n",
        encoding="utf-8",
    )
    dist = root / "dist"
    work = root / "work"
    spec = root / "spec"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "scriber-backend",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            str(source),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    staged = root / "staged"
    backend = staged / "backend"
    shutil.copytree(dist / "scriber-backend", backend)
    (staged / "scriber-desktop.exe").write_bytes(b"desktop-shell")
    (staged / "scriber-audio-sidecar.exe").write_bytes(b"audio-sidecar")
    app = backend / "app"
    app.mkdir()
    (app / "app-layer-manifest.json").write_text(
        json.dumps({"applicationVersion": "9.8.7"}),
        encoding="utf-8",
    )
    backend_exe = backend / "scriber-backend.exe"
    (backend / "sidecar-build-metadata.json").write_text(
        json.dumps(_sidecar_metadata(backend_exe)),
        encoding="utf-8",
    )
    return staged


def _make_installer(root: Path, *, content: bytes = b"fake-nsis") -> Path:
    installer = root / "Scriber_9.8.7_x64-setup.exe"
    installer.parent.mkdir(parents=True, exist_ok=True)
    installer.write_bytes(content)
    return installer


def _make_symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"This Windows environment cannot create test symlinks: {exc}")


def _inventory(staged: Path, installer: Path, *, installed: Path | None = None) -> dict:
    return build_inventory(
        run_id=RUN_ID,
        source_commit=SOURCE_COMMIT,
        replica_id="packet-inventory-test",
        build_root_sha256=build_root_identity_sha256(staged),
        staged_root=staged,
        backend_exe=staged / "backend" / "scriber-backend.exe",
        component_map_path=COMPONENT_MAP,
        installer=installer,
        installed_root=installed,
        product_version=None,
        compression="bzip2",
        toolchain_hash=TOOLCHAIN_HASH,
        evaluator_hash=EVALUATOR_HASH,
    )


def test_real_pyinstaller_620_payload_has_exact_physical_and_virtual_partitions(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    staged = minimal_pyinstaller_payload
    installer = _make_installer(tmp_path)

    inventory = _inventory(staged, installer)
    tree = inventory["payload"]["staged"]
    backend = inventory["backendExecutable"]
    backend_file = next(
        item for item in tree["files"] if item["path"] == "backend/scriber-backend.exe"
    )

    assert inventory["ok"] is True
    assert tree["totalBytes"] == sum(item["length"] for item in tree["files"])
    assert tree["componentBytesSum"] == tree["totalBytes"]
    assert sum(item["rawBytes"] for item in tree["components"].values()) == tree["totalBytes"]
    assert backend["pyinstallerVersion"] == "6.20.0"
    assert backend["physicalAllocationMode"] == "disjoint-virtual-leaves"
    assert backend["virtualPartitionBytes"] == backend["length"]
    assert sum(backend["componentAllocations"].values()) == backend["length"]
    assert backend_file["component"] is None
    assert backend_file["componentAllocations"] == backend["componentAllocations"]
    for component, allocated_bytes in backend["componentAllocations"].items():
        assert tree["components"][component]["rawBytes"] >= allocated_bytes
    assert (
        tree["components"]["backend-executable"]["rawBytes"]
        < backend["length"]
    )
    assert backend["pyzDiagnostics"]["countedInStagedPayload"] is False
    assert backend["pyzDiagnostics"]["entryCount"] > 0
    assert (
        sum(
            item["compressedBytes"]
            for item in backend["pyzDiagnostics"]["entries"]
        )
        == backend["pyzDiagnostics"]["compressedModuleBytes"]
    )
    for module in backend["pyzDiagnostics"]["entries"]:
        assert module["decompressedBytes"] >= 0
        assert len(module["decompressedSha256"]) == 64


def test_inventory_rejects_forged_build_root_provenance(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(InventoryError, match="resolved staged-root identity"):
        build_inventory(
            run_id=RUN_ID,
            source_commit=SOURCE_COMMIT,
            replica_id="packet-forged-root",
            build_root_sha256="b" * 64,
            staged_root=minimal_pyinstaller_payload,
            backend_exe=minimal_pyinstaller_payload
            / "backend"
            / "scriber-backend.exe",
            component_map_path=COMPONENT_MAP,
            installer=_make_installer(tmp_path),
            compression="bzip2",
            toolchain_hash=TOOLCHAIN_HASH,
            evaluator_hash=EVALUATOR_HASH,
        )


def test_inventory_rejects_staged_root_and_backend_symlink_entries(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    linked_staged = tmp_path / "linked-staged"
    _make_symlink_or_skip(linked_staged, minimal_pyinstaller_payload, directory=True)
    with pytest.raises(InventoryError, match="Staged root must not be a symlink"):
        build_inventory(
            run_id=RUN_ID,
            source_commit=SOURCE_COMMIT,
            replica_id="packet-linked-root",
            build_root_sha256=build_root_identity_sha256(linked_staged),
            staged_root=linked_staged,
            backend_exe=linked_staged / "backend" / "scriber-backend.exe",
            component_map_path=COMPONENT_MAP,
            installer=_make_installer(tmp_path / "root-installer"),
            compression="bzip2",
            toolchain_hash=TOOLCHAIN_HASH,
            evaluator_hash=EVALUATOR_HASH,
        )

    staged = tmp_path / "backend-link-staged"
    (staged / "backend").mkdir(parents=True)
    real_backend = tmp_path / "real-backend.exe"
    real_backend.write_bytes(b"not-read")
    linked_backend = staged / "backend" / "scriber-backend.exe"
    _make_symlink_or_skip(linked_backend, real_backend)
    with pytest.raises(InventoryError, match="Backend path entry must not be a symlink"):
        build_inventory(
            run_id=RUN_ID,
            source_commit=SOURCE_COMMIT,
            replica_id="packet-linked-backend",
            build_root_sha256=build_root_identity_sha256(staged),
            staged_root=staged,
            backend_exe=linked_backend,
            component_map_path=COMPONENT_MAP,
            installer=_make_installer(tmp_path / "backend-installer"),
            product_version="9.8.7",
            compression="bzip2",
            toolchain_hash=TOOLCHAIN_HASH,
            evaluator_hash=EVALUATOR_HASH,
        )


def test_installed_tree_allows_only_the_nsis_uninstaller_as_an_extra_file(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    shutil.copytree(minimal_pyinstaller_payload, installed)
    (installed / "uninstall.exe").write_bytes(b"uninstaller")

    inventory = _inventory(
        minimal_pyinstaller_payload,
        _make_installer(tmp_path / "artifacts"),
        installed=installed,
    )

    assert inventory["ok"] is True
    assert inventory["payload"]["stagedInstalledParity"] == {
        "ok": True,
        "missingFromInstalled": [],
        "changedInInstalled": [],
        "installedOnly": ["uninstall.exe"],
        "allowedInstalledOnly": ["uninstall.exe"],
        "unexpectedInstalledOnly": [],
    }


def test_installed_tree_normalizes_only_the_tauri_nsis_bundle_marker(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    installed = tmp_path / "installed"
    shutil.copytree(minimal_pyinstaller_payload, staged)
    shutil.copytree(minimal_pyinstaller_payload, installed)
    prefix = b"__TAURI_BUNDLE_TYPE_VAR_"
    (staged / "scriber-desktop.exe").write_bytes(
        b"MZbefore" + prefix + b"UNK\xc0\x00after"
    )
    (installed / "scriber-desktop.exe").write_bytes(
        b"MZbefore" + prefix + b"NSS\xc0\x00after"
    )
    (installed / "uninstall.exe").write_bytes(b"uninstaller")

    inventory = _inventory(
        staged,
        _make_installer(tmp_path / "artifacts"),
        installed=installed,
    )

    staged_entry = next(
        item
        for item in inventory["payload"]["staged"]["files"]
        if item["path"] == "scriber-desktop.exe"
    )
    installed_entry = next(
        item
        for item in inventory["payload"]["installed"]["files"]
        if item["path"] == "scriber-desktop.exe"
    )
    assert staged_entry["sha256"] != installed_entry["sha256"]
    assert staged_entry["semanticSha256"] == installed_entry["semanticSha256"]
    assert inventory["payload"]["stagedInstalledParity"]["ok"] is True


def test_tauri_bundle_marker_normalization_is_fail_closed() -> None:
    marker = load_component_map(COMPONENT_MAP)[0]["semanticNormalization"][
        "tauriBundleTypeMarker"
    ]
    prefix = marker["prefix"].encode("ascii")

    staged = b"MZbefore" + prefix + b"UNK\xc0\x00after"
    installed = b"MZbefore" + prefix + b"NSS\xc0\x00after"
    assert _normalize_tauri_bundle_type(staged, marker) == _normalize_tauri_bundle_type(
        installed, marker
    )
    assert _normalize_tauri_bundle_type(staged, marker) != _normalize_tauri_bundle_type(
        installed.replace(b"after", b"other"), marker
    )

    with pytest.raises(InventoryError, match="unsupported bundle type"):
        _normalize_tauri_bundle_type(b"MZ" + prefix + b"BAD\xc0\x00", marker)
    with pytest.raises(InventoryError, match="multiple bundle-type markers"):
        _normalize_tauri_bundle_type(staged + prefix + b"UNK\xc0\x00", marker)
    with pytest.raises(InventoryError, match="no bundle-type marker"):
        _normalize_tauri_bundle_type(b"MZwithout-marker", marker)


def test_semantic_metadata_normalization_is_narrow_and_schema_validated(
    minimal_pyinstaller_payload: Path,
) -> None:
    backend_exe = minimal_pyinstaller_payload / "backend" / "scriber-backend.exe"
    first = _sidecar_metadata(backend_exe)
    volatile_change = _sidecar_metadata(
        backend_exe,
        generated_at="2026-07-18T11:22:33Z",
        root="D:/other/agent",
        cache_hit=True,
    )
    meaningful_change = _sidecar_metadata(backend_exe, stable_marker="changed")

    first_bytes = _normalize_sidecar_build_metadata(json.dumps(first).encode())
    volatile_bytes = _normalize_sidecar_build_metadata(
        json.dumps(volatile_change).encode()
    )
    meaningful_bytes = _normalize_sidecar_build_metadata(
        json.dumps(meaningful_change).encode()
    )

    assert first_bytes == volatile_bytes
    assert first_bytes != meaningful_bytes
    invalid = copy.deepcopy(first)
    invalid["phases"][0]["durationMs"] = "fast"
    with pytest.raises(InventoryError, match="durationMs"):
        _normalize_sidecar_build_metadata(json.dumps(invalid).encode())


def test_semantic_metadata_accepts_an_explicitly_disabled_sidecar_cache(
    minimal_pyinstaller_payload: Path,
) -> None:
    backend_exe = minimal_pyinstaller_payload / "backend" / "scriber-backend.exe"
    metadata = _sidecar_metadata(backend_exe)
    metadata["cache"] = {"enabled": False, "hit": False, "key": ""}

    normalized = json.loads(
        _normalize_sidecar_build_metadata(json.dumps(metadata).encode())
    )

    assert normalized["cache"] == {"enabled": False, "hit": False, "key": ""}

    invalid_hit = copy.deepcopy(metadata)
    invalid_hit["cache"]["hit"] = True
    with pytest.raises(InventoryError, match="cache.hit cannot be true"):
        _normalize_sidecar_build_metadata(json.dumps(invalid_hit).encode())

    invalid_enabled_key = copy.deepcopy(metadata)
    invalid_enabled_key["cache"] = {"enabled": True, "hit": False, "key": ""}
    with pytest.raises(InventoryError, match="cache.key"):
        _normalize_sidecar_build_metadata(json.dumps(invalid_enabled_key).encode())


def test_tree_exact_hash_changes_but_semantic_hash_ignores_only_allowlisted_metadata(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    shutil.copytree(minimal_pyinstaller_payload, first_root)
    shutil.copytree(minimal_pyinstaller_payload, second_root)
    second_backend = second_root / "backend" / "scriber-backend.exe"
    (second_root / "backend" / "sidecar-build-metadata.json").write_text(
        json.dumps(
            _sidecar_metadata(
                second_backend,
                generated_at="2026-07-19T00:00:00Z",
                root="E:/fresh/build",
                cache_hit=True,
            )
        ),
        encoding="utf-8",
    )
    component_map, _ = load_component_map(COMPONENT_MAP)
    first_backend = inspect_pyinstaller_executable(
        first_root / "backend" / "scriber-backend.exe",
        component_map=component_map,
    )
    second_backend_attribution = inspect_pyinstaller_executable(
        second_backend,
        component_map=component_map,
    )

    first = _build_tree_inventory(
        first_root,
        component_map=component_map,
        backend_relative_path="backend/scriber-backend.exe",
        backend_attribution=first_backend,
    )
    second = _build_tree_inventory(
        second_root,
        component_map=component_map,
        backend_relative_path="backend/scriber-backend.exe",
        backend_attribution=second_backend_attribution,
    )

    assert first["exactTreeSha256"] != second["exactTreeSha256"]
    assert first["semanticTreeSha256"] == second["semanticTreeSha256"]
    assert first["fileListSha256"] == second["fileListSha256"]


def test_artifact_directory_selects_only_one_exact_versioned_nsis_setup(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    exact = _make_installer(artifacts)
    (artifacts / "Scriber_9.8.7_x64-setup.exe.sig").write_bytes(b"sig")
    (artifacts / "Scriber_9.8.6_x64-setup.exe").write_bytes(b"old")

    assert (
        select_installer(
            product_version="9.8.7",
            installer=None,
            artifact_dir=artifacts,
        )
        == exact.resolve()
    )

    duplicate = artifacts / "nested" / exact.name
    duplicate.parent.mkdir()
    duplicate.write_bytes(b"duplicate")
    with pytest.raises(InventoryError, match="multiple exact"):
        select_installer(
            product_version="9.8.7",
            installer=None,
            artifact_dir=artifacts,
        )


def test_installer_selection_rejects_symlink_entries_before_resolving(
    tmp_path: Path,
) -> None:
    real = _make_installer(tmp_path / "real")
    explicit_link = tmp_path / "explicit" / real.name
    _make_symlink_or_skip(explicit_link, real)
    with pytest.raises(InventoryError, match="Installer must not be a symlink"):
        select_installer(
            product_version="9.8.7",
            installer=explicit_link,
            artifact_dir=None,
        )

    artifact_link = tmp_path / "artifact-dir" / "nested" / real.name
    _make_symlink_or_skip(artifact_link, real)
    with pytest.raises(InventoryError, match="Installer artifact must not be a symlink"):
        select_installer(
            product_version="9.8.7",
            installer=None,
            artifact_dir=tmp_path / "artifact-dir",
        )


def test_physical_component_map_rejects_multiple_assignments(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    payload = json.loads(COMPONENT_MAP.read_text(encoding="utf-8"))
    legal = next(item for item in payload["components"] if item["id"] == "legal-notices")
    legal["exactPaths"].append("scriber-desktop.exe")
    broken_map = tmp_path / "overlap.json"
    broken_map.write_text(json.dumps(payload), encoding="utf-8")
    component_map, _ = load_component_map(broken_map)
    backend = inspect_pyinstaller_executable(
        minimal_pyinstaller_payload / "backend" / "scriber-backend.exe",
        component_map=component_map,
    )

    with pytest.raises(InventoryError, match="multiplyAssignedObjects"):
        _build_tree_inventory(
            minimal_pyinstaller_payload,
            component_map=component_map,
            backend_relative_path="backend/scriber-backend.exe",
            backend_attribution=backend,
        )


def test_pyz_component_map_rejects_overlapping_prefixes(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    payload = json.loads(COMPONENT_MAP.read_text(encoding="utf-8"))
    payload["pyzPrefixComponents"].extend(
        [
            {"prefix": "overlap_pkg", "component": "provider-sdks"},
            {"prefix": "overlap_pkg.sub", "component": "python-runtime-other"},
        ]
    )
    broken_map = tmp_path / "pyz-overlap.json"
    broken_map.write_text(json.dumps(payload), encoding="utf-8")
    component_map, _ = load_component_map(broken_map)

    with pytest.raises(InventoryError, match="multiplyAssignedPyzModules"):
        inspect_pyinstaller_executable(
            minimal_pyinstaller_payload / "backend" / "scriber-backend.exe",
            component_map=component_map,
        )


def test_inventory_cli_writes_the_canonical_inventory_contract(
    minimal_pyinstaller_payload: Path,
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    _make_installer(artifacts)
    output = tmp_path / "inventory.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/installer_research.py",
            "inventory",
            "--staged-root",
            str(minimal_pyinstaller_payload),
            "--run-id",
            RUN_ID,
            "--source-commit",
            SOURCE_COMMIT,
            "--replica-id",
            "packet-cli-test",
            "--build-root-sha256",
            build_root_identity_sha256(minimal_pyinstaller_payload),
            "--backend-exe",
            str(minimal_pyinstaller_payload / "backend" / "scriber-backend.exe"),
            "--component-map",
            str(COMPONENT_MAP),
            "--artifact-dir",
            str(artifacts),
            "--compression",
            "bzip2",
            "--toolchain-hash",
            TOOLCHAIN_HASH,
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout)
    inventory = json.loads(output.read_text(encoding="utf-8"))
    assert summary["ok"] is True
    assert inventory["inventoryContract"] == "InstallerResearchInventoryV1"
    assert inventory["schemaVersion"] == 1
    assert inventory["runId"] == RUN_ID
    assert inventory["sourceCommit"] == SOURCE_COMMIT
    assert inventory["buildProvenance"] == {
        "replicaId": "packet-cli-test",
        "buildRootSha256": build_root_identity_sha256(minimal_pyinstaller_payload),
    }
    assert inventory["payload"]["staged"]["componentBytesSum"] == inventory["payload"][
        "staged"
    ]["totalBytes"]
