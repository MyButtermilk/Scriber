from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "promote_tauri_cache_from_terminal_run.ps1"
KEY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "write_release_cache_keys.ps1"
CLI_CONTRACT_PATH = REPO_ROOT / "packaging" / "tauri-cli-cache-contract.json"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
MAINTENANCE_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "release-cache-maintenance.yml"
)
REPO = "MyButtermilk/Scriber"
RUN_ID = 42
RUN_ATTEMPT = 2
HEAD_SHA = "9" * 40
WORKFLOW_ID = 314
ARTIFACT_NAME = "scriber-tauri-cache-promotion-evidence"
EVIDENCE_NAME = "tauri-cache-promotion-evidence.json"


def _powershell() -> str:
    # The terminal maintenance workflow runs with shell: pwsh. The checked-in
    # scripts are parsed separately with Windows PowerShell 5.1 below.
    executable = shutil.which("pwsh") or shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is required for terminal Tauri promotion tests")
    return executable


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _derive_current_tauri_identity() -> tuple[str, str]:
    unique = f"test-terminal-promotion-{uuid.uuid4().hex}"
    relative_output = Path("build") / unique / "cache-keys"
    output = REPO_ROOT / relative_output
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(KEY_SCRIPT),
            "-OutputDir",
            str(relative_output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    try:
        assert completed.returncode == 0, completed.stderr
        manifest = hashlib.sha256((output / "tauri-app-binary.txt").read_bytes()).hexdigest()
        actions = hashlib.sha256(bytes.fromhex(manifest)).hexdigest()
        return manifest, f"scriber-tauri-app-binary-v3-Windows-{actions}"
    finally:
        shutil.rmtree(REPO_ROOT / "build" / unique, ignore_errors=True)


@pytest.fixture(scope="module")
def tauri_identity() -> tuple[str, str]:
    return _derive_current_tauri_identity()


def _cache(
    cache_id: int,
    key: str,
    *,
    ref: str = "refs/heads/main",
    created: str = "2026-07-17T10:00:00Z",
) -> dict[str, object]:
    return {
        "id": cache_id,
        "key": key,
        "ref": ref,
        "sizeInBytes": cache_id * 1024,
        "createdAt": created,
        "lastAccessedAt": created,
    }


def _evidence(manifest: str, key: str) -> dict[str, object]:
    cli_contract = json.loads(CLI_CONTRACT_PATH.read_text(encoding="utf-8"))
    checks = {
        name: True
        for name in (
            "evaluated",
            "identity",
            "cacheReuse",
            "cacheKeyBinding",
            "cliContract",
            "cliVersion",
            "stepIsolation",
            "cacheSummary",
            "buildTiming",
            "releaseMetadata",
            "bundleIdentity",
        )
    }
    return {
        "schemaVersion": 3,
        "generatedAtUtc": _utc_text(),
        "repo": REPO,
        "consumption": {
            "mode": "deferred-terminal-run",
            "producerRunMustBeCompleted": True,
        },
        "cache": {
            "id": 123,
            "key": key,
            "ref": "refs/heads/main",
            "generation": 3,
            "fingerprint": key.rsplit("-", 1)[1],
            "manifestFingerprint": manifest,
        },
        "run": {
            "id": RUN_ID,
            "attempt": RUN_ATTEMPT,
            "headSha": HEAD_SHA,
            "headBranch": "main",
            "workflowId": WORKFLOW_ID,
            "workflowName": "Release Windows",
            "workflowPath": ".github/workflows/release-windows.yml",
            "event": "workflow_dispatch",
        },
        "reuse": {"exactCacheHit": True, "imported": True, "seedMiss": False},
        "eligible": True,
        "reason": "eligible",
        "reasons": [],
        "checks": checks,
        "cliContract": {
            "sha256": hashlib.sha256(CLI_CONTRACT_PATH.read_bytes()).hexdigest(),
            "version": cli_contract["version"],
            "versionOutput": cli_contract["versionOutput"],
        },
        "bundle": {
            "name": "Scriber_0.5.37_x64-setup.exe",
            "length": 123,
            "sha256": "f" * 64,
        },
    }


def _base_state(tmp_path: Path, manifest: str, key: str) -> tuple[dict[str, object], Path]:
    remote = tmp_path / "remote-artifact"
    remote.mkdir(parents=True)
    evidence_path = remote / EVIDENCE_NAME
    evidence_path.write_text(json.dumps(_evidence(manifest, key)), encoding="utf-8")
    v1_key = f"scriber-tauri-app-binary-v1-Windows-{'a' * 64}"
    v2_key = f"scriber-tauri-app-binary-v2-Windows-{'b' * 64}"
    branch_key = f"scriber-tauri-app-binary-v2-Windows-{'c' * 64}"
    state: dict[str, object] = {
        "trusted_ref": {
            "ref": "refs/heads/main",
            "object": {
                "type": "commit",
                "sha": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
                ).strip(),
            },
        },
        "run": {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "head_sha": HEAD_SHA,
            "head_branch": "main",
            "workflow_id": WORKFLOW_ID,
            "name": "Release Windows",
            "path": ".github/workflows/release-windows.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "updated_at": _utc_text(),
            "repository": {"full_name": REPO},
        },
        "artifacts": [
            {
                "id": 9001,
                "name": ARTIFACT_NAME,
                "size_in_bytes": evidence_path.stat().st_size,
                "expired": False,
                "workflow_run": {
                    "id": RUN_ID,
                    "head_sha": HEAD_SHA,
                    "head_branch": "main",
                },
            }
        ],
        "remote": str(remote),
        "caches": [
            _cache(10, v1_key),
            _cache(20, v2_key, created="2026-07-17T11:00:00Z"),
            _cache(123, key, created="2026-07-17T12:00:00Z"),
            _cache(77, branch_key, ref="refs/heads/canary"),
            _cache(999, "scriber-python-venv-Windows-unrelated"),
            _cache(998, "setup-python-obsolete"),
        ],
        "releases": [
            {
                "id": 501,
                "tag_name": "v0.5.37",
                "draft": False,
                "prerelease": False,
                "target_commitish": "main",
            },
            {
                "id": 502,
                "tag_name": "release-cache-backend-sidecar-v2",
                "draft": False,
                "prerelease": True,
                "target_commitish": "main",
            },
            {
                "id": 503,
                "tag_name": "vNext-preview",
                "draft": True,
                "prerelease": True,
                "target_commitish": "main",
            },
        ],
        "release_assets": {
            "501": [
                {
                    "id": 601,
                    "name": "Scriber_0.5.37_x64-setup.exe",
                    "size": 1234,
                    "digest": "sha256:" + "d" * 64,
                    "updated_at": "2026-07-17T12:00:00Z",
                }
            ],
            "502": [
                {
                    "id": 602,
                    "name": "backend.zip",
                    "size": 456,
                    "digest": "sha256:" + "e" * 64,
                    "updated_at": "2026-07-17T12:00:00Z",
                }
            ],
            "503": [],
        },
        "calls": [],
        "mutations": [],
        "cache_list_calls": 0,
        "race_on_cache_list_call": 0,
        "trusted_ref_calls": 0,
        "advance_trusted_ref_after_call": 0,
    }
    return state, evidence_path


def _install_fake_gh(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True)
    fake_script = fake_bin / "fake_gh.py"
    fake_script.write_text(
        r'''
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
state["calls"].append(args)

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def fail(message):
    save()
    print(message, file=sys.stderr)
    raise SystemExit(2)

if args[:2] == ["cache", "list"]:
    if args != [
        "cache", "list", "--repo", "MyButtermilk/Scriber", "--limit", "10000",
        "--json", "id,key,ref,sizeInBytes,createdAt,lastAccessedAt",
    ]:
        fail(f"unexpected cache list arguments: {args!r}")
    state["cache_list_calls"] += 1
    if state.get("race_on_cache_list_call") == state["cache_list_calls"]:
        state["caches"].append({
            "id": 88,
            "key": "scriber-tauri-app-binary-v2-Windows-" + "8" * 64,
            "ref": "refs/heads/main",
            "sizeInBytes": 88000,
            "createdAt": "2026-07-17T11:30:00Z",
            "lastAccessedAt": "2026-07-17T11:30:00Z",
        })
    save()
    print(json.dumps(state["caches"]))
elif args[:2] == ["cache", "delete"]:
    if args[3:] != ["--repo", "MyButtermilk/Scriber"]:
        fail(f"unexpected cache delete arguments: {args!r}")
    cache_id = int(args[2])
    before = len(state["caches"])
    state["caches"] = [row for row in state["caches"] if int(row["id"]) != cache_id]
    if len(state["caches"]) != before - 1:
        fail(f"cache id {cache_id} did not exist exactly once")
    state["mutations"].append({"kind": "cache-delete", "value": cache_id})
    save()
elif args == ["api", "/repos/MyButtermilk/Scriber/actions/runs/42/attempts/2"]:
    save()
    print(json.dumps(state["run"]))
elif args == ["api", "/repos/MyButtermilk/Scriber/git/ref/heads/main"]:
    state["trusted_ref_calls"] += 1
    response = json.loads(json.dumps(state["trusted_ref"]))
    if state.get("advance_trusted_ref_after_call") == state["trusted_ref_calls"]:
        state["trusted_ref"]["object"]["sha"] = "6" * 40
    save()
    print(json.dumps(response))
elif args == [
    "api", "--paginate", "--slurp",
    "/repos/MyButtermilk/Scriber/actions/runs/42/artifacts?per_page=100",
]:
    save()
    print(json.dumps([{"total_count": len(state["artifacts"]), "artifacts": state["artifacts"]}]))
elif args == [
    "api", "--paginate", "--slurp",
    "/repos/MyButtermilk/Scriber/releases?per_page=100",
]:
    save()
    print(json.dumps([state["releases"]]))
elif len(args) == 4 and args[:3] == ["api", "--paginate", "--slurp"] and args[3].startswith(
    "/repos/MyButtermilk/Scriber/releases/"
) and args[3].endswith("/assets?per_page=100"):
    release_id = args[3].split("/")[5]
    if release_id not in state["release_assets"]:
        fail(f"unexpected protected release id: {release_id}")
    save()
    print(json.dumps([state["release_assets"][release_id]]))
elif args[:3] == ["run", "download", "42"]:
    expected_prefix = [
        "run", "download", "42", "--repo", "MyButtermilk/Scriber", "--name",
        "scriber-tauri-cache-promotion-evidence", "--dir",
    ]
    if args[:8] != expected_prefix or len(args) != 9:
        fail(f"unexpected artifact download arguments: {args!r}")
    destination = Path(args[8])
    destination.mkdir(parents=True, exist_ok=True)
    for child in Path(state["remote"]).iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    save()
elif args[:1] == ["release"]:
    fail(f"release CLI mutation/query is forbidden in Tauri promotion mode: {args!r}")
else:
    fail(f"unexpected fake gh arguments: {args!r}")
'''.lstrip(),
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
    return fake_bin


def _run_terminal(
    tmp_path: Path,
    state: dict[str, object],
    *,
    source_branch: str = "main",
    expect_success: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, object] | None]:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_bin = _install_fake_gh(tmp_path)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    report_path = tmp_path / "terminal-report.json"
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["FAKE_GH_STATE"] = str(state_path)
    env["RUNNER_TEMP"] = str(runner_temp)
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Repo",
            REPO,
            "-SourceRunId",
            str(RUN_ID),
            "-SourceRunAttempt",
            str(RUN_ATTEMPT),
            "-SourceHeadSha",
            HEAD_SHA,
            "-SourceHeadBranch",
            source_branch,
            "-SourceWorkflowId",
            str(WORKFLOW_ID),
            "-SourceWorkflowName",
            "Release Windows",
            "-SourceWorkflowPath",
            ".github/workflows/release-windows.yml",
            "-SourceEvent",
            "workflow_dispatch",
            "-TrustedCheckoutSha",
            str(state["trusted_ref"]["object"]["sha"]),
            "-TrustedDefaultBranch",
            "main",
            "-OutputPath",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if expect_success:
        assert completed.returncode == 0, (
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )
    else:
        assert completed.returncode != 0, completed.stdout
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
    return completed, final_state, report


def test_terminal_promotion_roundtrip_mutates_only_main_tauri_generations(
    tmp_path: Path,
    tauri_identity: tuple[str, str],
) -> None:
    manifest, key = tauri_identity
    state, evidence_path = _base_state(tmp_path, manifest, key)
    evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    initial_release_state = json.dumps(
        [state["releases"], state["release_assets"]], sort_keys=True
    )

    _, final_state, report = _run_terminal(tmp_path, state)

    assert report is not None
    assert report["postflightPassed"] is True
    assert report["artifact"]["evidenceSha256"] == evidence_sha
    assert report["deletedIds"] == [10, 20]
    assert report["protectedReleaseInventory"]["unchanged"] is True
    assert sorted(row["id"] for row in final_state["caches"]) == [77, 123, 998, 999]
    assert final_state["mutations"] == [
        {"kind": "cache-delete", "value": 10},
        {"kind": "cache-delete", "value": 20},
    ]
    assert json.dumps(
        [final_state["releases"], final_state["release_assets"]], sort_keys=True
    ) == initial_release_state
    exact_attempt = [
        "api",
        "/repos/MyButtermilk/Scriber/actions/runs/42/attempts/2",
    ]
    assert final_state["calls"].count(exact_attempt) == 3
    assert final_state["trusted_ref_calls"] == 4
    assert any(call[:3] == ["run", "download", "42"] for call in final_state["calls"])
    assert final_state["cache_list_calls"] == 3
    assert not any(call and call[0] == "release" for call in final_state["calls"])


@pytest.mark.parametrize(
    "case",
    [
        "in-progress",
        "wrong-event",
        "wrong-workflow-id",
        "wrong-workflow-name",
        "wrong-workflow-path",
        "wrong-attempt",
        "wrong-evidence-event",
        "branch-source",
        "untrusted-default-sha",
        "trusted-ref-race",
        "trusted-ref-apply-race",
        "duplicate-artifact",
        "extra-artifact-file",
        "inventory-race",
    ],
)
def test_terminal_promotion_negatives_never_delete(
    tmp_path: Path,
    tauri_identity: tuple[str, str],
    case: str,
) -> None:
    manifest, key = tauri_identity
    state, evidence_path = _base_state(tmp_path, manifest, key)
    source_branch = "main"
    if case == "in-progress":
        state["run"]["status"] = "in_progress"
        state["run"]["conclusion"] = None
    elif case == "wrong-event":
        state["run"]["event"] = "push"
    elif case == "wrong-workflow-id":
        state["run"]["workflow_id"] = WORKFLOW_ID + 1
    elif case == "wrong-workflow-name":
        state["run"]["name"] = "Other Workflow"
    elif case == "wrong-workflow-path":
        state["run"]["path"] = ".github/workflows/other.yml"
    elif case == "wrong-attempt":
        state["run"]["run_attempt"] = RUN_ATTEMPT + 1
    elif case == "wrong-evidence-event":
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["run"]["event"] = "push"
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        state["artifacts"][0]["size_in_bytes"] = evidence_path.stat().st_size
    elif case == "branch-source":
        source_branch = "canary"
    elif case == "untrusted-default-sha":
        state["trusted_ref"]["object"]["sha"] = "7" * 40
    elif case in {"trusted-ref-race", "trusted-ref-apply-race"}:
        state["advance_trusted_ref_after_call"] = (
            1 if case == "trusted-ref-race" else 3
        )
        state["caches"].append(
            _cache(
                124,
                f"scriber-tauri-app-binary-v3-Windows-{'7' * 64}",
                created="2026-07-18T06:00:00Z",
            )
        )
    elif case == "duplicate-artifact":
        duplicate = dict(state["artifacts"][0])
        duplicate["id"] = 9002
        state["artifacts"].append(duplicate)
    elif case == "extra-artifact-file":
        (evidence_path.parent / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif case == "inventory-race":
        state["race_on_cache_list_call"] = 2

    _, final_state, _ = _run_terminal(
        tmp_path,
        state,
        source_branch=source_branch,
        expect_success=False,
    )

    assert final_state["mutations"] == []
    assert not any(call and call[0] == "release" for call in final_state["calls"])
    assert {10, 20, 123, 77, 998, 999}.issubset(
        {int(row["id"]) for row in final_state["caches"]}
    )
    if case in {"trusted-ref-race", "trusted-ref-apply-race"}:
        assert 124 in {int(row["id"]) for row in final_state["caches"]}


def test_absent_passive_artifact_is_a_clean_non_mutating_noop(
    tmp_path: Path,
    tauri_identity: tuple[str, str],
) -> None:
    manifest, key = tauri_identity
    state, _ = _base_state(tmp_path, manifest, key)
    state["artifacts"] = []

    _, final_state, report = _run_terminal(tmp_path, state)

    assert report == {
        "apiVersion": "1",
        "sourceRunId": RUN_ID,
        "outcome": "not-promotable",
        "reason": "passive-evidence-artifact-absent",
        "mutated": False,
    }
    assert final_state["mutations"] == []
    assert final_state["cache_list_calls"] == 0


def test_workflows_bind_passive_producer_to_trusted_terminal_consumer() -> None:
    release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    maintenance_text = MAINTENANCE_WORKFLOW.read_text(encoding="utf-8")

    assert "write_tauri_cache_promotion_evidence.ps1" in release_text
    assert "name: scriber-tauri-cache-promotion-evidence" in release_text
    assert "github.event_name == 'workflow_dispatch'" in release_text
    assert "github.ref == 'refs/heads/main'" in release_text
    assert "steps.tauri-app-binary-cache.outputs.cache-hit == 'true'" in release_text
    assert "github.event.workflow_run.status == 'completed'" in maintenance_text
    assert "github.event.workflow_run.conclusion == 'success'" in maintenance_text
    assert "github.event.workflow_run.event == 'workflow_dispatch'" in maintenance_text
    assert "github.event.workflow_run.head_branch == 'main'" in maintenance_text
    assert "github.event.workflow_run.name == 'Release Windows'" in maintenance_text
    assert (
        "github.event.workflow_run.path == '.github/workflows/release-windows.yml'"
        in maintenance_text
    )
    assert "ref: ${{ github.sha }}" in maintenance_text
    assert "promote_tauri_cache_from_terminal_run.ps1" in maintenance_text
    assert '-TrustedCheckoutSha "${{ github.sha }}"' in maintenance_text
    assert (
        '-TrustedDefaultBranch "${{ github.event.repository.default_branch }}"'
        in maintenance_text
    )
    for script in (SCRIPT, REPO_ROOT / "scripts" / "ci" / "prune_obsolete_release_caches.ps1"):
        text = script.read_text(encoding="utf-8")
        assert "Invoke-Expression" not in text
        assert "-EncodedCommand" not in text
