from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scriber_polishing.marker_repair_recovery import (
    finalize_marker_repair_recovery,
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> bytes:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _tree_sha256(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode())
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode())
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode())
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _model_inventory(model_root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for name, payload in (
        ("config.json", b'{"model_type":"gemma3_text"}\n'),
        ("model.safetensors", b"fixture-safetensors"),
    ):
        (model_root / name).parent.mkdir(parents=True, exist_ok=True)
        (model_root / name).write_bytes(payload)
        files.append(
            {
                "path": name,
                "bytes": len(payload),
                "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            }
        )
    return {
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": _tree_sha256(files),
    }


def _build_recovery_root(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "recovery"
    final_directory = "final-fixture"
    inventory = _model_inventory(root / "training" / final_directory)
    training_report = {
        "schema_version": 2,
        "training_completed": True,
        "max_steps": 146,
        "schedule": {
            "max_steps": 146,
            "update_steps_per_epoch": 146,
        },
        "checkpoint_state": {
            "global_step": 146,
            "last_checkpoint": "checkpoint-146",
        },
        "final_model": {
            "directory": final_directory,
            "inventory": inventory,
            "reload_verification": {"status": "passed"},
        },
    }
    training_report_payload = _write_json(
        root / "training-report.json",
        training_report,
    )
    training_report_sha256 = f"sha256:{hashlib.sha256(training_report_payload).hexdigest()}"
    _write_json(
        root / "evaluation-source-binding.json",
        {
            "schema_version": 1,
            "kind": "scriber_marker_repair_evaluation_recovery_binding",
            "training_reused": True,
            "training_steps_executed": 0,
            "completed_training_steps": 146,
            "training_report_sha256": training_report_sha256,
            "final_model_directory": final_directory,
            "final_model_tree_sha256": inventory["tree_sha256"],
            "final_model_file_count": inventory["file_count"],
            "final_model_total_bytes": inventory["total_bytes"],
        },
    )
    prediction_rows = [
        {
            "schema_version": 1,
            "case_id": f"case_{index:03d}",
            "prediction_sst": f"[DOC]\n[P]Fall {index}.[/P]\n[/DOC]",
        }
        for index in range(200)
    ]
    predictions_payload = b"".join(_json_bytes(row) for row in prediction_rows)
    (root / "predictions.jsonl").write_bytes(predictions_payload)
    prediction_sha256 = hashlib.sha256(predictions_payload).hexdigest()
    reference_sha256 = "1" * 64
    _write_json(
        root / "predictions.manifest.json",
        {
            "schema_version": 1,
            "candidate": "lexical_repair",
            "record_count": 200,
            "reference_sha256": reference_sha256,
            "prediction_sha256": prediction_sha256,
            "valid_sst_count": 200,
            "restoration_error_count": 0,
        },
    )
    _write_json(
        root / "evaluation.json",
        {
            "schema_version": 1,
            "candidate": "lexical_repair",
            "reference_sha256": reference_sha256,
            "prediction_sha256": prediction_sha256,
            "exact_case_coverage": True,
            "gate": {"passed": False},
            "metrics": {
                "total": 200,
                "sst_parse_rate": 1.0,
                "protected_span_exact_rate": 1.0,
                "case_results": [
                    {
                        "case_id": row["case_id"],
                        "sst_valid": True,
                        "protected_spans_exact": True,
                    }
                    for row in prediction_rows
                ],
            },
        },
    )
    _write_json(root / "repair-manifest.json", {})
    (root / "acceptance-policy.yaml").write_text(
        "schema_version: 1\n",
        encoding="utf-8",
    )
    _write_json(root / "preflight.json", {"ready_for_model_load": True})
    return root, {
        "final_directory": final_directory,
        "inventory": inventory,
        "training_report_sha256": training_report_sha256,
    }


def _file_evidence(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _mutate_json(path: Path, mutate: Any) -> None:
    value = json.loads(path.read_bytes())
    mutate(value)
    _write_json(path, value)


def test_finalizer_writes_bound_recovery_then_acceptance_cloud_inventory(
    tmp_path: Path,
) -> None:
    root, expected = _build_recovery_root(tmp_path)

    result = finalize_marker_repair_recovery(
        root,
        evaluation_gate_exit=2,
    )

    recovery_path = root / "recovery-evaluation-result.json"
    cloud_path = root / "cloud-result.json"
    assert recovery_path.is_file()
    assert cloud_path.is_file()

    recovery = json.loads(recovery_path.read_bytes())
    assert recovery == result["recovery_evaluation_result"]
    assert recovery["training_steps_executed"] == 0
    assert recovery["final_model_tree_sha256"] == expected["inventory"]["tree_sha256"]
    assert recovery["training_report_sha256"] == expected["training_report_sha256"]

    cloud = json.loads(cloud_path.read_bytes())
    assert cloud == result["cloud_result"]
    assert cloud["schema_version"] == 1
    assert cloud["kind"] == "scriber_hf_marker_repair_result"
    assert cloud["training_completed"] is True
    assert cloud["evaluation_completed"] is True
    assert cloud["evaluation_gate_exit"] == 2
    assert cloud["final_model_directory"] == expected["final_directory"]

    actual_files = {
        path.relative_to(root).as_posix(): _file_evidence(path)
        for path in root.rglob("*")
        if path.is_file() and path.name != "cloud-result.json"
    }
    assert cloud["files"] == actual_files


def test_finalizer_refuses_any_training_step_other_than_146(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    report_path = root / "training-report.json"
    report = json.loads(report_path.read_bytes())
    report["checkpoint_state"]["global_step"] = 145
    _write_json(report_path, report)

    try:
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)
    except ValueError as error:
        assert "training completed steps must equal 146" in str(error)
    else:
        raise AssertionError("145 training steps must fail closed")

    assert not (root / "recovery-evaluation-result.json").exists()
    assert not (root / "cloud-result.json").exists()


def test_finalizer_cli_exposes_only_the_two_bound_gate_exit_choices(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    script = Path(__file__).parents[1] / "scripts" / "finalize_marker_repair_recovery.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--recovery-root",
            str(root),
            "--evaluation-gate-exit",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "marker_repair_recovery=complete training_steps=146 evaluation_cases=200 gate_exit=2" in completed.stdout


def test_finalizer_requires_recovery_to_execute_zero_training_steps(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    _mutate_json(
        root / "evaluation-source-binding.json",
        lambda binding: binding.__setitem__("training_steps_executed", 1),
    )

    with pytest.raises(
        ValueError,
        match="recovery training steps executed must equal 0",
    ):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)

    assert not (root / "recovery-evaluation-result.json").exists()
    assert not (root / "cloud-result.json").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("candidate", "shuffled", "lexical_repair"),
        ("record_count", 199, "prediction count must equal 200"),
        ("valid_sst_count", 199, "valid SST count must equal 200"),
        ("restoration_error_count", 1, "restoration error count must equal 0"),
    ],
)
def test_finalizer_requires_exactly_200_clean_lexical_repair_predictions(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    _mutate_json(
        root / "predictions.manifest.json",
        lambda manifest: manifest.__setitem__(field, value),
    )

    with pytest.raises(ValueError, match=message):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)

    assert not (root / "recovery-evaluation-result.json").exists()
    assert not (root / "cloud-result.json").exists()


def test_finalizer_requires_exact_evaluation_case_coverage(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    _mutate_json(
        root / "evaluation.json",
        lambda evaluation: evaluation.__setitem__(
            "exact_case_coverage",
            False,
        ),
    )

    with pytest.raises(ValueError, match="exact 200-case"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)


def test_finalizer_binds_the_exact_training_report_hash(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    _mutate_json(
        root / "evaluation-source-binding.json",
        lambda binding: binding.__setitem__(
            "training_report_sha256",
            f"sha256:{'9' * 64}",
        ),
    )

    with pytest.raises(ValueError, match="completed training output"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)


def test_finalizer_rehashes_the_bound_final_model_tree(
    tmp_path: Path,
) -> None:
    root, expected = _build_recovery_root(tmp_path)
    model = root / "training" / expected["final_directory"] / "model.safetensors"
    model.write_bytes(model.read_bytes() + b"-tampered")

    with pytest.raises(ValueError, match="final model inventory"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)


def test_finalizer_never_overwrites_a_different_recovery_result(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)
    finalize_marker_repair_recovery(root, evaluation_gate_exit=2)
    cloud_before = (root / "cloud-result.json").read_bytes()
    _mutate_json(
        root / "recovery-evaluation-result.json",
        lambda recovery: recovery.__setitem__("evaluation_gate_exit", 0),
    )

    with pytest.raises(ValueError, match="refusing to overwrite different"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=2)

    assert (root / "cloud-result.json").read_bytes() == cloud_before


def test_finalizer_api_rejects_an_unbound_evaluation_exit(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)

    with pytest.raises(ValueError, match="must be 0 or 2"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=1)

    assert not (root / "recovery-evaluation-result.json").exists()
    assert not (root / "cloud-result.json").exists()


def test_finalizer_binds_gate_exit_to_the_evaluation_decision(
    tmp_path: Path,
) -> None:
    root, _ = _build_recovery_root(tmp_path)

    with pytest.raises(ValueError, match="differs from evaluation gate"):
        finalize_marker_repair_recovery(root, evaluation_gate_exit=0)

    assert not (root / "recovery-evaluation-result.json").exists()
    assert not (root / "cloud-result.json").exists()
