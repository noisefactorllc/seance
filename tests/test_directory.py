"""Tests for the member directory (app.directory).

The static and SQLite backends are exercised in full against databases created
under ``tmp_path``. The PostgreSQL backend is covered only when
``SEANCE_TEST_PG_DSN`` points at a reachable database (skipped otherwise); that
test creates a uniquely-named temporary schema, runs a real lookup through the
pool, and drops the schema on exit. No network is used unless that env var is
explicitly set.
"""

import os
import sqlite3
import uuid
from dataclasses import FrozenInstanceError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from app.config import ConfigError
from app.directory import (
    MemberDirectory,
    MemberRecord,
    PostgresDirectory,
    SqliteDirectory,
    StaticDirectory,
    make_directory,
)

PG_DSN = os.environ.get("SEANCE_TEST_PG_DSN")


def _make_sqlite_db(path, rows) -> None:
    """Create a members table at ``path`` and insert ``(id, username, deleted_at)`` rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE members (id TEXT PRIMARY KEY, username TEXT, deleted_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO members (id, username, deleted_at) VALUES (?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    """Return ``dsn`` with a ``search_path=<schema>`` query param (asyncpg server setting)."""
    parts = urlsplit(dsn)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in existing if k != "search_path"]
    query.append(("search_path", schema))
    return urlunsplit(parts._replace(query=urlencode(query)))


# --- MemberRecord -----------------------------------------------------------


def test_member_record_is_frozen():
    record = MemberRecord(id="u1", username="alice", deleted=False)
    with pytest.raises(FrozenInstanceError):
        record.username = "bob"  # type: ignore[misc]


# --- StaticDirectory --------------------------------------------------------


async def test_static_lookup_hit_and_miss():
    alice = MemberRecord(id="u1", username="alice", deleted=False)
    directory = StaticDirectory({"u1": alice})
    assert await directory.lookup("u1") == alice
    assert await directory.lookup("missing") is None
    await directory.close()  # no-op, does not raise


async def test_static_empty_directory():
    directory = StaticDirectory({})
    assert await directory.lookup("anyone") is None
    await directory.close()


# --- SqliteDirectory --------------------------------------------------------


async def test_sqlite_lookup_live_deleted_missing(tmp_path):
    db_path = tmp_path / "members.db"
    _make_sqlite_db(
        db_path,
        [
            ("u-live", "alice", None),
            ("u-deleted", "bob", 1_751_500_000),
            ("u-noname", None, None),
            ("u-empty", "", None),
        ],
    )
    directory = SqliteDirectory(str(db_path))
    try:
        assert await directory.lookup("u-live") == MemberRecord(
            id="u-live", username="alice", deleted=False
        )
        # A non-null deleted_at marks the row deleted.
        assert await directory.lookup("u-deleted") == MemberRecord(
            id="u-deleted", username="bob", deleted=True
        )
        # A NULL username cannot be a collab identity -> deleted, normalised to "".
        assert await directory.lookup("u-noname") == MemberRecord(
            id="u-noname", username="", deleted=True
        )
        # An empty username is treated the same as NULL.
        assert await directory.lookup("u-empty") == MemberRecord(
            id="u-empty", username="", deleted=True
        )
        # A missing row returns None and never raises.
        assert await directory.lookup("nobody") is None
    finally:
        await directory.close()


async def test_sqlite_lazy_open_reuse_and_idempotent_close(tmp_path):
    db_path = tmp_path / "members.db"
    _make_sqlite_db(db_path, [("u1", "alice", None)])
    directory = SqliteDirectory(str(db_path))
    # No connection is opened until the first lookup.
    assert directory._db is None
    await directory.lookup("u1")
    conn = directory._db
    assert conn is not None
    # A second lookup reuses the same connection object.
    await directory.lookup("u1")
    assert directory._db is conn
    await directory.close()
    assert directory._db is None
    # Closing again is a no-op.
    await directory.close()


# --- make_directory dispatch ------------------------------------------------


async def test_make_directory_none_returns_none():
    assert await make_directory(None) is None


async def test_make_directory_static_empty():
    directory = await make_directory("static:")
    assert isinstance(directory, StaticDirectory)
    assert await directory.lookup("anyone") is None
    await directory.close()


async def test_make_directory_static_prefix_ignores_suffix():
    directory = await make_directory("static:ignored")
    assert isinstance(directory, StaticDirectory)
    assert await directory.lookup("anyone") is None
    await directory.close()


async def test_make_directory_sqlite(tmp_path):
    db_path = tmp_path / "members.db"
    _make_sqlite_db(db_path, [("u1", "alice", None)])
    directory = await make_directory(f"sqlite:///{db_path}")
    assert isinstance(directory, SqliteDirectory)
    try:
        assert await directory.lookup("u1") == MemberRecord(
            id="u1", username="alice", deleted=False
        )
    finally:
        await directory.close()


async def test_make_directory_postgres_dispatch_is_lazy():
    # Both accepted schemes construct a PostgresDirectory without opening a pool.
    for dsn in ("postgres://user@host/db", "postgresql://user@host/db"):
        directory = await make_directory(dsn)
        assert isinstance(directory, PostgresDirectory)
        assert directory._dsn == dsn
        assert directory._pool is None
        await directory.close()  # idempotent; no pool was ever created


@pytest.mark.parametrize(
    "dsn",
    [
        "mysql://host/db",
        "redis://host",
        "sqlite://two-slashes/path",  # only the triple-slash form is accepted
        "garbage",
        "",  # only None is the null case; empty string is an unsupported DSN
    ],
)
async def test_make_directory_unknown_scheme_raises(dsn):
    with pytest.raises(ConfigError, match="SEANCE_DIRECTORY_DSN"):
        await make_directory(dsn)


# --- Protocol conformance ---------------------------------------------------


def test_backends_conform_to_protocol():
    assert isinstance(StaticDirectory({}), MemberDirectory)
    assert isinstance(SqliteDirectory("members.db"), MemberDirectory)
    assert isinstance(PostgresDirectory("postgres://host/db"), MemberDirectory)


# --- PostgresDirectory (env-gated, real) ------------------------------------


@pytest.mark.skipif(not PG_DSN, reason="no pg dsn (set SEANCE_TEST_PG_DSN)")
async def test_postgres_lookup_real():
    schema = "seance_dir_test_" + uuid.uuid4().hex
    admin = await asyncpg.connect(PG_DSN)
    directory = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute(
            "CREATE TABLE members (id TEXT PRIMARY KEY, username TEXT, deleted_at TIMESTAMPTZ)"
        )
        await admin.execute(
            "INSERT INTO members (id, username, deleted_at) VALUES "
            "($1, $2, NULL), ($3, $4, now()), ($5, NULL, NULL)",
            "pg-live",
            "alice",
            "pg-deleted",
            "bob",
            "pg-noname",
        )
        directory = await make_directory(_dsn_with_search_path(PG_DSN, schema))
        assert isinstance(directory, PostgresDirectory)
        assert await directory.lookup("pg-live") == MemberRecord(
            id="pg-live", username="alice", deleted=False
        )
        assert await directory.lookup("pg-deleted") == MemberRecord(
            id="pg-deleted", username="bob", deleted=True
        )
        assert await directory.lookup("pg-noname") == MemberRecord(
            id="pg-noname", username="", deleted=True
        )
        assert await directory.lookup("pg-missing") is None
    finally:
        if directory is not None:
            await directory.close()
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()
