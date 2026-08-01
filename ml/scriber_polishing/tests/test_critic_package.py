from copy import deepcopy

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.critic_package import build_blinded_case_index, build_blinded_cases
from scriber_polishing.document_ast import Block, BlockType, Document
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text


def _plan(seed_id: str = "general_business_100001") -> dict[str, object]:
    return {
        "seed_id": seed_id,
        "domain": "general_business",
        "document_type": "business_email",
        "language": "de-DE",
        "address_mode": "formal_sie",
        "risk_tags": ["formal_address", "protected_date"],
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Die Rückmeldung ist bis zum 15.08.2026 erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": ["15.08.2026"],
                }
            ],
            "entities": [],
            "structure": {"has_subject": False},
        },
        "generator_agent": "must_not_leak",
        "batch_id": "must_not_leak",
        "seed": 100001,
    }


def _target(seed_id: str = "general_business_100001") -> dict[str, object]:
    document = Document(
        blocks=(Block(BlockType.PARAGRAPH, text="Die Rückmeldung ist bis zum 15.08.2026 erforderlich."),)
    )
    return {
        "seed_id": seed_id,
        "target_ast": document_to_dict(document),
        "target_sst": render_sst(document),
        "target_plain_text": render_plain_text(document),
        "target_markdown": render_markdown(document),
        "target_html": render_html(document),
        "protected_spans": ["15.08.2026"],
        "plan_generator_agent": "must_not_leak",
        "target_writer_agent": "must_not_leak",
        "source_plan_batch": "must_not_leak",
        "canonical_document_id": "must_not_leak",
    }


def test_blinded_package_is_deterministic_and_excludes_identity_and_split_fields() -> None:
    first = build_blinded_cases([_plan()], [_target()], blind_seed=270_003)
    second = build_blinded_cases([_plan()], [_target()], blind_seed=270_003)

    assert first == second
    assert len(first) == 1
    case = first[0]
    assert case["case_id"].startswith("case_")
    serialized = repr(case)
    for forbidden in (
        "generator_agent",
        "target_writer_agent",
        "batch_id",
        "seed_id",
        "canonical_document_id",
        "split",
        "must_not_leak",
    ):
        assert forbidden not in serialized


def test_blinded_package_rejects_missing_duplicate_or_unmatched_pairs() -> None:
    with pytest.raises(ValueError, match="missing target"):
        build_blinded_cases([_plan()], [], blind_seed=1)

    duplicate = deepcopy(_target())
    with pytest.raises(ValueError, match="duplicate target"):
        build_blinded_cases([_plan()], [_target(), duplicate], blind_seed=1)

    with pytest.raises(ValueError, match="without a plan"):
        build_blinded_cases([_plan()], [_target(), _target("general_business_100002")], blind_seed=1)


def test_blinded_case_ids_remain_unique_for_semantically_identical_source_seeds() -> None:
    cases = build_blinded_cases(
        [_plan("general_business_100001"), _plan("general_business_100002")],
        [_target("general_business_100001"), _target("general_business_100002")],
        blind_seed=1,
    )

    assert len({case["case_id"] for case in cases}) == 2
    index = build_blinded_case_index(
        [_plan("general_business_100001"), _plan("general_business_100002")],
        [_target("general_business_100001"), _target("general_business_100002")],
        blind_seed=1,
    )
    assert set(index.values()) == {"general_business_100001", "general_business_100002"}
