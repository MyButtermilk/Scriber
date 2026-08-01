"""Focused regression tests for the closed Coverage Recovery v1.01 repair."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("repair_writer.py")
SPEC = importlib.util.spec_from_file_location("coverage_repair_writer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
WRITER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WRITER
SPEC.loader.exec_module(WRITER)


class CoverageRepairWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source_plans, cls.source_targets = WRITER._load_source_inputs()
        cls.scope = WRITER.load_repair_scope(cls.source_plans, cls.source_targets)
        cls.prompt_hash = "sha256:" + "a" * 64

    def test_preflight_scope_is_exactly_fourteen_cases_with_three_overlaps(self) -> None:
        self.assertEqual(len(self.scope.address_seed_ids), 7)
        self.assertEqual(len(self.scope.heading_seed_ids), 10)
        self.assertEqual(len(self.scope.overlap_seed_ids), 3)
        self.assertEqual(len(self.scope.changed_seed_ids), 14)

    def test_overlapping_formal_case_keeps_seit_seid_and_removes_only_the_heading(self) -> None:
        seed_id = "covrecovery_1020205"
        source_plan = self.source_plans[seed_id]
        source_target = self.source_targets[seed_id]
        flags = self.scope.flags_for(seed_id)

        self.assertTrue(flags.formal_address)
        self.assertTrue(flags.duplicate_subject_heading)
        repaired_plan = WRITER.repair_plan(source_plan, flags, prompt_hash=self.prompt_hash)
        repaired_target = WRITER.make_record(
            source_target,
            source_plan,
            repaired_plan,
            flags,
            prompt_hash=self.prompt_hash,
        )

        self.assertEqual(repaired_plan["address_mode"], "personal_du_capitalized")
        self.assertEqual(
            repaired_plan["semantic_plan"]["facts"][2]["text"],
            WRITER.FORMAL_REPAIRED_FACT_TEXT,
        )
        self.assertEqual(
            repaired_plan["semantic_plan"]["facts"][2]["protected_values"],
            ["Seit", "seid"],
        )
        self.assertEqual(repaired_plan["semantic_plan"]["entities"], source_plan["semantic_plan"]["entities"])
        self.assertEqual(repaired_plan["semantic_plan"]["structure"]["heading_levels"], [])
        self.assertIn("Seit Montag seid Ihr", repaired_target["target_plain_text"])
        self.assertNotIn("Seit Montag seid ihr", repaired_target["target_plain_text"])
        self.assertFalse(
            any(
                block["type"] in {"heading_1", "heading_2"}
                for block in repaired_target["target_ast"]["blocks"]
            )
        )

    def test_heading_only_case_keeps_all_fact_surfaces_and_the_subject(self) -> None:
        seed_id = "covrecovery_1020210"
        source_plan = self.source_plans[seed_id]
        source_target = self.source_targets[seed_id]
        flags = self.scope.flags_for(seed_id)

        self.assertFalse(flags.formal_address)
        self.assertTrue(flags.duplicate_subject_heading)
        repaired_plan = WRITER.repair_plan(source_plan, flags, prompt_hash=self.prompt_hash)
        repaired_target = WRITER.make_record(
            source_target,
            source_plan,
            repaired_plan,
            flags,
            prompt_hash=self.prompt_hash,
        )

        self.assertEqual(repaired_plan["address_mode"], source_plan["address_mode"])
        self.assertEqual(repaired_plan["semantic_plan"]["facts"], source_plan["semantic_plan"]["facts"])
        self.assertEqual(repaired_plan["semantic_plan"]["structure"]["heading_levels"], [])
        subjects = [
            block["text"]
            for block in repaired_target["target_ast"]["blocks"]
            if block["type"] == "subject"
        ]
        self.assertEqual(subjects, [source_plan["document_type"]])
        self.assertEqual(
            repaired_target["protected_spans"],
            source_target["protected_spans"],
        )


if __name__ == "__main__":
    unittest.main()
