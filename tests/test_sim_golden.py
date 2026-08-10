"""Golden test: the reference sim demo, ported onto a real seance server.

Ports ``noisedeck/util/stateSyncSimulation.js`` ``runDemo`` (``:1096-1179``) onto a
real :func:`app.main.create_app` server behind a 127.0.0.1 :class:`TestServer`,
driven by real ``aiohttp`` WebSocket clients. It asserts seance's *documented*
divergences (design §18) rather than sim parity:

* **§18.1** the server is the poly serialization authority — the ``poly-ack`` is
  emitted BY the server (``connection_id``/``user_id`` == ``"server"``), where the
  sim routed the proposal to the leader client for the ack (``:744-772``).
* **§18.2** ``owner-changed`` (naming the earliest remaining participant) replaces
  the sim's ``new-authoritative-user``.
* **§18.3** structured ``error`` frames where the sim silently dropped bad traffic.
* **§18.4** chat echoes back to the sender.
* **§18.9** sessions are created by an explicit ``POST /v1/sessions`` (with a
  server-minted id), where the sim implicitly created ``ABC123`` on first join.

Beat comments cite the sim line ranges they mirror. Every socket wait is bounded.
"""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet

from app.main import create_app

ORIGIN = "http://testorigin"
CLOSE_TYPES = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)


# --------------------------------------------------------------------------- #
# Bounded socket helpers (all waits <= 5 s)
# --------------------------------------------------------------------------- #


async def _recv(ws, mtype, *, within=5.0):
    """Receive frames until one of type ``mtype`` arrives; fail on close/timeout."""
    async with asyncio.timeout(within):
        while True:
            msg = await ws.receive()
            if msg.type is WSMsgType.TEXT:
                body = json.loads(msg.data)
                if body.get("type") == mtype:
                    return body
            elif msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
                raise AssertionError(f"socket closed (code={ws.close_code}) awaiting {mtype!r}")


async def _expect_silence(ws, *, within=0.4):
    """Assert no frame arrives within the window (a sender receiving no echo)."""
    with pytest.raises(TimeoutError):
        await ws.receive(timeout=within)


async def _drain(ws, *, quiet=0.4):
    """Consume all currently-pending frames, returning once the socket goes quiet."""
    while True:
        try:
            msg = await ws.receive(timeout=quiet)
        except TimeoutError:
            return
        if msg.type in CLOSE_TYPES or msg.type is WSMsgType.ERROR:
            return


async def _send(ws, frame):
    await ws.send_str(json.dumps(frame))


async def _join(client, server, session_id, *, token=None):
    """Open a ws, send ``hello``, and read ``welcome`` + ``session-snapshot``."""
    url = server.make_url(f"/v1/sessions/{session_id}/ws")
    ws = await client.ws_connect(url, headers={"Origin": ORIGIN})
    hello = {"type": "hello", "protocol": 1}
    if token is not None:
        hello["anon_token"] = token
    await _send(ws, hello)
    welcome = await _recv(ws, "welcome")
    snapshot = await _recv(ws, "session-snapshot")
    return ws, welcome, snapshot


# --------------------------------------------------------------------------- #
# A real server + a pool of real client sessions, torn down cleanly
# --------------------------------------------------------------------------- #


@pytest.fixture
async def demo(tmp_path):
    env = {
        "SEANCE_SECRET": Fernet.generate_key().decode(),
        "SEANCE_DB": str(tmp_path / "sim-golden.db"),
        "SEANCE_ALLOWED_ORIGINS": ORIGIN,
        # The demo fires state-set then poly-snapshot back-to-back (two snapshot-lane
        # frames); the default burst is exactly 2, so widen the lane to keep this
        # golden path clear of the rate-limit edge. Divergences under test are
        # unrelated to rate limiting.
        "SEANCE_LIMIT_SNAPSHOT_RATE": "50",
        "SEANCE_LIMIT_SNAPSHOT_BURST": "50",
    }
    app = await create_app(env)
    server = TestServer(app)
    await server.start_server()
    clients: list[aiohttp.ClientSession] = []

    async def new_client():
        client = aiohttp.ClientSession()
        clients.append(client)
        return client

    try:
        yield SimpleNamespace(server=server, new_client=new_client)
    finally:
        for client in clients:
            await client.close()
        await server.close()


# --------------------------------------------------------------------------- #
# The golden scenario
# --------------------------------------------------------------------------- #


async def test_sim_golden_rundemo(demo):
    server = demo.server

    # sim :1096-1101 — leader connects to a session. §18.9: the sim implicitly
    # creates 'ABC123' on first join; seance requires an explicit POST and the id
    # is server-minted (client-chosen ids are refused).
    http = await demo.new_client()
    async with http.post(server.make_url("/v1/sessions"), headers={"Origin": ORIGIN}) as resp:
        assert resp.status == 201
        created = await resp.json()
    session_id = created["session_id"]
    assert len(session_id) == 6
    leader_token = created["anon_token"]

    leader_client = await demo.new_client()
    ws_leader, w_leader, _ = await _join(leader_client, server, session_id, token=leader_token)
    assert w_leader["you"]["is_owner"] is True  # the creator is the owner
    leader_uid = w_leader["you"]["user_id"]

    # sim :1104-1108 — leader seeds the initial state (owner-only state-set).
    await _send(
        ws_leader,
        {"type": "state-set", "state": [
            {"id": "moduleSelect", "value": "default"},
            {"id": "param", "value": 0},
        ]},
    )
    # state-set is broadcast to OTHERS only, so the leader gets no echo to await.
    # A follow-up ping/pong is the ordering guarantee: the server processes one
    # connection's frames in order, so the pong proves the state-set was
    # dispatched before the peer connects (no reliance on loop scheduling).
    await _send(ws_leader, {"type": "ping"})
    await _recv(ws_leader, "pong")

    # sim :1110-1113 — peer joins; its snapshot carries both seeded values.
    peer_client = await demo.new_client()
    ws_peer, w_peer, snap_peer = await _join(peer_client, server, session_id)
    peer_uid = w_peer["you"]["user_id"]
    peer_name = w_peer["you"]["username"]
    assert {e["id"]: e["value"] for e in snap_peer["state"]} == {
        "moduleSelect": "default",
        "param": 0,
    }

    # sim :1115 — peer updates moduleSelect -> leader sees it; the sender gets no echo.
    await _send(ws_peer, {"type": "state-update", "id": "moduleSelect", "value": "alt"})
    got = await _recv(ws_leader, "state-update")
    assert got["id"] == "moduleSelect" and got["value"] == "alt"
    assert got["user_id"] == peer_uid
    await _expect_silence(ws_peer)

    # sim :1117 — leader updates param -> peer receives it.
    await _send(ws_leader, {"type": "state-update", "id": "param", "value": 42})
    got = await _recv(ws_peer, "state-update")
    assert got["id"] == "param" and got["value"] == 42
    assert got["user_id"] == leader_uid

    # sim :1120 — peer chat -> BOTH receive it (§18.4: echo confirms delivery).
    await _send(ws_peer, {"type": "chat-message", "message": "Hello from the peer!"})
    peer_chat = await _recv(ws_peer, "chat-message")
    leader_chat = await _recv(ws_leader, "chat-message")
    assert peer_chat["message"] == "Hello from the peer!"
    assert leader_chat["message"] == "Hello from the peer!"
    assert peer_chat["user_id"] == peer_uid

    # sim :1122 — peer app-ping -> pong.
    await _send(ws_peer, {"type": "ping"})
    assert (await _recv(ws_peer, "pong"))["type"] == "pong"

    # sim :1125-1133 — leader poly-snapshot -> peer receives it stamped with the
    # SERVER-assigned rev (the client's own rev/version fields are ignored).
    await _send(
        ws_leader,
        {
            "type": "poly-snapshot",
            "programText": "chain { noise() }",
            "nodes": [
                {"id": "root", "kind": "Chain", "text": "noise()"},
                {"id": "root.0", "kind": "Call", "text": "noise()"},
            ],
            "frame": {"beat": 0},
        },
    )
    poly_snap = await _recv(ws_peer, "poly-snapshot")
    assert isinstance(poly_snap["rev"], int)
    base_rev = poly_snap["rev"]
    assert base_rev == 1

    # sim :1136-1153 — peer upserts root.1. §18.1: the SERVER adjudicates and acks
    # (the sim routed the proposal to the leader for a client-emitted ack).
    await _send(
        ws_peer,
        {
            "type": "poly-token-upsert",
            "base_rev": base_rev,
            "parentId": "root",
            "id": "root.1",
            "kind": "Call",
            "text": "color()",
            "author_seq": 1,
        },
    )
    ack = await _recv(ws_peer, "poly-ack")
    assert ack["connection_id"] == "server"  # §18.1: ack is server-originated
    assert ack["user_id"] == "server"
    assert ack["rev"] == 2
    assert ack["applied"] == [{"id": "root.1", "version": 2}]
    # The leader receives the decorated broadcast (rev + version), stamped as the peer.
    up = await _recv(ws_leader, "poly-token-upsert")
    assert up["id"] == "root.1"
    assert up["rev"] == 2 and up["version"] == 2
    assert up["user_id"] == peer_uid

    # sim :1156-1160 — peer cursor -> leader receives it; the sender does not.
    await _send(ws_peer, {"type": "poly-cursor", "mode": "text", "range": {"start": 0, "end": 5}})
    cur = await _recv(ws_leader, "poly-cursor")
    assert cur["mode"] == "text" and cur["range"] == {"start": 0, "end": 5}
    assert cur["user_id"] == peer_uid
    await _expect_silence(ws_peer)

    # sim :1163-1166 — latecomer joins; snapshot carries all 3 nodes at the right
    # versions and the chat history holds the peer's message.
    late_client = await demo.new_client()
    ws_late, _, snap_late = await _join(late_client, server, session_id)
    poly = snap_late["poly"]
    assert poly["rev"] == 2
    assert {n["id"]: n["version"] for n in poly["nodes"]} == {
        "root": 1,
        "root.0": 1,
        "root.1": 2,
    }
    assert "Hello from the peer!" in [c["message"] for c in snap_late["chat"]]

    # sim :1168-1169 — leader disconnects. §18.2: remaining clients receive
    # owner-changed naming the earliest remaining participant (the peer). The
    # leader drops its connection (session close = TCP teardown), which the server
    # observes as the read loop ending and processes as a roster departure.
    await leader_client.close()
    oc_peer = await _recv(ws_peer, "owner-changed")
    assert oc_peer["user_id"] == peer_uid
    assert oc_peer["username"] == peer_name
    oc_late = await _recv(ws_late, "owner-changed")
    assert oc_late["user_id"] == peer_uid

    # sim :1171-1174 — a fresh anon returns; its snapshot has the poly intact.
    ret_client = await demo.new_client()
    _ws_ret, _, snap_ret = await _join(ret_client, server, session_id)
    ret_poly = snap_ret["poly"]
    assert ret_poly["rev"] == 2
    assert {n["id"] for n in ret_poly["nodes"]} == {"root", "root.0", "root.1"}

    # §18.3 — a bad frame earns a structured error where the sim silently dropped.
    await _drain(ws_peer)
    await _send(ws_peer, {"type": "bogus-type"})
    err = await _recv(ws_peer, "error")
    assert err["code"] == "bad_frame"
