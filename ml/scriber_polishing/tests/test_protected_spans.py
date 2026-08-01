"""Behavioural tests for the protected-span public seam."""

from __future__ import annotations

import hashlib
import json

import pytest

from scriber_polishing.model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from scriber_polishing.protected_spans import (
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    ProtectionPolicyError,
    TrainingPairProtectionError,
    build_gemma_unused_policy_evidence,
    build_lexical_keep_policy_evidence,
    mask_training_pair,
    protect_spans,
    protect_with_policy,
    restore_spans,
)


class _Backend:
    def to_str(self) -> str:
        return '{"model":{"vocab":{"<unused6200>":6300}}}'


class _UnusedTokenizer:
    vocab_size = 7000
    all_special_ids = [0, 1, 2]
    chat_template = "{{ messages }}"
    backend_tokenizer = _Backend()

    def __len__(self) -> int:
        return self.vocab_size

    def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return [100 + int(marker.removeprefix("<unused").removesuffix(">"))]

    def convert_tokens_to_ids(self, marker: str) -> int:
        return 100 + int(marker.removeprefix("<unused").removesuffix(">"))

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        del skip_special_tokens
        return f"<unused{token_ids[0] - 100}>"


class _LexicalTokenizer(_UnusedTokenizer):
    _markers = tuple(
        [f"⟦KEEP_{chr(ord('A') + index)}⟧" for index in range(26)]
        + [f"⟦KEEP_A{chr(ord('A') + index)}⟧" for index in range(6)]
    )
    _token_ids = {
        marker: [500 + index * 8 + offset for offset in range(5)]
        for index, marker in enumerate(_markers)
    }
    _decoded = {
        tuple(token_ids): marker for marker, token_ids in _token_ids.items()
    }

    def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return list(self._token_ids[marker])

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool) -> str:
        del skip_special_tokens
        return self._decoded[tuple(token_ids)]


def _tokenizer_identity() -> dict[str, object]:
    backend = _Backend().to_str().encode()
    template = (
        json.dumps(
            _UnusedTokenizer.chat_template,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "vocab_size": 7000,
        "backend_sha256": "sha256:" + hashlib.sha256(backend).hexdigest(),
        "chat_template_sha256": (
            "sha256:" + hashlib.sha256(template).hexdigest()
        ),
        "identity_sha256": "sha256:" + "c" * 64,
    }


def _unused_policy() -> dict[str, object]:
    return build_gemma_unused_policy_evidence(
        _UnusedTokenizer(),
        tokenizer_identity=_tokenizer_identity(),
    )


def _lexical_policy() -> dict[str, object]:
    return build_lexical_keep_policy_evidence(
        _LexicalTokenizer(),
        tokenizer_identity=_tokenizer_identity(),
    )


def test_lexical_policy_assigns_one_copyable_marker_per_occurrence() -> None:
    policy = _lexical_policy()
    source = "Die Werte sind 12 und nochmals 12."
    target = "[DOC]\n[P]Die Werte sind 12 und nochmals 12.[/P]\n[/DOC]"

    masked = mask_training_pair(source, target, policy)

    assert policy["policy_id"] == LEXICAL_KEEP_POLICY_ID
    assert [span.placeholder for span in masked.spans] == [
        "⟦KEEP_A⟧",
        "⟦KEEP_B⟧",
    ]
    assert [span.value for span in masked.spans] == ["12", "12"]
    assert all(span.occurrences == 1 for span in masked.spans)
    for span in masked.spans:
        assert masked.source_text.count(span.placeholder) == 1
        assert masked.target_sst.count(span.placeholder) == 1
    protected = protect_with_policy(source, policy)
    assert restore_spans(protected, masked.target_sst) == target


@pytest.mark.parametrize(
    "candidate",
    [
        "[DOC]\n[P]⟦KEEP_B⟧ und ⟦KEEP_A⟧.[/P]\n[/DOC]",
        "[DOC]\n[P]⟦KEEP_A⟧ und ⟦KEEP_A⟧.[/P]\n[/DOC]",
        "[DOC]\n[P]⟦KEEP_A⟧.[/P]\n[/DOC]",
        "[DOC]\n[P]⟦KEEP_A⟧ und ⟦KEEP_B⟧ und ⟦KEEP_C⟧.[/P]\n[/DOC]",
    ],
)
def test_lexical_restore_rejects_marker_set_or_order_damage(
    candidate: str,
) -> None:
    protected = protect_with_policy(
        "Die Werte sind 12 und 13.",
        _lexical_policy(),
    )

    with pytest.raises(ValueError):
        restore_spans(protected, candidate)


def test_protect_and_restore_preserves_critical_values_exactly_once() -> None:
    source = (
        "Betrag 1.234,56 € und 12,5 % am 15.08.2026 um 09:30 Uhr. "
        "Mail a.b@example.de, https://example.org/a?b=1, Az. 12 O 34/25, "
        "ECLI:DE:BGH:2025:010125UVIIZR1.24.0, § 7 Abs. 4 Satz 2 EStG, "
        "Modell M2, Sprecherin: und [00:01:23]."
    )

    protected = protect_spans(source)

    assert protected.text != source
    assert len(protected.spans) == 12
    assert restore_spans(protected, protected.text) == source


def test_restore_fails_closed_when_a_placeholder_is_missing_or_duplicated() -> None:
    protected = protect_spans("Bitte 1.234,56 € überweisen.")
    placeholder = protected.spans[0].placeholder

    with pytest.raises(ValueError, match="exactly once"):
        restore_spans(protected, "Bitte überweisen.")

    with pytest.raises(ValueError, match="exactly once"):
        restore_spans(protected, f"Bitte {placeholder} und {placeholder} überweisen.")


def test_protection_covers_compound_professional_units() -> None:
    source = "Flächen: 84,5 m²; Energie: 76 kWh/(m²·a); Miete: 4,20 €/m²; Tempo: 120 km/h."

    protected = protect_spans(source)

    assert [span.value for span in protected.spans] == [
        "84,5 m²",
        "76 kWh/(m²·a)",
        "4,20 €/m²",
        "120 km/h",
    ]
    assert restore_spans(protected, protected.text) == source


def test_gemma_unused_policy_proves_one_token_markers_without_vocab_resize() -> None:
    policy = _unused_policy()

    assert policy["policy_id"] == GEMMA_UNUSED_POLICY_ID
    assert len(policy["markers"]) == 32
    assert policy["markers"][0] == {
        "marker_index": 6200,
        "marker": "<unused6200>",
        "token_id": 6300,
    }
    assert policy["markers"][-1] == {
        "marker_index": 6231,
        "marker": "<unused6231>",
        "token_id": 6331,
    }
    assert policy["tokenizer"]["vocab_resize"] is False
    assert policy["policy_sha256"].startswith("sha256:")

    class SplitMarkerTokenizer(_UnusedTokenizer):
        def encode(self, marker: str, *, add_special_tokens: bool) -> list[int]:
            del marker, add_special_tokens
            return [300, 301]

    with pytest.raises(ProtectionPolicyError, match="exactly one token"):
        build_gemma_unused_policy_evidence(
            SplitMarkerTokenizer(),
            tokenizer_identity=_tokenizer_identity(),
        )

    class SkippedMarkerTokenizer(_UnusedTokenizer):
        def decode(
            self,
            token_ids: list[int],
            *,
            skip_special_tokens: bool,
        ) -> str:
            if skip_special_tokens:
                return ""
            return super().decode(
                token_ids,
                skip_special_tokens=skip_special_tokens,
            )

    with pytest.raises(ProtectionPolicyError, match="skip_special_tokens"):
        build_gemma_unused_policy_evidence(
            SkippedMarkerTokenizer(),
            tokenizer_identity=_tokenizer_identity(),
        )


def test_gemma_unused_pair_masks_source_and_target_with_identical_mapping() -> None:
    policy = _unused_policy()
    source = "Bitte 12 € nach § 7 Abs. 4 bis 15.08.2026 prüfen."
    target = (
        "[DOC]\n[OL]\n[LI1]Bitte 12 € nach § 7 Abs. 4 bis "
        "15.08.2026 prüfen.[/LI1]\n[/OL]\n[/DOC]"
    )

    masked = mask_training_pair(source, target, policy)

    assert [span.placeholder for span in masked.spans] == [
        "<unused6200>",
        "<unused6201>",
        "<unused6202>",
    ]
    for span in masked.spans:
        assert masked.source_text.count(span.placeholder) == 1
        assert masked.target_sst.count(span.placeholder) == 1
        assert span.value not in masked.source_text
        assert span.value not in masked.target_sst
    assert restore_spans(
        type(protect_with_policy(source, policy))(
            masked.source_text,
            masked.spans,
        ),
        masked.source_text,
    ) == source


@pytest.mark.parametrize(
    ("source", "target", "code"),
    [
        (
            "Kollision <unused7> und 12.",
            "[DOC]\n[P]Kollision <unused7> und 12.[/P]\n[/DOC]",
            "reserved_marker_collision",
        ),
        (
            "Der Wert ist 12.",
            "[DOC]\n[P]Der Wert ist 13.[/P]\n[/DOC]",
            "protected_value_mismatch",
        ),
        (
            "URL https://example.test/a12 und Code 12.",
            "[DOC]\n[P]URL https://example.test/a12 und Code 12.[/P]\n[/DOC]",
            "overlapping_distinct_values",
        ),
        (
            "URLs https://example.test/ARC-184. und https://example.test/ARC-184",
            "[DOC]\n[P]URLs https://example.test/ARC-184. und "
            "https://example.test/ARC-184[/P]\n[/DOC]",
            "terminal_url_punctuation_overlap",
        ),
    ],
)
def test_gemma_unused_pair_rejects_collisions_and_ambiguous_or_changed_values(
    source: str,
    target: str,
    code: str,
) -> None:
    with pytest.raises(TrainingPairProtectionError) as captured:
        mask_training_pair(source, target, _unused_policy())

    assert captured.value.code == code


def test_gemma_unused_repeated_value_uses_one_marker_with_exact_count() -> None:
    policy = _unused_policy()
    source = "Die Werte sind 12 und nochmals 12."
    target = "[DOC]\n[P]Die Werte sind 12 und nochmals 12.[/P]\n[/DOC]"

    masked = mask_training_pair(source, target, policy)

    assert len(masked.spans) == 1
    assert masked.spans[0].placeholder == "<unused6200>"
    assert masked.spans[0].occurrences == 2
    assert masked.source_text.count("<unused6200>") == 2
    assert masked.target_sst.count("<unused6200>") == 2
    protected = protect_with_policy(source, policy)
    assert restore_spans(protected, masked.target_sst).count("12") == 2


def test_gemma_unused_policy_rejects_more_than_its_bounded_span_count() -> None:
    policy = _unused_policy()
    source = " ".join(f"Wert {index}." for index in range(100, 133))

    with pytest.raises(ProtectionPolicyError, match="max_spans"):
        protect_with_policy(source, policy)
