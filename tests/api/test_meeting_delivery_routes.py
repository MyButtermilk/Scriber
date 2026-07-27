from __future__ import annotations

import asyncio
import socket
import threading

import pytest
from aiohttp import ClientSession, TCPConnector, web

from src.api.meeting_delivery_routes import (
    MeetingDeliveryService,
    PinnedWebhookResolver,
    ResolvedWebhookAddress,
    ValidatedWebhookTarget,
    _delivery_preview_hash,
    _claim_delivery_with_cancellation_barrier,
    _pinned_socket_factory,
    _update_delivery_after_cancellation,
    validate_webhook_target,
)


@pytest.mark.asyncio
async def test_webhook_validation_rejects_mixed_public_and_private_dns_answers(
    monkeypatch,
):
    async def mixed_dns(_loop, host, port, **_kwargs):
        assert host == "webhook.example"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", port),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", port),
            ),
        ]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", mixed_dns)

    with pytest.raises(ValueError, match="public internet"):
        await validate_webhook_target("https://webhook.example/hook")


@pytest.mark.asyncio
async def test_pinned_webhook_resolver_returns_only_prevalidated_addresses():
    target = ValidatedWebhookTarget(
        url="https://webhook.example/hook?opaque=value",
        stored_target="https://webhook.example/hook",
        hostname="webhook.example",
        port=443,
        addresses=(
            ResolvedWebhookAddress(
                host="93.184.216.34",
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
            ),
        ),
    )
    resolver = PinnedWebhookResolver(target)

    resolved = await resolver.resolve(
        "webhook.example",
        443,
        socket.AF_UNSPEC,
    )

    assert [item["host"] for item in resolved] == ["93.184.216.34"]
    assert [item["hostname"] for item in resolved] == ["webhook.example"]
    with pytest.raises(OSError, match="unexpected hostname"):
        await resolver.resolve("rebound.example", 443, socket.AF_UNSPEC)


def test_webhook_socket_factory_rejects_unvalidated_destination():
    target = ValidatedWebhookTarget(
        url="https://webhook.example/hook",
        stored_target="https://webhook.example/hook",
        hostname="webhook.example",
        port=443,
        addresses=(
            ResolvedWebhookAddress(
                host="93.184.216.34",
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
            ),
        ),
    )
    create_socket = _pinned_socket_factory(target)

    with pytest.raises(OSError, match="unvalidated destination"):
        create_socket(
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        )


def test_webhook_preview_hash_binds_full_target_url():
    payload = b'{"event":"meeting.ready"}'
    original = _delivery_preview_hash(
        "https://webhook.example/hook?tenant=one",
        payload,
    )

    assert original != _delivery_preview_hash(
        "https://other.example/hook?tenant=one",
        payload,
    )
    assert original != _delivery_preview_hash(
        "https://webhook.example/other?tenant=one",
        payload,
    )
    assert original != _delivery_preview_hash(
        "https://webhook.example/hook?tenant=two",
        payload,
    )


@pytest.mark.asyncio
async def test_pinned_transport_accepts_real_empty_204_response():
    async def empty_response(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/hook", empty_response)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        server = getattr(site, "_server")
        port = int(server.sockets[0].getsockname()[1])
        target = ValidatedWebhookTarget(
            url=f"http://webhook.test:{port}/hook",
            stored_target=f"http://webhook.test:{port}/hook",
            hostname="webhook.test",
            port=port,
            addresses=(
                ResolvedWebhookAddress(
                    host="127.0.0.1",
                    family=socket.AF_INET,
                    proto=socket.IPPROTO_TCP,
                ),
            ),
        )
        connector = TCPConnector(
            resolver=PinnedWebhookResolver(target),
            socket_factory=_pinned_socket_factory(target),
            use_dns_cache=False,
            force_close=True,
        )
        async with ClientSession(connector=connector, trust_env=False) as session:
            async with session.post(target.url, allow_redirects=False) as response:
                assert response.status == 204
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cancelled_delivery_after_network_attempt_is_outcome_unknown():
    updates: list[tuple[str, dict[str, object]]] = []

    class Store:
        def update_delivery(self, delivery_id: str, **changes):
            updates.append((delivery_id, changes))
            return {"id": delivery_id, **changes}

    async def broadcast(_event):
        return None

    service = MeetingDeliveryService(store=Store(), broadcast=broadcast)

    await _update_delivery_after_cancellation(
        service,
        "delivery-1",
        attempt_count=2,
    )

    assert updates == [
        (
            "delivery-1",
            {
                "status": "outcome_unknown",
                "response_payload": {},
                "error_message": "CancelledError",
                "attempt_count": 2,
            },
        )
    ]


@pytest.mark.asyncio
async def test_cancelled_delivery_before_network_attempt_is_failed():
    updates: list[tuple[str, dict[str, object]]] = []

    class Store:
        def update_delivery(self, delivery_id: str, **changes):
            updates.append((delivery_id, changes))
            return {"id": delivery_id, **changes}

    async def broadcast(_event):
        return None

    service = MeetingDeliveryService(store=Store(), broadcast=broadcast)

    await _update_delivery_after_cancellation(
        service,
        "delivery-1",
        attempt_count=0,
    )

    assert updates[0][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_request_settles_claim_and_marks_unsent_delivery_failed():
    claim_started = threading.Event()
    release_claim = threading.Event()
    updates: list[tuple[str, dict[str, object]]] = []

    class Store:
        def claim(self):
            claim_started.set()
            assert release_claim.wait(timeout=5)
            return {"id": "delivery-1", "status": "sending"}, True

        def update_delivery(self, delivery_id: str, **changes):
            updates.append((delivery_id, changes))
            return {"id": delivery_id, **changes}

    async def broadcast(_event):
        return None

    store = Store()
    service = MeetingDeliveryService(store=store, broadcast=broadcast)
    task = asyncio.create_task(
        _claim_delivery_with_cancellation_barrier(service, store.claim)
    )
    assert await asyncio.to_thread(claim_started.wait, 5)

    task.cancel()
    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert updates == [
        (
            "delivery-1",
            {
                "status": "failed",
                "response_payload": {},
                "error_message": "CancelledError",
                "attempt_count": 0,
            },
        )
    ]
