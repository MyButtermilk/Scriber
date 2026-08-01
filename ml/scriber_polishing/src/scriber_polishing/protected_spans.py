"""Versioned, fail-closed protection for literals that must remain verbatim."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator

from .model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION

LEGACY_POLICY_ID: Final = "legacy_scriber_v1"
GEMMA_UNUSED_POLICY_ID: Final = "gemma_unused_v1"
LEXICAL_KEEP_POLICY_ID: Final = "gemma_lexical_v1"
PROTECTION_POLICY_FILENAME: Final = "scriber-protection-policy.json"
GEMMA_UNUSED_MARKER_START: Final = 6200
GEMMA_UNUSED_MARKER_END: Final = 6231
GEMMA_UNUSED_MAX_SPANS: Final = 32
LEXICAL_KEEP_MARKER_START: Final = 0
LEXICAL_KEEP_MARKER_END: Final = 31
LEXICAL_KEEP_MAX_SPANS: Final = 32

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "protection_policy_evidence_schema.json"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNUSED_MARKER = re.compile(r"<unused([0-9]+)>")
_LEXICAL_KEEP_MARKER = re.compile(r"⟦KEEP_([A-Z]{1,2})⟧")
_LEGACY_RESERVED = "⟦SCRIBER_PROTECTED_"
_LEXICAL_KEEP_RESERVED = "⟦KEEP_"


class ProtectionPolicyError(ValueError):
    """A protection policy or one of its runtime bindings is invalid."""


class TrainingPairProtectionError(ProtectionPolicyError):
    """A source/target pair cannot be masked without ambiguity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProtectedSpan:
    """A verbatim fragment and its collision-resistant placeholder."""

    placeholder: str
    value: str
    occurrences: int = 1


@dataclass(frozen=True)
class ProtectedText:
    """Text suitable for model processing plus the values removed from it."""

    text: str
    spans: tuple[ProtectedSpan, ...]


@dataclass(frozen=True, slots=True)
class ProtectedTrainingPair:
    """One source/target pair masked with an identical marker mapping."""

    source_text: str
    target_sst: str
    spans: tuple[ProtectedSpan, ...]
    protection_source: str = "runtime_detector_v1"
    mapped_occurrence_count: int = 0
    deduplicated_source_occurrence_count: int = 0


_PROTECTED_PATTERN = re.compile(
    r"(?:"
    r"https?://[^\s,]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"
    r"|Az\.\s*[\w./ -]+?(?=(?:,|\.|;|$))"
    r"|§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+|Buchst\.\s*[a-z]))*(?:\s+[A-Z][A-Za-z0-9]*)?"
    r"|\b\d{1,2}\.\d{1,2}\.\d{4}\b"
    r"|\b\d{1,2}:\d{2}\s*Uhr\b"
    r"|\b\d+(?:[.,]\d+)?\s*(?:kWh/\(m²·a\)|€/m²|m²|m³|kWh|kW|MWh|MW|km/h|m/s)(?=$|[\s,.;:!?])"
    r"|\b\d{1,3}(?:\.\d{3})*,\d{2}\s*€"
    r"|\b\d+(?:,\d+)?\s*%"
    r"|\bModell\s+[A-Za-z]+\d+\b"
    r"|\b(?:Sprecher(?:in)?|Speaker)\s*:"
    r"|\[\d{2}:\d{2}:\d{2}\]"
    r")"
)

_AMOUNT = re.compile(
    r"(?<!\w)(?:"
    r"(?:EUR|USD|CHF|€|\$|£)\s*[-+]?\d[\d.]*(?:,\d+)?"
    r"|[-+]?\d[\d.]*(?:,\d+)?\s*(?:EUR|USD|CHF|€|\$|£)"
    r")(?!\w)",
    re.IGNORECASE,
)
_LEGAL_REFERENCE = re.compile(
    r"(?<!\w)(?:"
    r"§§?\s*\d+[a-zA-Z]*(?:\s+(?:Abs\.\s*\d+|Satz\s*\d+|Nr\.\s*\d+|"
    r"Buchst\.\s*[a-z]))*(?:\s+[A-Z][A-Za-z0-9.-]*)?"
    r"|(?:Art\.|Artikel)\s*\d+[a-zA-Z]*(?:\s+[A-Z][A-Za-z0-9.-]*)?"
    r"|ECLI:[A-Z]{2,}:[A-Z0-9]+(?::[A-Z0-9.]+)+"
    r"|Az\.\s*[\w./ -]+?(?=(?:,|;|$))"
    r")",
)
_NORM = re.compile(
    r"(?<!\w)(?:(?:DIN(?:\s+EN)?|ISO|IEC|VDE)\s+[A-Z0-9]+(?:[-:/.][A-Z0-9]+)*)(?!\w)",
    re.IGNORECASE,
)
_CRITICAL_LITERAL = re.compile(
    r"(?:"
    r"https?://[^\s,]+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b[A-ZÄÖÜ]{2,}[/-][A-Z0-9ÄÖÜ/-]*\d[A-Z0-9ÄÖÜ/-]*\b"
    r"|\b(?:DE\d{20}|[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){10,30})\b"
    r"|\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
    r")"
)
_NUMBER = re.compile(r"[-+]?\d+(?:[.,:/-]\d+)*")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _policy_self_hash(value: Mapping[str, object]) -> str:
    payload = dict(value)
    payload.pop("policy_sha256", None)
    return _sha256(_canonical_json(payload))


@cache
def _policy_validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtectionPolicyError(
            f"cannot load protection policy schema: {error}"
        ) from error
    return Draft202012Validator(schema)


def _as_token_ids(value: Any) -> list[int]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ProtectionPolicyError("tokenizer did not return a token-ID sequence")
    result = list(value)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in result):
        raise ProtectionPolicyError("tokenizer returned a non-integer marker token ID")
    return result


def _encode_marker(tokenizer: Any, marker: str) -> list[int]:
    encoder = getattr(tokenizer, "encode", None)
    if callable(encoder):
        try:
            return _as_token_ids(encoder(marker, add_special_tokens=False))
        except TypeError:
            return _as_token_ids(encoder(marker))
    try:
        encoded = tokenizer(marker, add_special_tokens=False)
    except TypeError as error:
        raise ProtectionPolicyError(
            "tokenizer cannot encode protection markers without special tokens"
        ) from error
    if not isinstance(encoded, Mapping) or "input_ids" not in encoded:
        raise ProtectionPolicyError("tokenizer call did not return input_ids")
    return _as_token_ids(encoded["input_ids"])


def _runtime_tokenizer_binding(
    tokenizer: Any,
    *,
    tokenizer_identity: Mapping[str, object],
) -> dict[str, object]:
    if tokenizer_identity.get("model_id") != BASE_MODEL_ID:
        raise ProtectionPolicyError("protection tokenizer must use the pinned Gemma base")
    if tokenizer_identity.get("revision") != BASE_MODEL_REVISION:
        raise ProtectionPolicyError(
            "protection tokenizer revision differs from the pinned Gemma revision"
        )
    for field in (
        "backend_sha256",
        "chat_template_sha256",
        "identity_sha256",
    ):
        if not isinstance(tokenizer_identity.get(field), str) or not _SHA256.fullmatch(
            str(tokenizer_identity[field])
        ):
            raise ProtectionPolicyError(
                f"tokenizer identity requires a valid {field}"
            )
    vocab_size = tokenizer_identity.get("vocab_size")
    if (
        isinstance(vocab_size, bool)
        or not isinstance(vocab_size, int)
        or vocab_size <= 0
    ):
        raise ProtectionPolicyError(
            "tokenizer identity has an invalid or insufficient vocabulary size"
        )
    runtime_vocab_size = getattr(tokenizer, "vocab_size", None)
    if runtime_vocab_size != vocab_size:
        raise ProtectionPolicyError(
            "runtime tokenizer vocabulary size differs from its bound identity"
        )
    backend = getattr(tokenizer, "backend_tokenizer", None)
    if backend is None:
        backend = getattr(tokenizer, "_tokenizer", None)
    serializer = getattr(backend, "to_str", None)
    if not callable(serializer):
        raise ProtectionPolicyError(
            "runtime tokenizer cannot serialize its exact backend"
        )
    serialized_backend = serializer()
    if (
        not isinstance(serialized_backend, str)
        or _sha256(serialized_backend.encode("utf-8"))
        != tokenizer_identity["backend_sha256"]
    ):
        raise ProtectionPolicyError(
            "runtime tokenizer backend differs from protection evidence"
        )
    chat_template = getattr(tokenizer, "chat_template", None)
    if (
        not isinstance(chat_template, str | dict)
        or not chat_template
        or _sha256(_canonical_json(chat_template))
        != tokenizer_identity["chat_template_sha256"]
    ):
        raise ProtectionPolicyError(
            "runtime tokenizer chat template differs from protection evidence"
        )
    return {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "vocab_size": vocab_size,
        "backend_sha256": tokenizer_identity["backend_sha256"],
        "chat_template_sha256": tokenizer_identity["chat_template_sha256"],
        "identity_sha256": tokenizer_identity["identity_sha256"],
        "vocab_resize": False,
    }


def build_gemma_unused_policy_evidence(
    tokenizer: Any,
    *,
    tokenizer_identity: Mapping[str, object],
    marker_start: int = GEMMA_UNUSED_MARKER_START,
    marker_end: int = GEMMA_UNUSED_MARKER_END,
    max_spans: int = GEMMA_UNUSED_MAX_SPANS,
) -> dict[str, object]:
    """Materialize the exact one-token `<unusedN>` contract for pinned Gemma."""

    if (
        marker_start != GEMMA_UNUSED_MARKER_START
        or marker_end != GEMMA_UNUSED_MARKER_END
        or max_spans != GEMMA_UNUSED_MAX_SPANS
    ):
        raise ProtectionPolicyError(
            "gemma_unused_v1 requires the fixed high reserved range "
            f"unused{GEMMA_UNUSED_MARKER_START}..unused{GEMMA_UNUSED_MARKER_END} "
            f"and max_spans={GEMMA_UNUSED_MAX_SPANS}"
        )
    capacity = marker_end - marker_start + 1
    if max_spans > capacity:
        raise ProtectionPolicyError("max_spans must fit inside the marker range")
    tokenizer_binding = _runtime_tokenizer_binding(
        tokenizer,
        tokenizer_identity=tokenizer_identity,
    )
    all_special_ids = set(getattr(tokenizer, "all_special_ids", ()) or ())
    token_ids: list[int] = []
    markers: list[dict[str, object]] = []
    converter = getattr(tokenizer, "convert_tokens_to_ids", None)
    decoder = getattr(tokenizer, "decode", None)
    for marker_index in range(marker_start, marker_end + 1):
        marker = f"<unused{marker_index}>"
        encoded = _encode_marker(tokenizer, marker)
        if len(encoded) != 1:
            raise ProtectionPolicyError(
                f"{marker} must encode to exactly one token, got {len(encoded)}"
            )
        token_id = encoded[0]
        if not 0 <= token_id < int(tokenizer_binding["vocab_size"]):
            raise ProtectionPolicyError(f"{marker} token ID is outside the base vocabulary")
        if token_id in all_special_ids:
            raise ProtectionPolicyError(
                f"{marker} must not be treated as a skipped special token"
            )
        if callable(converter) and converter(marker) != token_id:
            raise ProtectionPolicyError(
                f"{marker} conversion and encoding token IDs differ"
            )
        if callable(decoder):
            if decoder([token_id], skip_special_tokens=False) != marker:
                raise ProtectionPolicyError(
                    f"{marker} does not round-trip through tokenizer.decode"
                )
            if decoder([token_id], skip_special_tokens=True) != marker:
                raise ProtectionPolicyError(
                    f"{marker} is removed by skip_special_tokens"
                )
        token_ids.append(token_id)
        markers.append(
            {
                "marker_index": marker_index,
                "marker": marker,
                "token_id": token_id,
            }
        )
    if len(set(token_ids)) != len(token_ids):
        raise ProtectionPolicyError("protected marker token IDs must be unique")
    evidence: dict[str, object] = {
        "schema_version": 1,
        "policy_id": GEMMA_UNUSED_POLICY_ID,
        "detector": "product_critical_v1",
        "marker_format": "<unused{index}>",
        "marker_start": marker_start,
        "marker_end": marker_end,
        "max_spans": max_spans,
        "collision_scope": "all_unused_markers",
        "pair_mapping": "unique_exact_value_v1",
        "tokenizer": tokenizer_binding,
        "markers": markers,
        "marker_token_ids_sha256": _sha256(_canonical_json(token_ids)),
    }
    evidence["policy_sha256"] = _policy_self_hash(evidence)
    return validate_protection_policy_evidence(evidence, tokenizer=tokenizer)


def _lexical_keep_label(index: int) -> str:
    if not 0 <= index < LEXICAL_KEEP_MAX_SPANS:
        raise ProtectionPolicyError("lexical marker index is outside the fixed range")
    quotient, remainder = divmod(index, 26)
    if quotient == 0:
        return chr(ord("A") + remainder)
    return chr(ord("A") + quotient - 1) + chr(ord("A") + remainder)


def _lexical_keep_marker(index: int) -> str:
    return f"⟦KEEP_{_lexical_keep_label(index)}⟧"


def build_lexical_keep_policy_evidence(
    tokenizer: Any,
    *,
    tokenizer_identity: Mapping[str, object],
    marker_start: int = LEXICAL_KEEP_MARKER_START,
    marker_end: int = LEXICAL_KEEP_MARKER_END,
    max_spans: int = LEXICAL_KEEP_MAX_SPANS,
) -> dict[str, object]:
    """Materialize copyable, per-occurrence lexical markers for pinned Gemma."""

    if (
        marker_start != LEXICAL_KEEP_MARKER_START
        or marker_end != LEXICAL_KEEP_MARKER_END
        or max_spans != LEXICAL_KEEP_MAX_SPANS
    ):
        raise ProtectionPolicyError(
            "gemma_lexical_v1 requires the fixed A..AF marker range "
            f"and max_spans={LEXICAL_KEEP_MAX_SPANS}"
        )
    tokenizer_binding = _runtime_tokenizer_binding(
        tokenizer,
        tokenizer_identity=tokenizer_identity,
    )
    all_special_ids = set(getattr(tokenizer, "all_special_ids", ()) or ())
    markers: list[dict[str, object]] = []
    token_sequences: list[list[int]] = []
    decoder = getattr(tokenizer, "decode", None)
    for marker_index in range(marker_start, marker_end + 1):
        marker = _lexical_keep_marker(marker_index)
        token_ids = _encode_marker(tokenizer, marker)
        if not token_ids or len(token_ids) > 8:
            raise ProtectionPolicyError(
                f"{marker} must encode to between one and eight tokens"
            )
        if any(
            not 0 <= token_id < int(tokenizer_binding["vocab_size"])
            for token_id in token_ids
        ):
            raise ProtectionPolicyError(
                f"{marker} contains a token ID outside the base vocabulary"
            )
        if any(token_id in all_special_ids for token_id in token_ids):
            raise ProtectionPolicyError(
                f"{marker} must not contain a skipped special token"
            )
        if callable(decoder):
            if decoder(token_ids, skip_special_tokens=False) != marker:
                raise ProtectionPolicyError(
                    f"{marker} does not round-trip through tokenizer.decode"
                )
            if decoder(token_ids, skip_special_tokens=True) != marker:
                raise ProtectionPolicyError(
                    f"{marker} is altered by skip_special_tokens"
                )
        token_sequences.append(token_ids)
        markers.append(
            {
                "marker_index": marker_index,
                "marker": marker,
                "token_count": len(token_ids),
                "token_ids": token_ids,
            }
        )
    if len({tuple(token_ids) for token_ids in token_sequences}) != len(
        token_sequences
    ):
        raise ProtectionPolicyError(
            "lexical protection marker token sequences must be unique"
        )
    evidence: dict[str, object] = {
        "schema_version": 1,
        "policy_id": LEXICAL_KEEP_POLICY_ID,
        "detector": "product_critical_v1",
        "marker_format": "⟦KEEP_{label}⟧",
        "marker_start": marker_start,
        "marker_end": marker_end,
        "max_spans": max_spans,
        "collision_scope": "all_lexical_keep_markers",
        "pair_mapping": "ordered_occurrence_v1",
        "tokenizer": tokenizer_binding,
        "markers": markers,
        "marker_token_sequences_sha256": _sha256(
            _canonical_json(token_sequences)
        ),
    }
    evidence["policy_sha256"] = _policy_self_hash(evidence)
    return validate_protection_policy_evidence(evidence, tokenizer=tokenizer)


def legacy_policy_evidence(
    tokenizer_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Describe the byte-compatible historical policy without changing its text."""

    evidence: dict[str, object] = {
        "schema_version": 1,
        "policy_id": LEGACY_POLICY_ID,
        "detector": "legacy_regex_v1",
        "marker_format": "⟦SCRIBER_PROTECTED_{index:04d}⟧",
    }
    if tokenizer_identity is not None:
        evidence["tokenizer"] = {
            key: tokenizer_identity[key]
            for key in (
                "model_id",
                "revision",
                "vocab_size",
                "backend_sha256",
                "chat_template_sha256",
                "identity_sha256",
            )
        }
    evidence["policy_sha256"] = _policy_self_hash(evidence)
    return validate_protection_policy_evidence(evidence)


def validate_protection_policy_evidence(
    value: Mapping[str, object],
    *,
    tokenizer: Any | None = None,
) -> dict[str, object]:
    """Validate schema, self-hash, fixed base binding, and every runtime marker."""

    if not isinstance(value, Mapping):
        raise ProtectionPolicyError("protection policy evidence must be an object")
    materialized = json.loads(json.dumps(dict(value), ensure_ascii=False))
    errors = sorted(
        _policy_validator().iter_errors(materialized),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].absolute_path) or "<root>"
        raise ProtectionPolicyError(
            f"protection policy schema failed at {location}: {errors[0].message}"
        )
    expected = _policy_self_hash(materialized)
    if materialized["policy_sha256"] != expected:
        raise ProtectionPolicyError(
            f"protection policy self-hash mismatch: expected {expected}"
        )
    policy_id = materialized["policy_id"]
    if policy_id == LEGACY_POLICY_ID:
        return materialized
    tokenizer_binding = materialized["tokenizer"]
    if (
        tokenizer_binding["model_id"] != BASE_MODEL_ID
        or tokenizer_binding["revision"] != BASE_MODEL_REVISION
        or tokenizer_binding["vocab_resize"] is not False
    ):
        raise ProtectionPolicyError(
            f"{policy_id} must bind the pinned base tokenizer without resize"
        )
    if policy_id == GEMMA_UNUSED_POLICY_ID:
        return _validate_gemma_unused_policy(
            materialized,
            tokenizer=tokenizer,
            tokenizer_binding=tokenizer_binding,
        )
    if policy_id == LEXICAL_KEEP_POLICY_ID:
        return _validate_lexical_keep_policy(
            materialized,
            tokenizer=tokenizer,
            tokenizer_binding=tokenizer_binding,
        )
    raise ProtectionPolicyError(f"unsupported protection policy: {policy_id}")


def _validate_gemma_unused_policy(
    materialized: dict[str, object],
    *,
    tokenizer: Any | None,
    tokenizer_binding: Mapping[str, object],
) -> dict[str, object]:
    if (
        materialized["marker_start"] != GEMMA_UNUSED_MARKER_START
        or materialized["marker_end"] != GEMMA_UNUSED_MARKER_END
        or materialized["max_spans"] != GEMMA_UNUSED_MAX_SPANS
    ):
        raise ProtectionPolicyError("gemma_unused_v1 marker range differs")
    markers = materialized["markers"]
    expected_indices = list(
        range(materialized["marker_start"], materialized["marker_end"] + 1)
    )
    if [item["marker_index"] for item in markers] != expected_indices:
        raise ProtectionPolicyError("protection marker indices are not contiguous")
    if [item["marker"] for item in markers] != [
        f"<unused{index}>" for index in expected_indices
    ]:
        raise ProtectionPolicyError("protection marker strings differ from their IDs")
    token_ids = [item["token_id"] for item in markers]
    if len(set(token_ids)) != len(token_ids):
        raise ProtectionPolicyError("protection marker token IDs are not unique")
    if materialized["marker_token_ids_sha256"] != _sha256(
        _canonical_json(token_ids)
    ):
        raise ProtectionPolicyError("protection marker token-ID hash differs")
    if tokenizer is not None:
        runtime_binding = _runtime_tokenizer_binding(
            tokenizer,
            tokenizer_identity=tokenizer_binding,
        )
        if runtime_binding != tokenizer_binding:
            raise ProtectionPolicyError(
                "runtime tokenizer binding differs from protection evidence"
            )
        all_special_ids = set(getattr(tokenizer, "all_special_ids", ()) or ())
        for marker, expected_token_id in zip(
            (item["marker"] for item in markers),
            token_ids,
            strict=True,
        ):
            encoded = _encode_marker(tokenizer, marker)
            if encoded != [expected_token_id] or expected_token_id in all_special_ids:
                raise ProtectionPolicyError(
                    f"runtime tokenizer no longer maps {marker} to its one non-special token"
                )
            decoder = getattr(tokenizer, "decode", None)
            if callable(decoder) and (
                decoder([expected_token_id], skip_special_tokens=False) != marker
                or decoder([expected_token_id], skip_special_tokens=True) != marker
            ):
                raise ProtectionPolicyError(
                    f"runtime tokenizer no longer round-trips {marker}"
                )
    return materialized


def _validate_lexical_keep_policy(
    materialized: dict[str, object],
    *,
    tokenizer: Any | None,
    tokenizer_binding: Mapping[str, object],
) -> dict[str, object]:
    if (
        materialized["marker_start"] != LEXICAL_KEEP_MARKER_START
        or materialized["marker_end"] != LEXICAL_KEEP_MARKER_END
        or materialized["max_spans"] != LEXICAL_KEEP_MAX_SPANS
    ):
        raise ProtectionPolicyError("gemma_lexical_v1 marker range differs")
    markers = materialized["markers"]
    expected_indices = list(
        range(LEXICAL_KEEP_MARKER_START, LEXICAL_KEEP_MARKER_END + 1)
    )
    if [item["marker_index"] for item in markers] != expected_indices:
        raise ProtectionPolicyError(
            "lexical protection marker indices are not contiguous"
        )
    if [item["marker"] for item in markers] != [
        _lexical_keep_marker(index) for index in expected_indices
    ]:
        raise ProtectionPolicyError(
            "lexical protection marker strings differ from their IDs"
        )
    token_sequences = [item["token_ids"] for item in markers]
    if any(
        item["token_count"] != len(item["token_ids"])
        for item in markers
    ):
        raise ProtectionPolicyError(
            "lexical protection marker token counts differ"
        )
    if len({tuple(token_ids) for token_ids in token_sequences}) != len(
        token_sequences
    ):
        raise ProtectionPolicyError(
            "lexical protection marker token sequences are not unique"
        )
    if materialized["marker_token_sequences_sha256"] != _sha256(
        _canonical_json(token_sequences)
    ):
        raise ProtectionPolicyError(
            "lexical protection marker token-sequence hash differs"
        )
    if tokenizer is not None:
        runtime_binding = _runtime_tokenizer_binding(
            tokenizer,
            tokenizer_identity=tokenizer_binding,
        )
        if runtime_binding != tokenizer_binding:
            raise ProtectionPolicyError(
                "runtime tokenizer binding differs from protection evidence"
            )
        all_special_ids = set(getattr(tokenizer, "all_special_ids", ()) or ())
        decoder = getattr(tokenizer, "decode", None)
        for item in markers:
            marker = item["marker"]
            expected_token_ids = item["token_ids"]
            encoded = _encode_marker(tokenizer, marker)
            if encoded != expected_token_ids or any(
                token_id in all_special_ids for token_id in expected_token_ids
            ):
                raise ProtectionPolicyError(
                    f"runtime tokenizer no longer maps {marker} to its bound tokens"
                )
            if callable(decoder) and (
                decoder(expected_token_ids, skip_special_tokens=False) != marker
                or decoder(expected_token_ids, skip_special_tokens=True) != marker
            ):
                raise ProtectionPolicyError(
                    f"runtime tokenizer no longer round-trips {marker}"
                )
    return materialized


def product_span_ranges(text: str) -> tuple[tuple[int, int, str], ...]:
    """Return deterministic, non-overlapping product-critical literal ranges."""

    matches: list[tuple[int, int, str]] = []
    for pattern in (
        _AMOUNT,
        _LEGAL_REFERENCE,
        _NORM,
        _CRITICAL_LITERAL,
        _NUMBER,
    ):
        matches.extend(
            (match.start(), match.end(), match.group(0))
            for match in pattern.finditer(text)
        )
    selected: list[tuple[int, int, str]] = []
    for item in sorted(matches, key=lambda match: (match[0], -(match[1] - match[0]))):
        if any(item[0] < other[1] and other[0] < item[1] for other in selected):
            continue
        selected.append(item)
    selected.sort()
    return tuple(selected)


def _unused_marker_collision(text: str) -> bool:
    return _UNUSED_MARKER.search(text) is not None


def _policy_marker_collision(text: str, policy_id: object) -> bool:
    if policy_id == GEMMA_UNUSED_POLICY_ID:
        return _unused_marker_collision(text)
    if policy_id == LEXICAL_KEEP_POLICY_ID:
        return _LEXICAL_KEEP_RESERVED in text
    return False


def _policy_markers(validated: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(item["marker"]) for item in validated["markers"])


def protect_with_policy(
    text: str,
    policy: Mapping[str, object],
) -> ProtectedText:
    """Mask product-critical spans with a verified versioned policy."""

    validated = validate_protection_policy_evidence(policy)
    return _protect_with_validated_policy(text, validated)


def _protect_with_validated_policy(
    text: str,
    validated: Mapping[str, object],
) -> ProtectedText:
    """Internal hot path for policy evidence already verified at load time."""

    if validated["policy_id"] == LEGACY_POLICY_ID:
        return protect_spans(text)
    if _policy_marker_collision(text, validated["policy_id"]):
        raise ProtectionPolicyError(
            "source contains a reserved protection marker"
        )
    ranges = product_span_ranges(text)
    if validated["policy_id"] == LEXICAL_KEEP_POLICY_ID:
        if len(ranges) > validated["max_spans"]:
            raise ProtectionPolicyError(
                "source exceeds the protection policy max_spans gate"
            )
        marker_strings = _policy_markers(validated)
        spans = tuple(
            ProtectedSpan(
                placeholder=marker_strings[index],
                value=value,
            )
            for index, (_start, _end, value) in enumerate(ranges)
        )
        pieces: list[str] = []
        position = 0
        for (start, end, _value), span in zip(ranges, spans, strict=True):
            pieces.append(text[position:start])
            pieces.append(span.placeholder)
            position = end
        pieces.append(text[position:])
        return ProtectedText(text="".join(pieces), spans=spans)
    ordered_values = list(dict.fromkeys(value for _start, _end, value in ranges))
    if len(ordered_values) > validated["max_spans"]:
        raise ProtectionPolicyError(
            "source exceeds the protection policy max_spans gate"
        )
    counts = Counter(value for _start, _end, value in ranges)
    marker_start = int(validated["marker_start"])
    spans = tuple(
        ProtectedSpan(
            placeholder=f"<unused{marker_start + index}>",
            value=value,
            occurrences=counts[value],
        )
        for index, value in enumerate(ordered_values)
    )
    by_value = {span.value: span for span in spans}
    pieces: list[str] = []
    position = 0
    for start, end, value in ranges:
        placeholder = by_value[value].placeholder
        pieces.append(text[position:start])
        pieces.append(placeholder)
        position = end
    pieces.append(text[position:])
    return ProtectedText(text="".join(pieces), spans=spans)


def mask_training_pair(
    source_text: str,
    target_sst: str,
    policy: Mapping[str, object],
) -> ProtectedTrainingPair:
    """Mask source and target identically or reject the pair with a stable code."""

    validated = validate_protection_policy_evidence(policy)
    return _mask_training_pair_validated(
        source_text,
        target_sst,
        validated,
    )


def _mask_training_pair_validated(
    source_text: str,
    target_sst: str,
    validated: Mapping[str, object],
    *,
    declared_target_values: Sequence[str] | None = None,
    value_mappings: Sequence[Mapping[str, object]] | None = None,
) -> ProtectedTrainingPair:
    """Internal bulk path; callers must validate the policy exactly once."""

    if validated["policy_id"] == LEGACY_POLICY_ID:
        return ProtectedTrainingPair(source_text, target_sst, ())
    if _policy_marker_collision(
        source_text,
        validated["policy_id"],
    ) or _policy_marker_collision(target_sst, validated["policy_id"]):
        raise TrainingPairProtectionError(
            "reserved_marker_collision",
            "training pair contains a reserved protection marker",
        )
    if (
        validated["policy_id"] == LEXICAL_KEEP_POLICY_ID
        and declared_target_values is not None
    ):
        try:
            return _mask_declared_training_pair(
                source_text,
                target_sst,
                validated,
                declared_target_values=declared_target_values,
                value_mappings=value_mappings or (),
            )
        except TrainingPairProtectionError as error:
            if error.code != "declared_protected_value_mismatch":
                raise
            return _mask_training_pair_validated(
                source_text,
                target_sst,
                validated,
            )
    ranges = product_span_ranges(source_text)
    if validated["policy_id"] == LEXICAL_KEEP_POLICY_ID:
        if len(ranges) > validated["max_spans"]:
            raise TrainingPairProtectionError(
                "max_spans_exceeded",
                "training pair exceeds the protection policy max_spans gate",
            )
        values = [value for _start, _end, value in ranges]
        target_text_for_detection = re.sub(
            r"\[/?[A-Z][A-Z0-9_]*\]",
            "",
            target_sst,
        )
        target_values = [
            value
            for _start, _end, value in product_span_ranges(
                target_text_for_detection
            )
        ]
        if values != target_values:
            raise TrainingPairProtectionError(
                "protected_value_mismatch",
                "source and target protected-value occurrences differ",
            )
        source_protected = protect_with_policy(source_text, validated)
        target_pieces: list[str] = []
        target_position = 0
        for span in source_protected.spans:
            value_position = target_sst.find(span.value, target_position)
            if value_position < 0:
                raise TrainingPairProtectionError(
                    "ambiguous_target_value",
                    "a protected occurrence cannot be mapped in target order",
                )
            target_pieces.append(target_sst[target_position:value_position])
            target_pieces.append(span.placeholder)
            target_position = value_position + len(span.value)
        target_pieces.append(target_sst[target_position:])
        target_masked = "".join(target_pieces)
        if any(
            source_protected.text.count(span.placeholder) != 1
            or target_masked.count(span.placeholder) != 1
            for span in source_protected.spans
        ):
            raise TrainingPairProtectionError(
                "marker_uniqueness_failed",
                "masked pair does not contain every marker exactly once per side",
            )
        return ProtectedTrainingPair(
            source_text=source_protected.text,
            target_sst=target_masked,
            spans=source_protected.spans,
        )
    ordered_values = list(dict.fromkeys(value for _start, _end, value in ranges))
    if len(ordered_values) > validated["max_spans"]:
        raise TrainingPairProtectionError(
            "max_spans_exceeded",
            "training pair exceeds the protection policy max_spans gate",
        )
    for index, left in enumerate(ordered_values):
        for right in ordered_values[index + 1 :]:
            if left == right or (left not in right and right not in left):
                continue
            shorter, longer = sorted((left, right), key=len)
            terminal_url_overlap = (
                shorter.startswith(("http://", "https://"))
                and longer.startswith(("http://", "https://"))
                and longer[:-1] == shorter
                and longer[-1] in ".,;:!?"
            )
            code = (
                "terminal_url_punctuation_overlap"
                if terminal_url_overlap
                else "overlapping_distinct_values"
            )
            raise TrainingPairProtectionError(
                code,
                "distinct protected values overlap and cannot be replaced unambiguously",
            )
    values = [value for _start, _end, value in ranges]
    source_counts = Counter(values)
    target_text_for_detection = re.sub(
        r"\[/?[A-Z][A-Z0-9_]*\]",
        "",
        target_sst,
    )
    target_ranges = product_span_ranges(target_text_for_detection)
    target_values = [value for _start, _end, value in target_ranges]
    if source_counts != Counter(target_values):
        raise TrainingPairProtectionError(
            "protected_value_mismatch",
            "source and target protected values differ",
        )
    marker_start = int(validated["marker_start"])
    spans = tuple(
        ProtectedSpan(
            placeholder=f"<unused{marker_start + index}>",
            value=value,
            occurrences=source_counts[value],
        )
        for index, value in enumerate(ordered_values)
    )
    source_protected = protect_with_policy(source_text, validated)
    if source_protected.spans != spans:
        raise TrainingPairProtectionError(
            "source_mapping_invariant",
            "source marker mapping changed during pair protection",
        )
    target_masked = target_sst
    for span in sorted(spans, key=lambda item: len(item.value), reverse=True):
        if target_masked.count(span.value) != span.occurrences:
            raise TrainingPairProtectionError(
                "ambiguous_target_value",
                "a protected value cannot be mapped count-preservingly in the target",
            )
        target_masked = target_masked.replace(span.value, span.placeholder)
    if any(
        source_protected.text.count(span.placeholder) != span.occurrences
        or target_masked.count(span.placeholder) != span.occurrences
        for span in spans
    ):
        raise TrainingPairProtectionError(
            "marker_uniqueness_failed",
            "masked pair does not contain every marker exactly once per side",
        )
    return ProtectedTrainingPair(
        source_text=source_protected.text,
        target_sst=target_masked,
        spans=spans,
    )


def _exact_declared_surface_pattern(value: str) -> re.Pattern[str]:
    if not value:
        raise TrainingPairProtectionError(
            "invalid_declared_protected_value",
            "declared protected values must be non-empty",
        )
    left_boundary = r"(?<!\w)" if value[0].isalnum() else ""
    right_boundary = r"(?!\w)" if value[-1].isalnum() else ""
    return re.compile(f"{left_boundary}{re.escape(value)}{right_boundary}")


def _declared_nonoverlapping_ranges(
    text: str,
    surfaces: Sequence[tuple[str, str]],
) -> list[tuple[int, int, str, str]]:
    """Select longest declared surfaces and retain their canonical identities."""

    surface_to_canonical: dict[str, str] = {}
    for surface, canonical in surfaces:
        if not surface or not canonical:
            raise TrainingPairProtectionError(
                "invalid_declared_value_mapping",
                "declared value mappings require non-empty source and target values",
            )
        previous = surface_to_canonical.setdefault(surface, canonical)
        if previous != canonical:
            raise TrainingPairProtectionError(
                "ambiguous_declared_value_mapping",
                "one declared source surface maps to multiple protected target values",
            )
    candidates: list[tuple[int, int, str, str]] = []
    for ordinal, (surface, canonical) in enumerate(surface_to_canonical.items()):
        for match in _exact_declared_surface_pattern(surface).finditer(text):
            candidates.append(
                (match.start(), match.end(), surface, canonical, ordinal)
            )
    candidates.sort(
        key=lambda item: (
            -(item[1] - item[0]),
            item[0],
            item[4],
        )
    )
    selected: list[tuple[int, int, str, str]] = []
    for start, end, surface, canonical, _ordinal in candidates:
        if any(start < selected_end and end > selected_start for selected_start, selected_end, *_ in selected):
            continue
        selected.append((start, end, surface, canonical))
    selected.sort(key=lambda item: item[0])
    return selected


def _replace_declared_ranges(
    text: str,
    ranges: Sequence[tuple[int, int, str, str]],
    markers: Sequence[str],
) -> str:
    if len(ranges) != len(markers):
        raise TrainingPairProtectionError(
            "declared_marker_count_mismatch",
            "declared protected ranges and markers must have identical counts",
        )
    pieces: list[str] = []
    position = 0
    for (start, end, _surface, _canonical), marker in zip(
        ranges,
        markers,
        strict=True,
    ):
        pieces.append(text[position:start])
        pieces.append(marker)
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


def _mask_declared_training_pair(
    source_text: str,
    target_sst: str,
    validated: Mapping[str, object],
    *,
    declared_target_values: Sequence[str],
    value_mappings: Sequence[Mapping[str, object]],
) -> ProtectedTrainingPair:
    """Mask schema-declared semantic values through reversible source mappings."""

    if isinstance(declared_target_values, str | bytes) or any(
        not isinstance(value, str) or not value
        for value in declared_target_values
    ):
        raise TrainingPairProtectionError(
            "invalid_declared_protected_value",
            "declared protected values must be a sequence of non-empty strings",
        )
    canonical_values = tuple(dict.fromkeys(declared_target_values))
    target_surfaces = tuple((value, value) for value in canonical_values)
    source_surfaces: list[tuple[str, str]] = list(target_surfaces)
    mapped_surfaces: set[tuple[str, str]] = set()
    canonical_set = set(canonical_values)
    for mapping in value_mappings:
        if not isinstance(mapping, Mapping):
            raise TrainingPairProtectionError(
                "invalid_declared_value_mapping",
                "declared value mappings must be objects",
            )
        source = mapping.get("source")
        target = mapping.get("target")
        kind = mapping.get("kind")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(target, str)
            or not target
            or not isinstance(kind, str)
            or not kind
        ):
            raise TrainingPairProtectionError(
                "invalid_declared_value_mapping",
                "declared value mappings require non-empty kind, source, and target",
            )
        if target in canonical_set:
            source_surfaces.append((source, target))
            mapped_surfaces.add((source, target))

    source_ranges = _declared_nonoverlapping_ranges(source_text, source_surfaces)
    target_ranges = _declared_nonoverlapping_ranges(target_sst, target_surfaces)
    source_canonical = [item[3] for item in source_ranges]
    target_canonical = [item[3] for item in target_ranges]
    deduplicated_source_occurrence_count = 0
    if source_canonical != target_canonical:
        target_counter = Counter(target_canonical)
        source_counter = Counter(source_canonical)
        duplicate_only = (
            bool(target_canonical)
            and set(source_canonical) == set(target_canonical)
            and all(
                source_counter[value] >= count
                for value, count in target_counter.items()
            )
        )
        selected_source_ranges: list[tuple[int, int, str, str]] = []
        source_position = 0
        if duplicate_only:
            for canonical in target_canonical:
                while (
                    source_position < len(source_ranges)
                    and source_ranges[source_position][3] != canonical
                ):
                    source_position += 1
                if source_position >= len(source_ranges):
                    duplicate_only = False
                    break
                selected_source_ranges.append(source_ranges[source_position])
                source_position += 1
        if not duplicate_only:
            raise TrainingPairProtectionError(
                "declared_protected_value_mismatch",
                "declared source and target protected-value occurrences differ",
            )
        deduplicated_source_occurrence_count = (
            len(source_ranges) - len(selected_source_ranges)
        )
        source_ranges = selected_source_ranges
    if len(source_ranges) > int(validated["max_spans"]):
        raise TrainingPairProtectionError(
            "max_spans_exceeded",
            "training pair exceeds the protection policy max_spans gate",
        )
    policy_markers = _policy_markers(validated)
    markers = policy_markers[: len(source_ranges)]
    source_masked = _replace_declared_ranges(source_text, source_ranges, markers)
    target_masked = _replace_declared_ranges(target_sst, target_ranges, markers)
    spans = tuple(
        ProtectedSpan(placeholder=marker, value=source_range[2])
        for marker, source_range in zip(markers, source_ranges, strict=True)
    )
    if any(
        source_masked.count(span.placeholder) != 1
        or target_masked.count(span.placeholder) != 1
        for span in spans
    ):
        raise TrainingPairProtectionError(
            "marker_uniqueness_failed",
            "masked pair does not contain every marker exactly once per side",
        )
    mapped_occurrence_count = sum(
        (source_range[2], source_range[3]) in mapped_surfaces
        and source_range[2] != source_range[3]
        for source_range in source_ranges
    )
    return ProtectedTrainingPair(
        source_text=source_masked,
        target_sst=target_masked,
        spans=spans,
        protection_source="record_declared_reversible_values_v1",
        mapped_occurrence_count=mapped_occurrence_count,
        deduplicated_source_occurrence_count=(
            deduplicated_source_occurrence_count
        ),
    )


def protect_spans(text: str) -> ProtectedText:
    """Replace protected values with unique placeholders before model processing."""
    spans: list[ProtectedSpan] = []

    def replace(match: re.Match[str]) -> str:
        placeholder = f"⟦SCRIBER_PROTECTED_{len(spans):04d}⟧"
        spans.append(ProtectedSpan(placeholder=placeholder, value=match.group(0)))
        return placeholder

    return ProtectedText(text=_PROTECTED_PATTERN.sub(replace, text), spans=tuple(spans))


def restore_spans(protected: ProtectedText, text: str) -> str:
    """Restore only an intact placeholder set; otherwise reject the model output."""
    lexical_placeholders = tuple(
        span.placeholder
        for span in protected.spans
        if _LEXICAL_KEEP_MARKER.fullmatch(span.placeholder) is not None
    )
    if lexical_placeholders:
        observed_placeholders = tuple(
            match.group(0) for match in _LEXICAL_KEEP_MARKER.finditer(text)
        )
        if observed_placeholders != lexical_placeholders:
            raise ValueError(
                "protected placeholders must occur exactly once in source order"
            )
    restored = text
    for span in protected.spans:
        if restored.count(span.placeholder) != span.occurrences:
            expectation = (
                "exactly once"
                if span.occurrences == 1
                else f"exactly {span.occurrences} times"
            )
            raise ValueError(
                f"each protected placeholder must occur {expectation}"
            )
        restored = restored.replace(span.placeholder, span.value)
    used_gemma_markers = any(
        _UNUSED_MARKER.fullmatch(span.placeholder) is not None
        for span in protected.spans
    )
    used_lexical_markers = any(
        _LEXICAL_KEEP_MARKER.fullmatch(span.placeholder) is not None
        for span in protected.spans
    )
    if _LEGACY_RESERVED in restored or (
        used_gemma_markers and _UNUSED_MARKER.search(restored)
    ) or (
        used_lexical_markers and _LEXICAL_KEEP_RESERVED in restored
    ):
        raise ValueError("unknown protected placeholder")
    return restored
