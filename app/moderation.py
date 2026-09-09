"""Owner moderation verbs for a live :class:`app.session.Session` (spec §8).

:func:`handle_mod` is reached only after :meth:`Session.handle` has confirmed the
actor is the acting owner (the ``mod-*`` types are owner-gated), so this module
implements only the per-verb target logic, the durable side effects, and the
uniform trailer that every *successful* verb shares:

* an :class:`app.audit.AuditEvent` forwarded to the session's ``audit_cb``, and
* a ``moderation {action, target_user, by}`` broadcast to the whole room.

A failed permission check (self-targeting, an absent/ineligible target) sends a
single ``error {code:"forbidden"}`` to the actor and records nothing. Bans are
kept on the session (``session.bans``) and mirrored to the hub's store via
``ban_sink`` so a banned user stays blocked across freeze/thaw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.audit import AuditEvent
from app.identity import Kind
from app.protocol import ErrorCode

if TYPE_CHECKING:
    from app.session import ConnLike, Session


def handle_mod(session: Session, actor: ConnLike, msg: dict) -> None:
    """Dispatch one validated ``mod-*`` frame from the acting owner."""
    verb = _VERBS[msg["type"]]
    verb(session, actor, msg)


# --------------------------------------------------------------------------- #
# Per-verb handlers
# --------------------------------------------------------------------------- #


def _kick(session: Session, actor: ConnLike, msg: dict) -> None:
    target = _resolve_kick_target(session, actor, msg)
    if target is None:
        return
    session.kick_user(target)
    _finish(session, actor, msg, target, {})


def _ban(session: Session, actor: ConnLike, msg: dict) -> None:
    target = msg["target_user"]
    if target == actor.identity.user_id:
        session.send_error(actor, ErrorCode.forbidden, detail="cannot target self")
        return
    if target == session.created_by:
        # An acting owner (possibly an anonymous guest who inherited the role
        # while the creator was away) must not be able to lock the creator out
        # of their own session; bans persist and nobody else could lift it.
        session.send_error(actor, ErrorCode.forbidden, detail="cannot ban the session creator")
        return
    session.bans.add(target)
    session.fire_ban_sink(target, actor.identity.username, True)
    session.kick_user(target)  # no-op when the target is offline
    _finish(session, actor, msg, target, {})


def _unban(session: Session, actor: ConnLike, msg: dict) -> None:
    target = msg["target_user"]
    session.bans.discard(target)
    session.fire_ban_sink(target, actor.identity.username, False)
    _finish(session, actor, msg, target, {})


def _lock(session: Session, actor: ConnLike, msg: dict) -> None:
    session.settings.locked = bool(msg["locked"])
    _finish(session, actor, msg, None, {"locked": session.settings.locked})


def _guests(session: Session, actor: ConnLike, msg: dict) -> None:
    session.settings.guests_allowed = bool(msg["allowed"])
    _finish(session, actor, msg, None, {"allowed": session.settings.guests_allowed})


def _readonly(session: Session, actor: ConnLike, msg: dict) -> None:
    target = msg["target_user"]
    readonly = bool(msg["readonly"])
    # The owner may lift their OWN readonly (a user readonly'd before becoming
    # owner would otherwise be stuck); self-targeting with readonly:true stays
    # forbidden so an owner cannot lock themselves out.
    if target == actor.identity.user_id and readonly:
        session.send_error(actor, ErrorCode.forbidden, detail="cannot target self")
        return
    if readonly:
        session.readonly_users.add(target)
    else:
        session.readonly_users.discard(target)
    _finish(session, actor, msg, target, {"readonly": readonly})


def _transfer(session: Session, actor: ConnLike, msg: dict) -> None:
    target = msg["target_user"]
    ident = session._present_identity(target)
    if ident is None or ident.kind == Kind.GS_EPHEMERAL:
        session.send_error(actor, ErrorCode.forbidden, detail="target not eligible")
        return
    session.settings.explicit_owner = target
    session._recompute_owner()
    _finish(session, actor, msg, target, {})


_VERBS = {
    "mod-kick": _kick,
    "mod-ban": _ban,
    "mod-unban": _unban,
    "mod-lock": _lock,
    "mod-guests": _guests,
    "mod-readonly": _readonly,
    "mod-transfer": _transfer,
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _resolve_kick_target(session: Session, actor: ConnLike, msg: dict) -> str | None:
    """Resolve a kick target to a present user_id, or send ``forbidden`` and return None."""
    target = msg.get("target_user")
    if target is None:
        tconn = session.conns.get(msg.get("target_connection"))
        if tconn is None:
            session.send_error(actor, ErrorCode.forbidden, detail="no such target")
            return None
        target = tconn.identity.user_id
    if target == actor.identity.user_id:
        session.send_error(actor, ErrorCode.forbidden, detail="cannot target self")
        return None
    if session._present_identity(target) is None:
        session.send_error(actor, ErrorCode.forbidden, detail="target not present")
        return None
    return target


def _finish(
    session: Session, actor: ConnLike, msg: dict, target: str | None, detail: dict
) -> None:
    """Audit the action and broadcast a ``moderation`` event to the whole room."""
    action = msg["type"]
    session.record_audit(
        AuditEvent(
            ts=int(session.clock()),
            session_id=session.session_id,
            actor=actor.identity.username,
            action=action,
            target=target,
            detail=detail,
        )
    )
    session._broadcast(
        {
            "type": "moderation",
            "action": action.removeprefix("mod-"),
            "target_user": target,
            "by": actor.identity.username,
            "detail": detail,
        }
    )
