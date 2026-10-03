"""Remote cleanup is exercised exclusively through local transport doubles."""

from __future__ import annotations

import asyncio
import threading

import aiohttp
import pytest

from src.config import Config
from src.soniox_cleanup import cleanup_soniox_resources, drain_soniox_cleanup


class Response:
    def __init__(self, status=204, *, error=None, waiting=None):
        self.status = status
        self.error = error
        self.waiting = waiting
        self.closed = False

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        if self.waiting is not None:
            self.waiting.set()
            await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_):
        self.closed = True
        return False

    async def text(self):
        pytest.fail("Cleanup must not read provider response bodies")

    async def json(self):
        pytest.fail("Cleanup must not read provider response bodies")


class Session:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.requests = []

    def delete(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses, Response())


class Transport:
    def __init__(self, session, error=None):
        self.session = session
        self.error = error
        self.providers = []

    async def session_view(self, *, provider):
        self.providers.append(provider)
        if self.error:
            raise self.error
        return self.session


class Store:
    def __init__(self, rows=(), *, ack_error=None, ack_started=None, ack_release=None):
        self.rows = list(rows)
        self.ack_error = ack_error
        self.ack_started = ack_started
        self.ack_release = ack_release
        self.limits = []
        self.acknowledged = []
        self.attempted = []

    def list_soniox_cleanup(self, *, limit=20):
        self.limits.append(limit)
        return list(self.rows[:limit])

    def note_soniox_cleanup_attempt(self, *, job_id):
        self.attempted.append(job_id)

    def complete_soniox_cleanup(self, *, job_id, resource_kind, resource_id):
        if self.ack_error:
            raise self.ack_error
        if self.ack_started:
            self.ack_started.set()
            assert self.ack_release.wait(3)
        self.acknowledged.append((job_id, resource_kind, resource_id))
        self.rows = [
            row
            for row in self.rows
            if (row["job_id"], row["resource_kind"], row["resource_id"]) != (job_id, resource_kind, resource_id)
        ]
        return True


def row(kind, *, job="job-a", region="eu", resource_id=None):
    return {"job_id": job, "region": region, "resource_kind": kind, "resource_id": resource_id or f"{kind}-id"}


@pytest.fixture(autouse=True)
def inert_credentials(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "synthetic-cleanup-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("region,host", [("eu", "api.eu.soniox.com"), ("us", "api.soniox.com")])
async def test_cleanup_uses_frozen_region_and_deletes_transcription_before_file(region, host):
    responses = [Response(200), Response(204)]
    session = Session(responses)

    assert await cleanup_soniox_resources(
        session=session,
        api_key="synthetic",
        region=region,
        transcription_id="transcription-123",
        file_id="file_456",
    )

    assert [url for url, _ in session.requests] == [
        f"https://{host}/v1/transcriptions/transcription-123",
        f"https://{host}/v1/files/file_456",
    ]
    assert all(response.closed for response in responses)
    for _url, kwargs in session.requests:
        assert kwargs["headers"] == {"Authorization": "Bearer synthetic"}
        assert kwargs["timeout"].total == 10
        assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 202, 204, 299, 404])
async def test_every_success_status_and_already_absent_are_acknowledgeable(status):
    assert await cleanup_soniox_resources(
        session=Session([Response(status)]), api_key="synthetic", region="eu", file_id="file-id"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 409, 429, 500, 503])
async def test_failed_transcription_deletion_never_attempts_its_file(status):
    session = Session([Response(status)])

    assert not await cleanup_soniox_resources(
        session=session, api_key="synthetic", region="eu", transcription_id="job-id", file_id="file-id"
    )
    assert len(session.requests) == 1
    assert "/transcriptions/" in session.requests[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["", "   ", "jp", "https://example.invalid", None, 17])
async def test_missing_or_unknown_frozen_region_never_defaults_to_us(region):
    session = Session()

    assert not await cleanup_soniox_resources(session=session, api_key="synthetic", region=region, file_id="file-id")
    assert not session.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource_id",
    ["", "../another", "a/b", "a\\b", "a?query", "a#fragment", "%2F", "..", "a\n", "a b", "ä", "a" * 257, 12],
)
async def test_invalid_id_cannot_address_another_path_or_query(resource_id):
    session = Session()

    assert not await cleanup_soniox_resources(session=session, api_key="synthetic", region="eu", file_id=resource_id)
    assert not session.requests


@pytest.mark.asyncio
async def test_all_ids_validated_before_any_deletion():
    session = Session()

    assert not await cleanup_soniox_resources(
        session=session, api_key="synthetic", region="eu", transcription_id="valid-id", file_id="../unsafe"
    )
    assert not session.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimeoutError("private-response"), aiohttp.ClientConnectionError("private-url"), RuntimeError("private-key")],
)
async def test_transport_failure_is_false_without_logging_sensitive_exception(error, caplog):
    session = Session([Response(error=error)])

    assert not await cleanup_soniox_resources(
        session=session, api_key="synthetic", region="eu", file_id="private-resource"
    )
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_empty_resource_set_is_a_noop():
    session = Session()

    assert await cleanup_soniox_resources(session=session, api_key="", region="")
    assert not session.requests


@pytest.mark.asyncio
async def test_missing_key_does_not_send_request():
    session = Session()

    assert not await cleanup_soniox_resources(session=session, api_key="", region="eu", file_id="file-id")
    assert not session.requests


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_and_does_not_attempt_file():
    waiting = asyncio.Event()
    session = Session([Response(waiting=waiting)])
    task = asyncio.create_task(
        cleanup_soniox_resources(
            session=session, api_key="synthetic", region="eu", transcription_id="job-id", file_id="file-id"
        )
    )
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_drain_orders_outbox_resources_and_acks_only_confirmed_deletions(monkeypatch):
    monkeypatch.setattr(Config, "SONIOX_REGION", "us")
    store = Store([row("file"), row("transcription")])
    session = Session([Response(404), Response(204)])
    transport = Transport(session)

    assert await drain_soniox_cleanup(store=store, transport=transport) == 2

    assert store.limits == [20]
    assert store.attempted == ["job-a"]
    assert [kind for _job, kind, _id in store.acknowledged] == ["transcription", "file"]
    assert all(url.startswith("https://api.eu.soniox.com/") for url, _ in session.requests)
    assert transport.providers == ["soniox_async"]
    assert not store.rows


@pytest.mark.asyncio
async def test_drain_keeps_failed_job_and_file_but_can_clean_other_job():
    store = Store([row("transcription"), row("file"), row("file", job="job-b")])
    session = Session([Response(503), Response(204)])

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 1

    assert store.acknowledged == [("job-b", "file", "file-id")]
    assert len(session.requests) == 2
    assert [entry["job_id"] for entry in store.rows] == ["job-a", "job-a"]
    assert store.attempted == ["job-a", "job-b"]


@pytest.mark.asyncio
async def test_drain_retains_file_failure_after_successful_job_deletion():
    store = Store([row("transcription"), row("file")])
    session = Session([Response(204), Response(429)])

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 1
    assert store.rows == [row("file")]


@pytest.mark.asyncio
async def test_drain_enforces_bounded_batch():
    store = Store([row("file", job=f"job-{index}") for index in range(25)])
    session = Session()

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 20
    assert len(session.requests) == 20
    assert len(store.rows) == 5


@pytest.mark.asyncio
async def test_drain_preserves_store_retry_priority_between_jobs():
    store = Store(
        [
            row("file", job="job-z", resource_id="file-z"),
            row("transcription", job="job-z", resource_id="transcription-z"),
            row("file", job="job-a", resource_id="file-a"),
        ]
    )
    session = Session()

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 3
    assert [url.rsplit("/", 1)[-1] for url, _ in session.requests] == ["transcription-z", "file-z", "file-a"]
    assert store.attempted == ["job-z", "job-a"]


@pytest.mark.asyncio
async def test_drain_without_transport_or_credentials_preserves_outbox(monkeypatch):
    store = Store([row("file")])
    assert await drain_soniox_cleanup(store=store, transport=None) == 0
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "")
    transport = Transport(Session())

    assert await drain_soniox_cleanup(store=store, transport=transport) == 0
    assert not store.acknowledged
    assert not transport.providers


@pytest.mark.asyncio
async def test_drain_closed_transport_retains_all_rows():
    store = Store([row("file")])

    assert await drain_soniox_cleanup(store=store, transport=Transport(Session(), error=RuntimeError("closing"))) == 0
    assert not store.acknowledged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_row",
    [
        row("transcription", region="jp"),
        row("transcription", resource_id="../unsafe"),
        {"job_id": "job-a", "region": "eu", "resource_kind": "transcription"},
        row("unknown"),
    ],
)
async def test_drain_malformed_transcription_keeps_same_job_file(bad_row):
    store = Store([bad_row, row("file")])
    session = Session()

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 0
    assert not session.requests
    assert not store.acknowledged
    assert store.attempted == ["job-a"]


@pytest.mark.asyncio
async def test_drain_ack_failure_keeps_dependency_and_does_not_remove_file():
    store = Store([row("transcription"), row("file")], ack_error=OSError("busy"))
    session = Session()

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 0
    assert len(session.requests) == 1
    assert len(store.rows) == 2


@pytest.mark.asyncio
async def test_drain_cancellation_preserves_unconfirmed_rows():
    waiting = asyncio.Event()
    store = Store([row("transcription"), row("file")])
    session = Session([Response(waiting=waiting)])
    task = asyncio.create_task(drain_soniox_cleanup(store=store, transport=Transport(session)))
    await waiting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store.acknowledged
    assert len(store.rows) == 2


@pytest.mark.asyncio
async def test_confirmed_deletion_ack_finishes_before_cancellation_propagates():
    started = threading.Event()
    release = threading.Event()
    store = Store([row("transcription"), row("file")], ack_started=started, ack_release=release)
    session = Session()
    task = asyncio.create_task(drain_soniox_cleanup(store=store, transport=Transport(session)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.rows == [row("file")]
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_drain_timeout_retains_pending_resource(monkeypatch):
    monkeypatch.setattr("src.soniox_cleanup._CLEANUP_TIMEOUT_SECONDS", 0.01)
    store = Store([row("file")])
    waiting = asyncio.Event()
    session = Session([Response(waiting=waiting)])

    assert await drain_soniox_cleanup(store=store, transport=Transport(session)) == 0
    assert not store.acknowledged
    assert store.rows == [row("file")]
