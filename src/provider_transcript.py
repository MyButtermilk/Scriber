"""Normalize provider-native transcript timing into Scriber's canonical segments."""
from __future__ import annotations

from typing import Any, Iterable


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _speaker_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).strip()


def _timed_items(
    items: Iterable[Any], *, start_key: str, end_key: str, scale: float
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start, end = _number(item.get(start_key)), _number(item.get(end_key))
        text = str(item.get("text") or item.get("punctuated_word") or item.get("word") or "")
        if start is None or end is None or end < start or not text.strip():
            continue
        result.append({
            "text": text,
            "startMs": round(start * scale),
            "endMs": round(end * scale),
            "speaker": _speaker_key(item.get("speaker")),
            "confidence": _number(item.get("confidence")),
        })
    return result


def _duration_timed_items(
    items: Iterable[Any], *, offset_key: str, duration_key: str, scale: float
) -> list[dict[str, Any]]:
    """Normalize provider items that expose offset + duration instead of end."""
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start, duration = _number(item.get(offset_key)), _number(item.get(duration_key))
        text = str(item.get("text") or item.get("displayText") or item.get("display") or "")
        if start is None or duration is None or duration < 0 or not text.strip():
            continue
        result.append({
            "text": text,
            "startMs": round(start * scale),
            "endMs": round((start + duration) * scale),
            "speaker": _speaker_key(item.get("speaker")),
            "confidence": _number(item.get("confidence")),
        })
    return result


def _speechmatics_words(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Speechmatics JSON-v2 words while preserving punctuation.

    JSON-v2 reports seconds in ``start_time``/``end_time``. Punctuation is a
    separate result item, so it must be joined to the preceding word rather
    than becoming a whitespace-separated pseudo-word.
    """
    words: list[dict[str, Any]] = []
    results = payload.get("results")
    if not isinstance(results, list):
        return words
    for item in results:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        alternatives = item.get("alternatives")
        alternative = (
            alternatives[0]
            if isinstance(alternatives, list) and alternatives and isinstance(alternatives[0], dict)
            else {}
        )
        text = str(alternative.get("content") or "")
        if item_type == "punctuation":
            if words and text:
                words[-1]["text"] = f"{words[-1]['text']}{text}"
                end = _number(item.get("end_time"))
                if end is not None:
                    words[-1]["endMs"] = max(words[-1]["endMs"], round(end * 1_000))
            continue
        if item_type != "word":
            continue
        start, end = _number(item.get("start_time")), _number(item.get("end_time"))
        if start is None or end is None or end < start or not text.strip():
            continue
        speaker_value = alternative.get("speaker")
        if speaker_value in (None, ""):
            speaker_value = item.get("speaker")
        words.append({
            "text": text,
            "startMs": round(start * 1_000),
            "endMs": round(end * 1_000),
            "speaker": _speaker_key(speaker_value),
            "confidence": _number(alternative.get("confidence")),
        })
    return words


def _gladia_utterances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    transcription = (
        result.get("transcription")
        if isinstance(result.get("transcription"), dict)
        else {}
    )
    utterances = transcription.get("utterances")
    return _timed_items(
        utterances if isinstance(utterances, list) else [],
        start_key="start",
        end_key="end",
        scale=1_000,
    )


def _azure_phrase_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    phrases = payload.get("phrases") or payload.get("recognizedPhrases") or []
    return _duration_timed_items(
        phrases if isinstance(phrases, list) else [],
        offset_key="offsetMilliseconds",
        duration_key="durationMilliseconds",
        scale=1,
    )


def group_provider_words(
    words: list[dict[str, Any]], source: str, origin_ms: int, *, concatenate: bool = False,
    alignment_quality: str | None = None,
) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for word in words:
        if not groups:
            groups.append([word])
            continue
        previous = groups[-1][-1]
        speaker_changed = word["speaker"] != previous["speaker"] and bool(
            word["speaker"] or previous["speaker"]
        )
        long_pause = word["startMs"] - previous["endMs"] > 1_500
        long_turn = word["endMs"] - groups[-1][0]["startMs"] > 30_000
        if speaker_changed or long_pause or long_turn:
            groups.append([word])
        else:
            groups[-1].append(word)

    segments: list[dict[str, Any]] = []
    speaker_labels: dict[str, str] = {}
    quality_rank = {"estimated": 0, "provider_segment": 1, "exact_word": 2}
    for index, group in enumerate(groups):
        parts = [str(word["text"]) for word in group]
        text = ("".join(parts) if concatenate else " ".join(part.strip() for part in parts)).strip()
        if not text:
            continue
        speaker_key = _speaker_key(group[0]["speaker"])
        if source == "microphone":
            speaker = "You"
        elif speaker_key:
            speaker = speaker_labels.setdefault(speaker_key, f"Speaker {len(speaker_labels) + 1}")
        else:
            speaker = "Meeting audio"
        confidences = [word["confidence"] for word in group if word["confidence"] is not None]
        qualities = [
            str(word.get("alignmentQuality") or "exact_word") for word in group
        ]
        group_quality = alignment_quality or min(
            qualities, key=lambda value: quality_rank.get(value, 0)
        )
        segments.append({
            "revision": "canonical",
            "source": source,
            "providerSegmentId": f"provider-exact-{index}",
            "speakerKey": speaker_key,
            "speakerLabel": speaker,
            "startMs": origin_ms + group[0]["startMs"],
            "endMs": origin_ms + group[-1]["endMs"],
            "text": text,
            "confidence": sum(confidences) / len(confidences) if confidences else None,
            "alignmentQuality": group_quality,
            "isFinal": True,
        })
    return segments


def normalize_provider_segments(
    provider: str, payload: Any, source: str, origin_ms: int = 0
) -> list[dict[str, Any]]:
    """Return exact provider-timed segments, or an empty list when unavailable."""
    if not isinstance(payload, dict):
        return []
    provider = str(provider).lower()

    if provider in {"soniox", "soniox_async"}:
        return group_provider_words(
            _timed_items(payload.get("tokens", []), start_key="start_ms", end_key="end_ms", scale=1),
            source,
            origin_ms,
            concatenate=True,
        )

    if provider == "assemblyai":
        segments: list[dict[str, Any]] = []
        speaker_labels: dict[str, str] = {}
        for index, utterance in enumerate(payload.get("utterances", [])):
            if not isinstance(utterance, dict):
                continue
            start, end = _number(utterance.get("start")), _number(utterance.get("end"))
            text = str(utterance.get("text") or "").strip()
            if start is None or end is None or end < start or not text:
                continue
            speaker_key = _speaker_key(utterance.get("speaker"))
            if source == "microphone":
                speaker_label = "You"
            elif speaker_key:
                speaker_label = speaker_labels.setdefault(
                    speaker_key, f"Speaker {len(speaker_labels) + 1}"
                )
            else:
                speaker_label = "Meeting audio"
            segments.append({
                "revision": "canonical", "source": source,
                "providerSegmentId": f"provider-exact-{index}",
                "speakerKey": speaker_key,
                "speakerLabel": speaker_label,
                "startMs": origin_ms + round(start), "endMs": origin_ms + round(end),
                "text": text, "confidence": _number(utterance.get("confidence")), "isFinal": True,
                "alignmentQuality": "provider_segment",
            })
        return segments

    if provider in {"mistral", "mistral_async"}:
        segments = payload.get("segments", [])
        words = _timed_items(segments, start_key="start", end_key="end", scale=1_000)
        return group_provider_words(words, source, origin_ms, alignment_quality="provider_segment")

    if provider in {"smallest", "smallest_async"}:
        words = _timed_items(
            payload.get("words", []), start_key="start", end_key="end", scale=1_000
        )
        if words:
            return group_provider_words(words, source, origin_ms)
        utterances = _timed_items(
            payload.get("utterances", []), start_key="start", end_key="end", scale=1_000
        )
        return group_provider_words(
            utterances, source, origin_ms, alignment_quality="provider_segment"
        )

    if provider in {"gladia", "gladia_async"}:
        return group_provider_words(
            _gladia_utterances(payload),
            source,
            origin_ms,
            alignment_quality="provider_segment",
        )

    if provider == "speechmatics_async":
        return group_provider_words(_speechmatics_words(payload), source, origin_ms)

    if provider == "azure_mai":
        return group_provider_words(
            _azure_phrase_items(payload),
            source,
            origin_ms,
            alignment_quality="provider_segment",
        )

    if provider == "deepgram_async":
        try:
            words = payload["results"]["channels"][0]["alternatives"][0]["words"]
        except (KeyError, IndexError, TypeError):
            return []
        return group_provider_words(
            _timed_items(words, start_key="start", end_key="end", scale=1_000), source, origin_ms
        )

    if provider == "openai_async":
        words = _timed_items(
            payload.get("words", []), start_key="start", end_key="end", scale=1_000
        )
        if words:
            return group_provider_words(words, source, origin_ms)
        # ``diarized_json`` exposes speaker-attributed provider segments and
        # explicitly cannot return word timestamp granularities.
        segments = _timed_items(
            payload.get("segments", []), start_key="start", end_key="end", scale=1_000
        )
        return group_provider_words(
            segments, source, origin_ms, alignment_quality="provider_segment"
        )

    # Smallest and other compatible adapters commonly expose millisecond words.
    words = payload.get("words")
    if isinstance(words, list):
        return group_provider_words(
            _timed_items(words, start_key="start", end_key="end", scale=1), source, origin_ms
        )
    return []


def has_speaker_evidence(segments: Iterable[dict[str, Any]]) -> bool:
    """Return true only for concrete, valid speaker-attributed intervals.

    Provider capability metadata and rendered ``[Speaker]`` text are routing
    hints at most. Post-response native diarization is proven only by a
    normalized interval that contains text, has positive duration, and carries
    a label other than the normalizer's explicit no-speaker placeholder.
    """
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        label = str(segment.get("speakerLabel") or "").strip()
        text = str(segment.get("text") or "").strip()
        if not label or label == "Meeting audio" or not text:
            continue
        try:
            start_ms = int(segment.get("startMs"))
            end_ms = int(segment.get("endMs"))
        except (TypeError, ValueError):
            continue
        if start_ms >= 0 and end_ms > start_ms:
            return True
    return False


def normalize_provider_words(
    provider: str, payload: Any, origin_ms: int = 0
) -> list[dict[str, Any]]:
    """Return provider words with exact timing for local diarization alignment.

    Utterance-only APIs are represented as one timed item per utterance. That
    still preserves the provider's real interval and lets the local speaker
    timeline split it deterministically when a turn crosses the utterance.
    """
    if not isinstance(payload, dict):
        return []
    key = str(provider or "").strip().lower()
    words: list[dict[str, Any]] = []
    concatenate = False
    if key in {"soniox", "soniox_async"}:
        words = _timed_items(
            payload.get("tokens", []), start_key="start_ms", end_key="end_ms", scale=1
        )
        concatenate = True
        alignment_quality = "exact_word"
    elif key == "assemblyai":
        source_items = payload.get("words") or payload.get("utterances") or []
        words = _timed_items(source_items, start_key="start", end_key="end", scale=1)
        alignment_quality = "exact_word" if payload.get("words") else "provider_segment"
    elif key in {"mistral", "mistral_async"}:
        words = _timed_items(
            payload.get("words") or payload.get("segments", []),
            start_key="start",
            end_key="end",
            scale=1_000,
        )
        alignment_quality = "exact_word" if payload.get("words") else "provider_segment"
    elif key in {"smallest", "smallest_async"}:
        source_items = payload.get("words") or payload.get("utterances") or []
        words = _timed_items(
            source_items, start_key="start", end_key="end", scale=1_000
        )
        alignment_quality = "exact_word" if payload.get("words") else "provider_segment"
    elif key in {"gladia", "gladia_async"}:
        words = _gladia_utterances(payload)
        alignment_quality = "provider_segment"
    elif key == "speechmatics_async":
        words = _speechmatics_words(payload)
        alignment_quality = "exact_word"
    elif key == "azure_mai":
        words = _azure_phrase_items(payload)
        alignment_quality = "provider_segment"
    elif key == "deepgram_async":
        try:
            source_items = payload["results"]["channels"][0]["alternatives"][0]["words"]
        except (KeyError, IndexError, TypeError):
            source_items = []
        words = _timed_items(source_items, start_key="start", end_key="end", scale=1_000)
        alignment_quality = "exact_word"
    elif key == "openai_async":
        source_items = payload.get("words") or payload.get("segments") or []
        words = _timed_items(
            source_items, start_key="start", end_key="end", scale=1_000
        )
        alignment_quality = "exact_word" if payload.get("words") else "provider_segment"
    else:
        source_items = payload.get("words") or []
        words = _timed_items(source_items, start_key="start", end_key="end", scale=1)
        alignment_quality = "exact_word"
    for word in words:
        word["startMs"] += max(0, int(origin_ms))
        word["endMs"] += max(0, int(origin_ms))
        word["concatenate"] = concatenate
        word["alignmentQuality"] = alignment_quality
    return words
