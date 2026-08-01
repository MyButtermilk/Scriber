from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scriber_polishing.inference import PredictionResult, generate_prediction, main
from scriber_polishing.protected_spans import restore_spans
from scriber_polishing.safe_inference import (
    conservative_chunks,
    protect_product_spans,
    run_product_inference,
    warmup_product_inference,
)


def _prediction(sst: str, *, restoration_error: str | None = None) -> PredictionResult:
    return PredictionResult(
        prediction_sst=sst,
        valid_sst=restoration_error is None,
        restoration_error=restoration_error,
        generation_seconds=0.1,
        input_tokens=8,
        output_tokens=9,
    )


def test_product_inference_accepts_preserved_amount_and_rejects_changed_amount() -> None:
    source = "Die Rechnung beträgt 1.234,56 € und ist heute fällig."
    accepted = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[P]Die Rechnung über 1.234,56 € ist heute fällig.[/P]\n[/DOC]"
        ),
    )
    changed = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[P]Die Rechnung über 1.234,65 € ist heute fällig.[/P]\n[/DOC]"
        ),
    )

    assert accepted.output == "Die Rechnung über 1.234,56 € ist heute fällig."
    assert accepted.diagnostics.status == "accepted"
    assert changed.output == source
    assert changed.diagnostics.status == "original_fallback"
    assert [issue.code for issue in changed.diagnostics.issues] == [
        "changed_amount",
        "changed_number",
    ]
    assert changed.diagnostics.active_learning_priority == "critical"


def test_product_inference_rejects_damaged_placeholder_before_any_repair() -> None:
    source = "Bitte § 4 Abs. 2 BGB und support@example.de prüfen."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[P]Bitte ⟦SCRIBER_PROTECTED_0000 und support@example.de prüfen.[/P]\n[/DOC]",
            restoration_error="each protected placeholder must occur exactly once",
        ),
    )

    assert result.output == source
    assert result.diagnostics.status == "original_fallback"
    assert [issue.code for issue in result.diagnostics.issues] == ["damaged_placeholder"]


def test_product_inference_repairs_only_missing_sst_boundary_newlines() -> None:
    result = run_product_inference(
        "Bitte 12 prüfen.",
        predictor=lambda _text: _prediction("[DOC][P]Bitte 12 prüfen.[/P][/DOC]"),
        output_format="sst",
    )

    assert result.output == "[DOC]\n[P]Bitte 12 prüfen.[/P]\n[/DOC]"
    assert result.actual_format == "sst"
    assert result.diagnostics.status == "repaired_sst"
    assert [issue.code for issue in result.diagnostics.issues] == ["sst_syntax_repaired"]


def test_product_inference_uses_plain_text_only_when_structure_is_ambiguous() -> None:
    source = "Erstens prüfen. Zweitens freigeben."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[OL]\n[LI1]Erstens prüfen.[/LI1]\n"
            "[LI1]Zweitens freigeben.[/LI1]\n[/DOC]"
        ),
        output_format="html",
    )

    assert result.output == "Erstens prüfen.\nZweitens freigeben."
    assert result.actual_format == "plain"
    assert result.diagnostics.status == "plain_text_fallback"
    assert [issue.code for issue in result.diagnostics.issues] == [
        "invalid_sst",
        "plain_text_cleanup",
    ]


def test_product_inference_never_strips_or_emits_unknown_sst_tags() -> None:
    source = "Bitte den Status prüfen."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[TABLE]Bitte den Status prüfen.[/TABLE]\n[/DOC]"
        ),
    )

    assert result.output == source
    assert [issue.code for issue in result.diagnostics.issues] == ["invalid_sst"]

    lower_case = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[table]Bitte den Status prüfen.[/table]\n[/DOC]"
        ),
    )
    assert lower_case.output == source
    assert [issue.code for issue in lower_case.diagnostics.issues] == [
        "invalid_sst"
    ]


def test_product_inference_rejects_a_heading_without_explicit_source_evidence() -> None:
    source = "Wir besprechen morgen das Projekt Alpha."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[H1]Projekt Alpha[/H1]\n"
            "[P]Wir besprechen morgen das Projekt Alpha.[/P]\n[/DOC]"
        ),
    )

    assert result.output == source
    assert [issue.code for issue in result.diagnostics.issues] == ["invented_heading"]


def test_product_inference_requires_heading_text_to_come_from_the_source() -> None:
    source = "Neue Überschrift Projekt Alpha. Danach folgt der Status."
    invented = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[H1]Projekt Beta[/H1]\n"
            "[P]Danach folgt der Status.[/P]\n[/DOC]"
        ),
    )
    supported = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[H1]Projekt Alpha[/H1]\n"
            "[P]Danach folgt der Status.[/P]\n[/DOC]"
        ),
    )

    assert invented.output == source
    assert [issue.code for issue in invented.diagnostics.issues] == [
        "invented_heading"
    ]
    assert supported.diagnostics.status == "accepted"


def test_product_inference_rejects_body_content_after_the_closing_block() -> None:
    source = "Mit freundlichen Grüßen. Der Bericht folgt morgen."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[CLOSING]Mit freundlichen Grüßen.[/CLOSING]\n"
            "[P]Der Bericht folgt morgen.[/P]\n[/DOC]"
        ),
    )

    assert result.output == source
    assert [issue.code for issue in result.diagnostics.issues] == ["invalid_structure"]


@pytest.mark.parametrize(
    ("source", "candidate", "expected_code"),
    [
        (
            "Für die Berechnung gilt DIN EN 12831.",
            "Für die Berechnung gilt DIN EN 12832.",
            "changed_norm",
        ),
        (
            "Der Antrag muss heute eingereicht werden.",
            "Der Antrag kann heute eingereicht werden.",
            "changed_modality",
        ),
        (
            "Die Freigabe ist nicht erforderlich.",
            "Die Freigabe ist erforderlich.",
            "changed_polarity",
        ),
        (
            "Die Referenz AB-1234 bleibt bestehen.",
            "Die Referenz AB-1235 bleibt bestehen.",
            "changed_critical_span",
        ),
    ],
)
def test_product_inference_rejects_changed_critical_content(
    source: str,
    candidate: str,
    expected_code: str,
) -> None:
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(f"[DOC]\n[P]{candidate}[/P]\n[/DOC]"),
    )

    assert result.output == source
    assert expected_code in {issue.code for issue in result.diagnostics.issues}


def test_product_inference_rejects_catastrophic_content_loss() -> None:
    source = (
        "Der Bericht beschreibt die Ergebnisse der Prüfung und erläutert "
        "anschließend die vereinbarten nächsten Schritte."
    )
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction("[DOC]\n[P]Der Bericht liegt vor.[/P]\n[/DOC]"),
    )

    assert result.output == source
    assert [issue.code for issue in result.diagnostics.issues] == ["content_loss"]


def test_product_inference_rejects_reordered_critical_spans() -> None:
    source = "Zuerst AB-1234 prüfen, danach CD-5678 freigeben."
    result = run_product_inference(
        source,
        predictor=lambda _text: _prediction(
            "[DOC]\n[P]Zuerst CD-5678 freigeben, danach AB-1234 prüfen.[/P]\n[/DOC]"
        ),
    )

    assert result.output == source
    assert [issue.code for issue in result.diagnostics.issues] == [
        "reordered_critical_spans"
    ]


def test_product_span_protection_covers_norm_identifier_and_plain_number() -> None:
    source = "DIN EN 12831, Vorgang AB-1234, insgesamt 7 Teilnehmer."

    protected = protect_product_spans(source)

    assert protected.text == (
        "⟦SCRIBER_PROTECTED_0000⟧, Vorgang "
        "⟦SCRIBER_PROTECTED_0001⟧, insgesamt "
        "⟦SCRIBER_PROTECTED_0002⟧ Teilnehmer."
    )
    assert restore_spans(protected, protected.text) == source


def test_long_text_chunking_preserves_text_sentences_and_protected_spans() -> None:
    source = (
        "Gemäß § 4 Abs. 2 BGB gilt die Regel. "
        "Danach folgt ein vollständiger Satz.\n\n"
        "Neuer Absatz."
    )

    chunks = conservative_chunks(source, max_characters=20)

    assert "".join(chunk.text for chunk in chunks) == source
    assert [chunk.text.strip() for chunk in chunks] == [
        "Gemäß § 4 Abs. 2 BGB gilt die Regel.",
        "Danach folgt ein vollständiger Satz.",
        "Neuer Absatz.",
    ]
    assert all(not chunk.text.rstrip().endswith("Abs.") for chunk in chunks)
    assert len(chunks[0].text) > 20


def test_one_unsafe_long_text_chunk_returns_the_complete_original_transcript() -> None:
    source = "Der erste Satz bleibt erhalten. Der zweite Satz enthält 9 Punkte."

    def predict(chunk: str) -> PredictionResult:
        text = chunk.strip()
        if text.startswith("Der zweite"):
            text = text.replace("9", "8")
        return _prediction(f"[DOC]\n[P]{text}[/P]\n[/DOC]")

    result = run_product_inference(
        source,
        predictor=predict,
        max_chunk_characters=30,
    )

    assert result.output == source
    assert result.diagnostics.status == "original_fallback"
    assert "changed_number" in {
        issue.code for issue in result.diagnostics.issues
    }


def test_safe_long_text_chunks_are_combined_as_one_document() -> None:
    source = "Der erste Satz bleibt erhalten. Der zweite Satz bleibt ebenfalls erhalten."

    result = run_product_inference(
        source,
        predictor=lambda chunk: _prediction(
            f"[DOC]\n[P]{chunk.strip()}[/P]\n[/DOC]"
        ),
        max_chunk_characters=32,
    )

    assert result.output == (
        "Der erste Satz bleibt erhalten.\n\n"
        "Der zweite Satz bleibt ebenfalls erhalten."
    )
    assert result.diagnostics.status == "accepted"
    assert result.diagnostics.chunk_count == 2


def test_generation_failure_is_structured_and_returns_original() -> None:
    source = "Der Originaltext bleibt verfügbar."

    def fail(_text: str) -> PredictionResult:
        raise RuntimeError("secret model path must not leak")

    result = run_product_inference(source, predictor=fail)

    assert result.output == source
    assert result.diagnostics.to_dict()["issues"] == [
        {
            "stage": "generation",
            "code": "generation_error",
            "severity": "high",
        }
    ]
    assert "secret" not in str(result.diagnostics.to_dict())


def test_blank_transcript_never_invokes_the_model_or_accepts_generated_text() -> None:
    calls = 0

    def predict(_text: str) -> PredictionResult:
        nonlocal calls
        calls += 1
        return _prediction("[DOC]\n[P]Erfundener Inhalt.[/P]\n[/DOC]")

    result = run_product_inference("   ", predictor=predict)

    assert calls == 0
    assert result.output == "   "
    assert [issue.code for issue in result.diagnostics.issues] == [
        "empty_transcript"
    ]


def test_warmup_runs_one_fixed_private_free_generation() -> None:
    observed: list[str] = []

    result = warmup_product_inference(
        lambda text: (
            observed.append(text)
            or _prediction("[DOC]\n[P]Lokaler Warmup.[/P]\n[/DOC]")
        )
    )

    assert observed == ["Lokaler Warmup."]
    assert result.to_dict() == {
        "schema_version": 1,
        "success": True,
        "valid_sst": True,
        "error_code": None,
    }


def test_product_generation_uses_broader_protection_without_changing_raw_default() -> None:
    prompts: list[str] = []

    class Tokenizer:
        def apply_chat_template(
            self,
            messages,
            *,
            tokenize,
            add_generation_prompt,
            **kwargs,
        ):
            del tokenize, add_generation_prompt
            if kwargs:
                raise TypeError
            prompts.append(messages[0]["content"])
            return [1, 2, 3]

        def decode(self, tokens, *, skip_special_tokens):
            del skip_special_tokens
            return tokens[0]

    class Model:
        def __init__(self, completion: str) -> None:
            self.completion = completion

        def generate(self, input_ids, **kwargs):
            del kwargs
            return [*input_ids, self.completion]

    raw = generate_prediction(
        "Es sind 7 Teilnehmer.",
        tokenizer=Tokenizer(),
        model=Model("[DOC]\n[P]Es sind 7 Teilnehmer.[/P]\n[/DOC]"),
    )
    product = generate_prediction(
        "Es sind 7 Teilnehmer.",
        tokenizer=Tokenizer(),
        model=Model(
            "[DOC]\n[P]Es sind ⟦SCRIBER_PROTECTED_0000⟧ Teilnehmer.[/P]\n[/DOC]"
        ),
        span_protector=protect_product_spans,
    )

    assert "Es sind 7 Teilnehmer." in prompts[0]
    assert "Es sind ⟦SCRIBER_PROTECTED_0000⟧ Teilnehmer." in prompts[1]
    assert raw.prediction_sst == product.prediction_sst


def test_local_cli_keeps_structured_diagnostics_separate_from_safe_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    diagnostics = tmp_path / "diagnostics.json"
    monkeypatch.setattr(
        "scriber_polishing.inference.load_transformers_runtime",
        lambda **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        "scriber_polishing.inference.generate_prediction",
        lambda *_args, **_kwargs: _prediction(
            "[DOC]\n[P]Es sind 8 Teilnehmer.[/P]\n[/DOC]"
        ),
    )

    assert (
        main(
            [
                "--model",
                "local",
                "--text",
                "Es sind 7 Teilnehmer.",
                "--diagnostics-output",
                str(diagnostics),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "Es sind 7 Teilnehmer.\n"
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "product_inference_diagnostics_schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(payload)
    assert payload["status"] == "original_fallback"
    assert payload["active_learning"]["priority"] == "critical"
    assert "Es sind 7 Teilnehmer." not in diagnostics.read_text(encoding="utf-8")
