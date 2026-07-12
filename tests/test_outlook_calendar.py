from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from src import database
from src.outlook_calendar import (
    OutlookCalendarService,
    SCOPES,
    _delta_window_needs_reseed,
    _graph_datetime_utc,
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
    assert status["scopes"] == ["User.Read", "Calendars.Read", "offline_access"]
    assert "accessToken" not in status
    assert "refreshToken" not in status


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
async def test_repeated_connect_does_not_invalidate_first_state_and_cancel_is_terminal(service):
    calendar, _calls = service
    first = calendar.begin_connect(open_browser=False)
    second = calendar.begin_connect(open_browser=False)
    first_state = parse_qs(urlparse(first["authorizationUrl"]).query)["state"][0]
    second_state = parse_qs(urlparse(second["authorizationUrl"]).query)["state"][0]
    assert first_state != second_state
    assert (await calendar.status())["authorizationPending"] is True

    calendar.cancel_connect(first_state)
    assert (await calendar.status())["authorizationPending"] is True
    await calendar.complete_connect(second_state, "authorization-code")
    assert (await calendar.status())["authorizationPending"] is False


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
            return Response()

    session = Session()
    assert await calendar.sync(session) == 1
    assert len(session.urls) == 1
    assert "$deltatoken=old" not in session.urls[0]
    assert "startDateTime=" in session.urls[0]
    rows = database._get_connection().execute(
        "SELECT id,start_at,end_at FROM outlook_calendar_events ORDER BY id"
    ).fetchall()
    assert [row["id"] for row in rows] == ["future"]
    assert rows[0]["start_at"].endswith("+00:00")
