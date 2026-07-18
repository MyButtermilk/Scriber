from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.perf import autoresearch_profiles
from scripts.perf.autoresearch_profiles import ProfileError, resolve_profile_context
from scripts.perf.installer_size import doctor, evaluator, runner, state


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
FIXED_NOW = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
UX_FILE_HASHES = {
    "scripts/perf/doctor.py": "f52d6bb338bdeb6ee255a4fb6ea68ea6b1154d073ea989bb8d486ede4b5fcea6",
    "scripts/perf/evaluator/local_wux.py": "1f4f1d26e59a9330b47a6b516f0c1fd9d4a76e4e4117573bb999355ad6a4364b",
    "scripts/perf/run.ps1": "ea8baec1f02b83694dda0f41ba3911c2ff1b2f57450e59d943dd0bc263d6687b",
}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def git(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("entrypoint", ["runner.py", "doctor.py"])
def test_installer_size_cli_entrypoints_bootstrap_the_repo_package(entrypoint: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "perf" / "installer_size" / entrypoint),
            "--help",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def profile_config() -> dict:
    return {
        "schemaVersion": 1,
        "profile": "installer-size",
        "campaign": "installer-size-v1-test",
        "durationSeconds": 43200,
        "minimumFreeBytes": 1,
        "referenceCompression": "bzip2",
        "minimumInstallerReduction": {"bytes": 262144, "fraction": 0.0025},
        "maximumInstallRegressionFraction": 0.05,
        "installTiming": {"pairCount": 20, "warmupPerVariant": 1},
        "finalCombinedImprovement": {
            "nanoseconds": 500000000,
            "fraction": 0.01,
        },
        "protectedInputs": ["protected.txt"],
        "finalization": {"minimumReserveSeconds": 5400, "ewmaMultiplier": 1.25},
        "lanePolicy": {
            "betaPriorAlpha": 1.0,
            "betaPriorBeta": 1.0,
            "ewmaAlpha": 0.5,
            "lockAfterValidDiscards": 3,
            "plateauAfterValidDiscards": 10,
            "explorationEveryPackets": 4,
            "minimumExplorationPotentialBytes": 1048576,
        },
    }


def make_repo(tmp_path: Path):
    repo_root = tmp_path / "repo"
    profile_root = repo_root / "scripts" / "perf" / "profiles" / "installer-size"
    profile_root.mkdir(parents=True)
    write_json(profile_root / "config.json", profile_config())
    (profile_root / "GOAL.md").write_text("test installer goal\n", encoding="utf-8")
    holdouts = json.loads(
        (
            REPO_ROOT
            / "scripts"
            / "perf"
            / "profiles"
            / "installer-size"
            / "youtube-holdouts.json"
        ).read_text(encoding="utf-8")
    )
    write_json(profile_root / "youtube-holdouts.json", holdouts)
    (repo_root / "protected.txt").write_text("frozen evaluator\n", encoding="utf-8")
    (repo_root / "requirements-base.txt").write_text(
        "runtime-fixture==1\n", encoding="utf-8"
    )
    (repo_root / "requirements-build.txt").write_text(
        "build-fixture==1\n", encoding="utf-8"
    )
    (repo_root / ".gitignore").write_text("/autoresearch-results/\n", encoding="utf-8")
    git(repo_root, "init")
    git(repo_root, "add", ".")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    context = resolve_profile_context(
        repo_root,
        profile="installer-size",
        run_id=RUN_ID,
        require_run_id=True,
    )
    return repo_root, context


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        pass
    cmd = shutil.which("cmd.exe")
    if not cmd:
        pytest.skip("directory symlinks and Windows junctions are unavailable")
    result = subprocess.run(
        [cmd, "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(
            f"directory symlinks and junctions are unavailable: {result.stderr}"
        )


def test_profile_context_is_immutable_and_namespaced(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)

    assert context.run_id == RUN_ID
    assert context.duration_seconds == 43200
    assert context.run_root == context.repo_root / "autoresearch-results" / "installer-size" / RUN_ID
    with pytest.raises(FrozenInstanceError):
        context.run_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        RUN_ID.upper(),
        "00000000-0000-0000-0000-000000000000",
        "123e4567-e89b-12d3-0456-426614174000",
    ],
)
def test_profile_rejects_noncanonical_or_non_rfc4122_run_ids(
    tmp_path: Path,
    run_id: str,
) -> None:
    repo_root, _context = make_repo(tmp_path)
    with pytest.raises(ProfileError):
        resolve_profile_context(
            repo_root,
            profile="installer-size",
            run_id=run_id,
            require_run_id=True,
        )


def test_ux_profile_rejects_installer_only_arguments(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        resolve_profile_context(tmp_path, profile="ux", run_id=RUN_ID)
    with pytest.raises(ProfileError):
        resolve_profile_context(tmp_path, profile="ux", duration_seconds=43200)


def test_session_init_does_not_claim_a_research_start_or_manifest(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    paths, session_init = state.initialize_run(context, resume=False, now=FIXED_NOW)

    assert paths.session_init.is_file()
    assert not paths.manifest.exists()
    assert session_init["sessionInitContract"] == state.SESSION_INIT_CONTRACT
    assert session_init["researchStartedAtUtc"] is None
    assert session_init["researchDeadlineUtc"] is None
    progress = state.load_progress(context)
    assert progress["phase"] == "prepare"
    assert progress["researchStartedAtUtc"] is None
    assert progress["researchDeadlineUtc"] is None
    assert paths.snapshots_dir.is_dir()
    assert paths.preflight_dir.is_dir()
    assert paths.baselines_dir.is_dir()
    assert paths.packets_dir.is_dir()
    assert paths.packet_results_dir.is_dir()
    assert paths.final_dir.is_dir()
    assert paths.baseline_requirements_base.read_bytes() == (
        context.repo_root / "requirements-base.txt"
    ).read_bytes()
    assert paths.baseline_requirements_build.read_bytes() == (
        context.repo_root / "requirements-build.txt"
    ).read_bytes()
    assert session_init["baselineRequirementSources"] == [
        {
            "name": path.name,
            "length": path.stat().st_size,
            "sha256": state.file_sha256(path),
        }
        for path in (
            paths.baseline_requirements_base,
            paths.baseline_requirements_build,
        )
    ]


@pytest.mark.parametrize(
    "failure_boundary",
    ["requirement-snapshots", "session-init", "progress", "ledger"],
)
def test_fresh_init_process_abort_leaves_only_ignorable_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_boundary: str,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    final_paths = state.paths_for(context)
    staging_parent = context.state_root / ".initializing"

    class SimulatedProcessAbort(BaseException):
        pass

    original_write_bytes = state.write_bytes_atomic
    original_write_json = state.write_json_atomic
    original_append_ledger = state.append_ledger
    original_discard_staging = state._discard_initialization_staging

    # Simulate abrupt termination: the process cannot execute its best-effort
    # cleanup, so a partially populated private staging directory survives.
    monkeypatch.setattr(
        state,
        "_discard_initialization_staging",
        lambda *_args, **_kwargs: None,
    )
    if failure_boundary == "requirement-snapshots":
        write_count = 0

        def abort_after_requirements(path: Path, value: bytes) -> None:
            nonlocal write_count
            original_write_bytes(path, value)
            write_count += 1
            if write_count == 2:
                raise SimulatedProcessAbort

        monkeypatch.setattr(state, "write_bytes_atomic", abort_after_requirements)
    elif failure_boundary in {"session-init", "progress"}:
        write_count = 0
        abort_after = 1 if failure_boundary == "session-init" else 2

        def abort_after_json(path: Path, value: dict) -> None:
            nonlocal write_count
            original_write_json(path, value)
            write_count += 1
            if write_count == abort_after:
                raise SimulatedProcessAbort

        monkeypatch.setattr(state, "write_json_atomic", abort_after_json)
    else:

        def abort_after_ledger(*args, **kwargs):
            original_append_ledger(*args, **kwargs)
            raise SimulatedProcessAbort

        monkeypatch.setattr(state, "append_ledger", abort_after_ledger)

    with pytest.raises(SimulatedProcessAbort):
        state.initialize_run(context, resume=False, now=FIXED_NOW)

    assert not final_paths.root.exists()
    orphaned_attempts = sorted(staging_parent.iterdir())
    assert len(orphaned_attempts) == 1
    assert orphaned_attempts[0].name.startswith(f".{RUN_ID}.")

    monkeypatch.setattr(state, "write_bytes_atomic", original_write_bytes)
    monkeypatch.setattr(state, "write_json_atomic", original_write_json)
    monkeypatch.setattr(state, "append_ledger", original_append_ledger)
    monkeypatch.setattr(
        state,
        "_discard_initialization_staging",
        original_discard_staging,
    )

    paths, session_init = state.initialize_run(
        context,
        resume=False,
        now=FIXED_NOW,
    )
    assert paths.root.is_dir()
    assert session_init == state.load_session_init(context)
    assert state.load_progress(context)["phase"] == "prepare"
    assert [row["event"] for row in state.read_ledger(paths.ledger)] == [
        "run_initialized"
    ]
    assert orphaned_attempts[0].is_dir()


def test_fresh_init_abort_after_atomic_promotion_is_resumable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)

    class SimulatedProcessAbort(BaseException):
        pass

    original_promote = state._promote_initialization_staging

    def promote_then_abort(staging_root: Path, final_root: Path) -> None:
        original_promote(staging_root, final_root)
        raise SimulatedProcessAbort

    monkeypatch.setattr(state, "_promote_initialization_staging", promote_then_abort)
    with pytest.raises(SimulatedProcessAbort):
        state.initialize_run(context, resume=False, now=FIXED_NOW)

    paths = state.paths_for(context)
    assert state.load_session_init(context)["runId"] == RUN_ID
    assert state.load_progress(context)["phase"] == "prepare"
    assert [row["event"] for row in state.read_ledger(paths.ledger)] == [
        "run_initialized"
    ]

    monkeypatch.setattr(state, "_promote_initialization_staging", original_promote)
    with pytest.raises(state.StateError, match="use -Resume"):
        state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, resumed = state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )
    assert resumed["sessionInitContract"] == state.SESSION_INIT_CONTRACT


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_fresh_init_rejects_initializing_junction_before_writing(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    external = tmp_path / "external-initialization-target"
    external.mkdir()
    context.state_root.mkdir(parents=True)
    _create_directory_reparse_point(context.state_root / ".initializing", external)

    with pytest.raises(state.StateError, match="reparse-point ancestor"):
        state.initialize_run(context, resume=False, now=FIXED_NOW)

    assert not state.paths_for(context).root.exists()
    assert list(external.iterdir()) == []


def test_next_before_start_does_not_poison_fresh_run(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)

    with pytest.raises(state.StateError, match="pending-packet"):
        runner.dispatch_next(context, now=FIXED_NOW)

    with state.acquire_dispatch_lock(context):
        with pytest.raises(state.StateError, match="still active"):
            runner.start_session(context, resume=False, now=FIXED_NOW)

    paths, session_init = state.initialize_run(
        context,
        resume=False,
        now=FIXED_NOW,
    )
    assert paths.session_init.is_file()
    assert session_init["runId"] == RUN_ID


def test_resume_rehashes_immutable_baseline_requirement_snapshots(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    paths, _session = state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths.baseline_requirements_base.write_text("tampered==1\n", encoding="utf-8")

    with pytest.raises(state.StateError, match="immutable ledger binding"):
        state.initialize_run(
            context,
            resume=True,
            now=FIXED_NOW + timedelta(minutes=1),
        )


def test_start_state_and_recommend_work_before_run_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "phase": "prepare",
            "runId": RUN_ID,
            "ok": False,
            "findings": [{"level": "block", "code": "fixture_preflight_block"}],
            "evidenceHashes": {},
        },
    )

    exit_code, payload = runner.start_session(context, resume=False, now=FIXED_NOW)

    assert exit_code == 2
    assert payload["researchClockStarted"] is False
    assert not state.paths_for(context).manifest.exists()
    assert state.summarize(context, now=FIXED_NOW)["phase"] == "prepare"
    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == "complete-preflight"


def test_new_run_requires_resume_and_resume_cannot_change_duration(tmp_path: Path) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    with pytest.raises(state.StateError, match="use -Resume"):
        state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, binding = state.initialize_run(context, resume=True, now=FIXED_NOW + timedelta(hours=2))
    assert binding["sessionInitContract"] == state.SESSION_INIT_CONTRACT
    assert not paths.manifest.exists()
    with pytest.raises(ProfileError, match="frozen"):
        resolve_profile_context(
            repo_root,
            profile="installer-size",
            run_id=RUN_ID,
            duration_seconds=43199,
            require_run_id=True,
        )


def test_resume_recovers_interrupted_dispatch_without_replaying_it(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "interrupted-baseline",
        "lane": "baseline",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "hypothesis": {
            "statement": "Measure a baseline.",
            "mechanism": "Create an independent inventory.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the interrupted attempt.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": 1,
            "resultRelativePath": "baselines/baseline-replica-1.json",
            "timeoutSeconds": 60,
            "dispatch": {},
        },
    }
    state.set_pending_packet(context, packet)
    progress = state.load_progress(context)
    progress["activePacketId"] = packet["packetId"]
    progress["packetSequence"] = 1
    state.write_json_atomic(paths.progress, progress)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "baseline-replica",
        },
        now=FIXED_NOW,
    )
    write_json(
        paths.baseline_replica(1),
        {
            "inventoryContract": "InstallerResearchInventoryV1",
            "runId": RUN_ID,
            "replica": 1,
        },
    )

    with state.acquire_dispatch_lock(context):
        with pytest.raises(state.StateError, match="still active"):
            state.initialize_run(
                context,
                resume=True,
                now=FIXED_NOW + timedelta(seconds=30),
            )

    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )

    recovered = state.load_progress(context)
    assert recovered["activePacketId"] is None
    assert recovered["interruptedPacketIds"] == [packet["packetId"]]
    assert not paths.pending_packet.exists()
    assert not paths.baseline_replica(1).exists()
    assert paths.interrupted_result(packet["packetId"]).is_file()
    assert paths.interruption(packet["packetId"]).is_file()
    assert state.load_json_object(paths.last_run)["decision"] == "crash"
    assert state.read_ledger(paths.ledger)[-1]["event"] == "packet_interrupted_on_resume"


def test_resume_reconciles_completed_packet_commit_tail_idempotently(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)

    def stage_completed_replica(packet_id: str, replica: int, *, ledger_done: bool) -> None:
        packet = {
            "packetContract": "InstallerResearchPacketV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet_id,
            "lane": "baseline",
            "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
            "hypothesis": {
                "statement": f"Measure baseline replica {replica}.",
                "mechanism": "Create one independent inventory.",
                "expectedReductionBytes": 0,
                "risk": "low",
                "rollback": "Discard only the inventory.",
            },
            "action": {
                "kind": "baseline-replica",
                "replica": replica,
                "resultRelativePath": f"baselines/baseline-replica-{replica}.json",
                "timeoutSeconds": 60,
                "dispatch": {},
            },
        }
        state.set_pending_packet(context, packet)
        result_path = paths.baseline_replica(replica)
        write_json(
            result_path,
            {
                "inventoryContract": "InstallerResearchInventoryV1",
                "runId": RUN_ID,
                "replica": replica,
            },
        )
        state.append_ledger(
            paths.ledger,
            event="packet_started",
            payload={
                "packetId": packet_id,
                "packetSha256": state.file_sha256(paths.packet(packet_id)),
                "actionKind": "baseline-replica",
            },
            now=FIXED_NOW + timedelta(seconds=replica),
        )
        progress = state.load_progress(context)
        progress["packetSequence"] = replica
        progress["activePacketId"] = None
        progress["baselineReplicasAccepted"] = replica
        state.write_json_atomic(paths.progress, progress)
        last_run = {
            "lastRunContract": "InstallerSizeLastRunV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet_id,
            "packetSha256": state.file_sha256(paths.packet(packet_id)),
            "startedAtUtc": state.format_utc(FIXED_NOW),
            "finishedAtUtc": state.format_utc(
                FIXED_NOW + timedelta(seconds=replica)
            ),
            "exitCode": 0,
            "dispatchError": None,
            "stdout": "",
            "stderr": "",
            "resultContract": "InstallerResearchInventoryV1",
            "resultSha256": state.file_sha256(result_path),
            "resultFindings": [],
            "gateEvidenceSha256": None,
            "finalGateEvidenceSha256": None,
            "finalFullSuiteSha256": None,
            "finalTimingSha256": None,
            "finalTimingSummary": None,
            "decision": "baseline_accept",
            "learningUpdate": {"lane": "baseline"},
        }
        state.write_json_atomic(paths.last_run, last_run)
        if ledger_done:
            state.append_ledger(
                paths.ledger,
                event="packet_completed",
                payload=state.packet_completed_ledger_payload(
                    packet_id=packet_id,
                    last_run=last_run,
                    last_run_sha256=state.file_sha256(paths.last_run),
                ),
                now=FIXED_NOW + timedelta(seconds=replica),
            )

    stage_completed_replica("commit-tail-1", 1, ledger_done=False)
    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )
    assert not paths.pending_packet.exists()
    first_events = [
        row
        for row in state.read_ledger(paths.ledger)
        if row["event"] == "packet_completed"
        and row["payload"]["packetId"] == "commit-tail-1"
    ]
    assert len(first_events) == 1

    stage_completed_replica("commit-tail-2", 2, ledger_done=True)
    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=2),
    )
    assert not paths.pending_packet.exists()
    second_events = [
        row
        for row in state.read_ledger(paths.ledger)
        if row["event"] == "packet_completed"
        and row["payload"]["packetId"] == "commit-tail-2"
    ]
    assert len(second_events) == 1


def test_resume_commits_no_result_crash_tail_without_replay(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = _payload_packet(context, packet_id="no-result-crash")
    state.set_pending_packet(context, packet)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "measure-candidate",
        },
        now=FIXED_NOW,
    )
    progress = state.load_progress(context)
    progress["packetSequence"] = 1
    progress["activePacketId"] = None
    state.write_json_atomic(paths.progress, progress)
    last_run = {
        "lastRunContract": "InstallerSizeLastRunV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": packet["packetId"],
        "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
        "priorChampionId": None,
        "priorChampionSha256": None,
        "startedAtUtc": state.format_utc(FIXED_NOW),
        "finishedAtUtc": state.format_utc(FIXED_NOW + timedelta(seconds=1)),
        "exitCode": 2,
        "dispatchError": "TimeoutExpired",
        "stdout": "",
        "stderr": "",
        "resultContract": "",
        "resultSha256": None,
        "resultFindings": [],
        "gateEvidenceSha256": None,
        "finalGateEvidenceSha256": None,
        "finalFullSuiteSha256": None,
        "finalTimingSha256": None,
        "finalTimingSummary": None,
        "decision": "crash",
        "learningUpdate": {"lane": "fixture-lane"},
    }
    state.write_json_atomic(paths.last_run, last_run)

    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )

    assert not paths.pending_packet.exists()
    completed = [
        row
        for row in state.read_ledger(paths.ledger)
        if row["event"] == "packet_completed"
        and row["payload"]["packetId"] == packet["packetId"]
    ]
    assert len(completed) == 1
    assert completed[0]["payload"]["decision"] == "crash"


def test_resume_finishes_second_keep_over_prior_champion(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    old_champion = {
        "resultContract": "InstallerResearchResultV1",
        "runId": RUN_ID,
        "packetId": "old-champion",
        "decision": "keep",
    }
    state.write_json_atomic(paths.champion, old_champion)
    progress = state.load_progress(context)
    progress["championId"] = "old-champion"
    state.write_json_atomic(paths.progress, progress)
    prior_sha = state.file_sha256(paths.champion)

    packet = _payload_packet(context, packet_id="new-champion")
    packet["parentChampionId"] = "old-champion"
    state.set_pending_packet(context, packet)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "measure-candidate",
        },
        now=FIXED_NOW,
    )
    new_champion = {
        "resultContract": "InstallerResearchResultV1",
        "runId": RUN_ID,
        "packetId": packet["packetId"],
        "decision": "keep",
    }
    state.write_json_atomic(paths.packet_result(packet["packetId"]), new_champion)
    progress = state.load_progress(context)
    progress["packetSequence"] = 1
    progress["activePacketId"] = None
    progress["championId"] = packet["packetId"]
    state.write_json_atomic(paths.progress, progress)
    last_run = {
        "lastRunContract": "InstallerSizeLastRunV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": packet["packetId"],
        "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
        "priorChampionId": "old-champion",
        "priorChampionSha256": prior_sha,
        "startedAtUtc": state.format_utc(FIXED_NOW),
        "finishedAtUtc": state.format_utc(FIXED_NOW + timedelta(seconds=1)),
        "exitCode": 0,
        "dispatchError": None,
        "stdout": "",
        "stderr": "",
        "resultContract": "InstallerResearchResultV1",
        "resultSha256": state.file_sha256(
            paths.packet_result(packet["packetId"])
        ),
        "resultFindings": [],
        "gateEvidenceSha256": None,
        "finalGateEvidenceSha256": None,
        "finalFullSuiteSha256": None,
        "finalTimingSha256": None,
        "finalTimingSummary": None,
        "decision": "keep",
        "learningUpdate": {"lane": "fixture-lane"},
    }
    state.write_json_atomic(paths.last_run, last_run)

    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )

    assert state.load_json_object(paths.champion) == new_champion
    assert not paths.pending_packet.exists()


def test_recommendation_requires_resume_for_completion_tail(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = _payload_packet(context, packet_id="completion-tail-recommendation")
    state.set_pending_packet(context, packet)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "measure-candidate",
        },
        now=FIXED_NOW,
    )
    write_json(
        paths.packet_result(packet["packetId"]),
        {"resultContract": "InstallerResearchResultV1", "decision": "discard"},
    )
    progress = state.load_progress(context)
    progress["activePacketId"] = None
    state.write_json_atomic(paths.progress, progress)
    state.write_json_atomic(
        paths.last_run,
        {
            "lastRunContract": "InstallerSizeLastRunV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "decision": "discard",
        },
    )

    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == (
        "resume-run-to-reconcile-pending-tail"
    )


def test_recommendation_requires_resume_for_interruption_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = _payload_packet(context, packet_id="interruption-tail-recommendation")
    state.set_pending_packet(context, packet)
    progress = state.load_progress(context)
    progress["activePacketId"] = packet["packetId"]
    progress["packetSequence"] = 1
    state.write_json_atomic(paths.progress, progress)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "measure-candidate",
        },
        now=FIXED_NOW,
    )

    def interrupt_after_progress(*_args, **_kwargs):
        raise RuntimeError("fault after interruption progress commit")

    monkeypatch.setattr(state, "clear_pending_packet", interrupt_after_progress)
    with pytest.raises(RuntimeError, match="interruption progress"):
        state.initialize_run(
            context,
            resume=True,
            now=FIXED_NOW + timedelta(minutes=1),
        )

    assert state.load_progress(context)["activePacketId"] is None
    assert paths.interruption(packet["packetId"]).is_file()
    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == (
        "resume-run-to-reconcile-pending-tail"
    )


def test_recommendation_requires_resume_for_abandonment_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = _payload_packet(context, packet_id="abandonment-tail-recommendation")
    state.set_pending_packet(context, packet)

    def interrupt_after_progress(*_args, **_kwargs):
        raise RuntimeError("fault after abandonment progress commit")

    monkeypatch.setattr(state, "clear_pending_packet", interrupt_after_progress)
    with pytest.raises(RuntimeError, match="abandonment progress"):
        state.abandon_pending_packet(
            context,
            reason="operator_canceled",
            now=FIXED_NOW + timedelta(minutes=1),
        )

    assert paths.abandoned_packet(packet["packetId"]).is_file()
    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == (
        "resume-run-to-reconcile-pending-tail"
    )


def test_ledger_is_append_only_and_hash_chained(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    paths, _ = state.initialize_run(context, resume=False, now=FIXED_NOW)
    state.append_ledger(
        paths.ledger,
        event="test_event",
        payload={"value": 1},
        now=FIXED_NOW + timedelta(seconds=1),
    )

    rows = state.read_ledger(paths.ledger)
    assert len(rows) == 2
    assert rows[1]["previousEntrySha256"] == rows[0]["entrySha256"]

    raw_rows = paths.ledger.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(raw_rows[0])
    tampered["payload"]["durationSeconds"] = 1
    raw_rows[0] = json.dumps(tampered, separators=(",", ":"))
    paths.ledger.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")
    with pytest.raises(state.StateError, match="entry hash mismatch"):
        state.read_ledger(paths.ledger)


def _write_arm_inputs(context) -> tuple[state.RunPaths, dict, dict]:
    paths = state.paths_for(context)
    evidence_hashes = {"fixture": "a" * 64}
    state.write_preflight(
        context,
        findings=[{"level": "ok", "code": "preflight_ready"}],
        accepted=True,
        evidence_hashes=evidence_hashes,
        now=FIXED_NOW,
    )
    write_json(paths.baseline_replica(1), {"inventoryContract": "InstallerResearchInventoryV1", "replica": 1})
    write_json(paths.baseline_replica(2), {"inventoryContract": "InstallerResearchInventoryV1", "replica": 2})
    source_commit = git(context.repo_root, "rev-parse", "HEAD")
    for replica in (1, 2):
        packet_id = f"accepted-baseline-{replica}"
        packet = {
            "packetContract": "InstallerResearchPacketV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet_id,
            "lane": "baseline",
            "sourceCommit": source_commit,
            "hypothesis": {
                "statement": f"Measure baseline replica {replica}.",
                "mechanism": "Create one independent inventory.",
                "expectedReductionBytes": 0,
                "risk": "low",
                "rollback": "Discard only the inventory.",
            },
            "action": {
                "kind": "baseline-replica",
                "replica": replica,
                "resultRelativePath": f"baselines/baseline-replica-{replica}.json",
                "timeoutSeconds": 60,
                "dispatch": {},
            },
        }
        state.store_immutable_json(paths.packet(packet_id), packet)
        state.append_ledger(
            paths.ledger,
            event="packet_started",
            payload={
                "packetId": packet_id,
                "packetSha256": state.file_sha256(paths.packet(packet_id)),
                "actionKind": "baseline-replica",
            },
            now=FIXED_NOW,
        )
        state.append_ledger(
            paths.ledger,
            event="packet_completed",
            payload={
                "packetId": packet_id,
                "decision": "baseline_accept",
                "resultSha256": state.file_sha256(
                    paths.baseline_replica(replica)
                ),
            },
            now=FIXED_NOW,
        )
    write_json(paths.wheelhouse_manifest, {"kind": "wheelhouse"})
    write_json(
        paths.environment_manifest,
        {"productDependenciesSha256": "b" * 64},
    )
    write_json(paths.toolchain_manifest, {"rustToolchain": "1.97.0"})
    write_json(paths.holdout_snapshot, {"holdoutSnapshotContract": "test"})
    baseline = {
        "baselineContract": "InstallerResearchBaselineV1",
        "schemaVersion": 1,
        "accepted": True,
        "runId": RUN_ID,
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "evaluatorHash": "c" * 64,
        "toolchainHash": "d" * 64,
        "componentMapSha256": "e" * 64,
        "semanticTreeSha256": "f" * 64,
        "fileListSha256": "1" * 64,
        "pyzInventorySha256": "2" * 64,
    }
    write_json(paths.baseline, baseline)
    doctor_report = {
        "doctorContract": "InstallerSizeDoctorV1",
        "phase": "run",
        "runId": context.run_id,
        "ok": True,
        "evidenceHashes": evidence_hashes,
    }
    return paths, baseline, doctor_report


def _payload_packet(
    context,
    *,
    packet_id: str,
    source_commit: str | None = None,
    parent_tree_oid: str | None = None,
) -> dict:
    source = source_commit or git(context.repo_root, "rev-parse", "HEAD")
    parent_tree = parent_tree_oid or git(
        context.repo_root,
        "rev-parse",
        "HEAD^{tree}",
    )
    return {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": packet_id,
        "lane": "fixture-lane",
        "sourceCommit": source,
        "parentChampionId": "baseline",
        "parentSourceTreeOid": parent_tree,
        "hypothesis": {
            "statement": f"Measure {packet_id}.",
            "mechanism": "Evaluate one bounded payload change.",
            "expectedReductionBytes": 1,
            "risk": "low",
            "rollback": "Revert only the candidate commit.",
        },
        "action": {
            "kind": "measure-candidate",
            "comparisonKind": "payload",
            "compression": "bzip2",
            "resultRelativePath": f"packet-results/{packet_id}.json",
            "timeoutSeconds": 60,
            "dispatch": {},
        },
    }


def _baseline_packet(context, *, packet_id: str, replica: int) -> dict:
    return {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": packet_id,
        "lane": "baseline",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "hypothesis": {
            "statement": f"Measure baseline replica {replica}.",
            "mechanism": "Create one independent inventory.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard only the inventory.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": replica,
            "resultRelativePath": f"baselines/baseline-replica-{replica}.json",
            "timeoutSeconds": 60,
            "dispatch": {
                "driver": "powershell-file",
                "entrypoint": "scripts/run_installer_size_packet.ps1",
                "arguments": [
                    "-RunId",
                    RUN_ID,
                    "-Mode",
                    f"baseline-{replica}",
                ],
            },
        },
    }


def test_arm_writes_one_immutable_manifest_and_resume_preserves_deadline(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)

    armed = state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW + timedelta(hours=3),
    )
    assert armed["researchStartedAtUtc"] == "2026-07-18T13:00:00Z"
    assert armed["researchDeadlineUtc"] == "2026-07-19T01:00:00Z"
    manifest = state.load_manifest(context)
    assert manifest["researchStartedAtUtc"] == armed["researchStartedAtUtc"]
    assert manifest["researchDeadlineUtc"] == armed["researchDeadlineUtc"]
    assert manifest["bindings"]["baselineSha256"] == state.file_sha256(paths.baseline)
    assert manifest["bindings"]["environmentManifestSha256"] == state.file_sha256(
        paths.environment_manifest
    )
    original_manifest_sha = state.file_sha256(paths.manifest)

    _paths, resumed = state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(days=2),
    )
    assert resumed["researchDeadlineUtc"] == "2026-07-19T01:00:00Z"
    assert state.file_sha256(paths.manifest) == original_manifest_sha
    assert (
        state.remaining_seconds(
            context,
            now=FIXED_NOW + timedelta(hours=4),
        )
        == 39600
    )


def test_resume_repairs_progress_clock_only_from_immutable_manifest(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)
    armed = state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW + timedelta(hours=1),
    )
    progress = state.load_progress(context)
    progress["researchStartedAtUtc"] = "2026-07-18T12:00:00Z"
    progress["researchDeadlineUtc"] = "2026-07-19T00:00:00Z"
    state.write_json_atomic(paths.progress, progress)

    with pytest.raises(state.StateError, match="immutable run manifest"):
        state.load_progress(context)

    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(hours=2),
    )
    repaired = state.load_progress(context)
    assert repaired["researchStartedAtUtc"] == armed["researchStartedAtUtc"]
    assert repaired["researchDeadlineUtc"] == armed["researchDeadlineUtc"]
    assert state.read_ledger(paths.ledger)[-1]["event"] == (
        "progress_clock_recovered_from_manifest"
    )


def test_resume_heals_interrupted_manifest_arm_without_extending_deadline(
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)
    armed = state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW + timedelta(hours=1),
    )
    progress = state.load_progress(context)
    progress.update(
        {
            "phase": "prepare",
            "researchStartedAtUtc": None,
            "researchDeadlineUtc": None,
        }
    )
    state.write_json_atomic(paths.progress, progress)
    ledger_lines = paths.ledger.read_text(encoding="utf-8").splitlines()
    assert json.loads(ledger_lines[-1])["event"] == "research_clock_started"
    paths.ledger.write_text("\n".join(ledger_lines[:-1]) + "\n", encoding="utf-8")

    state.initialize_run(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(hours=2),
    )

    repaired = state.load_progress(context)
    assert repaired["researchStartedAtUtc"] == armed["researchStartedAtUtc"]
    assert repaired["researchDeadlineUtc"] == armed["researchDeadlineUtc"]
    clock_events = [
        row
        for row in state.read_ledger(paths.ledger)
        if row["event"] == "research_clock_started"
    ]
    assert len(clock_events) == 1
    assert clock_events[0]["payload"]["recoveredAfterInterruptedArm"] is True


def test_manifest_cannot_rebind_a_changed_protected_input(tmp_path: Path) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    protected = repo_root / "protected.txt"
    protected.write_text("tampered evaluator\n", encoding="utf-8")
    manifest = state.load_json_object(paths.manifest)
    manifest["protectedInputHashes"]["protected.txt"] = state.file_sha256(protected)
    state.write_json_atomic(paths.manifest, manifest)

    with pytest.raises(state.StateError, match="session initialization"):
        state.load_manifest(context)

    session_init = state.load_json_object(paths.session_init)
    session_init["protectedInputHashes"]["protected.txt"] = state.file_sha256(
        protected
    )
    state.write_json_atomic(paths.session_init, session_init)
    with pytest.raises(state.StateError, match="immutable ledger binding"):
        state.load_manifest(context)


def test_holdout_contract_never_treats_route_rewrite_as_proof(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)

    missing_findings, _ = doctor.validate_holdouts(context)
    assert any(
        item["code"] == "youtube_holdout_snapshot_missing_or_invalid"
        for item in missing_findings
    )

    fixture = json.loads(
        (context.config_path.parent / "youtube-holdouts.json").read_text(encoding="utf-8")
    )
    runtime_identity = {
        "name": "deno",
        "version": "2.9.2",
        "length": 100,
        "sha256": "8" * 64,
    }
    distribution_identities = {
        name: {
            "name": name,
            "version": "1.0",
            "fileCount": 1,
            "contentSha256": character * 64,
        }
        for name, character in (("deno", "9"), ("yt-dlp", "a"), ("yt-dlp-ejs", "b"))
    }
    rows = []
    for case in fixture["cases"]:
        case_id = case["id"]
        url = case["url"]
        video_id = doctor._holdout_video_id(url)
        evidence_path = paths.preflight_dir / "youtube-holdout-probes" / f"{case_id}.json"
        probe = {
            "probeContract": "InstallerSizeYoutubeHoldoutProbeV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "fixtureId": fixture["fixtureId"],
            "caseId": case_id,
            "family": case["family"],
            "status": "pass",
            "url": url,
            "videoId": video_id,
            "capturedAtUtc": "2026-07-18T10:00:00Z",
            "observedCapabilities": case["requiredCapabilities"],
            "runtime": runtime_identity,
            "distributions": distribution_identities,
        }
        write_json(evidence_path, probe)
        rows.append(
            {
                "id": case_id,
                "family": case["family"],
                "status": "validated",
                "url": url,
                "videoId": video_id,
                "observedCapabilities": case["requiredCapabilities"],
                "denoProbe": "pass",
                "probeEvidenceSha256": state.file_sha256(evidence_path),
            }
        )
    snapshot = {
        "holdoutSnapshotContract": "InstallerSizeYoutubeHoldoutsV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "fixtureId": fixture["fixtureId"],
        "fixtureSha256": state.file_sha256(context.config_path.parent / "youtube-holdouts.json"),
        "capturedAtUtc": "2026-07-18T10:00:00Z",
        "runtime": runtime_identity,
        "distributions": distribution_identities,
        "cases": rows,
    }
    write_json(paths.holdout_snapshot, snapshot)
    findings, evidence = doctor.validate_holdouts(context)
    assert [item for item in findings if item["level"] == "block"] == []
    assert evidence["youtube-holdouts.snapshot.json"]
    assert len([name for name in evidence if name.startswith("youtube-holdout-probe:")]) == 6

    snapshot["cases"][2]["url"] = fixture["cases"][0]["url"]
    write_json(paths.holdout_snapshot, snapshot)
    findings, _ = doctor.validate_holdouts(context)
    assert any(item["code"] == "youtube_holdout_snapshot_url_invalid" for item in findings)


def test_environment_manifest_binds_run_and_baseline_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    monkeypatch.setattr(doctor, "_active_environment_verification", lambda _context: [])
    requirement_identities = [
        {
            "name": path.name,
            "length": path.stat().st_size,
            "sha256": state.file_sha256(path),
        }
        for path in (
            paths.baseline_requirements_base,
            paths.baseline_requirements_build,
        )
    ]
    write_json(
        paths.wheelhouse_manifest,
        {
            "kind": "scriber-installer-research-wheelhouse",
            "runId": RUN_ID,
            "requirements": requirement_identities,
            "requirementsSha256": "a" * 64,
            "wheelhouseSha256": "b" * 64,
        },
    )
    environment = {
        "kind": "scriber-installer-research-python-environment",
        "runId": "another-run",
        "environmentName": "candidate",
        "requirements": requirement_identities,
        "requirementsSha256": "a" * 64,
        "wheelhouseSha256": "b" * 64,
        "productDependenciesSha256": "c" * 64,
        "distributions": [{"name": "example", "version": "1", "recordSha256": None}],
    }
    write_json(paths.environment_manifest, environment)

    findings, _ = doctor.validate_environment_manifests(context)
    assert any(item["code"] == "environment_manifest_identity_mismatch" for item in findings)
    environment["runId"] = RUN_ID
    environment["environmentName"] = "baseline"
    write_json(paths.environment_manifest, environment)
    findings, evidence = doctor.validate_environment_manifests(context)
    assert [item for item in findings if item["level"] == "block"] == []
    assert set(evidence) == {
        "baseline-requirements-base.txt",
        "baseline-requirements-build.txt",
        "wheelhouse-manifest.json",
        "environment-manifest.json",
    }
    paths.baseline_requirements_base.write_text("tampered==9\n", encoding="utf-8")
    findings, _evidence = doctor.validate_environment_manifests(context)
    assert any(
        item["code"]
        in {
            "wheelhouse_baseline_requirement_snapshot_mismatch",
            "environment_baseline_requirement_snapshot_mismatch",
        }
        for item in findings
    )


def test_environment_doctor_runs_exact_baseline_verifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    python_executable = paths.environment_manifest.parent / ".venv" / "Scripts" / "python.exe"
    python_executable.parent.mkdir(parents=True)
    python_executable.write_bytes(b"python")
    paths.environment_manifest.parent.mkdir(parents=True, exist_ok=True)
    write_json(paths.environment_manifest, {"kind": "fixture"})
    (paths.root / "wheelhouse").mkdir(parents=True)
    writer = repo_root / "scripts" / "write_installer_research_environment_manifest.py"
    writer.parent.mkdir(parents=True, exist_ok=True)
    writer.write_text("# fixture\n", encoding="utf-8")
    (repo_root / "requirements-base.txt").write_text("candidate-removal==1\n", encoding="utf-8")
    (repo_root / "requirements-build.txt").write_text("candidate-build==1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "runId": RUN_ID,
                    "environmentName": "baseline",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor._active_environment_verification(context) == []
    assert calls[0][0] == str(python_executable)
    assert str(paths.baseline_requirements_base) in calls[0]
    assert str(repo_root / "requirements-base.txt") not in calls[0]
    assert calls[0][-2:] == ["--verify", str(paths.environment_manifest)]


def test_toolchain_doctor_rehashes_and_executes_every_pinned_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    toolchain_root = paths.toolchain_manifest.parent
    node_version = "26.3.1"
    (repo_root / ".node-version").write_text(node_version + "\n", encoding="utf-8")
    files = {
        "node": toolchain_root / "node" / "node.exe",
        "npm": toolchain_root / "node" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        "tauri": repo_root / "Frontend" / "node_modules" / "@tauri-apps" / "cli" / "tauri.js",
        "nativeTauriCli": repo_root / "Frontend" / "node_modules" / "@tauri-apps" / "cli-win32-x64-msvc" / "cli.win32-x64-msvc.node",
        "unrelatedNodeModule": repo_root / "Frontend" / "node_modules" / "fixture-package" / "index.js",
        "frontendPackageLock": repo_root / "Frontend" / "package-lock.json",
        "nodeArchive": toolchain_root / "downloads" / f"node-v{node_version}-win-x64.zip",
        "rustc": tmp_path / "rust" / "rustc.exe",
        "cargo": tmp_path / "rust" / "cargo.exe",
        "rustfmt": tmp_path / "rust" / "rustfmt.exe",
        "clippyDriver": tmp_path / "rust" / "clippy-driver.exe",
        "nsis": tmp_path / "localapp" / "tauri" / "NSIS" / "Bin" / "makensis.exe",
        "nsisInclude": tmp_path / "localapp" / "tauri" / "NSIS" / "Include" / "fixture.nsh",
        "rustup": tmp_path / "rust" / "rustup.exe",
    }
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\n").encode("utf-8"))

    def identity(name: str, file_name: str, version: str) -> dict:
        path = files[name]
        return {
            "name": {
                "node": "node",
                "npm": "npm-cli",
                "tauri": "tauri-cli",
                "nativeTauriCli": "native-tauri-cli",
                "frontendPackageLock": "frontend-package-lock",
                "rustc": "rustc-rustup-proxy",
                "cargo": "cargo-rustup-proxy",
                "rustfmt": "rustfmt-rustup-proxy",
                "clippyDriver": "clippy-driver-rustup-proxy",
                "nsis": "makensis",
            }[name],
            "version": version,
            "fileName": file_name,
            "length": path.stat().st_size,
            "sha256": state.file_sha256(path),
        }

    payload = {
        "schemaVersion": 1,
        "kind": "scriber-installer-research-toolchain",
        "runId": RUN_ID,
        "node": identity("node", "node.exe", f"v{node_version}"),
        "npm": identity("npm", "npm-cli.js", "11.4.2"),
        "tauri": identity("tauri", "tauri.js", "tauri-cli 2.8.4"),
        "nativeTauriCli": identity(
            "nativeTauriCli",
            "cli.win32-x64-msvc.node",
            "tauri-cli 2.8.4",
        ),
        "frontendNodeModules": doctor._plain_tree_identity(
            repo_root / "Frontend" / "node_modules"
        ),
        "frontendPackageLock": identity(
            "frontendPackageLock", "package-lock.json", "lockfile-v3"
        ),
        "nodeArchive": {
            "fileName": files["nodeArchive"].name,
            "length": files["nodeArchive"].stat().st_size,
            "sha256": state.file_sha256(files["nodeArchive"]),
            "checksumSource": f"https://nodejs.org/dist/v{node_version}/SHASUMS256.txt",
        },
        "rustc": identity("rustc", "rustc.exe", "rustc 1.97.0 (fixture)"),
        "cargo": identity("cargo", "cargo.exe", "cargo 1.97.0 (fixture)"),
        "rustfmt": identity(
            "rustfmt", "rustfmt.exe", "rustfmt 1.8.0-stable (fixture)"
        ),
        "clippyDriver": identity(
            "clippyDriver", "clippy-driver.exe", "clippy 0.1.97 (fixture)"
        ),
        "rustToolchain": "1.97.0",
        "nsis": {
            **identity("nsis", "makensis.exe", "v3.11"),
            "relativePath": "Bin/makensis.exe",
        },
        "nsisTree": doctor._plain_tree_identity(
            tmp_path / "localapp" / "tauri" / "NSIS"
        ),
    }
    write_json(paths.toolchain_manifest, payload)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(files["rustup"]) if name == "rustup" else None)

    def fake_version(command, **_kwargs):
        if command[0] == str(files["rustup"]):
            return str(
                files[
                    {
                        "clippy-driver": "clippyDriver",
                    }.get(command[-1], command[-1])
                ]
            )
        if command[-1] == "--version" and command[0] == str(files["node"]):
            if len(command) == 2:
                return f"v{node_version}"
            if command[1] == str(files["npm"]):
                return "11.4.2"
            return "tauri-cli 2.8.4"
        if command[0] == str(files["rustc"]):
            return "rustc 1.97.0 (fixture)"
        if command[0] == str(files["cargo"]):
            return "cargo 1.97.0 (fixture)"
        if command[0] == str(files["rustfmt"]):
            return "rustfmt 1.8.0-stable (fixture)"
        if command[0] == str(files["clippyDriver"]):
            return "clippy 0.1.97 (fixture)"
        if command[0] == str(files["nsis"]):
            return "v3.11"
        return None

    monkeypatch.setattr(doctor, "_capture_tool_version", fake_version)
    findings, evidence = doctor.validate_toolchain_manifest(context)
    assert findings == []
    assert evidence["toolchain-manifest.json"] == state.file_sha256(paths.toolchain_manifest)

    files["unrelatedNodeModule"].write_bytes(b"tampered\n")
    findings, _evidence = doctor.validate_toolchain_manifest(context)
    assert any(item["code"] == "toolchain_node_modules_tree_drift" for item in findings)

    files["unrelatedNodeModule"].write_bytes(b"unrelatedNodeModule\n")
    files["nsisInclude"].write_bytes(b"tampered include\n")
    findings, _evidence = doctor.validate_toolchain_manifest(context)
    assert any(item["code"] == "toolchain_nsis_tree_drift" for item in findings)

    files["nsisInclude"].write_bytes(b"nsisInclude\n")
    payload.pop("nsisTree")
    write_json(paths.toolchain_manifest, payload)
    findings, _evidence = doctor.validate_toolchain_manifest(context)
    assert any(
        item["code"] == "toolchain_nsis_tree_identity_invalid" for item in findings
    )


def test_baseline_doctor_recomputes_authoritative_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    first = {"inventoryContract": "InstallerResearchInventoryV1", "ordinal": 1}
    second = {"inventoryContract": "InstallerResearchInventoryV1", "ordinal": 2}
    write_json(paths.baseline_replica(1), first)
    write_json(paths.baseline_replica(2), second)
    write_json(paths.toolchain_manifest, {"kind": "toolchain"})
    component_map = repo_root / "packaging" / "installer-component-map-v1.json"
    write_json(component_map, {"mapId": "test"})
    expected = {
        "baselineContract": "InstallerResearchBaselineV1",
        "schemaVersion": 1,
        "accepted": True,
        "reasonCodes": [],
        "acceptedAtUtc": "2026-07-18T10:00:00Z",
        "evaluatorHash": "a" * 64,
        "toolchainHash": state.file_sha256(paths.toolchain_manifest),
        "componentMapSha256": state.file_sha256(component_map),
        "installedBytes": 1,
    }
    write_json(paths.baseline, expected)
    monkeypatch.setattr(doctor, "accept_baseline", lambda *_args, **_kwargs: dict(expected))
    monkeypatch.setattr(doctor, "current_installer_evaluator_hash", lambda _root: "a" * 64)

    assert doctor.validate_baseline_state(context) == []
    tampered = dict(expected)
    tampered["toolchainHash"] = "f" * 64
    write_json(paths.baseline, tampered)
    findings = doctor.validate_baseline_state(context)
    assert any(item["code"] == "baseline_summary_binding_mismatch" for item in findings)
    assert any(item["code"] == "baseline_toolchain_hash_mismatch" for item in findings)


def test_next_dispatches_exactly_one_existing_packet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={},
        now=FIXED_NOW,
    )
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "baseline-1",
        "lane": "baseline",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "hypothesis": {
            "statement": "The hermetic baseline is reproducible.",
            "mechanism": "Build and inventory one independent replica.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the replica without changing product code.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": 1,
            "resultRelativePath": "baselines/baseline-replica-1.json",
            "timeoutSeconds": 60,
            "dispatch": {
                "driver": "powershell-file",
                "entrypoint": "scripts/run_installer_size_packet.ps1",
                "arguments": ["-RunId", RUN_ID, "-Mode", "baseline-1"],
            },
        },
    }
    state.set_pending_packet(context, packet)
    calls = []
    monkeypatch.setattr(runner, "current_installer_evaluator_hash", lambda _root: "e" * 64)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "ok": True,
            "findings": [],
            "evidenceHashes": {},
        },
    )

    def fake_dispatch(_context, received):
        calls.append(received["packetId"])
        component_map = context.repo_root / "packaging" / "installer-component-map-v1.json"
        write_json(component_map, {"mapId": "test"})
        write_json(paths.toolchain_manifest, {"kind": "toolchain"})
        write_json(
            paths.baseline_replica(1),
            {
                "inventoryContract": "InstallerResearchInventoryV1",
                "schemaVersion": 1,
                "ok": True,
                "runId": RUN_ID,
                "sourceCommit": received["sourceCommit"],
                "buildProvenance": {
                    "replicaId": received["packetId"],
                    "buildRootSha256": "a" * 64,
                },
                "evaluatorHash": "e" * 64,
                "toolchainHash": state.file_sha256(paths.toolchain_manifest),
                "componentMap": {"sha256": state.file_sha256(component_map)},
                "compression": "bzip2",
                "payload": {"installed": {"totalBytes": 1}},
            },
        )
        return subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    monkeypatch.setattr(runner, "_dispatch_command", fake_dispatch)
    exit_code, response = runner.dispatch_next(context, now=FIXED_NOW + timedelta(seconds=1))

    assert exit_code == 0
    assert response["decision"] == "baseline_accept"
    assert calls == ["baseline-1"]
    assert not paths.pending_packet.exists()
    assert state.load_progress(context)["baselineReplicasAccepted"] == 1
    with pytest.raises(state.StateError, match="one existing pending-packet"):
        runner.dispatch_next(context, now=FIXED_NOW + timedelta(seconds=2))


def test_failed_baseline_output_is_quarantined_and_never_arms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={},
        now=FIXED_NOW,
    )
    packet = _baseline_packet(
        context,
        packet_id="failed-baseline-output",
        replica=1,
    )
    state.set_pending_packet(context, packet)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "ok": True,
            "findings": [],
            "evidenceHashes": {},
        },
    )

    def failed_dispatch(_context, _packet):
        write_json(
            paths.baseline_replica(1),
            {"inventoryContract": "unattested-partial-output"},
        )
        return subprocess.CompletedProcess([], 2, stdout="", stderr="failed")

    monkeypatch.setattr(runner, "_dispatch_command", failed_dispatch)

    exit_code, response = runner.dispatch_next(
        context,
        now=FIXED_NOW + timedelta(seconds=1),
    )

    assert exit_code == 2
    assert response["decision"] == "crash"
    assert not paths.baseline_replica(1).exists()
    assert paths.failed_result(packet["packetId"]).is_file()
    assert state.accepted_baseline_replica_packet_id(context, 1) is None
    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == (
        "formulate-baseline-replica-1-packet"
    )
    assert not paths.manifest.exists()


def test_rejected_baseline_pair_is_run_id_fatal(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={},
        now=FIXED_NOW,
    )
    write_json(paths.baseline_replica(1), {"replica": 1})
    write_json(paths.baseline_replica(2), {"replica": 2})
    for replica in (1, 2):
        packet_id = f"retry-baseline-{replica}"
        packet = {
            "packetContract": "InstallerResearchPacketV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet_id,
            "lane": "baseline",
            "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
            "hypothesis": {
                "statement": "Measure a baseline.",
                "mechanism": "Create one inventory.",
                "expectedReductionBytes": 0,
                "risk": "low",
                "rollback": "Discard the inventory.",
            },
            "action": {
                "kind": "baseline-replica",
                "replica": replica,
                "resultRelativePath": f"baselines/baseline-replica-{replica}.json",
                "timeoutSeconds": 60,
                "dispatch": {},
            },
        }
        state.store_immutable_json(paths.packet(packet_id), packet)
        state.append_ledger(
            paths.ledger,
            event="packet_started",
            payload={
                "packetId": packet_id,
                "packetSha256": state.file_sha256(paths.packet(packet_id)),
                "actionKind": "baseline-replica",
            },
            now=FIXED_NOW,
        )
        state.append_ledger(
            paths.ledger,
            event="packet_completed",
            payload={
                "packetId": packet_id,
                "decision": "baseline_accept",
                "resultSha256": state.file_sha256(
                    paths.baseline_replica(replica)
                ),
            },
            now=FIXED_NOW,
        )
    write_json(
        paths.baseline,
        {
            "baselineContract": "InstallerResearchBaselineV1",
            "runId": RUN_ID,
            "accepted": False,
            "reasonCodes": ["installer_not_reproducible"],
        },
    )

    exit_code, payload = runner.start_session(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )

    assert exit_code == 2
    assert payload["phase"] == "baseline-rejected"
    assert payload["baselineAcceptance"]["fatalForRunId"] is True
    assert payload["requiredActions"][0]["id"] == "start-new-run-id"
    assert runner.recommend_next(context, now=FIXED_NOW)["safeNextStep"] == (
        "start-new-run-id-after-baseline-rejection"
    )
    assert not paths.manifest.exists()


def test_missing_baseline_pair_artifact_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={},
        now=FIXED_NOW,
    )
    write_json(paths.baseline_replica(1), {"replica": 1})
    write_json(paths.baseline_replica(2), {"replica": 2})
    for replica in (1, 2):
        packet_id = f"timeout-baseline-{replica}"
        packet = {
            "packetContract": "InstallerResearchPacketV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet_id,
            "lane": "baseline",
            "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
            "hypothesis": {
                "statement": "Measure a baseline.",
                "mechanism": "Create one inventory.",
                "expectedReductionBytes": 0,
                "risk": "low",
                "rollback": "Discard the inventory.",
            },
            "action": {
                "kind": "baseline-replica",
                "replica": replica,
                "resultRelativePath": f"baselines/baseline-replica-{replica}.json",
                "timeoutSeconds": 60,
                "dispatch": {},
            },
        }
        state.store_immutable_json(paths.packet(packet_id), packet)
        state.append_ledger(
            paths.ledger,
            event="packet_started",
            payload={
                "packetId": packet_id,
                "packetSha256": state.file_sha256(paths.packet(packet_id)),
                "actionKind": "baseline-replica",
            },
            now=FIXED_NOW,
        )
        state.append_ledger(
            paths.ledger,
            event="packet_completed",
            payload={
                "packetId": packet_id,
                "decision": "baseline_accept",
                "resultSha256": state.file_sha256(
                    paths.baseline_replica(replica)
                ),
            },
            now=FIXED_NOW,
        )

    def timeout_acceptance(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["accept-baseline"], 300)

    monkeypatch.setattr(runner.subprocess, "run", timeout_acceptance)

    exit_code, payload = runner.start_session(
        context,
        resume=True,
        now=FIXED_NOW + timedelta(minutes=1),
    )

    assert exit_code == 2
    assert payload["phase"] == "baseline-validation"
    assert payload["baselineAcceptance"]["exitCode"] == 124
    assert payload["requiredActions"][0]["id"] == (
        "retry-baseline-pair-acceptance"
    )
    assert not paths.manifest.exists()


def test_clock_arms_after_pre_arm_doctor_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    captured: dict[str, datetime] = {}

    monkeypatch.setattr(runner, "run_doctor", lambda *_args, **_kwargs: doctor_report)
    monkeypatch.setattr(
        runner,
        "utc_now",
        lambda: FIXED_NOW + timedelta(minutes=7),
    )

    def fake_arm(_context, *, baseline, doctor_report, now):
        del baseline, doctor_report
        captured["now"] = now
        return {}

    monkeypatch.setattr(runner, "arm_research_clock", fake_arm)

    exit_code, _payload = runner.start_session(context, resume=True)

    assert exit_code == 0
    assert captured["now"] == FIXED_NOW + timedelta(minutes=7)


def test_every_packet_dispatch_runs_a_fresh_fail_closed_doctor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={},
        now=FIXED_NOW,
    )
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "baseline-doctor-drift",
        "lane": "baseline",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "hypothesis": {
            "statement": "Recheck every frozen input immediately before dispatch.",
            "mechanism": "Run the packet-bound prepare Doctor.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the unexecuted packet.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": 1,
            "resultRelativePath": "baselines/baseline-replica-1.json",
            "timeoutSeconds": 60,
            "dispatch": {
                "driver": "powershell-file",
                "entrypoint": "scripts/run_installer_size_packet.ps1",
                "arguments": ["-RunId", RUN_ID, "-Mode", "baseline-1"],
            },
        },
    }
    state.set_pending_packet(context, packet)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "ok": False,
            "findings": [
                {"level": "block", "code": "preflight_evidence_drift"}
            ],
        },
    )
    monkeypatch.setattr(
        runner,
        "_dispatch_command",
        lambda *_args, **_kwargs: pytest.fail("drifted packet must not dispatch"),
    )

    with pytest.raises(state.StateError, match="preflight_evidence_drift"):
        runner.dispatch_next(context, now=FIXED_NOW + timedelta(seconds=1))
    assert paths.pending_packet.is_file()


def test_prepare_doctor_revalidates_an_existing_preflight_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    state.write_preflight(
        context,
        findings=[],
        accepted=True,
        evidence_hashes={"fixture.json": "a" * 64},
        now=FIXED_NOW,
    )
    monkeypatch.setattr(
        doctor,
        "_common_checks",
        lambda _context: ([], {"fixture.json": "b" * 64}),
    )

    report = doctor.run_doctor(context, phase="prepare", now=FIXED_NOW)

    assert report["ok"] is False
    assert any(
        item["code"] == "preflight_evidence_drift"
        for item in report["findings"]
    )


def test_packet_timeout_and_dispatch_driver_are_fail_closed(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    source_commit = git(context.repo_root, "rev-parse", "HEAD")
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "baseline-1",
        "lane": "baseline",
        "sourceCommit": source_commit,
        "hypothesis": {
            "statement": "Measure a baseline.",
            "mechanism": "Create an independent inventory.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the inventory.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": 1,
            "resultRelativePath": "baselines/baseline-replica-1.json",
            "timeoutSeconds": 29,
            "dispatch": {},
        },
    }
    with pytest.raises(state.StateError, match="timeoutSeconds"):
        state.validate_packet(packet, run_id=RUN_ID)
    packet["action"]["timeoutSeconds"] = 60
    state.validate_packet(packet, run_id=RUN_ID)
    with pytest.raises(state.StateError, match="only the frozen"):
        runner._validate_dispatch_policy(
            context,
            packet,
            driver="powershell-file",
            entrypoint="scripts/build_windows.ps1",
            arguments=[],
        )
    with pytest.raises(state.StateError, match="only the frozen"):
        runner._validate_dispatch_policy(
            context,
            packet,
            driver="project-python",
            entrypoint="scripts/installer_research.py",
            arguments=[],
        )


def test_candidate_timeout_cannot_consume_finalization_reserve(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "candidate-reserve",
        "lane": "fixture-lane",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "parentChampionId": "baseline",
        "parentSourceTreeOid": git(context.repo_root, "rev-parse", "HEAD^{tree}"),
        "hypothesis": {
            "statement": "Try one candidate.",
            "mechanism": "Evaluate a bounded change.",
            "expectedReductionBytes": 1,
            "risk": "low",
            "rollback": "Revert the candidate commit.",
        },
        "action": {
            "kind": "measure-candidate",
            "comparisonKind": "payload",
            "compression": "bzip2",
            "resultRelativePath": "packet-results/candidate-reserve.json",
            "timeoutSeconds": 1800,
            "dispatch": {},
        },
    }
    state.validate_packet(packet, run_id=RUN_ID)
    deadline = state.parse_utc(
        state.load_progress(context)["researchDeadlineUtc"],
        field="researchDeadlineUtc",
    )
    with pytest.raises(state.StateError, match="finalization reserve"):
        runner._check_packet_phase(
            context,
            packet,
            now=deadline - timedelta(seconds=5400 + 1800),
        )


def test_payload_candidate_must_start_from_current_champion_tree(
    tmp_path: Path,
) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    baseline_tree = git(repo_root, "rev-parse", "HEAD^{tree}")

    discarded = repo_root / "discarded.txt"
    discarded.write_text("discarded\n", encoding="utf-8")
    git(repo_root, "add", "discarded.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "discarded candidate",
    )
    discarded_commit = git(repo_root, "rev-parse", "HEAD")
    stacked = repo_root / "stacked.txt"
    stacked.write_text("stacked\n", encoding="utf-8")
    git(repo_root, "add", "stacked.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "candidate stacked on discard",
    )
    stacked_commit = git(repo_root, "rev-parse", "HEAD")
    stacked_packet = _payload_packet(
        context,
        packet_id="stacked-candidate",
        source_commit=stacked_commit,
        parent_tree_oid=baseline_tree,
    )
    with pytest.raises(state.StateError, match="current champion source tree"):
        runner._check_packet_phase(
            context,
            stacked_packet,
            now=FIXED_NOW + timedelta(minutes=1),
        )

    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "revert",
        "--no-edit",
        stacked_commit,
    )
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "revert",
        "--no-edit",
        discarded_commit,
    )
    assert git(repo_root, "rev-parse", "HEAD^{tree}") == baseline_tree
    candidate = repo_root / "candidate.txt"
    candidate.write_text("candidate\n", encoding="utf-8")
    git(repo_root, "add", "candidate.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "candidate after scoped rollback",
    )
    clean_packet = _payload_packet(
        context,
        packet_id="clean-candidate",
        parent_tree_oid=baseline_tree,
    )
    runner._check_packet_phase(
        context,
        clean_packet,
        now=FIXED_NOW + timedelta(minutes=2),
    )


def test_abandon_pending_is_immutable_and_resume_idempotent(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    first = _payload_packet(context, packet_id="stale-reserve")
    state.set_pending_packet(context, first)
    assert runner.recommend_next(
        context,
        now=FIXED_NOW + timedelta(hours=11),
    )["safeNextStep"] == "abandon-pending-for-finalization-reserve"
    tombstone = state.abandon_pending_packet(
        context,
        reason="finalization_reserve",
        now=FIXED_NOW + timedelta(hours=11),
    )
    assert tombstone["reason"] == "finalization_reserve"
    assert not paths.pending_packet.exists()
    assert paths.abandoned_packet("stale-reserve").is_file()
    assert "stale-reserve" in state.load_progress(context)["abandonedPacketIds"]

    second = _payload_packet(context, packet_id="abandon-crash-window")
    state.set_pending_packet(context, second)
    abandoned_at = FIXED_NOW + timedelta(hours=11, minutes=1)
    assert runner.recommend_next(
        context,
        now=FIXED_NOW + timedelta(hours=12),
    )["safeNextStep"] == "abandon-pending-deadline-expired"
    interrupted_tombstone = {
        "abandonContract": "InstallerSizePacketAbandonmentV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": second["packetId"],
        "packetSha256": state.file_sha256(paths.packet(second["packetId"])),
        "reason": "deadline_expired",
        "abandonedAtUtc": state.format_utc(abandoned_at),
    }
    state.store_immutable_json(
        paths.abandoned_packet(second["packetId"]),
        interrupted_tombstone,
    )

    state.initialize_run(
        context,
        resume=True,
        now=abandoned_at + timedelta(minutes=1),
    )
    assert not paths.pending_packet.exists()
    assert state.load_progress(context)["abandonedPacketIds"] == [
        "stale-reserve",
        "abandon-crash-window",
    ]
    events = [
        row
        for row in state.read_ledger(paths.ledger)
        if row["event"] == "packet_abandoned_before_dispatch"
    ]
    assert [row["payload"]["packetId"] for row in events] == [
        "stale-reserve",
        "abandon-crash-window",
    ]


def test_packet_started_hash_blocks_immutable_packet_rewrite(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "immutable-packet",
        "lane": "baseline",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "hypothesis": {
            "statement": "Measure a baseline.",
            "mechanism": "Create one inventory.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the inventory.",
        },
        "action": {
            "kind": "baseline-replica",
            "replica": 1,
            "resultRelativePath": "baselines/baseline-replica-1.json",
            "timeoutSeconds": 60,
            "dispatch": {},
        },
    }
    state.set_pending_packet(context, packet)
    state.append_ledger(
        paths.ledger,
        event="packet_started",
        payload={
            "packetId": packet["packetId"],
            "packetSha256": state.file_sha256(paths.packet(packet["packetId"])),
            "actionKind": "baseline-replica",
        },
        now=FIXED_NOW,
    )
    rewritten = state.load_json_object(paths.packet(packet["packetId"]))
    rewritten["hypothesis"]["rollback"] = "Hide the discarded changes."
    state.write_json_atomic(paths.packet(packet["packetId"]), rewritten)

    with pytest.raises(state.StateError, match="packet_started ledger hash"):
        state.load_progress(context)


def test_final_packet_allows_revert_commit_only_when_git_tree_matches_champion(
    tmp_path: Path,
) -> None:
    repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    product = repo_root / "product.txt"
    product.write_text("champion\n", encoding="utf-8")
    git(repo_root, "add", "product.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "champion",
    )
    champion_commit = git(repo_root, "rev-parse", "HEAD")
    champion_tree = state.git_tree_oid(repo_root, champion_commit)
    paths = state.paths_for(context)
    write_json(
        paths.champion,
        {
            "resultContract": "InstallerResearchResultV1",
            "packetId": "champion-packet",
            "sourceCommit": champion_commit,
            "decision": "keep",
        },
    )
    progress = state.load_progress(context)
    progress.update({"phase": "plateau", "championId": "champion-packet"})
    state.write_json_atomic(paths.progress, progress)

    product.write_text("discarded candidate\n", encoding="utf-8")
    git(repo_root, "add", "product.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "discarded candidate",
    )
    product.write_text("champion\n", encoding="utf-8")
    git(repo_root, "add", "product.txt")
    git(
        repo_root,
        "-c",
        "user.name=Scriber Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        "revert discarded candidate",
    )
    reverted_head = git(repo_root, "rev-parse", "HEAD")
    assert reverted_head != champion_commit
    assert state.git_tree_oid(repo_root, reverted_head) == champion_tree
    packet = {
        "packetContract": "InstallerResearchPacketV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "final-1-reverted-head",
        "lane": "final",
        "sourceCommit": reverted_head,
        "hypothesis": {
            "statement": "Reproduce the kept champion from an equivalent revert tree.",
            "mechanism": "Bind both commits to one exact Git tree object.",
            "expectedReductionBytes": 0,
            "risk": "low",
            "rollback": "Discard the final replica.",
        },
        "action": {
            "kind": "final-replica",
            "replica": 1,
            "championSha256": state.file_sha256(paths.champion),
            "championSourceTreeOid": champion_tree,
            "resultRelativePath": "packet-results/final-1-reverted-head.json",
            "timeoutSeconds": 60,
            "dispatch": {
                "driver": "powershell-file",
                "entrypoint": "scripts/run_installer_size_packet.ps1",
                "arguments": [
                    "-RunId",
                    RUN_ID,
                    "-Mode",
                    "final-1",
                ],
            },
        },
    }
    state.validate_packet(packet, run_id=RUN_ID)
    runner._check_packet_phase(
        context,
        packet,
        now=FIXED_NOW + timedelta(hours=1),
    )

    packet["action"]["championSourceTreeOid"] = "f" * 40
    with pytest.raises(state.StateError, match="source tree"):
        runner._check_packet_phase(
            context,
            packet,
            now=FIXED_NOW + timedelta(hours=1),
        )


def test_keep_gate_hashes_must_rehash_retained_packet_artifacts(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    packet = {
        "packetId": "candidate-retained-gates",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
    }
    evidence_root = (
        state.paths_for(context).root
        / "packet-evidence"
        / packet["packetId"]
    )
    youtube_detail_path = evidence_root / "youtube-holdouts-candidate.json"
    write_json(
        youtube_detail_path,
        {
            "holdoutSnapshotContract": "InstallerSizeYoutubeCandidateHoldoutsV1",
            "schemaVersion": 1,
            "status": "pass",
            "reasonCodes": [],
            "runId": RUN_ID,
            "packetId": packet["packetId"],
            "parentChampionId": "baseline",
            "sourceCommit": packet["sourceCommit"],
            "inputImmutabilityVerified": True,
            "cases": [{"id": f"case-{index}"} for index in range(6)],
        },
    )
    gates: dict[str, dict[str, str]] = {}
    for name in sorted(runner.FINAL_EXTERNAL_GATES):
        artifact_path = evidence_root / "gates" / f"{name}.json"
        write_json(
            artifact_path,
            {
                "gateArtifactContract": "InstallerResearchGateArtifactV1",
                "schemaVersion": 1,
                "runId": RUN_ID,
                "packetId": packet["packetId"],
                "parentChampionId": "baseline",
                "sourceCommit": packet["sourceCommit"],
                "gate": name,
                "status": "pass",
                "checks": [{"name": "fixture-check", "status": "pass"}],
                "detailEvidence": (
                    {
                        "kind": "candidate-youtube-holdout",
                        "relativePath": (
                            f"packet-evidence/{packet['packetId']}/"
                            "youtube-holdouts-candidate.json"
                        ),
                        "sha256": state.file_sha256(youtube_detail_path),
                    }
                    if name == "youtubeWorkflow"
                    else None
                ),
            },
        )
        gates[name] = {
            "status": "pass",
            "evidenceSha256": state.file_sha256(artifact_path),
        }
    write_json(
        evidence_root / "gate-evidence.json",
        {
            "gateEvidenceContract": "InstallerResearchGateEvidenceV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet["packetId"],
            "parentChampionId": "baseline",
            "sourceCommit": packet["sourceCommit"],
            "gates": gates,
        },
    )

    findings, evidence_sha = runner._validate_packet_gate_evidence(
        context,
        packet,
        expected_parent_champion_id="baseline",
        result_gates=gates,
    )
    assert findings == []
    assert evidence_sha == state.file_sha256(evidence_root / "gate-evidence.json")

    tampered = evidence_root / "gates" / "liveMic.json"
    tampered.write_text(tampered.read_text(encoding="utf-8") + " ", encoding="utf-8")
    findings, _evidence_sha = runner._validate_packet_gate_evidence(
        context,
        packet,
        expected_parent_champion_id="baseline",
        result_gates=gates,
    )
    assert any(item["code"] == "gate_artifact_hash_mismatch" for item in findings)

    youtube_detail_path.write_text(
        youtube_detail_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
    )
    findings, _evidence_sha = runner._validate_packet_gate_evidence(
        context,
        packet,
        expected_parent_champion_id="baseline",
        result_gates=gates,
    )
    assert any(
        item["code"] == "youtube_detail_evidence_hash_mismatch"
        for item in findings
    )


def test_final_full_suite_rehashes_every_retained_gate_artifact(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    packet = {
        "packetId": "final-retained-suite",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "action": {
            "kind": "final-replica",
            "replica": 1,
            "championSha256": "a" * 64,
            "championSourceTreeOid": state.git_tree_oid(
                context.repo_root,
                git(context.repo_root, "rev-parse", "HEAD"),
            ),
        },
    }
    evidence_root = (
        state.paths_for(context).root
        / "packet-evidence"
        / packet["packetId"]
    )
    gates: dict[str, dict[str, str]] = {}
    for name in sorted(runner.FINAL_FULL_SUITE_GATES):
        artifact_path = evidence_root / "full-suite" / f"{name}.json"
        write_json(
            artifact_path,
            {
                "fullSuiteGateArtifactContract": "InstallerResearchFullSuiteGateArtifactV1",
                "schemaVersion": 1,
                "runId": RUN_ID,
                "packetId": packet["packetId"],
                "sourceCommit": packet["sourceCommit"],
                "gate": name,
                "status": "pass",
                "checks": [{"name": "command-exit", "status": "pass"}],
            },
        )
        gates[name] = {
            "status": "pass",
            "evidenceSha256": state.file_sha256(artifact_path),
        }
    write_json(
        evidence_root / "full-suite-evidence.json",
        {
            "fullSuiteEvidenceContract": "InstallerResearchFullSuiteEvidenceV1",
            "schemaVersion": 1,
            "runId": RUN_ID,
            "packetId": packet["packetId"],
            "sourceCommit": packet["sourceCommit"],
            "championSha256": packet["action"]["championSha256"],
            "championSourceTreeOid": packet["action"]["championSourceTreeOid"],
            "gates": gates,
        },
    )

    findings, evidence_sha = runner._validate_final_full_suite_evidence(
        context,
        packet,
    )
    assert findings == []
    assert evidence_sha == state.file_sha256(
        evidence_root / "full-suite-evidence.json"
    )

    artifact = evidence_root / "full-suite" / "pythonPytest.json"
    artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
    findings, _evidence_sha = runner._validate_final_full_suite_evidence(
        context,
        packet,
    )
    assert any(
        item["code"] == "final_full_suite_artifact_hash_mismatch"
        for item in findings
    )


def test_bounded_dispatch_invokes_process_tree_cleanup_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FakeProcess:
        pid = 12345
        returncode = -9

        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["fixture"], timeout)
            return "", ""

        def poll(self):
            return self.returncode if self.killed else None

        def kill(self):
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        runner,
        "_attach_windows_kill_on_close_job",
        lambda _process: None,
    )

    def fake_terminate(received):
        assert received is process
        calls.append("tree")
        received.kill()

    monkeypatch.setattr(runner, "_terminate_process_tree", fake_terminate)
    with pytest.raises(subprocess.TimeoutExpired):
        runner._run_bounded_command(
            ["fixture"],
            cwd=tmp_path,
            timeout_seconds=30,
        )
    assert calls == ["tree"]


def test_lane_learning_locks_plateaus_and_expands_final_reserve(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    progress = state.load_progress(context)
    result = {
        "installer": {"deltaBytes": -400000},
        "reasonCodes": ["gate_failed:installTimingRegression"],
    }
    for index in range(10):
        lane = "locked-lane" if index < 3 else f"lane-{index}"
        packet = {
            "packetId": f"discard-{index}",
            "lane": lane,
            "hypothesis": {"expectedReductionBytes": 500000},
            "action": {"kind": "measure-candidate"},
        }
        update = runner._record_packet_learning(
            context,
            progress,
            packet,
            decision="discard",
            result=result,
            duration_seconds=5000 if index < 3 else 100,
        )
    locked = progress["laneLearning"]["locked-lane"]
    assert locked["alpha"] == 1.0
    assert locked["beta"] == 4.0
    assert locked["locked"] is True
    assert locked["lastExpectedReductionBytes"] == 500000
    assert locked["lastActualReductionBytes"] == 400000
    assert locked["validDiscardReasons"][0]["reasonCodes"] == [
        "gate_failed:installTimingRegression"
    ]
    assert progress["validDiscardsWithoutEvidence"] == 10
    assert progress["phase"] == "plateau"
    assert update["plateau"] is True
    assert state.effective_finalization_reserve(context, progress=progress) == 6250


def test_recommendation_exposes_exploration_lock_and_dynamic_reserve(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    paths = state.paths_for(context)
    progress = state.load_progress(context)
    progress["packetSequence"] = 3
    progress["laneLearning"] = {
        "nltk": {
            "alpha": 1.0,
            "beta": 4.0,
            "durationEwmaSeconds": 5000.0,
            "locked": True,
        }
    }
    state.write_json_atomic(paths.progress, progress)

    recommendation = runner.recommend_next(context, now=FIXED_NOW + timedelta(hours=1))

    assert recommendation["safeNextStep"] == "formulate-high-potential-exploration-packet"
    assert recommendation["learningPolicy"]["explorationDue"] is True
    assert recommendation["learningPolicy"]["lockedLanes"] == ["nltk"]
    assert recommendation["learningPolicy"]["effectiveFinalizationReserveSeconds"] == 6250


def test_result_validator_matches_authoritative_comparator_shape() -> None:
    result = {
        "resultContract": "InstallerResearchResultV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "candidate-1",
        "parentChampionId": "baseline",
        "hypothesis": "Remove an unused package family.",
        "sourceCommit": "a" * 40,
        "comparisonKind": "payload",
        "evaluatorHash": "b" * 64,
        "toolchainHash": "c" * 64,
        "compression": "bzip2",
        "installer": {
            "path": "Scriber_1.0.0_x64-setup.exe",
            "name": "Scriber_1.0.0_x64-setup.exe",
            "length": 100,
            "sha256": "d" * 64,
            "deltaBytes": -10,
            "deltaPercent": -10.0,
        },
        "payload": {
            "stagedBytes": 200,
            "installedBytes": 210,
            "exactTreeSha256": "e" * 64,
            "semanticTreeSha256": "f" * 64,
            "fileListSha256": "1" * 64,
            "deltaBytes": -20,
        },
        "attribution": {
            "componentMapSha256": "2" * 64,
            "components": {"python-core": {"rawBytes": 200}},
            "pyzInventorySha256": "3" * 64,
            "componentDeltas": {"python-core": -20},
            "pyzRootDeltas": {"sympy": -20},
        },
        "gates": {
            "bindings": {"status": "pass"},
            "functionalEvidence": {"status": "not_run"},
        },
        "installMeasurements": None,
        "decision": "measure_only",
        "reasonCodes": ["gate_not_run:functionalEvidence"],
    }
    assert evaluator.validate_result(result, expected_run_id=RUN_ID) == []
    assert not hasattr(evaluator, "BASELINE_CONTRACT")
    assert not hasattr(evaluator, "create_baseline")


def test_runner_rejects_forged_keep_without_mandatory_evidence(tmp_path: Path) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    _paths, baseline, doctor_report = _write_arm_inputs(context)
    state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    packet = {
        "packetId": "forged-keep",
        "sourceCommit": git(context.repo_root, "rev-parse", "HEAD"),
        "parentChampionId": "baseline",
        "hypothesis": {"statement": "Forge a nominal keep."},
    }
    result = {
        "resultContract": "InstallerResearchResultV1",
        "schemaVersion": 1,
        "runId": RUN_ID,
        "packetId": "forged-keep",
        "parentChampionId": "baseline",
        "hypothesis": "Forge a nominal keep.",
        "sourceCommit": packet["sourceCommit"],
        "comparisonKind": "payload",
        "evaluatorHash": baseline["evaluatorHash"],
        "toolchainHash": baseline["toolchainHash"],
        "compression": "bzip2",
        "installer": {
            "name": "Scriber_1.0.0_x64-setup.exe",
            "length": 100,
            "sha256": "3" * 64,
            "deltaPercent": -1.0,
        },
        "payload": {
            "stagedBytes": 100,
            "installedBytes": 100,
            "exactTreeSha256": "4" * 64,
            "semanticTreeSha256": "5" * 64,
            "fileListSha256": "6" * 64,
        },
        "attribution": {
            "componentMapSha256": baseline["componentMapSha256"],
            "components": {"fixture": {}},
            "pyzInventorySha256": "7" * 64,
            "componentDeltas": {},
            "pyzRootDeltas": {},
        },
        "installMeasurements": None,
        "gates": {"final": {"status": "pass"}},
        "decision": "keep",
        "reasonCodes": [],
    }
    findings = runner._validate_result_bindings(context, packet, result)
    assert any(item["code"] == "keep_mandatory_gates_missing" for item in findings)


def test_finalize_preview_never_claims_release_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "ok": False,
            "blocked": True,
            "findings": [{"level": "block", "code": "champion_missing"}],
        },
    )
    preview = runner.finalize_preview(context, now=FIXED_NOW)
    assert preview["researchChampionReady"] is False
    assert preview["releaseReady"] is False
    assert "signed-tag-release-not-run" in preview["releaseBlockers"]
    assert state.paths_for(context).finalize_preview.is_file()


def test_arbitrary_final_pass_cannot_promote_a_champion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths = state.paths_for(context)
    progress = state.load_progress(context)
    progress.update({"phase": "complete", "championId": "forged-final"})
    state.write_json_atomic(paths.progress, progress)
    write_json(
        paths.champion,
        {"decision": "keep", "gates": {"final": {"status": "pass"}}},
    )
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {"doctorContract": "InstallerSizeDoctorV1", "ok": True},
    )
    preview = runner.finalize_preview(context, now=FIXED_NOW)
    assert preview["researchComplete"] is False
    assert preview["researchChampionReady"] is False
    assert preview["finalProtocol"]["ok"] is False


def test_complete_final_evidence_cannot_end_the_fixed_clock_early(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _repo_root, context = make_repo(tmp_path)
    state.initialize_run(context, resume=False, now=FIXED_NOW)
    paths, baseline, doctor_report = _write_arm_inputs(context)
    armed = state.arm_research_clock(
        context,
        baseline=baseline,
        doctor_report=doctor_report,
        now=FIXED_NOW,
    )
    progress = state.load_progress(context)
    progress["phase"] = "complete"
    state.write_json_atomic(paths.progress, progress)
    monkeypatch.setattr(
        runner,
        "run_doctor",
        lambda *_args, **_kwargs: {
            "doctorContract": "InstallerSizeDoctorV1",
            "ok": True,
        },
    )
    deadline = state.parse_utc(
        armed["researchDeadlineUtc"],
        field="researchDeadlineUtc",
    )

    early = runner.finalize_preview(
        context,
        now=deadline - timedelta(milliseconds=500),
    )
    on_time = runner.finalize_preview(context, now=deadline)

    assert early["researchComplete"] is False
    assert on_time["researchComplete"] is True


def test_ux_evaluator_and_doctor_files_remain_byte_identical() -> None:
    for relative, expected in UX_FILE_HASHES.items():
        worktree_diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", relative],
            cwd=REPO_ROOT,
            check=False,
        )
        assert worktree_diff.returncode == 0, relative
        blob = subprocess.check_output(
            ["git", "cat-file", "blob", f"HEAD:{relative}"],
            cwd=REPO_ROOT,
        )
        actual = hashlib.sha256(blob).hexdigest()
        assert actual == expected, relative


def test_root_wrappers_keep_ux_as_default_and_route_installer_explicitly() -> None:
    autoresearch = (REPO_ROOT / "autoresearch.ps1").read_text(encoding="utf-8")
    assert '[string]$Profile = "ux"' in autoresearch
    assert 'Join-Path $repoRoot "scripts\\perf\\run.ps1"' in autoresearch
    assert 'Join-Path $repoRoot "scripts\\perf\\installer_size\\runner.py"' in autoresearch
    assert 'if ($Profile -eq "installer-size")' in autoresearch
    assert 'if ($RunId -or $Resume -or $PSBoundParameters.ContainsKey("DurationSeconds"))' in autoresearch

    next_wrapper = (REPO_ROOT / "next.ps1").read_text(encoding="utf-8")
    assert "next requires one existing pending-packet.json" in (
        REPO_ROOT / "scripts" / "perf" / "installer_size" / "runner.py"
    ).read_text(encoding="utf-8")
    assert 'if ($Profile -eq "installer-size")' in next_wrapper
    assert 'scripts\\perf\\autoresearch_state.py' in next_wrapper


def test_profile_config_protects_every_existing_evaluator_input() -> None:
    config = json.loads(
        (
            REPO_ROOT
            / "scripts"
            / "perf"
            / "profiles"
            / "installer-size"
            / "config.json"
        ).read_text(encoding="utf-8")
    )
    assert config["durationSeconds"] == 43200
    assert config["referenceCompression"] == "bzip2"
    assert config["minimumFreeBytes"] == 50 * 1024**3
    for relative in config["protectedInputs"]:
        assert (REPO_ROOT / relative).is_file(), relative
    assert {
        "scripts/run_installer_size_packet.ps1",
        "scripts/smoke_windows_installer.ps1",
        "scripts/smoke_rust_audio_sidecar.py",
        "scripts/smoke_diarization_worker_resource.py",
        "scripts/smoke_tauri_desktop.ps1",
        "scripts/validate_installer_youtube_holdouts.py",
        "tests/test_meeting_export.py",
        "tests/perf/test_upload_export_baseline_script.py",
        "tests/test_sidecar_metadata_privacy.py",
    }.issubset(set(config["protectedInputs"]))
    assert "/autoresearch-results/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
