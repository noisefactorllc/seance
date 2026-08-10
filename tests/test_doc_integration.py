"""Session and hub integration coverage for the document lane."""

from __future__ import annotations

import dataclasses

from cryptography.fernet import Fernet

from app.config import Config
from app.hub import Hub
from app.identity import Identity, Kind
from app.session import Session
from app.store import Store
from tests.conftest import FakeConn


def member(uid: str, name: str | None = None) -> Identity:
    return Identity(user_id=uid, username=name or uid, kind=Kind.MEMBER)


def anon(uid: str, name: str | None = None) -> Identity:
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.ANON)


def frames(conn: FakeConn, ftype: str) -> list[dict]:
    return [frame for frame in conn.sent if frame["type"] == ftype]


def _config(**limit_overrides) -> Config:
    env = {"SEANCE_SECRET": Fernet.generate_key().decode(), "SEANCE_DB": ":memory:"}
    base = Config.from_env(env)
    limits = dataclasses.replace(base.limits, **limit_overrides) if limit_overrides else base.limits
    return dataclasses.replace(base, limits=limits)


def test_doc_create_and_reset_are_owner_only_and_broadcast_snapshot(clock):
    session = Session("sess01", "owner", _config().limits, clock)
    owner = FakeConn(member("owner", "alice"))
    writer = FakeConn(member("writer", "bob"))
    session.join(owner)
    session.join(writer)
    owner.sent.clear()
    writer.sent.clear()

    session.handle(
        writer.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "seed()",
                "default": True,
            },
        },
    )
    assert frames(writer, "error")[-1]["code"] == "forbidden"

    session.handle(
        owner.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "seed()",
                "default": True,
            },
        },
    )

    owner_snapshot = frames(owner, "doc-snapshot")[-1]
    writer_snapshot = frames(writer, "doc-snapshot")[-1]
    assert owner_snapshot["docs"] == writer_snapshot["docs"] == [
        {
            "id": "main",
            "title": "Program",
            "kind": "dsl",
            "rev": 0,
            "text": "seed()",
            "default": True,
        }
    ]
    assert session.to_snapshot()["docs"] == owner_snapshot["docs"]

    owner.sent.clear()
    writer.sent.clear()
    session.handle(
        writer.connection_id,
        {"type": "doc-reset", "docId": "main", "baseRev": 0, "text": "writer()"},
    )
    assert frames(writer, "error")[-1]["code"] == "forbidden"

    writer.sent.clear()
    session.handle(
        owner.connection_id,
        {"type": "doc-reset", "docId": "main", "baseRev": 0, "text": "owner()"},
    )
    assert frames(writer, "doc-snapshot")[-1]["docs"][0]["text"] == "owner()"
    assert frames(writer, "doc-snapshot")[-1]["docs"][0]["rev"] == 1

    owner.sent.clear()
    session.handle(
        owner.connection_id,
        {"type": "doc-reset", "docId": "main", "baseRev": 0, "text": "stale()"},
    )
    reject = frames(owner, "doc-reject")[-1]
    assert reject["docId"] == "main"
    assert reject["baseRev"] == 0
    assert reject["authorSeq"] is None
    assert reject["reason"] == "stale"
    assert reject["snapshot"]["rev"] == 1
    assert reject["snapshot"]["text"] == "owner()"
    assert frames(owner, "error") == []


def test_doc_create_rejects_aggregate_session_budget_without_mutating(clock):
    session = Session(
        "sess01",
        "owner",
        _config(max_session_bytes=180, max_doc_text=1_000).limits,
        clock,
    )
    owner = FakeConn(member("owner", "alice"))
    peer = FakeConn(member("peer", "bob"))
    session.join(owner)
    session.join(peer)
    owner.sent.clear()
    peer.sent.clear()

    session.handle(
        owner.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "x" * 80,
                "default": True,
            },
        },
    )

    assert frames(owner, "error")[-1]["code"] == "too_large"
    assert frames(peer, "doc-snapshot") == []
    assert session.docs.snapshot() == []


def test_doc_edit_acks_originator_and_relays_to_peers(clock):
    session = Session("sess01", "owner", _config().limits, clock)
    owner = FakeConn(member("owner", "alice"))
    writer = FakeConn(member("writer", "bob"))
    peer = FakeConn(member("peer", "carol"))
    session.join(owner)
    session.join(writer)
    session.join(peer)
    session.handle(
        owner.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "abcdef",
                "default": True,
            },
        },
    )
    owner.sent.clear()
    writer.sent.clear()
    peer.sent.clear()

    session.handle(
        writer.connection_id,
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 0,
            "authorSeq": 7,
            "edit": {"start": 1, "end": 3, "text": "XY"},
        },
    )

    ack = frames(writer, "doc-ack")
    relay = frames(peer, "doc-edit")
    assert len(ack) == 1
    assert len(relay) == 1
    assert frames(writer, "doc-edit") == []
    assert ack[0]["docId"] == "main"
    assert ack[0]["authorSeq"] == 7
    assert ack[0]["rev"] == 1
    assert ack[0]["edit"] == {"start": 1, "end": 3, "text": "XY"}
    assert relay[0]["rev"] == 1
    assert relay[0]["authorSeq"] == 7
    assert relay[0]["edit"] == {"start": 1, "end": 3, "text": "XY"}
    assert session.to_snapshot()["docs"][0]["text"] == "aXYdef"


def test_doc_edit_rejects_stale_window_with_snapshot(clock):
    limits = _config(max_doc_oplog=1, max_doc_oplog_bytes=1_048_576).limits
    session = Session("sess01", "owner", limits, clock)
    owner = FakeConn(member("owner", "alice"))
    writer = FakeConn(member("writer", "bob"))
    session.join(owner)
    session.join(writer)
    session.handle(
        owner.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "abcd",
                "default": True,
            },
        },
    )
    session.handle(
        writer.connection_id,
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 0,
            "authorSeq": 1,
            "edit": {"start": 1, "end": 1, "text": "X"},
        },
    )
    session.handle(
        writer.connection_id,
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 1,
            "authorSeq": 2,
            "edit": {"start": 5, "end": 5, "text": "Y"},
        },
    )
    writer.sent.clear()

    session.handle(
        writer.connection_id,
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 0,
            "authorSeq": 3,
            "edit": {"start": 0, "end": 0, "text": "Z"},
        },
    )

    reject = frames(writer, "doc-reject")
    assert len(reject) == 1
    assert reject[0]["docId"] == "main"
    assert reject[0]["baseRev"] == 0
    assert reject[0]["authorSeq"] == 3
    assert reject[0]["reason"] == "stale"
    assert reject[0]["snapshot"]["rev"] == 2
    assert reject[0]["snapshot"]["text"] == "aXbcdY"


def test_readonly_user_can_send_doc_cursor_but_not_doc_edit(clock):
    session = Session("sess01", "owner", _config().limits, clock)
    owner = FakeConn(member("owner", "alice"))
    guest = FakeConn(anon("guest", "guest"))
    peer = FakeConn(member("peer", "carol"))
    session.join(owner)
    session.join(guest)
    session.join(peer)
    session.handle(
        owner.connection_id,
        {"type": "mod-readonly", "target_user": "guest", "readonly": True},
    )
    session.handle(
        owner.connection_id,
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "text": "abcd",
                "default": True,
            },
        },
    )
    guest.sent.clear()
    peer.sent.clear()

    session.handle(
        guest.connection_id,
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 0,
            "authorSeq": 1,
            "edit": {"start": 0, "end": 0, "text": "X"},
        },
    )
    assert frames(guest, "error")[-1]["code"] == "readonly"

    guest.sent.clear()
    session.handle(
        guest.connection_id,
        {
            "type": "doc-cursor",
            "docId": "main",
            "range": {"start": 1, "end": 3},
            "direction": "backward",
        },
    )
    cursor = frames(peer, "doc-cursor")
    assert len(cursor) == 1
    assert cursor[0]["docId"] == "main"
    assert cursor[0]["user"] == "guest"
    assert cursor[0]["connectionId"] == guest.connection_id
    assert cursor[0]["range"] == {"start": 1, "end": 3}
    assert cursor[0]["direction"] == "backward"


async def test_doc_state_persists_across_freeze_thaw_and_retains_transform_window(tmp_path, clock):
    store = await Store.open(str(tmp_path / "docs.db"))
    try:
        hub = Hub(_config(), store, clock)
        session_id = await hub.create_session(
            member("owner"),
            {
                "docs": [
                    {
                        "id": "main",
                        "title": "Program",
                        "kind": "dsl",
                        "text": "abcd",
                        "default": True,
                    }
                ]
            },
        )
        owner = FakeConn(member("owner", "alice"))
        session = await hub.connect(session_id, owner)
        session.handle(
            owner.connection_id,
            {
                "type": "doc-edit",
                "docId": "main",
                "baseRev": 0,
                "authorSeq": 1,
                "edit": {"start": 1, "end": 1, "text": "X"},
            },
        )
        hub.disconnect(session, owner.connection_id)
        clock.advance(hub.limits.freeze_grace + 1)
        await hub.scan()

        rejoined = FakeConn(member("owner", "alice"))
        session2 = await hub.connect(session_id, rejoined)
        snapshot = frames(rejoined, "session-snapshot")[-1]
        assert snapshot["docs"] == [
            {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "rev": 1,
                "text": "aXbcd",
                "default": True,
            }
        ]

        rejoined.sent.clear()
        session2.handle(
            rejoined.connection_id,
            {
                "type": "doc-edit",
                "docId": "main",
                "baseRev": 0,
                "authorSeq": 2,
                "edit": {"start": 4, "end": 4, "text": "Y"},
            },
        )
        ack = frames(rejoined, "doc-ack")
        assert len(ack) == 1
        assert ack[0]["rev"] == 2
        assert ack[0]["edit"] == {"start": 5, "end": 5, "text": "Y"}
    finally:
        await store.close()


async def test_scan_deletes_frozen_sessions_older_than_ttl(tmp_path, clock):
    store = await Store.open(str(tmp_path / "retention.db"))
    try:
        config = Config.from_env(
            {
                "SEANCE_SECRET": Fernet.generate_key().decode(),
                "SEANCE_DB": ":memory:",
                "SEANCE_LIMIT_FROZEN_SESSION_TTL": "100",
            }
        )
        hub = Hub(config, store, clock)

        payload = Session("oldses", "owner", config.limits, clock).freeze_snapshot()
        payload["docs"] = [
            {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "rev": 0,
                "text": "old",
                "default": True,
                "oplog": [],
            }
        ]
        payload["frozen_at"] = int(clock.now - 200)
        await store.save_session("oldses", payload)

        fresh = Session("newses", "owner", config.limits, clock).freeze_snapshot()
        fresh["frozen_at"] = int(clock.now - 50)
        await store.save_session("newses", fresh)

        await hub.scan()

        assert await store.load_session("oldses") is None
        assert await store.load_session("newses") is not None
    finally:
        await store.close()
