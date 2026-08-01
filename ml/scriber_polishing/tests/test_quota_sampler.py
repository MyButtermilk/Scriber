from __future__ import annotations

import pytest

from scriber_polishing.dataset_policy import (
    bind_payload,
    bind_regular_file,
    load_data_generation_policy,
    read_regular_file_snapshot,
)
from scriber_polishing.quota_sampler import select_quota_preserving_records


def _records() -> list[dict[str, str]]:
    return [{"example_id": f"row-{index:02d}", "kind": kind} for index, kind in enumerate(
        ("rare-a", "rare-a", "rare-b", "rare-b", "both", "common", "common", "common")
    )]


def _evidence(record: dict[str, str]) -> frozenset[str]:
    return {
        "rare-a": frozenset({"quota-a"}),
        "rare-b": frozenset({"quota-b"}),
        "both": frozenset({"quota-a", "quota-b"}),
        "common": frozenset(),
    }[record["kind"]]


def test_quota_sampler_is_order_independent_and_fills_the_exact_target() -> None:
    records = _records()

    first = select_quota_preserving_records(
        records,
        target_count=5,
        seed=270_001,
        quota_minima={"quota-a": 2, "quota-b": 2},
        evidence_fn=_evidence,
    )
    second = select_quota_preserving_records(
        reversed(records),
        target_count=5,
        seed=270_001,
        quota_minima={"quota-a": 2, "quota-b": 2},
        evidence_fn=_evidence,
    )

    assert first.records == second.records
    assert len(first.records) == 5
    assert sum("quota-a" in _evidence(record) for record in first.records) >= 2
    assert sum("quota-b" in _evidence(record) for record in first.records) >= 2
    assert first.source_count == 8
    assert first.selected_count == 5


def test_quota_sampler_fails_when_the_target_cannot_hold_the_required_union() -> None:
    records = [
        {"example_id": "only-a", "kind": "rare-a"},
        {"example_id": "only-b", "kind": "rare-b"},
    ]

    with pytest.raises(ValueError, match="target_count cannot retain"):
        select_quota_preserving_records(
            records,
            target_count=1,
            seed=1,
            quota_minima={"quota-a": 1, "quota-b": 1},
            evidence_fn=_evidence,
        )


def test_quota_sampler_rejects_duplicate_example_ids_and_missing_quota_evidence() -> None:
    duplicate = [
        {"example_id": "same", "kind": "rare-a"},
        {"example_id": "same", "kind": "rare-b"},
    ]
    with pytest.raises(ValueError, match="duplicate example_id"):
        select_quota_preserving_records(
            duplicate,
            target_count=2,
            seed=1,
            quota_minima={"quota-a": 1},
            evidence_fn=_evidence,
        )

    with pytest.raises(ValueError, match="insufficient evidence for quota-b"):
        select_quota_preserving_records(
            [{"example_id": "only-a", "kind": "rare-a"}],
            target_count=1,
            seed=1,
            quota_minima={"quota-a": 1, "quota-b": 1},
            evidence_fn=_evidence,
        )


def test_repository_data_policy_is_the_train_target_source_of_truth() -> None:
    policy = load_data_generation_policy()

    assert policy.target_train_pairs == 120_000
    assert policy.minimum_train_pairs == 80_000
    assert policy.split_group == "parent_canonical_document_id"
    assert policy.resolve_target_train(None) == 120_000
    assert policy.resolve_target_train(120_000) == 120_000
    with pytest.raises(ValueError, match="must equal configured target_train_pairs"):
        policy.resolve_target_train(100_000)


def test_manifest_file_binding_rejects_external_or_symlinked_artifacts(tmp_path) -> None:
    root = tmp_path / "dataset"
    root.mkdir()
    report = root / "coverage.json"
    report.write_text('{"passed":true}\n', encoding="utf-8")

    binding = bind_regular_file(report, allowed_root=root)
    snapshot = read_regular_file_snapshot(report, allowed_root=root)

    assert binding["path"] == "coverage.json"
    assert binding["bytes"] == report.stat().st_size
    assert binding["sha256"].startswith("sha256:")
    report.write_text('{"passed":false}\n', encoding="utf-8")
    assert snapshot.binding == binding
    assert bind_payload(report, snapshot.payload, allowed_root=root) == binding
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the allowed root"):
        bind_regular_file(outside, allowed_root=root)
