from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.corruption_engine import ValueMapping
from scriber_polishing.dataset_builder import (
    _assert_protected_values,
    _targeted_train_recipes,
    build_dataset_pairs,
)
from scriber_polishing.document_ast import Block, BlockType, Document
from scriber_polishing.split_dataset import Split
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text


def _canonical(index: int, *, domain: str = "general_business", text: str | None = None) -> dict[str, object]:
    body = text or f"Die Prüfung {index} endet am 12.03.2026."
    document = Document((Block(BlockType.PARAGRAPH, body),))
    return {
        "schema_version": 1,
        "canonical_document_id": f"canonical_{index:04d}__variant_01",
        "parent_canonical_document_id": f"canonical_{index:04d}",
        "seed_id": f"seed_{index:04d}",
        "source_plan_batch": "batch_example",
        "plan_generator_agent": "generator_example",
        "target_writer_agent": "writer_example",
        "canonical_generator_id": "local_canonical_expander",
        "canonical_generator_version": "1",
        "variant_index": 1,
        "reviewed_package_sha256": "c" * 64,
        "domain": domain,
        "document_type": "Geschäftsnotiz",
        "language": "de-DE",
        "address_mode": "formal_sie",
        "risk_tags": ["fictional_entities"],
        "semantic_plan": {
            "facts": [],
            "entities": [],
            "structure": {"paragraph_topics": ["Prüfung"]},
        },
        "target_ast": document_to_dict(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "protected_spans": ["12.03.2026"] if "12.03.2026" in body else [],
        "applied_normalizations": [],
        "ambiguity_flags": [],
    }


def test_builds_reproducible_pairs_and_keeps_all_parent_variants_in_one_split() -> None:
    records = [
        _canonical(
            index,
            text=f"Die Prüfung {index} beginnt heute.\n\nDie Freigabe {index} folgt morgen.",
        )
        for index in range(100)
    ]
    counts = {
        Split.TRAIN: 3,
        Split.VALIDATION: 2,
        Split.TEST: 2,
        Split.CHALLENGE: 2,
    }

    first = build_dataset_pairs(records, seed=270_001, variants_per_canonical=counts)
    second = build_dataset_pairs(reversed(records), seed=270_001, variants_per_canonical=counts)

    assert first == second
    expected_count = sum(
        counts[split] * len({row["canonical_document_id"] for row in rows}) for split, rows in first.items()
    )
    assert sum(len(rows) for rows in first.values()) == expected_count
    for split, rows in first.items():
        assert all(row["split"] == split.value for row in rows)
        assert all(row["semantic_plan"] for row in rows)
        assert all(row["target_ast"] and row["target_sst"] for row in rows)
        assert all(row["reviewed_package_sha256"] == "c" * 64 for row in rows)
        assert all(row["provenance"]["synthetic_only"] is True for row in rows)
    train_operations = {operation for row in first[Split.TRAIN] for operation in row["operations"]}
    assert {"blank_line_error", "merged_paragraphs"}.issubset(train_operations)
    by_parent: dict[str, set[str]] = {}
    for rows in first.values():
        for row in rows:
            by_parent.setdefault(row["parent_canonical_document_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in by_parent.values())


def test_applies_reversible_unit_surfaces_but_preserves_hard_negative_identity() -> None:
    unit = _canonical(1, domain="real_estate_energy", text="Die Fläche beträgt 84,5 m².")
    hard_negative = _canonical(2, domain="hard_negatives", text="Die Schreibweise 84,5 m² bleibt unverändert.")
    counts = {split: 1 for split in Split}

    result = build_dataset_pairs(
        [unit, hard_negative],
        seed=270_001,
        variants_per_canonical=counts,
    )
    rows = {row["seed_id"]: row for split_rows in result.values() for row in split_rows}

    assert "84,5 Quadratmeter" in rows["seed_0001"]["source_text"]
    assert rows["seed_0001"]["value_mappings"] == [
        {"kind": "unit_surface", "source": "84,5 Quadratmeter", "target": "84,5 m²"}
    ]
    assert "84,5 m²" in rows["seed_0002"]["source_text"]
    assert rows["seed_0002"]["value_mappings"] == []


def test_applies_reversible_house_style_surface_only_to_explicit_orthography_records() -> None:
    record = _canonical(4, text="Die Rückmeldung liegt zurzeit vor.")
    record["risk_tags"] = ["orthography_pronouns"]

    result = build_dataset_pairs(
        [record],
        seed=270_001,
        variants_per_canonical={split: 1 for split in Split},
    )
    row = next(row for rows in result.values() for row in rows)

    assert "zur Zeit" in row["source_text"]
    assert row["value_mappings"] == [{"kind": "orthography_surface", "source": "zur Zeit", "target": "zurzeit"}]


def test_preserves_repeated_canonical_protected_values_without_requiring_unique_text_occurrence() -> None:
    record = _canonical(
        3,
        text="Mara Feld prüft den Vorgang. Mara Feld gibt ihn frei.",
    )
    record["protected_spans"] = ["Mara Feld"]

    result = build_dataset_pairs(
        [record],
        seed=270_001,
        variants_per_canonical={split: 1 for split in Split},
    )
    row = next(row for rows in result.values() for row in rows)

    assert row["source_text"].count("Mara Feld") == 2


def test_allows_corruption_to_add_an_unrelated_matching_pronoun_surface() -> None:
    _assert_protected_values(
        ("sie",),
        "Mara bestätigt, dass sie prüft.",
        "sie bestätigt dass sie prüft",
        (),
    )


def test_nested_protected_value_can_be_preserved_by_a_reversible_container_mapping() -> None:
    _assert_protected_values(
        ("so, dass", "dass"),
        "Wir formulieren es so, dass alles klar bleibt.",
        "Wir formulieren es sodass alles klar bleibt.",
        (ValueMapping("orthography_surface", "sodass", "so, dass"),),
    )


def test_targeted_train_recipes_close_only_explicit_coverage_families() -> None:
    orthography = _canonical(5)
    orthography["risk_tags"] = ["orthography_pronouns"]
    later_orthography_variant = _canonical(8)
    later_orthography_variant["risk_tags"] = ["orthography_pronouns"]
    later_orthography_variant["variant_index"] = 3
    hard_unit = _canonical(6, domain="hard_negatives")
    hard_unit["risk_tags"] = ["ambiguous_unit_hard_negative"]
    spelling_contrast = _canonical(9, domain="hard_negatives")
    spelling_contrast["risk_tags"] = ["orthography_pronouns", "das_dass"]
    unrelated = _canonical(7)

    assert len(_targeted_train_recipes(orthography)) == 3
    assert len(_targeted_train_recipes(later_orthography_variant)) == 2
    assert len(_targeted_train_recipes(hard_unit)) == 2
    assert len(_targeted_train_recipes(spelling_contrast)) == 8
    assert _targeted_train_recipes(unrelated) == ()


def test_spelling_contrast_coverage_variants_are_distinct_and_preserve_contrast_tokens() -> None:
    record = _canonical(
        10,
        domain="hard_negatives",
        text=(
            "Bitte prüfen Sie das Angebot, das Frau Linden vorbereitet hat, "
            "bevor Sie bestätigen, dass es vollständig ist."
        ),
    )
    record["risk_tags"] = ["orthography_pronouns", "das_dass"]
    record["protected_spans"] = ["das", "dass"]

    result = build_dataset_pairs([record], seed=270_001)
    train_rows = result[Split.TRAIN]

    assert len(train_rows) == 20
    assert len({row["source_text"] for row in train_rows}) == 20
    assert all("das" in row["source_text"] and "dass" in row["source_text"] for row in train_rows)
