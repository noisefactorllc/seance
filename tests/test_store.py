"""Tests for the SQLite persistence layer (app.store) and audit module (app.audit).

All databases are created under ``tmp_path``; the audit stdout line is captured
via ``caplog`` on the ``seance.audit`` logger. No network, no sleeping.
"""

import dataclasses
import json
import logging
import os
import sqlite3

import pytest

from app.audit import AuditEvent, log_audit
from app.store import Store


def _sample_payload() -> dict:
    """A full session payload exercising every column type, incl. JSON columns."""
    return {
        "created_by": "user-alice",
        "dialect": "noisemaker-dsl",
        "created_at": 1_751_500_000,
        "settings": {
            "locked": False,
            "guests_allowed": True,
            "guests_readonly": False,
            "explicit_owner": "user-alice",
            "readonly_users": ["user-bob"],
        },
        "state": [{"id": "bg", "value": {"hue": 200}, "seq": 5, "by": "user-alice"}],
        "data": {"grid": {"cell": {"x": 1}}},
        "poly": {
            "rev": 3,
            "programText": "noise()",
            "frame": 0,
            "nodes": [
                {"id": "n1", "kind": "call", "text": "noise", "version": 1, "parentId": None}
            ],
        },
        "docs": [
            {
                "id": "main",
                "title": "Program",
                "kind": "dsl",
                "rev": 2,
                "text": "noise()",
                "default": True,
                "oplog": [
                    {
                        "rev": 2,
                        "edit": {"start": 0, "end": 5, "text": "noise"},
                        "prior_text": "tone",
                    }
                ],
            }
        ],
        "chat": [{"message_id": "m1", "message": "hello", "user_id": "user-alice"}],
        "rev": 3,
        "seq": 42,
        "frozen_at": 1_751_500_100,
        "last_active": 1_751_500_090,
    }


async def test_open_creates_schema_and_wal(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        async with store._db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0] == "wal"
        # All four schema tables exist (ignoring SQLite internals).
        async with store._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ) as cur:
            names = [r[0] for r in await cur.fetchall()]
        assert names == ["audit", "bans", "meta", "sessions"]
    finally:
        await store.close()


async def test_schema_version_row_present(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        async with store._db.execute(
            "SELECT v FROM meta WHERE k = 'schema_version'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "3"
    finally:
        await store.close()


async def test_db_file_mode_0600(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    try:
        assert os.stat(db_path).st_mode & 0o777 == 0o600
    finally:
        await store.close()


async def test_file_backed_store_has_one_process_owner(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    second = None
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second = await Store.open(str(db_path))
    finally:
        if second is not None:
            await second.close()
        await store.close()

    reopened = await Store.open(str(db_path))
    try:
        assert os.stat(str(db_path) + ".lock").st_mode & 0o777 == 0o600
    finally:
        await reopened.close()


async def test_wal_sidecar_mode_0600(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    try:
        await store.save_session("s1", _sample_payload())  # force a WAL write
        wal = str(db_path) + "-wal"
        # The -wal companion is hardened to 0600 alongside the DB file; if a
        # checkpoint has already folded it away, there is nothing to check.
        try:
            mode = os.stat(wal).st_mode & 0o777
        except FileNotFoundError:
            pass
        else:
            assert mode == 0o600
    finally:
        await store.close()


async def test_wal_sidecars_rehardened_after_write_commit(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    try:
        await store.save_session("s1", _sample_payload())
        wal = str(db_path) + "-wal"
        try:
            os.stat(wal)
        except FileNotFoundError:
            pytest.skip("SQLite checkpointed away the WAL sidecar before it could be inspected")

        sidecars = [wal]
        shm = str(db_path) + "-shm"
        try:
            os.stat(shm)
        except FileNotFoundError:
            pass
        else:
            sidecars.append(shm)

        for sidecar in sidecars:
            os.chmod(sidecar, 0o666)  # noqa: S103 - intentionally simulates loose sidecars.
            assert os.stat(sidecar).st_mode & 0o777 == 0o666

        await store.audit(
            ts=1,
            session_id="s1",
            actor="system",
            action="test",
            target=None,
            detail={},
        )

        for sidecar in sidecars:
            assert os.stat(sidecar).st_mode & 0o777 == 0o600
    finally:
        await store.close()


async def test_save_load_round_trip(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        payload = _sample_payload()
        await store.save_session("s1", payload)
        loaded = await store.load_session("s1")
        assert loaded == payload
    finally:
        await store.close()


async def test_load_missing_returns_none(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        assert await store.load_session("nope") is None
    finally:
        await store.close()


async def test_count_sessions_tracks_persisted_rows(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        assert await store.count_sessions() == 0
        await store.save_session("s1", _sample_payload())
        await store.save_session("s2", _sample_payload())
        assert await store.count_sessions() == 2
        await store.save_session("s1", _sample_payload())
        assert await store.count_sessions() == 2
        await store.delete_session("s1")
        assert await store.count_sessions() == 1
    finally:
        await store.close()


async def test_save_missing_key_raises_keyerror(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        payload = _sample_payload()
        del payload["rev"]
        with pytest.raises(KeyError):
            await store.save_session("s1", payload)
    finally:
        await store.close()


async def test_frozen_at_none_round_trip(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        payload = _sample_payload()
        payload["frozen_at"] = None
        await store.save_session("live", payload)
        loaded = await store.load_session("live")
        assert loaded["frozen_at"] is None
        assert loaded == payload
    finally:
        await store.close()


async def test_upsert_overwrites_single_row(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        payload = _sample_payload()
        await store.save_session("s1", payload)
        updated = _sample_payload()
        updated["rev"] = 99
        updated["seq"] = 500
        await store.save_session("s1", updated)
        loaded = await store.load_session("s1")
        assert loaded["rev"] == 99
        assert loaded["seq"] == 500
        async with store._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 's1'"
        ) as cur:
            count = (await cur.fetchone())[0]
        assert count == 1
    finally:
        await store.close()


async def test_delete_session(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        await store.save_session("s1", _sample_payload())
        await store.add_ban("s1", "user-x", "owner", 1)
        await store.delete_session("s1")
        assert await store.load_session("s1") is None
        assert await store.get_bans("s1") == set()
        # Deleting a missing row is a no-op, not an error.
        await store.delete_session("s1")
    finally:
        await store.close()


async def test_list_frozen_older_than_boundary(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        a = _sample_payload()
        a["frozen_at"] = 100
        b = _sample_payload()
        b["frozen_at"] = 200
        live = _sample_payload()
        live["frozen_at"] = None
        await store.save_session("sess-a", a)
        await store.save_session("sess-b", b)
        await store.save_session("sess-live", live)

        # Strictly-less-than: the boundary value itself is excluded.
        assert await store.list_frozen_older_than(100) == []
        assert await store.list_frozen_older_than(101) == ["sess-a"]
        assert await store.list_frozen_older_than(200) == ["sess-a"]
        # Sorted ids; the never-frozen row is never returned.
        assert await store.list_frozen_older_than(201) == ["sess-a", "sess-b"]
        assert await store.list_frozen_older_than(10_000) == ["sess-a", "sess-b"]
    finally:
        await store.close()


async def test_bans_add_dup_remove_get(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    try:
        assert await store.get_bans("s1") == set()
        await store.add_ban("s1", "user-x", "user-owner", 1_751_500_000)
        # Duplicate add is idempotent (INSERT OR REPLACE).
        await store.add_ban("s1", "user-x", "user-owner", 1_751_500_050)
        await store.add_ban("s1", "user-y", "user-owner", 1_751_500_060)
        assert await store.get_bans("s1") == {"user-x", "user-y"}
        # Bans are session-scoped.
        assert await store.get_bans("other") == set()

        await store.remove_ban("s1", "user-x")
        assert await store.get_bans("s1") == {"user-y"}
        # Removing an absent ban is idempotent.
        await store.remove_ban("s1", "user-x")
        assert await store.get_bans("s1") == {"user-y"}
    finally:
        await store.close()


async def test_audit_inserts_row_and_logs(tmp_path, caplog):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    try:
        with caplog.at_level(logging.INFO, logger="seance.audit"):
            await store.audit(
                ts=1_751_500_123,
                session_id="s1",
                actor="user-owner",
                action="ban",
                target="user-x",
                detail={"reason": "spam"},
            )
        # Persisted row (read back via a raw connection; detail is JSON text).
        async with store._db.execute(
            "SELECT ts, session_id, actor, action, target, detail FROM audit"
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        ts, session_id, actor, action, target, detail = rows[0]
        assert (ts, session_id, actor, action, target) == (
            1_751_500_123,
            "s1",
            "user-owner",
            "ban",
            "user-x",
        )
        assert json.loads(detail) == {"reason": "spam"}

        # Structured stdout line on the seance.audit logger.
        records = [r for r in caplog.records if r.name == "seance.audit"]
        assert len(records) == 1
        emitted = json.loads(records[0].getMessage())
        assert emitted == {
            "ts": 1_751_500_123,
            "session": "s1",
            "actor": "user-owner",
            "action": "ban",
            "target": "user-x",
            "detail": {"reason": "spam"},
        }
    finally:
        await store.close()


async def test_audit_persists_across_raw_reopen(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    await store.audit(
        ts=7,
        session_id=None,
        actor="system",
        action="freeze",
        target=None,
        detail={},
    )
    await store.close()
    # A wholly separate connection sees the committed audit row.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT ts, session_id, actor, action, target, detail FROM audit"
        ).fetchone()
    finally:
        conn.close()
    assert row == (7, None, "system", "freeze", None, "{}")


async def test_reopen_persists_session(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    payload = _sample_payload()
    await store.save_session("s1", payload)
    await store.close()

    store2 = await Store.open(str(db_path))
    try:
        assert await store2.load_session("s1") == payload
        # WAL mode is persistent in the db header.
        async with store2._db.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0] == "wal"
    finally:
        await store2.close()


async def test_close_is_idempotent(tmp_path):
    store = await Store.open(str(tmp_path / "test.db"))
    await store.close()
    # A second close is a no-op, not an error.
    await store.close()


async def test_open_rejects_unsupported_schema_version(tmp_path):
    db_path = tmp_path / "test.db"
    store = await Store.open(str(db_path))
    await store.close()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE meta SET v = '4' WHERE k = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="unsupported schema version"):
        await Store.open(str(db_path))


def test_log_audit_emits_compact_sorted_json(caplog):
    with caplog.at_level(logging.INFO, logger="seance.audit"):
        log_audit(
            AuditEvent(
                ts=5,
                session_id=None,
                actor="system",
                action="freeze",
                target=None,
                detail={"z": 1, "a": 2},
            )
        )
    records = [r for r in caplog.records if r.name == "seance.audit"]
    assert len(records) == 1
    # Compact separators, keys sorted (including the nested detail dict), None -> null.
    assert records[0].getMessage() == (
        '{"action":"freeze","actor":"system","detail":{"a":2,"z":1},'
        '"session":null,"target":null,"ts":5}'
    )


def test_audit_event_is_frozen():
    event = AuditEvent(
        ts=1, session_id="s", actor="a", action="x", target=None, detail={}
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.ts = 2  # type: ignore[misc]
