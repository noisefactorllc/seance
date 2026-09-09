"""Tests for the Session domain object (roster, owner rules, dispatch, fan-out).

The Session is a pure synchronous state machine: connections are duck-typed
sinks (:class:`tests.conftest.FakeConn`), time is injected, and there is no I/O.
Every outbound frame is server-stamped; these tests assert the envelope, the
fan-out recipient sets, and the seq/owner state machine per the spec §5-§8.
"""

from app import protocol
from app.config import Limits
from app.identity import Identity, Kind
from app.session import JoinRefused, Session, Settings
from tests.conftest import FakeConn

# --------------------------------------------------------------------------- #
# Identity / session helpers
# --------------------------------------------------------------------------- #


def member(uid, name=None):
    return Identity(user_id=uid, username=name or uid, kind=Kind.MEMBER)


def anon(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.ANON)


def ephemeral(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.GS_EPHEMERAL)


def frames(conn, ftype):
    return [f for f in conn.sent if f["type"] == ftype]


def last_chat_id(conn):
    return frames(conn, "chat-message")[-1]["message_id"]


# --------------------------------------------------------------------------- #
# Join: happy path + refusals
# --------------------------------------------------------------------------- #


def test_join_happy_path_welcome_then_snapshot(clock):
    s = Session("sess01", "u-creator", Limits(), clock)
    c = FakeConn(member("u-creator", "alice"))
    s.join(c)

    welcome = c.sent[0]
    assert welcome["type"] == "welcome"
    assert welcome["protocol"] == 1
    assert welcome["session"] == "sess01"
    assert welcome["you"] == {
        "user_id": "u-creator",
        "username": "alice",
        "kind": "member",
        "readonly": False,
        "is_owner": True,
    }
    assert welcome["owner"] == {"user_id": "u-creator", "username": "alice"}
    assert welcome["roster"] == [
        {"user_id": "u-creator", "username": "alice", "kind": "member", "connections": 1}
    ]
    assert welcome["settings"] == {
        "locked": False,
        "guests_allowed": True,
        "guests_readonly": False,
    }
    assert welcome["rev"] == 0
    # envelope present
    for key in (
        "seq", "session", "user_id", "username", "connection_id", "message_id", "timestamp",
    ):
        assert key in welcome

    snap = c.sent[1]
    assert snap["type"] == "session-snapshot"
    assert snap["state"] == []
    assert snap["data"] == {}
    assert snap["poly"] == {"rev": 0, "programText": "", "frame": None, "nodes": []}
    assert snap["chat"] == []

    # seq strictly increasing and unique across the whole join burst
    seqs = [f["seq"] for f in c.sent]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_join_first_owner_gets_owner_changed(clock):
    s = Session("s", "u-creator", Limits(), clock)
    c = FakeConn(member("u-creator", "alice"))
    s.join(c)
    oc = frames(c, "owner-changed")
    assert len(oc) == 1
    assert oc[0]["user_id"] == "u-creator"
    assert oc[0]["username"] == "alice"
    assert oc[0]["connection_id"] == "server"


def test_join_banned_refused_4403(clock):
    s = Session("s", "creator", Limits(), clock, bans={"u-bad"})
    try:
        s.join(FakeConn(anon("u-bad")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("expected JoinRefused")


def test_join_locked_refused_4423(clock):
    s = Session("s", "creator", Limits(), clock, settings=Settings(locked=True))
    try:
        s.join(FakeConn(member("m1")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LOCKED
    else:
        raise AssertionError("expected JoinRefused")


def test_join_guests_off_blocks_anon_not_member(clock):
    s = Session("s", "creator", Limits(), clock, settings=Settings(guests_allowed=False))
    try:
        s.join(FakeConn(anon("a1")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("expected JoinRefused")
    try:
        s.join(FakeConn(ephemeral("e1")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("expected JoinRefused for ephemeral")
    # members are still admitted
    s.join(FakeConn(member("m1")))


def test_join_roster_full_refused_4429_but_extra_tab_admitted(clock):
    s = Session("s", "creator", Limits(max_clients=2), clock)
    s.join(FakeConn(member("m1")))
    s.join(FakeConn(member("m2")))
    try:
        s.join(FakeConn(member("m3")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LIMIT
    else:
        raise AssertionError("expected JoinRefused")
    # a second connection of an already-present user is always admitted
    s.join(FakeConn(member("m1")))
    assert sum(1 for c in s.conns.values() if c.identity.user_id == "m1") == 2


def test_join_per_user_conn_cap_default_allows_eight_refuses_ninth(clock):
    # With the default max_conns_per_user (8), one user may open 8 tabs; the 9th
    # is refused 4429 ("too many connections"), leaving the roster untouched.
    s = Session("s", "creator", Limits(), clock)
    for _ in range(8):
        s.join(FakeConn(member("m1")))
    assert sum(1 for c in s.conns.values() if c.identity.user_id == "m1") == 8
    try:
        s.join(FakeConn(member("m1")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LIMIT
        assert exc.reason == "too many connections"
    else:
        raise AssertionError("expected JoinRefused for the 9th tab")
    # the refused join added nothing — still exactly 8 tabs
    assert sum(1 for c in s.conns.values() if c.identity.user_id == "m1") == 8


def test_join_per_user_conn_cap_override_refuses_third_tab_others_unaffected(clock):
    # An override caps each user at 2 tabs: the 3rd tab of a present user is
    # refused 4429, but a different user is entirely unaffected by m1's cap.
    s = Session("s", "creator", Limits(max_conns_per_user=2), clock)
    s.join(FakeConn(member("m1")))  # first connection
    s.join(FakeConn(member("m1")))  # second tab, at the cap
    try:
        s.join(FakeConn(member("m1")))  # third tab, refused
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LIMIT
        assert exc.reason == "too many connections"
    else:
        raise AssertionError("expected JoinRefused for the 3rd tab")
    # a distinct user's first connection and second tab are unaffected
    s.join(FakeConn(member("m2")))
    s.join(FakeConn(member("m2")))
    assert sum(1 for c in s.conns.values() if c.identity.user_id == "m2") == 2
    assert sum(1 for c in s.conns.values() if c.identity.user_id == "m1") == 2


def test_handle_unknown_connection_is_silent(clock):
    s = Session("s", "creator", Limits(), clock)
    s.handle("ghost-conn", {"type": "ping"})  # no raise, nothing to observe


def test_handle_protocol_error_returns_error_to_sender(clock):
    s = Session("s", "creator", Limits(), clock)
    c = FakeConn(member("m1"))
    s.join(c)
    c.sent.clear()
    s.handle(c.connection_id, {"type": "state-update", "id": "x"})  # missing value
    err = frames(c, "error")
    assert len(err) == 1
    assert err[0]["code"] == "bad_frame"


# --------------------------------------------------------------------------- #
# Multi-tab roster semantics
# --------------------------------------------------------------------------- #


def test_multi_tab_roster_and_single_user_joined(clock):
    s = Session("s", "creator", Limits(), clock)
    peer = FakeConn(member("peer"))
    s.join(peer)
    peer.sent.clear()

    c1 = FakeConn(member("u1", "bob"))
    s.join(c1)
    joined = frames(peer, "user-joined")
    assert len(joined) == 1
    assert joined[0]["user_id"] == "u1"
    assert joined[0]["username"] == "bob"
    assert joined[0]["kind"] == "member"
    # a system-message accompanies the join for peers
    sysmsg = frames(peer, "system-message")
    assert sysmsg[0]["message"] == "bob has joined"

    peer.sent.clear()
    c2 = FakeConn(member("u1", "bob"))
    s.join(c2)
    # second tab of the SAME user: peers get NO user-joined
    assert frames(peer, "user-joined") == []
    # the new tab's welcome shows the user with two connections
    entry = [e for e in c2.sent[0]["roster"] if e["user_id"] == "u1"][0]
    assert entry["connections"] == 2


# --------------------------------------------------------------------------- #
# Fan-out: state-update excludes sender, envelope is server-derived
# --------------------------------------------------------------------------- #


def test_state_update_fanout_excludes_sender(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a", "alice"))
    b = FakeConn(member("b", "bob"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()

    # a client-supplied envelope value must be ignored (server-derived only)
    s.handle(a.connection_id, {"type": "state-update", "id": "x", "value": 42, "username": "SPOOF"})

    got = frames(b, "state-update")
    assert len(got) == 1
    assert got[0]["id"] == "x"
    assert got[0]["value"] == 42
    assert got[0]["user_id"] == "a"
    assert got[0]["username"] == "alice"  # server-derived, not the spoofed value
    assert got[0]["connection_id"] == a.connection_id
    # sender does NOT receive its own state-update echo
    assert frames(a, "state-update") == []
    # engine stored it under the same seq the frame carries
    assert s.state.snapshot() == [{"id": "x", "value": 42, "seq": got[0]["seq"], "by": "a"}]


def test_state_update_too_large_errors_sender_only(clock):
    s = Session("s", "creator", Limits(max_value_bytes=10), clock)
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()
    s.handle(a.connection_id, {"type": "state-update", "id": "x", "value": "y" * 50})
    # too_large surfaces via engine cap; note protocol also caps value at max_value_bytes,
    # so the surfaced error is a size error to the sender only, nothing to peers.
    err = frames(a, "error")
    assert len(err) == 1
    assert err[0]["code"] == "too_large"
    assert frames(b, "state-update") == []
    assert s.state.snapshot() == []  # nothing mutated


def test_state_update_rejects_aggregate_session_budget_without_mutating(clock):
    s = Session(
        "s",
        "creator",
        Limits(max_session_bytes=180, max_value_bytes=1_000),
        clock,
    )
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()

    s.handle(a.connection_id, {"type": "state-update", "id": "x", "value": "y" * 100})

    assert frames(a, "error")[-1]["code"] == "too_large"
    assert frames(b, "state-update") == []
    assert s.state.snapshot() == []


def test_state_content_byte_cache_stays_exact_across_incremental_updates(clock):
    s = Session("s", "owner", Limits(), clock)
    owner = FakeConn(member("owner"))
    s.join(owner)

    def assert_exact() -> None:
        frozen = s.freeze_snapshot()
        content = {
            key: frozen[key] for key in ("state", "data", "poly", "docs", "chat")
        }
        assert s.content_bytes == protocol.json_size(content)

    assert_exact()
    s.handle(owner.connection_id, {"type": "state-update", "id": "alpha", "value": 1})
    assert_exact()
    s.handle(
        owner.connection_id,
        {"type": "state-update", "id": "alpha", "value": {"unicode": "雪"}},
    )
    assert_exact()
    s.handle(owner.connection_id, {"type": "state-update", "id": "beta", "value": True})
    assert_exact()
    s.handle(
        owner.connection_id,
        {"type": "state-set", "state": [{"id": "replacement", "value": [1, 2, 3]}]},
    )
    assert_exact()


def test_data_update_fanout_excludes_sender(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a", "alice"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()
    s.handle(
        a.connection_id,
        {"type": "data-update", "id": "node1", "role": "meta", "key": "color", "value": "red"},
    )
    got = frames(b, "data-update")
    assert len(got) == 1
    assert got[0]["id"] == "node1"
    assert got[0]["value"] == "red"
    assert got[0]["user_id"] == "a"
    assert frames(a, "data-update") == []
    assert s.data.snapshot() == {"node1": {"meta": {"color": "red"}}}


def test_data_update_rejects_aggregate_session_budget_without_mutating(clock):
    s = Session(
        "s",
        "creator",
        Limits(max_session_bytes=180, max_value_bytes=1_000),
        clock,
    )
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()

    s.handle(
        a.connection_id,
        {"type": "data-update", "id": "node", "role": "meta", "key": "value", "value": "x" * 100},
    )

    assert frames(a, "error")[-1]["code"] == "too_large"
    assert frames(b, "data-update") == []
    assert s.data.snapshot() == {}


def test_clicked_button_relays_opaque_to_others(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()
    s.handle(a.connection_id, {"type": "clicked-button", "button": "go", "payload": {"n": 1}})
    got = frames(b, "clicked-button")
    assert len(got) == 1
    assert got[0]["button"] == "go"
    assert got[0]["payload"] == {"n": 1}
    assert frames(a, "clicked-button") == []


# --------------------------------------------------------------------------- #
# state-set: owner-only bulk replace, fan-out, and too_large refusal
# --------------------------------------------------------------------------- #


def test_state_set_owner_replaces_map_and_broadcasts(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner", "olive"))
    b = FakeConn(member("b", "bea"))
    s.join(o)
    s.join(b)
    o.sent.clear()
    b.sent.clear()

    s.handle(
        o.connection_id,
        {"type": "state-set", "state": [
            {"id": "a", "value": 1},
            {"id": "b", "value": 2},
        ]},
    )
    # engine map is replaced wholesale; the session-state snapshot is exactly the
    # new list (id-sorted), each entry stamped with the owner as author.
    o.sent.clear()
    s.handle(o.connection_id, {"type": "session-state"})
    snap = frames(o, "session-snapshot")[0]
    assert [(e["id"], e["value"]) for e in snap["state"]] == [("a", 1), ("b", 2)]
    assert all(e["by"] == "owner" for e in snap["state"])
    # the broadcast reached the peer (excluding the sender)
    got = frames(b, "state-set")
    assert len(got) == 1
    assert got[0]["user_id"] == "owner"
    assert got[0]["username"] == "olive"
    assert {(item["id"], item["value"]) for item in got[0]["state"]} == {("a", 1), ("b", 2)}
    assert frames(o, "state-set") == []  # sender excluded from the fan-out


def test_state_set_non_owner_forbidden(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    b.sent.clear()
    s.handle(b.connection_id, {"type": "state-set", "state": [{"id": "x", "value": 1}]})
    assert frames(b, "error")[0]["code"] == "forbidden"
    assert s.state.snapshot() == []  # nothing applied by the non-owner


def test_state_set_oversized_value_too_large_no_broadcast(clock):
    s = Session("s", "owner", Limits(max_value_bytes=16), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    # seed a valid map first so we can prove the failed replace leaves it untouched
    s.handle(o.connection_id, {"type": "state-set", "state": [{"id": "keep", "value": 1}]})
    o.sent.clear()
    b.sent.clear()
    # a value exceeding max_value_bytes -> too_large, no broadcast, map unchanged
    s.handle(o.connection_id, {"type": "state-set", "state": [{"id": "big", "value": "y" * 100}]})
    assert frames(o, "error")[0]["code"] == "too_large"
    assert frames(b, "state-set") == []
    assert [e["id"] for e in s.state.snapshot()] == ["keep"]


# --------------------------------------------------------------------------- #
# Chat: echo, delete, recall
# --------------------------------------------------------------------------- #


def test_chat_message_echo_includes_sender(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()
    s.handle(a.connection_id, {"type": "chat-message", "message": "hi"})
    ca = frames(a, "chat-message")
    cb = frames(b, "chat-message")
    assert len(ca) == 1 and len(cb) == 1
    assert ca[0]["message"] == "hi"
    # every recipient sees the SAME message_id and seq (one logical event)
    assert ca[0]["message_id"] == cb[0]["message_id"]
    assert ca[0]["seq"] == cb[0]["seq"]
    assert ca[0]["user_id"] == "a"
    # stored in the bounded history deque
    assert len(s.chat) == 1
    assert s.chat[0]["message_id"] == ca[0]["message_id"]


def test_chat_message_rejects_aggregate_session_budget_without_echo(clock):
    s = Session("s", "creator", Limits(max_session_bytes=180), clock)
    a = FakeConn(member("a"))
    b = FakeConn(member("b"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    b.sent.clear()

    s.handle(a.connection_id, {"type": "chat-message", "message": "hi"})

    assert frames(a, "error")[-1]["code"] == "too_large"
    assert frames(a, "chat-message") == []
    assert frames(b, "chat-message") == []
    assert list(s.chat) == []


def test_chat_delete_by_owner_and_author_and_stranger(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner", "owner"))
    m = FakeConn(member("m", "mel"))
    stranger = FakeConn(member("z", "zed"))
    s.join(o)
    s.join(m)
    s.join(stranger)

    # author m posts a message
    s.handle(m.connection_id, {"type": "chat-message", "message": "yo"})
    mid = last_chat_id(m)

    # stranger cannot delete it
    stranger.sent.clear()
    s.handle(stranger.connection_id, {"type": "chat-delete", "message_id": mid})
    err = frames(stranger, "error")
    assert err[0]["code"] == "forbidden"
    assert len(s.chat) == 1  # untouched

    # owner CAN delete it -> everyone (incl. actor) gets chat-deleted
    o.sent.clear()
    m.sent.clear()
    s.handle(o.connection_id, {"type": "chat-delete", "message_id": mid})
    do = frames(o, "chat-deleted")[0]
    dm = frames(m, "chat-deleted")[0]
    assert do["target_message_id"] == mid
    assert do["by"] == "owner"
    assert dm["target_message_id"] == mid
    assert len(s.chat) == 0

    # author can delete their own message
    s.handle(m.connection_id, {"type": "chat-message", "message": "again"})
    mid2 = last_chat_id(m)
    s.handle(m.connection_id, {"type": "chat-delete", "message_id": mid2})
    assert len(s.chat) == 0


def test_chat_delete_unknown_message_is_forbidden(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    s.join(o)
    o.sent.clear()
    s.handle(o.connection_id, {"type": "chat-delete", "message_id": "does-not-exist"})
    err = frames(o, "error")
    assert err[0]["code"] == "forbidden"


def test_chat_recall_within_window_and_after(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner", "owner"))
    m = FakeConn(member("m", "mel"))
    s.join(o)
    s.join(m)

    s.handle(m.connection_id, {"type": "chat-message", "message": "recall me"})
    mid = last_chat_id(m)
    o.sent.clear()
    m.sent.clear()

    s.handle(m.connection_id, {"type": "chat-recall", "message_id": mid})
    # requester gets the original text back
    rec = frames(m, "chat-recalled")[0]
    assert rec["target_message_id"] == mid
    assert rec["original_message"] == "recall me"
    # peers get a plain delete, not the original text
    do = frames(o, "chat-deleted")[0]
    assert do["target_message_id"] == mid
    assert frames(o, "chat-recalled") == []
    assert len(s.chat) == 0

    # after the recall window -> forbidden
    s.handle(m.connection_id, {"type": "chat-message", "message": "too late"})
    mid2 = last_chat_id(m)
    clock.advance(Limits().recall_window + 1)
    m.sent.clear()
    s.handle(m.connection_id, {"type": "chat-recall", "message_id": mid2})
    assert frames(m, "error")[0]["code"] == "forbidden"
    assert len(s.chat) == 1  # still there


def test_chat_recall_by_non_author_forbidden(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    m = FakeConn(member("m"))
    s.join(o)
    s.join(m)
    s.handle(m.connection_id, {"type": "chat-message", "message": "mine"})
    mid = last_chat_id(m)
    o.sent.clear()
    # the owner is not the author -> recall is author-only -> forbidden
    s.handle(o.connection_id, {"type": "chat-recall", "message_id": mid})
    assert frames(o, "error")[0]["code"] == "forbidden"
    assert len(s.chat) == 1


# --------------------------------------------------------------------------- #
# Polydoc dispatch: ack / decorated op / duplicate / reject
# --------------------------------------------------------------------------- #


def test_poly_upsert_ack_decorate_duplicate_reject(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    o.sent.clear()
    b.sent.clear()

    # applied upsert -> ack to author, decorated op to peers
    s.handle(
        o.connection_id,
        {"type": "poly-token-upsert", "base_rev": 0, "id": "root", "kind": "grp",
         "text": "x", "author_seq": 1},
    )
    ack = frames(o, "poly-ack")[0]
    assert ack["rev"] == 1
    assert ack["applied"] == [{"id": "root", "version": 1}]
    assert ack["author_seq"] == 1
    op = frames(b, "poly-token-upsert")[0]
    assert op["rev"] == 1
    assert op["version"] == 1
    assert op["user_id"] == "owner"
    assert frames(o, "poly-token-upsert") == []  # author excluded from the op fan-out

    # duplicate retransmit (same base_rev + author_seq) -> total silence
    o.sent.clear()
    b.sent.clear()
    s.handle(
        o.connection_id,
        {"type": "poly-token-upsert", "base_rev": 0, "id": "root", "kind": "grp",
         "text": "x", "author_seq": 1},
    )
    assert o.sent == []
    assert b.sent == []

    # stale conflict -> poly-reject to author only
    o.sent.clear()
    b.sent.clear()
    s.handle(
        o.connection_id,
        {"type": "poly-token-upsert", "base_rev": 0, "id": "root", "kind": "grp",
         "text": "y", "author_seq": 2},
    )
    rej = frames(o, "poly-reject")[0]
    assert rej["reason"] == "stale"
    assert rej["id"] == "root"
    assert rej["rev"] == 1
    assert rej["author_seq"] == 2
    assert frames(b, "poly-token-upsert") == []


def test_poly_delete_ack_and_decorated_without_version(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    # seed a node so there is something to delete
    s.handle(
        o.connection_id,
        {"type": "poly-token-upsert", "base_rev": 0, "id": "root", "kind": "grp", "text": "x"},
    )
    o.sent.clear()
    b.sent.clear()
    s.handle(o.connection_id, {"type": "poly-token-delete", "base_rev": 1, "id": "root"})
    ack = frames(o, "poly-ack")[0]
    assert ack["rev"] == 2
    assert ack["applied"] == [{"id": "root", "version": 2}]
    op = frames(b, "poly-token-delete")[0]
    assert op["rev"] == 2
    assert "version" not in op  # delete ops are not decorated with a node version
    assert op["user_id"] == "owner"
    assert "root" not in s.poly.nodes


def test_poly_lock_relays_and_readonly_blocks(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    o.sent.clear()
    b.sent.clear()
    # a normal writer's lock relays to others
    s.handle(o.connection_id, {"type": "poly-lock", "node_id": "root", "locked": True})
    got = frames(b, "poly-lock")[0]
    assert got["node_id"] == "root"
    assert got["locked"] is True
    assert got["user_id"] == "owner"
    # poly-lock is a write type -> a read-only user is blocked
    s.readonly_users.add("b")
    b.sent.clear()
    o.sent.clear()
    s.handle(b.connection_id, {"type": "poly-lock", "node_id": "root", "locked": False})
    assert frames(b, "error")[0]["code"] == "readonly"
    assert frames(o, "poly-lock") == []


def test_poly_snapshot_owner_only_and_decorated(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)

    # non-owner is forbidden from poly-snapshot
    b.sent.clear()
    s.handle(
        b.connection_id,
        {"type": "poly-snapshot", "programText": "p", "nodes": []},
    )
    assert frames(b, "error")[0]["code"] == "forbidden"

    # owner snapshot -> broadcast to others decorated with rev
    o.sent.clear()
    b.sent.clear()
    s.handle(
        o.connection_id,
        {"type": "poly-snapshot", "programText": "p2",
         "nodes": [{"id": "root", "kind": "grp", "text": ""}]},
    )
    got = frames(b, "poly-snapshot")[0]
    assert got["rev"] == 1
    assert got["user_id"] == "owner"
    assert s.poly.rev == 1
    assert frames(o, "poly-snapshot") == []


def test_poly_snapshot_relay_carries_the_servers_node_versions(clock):
    """Peers must see canonical versions, not whatever the owner's client sent.

    A peer that derives its next ``base_rev`` from a relayed version would be
    rejected as stale (or worse, accepted against the wrong node).
    """
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    b.sent.clear()

    s.handle(
        o.connection_id,
        {
            "type": "poly-snapshot",
            "programText": "p",
            "nodes": [
                {"id": "n", "kind": "k", "text": "", "version": 41},  # stale client value
                {"id": "m", "kind": "k", "text": ""},  # no version at all
            ],
        },
    )

    relayed = frames(b, "poly-snapshot")[0]
    assert relayed["rev"] == s.poly.rev == 1
    assert relayed["nodes"] == s.poly.snapshot()["nodes"]
    assert {node["version"] for node in relayed["nodes"]} == {1}


def test_poly_upsert_rejects_aggregate_session_budget_without_advancing_rev(clock):
    s = Session("s", "owner", Limits(max_session_bytes=180), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    o.sent.clear()
    b.sent.clear()

    s.handle(
        o.connection_id,
        {
            "type": "poly-token-upsert",
            "base_rev": 0,
            "id": "root",
            "kind": "grp",
            "text": "x" * 100,
            "author_seq": 1,
        },
    )

    assert frames(o, "poly-reject")[-1]["reason"] == "limit"
    assert frames(b, "poly-token-upsert") == []
    assert s.poly.rev == 0
    assert s.poly.nodes == {}


def test_poly_cursor_relays_to_others_even_for_readonly(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    s.readonly_users.add("b")  # b is read-only
    o.sent.clear()
    b.sent.clear()
    # cursor is not a write; a read-only user may still emit it
    s.handle(b.connection_id, {"type": "poly-cursor", "mode": "node", "node_id": "root"})
    got = frames(o, "poly-cursor")[0]
    assert got["node_id"] == "root"
    assert got["user_id"] == "b"


# --------------------------------------------------------------------------- #
# ping / session-state
# --------------------------------------------------------------------------- #


def test_ping_returns_pong_to_sender(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    b = FakeConn(member("b"))
    s.join(o)
    s.join(b)
    o.sent.clear()
    b.sent.clear()
    s.handle(o.connection_id, {"type": "ping"})
    assert len(frames(o, "pong")) == 1
    assert frames(b, "pong") == []


def test_session_state_returns_snapshot_to_sender(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    s.join(o)
    s.handle(o.connection_id, {"type": "state-update", "id": "k", "value": 1})
    o.sent.clear()
    s.handle(o.connection_id, {"type": "session-state"})
    snap = frames(o, "session-snapshot")
    assert len(snap) == 1
    entry = snap[0]["state"][0]
    assert entry == {"id": "k", "value": 1, "seq": entry["seq"], "by": "owner"}


# --------------------------------------------------------------------------- #
# Leave semantics
# --------------------------------------------------------------------------- #


def test_leave_last_connection_broadcasts_parted(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a", "alice"))
    b = FakeConn(member("b", "bob"))
    s.join(a)
    s.join(b)
    a.sent.clear()
    s.leave(b.connection_id)
    parted = frames(a, "user-parted")
    assert len(parted) == 1
    assert parted[0]["user_id"] == "b"
    assert parted[0]["username"] == "bob"
    assert frames(a, "system-message")[-1]["message"] == "bob has left"
    assert s.empty is False


def test_leave_non_last_connection_is_quiet(clock):
    s = Session("s", "creator", Limits(), clock)
    a = FakeConn(member("a"))
    a2 = FakeConn(member("a"))
    peer = FakeConn(member("peer"))
    s.join(a)
    s.join(a2)
    s.join(peer)
    peer.sent.clear()
    s.leave(a.connection_id)  # a still has a2 open
    assert frames(peer, "user-parted") == []
    assert any(c.identity.user_id == "a" for c in s.conns.values())


def test_leave_unknown_is_noop(clock):
    s = Session("s", "creator", Limits(), clock)
    s.leave("nope")  # no raise


def test_empty_property(clock):
    s = Session("s", "creator", Limits(), clock)
    assert s.empty is True
    a = FakeConn(member("a"))
    s.join(a)
    assert s.empty is False
    s.leave(a.connection_id)
    assert s.empty is True


# --------------------------------------------------------------------------- #
# Owner recompute ladder (join/leave interleavings)
# --------------------------------------------------------------------------- #


def test_owner_ladder_creator_leaves_then_reclaims(clock):
    s = Session("s", "creator", Limits(), clock)
    cr = FakeConn(member("creator"))
    e1 = FakeConn(member("early"))
    e2 = FakeConn(member("late"))
    s.join(cr)
    s.join(e1)
    s.join(e2)
    assert s.owner_user_id == "creator"

    e1.sent.clear()
    s.leave(cr.connection_id)
    # created_by gone -> earliest remaining joiner (early) owns
    assert s.owner_user_id == "early"
    assert frames(e1, "owner-changed")[-1]["user_id"] == "early"

    # creator returns -> reclaims via the created_by rule
    cr2 = FakeConn(member("creator"))
    e1.sent.clear()
    s.join(cr2)
    assert s.owner_user_id == "creator"
    assert frames(e1, "owner-changed")[-1]["user_id"] == "creator"


def test_owner_excludes_ephemeral(clock):
    s = Session("s", "ghost-creator", Limits(), clock)
    e = FakeConn(ephemeral("eph"))
    s.join(e)
    # a gs_ephemeral is never an owner candidate
    assert s.owner_user_id is None
    assert e.sent[0]["you"]["is_owner"] is False
    assert e.sent[0]["owner"] is None
    # a member joining becomes owner
    m = FakeConn(member("m"))
    s.join(m)
    assert s.owner_user_id == "m"


# --------------------------------------------------------------------------- #
# kick_user
# --------------------------------------------------------------------------- #


def test_kick_user_closes_all_tabs_and_returns_count(clock):
    s = Session("s", "owner", Limits(), clock)
    o = FakeConn(member("owner"))
    t1 = FakeConn(member("t"))
    t2 = FakeConn(member("t"))
    s.join(o)
    s.join(t1)
    s.join(t2)
    n = s.kick_user("t")
    assert n == 2
    assert t1.closed == (protocol.CLOSE_KICKED, "kicked")
    assert t2.closed == (protocol.CLOSE_KICKED, "kicked")
    assert not any(c.identity.user_id == "t" for c in s.conns.values())


# --------------------------------------------------------------------------- #
# freeze / thaw round-trip
# --------------------------------------------------------------------------- #


def test_freeze_thaw_round_trip(clock):
    s = Session("s01", "owner", Limits(), clock, bans={"banned1"})
    o = FakeConn(member("owner"))
    s.join(o)
    s.handle(o.connection_id, {"type": "state-update", "id": "k", "value": 1})
    s.handle(o.connection_id, {"type": "chat-message", "message": "hello"})
    s.readonly_users.add("ro-user")
    s.settings.explicit_owner = "owner"

    payload = s.freeze_snapshot()
    # exactly the store contract keys
    assert set(payload) == {
        "created_by", "dialect", "created_at", "settings", "state", "data", "poly",
        "docs", "chat", "rev", "seq", "frozen_at", "last_active", "first_joined_at",
    }
    assert payload["frozen_at"] is None
    assert payload["settings"]["readonly_users"] == ["ro-user"]
    assert payload["settings"]["explicit_owner"] == "owner"
    seq_before = payload["seq"]
    rev_before = payload["rev"]

    t = Session.thaw("s01", payload, Limits(), clock, bans={"banned1"})
    assert t.seq == seq_before
    assert t.poly.rev == rev_before
    assert t.bans == {"banned1"}
    assert t.readonly_users == {"ro-user"}
    assert t.settings.explicit_owner == "owner"
    assert t.state.snapshot() == s.state.snapshot()
    assert len(t.chat) == 1
    assert t._join_order == {}  # first-join counters reset for the new epoch

    # bans survive the round-trip
    try:
        t.join(FakeConn(anon("banned1")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("banned user should be refused after thaw")

    # seq continues monotonically on the thawed session: the new entry's seq (which
    # equals the frame seq) is drawn from the restored counter, so it exceeds it.
    o2 = FakeConn(member("owner"))
    t.join(o2)
    t.handle(o2.connection_id, {"type": "state-update", "id": "k2", "value": 2})
    k2_entry = [e for e in t.state.snapshot() if e["id"] == "k2"][0]
    assert k2_entry["seq"] > seq_before


def test_incremental_budget_accounting_matches_full_serialization(clock):
    """data-update, poly upsert/delete and doc-edit account per item, exactly.

    Every step (including budget refusals and their in-place rollbacks) must
    leave the cached totals equal to a full re-serialization of the content.
    """
    from random import Random

    rng = Random(7)
    limits = Limits(
        max_session_bytes=5000, max_doc_oplog=6, max_doc_oplog_bytes=1500, chat_history=5
    )
    s = Session("s", "owner", limits, clock)
    owner, w = FakeConn(member("owner")), FakeConn(member("w"))
    s.join(owner)
    s.join(w)
    s.handle(owner.connection_id, {"type": "doc-create", "doc": {
        "id": "d", "title": "t", "kind": "k", "text": "seed", "default": True}})
    seq = 0
    refusals = 0
    for _ in range(400):
        blob = "x" * rng.randint(0, 400)
        op = rng.random()
        before = len(w.sent)
        if op < 0.3:
            s.handle(w.connection_id, {"type": "data-update", "id": rng.choice("ab"),
                                       "role": rng.choice("rs"), "key": rng.choice("xyz"),
                                       "value": blob})
        elif op < 0.55:
            s.handle(w.connection_id, {"type": "poly-token-upsert", "base_rev": s.poly.rev,
                                       "id": rng.choice(["n1", "n2", "n1.c", "n1.c.d"]),
                                       "kind": "k", "text": blob, "parentId": None})
        elif op < 0.7:
            s.handle(w.connection_id, {"type": "poly-token-delete", "base_rev": s.poly.rev,
                                       "id": rng.choice(["n1", "n2", "n9"])})
        else:
            text = s.docs.snapshot()[0]["text"]
            pos = rng.randint(0, len(text))
            seq += 1
            s.handle(w.connection_id, {"type": "doc-edit", "docId": "d",
                                       "baseRev": s.docs.snapshot()[0]["rev"],
                                       "authorSeq": seq,
                                       "edit": {"start": pos,
                                                "end": min(len(text), pos + rng.randint(0, 3)),
                                                "text": blob[:60]}})
        refusals += sum(
            1 for f in w.sent[before:]
            if f["type"] == "error" or f.get("reason") in ("limit", "too_large")
        )
        payload = s._content_payload()
        assert s._content_bytes == protocol.json_size(payload)
        assert s._content_component_sizes == {
            k: protocol.json_size(v) for k, v in payload.items()
        }
    assert refusals > 0, "the budget was never hit; the rollback paths went untested"
