from __future__ import annotations

from scriber_polishing.tokenize import build_training_messages, tokenize_example


class FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], bool, bool]] = []

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        copied = [dict(message) for message in messages]
        self.calls.append((copied, tokenize, add_generation_prompt))
        text = "user:" + copied[0]["content"] + "|model:"
        if len(copied) == 2:
            text += copied[1]["content"]
        return [ord(character) for character in text] if tokenize else text


def test_tokenization_uses_native_gemma_template_without_a_system_turn_and_masks_prompt() -> None:
    tokenizer = FakeTokenizer()
    messages = build_training_messages("bitte pruefen", "Bitte prüfen.")

    encoded = tokenize_example(tokenizer, transcript="bitte pruefen", completion="Bitte prüfen.")

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert all(message["role"] != "system" for message in messages)
    assert tokenizer.calls[0][2] is True
    assert tokenizer.calls[1][2] is False
    prompt_length = len(tokenizer.calls[0][0][0]["content"]) + len("user:|model:")
    assert encoded["labels"][:prompt_length] == [-100] * prompt_length
    assert encoded["labels"][prompt_length:] == encoded["input_ids"][prompt_length:]
