from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "scripts"))

from lab_common import (  # noqa: E402
    ALLOWED_DURATIONS_SECONDS,
    ALLOWED_SAMPLE_RATES_HZ,
    CANDIDATES,
    deterministic_sample,
    quality_metrics,
    validate_matrix_value,
    write_fixture,
)
from run_counterbalanced import PAIR_ORDERS  # noqa: E402


class MatrixContractTests(unittest.TestCase):
    def test_matrix_is_exactly_issue_18_short_duration_set(self) -> None:
        self.assertEqual(ALLOWED_SAMPLE_RATES_HZ, (16_000, 48_000))
        self.assertEqual(ALLOWED_DURATIONS_SECONDS, (5, 15, 30, 60))
        self.assertEqual(
            CANDIDATES,
            ("mp3-lame", "mp3-ffmpeg", "opus-ruopus", "opus-libopus"),
        )
        validate_matrix_value(16_000, 5)
        validate_matrix_value(48_000, 60)
        with self.assertRaisesRegex(ValueError, "sample rate"):
            validate_matrix_value(44_100, 5)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_matrix_value(16_000, 10)

    def test_counterbalance_is_abba_baab_and_equal(self) -> None:
        for codec, order in PAIR_ORDERS.items():
            self.assertEqual(len(order), 8, codec)
            self.assertEqual(order[0], order[3])
            self.assertEqual(order[1], order[2])
            self.assertEqual(order[0], order[6])
            self.assertEqual(order[1], order[7])
            self.assertEqual(order.count(order[0]), 4)
            self.assertEqual(order.count(order[1]), 4)


class FixtureAndMetricTests(unittest.TestCase):
    def test_fixture_is_deterministic_and_exact_five_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pcm"
            second = Path(directory) / "second.pcm"
            write_fixture(first, 16_000, 5)
            write_fixture(second, 16_000, 5)
            self.assertEqual(first.stat().st_size, 16_000 * 5 * 2)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertNotEqual(deterministic_sample(0, 16_000), 0)

    def test_exact_copy_has_no_tail_and_high_snr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pcm"
            decoded = Path(directory) / "decoded.pcm"
            write_fixture(source, 16_000, 5)
            decoded.write_bytes(source.read_bytes())
            metrics = quality_metrics(source, decoded, 16_000)
            self.assertEqual(metrics["alignmentLagSamples"], 0)
            self.assertEqual(metrics["durationErrorSamples"], 0)
            self.assertGreaterEqual(metrics["rawSnrDb"], 100.0)
            self.assertTrue(metrics["qualityPass"])
            self.assertTrue(metrics["tailPass"])


class LockAndSafetyContractTests(unittest.TestCase):
    def test_codec_and_toolchain_pins_are_explicit(self) -> None:
        manifest = json.loads((LAB_ROOT / "toolchains.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["rust"]["release"], "1.90.0")
        self.assertEqual(manifest["crates"]["mp3lame-encoder"]["version"], "0.2.4")
        self.assertEqual(manifest["crates"]["mp3lame-sys"]["bundledLameVersion"], "3.100")
        self.assertEqual(manifest["crates"]["ruopus"]["version"], "0.1.2")
        self.assertFalse(manifest["crates"]["ruopus"]["defaultFeatures"])
        lock = (LAB_ROOT / "Cargo.lock").read_text(encoding="utf-8")
        self.assertIn('name = "mp3lame-sys"\nversion = "0.1.11"', lock)
        self.assertIn('name = "ruopus"\nversion = "0.1.2"', lock)

    def test_lab_is_not_part_of_parent_workspaces_or_product(self) -> None:
        cargo = (LAB_ROOT / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn("publish = false", cargo)
        self.assertNotIn("tauri", cargo.lower())
        source = (LAB_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn("production_ready: false", source)
        self.assertIn("production_integrated: false", source)
        self.assertIn("production_promoted: false", source)
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)


if __name__ == "__main__":
    unittest.main()
