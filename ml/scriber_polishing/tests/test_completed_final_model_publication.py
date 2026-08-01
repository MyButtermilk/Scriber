from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

from scriber_polishing.checkpoint_selection import CheckpointSelectionError


def _script_module():
    script = (
        Path(__file__).parents[1]
        / "scripts"
        / "prepare_completed_final_model_publication.py"
    )
    spec = importlib.util.spec_from_file_location(
        "prepare_completed_final_model_publication",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evaluation_result() -> dict[str, object]:
    reports = []
    for candidate in ("base_model", "final"):
        for suite in ("ai_gold", "critical_regression", "regular_test"):
            reports.append(
                {
                    "path": f"{candidate}/{suite}/evaluation.json",
                    "bytes": 100,
                    "sha256": "sha256:" + "a" * 64,
                }
            )
    return {
        "schema_version": 1,
        "kind": "scriber_full_model_final_evaluation_result",
        "model_tree_sha256": "sha256:" + "b" * 64,
        "training_steps_executed": 0,
        "reports": reports,
    }


def test_full_model_evaluation_result_binds_exact_closed_inventory() -> None:
    module = _script_module()
    result = _evaluation_result()

    module._validate_final_evaluation_result(result)

    duplicate = deepcopy(result)
    duplicate["reports"][-1] = deepcopy(duplicate["reports"][0])
    with pytest.raises(CheckpointSelectionError, match="failed schema|six required"):
        module._validate_final_evaluation_result(duplicate)

    unexpected = deepcopy(result)
    unexpected["unbound"] = True
    with pytest.raises(CheckpointSelectionError, match="Additional properties"):
        module._validate_final_evaluation_result(unexpected)
