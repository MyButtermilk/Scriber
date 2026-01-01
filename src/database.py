"""
Local SQLite database for persisting transcripts.
"""
import atexit
import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional, List, Any
from datetime import datetime

from loguru import logger

# Database file location - use absolute path based on project root
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_DB_PATH = _PROJECT_ROOT / "transcripts.db"

# Thread-local storage for database connections
# Each thread gets its own connection to avoid repeated open/close overhead
_thread_local = threading.local()
_all_connections: list[sqlite3.Connection] = []
_connections_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    """Get or create a thread-local database connection.
    
    SQLite connections are not thread-safe, so we maintain one connection
    per thread. This avoids the overhead of opening a new connection for
    every database operation (~10-50ms savings per call).
    """
    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrent read performance
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _thread_local.conn = conn
        # Track all connections for cleanup on exit
        with _connections_lock:
            _all_connections.append(conn)
        logger.debug(f"Created new database connection for thread {threading.current_thread().name}")
    return _thread_local.conn


def _close_all_connections():
    """Close all database connections on application exit."""
    with _connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
    logger.debug("Closed all database connections")


# Register cleanup on interpreter exit
atexit.register(_close_all_connections)


def init_database() -> None:
    """Initialize the database schema."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                date TEXT NOT NULL,
                duration TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT NOT NULL,
                language TEXT NOT NULL,
                step TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                channel TEXT DEFAULT '',
                thumbnail_url TEXT DEFAULT '',
                content TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT DEFAULT ''
            )
        """)
        conn.commit()
    logger.info(f"Database initialized at {_DB_PATH}")


def save_transcript(record: Any) -> None:
    """Save or update a transcript record."""
    try:
        data = record.to_public(include_content=True)
        # Map camelCase to snake_case for database
        with _get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO transcripts 
                (id, title, date, duration, status, type, language, step, 
                 source_url, channel, thumbnail_url, content, created_at, updated_at, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("id"),
                data.get("title", ""),
                data.get("date", ""),
                data.get("duration", ""),
                data.get("status", ""),
                data.get("type", ""),
                data.get("language", ""),
                data.get("step", ""),
                data.get("sourceUrl", ""),
                data.get("channel", ""),
                data.get("thumbnailUrl", ""),
                data.get("content", ""),
                data.get("createdAt", datetime.now().isoformat()),
                data.get("updatedAt", datetime.now().isoformat()),
                data.get("summary", ""),
            ))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")


def load_all_transcripts() -> List[dict]:
    """Load all transcripts from database, newest first."""
    try:
        with _get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM transcripts 
                ORDER BY created_at DESC
            """)
            rows = cursor.fetchall()
            
            transcripts = []
            for row in rows:
                transcripts.append({
                    "id": row["id"],
                    "title": row["title"],
                    "date": row["date"],
                    "duration": row["duration"],
                    "status": row["status"],
                    "type": row["type"],
                    "language": row["language"],
                    "step": row["step"],
                    "sourceUrl": row["source_url"],
                    "channel": row["channel"],
                    "thumbnailUrl": row["thumbnail_url"],
                    "content": row["content"],
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                    "summary": row["summary"],
                })
            return transcripts
    except Exception as e:
        logger.error(f"Failed to load transcripts: {e}")
        return []


def get_transcript(transcript_id: str) -> Optional[dict]:
    """Get a single transcript by ID."""
    try:
        with _get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM transcripts WHERE id = ?", 
                (transcript_id,)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": row["id"],
                    "title": row["title"],
                    "date": row["date"],
                    "duration": row["duration"],
                    "status": row["status"],
                    "type": row["type"],
                    "language": row["language"],
                    "step": row["step"],
                    "sourceUrl": row["source_url"],
                    "channel": row["channel"],
                    "thumbnailUrl": row["thumbnail_url"],
                    "content": row["content"],
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                    "summary": row["summary"],
                }
    except Exception as e:
        logger.error(f"Failed to get transcript: {e}")
    return None


def delete_transcript(transcript_id: str) -> bool:
    """Delete a transcript by ID."""
    try:
        with _get_connection() as conn:
            conn.execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to delete transcript: {e}")
        return False


def update_transcript_summary(transcript_id: str, summary: str) -> bool:
    """Update just the summary field of a transcript."""
    try:
        with _get_connection() as conn:
            conn.execute(
                "UPDATE transcripts SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, datetime.now().isoformat(), transcript_id)
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to update transcript summary: {e}")
        return False
