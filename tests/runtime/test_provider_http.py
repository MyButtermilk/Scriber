from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from src.runtime.provider_http import ProviderHttpTransport


async def _start_server() -> tuple[web.AppRunner, str]:
    app = web.Application()

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_post("/private/path", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[union-attr]
    port = int(sockets[0].getsockname()[1])
    return runner, f"http://127.0.0.1:{port}/private/path?ignored=yes"


@pytest.mark.asyncio
async def test_provider_http_reuses_connection_and_redacts_url() -> None:
    runner, url = await _start_server()
    transport = ProviderHttpTransport()
    try:
        session = await transport.session()
        trace_context = transport.trace_request_context(provider="azure_mai")
        for _ in range(2):
            async with session.post(
                url,
                data=b"fixture-audio-must-not-appear",
                trace_request_ctx=trace_context,
            ) as response:
                assert await response.json() == {"ok": True}

        diagnostics = transport.diagnostics()
        assert diagnostics["connectionCreatedCount"] == 1
        assert diagnostics["connectionReusedCount"] == 1
        assert diagnostics["retainedRequestCount"] == 2
        assert all(item["provider"] == "azure_mai" for item in diagnostics["items"])
        rendered = str(diagnostics)
        assert "private/path" not in rendered
        assert "ignored=yes" not in rendered
        assert "fixture-audio" not in rendered
    finally:
        await transport.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_provider_http_emits_request_chunk_markers() -> None:
    runner, url = await _start_server()
    transport = ProviderHttpTransport()
    markers: list[tuple[str, int | None]] = []

    def mark(name: str, *, timestamp_ns: int | None = None) -> None:
        markers.append((name, timestamp_ns))

    try:
        session = await transport.session()
        async with session.post(
            url,
            data=b"bounded-fixture",
            trace_request_ctx=transport.trace_request_context(
                provider="test",
                marker=mark,
            ),
        ) as response:
            await response.read()

        names = [name for name, _timestamp in markers]
        assert names[0] == "request_started"
        assert "first_request_chunk_sent" in names
        assert "last_request_chunk_sent" in names
        assert "response_headers_received" in names
        assert names[-1] == "first_response_chunk_received"
        assert all(timestamp is not None and timestamp > 0 for _name, timestamp in markers)
    finally:
        await transport.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_provider_http_session_view_attaches_context_implicitly() -> None:
    runner, url = await _start_server()
    transport = ProviderHttpTransport()
    markers: list[str] = []
    try:
        session = await transport.session_view(
            provider="openai_async",
            marker=lambda name, **_kwargs: markers.append(name),
        )
        async with session.post(url, data=b"fixture") as response:
            await response.read()
        assert markers == [
            "request_started",
            "first_request_chunk_sent",
            "last_request_chunk_sent",
            "response_headers_received",
            "first_response_chunk_received",
        ]
        item = transport.diagnostics()["items"][0]
        assert item["provider"] == "openai_async"
        assert item["outcome"] == "response_body"
        assert item["responseChunkCount"] >= 1
    finally:
        await transport.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_provider_http_close_is_idempotent() -> None:
    transport = ProviderHttpTransport()
    session = await transport.session()
    assert transport.is_open
    await transport.close()
    await transport.close()
    assert session.closed
    assert not transport.is_open


@pytest.mark.asyncio
async def test_provider_http_never_returns_session_from_another_loop() -> None:
    transport = ProviderHttpTransport()
    await transport.session()

    async def reject() -> None:
        with pytest.raises(RuntimeError, match="cannot cross asyncio event loops"):
            await transport.session()

    try:
        await asyncio.to_thread(asyncio.run, reject())
    finally:
        await transport.close()
