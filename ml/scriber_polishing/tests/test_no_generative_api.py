"""Repository-local guard against forbidden generative model APIs."""

from __future__ import annotations

from pathlib import Path

from scriber_polishing.validators import find_forbidden_generative_api_references


def test_ml_runtime_sources_are_free_of_forbidden_generative_api_references() -> None:
    project_root = Path(__file__).parents[1]

    assert find_forbidden_generative_api_references(project_root) == []


def test_scanner_reports_forbidden_api_usage_but_ignores_policy_docs_and_tests(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("The token OPENAI_API_KEY is forbidden.", encoding="utf-8")
    (tmp_path / "tests" / "test_pattern.py").write_text("import openai", encoding="utf-8")
    offender = tmp_path / "src" / "teacher.py"
    offender.write_text("from openai import OpenAI", encoding="utf-8")

    findings = find_forbidden_generative_api_references(tmp_path)

    assert findings == [f"{offender.relative_to(tmp_path).as_posix()}: from openai import"]
