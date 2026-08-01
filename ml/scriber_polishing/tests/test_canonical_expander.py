from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scriber_polishing.ast_codec import document_from_dict, document_to_dict
from scriber_polishing.canonical_expander import expand_canonical_variants, expand_roots
from scriber_polishing.document_ast import Block, BlockType, Document, ListItem
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text
from scriber_polishing.validators import validate_canonical_record


def _plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed_id": "example_seed_01",
        "generator_agent": "generator_example_01",
        "batch_id": "example_batch_01",
        "prompt_hash": "sha256:" + "a" * 64,
        "seed": 17,
        "language": "de-DE",
        "domain": "general_business",
        "document_type": "Projektabstimmung",
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Asterwerk Solutions GmbH darf Projekt Atlas nicht freigeben.",
                    "polarity": "negative",
                    "modality": "obligation",
                    "condition": "wenn die Unterlagen fehlen",
                    "protected_values": ["14.08.2026", "12 %"],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Mara Feld empfiehlt die Prüfung am Standort Fiktivstadt.",
                    "polarity": "positive",
                    "modality": "recommendation",
                    "condition": None,
                    "protected_values": [],
                },
            ],
            "entities": [
                {"type": "organization", "value": "Asterwerk Solutions GmbH", "fictional": True, "protected": True},
                {"type": "organization", "value": "Lumenpfad Services GmbH", "fictional": True, "protected": True},
                {"type": "person", "value": "Mara Feld", "fictional": True, "protected": True},
                {"type": "location", "value": "Fiktivstadt", "fictional": True, "protected": True},
                {"type": "product", "value": "Projekt Atlas", "fictional": True, "protected": True},
                {"type": "technical_term", "value": "ARC-184", "fictional": True, "protected": True},
            ],
            "structure": {
                "has_subject": True,
                "has_salutation": True,
                "paragraph_topics": ["Abstimmung", "Freigabe"],
                "heading_levels": ["h2"],
                "list_kind": "ordered",
                "list_items": ["Prüfung", "Freigabe"],
                "has_closing": True,
                "signature_lines": ["Mara Feld", "Fiktive Projektkoordination"],
                "has_quote": True,
                "quote_text": "Die Freigabe bleibt bis zur Prüfung offen.",
                "has_attachments": True,
                "attachment_lines": ["Anlage: Prüfliste"],
                "has_post_script": True,
                "post_script_text": "Bitte beachten Sie die Frist.",
            },
        },
        "risk_tags": ["fictional_entities"],
        "source_class": "ai_generated",
        "private_data": False,
    }


def _target(plan: dict[str, object]) -> dict[str, object]:
    document = Document(
        blocks=(
            Block(BlockType.SUBJECT, "Projekt Atlas – Schriftliche Abstimmung"),
            Block(BlockType.SALUTATION, "Guten Tag,"),
            Block(BlockType.HEADING_2, "Projekt Atlas"),
            Block(
                BlockType.PARAGRAPH,
                "Die fiktive Asterwerk Solutions GmbH stimmt sich mit der fiktiven Lumenpfad Services GmbH ab.",
            ),
            Block(BlockType.PARAGRAPH, "Mara Feld koordiniert die Prüfung am Standort Fiktivstadt für ARC-184."),
            Block(
                BlockType.PARAGRAPH,
                "Die Freigabe darf nicht erfolgen, wenn die Unterlagen fehlen; die Frist endet am 14.08.2026 bei 12 %.",
            ),
            Block(BlockType.QUOTE, "Die Freigabe bleibt bis zur Prüfung offen."),
            Block(
                BlockType.ORDERED_LIST,
                items=(ListItem(1, "Prüfung"), ListItem(1, "Freigabe")),
            ),
            Block(BlockType.CLOSING, "Freundliche Grüße"),
            Block(BlockType.SIGNATURE, lines=("Mara Feld", "Fiktive Projektkoordination")),
            Block(BlockType.ATTACHMENTS, "Anlage: Prüfliste"),
            Block(BlockType.POST_SCRIPT, "Bitte beachten Sie die Frist."),
        )
    )
    semantic = plan["semantic_plan"]
    return {
        "schema_version": 1,
        "canonical_document_id": "canonical_example_01",
        "seed_id": plan["seed_id"],
        "plan_generator_agent": plan["generator_agent"],
        "source_plan_batch": plan["batch_id"],
        "semantic_plan_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "target_writer_agent": "target_writer_business_technical_01",
        "target_prompt_hash": "sha256:" + "b" * 64,
        "language": "de-DE",
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "normalize_valid_variants": False,
        "source_text": render_plain_text(document),
        "target_ast": document_to_dict(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "protected_spans": [
            "Asterwerk Solutions GmbH",
            "Lumenpfad Services GmbH",
            "Mara Feld",
            "Fiktivstadt",
            "Projekt Atlas",
            "ARC-184",
            "14.08.2026",
            "12 %",
        ],
        "ambiguity_flags": [],
        "applied_normalizations": [],
        "rejected_normalizations": [],
    }


def test_expands_exactly_seven_deterministic_valid_ast_variants() -> None:
    plan = _plan()
    target = _target(plan)

    first = expand_canonical_variants(plan, target)
    second = expand_canonical_variants(plan, target)

    assert first == second
    assert len(first) == 7
    assert [record["canonical_document_id"] for record in first] == [
        f"canonical_example_01__variant_{index:02d}" for index in range(1, 8)
    ]
    assert len({record["semantic_plan_sha256"] for record in first}) == 7
    assert {record["source_plan_batch"] for record in first} == {"example_batch_01"}
    for index, record in enumerate(first, start=1):
        assert record["parent_canonical_document_id"] == "canonical_example_01"
        assert record["canonical_generator_id"] == "local_canonical_expander"
        assert record["canonical_generator_version"] == "1"
        assert record["variant_index"] == index
        assert (
            record["semantic_plan_sha256"]
            == "sha256:"
            + hashlib.sha256(
                json.dumps(
                    record["semantic_plan"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        validate_canonical_record(record)


def test_substitutes_entity_types_consistently_and_preserves_protected_facts_and_structure() -> None:
    plan = _plan()
    target = _target(plan)

    variants = expand_canonical_variants(plan, target)

    assert len({record["target_plain_text"] for record in variants}) == 7
    for record in variants:
        text = record["target_plain_text"]
        assert "14.08.2026" in text
        assert "12 %" in text
        assert "nicht erfolgen" in text
        assert "wenn die Unterlagen fehlen" in text
        assert "ARC-184" in text
        assert "Asterwerk Solutions GmbH" not in text
        assert "Mara Feld" not in text
        entity_values = {entity["value"] for entity in record["semantic_plan"]["entities"]}
        assert "ARC-184" in entity_values
        assert "Asterwerk Solutions GmbH" not in entity_values
        assert record["semantic_plan"]["facts"][0]["text"].split()[0] in text
        signature_person = record["target_ast"]["blocks"][9]["lines"][0]
        assert text.count(signature_person) >= 2
        block_types = [block["type"] for block in record["target_ast"]["blocks"]]
        assert block_types == [block["type"] for block in target["target_ast"]["blocks"]]


def test_preserves_spelled_out_article_citations_as_normative_entities() -> None:
    plan = _plan()
    article = "Artikel 3 Absatz 1 GG"
    plan["semantic_plan"]["entities"].append(
        {"type": "legal_term", "value": article, "fictional": True, "protected": True}
    )
    plan["semantic_plan"]["facts"][1]["text"] += f" Maßgeblich bleibt {article}."
    plan["semantic_plan"]["facts"][1]["protected_values"].append(article)
    target = _target(plan)
    document = document_from_dict(target["target_ast"])
    document = Document(
        blocks=(
            *document.blocks[:5],
            Block(
                BlockType.PARAGRAPH,
                f"{document.blocks[5].text} Maßgeblich bleibt {article}.",
            ),
            *document.blocks[6:],
        )
    )
    target["target_ast"] = document_to_dict(document)
    target["target_sst"] = render_sst(document)
    target["target_plain_text"] = render_plain_text(document)
    target["target_markdown"] = render_markdown(document)
    target["target_html"] = render_html(document)
    target["source_text"] = render_plain_text(document)
    target["protected_spans"].append(article)

    variants = expand_canonical_variants(plan, target)

    assert len(variants) == 7
    assert all(article in record["target_plain_text"] for record in variants)
    assert all(article in record["protected_spans"] for record in variants)
    assert all(article in {entity["value"] for entity in record["semantic_plan"]["entities"]} for record in variants)


def test_rejects_mismatched_seed_and_invalid_baseline() -> None:
    plan = _plan()
    target = _target(plan)
    target["seed_id"] = "other_seed_01"

    with pytest.raises(ValueError, match="seed_id"):
        expand_canonical_variants(plan, target)


def test_rejects_baseline_structure_not_allowed_by_plan() -> None:
    plan = _plan()
    plan["semantic_plan"]["structure"]["has_quote"] = False
    target = _target(plan)

    with pytest.raises(ValueError, match="quote"):
        expand_canonical_variants(plan, target)


def test_allows_repeated_heading_levels_declared_by_the_plan() -> None:
    plan = _plan()
    target = _target(plan)
    document = document_from_dict(target["target_ast"])
    document = Document(
        blocks=(
            *document.blocks[:5],
            Block(BlockType.HEADING_2, "Weitere Abstimmung"),
            *document.blocks[5:],
        )
    )
    target["target_ast"] = document_to_dict(document)
    target["target_sst"] = render_sst(document)
    target["target_plain_text"] = render_plain_text(document)
    target["target_markdown"] = render_markdown(document)
    target["target_html"] = render_html(document)
    target["source_text"] = render_plain_text(document)

    variants = expand_canonical_variants(plan, target)

    assert len(variants) == 7


def test_uses_ast_only_format_variants_when_a_valid_plan_has_no_entities() -> None:
    plan = _plan()
    plan["semantic_plan"]["entities"] = []
    target = _target(plan)

    variants = expand_canonical_variants(plan, target)

    assert len({record["target_sst"] for record in variants}) == 7
    assert {record["target_plain_text"] for record in variants} == {target["target_plain_text"]}


def test_expands_explicit_roots_and_writes_checksum_manifest(tmp_path: Path) -> None:
    plan = _plan()
    target = _target(plan)
    seed_root = tmp_path / "seeds"
    target_root = tmp_path / "targets"
    output = tmp_path / "expanded.jsonl"
    manifest = tmp_path / "expanded.manifest.json"
    seed_root.mkdir()
    target_root.mkdir()
    (seed_root / "plans.jsonl").write_text(json.dumps(plan) + "\n", encoding="utf-8")
    (target_root / "targets.jsonl").write_text(json.dumps(target) + "\n", encoding="utf-8")

    result = expand_roots(seed_root, target_root, output, manifest, minimum_count=7)

    assert result["record_count"] == 7
    assert result["sha256"] == "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    assert json.loads(manifest.read_text(encoding="utf-8")) == result
    assert len(output.read_text(encoding="utf-8").splitlines()) == 7


def test_expands_multiple_explicit_target_roots(tmp_path: Path) -> None:
    first_plan = _plan()
    first_target = _target(first_plan)
    second_plan = copy.deepcopy(first_plan)
    second_plan["seed_id"] = "seed_expansion_02"
    second_target = copy.deepcopy(first_target)
    second_target["seed_id"] = second_plan["seed_id"]
    seed_root = tmp_path / "seeds"
    first_target_root = tmp_path / "targets_a"
    second_target_root = tmp_path / "targets_b"
    output = tmp_path / "expanded.jsonl"
    manifest = tmp_path / "expanded.manifest.json"
    seed_root.mkdir()
    first_target_root.mkdir()
    second_target_root.mkdir()
    (seed_root / "plans.jsonl").write_text(
        f"{json.dumps(first_plan)}\n{json.dumps(second_plan)}\n",
        encoding="utf-8",
    )
    (first_target_root / "targets.jsonl").write_text(
        json.dumps(first_target) + "\n",
        encoding="utf-8",
    )
    (second_target_root / "targets.jsonl").write_text(
        json.dumps(second_target) + "\n",
        encoding="utf-8",
    )

    result = expand_roots(
        seed_root,
        (first_target_root, second_target_root),
        output,
        manifest,
        minimum_count=14,
    )

    assert result["record_count"] == 14
    assert len(output.read_text(encoding="utf-8").splitlines()) == 14


def test_expands_multiple_explicit_seed_and_target_roots(tmp_path: Path) -> None:
    first_plan = _plan()
    first_target = _target(first_plan)
    second_plan = copy.deepcopy(first_plan)
    second_plan["seed_id"] = "seed_expansion_02"
    second_target = copy.deepcopy(first_target)
    second_target["seed_id"] = second_plan["seed_id"]
    second_target["canonical_document_id"] = "canonical_example_02"
    first_seed_root = tmp_path / "seeds_a"
    second_seed_root = tmp_path / "seeds_b"
    first_target_root = tmp_path / "targets_a"
    second_target_root = tmp_path / "targets_b"
    output = tmp_path / "expanded.jsonl"
    manifest = tmp_path / "expanded.manifest.json"
    for root in (first_seed_root, second_seed_root, first_target_root, second_target_root):
        root.mkdir()
    (first_seed_root / "plans.jsonl").write_text(json.dumps(first_plan) + "\n", encoding="utf-8")
    (second_seed_root / "plans.jsonl").write_text(json.dumps(second_plan) + "\n", encoding="utf-8")
    (first_target_root / "targets.jsonl").write_text(json.dumps(first_target) + "\n", encoding="utf-8")
    (second_target_root / "targets.jsonl").write_text(json.dumps(second_target) + "\n", encoding="utf-8")

    result = expand_roots(
        (first_seed_root, second_seed_root),
        (first_target_root, second_target_root),
        output,
        manifest,
        minimum_count=14,
    )

    assert result["record_count"] == 14
    assert len(output.read_text(encoding="utf-8").splitlines()) == 14


def test_filters_and_binds_complete_independent_critic_approval(tmp_path: Path) -> None:
    plan = _plan()
    target = _target(plan)
    seed_root = tmp_path / "seeds"
    target_root = tmp_path / "targets"
    output = tmp_path / "expanded.jsonl"
    manifest = tmp_path / "expanded.manifest.json"
    approval = tmp_path / "approval.json"
    seed_root.mkdir()
    target_root.mkdir()
    (seed_root / "plans.jsonl").write_text(json.dumps(plan) + "\n", encoding="utf-8")
    (target_root / "targets.jsonl").write_text(json.dumps(target) + "\n", encoding="utf-8")
    approval.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_sha256": "c" * 64,
                "accepted_seed_ids": [plan["seed_id"]],
                "critic_ids": {
                    "critic_fact_terra_01": "fact",
                    "critic_style_terra_01": "style",
                    "critic_adversarial_sol_01": "adversarial",
                    "critic_adversarial_sol_02": "adversarial",
                },
                "approval_rule": "all_independent_critics_acceptable_no_critical_flags",
            }
        ),
        encoding="utf-8",
    )

    result = expand_roots(
        seed_root,
        target_root,
        output,
        manifest,
        minimum_count=7,
        approval=approval,
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert result["critic_approved"] is True
    assert result["reviewed_package_sha256"] == "c" * 64
    assert all(len(record["critic_agents"]) == 4 for record in records)
    assert all(all(item["acceptable"] for item in record["critic_decisions"]) for record in records)
