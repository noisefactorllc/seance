"""Protocol coverage for the Seance document-collaboration lane."""

from dataclasses import dataclass

import pytest

from app.protocol import (
    CLIENT_TYPES,
    FAST_LANE,
    OWNER_TYPES,
    PROPOSAL_LANE,
    SERVER_TYPES,
    SNAPSHOT_LANE,
    WRITE_TYPES,
    ErrorCode,
    ProtocolError,
    validate_message,
)


@dataclass
class FakeLimits:
    max_frame: int = 65536
    max_snapshot_frame: int = 1_048_576
    max_value_bytes: int = 8192
    max_state_id_len: int = 128
    max_chat_len: int = 2000
    max_node_text: int = 65536
    max_program_text: int = 262144
    max_docs_per_session: int = 8
    max_doc_id_len: int = 128
    max_doc_title_len: int = 128
    max_doc_text: int = 262144
    max_doc_edit_text: int = 65536


@pytest.fixture
def limits() -> FakeLimits:
    return FakeLimits()


def test_doc_types_extend_client_server_lane_and_auth_sets():
    assert {"doc-create", "doc-edit", "doc-cursor", "doc-reset"} <= CLIENT_TYPES
    assert {"doc-snapshot", "doc-ack", "doc-reject"} <= SERVER_TYPES
    assert "doc-cursor" not in SERVER_TYPES

    assert "doc-cursor" in FAST_LANE
    assert "doc-edit" in PROPOSAL_LANE
    assert {"doc-create", "doc-reset"} <= SNAPSHOT_LANE
    assert "doc-edit" in WRITE_TYPES
    assert {"doc-create", "doc-reset"} <= OWNER_TYPES


def test_validate_doc_create_normalizes_payload(limits):
    msg = validate_message(
        {
            "type": "doc-create",
            "doc": {
                "id": "main",
                "title": "Program",
                "kind": "noisemaker-dsl",
                "text": "osc()",
                "default": True,
                "ignored": "nope",
            },
            "seq": 99,
        },
        limits,
    )
    assert msg == {
        "type": "doc-create",
        "doc": {
            "id": "main",
            "title": "Program",
            "kind": "noisemaker-dsl",
            "text": "osc()",
            "default": True,
        },
    }


def test_validate_doc_edit_accepts_canonical_shape(limits):
    msg = validate_message(
        {
            "type": "doc-edit",
            "docId": "main",
            "baseRev": 3,
            "authorSeq": 7,
            "edit": {"start": 1, "end": 4, "text": "hi"},
            "username": "spoofed",
        },
        limits,
    )
    assert msg == {
        "type": "doc-edit",
        "docId": "main",
        "baseRev": 3,
        "authorSeq": 7,
        "edit": {"start": 1, "end": 4, "text": "hi"},
    }


def test_validate_doc_cursor_accepts_optional_direction(limits):
    msg = validate_message(
        {
            "type": "doc-cursor",
            "docId": "deck:A",
            "range": {"start": 2, "end": 5},
            "direction": "backward",
        },
        limits,
    )
    assert msg == {
        "type": "doc-cursor",
        "docId": "deck:A",
        "range": {"start": 2, "end": 5},
        "direction": "backward",
    }


@pytest.mark.parametrize("msg_type", ["doc-snapshot", "doc-ack", "doc-reject"])
def test_server_only_doc_messages_rejected_inbound(msg_type, limits):
    with pytest.raises(ProtocolError) as exc:
        validate_message({"type": msg_type}, limits)
    assert exc.value.code == ErrorCode.bad_frame
    assert "server-only" in exc.value.detail
