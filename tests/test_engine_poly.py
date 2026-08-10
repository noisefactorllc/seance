"""Tests for the server-serialized OT document (PolyDoc) in app.engine.

Covers every numbered rule of the spec §6.3 algorithm: author-seq dedup,
stale/orphan/limit rejection, subtree delete, apply/no-op semantics, and
snapshot/load round-trips (with the author-seq dedup map explicitly NOT
persisted — a fresh epoch after load).
"""

import dataclasses
import json

import pytest

from app.config import Limits
from app.engine import ApplyResult, EngineLimit, Node, PolyDoc

ROOT = {"id": "root", "kind": "grp", "text": ""}


def _doc(**limit_overrides) -> PolyDoc:
    return PolyDoc(Limits(**limit_overrides))


def _root_doc(**limit_overrides) -> PolyDoc:
    doc = _doc(**limit_overrides)
    doc.apply_snapshot("p", [dict(ROOT)], None)  # rev 1, node "root"
    return doc


# --------------------------------------------------------------------------- #
# Dataclasses / result shape
# --------------------------------------------------------------------------- #


def test_node_fields_exact():
    names = [f.name for f in dataclasses.fields(Node)]
    assert names == ["id", "kind", "text", "version", "parent_id"]


def test_apply_result_defaults():
    r = ApplyResult(status="applied", rev=1, applied=[{"id": "root", "version": 1}])
    assert r.reason is None


# --------------------------------------------------------------------------- #
# apply_snapshot
# --------------------------------------------------------------------------- #


def test_fresh_snapshot_gives_rev_1_and_versions_1():
    doc = _doc()
    rev = doc.apply_snapshot(
        "prog",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.0", "kind": "step", "text": "a", "parentId": "root"},
        ],
        {"w": 2},
    )
    assert rev == 1
    snap = doc.snapshot()
    assert snap["rev"] == 1
    assert snap["programText"] == "prog"
    assert snap["frame"] == {"w": 2}
    assert [n["id"] for n in snap["nodes"]] == ["root", "root.0"]
    assert all(n["version"] == 1 for n in snap["nodes"])
    assert snap["nodes"][0] == {
        "id": "root", "kind": "grp", "text": "", "version": 1, "parentId": None
    }
    assert snap["nodes"][1]["parentId"] == "root"


def test_second_snapshot_bumps_rev_and_resets_versions():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2, root.0 version 2
    rev = doc.apply_snapshot(
        "p2",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.0", "kind": "s", "text": "x", "parentId": "root"},
        ],
        None,
    )
    assert rev == 3
    assert all(n["version"] == 3 for n in doc.snapshot()["nodes"])


def test_apply_snapshot_program_text_limit_changes_nothing():
    doc = _doc(max_program_text=5)
    with pytest.raises(EngineLimit):
        doc.apply_snapshot("123456", [], None)
    assert doc.rev == 0
    assert doc.nodes == {}


def test_apply_snapshot_node_count_limit_changes_nothing():
    doc = _doc(max_nodes=1)
    with pytest.raises(EngineLimit):
        doc.apply_snapshot(
            "p",
            [{"id": "a", "kind": "s", "text": ""}, {"id": "b", "kind": "s", "text": ""}],
            None,
        )
    assert doc.rev == 0
    assert doc.nodes == {}


def test_apply_snapshot_node_text_limit_changes_nothing():
    doc = _doc(max_node_text=3)
    doc.apply_snapshot("p", [{"id": "root", "kind": "grp", "text": "ok"}], None)  # rev 1
    before = doc.snapshot()
    with pytest.raises(EngineLimit):
        doc.apply_snapshot("p2", [{"id": "root", "kind": "grp", "text": "toolong"}], None)
    assert doc.snapshot() == before
    assert doc.rev == 1


def test_apply_snapshot_invalid_node_field_raises_and_changes_nothing():
    doc = _root_doc()  # rev 1
    before = doc.snapshot()
    with pytest.raises(EngineLimit):
        doc.apply_snapshot("p2", [{"id": "root", "kind": 123, "text": ""}], None)  # kind not str
    assert doc.snapshot() == before
    with pytest.raises(EngineLimit):
        doc.apply_snapshot("p2", [{"id": "root", "text": ""}], None)  # kind missing
    assert doc.snapshot() == before


# --------------------------------------------------------------------------- #
# upsert: apply / stale / orphan / limit
# --------------------------------------------------------------------------- #


def test_upsert_on_current_base_applies_and_bumps_rev():
    doc = _root_doc()  # rev 1
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="step", text="hi",
        parent_id="root", author="member:a", author_seq=None,
    )
    assert r.status == "applied"
    assert r.rev == 2
    assert r.applied == [{"id": "root.0", "version": 2}]
    assert r.reason is None
    assert doc.rev == 2
    assert doc.nodes["root.0"].version == 2
    assert doc.nodes["root.0"].text == "hi"
    assert doc.nodes["root.0"].parent_id == "root"


def test_root_level_upsert_allows_none_parent():
    doc = _doc()
    r = doc.upsert(
        base_rev=0, node_id="root", kind="grp", text="",
        parent_id=None, author="a", author_seq=None,
    )
    assert r.status == "applied"
    assert r.rev == 1
    assert doc.nodes["root"].parent_id is None


def test_stale_upsert_rejected_and_mutates_nothing():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v1",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2, root.0 version 2
    before = doc.snapshot()
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v2",
        parent_id="root", author="a", author_seq=None,
    )
    assert r.status == "rejected"
    assert r.reason == "stale"
    assert r.rev == 2
    assert r.applied == []
    assert doc.snapshot() == before


def test_upsert_after_refresh_applies():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v1",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2
    r = doc.upsert(
        base_rev=2, node_id="root.0", kind="s", text="v2",
        parent_id="root", author="a", author_seq=None,
    )
    assert r.status == "applied"
    assert r.rev == 3
    assert doc.nodes["root.0"].text == "v2"
    assert doc.nodes["root.0"].version == 3


def test_orphan_parent_rejected_and_mutates_nothing():
    doc = _root_doc()  # rev 1
    before = doc.snapshot()
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="ghost", author="a", author_seq=None,
    )
    assert r.status == "rejected"
    assert r.reason == "orphan"
    assert r.rev == 1
    assert doc.snapshot() == before


def test_upsert_node_count_limit_and_mutates_nothing():
    doc = _doc(max_nodes=2)
    doc.upsert(
        base_rev=0, node_id="root", kind="grp", text="",
        parent_id=None, author="a", author_seq=None,
    )  # rev 1 (1 node)
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2 (2 nodes)
    before = doc.snapshot()
    r = doc.upsert(
        base_rev=2, node_id="root.1", kind="s", text="y",
        parent_id="root", author="a", author_seq=None,
    )  # would be the 3rd node
    assert r.status == "rejected"
    assert r.reason == "limit"
    assert r.rev == 2
    assert "root.1" not in doc.nodes
    assert doc.snapshot() == before


def test_upsert_new_node_text_limit():
    doc = _root_doc(max_node_text=5)
    ok = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="12345",
        parent_id="root", author="a", author_seq=None,
    )
    assert ok.status == "applied"
    r = doc.upsert(
        base_rev=2, node_id="root.1", kind="s", text="123456",
        parent_id="root", author="a", author_seq=None,
    )
    assert r.status == "rejected"
    assert r.reason == "limit"


def test_existing_node_upsert_text_limit_leaves_node_unchanged():
    doc = _root_doc(max_node_text=5)
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="ok",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2
    r = doc.upsert(
        base_rev=2, node_id="root.0", kind="s", text="toolong",
        parent_id="root", author="a", author_seq=None,
    )
    assert r.status == "rejected"
    assert r.reason == "limit"
    assert doc.nodes["root.0"].text == "ok"


def test_existing_node_upsert_ignores_parent_and_updates_in_place():
    doc = _doc()
    doc.apply_snapshot(
        "p",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.0", "kind": "s", "text": "a", "parentId": "root"},
        ],
        None,
    )  # rev 1
    # parent points at a non-existent node, but for an EXISTING node it is ignored
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="s2", text="b",
        parent_id="ghost", author="a", author_seq=None,
    )
    assert r.status == "applied"
    assert doc.nodes["root.0"].kind == "s2"
    assert doc.nodes["root.0"].text == "b"
    assert doc.nodes["root.0"].parent_id == "ghost"  # updated when provided non-None


def test_existing_node_upsert_keeps_parent_when_none():
    doc = _doc()
    doc.apply_snapshot(
        "p",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.0", "kind": "s", "text": "a", "parentId": "root"},
        ],
        None,
    )  # rev 1
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="b",
        parent_id=None, author="a", author_seq=None,
    )
    assert r.status == "applied"
    assert doc.nodes["root.0"].parent_id == "root"  # None means "leave parent unchanged"


# --------------------------------------------------------------------------- #
# Author-seq dedup
# --------------------------------------------------------------------------- #


def test_duplicate_author_seq_same_base_is_dropped():
    doc = _root_doc()  # rev 1
    r1 = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="member:a", author_seq=3,
    )
    assert r1.status == "applied"
    assert doc.rev == 2
    r2 = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="member:a", author_seq=3,
    )
    assert r2.status == "duplicate"
    assert r2.rev == 2  # unchanged
    assert r2.applied == []
    assert doc.rev == 2


def test_same_author_seq_different_base_applies():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="member:a", author_seq=3,
    )  # rev 2, record {seq:3, base_rev:1}
    r = doc.upsert(
        base_rev=2, node_id="root.1", kind="s", text="y",
        parent_id="root", author="member:a", author_seq=3,
    )  # base differs -> not a duplicate
    assert r.status == "applied"
    assert r.rev == 3


def test_lower_author_seq_same_base_dedups():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=5,
    )  # record {seq:5, base_rev:1}, rev 2
    lower = doc.upsert(
        base_rev=1, node_id="root.9", kind="s", text="z",
        parent_id="root", author="a", author_seq=4,
    )
    assert lower.status == "duplicate"
    assert "root.9" not in doc.nodes  # deduped: never created


def test_rejected_proposal_still_updates_dedup_record():
    # "record then evaluate": a rejected op records its author_seq first, so an
    # identical retransmit of that rejected op is subsequently deduped.
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v1",
        parent_id="root", author="a", author_seq=1,
    )  # rev 2, root.0 version 2, record {seq:1, base_rev:1}
    rej = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v2",
        parent_id="root", author="a", author_seq=2,
    )  # records {seq:2, base_rev:1}, then rejects stale
    assert rej.status == "rejected"
    assert rej.reason == "stale"
    dup = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="v2",
        parent_id="root", author="a", author_seq=2,
    )  # identical retransmit -> now a duplicate
    assert dup.status == "duplicate"
    assert dup.rev == 2


def test_author_seq_none_never_dedups():
    doc = _root_doc()  # rev 1
    r1 = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2
    r2 = doc.upsert(
        base_rev=2, node_id="root.1", kind="s", text="y",
        parent_id="root", author="a", author_seq=None,
    )  # rev 3
    assert r1.status == "applied"
    assert r2.status == "applied"
    assert doc._author_seq == {}  # None never records dedup state


def test_apply_snapshot_clears_dedup_epoch():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.A", kind="s", text="x",
        parent_id="root", author="c", author_seq=5,
    )  # rev 2, record {seq:5, base_rev:1}
    dup = doc.upsert(
        base_rev=1, node_id="root.B", kind="s", text="y",
        parent_id="root", author="c", author_seq=5,
    )
    assert dup.status == "duplicate"  # dedup active before snapshot
    doc.apply_snapshot("p2", [dict(ROOT)], None)  # rev 3, dedup cleared
    r = doc.upsert(
        base_rev=3, node_id="root.B", kind="s", text="y",
        parent_id="root", author="c", author_seq=5,
    )
    assert r.status == "applied"  # previously-duplicate op now applies
    assert r.rev == 4
    assert "root.B" in doc.nodes


def test_author_seq_map_evicts_oldest_over_cap(monkeypatch):
    # Cap the per-author dedup map small; a churn of distinct authors must evict
    # the oldest-inserted record so the map stays bounded (guest-author churn).
    monkeypatch.setattr("app.engine._AUTHOR_SEQ_CAP", 3)
    doc = _doc()  # rev 0, empty
    # Fill the dedup map to the cap with 3 distinct authors, each creating a node.
    for i in range(3):
        r = doc.upsert(
            base_rev=0, node_id=f"n{i}", kind="s", text="x",
            parent_id=None, author=f"author-{i}", author_seq=1,
        )
        assert r.status == "applied"
    assert len(doc._author_seq) == 3
    # author-0's record is active: a fresh proposal at the same (base_rev, seq) is
    # deduped, so "a0-second" is never created.
    dup = doc.upsert(
        base_rev=0, node_id="a0-second", kind="s", text="y",
        parent_id=None, author="author-0", author_seq=1,
    )
    assert dup.status == "duplicate"
    assert "a0-second" not in doc.nodes
    # A 4th distinct author overflows the cap and evicts author-0 (oldest inserted).
    over = doc.upsert(
        base_rev=0, node_id="n3", kind="s", text="z",
        parent_id=None, author="author-3", author_seq=1,
    )
    assert over.status == "applied"
    assert "author-0" not in doc._author_seq
    assert len(doc._author_seq) == 3  # bounded at the cap, not grown
    # author-0's record is gone, so its previously-duplicate proposal now applies.
    reapplied = doc.upsert(
        base_rev=0, node_id="a0-second", kind="s", text="y",
        parent_id=None, author="author-0", author_seq=1,
    )
    assert reapplied.status == "applied"
    assert "a0-second" in doc.nodes


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_removes_subtree():
    doc = _doc()
    doc.apply_snapshot(
        "p",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.1", "kind": "grp", "text": "", "parentId": "root"},
            {"id": "root.1.0", "kind": "s", "text": "a", "parentId": "root.1"},
            {"id": "root.1.1", "kind": "s", "text": "b", "parentId": "root.1"},
            {"id": "root.2", "kind": "s", "text": "c", "parentId": "root"},
        ],
        None,
    )  # rev 1
    r = doc.delete(base_rev=1, node_id="root.1", author="a", author_seq=None)
    assert r.status == "applied"
    assert r.rev == 2
    assert r.applied == [
        {"id": "root.1", "version": 2},
        {"id": "root.1.0", "version": 2},
        {"id": "root.1.1", "version": 2},
    ]
    assert set(doc.nodes) == {"root", "root.2"}


def test_delete_prefix_precision_keeps_similar_sibling():
    doc = _doc()
    doc.apply_snapshot(
        "p",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.1", "kind": "s", "text": "a", "parentId": "root"},
            {"id": "root.10", "kind": "s", "text": "b", "parentId": "root"},
        ],
        None,
    )  # rev 1
    r = doc.delete(base_rev=1, node_id="root.1", author="a", author_seq=None)
    assert [a["id"] for a in r.applied] == ["root.1"]
    assert "root.10" in doc.nodes  # not a dotted descendant of root.1


def test_delete_unknown_is_applied_noop():
    doc = _root_doc()  # rev 1
    before = doc.snapshot()
    r = doc.delete(base_rev=1, node_id="does.not.exist", author="a", author_seq=None)
    assert r.status == "applied"
    assert r.applied == []
    assert r.rev == 1  # unchanged
    assert doc.snapshot() == before


def test_delete_stale_rejected_and_mutates_nothing():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2, root.0 version 2
    before = doc.snapshot()
    r = doc.delete(base_rev=1, node_id="root.0", author="a", author_seq=None)  # base stale
    assert r.status == "rejected"
    assert r.reason == "stale"
    assert r.rev == 2
    assert doc.snapshot() == before


# --------------------------------------------------------------------------- #
# bool / non-int guard (defense in depth; the protocol layer normally gates these)
# --------------------------------------------------------------------------- #


def test_upsert_bool_base_rev_rejected_stale_and_mutates_nothing():
    doc = _root_doc()  # rev 1
    before = doc.snapshot()
    r = doc.upsert(
        base_rev=True, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )
    assert r.status == "rejected"
    assert r.reason == "stale"
    assert r.rev == 1
    assert doc.snapshot() == before


def test_delete_bool_base_rev_rejected_stale_and_mutates_nothing():
    doc = _root_doc()  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=None,
    )  # rev 2
    before = doc.snapshot()
    r = doc.delete(base_rev=True, node_id="root.0", author="a", author_seq=None)
    assert r.status == "rejected"
    assert r.reason == "stale"
    assert r.rev == 2
    assert doc.snapshot() == before


def test_upsert_bool_author_seq_treated_as_absent():
    doc = _root_doc()  # rev 1
    r = doc.upsert(
        base_rev=1, node_id="root.0", kind="s", text="x",
        parent_id="root", author="a", author_seq=True,
    )
    assert r.status == "applied"  # a bool author_seq is ignored -> the op applies
    assert doc._author_seq == {}  # and no dedup record is keyed by the bool
    assert doc.nodes["root.0"].text == "x"


# --------------------------------------------------------------------------- #
# snapshot / load round-trip
# --------------------------------------------------------------------------- #


def test_snapshot_load_round_trip_and_dedup_not_persisted():
    doc = _doc()
    doc.apply_snapshot(
        "prog",
        [
            {"id": "root", "kind": "grp", "text": ""},
            {"id": "root.0", "kind": "s", "text": "a", "parentId": "root"},
        ],
        {"frameKey": [1, 2]},
    )  # rev 1
    doc.upsert(
        base_rev=1, node_id="root.A", kind="s", text="v",
        parent_id="root", author="member:c", author_seq=5,
    )  # rev 2, record {seq:5, base_rev:1}
    # same author+base+seq -> a duplicate in `doc`, so root.B is never created
    dup = doc.upsert(
        base_rev=1, node_id="root.B", kind="s", text="w",
        parent_id="root", author="member:c", author_seq=5,
    )
    assert dup.status == "duplicate"
    assert "root.B" not in doc.nodes

    snap = doc.snapshot()
    assert set(snap.keys()) == {"rev", "programText", "frame", "nodes"}  # dedup NOT persisted
    assert snap["rev"] == 2
    assert snap["frame"] == {"frameKey": [1, 2]}

    restored = PolyDoc(Limits())
    restored.load(snap)
    assert restored.snapshot() == snap
    assert json.dumps(restored.snapshot()) == json.dumps(snap)
    assert restored.rev == 2

    # dedup epoch reset by load: the previously-duplicate op now applies
    r = restored.upsert(
        base_rev=1, node_id="root.B", kind="s", text="w",
        parent_id="root", author="member:c", author_seq=5,
    )
    assert r.status == "applied"
    assert "root.B" in restored.nodes
    assert restored.rev == 3
