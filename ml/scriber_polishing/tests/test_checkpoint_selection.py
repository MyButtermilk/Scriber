from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scriber_polishing.checkpoint_selection import (
    CheckpointSelectionError,
    build_checkpoint_selection_handoff,
    build_selected_checkpoint_publication,
    canonical_json_sha256,
    discover_surviving_checkpoint_candidates,
    load_checkpoint_selection_config,
    select_checkpoint,
    validate_checkpoint_candidate_evidence,
    validate_checkpoint_selection_handoff,
    validate_checkpoint_selection_report,
    validate_selected_checkpoint_publication,
)


def _hash(character: str) -> str:
    return f"sha256:{character * 64}"


def _config(*, tail_count: int = 2) -> dict[str, object]:
    return {
        "schema_version": 2,
        "training_report": {
            "schema_version": 2,
            "run_kind": "final",
            "require_bf16": True,
            "require_training_completed": True,
            "best_checkpoint_metric": "eval_loss",
            "recent_tail_count": tail_count,
        },
        "checkpoint": {
            "format": "transformers_safetensors",
            "variant": "bf16",
            "architecture": "Gemma3ForCausalLM",
            "model_type": "gemma3_text",
            "base_model": {
                "id": "google/gemma-3-270m-it",
                "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
            },
        },
        "generated_metrics": {
            "generator": "deterministic_automatic_v1",
            "required_hard_safety_gates": [
                "protected_spans_exact",
                "critical_errors_zero",
                "deterministic_sst_valid",
            ],
            "composite": [
                {"metric": "generated_safety_content", "weight": 0.6},
                {"metric": "task", "weight": 0.25},
                {"metric": "structure", "weight": 0.1},
                {"metric": "normalized_eval_loss", "weight": 0.05},
            ],
        },
        "tie_breakers": [
            {"field": "hard_safety_passed", "direction": "desc"},
            {"field": "composite_score", "direction": "desc"},
            {"field": "critical_error_count", "direction": "asc"},
            {"field": "generated_subtotal", "direction": "desc"},
            {"field": "eval_loss", "direction": "asc"},
            {"field": "global_step", "direction": "asc"},
        ],
        "publication": {
            "base_tokenizer": {
                "id": "google/gemma-3-270m-it",
                "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
            },
            "required_reload": {
                "process_boundary": "fresh_subprocess",
                "configuration_source": "saved_artifact",
                "tokenizer_source": "pinned_base_tokenizer",
            },
        },
    }


def _completed_report() -> dict[str, object]:
    fingerprint = _hash("a")
    checkpoints = []
    for step, character, state_character in ((100, "b", "5"), (200, "c", "6"), (300, "d", "7")):
        checkpoints.append(
            {
                "checkpoint": f"checkpoint-{step}",
                "global_step": step,
                "run_fingerprint": fingerprint,
                "identity_sha256": _hash(character),
                "trainer_state_path": f"C:/private/checkpoint-{step}/trainer_state.json",
                "trainer_state_sha256": _hash(state_character),
            }
        )
    return {
        "schema_version": 2,
        "base_model": "google/gemma-3-270m-it",
        "base_revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
        "run_kind": "final",
        "run_fingerprint": fingerprint,
        "bf16": True,
        "max_steps": 300,
        "early_stopping": True,
        "save_total_limit": 3,
        "checkpoint_state": {
            "global_step": 300,
            "best_metric": 0.2,
            "best_model_checkpoint": "checkpoint-100",
            "last_checkpoint": "checkpoint-300",
            "surviving_checkpoints": checkpoints,
        },
        "training_completed": True,
    }


def _surviving_checkpoint(report: dict[str, object], candidate_id: str) -> dict[str, object]:
    return next(
        item
        for item in report["checkpoint_state"]["surviving_checkpoints"]
        if item["checkpoint"] == candidate_id
    )


def _candidate(
    report: dict[str, object],
    candidate_id: str,
    *,
    completed_report_sha256: str,
    selection_config_sha256: str,
    hard_passed: bool = True,
    critical_error_count: int = 0,
    scores: tuple[float, float, float] = (0.9, 0.9, 0.9),
    eval_loss: float = 0.2,
) -> dict[str, object]:
    surviving = _surviving_checkpoint(report, candidate_id)
    step = surviving["global_step"]
    config_hash = _hash("e")
    model_file = {
        "path": "model-00001-of-00001.safetensors",
        "bytes": 10,
        "sha256": _hash("f"),
    }
    inventory = {
        "schema_version": 1,
        "variant": "bf16",
        "format": "transformers_safetensors",
        "source_id": f"training-run:{report['run_fingerprint']}",
        "model_type": "gemma3_text",
        "declared_dtype": "bfloat16",
        "weight_dtype_counts": {"BF16": 1},
        "file_count": 3,
        "total_bytes": 30,
        "tree_sha256": _hash({"checkpoint-100": "1", "checkpoint-200": "2", "checkpoint-300": "3"}[candidate_id]),
        "files": [
            {"path": "config.json", "bytes": 10, "sha256": config_hash},
            model_file,
            {"path": "tokenizer.json", "bytes": 10, "sha256": _hash("9")},
        ],
    }
    hard_gates = {
        "protected_spans_exact": hard_passed,
        "critical_errors_zero": hard_passed,
        "deterministic_sst_valid": hard_passed,
    }
    return {
        "schema_version": 2,
        "candidate_id": candidate_id,
        "completed_training_report_sha256": completed_report_sha256,
        "selection_config_sha256": selection_config_sha256,
        "run_fingerprint": report["run_fingerprint"],
        "checkpoint_inventory": inventory,
        "checkpoint_identity": {
            "checkpoint": candidate_id,
            "global_step": step,
            "run_fingerprint": report["run_fingerprint"],
            "identity_sha256": surviving["identity_sha256"],
            "tree_sha256": inventory["tree_sha256"],
            "config_sha256": config_hash,
            "model_sha256": canonical_json_sha256([model_file]),
            "architecture": "Gemma3ForCausalLM",
            "model_type": "gemma3_text",
            "base_model": {
                "id": "google/gemma-3-270m-it",
                "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
            },
        },
        "evaluation_manifests": {
            "reference_manifest_sha256": _hash("7"),
            "prediction_manifest_sha256": _hash("8"),
        },
        "deterministic_automatic_report": {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "run_fingerprint": report["run_fingerprint"],
            "checkpoint": candidate_id,
            "global_step": step,
            "identity_sha256": surviving["identity_sha256"],
            "reference_manifest_sha256": _hash("7"),
            "prediction_manifest_sha256": _hash("8"),
            "generator": "deterministic_automatic_v1",
            "status": "complete",
            "provenance": "generated",
            "report_sha256": _hash("6"),
            "hard_safety_gates": hard_gates,
            "critical_error_count": critical_error_count,
            "generated_safety_content_score": scores[0],
            "task_score": scores[1],
            "structure_score": scores[2],
        },
        "checkpoint_eval": {
            "metric": "eval_loss",
            "global_step": step,
            "trainer_state_sha256": surviving["trainer_state_sha256"],
            "eval_loss": eval_loss,
        },
    }


def _publication_evidence(candidate: dict[str, object], config: dict[str, object]) -> dict[str, object]:
    identity = candidate["checkpoint_identity"]
    inventory = candidate["checkpoint_inventory"]
    return {
        "schema_version": 2,
        "candidate_id": candidate["candidate_id"],
        "run_fingerprint": candidate["run_fingerprint"],
        "checkpoint_identity": deepcopy(identity),
        "base_tokenizer": deepcopy(config["publication"]["base_tokenizer"]),
        "fresh_reload": {
            "status": "passed",
            "process_boundary": "fresh_subprocess",
            "configuration_source": "saved_artifact",
            "tokenizer_source": "pinned_base_tokenizer",
            "model_tree_sha256": identity["tree_sha256"],
            "model_config_sha256": identity["config_sha256"],
            "model_sha256": identity["model_sha256"],
            "generated_tokens": 7,
        },
        "fresh_inventory": {
            "status": "passed",
            "tree_sha256": identity["tree_sha256"],
            "config_sha256": identity["config_sha256"],
            "model_sha256": identity["model_sha256"],
            "file_count": inventory["file_count"],
            "total_bytes": inventory["total_bytes"],
        },
    }


def test_repository_policy_discovers_dynamic_candidates_without_hardcoding_ids() -> None:
    config = load_checkpoint_selection_config(
        Path(__file__).parents[1] / "configs" / "checkpoint_selection.yaml"
    )
    report = _completed_report()

    discovery = discover_surviving_checkpoint_candidates(report, config)

    assert "candidates" not in config
    assert discovery["best_loss_checkpoint"] == "checkpoint-100"
    assert discovery["recent_tail_checkpoints"] == [
        "checkpoint-100",
        "checkpoint-200",
        "checkpoint-300",
    ]
    assert discovery["candidate_checkpoint_ids"] == ["checkpoint-100", "checkpoint-200", "checkpoint-300"]
    assert config["publication"]["base_tokenizer"] == config["checkpoint"]["base_model"]


def test_completed_report_accepts_terminal_step_after_latest_saved_checkpoint() -> None:
    config = _config(tail_count=2)
    report = _completed_report()
    state = report["checkpoint_state"]
    state["surviving_checkpoints"] = state["surviving_checkpoints"][:2]
    state["last_checkpoint"] = "checkpoint-200"
    state["global_step"] = 250
    state["best_metric"] = None
    state["best_model_checkpoint"] = None
    report["max_steps"] = 250
    report["early_stopping"] = False
    report["schedule"] = {"save_steps": 100}

    discovery = discover_surviving_checkpoint_candidates(report, config)

    assert discovery["candidate_checkpoint_ids"] == ["checkpoint-100", "checkpoint-200"]


def test_early_stopped_report_may_finish_before_configured_max_steps() -> None:
    config = _config(tail_count=2)
    report = _completed_report()
    report["max_steps"] = 500

    discovery = discover_surviving_checkpoint_candidates(report, config)

    assert discovery["candidate_checkpoint_ids"] == [
        "checkpoint-100",
        "checkpoint-200",
        "checkpoint-300",
    ]


def test_completed_report_without_best_tracking_uses_only_recent_tail() -> None:
    config = _config(tail_count=2)
    report = _completed_report()
    state = report["checkpoint_state"]
    state["best_metric"] = None
    state["best_model_checkpoint"] = None
    report["early_stopping"] = False

    discovery = discover_surviving_checkpoint_candidates(report, config)

    assert discovery["best_loss_checkpoint"] is None
    assert discovery["candidate_checkpoint_ids"] == ["checkpoint-200", "checkpoint-300"]


@pytest.mark.parametrize(
    ("best_model_checkpoint", "best_metric"),
    [
        (None, 0.2),
        ("checkpoint-100", None),
    ],
)
def test_completed_report_rejects_inconsistent_best_checkpoint_pair(
    best_model_checkpoint: str | None,
    best_metric: float | None,
) -> None:
    config = _config()
    report = _completed_report()
    report["checkpoint_state"]["best_model_checkpoint"] = best_model_checkpoint
    report["checkpoint_state"]["best_metric"] = best_metric

    with pytest.raises(CheckpointSelectionError, match="best_model_checkpoint and best_metric"):
        discover_surviving_checkpoint_candidates(report, config)


def test_save_total_limit_inventory_keeps_old_best_plus_recent_tail() -> None:
    config = _config(tail_count=2)
    report = _completed_report()
    state = report["checkpoint_state"]
    survivors = state["surviving_checkpoints"]
    for survivor, step in zip(survivors[1:], (800, 900), strict=True):
        survivor["checkpoint"] = f"checkpoint-{step}"
        survivor["global_step"] = step
    state["global_step"] = 950
    state["last_checkpoint"] = "checkpoint-900"
    report["max_steps"] = 950
    report["save_total_limit"] = 3
    report["schedule"] = {"save_steps": 100, "save_total_limit": 3}

    discovery = discover_surviving_checkpoint_candidates(report, config)

    assert discovery["best_loss_checkpoint"] == "checkpoint-100"
    assert discovery["recent_tail_checkpoints"] == ["checkpoint-800", "checkpoint-900"]
    assert discovery["candidate_checkpoint_ids"] == [
        "checkpoint-100",
        "checkpoint-800",
        "checkpoint-900",
    ]


def test_completed_report_rejects_global_step_before_latest_saved_checkpoint() -> None:
    config = _config()
    report = _completed_report()
    report["checkpoint_state"]["global_step"] = 299

    with pytest.raises(CheckpointSelectionError, match="before last checkpoint"):
        discover_surviving_checkpoint_candidates(report, config)


def test_selector_requires_exact_discovered_candidates_and_hard_safety_precedes_composite() -> None:
    config = _config()
    report = _completed_report()
    config_sha256 = _hash("1")
    report_sha256 = _hash("2")
    candidates = [
        _candidate(
            report,
            "checkpoint-300",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            scores=(0.88, 0.88, 0.88),
            eval_loss=0.3,
        ),
        _candidate(
            report,
            "checkpoint-200",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            hard_passed=False,
            critical_error_count=1,
            scores=(0.99, 0.99, 0.99),
            eval_loss=0.1,
        ),
        _candidate(
            report,
            "checkpoint-100",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            scores=(0.9, 0.9, 0.9),
            eval_loss=0.2,
        ),
    ]

    selection = select_checkpoint(
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )

    assert selection["status"] == "selected"
    assert selection["selected"]["candidate_id"] == "checkpoint-100"
    rows = {row["candidate_id"]: row for row in selection["candidates"]}
    assert rows["checkpoint-200"]["hard_safety_passed"] is False
    assert rows["checkpoint-200"]["rank"] == 3
    assert rows["checkpoint-100"]["composite_score"] == pytest.approx(0.88)
    assert rows["checkpoint-100"]["normalized_eval_loss"] == pytest.approx(0.5)
    assert "judge" not in json.dumps(selection)

    with pytest.raises(CheckpointSelectionError, match="exact discovered candidate inventory"):
        select_checkpoint(
            report,
            candidates[:-1],
            config,
            completed_training_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
        )


def test_selector_uses_the_required_lower_step_final_tie_breaker() -> None:
    config = _config()
    report = _completed_report()
    config_sha256 = _hash("3")
    report_sha256 = _hash("4")
    candidates = [
        _candidate(
            report,
            "checkpoint-300",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            scores=(0.9, 0.9, 0.9),
            eval_loss=0.2,
        ),
        _candidate(
            report,
            "checkpoint-200",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            hard_passed=False,
            critical_error_count=1,
            scores=(0.99, 0.99, 0.99),
            eval_loss=0.1,
        ),
        _candidate(
            report,
            "checkpoint-100",
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            scores=(0.9, 0.9, 0.9),
            eval_loss=0.2,
        ),
    ]

    selection = select_checkpoint(
        report,
        list(reversed(candidates)),
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )

    assert selection["selected"]["candidate_id"] == "checkpoint-100"
    assert [row["candidate_id"] for row in selection["candidates"]] == [
        "checkpoint-100",
        "checkpoint-200",
        "checkpoint-300",
    ]
    assert [row["rank"] for row in selection["candidates"]] == [1, 3, 2]


def test_candidate_binds_completed_identity_model_hash_and_exact_step_eval_loss() -> None:
    config = _config()
    report = _completed_report()
    config_sha256 = _hash("5")
    report_sha256 = _hash("6")
    candidate = _candidate(
        report,
        "checkpoint-100",
        completed_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )
    candidate["checkpoint_identity"]["config_sha256"] = _hash("d")

    with pytest.raises(CheckpointSelectionError, match="config.json"):
        validate_checkpoint_candidate_evidence(
            candidate,
            report,
            config,
            completed_training_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
        )

    candidate = _candidate(
        report,
        "checkpoint-100",
        completed_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )
    candidate["checkpoint_eval"]["global_step"] = 200
    with pytest.raises(CheckpointSelectionError, match="eval loss global step"):
        validate_checkpoint_candidate_evidence(
            candidate,
            report,
            config,
            completed_training_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
        )


def test_selection_handoff_and_publication_only_revalidate_the_selected_checkpoint() -> None:
    config = _config()
    report = _completed_report()
    config_sha256 = _hash("7")
    report_sha256 = _hash("8")
    candidates = [
        _candidate(
            report,
            candidate_id,
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            hard_passed=candidate_id != "checkpoint-200",
            critical_error_count=1 if candidate_id == "checkpoint-200" else 0,
            scores=(0.95, 0.95, 0.95) if candidate_id == "checkpoint-100" else (0.9, 0.9, 0.9),
            eval_loss={"checkpoint-100": 0.2, "checkpoint-200": 0.1, "checkpoint-300": 0.3}[candidate_id],
        )
        for candidate_id in ("checkpoint-100", "checkpoint-200", "checkpoint-300")
    ]
    selection = select_checkpoint(
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )

    assert validate_checkpoint_selection_report(
        selection,
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    ) == selection
    tampered_selection = deepcopy(selection)
    tampered_selection["selected"]["candidate_id"] = "checkpoint-300"
    with pytest.raises(CheckpointSelectionError, match="does not match recomputed"):
        validate_checkpoint_selection_report(
            tampered_selection,
            report,
            candidates,
            config,
            completed_training_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
        )
    handoff = build_checkpoint_selection_handoff(
        selection,
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
        selection_report_sha256=canonical_json_sha256(selection),
    )
    assert handoff["selected_checkpoint"]["candidate_id"] == "checkpoint-100"
    assert validate_checkpoint_selection_handoff(
        handoff,
        selection,
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
        selection_report_sha256=canonical_json_sha256(selection),
    ) == handoff

    selected_candidate = next(item for item in candidates if item["candidate_id"] == "checkpoint-100")
    publication_evidence = _publication_evidence(selected_candidate, config)
    publication = build_selected_checkpoint_publication(
        handoff,
        publication_evidence,
        config,
        selection_handoff_sha256=canonical_json_sha256(handoff),
    )
    assert publication["status"] == "ready_for_publication"
    assert publication["selected_candidate_id"] == "checkpoint-100"
    assert publication["base_tokenizer"] == config["publication"]["base_tokenizer"]
    assert validate_selected_checkpoint_publication(
        publication,
        handoff,
        publication_evidence,
        config,
        selection_handoff_sha256=canonical_json_sha256(handoff),
    ) == publication

    stale_inventory = deepcopy(publication_evidence)
    stale_inventory["fresh_inventory"]["tree_sha256"] = _hash("e")
    with pytest.raises(CheckpointSelectionError, match="fresh inventory tree_sha256"):
        build_selected_checkpoint_publication(
            handoff,
            stale_inventory,
            config,
            selection_handoff_sha256=canonical_json_sha256(handoff),
        )


def test_selector_fails_closed_when_no_discovered_candidate_passes_safety() -> None:
    config = _config()
    report = _completed_report()
    config_sha256 = _hash("a")
    report_sha256 = _hash("b")
    candidates = [
        _candidate(
            report,
            candidate_id,
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            hard_passed=False,
            critical_error_count=1,
            eval_loss={"checkpoint-100": 0.3, "checkpoint-200": 0.2, "checkpoint-300": 0.1}[candidate_id],
        )
        for candidate_id in ("checkpoint-100", "checkpoint-200", "checkpoint-300")
    ]

    selection = select_checkpoint(
        report,
        candidates,
        config,
        completed_training_report_sha256=report_sha256,
        selection_config_sha256=config_sha256,
    )

    assert selection["status"] == "no_safe_candidate"
    assert selection["selected"] is None
    with pytest.raises(CheckpointSelectionError, match="without a safety-passed checkpoint"):
        build_checkpoint_selection_handoff(
            selection,
            report,
            candidates,
            config,
            completed_training_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            selection_report_sha256=canonical_json_sha256(selection),
        )


def test_local_scripts_select_handoff_and_validate_publication_without_model_or_hub(tmp_path: Path) -> None:
    config = _config()
    report = _completed_report()
    config_path = tmp_path / "checkpoint_selection.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    report_path = tmp_path / "completed-final-training.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    config_sha256 = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
    report_sha256 = "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    candidates = [
        _candidate(
            report,
            candidate_id,
            completed_report_sha256=report_sha256,
            selection_config_sha256=config_sha256,
            hard_passed=candidate_id != "checkpoint-200",
            critical_error_count=1 if candidate_id == "checkpoint-200" else 0,
            scores=(0.95, 0.95, 0.95) if candidate_id == "checkpoint-100" else (0.9, 0.9, 0.9),
            eval_loss={"checkpoint-100": 0.2, "checkpoint-200": 0.1, "checkpoint-300": 0.3}[candidate_id],
        )
        for candidate_id in ("checkpoint-100", "checkpoint-200", "checkpoint-300")
    ]
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    selection_path = tmp_path / "selection.json"
    handoff_path = tmp_path / "handoff.json"
    publication_evidence_path = tmp_path / "publication-evidence.json"
    publication_evidence_path.write_text(
        json.dumps(_publication_evidence(candidates[0], config)),
        encoding="utf-8",
    )
    publication_path = tmp_path / "publication.json"
    scripts_root = Path(__file__).parents[1] / "scripts"

    selected = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "select_final_checkpoint.py"),
            "--config",
            str(config_path),
            "--completed-training-report",
            str(report_path),
            "--candidates",
            str(candidates_path),
            "--output",
            str(selection_path),
            "--handoff-output",
            str(handoff_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert selected.returncode == 0, selected.stderr
    assert "selected=checkpoint-100" in selected.stdout

    prepared = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "prepare_selected_checkpoint_publication.py"),
            "--config",
            str(config_path),
            "--handoff",
            str(handoff_path),
            "--publication-evidence",
            str(publication_evidence_path),
            "--output",
            str(publication_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert prepared.returncode == 0, prepared.stderr

    verified = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "verify_selected_checkpoint_publication.py"),
            "--config",
            str(config_path),
            "--handoff",
            str(handoff_path),
            "--publication-evidence",
            str(publication_evidence_path),
            "--publication",
            str(publication_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert "selected_checkpoint_publication=verified candidate=checkpoint-100" in verified.stdout

    hooks = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "verify_selected_checkpoint_publication.py"),
            "--list-hooks",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert hooks.returncode == 0, hooks.stderr
    assert "scriber_polishing.hub_publication.publish_private_bundle" in hooks.stdout
