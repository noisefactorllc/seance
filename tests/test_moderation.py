"""Tests for owner moderation verbs (app.moderation.handle_mod via Session.handle).

Exercised through ``Session.handle`` so the full path is covered: validation,
the readonly/owner authorization gates, the verb effect, the audit emission, and
the ``moderation`` broadcast. ``audit_cb`` and ``ban_sink`` are captured as plain
callables per the Task 9 contract.
"""

from app import protocol
from app.config import Limits
from app.identity import Identity, Kind
from app.session import JoinRefused, Session
from tests.conftest import FakeConn

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def member(uid, name=None):
    return Identity(user_id=uid, username=name or uid, kind=Kind.MEMBER)


def anon(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.ANON)


def ephemeral(uid, name=None):
    return Identity(user_id=uid, username=name or f"guest-{uid[:6]}", kind=Kind.GS_EPHEMERAL)


def frames(conn, ftype):
    return [f for f in conn.sent if f["type"] == ftype]


class Harness:
    """A live owned session with an owner, a member, and captured audit/ban sinks."""

    def __init__(self, clock, **limit_overrides):
        self.audits = []
        self.bans_fired = []
        self.session = Session(
            "sess",
            "owner",
            Limits(**limit_overrides),
            clock,
            audit_cb=self.audits.append,
            ban_sink=lambda *a: self.bans_fired.append(a),
        )
        self.owner = FakeConn(member("owner", "owner"))
        self.mem = FakeConn(member("m", "mel"))
        self.session.join(self.owner)
        self.session.join(self.mem)

    def owner_do(self, msg):
        self.owner.sent.clear()
        self.session.handle(self.owner.connection_id, msg)


# --------------------------------------------------------------------------- #
# kick / ban / unban
# --------------------------------------------------------------------------- #


def test_kick_disconnects_all_tabs_without_ban(clock):
    h = Harness(clock)
    t2 = FakeConn(member("m", "mel"))  # a second tab of the target member
    h.session.join(t2)

    h.owner_do({"type": "mod-kick", "target_user": "m"})

    assert h.mem.closed == (protocol.CLOSE_KICKED, "kicked")
    assert t2.closed == (protocol.CLOSE_KICKED, "kicked")
    assert not any(c.identity.user_id == "m" for c in h.session.conns.values())
    assert "m" not in h.session.bans
    assert h.bans_fired == []
    # one audit + one moderation broadcast
    assert len(h.audits) == 1
    assert h.audits[0].action == "mod-kick"
    assert h.audits[0].actor == "owner"
    assert h.audits[0].target == "m"
    mod = frames(h.owner, "moderation")[0]
    assert mod["action"] == "kick"
    assert mod["target_user"] == "m"
    assert mod["by"] == "owner"


def test_kick_by_connection_resolves_user(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-kick", "target_connection": h.mem.connection_id})
    assert h.mem.closed == (protocol.CLOSE_KICKED, "kicked")
    assert frames(h.owner, "moderation")[0]["target_user"] == "m"


def test_kick_absent_target_is_forbidden(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-kick", "target_user": "ghost"})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert h.audits == []  # failed permission -> no audit


def test_ban_kicks_persists_and_refuses_rejoin(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-ban", "target_user": "m"})

    assert "m" in h.session.bans
    assert h.mem.closed == (protocol.CLOSE_KICKED, "kicked")
    assert h.bans_fired == [("sess", "m", "owner", True)]
    assert frames(h.owner, "moderation")[0]["action"] == "ban"
    assert len(h.audits) == 1 and h.audits[0].action == "mod-ban"

    # rejoin refused
    try:
        h.session.join(FakeConn(member("m")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("banned user should be refused")


def test_ban_offline_user_still_persists(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-ban", "target_user": "absent-user"})
    assert "absent-user" in h.session.bans
    assert h.bans_fired == [("sess", "absent-user", "owner", True)]


def test_acting_owner_cannot_ban_the_session_creator(clock):
    """An acting owner (a guest who inherited the role) must not lock the creator out.

    Bans persist and only an owner can lift one, so banning ``created_by`` is a
    permanent takeover of somebody else's session.
    """
    h = Harness(clock)
    guest = FakeConn(anon("ag"))
    h.session.join(guest)
    for connection_id in [c.connection_id for c in (h.owner, h.mem)]:
        h.session.leave(connection_id)
    assert h.session.owner_user_id == "ag"

    guest.sent.clear()
    h.session.handle(guest.connection_id, {"type": "mod-ban", "target_user": "owner"})

    assert frames(guest, "error")[0]["code"] == "forbidden"
    assert "owner" not in h.session.bans
    assert h.bans_fired == []
    assert h.audits == []
    h.session.join(FakeConn(member("owner")))  # the creator can still return


def test_unban_fires_sink_and_readmits(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-ban", "target_user": "m"})
    h.bans_fired.clear()
    h.owner_do({"type": "mod-unban", "target_user": "m"})
    assert "m" not in h.session.bans
    assert h.bans_fired == [("sess", "m", "owner", False)]
    assert frames(h.owner, "moderation")[0]["action"] == "unban"
    # a formerly-banned user can now rejoin
    h.session.join(FakeConn(member("m")))


def test_owner_cannot_ban_or_kick_self(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-ban", "target_user": "owner"})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert "owner" not in h.session.bans
    assert h.audits == []

    h.owner_do({"type": "mod-kick", "target_user": "owner"})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert h.audits == []


# --------------------------------------------------------------------------- #
# lock / guests
# --------------------------------------------------------------------------- #


def test_lock_toggles_and_blocks_new_joins(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-lock", "locked": True})
    assert h.session.settings.locked is True
    assert h.audits[-1].detail == {"locked": True}
    try:
        h.session.join(FakeConn(member("newbie")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LOCKED
    else:
        raise AssertionError("locked session should refuse new joins")
    h.owner_do({"type": "mod-lock", "locked": False})
    assert h.session.settings.locked is False


def test_lock_still_admits_the_creator_and_an_explicit_owner(clock):
    """A lock keeps newcomers out; it must not seal the session against its keys.

    The lock is persisted, so an owner who locks and then reloads (or whose
    session freezes) would otherwise never get back in to lift it.
    """
    h = Harness(clock)
    h.owner_do({"type": "mod-lock", "locked": True})
    h.owner_do({"type": "mod-transfer", "target_user": "m"})
    assert h.session.settings.explicit_owner == "m"

    h.session.join(FakeConn(member("owner")))  # created_by
    h.session.join(FakeConn(member("m")))  # explicit_owner

    try:
        h.session.join(FakeConn(member("newbie")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_LOCKED
    else:
        raise AssertionError("locked session should still refuse other joins")


def test_lock_admits_the_creator_after_freeze_and_thaw(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-lock", "locked": True})
    for connection_id in list(h.session.conns):
        h.session.leave(connection_id)

    thawed = Session.thaw("sess", h.session.freeze_snapshot(), Limits(), clock)

    thawed.join(FakeConn(member("owner")))
    assert thawed.settings.locked is True


def test_guests_toggle_boots_nobody_but_blocks_new_anon(clock):
    h = Harness(clock)
    guest = FakeConn(anon("ag"))
    h.session.join(guest)

    h.owner_do({"type": "mod-guests", "allowed": False})
    assert h.session.settings.guests_allowed is False
    # present guest is not booted
    assert any(c.identity.user_id == "ag" for c in h.session.conns.values())
    # new anon is refused
    try:
        h.session.join(FakeConn(anon("newguest")))
    except JoinRefused as exc:
        assert exc.close_code == protocol.CLOSE_FORBIDDEN
    else:
        raise AssertionError("guests-off should refuse new anon joins")


# --------------------------------------------------------------------------- #
# readonly
# --------------------------------------------------------------------------- #


def test_readonly_blocks_target_write_not_others(clock):
    h = Harness(clock)
    other = FakeConn(member("n", "nan"))
    h.session.join(other)

    h.owner_do({"type": "mod-readonly", "target_user": "m", "readonly": True})
    assert "m" in h.session.readonly_users

    h.mem.sent.clear()
    h.session.handle(h.mem.connection_id, {"type": "state-update", "id": "x", "value": 1})
    assert frames(h.mem, "error")[0]["code"] == "readonly"

    # a non-readonly member is unaffected
    other.sent.clear()
    h.session.handle(other.connection_id, {"type": "state-update", "id": "y", "value": 2})
    assert frames(other, "error") == []
    assert any(e["id"] == "y" for e in h.session.state.snapshot())

    # clearing readonly re-enables writes
    h.owner_do({"type": "mod-readonly", "target_user": "m", "readonly": False})
    assert "m" not in h.session.readonly_users


def test_readonly_cannot_target_self(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-readonly", "target_user": "owner", "readonly": True})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert "owner" not in h.session.readonly_users


def test_readonly_owner_can_lift_own_readonly(clock):
    h = Harness(clock)
    # A user readonly'd before becoming owner would otherwise be stuck; the owner
    # may clear their OWN readonly (readonly:false self-target is allowed).
    h.session.readonly_users.add("owner")
    assert h.session._is_readonly(h.owner.identity)

    h.owner_do({"type": "mod-readonly", "target_user": "owner", "readonly": False})
    assert "owner" not in h.session.readonly_users
    # audit + moderation broadcast fire as for any successful readonly action
    assert h.audits[-1].action == "mod-readonly"
    assert h.audits[-1].target == "owner"
    mod = frames(h.owner, "moderation")[-1]
    assert mod["action"] == "readonly"
    assert mod["target_user"] == "owner"
    assert mod["detail"] == {"readonly": False}
    # writes flow again now that the owner's readonly is cleared
    h.owner.sent.clear()
    h.session.handle(h.owner.connection_id, {"type": "state-update", "id": "z", "value": 7})
    assert frames(h.owner, "error") == []
    assert any(e["id"] == "z" for e in h.session.state.snapshot())


# --------------------------------------------------------------------------- #
# transfer + owner recompute interplay
# --------------------------------------------------------------------------- #


def test_transfer_to_present_member_emits_owner_changed(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-transfer", "target_user": "m"})
    assert h.session.settings.explicit_owner == "m"
    assert h.session.owner_user_id == "m"
    oc = frames(h.owner, "owner-changed")[-1]
    assert oc["user_id"] == "m"
    # a moderation broadcast also fired
    assert frames(h.owner, "moderation")[-1]["action"] == "transfer"


def test_transfer_absent_or_ephemeral_forbidden(clock):
    h = Harness(clock)
    h.owner_do({"type": "mod-transfer", "target_user": "ghost"})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert h.session.owner_user_id == "owner"

    eph = FakeConn(ephemeral("eph"))
    h.session.join(eph)
    h.owner_do({"type": "mod-transfer", "target_user": "eph"})
    assert frames(h.owner, "error")[0]["code"] == "forbidden"
    assert h.session.owner_user_id == "owner"


def test_transfer_suppresses_creator_reclaim_until_owner_leaves(clock):
    s = Session("sess", "creator", Limits(), clock)
    cr = FakeConn(member("creator"))
    late = FakeConn(member("late"))
    s.join(cr)
    s.join(late)
    assert s.owner_user_id == "creator"

    # explicit transfer to late; creator is still present but no longer owner
    s.handle(cr.connection_id, {"type": "mod-transfer", "target_user": "late"})
    assert s.owner_user_id == "late"
    assert s.settings.explicit_owner == "late"

    # explicit owner leaves -> explicit_owner cleared -> falls back to creator rule
    cr.sent.clear()
    s.leave(late.connection_id)
    assert s.settings.explicit_owner is None
    assert s.owner_user_id == "creator"
    assert frames(cr, "owner-changed")[-1]["user_id"] == "creator"


def test_non_owner_mod_is_forbidden(clock):
    h = Harness(clock)
    h.mem.sent.clear()
    h.session.handle(h.mem.connection_id, {"type": "mod-lock", "locked": True})
    assert frames(h.mem, "error")[0]["code"] == "forbidden"
    assert h.session.settings.locked is False
    assert h.audits == []


def test_ephemeral_mod_is_forbidden(clock):
    s = Session("sess", "creator", Limits(), clock)
    creator = FakeConn(member("creator"))
    eph = FakeConn(ephemeral("eph"))
    s.join(creator)
    s.join(eph)
    eph.sent.clear()
    s.handle(eph.connection_id, {"type": "mod-kick", "target_user": "creator"})
    assert frames(eph, "error")[0]["code"] == "forbidden"


def test_every_successful_verb_audits_and_broadcasts_once(clock):
    h = Harness(clock)
    n = FakeConn(member("n"))
    h.session.join(n)

    verbs = [
        {"type": "mod-lock", "locked": True},
        {"type": "mod-guests", "allowed": False},
        {"type": "mod-readonly", "target_user": "m", "readonly": True},
        {"type": "mod-transfer", "target_user": "n"},
    ]
    for i, msg in enumerate(verbs, start=1):
        # after transfer the owner changes; drive each verb from the current owner
        actor = h.owner if h.session.owner_user_id == "owner" else n
        actor.sent.clear()
        h.session.handle(actor.connection_id, msg)
        assert len(h.audits) == i
        assert len(frames(actor, "moderation")) == 1
