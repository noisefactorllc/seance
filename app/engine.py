"""Pure sync-engine core for seance: zero-I/O convergence primitives.

Three cooperating pieces, all synchronous and free of side effects beyond their
own in-memory state:

* :class:`StateLane` — the LWW param register (``state-update`` / ``state-set``):
  ``Map<id, {value, seq, by}>`` where the last write in server-arrival order
  wins (CRDT semantics under the session's total order).
* :class:`DataLane` — the nested ``data[id][role][key] = value`` side map
  (``data-update``), same validation discipline with per-leaf accounting.
* :class:`PolyDoc` — the server-serialized OT document: a flat node tree keyed by
  dotted id, carrying a monotonic ``rev`` and per-node ``version`` plus per-author
  sequence dedup. Proposals are validated against the ``base_rev`` / ``version``
  they were based on and either applied or rejected — never silently reordered
  (spec §6.3).

Capacity and size caps come from :class:`app.config.Limits`; a breach raises
:class:`EngineLimit`, whose ``detail`` names the violated cap for the caller to
surface as a ``too_large`` protocol error. String fields (ids, node/program
text) are capped by character length; ``value`` payloads are capped by canonical
JSON byte length via :func:`_json_size`.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Literal

from app.config import Limits


def _json_size(value) -> int:
    """Byte length of ``value`` serialized as compact (canonical) JSON."""
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _is_int(value) -> bool:
    """True for a genuine ``int``, excluding ``bool`` (an ``int`` subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


class EngineLimit(Exception):
    """A mutation would exceed an engine capacity or size cap.

    ``detail`` names the violated cap (e.g. ``"state map full (4096 keys)"``).
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------- #
# LWW param lane
# --------------------------------------------------------------------------- #


@dataclass
class LwwEntry:
    """One LWW register cell: exactly ``{value, seq, by}``."""

    value: object
    seq: int
    by: str


class StateLane:
    """Last-writer-wins map keyed by client-defined id (server-opaque)."""

    def __init__(self, limits: Limits):
        self._limits = limits
        self._entries: dict[str, LwwEntry] = {}

    def _check_entry(self, id: str, value) -> None:
        if len(id) > self._limits.max_state_id_len:
            raise EngineLimit(f"state id too long ({self._limits.max_state_id_len} chars)")
        if _json_size(value) > self._limits.max_value_bytes:
            raise EngineLimit(f"state value too large ({self._limits.max_value_bytes} bytes)")

    def apply(self, id: str, value, seq: int, by: str) -> None:
        self._check_entry(id, value)
        if id not in self._entries and len(self._entries) >= self._limits.max_state_keys:
            raise EngineLimit(f"state map full ({self._limits.max_state_keys} keys)")
        self._entries[id] = LwwEntry(value=value, seq=seq, by=by)

    def bulk_set(self, items: list[dict], seq: int, by: str) -> None:
        candidate: dict[str, LwwEntry] = {}
        for item in items:
            sid = item["id"]
            value = item["value"]
            self._check_entry(sid, value)
            candidate[sid] = LwwEntry(value=value, seq=seq, by=by)
        if len(candidate) > self._limits.max_state_keys:
            raise EngineLimit(f"state map full ({self._limits.max_state_keys} keys)")
        self._entries = candidate

    def snapshot(self) -> list[dict]:
        return [
            {"id": sid, "value": entry.value, "seq": entry.seq, "by": entry.by}
            for sid, entry in sorted(self._entries.items(), key=lambda kv: kv[0])
        ]

    def load(self, data: list[dict]) -> None:
        self._entries = {
            item["id"]: LwwEntry(value=item["value"], seq=item["seq"], by=item["by"])
            for item in data
        }


# --------------------------------------------------------------------------- #
# Nested data lane
# --------------------------------------------------------------------------- #


class DataLane:
    """Nested ``data[id][role][key] = value`` map with per-leaf accounting."""

    def __init__(self, limits: Limits):
        self._limits = limits
        self._data: dict[str, dict[str, dict[str, object]]] = {}

    def _leaf_count(self) -> int:
        return sum(len(keys) for roles in self._data.values() for keys in roles.values())

    def apply(self, id: str, role: str, key: str, value) -> None:
        cap = self._limits.max_state_id_len
        for label, text in (("id", id), ("role", role), ("key", key)):
            if len(text) > cap:
                raise EngineLimit(f"data {label} too long ({cap} chars)")
        if _json_size(value) > self._limits.max_value_bytes:
            raise EngineLimit(f"data value too large ({self._limits.max_value_bytes} bytes)")
        roles = self._data.get(id)
        keys = roles.get(role) if roles is not None else None
        is_new_leaf = keys is None or key not in keys
        if is_new_leaf and self._leaf_count() >= self._limits.max_state_keys:
            raise EngineLimit(f"data map full ({self._limits.max_state_keys} leaves)")
        self._data.setdefault(id, {}).setdefault(role, {})[key] = value

    def snapshot(self) -> dict:
        return copy.deepcopy(self._data)

    def load(self, data: dict) -> None:
        self._data = copy.deepcopy(data)


# --------------------------------------------------------------------------- #
# Structural OT document
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    """One node in the polydoc tree: ``{id, kind, text, version, parent_id}``."""

    id: str
    kind: str
    text: str
    version: int
    parent_id: str | None


@dataclass
class ApplyResult:
    """Outcome of a polydoc proposal."""

    status: Literal["applied", "rejected", "duplicate"]
    rev: int
    applied: list[dict]
    reason: str | None = None


# Bound on :class:`PolyDoc`'s per-author dedup map. Guest proposals are keyed by a
# per-connection author string, so a churn of short-lived guests could otherwise
# grow the map without limit. Past this many distinct authors the oldest-inserted
# entry is evicted (dicts preserve insertion order).
_AUTHOR_SEQ_CAP = 4096


class PolyDoc:
    """Server-serialized OT over a flat, dotted-id node tree (spec §6.3)."""

    def __init__(self, limits: Limits):
        self._limits = limits
        self.rev = 0
        self.program_text = ""
        self.frame: object = None
        self.nodes: dict[str, Node] = {}
        self._author_seq: dict[str, dict] = {}

    # -- author-seq dedup -------------------------------------------------- #

    def _is_duplicate(self, author: str, base_rev: int, author_seq: int | None) -> bool:
        """Record-then-evaluate author-seq dedup; True means drop the proposal.

        A finite ``author_seq`` at the author's last-recorded ``base_rev`` that is
        not newer than the recorded high-water mark is a retransmit and dropped.
        Otherwise the record is updated (even for a proposal that later rejects).
        A non-``int`` (or ``bool``) ``author_seq`` is treated as absent: no dedup
        record is keyed by it. Recording a **new** author once the map is at
        :data:`_AUTHOR_SEQ_CAP` first evicts the oldest-inserted author, so the
        map stays bounded under guest (connection-keyed) author churn.
        """
        if not _is_int(author_seq):
            return False
        last = self._author_seq.get(author)
        if last is not None and last["base_rev"] == base_rev and author_seq <= last["seq"]:
            return True
        if author not in self._author_seq and len(self._author_seq) >= _AUTHOR_SEQ_CAP:
            del self._author_seq[next(iter(self._author_seq))]
        self._author_seq[author] = {"seq": author_seq, "base_rev": base_rev}
        return False

    # -- mutations --------------------------------------------------------- #

    def apply_snapshot(self, program_text: str, nodes: list[dict], frame) -> int:
        """Replace the whole doc (owner-only). Validate first; on any breach raise
        :class:`EngineLimit` and change nothing. On success bump ``rev``, reset
        every node ``version`` to it, and clear the dedup epoch."""
        if len(program_text) > self._limits.max_program_text:
            raise EngineLimit(f"program text too large ({self._limits.max_program_text} chars)")
        if len(nodes) > self._limits.max_nodes:
            raise EngineLimit(f"node count exceeds maximum ({self._limits.max_nodes} nodes)")
        new_rev = self.rev + 1
        prepared: list[Node] = []
        for node in nodes:
            node_id = node.get("id")
            kind = node.get("kind")
            text = node.get("text")
            parent_id = node.get("parentId")
            if not (isinstance(node_id, str) and isinstance(kind, str) and isinstance(text, str)):
                raise EngineLimit("node id, kind and text must be strings")
            if parent_id is not None and not isinstance(parent_id, str):
                raise EngineLimit("node parentId must be a string or null")
            if len(text) > self._limits.max_node_text:
                raise EngineLimit(f"node text too large ({self._limits.max_node_text} chars)")
            prepared.append(
                Node(id=node_id, kind=kind, text=text, version=new_rev, parent_id=parent_id)
            )
        self.nodes = {node.id: node for node in prepared}
        self.rev = new_rev
        self.program_text = program_text
        self.frame = frame
        self._author_seq.clear()
        return new_rev

    def upsert(
        self,
        *,
        base_rev: int,
        node_id: str,
        kind: str,
        text: str,
        parent_id: str | None,
        author: str,
        author_seq: int | None,
    ) -> ApplyResult:
        # A base_rev the server has not reached yet cannot describe any state the
        # author observed; treating it as current would let a stale client skip
        # the per-node version check (the lane's only concurrency control).
        if not _is_int(base_rev) or base_rev > self.rev:
            return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="stale")
        if self._is_duplicate(author, base_rev, author_seq):
            return ApplyResult(status="duplicate", rev=self.rev, applied=[])
        existing = self.nodes.get(node_id)
        if existing is not None and existing.version > base_rev:
            return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="stale")
        if existing is None:
            if parent_id is not None and parent_id not in self.nodes:
                return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="orphan")
            if len(self.nodes) >= self._limits.max_nodes or len(text) > self._limits.max_node_text:
                return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="limit")
            new_parent = parent_id
        else:
            if len(text) > self._limits.max_node_text:
                return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="limit")
            new_parent = parent_id if parent_id is not None else existing.parent_id
        new_rev = self.rev + 1
        self.nodes[node_id] = Node(
            id=node_id, kind=kind, text=text, version=new_rev, parent_id=new_parent
        )
        self.rev = new_rev
        return ApplyResult(
            status="applied", rev=new_rev, applied=[{"id": node_id, "version": new_rev}]
        )

    def delete(
        self, *, base_rev: int, node_id: str, author: str, author_seq: int | None
    ) -> ApplyResult:
        # A base_rev the server has not reached yet cannot describe any state the
        # author observed; treating it as current would let a stale client skip
        # the per-node version check (the lane's only concurrency control).
        if not _is_int(base_rev) or base_rev > self.rev:
            return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="stale")
        if self._is_duplicate(author, base_rev, author_seq):
            return ApplyResult(status="duplicate", rev=self.rev, applied=[])
        existing = self.nodes.get(node_id)
        if existing is None:
            return ApplyResult(status="applied", rev=self.rev, applied=[])
        if existing.version > base_rev:
            return ApplyResult(status="rejected", rev=self.rev, applied=[], reason="stale")
        new_rev = self.rev + 1
        prefix = node_id + "."
        removed = [nid for nid in self.nodes if nid == node_id or nid.startswith(prefix)]
        for nid in removed:
            del self.nodes[nid]
        self.rev = new_rev
        applied = [{"id": nid, "version": new_rev} for nid in sorted(removed)]
        return ApplyResult(status="applied", rev=new_rev, applied=applied)

    # -- persistence ------------------------------------------------------- #

    def snapshot(self) -> dict:
        nodes = [
            {
                "id": node.id,
                "kind": node.kind,
                "text": node.text,
                "version": node.version,
                "parentId": node.parent_id,
            }
            for _, node in sorted(self.nodes.items(), key=lambda kv: kv[0])
        ]
        return {
            "rev": self.rev,
            "programText": self.program_text,
            "frame": self.frame,
            "nodes": nodes,
        }

    def load(self, data: dict) -> None:
        self.rev = data["rev"]
        self.program_text = data["programText"]
        self.frame = data.get("frame")
        self.nodes = {
            node["id"]: Node(
                id=node["id"],
                kind=node["kind"],
                text=node["text"],
                version=node["version"],
                parent_id=node.get("parentId"),
            )
            for node in data["nodes"]
        }
        self._author_seq.clear()
