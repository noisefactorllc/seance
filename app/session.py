"""The :class:`Session` — seance's server-authoritative collaboration core.

A Session is a **pure synchronous state machine**: it owns the roster, the owner
rules, the three engine lanes, the bounded chat history, and the full inbound
message dispatch with server-stamped fan-out. It performs no I/O and imports no
asyncio — connections are duck-typed sinks (:class:`ConnLike`) that the transport
supplies, and time is injected as ``clock``.

Responsibilities (spec §5-§8):

* **Roster** — multiple connections per user (multi-tab); join/part events fire on
  a user's first connection and last disconnection.
* **Owner** — the acting owner is recomputed from the present roster on every
  join/leave/transfer; a change to a non-``None`` owner emits ``owner-changed``.
* **Dispatch** — :meth:`Session.handle` validates each frame, applies the
  readonly/owner authorization gates, mutates the engine, and fans the result out
  with a per-session monotonic ``seq`` stamped into every outbound envelope.
* **Persistence** — :meth:`freeze_snapshot` / :meth:`thaw` round-trip the whole
  session to and from the store payload contract; bans arrive from the hub and
  are mirrored back through ``ban_sink``.

Every outbound frame is stamped via :func:`app.protocol.stamp`. A broadcast is one
logical event: all copies share a single ``seq`` and ``message_id``. Recipient-
specific frames (``welcome``, ``session-snapshot``, ``pong``, ``error``,
``poly-ack``, ``poly-reject``, ``doc-ack``, ``doc-reject``, ``chat-recalled``)
each consume their own ``seq``.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app import protocol
from app.audit import AuditEvent
from app.config import Limits
from app.engine import DataLane, EngineLimit, PolyDoc, StateLane
from app.identity import Identity, Kind
from app.moderation import handle_mod
from app.protocol import ErrorCode, ProtocolError
from app.textdoc import TextDocCollection, TextDocReject, TextEdit, _OpLogEntry, to_utf16_units

_GUEST_KINDS = (Kind.ANON, Kind.GS_EPHEMERAL)


class JoinRefused(Exception):
    """A join was refused; ``close_code`` is the WebSocket close code to send."""

    def __init__(self, close_code: int, reason: str):
        super().__init__(reason)
        self.close_code = close_code
        self.reason = reason


@dataclass
class Settings:
    """Mutable per-session policy. ``explicit_owner`` is set by ``mod-transfer``."""

    locked: bool = False
    guests_allowed: bool = True
    guests_readonly: bool = False
    explicit_owner: str | None = None


class ConnLike(Protocol):
    """The connection contract a Session drives (transport ``WsConn`` / ``FakeConn``)."""

    identity: Identity
    connection_id: str
    declared_dialects: list[str] | None

    def send_json(self, msg: dict) -> bool: ...

    def close_soon(self, code: int, reason: str = "") -> None: ...


class Session:
    """One live collaboration session: roster, owner, engine lanes, dispatch."""

    def __init__(
        self,
        session_id: str,
        created_by: str,
        limits: Limits,
        clock: Callable[[], float],
        settings: Settings | None = None,
        bans: set[str] | None = None,
        audit_cb: Callable[[AuditEvent], None] | None = None,
        ban_sink: Callable[[str, str, str, bool], None] | None = None,
        dialect: str = protocol.DEFAULT_DIALECT,
    ):
        self.session_id = session_id
        self.created_by = created_by
        self.dialect = dialect
        self.limits = limits
        self.clock = clock
        self.settings = settings if settings is not None else Settings()
        self.bans: set[str] = set(bans) if bans is not None else set()
        self.audit_cb = audit_cb
        self.ban_sink = ban_sink
        self.created_at = int(clock())

        self.state = StateLane(limits)
        self.data = DataLane(limits)
        self.poly = PolyDoc(limits)
        self.docs = TextDocCollection(limits)
        self.chat: deque[dict] = deque(maxlen=limits.chat_history)
        self.conns: dict[str, ConnLike] = {}
        self.readonly_users: set[str] = set()

        self._join_order: dict[str, int] = {}
        self._join_counter = 0
        self.seq = 0
        self._owner: str | None = None

        self._reset_content_budget()

        self._dispatch: dict[str, Callable[[ConnLike, dict], None]] = {
            "state-set": self._do_state_set,
            "state-update": self._do_state_update,
            "data-update": self._do_data_update,
            "clicked-button": self._do_clicked_button,
            "chat-message": self._do_chat_message,
            "chat-delete": self._do_chat_delete,
            "chat-recall": self._do_chat_recall,
            "ping": self._do_ping,
            "session-state": self._do_session_state,
            "poly-snapshot": self._do_poly_snapshot,
            "poly-token-upsert": self._do_poly_upsert,
            "poly-token-delete": self._do_poly_delete,
            "poly-cursor": self._do_poly_relay,
            "poly-lock": self._do_poly_relay,
            "doc-create": self._do_doc_create,
            "doc-edit": self._do_doc_edit,
            "doc-cursor": self._do_doc_cursor,
            "doc-reset": self._do_doc_reset,
            "mod-kick": self._do_mod,
            "mod-ban": self._do_mod,
            "mod-unban": self._do_mod,
            "mod-lock": self._do_mod,
            "mod-guests": self._do_mod,
            "mod-readonly": self._do_mod,
            "mod-transfer": self._do_mod,
        }

    # --------------------------------------------------------------------- #
    # Properties
    # --------------------------------------------------------------------- #

    @property
    def empty(self) -> bool:
        """True when no connections remain (a freeze candidate)."""
        return not self.conns

    @property
    def owner_user_id(self) -> str | None:
        """The acting owner's ``user_id`` (spec §5), or ``None`` when unowned."""
        return self._owner

    @property
    def content_bytes(self) -> int:
        """Compact-JSON bytes occupied by all persisted, client-writable content."""
        return self._content_bytes

    @property
    def content_within_budget(self) -> bool:
        """Whether persisted client-writable content fits the aggregate cap."""
        return self._content_bytes <= self.limits.max_session_bytes

    def _frozen_docs(self, docs: TextDocCollection | None = None) -> list[dict]:
        """Return the persistence form of the document lane, including its op logs."""
        collection = docs if docs is not None else self.docs
        return [
            {
                "id": doc._doc_id,
                "title": doc._title,
                "kind": doc._kind,
                "rev": doc._rev,
                "text": doc._text,
                "default": doc._default,
                "oplog": [
                    {
                        "rev": entry.rev,
                        "edit": self._serialize_edit(entry.edit),
                        "prior_text": entry.prior_text,
                    }
                    for entry in doc._oplog
                ],
            }
            for doc in collection._docs.values()
        ]

    def _content_payload(self) -> dict:
        return {
            "state": self.state.snapshot(),
            "data": self.data.snapshot(),
            "poly": self.poly.snapshot(),
            "docs": self._frozen_docs(),
            "chat": list(self.chat),
        }

    def _reset_content_budget(self) -> None:
        payload = self._content_payload()
        self._content_component_sizes = {
            name: protocol.json_size(value) for name, value in payload.items()
        }
        self._state_entry_sizes = {
            item["id"]: protocol.json_size(item) for item in payload["state"]
        }
        self._refresh_lane_sizes("poly", payload["poly"])
        self._refresh_lane_sizes("docs", payload["docs"])
        self._content_bytes = protocol.json_size(payload)

    def _refresh_lane_sizes(self, name: str, serialized) -> None:
        """Rebuild the per-node (poly) or per-document (docs) size caches.

        The proposal-lane handlers account for one node or one document per
        frame from these caches instead of copying and re-serializing the whole
        lane. ``serialized`` is the lane's persistence form, so a full commit
        (snapshot lane) refreshes them from what it already serialized.
        """
        if name == "poly":
            self._poly_node_sizes = {
                node["id"]: protocol.json_size(node) for node in serialized["nodes"]
            }
            self._poly_nodes_total = sum(self._poly_node_sizes.values())
            # The lane minus its node entries and rev digits ("rev": 0 is one digit).
            self._poly_static_size = (
                protocol.json_size(
                    {
                        "rev": 0,
                        "programText": serialized["programText"],
                        "frame": serialized["frame"],
                        "nodes": [],
                    }
                )
                - 1
            )
        elif name == "docs":
            self._doc_sizes = {doc["id"]: protocol.json_size(doc) for doc in serialized}

    def _poly_lane_size(self) -> int:
        """Exact compact-JSON bytes of ``poly.snapshot()``, from the node cache."""
        count = len(self._poly_node_sizes)
        return (
            self._poly_static_size
            + len(str(self.poly.rev))
            + self._poly_nodes_total
            + max(0, count - 1)
        )

    def _record_lane_size(self, name: str, size: int) -> None:
        self._content_bytes += size - self._content_component_sizes[name]
        self._content_component_sizes[name] = size

    def _frozen_doc_size(self, doc_id: str) -> int:
        """Exact compact-JSON bytes of one document's persistence form (op log included)."""
        view = TextDocCollection(self.limits)
        view._docs = {doc_id: self.docs._docs[doc_id]}
        return protocol.json_size(self._frozen_docs(view)[0])

    @staticmethod
    def _doc_state(doc) -> tuple:
        """Capture what an edit mutates, so a refused edit can be undone in place."""
        return (
            doc._text,
            doc._rev,
            list(doc._oplog),
            doc._oplog_bytes,
            dict(doc._retry_cache),
            dict(doc._author_seq_highwater),
        )

    @staticmethod
    def _restore_doc_state(doc, state: tuple) -> None:
        (
            doc._text,
            doc._rev,
            doc._oplog,
            doc._oplog_bytes,
            doc._retry_cache,
            doc._author_seq_highwater,
        ) = state

    def recalculate_content_budget(self) -> None:
        """Recalculate accounting after a trusted bulk load or creator snapshot."""
        self._reset_content_budget()

    def _commit_content(self, name: str, candidate, serialized) -> bool:
        """Atomically replace one content lane when its aggregate size is allowed."""
        candidate_size = protocol.json_size(serialized)
        if not self._commit_content_size(name, candidate, candidate_size):
            return False
        self._refresh_lane_sizes(name, serialized)
        return True

    def _commit_content_size(self, name: str, candidate, candidate_size: int) -> bool:
        """Commit one lane using an already-computed exact serialized size."""
        total = self._content_bytes - self._content_component_sizes[name] + candidate_size
        if total > self.limits.max_session_bytes and total >= self._content_bytes:
            return False
        setattr(self, name, candidate)
        self._content_component_sizes[name] = candidate_size
        self._content_bytes = total
        return True

    # --------------------------------------------------------------------- #
    # Stamping / fan-out primitives
    # --------------------------------------------------------------------- #

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _stamp(
        self, body: dict, seq: int, *, user_id: str, username: str, connection_id: str
    ) -> dict:
        return protocol.stamp(
            body,
            seq=seq,
            session=self.session_id,
            user_id=user_id,
            username=username,
            connection_id=connection_id,
            now=self.clock(),
        )

    def _send(
        self, conn: ConnLike, body: dict, *, user_id: str = "server",
        username: str = "server", connection_id: str = "server",
    ) -> dict:
        frame = self._stamp(body, self._next_seq(), user_id=user_id, username=username,
                            connection_id=connection_id)
        conn.send_json(frame)
        return frame

    def _broadcast(
        self, body: dict, seq: int | None = None, *, exclude: str | None = None,
        user_id: str = "server", username: str = "server", connection_id: str = "server",
    ) -> dict:
        if seq is None:
            seq = self._next_seq()
        frame = self._stamp(body, seq, user_id=user_id, username=username,
                            connection_id=connection_id)
        for cid, conn in self.conns.items():
            if cid != exclude:
                conn.send_json(frame)
        return frame

    def send_error(
        self, conn: ConnLike, code: ErrorCode, detail: str = "", ref_type: str | None = None
    ) -> dict:
        """Send a server-stamped ``error`` frame to a single connection."""
        return self._send(conn, protocol.error_frame(code, detail=detail, ref_type=ref_type))

    # --------------------------------------------------------------------- #
    # Roster / owner helpers
    # --------------------------------------------------------------------- #

    def _present_user_ids(self) -> set[str]:
        return {conn.identity.user_id for conn in self.conns.values()}

    def _present_identity(self, user_id: str) -> Identity | None:
        for conn in self.conns.values():
            if conn.identity.user_id == user_id:
                return conn.identity
        return None

    def _roster(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for conn in self.conns.values():
            ident = conn.identity
            entry = agg.get(ident.user_id)
            if entry is None:
                agg[ident.user_id] = {
                    "user_id": ident.user_id,
                    "username": ident.username,
                    "kind": ident.kind.value,
                    "connections": 1,
                }
            else:
                entry["connections"] += 1
        return list(agg.values())

    def _is_readonly(self, identity: Identity) -> bool:
        if identity.user_id in self.readonly_users:
            return True
        return self.settings.guests_readonly and identity.kind in _GUEST_KINDS

    def _compute_owner(self) -> str | None:
        candidates: dict[str, Identity] = {}
        for conn in self.conns.values():
            ident = conn.identity
            if ident.kind == Kind.GS_EPHEMERAL:
                continue
            candidates.setdefault(ident.user_id, ident)
        if not candidates:
            return None
        explicit = self.settings.explicit_owner
        if explicit is not None and explicit in candidates:
            return explicit
        if self.created_by in candidates:
            return self.created_by
        return min(candidates, key=lambda uid: self._join_order.get(uid, self._join_counter))

    def _owner_dict(self) -> dict | None:
        if self._owner is None:
            return None
        ident = self._present_identity(self._owner)
        if ident is None:
            return None
        return {"user_id": self._owner, "username": ident.username}

    def _emit_owner_changed(self, owner_uid: str) -> None:
        ident = self._present_identity(owner_uid)
        username = ident.username if ident is not None else owner_uid
        self._broadcast(
            {"type": "owner-changed"},
            user_id=owner_uid, username=username, connection_id="server",
        )

    def _recompute_owner(self) -> None:
        """Recompute the acting owner; emit ``owner-changed`` on a change to non-None."""
        new_owner = self._compute_owner()
        if new_owner != self._owner:
            self._owner = new_owner
            if new_owner is not None:
                self._emit_owner_changed(new_owner)

    # --------------------------------------------------------------------- #
    # Welcome / snapshot bodies
    # --------------------------------------------------------------------- #

    def to_welcome(self, conn: ConnLike) -> dict:
        """Build the ``welcome`` body (envelope + ``anon_token`` are added later)."""
        identity = conn.identity
        return {
            "type": "welcome",
            "protocol": protocol.PROTOCOL_VERSION,
            "dialect": self.dialect,
            "you": {
                "user_id": identity.user_id,
                "username": identity.username,
                "kind": identity.kind.value,
                "readonly": self._is_readonly(identity),
                "is_owner": identity.user_id == self._owner,
            },
            "roster": self._roster(),
            "owner": self._owner_dict(),
            "rev": self.poly.rev,
            "settings": {
                "locked": self.settings.locked,
                "guests_allowed": self.settings.guests_allowed,
                "guests_readonly": self.settings.guests_readonly,
            },
        }

    def to_snapshot(self) -> dict:
        """Build the ``session-snapshot`` body (full session state)."""
        return {
            "type": "session-snapshot",
            "state": self.state.snapshot(),
            "data": self.data.snapshot(),
            "poly": self.poly.snapshot(),
            "docs": self.docs.snapshot(),
            "chat": list(self.chat),
        }

    # --------------------------------------------------------------------- #
    # Join / leave / kick
    # --------------------------------------------------------------------- #

    def join(self, conn: ConnLike) -> None:
        """Admit a connection or raise :class:`JoinRefused`.

        Refusal order (spec §5, dialect check added by the layers-dialect design
        §3): banned (4403) → locked (4423) → guests-off for a guest kind (4403) →
        dialect mismatch (4409, the session's ``dialect`` absent from the
        connection's declared ``dialects``, which defaults to
        ``[DEFAULT_DIALECT]`` when the connection declared none) → roster full
        (4429, counting distinct users) → the per-user connection cap (4429,
        ``max_conns_per_user``) for an additional tab of an already-present user.
        A user's **first** connection is bounded only by the distinct-user
        roster; each further tab is bounded only by the per-user cap. On success
        the joiner receives ``welcome`` then ``session-snapshot``; peers receive
        ``user-joined`` + ``system-message`` on the user's first connection; the
        owner is recomputed.
        """
        identity = conn.identity
        if identity.user_id in self.bans:
            raise JoinRefused(protocol.CLOSE_FORBIDDEN, "banned")
        # A lock keeps newcomers out; it must not strand the people who hold the
        # keys. The creator and an explicitly transferred owner may always
        # re-enter (a reload or a thaw would otherwise seal the session forever,
        # since the lock is persisted and no other unlock path exists).
        if self.settings.locked and identity.user_id not in (
            self.created_by,
            self.settings.explicit_owner,
        ):
            raise JoinRefused(protocol.CLOSE_LOCKED, "locked")
        if not self.settings.guests_allowed and identity.kind in _GUEST_KINDS:
            raise JoinRefused(protocol.CLOSE_FORBIDDEN, "guests not allowed")
        declared = conn.declared_dialects or [protocol.DEFAULT_DIALECT]
        if self.dialect not in declared:
            raise JoinRefused(protocol.CLOSE_DIALECT, f"session dialect is {self.dialect!r}")
        present = self._present_user_ids()
        is_first_conn = identity.user_id not in present
        if is_first_conn:
            if len(present) >= self.limits.max_clients:
                raise JoinRefused(protocol.CLOSE_LIMIT, "roster full")
        else:
            open_tabs = sum(
                1 for c in self.conns.values() if c.identity.user_id == identity.user_id
            )
            if open_tabs >= self.limits.max_conns_per_user:
                raise JoinRefused(protocol.CLOSE_LIMIT, "too many connections")

        self.conns[conn.connection_id] = conn
        if identity.user_id not in self._join_order:
            self._join_order[identity.user_id] = self._join_counter
            self._join_counter += 1

        # Update the acting owner before the welcome so it reflects the new roster;
        # defer any owner-changed emission until after welcome + snapshot are sent.
        old_owner = self._owner
        self._owner = self._compute_owner()

        self._send(conn, self.to_welcome(conn))
        self._send(conn, self.to_snapshot())

        if is_first_conn:
            self._broadcast(
                {"type": "user-joined", "kind": identity.kind.value},
                exclude=conn.connection_id,
                user_id=identity.user_id, username=identity.username, connection_id="server",
            )
            self._broadcast(
                {"type": "system-message", "message": f"{identity.username} has joined"},
                exclude=conn.connection_id,
            )

        if self._owner != old_owner and self._owner is not None:
            self._emit_owner_changed(self._owner)

    def leave(self, connection_id: str) -> None:
        """Remove a connection; on a user's last connection emit ``user-parted`` +
        ``system-message`` and recompute the owner (clearing an explicit transfer
        that pointed at the departing user). Unknown ids are a no-op."""
        conn = self.conns.pop(connection_id, None)
        if conn is None:
            return
        for doc in self.docs._docs.values():
            doc.forget_connection(connection_id)
        identity = conn.identity
        still_present = any(c.identity.user_id == identity.user_id for c in self.conns.values())
        if still_present:
            return
        if self.settings.explicit_owner == identity.user_id:
            self.settings.explicit_owner = None
        self._broadcast(
            {"type": "user-parted"},
            user_id=identity.user_id, username=identity.username, connection_id="server",
        )
        self._broadcast({"type": "system-message", "message": f"{identity.username} has left"})
        self._recompute_owner()

    def kick_user(self, user_id: str, code: int = protocol.CLOSE_KICKED) -> int:
        """Close every connection of ``user_id`` and process each leave. Returns the
        number of connections closed. Tolerant of a user with no connections."""
        targets = [cid for cid, c in self.conns.items() if c.identity.user_id == user_id]
        for cid in targets:
            conn = self.conns.get(cid)
            if conn is not None:
                conn.close_soon(code, "kicked")
            self.leave(cid)
        return len(targets)

    # --------------------------------------------------------------------- #
    # Inbound dispatch
    # --------------------------------------------------------------------- #

    def handle(self, connection_id: str, raw_msg: dict) -> None:
        """Validate, authorize, apply, and fan out one inbound client frame.

        Unknown ``connection_id`` returns silently (a race with ``leave``). A
        :class:`ProtocolError` is answered with an ``error`` frame to the sender.
        Readonly-gated writes and owner-gated verbs short-circuit with an ``error``.
        """
        conn = self.conns.get(connection_id)
        if conn is None:
            return
        try:
            msg = protocol.validate_message(raw_msg, self.limits)
        except ProtocolError as exc:
            self.send_error(conn, exc.code, detail=exc.detail, ref_type=exc.ref_type)
            return

        mtype = msg["type"]
        identity = conn.identity
        if mtype in protocol.WRITE_TYPES and self._is_readonly(identity):
            self.send_error(conn, ErrorCode.readonly, detail="write access is read-only")
            return
        if mtype in protocol.OWNER_TYPES and identity.user_id != self._owner:
            self.send_error(conn, ErrorCode.forbidden, detail="owner only")
            return

        handler = self._dispatch.get(mtype)
        if handler is None:
            self.send_error(
                conn, ErrorCode.bad_frame, detail=f"unexpected '{mtype}'", ref_type=mtype
            )
            return
        handler(conn, msg)

    # -- state / data ------------------------------------------------------ #

    def _do_state_set(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        seq = self.seq + 1
        candidate = copy.deepcopy(self.state)
        try:
            candidate.bulk_set(msg["state"], seq, by=identity.user_id)
        except EngineLimit as exc:
            self.send_error(conn, ErrorCode.too_large, detail=exc.detail)
            return
        if not self._commit_content("state", candidate, candidate.snapshot()):
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self._state_entry_sizes = {
            item["id"]: protocol.json_size(item) for item in candidate.snapshot()
        }
        self.seq = seq
        self._broadcast(
            msg, seq, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    def _do_state_update(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        seq = self.seq + 1
        state_id = msg["id"]
        had_previous = state_id in self.state._entries
        previous = self.state._entries.get(state_id)
        try:
            self.state.apply(state_id, msg["value"], seq, by=identity.user_id)
        except EngineLimit as exc:
            self.send_error(conn, ErrorCode.too_large, detail=exc.detail)
            return
        entry_size = protocol.json_size(
            {"id": state_id, "value": msg["value"], "seq": seq, "by": identity.user_id}
        )
        previous_size = self._state_entry_sizes.get(state_id)
        if previous_size is None:
            separator = 1 if self._state_entry_sizes else 0
            state_size = self._content_component_sizes["state"] + separator + entry_size
        else:
            state_size = self._content_component_sizes["state"] - previous_size + entry_size
        if not self._commit_content_size("state", self.state, state_size):
            if had_previous:
                self.state._entries[state_id] = previous
            else:
                del self.state._entries[state_id]
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self._state_entry_sizes[state_id] = entry_size
        self.seq = seq
        self._broadcast(
            msg, seq, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    def _do_data_update(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        data_id, role, key, value = msg["id"], msg["role"], msg["key"], msg["value"]
        lane = self.data._data
        roles = lane.get(data_id)
        keys = roles.get(role) if roles is not None else None
        had_leaf = keys is not None and key in keys
        previous = keys[key] if had_leaf else None
        # Container sizes before the write decide the separator bytes below.
        n_ids, n_roles, n_keys = len(lane), len(roles or ()), len(keys or ())
        try:
            self.data.apply(data_id, role, key, value)
        except EngineLimit as exc:
            self.send_error(conn, ErrorCode.too_large, detail=exc.detail)
            return
        # Exact compact-JSON delta of data[id][role][key] = value: a changed leaf
        # swaps its value bytes; a new leaf/role/id adds its key, colon and
        # braces, plus a comma when the enclosing container was non-empty.
        leaf = protocol.json_size(key) + 1 + protocol.json_size(value)
        if had_leaf:
            delta = protocol.json_size(value) - protocol.json_size(previous)
        elif keys is not None:
            delta = leaf + (1 if n_keys else 0)
        elif roles is not None:
            delta = protocol.json_size(role) + 3 + leaf + (1 if n_roles else 0)
        else:
            delta = protocol.json_size(data_id) + 3 + protocol.json_size(role) + 3 + leaf
            delta += 1 if n_ids else 0
        data_size = self._content_component_sizes["data"] + delta
        if not self._commit_content_size("data", self.data, data_size):
            if had_leaf:
                keys[key] = previous
            elif keys is not None:
                del keys[key]
            elif roles is not None:
                del roles[role]
            else:
                del lane[data_id]
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self._broadcast(
            msg, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    def _do_clicked_button(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        self._broadcast(
            msg, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    # -- chat -------------------------------------------------------------- #

    def _do_chat_message(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        seq = self.seq + 1
        frame = self._stamp(
            {"type": "chat-message", "message": msg["message"]},
            seq,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )
        candidate = deque(self.chat, maxlen=self.limits.chat_history)
        candidate.append(frame)
        if not self._commit_content("chat", candidate, list(candidate)):
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self.seq = seq
        for c in self.conns.values():  # echo includes the sender
            c.send_json(frame)

    def _find_chat(self, message_id: str) -> dict | None:
        for frame in self.chat:
            if frame.get("message_id") == message_id:
                return frame
        return None

    def _do_chat_delete(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        target = self._find_chat(msg["message_id"])
        authorized = target is not None and (
            target.get("user_id") == identity.user_id or identity.user_id == self._owner
        )
        if not authorized:
            self.send_error(conn, ErrorCode.forbidden, detail="unknown or unauthorized message")
            return
        candidate = deque(self.chat, maxlen=self.limits.chat_history)
        candidate.remove(target)
        self._commit_content("chat", candidate, list(candidate))
        self._broadcast(
            {
                "type": "chat-deleted",
                "target_message_id": msg["message_id"],
                "by": identity.username,
            }
        )

    def _do_chat_recall(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        target = self._find_chat(msg["message_id"])
        within_window = (
            target is not None
            and self.clock() - target.get("timestamp", 0) <= self.limits.recall_window
        )
        if target is None or target.get("user_id") != identity.user_id or not within_window:
            self.send_error(conn, ErrorCode.forbidden, detail="unknown or unauthorized message")
            return
        candidate = deque(self.chat, maxlen=self.limits.chat_history)
        candidate.remove(target)
        self._commit_content("chat", candidate, list(candidate))
        self._broadcast(
            {
                "type": "chat-deleted",
                "target_message_id": msg["message_id"],
                "by": identity.username,
            },
            exclude=conn.connection_id,
        )
        self._send(
            conn,
            {"type": "chat-recalled", "target_message_id": msg["message_id"],
             "original_message": target.get("message")},
        )

    # -- control ----------------------------------------------------------- #

    def _do_ping(self, conn: ConnLike, msg: dict) -> None:
        self._send(conn, {"type": "pong"})

    def _do_session_state(self, conn: ConnLike, msg: dict) -> None:
        self._send(conn, self.to_snapshot())

    # -- polydoc ----------------------------------------------------------- #

    def _author_key(self, identity: Identity, connection_id: str) -> str:
        if identity.kind == Kind.MEMBER:
            return f"member:{identity.user_id}"
        return f"connection:{connection_id}"

    def _do_poly_snapshot(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        seq = self.seq + 1
        candidate = copy.deepcopy(self.poly)
        try:
            new_rev = candidate.apply_snapshot(msg["programText"], msg["nodes"], msg.get("frame"))
        except EngineLimit as exc:
            self.send_error(conn, ErrorCode.too_large, detail=exc.detail)
            return
        if not self._commit_content("poly", candidate, candidate.snapshot()):
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self.seq = seq
        body = dict(msg)
        body["rev"] = new_rev
        # Relay the server's canonical node set (every version == new_rev, ids
        # sorted, parentId normalized), never the owner's client-side versions:
        # a peer that trusts relayed versions would derive wrong base_revs.
        body["nodes"] = self.poly.snapshot()["nodes"]
        self._broadcast(
            body, seq, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    def _do_poly_upsert(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        author = self._author_key(identity, conn.connection_id)
        node_id = msg["id"]
        poly = self.poly
        # Applied in place; enough is captured to undo a budget refusal exactly
        # (only an author evicted from the dedup map at its cap is not restored).
        saved = (poly.nodes.get(node_id), poly.rev, poly._author_seq.get(author))
        result = poly.upsert(
            base_rev=msg["base_rev"],
            node_id=node_id,
            kind=msg["kind"],
            text=msg["text"],
            parent_id=msg.get("parentId"),
            author=author,
            author_seq=msg.get("author_seq"),
        )
        if result.status == "applied":
            node = poly.nodes[node_id]
            node_size = protocol.json_size(
                {
                    "id": node.id,
                    "kind": node.kind,
                    "text": node.text,
                    "version": node.version,
                    "parentId": node.parent_id,
                }
            )
            previous = self._poly_node_sizes.get(node_id)
            self._poly_nodes_total += node_size - (previous or 0)
            self._poly_node_sizes[node_id] = node_size
            if not self._commit_content_size("poly", poly, self._poly_lane_size()):
                self._poly_nodes_total -= node_size - (previous or 0)
                if previous is None:
                    del self._poly_node_sizes[node_id]
                else:
                    self._poly_node_sizes[node_id] = previous
                old_node, old_rev, old_author = saved
                if old_node is None:
                    poly.nodes.pop(node_id, None)
                else:
                    poly.nodes[node_id] = old_node
                poly.rev = old_rev
                if old_author is None:
                    poly._author_seq.pop(author, None)
                else:
                    poly._author_seq[author] = old_author
                self._send_poly_reject(conn, msg, "limit", poly.rev)
                return
        self._poly_result(conn, msg, result, upsert=True)

    def _do_poly_delete(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        result = self.poly.delete(
            base_rev=msg["base_rev"],
            node_id=msg["id"],
            author=self._author_key(identity, conn.connection_id),
            author_seq=msg.get("author_seq"),
        )
        if result.status == "applied":
            for entry in result.applied:
                self._poly_nodes_total -= self._poly_node_sizes.pop(entry["id"])
            # A delete never grows the lane, so it is recorded rather than gated.
            self._record_lane_size("poly", self._poly_lane_size())
        self._poly_result(conn, msg, result, upsert=False)

    def _send_poly_reject(self, conn: ConnLike, msg: dict, reason: str, rev: int) -> None:
        body = {"type": "poly-reject", "reason": reason, "id": msg["id"], "rev": rev}
        if "author_seq" in msg:
            body["author_seq"] = msg["author_seq"]
        self._send(conn, body)

    def _poly_result(self, conn: ConnLike, msg: dict, result, *, upsert: bool) -> None:
        if result.status == "duplicate":
            return  # a retransmit — total silence
        if result.status == "rejected":
            # The engine, applied in place, already kept the author-sequence
            # high-water mark for the rejected proposal.
            self._send_poly_reject(conn, msg, result.reason, result.rev)
            return

        identity = conn.identity
        ack = {"type": "poly-ack", "rev": result.rev, "applied": result.applied}
        if "author_seq" in msg:
            ack["author_seq"] = msg["author_seq"]
        self._send(conn, ack)

        decorated = dict(msg)
        decorated["rev"] = result.rev
        if upsert and result.applied:
            decorated["version"] = result.applied[0]["version"]
        self._broadcast(
            decorated, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    def _do_poly_relay(self, conn: ConnLike, msg: dict) -> None:
        identity = conn.identity
        self._broadcast(
            msg, exclude=conn.connection_id,
            user_id=identity.user_id, username=identity.username, connection_id=conn.connection_id,
        )

    # -- doc lane --------------------------------------------------------- #

    def _doc_error(self, conn: ConnLike, exc: TextDocReject) -> None:
        code = ErrorCode.too_large if exc.reason == "too_large" else ErrorCode.bad_frame
        if exc.reason == "stale":
            code = ErrorCode.stale
        self.send_error(conn, code, detail=exc.detail)

    def _doc_snapshot(self, doc_id: str) -> dict | None:
        doc = self.docs._docs.get(doc_id)
        if doc is None:
            return None
        return doc.snapshot()

    def _send_doc_reject(
        self,
        conn: ConnLike,
        *,
        doc_id: str,
        base_rev: int,
        author_seq: int | None,
        reason: str,
        snapshot: dict | None,
    ) -> None:
        self._send(
            conn,
            {
                "type": "doc-reject",
                "docId": doc_id,
                "baseRev": base_rev,
                "authorSeq": author_seq,
                "reason": reason,
                "snapshot": snapshot,
            },
        )

    def _doc_duplicate_retry(self, doc_id: str, connection_id: str, author_seq: int) -> bool:
        doc = self.docs._docs.get(doc_id)
        if doc is None:
            return False
        return (connection_id, author_seq) in doc._retry_cache

    def _serialize_edit(self, edit: TextEdit) -> dict:
        return {"start": edit.start, "end": edit.end, "text": edit.text}

    def _broadcast_doc_snapshot(self) -> None:
        self._broadcast({"type": "doc-snapshot", "docs": self.docs.snapshot()})

    def _do_doc_create(self, conn: ConnLike, msg: dict) -> None:
        candidate = copy.deepcopy(self.docs)
        try:
            candidate.create_doc(
                msg["doc"]["id"],
                msg["doc"]["title"],
                msg["doc"]["kind"],
                msg["doc"]["text"],
                msg["doc"]["default"],
            )
        except TextDocReject as exc:
            self._doc_error(conn, exc)
            return
        if not self._commit_content("docs", candidate, self._frozen_docs(candidate)):
            self.send_error(conn, ErrorCode.too_large, detail="session content budget exceeded")
            return
        self._broadcast_doc_snapshot()

    def _do_doc_reset(self, conn: ConnLike, msg: dict) -> None:
        candidate = copy.deepcopy(self.docs)
        try:
            candidate.reset_doc(msg["docId"], msg["text"], msg["baseRev"])
        except TextDocReject as exc:
            self._send_doc_reject(
                conn,
                doc_id=msg["docId"],
                base_rev=msg["baseRev"],
                author_seq=None,
                reason=exc.reason,
                snapshot=self._doc_snapshot(msg["docId"]),
            )
            return
        if not self._commit_content("docs", candidate, self._frozen_docs(candidate)):
            self._send_doc_reject(
                conn,
                doc_id=msg["docId"],
                base_rev=msg["baseRev"],
                author_seq=None,
                reason="too_large",
                snapshot=self._doc_snapshot(msg["docId"]),
            )
            return
        self._broadcast_doc_snapshot()

    def _do_doc_edit(self, conn: ConnLike, msg: dict) -> None:
        doc_id = msg["docId"]
        duplicate = self._doc_duplicate_retry(doc_id, conn.connection_id, msg["authorSeq"])
        # Applied in place; a refused edit (engine or budget) is undone from the
        # captured state so it leaves no trace, retry cache included.
        doc = self.docs._docs.get(doc_id)
        saved = None if doc is None else self._doc_state(doc)
        try:
            accepted = self.docs.apply_edit(
                doc_id,
                base_rev=msg["baseRev"],
                edit=TextEdit(**msg["edit"]),
                author_id=conn.identity.user_id,
                connection_id=conn.connection_id,
                author_seq=msg["authorSeq"],
            )
        except TextDocReject as exc:
            if doc is not None:
                self._restore_doc_state(doc, saved)
            self._send_doc_reject(
                conn,
                doc_id=doc_id,
                base_rev=msg["baseRev"],
                author_seq=msg["authorSeq"],
                reason=exc.reason,
                snapshot=self._doc_snapshot(doc_id),
            )
            return

        doc_size = self._frozen_doc_size(doc_id)
        docs_size = self._content_component_sizes["docs"] - self._doc_sizes[doc_id] + doc_size
        if not self._commit_content_size("docs", self.docs, docs_size):
            self._restore_doc_state(doc, saved)
            self._send_doc_reject(
                conn,
                doc_id=doc_id,
                base_rev=msg["baseRev"],
                author_seq=msg["authorSeq"],
                reason="too_large",
                snapshot=self._doc_snapshot(doc_id),
            )
            return
        self._doc_sizes[doc_id] = doc_size

        self._send(
            conn,
            {
                "type": "doc-ack",
                "docId": accepted.doc_id,
                "rev": accepted.rev,
                "authorSeq": accepted.author_seq,
                "edit": self._serialize_edit(accepted.edit),
            },
        )
        if duplicate:
            return
        self._broadcast(
            {
                "type": "doc-edit",
                "docId": accepted.doc_id,
                "rev": accepted.rev,
                "authorSeq": accepted.author_seq,
                "edit": self._serialize_edit(accepted.edit),
            },
            exclude=conn.connection_id,
            user_id=conn.identity.user_id,
            username=conn.identity.username,
            connection_id=conn.connection_id,
        )

    def _do_doc_cursor(self, conn: ConnLike, msg: dict) -> None:
        body = {
            "type": "doc-cursor",
            "docId": msg["docId"],
            "user": conn.identity.user_id,
            "connectionId": conn.connection_id,
            "range": msg["range"],
            "direction": msg.get("direction", "forward"),
        }
        self._broadcast(
            body,
            exclude=conn.connection_id,
            user_id=conn.identity.user_id,
            username=conn.identity.username,
            connection_id=conn.connection_id,
        )

    # -- moderation -------------------------------------------------------- #

    def _do_mod(self, conn: ConnLike, msg: dict) -> None:
        handle_mod(self, conn, msg)

    def record_audit(self, event: AuditEvent) -> None:
        """Forward an audit event to the installed ``audit_cb`` (a no-op if unset)."""
        if self.audit_cb is not None:
            self.audit_cb(event)

    def fire_ban_sink(self, user_id: str, by: str, banned: bool) -> None:
        """Notify the hub's ``ban_sink`` of a ban/unban (a no-op if unset)."""
        if self.ban_sink is not None:
            self.ban_sink(self.session_id, user_id, by, banned)

    # --------------------------------------------------------------------- #
    # Persistence
    # --------------------------------------------------------------------- #

    def freeze_snapshot(self) -> dict:
        """Serialize the whole session to the store payload (the hub sets ``frozen_at``).

        Bans live in a separate store table and travel via ``ban_sink``, so they are
        deliberately absent here; ``readonly_users`` is nested inside ``settings``.
        """
        return {
            "created_by": self.created_by,
            "dialect": self.dialect,
            "created_at": self.created_at,
            "settings": {
                "locked": self.settings.locked,
                "guests_allowed": self.settings.guests_allowed,
                "guests_readonly": self.settings.guests_readonly,
                "explicit_owner": self.settings.explicit_owner,
                "readonly_users": sorted(self.readonly_users),
            },
            "state": self.state.snapshot(),
            "data": self.data.snapshot(),
            "poly": self.poly.snapshot(),
            "docs": self._frozen_docs(),
            "chat": list(self.chat),
            "rev": self.poly.rev,
            "seq": self.seq,
            "frozen_at": None,
            "last_active": int(self.clock()),
        }

    @classmethod
    def thaw(
        cls,
        session_id: str,
        payload: dict,
        limits: Limits,
        clock: Callable[[], float],
        audit_cb: Callable[[AuditEvent], None] | None = None,
        bans: set[str] | None = None,
        ban_sink: Callable[[str, str, str, bool], None] | None = None,
    ) -> Session:
        """Rebuild a Session from a store ``payload``.

        ``session_id`` and ``bans`` are supplied by the hub (both live outside the
        session JSON payload — the id is the store primary key, bans are a separate
        table). The first-join counters reset: a thaw begins a fresh epoch, so the
        first user to re-join becomes owner (spec §5 rule 3) unless a persisted
        explicit transfer still names a re-joining participant.
        """
        raw_settings = payload["settings"]
        settings = Settings(
            locked=raw_settings["locked"],
            guests_allowed=raw_settings["guests_allowed"],
            guests_readonly=raw_settings["guests_readonly"],
            explicit_owner=raw_settings.get("explicit_owner"),
        )
        session = cls(
            session_id=session_id,
            created_by=payload["created_by"],
            limits=limits,
            clock=clock,
            settings=settings,
            bans=bans,
            audit_cb=audit_cb,
            ban_sink=ban_sink,
            dialect=payload.get("dialect", protocol.DEFAULT_DIALECT),
        )
        session.created_at = payload["created_at"]
        session.readonly_users = set(raw_settings.get("readonly_users", []))
        session.state.load(payload["state"])
        session.data.load(payload["data"])
        session.poly.load(payload["poly"])
        for frozen in payload.get("docs", []):
            session.docs.create_doc(
                frozen["id"],
                frozen["title"],
                frozen["kind"],
                frozen["text"],
                frozen.get("default", False),
            )
            doc = session.docs._docs[frozen["id"]]
            doc._rev = frozen["rev"]
            # The store round-trips through json, which recombines escaped
            # surrogate pairs into code points; re-split into UTF-16 units.
            doc._text = to_utf16_units(frozen["text"])
            doc._oplog = []
            doc._oplog_bytes = 0
            for entry in frozen.get("oplog", []):
                stored_edit = entry["edit"]
                oplog_entry = _OpLogEntry(
                    rev=entry["rev"],
                    edit=TextEdit(
                        stored_edit["start"],
                        stored_edit["end"],
                        to_utf16_units(stored_edit["text"]),
                    ),
                    prior_text=to_utf16_units(entry["prior_text"]),
                    size=protocol.json_size(
                        {
                            "rev": entry["rev"],
                            "edit": entry["edit"],
                            "prior": entry["prior_text"],
                        }
                    ),
                )
                doc._oplog.append(oplog_entry)
                doc._oplog_bytes += oplog_entry.size
        for frame in payload["chat"]:
            session.chat.append(frame)
        session.seq = payload["seq"]
        session.recalculate_content_budget()
        if not session.content_within_budget:
            raise EngineLimit("stored session content budget exceeded")
        return session
