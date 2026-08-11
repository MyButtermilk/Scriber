from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src import summarization
from src.celeris import (
    CELERIS_CHAT_COMPLETIONS_URL,
    CELERIS_CONTEXT_TOKENS,
    CELERIS_TOKEN_QUANTUM,
    celeris_chat_completion,
    celeris_prompt_fits,
    celeris_request_max_tokens,
    estimate_celeris_prompt_tokens,
)
from src.meeting_analysis import analyze_meeting, partition_analysis_segments


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._text = json.dumps(payload)
        self.content_length = len(self._text.encode("utf-8"))
        self.charset = "utf-8"
        self.content = None

    async def text(self) -> str:
        return self._text


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeRequestContext:
        self.calls.append({"url": url, **kwargs})
        return _FakeRequestContext(self.responses.pop(0))


def test_celeris_token_budget_is_quantized_and_within_context() -> None:
    prompt = "Zusammenfassung " * 150
    result = celeris_request_max_tokens(prompt, 1_001)
    assert result % CELERIS_TOKEN_QUANTUM == 0
    assert result >= CELERIS_TOKEN_QUANTUM
    assert estimate_celeris_prompt_tokens(prompt) + result <= CELERIS_CONTEXT_TOKENS


def test_celeris_rejects_oversized_unsplit_prompt() -> None:
    with pytest.raises(ValueError, match="split"):
        celeris_request_max_tokens("x" * 9_000, 256)


def test_celeris_fit_check_reserves_the_requested_output_budget() -> None:
    prompt = "x" * 6_900
    assert celeris_request_max_tokens(prompt, 2_048) < 2_048
    assert celeris_prompt_fits(prompt, 2_048) is False


@pytest.mark.asyncio
async def test_celeris_transport_uses_documented_request_contract() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                payload={"choices": [{"message": {"role": "assistant", "content": "Celeris ready"}}]},
                headers={"Server-Timing": "model;dur=75"},
            )
        ]
    )
    result = await celeris_chat_completion(
        "Reply with exactly: Celeris ready",
        max_output_tokens=257,
        timeout_seconds=30,
        api_key="test-key",
        session=session,
    )
    assert result == "Celeris ready"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == CELERIS_CHAT_COMPLETIONS_URL
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["json"]["model"] == "celeris-1"
    assert call["json"]["max_tokens"] == 512
    assert call["json"]["temperature"] == 0
    assert call["json"]["seed"] == 7


@pytest.mark.asyncio
async def test_celeris_summary_request_can_omit_scriber_output_limit() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                payload={"choices": [{"finish_reason": "stop", "message": {"content": "ready"}}]},
            )
        ]
    )

    result = await celeris_chat_completion(
        "Summarize completely.",
        max_output_tokens=None,
        timeout_seconds=30,
        api_key="test-key",
        session=session,
    )

    assert result == "ready"
    assert "max_tokens" not in session.calls[0]["json"]


@pytest.mark.asyncio
async def test_celeris_transport_retries_rate_limits_without_logging_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=429,
                payload={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "private prompt must not escape",
                    }
                },
                headers={"Retry-After": "0"},
            ),
            _FakeResponse(
                status=200,
                payload={"choices": [{"message": {"content": "ready"}}]},
            ),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("src.celeris.asyncio.sleep", fake_sleep)
    result = await celeris_chat_completion(
        "private prompt must not escape",
        max_output_tokens=256,
        timeout_seconds=30,
        api_key="private-test-key",
        session=session,
    )

    assert result == "ready"
    assert len(session.calls) == 2
    assert sleeps == [0.05]


@pytest.mark.asyncio
async def test_celeris_transport_error_is_bounded_and_content_free() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=400,
                payload={
                    "error": {
                        "code": "invalid_request_error",
                        "message": "private prompt and private-test-key",
                    }
                },
            )
        ]
    )

    with pytest.raises(RuntimeError) as caught:
        await celeris_chat_completion(
            "private prompt",
            max_output_tokens=256,
            timeout_seconds=30,
            api_key="private-test-key",
            session=session,
        )

    message = str(caught.value)
    assert "invalid_request_error" in message
    assert "private prompt" not in message
    assert "private-test-key" not in message


@pytest.mark.asyncio
async def test_generate_text_routes_directly_to_celeris(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def fake(
        prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        api_key: str | None = None,
        session: Any | None = None,
    ) -> str:
        del timeout_seconds, api_key, session
        calls.append((prompt, max_output_tokens))
        return "ok"

    monkeypatch.setattr(summarization, "celeris_chat_completion", fake)
    result = await summarization.generate_text_with_model(
        "Return one word.",
        "celeris-1",
        max_output_tokens=777,
    )
    assert result == "ok"
    assert calls == [("Return one word.", 777)]


@pytest.mark.asyncio
async def test_long_transcript_uses_hierarchical_celeris_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    async def fake(
        prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        api_key: str | None = None,
        session: Any | None = None,
    ) -> str:
        del max_output_tokens, timeout_seconds, api_key, session
        prompts.append(prompt)
        if "Output contract (mandatory" in prompt:
            return "<section><h2>Ergebnis</h2><p>Faktenbasierte Zusammenfassung.</p></section>"
        return "Kompakte Teilanalyse mit Fakten, Entscheidungen und offenen Punkten."

    monkeypatch.setattr(summarization, "celeris_chat_completion", fake)
    monkeypatch.setattr(summarization.Config, "SUMMARIZATION_PROMPT", "Fasse zusammen.")
    text = ("Sprecher 1: Das Projekt wird im September umgesetzt. " * 900).strip()
    result = await summarization.summarize_text(
        text,
        "celeris-1",
        fallback_language="de",
    )
    assert result.startswith("<section>")
    assert len(prompts) > 2
    assert any("UNTRUSTED_TRANSCRIPT_PART" in prompt for prompt in prompts)
    assert any("UNTRUSTED_PARTIAL_SUMMARIES_JSON" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_meeting_analysis_uses_bounded_celeris_map_reduce() -> None:
    calls: list[tuple[str, int]] = []
    segments = [
        {
            "id": f"segment-{index}",
            "startMs": index * 1_000,
            "endMs": (index + 1) * 1_000,
            "speakerLabel": "Sprecher 1",
            "text": "Beschluss und Aufgabe " + ("Inhalt " * 180),
        }
        for index in range(18)
    ]

    async def generate(
        prompt: str,
        model: str | None,
        *,
        max_output_tokens: int,
    ) -> str:
        assert model == "celeris-1"
        assert celeris_prompt_fits(prompt, max_output_tokens)
        calls.append((prompt, max_output_tokens))
        ids = _segment_ids_from_prompt(prompt)
        first = ids[0]
        last = ids[-1]
        return json.dumps(
            {
                "schemaVersion": "1",
                "outputLanguage": "de",
                "title": "Besprechung",
                "executiveSummary": "Kurze Zusammenfassung.",
                "topics": [
                    {
                        "title": "Thema",
                        "summary": "Zusammenfassung",
                        "segmentIds": [first],
                    }
                ],
                "decisions": [],
                "actionItems": [],
                "openQuestions": [],
                "risks": [],
                "chapters": [
                    {
                        "title": "Kapitel",
                        "startMs": 0,
                        "endMs": 1_000,
                        "segmentIds": [first, last],
                    }
                ],
                "keywords": ["Projekt"],
            },
            ensure_ascii=False,
        )

    result = await analyze_meeting(
        "漢" * 300,
        segments,
        [{"body": "重要な Besprechungsnotiz " * 1_000}],
        model="celeris-1",
        generate=generate,
        fallback_language="de",
    )
    assert result["outputLanguage"] == "de"
    assert len(calls) > 1
    assert all(tokens <= 2_048 for _, tokens in calls)
    assert len(partition_analysis_segments(segments, max_chars=7_000)) > 1


def _segment_ids_from_prompt(prompt: str) -> list[str]:
    ids: list[str] = []
    for value in re.findall(r'"segmentId"\s*:\s*"([^"]+)"', prompt):
        if value not in ids:
            ids.append(value)
    for values in re.findall(r'"segmentIds"\s*:\s*\[([^\]]+)\]', prompt):
        for value in re.findall(r'"([^"]+)"', values):
            if value not in ids:
                ids.append(value)
    return ids or ["segment-0"]


def test_frontend_and_settings_contract_expose_celeris_without_a_key() -> None:
    root = Path(__file__).resolve().parents[1]
    settings = (root / "Frontend/client/src/pages/Settings.tsx").read_text(encoding="utf-8")
    api_types = (root / "Frontend/client/src/lib/api-types.ts").read_text(encoding="utf-8")
    web_api = (root / "src/web_api.py").read_text(encoding="utf-8")
    config = (root / "src/config.py").read_text(encoding="utf-8")
    assert 'value: "celeris-1"' in settings
    assert 'provider="Celeris"' in settings
    assert "celeris?: string" in api_types
    assert '"celeris": getattr(Config, "CELERIS_API_KEY"' in web_api
    assert '"celeris": "CELERIS_API_KEY"' in config
