"""End-to-end integration scenarios (spec §16): real server, real sockets.

Each test stands up a fully-wired app through :func:`app.main.create_app` (env
dict → :func:`tests.helpers.world`), connects real ``aiohttp`` WebSocket
:class:`~tests.helpers.Peer` clients, and drives a multi-client scenario from the
spec's §16 integration list — convergence, late-joiner catch-up, owner handoff
and creator reclaim, freeze→restart→thaw with counters and bans intact, the
moderation matrix, chat delete/recall visibility, the ticket and member-cookie
identity flows, and cross-session isolation.

These are SCENARIOS, not route re-tests: single-connection protocol/route
behaviour lives in ``test_transport`` and ``test_httpapi``. Every wait here is
bounded (``asyncio.timeout`` ≤ 5 s); nothing sleeps to advance logic.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from cryptography.fernet import Fernet

from tests.helpers import (
    PeerClosed,
    create_session,
    gs_cookie,
    make_members_db,
    mint_ticket,
    world,  # noqa: F401  (re-exported pytest fixture)
)


def _doc(snapshot: dict) -> dict:
    """The comparable payload of a session-snapshot: state, data, and polydoc."""
    return {"state": snapshot["state"], "data": snapshot["data"], "poly": snapshot["poly"]}


def _state_value(snapshot: dict, state_id: str):
    """The LWW value stored under ``state_id`` in a session-snapshot."""
    for entry in snapshot["state"]:
        if entry["id"] == state_id:
            return entry["value"]
    raise AssertionError(f"state id {state_id!r} not present")


# --------------------------------------------------------------------------- #
# (a) three peers converge on identical state + polydoc
# --------------------------------------------------------------------------- #


async def test_a_three_peers_converge_identically(world):  # noqa: F811
    ctx = await world.app()
    sid, owner_token = await create_session(ctx)

    a = world.peer("A")
    await a.connect(ctx.server, sid, anon_token=owner_token)
    assert a.is_owner
    b = world.peer("B")
    await b.connect(ctx.server, sid)
    c = world.peer("C")
    await c.connect(ctx.server, sid)

    # Interleaved LWW updates on distinct keys (final map is order-independent).
    await a.send({"type": "state-update", "id": "ka", "value": 1})
    await b.send({"type": "state-update", "id": "kb", "value": 2})
    await c.send({"type": "state-update", "id": "kc", "value": 3})
    await a.barrier()
    await b.barrier()
    await c.barrier()

    # Owner seeds the polydoc; a round-trip syncs the owner's rev before it upserts.
    await a.send(
        {
            "type": "poly-snapshot",
            "programText": "root()",
            "nodes": [{"id": "root", "kind": "group", "text": "", "parentId": None}],
        }
    )
    await a.session_state()

    # Distinct child nodes from all three — new nodes never conflict, so a stale
    # base_rev on B/C cannot reject; the parent "root" already exists server-side.
    await a.send(_upsert("root.a", "root", "AA", a.rev))
    await b.send(_upsert("root.b", "root", "BB", b.rev))
    await c.send(_upsert("root.c", "root", "CC", c.rev))
    await a.expect("poly-ack")
    await b.expect("poly-ack")
    await c.expect("poly-ack")

    sa = await a.session_state()
    sb = await b.session_state()
    sc = await c.session_state()

    # A fourth, fresh joiner's welcome-time snapshot must match the other three.
    d = world.peer("D")
    await d.connect(ctx.server, sid)
    sd = d.snapshot

    # Store fidelity (the frozen payload equalling the live doc) is proven by
    # scenario (d)'s freeze -> restart -> thaw round-trip; here we assert only
    # live cross-peer convergence.
    assert _doc(sa) == _doc(sb) == _doc(sc) == _doc(sd)
    assert sa["poly"]["rev"] == 4  # snapshot (1) + three upserts
    assert {n["id"] for n in sa["poly"]["nodes"]} == {"root", "root.a", "root.b", "root.c"}
    assert {e["id"] for e in sa["state"]} == {"ka", "kb", "kc"}


def _upsert(node_id: str, parent: str, text: str, base_rev: int) -> dict:
    return {
        "type": "poly-token-upsert",
        "base_rev": base_rev,
        "id": node_id,
        "kind": "leaf",
        "text": text,
        "parentId": parent,
        "author_seq": 1,
    }


# --------------------------------------------------------------------------- #
# (b) late joiner catches up mid-storm
# --------------------------------------------------------------------------- #


async def test_b_late_joiner_converges_mid_storm(world):  # noqa: F811
    ctx = await world.app()
    sid, owner_token = await create_session(ctx)

    a = world.peer("A")
    await a.connect(ctx.server, sid, anon_token=owner_token)

    burst = 40  # well under the fast-lane burst ceiling (120)

    async def storm() -> None:
        for i in range(burst):
            await a.send({"type": "state-update", "id": "k", "value": i})

    storm_task = asyncio.create_task(storm())
    b = world.peer("B")
    await b.connect(ctx.server, sid)  # joins while the storm is in flight
    await storm_task

    await a.barrier()
    await b.barrier()
    sa = await a.session_state()
    sb = await b.session_state()

    assert _state_value(sa, "k") == burst - 1
    assert _state_value(sb, "k") == burst - 1
    assert _doc(sa) == _doc(sb)


# --------------------------------------------------------------------------- #
# (c) owner disconnect handoff + creator reclaim
# --------------------------------------------------------------------------- #


async def test_c_owner_handoff_and_creator_reclaim(world):  # noqa: F811
    ctx = await world.app()
    sid, owner_token = await create_session(ctx)

    a = world.peer("A")
    await a.connect(ctx.server, sid, anon_token=owner_token)
    a_uid = a.user_id
    assert a.is_owner
    b = world.peer("B")
    await b.connect(ctx.server, sid)  # earliest remaining joiner
    b_uid = b.user_id
    c = world.peer("C")
    await c.connect(ctx.server, sid)

    # Owner leaves -> earliest joiner (B) becomes the acting owner.
    await a.close()
    assert (await b.expect("owner-changed"))["user_id"] == b_uid
    assert (await c.expect("owner-changed"))["user_id"] == b_uid

    # Creator rejoins with the same identity -> ownership reverts to the creator.
    a2 = world.peer("A2")
    await a2.connect(ctx.server, sid, anon_token=owner_token)
    assert a2.user_id == a_uid
    assert a2.is_owner
    assert (await b.expect("owner-changed"))["user_id"] == a_uid
    assert (await c.expect("owner-changed"))["user_id"] == a_uid


# --------------------------------------------------------------------------- #
# (d) freeze -> restart -> thaw: counters + doc + ban intact
# --------------------------------------------------------------------------- #


async def test_d_freeze_restart_thaw_intact(world):  # noqa: F811
    shared_db = str(world.tmp_path / "shared-d.db")
    ctx1 = await world.app(db=shared_db)
    sid, owner_token = await create_session(ctx1)

    a = world.peer("A")
    await a.connect(ctx1.server, sid, anon_token=owner_token)
    assert a.is_owner
    c = world.peer("C")
    await c.connect(ctx1.server, sid)
    c_token = c.welcome["anon_token"]
    c_uid = c.user_id

    await a.send({"type": "state-update", "id": "bg", "value": {"hue": 9}})
    await a.send(
        {
            "type": "poly-snapshot",
            "programText": "noise()",
            "nodes": [{"id": "root", "kind": "group", "text": "r", "parentId": None}],
        }
    )
    await a.send({"type": "chat-message", "message": "hello"})
    await a.barrier()

    # Owner bans the guest, then a final snapshot fixes the pre-stop counters.
    await a.send({"type": "mod-ban", "target_user": c_uid})
    await a.expect("moderation", where=lambda f: f.get("action") == "ban")
    pre = await a.session_state()
    pre_rev = pre["poly"]["rev"]
    pre_seq = pre["seq"]

    # Everyone leaves; stopping the app runs on_cleanup -> hub.stop freezes to DB.
    await a.close()
    await c.close()
    await ctx1.close()

    # A brand-new process over the SAME database thaws the frozen session.
    ctx2 = await world.app(db=shared_db)
    a2 = world.peer("A2")
    await a2.connect(ctx2.server, sid, anon_token=owner_token)
    assert a2.is_owner
    assert a2.welcome["rev"] == pre_rev  # rev continues
    assert a2.welcome["seq"] > pre_seq  # seq continues monotonically
    assert _doc(a2.snapshot) == _doc(pre)  # doc restored byte-for-byte

    # The ban survived the freeze/thaw: the guest's fresh join is refused 4403.
    cx = world.peer("Cx")
    with pytest.raises(PeerClosed) as excinfo:
        await cx.connect(ctx2.server, sid, anon_token=c_token)
    assert excinfo.value.close_code == 4403


# --------------------------------------------------------------------------- #
# (e) moderation matrix over sockets: readonly / guests-off / lock
# --------------------------------------------------------------------------- #


async def test_e_moderation_matrix_over_sockets(world):  # noqa: F811
    gs_key = Fernet.generate_key()
    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    members_db = str(world.tmp_path / "members-e.db")
    make_members_db(members_db, [(m1, "mem1", None), (m2, "mem2", None)])
    ctx = await world.app(
        SEANCE_GS_SERIALIZER_KEY=gs_key.decode(),
        SEANCE_DIRECTORY_DSN=f"sqlite:///{members_db}",
    )
    sid, owner_token = await create_session(ctx)

    a = world.peer("A")
    await a.connect(ctx.server, sid, anon_token=owner_token)
    assert a.is_owner
    b = world.peer("B")
    await b.connect(ctx.server, sid)
    b_uid = b.user_id

    # --- readonly: B's writes are dropped with an error and never reach A ---
    await a.send({"type": "mod-readonly", "target_user": b_uid, "readonly": True})
    await a.expect("moderation", where=lambda f: f.get("action") == "readonly")
    await b.send({"type": "state-update", "id": "x", "value": 1})
    assert (await b.expect("error"))["code"] == "readonly"
    await a.drain_quiet()
    assert not a.frames("state-update")  # the blocked write never fanned out

    await a.send({"type": "mod-readonly", "target_user": b_uid, "readonly": False})
    await a.expect("moderation", where=lambda f: f.get("action") == "readonly")
    await b.send({"type": "state-update", "id": "x", "value": 2})
    got = await a.expect("state-update", where=lambda f: f.get("value") == 2)
    assert got["user_id"] == b_uid  # writes flow again once cleared

    # --- guests off: fresh anon refused, existing anon stays, member still joins ---
    await a.send({"type": "mod-guests", "allowed": False})
    await a.expect("moderation", where=lambda f: f.get("action") == "guests")
    await b.barrier()  # existing guest B is untouched

    # An existing guest keeps full write access after guests-off (the dial only
    # blocks new guest JOINS): B's update still fans out to A.
    await b.send({"type": "state-update", "id": "guarded", "value": 7})
    got = await a.expect("state-update", where=lambda f: f.get("id") == "guarded")
    assert got["value"] == 7
    assert got["user_id"] == b_uid

    d = world.peer("D")
    with pytest.raises(PeerClosed) as ed:
        await d.connect(ctx.server, sid)
    assert ed.value.close_code == 4403

    mem = world.peer("mem1")
    await mem.connect(ctx.server, sid, cookies={"SESSION": gs_cookie(gs_key, m1)})
    assert mem.welcome["you"]["kind"] == "member"
    assert mem.username == "mem1"

    # --- lock: every fresh join is refused 4423, members included ---
    await a.send({"type": "mod-lock", "locked": True})
    await a.expect("moderation", where=lambda f: f.get("action") == "lock")

    e2 = world.peer("E")
    with pytest.raises(PeerClosed) as ee:
        await e2.connect(ctx.server, sid)
    assert ee.value.close_code == 4423

    m2p = world.peer("mem2")
    with pytest.raises(PeerClosed) as em:
        await m2p.connect(ctx.server, sid, cookies={"SESSION": gs_cookie(gs_key, m2)})
    assert em.value.close_code == 4423


# --------------------------------------------------------------------------- #
# (f) chat delete / recall visibility
# --------------------------------------------------------------------------- #


async def test_f_chat_delete_and_recall_visibility(world):  # noqa: F811
    ctx = await world.app()
    sid, owner_token = await create_session(ctx)

    a = world.peer("A")
    await a.connect(ctx.server, sid, anon_token=owner_token)
    b = world.peer("B")
    await b.connect(ctx.server, sid)

    m_a1 = await _say(a, b, "A1")
    m_b1 = await _say(b, a, "B1")
    m_b2 = await _say(b, a, "B2")

    # Owner deletes one of B's messages -> BOTH peers see chat-deleted.
    await a.send({"type": "chat-delete", "message_id": m_b1})
    deleted_a = await a.expect("chat-deleted", where=lambda f: f.get("target_message_id") == m_b1)
    await b.expect("chat-deleted", where=lambda f: f.get("target_message_id") == m_b1)
    assert deleted_a["by"] == a.username

    # Author recalls within window -> author gets the text back, peer gets a delete.
    await b.send({"type": "chat-recall", "message_id": m_b2})
    recalled = await b.expect("chat-recalled", where=lambda f: f.get("target_message_id") == m_b2)
    assert recalled["original_message"] == "B2"
    await a.expect("chat-deleted", where=lambda f: f.get("target_message_id") == m_b2)

    # A later joiner sees only the surviving history — no deleted/recalled entries.
    await a.barrier()
    c = world.peer("C")
    await c.connect(ctx.server, sid)
    chat_ids = {f.get("message_id") for f in c.snapshot["chat"]}
    assert m_a1 in chat_ids
    assert m_b1 not in chat_ids
    assert m_b2 not in chat_ids
    assert {f.get("message") for f in c.snapshot["chat"]} == {"A1"}


async def _say(sender, other, text: str) -> str:
    """``sender`` posts ``text``; both it and ``other`` observe the echo. Returns id."""
    await sender.send({"type": "chat-message", "message": text})
    frame = await sender.expect("chat-message", where=lambda f: f["message"] == text)
    await other.expect("chat-message", where=lambda f: f["message"] == text)
    return frame["message_id"]


# --------------------------------------------------------------------------- #
# (g) ticket flow end-to-end -> member identity in welcome
# --------------------------------------------------------------------------- #


async def test_g_ticket_flow_yields_member_identity(world):  # noqa: F811
    ctx = await world.app(SEANCE_GS_TRUSTED_NETS="127.0.0.0/8")
    sid, _ = await create_session(ctx)

    ticket = await mint_ticket(ctx, "member-77", "Grace")
    m = world.peer("member")
    await m.connect(ctx.server, sid, ticket=ticket)

    assert m.welcome["you"]["kind"] == "member"
    assert m.user_id == "member-77"
    assert m.username == "Grace"


# --------------------------------------------------------------------------- #
# (h) member cookie flow -> member identity in welcome
# --------------------------------------------------------------------------- #


async def test_h_member_cookie_flow(world):  # noqa: F811
    gs_key = Fernet.generate_key()
    uid = str(uuid.uuid4())
    members_db = str(world.tmp_path / "members-h.db")
    make_members_db(members_db, [(uid, "alice", None)])
    ctx = await world.app(
        SEANCE_GS_SERIALIZER_KEY=gs_key.decode(),
        SEANCE_DIRECTORY_DSN=f"sqlite:///{members_db}",
    )
    sid, _ = await create_session(ctx)

    alice = world.peer("alice")
    await alice.connect(ctx.server, sid, cookies={"SESSION": gs_cookie(gs_key, uid)})

    assert alice.welcome["you"]["user_id"] == uid
    assert alice.username == "alice"
    assert alice.welcome["you"]["kind"] == "member"


# --------------------------------------------------------------------------- #
# (i) two sessions on one app: zero cross-talk, no state bleed
# --------------------------------------------------------------------------- #


async def test_i_session_isolation_no_crosstalk(world):  # noqa: F811
    ctx = await world.app()
    sid1, tok1 = await create_session(ctx)
    sid2, tok2 = await create_session(ctx)
    assert sid1 != sid2

    a1 = world.peer("A1")
    await a1.connect(ctx.server, sid1, anon_token=tok1)
    b1 = world.peer("B1")
    await b1.connect(ctx.server, sid1)
    a2 = world.peer("A2")
    await a2.connect(ctx.server, sid2, anon_token=tok2)
    b2 = world.peer("B2")
    await b2.connect(ctx.server, sid2)

    await a1.send({"type": "state-update", "id": "s1key", "value": "one"})
    await a2.send({"type": "state-update", "id": "s2key", "value": "two"})
    await a1.barrier()
    await a2.barrier()
    for peer in (a1, b1, a2, b2):
        await peer.drain_quiet()

    # Not one frame may carry the other session's id to a peer.
    for peer, sid in ((a1, sid1), (b1, sid1), (a2, sid2), (b2, sid2)):
        assert peer.inbox, "expected the peer to have received frames"
        for frame in peer.inbox:
            assert frame.get("session") == sid, (
                f"leak: {frame.get('type')} carried session {frame.get('session')} into {sid}"
            )

    # State does not bleed across sessions.
    s1 = await a1.session_state()
    s2 = await a2.session_state()
    assert {e["id"] for e in s1["state"]} == {"s1key"}
    assert {e["id"] for e in s2["state"]} == {"s2key"}
    assert _doc(s1) != _doc(s2)
