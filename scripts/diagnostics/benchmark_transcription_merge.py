"""Synthetic long-file merge benchmark, with the pre-frontier reference algorithm.

The reference stays deliberately unoptimized for exact output comparisons.
No recordings, providers, or user databases are accessed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import transcription_merge as merge


def reference_same_word(left, right):
    key = merge._key(left.text)
    return bool(key) and key == merge._key(right.text) and merge._same_interval(left, right)


def reference_alignment(left: Sequence[merge._Evidence], right: Sequence[merge._Evidence]) -> list[tuple[int, int]]:
    """Monotone, time-constrained LCS; ties prefer the closest real intervals."""
    rows, columns = len(left), len(right)
    scores = [[(0, 0) for _ in range(columns + 1)] for _ in range(rows + 1)]
    moves = [[0 for _ in range(columns + 1)] for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        for j in range(1, columns + 1):
            score, move = scores[i - 1][j], 1
            if scores[i][j - 1] > score:
                score, move = scores[i][j - 1], 2
            old, new = left[i - 1].word, right[j - 1].word
            if reference_same_word(old, new):
                count, distance = scores[i - 1][j - 1]
                candidate = (count + 1, distance - abs(old.start_ms + old.end_ms - new.start_ms - new.end_ms))
                if candidate > score:
                    score, move = candidate, 3
            scores[i][j], moves[i][j] = score, move
    result: list[tuple[int, int]] = []
    i, j = rows, columns
    while i and j:
        move = moves[i][j]
        if move == 3:
            result.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif move == 1:
            i -= 1
        else:
            j -= 1
    return list(reversed(result))


def reference_merge_timed(
    parts: Sequence[merge.PartTranscript],
) -> tuple[list[merge.TranscriptWord], list[merge.BoundaryWarning]]:
    retained: list[merge._Evidence] = []
    warnings: list[merge.BoundaryWarning] = []
    for part_index, part in enumerate(parts):
        new = [
            merge._Evidence(
                replace(word, speaker=f"part-{part.index}:{word.speaker}" if word.speaker not in (None, "") else None),
                part,
            )
            for word in part.words
        ]
        if not part_index:
            retained.extend(new)
            continue
        previous = parts[part_index - 1]
        overlap_end = min(previous.end_ms, part.end_ms)
        old_indices = [
            i
            for i, item in enumerate(retained)
            if item.word.end_ms >= part.start_ms and item.word.start_ms <= overlap_end
        ]
        new_indices = [i for i, item in enumerate(new) if item.word.start_ms <= overlap_end]
        old_overlap = [retained[i] for i in old_indices]
        new_overlap = [new[i] for i in new_indices]
        matches = reference_alignment(old_overlap, new_overlap)
        speaker_map = merge._speaker_mapping(old_overlap, new_overlap, matches)
        new = [
            replace(item, word=replace(item.word, speaker=speaker_map.get(item.word.speaker, item.word.speaker)))
            for item in new
        ]
        skipped: set[int] = set()
        matched_old: set[int] = set()
        for old_match, new_match in matches:
            old_index, new_index = old_indices[old_match], new_indices[new_match]
            matched_old.add(old_index)
            skipped.add(new_index)
            if merge._word_quality(new[new_index]) > merge._word_quality(retained[old_index]):
                retained[old_index] = new[new_index]
        removed: set[int] = set()
        repaired_new: set[int] = set()
        for old_index in old_indices:
            if old_index in matched_old:
                continue
            for new_index in new_indices:
                if new_index in skipped:
                    continue
                if merge._clipped_variant(retained[old_index], new[new_index]):
                    removed.add(old_index)
                    break
                if merge._clipped_variant(new[new_index], retained[old_index]):
                    skipped.add(new_index)
                    repaired_new.add(new_index)
        conflict = any(
            merge._uncertain_same_event(retained[old_index].word, new[new_index].word)
            for old_index in old_indices
            if old_index not in removed
            for new_index in new_indices
            if new_index not in repaired_new and (old_index not in matched_old or new_index not in skipped)
        )
        if conflict:
            warnings.append(merge._warning("overlap_conflicting_words", previous, part))
        retained = [item for i, item in enumerate(retained) if i not in removed]
        retained.extend(item for i, item in enumerate(new) if i not in skipped)
        retained.sort(key=lambda item: (item.word.start_ms, item.word.end_ms, item.part.index))
    return [item.word for item in retained], warnings


def synthetic_parts(count=160, words_per_part=100):
    parts = []
    width = words_per_part * 500
    for index in range(count):
        start, end = max(0, index * width - 2_000), (index + 1) * width + 2_000
        words = tuple(
            merge.TranscriptWord(f"word-{tick}", tick * 500, tick * 500 + 200, speaker="0")
            for tick in range(start // 500, end // 500)
        )
        parts.append(
            merge.PartTranscript(
                index, start, end, index * width, (index + 1) * width, " ".join(word.text for word in words), words
            )
        )
    return parts


def measure(run, expected, iterations):
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        result = run()
        samples.append((time.perf_counter() - start) * 1000)
        assert result == expected
    return round(statistics.median(samples), 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=int, default=160)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if not 2 <= args.parts <= 500 or not 1 <= args.iterations <= 20:
        parser.error("parts must be 2..500 and iterations 1..20")
    parts = synthetic_parts(args.parts)
    expected = reference_merge_timed(parts)
    assert merge._merge_timed(parts) == expected
    evidence = [merge._Evidence(parts[0].words[0], parts[0])] * 300
    alignment = reference_alignment(evidence, evidence)
    peaks = {}
    for name, run in (("before", reference_alignment), ("after", merge._alignment)):
        tracemalloc.start()
        try:
            assert run(evidence, evidence) == alignment
            peaks[name] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    print(
        json.dumps(
            {
                "parts": args.parts,
                "inputWords": sum(len(part.words) for part in parts),
                "outputWords": len(expected[0]),
                "beforeMs": measure(lambda: reference_merge_timed(parts), expected, args.iterations),
                "afterMs": measure(lambda: merge._merge_timed(parts), expected, args.iterations),
                "alignment300PeakBytes": peaks,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
