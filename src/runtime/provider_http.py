"""Application-owned HTTP transport for provider requests.

The transport is deliberately bound to the asyncio loop that first uses it.
Provider sessions therefore retain DNS and TCP/TLS state between sequential
dictations without ever crossing an event-loop boundary.  Trace diagnostics
are bounded and contain only opaque flow/origin identifiers plus timings; URL
paths, query strings, headers, bodies, filenames, and transcript text never
cross this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import socket
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from uuid import uuid4

import aiohttp


_TRACE_LIMIT = 128
_PROVIDER_LIMIT = 48
_MARKER_LIMIT = 64


class ProviderRequestAcceptanceUnknown(RuntimeError):
    """A billable request may have reached the provider.

    Callers must not automatically replay the audio or try a second format for
    this failure class.  The original exception remains available through
    normal exception chaining, while the public message stays bounded and does
    not copy provider response bodies.
    """

    provider_request_may_be_committed = True

    def __init__(self, provider: str) -> None:
        self.provider = _bounded_label(provider, limit=_PROVIDER_LIMIT)
        super().__init__(
            f"{self.provider} request outcome is unknown; automatic replay is disabled"
        )


class _ProviderHttpSessionView:
    """Delegate to one session while attaching privacy-safe trace context."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        provider: str,
        marker: Callable[..., None] | None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._marker = marker

    def _kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        prepared = dict(kwargs)
        prepared.setdefault(
            "trace_request_ctx",
            ProviderHttpTransport.trace_request_context(
                provider=self._provider,
                marker=self._marker,
            ),
        )
        return prepared

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        return self._session.request(method, url, **self._kwargs(kwargs))

    def get(self, url: Any, **kwargs: Any) -> Any:
        return self._session.get(url, **self._kwargs(kwargs))

    def post(self, url: Any, **kwargs: Any) -> Any:
        return self._session.post(url, **self._kwargs(kwargs))

    def put(self, url: Any, **kwargs: Any) -> Any:
        return self._session.put(url, **self._kwargs(kwargs))

    def patch(self, url: Any, **kwargs: Any) -> Any:
        return self._session.patch(url, **self._kwargs(kwargs))

    def delete(self, url: Any, **kwargs: Any) -> Any:
        return self._session.delete(url, **self._kwargs(kwargs))

    def head(self, url: Any, **kwargs: Any) -> Any:
        return self._session.head(url, **self._kwargs(kwargs))

    def options(self, url: Any, **kwargs: Any) -> Any:
        return self._session.options(url, **self._kwargs(kwargs))

    def __getattr__(self, name: str) -> Any:
        # WebSocket transports and uncommon ClientSession attributes retain
        # native behavior. Batch HTTP verbs above are the traced boundary.
        return getattr(self._session, name)


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(maximum, max(minimum, value))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _bounded_label(value: Any, *, limit: int) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > limit:
        return "unknown"
    return text if all(ch.isalnum() or ch in "._-" for ch in text) else "unknown"


def _origin_hash(url: Any) -> str:
    try:
        scheme = str(url.scheme or "").lower()
        host = str(url.host or "").lower()
        port = int(url.port) if url.port is not None else (443 if scheme == "https" else 80)
    except Exception:
        return "unknown"
    if scheme not in {"http", "https", "ws", "wss"} or not host:
        return "unknown"
    origin = f"{scheme}://{host}:{port}"
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]


class ProviderHttpTransport:
    """Own one reusable, bounded :class:`aiohttp.ClientSession` per app loop."""

    def __init__(self, *, trace_limit: int = _TRACE_LIMIT) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: aiohttp.ClientSession | None = None
        self._trace_limit = min(512, max(8, int(trace_limit)))
        self._completed: deque[dict[str, Any]] = deque(maxlen=self._trace_limit)
        self._active: dict[str, SimpleNamespace] = {}
        self._diagnostics_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._session is not None and not self._session.closed

    @staticmethod
    def trace_request_context(
        *,
        provider: str,
        marker: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        """Build the only supported per-request trace context.

        ``marker`` is process-local and is never persisted in diagnostics.  It
        receives canonical latency marker names and an optional ``timestamp_ns``.
        """

        result: dict[str, Any] = {
            "provider": _bounded_label(provider, limit=_PROVIDER_LIMIT),
        }
        if callable(marker):
            result["marker"] = marker
        return result

    async def session(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("Provider HTTP transport cannot cross asyncio event loops")
        if self._session is not None and not self._session.closed:
            return self._session

        self._loop = loop
        connector = aiohttp.TCPConnector(
            family=socket.AF_UNSPEC,
            happy_eyeballs_delay=_env_float(
                "SCRIBER_PROVIDER_HTTP_HAPPY_EYEBALLS_DELAY_SECONDS",
                0.25,
                minimum=0.0,
                maximum=2.0,
            ),
            interleave=1,
            ttl_dns_cache=_env_int(
                "SCRIBER_PROVIDER_HTTP_DNS_TTL_SECONDS",
                300,
                minimum=0,
                maximum=3600,
            ),
            keepalive_timeout=_env_float(
                "SCRIBER_PROVIDER_HTTP_KEEPALIVE_SECONDS",
                60.0,
                minimum=5.0,
                maximum=300.0,
            ),
            limit=_env_int(
                "SCRIBER_PROVIDER_HTTP_CONNECTION_LIMIT",
                32,
                minimum=1,
                maximum=128,
            ),
            limit_per_host=_env_int(
                "SCRIBER_PROVIDER_HTTP_CONNECTIONS_PER_HOST",
                8,
                minimum=1,
                maximum=32,
            ),
        )
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=_env_float(
                "SCRIBER_PROVIDER_HTTP_CONNECT_TIMEOUT_SECONDS",
                15.0,
                minimum=1.0,
                maximum=120.0,
            ),
            sock_connect=_env_float(
                "SCRIBER_PROVIDER_HTTP_SOCKET_CONNECT_TIMEOUT_SECONDS",
                15.0,
                minimum=1.0,
                maximum=120.0,
            ),
            sock_read=None,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            cookie_jar=aiohttp.DummyCookieJar(),
            trace_configs=[self._build_trace_config()],
        )
        return self._session

    async def session_view(
        self,
        *,
        provider: str,
        marker: Callable[..., None] | None = None,
    ) -> Any:
        return _ProviderHttpSessionView(
            await self.session(),
            provider=_bounded_label(provider, limit=_PROVIDER_LIMIT),
            marker=marker,
        )

    async def close(self) -> None:
        session = self._session
        if session is None:
            self._loop = None
            return
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Provider HTTP transport must close on its owning event loop")
        self._session = None
        try:
            if not session.closed:
                await session.close()
        finally:
            self._loop = None
            with self._diagnostics_lock:
                self._active.clear()

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded, credential-free connection evidence."""

        with self._diagnostics_lock:
            items = [dict(item) for item in self._completed]
            active_count = len(self._active)
        return {
            "schemaVersion": 1,
            "sessionOpen": self.is_open,
            "activeRequestCount": active_count,
            "retainedRequestCount": len(items),
            "connectionCreatedCount": sum(
                1 for item in items if item.get("connection") == "created"
            ),
            "connectionReusedCount": sum(
                1 for item in items if item.get("connection") == "reused"
            ),
            "dnsCacheHitCount": sum(1 for item in items if item.get("dns") == "cache_hit"),
            "dnsCacheMissCount": sum(1 for item in items if item.get("dns") == "cache_miss"),
            "items": items,
        }

    def _build_trace_config(self) -> aiohttp.TraceConfig:
        def context_factory(*, trace_request_ctx: Any = None) -> SimpleNamespace:
            request_ctx = trace_request_ctx if isinstance(trace_request_ctx, Mapping) else {}
            flow_id = uuid4().hex
            return SimpleNamespace(
                flow_id=flow_id,
                provider=_bounded_label(request_ctx.get("provider"), limit=_PROVIDER_LIMIT),
                marker=request_ctx.get("marker") if callable(request_ctx.get("marker")) else None,
                started_ns=0,
                origin_hash="unknown",
                dns="not_observed",
                dns_started_ns=0,
                dns_duration_ms=None,
                connection="not_observed",
                connection_started_ns=0,
                connection_duration_ms=None,
                queue_started_ns=0,
                queue_duration_ms=None,
                first_request_chunk_ns=0,
                last_request_chunk_ns=0,
                request_chunk_count=0,
                response_headers_ns=0,
                first_response_chunk_ns=0,
                last_response_chunk_ns=0,
                response_chunk_count=0,
                completed_item=None,
                outcome="active",
            )

        trace = aiohttp.TraceConfig(trace_config_ctx_factory=context_factory)

        async def request_start(_session: Any, ctx: SimpleNamespace, params: Any) -> None:
            now = time.perf_counter_ns()
            ctx.started_ns = now
            ctx.origin_hash = _origin_hash(params.url)
            with self._diagnostics_lock:
                self._active[ctx.flow_id] = ctx
            self._emit_marker(ctx, "request_started", now)

        async def dns_start(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.dns_started_ns = time.perf_counter_ns()
            ctx.dns = "cache_miss"

        async def dns_end(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            now = time.perf_counter_ns()
            if ctx.dns_started_ns:
                ctx.dns_duration_ms = (now - ctx.dns_started_ns) / 1_000_000

        async def dns_hit(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.dns = "cache_hit"

        async def dns_miss(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.dns = "cache_miss"

        async def connection_start(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.connection_started_ns = time.perf_counter_ns()

        async def connection_end(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            now = time.perf_counter_ns()
            ctx.connection = "created"
            if ctx.connection_started_ns:
                ctx.connection_duration_ms = (now - ctx.connection_started_ns) / 1_000_000

        async def connection_reused(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.connection = "reused"

        async def queue_start(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.queue_started_ns = time.perf_counter_ns()

        async def queue_end(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            if ctx.queue_started_ns:
                ctx.queue_duration_ms = (
                    time.perf_counter_ns() - ctx.queue_started_ns
                ) / 1_000_000

        async def request_chunk(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            now = time.perf_counter_ns()
            ctx.request_chunk_count += 1
            ctx.last_request_chunk_ns = now
            if not ctx.first_request_chunk_ns:
                ctx.first_request_chunk_ns = now
                self._emit_marker(ctx, "first_request_chunk_sent", now)

        async def response_chunk(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            now = time.perf_counter_ns()
            ctx.response_chunk_count += 1
            ctx.last_response_chunk_ns = now
            if not ctx.first_response_chunk_ns:
                ctx.first_response_chunk_ns = now
                self._emit_marker(ctx, "first_response_chunk_received", now)
            completed_item = getattr(ctx, "completed_item", None)
            if isinstance(completed_item, dict):
                with self._diagnostics_lock:
                    completed_item["outcome"] = "response_body"
                    completed_item["responseChunkCount"] = min(
                        1_000_000,
                        int(ctx.response_chunk_count),
                    )
                    if ctx.response_headers_ns:
                        completed_item["headersToLastBodyChunkMs"] = round(
                            max(0.0, (now - ctx.response_headers_ns) / 1_000_000),
                            3,
                        )

        async def request_end(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.response_headers_ns = time.perf_counter_ns()
            ctx.outcome = "response_headers"
            self._complete_trace(ctx)
            self._emit_marker(
                ctx,
                "response_headers_received",
                ctx.response_headers_ns,
            )

        async def request_exception(_session: Any, ctx: SimpleNamespace, _params: Any) -> None:
            ctx.outcome = "exception"
            self._complete_trace(ctx)

        trace.on_request_start.append(request_start)
        trace.on_dns_resolvehost_start.append(dns_start)
        trace.on_dns_resolvehost_end.append(dns_end)
        trace.on_dns_cache_hit.append(dns_hit)
        trace.on_dns_cache_miss.append(dns_miss)
        trace.on_connection_create_start.append(connection_start)
        trace.on_connection_create_end.append(connection_end)
        trace.on_connection_reuseconn.append(connection_reused)
        trace.on_connection_queued_start.append(queue_start)
        trace.on_connection_queued_end.append(queue_end)
        trace.on_request_chunk_sent.append(request_chunk)
        trace.on_response_chunk_received.append(response_chunk)
        trace.on_request_end.append(request_end)
        trace.on_request_exception.append(request_exception)
        return trace

    @staticmethod
    def _emit_marker(ctx: SimpleNamespace, name: str, timestamp_ns: int) -> None:
        marker = getattr(ctx, "marker", None)
        if not callable(marker):
            return
        try:
            marker(_bounded_label(name, limit=_MARKER_LIMIT), timestamp_ns=timestamp_ns)
        except TypeError:
            marker(_bounded_label(name, limit=_MARKER_LIMIT))
        except Exception:
            # Diagnostics must never perturb a provider request.
            return

    def _complete_trace(self, ctx: SimpleNamespace) -> None:
        now = time.perf_counter_ns()
        if ctx.last_request_chunk_ns:
            self._emit_marker(ctx, "last_request_chunk_sent", ctx.last_request_chunk_ns)
        item: dict[str, Any] = {
            "flowId": ctx.flow_id,
            "provider": ctx.provider,
            "originHash": ctx.origin_hash,
            "outcome": ctx.outcome,
            "connection": ctx.connection,
            "dns": ctx.dns,
            "requestChunkCount": min(1_000_000, int(ctx.request_chunk_count)),
            "responseChunkCount": min(1_000_000, int(ctx.response_chunk_count)),
            "totalToHeadersMs": (
                round((now - ctx.started_ns) / 1_000_000, 3)
                if ctx.started_ns
                else None
            ),
        }
        for key, value in (
            ("dnsMs", ctx.dns_duration_ms),
            ("connectionMs", ctx.connection_duration_ms),
            ("connectionQueueMs", ctx.queue_duration_ms),
        ):
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                item[key] = round(max(0.0, float(value)), 3)
        with self._diagnostics_lock:
            self._active.pop(ctx.flow_id, None)
            self._completed.append(item)
            ctx.completed_item = item


__all__ = ["ProviderHttpTransport", "ProviderRequestAcceptanceUnknown"]
