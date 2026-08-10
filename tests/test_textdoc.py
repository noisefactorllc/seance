"""Tests for the pure text document engine in app.textdoc."""

import dataclasses

import pytest

from app.config import Limits
from app.textdoc import (
    AcceptedTextEdit,
    TextDocCollection,
    TextDocLimit,
    TextDocReject,
    TextDocStaleWindow,
    TextEdit,
)


def _collection(**limit_overrides) -> TextDocCollection:
    return TextDocCollection(Limits(**limit_overrides))


def _doc_snapshot(collection: TextDocCollection, doc_id: str) -> dict:
    for doc in collection.snapshot():
        if doc["id"] == doc_id:
            return doc
    raise AssertionError(f"missing doc {doc_id}")


def test_text_edit_fields_exact():
    assert [field.name for field in dataclasses.fields(TextEdit)] == ["start", "end", "text"]


def test_accepted_text_edit_fields_exact():
    assert [field.name for field in dataclasses.fields(AcceptedTextEdit)] == [
        "doc_id",
        "rev",
        "author_id",
        "connection_id",
        "author_seq",
        "edit",
    ]


def test_create_doc_snapshot_and_default_reassignment():
    docs = _collection()

    first = docs.create_doc("main", "Program", "noisemaker-dsl", "alpha", default=True)
    second = docs.create_doc("deck:b", "Deck B", "noisemaker-dsl", "beta")
    third = docs.create_doc("deck:a", "Deck A", "noisemaker-dsl", "gamma", default=True)

    assert first == {
        "id": "main",
        "title": "Program",
        "kind": "noisemaker-dsl",
        "rev": 0,
        "text": "alpha",
        "default": True,
    }
    assert second["default"] is False
    assert third["default"] is True
    assert docs.snapshot() == [
        {
            "id": "main",
            "title": "Program",
            "kind": "noisemaker-dsl",
            "rev": 0,
            "text": "alpha",
            "default": False,
        },
        {
            "id": "deck:b",
            "title": "Deck B",
            "kind": "noisemaker-dsl",
            "rev": 0,
            "text": "beta",
            "default": False,
        },
        {
            "id": "deck:a",
            "title": "Deck A",
            "kind": "noisemaker-dsl",
            "rev": 0,
            "text": "gamma",
            "default": True,
        },
    ]


def test_create_doc_enforces_count_and_string_caps():
    docs = _collection(
        max_docs_per_session=1,
        max_doc_id_len=4,
        max_doc_title_len=5,
        max_doc_text=4,
    )
    docs.create_doc("main", "title", "dsl", "text", default=True)

    with pytest.raises(TextDocLimit):
        docs.create_doc("next", "title", "dsl", "x")
    with pytest.raises(TextDocLimit):
        _collection(max_doc_id_len=3).create_doc("long", "ok", "dsl", "")
    with pytest.raises(TextDocLimit):
        _collection(max_doc_title_len=2).create_doc("ok", "bad", "dsl", "")
    with pytest.raises(TextDocLimit):
        _collection(max_doc_text=3).create_doc("ok", "ok", "dsl", "long")


def test_reset_doc_requires_current_base_rev_and_clears_text():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "alpha", default=True)
    docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(5, 5, "!"),
        author_id="owner",
        connection_id="conn-1",
        author_seq=1,
    )

    with pytest.raises(TextDocReject) as exc:
        docs.reset_doc("main", "fresh", base_rev=0)
    assert exc.value.reason == "stale"

    snap = docs.reset_doc("main", "fresh", base_rev=1)
    assert snap == {
        "id": "main",
        "title": "Program",
        "kind": "dsl",
        "rev": 2,
        "text": "fresh",
        "default": True,
    }


def test_apply_edit_insert_accepts_and_bumps_rev():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abcd", default=True)

    accepted = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(2, 2, "XY"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )

    assert accepted == AcceptedTextEdit(
        doc_id="main",
        rev=1,
        author_id="u1",
        connection_id="c1",
        author_seq=1,
        edit=TextEdit(2, 2, "XY"),
    )
    assert _doc_snapshot(docs, "main")["text"] == "abXYcd"


def test_apply_edit_delete_accepts():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abcdef", default=True)

    accepted = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(2, 5, ""),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )

    assert accepted.rev == 1
    assert accepted.edit == TextEdit(2, 5, "")
    assert _doc_snapshot(docs, "main")["text"] == "abf"


def test_apply_edit_replace_accepts():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abcdef", default=True)

    accepted = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(1, 4, "XY"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )

    assert accepted.rev == 1
    assert accepted.edit == TextEdit(1, 4, "XY")
    assert _doc_snapshot(docs, "main")["text"] == "aXYef"


def test_apply_edit_paste_accepts():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "[]", default=True)

    accepted = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(1, 1, "line 1\nline 2"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )

    assert accepted.rev == 1
    assert _doc_snapshot(docs, "main")["text"] == "[line 1\nline 2]"


def test_apply_edit_rejects_empty_edit():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abc", default=True)

    with pytest.raises(TextDocReject) as exc:
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(1, 1, ""),
            author_id="u1",
            connection_id="c1",
            author_seq=1,
        )
    assert exc.value.reason == "invalid"


def test_apply_edit_rejects_out_of_range_edit():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abc", default=True)

    with pytest.raises(TextDocReject) as exc:
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(1, 4, "x"),
            author_id="u1",
            connection_id="c1",
            author_seq=1,
        )
    assert exc.value.reason == "invalid"


def test_apply_edit_rejects_oversize_edit_text():
    docs = _collection(max_doc_edit_text=3)
    docs.create_doc("main", "Program", "dsl", "abc", default=True)

    with pytest.raises(TextDocLimit):
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(1, 2, "long"),
            author_id="u1",
            connection_id="c1",
            author_seq=1,
        )


def test_apply_edit_transforms_stale_edit_inside_retained_log():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abcd", default=True)

    docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(1, 1, "X"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )

    accepted = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(2, 4, "YZ"),
        author_id="u2",
        connection_id="c2",
        author_seq=1,
    )

    assert accepted.rev == 2
    assert accepted.edit == TextEdit(3, 5, "YZ")
    assert _doc_snapshot(docs, "main")["text"] == "aXbYZ"


def test_apply_edit_rejects_stale_edit_outside_entry_window():
    docs = _collection(max_doc_oplog=1, max_doc_oplog_bytes=1_048_576)
    docs.create_doc("main", "Program", "dsl", "abcd", default=True)

    docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(0, 0, "X"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )
    docs.apply_edit(
        "main",
        base_rev=1,
        edit=TextEdit(5, 5, "Y"),
        author_id="u1",
        connection_id="c1",
        author_seq=2,
    )

    with pytest.raises(TextDocStaleWindow) as exc:
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(1, 2, "Q"),
            author_id="u2",
            connection_id="c2",
            author_seq=1,
        )
    assert exc.value.reason == "stale"


def test_apply_edit_rejects_stale_edit_outside_byte_window():
    docs = _collection(max_doc_oplog=10, max_doc_oplog_bytes=120)
    docs.create_doc("main", "Program", "dsl", "abcd", default=True)

    docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(0, 0, "first payload"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )
    docs.apply_edit(
        "main",
        base_rev=1,
        edit=TextEdit(0, 0, "second payload"),
        author_id="u1",
        connection_id="c1",
        author_seq=2,
    )

    with pytest.raises(TextDocStaleWindow):
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(2, 3, "Q"),
            author_id="u2",
            connection_id="c2",
            author_seq=1,
        )


def test_apply_edit_retry_dedup_is_keyed_by_doc_connection_and_author_seq():
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "abc", default=True)
    docs.create_doc("secondary", "Secondary", "dsl", "xyz")

    first = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(1, 1, "Q"),
        author_id="u1",
        connection_id="c1",
        author_seq=7,
    )
    retry = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(0, 0, "SHOULD NOT APPLY"),
        author_id="u1",
        connection_id="c1",
        author_seq=7,
    )
    other_connection = docs.apply_edit(
        "main",
        base_rev=1,
        edit=TextEdit(0, 0, "!"),
        author_id="u1",
        connection_id="c2",
        author_seq=7,
    )
    other_doc = docs.apply_edit(
        "secondary",
        base_rev=0,
        edit=TextEdit(1, 1, "!"),
        author_id="u1",
        connection_id="c1",
        author_seq=7,
    )

    assert retry == first
    assert first.rev == 1
    assert other_connection.rev == 2
    assert other_doc.rev == 1
    assert _doc_snapshot(docs, "main")["text"] == "!aQbc"
    assert _doc_snapshot(docs, "secondary")["text"] == "x!yz"


def test_retry_after_cached_outcome_eviction_never_reapplies_old_author_seq():
    docs = _collection(max_doc_oplog=2, max_doc_oplog_bytes=1_048_576)
    docs.create_doc("main", "Program", "dsl", "abc", default=True)

    first = docs.apply_edit(
        "main",
        base_rev=0,
        edit=TextEdit(1, 1, "Q"),
        author_id="u1",
        connection_id="c1",
        author_seq=1,
    )
    for author_seq in (2, 3):
        with pytest.raises(TextDocReject):
            docs.apply_edit(
                "main",
                base_rev=1,
                edit=TextEdit(99, 99, "!"),
                author_id="u1",
                connection_id="c1",
                author_seq=author_seq,
            )

    with pytest.raises(TextDocReject) as exc:
        docs.apply_edit(
            "main",
            base_rev=0,
            edit=TextEdit(0, 0, "SHOULD NOT APPLY"),
            author_id="u1",
            connection_id="c1",
            author_seq=1,
        )

    assert first.rev == 1
    assert exc.value.reason == "duplicate"
    assert _doc_snapshot(docs, "main")["text"] == "aQbc"


def test_create_doc_default_reassignment_is_failure_atomic():
    docs = _collection(max_doc_title_len=4)
    docs.create_doc("main", "Main", "dsl", "abc", default=True)

    with pytest.raises(TextDocLimit):
        docs.create_doc("bad", "too long", "dsl", "def", default=True)

    assert _doc_snapshot(docs, "main")["default"] is True
