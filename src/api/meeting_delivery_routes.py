"""Meeting webhook delivery routes with DNS-rebinding-resistant transport."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.abc import AbstractResolver, ResolveResult
from loguru import logger

from src.core.rest_contracts import REST_API_VERSION
from src.core.ws_contracts import meeting_delivery_updated_event
from src.data.meeting_store import MeetingConflict, MeetingNotFound
from src.version import app_version

_WEBHOOK_TIMEOUT = ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=15)
_WEBHOOK_RETRY_STATUSES = {408, 425, 429}


@dataclass(frozen=True)
class ResolvedWebhookAddress:
    host: str
    family: int
    proto: int


@dataclass(frozen=True)
class ValidatedWebhookTarget:
    url: str
    stored_target: str
    hostname: str
    port: int
    addresses: tuple[ResolvedWebhookAddress, ...]

    @property
    def allowed_ips(self) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return frozenset(ipaddress.ip_address(item.host) for item in self.addresses)


@dataclass(frozen=True)
class MeetingDeliveryService:
    store: Any
    broadcast: Callable[[dict[str, Any]], Awaitable[None]]


APP_MEETING_DELIVERY_SERVICE: web.AppKey[MeetingDeliveryService] = web.AppKey(
    "meeting_delivery_service",
    MeetingDeliveryService,
)


def _normalized_hostname(hostname: str) -> str:
    value = str(hostname or "").strip().rstrip(".")
    try:
        return value.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return value.casefold()


class PinnedWebhookResolver(AbstractResolver):
    """Resolve one hostname to the exact public IP set validated earlier."""

    def __init__(self, target: ValidatedWebhookTarget) -> None:
        self._hostname = _normalized_hostname(target.hostname)
        self._addresses = target.addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        if _normalized_hostname(host) != self._hostname:
            raise OSError("Webhook resolver rejected an unexpected hostname.")
        requested_family = int(family)
        results = [
            ResolveResult(
                hostname=host,
                host=address.host,
                port=int(port),
                family=address.family,
                proto=address.proto,
                flags=socket.AI_NUMERICHOST,
            )
            for address in self._addresses
            if requested_family in {socket.AF_UNSPEC, address.family}
        ]
        if not results:
            raise OSError("Webhook resolver has no address for the requested family.")
        return results

    async def close(self) -> None:
        return None


async def validate_webhook_target(raw_url: str) -> ValidatedWebhookTarget:
    parsed = urlparse(str(raw_url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Webhook URL must be HTTPS and must not contain credentials.")
    if parsed.port not in {None, 443}:
        raise ValueError("Webhook URL must use the standard HTTPS port.")

    hostname = parsed.hostname
    port = parsed.port or 443
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("Webhook hostname could not be resolved.") from exc
    if not infos:
        raise ValueError("Webhook hostname did not resolve to an address.")

    addresses: list[ResolvedWebhookAddress] = []
    seen: set[tuple[str, int, int]] = set()
    for family, _socktype, proto, _canonname, sockaddr in infos:
        try:
            value = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError as exc:
            raise ValueError("Webhook hostname resolved to an invalid address.") from exc
        if not value.is_global:
            raise ValueError("Webhook targets must resolve only to public internet addresses.")
        item = (value.compressed, int(family), int(proto))
        if item in seen:
            continue
        seen.add(item)
        addresses.append(
            ResolvedWebhookAddress(
                host=item[0],
                family=item[1],
                proto=item[2],
            )
        )
    if not addresses:
        raise ValueError("Webhook hostname did not resolve to an address.")

    return ValidatedWebhookTarget(
        url=parsed._replace(fragment="").geturl(),
        stored_target=parsed._replace(query="", fragment="").geturl(),
        hostname=hostname,
        port=port,
        addresses=tuple(addresses),
    )


def build_meeting_delivery_payload(detail: dict[str, Any]) -> dict[str, Any]:
    analysis: Any = next(
        (item.get("payload", {}) for item in detail.get("outputs", []) if item.get("kind") == "analysis"),
        {},
    )
    if isinstance(analysis, dict):
        analysis = dict(analysis)
        analysis["actionItems"] = detail.get(
            "actionItems",
            analysis.get("actionItems", []),
        )
    return {
        "apiVersion": REST_API_VERSION,
        "event": "meeting.ready",
        "meeting": {
            "id": detail["id"],
            "title": detail["title"],
            "language": detail["language"],
            "startedAt": detail["startedAt"],
            "endedAt": detail["endedAt"],
            "state": detail["state"],
        },
        "analysis": analysis,
        "segments": [
            {
                "id": item["id"],
                "source": item["source"],
                "speakerLabel": item["speakerLabel"],
                "startMs": item["startMs"],
                "endMs": item["endMs"],
                "text": item["text"],
            }
            for item in detail.get("segments", [])
        ],
        "notes": [{"id": item["id"], "body": item["body"], "atMs": item["atMs"]} for item in detail.get("notes", [])],
    }


def _pinned_socket_factory(
    target: ValidatedWebhookTarget,
) -> Callable[[tuple[Any, ...]], socket.socket]:
    """Create sockets only for the IPs and port authorized before delivery.

    The check happens before ``connect()``, unlike response-level peer
    inspection: aiohttp may release a connection before user code enters the
    response context (notably for an empty 204 response).
    """

    allowed = {(address.family, ipaddress.ip_address(address.host), target.port) for address in target.addresses}

    def create_socket(address_info: tuple[Any, ...]) -> socket.socket:
        family, sock_type, proto, _canonname, sockaddr = address_info
        try:
            destination = (
                int(family),
                ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]),
                int(sockaddr[1]),
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise OSError("Webhook connector rejected an invalid destination.") from exc
        if destination not in allowed:
            raise OSError("Webhook connector rejected an unvalidated destination.")
        return socket.socket(family=int(family), type=int(sock_type), proto=int(proto))

    return create_socket


def _delivery_preview_hash(target_url: str, encoded_payload: bytes) -> str:
    digest = hashlib.sha256()
    encoded_target = target_url.encode("utf-8")
    digest.update(len(encoded_target).to_bytes(8, "big"))
    digest.update(encoded_target)
    digest.update(encoded_payload)
    return digest.hexdigest()


async def _update_delivery_after_cancellation(
    service: MeetingDeliveryService,
    delivery_id: str,
    *,
    attempt_count: int,
) -> None:
    network_started = attempt_count > 0
    update_task = asyncio.create_task(
        asyncio.to_thread(
            service.store.update_delivery,
            delivery_id,
            status="outcome_unknown" if network_started else "failed",
            response_payload={},
            error_message="CancelledError",
            attempt_count=attempt_count,
        )
    )
    await asyncio.shield(update_task)


async def _claim_delivery_with_cancellation_barrier(
    service: MeetingDeliveryService,
    claim: Callable[[], tuple[dict[str, Any], bool]],
) -> tuple[dict[str, Any], bool]:
    """Settle the durable claim before propagating request cancellation."""

    worker = asyncio.create_task(asyncio.to_thread(claim))
    pending_cancel: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            pending_cancel = exc
    try:
        result = worker.result()
    except BaseException:
        if pending_cancel is not None:
            logger.warning("Meeting delivery claim failed while its request was cancelling")
            raise pending_cancel from None
        raise
    if pending_cancel is not None:
        delivery, claimed = result
        if claimed:
            try:
                await _update_delivery_after_cancellation(
                    service,
                    delivery["id"],
                    attempt_count=0,
                )
            except asyncio.CancelledError as exc:
                pending_cancel = exc
            except Exception as exc:
                logger.warning(
                    "Meeting delivery cancellation reconciliation failed (error_type={})",
                    type(exc).__name__,
                )
        raise pending_cancel
    return result


async def _preview_meeting_delivery(request: web.Request) -> web.Response:
    service = request.app[APP_MEETING_DELIVERY_SERVICE]
    meeting_id = request.match_info.get("id", "")
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("Expected JSON object")
        target = await validate_webhook_target(str(raw.get("url", "")))
        detail = await asyncio.to_thread(service.store.detail, meeting_id)
        payload = build_meeting_delivery_payload(detail)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = _delivery_preview_hash(target.url, encoded)
        confirmation_token = await asyncio.to_thread(
            service.store.create_delivery_confirmation,
            meeting_id,
            fingerprint=fingerprint,
        )
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "target": target.url,
                "previewHash": confirmation_token,
                "payload": payload,
                "byteSize": len(encoded),
            }
        )
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def _deliver_meeting_webhook(request: web.Request) -> web.Response:
    service = request.app[APP_MEETING_DELIVERY_SERVICE]
    meeting_id = request.match_info.get("id", "")
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("Expected JSON object")
        if raw.get("confirmed") is not True:
            return web.json_response(
                {"message": "Webhook delivery requires explicit confirmation."},
                status=409,
            )
        target = await validate_webhook_target(str(raw.get("url", "")))
        detail = await asyncio.to_thread(service.store.detail, meeting_id)
        payload = build_meeting_delivery_payload(detail)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = _delivery_preview_hash(target.url, encoded)
        confirmation_token = str(raw.get("previewHash", "")).strip()
        delivery, claimed = await _claim_delivery_with_cancellation_barrier(
            service,
            lambda: service.store.claim_delivery_confirmation(
                meeting_id,
                confirmation_token=confirmation_token,
                fingerprint=fingerprint,
                kind="webhook",
                target=target.stored_target,
                request_payload={
                    "previewHash": confirmation_token,
                    "byteSize": len(encoded),
                    "event": "meeting.ready",
                },
                status="sending",
            ),
        )
        if not claimed:
            replay_status = 202 if delivery["status"] == "sending" else 200
            return web.json_response(
                {"apiVersion": REST_API_VERSION, "delivery": delivery},
                status=replay_status,
            )
        secret = str(raw.get("secret", ""))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"Scriber/{app_version()}",
            "X-Scriber-Event": "meeting.ready",
            "X-Scriber-Delivery": delivery["id"],
            "Idempotency-Key": delivery["idempotencyKey"],
        }
        if secret:
            headers["X-Scriber-Signature"] = (
                "sha256="
                + hmac.new(
                    secret.encode("utf-8"),
                    encoded,
                    hashlib.sha256,
                ).hexdigest()
            )

        final_status = "failed"
        final_response: dict[str, Any] = {}
        final_error = "Webhook delivery failed"
        attempts = 0
        try:
            connector = TCPConnector(
                resolver=PinnedWebhookResolver(target),
                socket_factory=_pinned_socket_factory(target),
                family=socket.AF_UNSPEC,
                use_dns_cache=False,
                force_close=True,
                limit=1,
                limit_per_host=1,
            )
            async with ClientSession(
                connector=connector,
                timeout=_WEBHOOK_TIMEOUT,
                trust_env=False,
            ) as session:
                for attempt in range(1, 4):
                    attempts = attempt
                    try:
                        async with session.post(
                            target.url,
                            data=encoded,
                            headers=headers,
                            allow_redirects=False,
                        ) as response:
                            final_response = {"httpStatus": response.status}
                            if 200 <= response.status < 300:
                                final_status, final_error = "delivered", ""
                                break
                            final_error = f"Webhook returned HTTP {response.status}"
                            if response.status not in _WEBHOOK_RETRY_STATUSES and response.status < 500:
                                break
                    except Exception as exc:
                        final_error = type(exc).__name__
                    if attempt < 3:
                        await asyncio.sleep(0.25 * (4 ** (attempt - 1)))
        except asyncio.CancelledError:
            await _update_delivery_after_cancellation(
                service,
                delivery["id"],
                attempt_count=attempts,
            )
            raise
        except Exception as exc:
            final_error = type(exc).__name__

        delivery = await asyncio.to_thread(
            service.store.update_delivery,
            delivery["id"],
            status=final_status,
            response_payload=final_response,
            error_message=final_error,
            attempt_count=attempts,
        )
        try:
            await service.broadcast(meeting_delivery_updated_event(meeting_id, delivery))
        except Exception as exc:
            logger.warning(
                "Meeting delivery broadcast failed (error_type={})",
                type(exc).__name__,
            )
        response_status = 201 if delivery["status"] == "delivered" else 502
        return web.json_response(
            {"apiVersion": REST_API_VERSION, "delivery": delivery},
            status=response_status,
        )
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    except MeetingConflict:
        return web.json_response(
            {"message": "Webhook preview changed; review it again before sending."},
            status=409,
        )
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def _list_meeting_deliveries(request: web.Request) -> web.Response:
    service = request.app[APP_MEETING_DELIVERY_SERVICE]
    meeting_id = request.match_info.get("id", "")
    try:
        items = await asyncio.to_thread(service.store.deliveries, meeting_id)
        return web.json_response({"apiVersion": REST_API_VERSION, "items": items})
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)


def register_meeting_delivery_routes(
    app: web.Application,
    *,
    store: Any,
    broadcast: Callable[[dict[str, Any]], Awaitable[None]] | None,
) -> None:
    """Register the meeting-delivery domain without web_api closure coupling."""

    async def ignore_broadcast(_event: dict[str, Any]) -> None:
        return None

    app[APP_MEETING_DELIVERY_SERVICE] = MeetingDeliveryService(
        store=store,
        broadcast=broadcast or ignore_broadcast,
    )
    app.router.add_post(
        "/api/meetings/{id}/deliveries/preview",
        _preview_meeting_delivery,
    )
    app.router.add_post(
        "/api/meetings/{id}/deliveries",
        _deliver_meeting_webhook,
    )
    app.router.add_get(
        "/api/meetings/{id}/deliveries",
        _list_meeting_deliveries,
    )
