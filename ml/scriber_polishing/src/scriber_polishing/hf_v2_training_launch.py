"""Single-shot, permanently claimed launch gate for Scriber V2 training."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .hf_launch_gate import HfLaunchGateError, require_no_active_hf_jobs
from .hf_v2_training_job import (
    V2_JOB_FLAVOR,
    V2_JOB_IMAGE,
    V2_JOB_NAME,
    V2_LAUNCH_CLAIM_PLACEHOLDER,
    V2_OUTPUT_REMOTE_URI,
    V2_PACKET_REMOTE_URI,
    V2TrainingJobError,
    _canonical_bytes,
    _launch_contract,
    _safe_relative,
    _sha256,
    verify_v2_training_packet,
)

V2_CLAIM_REPOSITORY_ID = "Buttermilk03/scriber-polishing-launch-claims"
V2_CLAIM_REPOSITORY_TYPE = "dataset"
V2_CLAIM_PATH = "claims/v2-hardcase-replay/attempt12-scriber-v2-hardcase-replay-v1.json"
V2_CONTRACT_HASH_PLACEHOLDER = "__CONTRACT_SHA256_BOUND_BY_LAUNCHER__"
V2_PACKET_REMOTE_RELATIVE = "attempt12/packets/scriber-v2-hardcase-replay-v1"

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_DETACHED_JOB_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
_MISSING = object()


class V2LaunchError(RuntimeError):
    """The V2 control plane could not prove an exclusive single submission."""


class V2LaunchControlPlane(Protocol):
    def authoritative_jobs(self) -> list[dict[str, object]]: ...

    def packet_entries(self) -> list[dict[str, object]]: ...

    def packet_metadata(self, relative: str) -> bytes: ...

    def output_prefix_entries(self) -> list[dict[str, object]]: ...

    def claim_exists(self) -> bool: ...

    def acquire_permanent_claim(self, payload: bytes) -> str: ...

    def submit_detached_once(self, argv: list[str]) -> str: ...

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]: ...


def _load_packet_metadata(packet: Path) -> tuple[dict[str, object], dict[str, object], str, str, str]:
    manifest_payload = (packet / "package-manifest.json").read_bytes()
    inventory_payload = (packet / "tree-inventory.json").read_bytes()
    contract_payload = (packet / "launch-contract.json").read_bytes()
    try:
        manifest = json.loads(manifest_payload)
        contract = json.loads(contract_payload)
    except json.JSONDecodeError as error:
        raise V2LaunchError("local V2 packet metadata is invalid JSON") from error
    if not isinstance(manifest, dict) or not isinstance(contract, dict):
        raise V2LaunchError("local V2 packet metadata must contain objects")
    return manifest, contract, _sha256(manifest_payload), _sha256(inventory_payload), _sha256(contract_payload)


def validate_v2_launch_contract(contract: Mapping[str, object], *, contract_sha256: str) -> dict[str, object]:
    if _HASH.fullmatch(contract_sha256) is None:
        raise V2LaunchError("V2 launch contract SHA-256 is invalid")
    expected = _launch_contract(
        source_git_head=str(contract.get("source_git_head", "")),
        packet_tree_sha256=str(contract.get("packet_tree_sha256", "")),
        manifest_sha256=str(contract.get("manifest_sha256", "")),
        inventory_sha256=str(contract.get("inventory_sha256", "")),
    )
    if dict(contract) != expected or contract.get("active_job_snapshots_required") != 3:
        raise V2LaunchError("V2 launch contract differs from the reviewed single-shot argv")
    return dict(contract)


def bind_v2_launch_argv(contract: Mapping[str, object], *, owner_token: str, contract_sha256: str) -> list[str]:
    checked = validate_v2_launch_contract(contract, contract_sha256=contract_sha256)
    if _OWNER.fullmatch(owner_token) is None:
        raise V2LaunchError("V2 claim owner token is invalid")
    argv = checked.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise V2LaunchError("V2 launch argv is invalid")
    if sum(V2_LAUNCH_CLAIM_PLACEHOLDER in item for item in argv) != 2 or argv.count(V2_CONTRACT_HASH_PLACEHOLDER) != 1:
        raise V2LaunchError("V2 launch placeholders differ")
    bound = [
        item.replace(V2_LAUNCH_CLAIM_PLACEHOLDER, owner_token).replace(V2_CONTRACT_HASH_PLACEHOLDER, contract_sha256)
        for item in argv
    ]
    if any(V2_LAUNCH_CLAIM_PLACEHOLDER in item or V2_CONTRACT_HASH_PLACEHOLDER in item for item in bound):
        raise V2LaunchError("V2 launch placeholder remains after binding")
    return bound


def _labels(row: Mapping[str, object]) -> dict[str, str]:
    raw = row.get("labels", {})
    if isinstance(raw, Mapping):
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()):
            raise V2LaunchError("HF job labels are malformed")
        return dict(raw)
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        result: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                raise V2LaunchError("HF job labels are malformed")
            key, value = item.split("=", 1)
            if not key or key in result:
                raise V2LaunchError("HF job labels are malformed")
            result[key] = value
        return result
    raise V2LaunchError("HF job labels are malformed")


def require_no_prior_v2_job(rows: Sequence[Mapping[str, object]]) -> None:
    for row in rows:
        labels = _labels(row)
        if (
            row.get("name") == V2_JOB_NAME
            or labels.get("campaign") == "v2-hardcase-replay"
            or labels.get("role") == "v2-training"
            or labels.get("output-prefix") == "attempt12/scriber-v2-hardcase-replay-v1"
        ):
            raise V2LaunchError("an authoritative HF snapshot already contains this V2 job")


def validate_remote_packet(entries: Sequence[Mapping[str, object]], *, packet_dir: str | Path) -> None:
    packet = Path(packet_dir).expanduser().resolve()
    expected = {
        path.relative_to(packet).as_posix(): path.stat().st_size
        for path in packet.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    observed: dict[str, int] = {}
    for row in entries:
        raw_path = row.get("path")
        size = row.get("size")
        xet_hash = row.get("xet_hash")
        if (
            row.get("type") != "file"
            or not isinstance(raw_path, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not isinstance(xet_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", xet_hash) is None
        ):
            raise V2LaunchError("remote V2 packet inventory contains an ambiguous entry")
        relative = raw_path
        prefix = V2_PACKET_REMOTE_RELATIVE + "/"
        if relative.startswith(prefix):
            relative = relative[len(prefix) :]
        if not _safe_relative(relative) or relative in observed:
            raise V2LaunchError("remote V2 packet inventory path is unsafe or duplicated")
        observed[relative] = size
    if observed != expected:
        raise V2LaunchError("remote V2 packet inventory differs from the local immutable packet")


def build_v2_launch_claim(
    contract: Mapping[str, object],
    *,
    contract_sha256: str,
    claimed_at_utc: datetime,
) -> tuple[bytes, str]:
    checked = validate_v2_launch_contract(contract, contract_sha256=contract_sha256)
    if claimed_at_utc.tzinfo is None:
        raise V2LaunchError("V2 claim time must be timezone-aware")
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_launch_claim",
        "campaign": "v2-hardcase-replay",
        "attempt": "attempt12",
        "source_git_head": checked["source_git_head"],
        "packet_remote_uri": V2_PACKET_REMOTE_URI,
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "manifest_sha256": checked["manifest_sha256"],
        "launch_contract_sha256": contract_sha256,
        "remote_result_uri": V2_OUTPUT_REMOTE_URI,
        "claimed_at_utc": claimed_at_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "single_shot": True,
        "auto_retry_allowed": False,
    }
    owner = hashlib.sha256(_canonical_bytes(claim)).hexdigest()
    return _canonical_bytes({**claim, "owner_token": owner}), owner


def _value(row: object, name: str, default: object = None) -> object:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _canonical_volume_source(value: str) -> str:
    if not value.startswith("hf://buckets/Buttermilk03/") or any(
        marker in value for marker in ("\\", "?", "#", "//..", "/../", "/./")
    ):
        raise V2LaunchError("submitted V2 job volume source is invalid")
    if value.endswith("/") or not _safe_relative(value.removeprefix("hf://buckets/Buttermilk03/")):
        raise V2LaunchError("submitted V2 job volume source is invalid")
    return value


def _normalize_volumes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise V2LaunchError("submitted V2 job volumes are malformed")
    normalized: list[str] = []
    for raw_volume in value:
        if isinstance(raw_volume, str):
            source_and_mount, mode_separator, mode = raw_volume.rpartition(":")
            source, mount_separator, mount_path = source_and_mount.rpartition(":")
            if not mode_separator or not mount_separator or mode not in {"ro", "rw"} or not mount_path.startswith("/"):
                raise V2LaunchError("submitted V2 job volume is invalid")
            normalized.append(f"{_canonical_volume_source(source)}:{mount_path}:{mode}")
            continue
        volume_type = _value(raw_volume, "type")
        source = _value(raw_volume, "source")
        mount_path = _value(raw_volume, "mount_path")
        path = _value(raw_volume, "path", _MISSING)
        read_only = _value(raw_volume, "read_only")
        if (
            volume_type != "bucket"
            or not isinstance(source, str)
            or not isinstance(mount_path, str)
            or not mount_path.startswith("/")
            or not isinstance(read_only, bool)
            or path is _MISSING
            or (path is not None and not isinstance(path, str))
        ):
            raise V2LaunchError("submitted V2 job volume is incomplete")
        source_uri = source if source.startswith("hf://buckets/") else f"hf://buckets/{source}"
        if path:
            if not _safe_relative(path):
                raise V2LaunchError("submitted V2 job volume path is invalid")
            source_uri += f"/{path}"
        normalized.append(f"{_canonical_volume_source(source_uri)}:{mount_path}:{'ro' if read_only else 'rw'}")
    return normalized


def _parse_detached_v2_job_id(payload: str) -> str:
    if not isinstance(payload, str):
        raise V2LaunchError("V2 submission output is not text")
    matches = sorted(set(_DETACHED_JOB_ID.findall(payload.lower())))
    if len(matches) != 1:
        raise V2LaunchError("V2 submission returned an ambiguous job ID; claim retained and retry forbidden")
    return matches[0]


def parse_hf_cli_single_job_inspection(payload: str | bytes) -> dict[str, object]:
    """Parse the HF CLI 1.9 inspect shape: a JSON list with exactly one job."""

    try:
        text = payload.decode("utf-8", errors="strict") if isinstance(payload, bytes) else payload
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2LaunchError("submitted V2 CLI inspection is invalid JSON") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise V2LaunchError("submitted V2 CLI inspection must contain exactly one job object")
    return dict(value[0])


def _normalize_inspection(row: object) -> dict[str, object]:
    status = _value(row, "status", {})
    stage = _value(status, "stage", _value(row, "stage", ""))
    owner = _value(row, "owner", {})
    command = _value(row, "command")
    arguments = _value(row, "arguments")
    if (
        not isinstance(command, Sequence)
        or isinstance(command, str | bytes)
        or any(not isinstance(item, str) for item in command)
        or not isinstance(arguments, Sequence)
        or isinstance(arguments, str | bytes)
        or any(not isinstance(item, str) for item in arguments)
    ):
        raise V2LaunchError("submitted V2 job command is malformed")
    labels = _labels(dict(row) if isinstance(row, Mapping) else {"labels": _value(row, "labels")})
    name = labels.get("name")
    if not isinstance(name, str) or not name:
        raise V2LaunchError("submitted V2 job has no exact name label")
    volumes = _value(row, "volumes")
    environment = _value(row, "environment")
    secrets = _value(row, "secrets")
    if environment not in ({}, []) or secrets not in ({}, []):
        raise V2LaunchError("submitted V2 job unexpectedly carries environment or secrets")
    return {
        "id": str(_value(row, "id", "")),
        "name": name,
        "image": str(_value(row, "docker_image", _value(row, "image", ""))),
        "flavor": str(_value(row, "flavor", "")),
        "labels": dict(sorted(labels.items())),
        "volumes": _normalize_volumes(volumes),
        "command": list(command),
        "arguments": list(arguments),
        "environment": {},
        "secrets": {},
        "owner": str(_value(owner, "name", "")),
        "stage": str(stage).upper(),
    }


def validate_submitted_v2_job(
    cli_row: object,
    api_row: object,
    *,
    expected_job_id: str,
    bound_argv: Sequence[str],
) -> dict[str, object]:
    if _JOB_ID.fullmatch(expected_job_id) is None:
        raise V2LaunchError("submitted V2 job ID is invalid")
    cli = _normalize_inspection(cli_row)
    api = _normalize_inspection(api_row)
    if cli != api:
        raise V2LaunchError("HF API and CLI V2 job inspections differ")
    argv = list(bound_argv)
    image_index = argv.index(V2_JOB_IMAGE)
    labels: dict[str, str] = {}
    volumes: list[str] = []
    index = 0
    while index < image_index:
        if argv[index] == "--label":
            key, value = argv[index + 1].split("=", 1)
            labels[key] = value
            index += 2
        elif argv[index] == "-v":
            volumes.append(argv[index + 1])
            index += 2
        else:
            index += 1
    expected = {
        "id": expected_job_id,
        "name": V2_JOB_NAME,
        "image": V2_JOB_IMAGE,
        "flavor": V2_JOB_FLAVOR,
        "labels": dict(sorted(labels.items())),
        "volumes": volumes,
        "command": argv[image_index + 1 :],
        "arguments": [],
        "environment": {},
        "secrets": {},
        "owner": "Buttermilk03",
        "stage": cli["stage"],
    }
    if cli != expected or cli["stage"] not in {"PENDING", "QUEUED", "SCHEDULING", "RUNNING"}:
        raise V2LaunchError("submitted V2 job differs from the exact launch contract")
    return {
        "id": expected_job_id,
        "stage": cli["stage"],
        "exact_identity_verified": True,
        "api_cli_identity_sha256": _sha256(_canonical_bytes(cli)),
        "command_sha256": _sha256(_canonical_bytes(expected["command"])),
    }


def _write_receipt(path: Path, value: object) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(_canonical_bytes(value))
    except FileExistsError as error:
        raise V2LaunchError("V2 launch receipt already exists") from error


def _verify_remote_preconditions(
    control: V2LaunchControlPlane,
    *,
    packet: Path,
    claim_must_exist: bool,
) -> None:
    validate_remote_packet(control.packet_entries(), packet_dir=packet)
    for relative in ("package-manifest.json", "tree-inventory.json", "launch-contract.json"):
        if control.packet_metadata(relative) != (packet / relative).read_bytes():
            raise V2LaunchError("remote V2 packet metadata bytes differ")
    if control.output_prefix_entries():
        raise V2LaunchError("the unique V2 output prefix is already populated")
    claim_exists = control.claim_exists()
    if claim_exists is not claim_must_exist:
        expected = "present" if claim_must_exist else "absent"
        raise V2LaunchError(f"the permanent V2 launch claim is not {expected}")


def launch_v2_training_job(
    control: V2LaunchControlPlane,
    *,
    packet_dir: str | Path,
    receipt_path: str | Path,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Acquire one permanent claim and call detached submission exactly once."""

    packet = Path(packet_dir).expanduser().resolve()
    _manifest, contract, manifest_sha, inventory_sha, contract_sha = _load_packet_metadata(packet)
    checked = validate_v2_launch_contract(contract, contract_sha256=contract_sha)
    try:
        verify_v2_training_packet(
            packet,
            expected_packet_tree_sha256=str(checked["packet_tree_sha256"]),
            expected_manifest_sha256=manifest_sha,
            expected_launch_contract_sha256=contract_sha,
        )
    except V2TrainingJobError as error:
        raise V2LaunchError(f"local V2 packet verification failed: {error}") from error
    if checked["inventory_sha256"] != inventory_sha:
        raise V2LaunchError("local V2 inventory SHA-256 differs from launch contract")

    first = control.authoritative_jobs()
    try:
        require_no_active_hf_jobs(first)
    except HfLaunchGateError as error:
        raise V2LaunchError(str(error)) from error
    require_no_prior_v2_job(first)
    _verify_remote_preconditions(control, packet=packet, claim_must_exist=False)

    second = control.authoritative_jobs()
    try:
        require_no_active_hf_jobs(second)
    except HfLaunchGateError as error:
        raise V2LaunchError(str(error)) from error
    require_no_prior_v2_job(second)
    if _canonical_bytes(first) != _canonical_bytes(second):
        raise V2LaunchError("authoritative HF job inventory changed between the two required snapshots")
    _verify_remote_preconditions(control, packet=packet, claim_must_exist=False)

    claimed_at = now_utc or datetime.now(UTC)
    claim_payload, owner = build_v2_launch_claim(checked, contract_sha256=contract_sha, claimed_at_utc=claimed_at)
    claim_commit = control.acquire_permanent_claim(claim_payload)
    if re.fullmatch(r"[0-9a-f]{40}", claim_commit) is None:
        raise V2LaunchError("permanent V2 claim commit is invalid")
    _verify_remote_preconditions(control, packet=packet, claim_must_exist=True)
    third = control.authoritative_jobs()
    try:
        require_no_active_hf_jobs(third)
    except HfLaunchGateError as error:
        raise V2LaunchError(str(error)) from error
    require_no_prior_v2_job(third)
    if _canonical_bytes(second) != _canonical_bytes(third):
        raise V2LaunchError("authoritative HF job inventory changed after the permanent claim")
    bound_argv = bind_v2_launch_argv(checked, owner_token=owner, contract_sha256=contract_sha)
    try:
        submission = control.submit_detached_once(bound_argv)
    except Exception as error:
        raise V2LaunchError("V2 submission failed or is ambiguous; claim retained and retry forbidden") from error
    job_id = _parse_detached_v2_job_id(submission)
    cli_row, api_row, url = control.inspect_submitted_job(job_id)
    inspection = validate_submitted_v2_job(cli_row, api_row, expected_job_id=job_id, bound_argv=bound_argv)
    if re.fullmatch(rf"https://huggingface\.co/jobs/(?:Buttermilk03/)?{re.escape(job_id)}", url) is None:
        raise V2LaunchError("submitted V2 job URL differs from the inspected job ID; claim retained")
    receipt = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_launch_receipt",
        "claim_repository": V2_CLAIM_REPOSITORY_ID,
        "claim_path": V2_CLAIM_PATH,
        "claim_commit": claim_commit,
        "owner_token": owner,
        "job_id": job_id,
        "job_inspection": {**inspection, "url": url},
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "launch_contract_sha256": contract_sha,
        "remote_result_uri": V2_OUTPUT_REMOTE_URI,
        "submitted_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authoritative_snapshot_count": 3,
        "duplicate_retry_allowed": False,
    }
    _write_receipt(Path(receipt_path), receipt)
    return receipt
