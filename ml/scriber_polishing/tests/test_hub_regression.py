from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.hub_regression import (
    HubRegressionError,
    load_hub_regression_suite,
    materialize_hub_regression_suite,
    run_hub_regression_suite,
)

_CATEGORIES = (
    "ai_gold_smoke",
    "protected_span",
    "business_post",
    "tax_law",
    "paragraphs",
    "headings",
    "lists",
    "units",
    "legal_citations",
    "address_pronouns",
    "identity",
)


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_records() -> list[dict[str, object]]:
    records = []
    for index in range(110):
        category = _CATEGORIES[index % len(_CATEGORIES)]
        protected = f"AZ-{index:04d}"
        records.append(
            {
                "schema_version": 1,
                "case_id": f"hub-{index:04d}",
                "source_kind": "ai_gold_smoke" if category == "ai_gold_smoke" else "synthetic_regression",
                "source_partition": (
                    "ai_gold_validation"
                    if category == "ai_gold_smoke"
                    else "synthetic_non_holdout"
                ),
                "category": category,
                "source_text": f"bitte bestätigen sie den vorgang {protected}",
                "protected_spans": [protected],
                "private_data": False,
                "holdout": False,
            }
        )
    return records


def _prediction(case: dict[str, object]) -> str:
    protected = case["protected_spans"][0]
    return f"[DOC]\n[P]Bitte bestätigen Sie den Vorgang {protected}.[/P]\n[/DOC]"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _policy() -> dict[str, object]:
    return {
        "file": "examples/regression_sample.jsonl",
        "minimum_cases": 100,
        "ai_gold_smoke_minimum": 10,
        "required_categories": list(_CATEGORIES),
        "max_new_tokens": 1536,
    }


def test_materialized_suite_runs_110_hash_bound_regressions_and_all_renderers(
    tmp_path: Path,
) -> None:
    sources = _source_records()
    source_path = tmp_path / "sources.jsonl"
    prediction_path = tmp_path / "predictions.jsonl"
    suite_path = tmp_path / "suite.jsonl"
    _write_jsonl(source_path, sources)
    _write_jsonl(
        prediction_path,
        [
            {
                "case_id": case["case_id"],
                "prediction_sst": _prediction(case),
            }
            for case in sources
        ],
    )

    materialized = materialize_hub_regression_suite(
        source_path,
        prediction_path,
        suite_path,
        policy=_policy(),
    )
    loaded, evidence = load_hub_regression_suite(suite_path, _policy())
    result = run_hub_regression_suite(
        suite_path,
        _policy(),
        predictor=lambda case: {
            "prediction_sst": _prediction(case),
            "valid_sst": True,
            "restoration_error": None,
            "generation_seconds": 0.001,
        },
    )

    assert materialized == evidence
    assert len(loaded) == 110
    assert result["status"] == "passed"
    assert result["case_count"] == 110
    assert result["ai_gold_smoke_count"] == 10
    assert result["source_partition_counts"] == {
        "ai_gold_validation": 10,
        "synthetic_non_holdout": 100,
    }
    assert result["exact_output_hash_matches"] == 110
    assert result["protected_span_checks"] == 110
    assert result["sst_round_trip_checks"] == 110
    assert result["renderer_checks"] == {
        "html": 110,
        "html_clipboard": 110,
        "markdown": 110,
        "plain_text": 110,
        "sst": 110,
    }
    assert result["latency_ms"]["sample_count"] == 110
    assert result["cases_sha256"] == _sha256(suite_path.read_text(encoding="utf-8"))


def test_suite_rejects_holdouts_missing_categories_and_too_few_ai_gold_cases(
    tmp_path: Path,
) -> None:
    sources = _source_records()
    sources[0]["holdout"] = True
    source_path = tmp_path / "sources.jsonl"
    prediction_path = tmp_path / "predictions.jsonl"
    _write_jsonl(source_path, sources)
    _write_jsonl(
        prediction_path,
        [
            {
                "case_id": case["case_id"],
                "prediction_sst": _prediction(case),
            }
            for case in sources
        ],
    )

    with pytest.raises(HubRegressionError, match="holdout"):
        materialize_hub_regression_suite(
            source_path,
            prediction_path,
            tmp_path / "suite.jsonl",
            policy=_policy(),
        )
    assert not (tmp_path / "suite.jsonl").exists()


def test_fresh_reload_rejects_one_changed_model_output(tmp_path: Path) -> None:
    sources = _source_records()
    source_path = tmp_path / "sources.jsonl"
    prediction_path = tmp_path / "predictions.jsonl"
    suite_path = tmp_path / "suite.jsonl"
    _write_jsonl(source_path, sources)
    _write_jsonl(
        prediction_path,
        [
            {
                "case_id": case["case_id"],
                "prediction_sst": _prediction(case),
            }
            for case in sources
        ],
    )
    materialize_hub_regression_suite(
        source_path,
        prediction_path,
        suite_path,
        policy=_policy(),
    )

    def changed(case: dict[str, object]) -> dict[str, object]:
        prediction = _prediction(case)
        if case["case_id"] == "hub-0042":
            prediction = prediction.replace("Vorgang", "Fall")
        return {
            "prediction_sst": prediction,
            "valid_sst": True,
            "restoration_error": None,
            "generation_seconds": 0.001,
        }

    with pytest.raises(HubRegressionError, match="output hash"):
        run_hub_regression_suite(suite_path, _policy(), predictor=changed)


def test_cli_materializes_the_checked_in_publication_policy(tmp_path: Path) -> None:
    sources = _source_records()
    source_path = tmp_path / "sources.jsonl"
    prediction_path = tmp_path / "predictions.jsonl"
    suite_path = tmp_path / "suite.jsonl"
    _write_jsonl(source_path, sources)
    _write_jsonl(
        prediction_path,
        [
            {
                "case_id": case["case_id"],
                "prediction_sst": _prediction(case),
            }
            for case in sources
        ],
    )
    script = Path(__file__).parents[1] / "scripts" / "build_hub_regression_suite.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--sources",
            str(source_path),
            "--predictions",
            str(prediction_path),
            "--output",
            str(suite_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["case_count"] == 110
    assert suite_path.is_file()


def test_materialization_accepts_schema_versioned_batch_predictions(
    tmp_path: Path,
) -> None:
    sources = _source_records()
    source_path = tmp_path / "sources.jsonl"
    prediction_path = tmp_path / "predictions.jsonl"
    suite_path = tmp_path / "suite.jsonl"
    _write_jsonl(source_path, sources)
    _write_jsonl(
        prediction_path,
        [
            {
                "schema_version": 1,
                "case_id": case["case_id"],
                "prediction_sst": _prediction(case),
            }
            for case in sources
        ],
    )

    result = materialize_hub_regression_suite(
        source_path,
        prediction_path,
        suite_path,
        policy=_policy(),
    )

    assert result["case_count"] == 110
