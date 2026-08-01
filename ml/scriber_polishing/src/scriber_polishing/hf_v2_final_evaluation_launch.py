"""Fail-closed, single-shot launcher for the Scriber V2 compact evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .curriculum import canonical_json_bytes
from .hf_launch_gate import HfLaunchGateError, require_no_active_hf_jobs
from .hf_v2_final_evaluation import (
    V2_EVALUATION_ATTEMPT,
    V2_EVALUATION_FLAVOR,
    V2_EVALUATION_IMAGE,
    V2_EVALUATION_JOB_NAME,
    V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI,
    V2_EVALUATION_OUTPUT_REMOTE_URI,
    V2_EVALUATION_PACKET_REMOTE_URI,
    V2FinalEvaluationError,
    _launch_contract,
    _safe_relative,
    _sha256,
    verify_v2_final_evaluation_packet,
)

V2_EVALUATION_CLAIM_REPOSITORY_ID = "Buttermilk03/scriber-polishing-launch-claims"
V2_EVALUATION_CLAIM_REPOSITORY_TYPE = "dataset"
V2_EVALUATION_CLAIM_PATH = (
    f"claims/v2-final-evaluation/{V2_EVALUATION_ATTEMPT}-{V2_EVALUATION_JOB_NAME}.json"
)
V2_EVALUATION_PACKET_REMOTE_RELATIVE = (
    f"{V2_EVALUATION_ATTEMPT}/packets/{V2_EVALUATION_JOB_NAME}"
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_DETACHED_JOB_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
_MISSING = object()
_RECEIPT_RESERVATION = canonical_json_bytes(
    {"schema_version": 1, "kind": "scriber_hf_v2_final_evaluation_launch_receipt_reservation"}
)


class V2FinalEvaluationLaunchError(RuntimeError):
    """The attempt14 control plane could not prove one exclusive submission."""


class V2FinalEvaluationControlPlane(Protocol):
    def authoritative_jobs(self) -> list[dict[str, object]]: ...

    def packet_entries(self) -> list[dict[str, object]]: ...

    def upload_packet_once(self, packet_dir: Path) -> None: ...

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]: ...

    def output_prefix_entries(self) -> list[dict[str, object]]: ...

    def claim_exists(self) -> bool: ...

    def acquire_permanent_claim(self, payload: bytes) -> str: ...

    def submit_detached_once(self, argv: list[str]) -> str: ...

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]: ...


def _load_packet(packet: Path) -> tuple[dict[str, object], str, str, str]:
    try:
        manifest_payload = (packet / "package-manifest.json").read_bytes()
        inventory_payload = (packet / "tree-inventory.json").read_bytes()
        contract_payload = (packet / "launch-contract.json").read_bytes()
        manifest = json.loads(manifest_payload)
        contract = json.loads(contract_payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2FinalEvaluationLaunchError("local attempt14 packet metadata is unreadable") from error
    if not isinstance(manifest, dict) or not isinstance(contract, dict):
        raise V2FinalEvaluationLaunchError("local attempt14 packet metadata must contain objects")
    if manifest_payload != canonical_json_bytes(manifest) or contract_payload != canonical_json_bytes(contract):
        raise V2FinalEvaluationLaunchError("local attempt14 packet metadata is not canonical JSON")
    manifest_sha = _sha256(manifest_payload)
    inventory_sha = _sha256(inventory_payload)
    contract_sha = _sha256(contract_payload)
    expected = _launch_contract(
        packet_tree_sha256=str(contract.get("packet_tree_sha256", "")),
        manifest_sha256=manifest_sha,
        inventory_sha256=inventory_sha,
    )
    if contract != expected:
        raise V2FinalEvaluationLaunchError("attempt14 launch contract differs from the reviewed contract")
    return contract, manifest_sha, inventory_sha, contract_sha


def _local_files(packet: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(packet.rglob("*")):
        if path.is_symlink():
            raise V2FinalEvaluationLaunchError("local attempt14 packet contains a symlink")
        if path.is_file():
            relative = path.relative_to(packet).as_posix()
            if not _safe_relative(relative) or relative in result:
                raise V2FinalEvaluationLaunchError("local attempt14 packet path is unsafe or duplicated")
            result[relative] = path.read_bytes()
    if not result:
        raise V2FinalEvaluationLaunchError("local attempt14 packet is empty")
    return result


def validate_remote_v2_evaluation_packet(
    control: V2FinalEvaluationControlPlane, *, packet_dir: str | Path
) -> None:
    packet = Path(packet_dir).expanduser().resolve()
    local = _local_files(packet)
    local_xet = control.packet_xet_hashes(packet)
    if set(local_xet) != set(local) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in local_xet.values()
    ):
        raise V2FinalEvaluationLaunchError("local attempt14 Xet inventory is incomplete or malformed")
    observed: dict[str, tuple[int, str]] = {}
    for row in control.packet_entries():
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
            raise V2FinalEvaluationLaunchError("remote attempt14 packet inventory is ambiguous")
        relative = raw_path
        prefix = V2_EVALUATION_PACKET_REMOTE_RELATIVE + "/"
        if relative.startswith(prefix):
            relative = relative[len(prefix) :]
        if not _safe_relative(relative) or relative in observed:
            raise V2FinalEvaluationLaunchError("remote attempt14 packet path is unsafe or duplicated")
        observed[relative] = (size, xet_hash)
    expected = {relative: (len(payload), local_xet[relative]) for relative, payload in local.items()}
    if observed != expected:
        raise V2FinalEvaluationLaunchError("remote attempt14 packet inventory differs from local Xet bytes")


def _labels(row: Mapping[str, object]) -> dict[str, str]:
    raw = row.get("labels", {})
    if isinstance(raw, Mapping):
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()):
            raise V2FinalEvaluationLaunchError("HF job labels are malformed")
        return dict(raw)
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        labels: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                raise V2FinalEvaluationLaunchError("HF job labels are malformed")
            key, value = item.split("=", 1)
            if not key or key in labels:
                raise V2FinalEvaluationLaunchError("HF job labels are malformed")
            labels[key] = value
        return labels
    raise V2FinalEvaluationLaunchError("HF job labels are malformed")


def _require_jobs_snapshot(rows: Sequence[Mapping[str, object]], identity: Mapping[str, object]) -> None:
    try:
        require_no_active_hf_jobs(rows)
    except HfLaunchGateError as error:
        raise V2FinalEvaluationLaunchError(str(error)) from error
    for row in rows:
        labels = _labels(row)
        exact_identity = all(labels.get(str(key)) == value for key, value in identity.items())
        same_attempt = labels.get("campaign") == "v2-final-evaluation" and labels.get("attempt") == V2_EVALUATION_ATTEMPT
        serialized = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if row.get("name") == V2_EVALUATION_JOB_NAME or exact_identity or same_attempt or (
            V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI in serialized
        ):
            raise V2FinalEvaluationLaunchError("an all-jobs snapshot already contains attempt14")


def _claim_payload(contract: Mapping[str, object], *, now_utc: datetime) -> tuple[bytes, str]:
    if now_utc.tzinfo is None:
        raise V2FinalEvaluationLaunchError("attempt14 claim time must be timezone-aware")
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_launch_claim",
        "campaign": "v2-final-evaluation",
        "attempt": V2_EVALUATION_ATTEMPT,
        "packet_remote_uri": V2_EVALUATION_PACKET_REMOTE_URI,
        "packet_tree_sha256": contract["packet_tree_sha256"],
        "manifest_sha256": contract["manifest_sha256"],
        "launch_contract_sha256": contract["launch_contract_sha256"],
        "remote_result_uri": V2_EVALUATION_OUTPUT_REMOTE_URI,
        "claimed_at_utc": now_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "single_shot": True,
        "auto_retry_allowed": False,
    }
    unowned = canonical_json_bytes(claim)
    owner = hashlib.sha256(unowned).hexdigest()
    return canonical_json_bytes({**claim, "owner_token": owner}), owner


def _bind_argv(contract: Mapping[str, object], contract_sha: str) -> list[str]:
    argv = contract.get("argv_template")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise V2FinalEvaluationLaunchError("attempt14 launch argv is malformed")
    placeholder = "__LAUNCH_CONTRACT_SHA256__"
    if argv.count(placeholder) != 1 or argv.count("--detach") != 1:
        raise V2FinalEvaluationLaunchError("attempt14 launch argv placeholders differ")
    bound = list(argv)
    bound[bound.index(placeholder)] = contract_sha
    if placeholder in bound:
        raise V2FinalEvaluationLaunchError("attempt14 launch placeholder remains")
    return bound


def _value(row: object, name: str, default: object = None) -> object:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _bucket_id_and_prefix(remote_uri: str) -> tuple[str, str]:
    marker = "hf://buckets/"
    if not remote_uri.startswith(marker) or remote_uri.endswith("/"):
        raise V2FinalEvaluationLaunchError("attempt14 bucket metadata URI is invalid")
    parts = remote_uri[len(marker) :].split("/")
    if len(parts) < 3 or any(not part for part in parts):
        raise V2FinalEvaluationLaunchError("attempt14 bucket metadata URI is invalid")
    bucket_id = "/".join(parts[:2])
    prefix = "/".join(parts[2:])
    if bucket_id != "Buttermilk03/scriber-polishing-private-runs" or not _safe_relative(prefix):
        raise V2FinalEvaluationLaunchError("attempt14 bucket metadata URI is outside the exact private bucket")
    return bucket_id, prefix


def metadata_only_bucket_entries(api: object, *, remote_uri: str) -> list[dict[str, object]]:
    """List exact Bucket file metadata without resolving or streaming file content."""

    bucket_id, prefix = _bucket_id_and_prefix(remote_uri)
    list_tree = getattr(api, "list_bucket_tree", None)
    if not callable(list_tree):
        raise V2FinalEvaluationLaunchError("HF 1.9 bucket metadata API is unavailable")
    try:
        raw_entries: Iterable[object] = list_tree(
            bucket_id,
            prefix=prefix,
            recursive=True,
        )
        entries = list(raw_entries)
    except Exception as error:
        raise V2FinalEvaluationLaunchError("HF 1.9 bucket metadata listing failed closed") from error

    result: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    for row in entries:
        entry_type = _value(row, "type", _MISSING)
        path = _value(row, "path", _MISSING)
        if (
            entry_type not in {"file", "directory"}
            or not isinstance(path, str)
            or not _safe_relative(path)
            or path in seen
        ):
            raise V2FinalEvaluationLaunchError("remote attempt14 bucket metadata is ambiguous")
        seen.add(path)
        if entry_type == "directory":
            continue
        size = _value(row, "size", _MISSING)
        xet_hash = _value(row, "xet_hash", _MISSING)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(xet_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", xet_hash) is None
        ):
            raise V2FinalEvaluationLaunchError("remote attempt14 bucket file metadata is ambiguous")
        result[path] = {
            "type": "file",
            "path": path,
            "size": size,
            "xet_hash": xet_hash,
        }
    return [result[path] for path in sorted(result)]


def validate_exact_claim_commit_metadata(
    entries: Sequence[object],
    *,
    claim_path: str,
    payload: bytes,
) -> str:
    """Verify one regular-Git claim blob by metadata at the exact commit."""

    if len(entries) != 1:
        raise V2FinalEvaluationLaunchError("attempt14 claim commit metadata is incomplete")
    row = entries[0]
    expected_blob_id = hashlib.sha1(
        b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload,
        usedforsecurity=False,
    ).hexdigest()
    if (
        _value(row, "path", _MISSING) != claim_path
        or _value(row, "size", _MISSING) != len(payload)
        or _value(row, "blob_id", _MISSING) != expected_blob_id
        or _value(row, "lfs", _MISSING) is not None
        or _value(row, "xet_hash", _MISSING) is not None
    ):
        raise V2FinalEvaluationLaunchError("attempt14 claim commit metadata differs from exact payload")
    return expected_blob_id


def _volume_uri(value: str) -> str:
    if not value.startswith("hf://buckets/Buttermilk03/") or value.endswith("/") or any(
        marker in value for marker in ("\\", "?", "#", "/../", "/./")
    ):
        raise V2FinalEvaluationLaunchError("submitted attempt14 volume source is invalid")
    return value


def _volumes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise V2FinalEvaluationLaunchError("submitted attempt14 volumes are malformed")
    result: list[str] = []
    for raw in value:
        if isinstance(raw, str):
            source_and_mount, separator, mode = raw.rpartition(":")
            source, mount_separator, mount = source_and_mount.rpartition(":")
            if not separator or not mount_separator or mode not in {"ro", "rw"} or not mount.startswith("/"):
                raise V2FinalEvaluationLaunchError("submitted attempt14 volume is invalid")
            result.append(f"{_volume_uri(source)}:{mount}:{mode}")
            continue
        source = _value(raw, "source")
        mount = _value(raw, "mount_path")
        path = _value(raw, "path", _MISSING)
        read_only = _value(raw, "read_only")
        if (
            _value(raw, "type") != "bucket"
            or not isinstance(source, str)
            or not isinstance(mount, str)
            or not mount.startswith("/")
            or not isinstance(read_only, bool)
            or path is _MISSING
            or (path is not None and not isinstance(path, str))
        ):
            raise V2FinalEvaluationLaunchError("submitted attempt14 volume is incomplete")
        uri = source if source.startswith("hf://buckets/") else f"hf://buckets/{source}"
        if path:
            if not _safe_relative(path):
                raise V2FinalEvaluationLaunchError("submitted attempt14 volume path is invalid")
            uri += f"/{path}"
        result.append(f"{_volume_uri(uri)}:{mount}:{'ro' if read_only else 'rw'}")
    return result


def _inspection(row: object) -> dict[str, object]:
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
        raise V2FinalEvaluationLaunchError("submitted attempt14 command is malformed")
    labels = _labels(dict(row) if isinstance(row, Mapping) else {"labels": _value(row, "labels")})
    if labels.get("name") != V2_EVALUATION_JOB_NAME:
        raise V2FinalEvaluationLaunchError("submitted attempt14 name label differs")
    if _value(row, "environment") not in ({}, []) or _value(row, "secrets") not in ({}, []):
        raise V2FinalEvaluationLaunchError("submitted attempt14 job has environment or secrets")
    return {
        "id": str(_value(row, "id", "")),
        "image": str(_value(row, "docker_image", _value(row, "image", ""))),
        "flavor": str(_value(row, "flavor", "")),
        "labels": dict(sorted(labels.items())),
        "volumes": _volumes(_value(row, "volumes")),
        "command": list(command),
        "arguments": list(arguments),
        "environment": {},
        "secrets": {},
        "owner": str(_value(owner, "name", "")),
        "stage": str(stage).upper(),
    }


def _validate_submitted(
    cli_row: object, api_row: object, *, job_id: str, argv: Sequence[str]
) -> dict[str, object]:
    if _JOB_ID.fullmatch(job_id) is None:
        raise V2FinalEvaluationLaunchError("submitted attempt14 job ID is invalid")
    cli = _inspection(cli_row)
    api = _inspection(api_row)
    if cli != api:
        raise V2FinalEvaluationLaunchError("HF API and CLI attempt14 inspections differ")
    command = list(argv)
    image_index = command.index(V2_EVALUATION_IMAGE)
    labels: dict[str, str] = {}
    volumes: list[str] = []
    index = 0
    while index < image_index:
        if command[index] == "--label":
            key, value = command[index + 1].split("=", 1)
            labels[key] = value
            index += 2
        elif command[index] == "-v":
            volumes.append(command[index + 1])
            index += 2
        else:
            index += 1
    expected = {
        "id": job_id,
        "image": V2_EVALUATION_IMAGE,
        "flavor": V2_EVALUATION_FLAVOR,
        "labels": dict(sorted(labels.items())),
        "volumes": volumes,
        "command": command[image_index + 1 :],
        "arguments": [],
        "environment": {},
        "secrets": {},
        "owner": "Buttermilk03",
        "stage": cli["stage"],
    }
    if cli != expected or cli["stage"] not in {"PENDING", "QUEUED", "SCHEDULING", "RUNNING"}:
        raise V2FinalEvaluationLaunchError("submitted attempt14 job differs from the exact contract")
    return {
        "id": job_id,
        "stage": cli["stage"],
        "exact_identity_verified": True,
        "api_cli_identity_sha256": _sha256(canonical_json_bytes(cli)),
        "command_sha256": _sha256(canonical_json_bytes(expected["command"])),
    }


def _reserve_receipt(path: Path) -> tuple[Path, bytes]:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(_RECEIPT_RESERVATION)
    except FileExistsError as error:
        raise V2FinalEvaluationLaunchError("attempt14 launch receipt already exists") from error
    return destination, _RECEIPT_RESERVATION


def _require_receipt_absent(path: Path) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() or path.is_symlink():
        raise V2FinalEvaluationLaunchError("attempt14 launch receipt already exists")
    return destination


def _require_preclaim_receipt_reservation(path: Path) -> tuple[Path, bytes]:
    destination = path.expanduser().resolve()
    completed = destination.with_name(destination.name + ".complete")
    if (
        path.is_symlink()
        or destination.is_symlink()
        or not destination.is_file()
        or destination.read_bytes() != _RECEIPT_RESERVATION
        or completed.exists()
        or completed.is_symlink()
    ):
        raise V2FinalEvaluationLaunchError("attempt14 preclaim receipt reservation is not exact")
    return destination, _RECEIPT_RESERVATION


def _write_receipt(destination: Path, value: object, *, reservation: bytes) -> None:
    if destination.is_symlink() or destination.read_bytes() != reservation:
        raise V2FinalEvaluationLaunchError("attempt14 launch receipt reservation differs")
    completed = destination.with_name(destination.name + ".complete")
    try:
        with completed.open("xb") as stream:
            stream.write(canonical_json_bytes(value))
        completed.replace(destination)
    except FileExistsError as error:
        raise V2FinalEvaluationLaunchError("attempt14 completed receipt staging path already exists") from error


def _remote_preconditions(
    control: V2FinalEvaluationControlPlane, *, packet: Path, claim_must_exist: bool
) -> None:
    validate_remote_v2_evaluation_packet(control, packet_dir=packet)
    if control.output_prefix_entries():
        raise V2FinalEvaluationLaunchError("the unique attempt14 output prefix is not empty")
    if control.claim_exists() is not claim_must_exist:
        state = "present" if claim_must_exist else "absent"
        raise V2FinalEvaluationLaunchError(f"the permanent attempt14 claim is not {state}")


def launch_v2_final_evaluation_job(
    control: V2FinalEvaluationControlPlane,
    *,
    packet_dir: str | Path,
    receipt_path: str | Path,
    now_utc: datetime | None = None,
    resume_preclaim_reservation: bool = False,
) -> dict[str, object]:
    """Upload an empty target exactly, claim permanently, and submit once.

    Recovery is explicit and accepts only the exact reservation left before a
    claim. It never uploads again and reaches the claim only after two stable
    all-job snapshots plus repeated empty-output and absent-claim checks.
    """

    packet = Path(packet_dir).expanduser().resolve()
    contract, manifest_sha, inventory_sha, contract_sha = _load_packet(packet)
    try:
        verify_v2_final_evaluation_packet(
            packet,
            expected_packet_tree_sha256=str(contract["packet_tree_sha256"]),
            expected_manifest_sha256=manifest_sha,
            expected_launch_contract_sha256=contract_sha,
        )
    except V2FinalEvaluationError as error:
        raise V2FinalEvaluationLaunchError(f"local attempt14 packet verification failed: {error}") from error
    if contract["inventory_sha256"] != inventory_sha:
        raise V2FinalEvaluationLaunchError("local attempt14 inventory differs from contract")
    contract = {**contract, "launch_contract_sha256": contract_sha}
    identity = contract["identity_labels"]
    if not isinstance(identity, Mapping):
        raise V2FinalEvaluationLaunchError("attempt14 identity labels are malformed")
    if resume_preclaim_reservation:
        receipt_destination, receipt_reservation = _require_preclaim_receipt_reservation(
            Path(receipt_path)
        )
        receipt_candidate = receipt_destination
    else:
        receipt_candidate = _require_receipt_absent(Path(receipt_path))
        receipt_destination = None
        receipt_reservation = None

    first = control.authoritative_jobs()
    _require_jobs_snapshot(first, identity)
    if control.output_prefix_entries():
        raise V2FinalEvaluationLaunchError("the unique attempt14 output prefix is not empty")
    if control.claim_exists():
        raise V2FinalEvaluationLaunchError("the permanent attempt14 claim already exists")
    packet_was_uploaded = False
    if not control.packet_entries():
        if resume_preclaim_reservation:
            raise V2FinalEvaluationLaunchError(
                "attempt14 preclaim recovery requires the exact existing remote packet"
            )
        receipt_destination, receipt_reservation = _reserve_receipt(receipt_candidate)
        try:
            control.upload_packet_once(packet)
        except Exception as error:
            raise V2FinalEvaluationLaunchError("attempt14 packet upload failed closed") from error
        packet_was_uploaded = True
    _remote_preconditions(control, packet=packet, claim_must_exist=False)

    second = control.authoritative_jobs()
    _require_jobs_snapshot(second, identity)
    if canonical_json_bytes(first) != canonical_json_bytes(second):
        raise V2FinalEvaluationLaunchError("fresh all-jobs snapshot changed before attempt14 claim")
    _remote_preconditions(control, packet=packet, claim_must_exist=False)

    if receipt_destination is None or receipt_reservation is None:
        receipt_destination, receipt_reservation = _reserve_receipt(receipt_candidate)
    claim_payload, owner = _claim_payload(contract, now_utc=now_utc or datetime.now(UTC))
    claim_commit = control.acquire_permanent_claim(claim_payload)
    if _COMMIT.fullmatch(claim_commit) is None:
        raise V2FinalEvaluationLaunchError("permanent attempt14 claim commit is invalid")
    _remote_preconditions(control, packet=packet, claim_must_exist=True)
    third = control.authoritative_jobs()
    _require_jobs_snapshot(third, identity)
    if canonical_json_bytes(second) != canonical_json_bytes(third):
        raise V2FinalEvaluationLaunchError("fresh all-jobs snapshot changed after attempt14 claim")

    bound_argv = _bind_argv(contract, contract_sha)
    try:
        submission = control.submit_detached_once(bound_argv)
    except Exception as error:
        raise V2FinalEvaluationLaunchError(
            "attempt14 submission failed or is ambiguous; claim retained and retry forbidden"
        ) from error
    matches = sorted(set(_DETACHED_JOB_ID.findall(submission.lower())))
    if len(matches) != 1:
        raise V2FinalEvaluationLaunchError(
            "attempt14 submission returned an ambiguous job ID; claim retained and retry forbidden"
        )
    job_id = matches[0]
    cli_row, api_row, url = control.inspect_submitted_job(job_id)
    inspection = _validate_submitted(cli_row, api_row, job_id=job_id, argv=bound_argv)
    if re.fullmatch(rf"https://huggingface\.co/jobs/(?:Buttermilk03/)?{re.escape(job_id)}", url) is None:
        raise V2FinalEvaluationLaunchError("attempt14 job URL differs from inspected ID; claim retained")
    receipt = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_launch_receipt",
        "claim_repository": V2_EVALUATION_CLAIM_REPOSITORY_ID,
        "claim_path": V2_EVALUATION_CLAIM_PATH,
        "claim_commit": claim_commit,
        "owner_token": owner,
        "job_id": job_id,
        "job_inspection": {**inspection, "url": url},
        "packet_tree_sha256": contract["packet_tree_sha256"],
        "manifest_sha256": manifest_sha,
        "launch_contract_sha256": contract_sha,
        "packet_upload": {
            "performed": packet_was_uploaded,
            "remote_uri": V2_EVALUATION_PACKET_REMOTE_URI,
            "exact_xet_hashes_verified": True,
            "downloaded_for_verification": False,
        },
        "output_prefix_uri": V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI,
        "remote_result_uri": V2_EVALUATION_OUTPUT_REMOTE_URI,
        "submitted_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authoritative_snapshot_count": 3,
        "duplicate_retry_allowed": False,
    }
    _write_receipt(receipt_destination, receipt, reservation=receipt_reservation)
    return receipt
