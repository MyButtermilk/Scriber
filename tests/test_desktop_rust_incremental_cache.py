from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
KEY_SCRIPT = REPO_ROOT / "scripts/ci/write_desktop_rust_incremental_cache_key.ps1"
SYNC_SCRIPT = REPO_ROOT / "scripts/ci/sync_desktop_rust_incremental_cache.ps1"
CONTRACT_PATH = REPO_ROOT / "packaging/desktop-rust-incremental-cache-contract.json"
GENERATION = "scriber-desktop-rust-incremental-v1"


def _powershell_51() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is required for the Desktop incremental-cache contract")
    return executable


def _read_outputs(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, value = line.split("=", 1)
            values[name] = value
    return values


def _invoke_key_writer(
    key_dir: Path,
    *,
    git_ref: str = "refs/heads/codex/desktop-incremental-canary",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = key_dir.parent / f"key-output-{uuid.uuid4().hex}.txt"
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(output)
    result = subprocess.run(
        [
            _powershell_51(),
            "-NoProfile",
            "-File",
            str(KEY_SCRIPT),
            "-CacheKeyDir",
            str(key_dir.relative_to(REPO_ROOT)),
            "-GitRef",
            git_ref,
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, _read_outputs(output)


def _invoke_sync(
    fixture_root: Path,
    mode: str,
    *,
    current_key: str = "a" * 64,
    dependency_key: str = "b" * 64,
    ref_key: str = "c" * 64,
    matched_key: str = "",
    cache_root: Path | None = None,
    target_dir: Path | None = None,
    timing_path: Path | None = None,
    binary_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    cache_root = cache_root or fixture_root / "cache"
    target_dir = target_dir or fixture_root / "target"
    timing_path = timing_path or fixture_root / "build-timing.json"
    binary_path = binary_path or fixture_root / "scriber-desktop.exe"
    output = fixture_root / f"sync-output-{uuid.uuid4().hex}.txt"
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(output)
    env["GITHUB_SHA"] = "d" * 40
    env["SCRIBER_DESKTOP_INCREMENTAL_CACHE_TEST_MODE"] = "1"
    env["PYTEST_CURRENT_TEST"] = env.get("PYTEST_CURRENT_TEST", "desktop incremental fixture")
    command = [
        _powershell_51(),
        "-NoProfile",
        "-File",
        str(SYNC_SCRIPT),
        "-Mode",
        mode,
        "-CurrentInputKey",
        current_key,
        "-DependencyScopeKey",
        dependency_key,
        "-RefScopeKey",
        ref_key,
        "-CacheRoot",
        str(cache_root.relative_to(REPO_ROOT)),
        "-TargetDir",
        str(target_dir.relative_to(REPO_ROOT)),
        "-BuildTimingPath",
        str(timing_path.relative_to(REPO_ROOT)),
        "-BinaryPath",
        str(binary_path.relative_to(REPO_ROOT)),
        "-SourceCommit",
        "d" * 40,
    ]
    if matched_key:
        command.extend(["-MatchedCacheKey", matched_key])
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, _read_outputs(output)


def _write_build_evidence(fixture_root: Path, *, prebuilt: bool = False) -> None:
    (fixture_root / "scriber-desktop.exe").write_bytes(b"fresh-desktop-build")
    (fixture_root / "build-timing.json").write_text(
        json.dumps(
            {
                "totalDurationMs": 1234,
                "buildMode": {
                    "artifactKind": "installer",
                    "prebuiltTauriApp": prebuilt,
                    "tauriAppBuiltBeforeBundle": not prebuilt,
                    "installerBuilt": True,
                },
            }
        ),
        encoding="utf-8",
    )


def _full_key(input_key: str = "a" * 64) -> str:
    return f"{GENERATION}-Windows-{'c' * 64}-{'b' * 64}-{input_key}"


def _create_directory_junction(link: Path, target: Path) -> None:
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if cmd is None:
        pytest.skip("cmd.exe is required to create the Windows junction fixture")
    result = subprocess.run(
        [cmd, "/d", "/c", "mklink", "/J", str(link), str(target)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _write_rustc_session(
    crate: Path,
    *,
    timestamp: str,
    random: str,
    svh: str,
    files: dict[str, bytes],
) -> tuple[Path, Path]:
    assert re.fullmatch(r"[0-9a-z]{10}", timestamp)
    assert re.fullmatch(r"[0-9a-z]{7}", random)
    assert re.fullmatch(r"[0-9a-z]{25}", svh)
    session = crate / f"s-{timestamp}-{random}-{svh}"
    session.mkdir(parents=True)
    for name, content in files.items():
        (session / name).write_bytes(content)
    lock = crate / f"s-{timestamp}-{random}.lock"
    lock.write_bytes(b"")
    return session, lock


def _tree_inventory(root: Path) -> tuple[dict[str, object], str]:
    inventory: dict[str, object] = {
        "directories": sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        ),
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "length": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(path for path in root.rglob("*") if path.is_file())
        ],
    }
    serialized = json.dumps(
        inventory, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return inventory, hashlib.sha256(serialized).hexdigest()


def _write_legacy_multisession_envelope(cache_root: Path) -> None:
    payload_root = cache_root / "payload"
    directories = sorted(
        path.relative_to(cache_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_dir()
    )
    file_records = []
    for path in sorted(path for path in payload_root.rglob("*") if path.is_file()):
        content = path.read_bytes()
        file_records.append(
            {
                "path": path.relative_to(cache_root).as_posix(),
                "length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    inventory = {
        "apiVersion": "1",
        "directories": directories,
        "files": file_records,
    }
    inventory_path = cache_root / "inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, separators=(",", ":")), encoding="utf-8"
    )
    inventory_bytes = inventory_path.read_bytes()
    crate_names = sorted(path.name for path in payload_root.iterdir() if path.is_dir())
    manifest = {
        "apiVersion": "1",
        "generation": GENERATION,
        "inputKey": "a" * 64,
        "dependencyScopeKey": "b" * 64,
        "refScopeKey": "c" * 64,
        "contractSha256": hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest(),
        "target": "x86_64-pc-windows-msvc",
        "profile": "release",
        "sourceCommit": "d" * 40,
        "buildEvidence": {
            "timingSha256": hashlib.sha256(b"legacy-build-timing").hexdigest(),
            "executableLength": len(b"legacy-desktop-executable"),
            "executableSha256": hashlib.sha256(
                b"legacy-desktop-executable"
            ).hexdigest(),
        },
        "content": {
            "crateDirectoryCount": len(crate_names),
            "crateDirectories": crate_names,
            "directoryCount": len(directories),
            "fileCount": len(file_records),
            "totalBytes": sum(record["length"] for record in file_records),
        },
        "inventory": {
            "length": len(inventory_bytes),
            "sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        },
        "exportedAtUtc": "2026-07-18T00:00:00.0000000Z",
    }
    (cache_root / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )


def test_desktop_incremental_key_is_ref_and_dependency_scoped_without_changing_tauri_output_key() -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-key-{uuid.uuid4().hex}"
    key_dir = fixture_root / "keys"
    key_dir.mkdir(parents=True)
    tauri_manifest = b"input\ttauri\tbaseline\n"
    rust_manifest = b"input\trust\ttoolchain-and-dependencies\n"
    (key_dir / "tauri-app-binary.txt").write_bytes(tauri_manifest)
    (key_dir / "rust-dependencies.txt").write_bytes(rust_manifest)
    contract_original = CONTRACT_PATH.read_bytes()
    try:
        baseline_result, baseline = _invoke_key_writer(key_dir)
        assert baseline_result.returncode == 0, baseline_result.stdout + baseline_result.stderr
        assert baseline["tauri-input-hash"] == hashlib.sha256(tauri_manifest).hexdigest()
        assert baseline["dependency-scope-hash"] == hashlib.sha256(rust_manifest).hexdigest()

        # Desktop Rust, frontend, and binary-producing packaging changes all
        # change the finalized Tauri input. They miss the exact key while the
        # ref+dependency restore prefix remains stable and can offer the prior state.
        for change_class in ("desktop-rust", "frontend", "packaging-input"):
            (key_dir / "tauri-app-binary.txt").write_bytes(
                tauri_manifest + f"input\t{change_class}\tchanged\n".encode()
            )
            changed_result, changed = _invoke_key_writer(key_dir)
            assert changed_result.returncode == 0, changed_result.stdout + changed_result.stderr
            assert changed["exact-input-hash"] != baseline["exact-input-hash"]
            assert changed["ref-scope-hash"] == baseline["ref-scope-hash"]
            assert changed["dependency-scope-hash"] == baseline["dependency-scope-hash"]

        # Audio-only and diarization changes are intentionally absent from the
        # exact Tauri manifest, so they cannot contaminate this Desktop cache.
        (key_dir / "tauri-app-binary.txt").write_bytes(tauri_manifest)
        isolated_result, isolated = _invoke_key_writer(key_dir)
        assert isolated_result.returncode == 0
        assert isolated["exact-input-hash"] == baseline["exact-input-hash"]

        # The cache-envelope contract invalidates only this cache. It is not an
        # input to tauri-app-binary.txt or the Tauri output contract.
        contract = json.loads(contract_original)
        contract["revision"] = 4
        CONTRACT_PATH.write_text(json.dumps(contract), encoding="utf-8")
        changed_contract_result, changed_contract = _invoke_key_writer(key_dir)
        assert changed_contract_result.returncode != 0
        assert (key_dir / "tauri-app-binary.txt").read_bytes() == tauri_manifest
        tauri_output_contract = json.loads(
            (REPO_ROOT / "packaging/tauri-app-binary-output-contract.json").read_text(
                encoding="utf-8"
            )
        )
        assert tauri_output_contract == {
            "schemaVersion": 1,
            "name": "scriber-tauri-app-binary",
            "revision": 3,
        }
    finally:
        CONTRACT_PATH.write_bytes(contract_original)
        shutil.rmtree(fixture_root, ignore_errors=True)


@pytest.mark.parametrize("git_ref", ["refs/heads/main", "refs/tags/v0.5.99", "refs/heads/feature/../main"])
def test_desktop_incremental_key_rejects_non_feature_or_unsafe_refs(git_ref: str) -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-ref-{uuid.uuid4().hex}"
    key_dir = fixture_root / "keys"
    key_dir.mkdir(parents=True)
    (key_dir / "tauri-app-binary.txt").write_text("tauri\n", encoding="utf-8")
    (key_dir / "rust-dependencies.txt").write_text("rust\n", encoding="utf-8")
    try:
        result, outputs = _invoke_key_writer(key_dir, git_ref=git_ref)
        assert result.returncode != 0
        assert outputs == {}
        assert "non-main branch ref" in (result.stdout + result.stderr)
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


def test_desktop_incremental_envelope_roundtrip_contains_only_desktop_state() -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-roundtrip-{uuid.uuid4().hex}"
    target = fixture_root / "target"
    incremental = target / "release/incremental"
    desktop_bin = incremental / "scriber_desktop-a1b2c3"
    desktop_lib = incremental / "scriber_desktop_lib-c3d4e5"
    audio = incremental / "scriber_audio_sidecar-d4e5f6"
    foreign = incremental / "dependency_crate-aabbcc"
    desktop_bin.mkdir(parents=True)
    desktop_lib.mkdir(parents=True)
    audio.mkdir(parents=True)
    foreign.mkdir(parents=True)
    old_bin, old_bin_lock = _write_rustc_session(
        desktop_bin,
        timestamp="000000000z",
        random="0000001",
        svh="1" * 25,
        files={"dep-graph.bin": b"stale-desktop-bin-graph"},
    )
    current_bin, current_bin_lock = _write_rustc_session(
        desktop_bin,
        timestamp="0000000010",
        random="0000002",
        svh="2" * 25,
        files={"dep-graph.bin": b"desktop-bin-graph"},
    )
    old_lib, old_lib_lock = _write_rustc_session(
        desktop_lib,
        timestamp="0000000003",
        random="0000003",
        svh="3" * 25,
        files={"dep-graph.bin": b"stale-desktop-lib-graph"},
    )
    current_lib, current_lib_lock = _write_rustc_session(
        desktop_lib,
        timestamp="0000000004",
        random="0000004",
        svh="4" * 25,
        files={
            "dep-graph.bin": b"desktop-lib-graph",
            "work-products.bin": b"desktop-lib-products",
            "thin-lto-past-keys.bin": b"desktop-thin-lto-past-keys",
            "abc123.o": b"desktop-object",
            "abc123.bc.z": b"desktop-bitcode",
            "01w6t5u1kedjhmd6zhuedks4j.pre-lto.bc": b"desktop-pre-lto-bitcode",
            "metadata.rmeta": b"desktop-metadata",
        },
    )
    source_hardlink = fixture_root / "cargo-hardlink.bin"
    os.link(current_lib / "dep-graph.bin", source_hardlink)
    assert (current_lib / "dep-graph.bin").stat().st_nlink >= 2
    (audio / "audio.o").write_bytes(b"audio-state")
    (foreign / "dependency.o").write_bytes(b"dependency-state")
    (target / "release/.fingerprint/scriber-desktop-fixture").mkdir(parents=True)
    (target / "release/deps").mkdir(parents=True)
    (target / "release/deps/scriber_desktop.pdb").write_bytes(b"pdb")
    _write_build_evidence(fixture_root)
    binary_hardlink = fixture_root / "scriber-desktop-cargo-hardlink.exe"
    os.link(fixture_root / "scriber-desktop.exe", binary_hardlink)
    assert (fixture_root / "scriber-desktop.exe").stat().st_nlink >= 2
    source_inventory_before, source_tree_sha_before = _tree_inventory(incremental)
    try:
        exported, export_outputs = _invoke_sync(fixture_root, "Export")
        assert exported.returncode == 0, exported.stdout + exported.stderr
        assert export_outputs["staged"] == "true"
        assert export_outputs["source-session-count"] == "4"
        assert export_outputs["staged-session-count"] == "2"
        assert export_outputs["stale-session-count"] == "2"
        assert export_outputs["file-count"] == "10"
        assert int(export_outputs["total-bytes"]) == sum(
            map(
                len,
                (
                    b"desktop-bin-graph",
                    b"desktop-lib-graph",
                    b"desktop-lib-products",
                    b"desktop-thin-lto-past-keys",
                    b"desktop-object",
                    b"desktop-bitcode",
                    b"desktop-pre-lto-bitcode",
                    b"desktop-metadata",
                ),
            )
        )

        cache_root = fixture_root / "cache"
        manifest = json.loads((cache_root / "manifest.json").read_text(encoding="utf-8-sig"))
        inventory = json.loads((cache_root / "inventory.json").read_text(encoding="utf-8-sig"))
        assert set(manifest["content"]["crateDirectories"]) == {
            "scriber_desktop-a1b2c3",
            "scriber_desktop_lib-c3d4e5",
        }
        cached_paths = {record["path"] for record in inventory["files"]}
        bin_prefix = f"payload/{desktop_bin.name}/{current_bin.name}"
        lib_prefix = f"payload/{desktop_lib.name}/{current_lib.name}"
        assert cached_paths == {
            f"payload/{desktop_bin.name}/{current_bin_lock.name}",
            f"{bin_prefix}/dep-graph.bin",
            f"payload/{desktop_lib.name}/{current_lib_lock.name}",
            f"{lib_prefix}/01w6t5u1kedjhmd6zhuedks4j.pre-lto.bc",
            f"{lib_prefix}/abc123.bc.z",
            f"{lib_prefix}/abc123.o",
            f"{lib_prefix}/dep-graph.bin",
            f"{lib_prefix}/metadata.rmeta",
            f"{lib_prefix}/thin-lto-past-keys.bin",
            f"{lib_prefix}/work-products.bin",
        }
        assert all(path.exists() for path in (old_bin, old_bin_lock, old_lib, old_lib_lock))
        assert all(
            path.exists()
            for path in (current_bin, current_bin_lock, current_lib, current_lib_lock)
        )
        source_inventory_after, source_tree_sha_after = _tree_inventory(incremental)
        assert source_inventory_after == source_inventory_before
        assert source_tree_sha_after == source_tree_sha_before
        assert not any("audio" in path for path in cached_paths)
        assert not any("dependency_crate" in path for path in cached_paths)
        assert not any(path.endswith((".exe", ".pdb")) for path in cached_paths)
        assert (
            cache_root / lib_prefix / "dep-graph.bin"
        ).stat().st_nlink == 1
        first_payload = {
            record["path"]: (record["length"], record["sha256"])
            for record in inventory["files"]
        }

        shutil.rmtree(desktop_bin)
        shutil.rmtree(desktop_lib)
        imported, import_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr
        assert import_outputs["usable"] == "true"
        assert import_outputs["exact-current"] == "true"
        assert (desktop_bin / current_bin.name / "dep-graph.bin").read_bytes() == b"desktop-bin-graph"
        assert (desktop_lib / current_lib.name / "dep-graph.bin").read_bytes() == b"desktop-lib-graph"
        assert (desktop_lib / current_lib.name / "work-products.bin").read_bytes() == b"desktop-lib-products"
        assert (desktop_lib / current_lib.name / "thin-lto-past-keys.bin").read_bytes() == b"desktop-thin-lto-past-keys"
        assert (desktop_lib / current_lib.name / "01w6t5u1kedjhmd6zhuedks4j.pre-lto.bc").read_bytes() == b"desktop-pre-lto-bitcode"
        assert (audio / "audio.o").read_bytes() == b"audio-state"
        assert (foreign / "dependency.o").read_bytes() == b"dependency-state"

        # A source-changing Tauri input gets a new exact key, but the same
        # ref+dependency prefix can safely offer the predecessor to rustc.
        shutil.rmtree(desktop_bin)
        shutil.rmtree(desktop_lib)
        prefixed, prefix_outputs = _invoke_sync(
            fixture_root,
            "Import",
            current_key="e" * 64,
            matched_key=_full_key(),
        )
        assert prefixed.returncode == 0, prefixed.stdout + prefixed.stderr
        assert prefix_outputs["usable"] == "true"
        assert prefix_outputs["exact-current"] == "false"
        assert (desktop_bin / current_bin.name / "dep-graph.bin").read_bytes() == b"desktop-bin-graph"
        assert (desktop_lib / current_lib.name / "dep-graph.bin").read_bytes() == b"desktop-lib-graph"

        reexported, reexport_outputs = _invoke_sync(
            fixture_root,
            "Export",
            current_key="e" * 64,
        )
        assert reexported.returncode == 0, reexported.stdout + reexported.stderr
        assert reexport_outputs["staged"] == "true"
        assert reexport_outputs["source-session-count"] == "2"
        assert reexport_outputs["staged-session-count"] == "2"
        assert reexport_outputs["stale-session-count"] == "0"
        assert reexport_outputs["file-count"] == export_outputs["file-count"]
        assert reexport_outputs["total-bytes"] == export_outputs["total-bytes"]
        reexport_inventory = json.loads(
            (cache_root / "inventory.json").read_text(encoding="utf-8-sig")
        )
        assert {
            record["path"]: (record["length"], record["sha256"])
            for record in reexport_inventory["files"]
        } == first_payload
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


def test_legacy_multisession_import_remains_usable_and_invalid_exports_fail_closed() -> None:
    legacy_root = (
        REPO_ROOT
        / "build"
        / f"test-desktop-incremental-legacy-{uuid.uuid4().hex}"
    )
    legacy_cache = legacy_root / "cache"
    legacy_crate = legacy_cache / "payload/scriber_desktop_lib-feed01"
    old_session, old_lock = _write_rustc_session(
        legacy_crate,
        timestamp="0000000001",
        random="0000001",
        svh="1" * 25,
        files={"query-cache.bin": b"legacy-stale-query-cache"},
    )
    current_session, current_lock = _write_rustc_session(
        legacy_crate,
        timestamp="0000000002",
        random="0000002",
        svh="2" * 25,
        files={
            "dep-graph.bin": b"legacy-current-dep-graph",
            "query-cache.bin": b"legacy-current-query-cache",
        },
    )
    _write_legacy_multisession_envelope(legacy_cache)
    try:
        imported, outputs = _invoke_sync(
            legacy_root,
            "Import",
            current_key="e" * 64,
            matched_key=_full_key(),
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr
        assert outputs["usable"] == "true"
        assert outputs["exact-current"] == "false"
        restored_crate = (
            legacy_root / "target/release/incremental/scriber_desktop_lib-feed01"
        )
        assert (
            restored_crate / old_session.name / "query-cache.bin"
        ).read_bytes() == b"legacy-stale-query-cache"
        assert (
            restored_crate / current_session.name / "query-cache.bin"
        ).read_bytes() == b"legacy-current-query-cache"
        assert (restored_crate / old_lock.name).is_file()
        assert (restored_crate / current_lock.name).is_file()
    finally:
        shutil.rmtree(legacy_root, ignore_errors=True)

    cases = {
        "working": "active rustc *-working session",
        "missing-lock": "missing its rustc-owned lock file",
        "extra-lock": "lock without one exact finalized session",
        "non-empty-lock": "non-empty rustc session lock file",
        "near-miss": "invalid or near-miss rustc session directory",
        "ambiguous-newest": "ambiguous finalized rustc sessions",
        "locked": "active or locked",
    }
    for case, expected_error in cases.items():
        fixture_root = (
            REPO_ROOT
            / "build"
            / f"test-desktop-incremental-layout-{case}-{uuid.uuid4().hex}"
        )
        desktop = (
            fixture_root / "target/release/incremental/scriber_desktop_lib-feed01"
        )
        desktop.mkdir(parents=True)
        _, base_lock = _write_rustc_session(
            desktop,
            timestamp="0000000002",
            random="0000002",
            svh="2" * 25,
            files={"query-cache.bin": b"trusted-query-cache"},
        )
        if case == "working":
            (desktop / "s-0000000003-0000003-working").mkdir()
            (desktop / "s-0000000003-0000003.lock").write_bytes(b"")
        elif case == "missing-lock":
            base_lock.unlink()
        elif case == "extra-lock":
            (desktop / "s-0000000003-0000003.lock").write_bytes(b"")
        elif case == "non-empty-lock":
            base_lock.write_bytes(b"x")
        elif case == "near-miss":
            (desktop / f"s-0000000003-0000003-{'3' * 24}").mkdir()
        elif case == "ambiguous-newest":
            _write_rustc_session(
                desktop,
                timestamp="0000000002",
                random="0000003",
                svh="3" * 25,
                files={"query-cache.bin": b"ambiguous-query-cache"},
            )
        _write_build_evidence(fixture_root)
        source_inventory, source_tree_sha = _tree_inventory(desktop)
        holder: subprocess.Popen[str] | None = None
        try:
            if case == "locked":
                holder_env = os.environ.copy()
                holder_env["SCRIBER_TEST_RUSTC_LOCK_PATH"] = str(base_lock)
                holder = subprocess.Popen(
                    [
                        _powershell_51(),
                        "-NoProfile",
                        "-Command",
                        (
                            "$share = [System.IO.FileShare]::Read -bor "
                            "[System.IO.FileShare]::Write -bor "
                            "[System.IO.FileShare]::Delete; "
                            "$stream = [System.IO.File]::Open("
                            "$env:SCRIBER_TEST_RUSTC_LOCK_PATH, "
                            "[System.IO.FileMode]::Open, "
                            "[System.IO.FileAccess]::ReadWrite, $share); "
                            "$stream.Lock(0, 1); "
                            "[Console]::Out.WriteLine('READY'); "
                            "[Console]::Out.Flush(); "
                            "[void][Console]::ReadLine(); "
                            "$stream.Dispose()"
                        ),
                    ],
                    cwd=REPO_ROOT,
                    env=holder_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert holder.stdout is not None
                assert holder.stdout.readline().strip() == "READY"

            rejected, reject_outputs = _invoke_sync(fixture_root, "Export")
            if holder is not None:
                assert holder.stdin is not None
                holder.stdin.write("\n")
                holder.stdin.flush()
                _, holder_stderr = holder.communicate(timeout=10)
                assert holder.returncode == 0, holder_stderr
                holder = None
            assert rejected.returncode != 0
            assert reject_outputs["staged"] == "false"
            assert expected_error in (rejected.stdout + rejected.stderr)
            assert not (fixture_root / "cache").exists()
            rejected_inventory, rejected_tree_sha = _tree_inventory(desktop)
            assert rejected_inventory == source_inventory
            assert rejected_tree_sha == source_tree_sha
            if case == "locked":
                assert base_lock.stat().st_size == 0
        finally:
            if holder is not None:
                assert holder.stdin is not None
                holder.stdin.write("\n")
                holder.stdin.flush()
                _, holder_stderr = holder.communicate(timeout=10)
                assert holder.returncode == 0, holder_stderr
            shutil.rmtree(fixture_root, ignore_errors=True)


def test_desktop_incremental_import_rejects_miss_tamper_wrong_key_and_foreign_paths() -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-reject-{uuid.uuid4().hex}"
    target = fixture_root / "target"
    desktop = target / "release/incremental/scriber_desktop-feed01"
    desktop.mkdir(parents=True)
    session, _ = _write_rustc_session(
        desktop,
        timestamp="0000000001",
        random="0000001",
        svh="1" * 25,
        files={"query-cache.bin": b"trusted-query-cache"},
    )
    _write_build_evidence(fixture_root)
    try:
        exported, outputs = _invoke_sync(fixture_root, "Export")
        assert exported.returncode == 0, exported.stdout + exported.stderr
        assert outputs["staged"] == "true"
        cache_root = fixture_root / "cache"
        pristine = fixture_root / "pristine"
        shutil.copytree(cache_root, pristine)

        shutil.rmtree(cache_root)
        shutil.rmtree(desktop)
        missed, miss_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert missed.returncode == 0
        assert miss_outputs["usable"] == "false"
        assert not desktop.exists()

        def restore() -> None:
            shutil.rmtree(cache_root, ignore_errors=True)
            shutil.copytree(pristine, cache_root)
            shutil.rmtree(desktop, ignore_errors=True)

        restore()
        wrong_key, wrong_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key("f" * 64)
        )
        assert wrong_key.returncode == 0
        assert wrong_outputs["usable"] == "false"
        assert not desktop.exists()

        restore()
        cached_session = cache_root / "payload/scriber_desktop-feed01" / session.name
        payload = cached_session / "query-cache.bin"
        payload.write_bytes(b"tampered-query-cache")
        desktop.mkdir(parents=True)
        (desktop / "existing-state.bin").write_bytes(b"untouched-existing-state")
        tampered, tamper_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert tampered.returncode == 0
        assert tamper_outputs["usable"] == "false"
        assert (desktop / "existing-state.bin").read_bytes() == b"untouched-existing-state"

        restore()
        (cached_session / "foreign.exe").write_bytes(b"foreign")
        foreign, foreign_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert foreign.returncode == 0
        assert foreign_outputs["usable"] == "false"
        assert not desktop.exists()

        restore()
        native_bytes = b"attested-but-forbidden-native-library"
        native_relative = f"payload/scriber_desktop-feed01/{session.name}/finished.rlib"
        native_path = cache_root / Path(native_relative)
        native_path.write_bytes(native_bytes)
        inventory_path = cache_root / "inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
        inventory["files"].append(
            {
                "path": native_relative,
                "length": len(native_bytes),
                "sha256": hashlib.sha256(native_bytes).hexdigest(),
            }
        )
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        manifest_path = cache_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest["content"]["fileCount"] += 1
        manifest["content"]["totalBytes"] += len(native_bytes)
        manifest["inventory"]["length"] = inventory_path.stat().st_size
        manifest["inventory"]["sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        forged, forged_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert forged.returncode == 0
        assert forged_outputs["usable"] == "false"
        assert "not an allowlisted rustc incremental artifact" in (
            forged.stdout + forged.stderr
        )
        assert not desktop.exists()

        restore()
        manifest_path = cache_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest["content"]["totalBytes"] = 536870913
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        oversized, oversized_outputs = _invoke_sync(
            fixture_root, "Import", matched_key=_full_key()
        )
        assert oversized.returncode == 0
        assert oversized_outputs["usable"] == "false"
        assert not desktop.exists()

        restore()
        link = cache_root / "payload/scriber_desktop-feed01" / session.name / "link.bin"
        try:
            link.symlink_to(fixture_root / "scriber-desktop.exe")
        except OSError:
            pass
        else:
            linked, linked_outputs = _invoke_sync(
                fixture_root, "Import", matched_key=_full_key()
            )
            assert linked.returncode == 0
            assert linked_outputs["usable"] == "false"
            assert not desktop.exists()

        restore()
        payload = cache_root / "payload/scriber_desktop-feed01" / session.name / "query-cache.bin"
        payload_hardlink = fixture_root / "cached-payload-hardlink.bin"
        os.link(payload, payload_hardlink)
        assert payload.stat().st_nlink >= 2
        try:
            hardlinked, hardlinked_outputs = _invoke_sync(
                fixture_root, "Import", matched_key=_full_key()
            )
        finally:
            payload_hardlink.unlink(missing_ok=True)
        assert hardlinked.returncode == 0
        assert hardlinked_outputs["usable"] == "false"
        assert "hard link" in (hardlinked.stdout + hardlinked.stderr).lower()
        assert not desktop.exists()
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


@pytest.mark.parametrize(
    "native_suffix",
    (".exe", ".pdb", ".dll", ".lib", ".rlib", ".a", ".so", ".dylib", ".sys", ".msi", ".wasm"),
)
def test_desktop_incremental_export_rejects_finished_native_artifacts(
    native_suffix: str,
) -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-native-{uuid.uuid4().hex}"
    desktop = fixture_root / "target/release/incremental/scriber_desktop_lib-deadbe"
    desktop.mkdir(parents=True)
    _write_rustc_session(
        desktop,
        timestamp="0000000001",
        random="0000001",
        svh="1" * 25,
        files={
            "query-cache.bin": b"legitimate-incremental-state",
            f"finished{native_suffix}": b"finished-native-artifact",
        },
    )
    _write_build_evidence(fixture_root)
    try:
        rejected, outputs = _invoke_sync(fixture_root, "Export")
        assert rejected.returncode != 0
        assert outputs["staged"] == "false"
        assert "not an allowlisted rustc incremental artifact" in (
            rejected.stdout + rejected.stderr
        )
        assert not (fixture_root / "cache").exists()
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


def test_unsafe_prefix_import_then_optional_reexport_is_a_nonfatal_skip() -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-reexport-{uuid.uuid4().hex}"
    desktop = fixture_root / "target/release/incremental/scriber_desktop_lib-feed01"
    desktop.mkdir(parents=True)
    session, _ = _write_rustc_session(
        desktop,
        timestamp="0000000001",
        random="0000001",
        svh="1" * 25,
        files={"query-cache.bin": b"trusted-query-cache"},
    )
    _write_build_evidence(fixture_root)
    junction: Path | None = None
    try:
        exported, export_outputs = _invoke_sync(fixture_root, "Export")
        assert exported.returncode == 0, exported.stdout + exported.stderr
        assert export_outputs["staged"] == "true"

        external = fixture_root / "must-not-be-traversed"
        external.mkdir()
        sentinel = external / "sentinel.bin"
        sentinel.write_bytes(b"external-sentinel")
        junction = fixture_root / "cache/payload/scriber_desktop_lib-feed01/escape"
        _create_directory_junction(junction, external)

        rejected, reject_outputs = _invoke_sync(
            fixture_root,
            "Import",
            current_key="e" * 64,
            matched_key=_full_key(),
        )
        assert rejected.returncode == 0
        assert reject_outputs["usable"] == "false"
        assert sentinel.read_bytes() == b"external-sentinel"
        assert (session / "query-cache.bin").read_bytes() == b"trusted-query-cache"

        restaged, restage_outputs = _invoke_sync(
            fixture_root,
            "Export",
            current_key="e" * 64,
        )
        assert restaged.returncode == 0, restaged.stdout + restaged.stderr
        assert restage_outputs["staged"] == "false"
        assert restage_outputs["skip-reason"] == "unsafe-existing-envelope"
        assert "Skipping optional Desktop Rust incremental re-export" in (
            restaged.stdout + restaged.stderr
        )
        assert junction.exists()
        assert sentinel.read_bytes() == b"external-sentinel"
        manifest = json.loads(
            (fixture_root / "cache/manifest.json").read_text(encoding="utf-8-sig")
        )
        assert manifest["inputKey"] == "a" * 64
    finally:
        if junction is not None and os.path.lexists(junction):
            os.rmdir(junction)
        shutil.rmtree(fixture_root, ignore_errors=True)


def test_desktop_incremental_export_requires_a_real_successful_desktop_build() -> None:
    fixture_root = REPO_ROOT / "build" / f"test-desktop-incremental-build-gate-{uuid.uuid4().hex}"
    desktop = fixture_root / "target/release/incremental/scriber_desktop-deadbe"
    desktop.mkdir(parents=True)
    _write_rustc_session(
        desktop,
        timestamp="0000000001",
        random="0000001",
        svh="1" * 25,
        files={"dep-graph.bin": b"graph"},
    )
    _write_build_evidence(fixture_root, prebuilt=True)
    try:
        rejected, outputs = _invoke_sync(fixture_root, "Export")
        assert rejected.returncode != 0
        assert outputs["staged"] == "false"
        assert "fresh Desktop app build" in (rejected.stdout + rejected.stderr)
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


def test_contract_matches_the_desktop_bin_and_app_library_from_cargo_metadata_only() -> None:
    cargo = shutil.which("cargo.exe") or shutil.which("cargo")
    if cargo is None:
        pytest.skip("Cargo is required to verify the Desktop target boundary")

    result = subprocess.run(
        [
            cargo,
            "metadata",
            "--manifest-path",
            str(REPO_ROOT / "Frontend/src-tauri/Cargo.toml"),
            "--format-version",
            "1",
            "--no-deps",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads(result.stdout)
    package = next(item for item in metadata["packages"] if item["name"] == "scriber-desktop")
    targets = {(target["name"], tuple(target["kind"])) for target in package["targets"]}
    assert ("scriber-desktop", ("bin",)) in targets
    assert ("scriber_desktop_lib", ("rlib",)) in targets
    assert ("scriber-audio-sidecar", ("bin",)) in targets

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    allowed = re.compile(contract["crateDirectoryPattern"])
    assert allowed.fullmatch("scriber_desktop-a1b2c3")
    assert allowed.fullmatch("scriber_desktop_lib-c3d4e5")
    assert not allowed.fullmatch("scriber_audio_sidecar-d4e5f6")
    assert not allowed.fullmatch("dependency_crate-aabbcc")

    file_patterns = [re.compile(pattern) for pattern in contract["incrementalFileNamePatterns"]]

    def file_allowed(name: str) -> bool:
        return any(pattern.fullmatch(name) for pattern in file_patterns)

    for name in (
        "dep-graph.bin",
        "query-cache.bin",
        "work-products.bin",
        "thin-lto-past-keys.bin",
        "metadata.rmeta",
        "abc123.o",
        "abc123.bc.z",
        "01w6t5u1kedjhmd6zhuedks4j.pre-lto.bc",
        "s-fixture-0001.lock",
    ):
        assert file_allowed(name)
    for name in (
        "finished.exe",
        "finished.pdb",
        "finished.dll",
        "finished.lib",
        "finished.rlib",
        "abc123.bc",
        "abc123.lto.bc",
        "ABC123.pre-lto.bc",
        ".pre-lto.bc",
        "thin-lto-future-keys.bin",
        "abc123.post-lto.bc",
        "abc123.pre-lto.bc.z",
        "abc123.pre_lto.bc",
        "abc123.pre-lto.bc.exe",
    ):
        assert not file_allowed(name)


def test_release_workflow_uses_only_the_bounded_feature_ref_envelope() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release-windows.yml").read_text(
        encoding="utf-8"
    )
    sync_script = SYNC_SCRIPT.read_text(encoding="utf-8")
    pruner = (REPO_ROOT / "scripts/ci/prune_rust_dependency_cache.ps1").read_text(
        encoding="utf-8"
    )

    assert "SCRIBER_SAVE_REF_LOCAL_DESKTOP_INCREMENTAL_CACHE" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.ref != 'refs/heads/main'" in workflow
    assert "github.event.inputs.refresh_release_cache_artifacts != 'true'" in workflow
    assert "path: build/desktop-rust-incremental-cache" in workflow
    assert "key: scriber-desktop-rust-incremental-v1-${{ runner.os }}-" in workflow
    assert "restore-keys: |" in workflow
    assert "steps.desktop-rust-incremental-key.outputs.ref-scope-hash" in workflow
    assert "steps.desktop-rust-incremental-key.outputs.dependency-scope-hash" in workflow
    rust_preparation_index = workflow.index("Select Rust build preparation")
    dependency_prune_index = workflow.index(
        "Remove app outputs from restored Rust dependency state"
    )
    desktop_identity_index = workflow.index(
        "Compute ref-local Desktop Rust incremental cache identity"
    )
    desktop_restore_index = workflow.index(
        "Restore ref-local Desktop Rust incremental envelope"
    )
    desktop_import_index = workflow.index(
        "Import ref-local Desktop Rust incremental envelope"
    )
    assert (
        rust_preparation_index
        < dependency_prune_index
        < desktop_identity_index
        < desktop_restore_index
        < desktop_import_index
        < workflow.index("Build Windows installer")
    )
    desktop_steps = (
        "Compute ref-local Desktop Rust incremental cache identity",
        "Restore ref-local Desktop Rust incremental envelope",
        "Import ref-local Desktop Rust incremental envelope",
        "Export bounded Desktop Rust incremental envelope",
        "Save bounded Desktop Rust incremental envelope",
    )
    desktop_blocks = {}
    for name in desktop_steps:
        block = workflow.split(f"- name: {name}", 1)[1].split(
            "\n      - name:", 1
        )[0]
        desktop_blocks[name] = block
        assert (
            "steps.rust-preparation.outputs.main-cargo-required == 'true'" in block
        )
        assert "steps.rust-preparation.outputs.required == 'true'" not in block
        assert "steps.frontend-preparation.outputs.use-prebuilt != 'true'" in block
    assert workflow.index("Build Windows installer") < workflow.index(
        "Export bounded Desktop Rust incremental envelope"
    ) < workflow.index("Save bounded Desktop Rust incremental envelope")
    assert workflow.index("Save exact Tauri app binary") < workflow.index(
        "Write passive Tauri cache-promotion evidence"
    ) < workflow.index("Upload passive Tauri cache-promotion evidence")
    save_block = desktop_blocks["Save bounded Desktop Rust incremental envelope"]
    assert "build/desktop-rust-incremental-cache" in save_block
    assert "target/release/incremental" not in save_block
    assert "steps.desktop-rust-incremental-export.outputs.staged == 'true'" in save_block
    assert 'Root = "incremental"; Patterns = @("scriber_desktop*", "scriber_audio_sidecar*")' in pruner

    for forbidden in (
        ".fingerprint",
        "release\\build",
        "release\\deps",
        "scriber_audio_sidecar",
        "native\\scriber-diarization-sidecar",
    ):
        assert forbidden not in sync_script
    assert "Copy-Item -LiteralPath $resolvedBinaryPath" not in sync_script
    assert "^scriber_desktop(?:_lib)?-[0-9A-Za-z_-]+$" in sync_script
    assert "incrementalFileNamePatterns" in sync_script
    assert "Assert-RustIncrementalFileName" in sync_script
    assert "-EnforceIncrementalFileNames" in sync_script
    assert "ReparsePoint" in sync_script
    assert "$sourceDirectoryCount += 1 + @($sourceTree.directories).Count" in sync_script
    assert "$sourceFileCount += @($sourceTree.files).Count" in sync_script
    assert "$sourceTotalBytes += [int64]$sourceTree.totalBytes" in sync_script
    assert "Combined Desktop Rust incremental source exceeds" in sync_script
    assert "RustcFinalizedSessionDirectoryPattern" in sync_script
    assert "Open-DesktopSessionLocks" in sync_script
    assert "ValidatedEmptySessionLockPaths" in sync_script
    assert "Copy-Item -LiteralPath $snapshot.current.path" in sync_script
    assert "[System.IO.FileMode]::CreateNew" in sync_script
    assert "Copy-Item -LiteralPath $directory.FullName" not in sync_script
    assert "maxBytes" in sync_script
    assert "inventory" in sync_script
    assert "prebuiltTauriApp" in sync_script
    assert "tauriAppBuiltBeforeBundle" in sync_script
    assert "unsafe-existing-envelope" in sync_script
    assert "Skipping optional Desktop Rust incremental re-export" in sync_script
    assert "Invoke-Expression" not in sync_script
    assert "-EncodedCommand" not in sync_script
