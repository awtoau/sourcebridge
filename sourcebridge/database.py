"""Shared SQLite connection and transaction ownership policy.

The evidence database has one supported writer transaction at a time.  Writers
use ``BEGIN IMMEDIATE`` so that the lock is acquired before any mutation is
performed; readers use a read-only, explicit transaction so every report sees
one SQLite snapshot.  This module is deliberately small so every database
adapter can use the same busy handling and connection pragmas.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Iterator
from urllib.parse import quote


# These values are part of the documented operational policy.  The timeout
# lets normal short transactions queue in SQLite; the bounded retry handles a
# checkpoint or a writer that releases the lock just after the timeout.
BUSY_TIMEOUT_MS = 5_000
BUSY_RETRY_ATTEMPTS = 4
BUSY_RETRY_BACKOFF_SECONDS = 0.025
WAL_AUTOCHECKPOINT_PAGES = 1_000


class SQLiteBusyError(sqlite3.OperationalError):
    """A supported mutation could not acquire the single-writer lease."""


def _is_busy(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _busy_message(database: Path, attempts: int, error: BaseException) -> str:
    return (
        f"{database}: SQLite writer remained busy after {attempts} attempts "
        f"(timeout={BUSY_TIMEOUT_MS}ms); wait for the active writer to finish, "
        "then retry the command. If an interrupted run is left non-terminal, "
        "recover it with `sourcebridge recover-run`. "
        f"Last SQLite error: {error}"
    )


def _connect(database: Path, *, readonly: bool) -> sqlite3.Connection:
    path = database.resolve()
    if readonly:
        # URI mode prevents a report from silently creating an empty database.
        target = f"file:{quote(str(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(
            target,
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
    else:
        connection = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _open_writer(database: Path, *, enable_wal: bool) -> sqlite3.Connection:
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(BUSY_RETRY_ATTEMPTS + 1):
        connection = _connect(database, readonly=False)
        try:
            if enable_wal:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute(
                    f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}"
                )
            # BEGIN IMMEDIATE is the application-level single-writer lease.
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except sqlite3.OperationalError as error:
            connection.close()
            if not _is_busy(error):
                raise
            last_error = error
            if attempt >= BUSY_RETRY_ATTEMPTS:
                break
            time.sleep(BUSY_RETRY_BACKOFF_SECONDS * (2**attempt))
    assert last_error is not None
    raise SQLiteBusyError(
        _busy_message(database, BUSY_RETRY_ATTEMPTS + 1, last_error)
    ) from last_error


@contextmanager
def write_transaction(
    database: Path, *, enable_wal: bool = False
) -> Iterator[sqlite3.Connection]:
    """Run a short, serialized mutation transaction.

    The caller owns all writes performed in the context.  A lock is acquired
    before yielding, so a busy failure cannot leave a partially applied
    mutation for the caller to interpret.
    """

    connection = _open_writer(database, enable_wal=enable_wal)
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        try:
            connection.commit()
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise SQLiteBusyError(
                    _busy_message(database, 1, error)
                ) from error
            raise
    finally:
        connection.close()


@contextmanager
def read_transaction(database: Path) -> Iterator[sqlite3.Connection]:
    """Read one consistent, read-only SQLite snapshot."""

    if not database.is_file():
        raise FileNotFoundError(f"{database}: evidence database does not exist")
    connection = _connect(database, readonly=True)
    try:
        connection.execute("BEGIN")
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def checkpoint(database: Path, *, mode: str = "PASSIVE") -> tuple[int, int, int]:
    """Run an explicit WAL checkpoint for maintenance.

    ``PASSIVE`` is the default because it never waits for readers.  The
    returned tuple is SQLite's ``(busy, log_pages, checkpointed_pages)``.
    """

    if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError(f"unsupported SQLite checkpoint mode `{mode}`")
    if not database.is_file():
        raise FileNotFoundError(f"{database}: evidence database does not exist")
    connection = _connect(database, readonly=False)
    try:
        result = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        assert result is not None
        return tuple(int(value) for value in result)
    finally:
        connection.close()
