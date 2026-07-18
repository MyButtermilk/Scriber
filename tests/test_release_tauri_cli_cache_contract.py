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
SCRIPT = REPO_ROOT / "scripts/ci/sync_tauri_app_binary_cache.ps1"
CONTRACT_PATH = REPO_ROOT / "packaging/tauri-cli-cache-contract.json"
PACKAGE_LOCK_PATH = REPO_ROOT / "Frontend/package-lock.json"
EXPECTED_VERSION = "2.11.3"
EXPECTED_VERSION_OUTPUT = "tauri-cli 2.11.3"
EXPECTED_PACKAGES = {
    "@tauri-apps/cli": (
        EXPECTED_VERSION,
        "sha512-EElQe8z8uD7Pi5++tJ/UfEwWuK08rd3oCDYdeIbJAb6pZRrxlqmoF5gh5H5YvzmUPhS4IRCaLSsQhvWkrfK+GQ==",
    ),
    "@tauri-apps/cli-win32-x64-msvc": (
        EXPECTED_VERSION,
        "sha512-GlciF75GdbseajOyib2aCHwE3BXIqZ1liGKWLFRvCdN5wm8h8hFssEVKQ/6E+2jsMLg9v7LCTb983YFnn0QSww==",
    ),
}
EXPECTED_FILES = {
    "tauri-cli/node_modules/@tauri-apps/cli/package.json": (
        2455,
        "faf3e54d36401f47119d9dd1386b08aabeffa04280b3289f1414e51679dd131d",
    ),
    "tauri-cli/node_modules/@tauri-apps/cli/tauri.js": (
        1864,
        "0dd6ec63c7c63a993fde20955e291d833c03f3760e63e0ee21e83482f6c0b43a",
    ),
    "tauri-cli/node_modules/@tauri-apps/cli/main.js": (
        450,
        "49df414a16784e3711d5582d55c5c9e537aceb1108c5ecfc6a17cdc2f5259b4d",
    ),
    "tauri-cli/node_modules/@tauri-apps/cli/index.js": (
        24199,
        "92ed83f37d18164f34114c3a411a7c1c4b2c59968d2dcce59c5546eaa43bb759",
    ),
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json": (
        707,
        "2c4b57cbaa6f47f8571fa6f575f28eaef30b7d297cc5f33142dcc63dd5938876",
    ),
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node": (
        15235072,
        "5cb11ab8a694496d3f6a57309a2d073edd876af0676a70b0a4d16c5b34bc6087",
    ),
}
SOURCE_FILES = {
    "tauri-cli/node_modules/@tauri-apps/cli/package.json": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli/package.json",
    "tauri-cli/node_modules/@tauri-apps/cli/tauri.js": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli/tauri.js",
    "tauri-cli/node_modules/@tauri-apps/cli/main.js": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli/main.js",
    "tauri-cli/node_modules/@tauri-apps/cli/index.js": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli/index.js",
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json",
    "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node": REPO_ROOT
    / "Frontend/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node",
}


def _windows_powershell() -> str:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell 5.1 is required for the Tauri CLI cache contract")
    return executable


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("NODE_OPTIONS", "NODE_PATH", "NAPI_RS_NATIVE_LIBRARY_PATH"):
        env.pop(name, None)
    return env


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _read_outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, value = line.split("=", 1)
        values[name] = value
    return values


def _invoke(
    *,
    mode: str = "Import",
    cache_root: Path,
    version: str = "0.5.37",
    binary_path: Path | None = None,
    package_lock_path: Path | None = None,
    contract_path: Path | None = None,
    cli_package_path: Path | None = None,
    cli_platform_package_path: Path | None = None,
    env_changes: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output_path = cache_root.parent / f"github-output-{uuid.uuid4().hex}.txt"
    env = _clean_environment()
    env["GITHUB_OUTPUT"] = str(output_path)
    env["GITHUB_SHA"] = "1" * 40
    if contract_path is not None:
        env["SCRIBER_TAURI_CLI_CACHE_TEST_CONTRACT"] = "1"
    env.update(env_changes or {})
    command = [
        _windows_powershell(),
        "-NoProfile",
        "-File",
        str(SCRIPT),
        "-Mode",
        mode,
        "-CacheKey",
        "a" * 64,
        "-Version",
        version,
        "-CacheRoot",
        _relative(cache_root),
    ]
    if binary_path is not None:
        command.extend(("-BinaryPath", _relative(binary_path)))
    if package_lock_path is not None:
        command.extend(("-PackageLockPath", _relative(package_lock_path)))
    if contract_path is not None:
        command.extend(("-ContractPath", _relative(contract_path)))
    if cli_package_path is not None:
        command.extend(("-TauriCliPackagePath", _relative(cli_package_path)))
    if cli_platform_package_path is not None:
        command.extend(("-TauriCliPlatformPackagePath", _relative(cli_platform_package_path)))
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, _read_outputs(output_path)


def _fixture_root() -> Path:
    root = REPO_ROOT / "build" / f"test-tauri-cli-contract-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    return root


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _create_fixture_cli(
    root: Path, *, version_output: str = EXPECTED_VERSION_OUTPUT
) -> tuple[Path, Path, Path]:
    cli = root / "node_modules/@tauri-apps/cli"
    platform = root / "node_modules/@tauri-apps/cli-win32-x64-msvc"
    cli.mkdir(parents=True)
    platform.mkdir(parents=True)
    (cli / "package.json").write_text(
        json.dumps({"name": "@tauri-apps/cli", "version": EXPECTED_VERSION}), encoding="utf-8"
    )
    (cli / "tauri.js").write_text(f"console.log({json.dumps(version_output)})\n", encoding="utf-8")
    (cli / "main.js").write_text("module.exports = {}\n", encoding="utf-8")
    (cli / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    (platform / "package.json").write_text(
        json.dumps({"name": "@tauri-apps/cli-win32-x64-msvc", "version": EXPECTED_VERSION}),
        encoding="utf-8",
    )
    (platform / "cli.win32-x64-msvc.node").write_bytes(b"fixture-native-module")

    source_by_relative = {
        "tauri-cli/node_modules/@tauri-apps/cli/package.json": cli / "package.json",
        "tauri-cli/node_modules/@tauri-apps/cli/tauri.js": cli / "tauri.js",
        "tauri-cli/node_modules/@tauri-apps/cli/main.js": cli / "main.js",
        "tauri-cli/node_modules/@tauri-apps/cli/index.js": cli / "index.js",
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/package.json": platform
        / "package.json",
        "tauri-cli/node_modules/@tauri-apps/cli-win32-x64-msvc/cli.win32-x64-msvc.node": platform
        / "cli.win32-x64-msvc.node",
    }
    contract = {
        "schemaVersion": 1,
        "name": "scriber-tauri-cli-cache-contract",
        "revision": "tauri-cli-2.11.3-win32-x64-msvc-v1",
        "target": "win32-x64-msvc",
        "version": EXPECTED_VERSION,
        "versionOutput": EXPECTED_VERSION_OUTPUT,
        "entrypoint": "tauri-cli/node_modules/@tauri-apps/cli/tauri.js",
        "packages": [
            {"name": name, "version": version, "integrity": integrity}
            for name, (version, integrity) in EXPECTED_PACKAGES.items()
        ],
        "files": [
            {
                "path": relative_path,
                "length": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for relative_path, path in source_by_relative.items()
        ],
    }
    contract_path = root / "fixture-contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return cli, platform, contract_path


def _node_binary_fixture(root: Path) -> tuple[Path, str]:
    node = shutil.which("node.exe") or shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the executable-version fixture")
    version_output = subprocess.run(
        [node, "--version"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", version_output)
    if match is None:
        pytest.skip("Node did not report a three-component version")
    binary = root / "target/scriber-desktop.exe"
    binary.parent.mkdir(parents=True)
    shutil.copy2(node, binary)
    return binary, match.group(1)


def test_checked_in_contract_is_the_exact_external_trust_anchor() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["schemaVersion"] == 1
    assert contract["name"] == "scriber-tauri-cli-cache-contract"
    assert contract["revision"] == "tauri-cli-2.11.3-win32-x64-msvc-v1"
    assert contract["target"] == "win32-x64-msvc"
    assert contract["version"] == EXPECTED_VERSION
    assert contract["versionOutput"] == EXPECTED_VERSION_OUTPUT
    assert contract["entrypoint"] == "tauri-cli/node_modules/@tauri-apps/cli/tauri.js"
    assert {
        record["name"]: (record["version"], record["integrity"])
        for record in contract["packages"]
    } == EXPECTED_PACKAGES
    assert {
        record["path"]: (record["length"], record["sha256"])
        for record in contract["files"]
    } == EXPECTED_FILES

    package_lock = json.loads(PACKAGE_LOCK_PATH.read_text(encoding="utf-8"))
    packages = package_lock["packages"]
    for name, (version, integrity) in EXPECTED_PACKAGES.items():
        record = packages[f"node_modules/{name}"]
        assert record["version"] == version
        assert record["integrity"] == integrity


def test_sync_script_is_windows_powershell_safe_and_uses_external_attestation() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "[System.IO.Path]::GetRelativePath" not in script
    assert "Substring($prefix.Length)" in script
    assert "packaging\\tauri-cli-cache-contract.json" in script
    assert '[string]$ContractPath = "packaging\\tauri-cli-cache-contract.json"' in script
    assert "[System.IO.File]::ReadAllBytes($Path)" in script
    assert "$trustedContractSha256 = [string]$trustedContract.sha256" in script
    assert 'Write-GitHubOutput -Name "cli-contract-sha256"' in script
    assert script.count('Write-GitHubOutput -Name "cli-version-output"') == 2
    assert '[string]$manifest.tauriCli.contractSha256 -ceq $trustedContractSha256' in script
    assert '$actualOutput -cne $ExpectedOutput' in script
    assert "expectedCliVersionPattern" not in script
    assert "Assert-SafePathAncestry" in script
    assert "Get-SafeTreeRelativeFiles" in script
    assert "fsutil.exe hardlink list" in script
    assert "[System.IO.FileAttributes]::ReparsePoint" in script
    for name in ("NODE_OPTIONS", "NODE_PATH", "NAPI_RS_NATIVE_LIBRARY_PATH"):
        assert name in script
    assert "Invoke-Expression" not in script
    assert "-EncodedCommand" not in script

    root = _fixture_root()
    try:
        result, outputs = _invoke(cache_root=root / "missing-cache")
        assert result.returncode == 0, _combined_output(result)
        assert outputs["usable"] == "false"
        assert outputs["cli-contract-sha256"] == hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
        assert "No complete Tauri app binary and CLI cache was restored." in result.stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("name", ["NODE_OPTIONS", "NODE_PATH", "NAPI_RS_NATIVE_LIBRARY_PATH"])
def test_sync_script_rejects_node_execution_environment_overrides(name: str) -> None:
    root = _fixture_root()
    try:
        result, outputs = _invoke(
            cache_root=root / "missing-cache",
            env_changes={name: "attacker-controlled"},
        )
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert f"{name} must be unset" in _combined_output(result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_contract_override_is_unavailable_outside_the_focused_pytest_fixture() -> None:
    root = _fixture_root()
    _, _, contract_path = _create_fixture_cli(root)
    try:
        result, outputs = _invoke(
            cache_root=root / "missing-cache",
            contract_path=contract_path,
            env_changes={"PYTEST_CURRENT_TEST": ""},
        )
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert "allowed only from the focused pytest contract fixture" in _combined_output(result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("tamper", ["integrity", "version"])
def test_sync_script_rejects_package_lock_tampering(tamper: str) -> None:
    root = _fixture_root()
    try:
        package_lock = json.loads(PACKAGE_LOCK_PATH.read_text(encoding="utf-8"))
        cli = package_lock["packages"]["node_modules/@tauri-apps/cli"]
        platform = package_lock["packages"]["node_modules/@tauri-apps/cli-win32-x64-msvc"]
        if tamper == "integrity":
            cli["integrity"] = "sha512-" + ("A" * 88)
        else:
            cli["version"] = "2.11.4"
            platform["version"] = "2.11.4"
        candidate = root / "package-lock.json"
        candidate.write_text(json.dumps(package_lock), encoding="utf-8")

        result, outputs = _invoke(cache_root=root / "missing-cache", package_lock_path=candidate)
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert "does not match the checked-in Tauri CLI cache contract" in _combined_output(result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sync_script_rejects_a_hard_linked_trust_input() -> None:
    if os.name != "nt":
        pytest.skip("The Tauri CLI cache is Windows-only")
    root = _fixture_root()
    try:
        linked_lock = root / "package-lock-hardlink.json"
        os.link(PACKAGE_LOCK_PATH, linked_lock)
        result, outputs = _invoke(cache_root=root / "missing-cache", package_lock_path=linked_lock)
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert "hard link" in _combined_output(result).lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sync_script_rejects_a_reparse_cache_root() -> None:
    if os.name != "nt" or shutil.which("cmd.exe") is None:
        pytest.skip("Windows junction support is required")
    root = _fixture_root()
    target = root / "junction-target"
    cache_root = root / "junction-cache"
    target.mkdir()
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(cache_root), str(target)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip("A local junction could not be created")
    try:
        result, outputs = _invoke(cache_root=cache_root)
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert "must not be a reparse point" in _combined_output(result)
    finally:
        if cache_root.exists():
            os.rmdir(cache_root)
        shutil.rmtree(root, ignore_errors=True)


def test_export_rejects_tauri_cli_bytes_not_in_the_contract() -> None:
    root = _fixture_root()
    binary, binary_version = _node_binary_fixture(root)
    cli, platform, contract_path = _create_fixture_cli(root)
    (cli / "main.js").write_text("module.exports = { tampered: true }\n", encoding="utf-8")
    try:
        result, outputs = _invoke(
            mode="Export",
            cache_root=root / "cache",
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
            cli_package_path=cli,
            cli_platform_package_path=platform,
        )
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert "does not match the checked-in cache contract" in _combined_output(result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_export_rejects_non_exact_tauri_cli_version_output() -> None:
    root = _fixture_root()
    binary, binary_version = _node_binary_fixture(root)
    cli, platform, contract_path = _create_fixture_cli(
        root, version_output=f"{EXPECTED_VERSION_OUTPUT} unexpected-suffix"
    )
    try:
        result, outputs = _invoke(
            mode="Export",
            cache_root=root / "cache",
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
            cli_package_path=cli,
            cli_platform_package_path=platform,
        )
        assert result.returncode != 0
        assert outputs["usable"] == "false"
        assert f"did not exactly match '{EXPECTED_VERSION_OUTPUT}'" in _combined_output(result)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_fixture_contract_roundtrip_is_real_networkless_windows_powershell() -> None:
    root = _fixture_root()
    cache_root = root / "cache"
    binary, binary_version = _node_binary_fixture(root)
    cli, platform, contract_path = _create_fixture_cli(root)
    source_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    try:
        exported, export_outputs = _invoke(
            mode="Export",
            cache_root=cache_root,
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
            cli_package_path=cli,
            cli_platform_package_path=platform,
        )
        assert exported.returncode == 0, _combined_output(exported)
        assert export_outputs["usable"] == "true"
        assert export_outputs["cli-version"] == EXPECTED_VERSION
        assert export_outputs["cli-version-output"] == EXPECTED_VERSION_OUTPUT
        assert export_outputs["cli-contract-sha256"] == hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest()
        manifest_path = cache_root / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        manifest = json.loads(original_manifest.decode("utf-8-sig"))
        assert manifest["apiVersion"] == "4"
        assert manifest["tauriCli"]["contractName"] == "scriber-tauri-cli-cache-contract"
        assert manifest["tauriCli"]["contractRevision"] == (
            "tauri-cli-2.11.3-win32-x64-msvc-v1"
        )
        assert manifest["tauriCli"]["versionOutput"] == EXPECTED_VERSION_OUTPUT

        binary.unlink()
        imported, import_outputs = _invoke(
            mode="Import",
            cache_root=cache_root,
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
        )
        assert imported.returncode == 0, _combined_output(imported)
        assert import_outputs["usable"] == "true"
        assert import_outputs["cli-version"] == EXPECTED_VERSION
        assert import_outputs["cli-version-output"] == EXPECTED_VERSION_OUTPUT
        assert import_outputs["cli-contract-sha256"] == export_outputs["cli-contract-sha256"]
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == source_sha256

        manifest["tauriCli"]["contractSha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        binary.unlink()
        tampered, tampered_outputs = _invoke(
            mode="Import",
            cache_root=cache_root,
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
        )
        assert tampered.returncode == 0, _combined_output(tampered)
        assert tampered_outputs["usable"] == "false"
        assert not binary.exists()

        manifest_path.write_bytes(original_manifest)
        cached_entrypoint = cache_root / "tauri-cli/node_modules/@tauri-apps/cli/tauri.js"
        hardlink_source = root / "hardlink-source-tauri.js"
        shutil.copy2(cached_entrypoint, hardlink_source)
        cached_entrypoint.unlink()
        os.link(hardlink_source, cached_entrypoint)
        hardlinked, hardlinked_outputs = _invoke(
            mode="Import",
            cache_root=cache_root,
            version=binary_version,
            binary_path=binary,
            contract_path=contract_path,
        )
        assert hardlinked.returncode != 0
        assert hardlinked_outputs["usable"] == "false"
        assert "hard link" in _combined_output(hardlinked).lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_real_export_import_roundtrip_with_exact_local_cli_bytes() -> None:
    missing = [path for path in SOURCE_FILES.values() if not path.is_file()]
    if missing:
        pytest.skip("Exact local Tauri CLI package bytes are not installed in this worktree")
    for relative_path, path in SOURCE_FILES.items():
        expected_length, expected_sha256 = EXPECTED_FILES[relative_path]
        assert path.stat().st_size == expected_length
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256

    node = shutil.which("node.exe") or shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the real Tauri CLI roundtrip")
    version_output = subprocess.run(
        [node, "--version"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", version_output)
    if match is None:
        pytest.skip("Node did not report a three-component version")

    root = _fixture_root()
    cache_root = root / "cache"
    binary = root / "target/scriber-desktop.exe"
    binary.parent.mkdir(parents=True)
    shutil.copy2(node, binary)
    source_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    try:
        exported, export_outputs = _invoke(
            mode="Export",
            cache_root=cache_root,
            version=match.group(1),
            binary_path=binary,
            cli_package_path=REPO_ROOT / "Frontend/node_modules/@tauri-apps/cli",
            cli_platform_package_path=REPO_ROOT
            / "Frontend/node_modules/@tauri-apps/cli-win32-x64-msvc",
        )
        assert exported.returncode == 0, _combined_output(exported)
        assert export_outputs["usable"] == "true"
        assert export_outputs["cli-version"] == EXPECTED_VERSION
        assert export_outputs["cli-contract-sha256"] == hashlib.sha256(
            CONTRACT_PATH.read_bytes()
        ).hexdigest()
        manifest = json.loads((cache_root / "manifest.json").read_text(encoding="utf-8-sig"))
        assert manifest["apiVersion"] == "4"
        assert manifest["tauriCli"]["versionOutput"] == EXPECTED_VERSION_OUTPUT
        assert manifest["tauriCli"]["contractSha256"] == export_outputs["cli-contract-sha256"]

        binary.unlink()
        imported, import_outputs = _invoke(
            mode="Import",
            cache_root=cache_root,
            version=match.group(1),
            binary_path=binary,
        )
        assert imported.returncode == 0, _combined_output(imported)
        assert import_outputs["usable"] == "true"
        assert import_outputs["cli-version"] == EXPECTED_VERSION
        assert import_outputs["cli-contract-sha256"] == export_outputs["cli-contract-sha256"]
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == source_sha256
    finally:
        shutil.rmtree(root, ignore_errors=True)
