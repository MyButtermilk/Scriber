"""Build local, immutable HF cost evidence from terminal campaign jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_quantization_pipeline import (  # noqa: E402
    bind_hf_auxiliary_cost_job,
    bind_hf_final_evaluation_cost_attempt,
    write_hf_actual_cost_evidence,
)


def _read_attempt(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"final-evaluation attempt must be a JSON object: {path}")
    expected = {
        "job_id",
        "terminal_status",
        "flavor",
        "timeout_minutes",
        "running_seconds",
        "last_pre_terminal_running_seconds",
        "packet_tree_sha256",
        "result_sha256",
    }
    if set(value) != expected:
        raise ValueError(f"final-evaluation attempt must contain exactly {sorted(expected)}: {path}")
    return bind_hf_final_evaluation_cost_attempt(
        job_id=str(value["job_id"]),
        terminal_status=str(value["terminal_status"]),
        flavor=str(value["flavor"]),
        running_seconds=value["running_seconds"],  # type: ignore[arg-type]
        packet_tree_sha256=str(value["packet_tree_sha256"]),
        result_sha256=value["result_sha256"],  # type: ignore[arg-type]
        timeout_minutes=value["timeout_minutes"],  # type: ignore[arg-type]
        last_pre_terminal_running_seconds=value["last_pre_terminal_running_seconds"],  # type: ignore[arg-type]
    )


def _read_auxiliary_job(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"auxiliary job must be a JSON object: {path}")
    required = {
        "job_id",
        "role",
        "terminal_status",
        "flavor",
        "running_seconds",
        "packet_tree_sha256",
        "training_steps_executed",
        "evaluation_reports_written",
        "prediction_records_written",
    }
    if frozenset(value) not in {
        frozenset(required),
        frozenset(required | {"observed_inventory_tree_sha256"}),
    }:
        raise ValueError(
            f"auxiliary job must contain exactly {sorted(required)} and optional observed_inventory_tree_sha256: {path}"
        )
    return bind_hf_auxiliary_cost_job(
        job_id=str(value["job_id"]),
        role=str(value["role"]),
        terminal_status=str(value["terminal_status"]),
        flavor=str(value["flavor"]),
        running_seconds=value["running_seconds"],  # type: ignore[arg-type]
        packet_tree_sha256=str(value["packet_tree_sha256"]),
        observed_inventory_tree_sha256=value.get("observed_inventory_tree_sha256"),  # type: ignore[arg-type]
        training_steps_executed=value["training_steps_executed"],  # type: ignore[arg-type]
        evaluation_reports_written=value["evaluation_reports_written"],  # type: ignore[arg-type]
        prediction_records_written=value["prediction_records_written"],  # type: ignore[arg-type]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieved-at-utc", required=True)
    parser.add_argument(
        "--final-evaluation-attempt",
        action="append",
        type=Path,
        required=True,
        help=(
            "Repeat in chronological order; each JSON object contains job_id, "
            "terminal_status, flavor, timeout_minutes, running_seconds, "
            "last_pre_terminal_running_seconds, packet_tree_sha256, and "
            "result_sha256. A duration-unavailable CANCELED job keeps "
            "running_seconds and result_sha256 null."
        ),
    )
    parser.add_argument(
        "--auxiliary-job",
        action="append",
        type=Path,
        required=True,
        help=(
            "Repeat in chronological order for every terminal auxiliary job; "
            "full-training jobs retain their observed training-step count; "
            "all diagnostic/toolchain jobs retain zero side-effect counters. "
            "Final-evaluation cancellations belong in "
            "--final-evaluation-attempt, never here."
        ),
    )
    parser.add_argument(
        "--no-other-campaign-jobs-running",
        action="store_true",
        required=True,
        help="Attest that the current read-only HF job listing has no active campaign job.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    attempts = [_read_attempt(path.expanduser().resolve()) for path in args.final_evaluation_attempt]
    auxiliary_jobs = [_read_auxiliary_job(path.expanduser().resolve()) for path in args.auxiliary_job]
    output = write_hf_actual_cost_evidence(
        args.output,
        retrieved_at_utc=args.retrieved_at_utc,
        final_evaluation_attempts=attempts,
        auxiliary_jobs=auxiliary_jobs,
        no_other_campaign_jobs_running=args.no_other_campaign_jobs_running,
    )
    payload = output.read_bytes()
    print(
        json.dumps(
            {
                "output": str(output),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "final_evaluation_attempt_count": len(attempts),
                "auxiliary_job_count": len(auxiliary_jobs),
                "external_job_started": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
