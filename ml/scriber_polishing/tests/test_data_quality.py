from scriber_polishing.data_quality import assess_data_quality
from scriber_polishing.split_dataset import Split


def test_quality_report_rejects_duplicates_and_cross_split_canonical_families() -> None:
    records = (
        {
            "example_id": "a",
            "canonical_document_id": "doc-a",
            "source_text": "Bitte prüfen Sie den Vertrag.",
            "target_sst": "[DOC]\n[P]Bitte prüfen Sie den Vertrag.[/P]\n[/DOC]",
            "semantic_plan": {"id": "plan-a"},
            "generator_agent_id": "generator-a",
        },
        {
            "example_id": "b",
            "canonical_document_id": "doc-b",
            "source_text": " bitte prüfen sie den vertrag ",
            "target_sst": "[DOC]\n[P]Bitte prüfen Sie den Vertrag.[/P]\n[/DOC]",
            "semantic_plan": {"id": "plan-b"},
            "generator_agent_id": "generator-b",
        },
        {
            "example_id": "c",
            "canonical_document_id": "doc-a",
            "source_text": "Anderer Text.",
            "target_sst": "[DOC]\n[P]Anderer Text.[/P]\n[/DOC]",
            "semantic_plan": {"id": "plan-c"},
            "generator_agent_id": "generator-c",
        },
    )
    report = assess_data_quality(records, {"a": Split.TRAIN, "b": Split.TRAIN, "c": Split.TEST})

    assert report.accepted_ids == ()
    assert report.rejected_ids == ("a", "b", "c")
    assert report.rejection_reasons["a"] == ("duplicate_family", "split_leakage")
    assert report.rejection_reasons["b"] == ("duplicate_family",)
    assert report.rejection_reasons["c"] == ("split_leakage",)
    assert report.duplicate_rate == 2 / 3


def test_quality_report_is_reproducible_and_reports_invalid_sst() -> None:
    records = (
        {
            "example_id": "invalid",
            "canonical_document_id": "doc",
            "source_text": "Text",
            "target_sst": "not sst",
            "semantic_plan": {"id": "plan"},
            "generator_agent_id": "generator",
        },
    )

    first = assess_data_quality(records, {"invalid": Split.TRAIN})
    second = assess_data_quality(records, {"invalid": Split.TRAIN})

    assert first == second
    assert first.rejection_reasons == {"invalid": ("invalid_sst",)}
