from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.evaluation_io import load_evaluation_references
from scriber_polishing.hf_final_evaluation import (
    _load_core_manifest,
    claim_final_evaluation_launch,
    release_final_evaluation_writer,
)
from scriber_polishing.hf_final_evaluation_amendment import (
    ACTUAL_BEFORE_EVALUATION_USD,
    AMENDMENT_LEDGER_SIBLING,
    AMENDMENT_MAXIMUM_COST_USD,
    MAXIMUM_TOTAL_COST_USD,
    OUTPUT_ROOT,
    PRIMARY_MAXIMUM_COST_USD,
    RUNTIME_COMPLETION_SIBLING,
    WRITER_LOCK_SIBLING,
    FinalEvaluationAmendmentError,
    _expected_preflight_reports,
    amendment_runner_payload,
    build_amendment_authorization_evidence,
    build_hf_final_evaluation_amendment_launch_contract,
    build_hf_final_evaluation_amendment_packet,
    build_terminal_predecessor_evidence,
    claim_final_evaluation_amendment,
    validate_amendment_authorization,
    validate_partial_output_snapshot,
    validate_terminal_predecessor,
    verify_hf_final_evaluation_amendment_packet,
)
from scriber_polishing.inference import _canonical_json

_PROJECT = Path(__file__).parents[1]
_PRIMARY_PACKET = _PROJECT / "artifacts/cloud/hf-final-evaluation-attempt14-job-packet"
_PRIMARY_TREE = "sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"
_PREDECESSOR_JOB_ID = "a" * 24


def _hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _authorization() -> dict[str, object]:
    return build_amendment_authorization_evidence("du hast 5 usd budget")


def _predecessor(
    *,
    status: str = "CANCELED",
    reason: str = "operator_performance_recovery",
) -> dict[str, object]:
    value = build_terminal_predecessor_evidence(
        job_id=_PREDECESSOR_JOB_ID,
        last_observed_running_seconds=2810,
        packet_tree_sha256=_PRIMARY_TREE,
    )
    value["terminal_status"] = status
    value["termination_reason"] = reason
    return value


def _copy_primary_output_evidence(root: Path, core: dict[str, object]) -> None:
    project = _PRIMARY_PACKET / "repo/ml/scriber_polishing"
    source_map = {
        "final-evaluation-manifest.json": project / "job-input/final-evaluation-manifest.json",
        "job-manifest.json": _PRIMARY_PACKET / "job-manifest.json",
        "hf-cost-audit.json": project / "job-input/hf-cost-audit.json",
        "continuation-result.json": project / "job-input/continuation-result.json",
        "training-report.json": project / "job-input/training-report.json",
    }
    if isinstance(core["continuation_evidence"].get("recovery_chain"), dict):
        for name in (
            "attempt9-finalization-source-contract.json",
            "attempt9-finalization-verification.json",
            "finalization-launch-guard.json",
        ):
            source_map[name] = project / "job-input" / name
    else:
        source_map["continuation-manifest.json"] = project / "job-input/continuation-manifest.json"
        source_map["continuation-packet-verification.json"] = (
            project / "job-input/continuation-packet-verification.json"
        )
    for name, source in source_map.items():
        (root / name).write_bytes(source.read_bytes())
    for name, report in _expected_preflight_reports(core).items():
        (root / name).write_bytes(canonical_json_bytes(report))


def _journal(
    *,
    core: dict[str, object],
    references_path: Path,
    count: int = 3,
) -> dict[str, Any]:
    references = load_evaluation_references(references_path)
    model = core["final_model"]
    model_binding: dict[str, object] = {
        "kind": "local_tree",
        "model_reference": "/workspace/scriber-1100-final-evaluation/final-model",
        "resolved_path": "/workspace/scriber-1100-final-evaluation/final-model",
        "requested_revision": None,
        "resolved_revision": None,
        "tree_sha256": model["final_model_tree_sha256"],
        "config_sha256": "sha256:" + "2" * 64,
        "file_count": model["final_model_file_count"],
        "total_bytes": model["final_model_total_bytes"],
    }
    model_binding["identity_sha256"] = _hash(_canonical_json(model_binding))
    case_ids = [reference["case_id"] for reference in references]
    policy = model["preprocessing_policy"]
    binding = {
        "reference_sha256": _hash(references_path.read_bytes()),
        "reference_case_ids_sha256": _hash(_canonical_json(case_ids)),
        "reference_count": len(references),
        "candidate": "final",
        "model": model_binding,
        "quantization": "none",
        "device": "cuda",
        "max_new_tokens": 512,
        "batch_size": 8,
        "protection_policy": {
            "policy_id": policy["policy_id"],
            "policy_sha256": policy["policy_sha256"],
            "evidence_sha256": "sha256:" + "3" * 64,
            "source": "explicit_artifact",
            "artifact_sha256": policy["artifact_sha256"],
        },
        "predictions_output_path": f"{OUTPUT_ROOT}/final/ai_gold/predictions.jsonl",
        "manifest_output_path": f"{OUTPUT_ROOT}/final/ai_gold/predictions.manifest.json",
    }
    entries: list[dict[str, object]] = []
    for index, reference in enumerate(references[:count]):
        case_id = str(reference["case_id"])
        entry: dict[str, object] = {
            "index": index,
            "case_id": case_id,
            "prediction": {
                "schema_version": 1,
                "case_id": case_id,
                "prediction_sst": "[DOC]\n[P]Synthetic test output.[/P]\n[/DOC]",
            },
            "evidence": {
                "case_id": case_id,
                "valid_sst": True,
                "restoration_error": False,
                "generation_seconds": 0.5,
                "input_tokens": 10,
                "output_tokens": 10,
            },
        }
        entry["entry_sha256"] = _hash(_canonical_json(entry))
        entries.append(entry)
    return {
        "schema_version": 1,
        "kind": "scriber_batch_inference_resume_journal",
        "status": "in_progress",
        "binding_sha256": _hash(_canonical_json(binding)),
        "binding": binding,
        "entries": entries,
    }


def _partial_output(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    manifest_path = _PRIMARY_PACKET / "repo/ml/scriber_polishing/job-input/final-evaluation-manifest.json"
    _, core = _load_core_manifest(manifest_path)
    root = tmp_path / "attempt8/scriber-1100-final-evaluation-v1"
    launch_id = hashlib.sha256(_PREDECESSOR_JOB_ID.encode("utf-8")).hexdigest()[:32]
    claim_final_evaluation_launch(
        manifest_path=manifest_path,
        output_root=root,
        launch_id=launch_id,
        owner_token="b" * 32,
    )
    release_final_evaluation_writer(
        output_root=root,
        launch_id=launch_id,
        owner_token="b" * 32,
    )
    _copy_primary_output_evidence(root, core)
    lane = root / "final/ai_gold"
    lane.mkdir(parents=True)
    references = (
        _PRIMARY_PACKET
        / "repo/ml/scriber_polishing/evaluation-input"
        / str(core["references"]["ai_gold"]["records_filename"])
    )
    (lane / "predictions.resume.json").write_bytes(
        canonical_json_bytes(_journal(core=core, references_path=references))
    )
    return root, manifest_path, core


def test_amendment_accepts_only_the_narrow_authorization_and_terminal_pairs() -> None:
    assert validate_amendment_authorization(_authorization())["authorized"] is True
    assert (
        validate_terminal_predecessor(
            _predecessor(),
            expected_packet_tree_sha256=_PRIMARY_TREE,
        )["terminal_status"]
        == "CANCELED"
    )
    too_broad = _authorization()
    too_broad["maximum_additional_jobs"] = 2
    with pytest.raises(FinalEvaluationAmendmentError, match="broader"):
        validate_amendment_authorization(too_broad)
    with pytest.raises(FinalEvaluationAmendmentError, match="terminal predecessor"):
        validate_terminal_predecessor(
            _predecessor(status="CANCELED", reason="timeout"),
            expected_packet_tree_sha256=_PRIMARY_TREE,
        )
    with pytest.raises(FinalEvaluationAmendmentError, match="terminal predecessor"):
        validate_terminal_predecessor(
            _predecessor(status="CANCELLED"),
            expected_packet_tree_sha256=_PRIMARY_TREE,
        )


def test_amendment_snapshot_rejects_lock_journal_tamper_and_zero_progress(
    tmp_path: Path,
) -> None:
    root, _manifest_path, _core = _partial_output(tmp_path)
    predecessor = _predecessor()
    snapshot = validate_partial_output_snapshot(
        output_root=root,
        primary_packet_root=_PRIMARY_PACKET,
        primary_manifest=json.loads((_PRIMARY_PACKET / "job-manifest.json").read_text(encoding="utf-8")),
        terminal_predecessor=predecessor,
    )
    assert snapshot["durable_prediction_records"] == 3
    assert snapshot["lanes"][0]["state"] == "journal"
    lock = root.parent / WRITER_LOCK_SIBLING
    lock.mkdir()
    with pytest.raises(FinalEvaluationAmendmentError, match="writer lock"):
        validate_partial_output_snapshot(
            output_root=root,
            primary_packet_root=_PRIMARY_PACKET,
            primary_manifest=json.loads((_PRIMARY_PACKET / "job-manifest.json").read_text(encoding="utf-8")),
            terminal_predecessor=predecessor,
        )
    lock.rmdir()
    journal_path = root / "final/ai_gold/predictions.resume.json"
    original = journal_path.read_bytes()
    journal = json.loads(original)
    journal["binding"]["max_new_tokens"] = 256
    journal["binding_sha256"] = _hash(_canonical_json(journal["binding"]))
    journal_path.write_bytes(canonical_json_bytes(journal))
    with pytest.raises(FinalEvaluationAmendmentError, match="request binding changed"):
        validate_partial_output_snapshot(
            output_root=root,
            primary_packet_root=_PRIMARY_PACKET,
            primary_manifest=json.loads((_PRIMARY_PACKET / "job-manifest.json").read_text(encoding="utf-8")),
            terminal_predecessor=predecessor,
        )
    journal_path.write_bytes(original)
    journal = json.loads(original)
    journal["entries"] = []
    journal_path.write_bytes(canonical_json_bytes(journal))
    with pytest.raises(FinalEvaluationAmendmentError, match="restart.*zero"):
        validate_partial_output_snapshot(
            output_root=root,
            primary_packet_root=_PRIMARY_PACKET,
            primary_manifest=json.loads((_PRIMARY_PACKET / "job-manifest.json").read_text(encoding="utf-8")),
            terminal_predecessor=predecessor,
        )


def test_amendment_packet_launch_and_job_bound_v2_ledger_preserve_primary_bytes(
    tmp_path: Path,
) -> None:
    root, manifest_path, _core = _partial_output(tmp_path)
    predecessor_path = tmp_path / "terminal-predecessor.json"
    predecessor_path.write_bytes(canonical_json_bytes(_predecessor()))
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_bytes(canonical_json_bytes(_authorization()))
    packet_dir = tmp_path / "amendment-packet"
    primary_job_before = (_PRIMARY_PACKET / "job-manifest.json").read_bytes()
    primary_core_before = manifest_path.read_bytes()
    v1_before = (root / "final-evaluation-launch-ledger.json").read_bytes()
    packet = build_hf_final_evaluation_amendment_packet(
        primary_packet_root=_PRIMARY_PACKET,
        expected_primary_packet_tree_sha256=_PRIMARY_TREE,
        output_snapshot_root=root,
        terminal_predecessor_path=predecessor_path,
        authorization_path=authorization_path,
        output_dir=packet_dir,
    )
    verified = verify_hf_final_evaluation_amendment_packet(
        packet.output_dir,
        primary_packet_root=_PRIMARY_PACKET,
        expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
    )
    launch = build_hf_final_evaluation_amendment_launch_contract(
        packet.output_dir,
        primary_packet_root=_PRIMARY_PACKET,
    )
    assert launch["timeout"] == "180m"
    assert launch["maximum_job_cost_usd"] == AMENDMENT_MAXIMUM_COST_USD
    assert launch["maximum_total_cost_usd"] == MAXIMUM_TOTAL_COST_USD
    assert launch["external_job_started"] is False
    assert launch["argv"][launch["argv"].index("--timeout") + 1] == "180m"
    assert verified["budget"]["actual_before_evaluation_usd"] == ACTUAL_BEFORE_EVALUATION_USD
    runner = (packet.output_dir / "run-final-evaluation-amendment.sh").read_text(encoding="utf-8")
    assert 'cp "$AMENDMENT_PROJECT/src/scriber_polishing/inference.py"' in runner
    assert "--mode claim-launch" in runner
    assert (_PRIMARY_PACKET / "job-manifest.json").read_bytes() == primary_job_before
    assert manifest_path.read_bytes() == primary_core_before

    current_job_id = "c" * 24
    current_launch_id = hashlib.sha256(current_job_id.encode("utf-8")).hexdigest()[:32]
    claim = claim_final_evaluation_amendment(
        packet_root=packet.output_dir,
        primary_packet_root=_PRIMARY_PACKET,
        expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
        output_root=root,
        job_id=current_job_id,
        launch_id=current_launch_id,
        owner_token="d" * 32,
    )
    assert claim["maximum_total_cost_usd"] == MAXIMUM_TOTAL_COST_USD
    ledger_path = root.parent / AMENDMENT_LEDGER_SIBLING
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["evaluation_launches"][0]["maximum_cost_usd"] == PRIMARY_MAXIMUM_COST_USD
    assert ledger["evaluation_launches"][1] == {
        "sequence": 2,
        "role": "amendment",
        "job_id": current_job_id,
        "launch_id": current_launch_id,
        "claim_status": "running",
        "flavor": "l4x1",
        "timeout_minutes": 180,
        "maximum_cost_usd": "2.400",
        "packet_tree_sha256": packet.manifest["packet_tree_sha256"],
    }
    assert (root / "final-evaluation-launch-ledger.json").read_bytes() == v1_before
    assert not (root / AMENDMENT_LEDGER_SIBLING).exists()
    assert not (root / RUNTIME_COMPLETION_SIBLING).exists()
    with pytest.raises(FinalEvaluationAmendmentError, match="already exists"):
        claim_final_evaluation_amendment(
            packet_root=packet.output_dir,
            primary_packet_root=_PRIMARY_PACKET,
            expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
            output_root=root,
            job_id="e" * 24,
            launch_id=hashlib.sha256(("e" * 24).encode("utf-8")).hexdigest()[:32],
            owner_token="f" * 32,
        )
    release_final_evaluation_writer(
        output_root=root,
        launch_id=current_launch_id,
        owner_token="d" * 32,
    )


def test_amendment_runner_rejects_an_unknown_primary_runner_anchor() -> None:
    primary = (_PRIMARY_PACKET / "run-final-evaluation.sh").read_bytes()
    changed = primary.replace(b"PACKET_ROOT=/input/repo", b"PACKET_ROOT=/changed", 1)
    with pytest.raises(FinalEvaluationAmendmentError, match="anchor changed"):
        amendment_runner_payload(changed)
