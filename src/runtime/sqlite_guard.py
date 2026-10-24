"""SQLite connections that cannot be closed underneath an in-flight call.

The stores keep one connection per thread and close every thread's connection
at shutdown. ``asyncio.to_thread`` work survives cancellation of its awaiting
task, so a worker can still be inside ``sqlite3_step`` when another thread calls
``close()``. CPython then frees the native statement under the running step,
which is an access violation on Windows and a segmentation fault elsewhere.

:class:`GuardedConnection` serializes ``close()`` with every statement step and
``with connection:`` transaction on one reentrant lock. A foreign ``close()``
waits for the owner's current statement or transaction; if that does not finish
within :data:`CLOSE_WAIT_SECONDS`, the handle is left to its owner, whose next
store access opens a fresh connection and releases the retired one.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, cast

from loguru import logger

CLOSE_WAIT_SECONDS = 5.0


class GuardedCursor(sqlite3.Cursor):
    """Cursor whose native steps hold the owning connection's usage lock."""

    def _usage(self) -> threading.RLock:
        return cast(GuardedConnection, self.connection).usage_lock

    def execute(self, sql: str, parameters: Any = (), /) -> GuardedCursor:
        with self._usage():
            super().execute(sql, parameters)
        return self

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> GuardedCursor:
        with self._usage():
            super().executemany(sql, seq_of_parameters)
        return self

    def executescript(self, sql_script: str, /) -> GuardedCursor:
        with self._usage():
            super().executescript(sql_script)
        return self

    def fetchone(self) -> Any:
        with self._usage():
            return super().fetchone()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        with self._usage():
            return super().fetchmany(self.arraysize if size is None else size)

    def fetchall(self) -> list[Any]:
        with self._usage():
            return super().fetchall()

    def __next__(self) -> Any:
        with self._usage():
            return super().__next__()


class GuardedConnection(sqlite3.Connection):
    """``sqlite3.Connection`` that a foreign thread cannot close mid-call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.usage_lock = threading.RLock()

    def cursor(self, factory: Any = GuardedCursor) -> Any:
        return super().cursor(factory)

    def execute(self, sql: str, parameters: Any = (), /) -> GuardedCursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> GuardedCursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> GuardedCursor:
        return self.cursor().executescript(sql_script)

    def commit(self) -> None:
        with self.usage_lock:
            super().commit()

    def rollback(self) -> None:
        with self.usage_lock:
            super().rollback()

    def __enter__(self) -> GuardedConnection:
        # The transaction scope, not just each statement, belongs to the owner:
        # closing between statements would discard its uncommitted work.
        self.usage_lock.acquire()
        try:
            super().__enter__()
        except BaseException:
            self.usage_lock.release()
            raise
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return super().__exit__(*exc_info)
        finally:
            self.usage_lock.release()

    def close(self) -> None:
        if not self.usage_lock.acquire(timeout=CLOSE_WAIT_SECONDS):
            logger.warning("SQLite connection close deferred to its owner while a statement is still running")
            return
        try:
            super().close()
        finally:
            self.usage_lock.release()
