"""Tests for the LWW state lane and nested data lane in app.engine."""

import dataclasses
import json

import pytest

from app.config import Limits
from app.engine import DataLane, EngineLimit, LwwEntry, StateLane

# --------------------------------------------------------------------------- #
# StateLane
# --------------------------------------------------------------------------- #


def test_lww_entry_fields_are_exactly_value_seq_by():
    names = [f.name for f in dataclasses.fields(LwwEntry)]
    assert names == ["value", "seq", "by"]


def test_apply_stores_entry_and_snapshot_shape():
    lane = StateLane(Limits())
    lane.apply("step_3.hueRange", [0, 1], seq=7, by="member:a")
    assert lane.snapshot() == [
        {"id": "step_3.hueRange", "value": [0, 1], "seq": 7, "by": "member:a"}
    ]


def test_apply_overwrite_keeps_latest_seq_and_value():
    lane = StateLane(Limits())
    lane.apply("k", "first", seq=1, by="a")
    lane.apply("k", "second", seq=9, by="b")
    assert lane.snapshot() == [{"id": "k", "value": "second", "seq": 9, "by": "b"}]


def test_snapshot_sorted_by_id():
    lane = StateLane(Limits())
    for sid in ("c", "a", "b"):
        lane.apply(sid, sid, seq=1, by="u")
    assert [e["id"] for e in lane.snapshot()] == ["a", "b", "c"]


def test_bulk_set_replaces_entire_map():
    lane = StateLane(Limits())
    lane.apply("old", 1, seq=1, by="u")
    lane.bulk_set([{"id": "x", "value": 10}, {"id": "y", "value": 20}], seq=5, by="owner")
    assert lane.snapshot() == [
        {"id": "x", "value": 10, "seq": 5, "by": "owner"},
        {"id": "y", "value": 20, "seq": 5, "by": "owner"},
    ]
    assert all(e["id"] != "old" for e in lane.snapshot())


def test_key_count_limit_4096_ok_4097_raises():
    lane = StateLane(Limits())
    for i in range(4096):
        lane.apply(f"k{i}", i, seq=i, by="u")
    assert len(lane.snapshot()) == 4096
    with pytest.raises(EngineLimit):
        lane.apply("k4096", 0, seq=4096, by="u")


def test_overwrite_allowed_at_capacity():
    lane = StateLane(Limits(max_state_keys=3))
    for i in range(3):
        lane.apply(f"k{i}", i, seq=i, by="u")
    with pytest.raises(EngineLimit):
        lane.apply("new", 0, seq=99, by="u")  # a NEW key beyond cap
    lane.apply("k0", "overwritten", seq=100, by="u")  # overwrite existing: allowed
    snap = {e["id"]: e["value"] for e in lane.snapshot()}
    assert snap["k0"] == "overwritten"
    assert len(snap) == 3


def test_value_size_limit_raises():
    lane = StateLane(Limits(max_value_bytes=10))
    lane.apply("ok", "hi", seq=1, by="u")  # json '"hi"' == 4 bytes
    with pytest.raises(EngineLimit):
        lane.apply("big", "x" * 50, seq=2, by="u")  # json ~52 bytes > 10


def test_id_length_limit_raises():
    lane = StateLane(Limits(max_state_id_len=5))
    lane.apply("abcde", 1, seq=1, by="u")
    with pytest.raises(EngineLimit):
        lane.apply("abcdef", 1, seq=2, by="u")


def test_bulk_set_all_or_nothing_on_failure():
    lane = StateLane(Limits(max_value_bytes=10))
    lane.apply("keep", "orig", seq=1, by="u")
    with pytest.raises(EngineLimit):
        lane.bulk_set(
            [{"id": "a", "value": "ok"}, {"id": "b", "value": "x" * 50}],
            seq=5,
            by="u",
        )
    assert lane.snapshot() == [{"id": "keep", "value": "orig", "seq": 1, "by": "u"}]


def test_bulk_set_count_limit():
    lane = StateLane(Limits(max_state_keys=2))
    lane.bulk_set([{"id": "a", "value": 1}, {"id": "b", "value": 2}], seq=1, by="u")
    with pytest.raises(EngineLimit):
        lane.bulk_set(
            [{"id": "a", "value": 1}, {"id": "b", "value": 2}, {"id": "c", "value": 3}],
            seq=2,
            by="u",
        )


def test_snapshot_load_round_trip_byte_equal():
    lane = StateLane(Limits())
    lane.apply("a", {"nested": [1, 2, 3]}, seq=3, by="member:x")
    lane.apply("b", "text", seq=4, by="anon:y")
    snap = lane.snapshot()
    restored = StateLane(Limits())
    restored.load(snap)
    assert restored.snapshot() == snap
    assert json.dumps(restored.snapshot()) == json.dumps(snap)


def test_load_replaces_without_limit_checks():
    lane = StateLane(Limits(max_state_keys=1, max_value_bytes=5, max_state_id_len=2))
    data = [
        {"id": "long-id", "value": "x" * 100, "seq": 1, "by": "u"},
        {"id": "second", "value": 2, "seq": 2, "by": "u"},
    ]
    lane.load(data)  # trusted persisted data may exceed live caps
    assert lane.snapshot() == [
        {"id": "long-id", "value": "x" * 100, "seq": 1, "by": "u"},
        {"id": "second", "value": 2, "seq": 2, "by": "u"},
    ]


def test_engine_limit_carries_detail():
    lane = StateLane(Limits(max_state_id_len=1))
    with pytest.raises(EngineLimit) as exc:
        lane.apply("toolong", 1, seq=1, by="u")
    assert isinstance(exc.value.detail, str)
    assert exc.value.detail


# --------------------------------------------------------------------------- #
# DataLane
# --------------------------------------------------------------------------- #


def test_data_apply_nested_and_snapshot():
    lane = DataLane(Limits())
    lane.apply("node1", "meta", "color", "red")
    lane.apply("node1", "meta", "size", 5)
    lane.apply("node2", "flags", "hidden", True)
    assert lane.snapshot() == {
        "node1": {"meta": {"color": "red", "size": 5}},
        "node2": {"flags": {"hidden": True}},
    }


def test_data_snapshot_is_deep_copy():
    lane = DataLane(Limits())
    lane.apply("i", "r", "k", {"nested": [1, 2, 3]})
    snap = lane.snapshot()
    snap["i"]["r"]["k"]["nested"].append(999)
    snap["i"]["r"]["new"] = "added"
    assert lane.snapshot() == {"i": {"r": {"k": {"nested": [1, 2, 3]}}}}


def test_data_overwrite_leaf_allowed():
    lane = DataLane(Limits())
    lane.apply("i", "r", "k", "one")
    lane.apply("i", "r", "k", "two")
    assert lane.snapshot() == {"i": {"r": {"k": "two"}}}


def test_data_leaf_count_limit():
    lane = DataLane(Limits(max_state_keys=2))
    lane.apply("i", "r", "k1", 1)
    lane.apply("i", "r", "k2", 2)
    with pytest.raises(EngineLimit):
        lane.apply("i", "r", "k3", 3)  # third distinct leaf beyond cap
    lane.apply("i", "r", "k1", 99)  # overwrite existing leaf at capacity: allowed
    assert lane.snapshot()["i"]["r"] == {"k1": 99, "k2": 2}


def test_data_value_size_limit():
    lane = DataLane(Limits(max_value_bytes=10))
    lane.apply("i", "r", "k", "ok")
    with pytest.raises(EngineLimit):
        lane.apply("i", "r", "big", "x" * 50)


def test_data_id_role_key_length_limit():
    lane = DataLane(Limits(max_state_id_len=4))
    lane.apply("id12", "role", "key4", 1)  # all exactly 4 chars: ok
    with pytest.raises(EngineLimit):
        lane.apply("id12345", "r", "k", 1)  # id too long
    with pytest.raises(EngineLimit):
        lane.apply("id12", "roleTooLong", "k", 1)  # role too long
    with pytest.raises(EngineLimit):
        lane.apply("id12", "r", "keyTooLong", 1)  # key too long


def test_data_load_replaces_and_deep_copies():
    lane = DataLane(Limits())
    lane.apply("old", "r", "k", 1)
    source = {"i": {"r": {"k": {"n": [1, 2]}}}}
    lane.load(source)
    assert lane.snapshot() == source
    source["i"]["r"]["k"]["n"].append(3)  # mutating source must not leak into lane
    assert lane.snapshot() == {"i": {"r": {"k": {"n": [1, 2]}}}}
    assert "old" not in lane.snapshot()
