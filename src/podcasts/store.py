"""Atomic subscription discovery and episode ownership in a separate local database."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from src.podcasts.feeds import PodcastError, PodcastFeed


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PodcastStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, feed_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    author TEXT NOT NULL, description TEXT NOT NULL, website_url TEXT NOT NULL,
                    auto_process INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    guid TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                    media_url TEXT NOT NULL, extension TEXT NOT NULL, published_at TEXT NOT NULL,
                    duration_seconds INTEGER, discovered_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available',
                    transcript_id TEXT NOT NULL DEFAULT '', downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    download_only INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    UNIQUE(subscription_id, guid), UNIQUE(subscription_id, media_url)
                );
                CREATE INDEX IF NOT EXISTS podcast_episode_queue ON episodes(status, discovered_at);
                CREATE INDEX IF NOT EXISTS podcast_episode_subscription ON episodes(subscription_id, published_at DESC);
            """)
            if "download_only" not in {row["name"] for row in con.execute("PRAGMA table_info(episodes)")}:
                con.execute("ALTER TABLE episodes ADD COLUMN download_only INTEGER NOT NULL DEFAULT 0")
            # A durable transcript ID is written before provider admission. Recovery
            # first checks that ID, so shutdown never turns an uncertain upload into
            # a second automatic paid request.
            con.execute(
                "UPDATE episodes SET status='queued' WHERE status IN ('downloading','admitting','transcribing','summarizing')"
            )

    def subscriptions(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [
                dict(row)
                for row in con.execute("""
                SELECT s.*, (SELECT COUNT(*) FROM episodes e WHERE e.subscription_id=s.id) episode_count,
                (SELECT COUNT(*) FROM episodes e WHERE e.subscription_id=s.id AND e.status='completed') completed_count
                FROM subscriptions s ORDER BY s.created_at DESC
            """)
            ]

    def subscription(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            return dict(row) if row else None

    def add(self, feed_url: str, feed: PodcastFeed, *, auto_process: bool) -> str:
        identifier = sha256(feed_url.encode()).hexdigest()[:32]
        stamp = now_iso()
        with self._connect() as con:
            existing = con.execute("SELECT id FROM subscriptions WHERE feed_url=?", (feed_url,)).fetchone()
            if existing:
                return str(existing["id"])
            if con.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] >= 100:
                raise PodcastError("The subscription limit of 100 podcasts has been reached.")
            con.execute(
                """
                INSERT INTO subscriptions (id,feed_url,title,author,description,website_url,auto_process,created_at,last_checked_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """,
                (
                    identifier,
                    feed_url,
                    feed.title,
                    feed.author,
                    feed.description,
                    feed.website_url,
                    int(auto_process),
                    stamp,
                    stamp,
                ),
            )
            self._insert_episodes(con, identifier, feed, queue_limit=1 if auto_process else 0, stamp=stamp)
        return identifier

    @staticmethod
    def _insert_episodes(con, identifier: str, feed: PodcastFeed, *, queue_limit: int, stamp: str) -> int:
        queued = 0
        for episode in feed.episodes:
            episode_id = sha256(f"{identifier}\n{episode.guid}".encode()).hexdigest()[:32]
            status = "queued" if queued < queue_limit else "available"
            result = con.execute(
                """
                INSERT OR IGNORE INTO episodes
                (id,subscription_id,guid,title,description,media_url,extension,published_at,duration_seconds,discovered_at,status,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    episode_id,
                    identifier,
                    episode.guid,
                    episode.title,
                    episode.description,
                    episode.media_url,
                    episode.extension,
                    episode.published_at,
                    episode.duration_seconds,
                    stamp,
                    status,
                    stamp,
                ),
            )
            if result.rowcount and status == "queued":
                queued += 1
            if not result.rowcount:
                # Publishers may rotate signed enclosure URLs while keeping the
                # stable GUID. Refresh unclaimed episodes without replaying work.
                con.execute(
                    """UPDATE OR IGNORE episodes SET title=?,description=?,media_url=?,extension=CASE WHEN downloaded_bytes=0 THEN ? ELSE extension END,
                    published_at=?,duration_seconds=? WHERE subscription_id=? AND guid=?
                    AND status IN ('available','failed')""",
                    (
                        episode.title,
                        episode.description,
                        episode.media_url,
                        episode.extension,
                        episode.published_at,
                        episode.duration_seconds,
                        identifier,
                        episode.guid,
                    ),
                )
        return queued

    def refresh(self, identifier: str, feed: PodcastFeed) -> int:
        stamp = now_iso()
        with self._connect() as con:
            row = con.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            if row is None:
                return 0
            # An initial index records the full bounded feed. Missing historical
            # items newly reintroduced by a publisher never auto-queue if their date
            # predates the subscription; undated new items remain eligible.
            recent = PodcastFeed(
                feed.title,
                feed.author,
                feed.description,
                feed.website_url,
                tuple(
                    item for item in feed.episodes if not item.published_at or item.published_at >= row["created_at"]
                ),
            )
            queued = self._insert_episodes(
                con, identifier, recent, queue_limit=len(recent.episodes) if row["auto_process"] else 0, stamp=stamp
            )
            self._insert_episodes(con, identifier, feed, queue_limit=0, stamp=stamp)
            con.execute(
                "UPDATE subscriptions SET title=?,author=?,description=?,website_url=?,last_checked_at=?,error='' WHERE id=?",
                (feed.title, feed.author, feed.description, feed.website_url, stamp, identifier),
            )
            con.execute(
                """DELETE FROM episodes WHERE subscription_id=? AND status='available' AND id NOT IN
                (SELECT id FROM episodes WHERE subscription_id=? ORDER BY published_at DESC, discovered_at DESC LIMIT 200)""",
                (identifier, identifier),
            )
            return queued

    def set_subscription(self, identifier: str, *, auto_process: bool) -> bool:
        with self._connect() as con:
            changed = con.execute(
                "UPDATE subscriptions SET auto_process=? WHERE id=?", (int(auto_process), identifier)
            ).rowcount
            if not auto_process:
                con.execute(
                    "UPDATE episodes SET status='available' WHERE subscription_id=? AND status='queued' AND transcript_id=''",
                    (identifier,),
                )
            return bool(changed)

    def refresh_failed(self, identifier: str, message: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE subscriptions SET last_checked_at=?,error=? WHERE id=?", (now_iso(), message[:300], identifier)
            )

    def remove(self, identifier: str) -> bool:
        with self._connect() as con:
            return bool(con.execute("DELETE FROM subscriptions WHERE id=?", (identifier,)).rowcount)

    def downloads(self, identifier: str) -> list[tuple[str, str]]:
        with self._connect() as con:
            return [
                (row["id"], row["extension"])
                for row in con.execute("SELECT id,extension FROM episodes WHERE subscription_id=?", (identifier,))
            ]

    def episodes(self, subscription_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM episodes WHERE subscription_id=? ORDER BY published_at DESC, discovered_at DESC LIMIT ? OFFSET ?",
                (subscription_id, limit, offset),
            ).fetchall()
            total = con.execute("SELECT COUNT(*) FROM episodes WHERE subscription_id=?", (subscription_id,)).fetchone()[
                0
            ]
            return {"items": [dict(row) for row in rows], "total": total}

    def episode(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT e.*,s.title podcast_title FROM episodes e JOIN subscriptions s ON s.id=e.subscription_id WHERE e.id=?",
                (identifier,),
            ).fetchone()
            return dict(row) if row else None

    def claim(self) -> dict[str, Any] | None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM episodes WHERE status='queued' ORDER BY discovered_at, published_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "UPDATE episodes SET status='downloading',error='',updated_at=? WHERE id=?", (now_iso(), row["id"])
            )
            return dict(row)

    def queue(self, identifier: str) -> bool:
        with self._connect() as con:
            return bool(
                con.execute(
                    """UPDATE episodes SET
                       download_only=CASE WHEN status='completed' THEN 1 ELSE download_only END,
                       status='queued',error='',updated_at=?
                       WHERE id=? AND (status IN ('available','failed') OR (status='completed' AND downloaded_bytes=0))""",
                    (now_iso(), identifier),
                ).rowcount
            )

    def update(
        self,
        identifier: str,
        *,
        status: str,
        transcript_id: str | None = None,
        downloaded_bytes: int | None = None,
        error: str = "",
    ) -> None:
        if status not in {
            "available",
            "queued",
            "downloading",
            "admitting",
            "transcribing",
            "summarizing",
            "completed",
            "failed",
        }:
            raise ValueError("Invalid episode status")
        with self._connect() as con:
            con.execute(
                """UPDATE episodes SET status=?,transcript_id=COALESCE(?,transcript_id),
                downloaded_bytes=COALESCE(?,downloaded_bytes),error=?,updated_at=? WHERE id=?""",
                (status, transcript_id, downloaded_bytes, error[:300], now_iso(), identifier),
            )

    def active_count(self) -> int:
        with self._connect() as con:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM episodes WHERE status IN ('queued','downloading','admitting','transcribing','summarizing')"
                ).fetchone()[0]
            )
