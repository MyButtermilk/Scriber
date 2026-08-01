"""Compare matched shuffled/staged pilot outputs and emit a binding adoption decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum_comparison import (  # noqa: E402
    compare_curriculum,
    load_curriculum_comparison_config,
    write_curriculum_comparison_report,
)
from scriber_polishing.evaluation_io import (  # noqa: E402
    join_evaluation_cases,
    load_candidate_predictions,
    load_evaluation_references,
)

_MATCHED_REPORT_FIELDS = (
    "base_model",
    "base_revision",
    "run_kind",
    "seed",
    "method",
    "optimizer",
    "learning_rate",
    "max_sequence_length",
    "micro_batch_size",
    "gradient_accumulation_steps",
    "effective_batch_size",
    "max_steps",
    "schedule",
    "early_stopping",
    "train_examples_loaded",
    "train_examples_used",
    "validation_examples_loaded",
    "validation_examples_used",
    "oversize_rejections",
)
_MATCHED_CURRICULUM_FIELDS = (
    "algorithm",
    "phase_rules_version",
    "seed",
    "epochs",
    "record_count",
    "phase_counts",
    "source_sha256",
    "ai_gold_train_sha256",
)


def _stable_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    before = resolved.stat()
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError(f"{label} changed while it was being read")
    return payload


def _load_report(path: Path, label: str) -> tuple[dict[str, Any], str]:
    payload = _stable_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value, hashlib.sha256(payload).hexdigest()


def _positive_eval_loss(report: Mapping[str, Any], label: str) -> float:
    metrics = report.get("eval_metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError(f"{label} requires eval_metrics")
    value = metrics.get("eval_loss")
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} requires a finite positive eval_metrics.eval_loss")
    return float(value)


def _curriculum_binding(
    report: Mapping[str, Any],
    *,
    expected_mode: str,
    label: str,
) -> Mapping[str, Any]:
    if report.get("training_completed") is not True:
        raise ValueError(f"{label} must declare training_completed=true")
    if report.get("run_kind") != "pilot":
        raise ValueError(f"{label} must be a pilot report")
    binding = report.get("curriculum_order")
    if not isinstance(binding, Mapping):
        raise ValueError(f"{label} requires curriculum_order evidence")
    if binding.get("mode") != expected_mode:
        raise ValueError(f"{label} curriculum mode must be {expected_mode}")
    plan_sha256 = binding.get("plan_sha256")
    if (
        not isinstance(plan_sha256, str)
        or len(plan_sha256) != 64
        or any(character not in "0123456789abcdef" for character in plan_sha256)
    ):
        raise ValueError(f"{label} requires a lowercase curriculum plan SHA-256")
    return binding


def _require_matched_reports(
    control: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> None:
    for field in _MATCHED_REPORT_FIELDS:
        if field not in control or field not in staged:
            raise ValueError(f"matched training reports require {field}")
        if control[field] != staged[field]:
            raise ValueError(f"training reports differ at matched field {field}")
    control_bindings = control.get("bindings")
    staged_bindings = staged.get("bindings")
    if not isinstance(control_bindings, Mapping) or not isinstance(staged_bindings, Mapping):
        raise ValueError("matched training reports require bindings")
    if control_bindings.get("dataset") != staged_bindings.get("dataset"):
        raise ValueError("training reports differ at bindings.dataset")


def _require_matched_curriculum(
    control: Mapping[str, Any],
    staged: Mapping[str, Any],
) -> None:
    for field in _MATCHED_CURRICULUM_FIELDS:
        if field not in control or field not in staged:
            raise ValueError(f"curriculum evidence requires {field}")
        if control[field] != staged[field]:
            raise ValueError(f"curriculum evidence differs at matched field {field}")
    if control["plan_sha256"] == staged["plan_sha256"]:
        raise ValueError("shuffled and staged curriculum plans must have different hashes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--control-predictions", type=Path, required=True)
    parser.add_argument("--staged-predictions", type=Path, required=True)
    parser.add_argument("--control-training-report", type=Path, required=True)
    parser.add_argument("--staged-training-report", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        control_report, control_report_sha256 = _load_report(
            args.control_training_report,
            "control training report",
        )
        staged_report, staged_report_sha256 = _load_report(
            args.staged_training_report,
            "staged training report",
        )
        _require_matched_reports(control_report, staged_report)
        control_curriculum = _curriculum_binding(
            control_report,
            expected_mode="shuffled",
            label="control training report",
        )
        staged_curriculum = _curriculum_binding(
            staged_report,
            expected_mode="staged",
            label="staged training report",
        )
        _require_matched_curriculum(control_curriculum, staged_curriculum)
        control_loss = _positive_eval_loss(control_report, "control training report")
        staged_loss = _positive_eval_loss(staged_report, "staged training report")

        before = {
            "references": hashlib.sha256(
                _stable_bytes(args.references, "curriculum references")
            ).hexdigest(),
            "control_predictions": hashlib.sha256(
                _stable_bytes(args.control_predictions, "control predictions")
            ).hexdigest(),
            "staged_predictions": hashlib.sha256(
                _stable_bytes(args.staged_predictions, "staged predictions")
            ).hexdigest(),
        }
        references = load_evaluation_references(args.references)
        control_predictions = load_candidate_predictions(args.control_predictions)
        staged_predictions = load_candidate_predictions(args.staged_predictions)
        control_cases = join_evaluation_cases(references, control_predictions)
        staged_cases = join_evaluation_cases(references, staged_predictions)
        after = {
            "references": hashlib.sha256(
                _stable_bytes(args.references, "curriculum references")
            ).hexdigest(),
            "control_predictions": hashlib.sha256(
                _stable_bytes(args.control_predictions, "control predictions")
            ).hexdigest(),
            "staged_predictions": hashlib.sha256(
                _stable_bytes(args.staged_predictions, "staged predictions")
            ).hexdigest(),
        }
        if before != after:
            raise ValueError("curriculum evaluation inputs changed while they were read")

        config_payload = _stable_bytes(args.config, "curriculum comparison config")
        config = load_curriculum_comparison_config(args.config)
        if _stable_bytes(args.config, "curriculum comparison config") != config_payload:
            raise ValueError("curriculum comparison config changed while it was read")
        config_sha256 = hashlib.sha256(config_payload).hexdigest()
        report = compare_curriculum(
            control_cases,
            staged_cases,
            control_eval_loss=control_loss,
            staged_eval_loss=staged_loss,
            config=config,
            config_sha256=config_sha256,
            inputs={
                "references_sha256": before["references"],
                "control_predictions_sha256": before["control_predictions"],
                "staged_predictions_sha256": before["staged_predictions"],
                "control_training_report_sha256": control_report_sha256,
                "staged_training_report_sha256": staged_report_sha256,
                "case_count": len(references),
            },
        )
        write_curriculum_comparison_report(report, args.output)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"curriculum_comparison=failed reason={error}", file=sys.stderr)
        return 2
    print(
        f"curriculum_comparison=complete decision={report['decision']} "
        f"delta={report['composite_delta']['actual']:.8f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
