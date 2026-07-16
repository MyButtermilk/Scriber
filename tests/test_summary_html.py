from src import export
from src.summary_html import (
    normalize_summary_html,
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
        '<a>bad</a><a>mail</a><a>local</a>'
        '<a href="https://example.com">good</a></section>'
    )


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


def test_summary_visible_text_and_export_projection_hide_markup():
    html = "<section><h2>Overview</h2><p><strong>Visible</strong> detail</p></section>"
    assert summary_visible_text(html, "html") == "Overview\nVisible detail"
    projected = summary_html_to_markdown(html)
    assert projected == "## Overview\n\n**Visible** detail"
    assert export._summary_for_export(html, "html") == projected
    assert export._summary_for_export("## Legacy", "markdown") == "## Legacy"
