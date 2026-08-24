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
    assert await transport.close() is True
    assert await transport.close() is True
    assert session.closed
    assert not transport.is_open


@pytest.mark.asyncio
async def test_provider_http_close_seals_new_work_but_defers_for_active_route() -> None:
    transport = ProviderHttpTransport()

    async with transport.borrow() as route:
        primary = await route.session_view(provider="cerebras")
        session = primary._session

        assert await transport.close() is False
        diagnostics = transport.diagnostics()
        assert diagnostics["closeRequested"] is True
        assert diagnostics["activeBorrowCount"] == 1
        assert not session.closed

        with pytest.raises(RuntimeError, match="rejects new work"):
            await transport.session_view(provider="unowned")
        with pytest.raises(RuntimeError, match="rejects new work"):
            async with transport.borrow():
                pass

        fallback = await route.session_view(provider="openrouter")
        assert fallback._session is session
        assert not session.closed

    assert session.closed
    assert not transport.is_open
    assert transport.diagnostics()["activeBorrowCount"] == 0
    with pytest.raises(RuntimeError, match="rejects new work"):
        await transport.session()


@pytest.mark.asyncio
async def test_provider_http_last_parallel_borrow_owns_deferred_close() -> None:
    transport = ProviderHttpTransport()

    async with transport.borrow() as first:
        session = (await first.session_view(provider="first"))._session
        async with transport.borrow() as second:
            assert (await second.session_view(provider="second"))._session is session
            assert await transport.close() is False
        assert not session.closed
        assert transport.diagnostics()["activeBorrowCount"] == 1

    assert session.closed
    assert transport.diagnostics()["activeBorrowCount"] == 0


@pytest.mark.asyncio
async def test_provider_http_deferred_close_survives_repeated_cancellation() -> None:
    transport = ProviderHttpTransport()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    release_route = asyncio.Event()
    route_ready = asyncio.Event()

    class _SlowSession:
        closed = False

        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            self.closed = True

    slow_session = _SlowSession()
    transport._loop = asyncio.get_running_loop()
    transport._session = slow_session  # type: ignore[assignment]

    async def use_route() -> None:
        async with transport.borrow() as route:
            await route.session_view(provider="openrouter")
            route_ready.set()
            await release_route.wait()

    task = asyncio.create_task(use_route())
    await route_ready.wait()
    assert await transport.close() is False
    release_route.set()
    await close_started.wait()
    task.cancel()
    task.cancel()
    allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow_session.closed
    assert not transport.is_open


@pytest.mark.asyncio
async def test_provider_http_close_before_first_session_permanently_seals_transport() -> None:
    transport = ProviderHttpTransport()

    assert await transport.close() is True
    assert transport.diagnostics()["closeRequested"] is True
    with pytest.raises(RuntimeError, match="rejects new work"):
        await transport.session()
    with pytest.raises(RuntimeError, match="rejects new work"):
        async with transport.borrow():
            pass


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
