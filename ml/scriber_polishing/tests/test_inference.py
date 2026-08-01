from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.inference import (
    PredictionResult,
    load_transformers_runtime,
    main,
    polish_transcript,
    write_batch_predictions,
)
from scriber_polishing.sst_parser import parse_sst


class FakeTokenizer:
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert add_generation_prompt is True
        assert [message["role"] for message in messages] == ["user"]
        return [1, 2, 3] if tokenize else "prompt"

    def decode(self, tokens, *, skip_special_tokens):
        assert skip_special_tokens is True
        return tokens[0]


class FakeModel:
    def __init__(self, output: str) -> None:
        self.output = output
        self.kwargs = None

    def generate(self, input_ids, **kwargs):
        self.kwargs = kwargs
        return [*input_ids, self.output]


def test_quantized_inference_reloads_canonical_saved_artifact_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "int4_nf4"
    artifact.mkdir()
    (artifact / "config.json").write_text(
        json.dumps(
            {
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_4bit": True,
                    "bnb_4bit_quant_type": "nf4",
                    "bnb_4bit_compute_dtype": "bfloat16",
                    "bnb_4bit_use_double_quant": True,
                }
            }
        ),
        encoding="utf-8",
    )
    observed_model_kwargs: dict[str, object] = {}

    class Tokenizer:
        pad_token_id = 1
        eos_token = "<eos>"

    class Model:
        is_loaded_in_4bit = True
        generation_config = SimpleNamespace()

        def eval(self) -> None:
            return None

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            del args, kwargs
            return Tokenizer()

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            del args
            observed_model_kwargs.update(kwargs)
            return Model()

    class ForbiddenBitsAndBytesConfig:
        def __init__(self, **kwargs):
            raise AssertionError(f"quantization override was supplied: {kwargs}")

    torch_module = ModuleType("torch")
    torch_module.bfloat16 = object()
    torch_module.float32 = object()
    torch_module.cuda = SimpleNamespace(is_available=lambda: True)
    transformers_module = ModuleType("transformers")
    transformers_module.AutoModelForCausalLM = AutoModelForCausalLM
    transformers_module.AutoTokenizer = AutoTokenizer
    transformers_module.BitsAndBytesConfig = ForbiddenBitsAndBytesConfig
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "transformers", transformers_module)

    _, loaded = load_transformers_runtime(
        model=str(artifact),
        device="cuda",
        quantization="int4_nf4",
        local_files_only=True,
    )

    assert loaded.is_loaded_in_4bit is True
    assert "quantization_config" not in observed_model_kwargs
    assert observed_model_kwargs["device_map"] == "auto"


def test_inference_restores_protected_spans_after_valid_sst_and_is_deterministic() -> None:
    model = FakeModel("[DOC]\n[P]Bitte ⟦SCRIBER_PROTECTED_0000⟧ prüfen.[/P]\n[/DOC]")

    result = polish_transcript("Bitte 1.234,56 € pruefen", tokenizer=FakeTokenizer(), model=model)

    assert result == "Bitte 1.234,56 € prüfen."
    assert model.kwargs == {"do_sample": False, "num_beams": 1, "max_new_tokens": 512}


def test_inference_fails_closed_to_the_original_transcript_for_invalid_sst() -> None:
    model = FakeModel("nicht erlaubter Freitext")

    assert (
        polish_transcript("Bitte 1.234,56 € pruefen", tokenizer=FakeTokenizer(), model=model)
        == "Bitte 1.234,56 € pruefen"
    )


def test_inference_fails_closed_when_a_deterministic_validator_rejects_sst() -> None:
    model = FakeModel("[DOC]\n[P]Bitte ⟦SCRIBER_PROTECTED_0000⟧ prüfen.[/P]\n[/DOC]")

    result = polish_transcript(
        "Bitte 1.234,56 € pruefen", tokenizer=FakeTokenizer(), model=model, validator=lambda _: False
    )

    assert result == "Bitte 1.234,56 € pruefen"


def test_local_cli_prints_plain_text_and_supports_sst_output(monkeypatch, capsys) -> None:
    result = PredictionResult(
        prediction_sst="[DOC]\n[P]Bereinigter Text.[/P]\n[/DOC]",
        valid_sst=True,
        restoration_error=None,
        generation_seconds=0.25,
        input_tokens=8,
        output_tokens=9,
    )
    monkeypatch.setattr(
        "scriber_polishing.inference.load_transformers_runtime",
        lambda **_: (FakeTokenizer(), FakeModel("unused")),
    )
    monkeypatch.setattr(
        "scriber_polishing.inference.generate_prediction",
        lambda *_args, **_kwargs: result,
    )

    assert main(["--model", "local-model", "--text", "roh"]) == 0
    assert capsys.readouterr().out == "Bereinigter Text.\n"
    assert main(["--model", "local-model", "--text", "roh", "--output-format", "sst"]) == 0
    assert capsys.readouterr().out == result.prediction_sst + "\n"


def test_batch_prediction_writer_is_complete_schema_valid_and_immutable(
    tmp_path: Path,
) -> None:
    target_sst = "[DOC]\n[P]Eins.[/P]\n[/DOC]"
    references = [
        {
            "schema_version": 1,
            "case_id": "eval_case_001",
            "split": "test",
            "source_text": "roh eins",
            "target_sst": target_sst,
            "target_plain_text": "Eins.",
            "target_ast": document_to_dict(parse_sst(target_sst)),
            "semantic_plan": {
                "facts": [
                    {
                        "fact_id": "f1",
                        "order": 1,
                        "text": "Der Inhalt lautet Eins.",
                        "polarity": "positive",
                        "modality": "fact",
                        "condition": None,
                        "protected_values": [],
                    },
                    {
                        "fact_id": "f2",
                        "order": 2,
                        "text": "Der Test bleibt deterministisch.",
                        "polarity": "positive",
                        "modality": "fact",
                        "condition": None,
                        "protected_values": [],
                    },
                ],
                "entities": [],
                "structure": {
                    "has_subject": False,
                    "has_salutation": False,
                    "paragraph_topics": ["Testinhalt"],
                    "heading_levels": [],
                    "list_kind": "none",
                    "list_items": [],
                    "has_closing": False,
                    "signature_lines": [],
                },
            },
            "protected_spans": [],
            "slice_tags": ["test_case"],
            "is_identity": False,
        }
    ]
    destination = tmp_path / "predictions.jsonl"
    progress: list[tuple[int, int]] = []

    manifest = write_batch_predictions(
        references,
        destination,
        predictor=lambda _source: PredictionResult(
            prediction_sst="[DOC]\n[P]Eins.[/P]\n[/DOC]",
            valid_sst=True,
            restoration_error=None,
            generation_seconds=0.1,
            input_tokens=3,
            output_tokens=5,
        ),
        candidate="candidate_a",
        progress=lambda completed, total: progress.append((completed, total)),
    )

    rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "schema_version": 1,
            "case_id": "eval_case_001",
            "prediction_sst": "[DOC]\n[P]Eins.[/P]\n[/DOC]",
        }
    ]
    assert manifest["candidate"] == "candidate_a"
    assert manifest["record_count"] == 1
    assert manifest["valid_sst_count"] == 1
    assert progress == [(1, 1)]

    try:
        write_batch_predictions(
            references,
            destination,
            predictor=lambda _source: PredictionResult(
                prediction_sst="changed",
                valid_sst=False,
                restoration_error=None,
                generation_seconds=0.1,
                input_tokens=3,
                output_tokens=1,
            ),
            candidate="candidate_a",
        )
    except ValueError as error:
        assert "overwrite" in str(error)
    else:
        raise AssertionError("changed prediction artifact was overwritten")
