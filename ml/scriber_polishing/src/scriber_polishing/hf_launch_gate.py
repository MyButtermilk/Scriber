"""Authoritative, fail-closed launch gates for the Scriber HF campaign."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .hf_llama_cpp_toolchain import (
    POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD,
    QUANTIZATION_RESERVED_COST_USD,
    REMAINING_HF_CREDITS_AUTHORIZATION_USD,
    REVIEWED_SCRIBER_GIT_HEAD,
    TOOLCHAIN_ATTEMPT,
    TOOLCHAIN_ATTEMPT_LABEL,
    TOOLCHAIN_CAMPAIGN_LABEL,
    TOOLCHAIN_FLAVOR,
    TOOLCHAIN_IMAGE,
    TOOLCHAIN_JOB_NAME,
    TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
    TOOLCHAIN_MAXIMUM_COST_USD,
    TOOLCHAIN_OUTPUT_MOUNT_RELATIVE,
    TOOLCHAIN_OUTPUT_MOUNT_URI,
    TOOLCHAIN_OUTPUT_ROOT,
    TOOLCHAIN_REMOTE_OUTPUT_URI,
    TOOLCHAIN_ROLE_LABEL,
    TOOLCHAIN_STAGING_ROOT,
    TOOLCHAIN_TIMEOUT_MINUTES,
    canonical_private_bucket_uri,
    hf_llama_cpp_toolchain_bootstrap_payload,
    validate_no_writable_read_only_uri_overlap,
)
from .hf_quantization_pipeline import (
    FULL_MODEL_SOURCE_REMOTE_URI,
    QUANTIZATION_ATTEMPT,
    QUANTIZATION_ATTEMPT_LABEL,
    QUANTIZATION_CAMPAIGN_LABEL,
    QUANTIZATION_FLAVOR,
    QUANTIZATION_IMAGE,
    QUANTIZATION_JOB_NAME,
    QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER,
    QUANTIZATION_OUTPUT_BUCKET,
    QUANTIZATION_OUTPUT_MOUNT_URI,
    QUANTIZATION_OUTPUT_RELATIVE,
    QUANTIZATION_OUTPUT_ROOT,
    QUANTIZATION_ROLE_LABEL,
    QUANTIZATION_TIMEOUT_MINUTES,
    hf_quantization_bootstrap_payload,
)

CLAIM_REPOSITORY_ID = "Buttermilk03/scriber-polishing-launch-claims"
CLAIM_REPOSITORY_TYPE = "dataset"
TOOLCHAIN_V19_CLAIM_PATH = "claims/full-120k-v2-quantized-eval/toolchain-v19.json"
QUANTIZATION_V2_CLAIM_PATH = (
    "claims/full-120k-v2-quantized-eval/quantization-v2.json"
)
HF_CLI_PREFIX = (
    "uvx",
    "--with",
    "click==8.3.1",
    "--with",
    "typer==0.21.0",
    "--with",
    "rich==14.3.3",
    "--from",
    "huggingface-hub==1.9.0",
)
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_OWNER = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{24}$")
_ACTIVE_STAGES = frozenset({"RUNNING", "SCHEDULING", "PENDING", "QUEUED"})
_TERMINAL_STAGES = frozenset({"COMPLETED", "ERROR", "CANCELED"})


class HfLaunchGateError(RuntimeError):
    """The authoritative remote launch gate could not prove exclusivity."""


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HfLaunchGateError(f"{label} must be an object")
    return value


def _required_hash(value: object, label: str) -> str:
    candidate = str(value or "")
    if _HASH.fullmatch(candidate) is None:
        raise HfLaunchGateError(f"{label} is invalid")
    return candidate


def _canonical_private_uri(value: object, label: str) -> str:
    try:
        return canonical_private_bucket_uri(str(value or ""), label)
    except (RuntimeError, ValueError) as error:
        raise HfLaunchGateError(f"{label} is not a canonical private bucket URI") from error


def _accounted_job_ids(contract: Mapping[str, object]) -> list[str]:
    raw = contract.get("accounted_campaign_job_ids")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise HfLaunchGateError("launch contract has no exact accounted job-ID set")
    job_ids = list(raw)
    if (
        not job_ids
        or job_ids != sorted(set(job_ids))
        or any(_JOB_ID.fullmatch(job_id) is None for job_id in job_ids)
        or contract.get("accounted_campaign_job_ids_sha256")
        != _sha256(_canonical_bytes(job_ids))
    ):
        raise HfLaunchGateError("launch contract accounted job-ID binding differs")
    return job_ids


def require_clean_checkout_at_head(
    expected_head: object,
    *,
    repository_root: str | Path,
    review_base_git_head: str = REVIEWED_SCRIBER_GIT_HEAD,
) -> str:
    """Recheck the exact clean reviewed checkout immediately before mutation."""

    head = str(expected_head or "")
    if _COMMIT_RE.fullmatch(head) is None or _COMMIT_RE.fullmatch(review_base_git_head) is None:
        raise HfLaunchGateError("reviewed source Git binding is invalid")
    root = Path(repository_root).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise HfLaunchGateError("reviewed repository root is invalid")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )

    resolved = run("rev-parse", "HEAD")
    status = run("status", "--porcelain=v1", "--untracked-files=all")
    ancestry = run("merge-base", "--is-ancestor", review_base_git_head, head)
    if (
        resolved.returncode != 0
        or resolved.stderr.strip()
        or resolved.stdout.strip() != head
        or status.returncode != 0
        or status.stderr.strip()
        or status.stdout.strip()
        or ancestry.returncode != 0
        or ancestry.stdout.strip()
        or ancestry.stderr.strip()
    ):
        raise HfLaunchGateError(
            "reviewed checkout changed, is dirty, or is outside the reviewed lineage"
        )
    return head


def require_claim_never_existed(
    api: object,
    *,
    repository_id: str,
    repository_type: str,
    claim_path: str,
    expected_head_sha: str,
    maximum_commits: int = 256,
) -> None:
    """Reject historical reuse and bind the scan to the later CAS parent."""

    if _COMMIT_RE.fullmatch(expected_head_sha) is None:
        raise HfLaunchGateError("launch-claim expected head is invalid")
    if not api.repo_exists(
        repository_id,
        repo_type=repository_type,
    ):
        raise HfLaunchGateError("launch-claim repository disappeared before history scan")
    commits = list(
        api.list_repo_commits(
            repository_id,
            repo_type=repository_type,
        )
    )
    if not commits or len(commits) > maximum_commits:
        raise HfLaunchGateError(
            "launch-claim history is empty, truncated, or exceeds the reviewed bound"
        )
    revisions: list[str] = []
    for commit in commits:
        if isinstance(commit, Mapping):
            revision = commit.get("commit_id") or commit.get("oid") or commit.get("id")
        else:
            revision = (
                getattr(commit, "commit_id", None)
                or getattr(commit, "oid", None)
                or getattr(commit, "id", None)
            )
        if not isinstance(revision, str) or _COMMIT_RE.fullmatch(revision) is None:
            raise HfLaunchGateError("launch-claim history contains an invalid revision")
        revisions.append(revision)
        files = api.list_repo_files(
            repository_id,
            repo_type=repository_type,
            revision=revision,
        )
        if claim_path in files:
            raise HfLaunchGateError(
                "exclusive launch claim existed historically and cannot be reused"
            )
    if expected_head_sha not in revisions:
        raise HfLaunchGateError(
            "launch-claim history does not contain the intended CAS parent"
        )
    final_info = api.repo_info(
        repository_id,
        repo_type=repository_type,
    )
    if (
        getattr(final_info, "private", None) is not True
        or getattr(final_info, "sha", None) != expected_head_sha
    ):
        raise HfLaunchGateError(
            "launch-claim repository changed during the history scan"
        )


def parse_hf_jobs_json(payload: str) -> list[dict[str, object]]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HfLaunchGateError(f"HF active-job response is not JSON: {error}") from error
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HfLaunchGateError("HF active-job response must be an array of objects")
    return value


def parse_hf_bucket_list(payload: str) -> list[dict[str, object]]:
    if payload.strip() == "(empty)":
        return []
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise HfLaunchGateError(f"HF bucket response is not JSON: {error}") from error
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HfLaunchGateError("HF bucket response must be an array of objects")
    return value


def require_hf_control_plane_runtime() -> None:
    try:
        version = importlib.metadata.version("huggingface-hub")
    except importlib.metadata.PackageNotFoundError as error:
        raise HfLaunchGateError("pinned Hugging Face control plane is unavailable") from error
    if version != "1.9.0":
        raise HfLaunchGateError(
            "launcher must run inside the reviewed huggingface-hub==1.9.0 environment"
        )


_MISSING = object()


def _job_value(row: object, field: str) -> object:
    if isinstance(row, Mapping):
        return row.get(field, _MISSING)
    return getattr(row, field, _MISSING)


def _normalize_job_labels(value: object, label: str) -> dict[str, str]:
    if isinstance(value, Mapping):
        pairs = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        pairs = []
        for raw in value:
            if not isinstance(raw, str):
                raise HfLaunchGateError(f"{label} has missing or malformed labels")
            key, separator, item_value = raw.partition("=")
            if not separator:
                raise HfLaunchGateError(f"{label} has missing or malformed labels")
            pairs.append((key, item_value))
    else:
        raise HfLaunchGateError(f"{label} has missing or malformed labels")
    normalized: dict[str, str] = {}
    for key, item_value in pairs:
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(item_value, str)
            or key in normalized
        ):
            raise HfLaunchGateError(
                f"{label} has missing, malformed, or duplicated labels"
            )
        normalized[key] = item_value
    return dict(sorted(normalized.items()))


def reconcile_authoritative_hf_job_snapshots(
    api_jobs: Sequence[object],
    cli_jobs: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Require independent API and CLI all-job snapshots to be exactly complete."""

    def identity(row: object, label: str) -> dict[str, object]:
        status = _job_value(row, "status")
        stage = (
            status.get("stage")
            if isinstance(status, Mapping)
            else getattr(status, "stage", None)
        ) or _job_value(row, "stage")
        owner = _job_value(row, "owner")
        owner_name = owner.get("name") if isinstance(owner, Mapping) else getattr(owner, "name", None)
        return {
            "id": str(_job_value(row, "id") or ""),
            "stage": str(stage or "").upper(),
            "owner": str(owner_name or ""),
            "flavor": str(_job_value(row, "flavor") or ""),
            "image": str(
                (_job_value(row, "docker_image") if _job_value(row, "docker_image") is not _MISSING else None)
                or (_job_value(row, "image") if _job_value(row, "image") is not _MISSING else None)
                or ""
            ),
            "labels": _normalize_job_labels(
                _job_value(row, "labels"),
                label,
            ),
        }

    def indexed(rows: Sequence[object], label: str) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            item = identity(row, label)
            job_id = str(item["id"])
            if _JOB_ID.fullmatch(job_id) is None or job_id in result:
                raise HfLaunchGateError(f"{label} job snapshot has invalid or duplicate IDs")
            if item["stage"] not in (_ACTIVE_STAGES | _TERMINAL_STAGES):
                raise HfLaunchGateError(f"{label} job snapshot has an ambiguous stage")
            result[job_id] = item
        return result

    api_index = indexed(list(api_jobs), "HF API")
    cli_rows_list: list[dict[str, object]] = []
    for raw in cli_jobs:
        row = dict(_mapping(raw, "HF CLI all-job row"))
        row["labels"] = _normalize_job_labels(
            _job_value(row, "labels"),
            "HF CLI",
        )
        cli_rows_list.append(row)
    cli_index = indexed(cli_rows_list, "HF CLI")
    if api_index != cli_index:
        raise HfLaunchGateError(
            "HF API and CLI all-job snapshots differ; completeness is not proven"
        )
    return cli_rows_list


def _job_stage(row: Mapping[str, object]) -> str:
    raw_status = row.get("status")
    if isinstance(raw_status, Mapping):
        raw_status = raw_status.get("stage")
    return str(raw_status or row.get("stage") or "").upper()


def require_no_active_hf_jobs(rows: Sequence[Mapping[str, object]]) -> None:
    active_or_ambiguous = [
        str(row.get("id", "unknown"))
        for row in rows
        if _job_stage(row) not in _TERMINAL_STAGES
    ]
    if active_or_ambiguous:
        raise HfLaunchGateError(
            "authoritative HF snapshot contains active or ambiguous jobs: "
            + ", ".join(active_or_ambiguous)
        )


def build_authoritative_campaign_inventory(
    rows: Sequence[Mapping[str, object]],
    *,
    observed_at_utc: datetime,
    forbidden_attempt: str,
    accounted_campaign_job_ids: Sequence[str],
) -> dict[str, object]:
    """Bind all terminal jobs related to the current quantization campaign."""

    if (
        forbidden_attempt not in {TOOLCHAIN_ATTEMPT, QUANTIZATION_ATTEMPT}
        or observed_at_utc.tzinfo is None
    ):
        raise HfLaunchGateError("campaign inventory forbidden attempt is invalid")
    accounted_ids = list(accounted_campaign_job_ids)
    if (
        not accounted_ids
        or accounted_ids != sorted(set(accounted_ids))
        or any(_JOB_ID.fullmatch(job_id) is None for job_id in accounted_ids)
    ):
        raise HfLaunchGateError("accounted campaign job IDs are not canonical")
    accounted = set(accounted_ids)
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in rows:
        row = _mapping(raw, "HF campaign inventory job")
        labels = _normalize_job_labels(
            _job_value(row, "labels"),
            "authoritative HF inventory",
        )
        job_id = str(row.get("id", ""))
        relevant = (
            job_id in accounted
            or
            labels.get("project") == "scriber-polishing"
            or
            labels.get("campaign") == "full-120k-v2-quantized-eval"
            or str(labels.get("run", "")).startswith("llama-cpp-")
            or str(labels.get("role", "")).startswith("toolchain-")
            or labels.get("role") == "quantized-eval-toolchain"
            or labels.get("role") == "quantized-evaluation"
        )
        if not relevant:
            continue
        if job_id not in accounted:
            raise HfLaunchGateError(
                "authoritative inventory contains an unaccounted campaign job"
            )
        stage = _job_stage(row)
        owner = row.get("owner")
        owner_name = owner.get("name") if isinstance(owner, Mapping) else None
        if (
            _JOB_ID.fullmatch(job_id) is None
            or job_id in seen
            or stage not in _TERMINAL_STAGES
            or owner_name != "Buttermilk03"
            or labels.get("attempt") == forbidden_attempt
            or (
                forbidden_attempt == QUANTIZATION_ATTEMPT
                and labels.get("role") == "quantized-evaluation"
            )
        ):
            raise HfLaunchGateError(
                "authoritative campaign inventory is ambiguous, active, or already launched"
            )
        seen.add(job_id)
        records.append(
            {
                "id": job_id,
                "stage": stage,
                "flavor": str(row.get("flavor", "")),
                "image": str(
                    row.get("docker_image") or row.get("image") or ""
                ),
                "labels": dict(sorted(labels.items())),
            }
        )
    records.sort(key=lambda record: str(record["id"]))
    if seen != accounted:
        raise HfLaunchGateError(
            "authoritative campaign inventory omits an accounted cost job"
        )
    payload_hash = _sha256(_canonical_bytes(records))
    return {
        "schema_version": 1,
        "kind": "scriber_hf_authoritative_campaign_inventory",
        "campaign": "full-120k-v2-quantized-eval",
        "observed_at_utc": observed_at_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "forbidden_attempt": forbidden_attempt,
        "terminal_only": True,
        "job_count": len(records),
        "job_ids": [record["id"] for record in records],
        "accounted_campaign_job_ids_sha256": _sha256(
            _canonical_bytes(accounted_ids)
        ),
        "jobs": records,
        "inventory_sha256": payload_hash,
    }


def validate_authoritative_campaign_inventory(
    value: Mapping[str, object],
    *,
    forbidden_attempt: str,
    accounted_campaign_job_ids: Sequence[str],
) -> dict[str, object]:
    inventory = dict(_mapping(value, "authoritative campaign inventory"))
    expected_fields = {
        "schema_version",
        "kind",
        "campaign",
        "observed_at_utc",
        "forbidden_attempt",
        "terminal_only",
        "job_count",
        "job_ids",
        "accounted_campaign_job_ids_sha256",
        "jobs",
        "inventory_sha256",
    }
    jobs = inventory.get("jobs")
    accounted_ids = list(accounted_campaign_job_ids)
    if (
        not accounted_ids
        or accounted_ids != sorted(set(accounted_ids))
        or any(_JOB_ID.fullmatch(job_id) is None for job_id in accounted_ids)
        or
        set(inventory) != expected_fields
        or inventory.get("schema_version") != 1
        or inventory.get("kind")
        != "scriber_hf_authoritative_campaign_inventory"
        or inventory.get("campaign") != "full-120k-v2-quantized-eval"
        or inventory.get("forbidden_attempt") != forbidden_attempt
        or inventory.get("terminal_only") is not True
        or not isinstance(jobs, list)
        or not jobs
        or inventory.get("job_count") != len(jobs)
        or inventory.get("job_ids")
        != [row.get("id") for row in jobs if isinstance(row, Mapping)]
        or inventory.get("job_ids") != accounted_ids
        or inventory.get("accounted_campaign_job_ids_sha256")
        != _sha256(_canonical_bytes(accounted_ids))
        or inventory.get("inventory_sha256") != _sha256(_canonical_bytes(jobs))
    ):
        raise HfLaunchGateError("authoritative campaign inventory binding differs")
    seen: set[str] = set()
    for row in jobs:
        if not isinstance(row, Mapping):
            raise HfLaunchGateError("campaign inventory job is not an object")
        labels = row.get("labels")
        job_id = str(row.get("id", ""))
        if (
            set(row) != {"id", "stage", "flavor", "image", "labels"}
            or _JOB_ID.fullmatch(job_id) is None
            or job_id in seen
            or row.get("stage") not in _TERMINAL_STAGES
            or not isinstance(row.get("flavor"), str)
            or not isinstance(row.get("image"), str)
            or not isinstance(labels, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(label, str)
                for key, label in labels.items()
            )
            or labels.get("attempt") == forbidden_attempt
            or (
                forbidden_attempt == QUANTIZATION_ATTEMPT
                and labels.get("role") == "quantized-evaluation"
            )
        ):
            raise HfLaunchGateError("campaign inventory job binding differs")
        seen.add(job_id)
    if [str(row["id"]) for row in jobs] != sorted(seen):
        raise HfLaunchGateError("campaign inventory jobs are not canonically ordered")
    observed = inventory.get("observed_at_utc")
    if not isinstance(observed, str):
        raise HfLaunchGateError("campaign inventory timestamp is missing")
    try:
        datetime.strptime(observed, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise HfLaunchGateError("campaign inventory timestamp is invalid") from error
    return inventory


def _require_claim_time_inventory_freshness(
    inventory: Mapping[str, object],
    *,
    claimed_at_utc: datetime,
) -> None:
    observed = datetime.strptime(
        str(inventory["observed_at_utc"]),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC)
    claimed = claimed_at_utc.astimezone(UTC)
    age = (claimed - observed).total_seconds()
    if age < 0 or age > 120:
        raise HfLaunchGateError(
            "authoritative campaign inventory is stale or from the future"
        )


def validate_toolchain_v19_launch_contract(
    value: Mapping[str, object],
    *,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    contract = dict(_mapping(value, "toolchain v19 launch contract"))
    argv = contract.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise HfLaunchGateError("toolchain launch argv must be a string array")
    packet_uri = contract.get("packet_remote_uri")
    runner_sha256 = contract.get("runner_sha256")
    tree = contract.get("packet_tree_sha256")
    manifest_sha256 = contract.get("manifest_sha256")
    accounted_job_ids = _accounted_job_ids(contract)
    actual_cost_evidence_sha256 = _required_hash(
        contract.get("actual_cost_evidence_sha256"),
        "toolchain actual-cost evidence hash",
    )
    canonical_packet_uri = _canonical_private_uri(packet_uri, "toolchain packet")
    expected_packet_volume = f"{packet_uri}:/input/repo:ro"
    expected_output_volume = f"{TOOLCHAIN_OUTPUT_MOUNT_URI}:/outputs:rw"
    expected_argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        TOOLCHAIN_JOB_NAME,
        "--flavor",
        TOOLCHAIN_FLAVOR,
        "--timeout",
        f"{TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=llama-cpp-b10156-toolchain",
        "--label",
        TOOLCHAIN_CAMPAIGN_LABEL,
        "--label",
        TOOLCHAIN_ROLE_LABEL,
        "--label",
        TOOLCHAIN_ATTEMPT_LABEL,
        "--label",
        f"job-timeout={TOOLCHAIN_TIMEOUT_MINUTES}m",
        "--label",
        f"output-prefix={TOOLCHAIN_OUTPUT_MOUNT_RELATIVE}",
        "--label",
        f"launch-claim={TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        expected_packet_volume,
        "-v",
        expected_output_volume,
        TOOLCHAIN_IMAGE,
        "python",
        "-I",
        "-c",
        hf_llama_cpp_toolchain_bootstrap_payload(),
        str(tree),
        str(runner_sha256),
        str(manifest_sha256),
        TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    if (
        contract.get("schema_version") != 1
        or contract.get("kind")
        != "scriber_hf_llama_cpp_toolchain_launch_contract"
        or contract.get("attempt") != TOOLCHAIN_ATTEMPT
        or not isinstance(contract.get("source_git_head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(contract["source_git_head"])) is None
        or contract.get("image") != TOOLCHAIN_IMAGE
        or contract.get("flavor") != TOOLCHAIN_FLAVOR
        or contract.get("timeout") != f"{TOOLCHAIN_TIMEOUT_MINUTES}m"
        or packet_uri != canonical_packet_uri
        or contract.get("packet_volume") != expected_packet_volume
        or contract.get("output_volume") != expected_output_volume
        or contract.get("output_mount_uri") != TOOLCHAIN_OUTPUT_MOUNT_URI
        or contract.get("output_root") != TOOLCHAIN_OUTPUT_ROOT
        or contract.get("staging_root") != TOOLCHAIN_STAGING_ROOT
        or contract.get("remote_result_uri") != TOOLCHAIN_REMOTE_OUTPUT_URI
        or contract.get("remote_output_absence_required") is not True
        or contract.get("external_job_started") is not False
        or contract.get("publication_allowed") is not False
        or contract.get("secret_names") != []
        or contract.get("fresh_campaign_inventory_required") is not True
        or contract.get("launch_claim_placeholder")
        != TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER
        or argv != expected_argv
    ):
        raise HfLaunchGateError("toolchain v19 launch identity differs")
    if argv.count(TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER) != 1:
        raise HfLaunchGateError("toolchain launch owner placeholder count differs")
    if sum(TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER in item for item in argv) != 2:
        raise HfLaunchGateError("toolchain launch claim is not bound in command and label")
    try:
        maximum = Decimal(str(contract["maximum_authorized_new_spend_usd"]))
        actual = Decimal(str(contract["post_authorization_actual_spend_usd"]))
        job_maximum = Decimal(str(contract["maximum_job_cost_usd"]))
    except (KeyError, InvalidOperation) as error:
        raise HfLaunchGateError("toolchain launch budget is invalid") from error
    if (
        actual < POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD
        or job_maximum != TOOLCHAIN_MAXIMUM_COST_USD
        or maximum != actual + job_maximum + QUANTIZATION_RESERVED_COST_USD
        or maximum >= REMAINING_HF_CREDITS_AUTHORIZATION_USD
    ):
        raise HfLaunchGateError("toolchain launch exceeds the remaining-credit cap")
    expires_raw = contract.get("actual_cost_evidence_expires_at_utc")
    if not isinstance(expires_raw, str):
        raise HfLaunchGateError("toolchain launch has no cost-evidence expiry")
    try:
        expires = datetime.strptime(expires_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise HfLaunchGateError("toolchain cost-evidence expiry is invalid") from error
    clock = (now_utc or datetime.now(UTC)).astimezone(UTC)
    if expires <= clock:
        raise HfLaunchGateError("toolchain actual-cost evidence has expired")
    if not isinstance(tree, str) or _HASH.fullmatch(tree) is None:
        raise HfLaunchGateError("toolchain packet tree is invalid")
    if not isinstance(runner_sha256, str) or _HASH.fullmatch(runner_sha256) is None:
        raise HfLaunchGateError("toolchain runner hash is invalid")
    _required_hash(manifest_sha256, "toolchain manifest hash")
    if not accounted_job_ids or not actual_cost_evidence_sha256:
        raise HfLaunchGateError("toolchain cost inventory binding is missing")
    return contract


def validate_quantization_v2_launch_contract(
    value: Mapping[str, object],
    *,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Validate the complete reviewed quantization submission argv exactly."""

    contract = dict(_mapping(value, "quantization v2 launch contract"))
    argv = contract.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise HfLaunchGateError("quantization launch argv must be a string array")
    tree = contract.get("packet_tree_sha256")
    runner = contract.get("runner_sha256")
    manifest_sha256 = contract.get("manifest_sha256")
    accounted_job_ids = _accounted_job_ids(contract)
    actual_cost_evidence_sha256 = _required_hash(
        contract.get("actual_cost_evidence_sha256"),
        "quantization actual-cost evidence hash",
    )
    packet_uri = contract.get("packet_remote_uri")
    model_uri = contract.get("model_remote_uri")
    llama_uri = contract.get("llama_cpp_remote_uri")
    baseline_uri = contract.get("bf16_baseline_remote_uri")
    packet_uri = _canonical_private_uri(packet_uri, "quantization packet")
    model_uri = _canonical_private_uri(model_uri, "full model")
    llama_uri = _canonical_private_uri(llama_uri, "llama.cpp toolchain")
    baseline_uri = _canonical_private_uri(baseline_uri, "BF16 baseline")
    if (
        model_uri != FULL_MODEL_SOURCE_REMOTE_URI
        or llama_uri != TOOLCHAIN_REMOTE_OUTPUT_URI
    ):
        raise HfLaunchGateError("quantization launch immutable model or toolchain URI differs")
    try:
        validate_no_writable_read_only_uri_overlap(
            writable_uri=QUANTIZATION_OUTPUT_MOUNT_URI,
            read_only_uris=(packet_uri, model_uri, llama_uri, baseline_uri),
        )
    except RuntimeError as error:
        raise HfLaunchGateError("quantization input/output URIs overlap") from error
    packet_volume = f"{packet_uri}:/input/repo:ro"
    model_volume = f"{model_uri}:/final-model:ro"
    llama_volume = f"{llama_uri}:/llama-cpp:ro"
    baseline_volume = f"{baseline_uri}:/bf16-baseline:ro"
    output_volume = f"{QUANTIZATION_OUTPUT_MOUNT_URI}:/outputs:rw"
    expected_argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        QUANTIZATION_JOB_NAME,
        "--flavor",
        QUANTIZATION_FLAVOR,
        "--timeout",
        f"{QUANTIZATION_TIMEOUT_MINUTES}m",
        "--label",
        "project=scriber-polishing",
        "--label",
        "run=quantization-full-120k-v2",
        "--label",
        QUANTIZATION_CAMPAIGN_LABEL,
        "--label",
        QUANTIZATION_ROLE_LABEL,
        "--label",
        QUANTIZATION_ATTEMPT_LABEL,
        "--label",
        f"job-timeout={QUANTIZATION_TIMEOUT_MINUTES}m",
        "--label",
        f"output-prefix={QUANTIZATION_OUTPUT_RELATIVE.removesuffix('/final')}",
        "--label",
        f"launch-claim={QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        model_volume,
        "-v",
        llama_volume,
        "-v",
        baseline_volume,
        "-v",
        output_volume,
        QUANTIZATION_IMAGE,
        "python",
        "-I",
        "-c",
        hf_quantization_bootstrap_payload(),
        str(tree),
        str(runner),
        str(manifest_sha256),
        QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER,
    ]
    if (
        contract.get("schema_version") != 1
        or contract.get("kind") != "scriber_hf_quantization_launch_contract"
        or contract.get("attempt") != QUANTIZATION_ATTEMPT
        or not isinstance(contract.get("source_git_head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(contract["source_git_head"])) is None
        or contract.get("image") != QUANTIZATION_IMAGE
        or contract.get("flavor") != QUANTIZATION_FLAVOR
        or contract.get("timeout") != f"{QUANTIZATION_TIMEOUT_MINUTES}m"
        or contract.get("packet_volume") != packet_volume
        or contract.get("model_volume") != model_volume
        or contract.get("llama_cpp_volume") != llama_volume
        or contract.get("bf16_baseline_volume") != baseline_volume
        or contract.get("output_volume") != output_volume
        or contract.get("output_root") != QUANTIZATION_OUTPUT_ROOT
        or contract.get("output_mount_uri") != QUANTIZATION_OUTPUT_MOUNT_URI
        or contract.get("remote_result_uri")
        != f"{QUANTIZATION_OUTPUT_BUCKET}/{QUANTIZATION_OUTPUT_RELATIVE}"
        or contract.get("launch_claim_placeholder")
        != QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER
        or contract.get("remote_output_absence_required") is not True
        or contract.get("external_job_started") is not False
        or contract.get("publication_allowed") is not False
        or contract.get("secret_names") != []
        or contract.get("fresh_campaign_inventory_required") is not True
        or argv != expected_argv
    ):
        raise HfLaunchGateError("quantization v2 launch identity differs")
    if not isinstance(tree, str) or _HASH.fullmatch(tree) is None:
        raise HfLaunchGateError("quantization packet tree is invalid")
    if not isinstance(runner, str) or _HASH.fullmatch(runner) is None:
        raise HfLaunchGateError("quantization runner hash is invalid")
    _required_hash(manifest_sha256, "quantization manifest hash")
    if not accounted_job_ids or not actual_cost_evidence_sha256:
        raise HfLaunchGateError("quantization cost inventory binding is missing")
    try:
        maximum = Decimal(str(contract["maximum_authorized_new_spend_usd"]))
        actual = Decimal(str(contract["post_authorization_actual_spend_usd"]))
        job_maximum = Decimal(str(contract["maximum_job_cost_usd"]))
    except (KeyError, InvalidOperation) as error:
        raise HfLaunchGateError("quantization launch budget is invalid") from error
    if (
        actual
        < POST_AUTHORIZATION_ACTUAL_BEFORE_V19_USD + TOOLCHAIN_MAXIMUM_COST_USD
        or job_maximum != QUANTIZATION_RESERVED_COST_USD
        or maximum != actual + job_maximum
        or maximum >= REMAINING_HF_CREDITS_AUTHORIZATION_USD
    ):
        raise HfLaunchGateError("quantization launch exceeds the remaining-credit cap")
    expires_raw = contract.get("actual_cost_evidence_expires_at_utc")
    if not isinstance(expires_raw, str):
        raise HfLaunchGateError("quantization launch has no cost-evidence expiry")
    try:
        expires = datetime.strptime(expires_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError as error:
        raise HfLaunchGateError("quantization cost-evidence expiry is invalid") from error
    clock = (now_utc or datetime.now(UTC)).astimezone(UTC)
    if expires <= clock:
        raise HfLaunchGateError("quantization actual-cost evidence has expired")
    return contract


def build_toolchain_v19_claim(
    contract: Mapping[str, object],
    *,
    claimed_at_utc: datetime,
    campaign_inventory: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    checked = validate_toolchain_v19_launch_contract(
        contract,
        now_utc=claimed_at_utc,
    )
    inventory = validate_authoritative_campaign_inventory(
        campaign_inventory,
        forbidden_attempt=TOOLCHAIN_ATTEMPT,
        accounted_campaign_job_ids=_accounted_job_ids(checked),
    )
    _require_claim_time_inventory_freshness(
        inventory,
        claimed_at_utc=claimed_at_utc,
    )
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_exclusive_launch_claim",
        "campaign": "full-120k-v2-quantized-eval",
        "role": "quantized-eval-toolchain",
        "attempt": TOOLCHAIN_ATTEMPT,
        "source_git_head": checked["source_git_head"],
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "manifest_sha256": checked["manifest_sha256"],
        "actual_cost_evidence_sha256": checked["actual_cost_evidence_sha256"],
        "output_mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
        "remote_result_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "maximum_authorized_new_spend_usd": checked[
            "maximum_authorized_new_spend_usd"
        ],
        "actual_cost_evidence_expires_at_utc": checked[
            "actual_cost_evidence_expires_at_utc"
        ],
        "claimed_at_utc": claimed_at_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "launch_contract_sha256": _sha256(_canonical_bytes(checked)),
        "campaign_inventory_sha256": inventory["inventory_sha256"],
        "campaign_job_ids": inventory["job_ids"],
    }
    owner = hashlib.sha256(_canonical_bytes(claim)).hexdigest()
    return claim, owner


def bind_toolchain_v19_claim_to_argv(
    contract: Mapping[str, object],
    owner_token: str,
) -> list[str]:
    checked = validate_toolchain_v19_launch_contract(contract)
    if _OWNER.fullmatch(owner_token) is None:
        raise HfLaunchGateError("launch-claim owner token is invalid")
    argv = [
        item.replace(TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER, owner_token)
        for item in checked["argv"]
    ]
    if any(TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER in item for item in argv):
        raise HfLaunchGateError("launch-claim placeholder remains after binding")
    if sum(owner_token in item for item in argv) != 2:
        raise HfLaunchGateError("launch-claim owner binding count differs")
    return argv


def build_quantization_v2_claim(
    contract: Mapping[str, object],
    *,
    claimed_at_utc: datetime,
    campaign_inventory: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    checked = validate_quantization_v2_launch_contract(
        contract,
        now_utc=claimed_at_utc,
    )
    inventory = validate_authoritative_campaign_inventory(
        campaign_inventory,
        forbidden_attempt=QUANTIZATION_ATTEMPT,
        accounted_campaign_job_ids=_accounted_job_ids(checked),
    )
    _require_claim_time_inventory_freshness(
        inventory,
        claimed_at_utc=claimed_at_utc,
    )
    claim = {
        "schema_version": 1,
        "kind": "scriber_hf_exclusive_launch_claim",
        "campaign": "full-120k-v2-quantized-eval",
        "role": "quantized-evaluation",
        "attempt": QUANTIZATION_ATTEMPT,
        "source_git_head": checked["source_git_head"],
        "packet_tree_sha256": checked["packet_tree_sha256"],
        "manifest_sha256": checked["manifest_sha256"],
        "actual_cost_evidence_sha256": checked["actual_cost_evidence_sha256"],
        "output_mount_uri": QUANTIZATION_OUTPUT_MOUNT_URI,
        "remote_result_uri": checked["remote_result_uri"],
        "maximum_authorized_new_spend_usd": checked[
            "maximum_authorized_new_spend_usd"
        ],
        "actual_cost_evidence_expires_at_utc": checked[
            "actual_cost_evidence_expires_at_utc"
        ],
        "claimed_at_utc": claimed_at_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "launch_contract_sha256": _sha256(_canonical_bytes(checked)),
        "campaign_inventory_sha256": inventory["inventory_sha256"],
        "campaign_job_ids": inventory["job_ids"],
    }
    owner = hashlib.sha256(_canonical_bytes(claim)).hexdigest()
    return claim, owner


def bind_quantization_v2_claim_to_argv(
    contract: Mapping[str, object],
    owner_token: str,
) -> list[str]:
    checked = validate_quantization_v2_launch_contract(contract)
    if _OWNER.fullmatch(owner_token) is None:
        raise HfLaunchGateError("quantization launch-claim owner token is invalid")
    argv = [
        item.replace(QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER, owner_token)
        for item in checked["argv"]
    ]
    if any(QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER in item for item in argv):
        raise HfLaunchGateError(
            "quantization launch-claim placeholder remains after binding"
        )
    if sum(owner_token in item for item in argv) != 2:
        raise HfLaunchGateError("quantization launch-claim owner binding count differs")
    return argv


def validate_preclaim_remote_state(
    *,
    contract: Mapping[str, object],
    active_jobs: Sequence[Mapping[str, object]],
    output_entries: Sequence[Mapping[str, object]],
    claim_path_exists: bool,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    checked = validate_toolchain_v19_launch_contract(contract, now_utc=now_utc)
    require_no_active_hf_jobs(active_jobs)
    if output_entries:
        raise HfLaunchGateError("v19 output or temporary prefix is already populated")
    if claim_path_exists:
        raise HfLaunchGateError("the exclusive v19 launch claim already exists")
    return checked


def validate_quantization_preclaim_remote_state(
    *,
    contract: Mapping[str, object],
    active_jobs: Sequence[Mapping[str, object]],
    output_entries: Sequence[Mapping[str, object]],
    claim_path_exists: bool,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    checked = validate_quantization_v2_launch_contract(
        contract,
        now_utc=now_utc,
    )
    require_no_active_hf_jobs(active_jobs)
    if output_entries:
        raise HfLaunchGateError(
            "quantization v2 output or staging prefix is already populated"
        )
    if claim_path_exists:
        raise HfLaunchGateError(
            "the exclusive quantization v2 launch claim already exists"
        )
    return checked


def _normalize_submitted_volumes(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise HfLaunchGateError(f"{label} volumes are missing")
    normalized: list[str] = []
    for raw_volume in value:
        if isinstance(raw_volume, str):
            source, separator, suffix = raw_volume.rpartition(":")
            source_uri, mount_separator, mount_path = source.rpartition(":")
            if (
                not separator
                or not mount_separator
                or suffix not in {"ro", "rw"}
                or not mount_path.startswith("/")
                or _canonical_private_uri(source_uri, f"{label} volume")
                != source_uri
            ):
                raise HfLaunchGateError(f"{label} volume is invalid")
            normalized.append(raw_volume)
            continue
        volume_type = _job_value(raw_volume, "type")
        source = _job_value(raw_volume, "source")
        mount_path = _job_value(raw_volume, "mount_path")
        path = _job_value(raw_volume, "path")
        read_only = _job_value(raw_volume, "read_only")
        if (
            volume_type != "bucket"
            or not isinstance(source, str)
            or not isinstance(mount_path, str)
            or not mount_path.startswith("/")
            or not isinstance(read_only, bool)
            or path is _MISSING
            or path is not None and not isinstance(path, str)
        ):
            raise HfLaunchGateError(f"{label} volume is incomplete")
        source_uri = (
            source if source.startswith("hf://buckets/") else f"hf://buckets/{source}"
        )
        if path:
            source_uri += f"/{path}"
        source_uri = _canonical_private_uri(source_uri, f"{label} volume")
        normalized.append(
            f"{source_uri}:{mount_path}:{'ro' if read_only else 'rw'}"
        )
    return normalized


def _normalize_empty_mapping(value: object, label: str) -> dict[str, str]:
    if isinstance(value, Mapping):
        normalized = dict(value)
        if normalized:
            raise HfLaunchGateError(f"{label} must be explicitly empty")
        return {}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes) and not value:
        return {}
    raise HfLaunchGateError(f"{label} is missing or not explicitly empty")


def _normalize_submitted_job(value: object, label: str) -> dict[str, object]:
    def required(field: str, *aliases: str) -> object:
        for candidate in (field, *aliases):
            raw = _job_value(value, candidate)
            if raw is not _MISSING:
                return raw
        raise HfLaunchGateError(f"{label} is missing submitted-job field {field}")

    status = required("status")
    stage = (
        status.get("stage")
        if isinstance(status, Mapping)
        else getattr(status, "stage", None)
    )
    if not stage:
        stage = required("stage")
    owner = required("owner")
    owner_name = owner.get("name") if isinstance(owner, Mapping) else getattr(owner, "name", None)
    command = required("command")
    arguments = required("arguments")
    if (
        not isinstance(command, Sequence)
        or isinstance(command, str | bytes)
        or any(not isinstance(item, str) for item in command)
        or not isinstance(arguments, Sequence)
        or isinstance(arguments, str | bytes)
        or any(not isinstance(item, str) for item in arguments)
    ):
        raise HfLaunchGateError(f"{label} command or arguments are malformed")
    name = required("name")
    timeout = required("timeout")
    job_id = required("id")
    image = required("docker_image", "image")
    flavor = required("flavor")
    if any(
        not isinstance(item, str) or not item
        for item in (name, timeout, job_id, image, flavor, owner_name)
    ):
        raise HfLaunchGateError(f"{label} scalar identity is incomplete")
    normalized_stage = str(stage).upper()
    if normalized_stage not in (_ACTIVE_STAGES | _TERMINAL_STAGES):
        raise HfLaunchGateError(f"{label} stage is ambiguous")
    return {
        "id": job_id,
        "name": name,
        "image": image,
        "flavor": flavor,
        "timeout": timeout,
        "labels": _normalize_job_labels(required("labels"), label),
        "volumes": _normalize_submitted_volumes(required("volumes"), label),
        "command": list(command),
        "arguments": list(arguments),
        "environment": _normalize_empty_mapping(
            required("environment"),
            f"{label} environment",
        ),
        "secrets": _normalize_empty_mapping(
            required("secrets"),
            f"{label} secrets",
        ),
        "owner": owner_name,
        "stage": normalized_stage,
    }


def validate_submitted_job_inspection(
    inspected: object,
    *,
    api_inspected: object,
    expected_job_id: str,
    contract: Mapping[str, object],
    bound_argv: Sequence[str],
) -> dict[str, object]:
    """Bind both complete submitted-job control-plane views to the contract."""

    if _JOB_ID.fullmatch(expected_job_id) is None:
        raise HfLaunchGateError("submitted job ID is invalid")
    checked = (
        validate_quantization_v2_launch_contract(contract)
        if contract.get("kind") == "scriber_hf_quantization_launch_contract"
        else validate_toolchain_v19_launch_contract(contract)
    )
    image_name = str(checked["image"])
    try:
        image_index = list(bound_argv).index(image_name)
    except ValueError as error:
        raise HfLaunchGateError("bound launch argv has no exact image") from error
    expected_command = list(bound_argv)[image_index + 1 :]
    expected_labels: dict[str, str] = {}
    expected_volumes: list[str] = []
    expected_name: str | None = None
    index = 0
    command_argv = list(bound_argv)
    while index < image_index:
        item = command_argv[index]
        if item == "--name":
            expected_name = command_argv[index + 1]
            expected_labels["name"] = expected_name
            index += 2
            continue
        if item == "--label":
            raw_label = command_argv[index + 1]
            key, separator, label_value = raw_label.partition("=")
            if not separator or key in expected_labels:
                raise HfLaunchGateError("bound launch labels are invalid")
            expected_labels[key] = label_value
            index += 2
            continue
        if item == "-v":
            expected_volumes.append(command_argv[index + 1])
            index += 2
            continue
        index += 1
    if expected_name is None:
        raise HfLaunchGateError("bound launch argv has no exact job name")
    cli_snapshot = _normalize_submitted_job(inspected, "HF CLI inspection")
    api_snapshot = _normalize_submitted_job(api_inspected, "HF API inspection")
    if cli_snapshot != api_snapshot:
        raise HfLaunchGateError(
            "HF API and CLI submitted-job inspections differ"
        )
    expected_snapshot = {
        "id": expected_job_id,
        "name": expected_name,
        "image": image_name,
        "flavor": str(checked["flavor"]),
        "timeout": checked["timeout"],
        "labels": dict(sorted(expected_labels.items())),
        "volumes": expected_volumes,
        "command": expected_command,
        "arguments": [],
        "environment": {},
        "secrets": {},
        "owner": "Buttermilk03",
    }
    without_stage = {
        key: value for key, value in cli_snapshot.items() if key != "stage"
    }
    if without_stage != expected_snapshot:
        raise HfLaunchGateError(
            "submitted HF job inspection differs from the exact launch contract"
        )
    return {
        "id": expected_job_id,
        "image": image_name,
        "flavor": str(checked["flavor"]),
        "command_sha256": _sha256(_canonical_bytes(expected_command)),
        "stage": cli_snapshot["stage"],
        "owner": cli_snapshot["owner"],
        "name": cli_snapshot["name"],
        "timeout": cli_snapshot["timeout"],
        "labels_sha256": _sha256(_canonical_bytes(expected_labels)),
        "volumes_sha256": _sha256(_canonical_bytes(expected_volumes)),
        "api_cli_identity_sha256": _sha256(_canonical_bytes(cli_snapshot)),
        "exact_identity_verified": True,
    }


def parse_detached_job_id(payload: str) -> str:
    matches = _JOB_ID.findall(payload.lower())
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise HfLaunchGateError("detached HF launch did not return exactly one job ID")
    return unique[0]


def load_canonical_launch_contract(path: str | Path) -> dict[str, object]:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise HfLaunchGateError("launch contract must be a regular file")
    payload = candidate.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HfLaunchGateError(f"launch contract is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise HfLaunchGateError("launch contract must contain an object")
    return value
