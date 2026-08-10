"""Member directory — read-only resolution of a member id to a username.

Groundsquirrel is the source of truth for member identity; the seance server
only needs a narrow lookup: given a member UUID, return the current username (or
learn that the member no longer exists). This module hides three interchangeable
backends behind one :class:`MemberDirectory` protocol:

* :class:`StaticDirectory` — an in-memory map (tests, fixtures, tiny deploys).
* :class:`SqliteDirectory` — a local SQLite mirror of the members table.
* :class:`PostgresDirectory` — the live groundsquirrel Postgres members table.

:func:`make_directory` selects a backend from the ``SEANCE_DIRECTORY_DSN`` value.
All backends open their connections lazily on first lookup and treat a member
with no username (NULL or empty) as deleted: a member without a username cannot
be a collaboration identity. A missing row is never an error — it returns
``None``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import aiosqlite
import asyncpg

from app.config import ConfigError

_STATIC_PREFIX = "static:"
_SQLITE_PREFIX = "sqlite:///"
_PG_PREFIXES = ("postgres://", "postgresql://")

_SQLITE_QUERY = "SELECT id, username, deleted_at FROM members WHERE id = ?"
_PG_QUERY = "SELECT id, username, deleted_at FROM members WHERE id = $1"


@dataclass(frozen=True)
class MemberRecord:
    """A resolved member: its id, current username, and deleted state."""

    id: str
    username: str
    deleted: bool


def _to_record(member_id: object, username: object, deleted_at: object) -> MemberRecord:
    """Build a MemberRecord from a raw ``(id, username, deleted_at)`` row.

    A member is deleted when ``deleted_at`` is set OR the username is NULL/empty;
    a NULL username is normalised to the empty string.
    """
    text = username or ""
    return MemberRecord(
        id=str(member_id),
        username=str(text),
        deleted=deleted_at is not None or not text,
    )


@runtime_checkable
class MemberDirectory(Protocol):
    """Read-only resolver from a groundsquirrel member id to a MemberRecord."""

    async def lookup(self, member_id: str) -> MemberRecord | None:
        """Return the record for ``member_id``, or ``None`` if there is no row."""
        ...

    async def close(self) -> None:
        """Release any backing resources. Idempotent."""
        ...


class StaticDirectory:
    """A directory backed by an in-memory mapping of member id to record."""

    def __init__(self, members: dict[str, MemberRecord]) -> None:
        self._members = members

    async def lookup(self, member_id: str) -> MemberRecord | None:
        return self._members.get(member_id)

    async def close(self) -> None:
        """No-op: a static directory owns no external resources."""


class SqliteDirectory:
    """A directory backed by a SQLite ``members`` table.

    A single connection is opened lazily on the first lookup and reused; an
    ``asyncio.Lock`` serialises access because a SQLite connection is not safe
    for concurrent use.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def lookup(self, member_id: str) -> MemberRecord | None:
        async with self._lock:
            if self._db is None:
                self._db = await aiosqlite.connect(self._path)
            async with self._db.execute(_SQLITE_QUERY, (member_id,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return _to_record(row[0], row[1], row[2])

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None


class PostgresDirectory:
    """A directory backed by the live groundsquirrel Postgres ``members`` table.

    A connection pool (min 1, max 4) is created lazily on the first lookup. The
    pool is safe for concurrent use, so only pool creation and close are guarded
    by the lock; individual lookups run concurrently against the pool.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def lookup(self, member_id: str) -> MemberRecord | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow(_PG_QUERY, member_id)
        if row is None:
            return None
        return _to_record(row["id"], row["username"], row["deleted_at"])

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=1, max_size=4
                    )
        return self._pool

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None


async def make_directory(dsn: str | None) -> MemberDirectory | None:
    """Construct the member directory selected by ``dsn``.

    ``None`` yields no directory. ``static:`` (any suffix ignored) yields an
    empty in-memory directory. ``sqlite:///<path>`` and ``postgres://…`` /
    ``postgresql://…`` yield the SQLite and Postgres backends respectively.
    Any other value raises :class:`ConfigError` naming ``SEANCE_DIRECTORY_DSN``.
    """
    if dsn is None:
        return None
    if dsn.startswith(_STATIC_PREFIX):
        return StaticDirectory({})
    if dsn.startswith(_SQLITE_PREFIX):
        return SqliteDirectory(dsn.removeprefix(_SQLITE_PREFIX))
    if dsn.startswith(_PG_PREFIXES):
        return PostgresDirectory(dsn)
    raise ConfigError(f"SEANCE_DIRECTORY_DSN: unsupported DSN {dsn!r}")
