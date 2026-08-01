from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hf_final_evaluation import _write_final_evaluation_core_manifest
from test_inference_resume import _model_fixture, _reference, _result, _write_references

import scriber_polishing.hf_final_evaluation_retry as retry_module
from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.evaluation_io import load_candidate_predictions
from scriber_polishing.hf_final_evaluation import (
    FinalEvaluationError,
    bind_final_evaluation_prediction_manifest,
    record_final_evaluation_exit,
    release_final_evaluation_writer,
    verify_final_evaluation_lane,
)
from scriber_polishing.hf_final_evaluation_retry import (
    FAILED_AMENDMENT_ACTUAL_COST_USD,
    FAILED_AMENDMENT_JOB_ID,
    MAXIMUM_TOTAL_COST_USD,
    PRIMARY_JOB_ID,
    REMAINING_BUDGET_USD,
    RETRY_LEDGER_SIBLING,
    RETRY_RUNTIME_COMPLETION_SIBLING,
    WRITER_LOCK_SIBLING,
    FinalEvaluationRetryError,
    build_failed_amendment_terminal_evidence,
    build_hf_final_evaluation_retry_launch_contract,
    build_hf_final_evaluation_retry_packet,
    build_no_active_jobs_evidence,
    build_retry_authorization_evidence,
    build_stale_writer_lock_observation,
    claim_final_evaluation_retry,
    retry_runner_payload,
    validate_failed_amendment_terminal_evidence,
    validate_retry_authorization,
    verify_hf_final_evaluation_retry_packet,
)
from scriber_polishing.hf_final_evaluation_v4 import (
    FinalEvaluationV4Error,
    validate_stale_resume_journal_candidate,
)
from scriber_polishing.inference import (
    _canonical_json,
    _journal_entry,
    build_batch_resume_binding,
    validate_batch_resume_journal,
    write_resumable_batch_predictions,
)
from scriber_polishing.protected_spans import legacy_policy_evidence

_PROJECT = Path(__file__).parents[1]
_PRIMARY_PACKET = _PROJECT / "artifacts/cloud/hf-final-evaluation-attempt14-job-packet"
_AMENDMENT_PACKET = _PROJECT / "artifacts/cloud/hf-final-evaluation-amendment-v2-job-packet"
_SNAPSHOT = _PROJECT / "artifacts/cloud/hf-final-evaluation-attempt14-partial-canceled"
_PRIMARY_TREE = "sha256:4d52adac816ea8cd2899c9690be2aaba78f06f07ecf5f3acd2ea5f78964786df"
_AMENDMENT_TREE = "sha256:432835f59848555349fdf921a33b56a6c35d6beccf91b6f89c53e289ac4cd365"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _owner(token: str = "b" * 32) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_hf_final_evaluation_writer_owner",
        "launch_id": hashlib.sha256(PRIMARY_JOB_ID.encode("utf-8")).hexdigest()[:32],
        "owner_token": token,
    }


def _evidence(
    tmp_path: Path,
    *,
    owner_path: Path,
) -> tuple[Path, Path, Path, Path]:
    amendment_manifest = json.loads((_AMENDMENT_PACKET / "job-manifest.json").read_text(encoding="utf-8"))
    failed = build_failed_amendment_terminal_evidence(
        observed_at_utc="2026-07-30T21:43:30.513Z",
        primary_packet_tree_sha256=_PRIMARY_TREE,
        amendment_packet_tree_sha256=_AMENDMENT_TREE,
    )
    no_active = build_no_active_jobs_evidence(observed_at_utc="2026-07-30T21:42:06.771Z")
    authorization = build_retry_authorization_evidence("Egal Fang an zur Not mit mehr Budget")
    stale = build_stale_writer_lock_observation(
        owner_path=owner_path,
        expected_owner_sha256=_sha256(owner_path.read_bytes()),
        observed_at_utc="2026-07-30T21:42:06.771Z",
        primary_terminal_evidence=amendment_manifest["terminal_predecessor"],
        failed_amendment_evidence=failed,
        no_active_jobs_evidence=no_active,
        output_snapshot_tree_sha256=amendment_manifest["output"]["snapshot"]["tree_sha256"],
    )
    values = {
        "failed.json": failed,
        "no-active.json": no_active,
        "authorization.json": authorization,
        "stale.json": stale,
    }
    paths: list[Path] = []
    for name, value in values.items():
        path = tmp_path / name
        path.write_bytes(canonical_json_bytes(value))
        paths.append(path)
    return tuple(paths)  # type: ignore[return-value]


def _output_root(tmp_path: Path, name: str) -> tuple[Path, Path]:
    root = tmp_path / name / "attempt8/scriber-1100-final-evaluation-v1"
    shutil.copytree(_SNAPSHOT, root)
    lock = root.parent / WRITER_LOCK_SIBLING
    lock.mkdir()
    owner_path = lock / "owner.json"
    owner_path.write_bytes(canonical_json_bytes(_owner()))
    return root, owner_path


def _completed_v3_lane_with_stale_prefix(
    tmp_path: Path,
    *,
    prefix_count: int = 1744,
) -> tuple[Path, Path, Path, list[dict[str, object]], dict[str, object]]:
    """Reproduce the observed Bucket view without reading frozen holdout content."""

    package_project = tmp_path / "packet/repo/ml/scriber_polishing"
    job_input = package_project / "job-input"
    job_input.mkdir(parents=True)
    core_path, _ = _write_final_evaluation_core_manifest(job_input)
    references = [_reference(f"v3_resume_{index:04d}", f"Quelle {index}") for index in range(1, 1751)]
    references_path = package_project / "evaluation-input/ai_gold_automatic_test_challenge.references.jsonl"
    references_path.parent.mkdir()
    _write_references(references_path, references)

    lane = tmp_path / "output/base_model/ai_gold"
    lane.mkdir(parents=True)
    predictions_path = lane / "predictions.jsonl"
    prediction_manifest_path = lane / "predictions.manifest.json"
    journal_path = lane / "predictions.resume.json"
    model_path = _model_fixture(tmp_path / "base-model")
    binding = build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate="base_model",
        model_reference=str(model_path),
        revision=None,
        tokenizer=SimpleNamespace(),
        model=SimpleNamespace(config=SimpleNamespace()),
        quantization="none",
        device="cuda",
        max_new_tokens=512,
        batch_size=8,
        protection_policy=legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=predictions_path,
        manifest_output=prediction_manifest_path,
    )
    partial_journal = validate_batch_resume_journal(
        {
            "schema_version": 1,
            "kind": "scriber_batch_inference_resume_journal",
            "status": "in_progress",
            "binding_sha256": _sha256(_canonical_json(binding)),
            "binding": binding,
            "entries": [
                _journal_entry(
                    index=index,
                    case_id=str(reference["case_id"]),
                    result=_result(str(reference["source_text"])),
                )
                for index, reference in enumerate(references[:prefix_count])
            ],
        },
        references=references,
        expected_binding=binding,
    )
    partial_payload = _canonical_json(partial_journal)
    journal_path.write_bytes(partial_payload)
    resumed_sources: list[str] = []
    produced_manifest = write_resumable_batch_predictions(
        references,
        predictions_path,
        prediction_manifest_path,
        predictor=_result,
        batch_predictor=lambda sources: (resumed_sources.extend(sources) or [_result(source) for source in sources]),
        batch_size=8,
        candidate="base_model",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields={
            "model": str(model_path),
            "revision": None,
            "reference_sha256": hashlib.sha256(references_path.read_bytes()).hexdigest(),
            "quantization": "none",
            "device": "cuda",
        },
    )
    assert resumed_sources == [f"Quelle {index}" for index in range(prefix_count + 1, 1751)]
    assert not journal_path.exists()
    assert len(load_candidate_predictions(predictions_path)) == 1750
    assert produced_manifest["record_count"] == 1750
    assert produced_manifest["prediction_sha256"] == hashlib.sha256(predictions_path.read_bytes()).hexdigest()

    # Model the exact stale Bucket observation after the in-process resume
    # completed and published the full prediction pair.
    journal_path.write_bytes(partial_payload)
    bound_manifest = bind_final_evaluation_prediction_manifest(
        manifest_path=core_path,
        prediction_manifest_path=prediction_manifest_path,
        candidate="base_model",
        model_root=model_path,
        allow_create=True,
    )
    (lane / "evaluation.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "candidate": "base_model",
                "evaluation_config_sha256": "656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998",
                "reference_sha256": bound_manifest["reference_sha256"],
                "prediction_sha256": bound_manifest["prediction_sha256"],
                "exact_case_coverage": True,
                "metrics": {},
                "gate": {"status": "failed", "passed": False},
            }
        )
    )
    record_final_evaluation_exit(
        candidate="base_model",
        suite_id="ai_gold",
        evaluation_exit=2,
        output_path=lane / "evaluation-exit.json",
    )

    journal_path.unlink()
    assert (
        verify_final_evaluation_lane(
            manifest_path=core_path,
            candidate="base_model",
            suite_id="ai_gold",
            lane_root=lane,
        )["evaluation_exit"]
        == 2
    )
    journal_path.write_bytes(partial_payload)
    return core_path, lane, journal_path, references, binding


def test_retry_evidence_binds_canonical_error_cost_and_narrow_scope() -> None:
    authorization = build_retry_authorization_evidence("Egal Fang an zur Not mit mehr Budget")
    assert validate_retry_authorization(authorization)["budget_increase_used"] is False
    failed = build_failed_amendment_terminal_evidence(
        observed_at_utc="2026-07-30T21:43:30.513Z",
        primary_packet_tree_sha256=_PRIMARY_TREE,
        amendment_packet_tree_sha256=_AMENDMENT_TREE,
    )
    validated = validate_failed_amendment_terminal_evidence(
        failed,
        expected_primary_packet_tree_sha256=_PRIMARY_TREE,
        expected_amendment_packet_tree_sha256=_AMENDMENT_TREE,
    )
    assert validated["job_id"] == FAILED_AMENDMENT_JOB_ID
    assert validated["durations"]["running_seconds"] == 78
    assert validated["billing"]["actual_cost_usd"] == FAILED_AMENDMENT_ACTUAL_COST_USD
    assert validated["prediction_records_written"] == 0
    assert validated["output_mutation_performed"] is False
    with pytest.raises(FinalEvaluationRetryError, match="exact user"):
        build_retry_authorization_evidence("please run anything")


def test_v3_lane_reconciles_exact_1744_prediction_prefix_with_one_six_record_tail(
    tmp_path: Path,
) -> None:
    core_path, lane, journal_path, _references, _binding = _completed_v3_lane_with_stale_prefix(tmp_path)
    journal = json.loads(journal_path.read_bytes())
    predictions = load_candidate_predictions(lane / "predictions.jsonl")

    assert len(journal["entries"]) == 1744
    assert len(predictions) == 1750
    assert [_canonical_json(entry["prediction"]) for entry in journal["entries"]] == [
        _canonical_json(prediction) for prediction in predictions[:1744]
    ]
    assert len(predictions) - len(journal["entries"]) == 6

    evidence = validate_stale_resume_journal_candidate(
        manifest_path=core_path,
        candidate="base_model",
        suite_id="ai_gold",
        lane_root=lane,
    )

    assert evidence["record_count"] == 1750
    assert evidence["journal_prefix_count"] == 1744
    assert evidence["tail_count"] == 6
    assert evidence["evaluation_exit_preserved"] == 2
    assert evidence["safe_to_remove_exact_journal"] is True
    assert journal_path.exists()

    # Simulate only the separately authorized, exact external Bucket deletion.
    journal_path.unlink()
    verified = verify_final_evaluation_lane(
        manifest_path=core_path,
        candidate="base_model",
        suite_id="ai_gold",
        lane_root=lane,
    )
    assert verified["record_count"] == 1750
    assert verified["evaluation_exit"] == 2


def test_v3_lane_never_cleans_a_stale_journal_whose_prediction_prefix_differs(
    tmp_path: Path,
) -> None:
    core_path, lane, journal_path, references, binding = _completed_v3_lane_with_stale_prefix(tmp_path)
    journal = json.loads(journal_path.read_bytes())
    first = journal["entries"][0]
    first["prediction"]["prediction_sst"] = "[DOC]\n[P]Abweichender Prefix.[/P]\n[/DOC]"
    unhashed = {key: value for key, value in first.items() if key != "entry_sha256"}
    first["entry_sha256"] = _sha256(_canonical_json(unhashed))
    journal = validate_batch_resume_journal(
        journal,
        references=references,
        expected_binding=binding,
    )
    tampered_payload = _canonical_json(journal)
    journal_path.write_bytes(tampered_payload)

    with pytest.raises(FinalEvaluationV4Error, match="prefix differs"):
        validate_stale_resume_journal_candidate(
            manifest_path=core_path,
            candidate="base_model",
            suite_id="ai_gold",
            lane_root=lane,
        )

    assert journal_path.read_bytes() == tampered_payload


def test_v3_lane_never_cleans_a_stale_journal_with_more_than_one_batch_tail(
    tmp_path: Path,
) -> None:
    core_path, lane, journal_path, references, binding = _completed_v3_lane_with_stale_prefix(tmp_path)
    journal = json.loads(journal_path.read_bytes())
    journal["entries"] = journal["entries"][:1741]
    journal = validate_batch_resume_journal(
        journal,
        references=references,
        expected_binding=binding,
    )
    unsafe_payload = _canonical_json(journal)
    journal_path.write_bytes(unsafe_payload)
    assert 1750 - len(journal["entries"]) == 9

    with pytest.raises(FinalEvaluationV4Error, match="bounded batch"):
        validate_stale_resume_journal_candidate(
            manifest_path=core_path,
            candidate="base_model",
            suite_id="ai_gold",
            lane_root=lane,
        )

    assert journal_path.read_bytes() == unsafe_payload


def test_retry_packet_recovers_only_exact_stale_owner_then_claims_v3(
    tmp_path: Path,
) -> None:
    build_root, owner_path = _output_root(tmp_path, "build")
    failed_path, no_active_path, authorization_path, stale_path = _evidence(
        tmp_path,
        owner_path=owner_path,
    )
    packet = build_hf_final_evaluation_retry_packet(
        primary_packet_root=_PRIMARY_PACKET,
        amendment_packet_root=_AMENDMENT_PACKET,
        expected_primary_packet_tree_sha256=_PRIMARY_TREE,
        expected_amendment_packet_tree_sha256=_AMENDMENT_TREE,
        output_snapshot_root=build_root,
        failed_amendment_evidence_path=failed_path,
        no_active_jobs_evidence_path=no_active_path,
        stale_writer_lock_observation_path=stale_path,
        stale_owner_path=owner_path,
        authorization_path=authorization_path,
        output_dir=tmp_path / "retry-packet",
    )
    verified = verify_hf_final_evaluation_retry_packet(
        packet.output_dir,
        amendment_packet_root=_AMENDMENT_PACKET,
        primary_packet_root=_PRIMARY_PACKET,
        expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
    )
    launch = build_hf_final_evaluation_retry_launch_contract(
        packet.output_dir,
        amendment_packet_root=_AMENDMENT_PACKET,
        primary_packet_root=_PRIMARY_PACKET,
    )
    assert launch["maximum_total_cost_usd"] == MAXIMUM_TOTAL_COST_USD
    assert launch["remaining_budget_usd"] == REMAINING_BUDGET_USD
    assert launch["external_job_started"] is False
    assert verified["output"]["snapshot"]["durable_prediction_records"] == 1080
    raw_token = str(_owner()["owner_token"]).encode("ascii")
    assert all(raw_token not in path.read_bytes() for path in packet.output_dir.rglob("*") if path.is_file())

    runtime_root, _runtime_owner = _output_root(tmp_path, "runtime")
    current_job_id = "c" * 24
    current_launch_id = hashlib.sha256(current_job_id.encode("utf-8")).hexdigest()[:32]
    claim = claim_final_evaluation_retry(
        packet_root=packet.output_dir,
        amendment_packet_root=_AMENDMENT_PACKET,
        primary_packet_root=_PRIMARY_PACKET,
        expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
        output_root=runtime_root,
        job_id=current_job_id,
        launch_id=current_launch_id,
        owner_token="d" * 32,
    )
    assert claim["stale_writer_recovered"] is True
    ledger_path = runtime_root.parent / RETRY_LEDGER_SIBLING
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["evaluation_launches"][0]["job_id"] == PRIMARY_JOB_ID
    assert ledger["evaluation_launches"][1]["job_id"] == FAILED_AMENDMENT_JOB_ID
    assert ledger["evaluation_launches"][1]["accounted_cost_usd"] == "0.026667"
    assert ledger["evaluation_launches"][2]["job_id"] == current_job_id
    assert ledger["maximum_total_cost_usd"] == MAXIMUM_TOTAL_COST_USD
    assert ledger["writer_recovery"]["owner_token_persisted"] is False
    assert not (runtime_root.parent / RETRY_RUNTIME_COMPLETION_SIBLING).exists()
    release_final_evaluation_writer(
        output_root=runtime_root,
        launch_id=current_launch_id,
        owner_token="d" * 32,
    )


def test_retry_refuses_owner_tamper_without_removing_the_lock(
    tmp_path: Path,
) -> None:
    root, owner_path = _output_root(tmp_path, "tamper")
    failed_path, no_active_path, authorization_path, stale_path = _evidence(
        tmp_path,
        owner_path=owner_path,
    )
    packet = build_hf_final_evaluation_retry_packet(
        primary_packet_root=_PRIMARY_PACKET,
        amendment_packet_root=_AMENDMENT_PACKET,
        expected_primary_packet_tree_sha256=_PRIMARY_TREE,
        expected_amendment_packet_tree_sha256=_AMENDMENT_TREE,
        output_snapshot_root=root,
        failed_amendment_evidence_path=failed_path,
        no_active_jobs_evidence_path=no_active_path,
        stale_writer_lock_observation_path=stale_path,
        stale_owner_path=owner_path,
        authorization_path=authorization_path,
        output_dir=tmp_path / "retry-packet",
    )
    owner_path.write_bytes(canonical_json_bytes(_owner("e" * 32)))
    current_job_id = "f" * 24
    with pytest.raises(FinalEvaluationRetryError, match="exact stale"):
        claim_final_evaluation_retry(
            packet_root=packet.output_dir,
            amendment_packet_root=_AMENDMENT_PACKET,
            primary_packet_root=_PRIMARY_PACKET,
            expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
            output_root=root,
            job_id=current_job_id,
            launch_id=hashlib.sha256(current_job_id.encode("utf-8")).hexdigest()[:32],
            owner_token="1" * 32,
        )
    assert (root.parent / WRITER_LOCK_SIBLING / "owner.json").is_file()
    assert not (root.parent / RETRY_LEDGER_SIBLING).exists()


def test_retry_stays_lock_absent_when_new_claim_fails_after_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, owner_path = _output_root(tmp_path, "claim-failure")
    failed_path, no_active_path, authorization_path, stale_path = _evidence(
        tmp_path,
        owner_path=owner_path,
    )
    packet = build_hf_final_evaluation_retry_packet(
        primary_packet_root=_PRIMARY_PACKET,
        amendment_packet_root=_AMENDMENT_PACKET,
        expected_primary_packet_tree_sha256=_PRIMARY_TREE,
        expected_amendment_packet_tree_sha256=_AMENDMENT_TREE,
        output_snapshot_root=root,
        failed_amendment_evidence_path=failed_path,
        no_active_jobs_evidence_path=no_active_path,
        stale_writer_lock_observation_path=stale_path,
        stale_owner_path=owner_path,
        authorization_path=authorization_path,
        output_dir=tmp_path / "retry-packet",
    )

    def fail_new_claim(*_args: object, **_kwargs: object) -> bool:
        raise FinalEvaluationError("synthetic new-claim failure")

    monkeypatch.setattr(
        retry_module,
        "_acquire_final_evaluation_writer",
        fail_new_claim,
    )
    current_job_id = "2" * 24
    with pytest.raises(FinalEvaluationError, match="synthetic new-claim failure"):
        claim_final_evaluation_retry(
            packet_root=packet.output_dir,
            amendment_packet_root=_AMENDMENT_PACKET,
            primary_packet_root=_PRIMARY_PACKET,
            expected_packet_tree_sha256=str(packet.manifest["packet_tree_sha256"]),
            output_root=root,
            job_id=current_job_id,
            launch_id=hashlib.sha256(current_job_id.encode("utf-8")).hexdigest()[:32],
            owner_token="3" * 32,
        )
    assert not (root.parent / WRITER_LOCK_SIBLING).exists()
    assert not (root.parent / RETRY_LEDGER_SIBLING).exists()


def test_retry_runner_rejects_an_unknown_primary_anchor() -> None:
    primary = (_PRIMARY_PACKET / "run-final-evaluation.sh").read_bytes()
    changed = primary.replace(
        b"PACKET_ROOT=/input/repo",
        b"PACKET_ROOT=/changed",
        1,
    )
    with pytest.raises(FinalEvaluationRetryError, match="anchor"):
        retry_runner_payload(changed)
