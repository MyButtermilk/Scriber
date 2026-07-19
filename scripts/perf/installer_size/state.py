from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from scripts.perf.autoresearch_profiles import ProfileContext, canonical_run_id


STATE_SCHEMA_VERSION = 1
SESSION_INIT_CONTRACT = "InstallerSizeResearchSessionInitV1"
RUN_MANIFEST_CONTRACT = "InstallerSizeResearchRunV1"
PROGRESS_CONTRACT = "InstallerSizeResearchProgressV1"
LEDGER_CONTRACT = "InstallerSizeResearchLedgerEntryV1"
PACKET_CONTRACT = "InstallerResearchPacketV1"
GENESIS_HASH = "0" * 64
TERMINAL_DECISIONS = {
    "baseline_accept",
    "final_replica_accept",
    "keep",
    "discard",
    "checks_failed",
    "invalid_measurement",
    "crash",
    "measure_only",
    "abandoned",
}
ABANDON_REASONS = frozenset(
    {
        "deadline_expired",
        "finalization_reserve",
        "source_superseded",
        "operator_canceled",
    }
)


class StateError(RuntimeError):
    """Raised when installer-size state cannot be trusted or mutated safely."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise StateError(f"{field} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise StateError(f"{field} is not a valid UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise StateError(f"{field} must include UTC timezone information")
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise StateError(f"missing state file: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"invalid JSON state file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"state file must contain a JSON object: {path.name}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git_capture(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise StateError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_snapshot(repo_root: Path) -> dict[str, Any]:
    status = [line for line in _git_capture(repo_root, "status", "--porcelain=v1").splitlines() if line]
    return {
        "sourceCommit": _git_capture(repo_root, "rev-parse", "HEAD"),
        "sourceBranch": _git_capture(repo_root, "branch", "--show-current"),
        "dirtyEntries": status,
    }


def git_tree_oid(repo_root: Path, source_commit: str) -> str:
    tree_oid = _git_capture(repo_root, "rev-parse", f"{source_commit}^{{tree}}")
    if not (
        len(tree_oid) in (40, 64)
        and tree_oid == tree_oid.casefold()
        and all(character in "0123456789abcdef" for character in tree_oid)
    ):
        raise StateError("git returned an invalid source tree object id")
    return tree_oid


def baseline_requirement_source_identities(repo_root: Path) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for name in ("requirements-base.txt", "requirements-build.txt"):
        path = repo_root / name
        if not path.is_file():
            raise StateError(f"installer-size initialization requires {name}")
        info = path.lstat()
        if path.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise StateError(f"installer-size initialization requires plain {name}")
        identities.append(
            {
                "name": name,
                "length": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return identities


def _baseline_requirement_snapshot_identities(paths: RunPaths) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for path in (
        paths.baseline_requirements_base,
        paths.baseline_requirements_build,
    ):
        if not path.is_file():
            raise StateError(f"missing immutable baseline snapshot: {path.name}")
        info = path.lstat()
        if path.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise StateError(f"baseline snapshot must be a plain file: {path.name}")
        identities.append(
            {
                "name": path.name,
                "length": info.st_size,
                "sha256": file_sha256(path),
            }
        )
    return identities


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    session_init: Path
    manifest: Path
    progress: Path
    preflight: Path
    holdout_snapshot: Path
    wheelhouse_manifest: Path
    environment_manifest: Path
    toolchain_manifest: Path
    baseline: Path
    champion: Path
    last_run: Path
    ledger: Path
    pending_packet: Path
    finalize_preview: Path
    snapshots_dir: Path
    preflight_dir: Path
    baselines_dir: Path
    packets_dir: Path
    packet_results_dir: Path
    abandoned_packets_dir: Path
    interrupted_results_dir: Path
    failed_results_dir: Path
    interruptions_dir: Path
    final_dir: Path
    dispatch_lock: Path
    baseline_requirements_base: Path
    baseline_requirements_build: Path

    def baseline_replica(self, index: int) -> Path:
        if index not in (1, 2):
            raise StateError("baseline replica index must be 1 or 2")
        return self.baselines_dir / f"baseline-replica-{index}.json"

    def packet(self, packet_id: str) -> Path:
        return self.packets_dir / f"{safe_packet_id(packet_id)}.json"

    def packet_result(self, packet_id: str) -> Path:
        return self.packet_results_dir / f"{safe_packet_id(packet_id)}.json"

    def abandoned_packet(self, packet_id: str) -> Path:
        return self.abandoned_packets_dir / f"{safe_packet_id(packet_id)}.json"

    def interrupted_result(self, packet_id: str) -> Path:
        return self.interrupted_results_dir / f"{safe_packet_id(packet_id)}.json"

    def failed_result(self, packet_id: str) -> Path:
        return self.failed_results_dir / f"{safe_packet_id(packet_id)}.json"

    def interruption(self, packet_id: str) -> Path:
        return self.interruptions_dir / f"{safe_packet_id(packet_id)}.json"


def _paths_for_root(context: ProfileContext, root: Path) -> RunPaths:
    if not context.is_installer_size or context.run_root is None or context.run_id is None:
        raise StateError("installer-size RunPaths require a resolved installer-size RunId")
    final_root = context.state_root / context.run_id
    return RunPaths(
        root=root,
        session_init=root / "snapshots" / "session-init.json",
        manifest=root / "run-manifest.json",
        progress=root / "progress.json",
        preflight=root / "preflight" / "preflight.json",
        holdout_snapshot=root / "preflight" / "youtube-holdouts.snapshot.json",
        wheelhouse_manifest=root / "wheelhouse-manifest.json",
        environment_manifest=root / "environments" / "baseline" / "environment-manifest.json",
        toolchain_manifest=root / "toolchain" / "toolchain-manifest.json",
        baseline=root / "baseline.json",
        champion=root / "champion.json",
        last_run=root / "last-run.json",
        ledger=root / "ledger.jsonl",
        pending_packet=root / "pending-packet.json",
        finalize_preview=root / "finalize-preview.json",
        snapshots_dir=root / "snapshots",
        preflight_dir=root / "preflight",
        baselines_dir=root / "baselines",
        packets_dir=root / "packets",
        packet_results_dir=root / "packet-results",
        abandoned_packets_dir=root / "abandoned-packets",
        interrupted_results_dir=root / "interrupted-results",
        failed_results_dir=root / "failed-results",
        interruptions_dir=root / "interruptions",
        final_dir=root / "final",
        dispatch_lock=final_root.parent / ".locks" / f"{context.run_id}.lock",
        baseline_requirements_base=root / "snapshots" / "requirements-base.txt",
        baseline_requirements_build=root / "snapshots" / "requirements-build.txt",
    )


def paths_for(context: ProfileContext) -> RunPaths:
    if not context.is_installer_size or context.run_root is None or context.run_id is None:
        raise StateError("installer-size RunPaths require a resolved installer-size RunId")
    return _paths_for_root(context, context.run_root)


def safe_packet_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 96:
        raise StateError("packetId must contain between 1 and 96 characters")
    if not all(character.isalnum() or character in {"-", "_"} for character in text):
        raise StateError("packetId may contain only letters, digits, '-' and '_'")
    return text


def safe_lane_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        raise StateError("lane must contain between 1 and 64 characters")
    if not all(character.isalnum() or character in {"-", "_"} for character in text):
        raise StateError("lane may contain only letters, digits, '-' and '_'")
    return text


@contextmanager
def acquire_dispatch_lock(context: ProfileContext):
    """Hold the run-scoped, cross-process dispatch mutation lane."""

    path = paths_for(context).dispatch_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise StateError(
                "another installer-size dispatch or resume operation is still active"
            ) from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def git_parent_oids(repo_root: Path, source_commit: str) -> list[str]:
    fields = _git_capture(
        repo_root,
        "rev-list",
        "--parents",
        "-n",
        "1",
        source_commit,
    ).split()
    if not fields or fields[0] != source_commit:
        raise StateError("git returned invalid commit parent provenance")
    parents = fields[1:]
    for parent in parents:
        if not (
            len(parent) in (40, 64)
            and parent == parent.casefold()
            and all(character in "0123456789abcdef" for character in parent)
        ):
            raise StateError("git returned an invalid parent object id")
    return parents


def _protected_input_hashes(context: ProfileContext) -> dict[str, str]:
    config = context.config
    protected = config.get("protectedInputs")
    if not isinstance(protected, list) or not protected:
        raise StateError("installer-size config protectedInputs must be a non-empty array")
    required_profile_inputs = [
        context.config_path.relative_to(context.repo_root).as_posix(),
        context.goal_path.relative_to(context.repo_root).as_posix(),
    ]
    hashes: dict[str, str] = {}
    for item in [*protected, *required_profile_inputs]:
        relative = str(item or "").strip().replace("\\", "/")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise StateError(f"unsafe protected input path: {item!r}")
        path = context.repo_root.joinpath(*relative.split("/"))
        if not path.is_file():
            raise StateError(f"missing protected input: {relative}")
        hashes[relative] = file_sha256(path)
    return hashes


def _expected_manifest_bindings(context: ProfileContext) -> dict[str, Any]:
    paths = paths_for(context)
    baseline = load_json_object(paths.baseline)
    environment = load_json_object(paths.environment_manifest)
    toolchain = load_json_object(paths.toolchain_manifest)
    return {
        "sessionInitSha256": file_sha256(paths.session_init),
        "baselineRequirementsBaseSha256": file_sha256(
            paths.baseline_requirements_base
        ),
        "baselineRequirementsBuildSha256": file_sha256(
            paths.baseline_requirements_build
        ),
        "preflightSha256": file_sha256(paths.preflight),
        "baselineSha256": file_sha256(paths.baseline),
        "baselineReplica1Sha256": file_sha256(paths.baseline_replica(1)),
        "baselineReplica2Sha256": file_sha256(paths.baseline_replica(2)),
        "toolchainManifestSha256": file_sha256(paths.toolchain_manifest),
        "environmentManifestSha256": file_sha256(paths.environment_manifest),
        "wheelhouseManifestSha256": file_sha256(paths.wheelhouse_manifest),
        "youtubeHoldoutSnapshotSha256": file_sha256(paths.holdout_snapshot),
        "evaluatorHash": baseline.get("evaluatorHash"),
        "toolchainHash": baseline.get("toolchainHash"),
        "componentMapSha256": baseline.get("componentMapSha256"),
        "productDependenciesSha256": environment.get("productDependenciesSha256"),
        "semanticTreeSha256": baseline.get("semanticTreeSha256"),
        "fileListSha256": baseline.get("fileListSha256"),
        "pyzInventorySha256": baseline.get("pyzInventorySha256"),
        "rustToolchain": toolchain.get("rustToolchain"),
    }


def validate_manifest(context: ProfileContext, manifest: dict[str, Any]) -> None:
    if manifest.get("runContract") != RUN_MANIFEST_CONTRACT:
        raise StateError("run manifest contract mismatch")
    if manifest.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise StateError("run manifest schema mismatch")
    if manifest.get("profile") != "installer-size":
        raise StateError("run manifest profile mismatch")
    if canonical_run_id(str(manifest.get("runId") or "")) != context.run_id:
        raise StateError("run manifest RunId mismatch")
    if manifest.get("durationSeconds") != context.duration_seconds:
        raise StateError("resume cannot change the original duration")
    session_init = load_session_init(context)
    exact_session_fields = {
        "campaign": context.config.get("campaign"),
        "createdAtUtc": session_init.get("createdAtUtc"),
        "sourceCommit": session_init.get("sourceCommit"),
        "sourceBranch": session_init.get("sourceBranch"),
        "referenceCompression": context.config.get("referenceCompression"),
    }
    for field, expected in exact_session_fields.items():
        if manifest.get(field) != expected:
            raise StateError(f"run manifest {field} differs from session initialization")
    started = parse_utc(manifest.get("researchStartedAtUtc"), field="researchStartedAtUtc")
    deadline = parse_utc(manifest.get("researchDeadlineUtc"), field="researchDeadlineUtc")
    if deadline != started + timedelta(seconds=int(context.duration_seconds or 0)):
        raise StateError("run manifest deadline does not match the immutable duration")
    hashes = manifest.get("protectedInputHashes")
    if not isinstance(hashes, dict) or not hashes:
        raise StateError("run manifest has no protected input hashes")
    if hashes != session_init.get("protectedInputHashes"):
        raise StateError(
            "run manifest protected input hashes differ from session initialization"
        )
    if protected_drift(context, session_init):
        raise StateError("protected installer-size inputs drifted after session initialization")
    bindings = manifest.get("bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise StateError("run manifest has no frozen evidence bindings")
    expected_bindings = _expected_manifest_bindings(context)
    if any(value in (None, "") for value in expected_bindings.values()):
        raise StateError("authoritative run manifest bindings are incomplete")
    if bindings != expected_bindings:
        raise StateError(
            "run manifest bindings differ from the authoritative immutable artifacts"
        )


def validate_session_init(context: ProfileContext, payload: dict[str, Any]) -> None:
    if payload.get("sessionInitContract") != SESSION_INIT_CONTRACT:
        raise StateError("session-init contract mismatch")
    if payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise StateError("session-init schema mismatch")
    if payload.get("profile") != "installer-size" or payload.get("runId") != context.run_id:
        raise StateError("session-init identity mismatch")
    if payload.get("durationSeconds") != context.duration_seconds:
        raise StateError("resume cannot change the original duration")
    if payload.get("researchStartedAtUtc") is not None or payload.get("researchDeadlineUtc") is not None:
        raise StateError("session-init must not claim that the research clock started")
    hashes = payload.get("protectedInputHashes")
    if not isinstance(hashes, dict) or not hashes:
        raise StateError("session-init has no protected input hashes")
    requirement_sources = payload.get("baselineRequirementSources")
    if (
        not isinstance(requirement_sources, list)
        or [item.get("name") for item in requirement_sources if isinstance(item, dict)]
        != ["requirements-base.txt", "requirements-build.txt"]
        or any(
            not isinstance(item, dict)
            or set(item) != {"name", "length", "sha256"}
            or isinstance(item.get("length"), bool)
            or not isinstance(item.get("length"), int)
            or item.get("length", -1) < 0
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or item["sha256"] != item["sha256"].casefold()
            or any(
                character not in "0123456789abcdef"
                for character in item["sha256"]
            )
            for item in requirement_sources
        )
    ):
        raise StateError("session-init has invalid baseline requirement identities")


def load_session_init(context: ProfileContext) -> dict[str, Any]:
    paths = paths_for(context)
    payload = load_json_object(paths.session_init)
    validate_session_init(context, payload)
    rows = read_ledger(paths.ledger)
    initialized = [row for row in rows if row.get("event") == "run_initialized"]
    if len(initialized) != 1 or initialized[0].get("sequence") != 1:
        raise StateError("session initialization ledger binding is missing or duplicated")
    ledger_payload = initialized[0].get("payload")
    expected = {
        "runId": context.run_id,
        "sourceCommit": payload.get("sourceCommit"),
        "durationSeconds": context.duration_seconds,
        "researchClockStarted": False,
        "sessionInitSha256": file_sha256(paths.session_init),
        "baselineRequirementsBaseSha256": file_sha256(
            paths.baseline_requirements_base
        ),
        "baselineRequirementsBuildSha256": file_sha256(
            paths.baseline_requirements_build
        ),
    }
    if ledger_payload != expected:
        raise StateError("session initialization differs from its immutable ledger binding")
    return payload


def _expected_clock_ledger_payload(
    context: ProfileContext,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    paths = paths_for(context)
    return {
        "researchStartedAtUtc": manifest["researchStartedAtUtc"],
        "researchDeadlineUtc": manifest["researchDeadlineUtc"],
        "durationSeconds": context.duration_seconds,
        "baselineSha256": file_sha256(paths.baseline),
        "runManifestSha256": file_sha256(paths.manifest),
    }


def _validate_clock_ledger_binding(
    context: ProfileContext,
    manifest: dict[str, Any],
    *,
    allow_missing: bool = False,
) -> bool:
    rows = read_ledger(paths_for(context).ledger)
    clock_rows = [row for row in rows if row.get("event") == "research_clock_started"]
    if not clock_rows:
        if allow_missing:
            return False
        raise StateError("immutable run manifest has no research clock ledger binding")
    if len(clock_rows) != 1:
        raise StateError("research clock ledger binding must occur exactly once")
    payload = clock_rows[0].get("payload")
    if not isinstance(payload, dict):
        raise StateError("research clock ledger binding payload is invalid")
    expected = _expected_clock_ledger_payload(context, manifest)
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            raise StateError(
                f"research clock ledger {field} differs from the immutable run manifest"
            )
    return True


def _ensure_clock_ledger_binding(
    context: ProfileContext,
    manifest: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    if _validate_clock_ledger_binding(context, manifest, allow_missing=True):
        return False
    append_ledger(
        paths_for(context).ledger,
        event="research_clock_started",
        payload={
            **_expected_clock_ledger_payload(context, manifest),
            "recoveredAfterInterruptedArm": True,
        },
        now=now,
    )
    return True


def load_manifest(
    context: ProfileContext,
    *,
    require_clock_ledger: bool = True,
) -> dict[str, Any]:
    manifest = load_json_object(paths_for(context).manifest)
    validate_manifest(context, manifest)
    if require_clock_ledger:
        _validate_clock_ledger_binding(context, manifest)
    return manifest


def _validate_started_packet_hashes(context: ProfileContext) -> None:
    paths = paths_for(context)
    started: dict[str, str] = {}
    for row in read_ledger(paths.ledger):
        if row.get("event") != "packet_started":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise StateError("packet_started ledger payload is invalid")
        packet_id = safe_packet_id(payload.get("packetId"))
        claimed = str(payload.get("packetSha256") or "")
        if packet_id in started:
            raise StateError("immutable packet has more than one packet_started entry")
        packet_path = paths.packet(packet_id)
        if not packet_path.is_file():
            raise StateError("packet_started ledger entry has no immutable packet record")
        actual = file_sha256(packet_path)
        if claimed != actual:
            raise StateError(
                "immutable packet differs from its packet_started ledger hash"
            )
        started[packet_id] = claimed


def load_progress(
    context: ProfileContext,
    *,
    allow_manifest_clock_recovery: bool = False,
) -> dict[str, Any]:
    paths = paths_for(context)
    progress = load_json_object(paths.progress)
    if progress.get("progressContract") != PROGRESS_CONTRACT:
        raise StateError("progress contract mismatch")
    if progress.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise StateError("progress schema mismatch")
    if progress.get("runId") != context.run_id:
        raise StateError("progress RunId mismatch")
    if progress.get("phase") not in {"prepare", "run", "finalizing", "plateau", "complete"}:
        raise StateError("progress phase is invalid")
    final_replica_ids = progress.get("finalReplicaPacketIds")
    if (
        not isinstance(final_replica_ids, list)
        or len(final_replica_ids) > 2
        or len(set(final_replica_ids)) != len(final_replica_ids)
    ):
        raise StateError("progress final replica packet ids are invalid")
    for packet_id in final_replica_ids:
        safe_packet_id(packet_id)
    interrupted_ids = progress.get("interruptedPacketIds")
    if not isinstance(interrupted_ids, list) or len(set(interrupted_ids)) != len(interrupted_ids):
        raise StateError("progress interrupted packet ids are invalid")
    for packet_id in interrupted_ids:
        safe_packet_id(packet_id)
    abandoned_ids = progress.get("abandonedPacketIds")
    if not isinstance(abandoned_ids, list) or len(set(abandoned_ids)) != len(
        abandoned_ids
    ):
        raise StateError("progress abandoned packet ids are invalid")
    for packet_id in abandoned_ids:
        safe_packet_id(packet_id)
    learning = progress.get("laneLearning")
    if not isinstance(learning, dict):
        raise StateError("progress lane learning state is invalid")
    for lane_id, lane_state in learning.items():
        safe_lane_id(lane_id)
        if not isinstance(lane_state, dict):
            raise StateError("progress lane learning entry is invalid")
    started = progress.get("researchStartedAtUtc")
    deadline = progress.get("researchDeadlineUtc")
    manifest = (
        load_manifest(context)
        if paths.manifest.is_file()
        else None
    )
    if bool(started) != bool(deadline) and not (
        manifest is not None and allow_manifest_clock_recovery
    ):
        raise StateError("research start and deadline must be written together")
    if started and deadline:
        start_time = parse_utc(started, field="researchStartedAtUtc")
        deadline_time = parse_utc(deadline, field="researchDeadlineUtc")
        expected = start_time + timedelta(seconds=int(context.duration_seconds or 0))
        if deadline_time != expected:
            raise StateError("research deadline does not match the immutable duration")
    if manifest is None and (started or deadline):
        raise StateError("research progress claims a clock without an immutable run manifest")
    if manifest is not None:
        manifest_clock = (
            manifest.get("researchStartedAtUtc"),
            manifest.get("researchDeadlineUtc"),
        )
        progress_clock = (started, deadline)
        if progress_clock != manifest_clock and not allow_manifest_clock_recovery:
            raise StateError(
                "progress research clock differs from the immutable run manifest"
            )
    _validate_started_packet_hashes(context)
    return progress


def _assert_no_reparse_ancestors(path: Path) -> None:
    """Reject every existing component without resolving through it."""

    absolute = Path(os.path.abspath(path))
    for current in reversed((absolute, *absolute.parents)):
        if not os.path.lexists(current):
            continue
        info = current.lstat()
        if current.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise StateError(
                "initialization path contains a symlink or reparse-point ancestor"
            )


def _validate_initialization_staging_path(
    staging_root: Path,
    final_root: Path,
) -> None:
    expected_parent = final_root.parent / ".initializing"
    expected_prefix = f".{final_root.name}."
    if (
        staging_root.parent != expected_parent
        or not staging_root.name.startswith(expected_prefix)
    ):
        raise StateError("untrusted initialization staging path")
    _assert_no_reparse_ancestors(staging_root)
    try:
        info = staging_root.lstat()
    except FileNotFoundError:
        return
    if (
        staging_root.is_symlink()
        or not staging_root.is_dir()
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise StateError("initialization staging root must be a plain directory")


def _discard_initialization_staging(staging_root: Path, final_root: Path) -> None:
    """Best-effort cleanup for a private, never-authoritative init directory."""

    _validate_initialization_staging_path(staging_root, final_root)
    try:
        shutil.rmtree(staging_root)
    except FileNotFoundError:
        pass
    except OSError:
        # A process termination can leave this private directory behind. Each
        # retry uses a unique sibling, so cleanup failure cannot bind the RunId.
        pass


def _promote_initialization_staging(staging_root: Path, final_root: Path) -> None:
    """Publish a complete fresh-run state tree through one atomic rename."""

    _validate_initialization_staging_path(staging_root, final_root)
    if os.path.lexists(final_root):
        raise StateError("run state already exists; use -Resume with the same RunId")
    os.replace(staging_root, final_root)


def initialize_run(
    context: ProfileContext,
    *,
    resume: bool,
    now: datetime | None = None,
    _lock_held: bool = False,
) -> tuple[RunPaths, dict[str, Any]]:
    if not _lock_held:
        with acquire_dispatch_lock(context):
            return initialize_run(
                context,
                resume=resume,
                now=now,
                _lock_held=True,
            )
    paths = paths_for(context)
    if resume:
        with nullcontext():
            recovered_at = now or utc_now()
            session_init = load_session_init(context)
            if session_init.get("baselineRequirementSources") != (
                _baseline_requirement_snapshot_identities(paths)
            ):
                raise StateError(
                    "immutable baseline requirement snapshots differ from session initialization"
                )
            manifest: dict[str, Any] | None = None
            if paths.manifest.is_file():
                manifest = load_json_object(paths.manifest)
                validate_manifest(context, manifest)
                _ensure_clock_ledger_binding(
                    context,
                    manifest,
                    now=recovered_at,
                )
                manifest = load_manifest(context)
            progress = load_progress(
                context,
                allow_manifest_clock_recovery=manifest is not None,
            )
            if manifest is not None:
                prior_clock = {
                    "researchStartedAtUtc": progress.get("researchStartedAtUtc"),
                    "researchDeadlineUtc": progress.get("researchDeadlineUtc"),
                }
                manifest_clock = {
                    "researchStartedAtUtc": manifest["researchStartedAtUtc"],
                    "researchDeadlineUtc": manifest["researchDeadlineUtc"],
                }
                if prior_clock != manifest_clock:
                    if not progress.get("researchStartedAtUtc"):
                        progress["phase"] = "run"
                        progress["baselineReplicasAccepted"] = 2
                    progress.update(manifest_clock)
                    progress["updatedAtUtc"] = format_utc(recovered_at)
                    write_json_atomic(paths.progress, progress)
                    append_ledger(
                        paths.ledger,
                        event="progress_clock_recovered_from_manifest",
                        payload={
                            "priorClockSha256": sha256_bytes(
                                canonical_json_bytes(prior_clock)
                            ),
                            "runManifestSha256": file_sha256(paths.manifest),
                            **manifest_clock,
                        },
                        now=recovered_at,
                    )
                    progress = load_progress(context)
            read_ledger(paths.ledger)
            active_packet_id = progress.get("activePacketId")
            if not active_packet_id and _reconcile_interrupted_pending(
                context,
                progress=progress,
            ):
                progress = load_progress(context)
                active_packet_id = progress.get("activePacketId")
            if not active_packet_id and _reconcile_completed_pending(
                context,
                now=recovered_at,
            ):
                progress = load_progress(context)
                active_packet_id = progress.get("activePacketId")
            if active_packet_id:
                progress = _interrupt_active_packet(
                    context,
                    progress=progress,
                    packet_id=safe_packet_id(active_packet_id),
                    now=recovered_at,
                )
            _recover_abandoned_pending(context, now=recovered_at)
            return paths, manifest or session_init
    if os.path.lexists(paths.root):
        raise StateError("run state already exists; use -Resume with the same RunId")

    source = git_snapshot(context.repo_root)
    if source["dirtyEntries"]:
        raise StateError("installer-size session initialization requires a clean Git worktree")
    hashes = _protected_input_hashes(context)
    source_requirement_identities = baseline_requirement_source_identities(
        context.repo_root
    )
    created = now or utc_now()
    staging_parent = paths.root.parent / ".initializing"
    _assert_no_reparse_ancestors(staging_parent)
    staging_parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_ancestors(staging_parent)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{context.run_id}.", dir=str(staging_parent))
    )
    _validate_initialization_staging_path(staging_root, paths.root)
    staging_context = replace(context, run_root=staging_root)
    staged_paths = _paths_for_root(staging_context, staging_root)
    try:
        for directory in (
            staged_paths.snapshots_dir,
            staged_paths.preflight_dir,
            staged_paths.baselines_dir,
            staged_paths.packets_dir,
            staged_paths.packet_results_dir,
            staged_paths.abandoned_packets_dir,
            staged_paths.interrupted_results_dir,
            staged_paths.failed_results_dir,
            staged_paths.interruptions_dir,
            staged_paths.final_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        for source_name, destination in (
            ("requirements-base.txt", staged_paths.baseline_requirements_base),
            ("requirements-build.txt", staged_paths.baseline_requirements_build),
        ):
            write_bytes_atomic(
                destination,
                (context.repo_root / source_name).read_bytes(),
            )
        snapshot_identities = _baseline_requirement_snapshot_identities(staged_paths)
        if snapshot_identities != source_requirement_identities:
            raise StateError(
                "baseline requirement snapshot copy differs from clean source bytes"
            )

        session_init = {
            "sessionInitContract": SESSION_INIT_CONTRACT,
            "schemaVersion": STATE_SCHEMA_VERSION,
            "profile": "installer-size",
            "campaign": context.config.get("campaign"),
            "runId": context.run_id,
            "createdAtUtc": format_utc(created),
            "durationSeconds": context.duration_seconds,
            "sourceCommit": source["sourceCommit"],
            "sourceBranch": source["sourceBranch"],
            "referenceCompression": context.config.get("referenceCompression"),
            "baselineRequirementSources": snapshot_identities,
            "protectedInputHashes": hashes,
            "researchStartedAtUtc": None,
            "researchDeadlineUtc": None,
            "platform": platform.system(),
            "platformRelease": platform.release(),
            "machine": platform.machine(),
            "pythonVersion": platform.python_version(),
            "freeBytes": shutil.disk_usage(context.repo_root).free,
            "worktreeClean": True,
        }
        write_json_atomic(staged_paths.session_init, session_init)
        write_json_atomic(
            staged_paths.progress,
            {
                "progressContract": PROGRESS_CONTRACT,
                "schemaVersion": STATE_SCHEMA_VERSION,
                "runId": context.run_id,
                "phase": "prepare",
                "baselineReplicasAccepted": 0,
                "researchStartedAtUtc": None,
                "researchDeadlineUtc": None,
                "packetSequence": 0,
                "activePacketId": None,
                "interruptedPacketIds": [],
                "abandonedPacketIds": [],
                "championId": None,
                "finalReplicaPacketIds": [],
                "validDiscardsWithoutEvidence": 0,
                "laneLearning": {},
                "updatedAtUtc": format_utc(created),
            },
        )
        append_ledger(
            staged_paths.ledger,
            event="run_initialized",
            payload={
                "runId": context.run_id,
                "sourceCommit": source["sourceCommit"],
                "durationSeconds": context.duration_seconds,
                "researchClockStarted": False,
                "sessionInitSha256": file_sha256(staged_paths.session_init),
                "baselineRequirementsBaseSha256": file_sha256(
                    staged_paths.baseline_requirements_base
                ),
                "baselineRequirementsBuildSha256": file_sha256(
                    staged_paths.baseline_requirements_build
                ),
            },
            now=created,
        )

        # Validate the complete private tree with the same readers Resume uses.
        session_init = load_session_init(staging_context)
        load_progress(staging_context)
        _promote_initialization_staging(staging_root, paths.root)
    finally:
        if staging_root.exists():
            _discard_initialization_staging(staging_root, paths.root)

    # A crash after the rename leaves a complete, ordinarily resumable run.
    session_init = load_session_init(context)
    load_progress(context)
    return paths, session_init


def protected_drift(context: ProfileContext, binding: dict[str, Any]) -> list[dict[str, str]]:
    expected = binding.get("protectedInputHashes")
    if not isinstance(expected, dict):
        return [{"path": "run-manifest.json", "reason": "missing_hash_map"}]
    drift: list[dict[str, str]] = []
    for relative, expected_hash in sorted(expected.items()):
        path = context.repo_root.joinpath(*str(relative).split("/"))
        if not path.is_file():
            drift.append({"path": str(relative), "reason": "missing"})
            continue
        actual = file_sha256(path)
        if actual.casefold() != str(expected_hash).casefold():
            drift.append({"path": str(relative), "reason": "sha256_mismatch"})
    return drift


def _entry_hash(entry_without_hash: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(entry_without_hash))


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    previous = GENESIS_HASH
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise StateError(f"ledger contains a blank line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StateError(f"ledger line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise StateError(f"ledger line {line_number} is not an object")
        if value.get("ledgerContract") != LEDGER_CONTRACT:
            raise StateError(f"ledger contract mismatch at line {line_number}")
        if value.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise StateError(f"ledger schema mismatch at line {line_number}")
        if value.get("sequence") != line_number:
            raise StateError(f"ledger sequence mismatch at line {line_number}")
        if value.get("previousEntrySha256") != previous:
            raise StateError(f"ledger previous hash mismatch at line {line_number}")
        claimed = str(value.get("entrySha256") or "")
        unsigned = {key: item for key, item in value.items() if key != "entrySha256"}
        actual = _entry_hash(unsigned)
        if claimed != actual:
            raise StateError(f"ledger entry hash mismatch at line {line_number}")
        previous = claimed
        rows.append(value)
    return rows


def append_ledger(
    path: Path,
    *,
    event: str,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    rows = read_ledger(path)
    unsigned = {
        "ledgerContract": LEDGER_CONTRACT,
        "schemaVersion": STATE_SCHEMA_VERSION,
        "sequence": len(rows) + 1,
        "previousEntrySha256": rows[-1]["entrySha256"] if rows else GENESIS_HASH,
        "recordedAtUtc": format_utc(now or utc_now()),
        "event": str(event),
        "payload": payload,
    }
    entry = {**unsigned, "entrySha256": _entry_hash(unsigned)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json_bytes(entry).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def write_preflight(
    context: ProfileContext,
    *,
    findings: Iterable[dict[str, Any]],
    accepted: bool,
    evidence_hashes: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    paths = paths_for(context)
    if paths.preflight.exists():
        raise StateError("preflight evidence is immutable once written")
    payload = {
        "preflightContract": "InstallerSizePreflightV1",
        "schemaVersion": STATE_SCHEMA_VERSION,
        "runId": context.run_id,
        "capturedAtUtc": format_utc(now or utc_now()),
        "accepted": bool(accepted),
        "findings": list(findings),
        "evidenceHashes": dict(sorted((evidence_hashes or {}).items())),
    }
    write_json_atomic(paths.preflight, payload)
    append_ledger(
        paths.ledger,
        event="preflight_recorded",
        payload={"accepted": bool(accepted), "findingCount": len(payload["findings"])},
        now=now,
    )
    return payload


def store_immutable_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        existing = load_json_object(path)
        if canonical_json_bytes(existing) != canonical_json_bytes(value):
            raise StateError(f"immutable artifact already exists with different content: {path.name}")
        return
    write_json_atomic(path, value)


def arm_research_clock(
    context: ProfileContext,
    *,
    baseline: dict[str, Any],
    doctor_report: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    paths = paths_for(context)
    progress = load_progress(context)
    if progress.get("researchStartedAtUtc"):
        load_manifest(context)
        return progress
    session_init = load_session_init(context)
    preflight = load_json_object(paths.preflight)
    if preflight.get("accepted") is not True:
        raise StateError("research clock cannot start before accepted preflight")
    if not accepted_baseline_replica_packet_id(
        context, 1
    ) or not accepted_baseline_replica_packet_id(context, 2):
        raise StateError(
            "research clock cannot start before two ledger-accepted baseline replicas"
        )
    if baseline.get("baselineContract") != "InstallerResearchBaselineV1":
        raise StateError("research clock requires an accepted InstallerResearchBaselineV1")
    if baseline.get("accepted") is not True:
        raise StateError("research clock requires an accepted reproducible baseline")
    if baseline.get("runId") != context.run_id:
        raise StateError("accepted baseline belongs to another RunId")
    if baseline.get("sourceCommit") != session_init.get("sourceCommit"):
        raise StateError("accepted baseline sourceCommit differs from session initialization")
    if not paths.baseline.is_file():
        raise StateError("accepted baseline.json must exist before the run-phase doctor can arm research")
    if canonical_json_bytes(load_json_object(paths.baseline)) != canonical_json_bytes(baseline):
        raise StateError("baseline argument differs from the accepted baseline.json")
    if (
        doctor_report.get("doctorContract") != "InstallerSizeDoctorV1"
        or doctor_report.get("phase") != "run"
        or doctor_report.get("runId") != context.run_id
        or doctor_report.get("ok") is not True
    ):
        raise StateError("research clock requires a passing unarmed run-phase doctor report")
    expected_evidence = preflight.get("evidenceHashes")
    if doctor_report.get("evidenceHashes") != expected_evidence:
        raise StateError("run-phase doctor evidence differs from accepted preflight")
    source = git_snapshot(context.repo_root)
    if source["dirtyEntries"]:
        raise StateError("research clock cannot start from a dirty worktree")
    if source["sourceCommit"] != session_init.get("sourceCommit"):
        raise StateError("source commit changed between session initialization and research arming")
    started = now or utc_now()
    deadline = started + timedelta(seconds=int(context.duration_seconds or 0))
    bindings = _expected_manifest_bindings(context)
    if any(value in (None, "") for value in bindings.values()):
        raise StateError("research clock bindings are incomplete")
    manifest = {
        "runContract": RUN_MANIFEST_CONTRACT,
        "schemaVersion": STATE_SCHEMA_VERSION,
        "profile": "installer-size",
        "campaign": context.config.get("campaign"),
        "runId": context.run_id,
        "createdAtUtc": session_init.get("createdAtUtc"),
        "durationSeconds": context.duration_seconds,
        "researchStartedAtUtc": format_utc(started),
        "researchDeadlineUtc": format_utc(deadline),
        "sourceCommit": source["sourceCommit"],
        "sourceBranch": source["sourceBranch"],
        "referenceCompression": context.config.get("referenceCompression"),
        "protectedInputHashes": session_init.get("protectedInputHashes"),
        "bindings": bindings,
    }
    validate_manifest(context, manifest)
    store_immutable_json(paths.manifest, manifest)
    progress.update(
        {
            "phase": "run",
            "baselineReplicasAccepted": 2,
            "researchStartedAtUtc": format_utc(started),
            "researchDeadlineUtc": format_utc(deadline),
            "updatedAtUtc": format_utc(started),
        }
    )
    write_json_atomic(paths.progress, progress)
    append_ledger(
        paths.ledger,
        event="research_clock_started",
        payload={
            "researchStartedAtUtc": progress["researchStartedAtUtc"],
            "researchDeadlineUtc": progress["researchDeadlineUtc"],
            "durationSeconds": context.duration_seconds,
            "baselineSha256": file_sha256(paths.baseline),
            "runManifestSha256": file_sha256(paths.manifest),
        },
        now=started,
    )
    return progress


def remaining_seconds(context: ProfileContext, *, now: datetime | None = None) -> int | None:
    progress = load_progress(context)
    deadline = progress.get("researchDeadlineUtc")
    if not deadline:
        return None
    remaining = parse_utc(deadline, field="researchDeadlineUtc") - (now or utc_now())
    seconds = remaining.total_seconds()
    return 0 if seconds <= 0 else math.ceil(seconds)


def accepted_baseline_replica_packet_id(
    context: ProfileContext,
    index: int,
) -> str | None:
    paths = paths_for(context)
    replica_path = paths.baseline_replica(index)
    if not replica_path.is_file():
        return None
    _validate_started_packet_hashes(context)
    matches: list[str] = []
    for row in read_ledger(paths.ledger):
        if row.get("event") != "packet_completed":
            continue
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("decision") != "baseline_accept"
            or payload.get("resultSha256") != file_sha256(replica_path)
        ):
            continue
        packet_id = safe_packet_id(payload.get("packetId"))
        packet = load_json_object(paths.packet(packet_id))
        if (
            packet.get("action", {}).get("kind") == "baseline-replica"
            and packet.get("action", {}).get("replica") == index
        ):
            matches.append(packet_id)
    if len(matches) > 1:
        raise StateError("baseline replica has multiple accepted packet authorities")
    return matches[0] if matches else None


def effective_finalization_reserve(
    context: ProfileContext,
    *,
    progress: dict[str, Any] | None = None,
) -> int:
    current = progress or load_progress(context)
    finalization = context.config.get("finalization")
    if not isinstance(finalization, dict):
        raise StateError("finalization policy is missing")
    minimum = finalization.get("minimumReserveSeconds")
    multiplier = finalization.get("ewmaMultiplier")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum <= 0
        or isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or float(multiplier) < 1.0
    ):
        raise StateError("finalization reserve policy is invalid")
    learning = current.get("laneLearning")
    ewmas: list[float] = []
    if isinstance(learning, dict):
        for lane in learning.values():
            if isinstance(lane, dict):
                value = lane.get("durationEwmaSeconds")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    ewmas.append(float(value))
    learned = math.ceil(max(ewmas, default=0.0) * float(multiplier))
    return max(minimum, learned)


def validate_packet(packet: dict[str, Any], *, run_id: str) -> None:
    if packet.get("packetContract") != PACKET_CONTRACT:
        raise StateError("pending packet contract mismatch")
    if packet.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise StateError("pending packet schema mismatch")
    if packet.get("runId") != run_id:
        raise StateError("pending packet RunId mismatch")
    safe_packet_id(packet.get("packetId"))
    safe_lane_id(packet.get("lane"))
    if not isinstance(packet.get("exploration", False), bool):
        raise StateError("packet exploration must be a boolean")
    hypothesis = packet.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise StateError("pending packet hypothesis must be an object")
    for field in ("statement", "mechanism", "expectedReductionBytes", "risk", "rollback"):
        if field not in hypothesis:
            raise StateError(f"pending packet hypothesis is missing {field}")
    for field in ("statement", "mechanism", "rollback"):
        if not isinstance(hypothesis.get(field), str) or not hypothesis[field].strip():
            raise StateError(f"pending packet hypothesis {field} must be a non-empty string")
    expected_reduction = hypothesis.get("expectedReductionBytes")
    if (
        isinstance(expected_reduction, bool)
        or not isinstance(expected_reduction, int)
        or expected_reduction < 0
    ):
        raise StateError("pending packet hypothesis expectedReductionBytes must be non-negative")
    if hypothesis.get("risk") not in {"low", "medium", "high"}:
        raise StateError("pending packet hypothesis risk must be low, medium, or high")
    source_commit = str(packet.get("sourceCommit") or "")
    if not (
        len(source_commit) in (40, 64)
        and source_commit == source_commit.casefold()
        and all(character in "0123456789abcdef" for character in source_commit)
    ):
        raise StateError("pending packet sourceCommit must be a full lowercase Git object id")
    action = packet.get("action")
    if not isinstance(action, dict) or action.get("kind") not in {
        "baseline-replica",
        "measure-candidate",
        "final-replica",
    }:
        raise StateError("pending packet action kind is not allowlisted")
    timeout_seconds = action.get("timeoutSeconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= 14_400
    ):
        raise StateError("pending packet timeoutSeconds must be between 30 and 14400")
    if action.get("kind") in {"baseline-replica", "final-replica"}:
        if action.get("replica") not in (1, 2):
            raise StateError(f"{action.get('kind')} action requires replica 1 or 2")
    if action.get("kind") == "baseline-replica":
        replica = int(action["replica"])
        expected_packet_id = f"baseline-{replica}"
        if packet.get("packetId") != expected_packet_id:
            raise StateError(
                "baseline-replica packetId must match its canonical "
                f"replica id {expected_packet_id}"
            )
        if packet.get("lane") != "baseline":
            raise StateError("baseline-replica packet lane must be baseline")
    if action.get("kind") == "measure-candidate":
        parent_champion_id = packet.get("parentChampionId")
        if not isinstance(parent_champion_id, str) or not parent_champion_id.strip():
            raise StateError("measure-candidate packet parentChampionId is required")
        if action.get("comparisonKind") not in {"payload", "compression"}:
            raise StateError("measure-candidate action comparisonKind must be payload or compression")
        if action.get("compression") not in {"bzip2", "zlib", "lzma"}:
            raise StateError("measure-candidate action compression is not allowlisted")
        if action.get("comparisonKind") == "payload" and action.get("compression") != "bzip2":
            raise StateError("payload experiments must retain explicit bzip2 compression")
        if action.get("comparisonKind") == "payload":
            parent_tree_oid = str(packet.get("parentSourceTreeOid") or "")
            if not (
                len(parent_tree_oid) in (40, 64)
                and parent_tree_oid == parent_tree_oid.casefold()
                and all(
                    character in "0123456789abcdef"
                    for character in parent_tree_oid
                )
            ):
                raise StateError(
                    "payload candidate requires a lowercase parentSourceTreeOid"
                )
        if action.get("comparisonKind") == "compression":
            tree_sha = str(action.get("payloadTreeSha256") or "")
            if len(tree_sha) != 64 or any(
                character not in "0123456789abcdef" for character in tree_sha
            ):
                raise StateError("compression action requires a lowercase payloadTreeSha256")
    elif "comparisonKind" in action:
        raise StateError("comparisonKind is valid only for measure-candidate actions")
    if action.get("kind") == "final-replica":
        champion_sha = str(action.get("championSha256") or "")
        if len(champion_sha) != 64 or any(
            character not in "0123456789abcdef" for character in champion_sha
        ):
            raise StateError("final-replica action requires a lowercase championSha256")
        champion_tree_oid = str(action.get("championSourceTreeOid") or "")
        if not (
            len(champion_tree_oid) in (40, 64)
            and champion_tree_oid == champion_tree_oid.casefold()
            and all(
                character in "0123456789abcdef"
                for character in champion_tree_oid
            )
        ):
            raise StateError(
                "final-replica action requires a lowercase championSourceTreeOid"
            )


def set_pending_packet(context: ProfileContext, packet: dict[str, Any]) -> Path:
    paths = paths_for(context)
    validate_packet(packet, run_id=str(context.run_id))
    if paths.pending_packet.exists():
        raise StateError("a pending packet already exists")
    if paths.abandoned_packet(str(packet["packetId"])).exists():
        raise StateError("an abandoned packetId cannot be reused")
    store_immutable_json(paths.packet(str(packet["packetId"])), packet)
    write_json_atomic(paths.pending_packet, packet)
    return paths.pending_packet


def clear_pending_packet(paths: RunPaths, *, expected_packet_id: str) -> None:
    packet = load_json_object(paths.pending_packet)
    if packet.get("packetId") != expected_packet_id:
        raise StateError("pending packet changed during dispatch")
    paths.pending_packet.unlink()


def _packet_result_path_from_packet(paths: RunPaths, packet: dict[str, Any]) -> Path:
    relative = str(packet.get("action", {}).get("resultRelativePath") or "").replace(
        "\\", "/"
    )
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise StateError("pending packet resultRelativePath is unsafe")
    result = paths.root.joinpath(*relative.split("/")).resolve()
    if not result.is_relative_to(paths.root.resolve()):
        raise StateError("pending packet resultRelativePath escaped the RunId root")
    return result


def pending_packet_requires_resume(context: ProfileContext) -> bool:
    """Return whether pending state contains evidence of an earlier dispatch."""

    paths = paths_for(context)
    progress = load_progress(context)
    if progress.get("activePacketId"):
        return True
    if not paths.pending_packet.is_file():
        return False

    packet = load_json_object(paths.pending_packet)
    validate_packet(packet, run_id=str(context.run_id))
    packet_id = safe_packet_id(packet.get("packetId"))
    result_path = _packet_result_path_from_packet(paths, packet)
    if any(
        path.exists()
        for path in (
            result_path,
            paths.failed_result(packet_id),
            paths.interrupted_result(packet_id),
            paths.interruption(packet_id),
            paths.abandoned_packet(packet_id),
        )
    ):
        return True

    if paths.last_run.is_file():
        last_run = load_json_object(paths.last_run)
        if (
            last_run.get("runId") == context.run_id
            and last_run.get("packetId") == packet_id
        ):
            return True

    lifecycle_events = {
        "packet_started",
        "packet_completed",
        "packet_interrupted_on_resume",
        "packet_abandoned_before_dispatch",
    }
    return any(
        row.get("event") in lifecycle_events
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("packetId") == packet_id
        for row in read_ledger(paths.ledger)
    )


def _validate_interruption_tombstone(
    context: ProfileContext,
    packet: dict[str, Any],
    tombstone: dict[str, Any],
) -> None:
    paths = paths_for(context)
    packet_id = safe_packet_id(packet.get("packetId"))
    if set(tombstone) != {
        "interruptionContract",
        "schemaVersion",
        "runId",
        "packetId",
        "packetSha256",
        "interruptedAtUtc",
        "interruptedResult",
    }:
        raise StateError("packet interruption tombstone shape is invalid")
    if (
        tombstone.get("interruptionContract")
        != "InstallerSizePacketInterruptionV1"
        or tombstone.get("schemaVersion") != STATE_SCHEMA_VERSION
        or tombstone.get("runId") != context.run_id
        or tombstone.get("packetId") != packet_id
        or tombstone.get("packetSha256") != file_sha256(paths.packet(packet_id))
    ):
        raise StateError("packet interruption tombstone binding is invalid")
    parse_utc(tombstone.get("interruptedAtUtc"), field="interruptedAtUtc")
    interrupted_result = tombstone.get("interruptedResult")
    quarantine = paths.interrupted_result(packet_id)
    if interrupted_result is None:
        if quarantine.exists():
            raise StateError("unbound interrupted result quarantine exists")
    elif (
        not isinstance(interrupted_result, dict)
        or set(interrupted_result) != {"relativePath", "sha256"}
        or interrupted_result.get("relativePath")
        != quarantine.relative_to(paths.root).as_posix()
        or not quarantine.is_file()
        or interrupted_result.get("sha256") != file_sha256(quarantine)
    ):
        raise StateError("interrupted result quarantine binding is invalid")


def _interruption_last_run(tombstone: dict[str, Any]) -> dict[str, Any]:
    return {
        "lastRunContract": "InstallerSizeLastRunV1",
        "schemaVersion": STATE_SCHEMA_VERSION,
        "runId": tombstone["runId"],
        "packetId": tombstone["packetId"],
        "decision": "crash",
        "dispatchError": "interrupted_dispatch_recovered_on_resume",
        "recoveredAtUtc": tombstone["interruptedAtUtc"],
        "packetSha256": tombstone["packetSha256"],
        "interruptedResult": tombstone["interruptedResult"],
    }


def _interruption_ledger_payload(
    tombstone: dict[str, Any],
    *,
    last_run_sha256: str,
) -> dict[str, Any]:
    return {
        "packetId": tombstone["packetId"],
        "packetSha256": tombstone["packetSha256"],
        "interruptedResult": tombstone["interruptedResult"],
        "tombstoneSha256": None,
        "lastRunSha256": last_run_sha256,
    }


def _validate_or_append_interruption_ledger(
    context: ProfileContext,
    tombstone: dict[str, Any],
    *,
    now: datetime,
) -> None:
    paths = paths_for(context)
    payload = _interruption_ledger_payload(
        tombstone,
        last_run_sha256=file_sha256(paths.last_run),
    )
    payload["tombstoneSha256"] = file_sha256(
        paths.interruption(str(tombstone["packetId"]))
    )
    events = [
        row
        for row in read_ledger(paths.ledger)
        if row.get("event") == "packet_interrupted_on_resume"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("packetId") == tombstone["packetId"]
    ]
    if len(events) > 1:
        raise StateError("packet interruption has duplicate ledger entries")
    if events:
        if events[0].get("payload") != payload:
            raise StateError("packet interruption ledger binding is invalid")
    else:
        append_ledger(
            paths.ledger,
            event="packet_interrupted_on_resume",
            payload=payload,
            now=now,
        )


def _interrupt_active_packet(
    context: ProfileContext,
    *,
    progress: dict[str, Any],
    packet_id: str,
    now: datetime,
) -> dict[str, Any]:
    paths = paths_for(context)
    immutable_packet = paths.packet(packet_id)
    if not immutable_packet.is_file():
        raise StateError("active packet has no immutable packet record")
    packet = load_json_object(immutable_packet)
    validate_packet(packet, run_id=str(context.run_id))
    if paths.pending_packet.is_file():
        pending = load_json_object(paths.pending_packet)
        if pending.get("packetId") != packet_id:
            raise StateError("active and pending packet identities differ")
        if canonical_json_bytes(pending) != canonical_json_bytes(packet):
            raise StateError("active pending packet differs from its immutable record")
    active_result = _packet_result_path_from_packet(paths, packet)
    quarantine = paths.interrupted_result(packet_id)
    failed_result = paths.failed_result(packet_id)
    if active_result.is_file() and failed_result.exists():
        raise StateError("active packet has both live and failed result evidence")
    if failed_result.is_file():
        if quarantine.exists():
            raise StateError("active packet has duplicate result quarantines")
        os.replace(failed_result, quarantine)
    if active_result.is_file():
        if quarantine.exists():
            raise StateError("active packet has both live and quarantined result evidence")
        result_sha = file_sha256(active_result)
        os.replace(active_result, quarantine)
        if file_sha256(quarantine) != result_sha:
            raise StateError("interrupted result quarantine hash mismatch")
    interrupted_result = (
        {
            "relativePath": quarantine.relative_to(paths.root).as_posix(),
            "sha256": file_sha256(quarantine),
        }
        if quarantine.is_file()
        else None
    )
    tombstone_path = paths.interruption(packet_id)
    if tombstone_path.is_file():
        tombstone = load_json_object(tombstone_path)
    else:
        tombstone = {
            "interruptionContract": "InstallerSizePacketInterruptionV1",
            "schemaVersion": STATE_SCHEMA_VERSION,
            "runId": context.run_id,
            "packetId": packet_id,
            "packetSha256": file_sha256(immutable_packet),
            "interruptedAtUtc": format_utc(now),
            "interruptedResult": interrupted_result,
        }
        store_immutable_json(tombstone_path, tombstone)
    _validate_interruption_tombstone(context, packet, tombstone)
    write_json_atomic(paths.last_run, _interruption_last_run(tombstone))
    _validate_or_append_interruption_ledger(context, tombstone, now=now)
    progress["activePacketId"] = None
    interrupted = progress.setdefault("interruptedPacketIds", [])
    if packet_id not in interrupted:
        interrupted.append(packet_id)
    progress["updatedAtUtc"] = format_utc(now)
    write_json_atomic(paths.progress, progress)
    if paths.pending_packet.is_file():
        clear_pending_packet(paths, expected_packet_id=packet_id)
    return progress


def _reconcile_interrupted_pending(
    context: ProfileContext,
    *,
    progress: dict[str, Any],
) -> bool:
    paths = paths_for(context)
    if not paths.pending_packet.is_file():
        return False
    packet = load_json_object(paths.pending_packet)
    packet_id = safe_packet_id(packet.get("packetId"))
    tombstone_path = paths.interruption(packet_id)
    if not tombstone_path.is_file():
        return False
    if progress.get("activePacketId") is not None:
        return False
    tombstone = load_json_object(tombstone_path)
    _validate_interruption_tombstone(context, packet, tombstone)
    expected_last_run = _interruption_last_run(tombstone)
    if canonical_json_bytes(load_json_object(paths.last_run)) != canonical_json_bytes(
        expected_last_run
    ):
        raise StateError("packet interruption last-run binding is invalid")
    _validate_or_append_interruption_ledger(
        context,
        tombstone,
        now=parse_utc(tombstone["interruptedAtUtc"], field="interruptedAtUtc"),
    )
    if packet_id not in progress.get("interruptedPacketIds", []):
        raise StateError("packet interruption is absent from durable progress")
    clear_pending_packet(paths, expected_packet_id=packet_id)
    return True


def packet_completed_ledger_payload(
    *,
    packet_id: str,
    last_run: dict[str, Any],
    last_run_sha256: str,
) -> dict[str, Any]:
    return {
        "packetId": packet_id,
        "decision": last_run.get("decision"),
        "exitCode": last_run.get("exitCode"),
        "resultSha256": last_run.get("resultSha256"),
        "failedResult": last_run.get("failedResult"),
        "gateEvidenceSha256": last_run.get("gateEvidenceSha256"),
        "finalGateEvidenceSha256": last_run.get("finalGateEvidenceSha256"),
        "finalFullSuiteSha256": last_run.get("finalFullSuiteSha256"),
        "finalTimingSha256": last_run.get("finalTimingSha256"),
        "learningUpdate": last_run.get("learningUpdate"),
        "lastRunSha256": last_run_sha256,
    }


def _reconcile_completed_pending(
    context: ProfileContext,
    *,
    now: datetime,
) -> bool:
    """Finish the commit tail after progress was durably written."""

    paths = paths_for(context)
    if not paths.pending_packet.is_file():
        return False
    progress = load_progress(context)
    if progress.get("activePacketId"):
        return False
    packet = load_json_object(paths.pending_packet)
    validate_packet(packet, run_id=str(context.run_id))
    packet_id = safe_packet_id(packet.get("packetId"))
    immutable_packet = paths.packet(packet_id)
    if canonical_json_bytes(packet) != canonical_json_bytes(
        load_json_object(immutable_packet)
    ):
        raise StateError("completed pending packet differs from its immutable record")
    result_path = _packet_result_path_from_packet(paths, packet)
    if not paths.last_run.is_file():
        if result_path.is_file():
            raise StateError("completed pending packet has no last-run commit record")
        return False
    last_run = load_json_object(paths.last_run)
    if (
        last_run.get("lastRunContract") != "InstallerSizeLastRunV1"
        or last_run.get("schemaVersion") != STATE_SCHEMA_VERSION
        or last_run.get("runId") != context.run_id
        or last_run.get("packetId") != packet_id
        or last_run.get("packetSha256") != file_sha256(immutable_packet)
        or last_run.get("decision") not in TERMINAL_DECISIONS
    ):
        raise StateError("completed pending packet has an invalid last-run commit record")
    result_sha = last_run.get("resultSha256")
    decision = str(last_run.get("decision"))
    if not result_path.is_file():
        if decision != "crash" or result_sha is not None:
            raise StateError("completed pending packet is missing required result evidence")
        failed_result = last_run.get("failedResult")
        failed_path = paths.failed_result(packet_id)
        if failed_result is None:
            if failed_path.exists():
                raise StateError("unbound failed packet result quarantine exists")
        elif (
            not isinstance(failed_result, dict)
            or set(failed_result) != {"relativePath", "sha256"}
            or failed_result.get("relativePath")
            != failed_path.relative_to(paths.root).as_posix()
            or not failed_path.is_file()
            or failed_result.get("sha256") != file_sha256(failed_path)
        ):
            raise StateError("failed packet result quarantine binding is invalid")
    elif result_sha is None:
        raise StateError("live completed packet result lacks an attested hash")
    if decision in {
        "baseline_accept",
        "final_replica_accept",
        "keep",
        "discard",
        "measure_only",
    }:
        if not result_path.is_file() or result_sha != file_sha256(result_path):
            raise StateError("completed pending result differs from last-run evidence")
    elif result_sha is not None and result_sha != file_sha256(result_path):
        raise StateError("completed pending result hash is stale")
    if decision == "keep":
        result = load_json_object(result_path)
        if progress.get("championId") != packet_id or result.get("decision") != "keep":
            raise StateError("completed keep progress is not bound to its packet result")
        if paths.champion.is_file():
            existing_champion = load_json_object(paths.champion)
            if canonical_json_bytes(existing_champion) != canonical_json_bytes(result):
                prior_id = last_run.get("priorChampionId")
                prior_sha = last_run.get("priorChampionSha256")
                if (
                    not isinstance(prior_id, str)
                    or not prior_id
                    or not isinstance(prior_sha, str)
                    or file_sha256(paths.champion) != prior_sha
                ):
                    raise StateError(
                        "completed keep champion copy differs from both prior and new result"
                    )
                write_json_atomic(paths.champion, result)
        else:
            if last_run.get("priorChampionId") is not None:
                raise StateError("completed keep lost its prior champion artifact")
            write_json_atomic(paths.champion, result)
    if decision == "final_replica_accept" and packet_id not in progress.get(
        "finalReplicaPacketIds", []
    ):
        raise StateError("completed final replica is absent from durable progress")
    if decision == "baseline_accept":
        replica = packet.get("action", {}).get("replica")
        accepted_count = sum(
            1 for index in (1, 2) if paths.baseline_replica(index).is_file()
        )
        if replica not in (1, 2) or progress.get("baselineReplicasAccepted") != accepted_count:
            raise StateError("completed baseline replica is absent from durable progress")
    last_run_sha = file_sha256(paths.last_run)
    expected_payload = packet_completed_ledger_payload(
        packet_id=packet_id,
        last_run=last_run,
        last_run_sha256=last_run_sha,
    )
    completed = [
        row
        for row in read_ledger(paths.ledger)
        if row.get("event") == "packet_completed"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("packetId") == packet_id
    ]
    if len(completed) > 1:
        raise StateError("packet completion has duplicate ledger entries")
    if completed:
        if completed[0].get("payload") != expected_payload:
            raise StateError("packet completion ledger binding is invalid")
    else:
        append_ledger(
            paths.ledger,
            event="packet_completed",
            payload=expected_payload,
            now=now,
        )
    clear_pending_packet(paths, expected_packet_id=packet_id)
    return True


def _validate_abandon_tombstone(
    context: ProfileContext,
    packet: dict[str, Any],
    tombstone: dict[str, Any],
) -> None:
    paths = paths_for(context)
    packet_id = safe_packet_id(packet.get("packetId"))
    expected_keys = {
        "abandonContract",
        "schemaVersion",
        "runId",
        "packetId",
        "packetSha256",
        "reason",
        "abandonedAtUtc",
    }
    if set(tombstone) != expected_keys:
        raise StateError("pending packet abandonment tombstone shape is invalid")
    if (
        tombstone.get("abandonContract") != "InstallerSizePacketAbandonmentV1"
        or tombstone.get("schemaVersion") != STATE_SCHEMA_VERSION
        or tombstone.get("runId") != context.run_id
        or tombstone.get("packetId") != packet_id
        or tombstone.get("reason") not in ABANDON_REASONS
    ):
        raise StateError("pending packet abandonment tombstone binding is invalid")
    parse_utc(tombstone.get("abandonedAtUtc"), field="abandonedAtUtc")
    if tombstone.get("packetSha256") != file_sha256(paths.packet(packet_id)):
        raise StateError("abandonment tombstone packet hash is stale")


def _recover_abandoned_pending(
    context: ProfileContext,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    paths = paths_for(context)
    if not paths.pending_packet.is_file():
        return None
    packet = load_json_object(paths.pending_packet)
    validate_packet(packet, run_id=str(context.run_id))
    packet_id = safe_packet_id(packet["packetId"])
    tombstone_path = paths.abandoned_packet(packet_id)
    if not tombstone_path.is_file():
        return None
    immutable_packet = paths.packet(packet_id)
    if canonical_json_bytes(packet) != canonical_json_bytes(
        load_json_object(immutable_packet)
    ):
        raise StateError("abandoned pending packet differs from its immutable record")
    tombstone = load_json_object(tombstone_path)
    _validate_abandon_tombstone(context, packet, tombstone)
    if _packet_result_path_from_packet(paths, packet).exists():
        raise StateError("a packet with result evidence cannot be abandoned")
    rows = read_ledger(paths.ledger)
    events = [
        row
        for row in rows
        if row.get("event") == "packet_abandoned_before_dispatch"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("packetId") == packet_id
    ]
    expected_payload = {
        "packetId": packet_id,
        "packetSha256": file_sha256(immutable_packet),
        "reason": tombstone["reason"],
        "tombstoneSha256": file_sha256(tombstone_path),
    }
    if len(events) > 1:
        raise StateError("packet abandonment has duplicate ledger entries")
    if events:
        if events[0].get("payload") != expected_payload:
            raise StateError("packet abandonment ledger binding is invalid")
    else:
        append_ledger(
            paths.ledger,
            event="packet_abandoned_before_dispatch",
            payload=expected_payload,
            now=now,
        )
    progress = load_progress(context)
    if progress.get("activePacketId"):
        raise StateError("an active packet cannot be abandoned")
    abandoned = progress.setdefault("abandonedPacketIds", [])
    if packet_id not in abandoned:
        abandoned.append(packet_id)
        progress["updatedAtUtc"] = format_utc(now)
        write_json_atomic(paths.progress, progress)
    clear_pending_packet(paths, expected_packet_id=packet_id)
    return tombstone


def abandon_pending_packet(
    context: ProfileContext,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if reason not in ABANDON_REASONS:
        raise StateError("pending packet abandonment reason is not allowlisted")
    paths = paths_for(context)
    with acquire_dispatch_lock(context):
        progress = load_progress(context)
        if progress.get("activePacketId"):
            raise StateError("an active packet cannot be abandoned")
        if not paths.pending_packet.is_file():
            raise StateError("abandon-pending requires one pending packet")
        packet = load_json_object(paths.pending_packet)
        validate_packet(packet, run_id=str(context.run_id))
        packet_id = safe_packet_id(packet["packetId"])
        immutable_packet = paths.packet(packet_id)
        if canonical_json_bytes(packet) != canonical_json_bytes(
            load_json_object(immutable_packet)
        ):
            raise StateError("pending packet differs from its immutable record")
        if _packet_result_path_from_packet(paths, packet).exists():
            raise StateError("a packet with result evidence cannot be abandoned")
        abandoned_at = now or utc_now()
        tombstone = {
            "abandonContract": "InstallerSizePacketAbandonmentV1",
            "schemaVersion": STATE_SCHEMA_VERSION,
            "runId": context.run_id,
            "packetId": packet_id,
            "packetSha256": file_sha256(immutable_packet),
            "reason": reason,
            "abandonedAtUtc": format_utc(abandoned_at),
        }
        store_immutable_json(paths.abandoned_packet(packet_id), tombstone)
        recovered = _recover_abandoned_pending(
            context,
            now=abandoned_at,
        )
        if recovered is None:
            raise StateError("pending packet abandonment did not complete")
        return recovered


def summarize(context: ProfileContext, *, now: datetime | None = None) -> dict[str, Any]:
    paths = paths_for(context)
    binding = load_manifest(context) if paths.manifest.is_file() else load_session_init(context)
    progress = load_progress(context)
    ledger = read_ledger(paths.ledger)
    learning = progress.get("laneLearning", {})
    locked_lanes = sorted(
        lane_id
        for lane_id, lane_state in learning.items()
        if isinstance(lane_state, dict) and lane_state.get("locked") is True
    )
    return {
        "profile": "installer-size",
        "runId": context.run_id,
        "campaign": binding.get("campaign"),
        "phase": progress.get("phase"),
        "researchStartedAtUtc": progress.get("researchStartedAtUtc"),
        "researchDeadlineUtc": progress.get("researchDeadlineUtc"),
        "remainingSeconds": remaining_seconds(context, now=now),
        "baselineReplicasAccepted": progress.get("baselineReplicasAccepted", 0),
        "packetSequence": progress.get("packetSequence", 0),
        "activePacketId": progress.get("activePacketId"),
        "interruptedPacketIds": progress.get("interruptedPacketIds", []),
        "abandonedPacketIds": progress.get("abandonedPacketIds", []),
        "championId": progress.get("championId"),
        "finalReplicaPacketIds": progress.get("finalReplicaPacketIds", []),
        "ledgerEntries": len(ledger),
        "pendingPacket": paths.pending_packet.is_file(),
        "validDiscardsWithoutEvidence": progress.get("validDiscardsWithoutEvidence", 0),
        "laneLearning": learning,
        "lockedLanes": locked_lanes,
        "effectiveFinalizationReserveSeconds": effective_finalization_reserve(
            context,
            progress=progress,
        ),
        "preflightAccepted": (
            load_json_object(paths.preflight).get("accepted") is True
            if paths.preflight.is_file()
            else False
        ),
    }
