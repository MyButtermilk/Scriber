from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scriber_polishing.hf_training_continuation_finalization import (
    ATTEMPT9_FINAL_MODEL_TREE_SHA256,
    ATTEMPT9_JOB_ID,
    ATTEMPT9_RUN_FINGERPRINT,
    ATTEMPT9_SOURCE_URI,
    FINALIZATION_REMOTE_OUTPUT_PATH,
    ContinuationFinalizationError,
    attempt9_finalization_source_contract,
    build_finalization_result,
    build_hf_continuation_finalization_launch_contract,
    build_hf_continuation_finalization_packet,
    canonical_json_bytes,
    finalization_runner_payload,
    normalize_attempt9_training_report,
    verify_final_model_inventory,
    verify_hf_continuation_finalization_packet,
)

_EFFECTIVE_ORDER_SHA256 = "b8a03f8cd657f755c06934aae4169dda4386c8bc31d3eb6fa218ac3faa435c2d"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _tree_sha256(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode())
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode())
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _source_pair() -> tuple[dict[str, object], dict[str, object]]:
    order = {
        "bucket_algorithm": "fixed_window_length_descending_v1",
        "effective_epoch_orders": [
            {
                "epoch": 0,
                "record_count": 4064,
                "filtered_plan_example_ids_sha256": (
                    "625bfc2b54d7403dd41532925795f8e081da7522a45346262a22ba2a9c2fbef3"
                ),
                "effective_example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
            }
        ],
    }
    bindings = {"training_order": order}
    core = {
        "schema_version": 1,
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "bindings": bindings,
    }
    fingerprint = _sha256(canonical_json_bytes(core))
    identity = {**core, "run_fingerprint": fingerprint}
    report = {
        "schema_version": 2,
        "run_fingerprint": fingerprint,
        "run_identity": {"sha256": _sha256(canonical_json_bytes(identity))},
        "bindings": bindings,
        "remediation_order": order,
        "recovery": {
            "training_steps_executed": 0,
            "validation_reexecuted": True,
        },
    }
    return report, identity


def test_attempt9_report_normalizes_both_order_views_without_new_work() -> None:
    report, identity = _source_pair()
    corrected = normalize_attempt9_training_report(
        report,
        run_identity=identity,
        source_training_report_sha256=_sha256(canonical_json_bytes(report)),
        source_job_id=ATTEMPT9_JOB_ID,
    )
    expected = {
        "algorithm": "fixed_window_length_descending_v1",
        "example_ids_sha256": _EFFECTIVE_ORDER_SHA256,
    }
    assert corrected["bindings"]["training_order"]["effective_order_lock"] == expected
    assert corrected["remediation_order"]["effective_order_lock"] == expected
    assert corrected["run_fingerprint"] == report["run_fingerprint"]
    assert corrected["recovery_finalization"]["training_steps_executed"] == 0
    assert corrected["recovery_finalization"]["validation_reexecuted"] is False


def test_attempt9_contract_binds_actual_cost_eval_and_complete_model() -> None:
    contract = attempt9_finalization_source_contract()
    assert contract["source_job_id"] == ATTEMPT9_JOB_ID
    assert contract["source_job_running_seconds"] == 93
    assert contract["source_job_billed_minutes"] == 2
    assert contract["source_job_cost_usd"] == "0.033333"
    assert contract["training_steps_executed"] == 0
    assert contract["validation"]["completed_steps"] == 216
    assert contract["final_model"]["tree_sha256"] == (ATTEMPT9_FINAL_MODEL_TREE_SHA256)
    assert contract["final_model"]["file_count"] == 12
    assert contract["final_model"]["total_bytes"] == 575466744


def test_existing_final_model_is_verified_in_place_without_copy(
    tmp_path: Path,
) -> None:
    final = tmp_path / "final"
    final.mkdir()
    payload = b"already-materialized"
    (final / "model.safetensors").write_bytes(payload)
    files = [
        {
            "path": "model.safetensors",
            "bytes": len(payload),
            "sha256": _sha256(payload),
        }
    ]
    inventory = {
        "schema_version": 1,
        "files": files,
        "file_count": 1,
        "total_bytes": len(payload),
        "tree_sha256": _tree_sha256(files),
    }
    (final / "checkpoint-inventory.json").write_bytes(canonical_json_bytes(inventory))
    result = verify_final_model_inventory(final, inventory=inventory)
    assert result["model_copied"] is False
    assert result["tree_sha256"] == inventory["tree_sha256"]
    (final / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(
        ContinuationFinalizationError,
        match="model.safetensors",
    ):
        verify_final_model_inventory(final, inventory=inventory)


def test_attempt10_packet_is_cpu_only_and_launch_is_not_started(
    tmp_path: Path,
) -> None:
    packet = build_hf_continuation_finalization_packet(output_dir=tmp_path / "packet")
    manifest = packet.manifest
    verification = verify_hf_continuation_finalization_packet(
        packet.output_dir,
        expected_packet_tree_sha256=str(manifest["packet_tree_sha256"]),
        expected_source_contract_sha256=str(manifest["source_contract_sha256"]),
    )
    launch = build_hf_continuation_finalization_launch_contract(packet.output_dir)
    assert manifest["flavor"] == "cpu-basic"
    assert manifest["timeout_minutes"] == 10
    assert manifest["budget"]["prior_cost_usd"] == 0.893
    assert manifest["budget"]["maximum_job_cost_usd"] == 0.001667
    assert manifest["training_steps_executed"] == 0
    assert manifest["validation_reexecuted"] is False
    assert manifest["model_copied"] is False
    assert verification["status"] == "ready"
    assert launch["external_job_started"] is False
    assert launch["source_volume"] == (f"{ATTEMPT9_SOURCE_URI}:/recovered-run:ro")
    assert launch["output_root"] == ("/outputs/" + FINALIZATION_REMOTE_OUTPUT_PATH)


def test_finalizer_has_no_training_reload_eval_or_model_copy_path() -> None:
    runner = finalization_runner_payload().decode()
    script = (Path(__file__).parents[1] / "scripts/finalize_hf_training_continuation_recovery.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "train_sft.py",
        ".train(",
        "optimizer.pt",
        "evaluator.evaluate",
        "AutoModel",
        "AutoTokenizer",
        "transformers",
        "torch",
        "copytree",
        "copyfile",
    ):
        assert forbidden not in runner + script
    result = build_finalization_result(
        corrected_training_report_sha256="sha256:" + "a" * 64,
        source_verification_sha256="sha256:" + "b" * 64,
    )
    assert result["run_fingerprint"] == ATTEMPT9_RUN_FINGERPRINT
    assert result["cumulative_training_steps"] == 1100
    assert result["training_steps_executed"] == 0
    assert result["validation_reexecuted"] is False
    assert result["model_copied"] is False
