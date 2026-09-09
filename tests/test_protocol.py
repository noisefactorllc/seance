"""Tests for app.protocol — the wire-protocol single source of truth.

Covers: frame parsing, per-type message validation and normalization, envelope
stamping, error frames, close/error code constants, and the lane/authz sets.
"""

from dataclasses import dataclass

import pytest

from app.protocol import (
    CHAT_LANE,
    CLIENT_TYPES,
    CLOSE_DIALECT,
    CLOSE_FORBIDDEN,
    CLOSE_KICKED,
    CLOSE_LIMIT,
    CLOSE_LOCKED,
    CLOSE_NOT_FOUND,
    CLOSE_PROTOCOL,
    CLOSE_SLOW,
    CONTROL_LANE,
    FAST_LANE,
    MAX_FRAME_DEPTH,
    MAX_SAFE_INT,
    MOD_TYPES,
    OWNER_TYPES,
    PROPOSAL_LANE,
    SERVER_TYPES,
    SNAPSHOT_LANE,
    WRITE_TYPES,
    ErrorCode,
    ProtocolError,
    error_frame,
    json_size,
    new_id,
    parse_frame,
    stamp,
    validate_message,
)

ENVELOPE_FIELDS = {
    "seq",
    "session",
    "user_id",
    "username",
    "connection_id",
    "message_id",
    "timestamp",
}
# The envelope's stamped `message_id` shares its name with the legitimate
# `message_id` client field on chat-delete / chat-recall (the target chat id),
# so it is excluded from the "no envelope key leaks through" invariant below.
NON_SCHEMA_ENVELOPE_FIELDS = ENVELOPE_FIELDS - {"message_id"}


@dataclass
class FakeLimits:
    """Duck-typed stand-in for app.config.Limits (built in parallel; not imported)."""

    max_frame: int = 65536
    max_snapshot_frame: int = 1_048_576
    max_value_bytes: int = 8192
    max_state_id_len: int = 128
    max_chat_len: int = 2000
    max_node_text: int = 65536
    max_program_text: int = 262144
    max_doc_id_len: int = 128
    max_doc_title_len: int = 128
    max_doc_text: int = 262144
    max_doc_edit_text: int = 65536


@pytest.fixture
def limits() -> FakeLimits:
    return FakeLimits()


# One literal happy-path frame per canonical client type (field shapes per the table).
CANONICAL_FRAMES: dict[str, dict] = {
    "hello": {"type": "hello", "protocol": 1},
    "state-set": {
        "type": "state-set",
        "state": [{"id": "step_3.hueRange", "value": [0, 1]}],
    },
    "state-update": {"type": "state-update", "id": "step_3.hueRange", "value": 0.5},
    "data-update": {
        "type": "data-update",
        "id": "n1",
        "role": "meta",
        "key": "color",
        "value": "red",
    },
    "clicked-button": {"type": "clicked-button", "action": "reset", "payload": {"x": 1}},
    "chat-message": {"type": "chat-message", "message": "hello world"},
    "chat-delete": {"type": "chat-delete", "message_id": "m1"},
    "chat-recall": {"type": "chat-recall", "message_id": "m1"},
    "ping": {"type": "ping"},
    "session-state": {"type": "session-state"},
    "poly-snapshot": {
        "type": "poly-snapshot",
        "programText": "osc()",
        "nodes": [{"id": "root.0", "kind": "call", "text": "osc()", "parentId": "root"}],
        "frame": {"w": 100, "h": 50},
    },
    "poly-token-upsert": {
        "type": "poly-token-upsert",
        "base_rev": 3,
        "id": "root.0",
        "kind": "call",
        "text": "osc()",
        "parentId": "root",
        "hash": "abc123",
        "author_seq": 7,
    },
    "poly-token-delete": {
        "type": "poly-token-delete",
        "base_rev": 3,
        "id": "root.0",
        "author_seq": 8,
    },
    "poly-cursor": {"type": "poly-cursor", "mode": "text", "range": {"start": 0, "end": 5}},
    "poly-lock": {"type": "poly-lock", "node_id": "root.0", "locked": True},
    "doc-create": {
        "type": "doc-create",
        "doc": {
            "id": "main",
            "title": "Program",
            "kind": "dsl",
            "text": "osc()",
            "default": True,
        },
    },
    "doc-edit": {
        "type": "doc-edit",
        "docId": "main",
        "baseRev": 3,
        "authorSeq": 7,
        "edit": {"start": 0, "end": 5, "text": "noise()"},
    },
    "doc-cursor": {
        "type": "doc-cursor",
        "docId": "main",
        "range": {"start": 0, "end": 5},
        "direction": "forward",
    },
    "doc-reset": {"type": "doc-reset", "docId": "main", "text": "osc()", "baseRev": 3},
    "mod-kick": {"type": "mod-kick", "target_user": "u1"},
    "mod-ban": {"type": "mod-ban", "target_user": "u1"},
    "mod-unban": {"type": "mod-unban", "target_user": "u1"},
    "mod-lock": {"type": "mod-lock", "locked": True},
    "mod-guests": {"type": "mod-guests", "allowed": False},
    "mod-readonly": {"type": "mod-readonly", "target_user": "u1", "readonly": True},
    "mod-transfer": {"type": "mod-transfer", "target_user": "u1"},
}


# --------------------------------------------------------------------------- #
# Constants: close codes, error codes, lane/authz sets
# --------------------------------------------------------------------------- #


def test_close_codes_have_canonical_values():
    assert CLOSE_PROTOCOL == 4400
    assert CLOSE_KICKED == 4401
    assert CLOSE_FORBIDDEN == 4403
    assert CLOSE_NOT_FOUND == 4404
    assert CLOSE_SLOW == 4408
    assert CLOSE_DIALECT == 4409
    assert CLOSE_LOCKED == 4423
    assert CLOSE_LIMIT == 4429


def test_error_code_members_and_values():
    assert [c.value for c in ErrorCode] == [
        "bad_frame",
        "unauthorized",
        "forbidden",
        "readonly",
        "rate_limited",
        "too_large",
        "stale",
        "unknown_session",
        "dialect_mismatch",
        "internal",
    ]
    assert str(ErrorCode.bad_frame) == "bad_frame"
    assert ErrorCode.too_large == "too_large"


def test_client_and_server_types_exact():
    assert CLIENT_TYPES == {
        "hello",
        "state-set",
        "state-update",
        "data-update",
        "clicked-button",
        "chat-message",
        "chat-delete",
        "chat-recall",
        "ping",
        "session-state",
        "poly-snapshot",
        "poly-token-upsert",
        "poly-token-delete",
            "poly-cursor",
            "poly-lock",
            "doc-create",
            "doc-edit",
            "doc-cursor",
            "doc-reset",
            "mod-kick",
            "mod-ban",
        "mod-unban",
        "mod-lock",
        "mod-guests",
        "mod-readonly",
        "mod-transfer",
    }
    assert SERVER_TYPES == {
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
    assert CLIENT_TYPES.isdisjoint(SERVER_TYPES)


def test_lane_sets_partition_client_types():
    assert FAST_LANE == {"state-update", "data-update", "poly-cursor", "doc-cursor"}
    assert PROPOSAL_LANE == {"poly-token-upsert", "poly-token-delete", "doc-edit"}
    assert CHAT_LANE == {"chat-message"}
    assert SNAPSHOT_LANE == {"state-set", "poly-snapshot", "doc-create", "doc-reset"}
    assert CONTROL_LANE == {
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
    lanes = [FAST_LANE, PROPOSAL_LANE, CHAT_LANE, CONTROL_LANE, SNAPSHOT_LANE]
    union = set().union(*lanes)
    # hello carries no lane; every other client type belongs to exactly one lane.
    assert union == CLIENT_TYPES - {"hello"}
    assert sum(len(lane) for lane in lanes) == len(union)


def test_authz_sets_exact():
    assert WRITE_TYPES == {
        "state-update",
        "data-update",
        "clicked-button",
        "chat-message",
        "doc-edit",
        "poly-token-upsert",
        "poly-token-delete",
        "poly-lock",
    }
    # Ephemeral presence and reads are never gated by readonly.
    assert WRITE_TYPES.isdisjoint(
        {"poly-cursor", "doc-cursor", "session-state", "ping", "chat-delete", "chat-recall"}
    )
    assert MOD_TYPES == {
        "mod-kick",
        "mod-ban",
        "mod-unban",
        "mod-lock",
        "mod-guests",
        "mod-readonly",
        "mod-transfer",
    }
    assert OWNER_TYPES == MOD_TYPES | {"state-set", "poly-snapshot", "doc-create", "doc-reset"}
    assert MOD_TYPES <= OWNER_TYPES
    assert WRITE_TYPES.isdisjoint(OWNER_TYPES)


# --------------------------------------------------------------------------- #
# parse_frame
# --------------------------------------------------------------------------- #


def test_parse_frame_accepts_object(limits):
    assert parse_frame('{"type":"ping"}', max_len=limits.max_frame) == {"type": "ping"}


def test_parse_frame_rejects_non_json(limits):
    with pytest.raises(ProtocolError) as exc:
        parse_frame("not json at all", max_len=limits.max_frame)
    assert exc.value.code is ErrorCode.bad_frame


@pytest.mark.parametrize("raw", ["[1,2,3]", "42", '"a string"', "true", "null"])
def test_parse_frame_rejects_non_object(raw, limits):
    with pytest.raises(ProtocolError) as exc:
        parse_frame(raw, max_len=limits.max_frame)
    assert exc.value.code is ErrorCode.bad_frame


def test_parse_frame_rejects_oversize():
    with pytest.raises(ProtocolError) as exc:
        parse_frame("x" * 101, max_len=100)
    assert exc.value.code is ErrorCode.too_large


def test_parse_frame_size_checked_before_parse():
    # Oversize, syntactically-broken input still reports too_large (size checked first).
    with pytest.raises(ProtocolError) as exc:
        parse_frame("{" * 200, max_len=100)
    assert exc.value.code is ErrorCode.too_large


def test_parse_frame_size_is_utf8_bytes():
    # Two-byte UTF-8 chars count as their byte length, not char length.
    raw = '{"x":"' + "é" * 60 + '"}'  # 60 é = 120 bytes of content
    assert len(raw) < 100 < len(raw.encode("utf-8"))
    with pytest.raises(ProtocolError) as exc:
        parse_frame(raw, max_len=100)
    assert exc.value.code is ErrorCode.too_large


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_parse_frame_rejects_non_finite_constants(literal, limits):
    # Python's json accepts these; no browser can parse them back, and the
    # server would relay and persist them into every later snapshot.
    with pytest.raises(ProtocolError) as exc:
        parse_frame(
            '{"type":"state-update","id":"k","value":' + literal + "}",
            max_len=limits.max_frame,
        )
    assert exc.value.code is ErrorCode.bad_frame


def test_parse_frame_accepts_nesting_at_the_depth_cap(limits):
    raw = '{"type":"state-update","id":"k","value":'
    raw += "[" * (MAX_FRAME_DEPTH - 1) + "]" * (MAX_FRAME_DEPTH - 1) + "}"
    assert parse_frame(raw, max_len=limits.max_frame)["id"] == "k"


def test_parse_frame_rejects_nesting_past_the_depth_cap(limits):
    # Deeper than copy.deepcopy can walk: the session copies a lane per write.
    raw = '{"type":"state-update","id":"k","value":'
    raw += "[" * (MAX_FRAME_DEPTH + 1) + "]" * (MAX_FRAME_DEPTH + 1) + "}"
    with pytest.raises(ProtocolError) as exc:
        parse_frame(raw, max_len=limits.max_frame)
    assert exc.value.code is ErrorCode.bad_frame


def test_parse_frame_rejects_nesting_far_past_any_recursion_limit(limits):
    raw = '{"v":' + "[" * 30_000 + "]" * 30_000 + "}"
    with pytest.raises(ProtocolError) as exc:
        parse_frame(raw, max_len=len(raw) + 1)
    assert exc.value.code is ErrorCode.bad_frame


# --------------------------------------------------------------------------- #
# validate_message: acceptance of every canonical client type
# --------------------------------------------------------------------------- #


def test_canonical_frames_cover_every_client_type():
    assert set(CANONICAL_FRAMES) == CLIENT_TYPES


@pytest.mark.parametrize("msg_type", sorted(CANONICAL_FRAMES))
def test_validate_accepts_canonical_frame(msg_type, limits):
    frame = CANONICAL_FRAMES[msg_type]
    result = validate_message(frame, limits)
    assert isinstance(result, dict)
    assert result is not frame  # a new dict is returned
    assert result["type"] == msg_type
    assert NON_SCHEMA_ENVELOPE_FIELDS.isdisjoint(result)  # no envelope keys leak through


# --------------------------------------------------------------------------- #
# validate_message: rejections and normalization
# --------------------------------------------------------------------------- #


def test_validate_rejects_unknown_type(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "nonsense"}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "nonsense"


def test_validate_rejects_missing_type(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"id": "x", "value": 1}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "type"


@pytest.mark.parametrize("server_type", sorted(SERVER_TYPES))
def test_validate_rejects_inbound_server_types(server_type, limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": server_type}, limits)
    assert exc.value.code is ErrorCode.bad_frame


def test_validate_state_update_missing_id(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-update", "value": 1}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "id"


def test_validate_state_update_id_length_boundary(limits):
    ok = validate_message({"type": "state-update", "id": "a" * 128, "value": 1}, limits)
    assert ok["id"] == "a" * 128
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-update", "id": "a" * 129, "value": 1}, limits)
    assert exc.value.code is ErrorCode.too_large
    assert exc.value.ref_type == "id"


def test_validate_state_update_value_size_boundary(limits):
    assert json_size("x" * 8190) == 8192
    assert json_size("x" * 8191) == 8193
    ok = validate_message({"type": "state-update", "id": "k", "value": "x" * 8190}, limits)
    assert ok["value"] == "x" * 8190
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-update", "id": "k", "value": "x" * 8191}, limits)
    assert exc.value.code is ErrorCode.too_large
    assert exc.value.ref_type == "value"


def test_validate_poly_cursor_bad_mode(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "poly-cursor", "mode": "weird"}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "mode"


def test_validate_hello_wrong_protocol_version(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "hello", "protocol": 2}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "protocol"


def test_validate_hello_protocol_must_be_int(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "hello", "protocol": "1"}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "protocol"


def test_validate_hello_protocol_bool_rejected(limits):
    # bool is an int subclass; the validator must reject it explicitly.
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "hello", "protocol": True}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "protocol"


def test_validate_strips_unknown_and_envelope_keys(limits):
    msg = {
        "type": "state-update",
        "id": "k",
        "value": 1,
        "bogus": "drop me",
        "seq": 999,
        "username": "attacker",
        "session": "ZZZ",
        "message_id": "forged",
    }
    result = validate_message(msg, limits)
    assert result == {"type": "state-update", "id": "k", "value": 1}


def test_validate_clicked_button_passthrough_strips_envelope(limits):
    msg = {
        "type": "clicked-button",
        "action": "x",
        "payload": {"a": 1},
        "seq": 9,
        "username": "evil",
    }
    result = validate_message(msg, limits)
    assert result == {"type": "clicked-button", "action": "x", "payload": {"a": 1}}


def test_validate_clicked_button_oversize(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "clicked-button", "blob": "y" * 70000}, limits)
    assert exc.value.code is ErrorCode.too_large


def test_validate_chat_delete_keeps_message_id_but_strips_spoofed_envelope(limits):
    # `message_id` is a schema field here (target chat id) and must survive,
    # while other spoofed envelope fields are stripped.
    result = validate_message(
        {"type": "chat-delete", "message_id": "target-42", "seq": 7, "user_id": "evil"}, limits
    )
    assert result == {"type": "chat-delete", "message_id": "target-42"}


def test_validate_state_set_strips_item_unknown_keys(limits):
    result = validate_message(
        {"type": "state-set", "state": [{"id": "a", "value": 1, "junk": 2}]}, limits
    )
    assert result == {"type": "state-set", "state": [{"id": "a", "value": 1}]}


def test_validate_state_set_item_missing_value(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-set", "state": [{"id": "a"}]}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "value"


def test_validate_state_set_allows_null_value(limits):
    result = validate_message(
        {"type": "state-set", "state": [{"id": "a", "value": None}]}, limits
    )
    assert result["state"][0]["value"] is None


def test_validate_state_set_not_a_list(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-set", "state": {"id": "a"}}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "state"


def test_validate_data_update_role_too_long(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {"type": "data-update", "id": "n", "role": "r" * 65, "key": "k", "value": 1}, limits
        )
    assert exc.value.code is ErrorCode.too_large
    assert exc.value.ref_type == "role"


def test_validate_data_update_missing_key(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "data-update", "id": "n", "role": "r", "value": 1}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "key"


def test_validate_upsert_parent_id_absent_normalizes_to_none(limits):
    result = validate_message(
        {"type": "poly-token-upsert", "base_rev": 0, "id": "n", "kind": "k", "text": "t"}, limits
    )
    assert result["parentId"] is None
    assert "hash" not in result
    assert "author_seq" not in result


def test_validate_upsert_parent_id_null_normalizes_to_none(limits):
    result = validate_message(
        {
            "type": "poly-token-upsert",
            "base_rev": 0,
            "id": "n",
            "kind": "k",
            "text": "t",
            "parentId": None,
        },
        limits,
    )
    assert result["parentId"] is None


def test_validate_upsert_parent_id_string_kept(limits):
    result = validate_message(
        {
            "type": "poly-token-upsert",
            "base_rev": 0,
            "id": "n",
            "kind": "k",
            "text": "t",
            "parentId": "root",
        },
        limits,
    )
    assert result["parentId"] == "root"


def test_validate_upsert_parent_id_wrong_type(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {
                "type": "poly-token-upsert",
                "base_rev": 0,
                "id": "n",
                "kind": "k",
                "text": "t",
                "parentId": 5,
            },
            limits,
        )
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "parentId"


def test_validate_upsert_negative_base_rev(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {"type": "poly-token-upsert", "base_rev": -1, "id": "n", "kind": "k", "text": "t"},
            limits,
        )
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "base_rev"


def test_validate_upsert_negative_author_seq(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {
                "type": "poly-token-upsert",
                "base_rev": 0,
                "id": "n",
                "kind": "k",
                "text": "t",
                "author_seq": -3,
            },
            limits,
        )
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "author_seq"


def test_validate_rejects_nesting_past_the_depth_cap(limits):
    value = []
    for _ in range(MAX_FRAME_DEPTH + 1):
        value = [value]
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "state-update", "id": "k", "value": value}, limits)
    assert exc.value.code is ErrorCode.bad_frame


def test_validate_accepts_integer_at_the_javascript_safe_ceiling(limits):
    result = validate_message(
        {
            "type": "doc-edit",
            "docId": "d",
            "baseRev": MAX_SAFE_INT,
            "authorSeq": 1,
            "edit": {"start": 0, "end": 0, "text": "x"},
        },
        limits,
    )
    assert result["baseRev"] == MAX_SAFE_INT


@pytest.mark.parametrize("field", ["baseRev", "authorSeq"])
def test_validate_rejects_integer_beyond_the_javascript_safe_ceiling(field, limits):
    # The server echoes these back; JSON.parse turns anything larger into a
    # float, and a 400-digit literal into Infinity.
    msg = {
        "type": "doc-edit",
        "docId": "d",
        "baseRev": 0,
        "authorSeq": 1,
        "edit": {"start": 0, "end": 0, "text": "x"},
    }
    msg[field] = MAX_SAFE_INT + 1
    with pytest.raises(ProtocolError) as exc:
        validate_message(msg, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == field


def test_validate_doc_edit_rejects_end_before_start(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {
                "type": "doc-edit",
                "docId": "d",
                "baseRev": 0,
                "authorSeq": 1,
                "edit": {"start": 5, "end": 2, "text": ""},
            },
            limits,
        )
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "edit"


def test_validate_poly_cursor_node_mode(limits):
    result = validate_message(
        {"type": "poly-cursor", "mode": "node", "node_id": "root.0", "param": "hue"}, limits
    )
    assert result == {"type": "poly-cursor", "mode": "node", "node_id": "root.0", "param": "hue"}


def test_validate_poly_cursor_text_end_before_start(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {"type": "poly-cursor", "mode": "text", "range": {"start": 5, "end": 2}}, limits
        )
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "range"


def test_validate_mod_lock_requires_real_bool(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "mod-lock", "locked": "yes"}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "locked"


def test_validate_poly_lock_rejects_int_for_bool(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "poly-lock", "node_id": "n", "locked": 1}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "locked"


def test_validate_mod_kick_requires_a_target(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "mod-kick"}, limits)
    assert exc.value.code is ErrorCode.bad_frame


def test_validate_mod_kick_target_connection_only(limits):
    result = validate_message({"type": "mod-kick", "target_connection": "c1"}, limits)
    assert result == {"type": "mod-kick", "target_connection": "c1"}


def test_validate_hello_resume_normalized(limits):
    result = validate_message(
        {"type": "hello", "protocol": 1, "resume": {"last_seq": 10, "junk": 1}}, limits
    )
    assert result == {"type": "hello", "protocol": 1, "resume": {"last_seq": 10}}


def test_validate_hello_resume_bad_last_seq(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "hello", "protocol": 1, "resume": {"last_seq": -1}}, limits)
    assert exc.value.code is ErrorCode.bad_frame


def test_validate_hello_resume_not_object(limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": "hello", "protocol": 1, "resume": "nope"}, limits)
    assert exc.value.code is ErrorCode.bad_frame
    assert exc.value.ref_type == "resume"


# --------------------------------------------------------------------------- #
# stamp
# --------------------------------------------------------------------------- #


def test_stamp_overwrites_reserved_and_preserves_payload():
    msg = {
        "type": "chat-message",
        "message": "hi",
        "extra": "keep",
        "username": "attacker",
        "seq": 999,
        "user_id": "evil",
        "message_id": "forged",
    }
    out = stamp(
        msg,
        seq=5,
        session="ABC123",
        user_id="u1",
        username="alice",
        connection_id="c1",
        now=1_751_500_000.9,
    )
    assert out is not msg
    assert out["seq"] == 5
    assert out["session"] == "ABC123"
    assert out["user_id"] == "u1"
    assert out["username"] == "alice"
    assert out["connection_id"] == "c1"
    assert out["timestamp"] == 1_751_500_000  # int(now), truncated
    assert out["type"] == "chat-message"
    assert out["message"] == "hi"
    assert out["extra"] == "keep"
    assert out["message_id"] != "forged"
    assert len(out["message_id"]) == 36
    # Original mapping is untouched.
    assert msg["username"] == "attacker"
    assert msg["seq"] == 999
    assert msg["message_id"] == "forged"


def test_stamp_message_id_is_fresh_each_call():
    base = {"type": "ping"}
    a = stamp(base, seq=1, session="S", user_id="u", username="n", connection_id="c", now=1.0)
    b = stamp(base, seq=2, session="S", user_id="u", username="n", connection_id="c", now=2.0)
    assert a["message_id"] != b["message_id"]


# --------------------------------------------------------------------------- #
# error_frame
# --------------------------------------------------------------------------- #


def test_error_frame_minimal():
    assert error_frame(ErrorCode.bad_frame) == {"type": "error", "code": "bad_frame"}


def test_error_frame_omits_unset_optionals():
    frame = error_frame(ErrorCode.internal)
    assert frame == {"type": "error", "code": "internal"}


def test_error_frame_includes_all_optionals():
    frame = error_frame(
        ErrorCode.rate_limited, detail="slow down", ref_type="chat-message", retry_after=1.239
    )
    assert frame["type"] == "error"
    assert frame["code"] == "rate_limited"
    assert frame["detail"] == "slow down"
    assert frame["ref_type"] == "chat-message"
    assert frame["retry_after"] == pytest.approx(1.24)


def test_error_frame_rounds_retry_after_to_two_decimals():
    frame = error_frame(ErrorCode.rate_limited, retry_after=2.017)
    assert frame["retry_after"] == pytest.approx(2.02)


def test_error_frame_retry_after_only_when_given():
    assert "retry_after" not in error_frame(ErrorCode.too_large, detail="big")
    assert error_frame(ErrorCode.rate_limited, retry_after=0.5)["retry_after"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# new_id, json_size, ProtocolError
# --------------------------------------------------------------------------- #


def test_new_id_is_unique_uuid4_string():
    a = new_id()
    b = new_id()
    assert isinstance(a, str)
    assert len(a) == 36
    assert a != b


def test_json_size_uses_compact_separators_and_bytes():
    assert json_size([1, 2, 3]) == len("[1,2,3]")
    assert json_size({"a": 1, "b": 2}) == len('{"a":1,"b":2}')
    assert json_size(None) == 4
    assert json_size("") == 2  # the two quote characters


def test_protocol_error_carries_fields():
    err = ProtocolError(ErrorCode.too_large, "too big", ref_type="value")
    assert err.code is ErrorCode.too_large
    assert err.detail == "too big"
    assert err.ref_type == "value"
    assert str(err) == "too big"


def test_protocol_error_defaults():
    err = ProtocolError(ErrorCode.internal)
    assert err.detail == ""
    assert err.ref_type is None
    assert str(err) == ""


@pytest.mark.parametrize("number", ["1e999", "-1e999", "1.8e308"])
def test_parse_frame_rejects_overflowing_finite_json_numbers(number):
    with pytest.raises(ProtocolError) as exc:
        parse_frame(
            '{"type":"state-update","id":"x","value":' + number + '}',
            max_len=65536,
        )
    assert exc.value.code is ErrorCode.bad_frame


@pytest.mark.parametrize("number", [float("inf"), float("-inf"), float("nan")])
def test_validate_message_rejects_nested_nonfinite_values(number, limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message(
            {"type": "state-update", "id": "x", "value": {"nested": [number]}}, limits
        )
    assert exc.value.code is ErrorCode.bad_frame
