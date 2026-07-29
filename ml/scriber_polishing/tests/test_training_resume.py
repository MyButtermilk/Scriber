from __future__ import annotations

from scriber_polishing.train import TrainingConfig, build_smoke_resume_plan


def test_smoke_resume_plan_is_deterministic_and_resumes_from_the_latest_checkpoint(tmp_path) -> None:
    output = tmp_path / "runs"
    (output / "checkpoint-2").mkdir(parents=True)
    (output / "checkpoint-10").mkdir()
    config = TrainingConfig(output_dir=output, seed=17, max_steps=12, save_steps=2)

    plan = build_smoke_resume_plan(config)

    assert plan["base_model"] == "google/gemma-3-270m-it"
    assert plan["resume_from_checkpoint"] == output / "checkpoint-10"
    assert plan["seed"] == 17
    assert plan["do_sample"] is False
