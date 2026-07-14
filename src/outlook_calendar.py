"""Outlook public-desktop OAuth/PKCE and incremental calendar context."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession

from src import database as db


AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
GRAPH_DELTA = "https://graph.microsoft.com/v1.0/me/calendarView/delta"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
SCOPES = "User.Read Calendars.Read offline_access"
GRAPH_SYNC_TIMEOUT_SECONDS = 60.0
REAUTHORIZATION_REQUIRED_ERROR = "OutlookReauthorizationRequired"
WINDOWS_TIMEZONE_TO_IANA = {
    "W. Europe Standard Time": "Europe/Berlin",
    "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
}


class _GraphUnauthorized(RuntimeError):
    pass


class _GraphDeltaExpired(RuntimeError):
    pass


class OutlookReauthorizationRequired(ValueError):
    """The cached Outlook credential exists but can no longer authorize Graph."""

    def __init__(self) -> None:
        super().__init__(
            "Outlook access expired or was revoked. Reconnect Outlook in Settings."
        )


def _normalize_public_client_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if len(candidate) != 36:
        return ""
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError):
        return ""
    if parsed.int == 0 or str(parsed).casefold() != candidate.casefold():
        return ""
    return str(parsed)


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
        self.client_id = _normalize_public_client_id(client_id)
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._claimed_pending: set[str] = set()
        # OAuth callbacks, cancellation, disconnect, and status reads can cross
        # the aiohttp loop / worker-thread boundary. Keep the short-lived PKCE
        # verifier map linearizable so a stale callback cannot observe or revive
        # a state that disconnect/cancel already invalidated.
        self._pending_lock = threading.RLock()
        self._access_token = ""
        self._access_token_expires_at = 0.0
        # A refresh and disconnect must not race over the persisted delta cursor
        # or Windows Credential Manager token. Read-only cached event queries do
        # not take this lock.
        self._mutation_lock = asyncio.Lock()
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
                    last_error TEXT NOT NULL DEFAULT '',
                    account_json TEXT NOT NULL DEFAULT '{}'
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
                    location TEXT NOT NULL DEFAULT '',
                    is_all_day INTEGER NOT NULL DEFAULT 0,
                    is_cancelled INTEGER NOT NULL DEFAULT 0,
                    etag TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outlook_events_start ON outlook_calendar_events(start_at);
            """)
            state_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(outlook_calendar_state)")
            }
            if "account_json" not in state_columns:
                conn.execute(
                    "ALTER TABLE outlook_calendar_state ADD COLUMN account_json TEXT NOT NULL DEFAULT '{}'"
                )
            event_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(outlook_calendar_events)")
            }
            for name, declaration in (
                ("location", "TEXT NOT NULL DEFAULT ''"),
                ("is_all_day", "INTEGER NOT NULL DEFAULT 0"),
                ("etag", "TEXT NOT NULL DEFAULT ''"),
                ("synced_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in event_columns:
                    conn.execute(
                        f"ALTER TABLE outlook_calendar_events ADD COLUMN {name} {declaration}"
                    )
            conn.commit()

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @property
    def authorization_pending(self) -> bool:
        with self._pending_lock:
            self._prune_pending_locked()
            return bool(self._pending)

    def _prune_pending_locked(self, now: float | None = None) -> None:
        """Drop expired PKCE states while ``_pending_lock`` is held."""
        deadline = time.monotonic() if now is None else now
        expired = [
            key
            for key, pending in self._pending.items()
            if pending[2] < deadline and key not in self._claimed_pending
        ]
        for key in expired:
            self._pending.pop(key, None)

    def _pending_state(self, state: str) -> tuple[str, str, float] | None:
        with self._pending_lock:
            self._prune_pending_locked()
            return self._pending.get(state)

    def _remove_pending_state(self, state: str, *, force: bool = False) -> bool:
        with self._pending_lock:
            if state in self._claimed_pending and not force:
                return False
            removed = self._pending.pop(state, None) is not None
            self._claimed_pending.discard(state)
            return removed

    def _claim_pending_state(
        self, state: str, expected: tuple[str, str, float]
    ) -> bool:
        """Atomically claim a still-current PKCE state after code exchange.

        Claimed states remain visible as pending until the account-cache reset
        commits, but cancellation can no longer invalidate them. This preserves
        the read guard while giving connect/cancel one linearization point.
        """
        with self._pending_lock:
            self._prune_pending_locked()
            if self._pending.get(state) != expected:
                return False
            self._claimed_pending.add(state)
            return True

    @staticmethod
    @contextmanager
    def _read_snapshot():
        """Read related calendar rows from one SQLite WAL snapshot.

        The database helper reuses a thread-local connection. A caller can
        already own a transaction, so only begin/roll back a read transaction
        when this method owns it. The explicit ``BEGIN`` is important: without
        it, two SELECT statements may observe opposite sides of an account
        switch, sync, or disconnect transaction.
        """
        conn = db._get_connection()
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN")
        try:
            yield conn
        finally:
            if owns_transaction and conn.in_transaction:
                conn.rollback()

    def redirect_uri(self) -> str:
        port = max(1, min(65535, int(os.getenv("SCRIBER_WEB_PORT", "8765") or 8765)))
        return f"http://localhost:{port}/api/calendar/outlook/callback"

    def begin_connect(self, *, open_browser: bool = True) -> dict[str, Any]:
        if not self.configured:
            raise ValueError("SCRIBER_OUTLOOK_CLIENT_ID is not configured for this build.")
        now = time.monotonic()
        with self._pending_lock:
            self._prune_pending_locked(now)
            reusable = next(
                (
                    (pending_state, pending)
                    for pending_state, pending in self._pending.items()
                    if pending_state not in self._claimed_pending
                ),
                None,
            )
            if reusable is not None:
                state, (verifier, redirect_uri, expires_at) = reusable
                reused = True
            elif self._claimed_pending:
                raise ValueError(
                    "Outlook sign-in is being completed. Return to Scriber in a moment."
                )
            else:
                verifier, _challenge = create_pkce_pair()
                state = secrets.token_urlsafe(32)
                redirect_uri = self.redirect_uri()
                expires_at = now + 600.0
                self._pending[state] = (verifier, redirect_uri, expires_at)
                reused = False
        # Reconstructing the S256 challenge makes a repeated Connect click
        # reopen the one active browser flow instead of creating another OAuth
        # state that could keep Settings stuck in "authorization pending" after
        # the user completed the first tab.
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
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
        return {
            "authorizationUrl": url,
            "expiresIn": max(1, int(expires_at - now)),
            "redirectUri": redirect_uri,
            "reused": reused,
        }

    async def complete_connect(self, state: str, code: str) -> None:
        pending = self._pending_state(state)
        if pending is None or not code:
            raise ValueError("Outlook authorization state is invalid or expired.")
        try:
            async with self._mutation_lock:
                # Disconnect or cancel may have run while this callback waited
                # for the mutation lane. Re-read the exact tuple under the
                # thread lock; never continue with a stale captured verifier.
                current = self._pending_state(state)
                if current is None or current != pending:
                    raise ValueError("Outlook authorization state is invalid or expired.")
                verifier, redirect_uri, _ = current
                response = await asyncio.to_thread(
                    self.shell_call,
                    "outlookAuthorizationCodeExchange",
                    {"clientId": self.client_id, "code": code, "codeVerifier": verifier,
                     "redirectUri": redirect_uri},
                    timeout_seconds=25.0,
                )
                if not response.get("success"):
                    raise ValueError(str(response.get("fallbackReason") or "Outlook authorization failed."))
                # The shell exchange can take up to 25 seconds. Cancellation is
                # allowed to invalidate the PKCE state during that await. Claim
                # it atomically only after the shell returns; if cancellation
                # won, remove the newly stored credential rather than reviving
                # a connection from a stale worker result.
                if not self._claim_pending_state(state, current):
                    self._access_token = ""
                    self._access_token_expires_at = 0.0
                    cleanup = await asyncio.to_thread(
                        self.shell_call,
                        "outlookCredentialDelete",
                        {},
                        timeout_seconds=3.0,
                    )
                    if not cleanup.get("success"):
                        raise ValueError(
                            "Outlook authorization was canceled and credential cleanup failed."
                        )
                    raise ValueError(
                        "Outlook authorization state is invalid or expired."
                    )
                self._remember_access_token(response.get("payload", {}))
                # Treat every successful authorization as a potential account
                # switch. The pending flag stays set until this transaction
                # commits, so event reads cannot expose old attendees under the
                # newly stored Windows credential.
                try:
                    with db._get_connection() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute("DELETE FROM outlook_calendar_events")
                        conn.execute(
                            """UPDATE outlook_calendar_state SET delta_link='',window_start='',
                               window_end='',last_sync_at='',last_error='',account_json='{}'
                               WHERE id=1"""
                        )
                        conn.commit()
                except Exception:
                    self._access_token = ""
                    self._access_token_expires_at = 0.0
                    await asyncio.to_thread(
                        self.shell_call,
                        "outlookCredentialDelete",
                        {},
                        timeout_seconds=3.0,
                    )
                    raise
        finally:
            self._remove_pending_state(state, force=True)

    def cancel_connect(self, state: str) -> None:
        if state and self._remove_pending_state(state):
            with db._get_connection() as conn:
                # Canceling a reconnect does not make Microsoft's rejected
                # refresh token valid again. The conditional write is atomic so
                # a concurrent sync cannot set reauthorization-required between
                # a separate read and this cancellation update.
                conn.execute(
                    """UPDATE outlook_calendar_state SET last_error='AuthorizationCanceled'
                       WHERE id=1 AND COALESCE(last_error,'')<>?""",
                    (REAUTHORIZATION_REQUIRED_ERROR,),
                )
                conn.commit()

    def _remember_access_token(self, payload: Any) -> str:
        if not isinstance(payload, dict) or not str(payload.get("accessToken", "")):
            raise ValueError("Outlook access token was not returned by the private shell.")
        self._access_token = str(payload["accessToken"])
        self._access_token_expires_at = time.monotonic() + max(30, int(payload.get("expiresIn", 0)) - 60)
        return self._access_token

    @staticmethod
    def _token_failure_requires_reauthorization(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        reason = str(response.get("fallbackReason") or "").casefold()
        # Microsoft documents invalid_grant as the refresh-token rejection for
        # revoked/expired credentials. interaction_required is also terminal
        # for the non-interactive desktop refresh flow. A corrupt credential
        # can only be repaired by replacing it through the PKCE flow.
        return any(
            marker in reason
            for marker in (
                "(invalid_grant)",
                "(interaction_required)",
                "stored outlook credential is invalid",
            )
        )

    def _reauthorization_required(self) -> OutlookReauthorizationRequired:
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self.record_sync_error(REAUTHORIZATION_REQUIRED_ERROR)
        return OutlookReauthorizationRequired()

    async def acquire_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token
        response = await asyncio.to_thread(
            self.shell_call, "outlookTokenAcquire", {"clientId": self.client_id},
            timeout_seconds=25.0,
        )
        if not response.get("success"):
            if self._token_failure_requires_reauthorization(response):
                raise self._reauthorization_required()
            raise ValueError(str(response.get("fallbackReason") or "Outlook token refresh failed."))
        return self._remember_access_token(response.get("payload", {}))

    @staticmethod
    def _validate_graph_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com":
            raise ValueError("Microsoft Graph pagination returned an invalid endpoint.")

    async def _graph_json(
        self,
        session: ClientSession,
        url: str,
        token: str,
        *,
        delta_request: bool = False,
    ) -> dict[str, Any]:
        self._validate_graph_url(url)
        async with session.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": 'outlook.timezone="UTC"',
            },
            allow_redirects=False,
        ) as response:
            if response.status == 401:
                raise _GraphUnauthorized()
            if delta_request and response.status == 410:
                # Graph explicitly expires delta tokens. The caller retries once
                # from a fresh bounded calendarView snapshot.
                raise _GraphDeltaExpired()
            if response.status != 200:
                raise ValueError(
                    f"Microsoft Graph request failed (HTTP {response.status})."
                )
            payload = await response.json()
        if not isinstance(payload, dict):
            raise ValueError("Microsoft Graph returned an invalid JSON object.")
        return payload

    @staticmethod
    def _account_payload(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        aliases = []
        for candidate in (value.get("mail"), value.get("userPrincipalName")):
            address = str(candidate or "").strip().lower()
            if (
                address
                and len(address) <= 320
                and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address)
                and address not in aliases
            ):
                aliases.append(address)
        if not aliases:
            return None
        return {
            "name": str(value.get("displayName") or "").strip()[:200],
            "address": aliases[0],
            "aliases": aliases,
        }

    async def _sync_with_token(
        self,
        session: ClientSession,
        token: str,
        *,
        force_reseed: bool = False,
    ) -> int:
        row = db._get_connection().execute("SELECT * FROM outlook_calendar_state WHERE id=1").fetchone()
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(days=1), now + timedelta(days=30)
        reseeding = force_reseed or _delta_window_needs_reseed(
            str(row["window_end"] or ""), now
        )
        url = "" if reseeding else str(row["delta_link"] or "")
        if not url:
            url = GRAPH_DELTA + "?" + urlencode({
                "startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
            })

        account_response = await self._graph_json(
            session,
            GRAPH_ME + "?" + urlencode(
                {"$select": "id,displayName,mail,userPrincipalName"}
            ),
            token,
        )
        account = self._account_payload(account_response)

        # Stage every page before mutating SQLite. A timeout on page N must not
        # leave a partly refreshed attendee list or advance the delta cursor.
        changed = 0
        delta_link = ""
        snapshot_ids: set[str] = set()
        changes: list[dict[str, Any]] = []
        visited_urls: set[str] = set()
        while url:
            if url in visited_urls or len(visited_urls) >= 100:
                raise ValueError("Microsoft Graph calendar pagination did not terminate.")
            visited_urls.add(url)
            payload = await self._graph_json(
                session, url, token, delta_request=True
            )
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise ValueError("Microsoft Graph calendar payload is invalid.")
            for event in values:
                if isinstance(event, dict) and event.get("id"):
                    changes.append(event)
                    if "@removed" not in event:
                        snapshot_ids.add(str(event["id"]))
            url = str(payload.get("@odata.nextLink") or "")
            delta_link = str(payload.get("@odata.deltaLink") or delta_link)

        if not delta_link or len(delta_link) > 16_384:
            raise ValueError("Microsoft Graph calendar sync did not return a delta cursor.")
        self._validate_graph_url(delta_link)

        with db._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for event in changes:
                event_id = str(event["id"])
                if "@removed" in event:
                    conn.execute(
                        "DELETE FROM outlook_calendar_events WHERE id=?", (event_id,)
                    )
                    changed += 1
                    continue
                start_at = _graph_datetime_utc(event.get("start"))
                end_at = _graph_datetime_utc(event.get("end"))
                if not start_at or not end_at:
                    continue
                online = (
                    event.get("onlineMeeting")
                    if isinstance(event.get("onlineMeeting"), dict)
                    else {}
                )
                location = (
                    event.get("location")
                    if isinstance(event.get("location"), dict)
                    else {}
                )
                conn.execute(
                    """INSERT INTO outlook_calendar_events
                       (id,subject,start_at,end_at,organizer_json,attendees_json,
                        join_url,location,is_all_day,is_cancelled,etag,synced_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       subject=excluded.subject,start_at=excluded.start_at,end_at=excluded.end_at,
                       organizer_json=excluded.organizer_json,attendees_json=excluded.attendees_json,
                       join_url=excluded.join_url,location=excluded.location,
                       is_all_day=excluded.is_all_day,is_cancelled=excluded.is_cancelled,
                       etag=excluded.etag,synced_at=excluded.synced_at,
                       updated_at=excluded.updated_at""",
                    (
                        event_id,
                        str(event.get("subject", ""))[:500],
                        start_at,
                        end_at,
                        json.dumps(event.get("organizer") or {}, ensure_ascii=False),
                        json.dumps(event.get("attendees") or [], ensure_ascii=False),
                        str(
                            online.get("joinUrl")
                            or event.get("onlineMeetingUrl")
                            or ""
                        )[:2048],
                        str(location.get("displayName") or "")[:500],
                        int(bool(event.get("isAllDay"))),
                        int(bool(event.get("isCancelled"))),
                        str(event.get("@odata.etag") or "")[:500],
                        now.isoformat(),
                        str(event.get("lastModifiedDateTime") or now.isoformat()),
                    ),
                )
                changed += 1
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
                   last_sync_at=?,last_error='',account_json=? WHERE id=1""",
                (
                    delta_link,
                    start.isoformat(),
                    end.isoformat(),
                    now.isoformat(),
                    json.dumps(account or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        return changed

    async def sync(self, session: ClientSession) -> int:
        # Bound the entire operation as well as each aiohttp request. A long
        # pagination chain must not monopolize the mutation lane indefinitely
        # and block a user's explicit refresh or disconnect action.
        async with asyncio.timeout(GRAPH_SYNC_TIMEOUT_SECONDS):
            async with self._mutation_lock:
                force_reseed = False
                retried_unauthorized = False
                retried_expired_delta = False
                while True:
                    token = await self.acquire_access_token()
                    try:
                        return await self._sync_with_token(
                            session, token, force_reseed=force_reseed
                        )
                    except _GraphUnauthorized:
                        if retried_unauthorized:
                            raise self._reauthorization_required()
                        retried_unauthorized = True
                        self._access_token = ""
                        self._access_token_expires_at = 0.0
                    except _GraphDeltaExpired:
                        if retried_expired_delta:
                            raise ValueError("Microsoft Graph delta cursor could not be renewed.")
                        retried_expired_delta = True
                        force_reseed = True

    async def disconnect(self) -> None:
        async with self._mutation_lock:
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
            with self._pending_lock:
                self._pending.clear()
                self._claimed_pending.clear()
            with db._get_connection() as conn:
                conn.execute("DELETE FROM outlook_calendar_events")
                conn.execute(
                    """UPDATE outlook_calendar_state SET delta_link='',window_start='',
                       window_end='',last_sync_at='',last_error='',account_json='{}'
                       WHERE id=1"""
                )
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
        credential_status_available = True
        try:
            shell = await asyncio.to_thread(
                self.shell_call, "outlookCredentialStatus", {}, timeout_seconds=2.0
            )
        except Exception:
            shell = {}
            credential_status_available = False
        credential_stored = bool(
            shell.get("success")
            and isinstance(shell.get("payload"), dict)
            and shell["payload"].get("credentialStored")
        )
        with self._pending_lock:
            self._prune_pending_locked()
            authorization_pending = bool(self._pending)
            with self._read_snapshot() as conn:
                state = conn.execute(
                    "SELECT * FROM outlook_calendar_state WHERE id=1"
                ).fetchone()
                account = self._load_json(state["account_json"], {})
                if not isinstance(account, dict) or not account.get("address"):
                    account = None
                if authorization_pending:
                    account = None
                    next_event = None
                else:
                    next_event = conn.execute(
                        """SELECT *
                           FROM outlook_calendar_events
                           WHERE is_cancelled=0 AND end_at>=? ORDER BY start_at LIMIT 1""",
                        (datetime.now(timezone.utc).isoformat(),),
                    ).fetchone()
        reauthorization_required = (
            str(state["last_error"] or "") == REAUTHORIZATION_REQUIRED_ERROR
        )
        return {
            "configured": self.configured,
            # Credential Manager only proves that a refresh token exists. A
            # rejected token must not keep presenting a misleading Connected
            # state while the last good local calendar snapshot remains usable.
            "connected": credential_stored and not reauthorization_required,
            "credentialStatusAvailable": credential_status_available,
            "authorizationPending": authorization_pending,
            "reauthRequired": reauthorization_required,
            "scopes": SCOPES.split(),
            "lastSyncAt": "" if authorization_pending else state["last_sync_at"],
            "lastError": state["last_error"],
            "account": account,
            "nextEvent": (
                self._calendar_event_payload(next_event, account=account)
                if next_event
                else None
            ),
        }

    def current_event(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        now_value = now.isoformat()
        lower = (now - timedelta(minutes=15)).isoformat()
        upper = (now + timedelta(minutes=10)).isoformat()
        with self._pending_lock:
            self._prune_pending_locked()
            if self._pending:
                return None
            with self._read_snapshot() as conn:
                state = conn.execute(
                    "SELECT account_json FROM outlook_calendar_state WHERE id=1"
                ).fetchone()
                row = conn.execute(
                    """SELECT *
                       FROM outlook_calendar_events
                       WHERE is_cancelled=0 AND start_at<=? AND end_at>=?
                       ORDER BY
                         is_all_day ASC,
                         CASE
                           WHEN start_at<=? AND end_at>=? THEN 0
                           WHEN start_at>? THEN 1
                           ELSE 2
                         END ASC,
                         CASE WHEN start_at<=? AND end_at>=? THEN start_at END DESC,
                         CASE WHEN start_at>? THEN start_at END ASC,
                         CASE WHEN end_at<? THEN end_at END DESC,
                         id ASC
                       LIMIT 1""",
                    (
                        upper,
                        lower,
                        now_value,
                        now_value,
                        now_value,
                        now_value,
                        now_value,
                        now_value,
                        now_value,
                    ),
                ).fetchone()
        if row is None:
            return None
        account = self._load_json(state["account_json"], {}) if state else {}
        return self._calendar_event_payload(row, account=account)

    def event_snapshot(self, event_id: str) -> dict[str, Any] | None:
        """Return a fully normalized immutable selection source from local cache."""
        normalized_id = str(event_id or "").strip()
        if not normalized_id or len(normalized_id) > 2048:
            return None
        with self._pending_lock:
            self._prune_pending_locked()
            if self._pending:
                return None
            with self._read_snapshot() as conn:
                state = conn.execute(
                    "SELECT last_sync_at,account_json FROM outlook_calendar_state WHERE id=1"
                ).fetchone()
                row = conn.execute(
                    "SELECT * FROM outlook_calendar_events WHERE id=? AND is_cancelled=0",
                    (normalized_id,),
                ).fetchone()
        if row is None:
            return None
        account = self._load_json(state["account_json"], {}) if state else {}
        payload = self._calendar_event_payload(row, account=account)
        payload["calendarSyncedAt"] = str(state["last_sync_at"] or "") if state else ""
        payload["snapshotCreatedAt"] = datetime.now(timezone.utc).isoformat()
        return payload

    @staticmethod
    def _resolve_day_window(
        day_value: str = "",
        time_zone_name: str = "",
        start_value: str = "",
        end_value: str = "",
    ) -> tuple[date_type, str, datetime, datetime]:
        requested_zone = str(time_zone_name or "").strip()
        if requested_zone and (
            len(requested_zone) > 100
            or not re.fullmatch(r"[A-Za-z0-9._+\-/]+", requested_zone)
        ):
            raise ValueError("timeZone is invalid.")
        zone_label = requested_zone

        requested_day = str(day_value or "").strip()
        if requested_day:
            try:
                day = date_type.fromisoformat(requested_day)
            except ValueError as exc:
                raise ValueError("date must use YYYY-MM-DD.") from exc
            if day.isoformat() != requested_day:
                raise ValueError("date must use YYYY-MM-DD.")
        start_raw = str(start_value or "").strip()
        end_raw = str(end_value or "").strip()
        if bool(start_raw) != bool(end_raw):
            raise ValueError("start and end must be provided together.")
        if start_raw:
            def parse_boundary(raw: str) -> datetime:
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("start and end must be ISO-8601 timestamps.") from exc
                if parsed.tzinfo is None:
                    raise ValueError("start and end must include a UTC offset.")
                return parsed.astimezone(timezone.utc)

            start_utc = parse_boundary(start_raw)
            end_utc = parse_boundary(end_raw)
            duration = end_utc - start_utc
            if not timedelta(hours=20) <= duration <= timedelta(hours=28):
                raise ValueError("Calendar day boundaries must span 20 to 28 hours.")
            if not zone_label:
                zone_label = "local"
            if not requested_day:
                day = start_utc.date()
            return day, zone_label, start_utc, end_utc

        # Browser callers send their DST-correct local-midnight UTC instants.
        # Never let development-machine tzdata make an explicit request behave
        # differently from the frozen Windows sidecar, which excludes tzdata.
        if requested_day or requested_zone:
            raise ValueError(
                "start and end are required when date or timeZone is provided."
            )
        # Parameterless callers retain a best-effort local-today fallback.
        zone = datetime.now().astimezone().tzinfo or timezone.utc
        day = datetime.now(zone).date()
        zone_label = zone_label or getattr(zone, "key", None) or str(zone)
        start_local = datetime.combine(day, time_type.min, tzinfo=zone)
        end_local = datetime.combine(day + timedelta(days=1), time_type.min, tzinfo=zone)
        return day, zone_label, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    def events_for_day(
        self,
        *,
        day_value: str = "",
        time_zone_name: str = "",
        start_value: str = "",
        end_value: str = "",
    ) -> dict[str, Any]:
        day, zone_label, start_utc, end_utc = self._resolve_day_window(
            day_value, time_zone_name, start_value, end_value
        )
        with self._pending_lock:
            self._prune_pending_locked()
            if self._pending:
                raise ValueError("Finish the Outlook sign-in before loading calendar events.")
            with self._read_snapshot() as conn:
                state = conn.execute(
                    "SELECT last_sync_at,account_json FROM outlook_calendar_state WHERE id=1"
                ).fetchone()
                rows = conn.execute(
                    """SELECT * FROM outlook_calendar_events
                       WHERE is_cancelled=0 AND start_at<? AND end_at>?
                       ORDER BY start_at,id LIMIT 501""",
                    (end_utc.isoformat(), start_utc.isoformat()),
                ).fetchall()
        account = self._load_json(state["account_json"], {}) if state else {}
        truncated = len(rows) > 500
        items = [
            self._calendar_event_payload(row, account=account) for row in rows[:500]
        ]
        return {
            "date": day.isoformat(),
            "timeZone": zone_label,
            "lastSyncAt": str(state["last_sync_at"] or "") if state else "",
            "account": account if isinstance(account, dict) and account.get("address") else None,
            "items": items,
            "truncated": truncated,
        }

    @staticmethod
    def _load_json(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value) if value else fallback
        except (TypeError, json.JSONDecodeError):
            return fallback

    @classmethod
    def _calendar_event_payload(
        cls, row: Any, *, account: Any = None
    ) -> dict[str, Any]:
        account_address = ""
        account_aliases: set[str] = set()
        if isinstance(account, dict):
            account_address = str(account.get("address") or "").strip().lower()
            raw_aliases = account.get("aliases")
            aliases = raw_aliases if isinstance(raw_aliases, list) else []
            for candidate in [account_address, *aliases]:
                normalized = str(candidate or "").strip().lower()
                if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", normalized):
                    account_aliases.add(normalized)

        def contact(
            value: Any, *, attendee: bool = False
        ) -> dict[str, Any] | None:
            if not isinstance(value, dict):
                return None
            email = value.get("emailAddress") if isinstance(value.get("emailAddress"), dict) else value
            address = str(email.get("address", "")).strip().lower()
            if (
                not address
                or len(address) > 320
                or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address)
            ):
                return None
            is_current_user = address in account_aliases
            participant_identity_address = (
                account_address if is_current_user and account_address else address
            )
            result: dict[str, Any] = {
                "participantId": hashlib.sha256(
                    f"{row['id']}\0{participant_identity_address}".encode("utf-8")
                ).hexdigest()[:20],
                "name": str(email.get("name", "")).strip()[:200],
                "address": address,
                "isCurrentUser": is_current_user,
            }
            if is_current_user:
                result["aliases"] = sorted(account_aliases)
            if attendee:
                attendee_type = str(value.get("type") or "required").strip().lower()
                if attendee_type not in {"required", "optional", "resource"}:
                    attendee_type = "required"
                status = value.get("status") if isinstance(value.get("status"), dict) else {}
                response = re.sub(
                    r"[^A-Za-z]", "", str(status.get("response") or "none")
                )[:40]
                result["type"] = attendee_type
                result["response"] = response or "none"
            return result

        organizer = contact(cls._load_json(row["organizer_json"], {}))
        participants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attendee in cls._load_json(row["attendees_json"], []):
            item = contact(attendee, attendee=True)
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
            "currentUser": (
                {
                    "participantId": hashlib.sha256(
                        f"{row['id']}\0{account_address}".encode("utf-8")
                    ).hexdigest()[:20],
                    "name": str(account.get("name") or "")[:200],
                    "address": account_address,
                    "aliases": sorted(account_aliases),
                    "isCurrentUser": True,
                }
                if account_address
                else None
            ),
            "location": row["location"],
            "isAllDay": bool(row["is_all_day"]),
            "etag": row["etag"],
            "lastModifiedAt": row["updated_at"],
            "syncedAt": row["synced_at"],
        }
