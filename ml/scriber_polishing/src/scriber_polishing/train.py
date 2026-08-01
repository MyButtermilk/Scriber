"""Deterministic, resumable training configuration without importing training extras."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model_factory import BASE_MODEL_ID, assert_base_model


@dataclass(frozen=True)
class TrainingConfig:
    output_dir: Path
    base_model: str = BASE_MODEL_ID
    seed: int = 42
    max_steps: int = 100
    save_steps: int = 25
    learning_rate: float = 2e-4

    def __post_init__(self) -> None:
        assert_base_model(self.base_model)
        if self.max_steps <= 0 or self.save_steps <= 0:
            raise ValueError("max_steps and save_steps must be positive")


def _latest_checkpoint(output_dir: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for path in output_dir.glob("checkpoint-*"):
        if path.is_dir() and path.name.removeprefix("checkpoint-").isdigit():
            candidates.append((int(path.name.removeprefix("checkpoint-")), path))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def build_smoke_resume_plan(config: TrainingConfig) -> dict[str, object]:
    """Return the explicit trainer plan, including a deterministic resume point."""
    return {
        "base_model": config.base_model,
        "output_dir": config.output_dir,
        "seed": config.seed,
        "max_steps": config.max_steps,
        "save_steps": config.save_steps,
        "learning_rate": config.learning_rate,
        "resume_from_checkpoint": _latest_checkpoint(config.output_dir),
        "do_sample": False,
    }
