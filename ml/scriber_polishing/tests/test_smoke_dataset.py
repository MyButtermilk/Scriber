from scriber_polishing.smoke_dataset import build_smoke_pairs


def test_smoke_dataset_has_exact_disjoint_reproducible_splits() -> None:
    records = [
        {
            "canonical_document_id": f"canonical-{index:04d}",
            "target_plain_text": f"Bitte prüfen Sie Vorgang {index}.",
            "target_sst": f"[DOC]\n[P]Bitte prüfen Sie Vorgang {index}.[/P]\n[/DOC]",
        }
        for index in range(320)
    ]

    first = build_smoke_pairs(records, seed=270_010)
    second = build_smoke_pairs(reversed(records), seed=270_010)

    assert first == second
    assert {key: len(value) for key, value in first.items()} == {"train": 200, "validation": 50, "test": 50}
    canonical_sets = [{item["canonical_document_id"] for item in first[split]} for split in first]
    assert not canonical_sets[0] & canonical_sets[1]
    assert not canonical_sets[0] & canonical_sets[2]
    assert not canonical_sets[1] & canonical_sets[2]
    assert all(item["source_text"] and item["target_sst"] for rows in first.values() for item in rows)
