from __future__ import annotations

import asyncio
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from src import database
from src.outlook_calendar import (
    OutlookCalendarService,
    OutlookReauthorizationRequired,
    REAUTHORIZATION_REQUIRED_ERROR,
    SCOPES,
    _GraphUnauthorized,
    _delta_window_needs_reseed,
    _graph_datetime_utc,
    _normalize_public_client_id,
    create_pkce_pair,
)


@pytest.fixture()
def service(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "outlook.db")
    database.init_database()
    calls = []

    def shell_call(command, payload, **_kwargs):
        calls.append((command, payload))
        if command == "outlookCredentialStatus":
            return {"success": True, "payload": {"credentialStored": True}}
        return {"success": True, "payload": {"accessToken": "short-lived", "expiresIn": 3600}}

    value = OutlookCalendarService(shell_call, "11111111-1111-1111-1111-111111111111")
    yield value, calls
    database._close_all_connections()


def test_pkce_pair_uses_s256_compatible_entropy():
    verifier, challenge = create_pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert 43 <= len(challenge) <= 128
    assert verifier != challenge
    assert "=" not in verifier + challenge


def test_public_client_id_requires_a_non_nil_canonical_guid():
    assert _normalize_public_client_id("11111111-1111-4111-8111-111111111111") == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert _normalize_public_client_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA") == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    for invalid in (
        "",
        "not-a-guid",
        "{11111111-1111-4111-8111-111111111111}",
        "11111111111141118111111111111111",
        "00000000-0000-0000-0000-000000000000",
    ):
        assert _normalize_public_client_id(invalid) == ""


def test_account_identity_preserves_mail_and_upn_aliases():
    assert OutlookCalendarService._account_payload({
        "displayName": "Guest User",
        "mail": "guest@external.example",
        "userPrincipalName": "guest_external.example#ext#@tenant.example",
    }) == {
        "name": "Guest User",
        "address": "guest@external.example",
        "aliases": [
            "guest@external.example",
            "guest_external.example#ext#@tenant.example",
        ],
    }


def test_system_browser_redirect_uses_registered_localhost_path(service, monkeypatch):
    calendar, _calls = service
    monkeypatch.setenv("SCRIBER_WEB_PORT", "49152")

    assert calendar.redirect_uri() == (
        "http://localhost:49152/api/calendar/outlook/callback"
    )


@pytest.mark.asyncio
async def test_public_desktop_flow_requests_only_approved_scopes_and_exchanges_in_shell(service):
    calendar, calls = service
    started = calendar.begin_connect(open_browser=False)
    query = parse_qs(urlparse(started["authorizationUrl"]).query)
    assert query["scope"] == [SCOPES]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [calendar.redirect_uri()]

    await calendar.complete_connect(query["state"][0], "authorization-code")
    command, payload = calls[-1]
    assert command == "outlookAuthorizationCodeExchange"
    assert payload["code"] == "authorization-code"
    assert 43 <= len(payload["codeVerifier"]) <= 128
    assert "refreshToken" not in payload


@pytest.mark.asyncio
async def test_status_reads_credential_state_without_exposing_tokens(service):
    calendar, _calls = service
    status = await calendar.status()
    assert status["configured"] is True
    assert status["connected"] is True
    assert status["reauthRequired"] is False
    assert status["scopes"] == ["User.Read", "Calendars.Read", "offline_access"]
    assert "accessToken" not in status
    assert "refreshToken" not in status


@pytest.mark.asyncio
async def test_invalid_grant_requires_reauthorization_without_deleting_cached_events(
    monkeypatch, tmp_path
):
    from datetime import datetime, timedelta, timezone

    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "outlook-reauth.db")
    database.init_database()

    def shell_call(command, _payload, **_kwargs):
        if command == "outlookCredentialStatus":
            # Credential Manager can still contain a token after Microsoft has
            # revoked it, which is the misleading state this contract fixes.
            return {"success": True, "payload": {"credentialStored": True}}
        if command == "outlookTokenAcquire":
            return {
                "success": False,
                "errorCode": "outlookTokenAcquireFailed",
                "fallbackReason": (
                    "Outlook token endpoint rejected the request (invalid_grant)"
                ),
            }
        raise AssertionError(command)

    calendar = OutlookCalendarService(
        shell_call, "11111111-1111-1111-1111-111111111111"
    )
    now = datetime.now(timezone.utc)
    with database._get_connection() as conn:
        conn.execute(
            "UPDATE outlook_calendar_state SET last_sync_at=?,account_json=? WHERE id=1",
            (
                now.isoformat(),
                '{"name":"Alex","address":"alex@example.com"}',
            ),
        )
        conn.execute(
            """INSERT INTO outlook_calendar_events
               (id,subject,start_at,end_at,updated_at) VALUES (?,?,?,?,?)""",
            (
                "cached-event",
                "Cached planning",
                (now + timedelta(minutes=5)).isoformat(),
                (now + timedelta(hours=1)).isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()

    with pytest.raises(OutlookReauthorizationRequired, match="Reconnect Outlook"):
        await calendar.acquire_access_token()

    status = await calendar.status()
    assert status["connected"] is False
    assert status["reauthRequired"] is True
    assert status["lastError"] == REAUTHORIZATION_REQUIRED_ERROR
    assert "invalid_grant" not in status["lastError"]
    assert status["account"]["address"] == "alex@example.com"
    assert status["nextEvent"]["id"] == "cached-event"
    assert database._get_connection().execute(
        "SELECT COUNT(*) FROM outlook_calendar_events"
    ).fetchone()[0] == 1
    database._close_all_connections()


@pytest.mark.asyncio
async def test_two_graph_unauthorized_responses_require_reauthorization(
    service, monkeypatch
):
    calendar, _calls = service
    attempts = 0

    async def always_unauthorized(_session, _token, *, force_reseed=False):
        nonlocal attempts
        attempts += 1
        raise _GraphUnauthorized()

    monkeypatch.setattr(calendar, "_sync_with_token", always_unauthorized)

    with pytest.raises(OutlookReauthorizationRequired):
        await calendar.sync(object())

    assert attempts == 2
    status = await calendar.status()
    assert status["connected"] is False
    assert status["reauthRequired"] is True
    assert status["lastError"] == REAUTHORIZATION_REQUIRED_ERROR


@pytest.mark.asyncio
async def test_cancelled_reconnect_does_not_restore_a_false_connected_state(service):
    calendar, _calls = service
    calendar.record_sync_error(REAUTHORIZATION_REQUIRED_ERROR)
    authorization = calendar.begin_connect(open_browser=False)
    state = parse_qs(urlparse(authorization["authorizationUrl"]).query)["state"][0]

    calendar.cancel_connect(state)

    status = await calendar.status()
    assert status["connected"] is False
    assert status["reauthRequired"] is True
    assert status["lastError"] == REAUTHORIZATION_REQUIRED_ERROR


def test_graph_datetime_is_stored_as_offset_aware_utc():
    assert _graph_datetime_utc({"dateTime": "2026-10-25T01:30:00", "timeZone": "UTC"}) == "2026-10-25T01:30:00+00:00"
    assert _graph_datetime_utc({"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "UTC"}) == "2026-10-25T01:30:00+00:00"
    assert _graph_datetime_utc({"dateTime": "2026-07-12T09:00:00", "timeZone": "W. Europe Standard Time"}) == "2026-07-12T07:00:00+00:00"
    assert _graph_datetime_utc({"dateTime": "2026-01-12T09:00:00", "timeZone": "W. Europe Standard Time"}) == "2026-01-12T08:00:00+00:00"
    assert _graph_datetime_utc({"dateTime": "2026-10-25T09:00:00", "timeZone": "W. Europe Standard Time"}) == "2026-10-25T08:00:00+00:00"
    assert _graph_datetime_utc({"dateTime": "2026-10-25T01:30:00", "timeZone": "Unknown Windows Zone"}) == ""


def test_delta_window_reseeds_before_the_forward_horizon_expires():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    assert _delta_window_needs_reseed("", now)
    assert _delta_window_needs_reseed((now + timedelta(days=6)).isoformat(), now)
    assert not _delta_window_needs_reseed((now + timedelta(days=8)).isoformat(), now)


@pytest.mark.asyncio
async def test_disconnect_preserves_local_state_when_shell_deletion_fails(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "outlook-delete.db")
    database.init_database()

    def shell_call(command, _payload, **_kwargs):
        if command == "outlookCredentialDelete":
            return {"success": False, "fallbackReason": "credentialDeleteFailed"}
        return {"success": True, "payload": {"credentialStored": True}}

    calendar = OutlookCalendarService(shell_call, "client-id")
    calendar._access_token = "still-valid"
    with database._get_connection() as conn:
        conn.execute(
            "INSERT INTO outlook_calendar_events(id,start_at,end_at,updated_at) VALUES (?,?,?,?)",
            ("event-1", "2026-07-12T10:00:00+00:00", "2026-07-12T11:00:00+00:00", "now"),
        )
        conn.commit()

    with pytest.raises(ValueError, match="credentialDeleteFailed"):
        await calendar.disconnect()

    assert calendar._access_token == "still-valid"
    assert database._get_connection().execute("SELECT COUNT(*) FROM outlook_calendar_events").fetchone()[0] == 1
    database._close_all_connections()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent_and_clears_all_local_account_state(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "outlook-disconnect.db")
    database.init_database()
    credential_stored = True
    calls: list[str] = []

    def shell_call(command, _payload, **_kwargs):
        nonlocal credential_stored
        calls.append(command)
        if command == "outlookCredentialDelete":
            credential_stored = False
            return {
                "success": True,
                "payload": {"deleted": True, "credentialStored": False},
            }
        if command == "outlookCredentialStatus":
            return {
                "success": True,
                "payload": {"credentialStored": credential_stored},
            }
        raise AssertionError(command)

    calendar = OutlookCalendarService(
        shell_call, "11111111-1111-1111-1111-111111111111"
    )
    calendar._access_token = "access-token"
    calendar._access_token_expires_at = 99_999_999.0
    calendar.begin_connect(open_browser=False)
    with database._get_connection() as conn:
        conn.execute(
            """UPDATE outlook_calendar_state SET delta_link=?,window_start=?,
               window_end=?,last_sync_at=?,last_error=?,account_json=? WHERE id=1""",
            (
                "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=old",
                "2026-07-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                "2026-07-14T08:00:00+00:00",
                "OldError",
                '{"name":"Alex","address":"alex@example.com"}',
            ),
        )
        conn.execute(
            """INSERT INTO outlook_calendar_events
               (id,start_at,end_at,updated_at) VALUES (?,?,?,?)""",
            (
                "event-1",
                "2026-07-14T10:00:00+00:00",
                "2026-07-14T11:00:00+00:00",
                "now",
            ),
        )
        conn.commit()

    # Repeating disconnect is intentionally safe: Windows Credential Manager
    # deletion is idempotent and local state remains empty.
    await calendar.disconnect()
    await calendar.disconnect()

    assert calls.count("outlookCredentialDelete") == 2
    assert calendar._access_token == ""
    assert calendar._access_token_expires_at == 0.0
    assert calendar.authorization_pending is False
    conn = database._get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM outlook_calendar_events"
    ).fetchone()[0] == 0
    state = conn.execute(
        "SELECT * FROM outlook_calendar_state WHERE id=1"
    ).fetchone()
    assert state["delta_link"] == ""
    assert state["window_start"] == ""
    assert state["window_end"] == ""
    assert state["last_sync_at"] == ""
    assert state["last_error"] == ""
    assert state["account_json"] == "{}"
    status = await calendar.status()
    assert status["connected"] is False
    assert status["account"] is None
    assert status["nextEvent"] is None
    assert calendar.events_for_day(
        day_value="2026-07-14",
        time_zone_name="Europe/Berlin",
        start_value="2026-07-13T22:00:00Z",
        end_value="2026-07-14T22:00:00Z",
    )["items"] == []
    database._close_all_connections()


@pytest.mark.asyncio
async def test_callback_waiting_for_mutation_lane_revalidates_canceled_state(service):
    calendar, calls = service
    started = calendar.begin_connect(open_browser=False)
    state = parse_qs(urlparse(started["authorizationUrl"]).query)["state"][0]

    async with calendar._mutation_lock:
        completion = asyncio.create_task(
            calendar.complete_connect(state, "authorization-code")
        )
        await asyncio.sleep(0)
        calendar.cancel_connect(state)

    with pytest.raises(ValueError, match="invalid or expired"):
        await completion
    assert all(command != "outlookAuthorizationCodeExchange" for command, _ in calls)


@pytest.mark.asyncio
async def test_cancel_during_shell_exchange_deletes_the_stale_new_credential(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "outlook-cancel-race.db")
    database.init_database()
    exchange_entered = threading.Event()
    exchange_release = threading.Event()
    credential_stored = False
    calls: list[str] = []

    def shell_call(command, _payload, **_kwargs):
        nonlocal credential_stored
        calls.append(command)
        if command == "outlookAuthorizationCodeExchange":
            credential_stored = True
            exchange_entered.set()
            assert exchange_release.wait(timeout=2.0)
            return {
                "success": True,
                "payload": {"accessToken": "stale-token", "expiresIn": 3600},
            }
        if command == "outlookCredentialDelete":
            credential_stored = False
            return {
                "success": True,
                "payload": {"deleted": True, "credentialStored": False},
            }
        raise AssertionError(command)

    calendar = OutlookCalendarService(
        shell_call, "11111111-1111-1111-1111-111111111111"
    )
    started = calendar.begin_connect(open_browser=False)
    state = parse_qs(urlparse(started["authorizationUrl"]).query)["state"][0]
    completion = asyncio.create_task(
        calendar.complete_connect(state, "authorization-code")
    )
    assert await asyncio.to_thread(exchange_entered.wait, 1.0)
    calendar.cancel_connect(state)
    exchange_release.set()

    with pytest.raises(ValueError, match="invalid or expired"):
        await completion
    assert calls == ["outlookAuthorizationCodeExchange", "outlookCredentialDelete"]
    assert credential_stored is False
    assert calendar._access_token == ""
    assert calendar.authorization_pending is False
    database._close_all_connections()


@pytest.mark.asyncio
async def test_repeated_connect_reopens_one_authorization_state(service):
    calendar, _calls = service
    first = calendar.begin_connect(open_browser=False)
    second = calendar.begin_connect(open_browser=False)
    first_state = parse_qs(urlparse(first["authorizationUrl"]).query)["state"][0]
    second_state = parse_qs(urlparse(second["authorizationUrl"]).query)["state"][0]
    assert first_state == second_state
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["authorizationUrl"] == first["authorizationUrl"]
    assert (await calendar.status())["authorizationPending"] is True

    await calendar.complete_connect(first_state, "authorization-code")
    assert (await calendar.status())["authorizationPending"] is False


@pytest.mark.asyncio
async def test_pending_authorization_hides_previous_account_and_events(service):
    calendar, _calls = service
    with database._get_connection() as conn:
        conn.execute(
            "UPDATE outlook_calendar_state SET last_sync_at=?,account_json=? WHERE id=1",
            ("2026-07-12T08:00:00+00:00", '{"name":"Old","address":"old@example.com"}'),
        )
        conn.execute(
            "INSERT INTO outlook_calendar_events(id,start_at,end_at,updated_at) VALUES (?,?,?,?)",
            ("old", "2026-07-12T09:00:00+00:00", "2026-07-12T10:00:00+00:00", "now"),
        )
        conn.commit()

    calendar.begin_connect(open_browser=False)
    status = await calendar.status()
    assert status["authorizationPending"] is True
    assert status["account"] is None
    assert status["nextEvent"] is None
    assert status["lastSyncAt"] == ""
    assert calendar.current_event() is None
    assert calendar.event_snapshot("old") is None
    with pytest.raises(ValueError, match="Finish the Outlook sign-in"):
        calendar.events_for_day()


@pytest.mark.asyncio
async def test_expired_delta_window_reseeds_and_reconciles_cache(service):
    from datetime import datetime, timedelta, timezone

    calendar, _calls = service
    now = datetime.now(timezone.utc)
    with database._get_connection() as conn:
        conn.execute(
            "UPDATE outlook_calendar_state SET delta_link=?,window_end=? WHERE id=1",
            ("https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=old", (now - timedelta(days=1)).isoformat()),
        )
        conn.execute(
            "INSERT INTO outlook_calendar_events(id,start_at,end_at,updated_at) VALUES (?,?,?,?)",
            ("stale", now.isoformat(), (now + timedelta(hours=1)).isoformat(), now.isoformat()),
        )
        conn.commit()

    event_start = now + timedelta(days=20)
    payload = {
        "value": [{
            "id": "future", "subject": "Future planning",
            "start": {"dateTime": event_start.replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": (event_start + timedelta(hours=1)).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        }],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=new",
    }

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return payload

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            if "/v1.0/me?" in url:
                return ResponseWithPayload({
                    "displayName": "Alex Example",
                    "mail": "alex@example.com",
                    "userPrincipalName": "alex@example.com",
                })
            return Response()

    class ResponseWithPayload(Response):
        def __init__(self, value):
            self.value = value

        async def json(self):
            return self.value

    session = Session()
    assert await calendar.sync(session) == 1
    assert len(session.urls) == 2
    delta_url = next(url for url in session.urls if "/calendarView/delta?" in url)
    assert "$deltatoken=old" not in delta_url
    assert "startDateTime=" in delta_url
    assert "%24select=" not in delta_url
    rows = database._get_connection().execute(
        "SELECT id,start_at,end_at FROM outlook_calendar_events ORDER BY id"
    ).fetchall()
    assert [row["id"] for row in rows] == ["future"]
    assert rows[0]["start_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_new_authorization_clears_old_account_calendar_before_first_sync(service):
    calendar, _calls = service
    with database._get_connection() as conn:
        conn.execute(
            """INSERT INTO outlook_calendar_events
               (id,start_at,end_at,updated_at) VALUES (?,?,?,?)""",
            ("old-account-event", "2026-07-12T10:00:00+00:00", "2026-07-12T11:00:00+00:00", "now"),
        )
        conn.execute(
            """UPDATE outlook_calendar_state SET delta_link=?,window_start=?,
               window_end=?,last_error=?,account_json=? WHERE id=1""",
            (
                "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=old",
                "2026-07-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                REAUTHORIZATION_REQUIRED_ERROR,
                '{"name":"Old User","address":"old@example.com"}',
            ),
        )
        conn.commit()

    started = calendar.begin_connect(open_browser=False)
    state = parse_qs(urlparse(started["authorizationUrl"]).query)["state"][0]
    await calendar.complete_connect(state, "authorization-code")

    conn = database._get_connection()
    assert conn.execute("SELECT COUNT(*) FROM outlook_calendar_events").fetchone()[0] == 0
    reset = conn.execute("SELECT * FROM outlook_calendar_state WHERE id=1").fetchone()
    assert reset["delta_link"] == ""
    assert reset["last_error"] == ""
    assert reset["account_json"] == "{}"
    status = await calendar.status()
    assert status["connected"] is True
    assert status["reauthRequired"] is False


def test_day_events_preserve_attendee_context_and_signed_in_identity(service):
    calendar, _calls = service
    with database._get_connection() as conn:
        conn.execute(
            "UPDATE outlook_calendar_state SET last_sync_at=?,account_json=? WHERE id=1",
            (
                "2026-07-12T08:00:00+00:00",
                '{"name":"Alex Example","address":"alex@example.com"}',
            ),
        )
        conn.execute(
            """INSERT INTO outlook_calendar_events
               (id,subject,start_at,end_at,organizer_json,attendees_json,join_url,
                location,is_all_day,is_cancelled,etag,synced_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "event-berlin",
                "Daily planning",
                "2026-07-11T22:30:00+00:00",
                "2026-07-11T23:30:00+00:00",
                '{"emailAddress":{"name":"Organizer","address":"owner@example.com"}}',
                """[
                  {"emailAddress":{"name":"Alex Example","address":"alex@example.com"},
                   "type":"required","status":{"response":"accepted"}},
                  {"emailAddress":{"name":"Board room","address":"room@example.com"},
                   "type":"resource","status":{"response":"accepted"}},
                  {"emailAddress":{"name":"No thanks","address":"declined@example.com"},
                   "type":"optional","status":{"response":"declined"}}
                ]""",
                "https://teams.example/join",
                "Online",
                0,
                0,
                'W/"etag-1"',
                "2026-07-12T08:00:00+00:00",
                "2026-07-12T07:55:00+00:00",
            ),
        )
        conn.commit()

    payload = calendar.events_for_day(
        day_value="2026-07-12",
        time_zone_name="Europe/Berlin",
        start_value="2026-07-11T22:00:00.000Z",
        end_value="2026-07-12T22:00:00.000Z",
    )
    assert payload["date"] == "2026-07-12"
    assert payload["timeZone"] == "Europe/Berlin"
    assert payload["account"] == {
        "name": "Alex Example",
        "address": "alex@example.com",
    }
    assert len(payload["items"]) == 1
    event = payload["items"][0]
    assert event["etag"] == 'W/"etag-1"'
    assert event["organizer"]["address"] == "owner@example.com"
    assert event["currentUser"]["participantId"]
    assert event["participants"][0]["isCurrentUser"] is True
    assert event["participants"][1]["type"] == "resource"
    assert event["participants"][2]["response"] == "declined"
    assert all(item["participantId"] for item in event["participants"])


def test_day_events_reject_invalid_date_and_timezone(service):
    calendar, _calls = service
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        calendar.events_for_day(
            day_value="12.07.2026",
            time_zone_name="Europe/Berlin",
            start_value="2026-07-11T22:00:00Z",
            end_value="2026-07-12T22:00:00Z",
        )
    with pytest.raises(ValueError, match="timeZone"):
        calendar.events_for_day(
            day_value="2026-07-12",
            time_zone_name="bad zone!",
            start_value="2026-07-11T22:00:00Z",
            end_value="2026-07-12T22:00:00Z",
        )


def test_browser_supplied_day_boundaries_preserve_dst_without_tzdata(service):
    calendar, _calls = service
    day, zone, start, end = calendar._resolve_day_window(
        "2026-03-29",
        "Europe/Berlin",
        "2026-03-28T23:00:00Z",
        "2026-03-29T22:00:00Z",
    )
    assert day.isoformat() == "2026-03-29"
    assert zone == "Europe/Berlin"
    assert (end - start).total_seconds() == 23 * 60 * 60


def test_current_event_prefers_active_then_upcoming_then_recent_over_all_day(service):
    from datetime import datetime, timedelta, timezone

    calendar, _calls = service
    now = datetime.now(timezone.utc)
    rows = [
        (
            "all-day",
            now - timedelta(hours=8),
            now + timedelta(hours=8),
            1,
        ),
        (
            "recently-ended",
            now - timedelta(minutes=45),
            now - timedelta(minutes=5),
            0,
        ),
        (
            "upcoming",
            now + timedelta(minutes=5),
            now + timedelta(minutes=35),
            0,
        ),
        (
            "active",
            now - timedelta(minutes=5),
            now + timedelta(minutes=25),
            0,
        ),
    ]
    with database._get_connection() as conn:
        conn.executemany(
            """INSERT INTO outlook_calendar_events
               (id,subject,start_at,end_at,is_all_day,updated_at)
               VALUES (?,?,?,?,?,?)""",
            [
                (event_id, event_id, start.isoformat(), end.isoformat(), all_day, now.isoformat())
                for event_id, start, end, all_day in rows
            ],
        )
        conn.commit()

    assert calendar.current_event()["id"] == "active"
    with database._get_connection() as conn:
        conn.execute("DELETE FROM outlook_calendar_events WHERE id='active'")
        conn.commit()
    assert calendar.current_event()["id"] == "upcoming"
    with database._get_connection() as conn:
        conn.execute("DELETE FROM outlook_calendar_events WHERE id='upcoming'")
        conn.commit()
    assert calendar.current_event()["id"] == "recently-ended"
    with database._get_connection() as conn:
        conn.execute("DELETE FROM outlook_calendar_events WHERE id='recently-ended'")
        conn.commit()
    assert calendar.current_event()["id"] == "all-day"
