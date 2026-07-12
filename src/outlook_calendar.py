"""Outlook public-desktop OAuth/PKCE and incremental calendar context."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession

from src import database as db


AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
GRAPH_DELTA = "https://graph.microsoft.com/v1.0/me/calendarView/delta"
SCOPES = "User.Read Calendars.Read offline_access"
WINDOWS_TIMEZONE_TO_IANA = {
    "W. Europe Standard Time": "Europe/Berlin",
    "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _graph_datetime_utc(value: Any) -> str:
    """Return a canonical offset-aware UTC timestamp from Graph DateTimeTimeZone."""
    if not isinstance(value, dict):
        return ""
    raw = str(value.get("dateTime") or "").strip()
    zone = str(value.get("timeZone") or "UTC").strip()
    if not raw:
        return ""
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        if zone.upper() in {"UTC", "ETC/UTC", "GMT"}:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(WINDOWS_TIMEZONE_TO_IANA.get(zone, zone)))
            except ZoneInfoNotFoundError:
                return ""
    return parsed.astimezone(timezone.utc).isoformat()


def _delta_window_needs_reseed(window_end: str, now: datetime) -> bool:
    if not window_end:
        return True
    try:
        parsed = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return parsed.astimezone(timezone.utc) <= now + timedelta(days=7)


class OutlookCalendarService:
    def __init__(self, shell_call: Callable[..., dict[str, Any]], client_id: str) -> None:
        self.shell_call = shell_call
        self.client_id = client_id.strip()
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._initialize()

    def _initialize(self) -> None:
        with db._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS outlook_calendar_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    delta_link TEXT NOT NULL DEFAULT '',
                    window_start TEXT NOT NULL DEFAULT '',
                    window_end TEXT NOT NULL DEFAULT '',
                    last_sync_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO outlook_calendar_state(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS outlook_calendar_events (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL DEFAULT '',
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    organizer_json TEXT NOT NULL DEFAULT '{}',
                    attendees_json TEXT NOT NULL DEFAULT '[]',
                    join_url TEXT NOT NULL DEFAULT '',
                    is_cancelled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outlook_events_start ON outlook_calendar_events(start_at);
            """)
            conn.commit()

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    def redirect_uri(self) -> str:
        port = max(1, min(65535, int(os.getenv("SCRIBER_WEB_PORT", "8765") or 8765)))
        return f"http://127.0.0.1:{port}/api/calendar/outlook/callback"

    def begin_connect(self, *, open_browser: bool = True) -> dict[str, Any]:
        if not self.configured:
            raise ValueError("SCRIBER_OUTLOOK_CLIENT_ID is not configured for this build.")
        verifier, challenge = create_pkce_pair()
        state = secrets.token_urlsafe(32)
        redirect_uri = self.redirect_uri()
        now = time.monotonic()
        self._pending = {
            key: pending for key, pending in self._pending.items() if pending[2] >= now
        }
        self._pending[state] = (verifier, redirect_uri, time.monotonic() + 600.0)
        url = AUTHORITY + "?" + urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        if open_browser:
            webbrowser.open(url, new=2)
        return {"authorizationUrl": url, "expiresIn": 600, "redirectUri": redirect_uri}

    async def complete_connect(self, state: str, code: str) -> None:
        pending = self._pending.pop(state, None)
        if pending is None or pending[2] < time.monotonic() or not code:
            raise ValueError("Outlook authorization state is invalid or expired.")
        verifier, redirect_uri, _ = pending
        response = await asyncio.to_thread(
            self.shell_call,
            "outlookAuthorizationCodeExchange",
            {"clientId": self.client_id, "code": code, "codeVerifier": verifier,
             "redirectUri": redirect_uri},
            timeout_seconds=25.0,
        )
        if not response.get("success"):
            raise ValueError(str(response.get("fallbackReason") or "Outlook authorization failed."))
        self._remember_access_token(response.get("payload", {}))

    def cancel_connect(self, state: str) -> None:
        if state:
            self._pending.pop(state, None)
        self.record_sync_error("AuthorizationCanceled")

    def _remember_access_token(self, payload: Any) -> str:
        if not isinstance(payload, dict) or not str(payload.get("accessToken", "")):
            raise ValueError("Outlook access token was not returned by the private shell.")
        self._access_token = str(payload["accessToken"])
        self._access_token_expires_at = time.monotonic() + max(30, int(payload.get("expiresIn", 0)) - 60)
        return self._access_token

    async def acquire_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token
        response = await asyncio.to_thread(
            self.shell_call, "outlookTokenAcquire", {"clientId": self.client_id},
            timeout_seconds=25.0,
        )
        if not response.get("success"):
            raise ValueError(str(response.get("fallbackReason") or "Outlook token refresh failed."))
        return self._remember_access_token(response.get("payload", {}))

    async def sync(self, session: ClientSession) -> int:
        token = await self.acquire_access_token()
        row = db._get_connection().execute("SELECT * FROM outlook_calendar_state WHERE id=1").fetchone()
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(days=1), now + timedelta(days=30)
        reseeding = _delta_window_needs_reseed(str(row["window_end"] or ""), now)
        url = "" if reseeding else str(row["delta_link"] or "")
        if not url:
            url = GRAPH_DELTA + "?" + urlencode({
                "startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
                "$select": "id,subject,start,end,organizer,attendees,onlineMeeting,onlineMeetingUrl,isCancelled,lastModifiedDateTime",
            })
        changed = 0
        delta_link = ""
        snapshot_ids: set[str] = set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
                raise ValueError("Microsoft Graph pagination returned an invalid endpoint.")
            async with session.get(
                url, headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError(f"Microsoft Graph calendar sync failed (HTTP {response.status}).")
                payload = await response.json()
            with db._get_connection() as conn:
                for event in payload.get("value", []):
                    if not isinstance(event, dict) or not event.get("id"):
                        continue
                    if "@removed" in event:
                        conn.execute("DELETE FROM outlook_calendar_events WHERE id=?", (str(event["id"]),))
                    else:
                        start_at = _graph_datetime_utc(event.get("start"))
                        end_at = _graph_datetime_utc(event.get("end"))
                        if not start_at or not end_at:
                            continue
                        snapshot_ids.add(str(event["id"]))
                        online = event.get("onlineMeeting") if isinstance(event.get("onlineMeeting"), dict) else {}
                        conn.execute(
                            """INSERT INTO outlook_calendar_events
                               (id,subject,start_at,end_at,organizer_json,attendees_json,join_url,is_cancelled,updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                               subject=excluded.subject,start_at=excluded.start_at,end_at=excluded.end_at,
                               organizer_json=excluded.organizer_json,attendees_json=excluded.attendees_json,
                               join_url=excluded.join_url,is_cancelled=excluded.is_cancelled,updated_at=excluded.updated_at""",
                            (str(event["id"]), str(event.get("subject", ""))[:500],
                             start_at, end_at,
                             json.dumps(event.get("organizer") or {}, ensure_ascii=False),
                             json.dumps(event.get("attendees") or [], ensure_ascii=False),
                             str(online.get("joinUrl") or event.get("onlineMeetingUrl") or "")[:2048],
                             int(bool(event.get("isCancelled"))),
                             str(event.get("lastModifiedDateTime") or now.isoformat())),
                        )
                    changed += 1
                conn.commit()
            url = str(payload.get("@odata.nextLink") or "")
            delta_link = str(payload.get("@odata.deltaLink") or delta_link)
        with db._get_connection() as conn:
            if reseeding:
                # A fresh delta response is a complete snapshot for this rolling
                # window. Reconcile only after all pages succeeded so a network
                # failure cannot destroy the last usable calendar cache.
                cached_ids = {
                    str(item["id"])
                    for item in conn.execute("SELECT id FROM outlook_calendar_events").fetchall()
                }
                for stale_id in cached_ids - snapshot_ids:
                    conn.execute("DELETE FROM outlook_calendar_events WHERE id=?", (stale_id,))
            conn.execute(
                """UPDATE outlook_calendar_state SET delta_link=?,window_start=?,window_end=?,
                   last_sync_at=?,last_error='' WHERE id=1""",
                (delta_link, start.isoformat(), end.isoformat(), now.isoformat()),
            )
            conn.commit()
        return changed

    async def disconnect(self) -> None:
        response = await asyncio.to_thread(
            self.shell_call, "outlookCredentialDelete", {}, timeout_seconds=3.0
        )
        if not response.get("success"):
            raise ValueError(str(response.get("fallbackReason") or "Outlook credential deletion failed."))
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
        if payload.get("credentialStored") is True or payload.get("deleted") is False:
            raise ValueError("Outlook credential deletion was not confirmed by the private shell.")
        self._access_token = ""
        self._access_token_expires_at = 0.0
        with db._get_connection() as conn:
            conn.execute("DELETE FROM outlook_calendar_events")
            conn.execute("UPDATE outlook_calendar_state SET delta_link='',last_sync_at='',last_error='' WHERE id=1")
            conn.commit()

    def record_sync_error(self, error_type: str) -> None:
        """Persist only a bounded error class; Graph/token details stay out of SQLite."""
        safe = "".join(char for char in str(error_type) if char.isalnum() or char in "_-")[:80]
        with db._get_connection() as conn:
            conn.execute(
                "UPDATE outlook_calendar_state SET last_error=? WHERE id=1",
                (safe or "CalendarSyncError",),
            )
            conn.commit()

    async def status(self) -> dict[str, Any]:
        monotonic_now = time.monotonic()
        self._pending = {
            key: pending for key, pending in self._pending.items() if pending[2] >= monotonic_now
        }
        shell = await asyncio.to_thread(
            self.shell_call, "outlookCredentialStatus", {}, timeout_seconds=2.0
        )
        connected = bool(shell.get("success") and (shell.get("payload") or {}).get("credentialStored"))
        state = db._get_connection().execute("SELECT * FROM outlook_calendar_state WHERE id=1").fetchone()
        next_event = db._get_connection().execute(
            """SELECT id,subject,start_at,end_at,join_url,organizer_json,attendees_json
               FROM outlook_calendar_events
               WHERE is_cancelled=0 AND end_at>=? ORDER BY start_at LIMIT 1""",
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchone()
        return {
            "configured": self.configured,
            "connected": connected,
            "authorizationPending": bool(self._pending),
            "scopes": SCOPES.split(),
            "lastSyncAt": state["last_sync_at"],
            "lastError": state["last_error"],
            "nextEvent": self._calendar_event_payload(next_event) if next_event else None,
        }

    def current_event(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        lower = (now - timedelta(minutes=15)).isoformat()
        upper = (now + timedelta(minutes=10)).isoformat()
        row = db._get_connection().execute(
            """SELECT id,subject,start_at,end_at,join_url,organizer_json,attendees_json
               FROM outlook_calendar_events
               WHERE is_cancelled=0 AND start_at<=? AND end_at>=? ORDER BY start_at LIMIT 1""",
            (upper, lower),
        ).fetchone()
        return self._calendar_event_payload(row) if row else None

    @staticmethod
    def _calendar_event_payload(row: Any) -> dict[str, Any]:
        def load_json(value: str, fallback: Any) -> Any:
            try:
                return json.loads(value) if value else fallback
            except (TypeError, json.JSONDecodeError):
                return fallback

        def contact(value: Any) -> dict[str, str] | None:
            if not isinstance(value, dict):
                return None
            email = value.get("emailAddress") if isinstance(value.get("emailAddress"), dict) else value
            address = str(email.get("address", "")).strip().lower()
            if not address or "@" not in address or len(address) > 320:
                return None
            return {"name": str(email.get("name", "")).strip()[:200], "address": address}

        organizer = contact(load_json(row["organizer_json"], {}))
        participants: list[dict[str, str]] = []
        seen: set[str] = set()
        for attendee in load_json(row["attendees_json"], []):
            item = contact(attendee)
            if item and item["address"] not in seen:
                seen.add(item["address"])
                participants.append(item)
        return {
            "id": row["id"],
            "subject": row["subject"],
            "start_at": row["start_at"],
            "end_at": row["end_at"],
            "join_url": row["join_url"],
            "organizer": organizer,
            "participants": participants,
        }
