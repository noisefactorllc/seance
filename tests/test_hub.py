"""Tests for the Hub — session registry, freeze/thaw lifecycle, sync->async bridge.

Each test opens its own ``tmp_path`` :class:`app.store.Store`, drives logic time
through the injected :class:`tests.conftest.FakeClock`, and calls the public
coroutines (:meth:`Hub.scan`, :meth:`Hub.checkpoint`) directly instead of waiting
on the background loop. No network, no sleeping.
"""

import asyncio
import dataclasses
import logging
import string

import pytest
from cryptography.fernet import Fernet

import app.hub
from app import protocol
from app.config import Config, Limits
from app.hub import Hub, HubError
from app.identity import Identity, Kind
from app.session import JoinRefused, Session
from app.store import Store
from tests.conftest import FakeConn

# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def member(uid, name=None):
    return Identity(user_id=uid, username=name or uid, kind=Kind.MEMBER)


def anon(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.ANON)


def ephemeral(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.GS_EPHEMERAL)


def _config(*, anon_can_create=True, **limit_overrides):
    env = {"SEANCE_SECRET": Fernet.generate_key().decode(), "SEANCE_DB": ":memory:"}
    base = Config.from_env(env)
    limits = dataclasses.replace(base.limits, **limit_overrides) if limit_overrides else base.limits
    return dataclasses.replace(base, anon_can_create=anon_can_create, limits=limits)


@pytest.fixture
async def hub_factory(tmp_path, clock):
    """Return a coroutine factory that builds Hubs over per-call tmp_path stores."""
    stores = []

    async def _make(**config_kwargs):
        store = await Store.open(str(tmp_path / f"hub-{len(stores)}.db"))
        stores.append(store)
        return Hub(_config(**config_kwargs), store, clock), store

    yield _make
    for store in stores:
        await store.close()


def snapshot_frame(conn):
    return next(f for f in conn.sent if f["type"] == "session-snapshot")


# --------------------------------------------------------------------------- #
# HubError
# --------------------------------------------------------------------------- #


def test_hub_error_carries_status_and_detail():
    err = HubError(503, "server session capacity reached")
    assert isinstance(err, Exception)
    assert err.status == 503
    assert err.detail == "server session capacity reached"
    assert str(err) == "server session capacity reached"


# --------------------------------------------------------------------------- #
# create_session — persistence, id, authz, caps
# --------------------------------------------------------------------------- #


async def test_create_persists_immediately_and_not_live(hub_factory, clock):
    hub, store = await hub_factory()
    sid = await hub.create_session(member("owner"))
    assert len(sid) == 6
    assert all(c in (string.ascii_letters + string.digits) for c in sid)
    # Not placed in the live map; first join thaws it.
    assert sid not in hub.live
    assert hub.live_count == 0
    stored = await store.load_session(sid)
    assert stored is not None
    assert stored["created_by"] == "owner"
    assert stored["frozen_at"] == int(clock.now)


async def test_create_applies_snapshot(hub_factory, clock):
    hub, store = await hub_factory()
    snapshot = {
        "state": [{"id": "bg", "value": {"hue": 200}}],
        "poly": {"programText": "noise()", "nodes": [], "frame": 0},
    }
    sid = await hub.create_session(member("owner"), snapshot)
    conn = FakeConn(member("owner"))
    await hub.connect(sid, conn)
    snap = snapshot_frame(conn)
    assert snap["state"] == [{"id": "bg", "value": {"hue": 200}, "seq": 1, "by": "owner"}]
    assert snap["poly"]["programText"] == "noise()"
    assert snap["poly"]["rev"] == 1
    assert snap["poly"]["frame"] == 0


async def test_create_forbidden_for_gs_ephemeral(hub_factory, clock):
    hub, store = await hub_factory()
    with pytest.raises(HubError) as exc:
        await hub.create_session(ephemeral("eph"))
    assert exc.value.status == 403


async def test_create_forbidden_for_anon_when_disabled(hub_factory, clock):
    hub, store = await hub_factory(anon_can_create=False)
    with pytest.raises(HubError) as exc:
        await hub.create_session(anon("guest"))
    assert exc.value.status == 403


async def test_create_allowed_for_anon_when_enabled(hub_factory, clock):
    hub, store = await hub_factory(anon_can_create=True)
    sid = await hub.create_session(anon("guest"))
    assert len(sid) == 6


async def test_create_rate_limited_429(hub_factory, clock):
    hub, store = await hub_factory(creates_per_identity_hour=2)
    ident = member("prolific")
    await hub.create_session(ident)
    await hub.create_session(ident)
    with pytest.raises(HubError) as exc:
        await hub.create_session(ident)
    assert exc.value.status == 429


async def test_create_server_full_503(hub_factory, clock):
    hub, store = await hub_factory(max_sessions=1)
    sid1 = await hub.create_session(member("a"))
    await hub.connect(sid1, FakeConn(member("a")))  # now live_count == 1
    with pytest.raises(HubError) as exc:
        await hub.create_session(member("b"))
    assert exc.value.status == 503


async def test_create_server_full_counts_frozen_rows(hub_factory):
    hub, store = await hub_factory(max_sessions=1)
    await hub.create_session(member("a"))
    assert hub.live_count == 0
    assert await store.count_sessions() == 1

    with pytest.raises(HubError) as exc:
        await hub.create_session(member("b"))

    assert exc.value.status == 503
    assert await store.count_sessions() == 1


async def test_create_capacity_check_and_insert_are_serialized(hub_factory, monkeypatch):
    hub, store = await hub_factory(max_sessions=1)
    original_count = store.count_sessions
    active_counts = 0
    peak_counts = 0

    async def observed_count():
        nonlocal active_counts, peak_counts
        active_counts += 1
        peak_counts = max(peak_counts, active_counts)
        await asyncio.sleep(0.01)
        try:
            return await original_count()
        finally:
            active_counts -= 1

    monkeypatch.setattr(store, "count_sessions", observed_count)
    results = await asyncio.gather(
        hub.create_session(member("a")),
        hub.create_session(member("b")),
        return_exceptions=True,
    )

    assert sum(isinstance(result, str) for result in results) == 1
    errors = [result for result in results if isinstance(result, HubError)]
    assert len(errors) == 1
    assert errors[0].status == 503
    assert peak_counts == 1
    assert await original_count() == 1


async def test_create_regenerates_id_on_collision(hub_factory, clock, monkeypatch):
    hub, store = await hub_factory()
    seed = Session("seed", "someone", Limits(), clock).freeze_snapshot()
    seed["frozen_at"] = int(clock.now)
    await store.save_session("AAAAAA", seed)
    chars = iter("AAAAAABBBBBB")
    monkeypatch.setattr(app.hub.secrets, "choice", lambda seq: next(chars))
    sid = await hub.create_session(member("owner"))
    assert sid == "BBBBBB"
    assert await store.load_session("BBBBBB") is not None


# --------------------------------------------------------------------------- #
# connect / disconnect
# --------------------------------------------------------------------------- #


async def test_connect_unknown_session_refused_4404(hub_factory, clock):
    hub, store = await hub_factory()
    with pytest.raises(JoinRefused) as exc:
        await hub.connect("ZZZZZZ", FakeConn(member("x")))
    assert exc.value.close_code == protocol.CLOSE_NOT_FOUND


async def test_connect_refuses_stored_session_over_aggregate_budget(hub_factory, clock):
    hub, store = await hub_factory(max_session_bytes=180, max_value_bytes=1_000)
    permissive = Session("stored", "owner", Limits(max_value_bytes=1_000), clock)
    permissive.state.apply("large", "x" * 100, 1, by="owner")
    payload = permissive.freeze_snapshot()
    payload["frozen_at"] = int(clock.now)
    await store.save_session("stored", payload)

    with pytest.raises(JoinRefused) as exc:
        await hub.connect("stored", FakeConn(member("owner")))

    assert exc.value.close_code == protocol.CLOSE_LIMIT
    assert "stored" not in hub.live


async def test_connect_live_join_returns_session(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    assert sid in hub.live
    assert hub.live[sid] is session
    assert hub.connection_count == 1
    assert owner.sent[0]["type"] == "welcome"
    # A second tab hits the live path and returns the same Session object.
    tab2 = FakeConn(member("owner"))
    session2 = await hub.connect(sid, tab2)
    assert session2 is session
    assert hub.connection_count == 2


async def test_connection_cap_refuses_when_full_4429(hub_factory, clock):
    hub, store = await hub_factory(max_connections=1)
    sid = await hub.create_session(member("owner"))
    await hub.connect(sid, FakeConn(member("owner")))
    assert hub.connection_count == 1
    with pytest.raises(JoinRefused) as exc:
        await hub.connect(sid, FakeConn(member("other")))
    assert exc.value.close_code == protocol.CLOSE_LIMIT


async def test_disconnect_tracks_empty_since(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    other = FakeConn(member("other"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    await hub.connect(sid, other)
    # Not empty while one connection remains.
    hub.disconnect(session, owner.connection_id)
    assert sid not in hub.empty_since
    # Empty once the last connection leaves.
    hub.disconnect(session, other.connection_id)
    assert hub.empty_since[sid] == clock.now


async def test_live_and_connection_count(hub_factory, clock):
    hub, store = await hub_factory()
    assert hub.live_count == 0
    assert hub.connection_count == 0
    sid = await hub.create_session(member("owner"))
    await hub.connect(sid, FakeConn(member("owner")))
    await hub.connect(sid, FakeConn(member("owner")))
    assert hub.live_count == 1
    assert hub.connection_count == 2


# --------------------------------------------------------------------------- #
# Freeze / thaw lifecycle via scan()
# --------------------------------------------------------------------------- #


async def test_last_leave_freezes_after_grace(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    hub.disconnect(session, owner.connection_id)
    assert sid in hub.empty_since
    # Within grace: no freeze.
    clock.advance(hub.limits.freeze_grace - 1)
    await hub.scan()
    assert sid in hub.live
    # Past grace: freeze and evict.
    clock.advance(2)
    await hub.scan()
    assert sid not in hub.live
    assert sid not in hub.empty_since
    stored = await store.load_session(sid)
    assert stored["frozen_at"] == int(clock.now)


async def test_rejoin_within_grace_cancels_freeze(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    hub.disconnect(session, owner.connection_id)
    assert sid in hub.empty_since
    clock.advance(hub.limits.freeze_grace / 2)
    # Rejoin clears the freeze timer.
    await hub.connect(sid, FakeConn(member("owner")))
    assert sid not in hub.empty_since
    clock.advance(hub.limits.freeze_grace)  # well past the original deadline
    await hub.scan()
    assert sid in hub.live  # not frozen


async def test_thaw_restores_rev_and_seq(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    session.handle(owner.connection_id, {"type": "state-update", "id": "bg", "value": {"hue": 9}})
    session.handle(
        owner.connection_id, {"type": "poly-snapshot", "programText": "noise()", "nodes": []}
    )
    pre_rev = session.poly.rev  # rev is stable across leave/freeze
    assert pre_rev == 1
    hub.disconnect(session, owner.connection_id)
    clock.advance(hub.limits.freeze_grace + 1)
    await hub.scan()
    assert sid not in hub.live
    stored = await store.load_session(sid)
    frozen_seq = stored["seq"]  # includes the leave broadcast frames
    assert stored["rev"] == pre_rev
    assert stored["frozen_at"] is not None
    # Reconnect: thaw restores seq/rev; the welcome frame reflects them.
    back = FakeConn(member("owner"))
    session2 = await hub.connect(sid, back)
    assert session2.poly.rev == pre_rev
    welcome = back.sent[0]
    assert welcome["type"] == "welcome"
    assert welcome["rev"] == pre_rev
    assert welcome["seq"] == frozen_seq + 1  # first stamped frame after thaw proves seq restore


async def test_banned_user_blocked_after_thaw_4403(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    target = FakeConn(anon("badguy"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    await hub.connect(sid, target)
    session.handle(owner.connection_id, {"type": "mod-ban", "target_user": "badguy"})
    hub.disconnect(session, owner.connection_id)  # target already kicked by the ban
    clock.advance(hub.limits.freeze_grace + 1)
    await hub.scan()
    assert sid not in hub.live
    # Banned user reconnects: thaw loads the ban, join refuses 4403.
    with pytest.raises(JoinRefused) as exc:
        await hub.connect(sid, FakeConn(anon("badguy")))
    assert exc.value.close_code == protocol.CLOSE_FORBIDDEN
    # A refused join does not leave the session live.
    assert sid not in hub.live


async def test_ban_write_through_lands_before_freeze(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    target = FakeConn(anon("badguy"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    await hub.connect(sid, target)
    session.handle(owner.connection_id, {"type": "mod-ban", "target_user": "badguy"})
    # The ban write is scheduled on the loop but not yet awaited (no await ran).
    assert hub._bg
    hub.disconnect(session, owner.connection_id)
    clock.advance(hub.limits.freeze_grace + 1)
    await hub.scan()  # settles _bg (ban write) BEFORE the freeze pass
    assert sid not in hub.live
    assert await store.get_bans(sid) == {"badguy"}
    assert (await store.load_session(sid))["frozen_at"] is not None


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #


async def test_checkpoint_public_forces_save(hub_factory, clock):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    # Store still shows the create-time frozen marker until the first checkpoint.
    assert (await store.load_session(sid))["frozen_at"] is not None
    await hub.checkpoint(session)
    stored = await store.load_session(sid)
    assert stored["frozen_at"] is None
    assert stored["seq"] == session.seq
    assert hub.last_saved_seq[sid] == session.seq
    assert hub.last_saved_at[sid] == clock.now
    assert sid in hub.live  # checkpoint never evicts


async def test_checkpoint_triggered_on_ops_threshold(hub_factory, clock):
    hub, store = await hub_factory(checkpoint_ops=10, checkpoint_secs=1_000_000.0)
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    # Below threshold immediately after connect: scan is a no-op, store stays frozen.
    await hub.scan()
    assert (await store.load_session(sid))["frozen_at"] is not None
    assert sid in hub.live
    # Drive state-updates until the op delta crosses the threshold.
    base = hub.last_saved_seq[sid]
    while session.seq - base < hub.limits.checkpoint_ops:
        session.handle(owner.connection_id, {"type": "state-update", "id": "k", "value": 1})
    await hub.scan()
    stored = await store.load_session(sid)
    assert stored["frozen_at"] is None
    assert stored["seq"] == session.seq
    assert hub.last_saved_seq[sid] == session.seq
    assert sid in hub.live


async def test_idle_session_is_not_rewritten_when_nothing_changed(hub_factory, clock):
    """A checkpoint is for durability of changes, not a heartbeat.

    ``checkpoint_secs`` measures time since the last save, so an idle session
    used to be re-serialized and rewritten to SQLite on every pass forever.
    """
    hub, store = await hub_factory(checkpoint_secs=30.0)
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)

    # The join is a change (welcome plus snapshot bump seq), so the first pass
    # past checkpoint_secs still saves and clears the create-time frozen marker.
    clock.advance(hub.limits.checkpoint_secs + 1)
    await hub.scan()
    assert (await store.load_session(sid))["frozen_at"] is None
    first_save = hub.last_saved_at[sid]

    for _ in range(5):
        clock.advance(hub.limits.checkpoint_secs + 1)
        await hub.scan()
    assert hub.last_saved_at[sid] == first_save  # no save happened
    assert sid in hub.live

    session.handle(owner.connection_id, {"type": "state-update", "id": "k", "value": 1})
    clock.advance(hub.limits.checkpoint_secs + 1)
    await hub.scan()
    assert hub.last_saved_at[sid] > first_save
    assert (await store.load_session(sid))["seq"] == session.seq


# --------------------------------------------------------------------------- #
# start / stop
# --------------------------------------------------------------------------- #


async def test_stop_freezes_all_live(hub_factory, clock):
    hub, store = await hub_factory()
    sid1 = await hub.create_session(member("a"))
    sid2 = await hub.create_session(member("b"))
    await hub.connect(sid1, FakeConn(member("a")))
    await hub.connect(sid2, FakeConn(member("b")))
    assert hub.live_count == 2
    await hub.stop()
    assert hub.live_count == 0
    assert hub.live == {}
    for sid in (sid1, sid2):
        stored = await store.load_session(sid)
        assert stored["frozen_at"] == int(clock.now)
    # Idempotent.
    await hub.stop()
    assert hub.live_count == 0


async def test_start_then_stop_cancels_loop(hub_factory, clock):
    hub, store = await hub_factory()
    await hub.start()
    assert hub._loop_task is not None
    # start() is idempotent — no second task.
    task = hub._loop_task
    await hub.start()
    assert hub._loop_task is task
    await hub.stop()
    assert hub._loop_task is None


# --------------------------------------------------------------------------- #
# Sync->async scheduling bridge (both paths)
# --------------------------------------------------------------------------- #


def test_schedule_defers_without_running_loop(clock):
    # The scheduler primitives never touch the store, so None is safe here.
    hub = Hub(_config(), None, clock)
    ran = []

    async def op():
        ran.append(True)

    hub._schedule(op())  # plain sync frame: no running loop -> deferred
    assert len(hub._pending) == 1
    assert hub._bg == set()
    assert ran == []
    asyncio.run(hub._drain_pending())  # a fresh loop drains the deferred coro
    assert ran == [True]
    assert hub._pending == []


async def test_schedule_uses_task_when_loop_running(hub_factory, clock):
    hub, store = await hub_factory()
    ran = []

    async def op():
        ran.append(True)

    hub._schedule(op())  # running loop: creates a task tracked in _bg
    assert len(hub._bg) == 1
    assert hub._pending == []
    await hub._settle_bg()
    assert ran == [True]


async def test_pending_drained_on_next_async_call(hub_factory, clock):
    hub, store = await hub_factory()
    ran = []

    async def op():
        ran.append(True)

    hub._pending.append(op())  # simulate a callback fired with no running loop
    await hub.scan()  # any async hub method drains _pending first
    assert ran == [True]
    assert hub._pending == []


# --------------------------------------------------------------------------- #
# Concurrency — thaw/freeze races
# --------------------------------------------------------------------------- #


async def test_concurrent_first_connect_single_thaw(hub_factory, clock):
    hub, store = await hub_factory()
    sid = await hub.create_session(member("owner"))
    conn_a = FakeConn(member("alice"))
    conn_b = FakeConn(member("bob"))
    # Two first-connects race the thaw path; the per-id lock must fold them into a
    # single live Session rather than let each thaw a rival copy (split-brain).
    sess_a, sess_b = await asyncio.gather(hub.connect(sid, conn_a), hub.connect(sid, conn_b))
    assert sid in hub.live
    live = hub.live[sid]
    assert sess_a is live
    assert sess_b is live
    assert conn_a.connection_id in live.conns
    assert conn_b.connection_id in live.conns
    assert hub.connection_count == 2
    assert len(live._roster()) == 2  # both users present in one shared session
    # A state-update from A reaches B: they share the same live session.
    conn_b.sent.clear()
    live.handle(conn_a.connection_id, {"type": "state-update", "id": "bg", "value": {"hue": 7}})
    assert any(
        f.get("type") == "state-update" and f.get("value") == {"hue": 7} for f in conn_b.sent
    )


async def test_connect_during_freeze_save_not_orphaned(hub_factory, clock, monkeypatch):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    hub.disconnect(session, owner.connection_id)  # empty -> freeze armed
    assert sid in hub.empty_since
    clock.advance(hub.limits.freeze_grace + 1)  # past grace: scan will freeze

    # Hold the freeze's save_session so a connect can slip in during the await.
    release = asyncio.Event()
    real_save = store.save_session

    async def slow_save(session_id, payload):
        await release.wait()
        await real_save(session_id, payload)

    monkeypatch.setattr(store, "save_session", slow_save)

    scan_task = asyncio.create_task(hub.scan())
    await asyncio.sleep(0)  # let scan reach the held save (holding the per-id lock)
    assert not scan_task.done()
    assert sid in hub.live  # not evicted yet — the save is blocked

    newconn = FakeConn(member("owner"))
    joined = await hub.connect(sid, newconn)  # live path joins the still-live session
    assert joined is session
    assert sid not in hub.empty_since  # rejoin cancelled the freeze timer

    release.set()
    await scan_task

    # Invariant: the client is attached to a Session still present in hub.live.
    assert sid in hub.live
    assert hub.live[sid] is session
    assert newconn.connection_id in hub.live[sid].conns
    assert hub.connection_count == 1


async def test_retention_delete_does_not_orphan_racing_thaw(hub_factory, clock, monkeypatch):
    hub, store = await hub_factory(frozen_session_ttl=100.0)
    sid = await hub.create_session(member("owner"))
    clock.advance(200)

    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    real_delete = store.delete_session

    async def slow_delete(session_id):
        entered_delete.set()
        await release_delete.wait()
        await real_delete(session_id)

    monkeypatch.setattr(store, "delete_session", slow_delete)

    scan_task = asyncio.create_task(hub.scan())
    await entered_delete.wait()

    racing_conn = FakeConn(member("owner"))
    connect_task = asyncio.create_task(hub.connect(sid, racing_conn))
    await asyncio.sleep(0)

    release_delete.set()
    await scan_task

    try:
        joined = await connect_task
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_NOT_FOUND
        assert sid not in hub.live
    else:
        assert joined is hub.live[sid]
        assert racing_conn.connection_id in hub.live[sid].conns
        assert await store.load_session(sid) is not None


async def test_unclaimed_session_is_swept_on_the_short_ttl(hub_factory, clock):
    """A created session nobody joined holds a row against the global cap.

    Rows are what MAX_SESSIONS counts, so cheap creates could otherwise fill it
    for a whole retention window and answer 503 to everyone.
    """
    hub, store = await hub_factory(frozen_session_ttl=86_400.0, unclaimed_session_ttl=100.0)
    sid = await hub.create_session(member("owner"))
    assert (await store.load_session(sid))["first_joined_at"] is None

    clock.advance(50.0)
    await hub.scan()
    assert await store.load_session(sid) is not None  # inside the short TTL

    clock.advance(60.0)
    await hub.scan()
    assert await store.load_session(sid) is None
    assert await store.count_sessions() == 0


async def test_a_joined_session_keeps_the_long_retention(hub_factory, clock):
    """The marker is durable: a session that was used is not disposable."""
    hub, store = await hub_factory(frozen_session_ttl=86_400.0, unclaimed_session_ttl=100.0)
    sid = await hub.create_session(member("owner"))
    owner = FakeConn(member("owner"))
    session = await hub.connect(sid, owner)
    joined_at = session.first_joined_at
    assert joined_at is not None

    hub.disconnect(session, owner.connection_id)
    clock.advance(hub.limits.freeze_grace + 1)
    await hub.scan()  # freezes it
    assert (await store.load_session(sid))["first_joined_at"] == joined_at

    clock.advance(1_000.0)  # far past the unclaimed TTL, far short of the long one
    await hub.scan()
    assert await store.load_session(sid) is not None

    # And the marker survives a thaw, so it is not re-armed by rejoining later.
    rejoined = await hub.connect(sid, FakeConn(member("owner")))
    assert rejoined.first_joined_at == joined_at


async def test_unclaimed_sweep_disabled_when_ttl_not_positive(hub_factory, clock):
    hub, store = await hub_factory(frozen_session_ttl=1_000_000.0, unclaimed_session_ttl=0.0)
    sid = await hub.create_session(member("owner"))

    clock.advance(100_000.0)
    await hub.scan()

    assert await store.load_session(sid) is not None


async def test_failed_ban_sink_logged_not_lost(hub_factory, clock, caplog, monkeypatch):
    hub, store = await hub_factory()
    owner = FakeConn(member("owner"))
    target = FakeConn(anon("badguy"))
    sid = await hub.create_session(member("owner"))
    session = await hub.connect(sid, owner)
    await hub.connect(sid, target)

    async def boom(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "add_ban", boom)

    with caplog.at_level(logging.ERROR, logger="seance.hub"):
        session.handle(owner.connection_id, {"type": "mod-ban", "target_user": "badguy"})
        assert hub._bg  # the guarded ban write is in flight
        await hub._settle_bg()  # returns normally — the guard consumed the failure

    records = [r for r in caplog.records if r.name == "seance.hub"]
    assert any("background store write failed" in r.getMessage() for r in records)
    assert any(r.exc_info for r in records)  # real exception captured, not swallowed blind


async def test_invalid_snapshot_raises_hub_error_400(hub_factory, clock):
    hub, store = await hub_factory()
    # Missing programText (KeyError) and a non-list state (TypeError) must each
    # surface as a 400, not escape as a 500.
    cases = (
        (member("a"), {"poly": {"nodes": "not-a-list"}}),
        (member("b"), {"state": "not-a-list"}),
        (member("c"), {"poly": {"programText": "x", "nodes": "ab"}}),
        (member("d"), {"poly": {"programText": "x", "nodes": [1, 2]}}),
        (member("e"), "garbage"),
        (member("f"), [1, 2]),
        (member("g"), 5),
    )
    for ident, bad in cases:
        with pytest.raises(HubError) as exc:
            await hub.create_session(ident, bad)
        assert exc.value.status == 400


async def test_create_rejects_snapshot_over_aggregate_session_budget(hub_factory, clock):
    hub, store = await hub_factory(max_session_bytes=180, max_value_bytes=1_000)

    with pytest.raises(HubError) as exc:
        await hub.create_session(
            member("owner"),
            {"state": [{"id": "large", "value": "x" * 100}]},
        )

    assert exc.value.status == 413
    assert await store.count_sessions() == 0


async def test_snapshot_node_id_too_long_rejected_400(hub_factory, clock):
    # The create-path snapshot bypasses the WS validators, so an over-length node
    # id (>200) must be caught before the engine call and surface as a 400.
    hub, store = await hub_factory()
    snapshot = {
        "poly": {"programText": "p", "nodes": [{"id": "x" * 201, "kind": "grp", "text": ""}]}
    }
    with pytest.raises(HubError) as exc:
        await hub.create_session(member("a"), snapshot)
    assert exc.value.status == 400
    # a 200-char id is within the cap and creates the session
    ok = {"poly": {"programText": "p", "nodes": [{"id": "x" * 200, "kind": "grp", "text": ""}]}}
    assert len(await hub.create_session(member("a"), ok)) == 6


async def test_snapshot_node_kind_too_long_rejected_400(hub_factory, clock):
    # An over-length node kind (>32) is likewise rejected as a 400.
    hub, store = await hub_factory()
    snapshot = {
        "poly": {"programText": "p", "nodes": [{"id": "root", "kind": "k" * 33, "text": ""}]}
    }
    with pytest.raises(HubError) as exc:
        await hub.create_session(member("b"), snapshot)
    assert exc.value.status == 400
    # a 32-char kind is within the cap and creates the session
    ok = {"poly": {"programText": "p", "nodes": [{"id": "root", "kind": "k" * 32, "text": ""}]}}
    assert len(await hub.create_session(member("b"), ok)) == 6


async def test_close_connections_schedules_close_on_every_live_connection(hub_factory, clock):
    hub, _ = await hub_factory()
    sid_a = await hub.create_session(member("u1"))
    sid_b = await hub.create_session(member("u1"))
    conns = [FakeConn(member("u1")), FakeConn(member("u2")), FakeConn(member("u3"))]
    await hub.connect(sid_a, conns[0])
    await hub.connect(sid_a, conns[1])
    await hub.connect(sid_b, conns[2])

    hub.close_connections(1001, "server shutting down")

    assert [c.closed for c in conns] == [(1001, "server shutting down")] * 3
