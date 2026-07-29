from scriber_polishing.split_dataset import (
    DatasetExample,
    Split,
    assign_document_splits,
    validate_no_split_leakage,
)


def test_all_variants_of_a_canonical_document_share_one_split() -> None:
    examples = [
        DatasetExample(example_id="doc-a-v1", canonical_document_id="doc-a"),
        DatasetExample(example_id="doc-a-v2", canonical_document_id="doc-a"),
        DatasetExample(example_id="doc-b-v1", canonical_document_id="doc-b"),
        DatasetExample(example_id="doc-c-v1", canonical_document_id="doc-c"),
    ]

    first = assign_document_splits(examples, seed=270_001)
    second = assign_document_splits(reversed(examples), seed=270_001)

    assert first == second
    assert first["doc-a-v1"] is first["doc-a-v2"]
    assert validate_no_split_leakage(examples, first) == ()


def test_split_leakage_reports_the_document_and_conflicting_splits() -> None:
    examples = [
        DatasetExample(example_id="doc-a-v1", canonical_document_id="doc-a"),
        DatasetExample(example_id="doc-a-v2", canonical_document_id="doc-a"),
    ]
    assignments = {
        "doc-a-v1": Split.TRAIN,
        "doc-a-v2": Split.TEST,
    }

    assert validate_no_split_leakage(examples, assignments) == ("doc-a: test, train",)
