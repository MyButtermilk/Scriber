"""Deterministic, conservative stitching of overlapping file transcriptions.

All public intervals are integer milliseconds on the original media clock.
Only :func:`part_transcript_from_payload` accepts request-local provider times.
Lexical disagreements are retained, never resolved by a language model or by
choosing one request's entire overlap. Warnings are metadata, not spoken text.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any

_EDGE_PUNCTUATION = " \t\r\n.,!?;:…\"'“”‘’„«»‹›()[]{}"
_CLIPPED_EDGE_MS = 120
_BOUNDARY_WARNING_PROXIMITY_MS = 250


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("A transcript word must contain recognized text")
        if any(type(value) is not int for value in (self.start_ms, self.end_ms)):
            raise ValueError("Word timestamps must be integer milliseconds")
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("Invalid word interval")
        if self.confidence is not None and (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("Invalid word confidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
            "speaker": self.speaker or "",
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PartTranscript:
    index: int
    start_ms: int
    end_ms: int
    core_start_ms: int
    core_end_ms: int
    text: str
    words: tuple[TranscriptWord, ...] = ()

    def __post_init__(self) -> None:
        fields = (self.index, self.start_ms, self.end_ms, self.core_start_ms, self.core_end_ms)
        if any(type(value) is not int for value in fields):
            raise ValueError("Part identity and intervals must be integers")
        if self.index < 0 or not 0 <= self.start_ms <= self.core_start_ms < self.core_end_ms <= self.end_ms:
            raise ValueError("Invalid part or core interval")
        if not isinstance(self.text, str):
            raise ValueError("Part text must be a string")
        object.__setattr__(self, "words", tuple(self.words))
        for word in self.words:
            if not isinstance(word, TranscriptWord) or not self.start_ms <= word.start_ms <= word.end_ms <= self.end_ms:
                raise ValueError("Word interval lies outside its original-file part interval")


@dataclass(frozen=True)
class BoundaryWarning:
    code: str
    left_part_index: int
    right_part_index: int
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "leftPartIndex": self.left_part_index,
            "rightPartIndex": self.right_part_index,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
        }


@dataclass(frozen=True)
class MergedTranscript:
    text: str
    words: tuple[TranscriptWord, ...]
    boundary_warnings: tuple[BoundaryWarning, ...]
    word_timing_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a provider-independent payload consumed by canonical parsing.

        If ``wordTimingComplete`` is false, words describe only the known
        evidence: canonical parsing must use the complete text fallback.
        """
        return {
            "text": self.text,
            "_scriberMerged": {
                "text": self.text,
                "words": [word.to_dict() for word in self.words],
                "boundaryWarnings": [warning.to_dict() for warning in self.boundary_warnings],
                "wordTimingComplete": self.word_timing_complete,
                "alignmentQuality": "exact_word" if self.word_timing_complete else "estimated",
            },
        }


def _key(text: str) -> str:
    # lower(), rather than casefold(), deliberately distinguishes Masse/Maße.
    # Internal apostrophes, hyphens and decimal separators are meaningful.
    normalized = unicodedata.normalize("NFC", text).lower()
    numeric_prefix = normalized.lstrip(_EDGE_PUNCTUATION.replace(".", "").replace(",", ""))
    key = normalized.strip(_EDGE_PUNCTUATION)
    if len(numeric_prefix) > 1 and numeric_prefix[0] in ".," and numeric_prefix[1].isdigit():
        return numeric_prefix[0] + key
    return key


def _coverage_key(text: str) -> tuple[str, ...]:
    return tuple(key for token in text.split() if (key := _key(token)))


def text_words_cover_transcript(text: str, words: Sequence[str]) -> bool:
    """Prove lexical coverage without erasing signs, decimals or word breaks."""
    return _coverage_key(text) == _coverage_key(" ".join(words))


def _has_complete_words(part: PartTranscript) -> bool:
    if not part.text.strip():
        return not part.words
    if not part.words or not text_words_cover_transcript(part.text, [word.text for word in part.words]):
        return False
    return all(left.start_ms <= right.start_ms for left, right in pairwise(part.words))


def _same_interval(left: TranscriptWord, right: TranscriptWord) -> bool:
    overlap = min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms)
    shorter = min(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    if shorter == 0:
        return left.start_ms == right.start_ms and left.end_ms == right.end_ms
    # Positive temporal intersection prevents deduplication of nearby repeated
    # words. A substantial intersection tolerates ordinary timestamp jitter.
    midpoint_distance_twice = abs(left.start_ms + left.end_ms - right.start_ms - right.end_ms)
    longer = max(left.end_ms - left.start_ms, right.end_ms - right.start_ms)
    return (
        overlap > 0
        and overlap * 2 >= shorter
        and midpoint_distance_twice <= max(200, shorter)
        and longer <= 4 * shorter
    )


def _uncertain_same_event(left: TranscriptWord, right: TranscriptWord) -> bool:
    if min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms):
        return True
    # Packet rebasing and provider word times can place the same short word
    # in nearby disjoint intervals. Preserve BOTH, but make the unresolved
    # boundary visible. This proximity never authorizes deduplication.
    return (
        bool(_key(left.text))
        and _key(left.text) == _key(right.text)
        and abs(left.start_ms + left.end_ms - right.start_ms - right.end_ms) <= 2 * _BOUNDARY_WARNING_PROXIMITY_MS
    )


@dataclass(frozen=True)
class _Evidence:
    word: TranscriptWord
    part: PartTranscript


def _word_quality(item: _Evidence) -> tuple[int, float, int]:
    word, part = item.word, item.part
    clearance = min(word.start_ms - part.start_ms, part.end_ms - word.end_ms)
    confidence = word.confidence if word.confidence is not None else -1.0
    return clearance, confidence, -part.index


def _alignment(left: Sequence[_Evidence], right: Sequence[_Evidence]) -> list[tuple[int, int]]:
    """Monotone, time-constrained LCS; ties prefer the closest real intervals."""
    rows, columns = len(left), len(right)
    if not rows or not columns:
        return []
    left_keys = [_key(item.word.text) for item in left]
    right_keys = [_key(item.word.text) for item in right]
    # Scores only depend on the previous row. Traceback needs one small move
    # code per cell, not a matrix of Python score tuples and integer lists.
    scores = [(0, 0)] * (columns + 1)
    moves = [bytearray(columns + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        previous_scores = scores
        scores = [(0, 0)] * (columns + 1)
        for j in range(1, columns + 1):
            score, move = previous_scores[j], 1
            if scores[j - 1] > score:
                score, move = scores[j - 1], 2
            old, new = left[i - 1].word, right[j - 1].word
            if left_keys[i - 1] and left_keys[i - 1] == right_keys[j - 1] and _same_interval(old, new):
                count, distance = previous_scores[j - 1]
                candidate = (count + 1, distance - abs(old.start_ms + old.end_ms - new.start_ms - new.end_ms))
                if candidate > score:
                    score, move = candidate, 3
            scores[j], moves[i][j] = score, move
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


def _clipped_variant(short: _Evidence, full: _Evidence) -> bool:
    """Repair only an explicit cut-off hyphen at the actual upload edge.

    A shorter unmarked word can be a real word/name and is never discarded.
    """
    short_key, full_key = _key(short.word.text), _key(full.word.text)
    if not short_key.endswith("-") or not short_key[:-1].isalpha() or len(short_key) < 5:
        return False
    if not full_key.isalpha() or not full_key.startswith(short_key[:-1]) or len(full_key) <= len(short_key):
        return False
    return (
        short.part.end_ms - short.word.end_ms <= _CLIPPED_EDGE_MS
        and full.part.end_ms - full.word.end_ms > _CLIPPED_EDGE_MS
        and _same_interval(short.word, full.word)
    )


def _join_words(words: Sequence[TranscriptWord]) -> str:
    text = " ".join(word.text.strip() for word in words)
    return re.sub(r"\s+([,.;:!?])(?=\s|$)", r"\1", text).strip()


def _warning(code: str, left: PartTranscript, right: PartTranscript) -> BoundaryWarning:
    start = max(left.start_ms, right.start_ms)
    return BoundaryWarning(code, left.index, right.index, start, max(start, min(left.end_ms, right.end_ms)))


def _speaker_mapping(
    left: Sequence[_Evidence], right: Sequence[_Evidence], matches: Sequence[tuple[int, int]]
) -> dict[str, str]:
    evidence: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    reverse: dict[str, set[str]] = defaultdict(set)
    for old_index, new_index in matches:
        old, new = left[old_index].word.speaker, right[new_index].word.speaker
        if old and new:
            evidence[new][old] += 1
            reverse[old].add(new)
    return {
        new: next(iter(old_counts))
        for new, old_counts in evidence.items()
        if len(old_counts) == 1 and next(iter(old_counts.values())) >= 2 and len(reverse[next(iter(old_counts))]) == 1
    }


def _merge_timed(parts: Sequence[PartTranscript]) -> tuple[list[TranscriptWord], list[BoundaryWarning]]:
    finished: list[TranscriptWord] = []
    retained: list[_Evidence] = []
    warnings: list[BoundaryWarning] = []
    for part_index, part in enumerate(parts):
        new = [
            _Evidence(
                word
                if word.speaker is None
                else replace(word, speaker=f"part-{part.index}:{word.speaker}" if word.speaker else None),
                part,
            )
            for word in part.words
        ]
        if not part_index:
            retained.extend(new)
            continue
        if part_index > 1:
            # After the first boundary retained is sorted. Parts advance on the
            # original clock: a leading word ending strictly before this part
            # can never participate in another overlap or change position.
            # Stop at the first still-live interval (end times need not sort).
            frontier = 0
            while frontier < len(retained) and retained[frontier].word.end_ms < part.start_ms:
                finished.append(retained[frontier].word)
                frontier += 1
            if frontier:
                del retained[:frontier]
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
        matches = _alignment(old_overlap, new_overlap)
        speaker_map = _speaker_mapping(old_overlap, new_overlap, matches)
        if speaker_map:
            new = [
                replace(item, word=replace(item.word, speaker=speaker_map[item.word.speaker]))
                if item.word.speaker is not None and item.word.speaker in speaker_map
                else item
                for item in new
            ]
        skipped: set[int] = set()
        matched_old: set[int] = set()
        for old_match, new_match in matches:
            old_index, new_index = old_indices[old_match], new_indices[new_match]
            matched_old.add(old_index)
            skipped.add(new_index)
            if _word_quality(new[new_index]) > _word_quality(retained[old_index]):
                retained[old_index] = new[new_index]
        removed: set[int] = set()
        repaired_new: set[int] = set()
        for old_index in old_indices:
            if old_index in matched_old:
                continue
            for new_index in new_indices:
                if new_index in skipped:
                    continue
                if _clipped_variant(retained[old_index], new[new_index]):
                    removed.add(old_index)
                    break
                if _clipped_variant(new[new_index], retained[old_index]):
                    skipped.add(new_index)
                    repaired_new.add(new_index)
        conflict = any(
            _uncertain_same_event(retained[old_index].word, new[new_index].word)
            for old_index in old_indices
            if old_index not in removed
            for new_index in new_indices
            if new_index not in repaired_new and (old_index not in matched_old or new_index not in skipped)
        )
        if conflict:
            warnings.append(_warning("overlap_conflicting_words", previous, part))
        retained = [item for i, item in enumerate(retained) if i not in removed]
        retained.extend(item for i, item in enumerate(new) if i not in skipped)
        retained.sort(key=lambda item: (item.word.start_ms, item.word.end_ms, item.part.index))
    finished.extend(item.word for item in retained)
    return finished, warnings


def merge_part_transcripts(parts: Sequence[PartTranscript]) -> MergedTranscript:
    """Merge an ordered, complete partition without guessing absent timings.

    Cores must meet exactly and indices must be consecutive. A missing part is
    an error, never a successful partial transcript. Untimed text is preserved
    in full; timed runs on either side can still deduplicate their own overlap.
    """
    parts = tuple(parts)
    for left, right in pairwise(parts):
        if right.index != left.index + 1 or right.core_start_ms != left.core_end_ms:
            raise ValueError("Transcript parts must be consecutive and have contiguous cores")
        if right.start_ms < left.start_ms or right.end_ms < left.end_ms:
            raise ValueError("Transcript part intervals must advance on the original timeline")
    if not parts:
        return MergedTranscript("", (), ())
    complete = [_has_complete_words(part) for part in parts]
    warnings = [
        _warning("overlap_missing_word_timestamps", left, right)
        for i, (left, right) in enumerate(pairwise(parts))
        if left.end_ms > right.start_ms and not (complete[i] and complete[i + 1])
    ]
    text_runs: list[str] = []
    words: list[TranscriptWord] = []
    index = 0
    while index < len(parts):
        if not complete[index]:
            if parts[index].text.strip():
                text_runs.append(parts[index].text.strip())
            index += 1
            continue
        end = index + 1
        while end < len(parts) and complete[end]:
            end += 1
        run_words, run_warnings = _merge_timed(parts[index:end])
        words.extend(run_words)
        warnings.extend(run_warnings)
        run_text = _join_words(run_words)
        if run_text:
            text_runs.append(run_text)
        index = end
    warnings.sort(key=lambda warning: (warning.right_part_index, warning.code))
    words.sort(key=lambda word: (word.start_ms, word.end_ms))
    return MergedTranscript("\n\n".join(text_runs), tuple(words), tuple(warnings), all(complete))


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _payload_text(provider: str, payload: Mapping[str, Any]) -> str:
    if provider == "azure_mai":
        for field in ("combinedPhrases", "phrases", "recognizedPhrases"):
            phrases = payload.get(field)
            if not isinstance(phrases, list):
                continue
            texts = [
                next(
                    (
                        value.strip()
                        for key in ("text", "displayText", "display", "lexical")
                        if isinstance(value := phrase.get(key), str) and value.strip()
                    ),
                    "",
                )
                for phrase in phrases
                if isinstance(phrase, Mapping)
            ]
            if text := " ".join(value for value in texts if value):
                return text
        for field in ("text", "displayText", "transcription", "transcript"):
            if isinstance(value := payload.get(field), str) and value.strip():
                return value
    if isinstance(payload.get("text"), str):
        return payload["text"]
    return ""


def part_transcript_from_payload(
    provider: str,
    *,
    index: int,
    start_ms: int,
    end_ms: int,
    core_start_ms: int,
    core_end_ms: int,
    payload: Mapping[str, Any],
    text: str | None = None,
) -> PartTranscript:
    """Normalize exact OpenRouter/Azure words and rebase request-local time.

    Azure phrase intervals are never promoted to word timing. Malformed or
    incomplete word data falls back to the complete recognized text.
    """
    if provider not in {"openrouter_stt", "azure_mai"}:
        raise ValueError("Unsupported split transcription provider")
    transcript_text = _payload_text(provider, payload) if text is None else text
    raw_words: list[tuple[Mapping[str, Any], Any]] = []
    if provider == "openrouter_stt":
        source = payload.get("words")
        if isinstance(source, list):
            raw_words = [(word, word.get("speaker")) for word in source if isinstance(word, Mapping)]
    else:
        phrases = payload.get("phrases") or payload.get("recognizedPhrases")
        if isinstance(phrases, list):
            for phrase in phrases:
                if isinstance(phrase, Mapping) and isinstance(phrase.get("words"), list):
                    raw_words.extend(
                        (word, word.get("speaker", phrase.get("speaker")))
                        for word in phrase["words"]
                        if isinstance(word, Mapping)
                    )
    if not transcript_text.strip() and raw_words:
        transcript_text = " ".join(
            value.strip()
            for raw, _speaker in raw_words
            if isinstance(value := raw.get("text") or raw.get("word"), str) and value.strip()
        )
    words: list[TranscriptWord] = []
    for raw, speaker in raw_words:
        word_text = raw.get("text") or raw.get("word")
        if not isinstance(word_text, str) or not word_text.strip():
            continue
        if provider == "openrouter_stt":
            start, end = _number(raw.get("start")), _number(raw.get("end"))
            scale = 1000
        else:
            start, duration = _number(raw.get("offsetMilliseconds")), _number(raw.get("durationMilliseconds"))
            end = start + duration if start is not None and duration is not None else None
            scale = 1
        if start is None or end is None or start < 0 or end < start:
            continue
        word_start, word_end = start_ms + round(start * scale), start_ms + round(end * scale)
        if not start_ms <= word_start <= word_end <= end_ms:
            continue
        confidence = _number(raw.get("confidence"))
        if confidence is not None and not 0 <= confidence <= 1:
            confidence = None
        if payload.get("_scriberDiarizationFallback") == "diarization_unavailable":
            speaker = None
        words.append(
            TranscriptWord(
                word_text,
                word_start,
                word_end,
                str(speaker).strip() if speaker not in (None, "") else None,
                confidence,
            )
        )
    part = PartTranscript(index, start_ms, end_ms, core_start_ms, core_end_ms, transcript_text, tuple(words))
    return part if _has_complete_words(part) else replace(part, words=())
