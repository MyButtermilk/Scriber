from scriber_polishing.deduplicate import audit_deduplication


def test_audit_groups_exact_normalized_and_near_duplicate_families_reproducibly() -> None:
    records = (
        {
            "example_id": "a",
            "canonical_document_id": "doc-a",
            "source_text": "Bitte prüfen Sie den Betrag von 1.200 Euro.",
        },
        {
            "example_id": "b",
            "canonical_document_id": "doc-b",
            "source_text": " bitte  prüfen  sie den betrag von 1.200 euro ",
        },
        {
            "example_id": "c",
            "canonical_document_id": "doc-c",
            "source_text": "Bitte prüfen Sie den Betrag von 1.200 Euro heute.",
        },
        {
            "example_id": "d",
            "canonical_document_id": "doc-d",
            "source_text": "Unabhängiger Vorgang mit anderer Aussage.",
        },
    )

    first = audit_deduplication(records, near_duplicate_threshold=0.70)
    second = audit_deduplication(tuple(reversed(records)), near_duplicate_threshold=0.70)

    assert first == second
    assert first.exact_groups == ()
    assert first.normalized_groups == (("a", "b"),)
    assert first.near_duplicate_groups == (("a", "b", "c"),)
    assert first.family_leakage == (("doc-a", "doc-b", "doc-c"),)


def test_audit_rejects_missing_identity_and_duplicate_example_ids() -> None:
    try:
        audit_deduplication(
            (
                {"example_id": "same", "canonical_document_id": "doc-a", "source_text": "Text"},
                {"example_id": "same", "canonical_document_id": "doc-b", "source_text": "Anderer Text"},
            )
        )
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate example ids must fail closed")
