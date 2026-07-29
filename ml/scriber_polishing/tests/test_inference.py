from __future__ import annotations

from scriber_polishing.inference import polish_transcript


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
