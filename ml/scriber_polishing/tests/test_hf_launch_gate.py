from __future__ import annotations

import importlib.util
import subprocess
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scriber_polishing import hf_launch_gate as gate
from scriber_polishing.hf_llama_cpp_toolchain import (
    REVIEWED_SCRIBER_GIT_HEAD,
    TOOLCHAIN_ATTEMPT_LABEL,
    TOOLCHAIN_CAMPAIGN_LABEL,
    TOOLCHAIN_IMAGE,
    TOOLCHAIN_JOB_NAME,
    TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER,
    TOOLCHAIN_OUTPUT_MOUNT_URI,
    TOOLCHAIN_OUTPUT_ROOT,
    TOOLCHAIN_REMOTE_OUTPUT_URI,
    TOOLCHAIN_ROLE_LABEL,
    TOOLCHAIN_STAGING_ROOT,
    hf_llama_cpp_toolchain_bootstrap_payload,
)
from scriber_polishing.hf_quantization_pipeline import (
    FULL_MODEL_BF16_BASELINE_REMOTE_URI,
    FULL_MODEL_SOURCE_REMOTE_URI,
    QUANTIZATION_ATTEMPT_LABEL,
    QUANTIZATION_CAMPAIGN_LABEL,
    QUANTIZATION_IMAGE,
    QUANTIZATION_JOB_NAME,
    QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER,
    QUANTIZATION_OUTPUT_BUCKET,
    QUANTIZATION_OUTPUT_MOUNT_URI,
    QUANTIZATION_OUTPUT_RELATIVE,
    QUANTIZATION_OUTPUT_ROOT,
    QUANTIZATION_ROLE_LABEL,
    hf_quantization_bootstrap_payload,
)


def _contract(now: datetime) -> dict[str, object]:
    placeholder = TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER
    packet_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "packets/toolchain-v19"
    )
    packet_volume = f"{packet_uri}:/input/repo:ro"
    output_volume = f"{TOOLCHAIN_OUTPUT_MOUNT_URI}:/outputs:rw"
    tree = "sha256:" + "a" * 64
    runner = "sha256:" + "b" * 64
    manifest = "sha256:" + "c" * 64
    accounted_ids = ["9" * 24]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_llama_cpp_toolchain_launch_contract",
        "attempt": "v19",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "image": TOOLCHAIN_IMAGE,
        "flavor": "cpu-basic",
        "timeout": "20m",
        "maximum_job_cost_usd": "0.003333",
        "post_authorization_actual_spend_usd": "1.476999",
        "maximum_authorized_new_spend_usd": "5.280332",
        "fresh_campaign_inventory_required": True,
        "actual_cost_evidence_sha256": "sha256:" + "d" * 64,
        "accounted_campaign_job_ids": accounted_ids,
        "accounted_campaign_job_ids_sha256": gate._sha256(
            gate._canonical_bytes(accounted_ids)
        ),
        "actual_cost_evidence_expires_at_utc": (now + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "packet_tree_sha256": tree,
        "runner_sha256": runner,
        "manifest_sha256": manifest,
        "packet_remote_uri": packet_uri,
        "packet_volume": packet_volume,
        "output_volume": output_volume,
        "output_mount_uri": TOOLCHAIN_OUTPUT_MOUNT_URI,
        "output_root": TOOLCHAIN_OUTPUT_ROOT,
        "staging_root": TOOLCHAIN_STAGING_ROOT,
        "remote_result_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "remote_output_absence_required": True,
        "external_job_started": False,
        "publication_allowed": False,
        "secret_names": [],
        "launch_claim_placeholder": placeholder,
        "argv": [
            "hf",
            "jobs",
            "run",
            "--detach",
            "--name",
            TOOLCHAIN_JOB_NAME,
            "--flavor",
            "cpu-basic",
            "--timeout",
            "20m",
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
            "job-timeout=20m",
            "--label",
            f"output-prefix={TOOLCHAIN_OUTPUT_MOUNT_URI.removeprefix('hf://buckets/Buttermilk03/scriber-polishing-private-runs/')}",
            "--label",
            f"launch-claim={placeholder}",
            "-v",
            packet_volume,
            "-v",
            output_volume,
            TOOLCHAIN_IMAGE,
            "python",
            "-I",
            "-c",
            hf_llama_cpp_toolchain_bootstrap_payload(),
            tree,
            runner,
            manifest,
            placeholder,
        ],
    }


def _inventory(now: datetime, forbidden_attempt: str) -> dict[str, object]:
    return gate.build_authoritative_campaign_inventory(
        [
            {
                "id": "9" * 24,
                "docker_image": TOOLCHAIN_IMAGE,
                "flavor": "cpu-basic",
                "labels": {
                    "campaign": "full-120k-v2-quantized-eval",
                    "role": "quantized-eval-toolchain",
                    "attempt": "v18",
                },
                "status": {"stage": "COMPLETED"},
                "owner": {"name": "Buttermilk03"},
            }
        ],
        observed_at_utc=now,
        forbidden_attempt=forbidden_attempt,
        accounted_campaign_job_ids=["9" * 24],
    )


def test_authoritative_preclaim_gate_rejects_active_output_or_existing_claim() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _contract(now)
    assert gate.validate_preclaim_remote_state(
        contract=contract,
        active_jobs=[],
        output_entries=[],
        claim_path_exists=False,
        now_utc=now,
    ) == contract

    with pytest.raises(gate.HfLaunchGateError, match="active"):
        gate.validate_preclaim_remote_state(
            contract=contract,
            active_jobs=[{"id": "1" * 24, "status": {"stage": "SCHEDULING"}}],
            output_entries=[],
            claim_path_exists=False,
            now_utc=now,
        )
    with pytest.raises(gate.HfLaunchGateError, match="populated"):
        gate.validate_preclaim_remote_state(
            contract=contract,
            active_jobs=[],
            output_entries=[{"path": "partial"}],
            claim_path_exists=False,
            now_utc=now,
        )
    with pytest.raises(gate.HfLaunchGateError, match="claim already exists"):
        gate.validate_preclaim_remote_state(
            contract=contract,
            active_jobs=[],
            output_entries=[],
            claim_path_exists=True,
            now_utc=now,
        )


def test_claim_owner_is_content_bound_and_replaces_exactly_two_placeholders() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _contract(now)
    claim, owner = gate.build_toolchain_v19_claim(
        contract,
        claimed_at_utc=now,
        campaign_inventory=_inventory(now, "v19"),
    )

    assert len(owner) == 64
    assert claim["packet_tree_sha256"] == contract["packet_tree_sha256"]
    argv = gate.bind_toolchain_v19_claim_to_argv(contract, owner)
    assert sum(owner in item for item in argv) == 2
    assert all(TOOLCHAIN_LAUNCH_CLAIM_PLACEHOLDER not in item for item in argv)


def test_toolchain_launch_contract_rejects_any_argv_substitution() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _contract(now)
    for mutate in (
        lambda argv: argv.__setitem__(5, "cpu-upgrade"),
        lambda argv: argv.__setitem__(-4, "python3"),
        lambda argv: argv.append("unexpected"),
        lambda argv: argv.__setitem__(21, argv[23]),
    ):
        changed = deepcopy(contract)
        mutate(changed["argv"])
        with pytest.raises(gate.HfLaunchGateError, match="identity differs"):
            gate.validate_toolchain_v19_launch_contract(changed, now_utc=now)


def _quant_contract(now: datetime) -> dict[str, object]:
    placeholder = QUANTIZATION_LAUNCH_CLAIM_PLACEHOLDER
    packet_uri = (
        "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
        "packets/quantization-v2"
    )
    model_uri = FULL_MODEL_SOURCE_REMOTE_URI
    llama_uri = TOOLCHAIN_REMOTE_OUTPUT_URI
    packet_volume = f"{packet_uri}:/input/repo:ro"
    model_volume = f"{model_uri}:/final-model:ro"
    llama_volume = f"{llama_uri}:/llama-cpp:ro"
    baseline_volume = f"{FULL_MODEL_BF16_BASELINE_REMOTE_URI}:/bf16-baseline:ro"
    output_volume = f"{QUANTIZATION_OUTPUT_MOUNT_URI}:/outputs:rw"
    tree = "sha256:" + "c" * 64
    runner = "sha256:" + "d" * 64
    manifest = "sha256:" + "e" * 64
    accounted_ids = ["9" * 24]
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--name",
        QUANTIZATION_JOB_NAME,
        "--flavor",
        "l4x1",
        "--timeout",
        "285m",
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
        "job-timeout=285m",
        "--label",
        f"output-prefix={QUANTIZATION_OUTPUT_RELATIVE.removesuffix('/final')}",
        "--label",
        f"launch-claim={placeholder}",
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
        tree,
        runner,
        manifest,
        placeholder,
    ]
    return {
        "schema_version": 1,
        "kind": "scriber_hf_quantization_launch_contract",
        "attempt": "v2",
        "source_git_head": REVIEWED_SCRIBER_GIT_HEAD,
        "image": QUANTIZATION_IMAGE,
        "flavor": "l4x1",
        "timeout": "285m",
        "maximum_job_cost_usd": "3.800",
        "maximum_total_cost_usd": "5.280332",
        "maximum_authorized_new_spend_usd": "5.280332",
        "post_authorization_actual_spend_usd": "1.480332",
        "fresh_campaign_inventory_required": True,
        "actual_cost_evidence_sha256": "sha256:" + "f" * 64,
        "accounted_campaign_job_ids": accounted_ids,
        "accounted_campaign_job_ids_sha256": gate._sha256(
            gate._canonical_bytes(accounted_ids)
        ),
        "actual_cost_evidence_expires_at_utc": (now + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "packet_tree_sha256": tree,
        "runner_sha256": runner,
        "manifest_sha256": manifest,
        "packet_remote_uri": packet_uri,
        "model_remote_uri": model_uri,
        "llama_cpp_remote_uri": llama_uri,
        "bf16_baseline_remote_uri": FULL_MODEL_BF16_BASELINE_REMOTE_URI,
        "packet_volume": packet_volume,
        "model_volume": model_volume,
        "llama_cpp_volume": llama_volume,
        "bf16_baseline_volume": baseline_volume,
        "output_volume": output_volume,
        "output_root": QUANTIZATION_OUTPUT_ROOT,
        "remote_result_uri": f"{QUANTIZATION_OUTPUT_BUCKET}/{QUANTIZATION_OUTPUT_RELATIVE}",
        "output_mount_uri": QUANTIZATION_OUTPUT_MOUNT_URI,
        "launch_claim_placeholder": placeholder,
        "remote_output_absence_required": True,
        "argv": argv,
        "external_job_started": False,
        "publication_allowed": False,
        "secret_names": [],
    }


def test_quantization_v2_gate_claim_and_exact_inspection() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _quant_contract(now)
    assert gate.validate_quantization_preclaim_remote_state(
        contract=contract,
        active_jobs=[],
        output_entries=[],
        claim_path_exists=False,
        now_utc=now,
    ) == contract
    claim, owner = gate.build_quantization_v2_claim(
        contract,
        claimed_at_utc=now,
        campaign_inventory=_inventory(now, "v2"),
    )
    assert claim["attempt"] == "v2"
    bound = gate.bind_quantization_v2_claim_to_argv(contract, owner)
    image_index = bound.index(QUANTIZATION_IMAGE)
    inspected = SimpleNamespace(
        id="1" * 24,
        name=QUANTIZATION_JOB_NAME,
        docker_image=QUANTIZATION_IMAGE,
        flavor="l4x1",
        timeout="285m",
        labels={
            "name": QUANTIZATION_JOB_NAME,
            "project": "scriber-polishing",
            "run": "quantization-full-120k-v2",
            "campaign": "full-120k-v2-quantized-eval",
            "role": "quantized-evaluation",
            "attempt": "v2",
            "job-timeout": "285m",
            "output-prefix": QUANTIZATION_OUTPUT_RELATIVE.removesuffix("/final"),
            "launch-claim": owner,
        },
        volumes=[
            contract["packet_volume"],
            contract["model_volume"],
            contract["llama_cpp_volume"],
            contract["bf16_baseline_volume"],
            contract["output_volume"],
        ],
        command=bound[image_index + 1 :],
        arguments=[],
        environment={},
        secrets={},
        owner=SimpleNamespace(name="Buttermilk03"),
        status=SimpleNamespace(stage="SCHEDULING"),
    )
    evidence = gate.validate_submitted_job_inspection(
        inspected,
        api_inspected=deepcopy(inspected),
        expected_job_id="1" * 24,
        contract=contract,
        bound_argv=bound,
    )
    assert evidence["exact_identity_verified"] is True

    changed = deepcopy(contract)
    changed["argv"][-4] = "python3"
    with pytest.raises(gate.HfLaunchGateError, match="identity differs"):
        gate.validate_quantization_v2_launch_contract(changed, now_utc=now)
    inspected.command = [*inspected.command, "unexpected"]
    with pytest.raises(gate.HfLaunchGateError, match="inspection differs"):
        gate.validate_submitted_job_inspection(
            inspected,
            api_inspected=deepcopy(inspected),
            expected_job_id="1" * 24,
            contract=contract,
            bound_argv=bound,
        )

    with pytest.raises(gate.HfLaunchGateError, match="active"):
        gate.validate_quantization_preclaim_remote_state(
            contract=contract,
            active_jobs=[{"id": "2" * 24, "stage": "RUNNING"}],
            output_entries=[],
            claim_path_exists=False,
            now_utc=now,
        )
    with pytest.raises(gate.HfLaunchGateError, match="populated"):
        gate.validate_quantization_preclaim_remote_state(
            contract=contract,
            active_jobs=[],
            output_entries=[{"path": ".staging/partial"}],
            claim_path_exists=False,
            now_utc=now,
        )


def test_inventory_blocks_unknown_missing_labels_and_unaccounted_paid_job() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    accounted = ["9" * 24]
    with pytest.raises(gate.HfLaunchGateError, match="active or ambiguous"):
        gate.require_no_active_hf_jobs([{"id": "8" * 24, "stage": "UNKNOWN"}])

    base = {
        "id": accounted[0],
        "docker_image": TOOLCHAIN_IMAGE,
        "flavor": "cpu-basic",
        "labels": {"project": "scriber-polishing"},
        "status": {"stage": "COMPLETED"},
        "owner": {"name": "Buttermilk03"},
    }
    missing_labels = deepcopy(base)
    missing_labels.pop("labels")
    with pytest.raises(gate.HfLaunchGateError, match="missing or malformed labels"):
        gate.build_authoritative_campaign_inventory(
            [missing_labels],
            observed_at_utc=now,
            forbidden_attempt="v2",
            accounted_campaign_job_ids=accounted,
        )

    extra = deepcopy(base)
    extra["id"] = "8" * 24
    extra["labels"] = {
        "campaign": "full-120k-v2-quantized-eval",
        "role": "toolchain-probe",
        "attempt": "v18",
    }
    with pytest.raises(gate.HfLaunchGateError, match="unaccounted campaign job"):
        gate.build_authoritative_campaign_inventory(
            [base, extra],
            observed_at_utc=now,
            forbidden_attempt="v2",
            accounted_campaign_job_ids=accounted,
        )


def test_api_cli_job_inventory_must_reconcile_exactly() -> None:
    cli = [
        {
            "id": "9" * 24,
            "docker_image": TOOLCHAIN_IMAGE,
            "flavor": "cpu-basic",
            "status": {"stage": "COMPLETED"},
            "owner": {"name": "Buttermilk03"},
            "labels": {"project": "scriber-polishing"},
        }
    ]
    api = [
        SimpleNamespace(
            id="9" * 24,
            docker_image=TOOLCHAIN_IMAGE,
            flavor="cpu-basic",
            status=SimpleNamespace(stage="COMPLETED"),
            owner=SimpleNamespace(name="Buttermilk03"),
            labels={"project": "scriber-polishing"},
        )
    ]
    assert gate.reconcile_authoritative_hf_job_snapshots(api, cli) == cli
    api[0].flavor = "l4x1"
    with pytest.raises(gate.HfLaunchGateError, match="snapshots differ"):
        gate.reconcile_authoritative_hf_job_snapshots(api, cli)

    api[0].flavor = "cpu-basic"
    api[0].labels = {"project": "different-campaign"}
    with pytest.raises(gate.HfLaunchGateError, match="snapshots differ"):
        gate.reconcile_authoritative_hf_job_snapshots(api, cli)


def test_quantization_contract_rejects_cost_set_and_uri_aliases() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _quant_contract(now)
    changed = deepcopy(contract)
    changed["accounted_campaign_job_ids_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(gate.HfLaunchGateError, match="job-ID binding"):
        gate.validate_quantization_v2_launch_contract(changed, now_utc=now)

    for alias in (
        contract["packet_remote_uri"] + "/./child",
        str(contract["packet_remote_uri"]).replace("/packets/", "//packets/"),
        contract["packet_remote_uri"] + "/%2e%2e/child",
        contract["packet_remote_uri"] + "/drive:C",
    ):
        changed = deepcopy(contract)
        changed["packet_remote_uri"] = alias
        with pytest.raises(gate.HfLaunchGateError, match="canonical private"):
            gate.validate_quantization_v2_launch_contract(changed, now_utc=now)


def test_submitted_inspection_rejects_every_unbound_control_plane_field() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    contract = _quant_contract(now)
    owner = "a" * 64
    bound = gate.bind_quantization_v2_claim_to_argv(contract, owner)
    image_index = bound.index(QUANTIZATION_IMAGE)
    inspected = {
        "id": "1" * 24,
        "name": QUANTIZATION_JOB_NAME,
        "docker_image": QUANTIZATION_IMAGE,
        "flavor": "l4x1",
        "timeout": "285m",
        "labels": {
            "name": QUANTIZATION_JOB_NAME,
            "project": "scriber-polishing",
            "run": "quantization-full-120k-v2",
            "campaign": "full-120k-v2-quantized-eval",
            "role": "quantized-evaluation",
            "attempt": "v2",
            "job-timeout": "285m",
            "output-prefix": QUANTIZATION_OUTPUT_RELATIVE.removesuffix("/final"),
            "launch-claim": owner,
        },
        "volumes": [
            contract["packet_volume"],
            contract["model_volume"],
            contract["llama_cpp_volume"],
            contract["bf16_baseline_volume"],
            contract["output_volume"],
        ],
        "command": bound[image_index + 1 :],
        "arguments": [],
        "environment": {},
        "secrets": {},
        "owner": {"name": "Buttermilk03"},
        "status": {"stage": "SCHEDULING"},
    }
    gate.validate_submitted_job_inspection(
        inspected,
        api_inspected=deepcopy(inspected),
        expected_job_id="1" * 24,
        contract=contract,
        bound_argv=bound,
    )
    for field, replacement in (
        ("id", "2" * 24),
        ("name", "different-name"),
        ("docker_image", "different/image@sha256:" + "0" * 64),
        ("flavor", "a10g-small"),
        ("timeout", "286m"),
        ("labels", {"project": "scriber-polishing"}),
        ("volumes", inspected["volumes"][:-1]),
        ("command", [*inspected["command"], "extra"]),
        ("arguments", ["extra"]),
        ("environment", {"UNBOUND": "1"}),
        ("secrets", {"UNBOUND": "secret"}),
        ("owner", {"name": "someone-else"}),
    ):
        changed = deepcopy(inspected)
        changed[field] = replacement
        with pytest.raises(gate.HfLaunchGateError):
            gate.validate_submitted_job_inspection(
                changed,
                api_inspected=deepcopy(changed),
                expected_job_id="1" * 24,
                contract=contract,
                bound_argv=bound,
            )

    for missing_field in (
        "id",
        "name",
        "docker_image",
        "flavor",
        "timeout",
        "labels",
        "volumes",
        "command",
        "arguments",
        "environment",
        "secrets",
        "owner",
        "status",
    ):
        incomplete = deepcopy(inspected)
        incomplete.pop(missing_field)
        with pytest.raises(gate.HfLaunchGateError, match="missing"):
            gate.validate_submitted_job_inspection(
                incomplete,
                api_inspected=deepcopy(incomplete),
                expected_job_id="1" * 24,
                contract=contract,
                bound_argv=bound,
            )

    api_changed = deepcopy(inspected)
    api_changed["name"] = "api-only-mismatch"
    with pytest.raises(gate.HfLaunchGateError, match="API and CLI"):
        gate.validate_submitted_job_inspection(
            inspected,
            api_inspected=api_changed,
            expected_job_id="1" * 24,
            contract=contract,
            bound_argv=bound,
        )


def test_reviewed_checkout_binding_is_satisfiable_and_rejects_dirty_state(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "reviewed"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert gate.require_clean_checkout_at_head(
        head,
        repository_root=tmp_path,
        review_base_git_head=head,
    ) == head

    tracked.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(gate.HfLaunchGateError, match="dirty"):
        gate.require_clean_checkout_at_head(
            head,
            repository_root=tmp_path,
            review_base_git_head=head,
        )
    subprocess.run(["git", "checkout", "--", "tracked.txt"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(gate.HfLaunchGateError, match="dirty"):
        gate.require_clean_checkout_at_head(
            head,
            repository_root=tmp_path,
            review_base_git_head=head,
        )


def test_hf_control_plane_dependencies_are_exactly_pinned() -> None:
    assert gate.HF_CLI_PREFIX == (
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


def test_deleted_historical_claim_and_mismatched_commit_readback_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HistoricalApi:
        def repo_exists(self, *_args, **_kwargs):
            return True

        def list_repo_commits(self, *_args, **_kwargs):
            return [SimpleNamespace(commit_id="a" * 40)]

        def list_repo_files(self, *_args, **kwargs):
            return (
                [gate.QUANTIZATION_V2_CLAIM_PATH]
                if kwargs.get("revision") == "a" * 40
                else []
            )

    with pytest.raises(gate.HfLaunchGateError, match="historically"):
        gate.require_claim_never_existed(
            HistoricalApi(),
            repository_id=gate.CLAIM_REPOSITORY_ID,
            repository_type=gate.CLAIM_REPOSITORY_TYPE,
            claim_path=gate.QUANTIZATION_V2_CLAIM_PATH,
            expected_head_sha="a" * 40,
        )

    path = Path(__file__).parents[1] / "scripts" / "launch_hf_quantization_v2.py"
    spec = importlib.util.spec_from_file_location("launch_hf_quantization_readback", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    wrong = tmp_path / "wrong-claim.json"
    wrong.write_bytes(b"not the committed payload\n")
    monkeypatch.setattr(launcher, "hf_hub_download", lambda **_kwargs: str(wrong))

    class ReadbackApi:
        def repo_exists(self, *_args, **_kwargs):
            return True

        def list_repo_commits(self, *_args, **_kwargs):
            return [SimpleNamespace(commit_id="a" * 40)]

        def list_repo_files(self, *_args, **_kwargs):
            return []

        def create_repo(self, *_args, **_kwargs):
            return None

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(private=True, sha="a" * 40)

        def create_commit(self, *_args, **_kwargs):
            return SimpleNamespace(oid="b" * 40)

    with pytest.raises(gate.HfLaunchGateError, match="readback bytes differ"):
        launcher._acquire_claim(
            ReadbackApi(),
            claim={"kind": "test"},
            owner_token="c" * 64,
        )


def test_claim_history_scan_is_bound_to_the_exact_cas_parent() -> None:
    class StableApi:
        def repo_exists(self, *_args, **_kwargs):
            return True

        def list_repo_commits(self, *_args, **_kwargs):
            return [SimpleNamespace(commit_id="a" * 40)]

        def list_repo_files(self, *_args, **_kwargs):
            return []

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(private=True, sha="a" * 40)

    gate.require_claim_never_existed(
        StableApi(),
        repository_id=gate.CLAIM_REPOSITORY_ID,
        repository_type=gate.CLAIM_REPOSITORY_TYPE,
        claim_path=gate.QUANTIZATION_V2_CLAIM_PATH,
        expected_head_sha="a" * 40,
    )

    class AddDeleteRaceApi(StableApi):
        def __init__(self) -> None:
            self.scanned = False

        def list_repo_files(self, *_args, **_kwargs):
            # Simulate another launcher adding and deleting the claim after
            # the listed revision was read. The new DAG head can no longer be
            # the caller's intended CAS parent.
            self.scanned = True
            return []

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(
                private=True,
                sha=("c" if self.scanned else "a") * 40,
            )

    with pytest.raises(gate.HfLaunchGateError, match="changed during"):
        gate.require_claim_never_existed(
            AddDeleteRaceApi(),
            repository_id=gate.CLAIM_REPOSITORY_ID,
            repository_type=gate.CLAIM_REPOSITORY_TYPE,
            claim_path=gate.QUANTIZATION_V2_CLAIM_PATH,
            expected_head_sha="a" * 40,
        )


def _load_launcher_module():
    path = Path(__file__).parents[1] / "scripts" / "launch_hf_llama_cpp_toolchain_v19.py"
    spec = importlib.util.spec_from_file_location("launch_hf_llama_cpp_toolchain_v19", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_two_launchers_racing_for_the_cas_claim_have_exactly_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    barrier = threading.Barrier(2)
    readback = tmp_path / "toolchain-claim.json"
    monkeypatch.setattr(launcher, "hf_hub_download", lambda **_kwargs: str(readback))

    class FakeApi:
        def __init__(self) -> None:
            self.sha = "a" * 40
            self.paths: set[str] = set()
            self.lock = threading.Lock()

        def create_repo(self, *_args, **_kwargs):
            return None

        def repo_exists(self, *_args, **_kwargs):
            return True

        def list_repo_files(self, *_args, **_kwargs):
            if _kwargs.get("revision") == "a" * 40:
                return []
            return sorted(self.paths)

        def list_repo_commits(self, *_args, **_kwargs):
            return [SimpleNamespace(commit_id="a" * 40)]

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(private=True, sha=self.sha)

        def create_commit(self, *_args, **kwargs):
            parent = kwargs["parent_commit"]
            operation = list(kwargs["operations"])[0]
            barrier.wait(timeout=5)
            with self.lock:
                if parent != self.sha:
                    raise RuntimeError("parent commit conflict")
                self.paths.add(operation.path_in_repo)
                operation.path_or_fileobj.seek(0)
                readback.write_bytes(operation.path_or_fileobj.read())
                self.sha = "b" * 40
                return SimpleNamespace(oid=self.sha)

    api = FakeApi()
    outcomes: list[str] = []

    def acquire() -> None:
        try:
            launcher._acquire_claim(
                api,
                repository_id=gate.CLAIM_REPOSITORY_ID,
                claim={"kind": "test"},
                owner_token="c" * 64,
            )
            outcomes.append("winner")
        except gate.HfLaunchGateError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["blocked", "winner"]
    assert api.paths == {gate.TOOLCHAIN_V19_CLAIM_PATH}


def test_two_quantization_launchers_racing_for_cas_have_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (
        Path(__file__).parents[1]
        / "scripts"
        / "launch_hf_quantization_v2.py"
    )
    spec = importlib.util.spec_from_file_location(
        "launch_hf_quantization_v2",
        path,
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    barrier = threading.Barrier(2)
    readback = tmp_path / "quantization-claim.json"
    monkeypatch.setattr(launcher, "hf_hub_download", lambda **_kwargs: str(readback))

    class FakeApi:
        def __init__(self) -> None:
            self.sha = "a" * 40
            self.paths: set[str] = set()
            self.lock = threading.Lock()

        def create_repo(self, *_args, **_kwargs):
            return None

        def repo_exists(self, *_args, **_kwargs):
            return True

        def list_repo_files(self, *_args, **_kwargs):
            if _kwargs.get("revision") == "a" * 40:
                return []
            return sorted(self.paths)

        def list_repo_commits(self, *_args, **_kwargs):
            return [SimpleNamespace(commit_id="a" * 40)]

        def repo_info(self, *_args, **_kwargs):
            return SimpleNamespace(private=True, sha=self.sha)

        def create_commit(self, *_args, **kwargs):
            parent = kwargs["parent_commit"]
            operation = list(kwargs["operations"])[0]
            barrier.wait(timeout=5)
            with self.lock:
                if parent != self.sha:
                    raise RuntimeError("parent commit conflict")
                self.paths.add(operation.path_in_repo)
                operation.path_or_fileobj.seek(0)
                readback.write_bytes(operation.path_or_fileobj.read())
                self.sha = "b" * 40
                return SimpleNamespace(oid=self.sha)

    api = FakeApi()
    outcomes: list[str] = []

    def acquire() -> None:
        try:
            launcher._acquire_claim(
                api,
                claim={"kind": "test"},
                owner_token="c" * 64,
            )
            outcomes.append("winner")
        except gate.HfLaunchGateError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["blocked", "winner"]
    assert api.paths == {gate.QUANTIZATION_V2_CLAIM_PATH}
