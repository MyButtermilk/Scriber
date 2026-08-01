from __future__ import annotations

import unicodedata

from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.standard_predictions import build_standard_predictions
from scriber_polishing.target_renderer import render_plain_text


def test_standard_candidates_emit_a_raw_baseline_and_a_safe_minimal_sst_baseline() -> None:
    source = "äh bitte  [P]CODE[/P] morgen senden"
    references = (
        {
            "case_id": "standard-case",
            "source_text": source,
            "protected_spans": ["[P]CODE[/P]"],
        },
    )

    raw = build_standard_predictions(references, candidate="raw_transcript")
    minimal = build_standard_predictions(references, candidate="deterministic_minimal")

    assert raw == [
        {
            "schema_version": 1,
            "case_id": "standard-case",
            "prediction_sst": source,
        }
    ]
    document = parse_sst(minimal[0]["prediction_sst"])
    plain_text = render_plain_text(document)
    assert plain_text.startswith("Bitte")
    assert "äh" not in plain_text.casefold()
    assert unicodedata.normalize("NFKC", plain_text).find("[P]CODE[/P]") >= 0
