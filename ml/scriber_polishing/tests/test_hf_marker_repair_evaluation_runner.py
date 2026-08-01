from __future__ import annotations

from pathlib import Path


def test_recovery_runner_reuses_the_bound_model_without_training() -> None:
    runner = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_hf_marker_repair_evaluation.sh"
    ).read_text(encoding="utf-8")

    assert "train_sft.py" not in runner
    assert "EXPECTED_MODEL_TREE_SHA256=" in runner
    assert "EXPECTED_TRAINING_REPORT_SHA256=" in runner
    assert 'cp -a --no-preserve=ownership "$REMOTE_MODEL_ROOT"' in runner
    assert "_inventory_local_model_tree" in runner
    assert "report['checkpoint_state']['global_step'] != 146" in runner
    assert "export HF_HUB_OFFLINE=1" in runner
    assert "export TRANSFORMERS_OFFLINE=1" in runner
    assert "--resume-journal" in runner
    assert "--device cuda" in runner
    assert "scripts/evaluate_predictions.py" in runner
    assert "--gate-candidate shuffled" in runner
    assert "recovery-evaluation-result.json" in runner
