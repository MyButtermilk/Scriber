from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.installer_research.comparator import (
    MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS,
    MINIMUM_TIMING_PAIR_COUNT,
    accept_baseline,
)
from scripts.installer_research.inventory import InventoryError
from scripts.perf.autoresearch_profiles import ProfileError, resolve_profile_context
from scripts.perf.doctor import list_scriber_processes
from scripts.perf.installer_size.evaluator import validate_result
from scripts.perf.installer_size.state import (
    StateError,
    file_sha256,
    git_snapshot,
    load_json_object,
    load_manifest,
    load_progress,
    load_session_init,
    parse_utc,
    paths_for,
    protected_drift,
    read_ledger,
    utc_now,
)


PHASES = ("prepare", "run", "finalize")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
REQUIRED_HOLDOUT_FAMILIES = {
    "regular",
    "signature-challenge",
    "shorts",
    "music",
    "captions",
    "live-replay",
}


def finding(level: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, **details}


def _blocking(findings: list[dict[str, Any]]) -> bool:
    return any(item.get("level") == "block" for item in findings)


def _load_optional(path: Path, *, code: str, label: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        return load_json_object(path), []
    except StateError as exc:
        return None, [finding("block", code, f"{label}: {exc}")]


def _hash_field(value: Any) -> bool:
    return bool(SHA256_PATTERN.fullmatch(str(value or "")))


def _lower_hash_field(value: Any) -> bool:
    text = str(value or "")
    return _hash_field(text) and text == text.casefold()


def current_installer_evaluator_hash(repo_root: Path) -> str:
    paths = [repo_root / "scripts" / "installer_research.py"]
    paths.extend(sorted((repo_root / "scripts" / "installer_research").glob("*.py")))
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise StateError(f"missing installer evaluator input: {path.name}")
        relative = path.relative_to(repo_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _manifest_is_path_redacted(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_manifest_is_path_redacted(item) for item in value.values())
    if isinstance(value, list):
        return all(_manifest_is_path_redacted(item) for item in value)
    if not isinstance(value, str):
        return True
    return not bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"))


def _active_environment_verification(context) -> list[dict[str, Any]]:
    paths = paths_for(context)
    python_executable = paths.environment_manifest.parent / ".venv" / "Scripts" / "python.exe"
    writer = context.repo_root / "scripts" / "write_installer_research_environment_manifest.py"
    wheelhouse = paths.root / "wheelhouse"
    requirements = [
        paths.baseline_requirements_base,
        paths.baseline_requirements_build,
    ]
    expected_files = [python_executable, writer, paths.environment_manifest, *requirements]
    if not wheelhouse.is_dir() or any(not path.is_file() for path in expected_files):
        return [
            finding(
                "block",
                "environment_active_verification_unavailable",
                "baseline environment verifier inputs are missing",
            )
        ]
    try:
        result = subprocess.run(
            [
                str(python_executable),
                str(writer),
                "--run-id",
                str(context.run_id),
                "--environment-name",
                "baseline",
                "--wheelhouse",
                str(wheelhouse),
                "--requirements",
                str(requirements[0]),
                "--requirements",
                str(requirements[1]),
                "--verify",
                str(paths.environment_manifest),
            ],
            cwd=str(context.repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [
            finding(
                "block",
                "environment_active_verification_failed",
                "baseline environment verification could not complete",
            )
        ]
    if result.returncode != 0:
        return [
            finding(
                "block",
                "environment_content_drift",
                "baseline environment, requirements, or wheelhouse content differs from its manifest",
            )
        ]
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError:
        summary = None
    if (
        not isinstance(summary, dict)
        or summary.get("ok") is not True
        or summary.get("runId") != context.run_id
        or summary.get("environmentName") != "baseline"
    ):
        return [
            finding(
                "block",
                "environment_active_verification_invalid",
                "baseline environment verifier returned an invalid identity summary",
            )
        ]
    return []


def validate_environment_manifests(context) -> tuple[list[dict[str, Any]], dict[str, str]]:
    paths = paths_for(context)
    findings: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    wheelhouse, errors = _load_optional(
        paths.wheelhouse_manifest,
        code="wheelhouse_manifest_missing_or_invalid",
        label="locked wheelhouse manifest is unavailable",
    )
    findings.extend(errors)
    requirement_snapshots = {
        "requirements-base.txt": paths.baseline_requirements_base,
        "requirements-build.txt": paths.baseline_requirements_build,
    }
    snapshot_identities: list[dict[str, Any]] = []
    for name, snapshot_path in requirement_snapshots.items():
        if not snapshot_path.is_file():
            findings.append(
                finding(
                    "block",
                    "baseline_requirement_snapshot_missing",
                    f"immutable baseline requirement snapshot {name} is unavailable",
                )
            )
            continue
        snapshot_identities.append(
            {
                "name": name,
                "length": snapshot_path.stat().st_size,
                "sha256": file_sha256(snapshot_path),
            }
        )
        evidence[f"baseline-{name}"] = file_sha256(snapshot_path)
    snapshot_identities.sort(key=lambda item: str(item["name"]).casefold())
    try:
        session_init = load_session_init(context)
    except StateError as exc:
        findings.append(
            finding(
                "block",
                "baseline_requirement_session_binding_invalid",
                f"baseline requirement session binding is unavailable: {exc}",
            )
        )
    else:
        if session_init.get("baselineRequirementSources") != snapshot_identities:
            findings.append(
                finding(
                    "block",
                    "baseline_requirement_snapshot_session_drift",
                    "immutable baseline requirement snapshots differ from session initialization",
                )
            )
    environment, errors = _load_optional(
        paths.environment_manifest,
        code="environment_manifest_missing_or_invalid",
        label="hermetic baseline environment manifest is unavailable",
    )
    findings.extend(errors)
    if wheelhouse is not None:
        if wheelhouse.get("kind") != "scriber-installer-research-wheelhouse":
            findings.append(finding("block", "wheelhouse_manifest_contract_mismatch", "wheelhouse manifest kind is invalid"))
        if wheelhouse.get("runId") != context.run_id:
            findings.append(finding("block", "wheelhouse_manifest_run_mismatch", "wheelhouse manifest belongs to another run"))
        for field in ("requirementsSha256", "wheelhouseSha256"):
            if not _hash_field(wheelhouse.get(field)):
                findings.append(finding("block", "wheelhouse_manifest_hash_invalid", f"wheelhouse {field} is invalid"))
        if wheelhouse.get("requirements") != snapshot_identities:
            findings.append(
                finding(
                    "block",
                    "wheelhouse_baseline_requirement_snapshot_mismatch",
                    "wheelhouse manifest does not bind the immutable baseline requirement snapshots",
                )
            )
        evidence["wheelhouse-manifest.json"] = file_sha256(paths.wheelhouse_manifest)
    if environment is not None:
        if environment.get("kind") != "scriber-installer-research-python-environment":
            findings.append(finding("block", "environment_manifest_contract_mismatch", "environment manifest kind is invalid"))
        if environment.get("runId") != context.run_id or environment.get("environmentName") != "baseline":
            findings.append(
                finding(
                    "block",
                    "environment_manifest_identity_mismatch",
                    "baseline environment manifest must bind this RunId and environmentName=baseline",
                )
            )
        for field in ("requirementsSha256", "wheelhouseSha256", "productDependenciesSha256"):
            if not _hash_field(environment.get(field)):
                findings.append(finding("block", "environment_manifest_hash_invalid", f"environment {field} is invalid"))
        distributions = environment.get("distributions")
        if not isinstance(distributions, list) or not distributions:
            findings.append(finding("block", "environment_distributions_missing", "environment distribution inventory is empty"))
        else:
            names = [str(item.get("name") or "") for item in distributions if isinstance(item, dict)]
            if len(names) != len(distributions) or any(not name for name in names) or len(names) != len(set(names)):
                findings.append(finding("block", "environment_distributions_invalid", "environment distributions are missing or duplicated"))
        if not _manifest_is_path_redacted(environment):
            findings.append(finding("block", "environment_manifest_contains_path", "environment manifest contains a local absolute path"))
        if environment.get("requirements") != snapshot_identities:
            findings.append(
                finding(
                    "block",
                    "environment_baseline_requirement_snapshot_mismatch",
                    "baseline environment does not bind the immutable requirement snapshots",
                )
            )
        evidence["environment-manifest.json"] = file_sha256(paths.environment_manifest)
    if wheelhouse is not None and environment is not None:
        for field in ("requirementsSha256", "wheelhouseSha256"):
            if wheelhouse.get(field) != environment.get(field):
                findings.append(
                    finding(
                        "block",
                        "environment_wheelhouse_drift",
                        f"environment and wheelhouse differ at {field}",
                        field=field,
                    )
                )
        findings.extend(_active_environment_verification(context))
    return findings, evidence


def _capture_tool_version(
    command: list[str],
    *,
    repo_root: Path,
    environment: dict[str, str] | None = None,
) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=str(repo_root),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _file_identity_matches(path: Path, component: Any) -> bool:
    if not isinstance(component, dict) or not path.is_file():
        return False
    length = component.get("length")
    sha256 = component.get("sha256")
    return bool(
        isinstance(length, int)
        and not isinstance(length, bool)
        and path.stat().st_size == length
        and _lower_hash_field(sha256)
        and file_sha256(path) == sha256
    )


def _plain_tree_identity(root: Path) -> dict[str, Any] | None:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    try:
        root_info = root.lstat()
    except OSError:
        return None
    if (
        not root.is_dir()
        or root.is_symlink()
        or bool(getattr(root_info, "st_file_attributes", 0) & reparse_point)
    ):
        return None
    entries: list[str] = []
    file_count = 0
    total_bytes = 0
    try:
        descendants = list(root.rglob("*"))
        for path in descendants:
            info = path.lstat()
            if path.is_symlink() or bool(
                getattr(info, "st_file_attributes", 0) & reparse_point
            ):
                return None
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                entries.append(f"D|{relative}")
            elif path.is_file():
                length = info.st_size
                entries.append(
                    f"F|{relative}|{length}|{file_sha256(path)}"
                )
                file_count += 1
                total_bytes += length
            else:
                return None
    except OSError:
        return None
    canonical = "\n".join(sorted(entries)).encode("utf-8")
    return {
        "fileCount": file_count,
        "totalBytes": total_bytes,
        "treeSha256": hashlib.sha256(canonical).hexdigest(),
    }


def _active_toolchain_verification(context, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rehash and execute every build tool bound by the run-local manifest."""

    paths = paths_for(context)
    toolchain_root = paths.toolchain_manifest.parent
    node_root = toolchain_root / "node"
    node = node_root / "node.exe"
    npm = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    tauri = context.repo_root / "Frontend" / "node_modules" / "@tauri-apps" / "cli" / "tauri.js"
    frontend_node_modules = context.repo_root / "Frontend" / "node_modules"
    native_tauri = (
        frontend_node_modules
        / "@tauri-apps"
        / "cli-win32-x64-msvc"
        / "cli.win32-x64-msvc.node"
    )
    package_lock = context.repo_root / "Frontend" / "package-lock.json"
    node_version_file = context.repo_root / ".node-version"
    findings: list[dict[str, Any]] = []

    components = {
        "node": (node, "node", "node.exe"),
        "npm": (npm, "npm-cli", "npm-cli.js"),
        "tauri": (tauri, "tauri-cli", "tauri.js"),
        "nativeTauriCli": (
            native_tauri,
            "native-tauri-cli",
            "cli.win32-x64-msvc.node",
        ),
        "frontendPackageLock": (
            package_lock,
            "frontend-package-lock",
            "package-lock.json",
        ),
    }
    for name, (path, expected_name, expected_file_name) in components.items():
        component = payload.get(name)
        if (
            not isinstance(component, dict)
            or component.get("name") != expected_name
            or component.get("fileName") != expected_file_name
            or (
                name == "frontendPackageLock"
                and component.get("version") != "lockfile-v3"
            )
            or not _file_identity_matches(path, component)
        ):
            findings.append(
                finding(
                    "block",
                    "toolchain_component_content_drift",
                    f"active toolchain component {name} differs from its manifest",
                    component=name,
                )
            )

    node_modules_identity = _plain_tree_identity(frontend_node_modules)
    if (
        node_modules_identity is None
        or payload.get("frontendNodeModules") != node_modules_identity
    ):
        findings.append(
            finding(
                "block",
                "toolchain_node_modules_tree_drift",
                "the complete locked Frontend node_modules tree differs from its manifest",
            )
        )
    tauri_component = payload.get("tauri")
    native_tauri_component = payload.get("nativeTauriCli")
    if (
        not isinstance(tauri_component, dict)
        or not isinstance(native_tauri_component, dict)
        or native_tauri_component.get("version") != tauri_component.get("version")
    ):
        findings.append(
            finding(
                "block",
                "toolchain_native_tauri_version_drift",
                "native and JavaScript Tauri CLI identities report different versions",
            )
        )

    node_archive = payload.get("nodeArchive")
    node_version = ""
    try:
        node_version = node_version_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    expected_archive_name = f"node-v{node_version}-win-x64.zip"
    archive_name = node_archive.get("fileName") if isinstance(node_archive, dict) else None
    archive = toolchain_root / "downloads" / str(archive_name or "invalid")
    expected_checksum_source = f"https://nodejs.org/dist/v{node_version}/SHASUMS256.txt"
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+", node_version)
        or not isinstance(node_archive, dict)
        or archive_name != expected_archive_name
        or node_archive.get("checksumSource") != expected_checksum_source
        or not _file_identity_matches(archive, node_archive)
    ):
        findings.append(
            finding(
                "block",
                "toolchain_node_archive_drift",
                "pinned Node archive differs from its manifest or repository version pin",
            )
        )

    node_actual = _capture_tool_version([str(node), "--version"], repo_root=context.repo_root)
    npm_actual = _capture_tool_version(
        [str(node), str(npm), "--version"],
        repo_root=context.repo_root,
    )
    tauri_actual = _capture_tool_version(
        [str(node), str(tauri), "--version"],
        repo_root=context.repo_root,
    )
    expected_versions = {
        "node": f"v{node_version}",
        "npm": payload.get("npm", {}).get("version") if isinstance(payload.get("npm"), dict) else None,
        "tauri": payload.get("tauri", {}).get("version") if isinstance(payload.get("tauri"), dict) else None,
    }
    actual_versions = {"node": node_actual, "npm": npm_actual, "tauri": tauri_actual}
    for name, actual in actual_versions.items():
        manifest_component = payload.get(name)
        manifest_version = (
            manifest_component.get("version") if isinstance(manifest_component, dict) else None
        )
        expected = expected_versions[name]
        if not isinstance(manifest_version, str) or not manifest_version or actual != expected or actual != manifest_version:
            findings.append(
                finding(
                    "block",
                    "toolchain_component_version_drift",
                    f"active toolchain component {name} reports a different version",
                    component=name,
                )
            )

    rust_toolchain = payload.get("rustToolchain")
    rustup = shutil.which("rustup")
    rust_paths: dict[str, Path] = {}
    if rust_toolchain != "1.97.0" or not rustup:
        findings.append(
            finding(
                "block",
                "toolchain_rust_resolution_failed",
                "the pinned Rust toolchain cannot be resolved actively",
            )
        )
    else:
        rust_components = {
            "rustc": "rustc",
            "cargo": "cargo",
            "rustfmt": "rustfmt",
            "clippyDriver": "clippy-driver",
        }
        for name, rustup_name in rust_components.items():
            resolved = _capture_tool_version(
                [rustup, "which", "--toolchain", rust_toolchain, rustup_name],
                repo_root=context.repo_root,
            )
            if not resolved:
                findings.append(
                    finding(
                        "block",
                        "toolchain_rust_resolution_failed",
                        f"the pinned Rust component {name} cannot be resolved actively",
                        component=name,
                    )
                )
                continue
            resolved_path = Path(resolved)
            component = payload.get(name)
            expected_name = f"{rustup_name}-rustup-proxy"
            if (
                not isinstance(component, dict)
                or component.get("name") != expected_name
                or component.get("fileName") != resolved_path.name
                or not _file_identity_matches(resolved_path, component)
            ):
                findings.append(
                    finding(
                        "block",
                        "toolchain_component_content_drift",
                        f"active toolchain component {name} differs from its manifest",
                        component=name,
                    )
                )
            rust_paths[name] = resolved_path

        rust_environment = dict(os.environ)
        rust_environment["RUSTUP_TOOLCHAIN"] = str(rust_toolchain)
        for name, path in rust_paths.items():
            actual = _capture_tool_version(
                [str(path), "--version"],
                repo_root=context.repo_root,
                environment=rust_environment,
            )
            component = payload.get(name)
            manifest_version = component.get("version") if isinstance(component, dict) else None
            if (
                not isinstance(manifest_version, str)
                or not manifest_version
                or actual != manifest_version
                or (name == "rustc" and not actual.startswith(f"rustc {rust_toolchain} "))
            ):
                findings.append(
                    finding(
                        "block",
                        "toolchain_component_version_drift",
                        f"active toolchain component {name} reports a different version",
                        component=name,
                    )
                )

    nsis_component = payload.get("nsis")
    nsis_tree = payload.get("nsisTree")
    local_app_data = os.environ.get("LOCALAPPDATA")
    relative_nsis = (
        nsis_component.get("relativePath")
        if isinstance(nsis_component, dict)
        else None
    )
    nsis_root = Path(local_app_data) / "tauri" / "NSIS" if local_app_data else None
    nsis = (
        nsis_root / Path(relative_nsis.replace("/", os.sep))
        if nsis_root is not None
        and relative_nsis in {"Bin/makensis.exe", "makensis.exe"}
        else None
    )
    if nsis is None:
        findings.append(
            finding(
                "block",
                "toolchain_component_content_drift",
                "active toolchain component nsis differs from its manifest",
                component="nsis",
            )
        )
    elif (
        nsis_component.get("name") != "makensis"
        or nsis_component.get("fileName") != "makensis.exe"
        or not _file_identity_matches(nsis, nsis_component)
    ):
        findings.append(
            finding(
                "block",
                "toolchain_component_content_drift",
                "active toolchain component nsis differs from its manifest",
                component="nsis",
            )
        )
    else:
        actual = _capture_tool_version([str(nsis), "/VERSION"], repo_root=context.repo_root)
        manifest_version = nsis_component.get("version")
        if not isinstance(manifest_version, str) or not manifest_version or actual != manifest_version:
            findings.append(
                finding(
                    "block",
                    "toolchain_component_version_drift",
                    "active toolchain component nsis reports a different version",
                    component="nsis",
                )
            )
    actual_nsis_tree = _plain_tree_identity(nsis_root) if nsis_root is not None else None
    if actual_nsis_tree is None or actual_nsis_tree != nsis_tree:
        findings.append(
            finding(
                "block",
                "toolchain_nsis_tree_drift",
                "the complete Tauri NSIS toolchain tree differs from its manifest",
            )
        )
    return findings


def validate_toolchain_manifest(context) -> tuple[list[dict[str, Any]], dict[str, str]]:
    path = paths_for(context).toolchain_manifest
    payload, findings = _load_optional(
        path,
        code="toolchain_manifest_missing_or_invalid",
        label="pinned toolchain manifest is unavailable",
    )
    evidence: dict[str, str] = {}
    if payload is None:
        return findings, evidence
    if payload.get("kind") != "scriber-installer-research-toolchain":
        findings.append(finding("block", "toolchain_manifest_contract_mismatch", "toolchain manifest kind is invalid"))
    if payload.get("runId") != context.run_id:
        findings.append(finding("block", "toolchain_manifest_run_mismatch", "toolchain manifest belongs to another run"))
    if payload.get("rustToolchain") != "1.97.0":
        findings.append(finding("block", "toolchain_rust_pin_mismatch", "research Rust toolchain must remain pinned to 1.97.0"))
    for name in (
        "node",
        "npm",
        "tauri",
        "rustc",
        "cargo",
        "rustfmt",
        "clippyDriver",
        "nsis",
        "frontendPackageLock",
        "nativeTauriCli",
        "nodeArchive",
    ):
        component = payload.get(name)
        if not isinstance(component, dict):
            findings.append(finding("block", "toolchain_component_missing", f"toolchain component {name} is missing"))
            continue
        if not _hash_field(component.get("sha256")):
            findings.append(finding("block", "toolchain_component_hash_invalid", f"toolchain component {name} has no valid SHA-256"))
        length = component.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            findings.append(finding("block", "toolchain_component_length_invalid", f"toolchain component {name} length is invalid"))
    frontend_node_modules = payload.get("frontendNodeModules")
    if (
        not isinstance(frontend_node_modules, dict)
        or set(frontend_node_modules)
        != {"fileCount", "totalBytes", "treeSha256"}
        or isinstance(frontend_node_modules.get("fileCount"), bool)
        or not isinstance(frontend_node_modules.get("fileCount"), int)
        or frontend_node_modules.get("fileCount", 0) <= 0
        or isinstance(frontend_node_modules.get("totalBytes"), bool)
        or not isinstance(frontend_node_modules.get("totalBytes"), int)
        or frontend_node_modules.get("totalBytes", 0) <= 0
        or not _lower_hash_field(frontend_node_modules.get("treeSha256"))
    ):
        findings.append(
            finding(
                "block",
                "toolchain_node_modules_identity_invalid",
                "Frontend node_modules tree identity is missing or invalid",
            )
        )
    nsis_tree = payload.get("nsisTree")
    if (
        not isinstance(nsis_tree, dict)
        or set(nsis_tree) != {"fileCount", "totalBytes", "treeSha256"}
        or isinstance(nsis_tree.get("fileCount"), bool)
        or not isinstance(nsis_tree.get("fileCount"), int)
        or nsis_tree.get("fileCount", 0) <= 0
        or isinstance(nsis_tree.get("totalBytes"), bool)
        or not isinstance(nsis_tree.get("totalBytes"), int)
        or nsis_tree.get("totalBytes", 0) <= 0
        or not _lower_hash_field(nsis_tree.get("treeSha256"))
    ):
        findings.append(
            finding(
                "block",
                "toolchain_nsis_tree_identity_invalid",
                "complete NSIS tree identity is missing or invalid",
            )
        )
    nsis_component = payload.get("nsis")
    if (
        not isinstance(nsis_component, dict)
        or nsis_component.get("relativePath")
        not in {"Bin/makensis.exe", "makensis.exe"}
    ):
        findings.append(
            finding(
                "block",
                "toolchain_nsis_binding_invalid",
                "NSIS executable is not bound to the attested tree",
            )
        )
    if not _manifest_is_path_redacted(payload):
        findings.append(finding("block", "toolchain_manifest_contains_path", "toolchain manifest contains a local absolute path"))
    findings.extend(_active_toolchain_verification(context, payload))
    evidence["toolchain-manifest.json"] = file_sha256(path)
    return findings, evidence


def _static_holdout_cases(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    if payload.get("schemaVersion") != 1 or payload.get("fixtureId") != "youtube-runtime-holdouts-v1":
        findings.append(finding("block", "youtube_holdout_contract_mismatch", "YouTube holdout fixture contract is invalid"))
    if payload.get("frozenCaseContract") is not True:
        findings.append(finding("block", "youtube_holdout_contract_not_frozen", "YouTube holdout case contract is not frozen"))
    rows = payload.get("cases")
    if not isinstance(rows, list):
        return {}, [*findings, finding("block", "youtube_holdout_cases_missing", "YouTube holdout cases are missing")]
    cases: dict[str, dict[str, Any]] = {}
    families: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            findings.append(finding("block", "youtube_holdout_case_invalid", "YouTube holdout case is not an object"))
            continue
        case_id = str(row.get("id") or "")
        family = str(row.get("family") or "")
        if not case_id or case_id in cases:
            findings.append(finding("block", "youtube_holdout_case_id_invalid", "YouTube holdout case id is missing or duplicated"))
            continue
        if row.get("status") != "pending_validation":
            findings.append(
                finding(
                    "block",
                    "youtube_holdout_static_false_attestation",
                    f"static holdout {case_id} must remain pending_validation until run-local proof exists",
                )
            )
        capabilities = row.get("requiredCapabilities")
        if not isinstance(capabilities, list) or not capabilities:
            findings.append(finding("block", "youtube_holdout_capabilities_missing", f"holdout {case_id} has no capability contract"))
        cases[case_id] = row
        families.add(family)
    if families != REQUIRED_HOLDOUT_FAMILIES:
        findings.append(
            finding(
                "block",
                "youtube_holdout_family_coverage_incomplete",
                "YouTube holdout contract does not cover every required family",
                expected=sorted(REQUIRED_HOLDOUT_FAMILIES),
                actual=sorted(families),
            )
        )
    return cases, findings


def _valid_holdout_url(family: str, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or host not in {
        "youtube.com",
        "www.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }:
        return False
    if family == "shorts":
        return host in {"youtube.com", "www.youtube.com"} and parsed.path.startswith("/shorts/")
    if family == "music":
        return host == "music.youtube.com" and parsed.path == "/watch"
    return bool(parsed.path == "/watch" or host == "youtu.be")


def _holdout_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if parsed.path.startswith("/shorts/"):
        return parsed.path.split("/", 3)[2]
    from urllib.parse import parse_qs

    return str((parse_qs(parsed.query).get("v") or [""])[0])


def _valid_holdout_runtime(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("name") == "deno"
        and value.get("version") == "2.9.2"
        and isinstance(value.get("length"), int)
        and not isinstance(value.get("length"), bool)
        and value.get("length", 0) > 0
        and _lower_hash_field(value.get("sha256"))
    )


def _valid_holdout_distributions(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"deno", "yt-dlp", "yt-dlp-ejs"}:
        return False
    for expected_name, identity in value.items():
        if not isinstance(identity, dict):
            return False
        if identity.get("name") != expected_name or not str(identity.get("version") or ""):
            return False
        if (
            isinstance(identity.get("fileCount"), bool)
            or not isinstance(identity.get("fileCount"), int)
            or identity.get("fileCount", 0) <= 0
            or not _lower_hash_field(identity.get("contentSha256"))
        ):
            return False
    return True


def validate_holdouts(context) -> tuple[list[dict[str, Any]], dict[str, str]]:
    profile_path = context.config_path.parent / "youtube-holdouts.json"
    fixture, errors = _load_optional(
        profile_path,
        code="youtube_holdout_fixture_missing_or_invalid",
        label="YouTube holdout fixture is unavailable",
    )
    if fixture is None:
        return errors, {}
    static_cases, findings = _static_holdout_cases(fixture)
    findings.extend(errors)
    snapshot_path = paths_for(context).holdout_snapshot
    snapshot, errors = _load_optional(
        snapshot_path,
        code="youtube_holdout_snapshot_missing_or_invalid",
        label="run-local YouTube holdout validation is required",
    )
    findings.extend(errors)
    evidence = {"youtube-holdouts.fixture.json": file_sha256(profile_path)}
    if snapshot is None:
        return findings, evidence
    if snapshot.get("holdoutSnapshotContract") != "InstallerSizeYoutubeHoldoutsV1":
        findings.append(finding("block", "youtube_holdout_snapshot_contract_mismatch", "run-local holdout snapshot contract is invalid"))
    if snapshot.get("schemaVersion") != 1 or snapshot.get("runId") != context.run_id:
        findings.append(finding("block", "youtube_holdout_snapshot_identity_mismatch", "run-local holdout snapshot belongs to another contract or run"))
    if snapshot.get("fixtureId") != fixture.get("fixtureId"):
        findings.append(finding("block", "youtube_holdout_snapshot_fixture_mismatch", "run-local holdout snapshot uses another fixture"))
    fixture_sha = file_sha256(profile_path)
    if snapshot.get("fixtureSha256") != fixture_sha:
        findings.append(finding("block", "youtube_holdout_snapshot_fixture_hash_mismatch", "run-local holdout snapshot is not bound to the frozen fixture bytes"))
    try:
        parse_utc(snapshot.get("capturedAtUtc"), field="youtubeHoldouts.capturedAtUtc")
    except StateError as exc:
        findings.append(finding("block", "youtube_holdout_snapshot_time_invalid", str(exc)))
    snapshot_runtime = snapshot.get("runtime")
    snapshot_distributions = snapshot.get("distributions")
    if not _valid_holdout_runtime(snapshot_runtime):
        findings.append(finding("block", "youtube_holdout_runtime_identity_invalid", "run-local Deno runtime identity is invalid or unpinned"))
    if not _valid_holdout_distributions(snapshot_distributions):
        findings.append(finding("block", "youtube_holdout_distribution_identity_invalid", "run-local Deno/yt-dlp distribution identities are invalid"))
    rows = snapshot.get("cases")
    seen: set[str] = set()
    seen_urls: set[str] = set()
    seen_video_ids: set[str] = set()
    if not isinstance(rows, list):
        findings.append(finding("block", "youtube_holdout_snapshot_cases_missing", "run-local holdout cases are missing"))
    else:
        for row in rows:
            if not isinstance(row, dict):
                findings.append(finding("block", "youtube_holdout_snapshot_case_invalid", "run-local holdout case is invalid"))
                continue
            case_id = str(row.get("id") or "")
            static = static_cases.get(case_id)
            if static is None or case_id in seen:
                findings.append(finding("block", "youtube_holdout_snapshot_case_unknown", f"run-local holdout case {case_id or '<missing>'} is unknown or duplicated"))
                continue
            seen.add(case_id)
            family = str(static.get("family") or "")
            if row.get("family") != family or row.get("status") != "validated":
                findings.append(finding("block", "youtube_holdout_snapshot_case_unvalidated", f"holdout {case_id} is not validated for its frozen family"))
            url = str(row.get("url") or "")
            if not _valid_holdout_url(family, url) or url != static.get("url"):
                findings.append(finding("block", "youtube_holdout_snapshot_url_invalid", f"holdout {case_id} URL does not represent its family"))
            video_id = str(row.get("videoId") or "")
            if (
                not video_id
                or video_id != _holdout_video_id(url)
                or url in seen_urls
                or video_id in seen_video_ids
            ):
                findings.append(finding("block", "youtube_holdout_snapshot_video_identity_invalid", f"holdout {case_id} URL/video identity is missing, mismatched, or duplicated"))
            seen_urls.add(url)
            seen_video_ids.add(video_id)
            observed = row.get("observedCapabilities")
            required = set(static.get("requiredCapabilities") or [])
            if not isinstance(observed, list) or not required.issubset(set(observed)):
                findings.append(finding("block", "youtube_holdout_snapshot_capability_missing", f"holdout {case_id} lacks required observed capabilities"))
            if row.get("denoProbe") != "pass":
                findings.append(finding("block", "youtube_holdout_snapshot_deno_probe_missing", f"holdout {case_id} has no passing Deno reference probe"))
            evidence_path = (
                paths_for(context).preflight_dir / "youtube-holdout-probes" / f"{case_id}.json"
            ).resolve()
            if not evidence_path.is_relative_to(paths_for(context).root.resolve()) or not evidence_path.is_file():
                findings.append(finding("block", "youtube_holdout_probe_missing", f"holdout {case_id} Deno probe evidence is missing"))
                continue
            if row.get("probeEvidenceSha256") != file_sha256(evidence_path):
                findings.append(finding("block", "youtube_holdout_probe_hash_mismatch", f"holdout {case_id} Deno probe hash is invalid"))
                continue
            evidence[f"youtube-holdout-probe:{case_id}"] = file_sha256(evidence_path)
            try:
                probe = load_json_object(evidence_path)
            except StateError as exc:
                findings.append(finding("block", "youtube_holdout_probe_invalid", f"holdout {case_id} Deno probe is invalid: {exc}"))
                continue
            if (
                probe.get("probeContract") != "InstallerSizeYoutubeHoldoutProbeV1"
                or probe.get("schemaVersion") != 1
                or probe.get("runId") != context.run_id
                or probe.get("fixtureId") != fixture.get("fixtureId")
                or probe.get("caseId") != case_id
                or probe.get("family") != family
                or probe.get("status") != "pass"
                or probe.get("url") != url
                or probe.get("videoId") != video_id
                or probe.get("capturedAtUtc") != snapshot.get("capturedAtUtc")
                or probe.get("runtime") != snapshot_runtime
                or probe.get("distributions") != snapshot_distributions
            ):
                findings.append(finding("block", "youtube_holdout_probe_contract_mismatch", f"holdout {case_id} Deno probe is not bound to this case"))
            probe_capabilities = probe.get("observedCapabilities")
            if not isinstance(probe_capabilities, list) or not required.issubset(set(probe_capabilities)):
                findings.append(finding("block", "youtube_holdout_probe_capability_missing", f"holdout {case_id} probe lacks frozen capabilities"))
        if seen != set(static_cases):
            findings.append(finding("block", "youtube_holdout_snapshot_incomplete", "run-local holdout snapshot omits frozen cases"))
    evidence["youtube-holdouts.snapshot.json"] = file_sha256(snapshot_path)
    return findings, evidence


def _preflight_evidence(context) -> tuple[list[dict[str, Any]], dict[str, str]]:
    findings: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    for validator in (validate_environment_manifests, validate_toolchain_manifest, validate_holdouts):
        validator_findings, validator_evidence = validator(context)
        findings.extend(validator_findings)
        evidence.update(validator_evidence)
    return findings, evidence


def validate_preflight_record(context, evidence: dict[str, str]) -> list[dict[str, Any]]:
    paths = paths_for(context)
    payload, findings = _load_optional(
        paths.preflight,
        code="preflight_record_missing_or_invalid",
        label="accepted preflight record is unavailable",
    )
    if payload is None:
        return findings
    if payload.get("preflightContract") != "InstallerSizePreflightV1" or payload.get("runId") != context.run_id:
        findings.append(finding("block", "preflight_record_identity_mismatch", "preflight record contract or RunId is invalid"))
    if payload.get("accepted") is not True:
        findings.append(finding("block", "preflight_not_accepted", "preflight record is not accepted"))
    recorded = payload.get("evidenceHashes")
    if not isinstance(recorded, dict) or recorded != dict(sorted(evidence.items())):
        findings.append(finding("block", "preflight_evidence_drift", "preflight evidence hashes changed after acceptance"))
    return findings


def validate_baseline_state(context) -> list[dict[str, Any]]:
    paths = paths_for(context)
    findings: list[dict[str, Any]] = []
    inventory, errors = _load_optional(
        paths.baseline_replica(1),
        code="baseline_inventory_missing_or_invalid",
        label="baseline inventory is unavailable",
    )
    findings.extend(errors)
    baseline, errors = _load_optional(
        paths.baseline,
        code="baseline_summary_missing_or_invalid",
        label="accepted baseline summary is unavailable",
    )
    findings.extend(errors)
    if baseline is None or inventory is None:
        return findings
    try:
        expected = accept_baseline(
            inventory,
            first_inventory_sha256=file_sha256(paths.baseline_replica(1)),
        )
    except InventoryError as exc:
        return [
            *findings,
            finding(
                "block",
                "baseline_inventory_contract_mismatch",
                f"the authoritative baseline evaluator rejected the inventory: {exc}",
            ),
        ]
    if expected.get("accepted") is not True:
        findings.append(
            finding(
                "block",
                "baseline_inventory_rejected",
                "the authoritative baseline evaluator rejected the baseline inventory",
                reasonCodes=expected.get("reasonCodes", []),
            )
        )
    expected_without_time = {key: value for key, value in expected.items() if key != "acceptedAtUtc"}
    actual_without_time = {key: value for key, value in baseline.items() if key != "acceptedAtUtc"}
    if actual_without_time != expected_without_time:
        findings.append(
            finding(
                "block",
                "baseline_summary_binding_mismatch",
                "baseline.json does not equal the authoritative acceptance result for its inventory",
            )
        )
    try:
        parse_utc(baseline.get("acceptedAtUtc"), field="baseline.acceptedAtUtc")
    except StateError as exc:
        findings.append(finding("block", "baseline_acceptance_time_invalid", str(exc)))
    if baseline.get("installedBytes") is None:
        findings.append(
            finding(
                "block",
                "baseline_installed_payload_missing",
                "the installer-size baseline must include the installed payload inventory",
            )
        )
    expected_evaluator = current_installer_evaluator_hash(context.repo_root)
    if baseline.get("evaluatorHash") != expected_evaluator:
        findings.append(
            finding(
                "block",
                "baseline_evaluator_hash_mismatch",
                "baseline evaluatorHash does not match the frozen installer evaluator",
            )
        )
    if baseline.get("toolchainHash") != file_sha256(paths.toolchain_manifest):
        findings.append(
            finding(
                "block",
                "baseline_toolchain_hash_mismatch",
                "baseline toolchainHash does not match the pinned toolchain manifest",
            )
        )
    component_map_path = context.repo_root / "packaging" / "installer-component-map-v1.json"
    if baseline.get("componentMapSha256") != file_sha256(component_map_path):
        findings.append(
            finding(
                "block",
                "baseline_component_map_hash_mismatch",
                "baseline component map hash does not match the frozen map",
            )
        )
    return findings


def _common_checks(context) -> tuple[list[dict[str, Any]], dict[str, str]]:
    findings: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    try:
        session_init = load_session_init(context)
        manifest = load_manifest(context) if paths_for(context).manifest.is_file() else None
        load_progress(context)
        read_ledger(paths_for(context).ledger)
    except StateError as exc:
        return [finding("block", "run_state_invalid", str(exc))], evidence
    drift = protected_drift(context, manifest or session_init)
    if drift:
        findings.append(finding("block", "protected_input_drift", "frozen installer-size evaluator inputs changed", drift=drift))
    timing_config = context.config.get("installTiming")
    combined_config = context.config.get("finalCombinedImprovement")
    if (
        not isinstance(timing_config, dict)
        or timing_config.get("pairCount") != MINIMUM_TIMING_PAIR_COUNT
        or timing_config.get("warmupPerVariant") != 1
        or context.config.get("maximumInstallRegressionFraction") != 0.05
        or not isinstance(combined_config, dict)
        or combined_config.get("nanoseconds")
        != MINIMUM_COMBINED_IMPROVEMENT_NANOSECONDS
        or combined_config.get("fraction") != 0.01
    ):
        findings.append(
            finding(
                "block",
                "installer_timing_policy_mismatch",
                "installer timing policy must remain frozen at 20 pairs, one warm-up, five-percent maximum regression, and max(0.5 seconds, 1 percent) combined improvement",
            )
        )
    lane_policy = context.config.get("lanePolicy")
    expected_lane_policy = {
        "betaPriorAlpha": 1.0,
        "betaPriorBeta": 1.0,
        "ewmaAlpha": 0.5,
        "lockAfterValidDiscards": 3,
        "plateauAfterValidDiscards": 10,
        "explorationEveryPackets": 4,
        "minimumExplorationPotentialBytes": 1_048_576,
    }
    if not isinstance(lane_policy, dict) or any(
        lane_policy.get(name) != value for name, value in expected_lane_policy.items()
    ):
        findings.append(
            finding(
                "block",
                "lane_learning_policy_mismatch",
                "lane Beta/EWMA, lock, plateau, and exploration policy differs from the frozen campaign",
            )
        )
    try:
        source = git_snapshot(context.repo_root)
        if source["dirtyEntries"]:
            findings.append(finding("block", "worktree_dirty", "installer-size measurement requires a clean worktree", dirtyEntries=source["dirtyEntries"]))
    except StateError as exc:
        findings.append(finding("block", "git_state_unavailable", str(exc)))
    minimum_free = context.config.get("minimumFreeBytes")
    free_bytes = shutil.disk_usage(context.repo_root).free
    if not isinstance(minimum_free, int) or free_bytes < minimum_free:
        findings.append(finding("block", "insufficient_disk_space", "installer-size research requires at least 50 GiB free", freeBytes=free_bytes, requiredBytes=minimum_free))
    if os.name != "nt" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        findings.append(finding("block", "unsupported_research_platform", "installer-size research requires native Windows x64"))
    processes = list_scriber_processes()
    if processes:
        findings.append(
            finding(
                "block",
                "preexisting_scriber_process",
                "Scriber processes must be stopped before installer research",
                processes=[{"processId": item.get("ProcessId"), "name": item.get("Name")} for item in processes],
            )
        )
    preflight_findings, evidence = _preflight_evidence(context)
    findings.extend(preflight_findings)
    return findings, evidence


def run_doctor(
    context,
    *,
    phase: str,
    now: datetime | None = None,
    allow_unarmed_run: bool = False,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown doctor phase {phase!r}")
    findings, evidence = _common_checks(context)
    paths = paths_for(context)
    progress: dict[str, Any] = {}
    try:
        progress = load_progress(context)
    except StateError:
        pass
    if phase == "prepare":
        if progress.get("researchStartedAtUtc"):
            findings.append(finding("block", "prepare_after_research_start", "prepare doctor cannot run after the research clock started"))
        if paths.preflight.is_file():
            findings.extend(validate_preflight_record(context, evidence))
    else:
        findings.extend(validate_preflight_record(context, evidence))
        findings.extend(validate_baseline_state(context))
        if not allow_unarmed_run and not progress.get("researchStartedAtUtc"):
            findings.append(finding("block", "research_clock_not_started", "run doctor requires an armed research clock"))
        if progress.get("researchStartedAtUtc") and phase == "run":
            deadline = parse_utc(progress.get("researchDeadlineUtc"), field="researchDeadlineUtc")
            if (now or utc_now()) >= deadline:
                findings.append(finding("block", "research_deadline_elapsed", "the immutable 12-hour research deadline has elapsed"))
    if phase == "finalize":
        champion, errors = _load_optional(
            paths.champion,
            code="champion_missing_or_invalid",
            label="research champion is unavailable",
        )
        findings.extend(errors)
        if champion is not None:
            findings.extend(validate_result(champion, expected_run_id=str(context.run_id)))
            if champion.get("decision") != "keep":
                findings.append(finding("block", "champion_decision_invalid", "research champion must be a kept result"))
    blocked = _blocking(findings)
    return {
        "doctorContract": "InstallerSizeDoctorV1",
        "schemaVersion": 1,
        "profile": "installer-size",
        "runId": context.run_id,
        "phase": phase,
        "ok": not blocked,
        "blocked": blocked,
        "findings": findings or [finding("ok", "installer_size_doctor_ready", f"installer-size {phase} doctor passed")],
        "evidenceHashes": dict(sorted(evidence.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Scriber installer-size AutoResearch readiness.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--phase", choices=PHASES, default="prepare")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)
    try:
        context = resolve_profile_context(
            Path(args.repo_root),
            profile="installer-size",
            run_id=args.run_id,
            duration_seconds=args.duration_seconds,
            require_run_id=True,
        )
        payload = run_doctor(context, phase=args.phase)
    except (ProfileError, StateError, ValueError) as exc:
        payload = {
            "doctorContract": "InstallerSizeDoctorV1",
            "schemaVersion": 1,
            "profile": "installer-size",
            "runId": args.run_id,
            "phase": args.phase,
            "ok": False,
            "blocked": True,
            "findings": [finding("block", "doctor_invocation_invalid", str(exc))],
            "evidenceHashes": {},
        }
    if args.explain:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("doctor: ok" if payload["ok"] else "doctor: blocked")
        for item in payload["findings"]:
            print(f"- {item.get('level')}: {item.get('code')} - {item.get('message')}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
