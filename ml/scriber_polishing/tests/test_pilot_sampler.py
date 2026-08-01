from __future__ import annotations

import json
from pathlib import Path

import pytest

from scriber_polishing.pilot_sampler import build_pilot_subsets


def _row(split: str, index: int, *, parent: str | None = None) -> dict[str, object]:
    return {
        "example_id": f"{split}_example_{index:03d}",
        "parent_canonical_document_id": parent or f"{split}_parent_{index:03d}",
        "split": split,
        "source_text": f"roher Text {index}",
        "target_sst": f"[DOC]\n[P]Ziel {index}.[/P]\n[/DOC]",
        "domain": "tax_legal" if index % 2 else "general_business",
        "corruption_profile": "identity" if index % 3 == 0 else "punctuation_case",
        "risk_tags": ["legal"] if index % 2 else ["business_mail"],
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_builds_exact_order_independent_hash_sample_with_auditable_manifest(
    tmp_path: Path,
) -> None:
    inputs: dict[str, Path] = {}
    counts = {"train": 12, "validation": 6, "test": 5, "challenge": 4}
    for split in counts:
        path = tmp_path / f"{split}.jsonl"
        _write(path, [_row(split, index) for index in range(30)])
        inputs[split] = path

    first = build_pilot_subsets(
        inputs,
        output_root=tmp_path / "first",
        counts=counts,
        seed=270_011,
    )
    for path in inputs.values():
        rows = [
            json.loads(line)
            for line in reversed(path.read_text(encoding="utf-8").splitlines())
        ]
        _write(path, rows)
    second = build_pilot_subsets(
        inputs,
        output_root=tmp_path / "second",
        counts=counts,
        seed=270_011,
    )

    assert first["split_counts"] == counts
    assert first["split_sha256"] == second["split_sha256"]
    assert first["selection_algorithm"] == "split_seeded_sha256_bottom_k_v1"
    assert first["split_leakage"] is False
    assert set(first["distributions"]["train"]["domain"]) == {
        "general_business",
        "tax_legal",
    }
    for split, expected in counts.items():
        output = tmp_path / "first" / f"{split}.jsonl"
        assert len(output.read_text(encoding="utf-8").splitlines()) == expected


def test_pilot_sampler_rejects_wrong_split_insufficient_rows_and_parent_leakage(
    tmp_path: Path,
) -> None:
    inputs: dict[str, Path] = {}
    for split in ("train", "validation", "test", "challenge"):
        path = tmp_path / f"{split}.jsonl"
        _write(path, [_row(split, index) for index in range(3)])
        inputs[split] = path

    wrong = [_row("validation", 99)]
    _write(inputs["train"], wrong)
    with pytest.raises(ValueError, match="declares split"):
        build_pilot_subsets(
            inputs,
            output_root=tmp_path / "wrong",
            counts={split: 1 for split in inputs},
            seed=1,
        )

    _write(inputs["train"], [_row("train", 1, parent="shared")])
    _write(inputs["validation"], [_row("validation", 1, parent="shared")])
    with pytest.raises(ValueError, match="parent split leakage"):
        build_pilot_subsets(
            inputs,
            output_root=tmp_path / "leak",
            counts={split: 1 for split in inputs},
            seed=1,
        )

    _write(inputs["validation"], [_row("validation", 1)])
    with pytest.raises(ValueError, match="insufficient.*train"):
        build_pilot_subsets(
            inputs,
            output_root=tmp_path / "short",
            counts={
                "train": 2,
                "validation": 1,
                "test": 1,
                "challenge": 1,
            },
            seed=1,
        )
