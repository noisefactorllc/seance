"""Wire protocol: the single source of truth for seance's JSON message contract.

Every other module dispatches through this module. It owns:

* the canonical close codes and :class:`ErrorCode` enum,
* the client/server type sets and the lane / authorization sets,
* frame parsing (:func:`parse_frame`) and strict per-type validation
  (:func:`validate_message`), which normalizes each accepted frame to exactly
  its known keys,
* envelope stamping (:func:`stamp`) and error-frame construction
  (:func:`error_frame`).

No I/O, no config import: :func:`validate_message` reads limits off a duck-typed
``limits`` object (see :class:`Limits`) so this module stays decoupled from
``app.config``.

Conventions:

* String fields are capped by character length (the ``str<=N`` table notation).
* ``value`` fields are capped by :func:`json_size` bytes against
  ``limits.max_value_bytes``.
* ``protocol`` integers reject ``bool`` (a Python ``int`` subclass).
"""

import json
import re
import uuid
from enum import StrEnum
from typing import Protocol

PROTOCOL_VERSION = 1

# --- Session dialect (layers-dialect-design §3) ------------------------------ #
DEFAULT_DIALECT = "noisemaker-dsl"
DIALECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_HELLO_DIALECTS = 16

# --- Close codes (WebSocket application close codes) ------------------------ #
CLOSE_PROTOCOL = 4400
CLOSE_KICKED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_SLOW = 4408
CLOSE_DIALECT = 4409
CLOSE_LOCKED = 4423
CLOSE_LIMIT = 4429


class ErrorCode(StrEnum):
    """Stable ``error`` frame codes. ``str(code)`` yields the wire string."""

    bad_frame = "bad_frame"
    unauthorized = "unauthorized"
    forbidden = "forbidden"
    readonly = "readonly"
    rate_limited = "rate_limited"
    too_large = "too_large"
    stale = "stale"
    unknown_session = "unknown_session"
    dialect_mismatch = "dialect_mismatch"
    internal = "internal"


class ProtocolError(Exception):
    """A rejected frame. ``str(err)`` is the human-readable ``detail``."""

    def __init__(self, code: ErrorCode, detail: str = "", ref_type: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.ref_type = ref_type

    def __str__(self) -> str:
        return self.detail


class Limits(Protocol):
    """Structural view of the config ``Limits`` — only the fields read here.

    Any object exposing these int attributes satisfies validation; this avoids
    importing ``app.config`` (built in parallel).
    """

    max_frame: int
    max_value_bytes: int
    max_state_id_len: int
    max_chat_len: int
    max_node_text: int
    max_program_text: int
    max_doc_id_len: int
    max_doc_title_len: int
    max_doc_text: int
    max_doc_edit_text: int


# Fields the server stamps on outbound frames; never accepted from a client.
ENVELOPE_KEYS = frozenset(
    {"seq", "session", "user_id", "username", "connection_id", "message_id", "timestamp"}
)

# Protocol-literal field caps (not config-tunable; per the message table).
# Integers above 2**53-1 are not representable by JSON.parse in browsers; the
# server echoes client integers (base_rev, author_seq) back, so refuse them.
MAX_SAFE_INT = 2**53 - 1
# Nesting deeper than this survives json.loads but not copy.deepcopy (which the
# session uses for transactional lane commits); refuse it at the frame edge.
MAX_FRAME_DEPTH = 64
_NODE_ID_MAX = 200
_KIND_MAX = 32
_ROLE_MAX = 64
_MESSAGE_ID_MAX = 64
_MOD_TARGET_MAX = 200
_PARAM_MAX = 128
_HASH_MAX = 128


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #


def new_id() -> str:
    """Return a fresh uuid4 hex-with-dashes string (36 chars)."""
    return str(uuid.uuid4())


def json_size(value) -> int:
    """Byte length of ``value`` serialized as compact JSON."""
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Field extraction helpers (each raises ProtocolError on violation)
# --------------------------------------------------------------------------- #


def _str_field(msg: dict, field: str, *, max_len: int | None, required: bool) -> str | None:
    if field not in msg:
        if required:
            raise ProtocolError(ErrorCode.bad_frame, f"{field} is required", ref_type=field)
        return None
    value = msg[field]
    if not isinstance(value, str):
        raise ProtocolError(ErrorCode.bad_frame, f"{field} must be a string", ref_type=field)
    if max_len is not None and len(value) > max_len:
        raise ProtocolError(ErrorCode.too_large, f"{field} exceeds maximum length", ref_type=field)
    return value


def _int_field(msg: dict, field: str, *, minimum: int | None, required: bool) -> int | None:
    if field not in msg:
        if required:
            raise ProtocolError(ErrorCode.bad_frame, f"{field} is required", ref_type=field)
        return None
    value = msg[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(ErrorCode.bad_frame, f"{field} must be an integer", ref_type=field)
    if minimum is not None and value < minimum:
        raise ProtocolError(ErrorCode.bad_frame, f"{field} is out of range", ref_type=field)
    if value > MAX_SAFE_INT or value < -MAX_SAFE_INT:
        raise ProtocolError(ErrorCode.bad_frame, f"{field} is out of range", ref_type=field)
    return value


def _bool_field(msg: dict, field: str) -> bool:
    if field not in msg:
        raise ProtocolError(ErrorCode.bad_frame, f"{field} is required", ref_type=field)
    value = msg[field]
    if not isinstance(value, bool):
        raise ProtocolError(ErrorCode.bad_frame, f"{field} must be a boolean", ref_type=field)
    return value


def _value_field(msg: dict, field: str, limits: Limits):
    if field not in msg:
        raise ProtocolError(ErrorCode.bad_frame, f"{field} is required", ref_type=field)
    value = msg[field]
    if json_size(value) > limits.max_value_bytes:
        raise ProtocolError(ErrorCode.too_large, f"{field} exceeds maximum size", ref_type=field)
    return value


def _parent_id(source: dict) -> str | None:
    """Normalize an optional ``parentId``: absent or null -> ``None``; else str."""
    if "parentId" not in source:
        return None
    value = source["parentId"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(
            ErrorCode.bad_frame, "parentId must be a string or null", ref_type="parentId"
        )
    return value


def _hello_dialects(msg: dict) -> list[str] | None:
    """Normalize the optional ``hello`` ``dialects`` list, or ``None`` if absent.

    A present value must be a non-empty list of at most :data:`MAX_HELLO_DIALECTS`
    strings, each matching :data:`DIALECT_RE`; any violation is ``bad_frame``.
    """
    if "dialects" not in msg:
        return None
    value = msg["dialects"]
    if not isinstance(value, list):
        raise ProtocolError(ErrorCode.bad_frame, "dialects must be a list", ref_type="dialects")
    if not value:
        raise ProtocolError(
            ErrorCode.bad_frame, "dialects must be non-empty", ref_type="dialects"
        )
    if len(value) > MAX_HELLO_DIALECTS:
        raise ProtocolError(
            ErrorCode.bad_frame, "dialects exceeds maximum entries", ref_type="dialects"
        )
    out = []
    for entry in value:
        if not isinstance(entry, str) or not DIALECT_RE.match(entry):
            raise ProtocolError(
                ErrorCode.bad_frame, "dialects entry is malformed", ref_type="dialects"
            )
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Per-type validators. Each returns a NEW dict of exactly the known keys.
# Signature is uniform (msg, limits) for dispatch through _VALIDATORS.
# --------------------------------------------------------------------------- #


def _v_hello(msg: dict, limits: Limits) -> dict:
    protocol = _int_field(msg, "protocol", minimum=None, required=True)
    if protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.bad_frame, "unsupported protocol version", ref_type="protocol"
        )
    out: dict = {"type": "hello", "protocol": protocol}
    ticket = _str_field(msg, "ticket", max_len=None, required=False)
    if ticket is not None:
        out["ticket"] = ticket
    anon_token = _str_field(msg, "anon_token", max_len=None, required=False)
    if anon_token is not None:
        out["anon_token"] = anon_token
    if "resume" in msg:
        resume = msg["resume"]
        if not isinstance(resume, dict):
            raise ProtocolError(ErrorCode.bad_frame, "resume must be an object", ref_type="resume")
        out["resume"] = {"last_seq": _int_field(resume, "last_seq", minimum=0, required=True)}
    dialects = _hello_dialects(msg)
    if dialects is not None:
        out["dialects"] = dialects
    return out


def _v_state_set(msg: dict, limits: Limits) -> dict:
    state = msg.get("state")
    if not isinstance(state, list):
        raise ProtocolError(ErrorCode.bad_frame, "state must be a list", ref_type="state")
    items = []
    for entry in state:
        if not isinstance(entry, dict):
            raise ProtocolError(
                ErrorCode.bad_frame, "state entry must be an object", ref_type="state"
            )
        items.append(
            {
                "id": _str_field(entry, "id", max_len=limits.max_state_id_len, required=True),
                "value": _value_field(entry, "value", limits),
            }
        )
    return {"type": "state-set", "state": items}


def _v_state_update(msg: dict, limits: Limits) -> dict:
    return {
        "type": "state-update",
        "id": _str_field(msg, "id", max_len=limits.max_state_id_len, required=True),
        "value": _value_field(msg, "value", limits),
    }


def _v_data_update(msg: dict, limits: Limits) -> dict:
    return {
        "type": "data-update",
        "id": _str_field(msg, "id", max_len=limits.max_state_id_len, required=True),
        "role": _str_field(msg, "role", max_len=_ROLE_MAX, required=True),
        "key": _str_field(msg, "key", max_len=limits.max_state_id_len, required=True),
        "value": _value_field(msg, "value", limits),
    }


def _v_clicked_button(msg: dict, limits: Limits) -> dict:
    # Opaque relay: keep the payload verbatim, dropping only envelope keys.
    payload = {k: v for k, v in msg.items() if k not in ENVELOPE_KEYS}
    if json_size(payload) > limits.max_frame:
        raise ProtocolError(
            ErrorCode.too_large,
            "clicked-button exceeds maximum frame size",
            ref_type="clicked-button",
        )
    return payload


def _v_chat_message(msg: dict, limits: Limits) -> dict:
    return {
        "type": "chat-message",
        "message": _str_field(msg, "message", max_len=limits.max_chat_len, required=True),
    }


def _v_chat_delete(msg: dict, limits: Limits) -> dict:
    return {
        "type": "chat-delete",
        "message_id": _str_field(msg, "message_id", max_len=_MESSAGE_ID_MAX, required=True),
    }


def _v_chat_recall(msg: dict, limits: Limits) -> dict:
    return {
        "type": "chat-recall",
        "message_id": _str_field(msg, "message_id", max_len=_MESSAGE_ID_MAX, required=True),
    }


def _v_ping(msg: dict, limits: Limits) -> dict:
    return {"type": "ping"}


def _v_session_state(msg: dict, limits: Limits) -> dict:
    return {"type": "session-state"}


def _validate_node(node, limits: Limits) -> dict:
    if not isinstance(node, dict):
        raise ProtocolError(ErrorCode.bad_frame, "node must be an object", ref_type="nodes")
    out: dict = {
        "id": _str_field(node, "id", max_len=_NODE_ID_MAX, required=True),
        "kind": _str_field(node, "kind", max_len=_KIND_MAX, required=True),
        "text": _str_field(node, "text", max_len=limits.max_node_text, required=True),
        "parentId": _parent_id(node),
    }
    version = _int_field(node, "version", minimum=0, required=False)
    if version is not None:
        out["version"] = version
    return out


def _v_poly_snapshot(msg: dict, limits: Limits) -> dict:
    program_text = _str_field(msg, "programText", max_len=limits.max_program_text, required=True)
    nodes = msg.get("nodes")
    if not isinstance(nodes, list):
        raise ProtocolError(ErrorCode.bad_frame, "nodes must be a list", ref_type="nodes")
    out: dict = {
        "type": "poly-snapshot",
        "programText": program_text,
        "nodes": [_validate_node(node, limits) for node in nodes],
    }
    if "frame" in msg:
        out["frame"] = msg["frame"]
    return out


def _v_poly_token_upsert(msg: dict, limits: Limits) -> dict:
    out: dict = {
        "type": "poly-token-upsert",
        "base_rev": _int_field(msg, "base_rev", minimum=0, required=True),
        "id": _str_field(msg, "id", max_len=_NODE_ID_MAX, required=True),
        "kind": _str_field(msg, "kind", max_len=_KIND_MAX, required=True),
        "text": _str_field(msg, "text", max_len=limits.max_node_text, required=True),
        "parentId": _parent_id(msg),
    }
    hash_value = _str_field(msg, "hash", max_len=_HASH_MAX, required=False)
    if hash_value is not None:
        out["hash"] = hash_value
    author_seq = _int_field(msg, "author_seq", minimum=0, required=False)
    if author_seq is not None:
        out["author_seq"] = author_seq
    return out


def _v_poly_token_delete(msg: dict, limits: Limits) -> dict:
    out: dict = {
        "type": "poly-token-delete",
        "base_rev": _int_field(msg, "base_rev", minimum=0, required=True),
        "id": _str_field(msg, "id", max_len=_NODE_ID_MAX, required=True),
    }
    author_seq = _int_field(msg, "author_seq", minimum=0, required=False)
    if author_seq is not None:
        out["author_seq"] = author_seq
    return out


def _v_poly_cursor(msg: dict, limits: Limits) -> dict:
    mode = _str_field(msg, "mode", max_len=None, required=True)
    if mode == "text":
        rng = msg.get("range")
        if not isinstance(rng, dict):
            raise ProtocolError(ErrorCode.bad_frame, "range must be an object", ref_type="range")
        start = _int_field(rng, "start", minimum=0, required=True)
        end = _int_field(rng, "end", minimum=0, required=True)
        if end < start:
            raise ProtocolError(
                ErrorCode.bad_frame, "range end must be >= start", ref_type="range"
            )
        return {"type": "poly-cursor", "mode": "text", "range": {"start": start, "end": end}}
    if mode == "node":
        out: dict = {
            "type": "poly-cursor",
            "mode": "node",
            "node_id": _str_field(msg, "node_id", max_len=_NODE_ID_MAX, required=True),
        }
        param = _str_field(msg, "param", max_len=_PARAM_MAX, required=False)
        if param is not None:
            out["param"] = param
        return out
    raise ProtocolError(ErrorCode.bad_frame, "mode must be 'text' or 'node'", ref_type="mode")


def _v_poly_lock(msg: dict, limits: Limits) -> dict:
    return {
        "type": "poly-lock",
        "node_id": _str_field(msg, "node_id", max_len=_NODE_ID_MAX, required=True),
        "locked": _bool_field(msg, "locked"),
    }


def _v_mod_kick(msg: dict, limits: Limits) -> dict:
    target_user = _str_field(msg, "target_user", max_len=_MOD_TARGET_MAX, required=False)
    target_connection = _str_field(
        msg, "target_connection", max_len=_MOD_TARGET_MAX, required=False
    )
    if target_user is None and target_connection is None:
        raise ProtocolError(
            ErrorCode.bad_frame,
            "mod-kick requires target_user or target_connection",
            ref_type="target_user",
        )
    out: dict = {"type": "mod-kick"}
    if target_user is not None:
        out["target_user"] = target_user
    if target_connection is not None:
        out["target_connection"] = target_connection
    return out


def _v_mod_ban(msg: dict, limits: Limits) -> dict:
    return {
        "type": "mod-ban",
        "target_user": _str_field(msg, "target_user", max_len=_MOD_TARGET_MAX, required=True),
    }


def _v_mod_unban(msg: dict, limits: Limits) -> dict:
    return {
        "type": "mod-unban",
        "target_user": _str_field(msg, "target_user", max_len=_MOD_TARGET_MAX, required=True),
    }


def _v_mod_lock(msg: dict, limits: Limits) -> dict:
    return {"type": "mod-lock", "locked": _bool_field(msg, "locked")}


def _v_mod_guests(msg: dict, limits: Limits) -> dict:
    return {"type": "mod-guests", "allowed": _bool_field(msg, "allowed")}


def _v_mod_readonly(msg: dict, limits: Limits) -> dict:
    return {
        "type": "mod-readonly",
        "target_user": _str_field(msg, "target_user", max_len=_MOD_TARGET_MAX, required=True),
        "readonly": _bool_field(msg, "readonly"),
    }


def _v_mod_transfer(msg: dict, limits: Limits) -> dict:
    return {
        "type": "mod-transfer",
        "target_user": _str_field(msg, "target_user", max_len=_MOD_TARGET_MAX, required=True),
    }


def _validate_doc(msg: dict, limits: Limits) -> dict:
    doc = msg.get("doc")
    if not isinstance(doc, dict):
        raise ProtocolError(ErrorCode.bad_frame, "doc must be an object", ref_type="doc")
    return {
        "id": _str_field(doc, "id", max_len=limits.max_doc_id_len, required=True),
        "title": _str_field(doc, "title", max_len=limits.max_doc_title_len, required=True),
        "kind": _str_field(doc, "kind", max_len=None, required=True),
        "text": _str_field(doc, "text", max_len=limits.max_doc_text, required=True),
        "default": _bool_field(doc, "default"),
    }


def _validate_text_edit(msg: dict, limits: Limits) -> dict:
    edit = msg.get("edit")
    if not isinstance(edit, dict):
        raise ProtocolError(ErrorCode.bad_frame, "edit must be an object", ref_type="edit")
    start = _int_field(edit, "start", minimum=0, required=True)
    end = _int_field(edit, "end", minimum=0, required=True)
    if end < start:
        raise ProtocolError(ErrorCode.bad_frame, "edit end must be >= start", ref_type="edit")
    return {
        "start": start,
        "end": end,
        "text": _str_field(edit, "text", max_len=limits.max_doc_edit_text, required=True),
    }


def _validate_text_range(msg: dict) -> dict:
    rng = msg.get("range")
    if not isinstance(rng, dict):
        raise ProtocolError(ErrorCode.bad_frame, "range must be an object", ref_type="range")
    start = _int_field(rng, "start", minimum=0, required=True)
    end = _int_field(rng, "end", minimum=0, required=True)
    if end < start:
        raise ProtocolError(ErrorCode.bad_frame, "range end must be >= start", ref_type="range")
    return {"start": start, "end": end}


def _v_doc_create(msg: dict, limits: Limits) -> dict:
    return {"type": "doc-create", "doc": _validate_doc(msg, limits)}


def _v_doc_edit(msg: dict, limits: Limits) -> dict:
    return {
        "type": "doc-edit",
        "docId": _str_field(msg, "docId", max_len=limits.max_doc_id_len, required=True),
        "baseRev": _int_field(msg, "baseRev", minimum=0, required=True),
        "authorSeq": _int_field(msg, "authorSeq", minimum=0, required=True),
        "edit": _validate_text_edit(msg, limits),
    }


def _v_doc_cursor(msg: dict, limits: Limits) -> dict:
    out: dict = {
        "type": "doc-cursor",
        "docId": _str_field(msg, "docId", max_len=limits.max_doc_id_len, required=True),
        "range": _validate_text_range(msg),
    }
    direction = _str_field(msg, "direction", max_len=None, required=False)
    if direction is not None:
        out["direction"] = direction
    return out


def _v_doc_reset(msg: dict, limits: Limits) -> dict:
    return {
        "type": "doc-reset",
        "docId": _str_field(msg, "docId", max_len=limits.max_doc_id_len, required=True),
        "text": _str_field(msg, "text", max_len=limits.max_doc_text, required=True),
        "baseRev": _int_field(msg, "baseRev", minimum=0, required=True),
    }


_VALIDATORS = {
    "hello": _v_hello,
    "state-set": _v_state_set,
    "state-update": _v_state_update,
    "data-update": _v_data_update,
    "clicked-button": _v_clicked_button,
    "chat-message": _v_chat_message,
    "chat-delete": _v_chat_delete,
    "chat-recall": _v_chat_recall,
    "ping": _v_ping,
    "session-state": _v_session_state,
    "poly-snapshot": _v_poly_snapshot,
    "poly-token-upsert": _v_poly_token_upsert,
    "poly-token-delete": _v_poly_token_delete,
    "poly-cursor": _v_poly_cursor,
    "poly-lock": _v_poly_lock,
    "mod-kick": _v_mod_kick,
    "mod-ban": _v_mod_ban,
    "mod-unban": _v_mod_unban,
    "mod-lock": _v_mod_lock,
    "mod-guests": _v_mod_guests,
    "mod-readonly": _v_mod_readonly,
    "mod-transfer": _v_mod_transfer,
    "doc-create": _v_doc_create,
    "doc-edit": _v_doc_edit,
    "doc-cursor": _v_doc_cursor,
    "doc-reset": _v_doc_reset,
}


# --------------------------------------------------------------------------- #
# Type / lane / authorization sets (exactly the message table)
# --------------------------------------------------------------------------- #

CLIENT_TYPES = frozenset(_VALIDATORS)

SERVER_TYPES = frozenset(
    {
        "welcome",
        "session-snapshot",
        "doc-snapshot",
        "doc-ack",
        "doc-reject",
        "chat-deleted",
        "chat-recalled",
        "poly-ack",
        "poly-reject",
        "user-joined",
        "user-parted",
        "owner-changed",
        "moderation",
        "system-message",
        "pong",
        "error",
    }
)

# The readonly gate (``session.py`` ``handle``). Read-only means "may not add
# persisted content": chat belongs here because it is kept in the session's chat
# history and charged to the content budget. Cursors are deliberately absent —
# they are ephemeral presence, never persisted, and pointing at code is exactly
# what a read-only participant is there to do. ``chat-delete`` / ``chat-recall``
# are absent too: they only remove the sender's own message.
WRITE_TYPES = frozenset(
    {
        "state-update",
        "data-update",
        "clicked-button",
        "chat-message",
        "doc-edit",
        "poly-token-upsert",
        "poly-token-delete",
        "poly-lock",
    }
)

MOD_TYPES = frozenset(
    {
        "mod-kick",
        "mod-ban",
        "mod-unban",
        "mod-lock",
        "mod-guests",
        "mod-readonly",
        "mod-transfer",
    }
)

OWNER_TYPES = MOD_TYPES | frozenset({"state-set", "poly-snapshot", "doc-create", "doc-reset"})

FAST_LANE = frozenset({"state-update", "data-update", "poly-cursor", "doc-cursor"})
PROPOSAL_LANE = frozenset({"poly-token-upsert", "poly-token-delete", "doc-edit"})
CHAT_LANE = frozenset({"chat-message"})
CONTROL_LANE = frozenset(
    {
        "clicked-button",
        "chat-delete",
        "chat-recall",
        "ping",
        "session-state",
        "poly-lock",
        "mod-kick",
        "mod-ban",
        "mod-unban",
        "mod-lock",
        "mod-guests",
        "mod-readonly",
        "mod-transfer",
    }
)
SNAPSHOT_LANE = frozenset({"state-set", "poly-snapshot", "doc-create", "doc-reset"})


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def _reject_constant(name: str):
    # Python's json accepts the non-standard NaN / Infinity / -Infinity literals
    # and would re-emit them on fan-out and persistence, where JSON.parse in every
    # browser client throws. Treat them as malformed JSON.
    raise ValueError(f"non-finite JSON constant {name}")


def _nesting_depth_exceeds(obj, limit: int) -> bool:
    """Iteratively check container nesting depth (no recursion, no stack risk)."""
    stack = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return False


def parse_frame(raw: str, *, max_len: int) -> dict:
    """Parse one text frame into a JSON object.

    Raises :class:`ProtocolError` with ``too_large`` when the UTF-8 byte length
    exceeds ``max_len`` (checked before parsing), or ``bad_frame`` when the
    payload is not valid JSON (including the non-standard ``NaN`` / ``Infinity``
    literals), is not a JSON object, or nests deeper than
    :data:`MAX_FRAME_DEPTH`.
    """
    if len(raw.encode("utf-8")) > max_len:
        raise ProtocolError(ErrorCode.too_large, "frame exceeds maximum size")
    try:
        obj = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(ErrorCode.bad_frame, "frame is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.bad_frame, "frame must be a JSON object")
    if _nesting_depth_exceeds(obj, MAX_FRAME_DEPTH):
        raise ProtocolError(ErrorCode.bad_frame, "frame nests too deeply")
    return obj


def validate_message(msg: dict, limits: Limits) -> dict:
    """Validate and normalize an inbound client frame.

    Dispatches on ``type`` to the per-type validator and returns a new dict
    holding only that type's known keys (unknown and envelope keys dropped).
    Raises :class:`ProtocolError` (``bad_frame`` / ``too_large``) on any
    violation; inbound server-only types are rejected as ``bad_frame``.
    """
    if not isinstance(msg, dict):
        raise ProtocolError(ErrorCode.bad_frame, "frame must be a JSON object")
    # parse_frame already enforces this for WebSocket frames; repeating it here
    # covers every other producer of a message dict (tests, future transports).
    if _nesting_depth_exceeds(msg, MAX_FRAME_DEPTH):
        raise ProtocolError(ErrorCode.bad_frame, "frame nests too deeply")
    msg_type = msg.get("type")
    if not isinstance(msg_type, str):
        raise ProtocolError(ErrorCode.bad_frame, "missing or invalid type", ref_type="type")
    if msg_type in SERVER_TYPES:
        raise ProtocolError(
            ErrorCode.bad_frame, f"'{msg_type}' is a server-only type", ref_type=msg_type
        )
    validator = _VALIDATORS.get(msg_type)
    if validator is None:
        raise ProtocolError(
            ErrorCode.bad_frame, f"unknown message type '{msg_type}'", ref_type=msg_type
        )
    return validator(msg, limits)


def stamp(
    msg: dict,
    *,
    seq: int,
    session: str,
    user_id: str,
    username: str,
    connection_id: str,
    now: float,
) -> dict:
    """Return a copy of ``msg`` with the server envelope applied.

    Overwrites the seven reserved envelope fields (minting a fresh
    ``message_id`` and an integer ``timestamp``) and preserves all other keys.
    The input mapping is not mutated.
    """
    stamped = dict(msg)
    stamped["seq"] = seq
    stamped["session"] = session
    stamped["user_id"] = user_id
    stamped["username"] = username
    stamped["connection_id"] = connection_id
    stamped["message_id"] = new_id()
    stamped["timestamp"] = int(now)
    return stamped


def error_frame(
    code: ErrorCode,
    *,
    detail: str = "",
    ref_type: str | None = None,
    retry_after: float | None = None,
) -> dict:
    """Build an ``error`` frame; optional fields appear only when provided."""
    frame: dict = {"type": "error", "code": str(code)}
    if detail:
        frame["detail"] = detail
    if ref_type is not None:
        frame["ref_type"] = ref_type
    if retry_after is not None:
        frame["retry_after"] = round(retry_after, 2)
    return frame
