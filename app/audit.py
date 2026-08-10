"""Structured audit trail for the seance server.

Defines the immutable :class:`AuditEvent` record and :func:`log_audit`, which
emits exactly one compact JSON line per event on the ``seance.audit`` logger.

Audit lines are deliberately identity-only: they carry ``user_id`` / username
material via ``actor`` / ``target`` and a caller-supplied ``detail`` dict, and
never any secret — no tokens, cookies, IP addresses, or email. Callers are
responsible for keeping such material out of ``detail``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_AUDIT_LOGGER = logging.getLogger("seance.audit")


@dataclass(frozen=True)
class AuditEvent:
    """A single, immutable audit-trail record."""

    ts: int
    session_id: str | None
    actor: str
    action: str
    target: str | None
    detail: dict


def log_audit(event: AuditEvent) -> None:
    """Emit ``event`` as one compact, key-sorted JSON line on ``seance.audit``."""
    _AUDIT_LOGGER.info(
        "%s",
        json.dumps(
            {
                "ts": event.ts,
                "session": event.session_id,
                "actor": event.actor,
                "action": event.action,
                "target": event.target,
                "detail": event.detail,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


# An optional async hook other modules may install to observe audit events.
AuditSink = Callable[[AuditEvent], Awaitable[None]] | None
