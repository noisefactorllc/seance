"""Tests for the session ``dialect`` feature (layers-dialect design §3:
``docs/superpowers/specs/2026-07-09-layers-dialect-design.md``).

Covers, per that contract:

* ``POST /v1/sessions`` accepting an optional ``dialect`` (default/custom/
  invalid/too-long/non-string).
* ``GET /v1/sessions/{id}`` (probe) and ``welcome`` both carrying ``dialect``.
* ``freeze_snapshot`` / ``thaw`` round-tripping ``dialect``.
* the join-time dialect gate — legacy (no declared ``dialects``) vs. declared,
  admit vs. refuse (close ``4409`` / error ``dialect_mismatch``), and malformed
  ``hello.dialects`` (close ``4400`` / error ``bad_frame``).
* the store schema v2 -> v3 and v1 -> v3 migrations (adds the ``dialect``
  column, and for v1 also the ``docs`` column; legacy rows default to
  ``noisemaker-dsl``).

Follows the harness conventions of the existing suite: a local ``app_factory``
(``test_httpapi.py`` style, direct ``hub``/``store`` access via ``FakeConn``)
for HTTP/probe/store-level coverage, direct ``Session`` + ``FakeConn``
(``test_session.py`` style) for welcome/freeze/thaw, and ``tests/helpers.py``'s
``world``/``Peer`` for real end-to-end WS join-gate coverage.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from app import protocol
from app.config import Config, Limits
from app.httpapi import build_app
from app.hub import Hub
from app.identity import Identity, IdentityService, Kind
from app.session import Session
from app.store import Store
from tests.conftest import FakeConn
from tests.helpers import (
    PeerClosed,
    create_session,
    world,  # noqa: F401  (re-exported pytest fixture)
)

ALLOWED = "http://allowed.test"
MEMBER_UUID = "0f9b2d7e-1111-2222-3333-444455556666"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "SEANCE_SECRET": Fernet.generate_key().decode(),
        "SEANCE_DB": ":memory:",
        "SEANCE_ALLOWED_ORIGINS": ALLOWED,
    }
    env.update(overrides)
    return env


def _member(uid: str = MEMBER_UUID, name: str = "Ada") -> Identity:
    return Identity(user_id=uid, username=name, kind=Kind.MEMBER)


@pytest.fixture
async def app_factory(tmp_path, clock):
    """Coroutine factory building isolated (client, hub, store, identity) contexts."""
    created: list[tuple[TestClient, Store]] = []

    async def _make(**env):
        store = await Store.open(str(tmp_path / f"dialect-{len(created)}.db"))
        config = Config.from_env(_env(**env))
        hub = Hub(config, store, clock)
        identity = IdentityService(config, None, clock=clock)
        app = build_app(config, hub, identity, clock=clock)
        client = TestClient(TestServer(app))
        await client.start_server()
        created.append((client, store))
        return SimpleNamespace(
            client=client, hub=hub, store=store, identity=identity, config=config
        )

    yield _make
    for client, store in created:
        await client.close()
        await store.close()


# --------------------------------------------------------------------------- #
# POST /v1/sessions — dialect validation
# --------------------------------------------------------------------------- #


async def test_create_session_default_dialect_when_absent(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post("/v1/sessions", headers={"Origin": ALLOWED})
    assert r.status == 201
    sid = (await r.json())["session_id"]
    probe = await ctx.client.get(f"/v1/sessions/{sid}")
    assert (await probe.json())["dialect"] == protocol.DEFAULT_DIALECT


async def test_create_session_custom_dialect(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data=json.dumps({"dialect": "layers"}),
    )
    assert r.status == 201
    sid = (await r.json())["session_id"]
    probe = await ctx.client.get(f"/v1/sessions/{sid}")
    assert (await probe.json())["dialect"] == "layers"


async def test_create_session_max_length_dialect_accepted(app_factory):
    # 64 chars (the required [a-z0-9] first char + 63 more) is the valid boundary.
    ctx = await app_factory()
    dialect = "a" * 64
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data=json.dumps({"dialect": dialect}),
    )
    assert r.status == 201
    sid = (await r.json())["session_id"]
    probe = await ctx.client.get(f"/v1/sessions/{sid}")
    assert (await probe.json())["dialect"] == dialect


@pytest.mark.parametrize(
    "bad_dialect",
    [
        "Not-Valid",      # uppercase not allowed
        "-leading-dash",  # must start with [a-z0-9]
        "has space",      # space not allowed
        "café",           # unicode not allowed
        "",                # empty string never matches (needs >=1 char)
        "a" * 65,          # exceeds the 64-char cap
    ],
)
async def test_create_session_invalid_dialect_rejected(app_factory, bad_dialect):
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data=json.dumps({"dialect": bad_dialect}),
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid dialect"


async def test_create_session_non_string_dialect_rejected(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data=json.dumps({"dialect": 7}),
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid dialect"


# --------------------------------------------------------------------------- #
# GET /v1/sessions/{id} — dialect in both the live and frozen branches
# --------------------------------------------------------------------------- #


async def test_session_probe_live_includes_dialect(app_factory):
    ctx = await app_factory()
    session_id = await ctx.hub.create_session(_member(), None, "layers")
    await ctx.hub.connect(session_id, FakeConn(_member(), declared_dialects=["layers"]))
    r = await ctx.client.get(f"/v1/sessions/{session_id}")
    assert r.status == 200
    assert await r.json() == {"id": session_id, "open": True, "dialect": "layers"}


async def test_session_probe_frozen_includes_dialect(app_factory):
    ctx = await app_factory()
    session_id = await ctx.hub.create_session(_member(), None, "layers")
    r = await ctx.client.get(f"/v1/sessions/{session_id}")
    assert r.status == 200
    assert await r.json() == {"id": session_id, "open": True, "dialect": "layers"}


# --------------------------------------------------------------------------- #
# Session-level: welcome + freeze/thaw round-trip (direct, no transport)
# --------------------------------------------------------------------------- #


def test_welcome_includes_custom_dialect(clock):
    s = Session("sess01", "u-creator", Limits(), clock, dialect="layers")
    c = FakeConn(_member(), declared_dialects=["layers"])
    s.join(c)
    assert c.sent[0]["type"] == "welcome"
    assert c.sent[0]["dialect"] == "layers"


def test_welcome_default_dialect_when_unspecified(clock):
    s = Session("sess01", "u-creator", Limits(), clock)
    c = FakeConn(_member())
    s.join(c)
    assert c.sent[0]["dialect"] == protocol.DEFAULT_DIALECT


def test_freeze_includes_dialect_and_thaw_restores_it(clock):
    s = Session("s01", "owner", Limits(), clock, dialect="layers")
    payload = s.freeze_snapshot()
    assert payload["dialect"] == "layers"

    t = Session.thaw("s01", payload, Limits(), clock)
    assert t.dialect == "layers"


def test_thaw_defaults_dialect_when_payload_lacks_it(clock):
    # A hand-built payload predating this field has no "dialect" key; thaw
    # falls back to DEFAULT_DIALECT rather than raising.
    s = Session("s01", "owner", Limits(), clock)
    payload = s.freeze_snapshot()
    del payload["dialect"]

    t = Session.thaw("s01", payload, Limits(), clock)
    assert t.dialect == protocol.DEFAULT_DIALECT


# --------------------------------------------------------------------------- #
# Join gate over real transport — admits, refusals, malformed hello
# --------------------------------------------------------------------------- #


async def test_join_default_dialect_no_declaration_matches_default_session(world):  # noqa: F811
    # Back-compat invariant (design §3): absent `dialects` + a default-dialect
    # session joins exactly as it did before this feature existed.
    ctx = await world.app()
    sid, _ = await create_session(ctx)

    peer = world.peer("legacy-default")
    await peer.connect(ctx.server, sid)
    assert peer.welcome["dialect"] == protocol.DEFAULT_DIALECT


async def test_join_legacy_hello_refused_from_layers_session(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx, dialect="layers")

    peer = world.peer("legacy")
    with pytest.raises(PeerClosed) as excinfo:
        await peer.connect(ctx.server, sid)  # no dialects declared -> [DEFAULT_DIALECT]
    assert excinfo.value.close_code == protocol.CLOSE_DIALECT
    errors = peer.frames("error")
    assert errors and errors[-1]["code"] == "dialect_mismatch"


async def test_join_declared_layers_refused_from_default_session(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx)  # default dialect

    peer = world.peer("layers-client")
    with pytest.raises(PeerClosed) as excinfo:
        await peer.connect(ctx.server, sid, dialects=["layers"])
    assert excinfo.value.close_code == protocol.CLOSE_DIALECT
    errors = peer.frames("error")
    assert errors and errors[-1]["code"] == "dialect_mismatch"


async def test_join_matching_dialect_admitted(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx, dialect="layers")

    peer = world.peer("layers-client")
    await peer.connect(ctx.server, sid, dialects=["layers"])
    assert peer.welcome["dialect"] == "layers"


async def test_join_multi_entry_dialects_admitted_when_any_matches(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx, dialect="layers")

    peer = world.peer("multi")
    await peer.connect(ctx.server, sid, dialects=["noisemaker-dsl", "layers"])
    assert peer.welcome["dialect"] == "layers"


async def test_join_exactly_max_dialects_admitted_when_default_included(world):  # noqa: F811
    # Boundary: exactly MAX_HELLO_DIALECTS entries is still a valid list (the
    # limit is inclusive); the join succeeds when the default dialect is among them.
    ctx = await world.app()
    sid, _ = await create_session(ctx)  # default dialect

    peer = world.peer("max-dialects")
    at_max = [f"d{i}" for i in range(protocol.MAX_HELLO_DIALECTS - 1)] + [protocol.DEFAULT_DIALECT]
    await peer.connect(ctx.server, sid, dialects=at_max)
    assert peer.welcome["dialect"] == protocol.DEFAULT_DIALECT


async def test_join_malformed_dialects_entry_closes_4400(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx)

    peer = world.peer("malformed")
    with pytest.raises(PeerClosed) as excinfo:
        await peer.connect(ctx.server, sid, dialects=["Not Valid!"])
    assert excinfo.value.close_code == protocol.CLOSE_PROTOCOL  # 4400
    errors = peer.frames("error")
    assert errors and errors[-1]["code"] == "bad_frame"


async def test_join_empty_dialects_list_closes_4400(world):  # noqa: F811
    # An explicit empty list is a deliberate rejection, not a fold into the
    # legacy [DEFAULT_DIALECT] default (app/protocol.py _hello_dialects).
    ctx = await world.app()
    sid, _ = await create_session(ctx)

    peer = world.peer("empty-dialects")
    with pytest.raises(PeerClosed) as excinfo:
        await peer.connect(ctx.server, sid, dialects=[])
    assert excinfo.value.close_code == protocol.CLOSE_PROTOCOL  # 4400
    errors = peer.frames("error")
    assert errors and errors[-1]["code"] == "bad_frame"


async def test_join_too_many_dialects_closes_4400(world):  # noqa: F811
    ctx = await world.app()
    sid, _ = await create_session(ctx)

    peer = world.peer("too-many")
    too_many = [f"d{i}" for i in range(protocol.MAX_HELLO_DIALECTS + 1)]
    with pytest.raises(PeerClosed) as excinfo:
        await peer.connect(ctx.server, sid, dialects=too_many)
    assert excinfo.value.close_code == protocol.CLOSE_PROTOCOL  # 4400
    errors = peer.frames("error")
    assert errors and errors[-1]["code"] == "bad_frame"


# --------------------------------------------------------------------------- #
# Storage migration — v1 and v2 databases upgrade to v3 in place
# --------------------------------------------------------------------------- #

# The v2 sessions schema, before the "dialect" column existed. Kept inline
# (not derived from app.store.Store.SCHEMA, which is now the v3 schema) so
# this test builds a genuinely old-shape database on disk, the way a
# pre-upgrade deployment's file would look.
_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
  settings TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
  poly TEXT NOT NULL, docs TEXT NOT NULL, chat TEXT NOT NULL, rev INTEGER NOT NULL,
  seq INTEGER NOT NULL, frozen_at INTEGER, last_active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bans (
  session_id TEXT NOT NULL, user_id TEXT NOT NULL, banned_by TEXT NOT NULL,
  at INTEGER NOT NULL, PRIMARY KEY (session_id, user_id));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, session_id TEXT,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL);
"""


def _build_v2_database(path: str) -> None:
    """Write a fresh sqlite file shaped like a pre-dialect (schema v2) store,
    with one legacy session row that has no ``dialect`` column at all."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V2_SCHEMA)
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?)", ("schema_version", "2"))
        conn.execute(
            "INSERT INTO sessions "
            "(id, created_by, created_at, settings, state, data, poly, docs, chat, "
            "rev, seq, frozen_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy1",
                "user-old",
                1_751_000_000,
                json.dumps(
                    {
                        "locked": False, "guests_allowed": True, "guests_readonly": False,
                        "explicit_owner": None, "readonly_users": [],
                    }
                ),
                json.dumps([]),
                json.dumps({}),
                json.dumps({"rev": 0, "programText": "", "frame": None, "nodes": []}),
                json.dumps([]),
                json.dumps([]),
                0,
                0,
                1_751_000_100,
                1_751_000_100,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_v2_database_migrates_to_v3_legacy_row_defaults_dialect(tmp_path):
    db_path = str(tmp_path / "v2-legacy.db")
    _build_v2_database(db_path)

    store = await Store.open(db_path)
    try:
        async with store._db.execute(
            "SELECT v FROM meta WHERE k = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "3"

        loaded = await store.load_session("legacy1")
        assert loaded is not None
        assert loaded["created_by"] == "user-old"
        assert loaded["dialect"] == "noisemaker-dsl"

        # The migrated store is fully usable going forward.
        payload = dict(loaded)
        payload["dialect"] = "layers"
        await store.save_session("legacy1", payload)
        assert (await store.load_session("legacy1"))["dialect"] == "layers"
    finally:
        await store.close()


# The v1 sessions schema, before either the "docs" or the "dialect" column
# existed. Kept inline (not derived from app.store.Store.SCHEMA, which is now
# the v3 schema) so this test builds a genuinely old-shape database on disk,
# the way a pre-upgrade deployment's file would look.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
  settings TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
  poly TEXT NOT NULL, chat TEXT NOT NULL, rev INTEGER NOT NULL,
  seq INTEGER NOT NULL, frozen_at INTEGER, last_active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bans (
  session_id TEXT NOT NULL, user_id TEXT NOT NULL, banned_by TEXT NOT NULL,
  at INTEGER NOT NULL, PRIMARY KEY (session_id, user_id));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, session_id TEXT,
  actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT NOT NULL);
"""


def _build_v1_database(path: str) -> None:
    """Write a fresh sqlite file shaped like a pre-docs, pre-dialect (schema v1)
    store, with one legacy session row that has neither the ``docs`` nor the
    ``dialect`` column at all."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute("INSERT INTO meta (k, v) VALUES (?, ?)", ("schema_version", "1"))
        conn.execute(
            "INSERT INTO sessions "
            "(id, created_by, created_at, settings, state, data, poly, chat, "
            "rev, seq, frozen_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy0",
                "user-ancient",
                1_750_000_000,
                json.dumps(
                    {
                        "locked": False, "guests_allowed": True, "guests_readonly": False,
                        "explicit_owner": None, "readonly_users": [],
                    }
                ),
                json.dumps([]),
                json.dumps({}),
                json.dumps({"rev": 0, "programText": "", "frame": None, "nodes": []}),
                json.dumps([]),
                0,
                0,
                1_750_000_100,
                1_750_000_100,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def test_v1_database_migrates_to_v3_legacy_row_defaults_docs_and_dialect(tmp_path):
    db_path = str(tmp_path / "v1-legacy.db")
    _build_v1_database(db_path)

    store = await Store.open(db_path)
    try:
        async with store._db.execute(
            "SELECT v FROM meta WHERE k = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "3"

        loaded = await store.load_session("legacy0")
        assert loaded is not None
        assert loaded["created_by"] == "user-ancient"
        assert loaded["docs"] == []
        assert loaded["dialect"] == "noisemaker-dsl"

        # The migrated store is fully usable going forward.
        payload = dict(loaded)
        payload["dialect"] = "layers"
        await store.save_session("legacy0", payload)
        assert (await store.load_session("legacy0"))["dialect"] == "layers"
    finally:
        await store.close()
