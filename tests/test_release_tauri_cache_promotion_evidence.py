from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "write_tauri_cache_promotion_evidence.ps1"
PRUNE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "prune_obsolete_release_caches.ps1"
CLI_CONTRACT_PATH = REPO_ROOT / "packaging" / "tauri-cli-cache-contract.json"
REPO = "MyButtermilk/Scriber"
HEAD_SHA = "9" * 40
WORKFLOW_ID = 314
MANIFEST_FINGERPRINT = "a" * 64
FINGERPRINT = hashlib.sha256(bytes.fromhex(MANIFEST_FINGERPRINT)).hexdigest()
CACHE_KEY = f"scriber-tauri-app-binary-v3-Windows-{FINGERPRINT}"
CLI_CONTRACT = json.loads(CLI_CONTRACT_PATH.read_text(encoding="utf-8"))
CLI_CONTRACT_SHA256 = hashlib.sha256(CLI_CONTRACT_PATH.read_bytes()).hexdigest()
CLI_VERSION_OUTPUT = str(CLI_CONTRACT["versionOutput"])
VERSION = "1.2.3"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is required for Tauri promotion evidence tests")
    return executable


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def _invoke(
    arguments: dict[str, str | Path],
    *,
    executable: str | None = None,
) -> tuple[dict[str, object], subprocess.CompletedProcess[str]]:
    command = [executable or _powershell(), "-NoProfile", "-File", str(SCRIPT)]
    for name, value in arguments.items():
        command.extend((f"-{name}", str(value)))
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )

    result = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("schemaVersion") == 3:
            result = candidate
            break
    assert result is not None, completed.stdout
    return result, completed


def _install_fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True)
    fake_script = fake_bin / "fake_gh.py"
    fake_script.write_text(
        """
import json
import os
from pathlib import Path
import sys


args = sys.argv[1:]


def emit_env(name, fallback):
    print(os.environ.get(name, fallback))


def record_mutation(kind, value):
    path = Path(os.environ["FAKE_GH_MUTATION_LOG"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "value": value}) + "\\n")


if args[:2] == ["cache", "list"]:
    emit_env("FAKE_GH_CACHES", "[]")
elif args[:2] == ["cache", "delete"]:
    record_mutation("cache-delete", int(args[2]))
elif args[:2] == ["release", "list"]:
    print("[]")
elif args[:2] == ["release", "view"]:
    print('{"assets": []}')
elif args[:2] == ["release", "delete"]:
    record_mutation("release-delete", args[2])
elif args[:2] == ["release", "delete-asset"]:
    record_mutation("release-asset-delete", "/".join(args[2:4]))
elif args[:1] == ["api"]:
    if args != ["api", "/repos/MyButtermilk/Scriber/actions/runs/42/attempts/2"]:
        print(f"unexpected exact-run API arguments: {args!r}", file=sys.stderr)
        raise SystemExit(2)
    emit_env("FAKE_GH_RUN", "{}")
else:
    print(f"unexpected fake gh arguments: {args!r}", file=sys.stderr)
    raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = fake_bin / "gh.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = fake_bin / "gh"
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(fake_script))} \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return fake_bin, tmp_path / "mutations.jsonl"


def _invoke_pruner_with_completed_run(
    tmp_path: Path,
    evidence_path: Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    fake_bin, mutation_log = _install_fake_gh(tmp_path)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["GITHUB_REF"] = "refs/heads/main"
    env["FAKE_GH_MUTATION_LOG"] = str(mutation_log)
    env["FAKE_GH_CACHES"] = json.dumps(
        [
            {
                "id": 10,
                "key": f"scriber-tauri-app-binary-v2-Windows-{'e' * 64}",
                "ref": "refs/heads/main",
                "sizeInBytes": 1024,
                "createdAt": "2026-07-17T10:00:00Z",
                "lastAccessedAt": "2026-07-17T10:00:00Z",
            },
            {
                "id": 123,
                "key": CACHE_KEY,
                "ref": "refs/heads/main",
                "sizeInBytes": 2048,
                "createdAt": "2026-07-17T12:00:00Z",
                "lastAccessedAt": "2026-07-17T12:00:00Z",
            },
        ]
    )
    env["FAKE_GH_RUN"] = json.dumps(
        {
            "id": 42,
            "run_attempt": 2,
            "head_sha": HEAD_SHA,
            "head_branch": "main",
            "workflow_id": WORKFLOW_ID,
            "name": "Release Windows",
            "path": ".github/workflows/release-windows.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "updated_at": _utc_text(datetime.now(timezone.utc)),
            "repository": {"full_name": REPO},
        }
    )
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PRUNE_SCRIPT),
            "-Repo",
            REPO,
            "-Apply",
            "-TauriPromotionEvidencePath",
            str(evidence_path),
            "-ExpectedTauriPromotionEvidenceSha256",
            evidence_sha256,
            "-ExpectedTauriAppKey",
            CACHE_KEY,
            "-ExpectedSourceRunId",
            "42",
            "-ExpectedSourceRunAttempt",
            "2",
            "-ExpectedSourceHeadSha",
            HEAD_SHA,
            "-ExpectedSourceHeadBranch",
            "main",
            "-ExpectedSourceWorkflowId",
            str(WORKFLOW_ID),
            "-ExpectedSourceWorkflowName",
            "Release Windows",
            "-ExpectedSourceWorkflowPath",
            ".github/workflows/release-windows.yml",
            "-ExpectedSourceEvent",
            "workflow_dispatch",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
    result = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("apiVersion") == "1":
            result = candidate
            break
    assert result is not None, completed.stdout
    mutations = []
    if mutation_log.exists():
        mutations = [
            json.loads(line)
            for line in mutation_log.read_text(encoding="utf-8").splitlines()
        ]
    return result, mutations


def _valid_setup(tmp_path: Path) -> tuple[dict[str, str | Path], dict[str, Path]]:
    now = datetime.now(timezone.utc)
    summary_time = now - timedelta(minutes=10)
    latest_time = now - timedelta(minutes=4)
    timing_time = now - timedelta(minutes=2)

    bundle = tmp_path / f"Scriber_{VERSION}_x64-setup.exe"
    bundle.write_bytes(b"offline installer fixture\n")
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()

    summary = tmp_path / "release-cache-summary.json"
    _write_json(
        summary,
        {
            "schemaVersion": 2,
            "generatedAtUtc": _utc_text(summary_time),
            "runIdentity": {
                "repository": REPO,
                "runId": 42,
                "runAttempt": 2,
                "headSha": HEAD_SHA,
                "ref": "refs/heads/main",
                "eventName": "workflow_dispatch",
            },
            # This is the manifest fingerprint, not the double-hashed Actions key suffix.
            "cacheKeyParity": {
                "apiVersion": "1",
                "planner": {"tauriAppBinary": MANIFEST_FINGERPRINT},
                "packager": {"tauriAppBinary": MANIFEST_FINGERPRINT},
                "componentMatches": {"tauriAppBinary": True},
                "allMatch": True,
            },
            "coldProductsUsed": False,
            "tauriAppBinary": {
                "actionsCacheExact": True,
                "importUsable": True,
                "importedSha256": "c" * 64,
                "cliAttested": True,
                "frontendDependenciesRequired": False,
            },
            "rows": [
                {
                    "Name": "Tauri app binary",
                    "Actions": "exact",
                    "ReleaseArtifact": "n/a",
                    "Effective": "actions-cache-exact-validated",
                }
            ],
        },
    )

    timing = tmp_path / "build-timing.json"
    _write_json(
        timing,
        {
            "apiVersion": "1",
            "generatedAt": _utc_text(timing_time),
            "totalDurationMs": 1_500,
            "phases": [
                {"label": "Python sidecar preparation", "durationMs": 300, "ok": True},
                {"label": "Tauri Windows bundle", "durationMs": 1_200, "ok": True},
            ],
            "buildMode": {
                "artifactKind": "installer",
                "prebuiltTauriApp": True,
                "isolatedTauriCli": True,
                "tauriAppBuiltBeforeBundle": False,
                "tauriAppBuiltInParallel": False,
                "installerBuilt": True,
            },
        },
    )

    latest = tmp_path / "latest.json"
    _write_json(
        latest,
        {
            "version": VERSION,
            "notes": "",
            "pub_date": _utc_text(latest_time),
            "platforms": {
                "windows-x86_64": {
                    "signature": "",
                    "url": bundle.name,
                }
            },
            "artifacts": [
                {
                    "name": bundle.name,
                    "url": bundle.name,
                    "sha256": bundle_sha256,
                    "sizeBytes": bundle.stat().st_size,
                    "signature": "",
                }
            ],
        },
    )

    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(f"{bundle_sha256}  {bundle.name}\n", encoding="utf-8")
    output = tmp_path / "tauri-cache-promotion-evidence.json"

    arguments: dict[str, str | Path] = {
        "Repo": REPO,
        "Ref": "refs/heads/main",
        "CacheRef": "refs/heads/main",
        "RunId": "42",
        "RunAttempt": "2",
        "HeadSha": HEAD_SHA,
        "HeadBranch": "main",
        "WorkflowId": str(WORKFLOW_ID),
        "WorkflowName": "Release Windows",
        "WorkflowPath": ".github/workflows/release-windows.yml",
        "EventName": "workflow_dispatch",
        "CacheId": "123",
        "ExpectedCacheKey": CACHE_KEY,
        "ActionsCacheKey": CACHE_KEY,
        "ActionsCacheHit": "true",
        "ImportUsable": "true",
        "ColdProductsUsable": "false",
        "UsePrebuiltTauriApp": "true",
        "FrontendDependenciesRequired": "false",
        "ImportCliContractSha256": CLI_CONTRACT_SHA256,
        "SelectedCliContractSha256": CLI_CONTRACT_SHA256,
        "ImportCliVersionOutput": CLI_VERSION_OUTPUT,
        "SelectedCliVersionOutput": CLI_VERSION_OUTPUT,
        "TauriExportOutcome": "skipped",
        "TauriSaveOutcome": "skipped",
        "FrontendNodeModulesRestoreOutcome": "skipped",
        "NpmPackageStoreRestoreOutcome": "skipped",
        "FrontendInstallOutcome": "skipped",
        "FrontendCachedUseOutcome": "skipped",
        "FrontendDependencySaveOutcome": "skipped",
        "NpmPackageStoreSaveOutcome": "skipped",
        "CacheSummaryPath": summary,
        "BuildTimingPath": timing,
        "LatestJsonPath": latest,
        "Sha256SumsPath": sums,
        "BundlePath": bundle,
        "ExpectedVersion": VERSION,
        "OutputPath": output,
    }
    paths = {
        "bundle": bundle,
        "summary": summary,
        "timing": timing,
        "latest": latest,
        "sums": sums,
        "output": output,
    }
    return arguments, paths


def test_valid_exact_hit_writes_pruner_compatible_evidence(tmp_path: Path) -> None:
    arguments, paths = _valid_setup(tmp_path)

    result, _ = _invoke(arguments)

    evidence_bytes = paths["output"].read_bytes()
    assert not evidence_bytes.startswith(b"\xef\xbb\xbf")
    evidence = json.loads(evidence_bytes)
    assert result == evidence
    assert evidence["eligible"] is True
    assert evidence["reason"] == "eligible"
    assert evidence["reasons"] == []
    assert evidence["schemaVersion"] == 3
    assert evidence["consumption"] == {
        "mode": "deferred-terminal-run",
        "producerRunMustBeCompleted": True,
    }
    assert evidence["checks"] == {
        "evaluated": True,
        "identity": True,
        "cacheReuse": True,
        "cacheKeyBinding": True,
        "cliContract": True,
        "cliVersion": True,
        "stepIsolation": True,
        "cacheSummary": True,
        "buildTiming": True,
        "releaseMetadata": True,
        "bundleIdentity": True,
    }
    assert evidence["repo"] == REPO
    assert evidence["cache"] == {
        "id": 123,
        "key": CACHE_KEY,
        "ref": "refs/heads/main",
        "generation": 3,
        "fingerprint": FINGERPRINT,
        "manifestFingerprint": MANIFEST_FINGERPRINT,
    }
    assert evidence["run"] == {
        "id": 42,
        "attempt": 2,
        "headSha": HEAD_SHA,
        "headBranch": "main",
        "workflowId": WORKFLOW_ID,
        "workflowName": "Release Windows",
        "workflowPath": ".github/workflows/release-windows.yml",
        "event": "workflow_dispatch",
    }
    assert evidence["reuse"] == {
        "exactCacheHit": True,
        "imported": True,
        "seedMiss": False,
    }
    assert evidence["cliContract"] == {
        "sha256": CLI_CONTRACT_SHA256,
        "version": CLI_CONTRACT["version"],
        "versionOutput": CLI_VERSION_OUTPUT,
    }
    assert evidence["bundle"] == {
        "name": paths["bundle"].name,
        "length": paths["bundle"].stat().st_size,
        "sha256": hashlib.sha256(paths["bundle"].read_bytes()).hexdigest(),
    }


def test_valid_exact_hit_is_also_accepted_by_workflow_pwsh(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for workflow timestamp parsing coverage")
    arguments, _ = _valid_setup(tmp_path)

    result, _ = _invoke(arguments, executable=pwsh)

    assert result["eligible"] is True
    assert result["schemaVersion"] == 3


def test_writer_output_is_consumed_only_by_a_later_completed_run_pruner(
    tmp_path: Path,
) -> None:
    producer_root = tmp_path / "producer"
    producer_root.mkdir(parents=True)
    arguments, paths = _valid_setup(producer_root)

    evidence, _ = _invoke(arguments)
    result, mutations = _invoke_pruner_with_completed_run(
        tmp_path / "terminal-consumer",
        paths["output"],
    )

    assert evidence["consumption"] == {
        "mode": "deferred-terminal-run",
        "producerRunMustBeCompleted": True,
    }
    assert result["tauriPromotionAuthorized"] is True, result["tauriPromotionReason"]
    assert result["tauriPromotionReason"] == "validated-successful-cache-reuse"
    assert result["tauriPromotedCache"] == {
        "id": 123,
        "key": CACHE_KEY,
        "ref": "refs/heads/main",
        "generation": 3,
        "fingerprint": FINGERPRINT,
    }
    assert result["tauriPromotionDeletedIds"] == [10]
    assert mutations == [{"kind": "cache-delete", "value": 10}]


def test_seed_miss_is_non_error_without_build_fixtures_and_replaces_old_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "tauri-cache-promotion-evidence.json"
    output.write_text('{"eligible":true}', encoding="utf-8")
    arguments: dict[str, str | Path] = {
        "Repo": REPO,
        "Ref": "refs/heads/main",
        "CacheRef": "refs/heads/main",
        "RunId": "42",
        "RunAttempt": "2",
        "HeadSha": HEAD_SHA,
        "HeadBranch": "main",
        "WorkflowId": str(WORKFLOW_ID),
        "WorkflowName": "Release Windows",
        "WorkflowPath": ".github/workflows/release-windows.yml",
        "EventName": "workflow_dispatch",
        "ExpectedCacheKey": CACHE_KEY,
        "ActionsCacheHit": "false",
        "OutputPath": output,
    }

    result, _ = _invoke(arguments)

    assert result["eligible"] is False
    assert result["reason"] == "seed-miss"
    assert result["reasons"] == ["seed-miss"]
    assert result["reuse"] == {
        "exactCacheHit": False,
        "imported": False,
        "seedMiss": True,
    }
    assert result["checks"]["evaluated"] is False
    assert json.loads(output.read_bytes()) == result
    assert list(tmp_path.glob(f"{output.name}.tmp.*")) == []
    assert list(tmp_path.glob(f"{output.name}.backup.*")) == []


@pytest.mark.parametrize(
    ("parameter", "replacement", "expected_reason"),
    [
        ("ActionsCacheHit", "TRUE", "actions-cache-hit-invalid"),
        ("ImportUsable", "false", "normal-import-not-usable"),
        ("ColdProductsUsable", "true", "cold-product-selected"),
        ("ExpectedCacheKey", "incomplete", "expected-cache-key-incomplete"),
        ("ActionsCacheKey", CACHE_KEY + "-wrong", "actions-cache-key-mismatch"),
        ("CacheRef", "refs/heads/other", "cache-ref-mismatch"),
        ("Ref", "refs/heads/canary", "source-ref-not-main"),
        ("Ref", "refs/tags/v1.2.3", "ref-invalid"),
        ("HeadBranch", "canary", "source-branch-not-main"),
        ("WorkflowId", "0", "workflow-id-invalid"),
        ("WorkflowName", "Other Workflow", "workflow-name-invalid"),
        ("WorkflowPath", ".github/workflows/other.yml", "workflow-path-invalid"),
        ("EventName", "push", "workflow-event-invalid"),
        ("ImportCliContractSha256", "e" * 64, "cli-contract-sha-mismatch"),
        ("SelectedCliVersionOutput", "tauri-cli 2.11.4", "cli-version-output-mismatch"),
        ("UsePrebuiltTauriApp", "false", "prebuilt-tauri-not-selected"),
        ("FrontendDependenciesRequired", "true", "frontend-dependencies-required"),
        ("TauriExportOutcome", "success", "tauri-export-not-skipped"),
        ("TauriSaveOutcome", "success", "tauri-save-not-skipped"),
        (
            "FrontendNodeModulesRestoreOutcome",
            "success",
            "frontend-node-modules-restore-not-skipped",
        ),
        ("NpmPackageStoreRestoreOutcome", "success", "npm-package-store-restore-not-skipped"),
        ("FrontendInstallOutcome", "success", "frontend-install-not-skipped"),
        ("FrontendCachedUseOutcome", "success", "frontend-cached-use-not-skipped"),
        ("FrontendDependencySaveOutcome", "success", "frontend-dependency-save-not-skipped"),
        ("NpmPackageStoreSaveOutcome", "success", "npm-package-store-save-not-skipped"),
    ],
)
def test_exact_hit_input_invariant_breaks_are_machine_readable(
    tmp_path: Path,
    parameter: str,
    replacement: str,
    expected_reason: str,
) -> None:
    arguments, paths = _valid_setup(tmp_path)
    arguments[parameter] = replacement

    result, _ = _invoke(arguments)

    assert result["eligible"] is False
    assert result["reason"] == "invariant-failed"
    assert expected_reason in result["reasons"]
    assert result["reuse"] == {
        "exactCacheHit": False,
        "imported": False,
        "seedMiss": False,
    }
    assert json.loads(paths["output"].read_bytes()) == result


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("summary-row", "cache-summary-tauri-row-mismatch"),
        ("summary-stale", "cache-summary-stale"),
        ("summary-key-binding", "cache-summary-key-binding-mismatch"),
        ("timing-before-summary", "build-timing-stale-or-before-summary"),
        ("duplicate-bundle-phase", "tauri-windows-bundle-phase-invalid"),
        ("failed-bundle-phase", "tauri-windows-bundle-phase-invalid"),
        ("fresh-tauri-build-phase", "unexpected-tauri-app-build-phase"),
        ("build-mode", "build-mode-not-isolated-prebuilt-tauri"),
        ("latest-version", "latest-json-version-mismatch"),
        ("latest-sha", "latest-json-bundle-identity-mismatch"),
        ("checksum", "sha256sums-bundle-identity-mismatch"),
    ],
)
def test_exact_hit_fixture_invariant_breaks_are_machine_readable(
    tmp_path: Path,
    case: str,
    expected_reason: str,
) -> None:
    arguments, paths = _valid_setup(tmp_path)

    if case.startswith("summary-"):
        summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
        if case == "summary-row":
            summary["rows"][0]["Effective"] = "actions-cache-exact-rejected"
        elif case == "summary-stale":
            summary["generatedAtUtc"] = _utc_text(
                datetime.now(timezone.utc) - timedelta(hours=25)
            )
        else:
            summary["cacheKeyParity"]["planner"]["tauriAppBinary"] = "b" * 64
            summary["cacheKeyParity"]["packager"]["tauriAppBinary"] = "b" * 64
        _write_json(paths["summary"], summary)
    elif case in {
        "timing-before-summary",
        "duplicate-bundle-phase",
        "failed-bundle-phase",
        "fresh-tauri-build-phase",
        "build-mode",
    }:
        timing = json.loads(paths["timing"].read_text(encoding="utf-8"))
        if case == "timing-before-summary":
            timing["generatedAt"] = _utc_text(
                datetime.now(timezone.utc) - timedelta(minutes=20)
            )
        elif case == "duplicate-bundle-phase":
            timing["phases"].append(
                {"label": "Tauri Windows bundle", "durationMs": 1, "ok": True}
            )
        elif case == "failed-bundle-phase":
            timing["phases"][1]["ok"] = False
        elif case == "fresh-tauri-build-phase":
            timing["phases"].append(
                {"label": "Tauri app binary build", "durationMs": 1, "ok": True}
            )
        else:
            timing["buildMode"]["isolatedTauriCli"] = False
        _write_json(paths["timing"], timing)
    elif case.startswith("latest-"):
        latest = json.loads(paths["latest"].read_text(encoding="utf-8"))
        if case == "latest-version":
            latest["version"] = "9.9.9"
        else:
            latest["artifacts"][0]["sha256"] = "0" * 64
        _write_json(paths["latest"], latest)
    else:
        paths["sums"].write_text(
            f"{'0' * 64}  {paths['bundle'].name}\n",
            encoding="utf-8",
        )

    result, _ = _invoke(arguments)

    assert result["eligible"] is False
    assert result["reason"] == "invariant-failed"
    assert expected_reason in result["reasons"]
    assert result["reuse"]["exactCacheHit"] is False


def test_missing_or_malformed_exact_hit_fixtures_fail_closed(tmp_path: Path) -> None:
    arguments, paths = _valid_setup(tmp_path)
    paths["summary"].write_bytes(b"{")
    arguments["BuildTimingPath"] = tmp_path / "missing-build-timing.json"

    result, _ = _invoke(arguments)

    assert result["eligible"] is False
    assert result["reason"] == "invariant-failed"
    assert "cache-summary-malformed" in result["reasons"]
    assert "build-timing-missing" in result["reasons"]
    assert result["reuse"] == {
        "exactCacheHit": False,
        "imported": False,
        "seedMiss": False,
    }


def test_writer_uses_real_file_entrypoint_without_dynamic_powershell() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Invoke-Expression" not in source
    assert "EncodedCommand" not in source
    assert "gh " not in source
    assert "deferred-terminal-run" in source
    assert "Convert-ManifestFingerprintToHashFilesFingerprint" in source
    assert "packaging\\tauri-cli-cache-contract.json" in source


def test_checked_in_cli_contract_is_the_exact_lf_only_trust_anchor() -> None:
    raw = CLI_CONTRACT_PATH.read_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert CLI_CONTRACT_SHA256 == hashlib.sha256(raw).hexdigest()
    assert CLI_VERSION_OUTPUT == f"tauri-cli {CLI_CONTRACT['version']}"
