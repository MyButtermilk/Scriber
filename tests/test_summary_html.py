from src import export
from src.summary_html import (
    normalize_summary_html,
    normalize_summary_document_html,
    summary_html_to_markdown,
    summary_visible_text,
)


def test_summary_html_sanitizer_removes_executable_markup_and_attributes():
    raw = (
        '<section class="agent"><h1 onclick="steal()">Title</h1>'
        '<script>alert(1)</script><p style="color:red">Hello <strong>world</strong></p>'
        '<a href="javascript:alert(1)">bad</a>'
        '<a href="mailto:test@example.com">mail</a>'
        '<a href="#local">local</a>'
        '<a href="https://example.com" target="_blank">good</a></section>'
    )
    assert normalize_summary_html(raw) == (
        '<section><h2>Title</h2><p>Hello <strong>world</strong></p>'
        'badmaillocalgood</section>'
    )


def test_summary_html_links_are_reduced_to_noninteractive_text():
    assert normalize_summary_html(
        '<p>Source: <a href="https://example.com" title="remote">example.com</a></p>'
    ) == "<p>Source: example.com</p>"


def test_summary_html_forbidden_only_markup_is_not_escaped_into_visible_text():
    assert normalize_summary_html("<script>alert(1)</script>") == ""
    assert normalize_summary_html('<img src=x onerror="alert(1)">') == ""


def test_summary_html_code_fence_is_unwrapped_and_h1_is_normalized():
    assert normalize_summary_html("```html\n<h1>Overview</h1><p>Body</p>\n```") == (
        "<h2>Overview</h2><p>Body</p>"
    )


def test_summary_html_markdown_and_plain_text_become_semantic_fragments():
    assert normalize_summary_html("# Overview\n\nBody") == "<h2>Overview</h2><p>Body</p>"
    assert normalize_summary_html("A short summary") == "<p>A short summary</p>"


def test_summary_document_requires_title_and_standfirst_in_first_section():
    structured = "<section><h2>Overview</h2><p>Short standfirst.</p><ul><li>Fact</li></ul></section>"
    assert normalize_summary_document_html(structured) == structured
    assert normalize_summary_document_html("<section><h2>Overview</h2></section>") == ""
    assert normalize_summary_document_html("<p>Only a paragraph</p>") == ""
    assert normalize_summary_document_html("<script>nothing safe</script>") == ""


def test_summary_document_repairs_markdown_drift_with_real_lead_structure():
    markdown = "# Overview\n\nShort standfirst.\n\n- First fact\n- Second fact"
    assert normalize_summary_document_html(markdown) == (
        "<section><h2>Overview</h2><p>Short standfirst.</p>"
        "<ul><li>First fact</li><li>Second fact</li></ul></section>"
    )


def test_summary_document_drops_reasoning_leak_and_plain_preface():
    structured = "<section><h2>Overview</h2><p>Short standfirst.</p></section>"

    assert normalize_summary_document_html(
        f"<think>We need to plan the answer first.</think>{structured}"
    ) == structured
    assert normalize_summary_document_html(f"Here is the requested summary:\n{structured}") == structured


def test_summary_document_extracts_one_html_fence_with_surrounding_chatter():
    structured = "<section><h2>Overview</h2><p>Short standfirst.</p></section>"

    assert normalize_summary_document_html(
        f"Here is the result:\n```html\n{structured}\n```\nHope this helps."
    ) == structured


def test_summary_document_accepts_balanced_sibling_sections():
    structured = (
        "<section><h2>Overview</h2><p>Short standfirst.</p></section>"
        "<section><h2>Details</h2><p>More context.</p></section>"
    )

    assert normalize_summary_document_html(structured) == structured


def test_summary_document_rejects_unbalanced_or_misnested_markup():
    assert normalize_summary_document_html(
        "<section><h2>Overview</h2><p>Short standfirst.</p><ul><li>Truncated"
    ) == ""
    assert normalize_summary_document_html(
        "<section><h2>Overview</h2><p>Short standfirst.</section></p>"
    ) == ""


def test_summary_document_rejects_trailing_chatter_or_top_level_markup():
    structured = "<section><h2>Overview</h2><p>Short standfirst.</p></section>"

    assert normalize_summary_document_html(f"{structured} Hope this helps.") == ""
    assert normalize_summary_document_html(f"{structured}<p>Hope this helps.</p>") == ""


def test_summary_document_rejects_multiple_fenced_blocks():
    first = "<section><h2>Overview</h2><p>Short standfirst.</p></section>"
    second = "<section><h2>Alternative</h2><p>Another version.</p></section>"

    assert normalize_summary_document_html(
        f"```html\n{first}\n```\n```html\n{second}\n```"
    ) == ""


def test_summary_visible_text_and_export_projection_hide_markup():
    html = "<section><h2>Overview</h2><p><strong>Visible</strong> detail</p></section>"
    assert summary_visible_text(html, "html") == "Overview\nVisible detail"
    projected = summary_html_to_markdown(html)
    assert projected == "## Overview\n\n**Visible** detail"
    assert export._summary_for_export(html, "html") == projected
    assert export._summary_for_export("## Legacy", "markdown") == "## Legacy"
