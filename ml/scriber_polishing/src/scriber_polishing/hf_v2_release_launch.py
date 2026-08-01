"""Exact-once launch gate for the Scriber V2 BF16/Q8_0 HF conversion."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .hf_launch_gate import HfLaunchGateError, require_no_active_hf_jobs
from .hf_v2_release_job import (
    V2_RELEASE_CLAIM_OWNER_PLACEHOLDER,
    V2_RELEASE_CONTRACT_HASH_PLACEHOLDER,
    V2_RELEASE_JOB_FLAVOR,
    V2_RELEASE_JOB_IMAGE,
    V2_RELEASE_OUTPUT_LABEL,
    V2_RELEASE_PACKET_REMOTE_URI,
    V2ReleaseJobError,
    launch_contract,
    validate_v2_release_job_schema,
    verify_v2_release_job_packet,
)
from .hf_v2_training_job import _safe_relative
from .v2_q8_bf16_release import (
    V2_RELEASE_ATTEMPT,
    V2_RELEASE_CLAIM_PATH,
    V2_RELEASE_CLAIM_REPOSITORY,
    V2_RELEASE_JOB_NAME,
    V2_RELEASE_OUTPUT_URI,
    canonical_json_bytes,
    sha256_bytes,
)

V2_RELEASE_CLAIM_REPOSITORY_TYPE = "dataset"
V2_RELEASE_PACKET_REMOTE_RELATIVE = (
    f"{V2_RELEASE_ATTEMPT}/packets/{V2_RELEASE_JOB_NAME}"
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_DETACHED_JOB_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
_MISSING = object()


class V2ReleaseLaunchError(RuntimeError):
    """The control plane could not prove one exclusive attempt15 submission."""


class V2ReleaseLaunchControlPlane(Protocol):
    def authoritative_jobs(self) -> list[dict[str, object]]: ...

    def packet_entries(self) -> list[dict[str, object]]: ...

    def packet_xet_hashes(self, packet_dir: Path) -> dict[str, str]: ...

    def output_prefix_entries(self) -> list[dict[str, object]]: ...

    def claim_exists(self) -> bool: ...

    def acquire_permanent_claim(self, payload: bytes) -> str: ...

    def submit_detached_once(self, argv: list[str]) -> str: ...

    def inspect_submitted_job(self, job_id: str) -> tuple[object, object, str]: ...


def _load_packet_metadata(
    packet: Path,
) -> tuple[dict[str, object], dict[str, object], str, str, str]:
    try:
        manifest_payload = (packet / "package-manifest.json").read_bytes()
        inventory_payload = (packet / "tree-inventory.json").read_bytes()
        contract_payload = (packet / "launch-contract.json").read_bytes()
        manifest = json.loads(manifest_payload)
        contract = json.loads(contract_payload)
    except (OSError, json.JSONDecodeError) as error:
        raise V2ReleaseLaunchError("local attempt15 packet metadata is unavailable") from error
    if not isinstance(manifest, dict) or not isinstance(contract, dict):
        raise V2ReleaseLaunchError("local attempt15 metadata must contain objects")
    return (
        manifest,
        contract,
        sha256_bytes(manifest_payload),
        sha256_bytes(inventory_payload),
        sha256_bytes(contract_payload),
    )


def validate_v2_release_launch_contract(
    contract: Mapping[str, object], *, contract_sha256: str
) -> dict[str, object]:
    if _HASH.fullmatch(contract_sha256) is None:
        raise V2ReleaseLaunchError("attempt15 launch contract SHA-256 is invalid")
    expected = launch_contract(
        source_git_head=str(contract.get("source_git_head", "")),
        packet_tree_sha256=str(contract.get("packet_tree_sha256", "")),
        manifest_sha256=str(contract.get("manifest_sha256", "")),
        inventory_sha256=str(contract.get("inventory_sha256", "")),
    )
    if dict(contract) != expected or contract.get("active_job_snapshots_required") != 3:
        raise V2ReleaseLaunchError("attempt15 launch contract differs from the reviewed argv")
    return dict(contract)


def bind_v2_release_launch_argv(
    contract: Mapping[str, object], *, owner_token: str, contract_sha256: str
) -> list[str]:
    checked = validate_v2_release_launch_contract(
        contract, contract_sha256=contract_sha256
    )
    if _OWNER.fullmatch(owner_token) is None:
        raise V2ReleaseLaunchError("attempt15 claim owner token is invalid")
    argv = checked.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise V2ReleaseLaunchError("attempt15 launch argv is malformed")
    owner_occurrences = sum(
        item.count(V2_RELEASE_CLAIM_OWNER_PLACEHOLDER) for item in argv
    )
    contract_occurrences = sum(
        item.count(V2_RELEASE_CONTRACT_HASH_PLACEHOLDER) for item in argv
    )
    if owner_occurrences != 1 or contract_occurrences != 1:
        raise V2ReleaseLaunchError("attempt15 launch placeholders differ")
    bound = [
        item.replace(V2_RELEASE_CLAIM_OWNER_PLACEHOLDER, owner_token).replace(
            V2_RELEASE_CONTRACT_HASH_PLACEHOLDER, contract_sha256
        )
        for item in argv
    ]
    if any(
        V2_RELEASE_CLAIM_OWNER_PLACEHOLDER in item
        or V2_RELEASE_CONTRACT_HASH_PLACEHOLDER in item
        for item in bound
    ):
        raise V2ReleaseLaunchError("attempt15 launch placeholder remains")
    return bound


def _labels(row: Mapping[str, object]) -> dict[str, str]:
    raw = row.get("labels", {})
    if isinstance(raw, Mapping):
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw.items()
        ):
            raise V2ReleaseLaunchError("HF job labels are malformed")
        return dict(raw)
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        result: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                raise V2ReleaseLaunchError("HF job labels are malformed")
            key, value = item.split("=", 1)
            if not key or key in result:
                raise V2ReleaseLaunchError("HF job labels are malformed")
            result[key] = value
        return result
    raise V2ReleaseLaunchError("HF job labels are malformed")


def require_no_prior_v2_release_job(rows: Sequence[Mapping[str, object]]) -> None:
    """Block any terminal or active job carrying the unique attempt15 identity."""

    for row in rows:
        labels = _labels(row)
        if (
            row.get("name") == V2_RELEASE_JOB_NAME
            or labels.get("name") == V2_RELEASE_JOB_NAME
            or labels.get("campaign") == "v2-q8-bf16-release"
            or labels.get("role") == "v2-quantization"
            or labels.get("output-prefix") == V2_RELEASE_OUTPUT_LABEL
        ):
            raise V2ReleaseLaunchError(
                "an authoritative HF snapshot already contains the attempt15 job"
            )


def build_local_v2_release_packet_xet_inventory(
    control: V2ReleaseLaunchControlPlane, *, packet_dir: str | Path
) -> dict[str, tuple[int, str]]:
    """Bind local packet files to the same Xet identity exposed by Buckets metadata."""

    packet = Path(packet_dir).expanduser().resolve()
    files: dict[str, int] = {}
    for path in sorted(packet.rglob("*")):
        if path.is_symlink():
            raise V2ReleaseLaunchError("local attempt15 packet contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(packet).as_posix()
        if not _safe_relative(relative) or relative in files:
            raise V2ReleaseLaunchError(
                "local attempt15 packet path is unsafe or duplicated"
            )
        files[relative] = path.stat().st_size
    if not files:
        raise V2ReleaseLaunchError("local attempt15 packet is empty")
    try:
        local_xet = control.packet_xet_hashes(packet)
    except Exception as error:
        raise V2ReleaseLaunchError(
            "local attempt15 Xet identity calculation failed"
        ) from error
    if set(local_xet) != set(files) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in local_xet.values()
    ):
        raise V2ReleaseLaunchError(
            "local attempt15 Xet inventory is incomplete or malformed"
        )
    return {
        relative: (size, local_xet[relative])
        for relative, size in files.items()
    }


def validate_remote_v2_release_packet(
    entries: Sequence[Mapping[str, object]],
    *,
    expected_inventory: Mapping[str, tuple[int, str]],
) -> None:
    """Compare only Buckets metadata; never read uploaded packet bytes back."""

    observed: dict[str, tuple[int, str]] = {}
    prefix = V2_RELEASE_PACKET_REMOTE_RELATIVE + "/"
    for row in entries:
        raw_path = row.get("path")
        entry_type = row.get("type")
        if entry_type == "directory":
            if not isinstance(raw_path, str) or not (
                raw_path == V2_RELEASE_PACKET_REMOTE_RELATIVE
                or raw_path.startswith(prefix)
            ) or not _safe_relative(raw_path):
                raise V2ReleaseLaunchError(
                    "remote attempt15 packet directory is outside the bound prefix"
                )
            continue
        size = row.get("size")
        xet_hash = row.get("xet_hash")
        if (
            entry_type != "file"
            or not isinstance(raw_path, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not isinstance(xet_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", xet_hash) is None
        ):
            raise V2ReleaseLaunchError("remote attempt15 packet has an ambiguous entry")
        if not raw_path.startswith(prefix):
            raise V2ReleaseLaunchError(
                "remote attempt15 packet file is outside the bound prefix"
            )
        relative = raw_path[len(prefix) :]
        if not _safe_relative(relative) or relative in observed:
            raise V2ReleaseLaunchError("remote attempt15 packet path is unsafe or duplicated")
        observed[relative] = (size, xet_hash)
    if observed != dict(expected_inventory):
        raise V2ReleaseLaunchError(
            "remote attempt15 packet Xet inventory differs from the local packet"
        )


def _git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def validate_v2_release_claim_commit_metadata(
    repository_info: object,
    path_entries: Sequence[object],
    *,
    expected_commit: str,
    payload: bytes,
) -> dict[str, object]:
    """Verify the permanent claim from metadata at its exact commit."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
        raise V2ReleaseLaunchError("permanent attempt15 claim commit is invalid")
    if (
        _value(repository_info, "sha") != expected_commit
        or _value(repository_info, "private") is not True
    ):
        raise V2ReleaseLaunchError(
            "permanent attempt15 claim exact-revision metadata differs"
        )
    if len(path_entries) != 1:
        raise V2ReleaseLaunchError(
            "permanent attempt15 claim path metadata is missing or ambiguous"
        )
    entry = path_entries[0]
    size = _value(entry, "size")
    if (
        _value(entry, "path") != V2_RELEASE_CLAIM_PATH
        or isinstance(size, bool)
        or size != len(payload)
    ):
        raise V2ReleaseLaunchError(
            "permanent attempt15 claim path or size metadata differs"
        )
    last_commit = _value(entry, "last_commit", _value(entry, "lastCommit"))
    if _value(last_commit, "oid", _value(last_commit, "id")) != expected_commit:
        raise V2ReleaseLaunchError(
            "permanent attempt15 claim was not created by the exact commit"
        )
    lfs = _value(entry, "lfs")
    blob_id = _value(entry, "blob_id", _value(entry, "oid"))
    if lfs is None:
        expected_blob = _git_blob_oid(payload)
        if blob_id != expected_blob:
            raise V2ReleaseLaunchError(
                "permanent attempt15 claim Git blob metadata differs"
            )
        storage_identity = {"kind": "git_blob", "oid": expected_blob}
    else:
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        lfs_size = _value(lfs, "size")
        lfs_sha256 = _value(lfs, "sha256", _value(lfs, "oid"))
        if (
            isinstance(lfs_size, bool)
            or lfs_size != len(payload)
            or lfs_sha256 != expected_sha256
            or not isinstance(blob_id, str)
            or re.fullmatch(r"[0-9a-f]{40}", blob_id) is None
        ):
            raise V2ReleaseLaunchError(
                "permanent attempt15 claim LFS metadata differs"
            )
        storage_identity = {"kind": "lfs", "sha256": expected_sha256}
    return {
        "commit": expected_commit,
        "path": V2_RELEASE_CLAIM_PATH,
        "size": len(payload),
        "storage_identity": storage_identity,
        "metadata_only": True,
    }


def build_v2_release_launch_claim(
    contract: Mapping[str, object],
    *,
    contract_sha256: str,
    claimed_at_utc: datetime,
) -> tuple[bytes, str]:
    checked = validate_v2_release_launch_contract(
        contract, contract_sha256=contract_sha256
    )
    if claimed_at_utc.tzinfo is None:
        raise V2ReleaseLaunchError("attempt15 claim time must be timezone-aware")
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_q8_bf16_release_launch_claim",
        "campaign": "v2-q8-bf16-release",
        "attempt": V2_RELEASE_ATTEMPT,
        "source_git_head": checked["source_git_head"],
        "packet_remote_uri": V2_RELEASE_PACKET_REMOTE_URI,
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "manifest_sha256": checked["manifest_sha256"],
        "launch_contract_sha256": contract_sha256,
        "remote_result_uri": f"{V2_RELEASE_OUTPUT_URI}/quantization",
        "claimed_at_utc": claimed_at_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "single_shot": True,
        "auto_retry_allowed": False,
    }
    owner = hashlib.sha256(canonical_json_bytes(claim)).hexdigest()
    return canonical_json_bytes({**claim, "owner_token": owner}), owner


def _value(row: object, name: str, default: object = None) -> object:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _canonical_volume_source(value: str) -> str:
    prefix = "hf://buckets/Buttermilk03/"
    if (
        not value.startswith(prefix)
        or any(marker in value for marker in ("\\", "?", "#", "//..", "/../", "/./"))
        or value.endswith("/")
        or not _safe_relative(value.removeprefix(prefix))
    ):
        raise V2ReleaseLaunchError("submitted attempt15 volume source is invalid")
    return value


def _normalize_volumes(value: object) -> list[str]:
    if not isinstance(value, list):
        raise V2ReleaseLaunchError("submitted attempt15 volumes are malformed")
    result: list[str] = []
    for raw in value:
        if isinstance(raw, str):
            source_and_mount, mode_separator, mode = raw.rpartition(":")
            source, mount_separator, mount = source_and_mount.rpartition(":")
            if (
                not mode_separator
                or not mount_separator
                or mode not in {"ro", "rw"}
                or not mount.startswith("/")
            ):
                raise V2ReleaseLaunchError("submitted attempt15 volume is invalid")
            result.append(f"{_canonical_volume_source(source)}:{mount}:{mode}")
            continue
        volume_type = _value(raw, "type")
        source = _value(raw, "source")
        mount = _value(raw, "mount_path")
        path = _value(raw, "path", _MISSING)
        read_only = _value(raw, "read_only")
        if (
            volume_type != "bucket"
            or not isinstance(source, str)
            or not isinstance(mount, str)
            or not mount.startswith("/")
            or not isinstance(read_only, bool)
            or path is _MISSING
            or (path is not None and not isinstance(path, str))
        ):
            raise V2ReleaseLaunchError("submitted attempt15 volume is incomplete")
        source_uri = source if source.startswith("hf://buckets/") else f"hf://buckets/{source}"
        if path:
            if not _safe_relative(path):
                raise V2ReleaseLaunchError("submitted attempt15 volume path is invalid")
            source_uri += f"/{path}"
        result.append(
            f"{_canonical_volume_source(source_uri)}:{mount}:{'ro' if read_only else 'rw'}"
        )
    return result


def _parse_detached_job_id(payload: str) -> str:
    if not isinstance(payload, str):
        raise V2ReleaseLaunchError("attempt15 submission output is not text")
    matches = sorted(set(_DETACHED_JOB_ID.findall(payload.lower())))
    if len(matches) != 1:
        raise V2ReleaseLaunchError(
            "attempt15 submission returned an ambiguous job ID; claim retained and retry forbidden"
        )
    return matches[0]


def parse_hf_cli_v2_release_inspection(payload: str | bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8", errors="strict") if isinstance(payload, bytes) else payload
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2ReleaseLaunchError("attempt15 CLI inspection is invalid JSON") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise V2ReleaseLaunchError(
            "attempt15 CLI inspection must contain exactly one job object"
        )
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
        raise V2ReleaseLaunchError("submitted attempt15 command is malformed")
    labels = _labels(
        dict(row) if isinstance(row, Mapping) else {"labels": _value(row, "labels")}
    )
    if labels.get("name") != V2_RELEASE_JOB_NAME:
        raise V2ReleaseLaunchError("submitted attempt15 job name differs")
    if _value(row, "environment") not in ({}, []) or _value(row, "secrets") not in ({}, []):
        raise V2ReleaseLaunchError("submitted attempt15 job carries environment or secrets")
    return {
        "id": str(_value(row, "id", "")),
        "name": labels["name"],
        "image": str(_value(row, "docker_image", _value(row, "image", ""))),
        "flavor": str(_value(row, "flavor", "")),
        "labels": dict(sorted(labels.items())),
        "volumes": _normalize_volumes(_value(row, "volumes")),
        "command": list(command),
        "arguments": list(arguments),
        "environment": {},
        "secrets": {},
        "owner": str(_value(owner, "name", "")),
        "stage": str(stage).upper(),
    }


def validate_submitted_v2_release_job(
    cli_row: object,
    api_row: object,
    *,
    expected_job_id: str,
    bound_argv: Sequence[str],
) -> dict[str, object]:
    if _JOB_ID.fullmatch(expected_job_id) is None:
        raise V2ReleaseLaunchError("submitted attempt15 job ID is invalid")
    cli = _normalize_inspection(cli_row)
    api = _normalize_inspection(api_row)
    if cli != api:
        raise V2ReleaseLaunchError("HF API and CLI attempt15 inspections differ")
    argv = list(bound_argv)
    try:
        image_index = argv.index(V2_RELEASE_JOB_IMAGE)
    except ValueError as error:
        raise V2ReleaseLaunchError("attempt15 image is missing from bound argv") from error
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
        "name": V2_RELEASE_JOB_NAME,
        "image": V2_RELEASE_JOB_IMAGE,
        "flavor": V2_RELEASE_JOB_FLAVOR,
        "labels": dict(sorted(labels.items())),
        "volumes": volumes,
        "command": argv[image_index + 1 :],
        "arguments": [],
        "environment": {},
        "secrets": {},
        "owner": "Buttermilk03",
        "stage": cli["stage"],
    }
    if cli != expected or cli["stage"] not in {
        "PENDING",
        "QUEUED",
        "SCHEDULING",
        "RUNNING",
    }:
        raise V2ReleaseLaunchError(
            "submitted attempt15 job differs from the exact launch contract"
        )
    return {
        "id": expected_job_id,
        "stage": cli["stage"],
        "exact_identity_verified": True,
        "api_cli_identity_sha256": sha256_bytes(canonical_json_bytes(cli)),
        "command_sha256": sha256_bytes(canonical_json_bytes(expected["command"])),
    }


def _write_receipt(path: Path, value: object) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(canonical_json_bytes(value))
    except FileExistsError as error:
        raise V2ReleaseLaunchError("attempt15 launch receipt already exists") from error


def _verify_remote_preconditions(
    control: V2ReleaseLaunchControlPlane,
    *,
    expected_packet_inventory: Mapping[str, tuple[int, str]],
    claim_must_exist: bool,
) -> None:
    validate_remote_v2_release_packet(
        control.packet_entries(), expected_inventory=expected_packet_inventory
    )
    if control.output_prefix_entries():
        raise V2ReleaseLaunchError("the unique attempt15 output prefix is already populated")
    if control.claim_exists() is not claim_must_exist:
        expected = "present" if claim_must_exist else "absent"
        raise V2ReleaseLaunchError(
            f"the permanent attempt15 launch claim is not {expected}"
        )


def _require_clean_snapshot(rows: list[dict[str, object]]) -> None:
    try:
        require_no_active_hf_jobs(rows)
    except HfLaunchGateError as error:
        raise V2ReleaseLaunchError(str(error)) from error
    require_no_prior_v2_release_job(rows)


def launch_v2_release_job(
    control: V2ReleaseLaunchControlPlane,
    *,
    packet_dir: str | Path,
    receipt_path: str | Path,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Acquire a permanent claim and submit attempt15 exactly once."""

    packet = Path(packet_dir).expanduser().resolve()
    _manifest, contract, manifest_sha, inventory_sha, contract_sha = _load_packet_metadata(packet)
    checked = validate_v2_release_launch_contract(
        contract, contract_sha256=contract_sha
    )
    try:
        verify_v2_release_job_packet(
            packet,
            expected_packet_tree_sha256=str(checked["packet_tree_sha256"]),
            expected_manifest_sha256=manifest_sha,
            expected_launch_contract_sha256=contract_sha,
        )
    except V2ReleaseJobError as error:
        raise V2ReleaseLaunchError(
            f"local attempt15 packet verification failed: {error}"
        ) from error
    if checked["inventory_sha256"] != inventory_sha:
        raise V2ReleaseLaunchError("local attempt15 inventory SHA-256 differs")
    expected_packet_inventory = build_local_v2_release_packet_xet_inventory(
        control, packet_dir=packet
    )

    first = control.authoritative_jobs()
    _require_clean_snapshot(first)
    _verify_remote_preconditions(
        control,
        expected_packet_inventory=expected_packet_inventory,
        claim_must_exist=False,
    )

    second = control.authoritative_jobs()
    _require_clean_snapshot(second)
    if canonical_json_bytes(first) != canonical_json_bytes(second):
        raise V2ReleaseLaunchError(
            "authoritative HF job inventory changed between required snapshots"
        )
    _verify_remote_preconditions(
        control,
        expected_packet_inventory=expected_packet_inventory,
        claim_must_exist=False,
    )

    claim_payload, owner = build_v2_release_launch_claim(
        checked,
        contract_sha256=contract_sha,
        claimed_at_utc=now_utc or datetime.now(UTC),
    )
    claim_commit = control.acquire_permanent_claim(claim_payload)
    if re.fullmatch(r"[0-9a-f]{40}", claim_commit) is None:
        raise V2ReleaseLaunchError("permanent attempt15 claim commit is invalid")
    _verify_remote_preconditions(
        control,
        expected_packet_inventory=expected_packet_inventory,
        claim_must_exist=True,
    )

    third = control.authoritative_jobs()
    _require_clean_snapshot(third)
    if canonical_json_bytes(second) != canonical_json_bytes(third):
        raise V2ReleaseLaunchError(
            "authoritative HF job inventory changed after the permanent claim"
        )
    bound_argv = bind_v2_release_launch_argv(
        checked, owner_token=owner, contract_sha256=contract_sha
    )
    try:
        submission = control.submit_detached_once(bound_argv)
    except Exception as error:
        raise V2ReleaseLaunchError(
            "attempt15 submission failed or is ambiguous; claim retained and retry forbidden"
        ) from error
    job_id = _parse_detached_job_id(submission)
    cli_row, api_row, url = control.inspect_submitted_job(job_id)
    inspection = validate_submitted_v2_release_job(
        cli_row,
        api_row,
        expected_job_id=job_id,
        bound_argv=bound_argv,
    )
    if re.fullmatch(
        rf"https://huggingface\.co/jobs/(?:Buttermilk03/)?{re.escape(job_id)}", url
    ) is None:
        raise V2ReleaseLaunchError(
            "attempt15 job URL differs from its inspected ID; claim retained"
        )
    receipt = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_q8_bf16_release_launch_receipt",
        "claim_repository": V2_RELEASE_CLAIM_REPOSITORY,
        "claim_path": V2_RELEASE_CLAIM_PATH,
        "claim_commit": claim_commit,
        "owner_token": owner,
        "job_id": job_id,
        "job_inspection": {**inspection, "url": url},
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "launch_contract_sha256": contract_sha,
        "remote_result_uri": f"{V2_RELEASE_OUTPUT_URI}/quantization",
        "submitted_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "authoritative_snapshot_count": 3,
        "duplicate_retry_allowed": False,
    }
    try:
        validate_v2_release_job_schema(
            receipt,
            "hf_v2_release_launch_receipt_schema.json",
            "attempt15 launch receipt",
        )
    except V2ReleaseJobError as error:
        raise V2ReleaseLaunchError(str(error)) from error
    _write_receipt(Path(receipt_path), receipt)
    return receipt
