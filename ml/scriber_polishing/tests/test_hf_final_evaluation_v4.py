from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_final_evaluation import release_final_evaluation_writer
from scriber_polishing.hf_final_evaluation_amendment import _inventory_tree
from scriber_polishing.hf_final_evaluation_retry import RETRY_LEDGER_SIBLING, _launch_id
from scriber_polishing.hf_final_evaluation_v4 import (
    _ALLOWED_EXTERNAL_JOURNALS,
    EXTERNAL_JOURNAL_ROOT,
    FINAL_AI_GOLD_PREDICTION_SHA256,
    OVERALL_MAXIMUM_USD,
    OVERALL_REMAINING_USD,
    V3_ACTUAL_COST_USD,
    V4_LEDGER_SIBLING,
    V4_RUNTIME_COMPLETION_SIBLING,
    WRITER_LOCK_SIBLING,
    FinalEvaluationV4Error,
    _validate_external_journal_root,
    build_hf_final_evaluation_v4_launch_contract,
    build_v4_external_journal_cleanup_plan,
    claim_final_evaluation_v4,
    v4_runner_payload,
    verify_hf_final_evaluation_v4_packet,
    verify_preserved_final_ai_gold_for_v4,
    verify_v4_external_journal_cleanup,
)

_PROJECT = Path(__file__).parents[1]
_PRIMARY = _PROJECT / "artifacts/cloud/hf-final-evaluation-attempt14-job-packet"
_AMENDMENT = _PROJECT / "artifacts/cloud/hf-final-evaluation-amendment-v2-job-packet"
_RETRY = _PROJECT / "artifacts/cloud/hf-final-evaluation-retry-v3-job-packet"
_V4 = _PROJECT / "artifacts/cloud/hf-final-evaluation-recovery-v4-job-packet"
_POST = _PROJECT / "runs/final-evaluation-v4-post-removal-remote-child"
_V3_LEDGER = _PROJECT / "runs/final-evaluation-v4-inputs/launch-ledger-v3.json"


def _v4_tree() -> str:
    return json.loads((_V4 / "job-manifest.json").read_bytes())["packet_tree_sha256"]


def test_v4_runner_never_repeats_final_ai_gold_and_uses_external_journals() -> None:
    runner = v4_runner_payload((_PRIMARY / "run-final-evaluation.sh").read_bytes())
    script = runner.decode("utf-8")

    assert script.count("preserved-lane") == 2
    assert script.count("predictions.resume.json") == 2
    assert '"$lane_root/predictions.resume.json"' not in script
    assert "recover-unbound" not in script
    assert "hf buckets rm" not in script
    assert "--recursive" not in script
    assert "$EXTERNAL_JOURNAL_ROOT/final/$suite_id/predictions.resume.json" in script
    assert "$EXTERNAL_JOURNAL_ROOT/base_model/$suite_id/predictions.resume.json" in script
    assert 'if [ "$SUITE_ID" = ai_gold ]; then' in script
    assert "V4 forbids final/ai_gold inference and evaluation" in script


def test_v4_packet_and_launch_contract_bind_exact_budget_and_predecessors() -> None:
    manifest = verify_hf_final_evaluation_v4_packet(
        _V4,
        retry_packet_root=_RETRY,
        amendment_packet_root=_AMENDMENT,
        primary_packet_root=_PRIMARY,
        expected_packet_tree_sha256=_v4_tree(),
    )
    launch = build_hf_final_evaluation_v4_launch_contract(
        _V4,
        retry_packet_root=_RETRY,
        amendment_packet_root=_AMENDMENT,
        primary_packet_root=_PRIMARY,
    )

    assert manifest["journal_recovery"]["preserved_lane"]["record_count"] == 1750
    assert launch["v3_actual_cost_usd"] == V3_ACTUAL_COST_USD
    assert launch["overall_maximum_usd"] == OVERALL_MAXIMUM_USD
    assert launch["overall_remaining_usd"] == OVERALL_REMAINING_USD
    assert launch["final_ai_gold_action"] == "verify_only_never_infer_or_evaluate"
    assert launch["external_journal_root"] == EXTERNAL_JOURNAL_ROOT
    assert launch["external_job_started"] is False
    assert launch["argv"][-1] == _v4_tree()


def test_v4_claim_preserves_complete_lane_and_binds_v3_ledger(tmp_path: Path) -> None:
    root = tmp_path / "attempt8/scriber-1100-final-evaluation-v1"
    root.parent.mkdir(parents=True)
    shutil.copytree(_POST, root)
    shutil.copy2(_V3_LEDGER, root.parent / RETRY_LEDGER_SIBLING)
    before = _inventory_tree(root)
    job_id = "a" * 24
    launch_id = _launch_id(job_id)
    owner_token = "c" * 32

    claim = claim_final_evaluation_v4(
        packet_root=_V4,
        retry_packet_root=_RETRY,
        amendment_packet_root=_AMENDMENT,
        primary_packet_root=_PRIMARY,
        expected_packet_tree_sha256=_v4_tree(),
        output_root=root,
        job_id=job_id,
        launch_id=launch_id,
        owner_token=owner_token,
    )
    preserved = verify_preserved_final_ai_gold_for_v4(
        packet_root=_V4,
        retry_packet_root=_RETRY,
        amendment_packet_root=_AMENDMENT,
        primary_packet_root=_PRIMARY,
        expected_packet_tree_sha256=_v4_tree(),
        output_root=root,
    )

    assert claim["writer_acquired"] is True
    assert claim["final_ai_gold_action"] == "verify_only"
    assert _inventory_tree(root) == before
    assert preserved["record_count"] == 1750
    assert preserved["prediction_sha256"] == FINAL_AI_GOLD_PREDICTION_SHA256.removeprefix("sha256:")
    assert not (root / "final/ai_gold/predictions.resume.json").exists()
    assert (root.parent / V4_LEDGER_SIBLING).is_file()
    assert not (root.parent / V4_RUNTIME_COMPLETION_SIBLING).exists()
    assert (root.parent / WRITER_LOCK_SIBLING).is_dir()
    release = release_final_evaluation_writer(
        output_root=root,
        launch_id=launch_id,
        owner_token=owner_token,
    )
    assert release["released"] is True


def test_v4_claim_rejects_preexisting_external_root_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "attempt8/scriber-1100-final-evaluation-v1"
    root.parent.mkdir(parents=True)
    shutil.copytree(_POST, root)
    shutil.copy2(_V3_LEDGER, root.parent / RETRY_LEDGER_SIBLING)
    external = root.parent / ".scriber-1100-final-evaluation-v1.resume-journals-v4"
    external.mkdir()
    before = _inventory_tree(root)
    job_id = "b" * 24

    with pytest.raises(FinalEvaluationV4Error, match="must be absent"):
        claim_final_evaluation_v4(
            packet_root=_V4,
            retry_packet_root=_RETRY,
            amendment_packet_root=_AMENDMENT,
            primary_packet_root=_PRIMARY,
            expected_packet_tree_sha256=_v4_tree(),
            output_root=root,
            job_id=job_id,
            launch_id=_launch_id(job_id),
            owner_token="d" * 32,
        )

    assert _inventory_tree(root) == before
    assert not (root.parent / WRITER_LOCK_SIBLING).exists()
    assert not (root.parent / V4_LEDGER_SIBLING).exists()


def test_v4_external_cleanup_refuses_unexpected_object(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "unexpected.resume.json").write_bytes(b"unexpected")

    with pytest.raises(FinalEvaluationV4Error, match="unexpected object"):
        _validate_external_journal_root(
            external_root=external,
            output_root=tmp_path / "output",
            primary_manifest_path=tmp_path / "manifest.json",
        )


def test_v4_post_terminal_cleanup_plan_is_exact_and_nonrecursive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "attempt8/scriber-1100-final-evaluation-v1"
    root.mkdir(parents=True)
    (root / "final-evaluation-result.json").write_bytes(b"{}")
    external = root.parent / ".scriber-1100-final-evaluation-v1.resume-journals-v4"
    external.mkdir()
    inventory = _inventory_tree(external)
    completion = {
        "status": "runtime_complete_pending_terminal_and_external_journal_cleanup",
        "job_id": "e" * 24,
        "launch_id": _launch_id("e" * 24),
        "v4_packet_tree_sha256": _v4_tree(),
        "external_journals": {
            "inventory": inventory,
            "validated_journals": [],
            "allowed_paths": list(_ALLOWED_EXTERNAL_JOURNALS),
            "unexpected_paths": [],
            "cleanup_required": False,
            "cleanup_method": ("post_terminal_exact_api_listing_hash_validation_and_nonrecursive_delete"),
        },
    }
    completion_path = root.parent / V4_RUNTIME_COMPLETION_SIBLING
    completion_payload = canonical_json_bytes(completion)
    completion_path.write_bytes(completion_payload)
    terminal = {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_v4_terminal_evidence",
        "source": "hugging_face_jobs_rest_api",
        "observed_at_utc": "2026-07-31T00:00:00Z",
        "job_id": completion["job_id"],
        "launch_id": completion["launch_id"],
        "terminal_status": "COMPLETED",
        "v4_packet_tree_sha256": _v4_tree(),
        "runtime_completion_sha256": "sha256:" + hashlib.sha256(completion_payload).hexdigest(),
        "writer_released": True,
    }

    plan = build_v4_external_journal_cleanup_plan(
        output_root=root,
        external_journal_root=external,
        runtime_completion_path=completion_path,
        terminal_evidence=terminal,
    )
    proof = verify_v4_external_journal_cleanup(
        output_root=root,
        external_journal_root=external,
        cleanup_plan=plan,
    )

    assert plan["status"] == "no_cleanup_required"
    assert plan["target_count"] == 0
    assert plan["recursive"] is False
    assert plan["operation"]["api"] == "HfApi.batch_bucket_files"
    assert proof["authoritative_prefix_empty"] is True
    assert proof["recursive_delete_performed"] is False
