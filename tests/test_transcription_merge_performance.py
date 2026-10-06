"""Exact equivalence and deterministic work budgets for long-file stitching."""

import random
import tracemalloc
from dataclasses import replace
from unittest.mock import patch

from scripts.diagnostics.benchmark_transcription_merge import (
    reference_alignment,
    reference_merge_timed,
    synthetic_parts,
)
from src import transcription_merge as merge


def test_rolling_frontier_matches_reference_for_adversarial_overlaps():
    rng = random.Random(83106)
    for _ in range(160):
        parts = []
        for index in range(rng.randrange(1, 10)):
            start, end = max(0, index * 1_000 - 1_000), (index + 1) * 1_000 + 1_000
            words = []
            for _ in range(rng.randrange(0, 25)):
                # Ties, zero-length and long nested intervals, conflicting text,
                # repeated speech and request-local speakers all remain legal.
                at = rng.randrange(start // 100, end // 100) * 100
                words.append(
                    merge.TranscriptWord(
                        rng.choice(["ja", "Maße", "Masse", "Anna", "-5", "Gesell-", "Gesellschaft", "!"]),
                        at,
                        rng.randrange(at // 100, end // 100 + 1) * 100,
                        rng.choice([None, "0", "1"]),
                        rng.choice([None, 0.5, 0.99]),
                    )
                )
            words.sort(key=lambda word: word.start_ms)
            parts.append(
                merge.PartTranscript(
                    index,
                    start,
                    end,
                    index * 1_000,
                    (index + 1) * 1_000,
                    " ".join(word.text for word in words),
                    tuple(words),
                )
            )
        assert merge._merge_timed(parts) == reference_merge_timed(parts)


def test_long_merge_does_not_revisit_finished_word_timestamps():
    reads = 0

    class CountedWord(merge.TranscriptWord):
        def __getattribute__(self, name):
            nonlocal reads
            if name in {"start_ms", "end_ms"}:
                reads += 1
            return super().__getattribute__(name)

    parts = [
        replace(
            part,
            words=tuple(
                CountedWord(word.text, word.start_ms, word.end_ms, word.speaker, word.confidence) for word in part.words
            ),
        )
        for part in synthetic_parts()
    ]
    reads = 0
    result, warnings = merge._merge_timed(parts)
    assert len(result) == 16_004
    assert not warnings
    assert reads < 600_000, f"{reads=}"


def test_alignment_normalizes_each_word_once_and_keeps_reference_ties():
    part = synthetic_parts(2)[0]
    evidence = [merge._Evidence(word, part) for word in part.words]
    expected = reference_alignment(evidence, evidence)
    with patch.object(merge, "_key", wraps=merge._key) as normalize:
        assert merge._alignment(evidence, evidence) == expected
        assert normalize.call_count == 2 * len(evidence)


def test_dense_alignment_keeps_traceback_without_a_full_score_matrix():
    part = synthetic_parts(2)[0]
    evidence = [merge._Evidence(part.words[0], part)] * 300
    expected = reference_alignment(evidence, evidence)
    tracemalloc.start()
    try:
        assert merge._alignment(evidence, evidence) == expected
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 400_000
