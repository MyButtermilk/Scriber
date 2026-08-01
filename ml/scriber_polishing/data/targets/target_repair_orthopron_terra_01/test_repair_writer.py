"""Focused regression tests for the deterministic Orthopron repair overlay."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("repair_writer.py")
SPEC = importlib.util.spec_from_file_location("orthopron_repair_writer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
WRITER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WRITER
SPEC.loader.exec_module(WRITER)


class OrthopronRepairWriterTests(unittest.TestCase):
    def test_referent_repairs_keep_the_protected_pronoun_surface(self) -> None:
        source = "Bitte gebt Ihr Eure Rückmeldung an Sina Körner, damit er Euch den Stand senden kann."

        repaired = WRITER.repair_referent_text(source)

        self.assertEqual(
            repaired,
            "Bitte gebt Ihr Eure Rückmeldung an Sina Körner, damit Sina Körner Euch den Stand senden kann. Das Pronomen „er“ bezieht sich dabei auf Sina Körner.",
        )
        for protected in ("Ihr", "Eure", "Euch", "er"):
            self.assertIn(protected, repaired)

    def test_omitted_condition_is_rendered_as_the_declared_requirement(self) -> None:
        source = "Die Frist wird aufgrund der Rückfrage nur vorbehaltlich bestätigt."

        repaired = WRITER.append_declared_condition(source, "wenn die Prüfung abgeschlossen ist")

        self.assertEqual(
            repaired,
            "Die Frist wird aufgrund der Rückfrage nur vorbehaltlich bestätigt. Dies gilt, wenn die Prüfung abgeschlossen ist.",
        )

    def test_selected_heading_is_distinct_from_the_subject(self) -> None:
        plan = WRITER.plan_by_seed_id("orthopron_800014")

        document, _ = WRITER.make_document(plan, {"redundant_subject_heading"})

        subject = next(block.text for block in document.blocks if block.type is WRITER.BlockType.SUBJECT)
        heading = next(block.text for block in document.blocks if block.type is WRITER.BlockType.HEADING_1)
        self.assertNotEqual(subject, heading)
        self.assertEqual(heading, "Abstimmung und Rückmeldung")

    def test_review_mapping_has_exact_critical_coverage_without_case_ids(self) -> None:
        review = WRITER.load_adversarial_review()

        self.assertEqual(review["reviewed_seed_count"], 196)
        self.assertEqual(
            review["critical_flag_counts"],
            {
                "omitted_condition": 68,
                "pronoun_referent_error": 141,
            },
        )
        self.assertEqual(review["noncritical_seed_count"], 12)
        self.assertNotIn("case_id", review)
        self.assertNotIn("private_index", review)

    def test_real_repair_examples_change_the_critical_surface(self) -> None:
        review = WRITER.load_adversarial_review()
        prompt_hash = WRITER.sha256_prefixed(b"test")

        pronoun_plan = WRITER.plan_by_seed_id("orthopron_800075")
        pronoun_record = WRITER.make_record(pronoun_plan, review["flags_by_seed"][pronoun_plan["seed_id"]], prompt_hash)
        self.assertIn("Das Pronomen „er“ bezieht sich dabei auf Sina Körner.", pronoun_record["target_plain_text"])

        condition_plan = WRITER.plan_by_seed_id("orthopron_800000")
        condition_record = WRITER.make_record(
            condition_plan, review["flags_by_seed"][condition_plan["seed_id"]], prompt_hash
        )
        self.assertIn("Dies gilt, wenn die Prüfung abgeschlossen ist.", condition_record["target_plain_text"])


if __name__ == "__main__":
    unittest.main()
