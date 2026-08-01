"""Acquire one permanent CAS claim and launch exactly one quantization v2 Job.

This command is deliberately single-shot. Any failed or ambiguous submission
retains its claim and must be diagnosed; it never retries automatically.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_launch_gate import (  # noqa: E402
    CLAIM_REPOSITORY_ID,
    CLAIM_REPOSITORY_TYPE,
    HF_CLI_PREFIX,
    QUANTIZATION_V2_CLAIM_PATH,
    HfLaunchGateError,
    bind_quantization_v2_claim_to_argv,
    build_authoritative_campaign_inventory,
    build_quantization_v2_claim,
    load_canonical_launch_contract,
    parse_detached_job_id,
    parse_hf_bucket_list,
    parse_hf_jobs_json,
    reconcile_authoritative_hf_job_snapshots,
    require_claim_never_existed,
    require_clean_checkout_at_head,
    require_hf_control_plane_runtime,
    require_no_active_hf_jobs,
    validate_quantization_preclaim_remote_state,
    validate_submitted_job_inspection,
)
from scriber_polishing.hf_quantization_pipeline import (  # noqa: E402
    QUANTIZATION_OUTPUT_MOUNT_URI,
)


def _run_cli(arguments: list[str], *, timeout_seconds: int = 90) -> str:
    completed = subprocess.run(
        [*HF_CLI_PREFIX, "hf", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0 or completed.stderr.strip():
        raise HfLaunchGateError(
            "HF CLI command failed closed: "
            + (completed.stderr.strip() or completed.stdout.strip())[-2000:]
        )
    return completed.stdout


def _all_jobs() -> list[dict[str, object]]:
    return parse_hf_jobs_json(
        _run_cli(["jobs", "ps", "-a", "--format", "json"])
    )


def _authoritative_all_jobs(api: HfApi) -> list[dict[str, object]]:
    api_jobs = list(api.list_jobs(namespace="Buttermilk03"))
    return reconcile_authoritative_hf_job_snapshots(api_jobs, _all_jobs())


def _output_entries() -> list[dict[str, object]]:
    return parse_hf_bucket_list(
        _run_cli(
            [
                "buckets",
                "list",
                QUANTIZATION_OUTPUT_MOUNT_URI,
                "-R",
                "--format",
                "json",
            ]
        )
    )


def _inspect_job_exact(
    api: HfApi,
    job_id: str,
) -> tuple[dict[str, object], object, str]:
    try:
        value = json.loads(
            _run_cli(["jobs", "inspect", job_id, "--format", "json"])
        )
    except json.JSONDecodeError as error:
        raise HfLaunchGateError("HF submitted-job inspection is not JSON") from error
    if not isinstance(value, dict):
        raise HfLaunchGateError("HF submitted-job inspection is not an object")
    api_job = api.inspect_job(job_id=job_id)
    return value, api_job, str(getattr(api_job, "url", ""))


def _claim_exists(api: HfApi) -> bool:
    if not api.repo_exists(
        CLAIM_REPOSITORY_ID,
        repo_type=CLAIM_REPOSITORY_TYPE,
    ):
        return False
    return QUANTIZATION_V2_CLAIM_PATH in api.list_repo_files(
        CLAIM_REPOSITORY_ID,
        repo_type=CLAIM_REPOSITORY_TYPE,
    )


def _acquire_claim(
    api: HfApi,
    *,
    claim: dict[str, object],
    owner_token: str,
) -> str:
    api.create_repo(
        CLAIM_REPOSITORY_ID,
        repo_type=CLAIM_REPOSITORY_TYPE,
        private=True,
        exist_ok=True,
    )
    info = api.repo_info(
        CLAIM_REPOSITORY_ID,
        repo_type=CLAIM_REPOSITORY_TYPE,
    )
    if info.private is not True or not info.sha:
        raise HfLaunchGateError(
            "quantization launch-claim repository is not private or has no parent SHA"
        )
    require_claim_never_existed(
        api,
        repository_id=CLAIM_REPOSITORY_ID,
        repository_type=CLAIM_REPOSITORY_TYPE,
        claim_path=QUANTIZATION_V2_CLAIM_PATH,
        expected_head_sha=info.sha,
    )
    payload = (
        json.dumps(
            {**claim, "owner_token": owner_token},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    try:
        commit = api.create_commit(
            CLAIM_REPOSITORY_ID,
            repo_type=CLAIM_REPOSITORY_TYPE,
            parent_commit=info.sha,
            commit_message="Acquire exclusive Scriber quantization v2 launch claim",
            operations=[
                CommitOperationAdd(
                    path_in_repo=QUANTIZATION_V2_CLAIM_PATH,
                    path_or_fileobj=io.BytesIO(payload),
                )
            ],
        )
    except Exception as error:
        raise HfLaunchGateError(
            "exclusive quantization v2 CAS claim failed; refusing submission"
        ) from error
    oid = str(commit.oid)
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise HfLaunchGateError("quantization claim commit OID is invalid")
    try:
        downloaded = Path(
            hf_hub_download(
                repo_id=CLAIM_REPOSITORY_ID,
                filename=QUANTIZATION_V2_CLAIM_PATH,
                repo_type=CLAIM_REPOSITORY_TYPE,
                revision=oid,
                force_download=True,
            )
        )
        if downloaded.is_symlink() or not downloaded.is_file() or downloaded.read_bytes() != payload:
            raise HfLaunchGateError("quantization claim readback bytes differ")
    except HfLaunchGateError:
        raise
    except Exception as error:
        raise HfLaunchGateError(
            "quantization claim commit could not be read back exactly"
        ) from error
    return oid


def _write_receipt(path: Path, value: dict[str, object]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except FileExistsError as error:
        raise HfLaunchGateError(
            f"quantization launch receipt already exists: {destination}"
        ) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch-contract", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)

    require_hf_control_plane_runtime()
    now = datetime.now(UTC)
    contract = load_canonical_launch_contract(args.launch_contract)
    repository_root = PROJECT_ROOT.parents[1]
    require_clean_checkout_at_head(
        contract.get("source_git_head"),
        repository_root=repository_root,
    )
    api = HfApi()
    authoritative_jobs = _authoritative_all_jobs(api)
    inventory = build_authoritative_campaign_inventory(
        authoritative_jobs,
        observed_at_utc=now,
        forbidden_attempt="v2",
        accounted_campaign_job_ids=contract.get("accounted_campaign_job_ids", []),
    )
    checked = validate_quantization_preclaim_remote_state(
        contract=contract,
        active_jobs=authoritative_jobs,
        output_entries=_output_entries(),
        claim_path_exists=_claim_exists(api),
        now_utc=now,
    )
    claim, owner = build_quantization_v2_claim(
        checked,
        claimed_at_utc=now,
        campaign_inventory=inventory,
    )

    renewed_jobs = _authoritative_all_jobs(api)
    validate_quantization_preclaim_remote_state(
        contract=checked,
        active_jobs=renewed_jobs,
        output_entries=_output_entries(),
        claim_path_exists=_claim_exists(api),
        now_utc=datetime.now(UTC),
    )
    require_clean_checkout_at_head(
        checked.get("source_git_head"),
        repository_root=repository_root,
    )
    renewed_inventory = build_authoritative_campaign_inventory(
        renewed_jobs,
        observed_at_utc=datetime.now(UTC),
        forbidden_attempt="v2",
        accounted_campaign_job_ids=checked.get("accounted_campaign_job_ids", []),
    )
    if renewed_inventory["inventory_sha256"] != inventory["inventory_sha256"]:
        raise HfLaunchGateError(
            "authoritative campaign inventory changed before the quantization claim"
        )
    require_clean_checkout_at_head(
        checked.get("source_git_head"),
        repository_root=repository_root,
    )
    claim_commit = _acquire_claim(api, claim=claim, owner_token=owner)

    postclaim_jobs = _authoritative_all_jobs(api)
    require_no_active_hf_jobs(postclaim_jobs)
    postclaim_inventory = build_authoritative_campaign_inventory(
        postclaim_jobs,
        observed_at_utc=datetime.now(UTC),
        forbidden_attempt="v2",
        accounted_campaign_job_ids=checked.get("accounted_campaign_job_ids", []),
    )
    if postclaim_inventory["inventory_sha256"] != inventory["inventory_sha256"]:
        raise HfLaunchGateError(
            "authoritative campaign inventory changed after the quantization claim"
        )
    if _output_entries():
        raise HfLaunchGateError(
            "quantization output changed after claim; claim retained and submission blocked"
        )
    launch_argv = bind_quantization_v2_claim_to_argv(checked, owner)
    require_clean_checkout_at_head(
        checked.get("source_git_head"),
        repository_root=repository_root,
    )
    completed = subprocess.run(
        [*HF_CLI_PREFIX, *launch_argv],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=90,
    )
    combined = "\n".join((completed.stdout, completed.stderr)).strip()
    if completed.returncode != 0:
        raise HfLaunchGateError(
            "HF quantization submission failed or was ambiguous; permanent claim "
            "retained, never retry: "
            + combined[-2000:]
        )
    job_id = parse_detached_job_id(combined)
    inspected, api_inspected, inspected_url = _inspect_job_exact(api, job_id)
    inspection = validate_submitted_job_inspection(
        inspected,
        api_inspected=api_inspected,
        expected_job_id=job_id,
        contract=checked,
        bound_argv=launch_argv,
    )
    receipt = {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_v2_launch_receipt",
        "claim_repository": CLAIM_REPOSITORY_ID,
        "claim_path": QUANTIZATION_V2_CLAIM_PATH,
        "claim_commit": claim_commit,
        "owner_token": owner,
        "job_id": job_id,
        "job_inspection": {**inspection, "url": inspected_url},
        "submitted_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duplicate_retry_allowed": False,
    }
    _write_receipt(args.receipt, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
