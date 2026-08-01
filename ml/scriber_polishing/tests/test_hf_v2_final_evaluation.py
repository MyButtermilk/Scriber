from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.curriculum import canonical_json_bytes
from scriber_polishing.hf_v2_final_evaluation import (
    V2_EVALUATION_OUTPUT_REMOTE_URI,
    V2_MODEL_TREE_SHA256,
    V2_MODEL_WEIGHT_SHA256,
    V2_POLICY_ARTIFACT_SHA256,
    V2FinalEvaluationError,
    build_v2_final_evaluation_packet,
    v2_final_evaluation_runner_payload,
    verify_v2_final_evaluation_launch_readiness,
    verify_v2_final_evaluation_packet,
    verify_v2_final_evaluation_result,
)
from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_plain_text

_PROJECT = Path(__file__).parents[1]
_V1_PACKET = _PROJECT / "artifacts/cloud/hf-full-model-final-evaluation-v1-job-packet"
_V1_RESULT = _PROJECT / "artifacts/cloud/hf-full-model-final-evaluation-v1-completed/final-evaluation-result.json"
_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64
_PINNED_V2_IMAGE = "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"


def _postflight_binding() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_hf_v2_training_completion_model_binding",
        "status": "complete",
        "job": {
            "id": "6a6e05bf6b79c09949c1e5fd",
            "terminal_status": "COMPLETED",
            "remote_result_uri": (
                "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
                "attempt13/scriber-v2-hardcase-replay-v1"
            ),
        },
        "source": {
            "source_git_head": "0e9d8c899442c090e90942791de7565119cfd552",
            "packet_tree_sha256": "sha256:3d11fbc9441ee4e563b46a87bf354a34fd9ff59d04a784325c725375f67ec5b6",
            "parent_checkpoint_tree_sha256": (
                "sha256:4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
            ),
            "launch_contract_sha256": (
                "sha256:ecb13e460f230f9ee5e265923df82fa8be3975033eddad9cf4e8356104a7a1f9"
            ),
        },
        "training": {
            "completed_steps": 640,
            "max_steps": 640,
            "learning_rate": 5e-6,
            "bf16": True,
        },
        "model": {
            "run_fingerprint": (
                "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
            ),
            "directory": "final-7b23f23e70f60df9",
            "remote_uri": (
                "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
                "scriber-v2-hardcase-replay-v1/training/final-7b23f23e70f60df9"
            ),
            "tree_sha256": V2_MODEL_TREE_SHA256,
            "weight_files": [
                {
                    "path": "model.safetensors",
                    "bytes": 536_223_056,
                    "sha256": V2_MODEL_WEIGHT_SHA256,
                }
            ],
            "protection_policy_sha256": V2_POLICY_ARTIFACT_SHA256,
            "reload_status": "passed",
            "inventory_sha256": _HASH_A,
            "reload_verification_sha256": _HASH_B,
        },
        "runtime": {
            "cuda_available": True,
            "bf16_supported": True,
            "device_count": 1,
            "gpu": "NVIDIA L4",
            "torch_version": "2.11.0+cu128",
            "cuda_runtime": "12.8",
            "verification_sha256": _HASH_C,
        },
        "evidence": {
            "launch_receipt_sha256": _HASH_A,
            "terminal_job_sha256": _HASH_B,
            "training_report_sha256": _HASH_A,
            "training_result_sha256": _HASH_B,
            "packet_verification_sha256": _HASH_C,
            "parent_binding_sha256": _HASH_A,
            "remote_inventory_sha256": _HASH_C,
        },
        "verification": {
            "metadata_only": True,
            "model_downloaded": False,
            "external_mutation_performed": False,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _built_packet(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    binding = tmp_path / "postflight.json"
    _write_json(binding, _postflight_binding())
    packet = tmp_path / "v2-evaluation-packet"
    built = build_v2_final_evaluation_packet(
        postflight_binding_path=binding,
        v1_evaluator_packet_root=_V1_PACKET,
        v1_completed_result_path=_V1_RESULT,
        output_dir=packet,
    )
    return packet, built


def test_builder_pins_v2_model_v1_evaluator_and_sealed_references(tmp_path: Path) -> None:
    packet, built = _built_packet(tmp_path)

    manifest = json.loads((packet / "package-manifest.json").read_bytes())
    contract = json.loads((packet / "launch-contract.json").read_bytes())
    runner = (packet / "run-v2-final-evaluation.sh").read_text("utf-8")

    assert manifest["model"]["tree_sha256"] == V2_MODEL_TREE_SHA256
    assert manifest["model"]["weight_files"][0]["sha256"] == V2_MODEL_WEIGHT_SHA256
    assert manifest["model"]["protection_policy_sha256"] == V2_POLICY_ARTIFACT_SHA256
    assert manifest["evaluator"]["snapshot_tree_sha256"] == (
        "sha256:75877a883963cdee4fef5d07f2ef92100d9ed843e29e48fc40b8aad74cb3960e"
    )
    assert {key: value["source_record_count"] for key, value in manifest["references"].items()} == {
        "ai_gold": 1750,
        "critical_regression": 500,
        "regular_test": 1000,
    }
    assert {value["selected_record_count"] for value in manifest["references"].values()} == {100}
    assert manifest["evaluation_scope"] == {
        "evidence_class": "compact_smoke_test",
        "publication_grade": False,
        "records_per_suite": 100,
        "total_records": 300,
        "selection_algorithm": "sha256_case_id_rank_v1",
        "selection_seed": "scriber-v2-compact-smoke-v1",
        "v1_prediction_strategy": "reuse_completed_v1_predictions_without_inference",
    }
    assert contract["remote_result_uri"] == V2_EVALUATION_OUTPUT_REMOTE_URI
    assert contract["image"] == _PINNED_V2_IMAGE
    assert contract["external_job_started"] is False
    assert contract["preconditions"]["no_prior_exact_job"]["required"] is True
    assert contract["preconditions"]["empty_remote_output"]["required"] is True
    assert contract["v1_evaluation_volume"].endswith(":/input/v1-evaluation:ro")
    assert "/outputs/scriber-v2-hardcase-replay-final-evaluation-v1/final" in runner
    assert runner.count("run_lane v2") == 3
    assert runner.count("\nevaluate_reused_v1 ") == 3
    assert "run_lane base_model" not in runner
    assert runner.count("--gate-candidate final") == 3
    assert 'cp -a "$LOCAL_RESULT_ROOT" "$OUTPUT_ROOT"' in runner
    assert runner.index('cp -a "$LOCAL_RESULT_ROOT" "$OUTPUT_ROOT"') < runner.index(
        '> "$OUTPUT_ROOT/COMPLETED"'
    )
    assert "train_sft" not in runner
    assert "sha256_case_id_rank_v1" in runner
    assert "FULL_3250" not in runner
    assert built["packet_tree_sha256"] == manifest["inventory"]["packet_tree_sha256"]
    mount_source = contract["output_volume"].removesuffix(":/outputs:rw")
    container_relative = manifest["output"]["container_path"].removeprefix("/outputs/")
    assert f"{mount_source}/{container_relative}" == contract["remote_result_uri"]


def test_runner_has_no_nul_and_every_python_heredoc_compiles(tmp_path: Path) -> None:
    runner = v2_final_evaluation_runner_payload()
    assert b"\x00" not in runner
    text = runner.decode("utf-8", errors="strict")
    heredocs = re.findall(r"(?ms)^[ \t]*python\b[^\n]* <<'PY'\r?\n(.*?)\r?\nPY$", text)
    assert len(heredocs) >= 3
    selection_source = next(source for source in heredocs if "prediction_manifest.get" in source)
    final_source = next(source for source in heredocs if "Terra source count differs" in source)
    assert "pathlib.Path(selection_out).write_bytes" in selection_source
    assert '(root/"v2-final-evaluation-result.json").write_bytes' in final_source
    for index, source in enumerate(heredocs):
        path = tmp_path / f"heredoc-{index}.py"
        path.write_text(source + "\n", encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr


def test_runner_namespaces_suite_local_ids_for_combined_gate_and_terra_handoff(tmp_path: Path) -> None:
    runner = v2_final_evaluation_runner_payload().decode("utf-8")
    heredocs = re.findall(r"(?ms)^[ \t]*python\b[^\n]* <<'PY'\r?\n(.*?)\r?\nPY$", runner)
    selection_source = next(source for source in heredocs if "prediction_manifest.get" in source)
    combined_source = next(source for source in heredocs if "combined case coverage differs" in source)
    final_source = next(source for source in heredocs if "Terra source count differs" in source)
    work = tmp_path / "work"
    result_root = tmp_path / "result"
    work.mkdir()
    result_root.mkdir()

    def execute(source: str, *arguments: Path | str) -> None:
        script = tmp_path / f"heredoc-exec-{len(list(tmp_path.glob('heredoc-exec-*')))}.py"
        script.write_text(source + "\n", encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [sys.executable, str(script), *(str(argument) for argument in arguments)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr

    source_counts = {"ai_gold": 1750, "critical_regression": 500, "regular_test": 1000}
    for suite, count in source_counts.items():
        references = tmp_path / f"{suite}.references.jsonl"
        predictions = tmp_path / f"{suite}.predictions.jsonl"
        reference_payload = b"".join(
            canonical_json_bytes(
                {
                    "case_id": f"eval_shared_{index:04d}",
                    "source_text": f"Synthetic source {suite} {index}",
                    "target_plain_text": f"Synthetic reference {suite} {index}",
                }
            )
            for index in range(count)
        )
        prediction_payload = b"".join(
            canonical_json_bytes(
                {
                    "case_id": f"eval_shared_{index:04d}",
                    "prediction_sst": f"Synthetic V1 {suite} {index}",
                }
            )
            for index in range(count)
        )
        references.write_bytes(reference_payload)
        predictions.write_bytes(prediction_payload)
        _write_json(
            predictions.with_name("predictions.manifest.json"),
            {
                "candidate": "final",
                "record_count": count,
                "prediction_sha256": hashlib.sha256(prediction_payload).hexdigest(),
            },
        )
        compact_references = work / f"{suite}.references.jsonl"
        compact_predictions = work / f"{suite}.v1-predictions.jsonl"
        selection = result_root / f"{suite}.selection.json"
        execute(
            selection_source,
            suite,
            references,
            predictions,
            compact_references,
            compact_predictions,
            selection,
        )
        v2_predictions = result_root / "v2" / suite / "predictions.jsonl"
        v2_predictions.parent.mkdir(parents=True, exist_ok=True)
        v2_rows = [json.loads(raw) for raw in compact_predictions.read_bytes().splitlines()]
        v2_predictions.write_bytes(
            b"".join(
                canonical_json_bytes({**row, "prediction_sst": str(row["prediction_sst"]).replace("V1", "V2")})
                for row in v2_rows
            )
        )

    execute(combined_source, work, result_root)
    compact_local_ids = {
        suite: {
            json.loads(raw)["case_id"]
            for raw in (work / f"{suite}.references.jsonl").read_bytes().splitlines()
        }
        for suite in ("ai_gold", "critical_regression", "regular_test")
    }
    assert len(set().union(*compact_local_ids.values())) < 300
    combined_ids: dict[str, list[str]] = {}
    for label, path in (
        ("references", work / "combined.references.jsonl"),
        ("v1", work / "combined.v1-predictions.jsonl"),
        ("v2", work / "combined.v2-predictions.jsonl"),
    ):
        ids = [json.loads(raw)["case_id"] for raw in path.read_bytes().splitlines()]
        assert len(ids) == len(set(ids)) == 300
        combined_ids[label] = ids
    assert combined_ids["references"] == combined_ids["v1"] == combined_ids["v2"]
    for suite, local_ids in compact_local_ids.items():
        prefix = f"{suite}__"
        suite_ids = [case_id for case_id in combined_ids["references"] if case_id.startswith(prefix)]
        assert len(suite_ids) == 100
        assert {case_id.removeprefix(prefix) for case_id in suite_ids} == local_ids
    for suite in ("ai_gold", "critical_regression", "regular_test"):
        for candidate in ("v1_reused", "v2"):
            lane = result_root / candidate / suite
            lane.mkdir(parents=True, exist_ok=True)
            _write_json(lane / "evaluation.json", {"candidate": candidate, "diagnostic_suite": suite})
            (lane / "evaluation-exit.txt").write_text("2", encoding="ascii")
    for candidate, exact, critical in (("v1_reused", 0.60, 4), ("v2", 0.65, 3)):
        lane = result_root / candidate / "combined"
        lane.mkdir(parents=True, exist_ok=True)
        _write_json(
            lane / "evaluation.json",
            {
                "candidate": candidate,
                "metrics": {
                    "total": 300,
                    "normalized_exact_match": exact,
                    "critical_errors": {"changed_number": critical},
                },
            },
        )
        (lane / "evaluation-exit.txt").write_text("2", encoding="ascii")
    manifest = tmp_path / "manifest.json"
    _write_json(
        manifest,
        {
            "model": {"tree_sha256": V2_MODEL_TREE_SHA256},
            "evaluator": {"snapshot": "synthetic"},
            "reference_bundle_sha256": _HASH_A,
            "evaluation_scope": {"total_records": 300},
            "paired_baseline": {"candidate": "v1_full_model"},
        },
    )
    execute(final_source, manifest, result_root, work)

    result = json.loads((result_root / "v2-final-evaluation-result.json").read_bytes())
    assert len(result["reports"]) == 8
    assert result["compact_comparison_gate"]["decision"] == "passed"
    assert result["terra_judge_source"]["record_count"] == 300
    terra_rows = [
        json.loads(raw) for raw in (result_root / "terra-source-cases.jsonl").read_bytes().splitlines()
    ]
    assert len(terra_rows) == 300
    assert len({row["case_id"] for row in terra_rows}) == 300
    assert {row["case_id"] for row in terra_rows} == set(combined_ids["references"])


def test_runner_gate_candidate_handoff_accepts_v2_reporting_label(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    evaluator = packet / "evaluator"
    target_sst = "[DOC]\n[P]Ein synthetischer Satz.[/P]\n[/DOC]"
    document = parse_sst(target_sst)
    reference = {
        "schema_version": 1,
        "case_id": "synthetic_gate_001",
        "split": "test",
        "source_text": "ein synthetischer satz",
        "target_sst": target_sst,
        "target_plain_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Ein synthetischer Satz.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Der Satz dient ausschließlich einem Test.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Synthetischer Test"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [],
        "slice_tags": ["synthetic"],
        "is_identity": False,
    }
    references = tmp_path / "references.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    references.write_bytes(canonical_json_bytes(reference))
    predictions.write_bytes(
        canonical_json_bytes(
            {"schema_version": 1, "case_id": "synthetic_gate_001", "prediction_sst": target_sst}
        )
    )
    output = tmp_path / "evaluation.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(evaluator / "scripts/evaluate_predictions.py"),
            "--references",
            str(references),
            "--predictions",
            str(predictions),
            "--candidate",
            "v2",
            "--gate-candidate",
            "final",
            "--config",
            str(evaluator / "configs/evaluation.yaml"),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "PYTHONPATH": str(evaluator / "src")},
    )
    assert completed.returncode in {0, 2}, completed.stderr
    report = json.loads(output.read_bytes())
    assert report["candidate"] == "v2"
    assert report["gate_candidate"] == "final"


def test_packet_verifier_rejects_any_reference_tamper(tmp_path: Path) -> None:
    packet, built = _built_packet(tmp_path)
    reference = packet / "references/regular_test_1000.references.jsonl"
    with reference.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(V2FinalEvaluationError, match="inventory"):
        verify_v2_final_evaluation_packet(
            packet,
            expected_packet_tree_sha256=str(built["packet_tree_sha256"]),
            expected_manifest_sha256=str(built["manifest_sha256"]),
            expected_launch_contract_sha256=str(built["launch_contract_sha256"]),
        )


def test_builder_rejects_incomplete_postflight_schema(tmp_path: Path) -> None:
    binding = _postflight_binding()
    del binding["evidence"]["launch_receipt_sha256"]  # type: ignore[index]
    path = tmp_path / "postflight.json"
    _write_json(path, binding)

    with pytest.raises(V2FinalEvaluationError, match="postflight.*schema"):
        build_v2_final_evaluation_packet(
            postflight_binding_path=path,
            v1_evaluator_packet_root=_V1_PACKET,
            v1_completed_result_path=_V1_RESULT,
            output_dir=tmp_path / "packet",
        )


def _remote_snapshot(kind: str, *, entries: list[object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": kind,
        "namespace": "Buttermilk03",
        "observed_at_utc": "2026-08-01T18:00:00Z",
        "complete": True,
        "entries": entries,
    }


def test_launch_readiness_fails_closed_on_duplicate_or_nonempty_output(tmp_path: Path) -> None:
    packet, built = _built_packet(tmp_path)
    contract = packet / "launch-contract.json"
    packet_snapshot = _remote_snapshot(
        "scriber_hf_v2_final_evaluation_remote_packet_snapshot",
        entries=[
            {
                "packet_tree_sha256": json.loads((packet / "package-manifest.json").read_bytes())["inventory"][
                    "packet_tree_sha256"
                ],
                "manifest_sha256": "sha256:placeholder",
            }
        ],
    )
    # Readiness accepts the canonical digests rather than this test placeholder.
    contract_value = json.loads(contract.read_bytes())
    packet_snapshot["entries"] = [
        {
            "packet_tree_sha256": contract_value["packet_tree_sha256"],
            "manifest_sha256": contract_value["manifest_sha256"],
            "launch_contract_sha256": built["launch_contract_sha256"],
        }
    ]
    clean_jobs = _remote_snapshot("scriber_hf_v2_final_evaluation_jobs_snapshot", entries=[])
    clean_jobs["scope"] = "all_historical_and_active_namespace_jobs"
    clean_target = _remote_snapshot("scriber_hf_v2_final_evaluation_output_snapshot", entries=[])
    clean_target["target_remote_uri"] = contract_value["preconditions"]["empty_remote_output"][
        "target_remote_uri"
    ]
    packet_snapshot["target_remote_uri"] = contract_value["packet_remote_uri"]

    readiness = verify_v2_final_evaluation_launch_readiness(
        contract_path=contract,
        jobs_snapshot=clean_jobs,
        output_snapshot=clean_target,
        packet_snapshot=packet_snapshot,
        now_utc="2026-08-01T18:00:30Z",
    )
    assert readiness["status"] == "ready"
    assert readiness["external_mutation_performed"] is False

    duplicate = _remote_snapshot(
        "scriber_hf_v2_final_evaluation_jobs_snapshot",
        entries=[{"labels": contract_value["identity_labels"], "status": "RUNNING"}],
    )
    duplicate["scope"] = "all_historical_and_active_namespace_jobs"
    with pytest.raises(V2FinalEvaluationError, match="prior exact job"):
        verify_v2_final_evaluation_launch_readiness(
            contract_path=contract,
            jobs_snapshot=duplicate,
            output_snapshot=clean_target,
            packet_snapshot=packet_snapshot,
            now_utc="2026-08-01T18:00:30Z",
        )

    nonempty = _remote_snapshot(
        "scriber_hf_v2_final_evaluation_output_snapshot",
        entries=[{"path": "partial/predictions.jsonl"}],
    )
    nonempty["target_remote_uri"] = contract_value["preconditions"]["empty_remote_output"][
        "target_remote_uri"
    ]
    with pytest.raises(V2FinalEvaluationError, match="not empty"):
        verify_v2_final_evaluation_launch_readiness(
            contract_path=contract,
            jobs_snapshot=clean_jobs,
            output_snapshot=nonempty,
            packet_snapshot=packet_snapshot,
            now_utc="2026-08-01T18:00:30Z",
        )

    incomplete_jobs = dict(clean_jobs)
    del incomplete_jobs["scope"]
    with pytest.raises(V2FinalEvaluationError, match="wrong-scope"):
        verify_v2_final_evaluation_launch_readiness(
            contract_path=contract,
            jobs_snapshot=incomplete_jobs,
            output_snapshot=clean_target,
            packet_snapshot=packet_snapshot,
            now_utc="2026-08-01T18:00:30Z",
        )


def test_result_verifier_binds_combined_comparison_and_terra_source(tmp_path: Path) -> None:
    packet, _built = _built_packet(tmp_path)
    manifest = json.loads((packet / "package-manifest.json").read_bytes())
    result_root = tmp_path / "result"
    reports = []
    selections = []
    for suite in ("ai_gold", "critical_regression", "regular_test"):
        for candidate in ("v1_reused", "v2"):
            path = result_root / candidate / suite / "evaluation.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((candidate + "-" + suite + "-sealed-report").encode("ascii"))
            reports.append(
                {
                    "candidate": candidate,
                    "suite": suite,
                    "path": f"{candidate}/{suite}/evaluation.json",
                    "bytes": path.stat().st_size,
                    "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                    "evaluation_exit": 0,
                    "record_count": 100,
                }
            )
        selection_value = {
            "schema_version": 1,
            "kind": "scriber_v2_compact_evaluation_selection",
            "algorithm": "sha256_case_id_rank_v1",
            "seed": "scriber-v2-compact-smoke-v1",
            "suite": suite,
            "source_record_count": manifest["references"][suite]["source_record_count"],
            "selected_record_count": 100,
            "case_ids_sha256": _HASH_A,
            "reference_subset_sha256": _HASH_B,
            "v1_prediction_subset_sha256": _HASH_C,
            "case_ids_exposed": False,
        }
        selection_path = result_root / f"{suite}.selection.json"
        _write_json(selection_path, selection_value)
        selections.append(
            {
                **selection_value,
                "path": selection_path.relative_to(result_root).as_posix(),
                "sha256": "sha256:" + hashlib.sha256(selection_path.read_bytes()).hexdigest(),
            }
        )
    combined_metrics = {
        "v1_reused": {"total": 300, "normalized_exact_match": 0.60, "critical_errors": {"changed_number": 4}},
        "v2": {"total": 300, "normalized_exact_match": 0.65, "critical_errors": {"changed_number": 3}},
    }
    for candidate in ("v1_reused", "v2"):
        path = result_root / candidate / "combined/evaluation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, {"candidate": candidate, "metrics": combined_metrics[candidate]})
        reports.append(
            {
                "candidate": candidate,
                "suite": "combined",
                "path": f"{candidate}/combined/evaluation.json",
                "bytes": path.stat().st_size,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "evaluation_exit": 2,
                "record_count": 300,
            }
        )
    terra_rows = [
        {
            "schema_version": 1,
            "case_id": f"eval_{suite}_{index:03d}",
            "suite": suite,
            "source_text": f"Synthetic source {suite} {index}",
            "reference_text": f"Synthetic reference {suite} {index}",
            "v1_output": f"Synthetic V1 {suite} {index}",
            "v2_output": f"Synthetic V2 {suite} {index}",
        }
        for suite in ("ai_gold", "critical_regression", "regular_test")
        for index in range(100)
    ]
    terra_payload = b"".join(canonical_json_bytes(row) for row in terra_rows)
    terra_path = result_root / "terra-source-cases.jsonl"
    terra_path.write_bytes(terra_payload)
    terra_binding = {
        "path": "terra-source-cases.jsonl",
        "bytes": len(terra_payload),
        "sha256": "sha256:" + hashlib.sha256(terra_payload).hexdigest(),
        "record_count": 300,
        "suite_counts": {"ai_gold": 100, "critical_regression": 100, "regular_test": 100},
        "schema_path": "contracts/v2_compact_terra_source_case_schema.json",
        "contains_candidate_identity": True,
        "judge_public": False,
    }
    result = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_result",
        "status": "complete",
        "model": manifest["model"],
        "evaluator": manifest["evaluator"],
        "reference_bundle_sha256": manifest["reference_bundle_sha256"],
        "evaluation_scope": manifest["evaluation_scope"],
        "selection_evidence": selections,
        "reports": reports,
        "compact_comparison_gate": {
            "kind": "scriber_v2_compact_comparison_gate",
            "source_report_suite": "combined",
            "record_count": 300,
            "coverage": {"v1_reused": 300, "v2": 300, "exact": True},
            "normalized_exact_match": {
                "v1_reused": 0.60,
                "v2": 0.65,
                "v2_gte_v1": True,
            },
            "deterministic_critical_error_count": {
                "v1_reused": 4,
                "v2": 3,
                "v2_lte_v1": True,
            },
            "decision": "passed",
            "publication_grade": False,
            "requires_terra": True,
        },
        "terra_judge_source": terra_binding,
        "paired_baseline": manifest["paired_baseline"],
        "training_steps_executed": 0,
        "publication_grade": False,
        "external_mutation_performed_by_verifier": False,
    }
    result_path = result_root / "v2-final-evaluation-result.json"
    _write_json(result_path, result)
    (result_root / "COMPLETED").write_bytes(b"scriber_v2_compact_final_evaluation_complete_v1\n")

    verified = verify_v2_final_evaluation_result(
        packet_manifest_path=packet / "package-manifest.json",
        result_root=result_root,
        result_path=result_path,
    )

    assert verified["status"] == "passed"
    assert verified["model_tree_sha256"] == V2_MODEL_TREE_SHA256
    assert verified["report_count"] == 8
    assert verified["evaluated_record_count"] == 300
    assert verified["terra_source_record_count"] == 300
    assert verified["terra_source_sha256"] == terra_binding["sha256"]
    assert verified["compact_comparison_gate_decision"] == "passed"
    assert verified["publication_grade"] is False

    result["model"] = {**manifest["model"], "tree_sha256": _HASH_A}
    _write_json(result_path, result)
    with pytest.raises(V2FinalEvaluationError, match="model binding"):
        verify_v2_final_evaluation_result(
            packet_manifest_path=packet / "package-manifest.json",
            result_root=result_root,
            result_path=result_path,
        )


def test_prepare_and_verify_packet_clis_never_launch_a_job(tmp_path: Path) -> None:
    postflight = tmp_path / "postflight.json"
    _write_json(postflight, _postflight_binding())
    packet = tmp_path / "packet"
    prepare = subprocess.run(
        [
            sys.executable,
            str(_PROJECT / "scripts/prepare_hf_v2_final_evaluation.py"),
            "--postflight-binding",
            str(postflight),
            "--v1-evaluator-packet",
            str(_V1_PACKET),
            "--v1-completed-result",
            str(_V1_RESULT),
            "--output-dir",
            str(packet),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert prepare.returncode == 0, prepare.stderr
    built = json.loads(prepare.stdout)
    assert built["external_job_started"] is False

    verify = subprocess.run(
        [
            sys.executable,
            str(_PROJECT / "scripts/verify_hf_v2_final_evaluation.py"),
            "--mode",
            "packet",
            "--packet-root",
            str(packet),
            "--expected-packet-tree-sha256",
            built["packet_tree_sha256"],
            "--expected-manifest-sha256",
            built["manifest_sha256"],
            "--expected-launch-contract-sha256",
            built["launch_contract_sha256"],
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert verify.returncode == 0, verify.stderr
    verified = json.loads(verify.stdout)
    assert verified["status"] == "passed"
    assert verified["selected_record_count"] == 300
    assert verified["external_job_started"] is False
