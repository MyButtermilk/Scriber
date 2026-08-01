"""Regression checks for the isolated coverage-completion target batch."""

from __future__ import annotations

import json
from pathlib import Path

from scriber_polishing.canonical_expander import expand_canonical_variants
from scriber_polishing.validators import validate_canonical_record

ROOT = Path(__file__).resolve().parent
PLAN_PATH = ROOT.parents[1] / "seeds" / "generator_coverage_completion_01" / "plans.jsonl"


def test_coverage_completion_targets_are_complete_and_expandable() -> None:
    plans = {json.loads(line)["seed_id"]: json.loads(line) for line in PLAN_PATH.read_text(encoding="utf-8").splitlines() if line}
    records = [json.loads(line) for line in (ROOT / "targets.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 300
    assert len({record["seed_id"] for record in records}) == 300
    for record in records:
        validate_canonical_record(record)
        assert record["source_text"] == record["target_plain_text"]
        assert len(expand_canonical_variants(plans[record["seed_id"]], record)) == 7
