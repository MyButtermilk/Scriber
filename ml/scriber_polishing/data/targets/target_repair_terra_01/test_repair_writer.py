"""Focused, local regression tests for the deterministic target repair."""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("repair_writer.py")
SPEC = importlib.util.spec_from_file_location("target_repair_writer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
WRITER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WRITER
SPEC.loader.exec_module(WRITER)


def sample_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed_id": "repair_test_0001",
        "generator_agent": "generator_test_01",
        "batch_id": "repair_test_batch",
        "prompt_hash": "sha256:" + "0" * 64,
        "seed": 1,
        "language": "de-DE",
        "domain": "general_business",
        "document_type": "formales_schreiben",
        "style_profile": "duden_preferred_business",
        "address_mode": "formal_sie",
        "legal_citation_style": "long",
        "unit_style": "professional_symbols",
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Die fiktive Aster GmbH bezieht sich auf die Mitteilung des Finanzamt Beispiel.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": ["Aster GmbH", "Finanzamt Beispiel"],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Rückmeldung ist bis zum 01.09.2030 erforderlich.",
                    "polarity": "positive",
                    "modality": "obligation",
                    "condition": None,
                    "protected_values": ["01.09.2030"],
                },
            ],
            "entities": [
                {"type": "organization", "value": "Aster GmbH", "fictional": True, "protected": True},
                {
                    "type": "organization",
                    "value": "Finanzamt Beispiel",
                    "fictional": True,
                    "protected": True,
                },
            ],
            "structure": {
                "has_subject": True,
                "has_salutation": False,
                "paragraph_topics": ["Rückmeldung"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "risk_tags": [],
        "source_class": "ai_generated",
        "private_data": False,
    }


class RepairWriterTests(unittest.TestCase):
    def test_removes_fiction_labels_and_repairs_finanzamt_government(self) -> None:
        record, _ = WRITER.make_record(sample_plan(), prompt_hash="sha256:" + "1" * 64)
        text = record["target_plain_text"]
        self.assertNotIn("fiktive", text.casefold())
        self.assertIn("Mitteilung von Finanzamt Beispiel", text)
        self.assertIn("Aster GmbH", text)
        self.assertIn("Finanzamt Beispiel", text)

    def test_empty_heading_contract_never_emits_a_heading_block(self) -> None:
        document, _ = WRITER.make_document(sample_plan())
        self.assertFalse(
            any(block.type in {WRITER.BlockType.HEADING_1, WRITER.BlockType.HEADING_2} for block in document.blocks)
        )

    def test_redundant_organization_role_label_is_not_emitted_as_prose(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["facts"][0]["text"] = "Die fiktive Organisation Aster GmbH bearbeitet die Rückmeldung."
        plan["semantic_plan"]["facts"][0]["protected_values"] = ["Aster GmbH"]
        plan["semantic_plan"]["entities"] = [
            {"type": "organization", "value": "Aster GmbH", "fictional": True, "protected": True}
        ]
        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        self.assertIn("Aster GmbH bearbeitet die Rückmeldung.", record["target_plain_text"])
        self.assertNotIn("Die Organisation Aster GmbH", record["target_plain_text"])

    def test_signature_lines_are_preserved_exactly_for_canonical_expansion(self) -> None:
        plan = copy.deepcopy(sample_plan())
        expected_lines = ["Aster GmbH", "Fiktive Projektkoordination"]
        plan["semantic_plan"]["structure"]["has_closing"] = True
        plan["semantic_plan"]["structure"]["signature_lines"] = expected_lines
        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        signature = next(block for block in record["target_ast"]["blocks"] if block["type"] == "signature")
        self.assertEqual(signature["lines"], expected_lines)
        self.assertEqual(WRITER.validate_canonical_expansion(plan, record), 7)

    def test_every_sample_target_expands_to_seven_unique_variants(self) -> None:
        plan = sample_plan()
        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        variants = WRITER.expand_canonical_variants(plan, record)
        self.assertEqual(len(variants), 7)
        self.assertEqual(len({variant["target_sst"] for variant in variants}), 7)

    def test_viable_nested_list_contains_a_real_child_level(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["structure"]["list_kind"] = "nested"
        plan["semantic_plan"]["structure"]["list_items"] = ["Prüfung", "Frist"]
        document, _ = WRITER.make_document(plan)
        list_block = next(block for block in document.blocks if block.type is WRITER.BlockType.UNORDERED_LIST)
        self.assertEqual([item.level for item in list_block.items], [1, 2])

    def test_one_item_nested_plan_is_rejected_without_inventing_a_child(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["structure"]["list_kind"] = "nested"
        plan["semantic_plan"]["structure"]["list_items"] = ["Nur ein Punkt"]
        with self.assertRaises(WRITER.IrreparablePlan) as raised:
            WRITER.make_document(plan)
        self.assertEqual(raised.exception.code, "nested_list_without_possible_child")

    def test_postscript_uses_a_visible_conventional_marker_without_losing_content(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["structure"].update(
            {
                "has_post_script": True,
                "post_script_text": "Die Rückmeldung ist bis zum 01.09.2030 erforderlich.",
            }
        )
        plan["semantic_plan"]["facts"].append(
            {
                "fact_id": "f3",
                "order": 3,
                "text": "Der Nachsatz lautet: Die Rückmeldung ist bis zum 01.09.2030 erforderlich.",
                "polarity": "positive",
                "modality": "obligation",
                "condition": None,
                "protected_values": ["Die Rückmeldung ist bis zum 01.09.2030 erforderlich."],
            }
        )

        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)

        postscript = next(block for block in record["target_ast"]["blocks"] if block["type"] == "post_script")
        self.assertEqual(postscript["text"], "PS: Die Rückmeldung ist bis zum 01.09.2030 erforderlich.")
        self.assertIn("[PS]PS: Die Rückmeldung ist bis zum 01.09.2030 erforderlich.[/PS]", record["target_sst"])
        self.assertIn("PS: Die Rückmeldung ist bis zum 01.09.2030 erforderlich.", record["target_plain_text"])

    def test_real_estate_article_adjective_and_quantity_frames_remain_grammatical(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["facts"] = [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Aster GmbH bearbeitet den fiktiven Los BAU-44.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["Aster GmbH", "Los BAU-44"],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Aster GmbH bearbeitet den fiktiven Projektakte NQ-208.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["Aster GmbH", "Projektakte NQ-208"],
            },
            {
                "fact_id": "f3",
                "order": 3,
                "text": "Die vereinbarte 42 m bleibt für die Einheit maßgeblich.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["42 m"],
            },
            {
                "fact_id": "f4",
                "order": 4,
                "text": "Die Ansprechperson Kira Damm empfiehlt eine Rückmeldung zur Position lichte Höhe bis zum 01.09.2030.",
                "polarity": "positive",
                "modality": "recommendation",
                "condition": None,
                "protected_values": ["Kira Damm", "01.09.2030"],
            },
        ]
        plan["semantic_plan"]["entities"] = [
            {"type": "organization", "value": "Aster GmbH", "fictional": True, "protected": True},
            {"type": "person", "value": "Kira Damm", "fictional": True, "protected": True},
        ]
        plan["semantic_plan"]["structure"].update({"list_kind": "unordered", "list_items": ["Prüfung von lichte Höhe"]})

        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        text = record["target_plain_text"]

        self.assertIn("das Los BAU-44", text)
        self.assertIn("die Projektakte NQ-208", text)
        self.assertIn("Der vereinbarte Wert von 42 m bleibt für die Einheit maßgeblich.", text)
        self.assertIn("Rückmeldung zum Thema „lichte Höhe“ bis zum 01.09.2030.", text)
        self.assertIn("Prüfung des Werts „lichte Höhe“", text)

    def test_condition_templates_preserve_the_declared_connective_and_avoid_double_punctuation(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["facts"] = [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Der Termin bleibt bestehen.",
                "polarity": "positive",
                "modality": "fact",
                "condition": "wenn die Freigabe vorliegt",
                "protected_values": [],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Die Buchung wird verarbeitet.",
                "polarity": "positive",
                "modality": "fact",
                "condition": "soweit die Buchungslogik nachvollziehbar erläutert wird",
                "protected_values": [],
            },
            {
                "fact_id": "f3",
                "order": 3,
                "text": "Die Maßnahme bleibt gesperrt.",
                "polarity": "negative",
                "modality": "prohibition",
                "condition": "solange der Prüfvermerk nicht vorliegt.",
                "protected_values": [],
            },
        ]

        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        text = record["target_plain_text"]

        self.assertIn("Die Regelung gilt, wenn die Freigabe vorliegt.", text)
        self.assertIn("Die Regelung gilt, soweit die Buchungslogik nachvollziehbar erläutert wird.", text)
        self.assertIn("Die Regelung gilt, solange der Prüfvermerk nicht vorliegt.", text)
        self.assertNotIn("folgende Bedingung:", text)
        self.assertNotIn("..", text)

    def test_technical_and_legal_frames_keep_protected_terms_grammatical(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["facts"] = [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Aster GmbH schlägt load test mit einem Zielwert von 15 min vor.",
                "polarity": "positive",
                "modality": "recommendation",
                "condition": None,
                "protected_values": ["Aster GmbH", "load test", "15 min"],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Als technische Grenze gelten 10 Mbit/s für Bandbreite im feature flag.",
                "polarity": "positive",
                "modality": "fact",
                "condition": None,
                "protected_values": ["10 Mbit/s", "feature flag"],
            },
            {
                "fact_id": "f3",
                "order": 3,
                "text": "Die nächste Stufe kann gestartet werden, unter der Voraussetzung, dass die schema migration rückwärtskompatibel bleibt.",
                "polarity": "positive",
                "modality": "permission",
                "condition": "unter der Voraussetzung, dass die schema migration rückwärtskompatibel bleibt",
                "protected_values": ["schema migration"],
            },
            {
                "fact_id": "f4",
                "order": 4,
                "text": "Das Zitat §§ 8c und 8d KStG wird ausschließlich als wörtliche Referenz im Vorgang geführt.",
                "polarity": "positive",
                "modality": "fact",
                "condition": "ohne inhaltliche Bewertung",
                "protected_values": ["§§ 8c und 8d KStG"],
            },
        ]

        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        text = record["target_plain_text"]

        self.assertIn("schlägt den Einsatz von „load test“ mit einem Zielwert von 15 min vor.", text)
        self.assertIn("für die Bandbreite im Kontext von „feature flag“.", text)
        self.assertIn("kann gestartet werden unter der Voraussetzung, dass die „schema migration“", text)
        self.assertIn("Die Zitate §§ 8c und 8d KStG werden im Vorgang", text)
        self.assertIn("Die wörtliche Wiedergabe erfolgt ohne inhaltliche Bewertung.", text)
        self.assertNotIn("Dies erfolgt", text)

    def test_mixed_language_directives_and_referents_are_rendered_explicitly(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["language"] = "mixed"
        plan["address_mode"] = "mixed_correspondence"
        plan["semantic_plan"]["facts"] = [
            {
                "fact_id": "f1",
                "order": 1,
                "text": "Bitte keep the technical term cash-flow forecast and reference https://docs.example.invalid/api/v2 exactly as written.",
                "polarity": "positive",
                "modality": "obligation",
                "condition": None,
                "protected_values": ["cash-flow forecast", "https://docs.example.invalid/api/v2"],
            },
            {
                "fact_id": "f2",
                "order": 2,
                "text": "Mara Linden 17 teilte mit, dass sie die Unterlagen nicht vor dem Termin versendet.",
                "polarity": "negative",
                "modality": "fact",
                "condition": None,
                "protected_values": ["Mara Linden 17"],
            },
        ]

        record, _ = WRITER.make_record(plan, prompt_hash="sha256:" + "1" * 64)
        text = record["target_plain_text"]

        self.assertIn(
            "Bitte behalten Sie den technischen Begriff „cash-flow forecast“ sowie die Referenz https://docs.example.invalid/api/v2 exakt bei.",
            text,
        )
        self.assertIn(
            "Mara Linden 17 teilte mit, dass Mara Linden 17 die Unterlagen nicht vor dem Termin versendet.",
            text,
        )
        self.assertNotIn("Bitte behalten Sie the technical term", text)

    def test_heading_uses_a_distinct_declared_topic_when_subject_is_present(self) -> None:
        plan = copy.deepcopy(sample_plan())
        plan["semantic_plan"]["structure"].update(
            {"heading_levels": ["h1"], "paragraph_topics": ["Finanzierung", "Wohnfläche"]}
        )

        document, _ = WRITER.make_document(plan)
        subject = next(block.text for block in document.blocks if block.type is WRITER.BlockType.SUBJECT)
        heading = next(block.text for block in document.blocks if block.type is WRITER.BlockType.HEADING_1)

        self.assertNotEqual(subject.split(" – ", 1)[0], heading)
        self.assertEqual(heading, "Wohnfläche")

    def test_approved_style_review_coverage_is_core_only_and_aggregate_only(self) -> None:
        core_seed_ids = {plan["seed_id"] for plan in WRITER.load_plans()}

        review = WRITER.load_style_repair_review(core_seed_ids)

        self.assertEqual(review["reviewed_seed_count"], 612)
        self.assertEqual(len(review["reviewed_seed_ids"]), 612)
        self.assertTrue(set(review["reviewed_seed_ids"]).issubset(core_seed_ids))
        self.assertEqual(review["critical_flag_counts"], WRITER.STYLE_REVIEW_CRITICAL_FLAG_COUNTS)
        self.assertNotIn("case_id", review)
        self.assertNotIn("private_index", review)


if __name__ == "__main__":
    unittest.main()
