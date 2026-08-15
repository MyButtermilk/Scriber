"""HTTP transport for Meeting catalogue, detail, and workspace discard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web


@dataclass(frozen=True, slots=True)
class MeetingListQuery:
    """Validated pagination input for the durable Meeting catalogue."""

    limit: int = 50
    offset: int = 0

    @classmethod
    def parse(cls, raw: Mapping[str, str]) -> MeetingListQuery:
        try:
            return cls(
                limit=int(raw.get("limit", "50")),
                offset=int(raw.get("offset", "0")),
            )
        except ValueError:
            raise ValueError("limit and offset must be integers") from None


@dataclass(frozen=True, slots=True)
class MeetingDetailQuery:
    """Immutable transcript revision selected for one Meeting detail read."""

    revision: str = "canonical"

    @classmethod
    def parse(cls, raw: Mapping[str, str]) -> MeetingDetailQuery:
        return cls(revision=raw.get("revision", "canonical"))


@dataclass(frozen=True, slots=True)
class MeetingCatalogOutcome:
    """Public response produced by one catalogue operation."""

    status: int
    payload: Mapping[str, Any]


class MeetingCatalogControllerPort(Protocol):
    """Catalogue capability exposed by the application controller."""

    async def list_meetings(
        self,
        query: MeetingListQuery,
    ) -> MeetingCatalogOutcome: ...

    async def meeting_detail(
        self,
        meeting_id: str,
        query: MeetingDetailQuery,
    ) -> MeetingCatalogOutcome: ...

    async def discard_meeting(
        self,
        meeting_id: str,
    ) -> MeetingCatalogOutcome: ...


@dataclass(frozen=True, slots=True)
class MeetingCatalogRoutes:
    control: MeetingCatalogControllerPort


APP_MEETING_CATALOG_ROUTES: web.AppKey[MeetingCatalogRoutes] = web.AppKey(
    "meeting_catalog_routes",
    MeetingCatalogRoutes,
)


def _control(request: web.Request) -> MeetingCatalogControllerPort:
    return request.app[APP_MEETING_CATALOG_ROUTES].control


def _response(outcome: MeetingCatalogOutcome) -> web.Response:
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def list_meetings(request: web.Request) -> web.Response:
    try:
        query = MeetingListQuery.parse(request.query)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    return _response(await _control(request).list_meetings(query))


async def meeting_detail(request: web.Request) -> web.Response:
    return _response(
        await _control(request).meeting_detail(
            request.match_info.get("id", ""),
            MeetingDetailQuery.parse(request.query),
        )
    )


async def discard_meeting(request: web.Request) -> web.Response:
    return _response(
        await _control(request).discard_meeting(
            request.match_info.get("id", ""),
        )
    )


def register_meeting_catalog_routes(
    app: web.Application,
    *,
    control: MeetingCatalogControllerPort,
) -> None:
    app[APP_MEETING_CATALOG_ROUTES] = MeetingCatalogRoutes(control=control)
    app.router.add_get("/api/meetings", list_meetings)
    app.router.add_get("/api/meetings/{id}", meeting_detail)
    app.router.add_delete("/api/meetings/{id}", discard_meeting)
    app.router.add_post("/api/meetings/{id}/discard", discard_meeting)
