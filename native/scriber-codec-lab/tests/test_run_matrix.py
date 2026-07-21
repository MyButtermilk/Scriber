from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_matrix.py"
SPEC = importlib.util.spec_from_file_location("run_codec_matrix", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)

COMPARISON_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_counterbalanced.py"
)
COMPARISON_SPEC = importlib.util.spec_from_file_location(
    "run_codec_comparison", COMPARISON_SCRIPT_PATH
)
assert COMPARISON_SPEC is not None and COMPARISON_SPEC.loader is not None
COMPARISON = importlib.util.module_from_spec(COMPARISON_SPEC)
COMPARISON_SPEC.loader.exec_module(COMPARISON)

CHALLENGER_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_challenger_matrix.py"
)
CHALLENGER_SPEC = importlib.util.spec_from_file_location(
    "run_codec_challengers", CHALLENGER_SCRIPT_PATH
)
assert CHALLENGER_SPEC is not None and CHALLENGER_SPEC.loader is not None
CHALLENGER = importlib.util.module_from_spec(CHALLENGER_SPEC)
CHALLENGER_SPEC.loader.exec_module(CHALLENGER)


class CodecMatrixTests(unittest.TestCase):
    def test_matrix_durations_are_exactly_bounded(self) -> None:
        self.assertEqual(MATRIX.DURATIONS_SECONDS, (5, 15, 30, 60))
        self.assertEqual(MATRIX.SAMPLE_RATES_HZ, (16_000, 48_000))

    def test_fixture_is_deterministic_and_exact_length(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.pcm"
            second = Path(directory) / "second.pcm"
            MATRIX.write_fixture(first, 16_000, 5)
            MATRIX.write_fixture(second, 16_000, 5)
            self.assertEqual(first.stat().st_size, 16_000 * 5 * 2)
            self.assertEqual(MATRIX.sha256_file(first), MATRIX.sha256_file(second))

    def test_fixture_rejects_unlisted_duration_and_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.pcm"
            with self.assertRaises(ValueError):
                MATRIX.write_fixture(output, 16_000, 7)
            with self.assertRaises(ValueError):
                MATRIX.write_fixture(output, 44_100, 5)

    def test_counterbalanced_order_is_symmetric(self) -> None:
        self.assertEqual(len(COMPARISON.RUN_ORDER), 8)
        self.assertEqual(COMPARISON.RUN_ORDER.count("stable"), 4)
        self.assertEqual(COMPARISON.RUN_ORDER.count("nightly-simd"), 4)
        stable_positions = [
            index
            for index, profile in enumerate(COMPARISON.RUN_ORDER, start=1)
            if profile == "stable"
        ]
        nightly_positions = [
            index
            for index, profile in enumerate(COMPARISON.RUN_ORDER, start=1)
            if profile == "nightly-simd"
        ]
        self.assertEqual(sum(stable_positions), sum(nightly_positions))

    def test_three_profile_order_is_balanced(self) -> None:
        self.assertEqual(len(CHALLENGER.RUN_ORDER), 9)
        for profile in CHALLENGER.PROFILES:
            self.assertEqual(CHALLENGER.RUN_ORDER.count(profile), 3)
            positions = [
                index % 3
                for index, value in enumerate(CHALLENGER.RUN_ORDER)
                if value == profile
            ]
            self.assertEqual(sorted(positions), [0, 1, 2])

    def test_wav_fixture_is_canonical_pcm16_mono(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pcm = Path(directory) / "fixture.pcm"
            wav = Path(directory) / "fixture.wav"
            MATRIX.write_fixture(pcm, 16_000, 5)
            CHALLENGER.write_wav_fixture(pcm, wav, 16_000)
            data = wav.read_bytes()
            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual(data[8:12], b"WAVE")
            self.assertEqual(data[36:40], b"data")
            self.assertEqual(len(data), len(pcm.read_bytes()) + 44)


if __name__ == "__main__":
    unittest.main()
