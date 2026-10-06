"""SQLite persistence for frozen sessions, bans, and the audit trail.

A single :class:`Store` wraps one WAL-mode ``aiosqlite`` connection. It owns the
schema (see :data:`Store.SCHEMA`), round-trips whole session snapshots to and
from the ``sessions`` table (the six structured columns — ``settings``,
``state``, ``data``, ``poly``, ``docs``, ``chat`` — are JSON-encoded), tracks per-session
bans, and appends to the ``audit`` table while mirroring each entry to the
``seance.audit`` logger via :func:`app.audit.log_audit`.

Storage is identity-only: no IPs, emails, cookies, or tokens are ever written —
only ``user_id`` / username material carried in the caller-supplied payloads.
All SQL is parameterized. On open the DB file is restricted to mode ``0600``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from functools import wraps

import aiosqlite

from app.audit import AuditEvent, log_audit
from app.images import ImageAsset, ImageError

_SCHEMA_VERSION = "5"


def _serialize_write(method):
    # One SQLite connection owns one transaction. Keep another writer's commit
    # from publishing a session while its image batch can still fail.
    @wraps(method)
    async def serialized(self, *args, **kwargs):
        async with self._write_lock:
            try:
                return await method(self, *args, **kwargs)
            except BaseException:
                if not self._closed:
                    await self._db.rollback()
                raise
    return serialized


def _harden_wal_sidecars(path: str) -> None:
    """Restrict the WAL/SHM sidecar files to owner-only ``0600``.

    SQLite creates ``<path>-wal`` and ``<path>-shm`` companions in WAL mode; they
    inherit the process umask rather than the DB file's mode, so they are
    tightened explicitly. The ``:memory:`` database has no sidecars, and a
    not-yet-created sidecar is ignored.
    """
    if path == ":memory:":
        return
    for suffix in ("-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except FileNotFoundError:
            pass


class StoreError(RuntimeError):
    """A store could not be opened: the operator has to act, not read a traceback.

    Subclasses :class:`RuntimeError` so existing callers and tests that expect
    one keep working; :func:`app.main.run` turns it into a one-line startup error.
    """


def _acquire_store_lock(path: str) -> int | None:
    """Hold an exclusive process lock for a file-backed SQLite database."""
    if path == ":memory:":
        return None
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise StoreError(f"database already in use: {path}") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_store_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Store:
    """WAL-mode SQLite persistence for sessions, bans, and audit records."""

    SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
  settings TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
  poly TEXT NOT NULL, docs TEXT NOT NULL, chat TEXT NOT NULL, rev INTEGER NOT NULL,
  seq INTEGER NOT NULL, frozen_at INTEGER, last_active INTEGER NOT NULL,
  dialect TEXT NOT NULL DEFAULT 'noisemaker-dsl', first_joined_at INTEGER);
CREATE TABLE IF NOT EXISTS bans (
  session_id TEXT NOT NULL, user_id TEXT NOT NULL, banned_by TEXT NOT NULL,
  at INTEGER NOT NULL, PRIMARY KEY (session_id, user_id));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, session_id TEXT,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_frozen_at ON sessions (frozen_at);
CREATE TABLE IF NOT EXISTS images (
  session_id TEXT NOT NULL, id TEXT NOT NULL, mime_type TEXT NOT NULL,
  width INTEGER NOT NULL, height INTEGER NOT NULL, data BLOB NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, id));
"""

    def __init__(
        self,
        db: aiosqlite.Connection,
        path: str = ":memory:",
        lock_fd: int | None = None,
    ) -> None:
        self._db = db
        self._path = path
        self._lock_fd = lock_fd
        self._closed = False
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> Store:
        """Connect to ``path``, apply PRAGMAs and schema, and verify meta version.

        First takes a lifetime exclusive lock on ``<path>.lock`` so only one
        process can own the database. Enables WAL mode, a 5 s busy timeout,
        foreign-key enforcement, and ``NORMAL`` synchronous durability. Restricts
        the DB file and its WAL/SHM
        sidecars to mode ``0600`` (skipped for the ``:memory:`` database). Records
        the current :data:`_SCHEMA_VERSION` on first open; a database at an older
        version is migrated forward in place; any other stored version raises
        :class:`StoreError`, as does a database another process already holds.
        """
        lock_fd = _acquire_store_lock(path)
        db = None
        try:
            db = await aiosqlite.connect(path)
            if path != ":memory:":
                os.chmod(path, 0o600)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript(cls.SCHEMA)
            async with db.execute(
                "SELECT v FROM meta WHERE k = ?", ("schema_version",)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO meta (k, v) VALUES (?, ?)",
                    ("schema_version", _SCHEMA_VERSION),
                )
                await db.commit()
            elif row[0] == "1":
                await _migrate_v1_to_v2(db)
                await _migrate_v2_to_v3(db)
                await _migrate_v3_to_v4(db)
                await _migrate_v4_to_v5(db)
                await db.execute(
                    "UPDATE meta SET v = ? WHERE k = ?", (_SCHEMA_VERSION, "schema_version")
                )
                await db.commit()
            elif row[0] == "2":
                await _migrate_v2_to_v3(db)
                await _migrate_v3_to_v4(db)
                await _migrate_v4_to_v5(db)
                await db.execute(
                    "UPDATE meta SET v = ? WHERE k = ?", (_SCHEMA_VERSION, "schema_version")
                )
                await db.commit()
            elif row[0] == "3":
                await _migrate_v3_to_v4(db)
                await _migrate_v4_to_v5(db)
                await db.execute(
                    "UPDATE meta SET v = ? WHERE k = ?", (_SCHEMA_VERSION, "schema_version")
                )
                await db.commit()
            elif row[0] == "4":
                await _migrate_v4_to_v5(db)
                await db.execute(
                    "UPDATE meta SET v = ? WHERE k = ?", (_SCHEMA_VERSION, "schema_version")
                )
                await db.commit()
            elif row[0] != _SCHEMA_VERSION:
                raise StoreError(
                    f"unsupported schema version {row[0]!r}, expected {_SCHEMA_VERSION!r}"
                )
            _harden_wal_sidecars(path)
        except BaseException:
            if db is not None:
                await db.close()
            _release_store_lock(lock_fd)
            raise
        return cls(db, path, lock_fd)

    @_serialize_write
    async def save_session(
        self, session_id: str, payload: dict, images: list[ImageAsset] | None = None
    ) -> None:
        """Insert or replace the whole session row from ``payload``.

        JSON-encodes the structured columns; stores scalars verbatim. A missing
        required key raises :class:`KeyError` (the caller supplied a bad payload).
        """
        values = (
            session_id,
            payload["created_by"],
            payload["created_at"],
            json.dumps(payload["settings"]),
            json.dumps(payload["state"]),
            json.dumps(payload["data"]),
            json.dumps(payload["poly"]),
            json.dumps(payload["docs"]),
            json.dumps(payload["chat"]),
            payload["rev"],
            payload["seq"],
            payload["frozen_at"],
            payload["last_active"],
            payload["dialect"],
            payload.get("first_joined_at"),
        )
        await self._db.execute(
            "INSERT OR REPLACE INTO sessions "
            "(id, created_by, created_at, settings, state, data, poly, docs, chat, "
            "rev, seq, frozen_at, last_active, dialect, first_joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        if images:
            await self._db.executemany(
                "INSERT INTO images (session_id, id, mime_type, width, height, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(session_id, image.id, image.mime_type, image.width, image.height, image.data,
                  payload["created_at"]) for image in images],
            )
        await self._db.commit()
        _harden_wal_sidecars(self._path)

    async def load_session(self, session_id: str) -> dict | None:
        """Return the session payload for ``session_id`` (JSON columns decoded) or None."""
        async with self._db.execute(
            "SELECT created_by, created_at, settings, state, data, poly, docs, chat, "
            "rev, seq, frozen_at, last_active, dialect, first_joined_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "created_by": row[0],
            "created_at": row[1],
            "settings": json.loads(row[2]),
            "state": json.loads(row[3]),
            "data": json.loads(row[4]),
            "poly": json.loads(row[5]),
            "docs": json.loads(row[6]),
            "chat": json.loads(row[7]),
            "rev": row[8],
            "seq": row[9],
            "frozen_at": row[10],
            "last_active": row[11],
            "dialect": row[12],
            "first_joined_at": row[13],
        }

    async def count_sessions(self) -> int:
        """Return the number of persisted session rows, live or frozen."""
        async with self._db.execute("SELECT COUNT(*) FROM sessions") as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    @_serialize_write
    async def delete_session(self, session_id: str) -> None:
        """Delete the session and its per-session bans (a no-op if absent).

        Audit rows are intentionally retained as the service audit trail.
        """
        await self._db.execute("DELETE FROM bans WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM images WHERE session_id = ?", (session_id,))
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()
        _harden_wal_sidecars(self._path)

    @_serialize_write
    async def save_image(
        self, session_id: str, image: ImageAsset, max_bytes: int, max_count: int,
        now: int | None = None,
    ) -> None:
        # The budget check and insertion are one SQLite statement, so simultaneous
        # uploads cannot both consume the same remaining allowance.
        await self._db.execute(
            "INSERT OR IGNORE INTO images "
            "(session_id, id, mime_type, width, height, data, created_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ?) "
            "AND (SELECT COALESCE(SUM(length(data)), 0) FROM images WHERE session_id = ?) "
            "+ ? <= ? AND (SELECT COUNT(*) FROM images WHERE session_id = ?) < ?",
            (session_id, image.id, image.mime_type, image.width, image.height, image.data,
             int(time.time()) if now is None else now,
             session_id, session_id, len(image.data), max_bytes, session_id, max_count),
        )
        await self._db.commit()
        if await self.load_image(session_id, image.id) is None:
            raise ImageError("session image limit exceeded", 413)
        _harden_wal_sidecars(self._path)

    @_serialize_write
    async def prune_images(self, session_id: str, keep: set[str], before: int) -> int:
        """Delete the session's images created before ``before`` whose ids are not in
        ``keep``; return how many went."""
        async with self._db.execute(
            "SELECT id FROM images WHERE session_id = ? AND created_at < ?", (session_id, before)
        ) as cursor:
            stale = [row[0] for row in await cursor.fetchall() if row[0] not in keep]
        if stale:
            await self._db.executemany(
                "DELETE FROM images WHERE session_id = ? AND id = ?",
                [(session_id, image_id) for image_id in stale],
            )
            await self._db.commit()
        return len(stale)

    async def load_image(self, session_id: str, image_id: str) -> ImageAsset | None:
        async with self._db.execute(
            "SELECT mime_type, width, height, data FROM images WHERE session_id = ? AND id = ?",
            (session_id, image_id),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else ImageAsset(image_id, row[0], row[1], row[2], row[3])

    async def list_frozen_older_than(self, ts: int) -> list[str]:
        """Return sorted ids of frozen sessions whose ``frozen_at`` is strictly < ``ts``.

        Served by the ``sessions_frozen_at`` index: ``frozen_at`` sits after six
        large TEXT columns, so a scan walks every row's overflow pages, which the
        retention sweep pays on every pass.
        """
        async with self._db.execute(
            "SELECT id FROM sessions WHERE frozen_at IS NOT NULL AND frozen_at < ? "
            "ORDER BY id",
            (ts,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def list_unclaimed_older_than(self, ts: int) -> list[str]:
        """Return sorted ids of frozen sessions nobody ever joined, frozen before ``ts``.

        A row keeps ``first_joined_at IS NULL`` from creation until its first
        connect, so this is exactly the set that was created and then abandoned.
        """
        async with self._db.execute(
            "SELECT id FROM sessions WHERE frozen_at IS NOT NULL AND first_joined_at IS NULL "
            "AND frozen_at < ? ORDER BY id",
            (ts,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    @_serialize_write
    async def add_ban(self, session_id: str, user_id: str, by: str, ts: int) -> None:
        """Record (or refresh) a ban for ``user_id`` in ``session_id`` (idempotent)."""
        await self._db.execute(
            "INSERT OR REPLACE INTO bans (session_id, user_id, banned_by, at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, user_id, by, ts),
        )
        await self._db.commit()
        _harden_wal_sidecars(self._path)

    @_serialize_write
    async def remove_ban(self, session_id: str, user_id: str) -> None:
        """Remove ``user_id``'s ban from ``session_id`` (a no-op if absent)."""
        await self._db.execute(
            "DELETE FROM bans WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        )
        await self._db.commit()
        _harden_wal_sidecars(self._path)

    async def get_bans(self, session_id: str) -> set[str]:
        """Return the set of banned ``user_id`` values for ``session_id``."""
        async with self._db.execute(
            "SELECT user_id FROM bans WHERE session_id = ?", (session_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0] for row in rows}

    @_serialize_write
    async def audit(
        self,
        ts: int,
        session_id: str,
        actor: str,
        action: str,
        target: str | None,
        detail: dict,
    ) -> None:
        """Append an audit row and mirror the event to the ``seance.audit`` logger."""
        await self._db.execute(
            "INSERT INTO audit (ts, session_id, actor, action, target, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, session_id, actor, action, target, json.dumps(detail)),
        )
        await self._db.commit()
        _harden_wal_sidecars(self._path)
        log_audit(
            AuditEvent(
                ts=ts,
                session_id=session_id,
                actor=actor,
                action=action,
                target=target,
                detail=detail,
            )
        )

    @_serialize_write
    async def close(self) -> None:
        """Commit and close the connection (idempotent; a second call is a no-op)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._db.commit()
            _harden_wal_sidecars(self._path)
            await self._db.close()
        finally:
            _release_store_lock(self._lock_fd)
            self._lock_fd = None


async def _migrate_v1_to_v2(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "docs" not in columns:
        await db.execute("ALTER TABLE sessions ADD COLUMN docs TEXT NOT NULL DEFAULT '[]'")


async def _migrate_v4_to_v5(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(images)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    # Images stored before the column existed read as created at zero: old enough
    # to free when a full budget needs room and nothing references them.
    if "created_at" not in columns:
        await db.execute("ALTER TABLE images ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")


async def _migrate_v3_to_v4(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "first_joined_at" not in columns:
        await db.execute("ALTER TABLE sessions ADD COLUMN first_joined_at INTEGER")
    # Older schemas never recorded the first join. Reserve zero for that unknown
    # history so existing content retains the long TTL. NULL means demonstrably
    # never joined, and is assigned only to sessions created with the new schema.
    await db.execute(
        "UPDATE sessions SET first_joined_at = 0 WHERE first_joined_at IS NULL"
    )


async def _migrate_v2_to_v3(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "dialect" not in columns:
        await db.execute(
            "ALTER TABLE sessions ADD COLUMN dialect TEXT NOT NULL DEFAULT 'noisemaker-dsl'"
        )
