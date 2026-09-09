"""Tests for app.transport (WebSocket transport) and app.main (app wiring).

The integration tests stand up a real :class:`aiohttp.web.Application` — built
either directly (:func:`app.httpapi.build_app` + :func:`make_websocket_handler`)
or end-to-end through :func:`app.main.create_app` — behind a real
:class:`aiohttp.test_utils.TestServer` on a 127.0.0.1 socket, and drive it with
real ``aiohttp`` WebSocket clients. Every client sends ``Origin: http://testorigin``
and the test config allows exactly that origin. Real-socket waits are bounded with
``asyncio.timeout``; there are no unbounded sleeps.

The two backpressure/injection tests exercise :class:`WsConn` directly (no writer
task running), which is the deterministic way to assert cursor shedding, the
slow-consumer close, and the one-shot welcome anon-token injection.
"""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet

from app import __version__
from app.config import Config, Limits
from app.httpapi import build_app
from app.hub import Hub
from app.identity import Identity, IdentityService, Kind
from app.main import create_app
from app.store import Store
from app.transport import WsConn, make_websocket_handler

ORIGIN = "http://testorigin"
EVIL = "http://evil.test"
CLOSE_TYPES = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
MEMBER_UUID = "0f9b2d7e-1111-2222-3333-444455556666"


def _member(uid: str = MEMBER_UUID, name: str = "Ada") -> Identity:
    return Identity(user_id=uid, username=name, kind=Kind.MEMBER)


def _anon(uid: str = "anon-1", name: str = "guest-anon1") -> Identity:
    return Identity(user_id=uid, username=name, kind=Kind.ANON)


# --------------------------------------------------------------------------- #
# Real-socket helpers (all bounded)
# --------------------------------------------------------------------------- #


async def _recv_type(ws, mtype, *, within=2.0):
    """Receive frames until one of ``mtype`` arrives; fail on close/timeout."""
    async with asyncio.timeout(within):
        while True:
            msg = await ws.receive()
            if msg.type is WSMsgType.TEXT:
                body = json.loads(msg.data)
                if body.get("type") == mtype:
                    return body
            elif msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
                raise AssertionError(f"socket closed (code={ws.close_code}) awaiting {mtype!r}")


async def _drain(ws, *, quiet=0.2):
    """Read all currently-pending frames, returning once the socket goes quiet."""
    frames = []
    while True:
        try:
            msg = await ws.receive(timeout=quiet)
        except TimeoutError:
            return frames
        if msg.type is WSMsgType.TEXT:
            frames.append(json.loads(msg.data))
        elif msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
            return frames


async def _await_close(ws, *, within=3.0):
    """Wait for the server to close the socket; return the close code."""
    async with asyncio.timeout(within):
        while True:
            msg = await ws.receive()
            if msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
                return ws.close_code


async def _welcome(ws, *, within=2.0):
    return await _recv_type(ws, "welcome", within=within)


# --------------------------------------------------------------------------- #
# Fixture: a real server + real ws clients, torn down cleanly
# --------------------------------------------------------------------------- #


@pytest.fixture
async def harness(tmp_path):
    servers: list[SimpleNamespace] = []
    client_sessions: list[aiohttp.ClientSession] = []

    async def make_server(**env):
        base = {
            "SEANCE_SECRET": Fernet.generate_key().decode(),
            "SEANCE_DB": str(tmp_path / f"transport-{len(servers)}.db"),
            "SEANCE_ALLOWED_ORIGINS": ORIGIN,
        }
        base.update(env)
        config = Config.from_env(base)
        store = await Store.open(config.db_path)
        hub = Hub(config, store)
        identity = IdentityService(config, None)
        handler = make_websocket_handler(config, hub, identity)
        app = build_app(config, hub, identity, ws_handler=handler)
        await hub.start()
        server = TestServer(app)
        await server.start_server()
        ctx = SimpleNamespace(
            server=server, hub=hub, identity=identity, config=config, store=store
        )
        servers.append(ctx)
        return ctx

    async def open_ws(ctx, session_id, *, hello=None, send_hello=True, origin=ORIGIN):
        session = aiohttp.ClientSession()
        client_sessions.append(session)
        url = ctx.server.make_url(f"/v1/sessions/{session_id}/ws")
        ws = await session.ws_connect(url, headers={"Origin": origin})
        if send_hello:
            frame = {"type": "hello", "protocol": 1}
            if hello:
                frame.update(hello)
            await ws.send_str(json.dumps(frame))
        return ws

    yield SimpleNamespace(server=make_server, ws=open_ws)

    for session in client_sessions:
        await session.close()
    for ctx in servers:
        await ctx.server.close()
        await ctx.hub.stop()
        await ctx.store.close()


# --------------------------------------------------------------------------- #
# Handshake: origin + join limiter
# --------------------------------------------------------------------------- #


async def test_bad_origin_rejected_pre_upgrade(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
            await session.ws_connect(
                ctx.server.make_url(f"/v1/sessions/{sid}/ws"), headers={"Origin": EVIL}
            )
    assert excinfo.value.status == 403


async def test_missing_origin_rejected_pre_upgrade(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
            await session.ws_connect(ctx.server.make_url(f"/v1/sessions/{sid}/ws"))
    assert excinfo.value.status == 403


async def test_join_rate_limit_429_pre_upgrade(harness):
    ctx = await harness.server(SEANCE_LIMIT_JOINS_PER_IP_MIN="1")
    sid = await ctx.hub.create_session(_member())
    url = ctx.server.make_url(f"/v1/sessions/{sid}/ws")
    async with aiohttp.ClientSession() as s1, aiohttp.ClientSession() as s2:
        ws1 = await s1.ws_connect(url, headers={"Origin": ORIGIN})  # consumes the budget
        with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
            await s2.ws_connect(url, headers={"Origin": ORIGIN})
        await ws1.close()
    assert excinfo.value.status == 429


# --------------------------------------------------------------------------- #
# Handshake: hello sequencing
# --------------------------------------------------------------------------- #


async def test_pre_hello_frame_closes_4400(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid, send_hello=False)
    await ws.send_str(json.dumps({"type": "state-update", "id": "x", "value": 1}))
    assert await _await_close(ws) == 4400


async def test_hello_unsupported_protocol_closes_4400(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid, send_hello=False)
    await ws.send_str(json.dumps({"type": "hello", "protocol": 2}))
    assert await _await_close(ws) == 4400


async def test_hello_anon_welcome_carries_token_then_snapshot(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    welcome = await _welcome(ws)
    assert welcome["you"]["kind"] == "anon"
    assert welcome["anon_token"]
    snapshot = await _recv_type(ws, "session-snapshot")
    assert "state" in snapshot and "poly" in snapshot


async def test_anon_token_reconnect_is_stable_identity(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws1 = await harness.ws(ctx, sid)
    welcome = await _welcome(ws1)
    token = welcome["anon_token"]
    uid = welcome["you"]["user_id"]
    await ws1.close()

    ws2 = await harness.ws(ctx, sid, hello={"anon_token": token})
    welcome2 = await _welcome(ws2)
    assert "anon_token" not in welcome2
    assert welcome2["you"]["user_id"] == uid


# --------------------------------------------------------------------------- #
# Convergence / fan-out
# --------------------------------------------------------------------------- #


async def test_two_clients_converge_on_state_update(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws_a = await harness.ws(ctx, sid)
    wa = await _welcome(ws_a)
    a_uid, a_name = wa["you"]["user_id"], wa["you"]["username"]
    ws_b = await harness.ws(ctx, sid)
    await _welcome(ws_b)
    await _drain(ws_a)
    await _drain(ws_b)

    await ws_a.send_str(json.dumps({"type": "state-update", "id": "x", "value": 42}))

    frame = await _recv_type(ws_b, "state-update")
    assert frame["id"] == "x"
    assert frame["value"] == 42
    assert frame["user_id"] == a_uid
    assert frame["username"] == a_name
    assert frame["session"] == sid
    assert isinstance(frame["seq"], int)

    # The originator receives no echo of its own broadcast.
    with pytest.raises(TimeoutError):
        await ws_a.receive(timeout=0.4)


# --------------------------------------------------------------------------- #
# Moderation over real sockets
# --------------------------------------------------------------------------- #


async def _owner_and_guest(harness, ctx):
    """Owner A (session creator, connected with its token) plus a fresh guest B."""
    a_ident, a_token = ctx.identity.mint_anon()
    sid = await ctx.hub.create_session(a_ident)
    ws_a = await harness.ws(ctx, sid, hello={"anon_token": a_token})
    wa = await _welcome(ws_a)
    assert wa["you"]["is_owner"] is True
    ws_b = await harness.ws(ctx, sid)
    wb = await _welcome(ws_b)
    await _drain(ws_a)
    await _drain(ws_b)
    return sid, ws_a, wb["you"]["user_id"], ws_b, wb["anon_token"]


async def test_owner_kick_closes_target_4401_and_rejoin_works(harness):
    ctx = await harness.server()
    sid, ws_a, b_uid, ws_b, b_token = await _owner_and_guest(harness, ctx)

    await ws_a.send_str(json.dumps({"type": "mod-kick", "target_user": b_uid}))
    assert await _await_close(ws_b) == 4401

    # Kick is not a ban: the same identity can rejoin and write again.
    ws_b2 = await harness.ws(ctx, sid, hello={"anon_token": b_token})
    w2 = await _welcome(ws_b2)
    assert w2["you"]["user_id"] == b_uid
    await _drain(ws_a)
    await _drain(ws_b2)

    await ws_b2.send_str(json.dumps({"type": "state-update", "id": "y", "value": 7}))
    frame = await _recv_type(ws_a, "state-update")
    assert frame["value"] == 7
    assert frame["user_id"] == b_uid


async def test_hello_mint_past_the_budget_closes_4429(harness):
    """A hello that mints a guest identity is metered like every other mint path."""
    ctx = await harness.server(SEANCE_LIMIT_ANON_MINTS_PER_IP_HOUR="1")
    sid = await ctx.hub.create_session(_member())

    first = await harness.ws(ctx, sid)
    await _welcome(first)

    second = await harness.ws(ctx, sid)
    frames = await _drain(second)
    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "rate_limited"
    assert await _await_close(second) == 4429


async def test_hello_with_a_valid_anon_token_is_not_charged_a_mint(harness):
    ctx = await harness.server(SEANCE_LIMIT_ANON_MINTS_PER_IP_HOUR="1")
    sid = await ctx.hub.create_session(_member())

    first = await harness.ws(ctx, sid)
    welcome = await _welcome(first)
    token = welcome["anon_token"]

    for _ in range(3):
        again = await harness.ws(ctx, sid, hello={"anon_token": token})
        assert (await _welcome(again))["you"]["user_id"] == welcome["you"]["user_id"]


async def test_owner_ban_blocks_rejoin_4403(harness):
    ctx = await harness.server()
    sid, ws_a, b_uid, ws_b, b_token = await _owner_and_guest(harness, ctx)

    await ws_a.send_str(json.dumps({"type": "mod-ban", "target_user": b_uid}))
    assert await _await_close(ws_b) == 4401  # the live connection is kicked

    ws_b2 = await harness.ws(ctx, sid, hello={"anon_token": b_token})
    assert await _await_close(ws_b2) == 4403  # rejoin refused by the ban


# --------------------------------------------------------------------------- #
# Rate limiting / abuse
# --------------------------------------------------------------------------- #


async def test_rate_limit_error_then_sustained_flood_closes_4429(harness):
    ctx = await harness.server(
        SEANCE_LIMIT_FAST_RATE="1.0",
        SEANCE_LIMIT_FAST_BURST="2",
        SEANCE_LIMIT_ABUSE_WINDOW="0.3",
    )
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)

    saw_rate_limited = False
    close_code = None
    async with asyncio.timeout(5):
        for i in range(300):
            try:
                await ws.send_str(json.dumps({"type": "state-update", "id": "k", "value": i}))
            except (ConnectionResetError, aiohttp.ClientError):
                pass
            try:
                while True:
                    msg = await ws.receive(timeout=0.03)
                    if msg.type is WSMsgType.TEXT:
                        body = json.loads(msg.data)
                        if body.get("type") == "error" and body.get("code") == "rate_limited":
                            saw_rate_limited = True
                    elif msg.type in CLOSE_TYPES:
                        close_code = ws.close_code
                        break
            except TimeoutError:
                pass
            if close_code is not None:
                break
    assert saw_rate_limited
    assert close_code == 4429


# --------------------------------------------------------------------------- #
# Violation handling
# --------------------------------------------------------------------------- #


async def _flood_until_close(ws, sender, *, error_code, count=3):
    for _ in range(count):
        await sender(ws)
    errors = 0
    close_code = None
    async with asyncio.timeout(5):
        while True:
            msg = await ws.receive()
            if msg.type is WSMsgType.TEXT:
                body = json.loads(msg.data)
                if body.get("code") == error_code:
                    errors += 1
            elif msg.type in CLOSE_TYPES:
                close_code = ws.close_code
                break
    return errors, close_code


async def test_oversize_nonsnapshot_frame_closes_after_violations(harness):
    ctx = await harness.server(SEANCE_LIMIT_MAX_FRAME="512")
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)

    big = "x" * 600  # raw frame > 512 bytes, value still well under max_value_bytes

    async def sender(w):
        await w.send_str(json.dumps({"type": "state-update", "id": "k", "value": big}))

    errors, close_code = await _flood_until_close(ws, sender, error_code="too_large")
    assert errors >= 1
    assert close_code == 4400


async def test_frame_over_max_msg_size_ends_connection(harness):
    # max_snapshot_frame caps the WebSocketResponse max_msg_size; a single text
    # frame larger than it trips aiohttp's framing guard, which ends the
    # connection (1009 "message too big") before the read loop ever parses it.
    ctx = await harness.server(
        SEANCE_LIMIT_MAX_SNAPSHOT_FRAME="2048",
        SEANCE_LIMIT_MAX_FRAME="1024",
    )
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)

    huge = "x" * 4096  # a single frame well over max_snapshot_frame (2048)
    try:
        await ws.send_str(json.dumps({"type": "state-update", "id": "k", "value": huge}))
    except (ConnectionResetError, aiohttp.ClientError):
        pass

    close_code = await _await_close(ws, within=5.0)
    assert close_code in (1009, None)  # 1009 when the close code is surfaced


async def test_malformed_json_closes_after_violations(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)

    async def sender(w):
        await w.send_str("this is definitely not json")

    errors, close_code = await _flood_until_close(ws, sender, error_code="bad_frame")
    assert errors >= 1
    assert close_code == 4400


async def test_binary_frame_counts_as_violation(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)

    async def sender(w):
        await w.send_bytes(b"\x00\x01\x02\x03")

    errors, close_code = await _flood_until_close(ws, sender, error_code="bad_frame")
    assert errors >= 1
    assert close_code == 4400


# --------------------------------------------------------------------------- #
# Keepalive
# --------------------------------------------------------------------------- #


async def test_app_level_ping_returns_pong(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)
    await _drain(ws)
    await ws.send_str(json.dumps({"type": "ping"}))
    pong = await _recv_type(ws, "pong")
    assert pong["type"] == "pong"


async def test_ws_protocol_ping_is_answered_with_pong(harness):
    ctx = await harness.server()
    sid = await ctx.hub.create_session(_member())
    # autoping=False so the server's PONG is surfaced to receive() instead of
    # being consumed by the aiohttp client internally (RFC 6455 §5.5.2).
    async with aiohttp.ClientSession() as session:
        url = ctx.server.make_url(f"/v1/sessions/{sid}/ws")
        ws = await session.ws_connect(url, headers={"Origin": ORIGIN}, autoping=False)
        await ws.send_str(json.dumps({"type": "hello", "protocol": 1}))
        await _welcome(ws)
        await _drain(ws)

        await ws.ping(b"x")
        async with asyncio.timeout(5):
            while True:
                msg = await ws.receive()
                if msg.type is WSMsgType.PONG:
                    break
                if msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
                    raise AssertionError(f"socket closed (code={ws.close_code}) awaiting a PONG")

        # The connection stays open afterward: an app-level ping still round-trips.
        await ws.send_str(json.dumps({"type": "ping"}))
        pong = await _recv_type(ws, "pong")
        assert pong["type"] == "pong"
        assert not ws.closed


async def test_heartbeat_keeps_connection_alive(harness):
    ctx = await harness.server(
        SEANCE_LIMIT_PING_INTERVAL="0.1", SEANCE_LIMIT_PING_TIMEOUT="0.3"
    )
    sid = await ctx.hub.create_session(_member())
    ws = await harness.ws(ctx, sid)
    await _welcome(ws)

    saw_close = False
    async with asyncio.timeout(3):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.5
        while loop.time() < deadline:
            try:
                msg = await ws.receive(timeout=0.1)
            except TimeoutError:
                continue
            if msg.type in CLOSE_TYPES:
                saw_close = True
                break
    assert not saw_close
    assert not ws.closed


# --------------------------------------------------------------------------- #
# WsConn unit tests: backpressure + anon-token injection
# --------------------------------------------------------------------------- #


async def test_wsconn_welcome_anon_token_injected_exactly_once(clock):
    conn = WsConn(SimpleNamespace(closed=False), _anon(), "c1", Limits(), clock)
    conn.pending_anon_token = "TICKET-TOKEN"

    assert conn.send_json({"type": "welcome", "protocol": 1, "you": {"user_id": "u"}}) is True
    assert conn.send_json({"type": "welcome", "protocol": 1}) is True

    frames = conn.queued_frames()
    assert frames[0]["anon_token"] == "TICKET-TOKEN"
    assert "anon_token" not in frames[1]
    assert conn.pending_anon_token is None


async def test_wsconn_sheds_cursors_then_closes_slow_consumer(clock):
    limits = Limits(send_queue_frames=8, send_queue_bytes=1_048_576)
    conn = WsConn(SimpleNamespace(closed=False), _anon(), "c1", limits, clock)

    def cursor(user, i):
        return {"type": "poly-cursor", "user_id": user, "mode": "node", "node_id": f"n{i}"}

    def normal(i):
        return {"type": "state-update", "user_id": "A", "id": f"k{i}", "value": i}

    # Fill the queue to its 8-frame cap with interleaved cursors and normal frames.
    for frame in (
        cursor("A", 0), cursor("B", 0), normal(0), cursor("A", 1),
        cursor("B", 1), normal(1), cursor("A", 2), cursor("B", 2),
    ):
        assert conn.send_json(frame) is True
    assert len(conn.queued_frames()) == 8

    # The next enqueue overflows: cursors are shed first, keeping only the newest
    # per user_id, which frees room for the new normal frame.
    assert conn.send_json(normal(2)) is True
    cursors = [f for f in conn.queued_frames() if f["type"] == "poly-cursor"]
    assert {(c["user_id"], c["node_id"]) for c in cursors} == {("A", "n2"), ("B", "n2")}
    assert not conn.closing

    # Continued overflow with non-cursor frames has nothing left to shed, so the
    # connection is scheduled to close 4408 and the send is refused.
    for i in range(3, 6):
        assert conn.send_json(normal(i)) is True
    assert conn.send_json(normal(6)) is False
    assert conn.closing
    assert conn.close_info == (4408, "slow consumer")


async def test_wsconn_sheds_doc_cursors_per_user_without_dropping_poly_cursors(clock):
    limits = Limits(send_queue_frames=6, send_queue_bytes=1_048_576)
    conn = WsConn(SimpleNamespace(closed=False), _anon(), "c1", limits, clock)

    def poly(user, i):
        return {"type": "poly-cursor", "user_id": user, "mode": "node", "node_id": f"p{i}"}

    def doc(user, i):
        return {
            "type": "doc-cursor",
            "user_id": user,
            "docId": "main",
            "range": {"start": i, "end": i},
        }

    def normal(i):
        return {"type": "state-update", "user_id": "A", "id": f"k{i}", "value": i}

    for frame in (doc("A", 0), poly("A", 0), normal(0), doc("A", 1), poly("A", 1), normal(1)):
        assert conn.send_json(frame) is True

    assert conn.send_json(normal(2)) is True
    frames = conn.queued_frames()
    doc_cursors = [f for f in frames if f["type"] == "doc-cursor"]
    poly_cursors = [f for f in frames if f["type"] == "poly-cursor"]

    assert [(c["user_id"], c["range"]["start"]) for c in doc_cursors] == [("A", 1)]
    assert [(c["user_id"], c["node_id"]) for c in poly_cursors] == [("A", "p1")]
    assert not conn.closing


# --------------------------------------------------------------------------- #
# main.create_app end-to-end smoke
# --------------------------------------------------------------------------- #


async def test_up_smoke_via_create_app(tmp_path):
    env = {
        "SEANCE_SECRET": Fernet.generate_key().decode(),
        "SEANCE_DB": str(tmp_path / "smoke.db"),
        "SEANCE_ALLOWED_ORIGINS": ORIGIN,
    }
    app = await create_app(env)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session, session.get(
            server.make_url("/up")
        ) as resp:
            assert resp.status == 200
            assert await resp.json() == {
                "status": "ok",
                "service": "seance",
                "version": __version__,
            }
    finally:
        await server.close()


def test_transport_liveness_clock_defaults_to_monotonic():
    """Heartbeat deadlines and lane buckets are durations: immune to wall-clock steps."""
    import inspect
    import time

    default = inspect.signature(make_websocket_handler).parameters["clock"].default
    assert default is time.monotonic
