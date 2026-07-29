from scriber_polishing.semantic_generator import generate_example
from scriber_polishing.sst_renderer import render_sst
from scriber_polishing.target_renderer import render_html, render_markdown, render_plain_text


def test_seeded_semantic_generation_creates_a_document_and_mechanical_targets() -> None:
    example = generate_example(seed=17)

    assert example == generate_example(seed=17)
    assert example.plan.domain
    assert example.document.blocks
    assert example.target_sst == render_sst(example.document)
    assert example.target_plain_text == render_plain_text(example.document)
    assert example.target_markdown == render_markdown(example.document)
    assert example.target_html == render_html(example.document)


def test_generated_examples_cover_different_domains_and_spoken_formatting_commands() -> None:
    examples = [generate_example(seed=seed) for seed in range(1, 12)]

    assert len({example.plan.domain for example in examples}) >= 2
    assert any(example.formatting_commands for example in examples)
