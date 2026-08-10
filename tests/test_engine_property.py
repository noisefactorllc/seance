"""Seeded property / convergence tests over the pure engine (no I/O, no sockets).

For each seed ``0..19`` a fresh :class:`PolyDoc` acts as the authoritative SERVER
while four simulated authors fire 300 mixed proposals (upsert / delete / owner
snapshot) from deliberately stale bases, with duplicate ``author_seq`` resends. A
MIRROR client reconstructs the document from *only* the accepted-op broadcast
stream — the ``poly-ack`` / decorated broadcast the server would emit — and must
converge byte-for-byte on the server's :meth:`PolyDoc.snapshot`. A companion
:class:`StateLane` run asserts last-writer-wins under the server's total order for
three concurrent writers.

The seed is encoded in every test id (``…[seed-7]``) and repeated in each
assertion message, so a failure names the exact reproducing seed. These tests are
pure and synchronous — they never open a socket or touch the transport.
"""

from random import Random

import pytest

from app.config import Limits
from app.engine import ApplyResult, PolyDoc, StateLane

SEEDS = list(range(20))

M_AUTHORS = 4
POLY_OPS = 300
SNAPSHOT_EVERY = 50
STATE_WRITERS = ("w0", "w1", "w2")
STATE_OPS_PER_WRITER = 200

KINDS = ("Chain", "Call", "grp", "step", "param", "const")
PROGRAMS = ("", "chain { noise() }", "grid { a b }", "stack {}")
_TEXT_ALPHABET = "abcdefgABCDEFG .()0123{}"


def _pool() -> list[str]:
    """A 40-id pool with a dotted ``root.N`` / ``root.N.M`` hierarchy."""
    ids = ["root"]
    ids += [f"root.{i}" for i in range(10)]
    for i in range(3):
        ids += [f"root.{i}.{j}" for j in range(10)]
    return ids[:40]


POOL = _pool()


def _text(rng: Random) -> str:
    """A random node text of at most 64 characters."""
    return "".join(rng.choice(_TEXT_ALPHABET) for _ in range(rng.randint(0, 64)))


# --------------------------------------------------------------------------- #
# The mirror: a client rebuilt purely from the accepted-op broadcast stream
# --------------------------------------------------------------------------- #


class _Mirror:
    """Reconstructs the doc from the accepted-op stream only (never the proposals).

    Nodes are tracked as ``id -> (kind, text, version, parent_id)`` — exactly the
    fields a peer learns from a decorated broadcast — plus the running ``rev``.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, tuple[str, str, int, str | None]] = {}
        self.rev = 0

    def on_upsert(
        self, node_id: str, kind: str, text: str, version: int, rev: int, parent_id: str | None
    ) -> None:
        # The decorated broadcast carries the op's parentId verbatim; for an
        # already-known node a None parentId means "keep the current parent",
        # exactly as the server's upsert rule does, so the mirror stays converged.
        if parent_id is None and node_id in self.nodes:
            parent_id = self.nodes[node_id][3]
        self.nodes[node_id] = (kind, text, version, parent_id)
        self.rev = rev

    def on_delete(self, removed_ids: list[str], rev: int) -> None:
        for rid in removed_ids:
            self.nodes.pop(rid, None)
        self.rev = rev

    def on_snapshot(self, nodes: list[dict], rev: int) -> None:
        self.nodes = {
            n["id"]: (n["kind"], n["text"], rev, n.get("parentId")) for n in nodes
        }
        self.rev = rev


# --------------------------------------------------------------------------- #
# Proposal generation
# --------------------------------------------------------------------------- #


def _base_rev(rng: Random, rev: int) -> int:
    """A possibly-stale local base: current, one behind, or ancient."""
    pick = rng.random()
    if pick < 0.6:
        return rev
    if pick < 0.8:
        return max(0, rev - 1)
    return max(0, rev - rng.randint(2, 6))


def _parent(rng: Random, server: PolyDoc) -> str | None:
    """Pick a parent from existing-or-None, occasionally an absent pool id (orphan)."""
    pick = rng.random()
    if pick < 0.4 or not server.nodes:
        return None
    if pick < 0.8:
        return rng.choice(list(server.nodes))
    return rng.choice(POOL)


def _new_proposal(rng: Random, author: int, server: PolyDoc, seqs: dict[int, int]) -> dict:
    """A fresh upsert or delete proposal, advancing the author's own seq counter."""
    seqs[author] += 1
    seq = seqs[author]
    base = _base_rev(rng, server.rev)
    if rng.random() < 0.7:
        return {
            "op": "upsert",
            "base_rev": base,
            "node_id": rng.choice(POOL),
            "kind": rng.choice(KINDS),
            "text": _text(rng),
            "parent_id": _parent(rng, server),
            "author_seq": seq,
        }
    if server.nodes and rng.random() < 0.7:
        target = rng.choice(list(server.nodes))
    else:
        target = rng.choice(POOL)
    return {"op": "delete", "base_rev": base, "node_id": target, "author_seq": seq}


def _apply(server: PolyDoc, frame: dict, author: int) -> ApplyResult:
    """Feed one proposal into the pure engine and return its result."""
    key = f"author:{author}"
    if frame["op"] == "upsert":
        return server.upsert(
            base_rev=frame["base_rev"],
            node_id=frame["node_id"],
            kind=frame["kind"],
            text=frame["text"],
            parent_id=frame["parent_id"],
            author=key,
            author_seq=frame["author_seq"],
        )
    return server.delete(
        base_rev=frame["base_rev"],
        node_id=frame["node_id"],
        author=key,
        author_seq=frame["author_seq"],
    )


def _snapshot_doc(rng: Random) -> list[dict]:
    """A small owner-rebuilt document rooted at ``root`` (2–5 nodes)."""
    nodes = [{"id": "root", "kind": rng.choice(KINDS), "text": _text(rng), "parentId": None}]
    for i in range(3):
        if rng.random() < 0.8:
            nodes.append(
                {
                    "id": f"root.{i}",
                    "kind": rng.choice(KINDS),
                    "text": _text(rng),
                    "parentId": "root",
                }
            )
    if any(n["id"] == "root.0" for n in nodes) and rng.random() < 0.5:
        nodes.append(
            {"id": "root.0.0", "kind": rng.choice(KINDS), "text": _text(rng), "parentId": "root.0"}
        )
    return nodes


# --------------------------------------------------------------------------- #
# Per-op bookkeeping + invariant checks
# --------------------------------------------------------------------------- #


def _new_counts() -> dict[str, int]:
    return {
        "upsert_applied": 0,
        "upsert_rejected": 0,
        "upsert_duplicate": 0,
        "delete_applied": 0,
        "delete_noop": 0,
        "delete_rejected": 0,
        "delete_duplicate": 0,
        "snapshot": 0,
    }


def _record(
    result: ApplyResult,
    frame: dict,
    server: PolyDoc,
    mirror: _Mirror,
    counts: dict[str, int],
    old_rev: int,
    before: dict,
    limits: Limits,
    seed: int,
) -> None:
    """Advance the mirror from ``result`` and assert the per-op invariants.

    ``rev`` never decreases and moves by at most 1; rejected/duplicate ops leave
    the doc byte-identical; an applied delete strips the whole dotted subtree from
    both server and mirror; the node cap is never breached.
    """
    op = frame["op"]
    new_rev = server.rev
    assert new_rev >= old_rev, f"seed {seed}: rev decreased {old_rev}->{new_rev}"
    assert new_rev - old_rev in (0, 1), f"seed {seed}: rev jumped {old_rev}->{new_rev}"

    if result.status in ("duplicate", "rejected"):
        counts[f"{op}_{result.status}"] += 1
        assert new_rev == old_rev, f"seed {seed}: {result.status} bumped rev"
        assert server.snapshot() == before, f"seed {seed}: {result.status} mutated the doc"
    elif op == "upsert":
        counts["upsert_applied"] += 1
        assert new_rev == old_rev + 1, f"seed {seed}: applied upsert must +1 rev"
        mirror.on_upsert(
            frame["node_id"], frame["kind"], frame["text"],
            result.applied[0]["version"], new_rev, frame["parent_id"],
        )
    elif result.applied:  # a delete that removed one or more nodes
        counts["delete_applied"] += 1
        assert new_rev == old_rev + 1, f"seed {seed}: applied delete must +1 rev"
        nid = frame["node_id"]
        mirror.on_delete([a["id"] for a in result.applied], new_rev)
        assert all(
            not (k == nid or k.startswith(nid + ".")) for k in server.nodes
        ), f"seed {seed}: subtree of {nid} not fully removed on server"
        assert all(
            not (k == nid or k.startswith(nid + ".")) for k in mirror.nodes
        ), f"seed {seed}: subtree of {nid} not fully removed in mirror"
    else:  # delete of an unknown id: an applied no-op, rev unchanged
        counts["delete_noop"] += 1
        assert new_rev == old_rev, f"seed {seed}: no-op delete changed rev"
        mirror.rev = new_rev

    assert len(server.nodes) <= limits.max_nodes, f"seed {seed}: node cap exceeded"


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

_ROOT_FRAME = {
    "op": "upsert",
    "base_rev": 0,
    "node_id": "root",
    "kind": "Chain",
    "text": "noise()",
    "parent_id": None,
    "author_seq": 1,
}


def run_polydoc(seed: int) -> dict[str, int]:
    """Drive one seeded PolyDoc convergence run; return the op distribution."""
    rng = Random(seed)
    limits = Limits()
    server = PolyDoc(limits)
    mirror = _Mirror()
    counts = _new_counts()
    seqs = dict.fromkeys(range(M_AUTHORS), 0)
    last_frame: dict[int, dict] = {}

    for i in range(POLY_OPS):
        before = server.snapshot()
        old_rev = server.rev

        if i == 0:
            # Establish the root so the pool's dotted children have a valid parent.
            seqs[0] = 1
            result = _apply(server, dict(_ROOT_FRAME), 0)
            assert result.status == "applied", f"seed {seed}: root create must apply"
            _record(result, _ROOT_FRAME, server, mirror, counts, old_rev, before, limits, seed)
            continue
        if i == 1:
            # Deliberate exact resend of (base_rev, author_seq) with no snapshot
            # since — guaranteed to dedup, exercising the duplicate-drop path.
            result = _apply(server, dict(_ROOT_FRAME), 0)
            assert result.status == "duplicate", f"seed {seed}: op1 resend must dedup"
            _record(result, _ROOT_FRAME, server, mirror, counts, old_rev, before, limits, seed)
            continue
        if i % SNAPSHOT_EVERY == 0:
            nodes = _snapshot_doc(rng)
            new_rev = server.apply_snapshot(rng.choice(PROGRAMS), nodes, {"beat": i})
            counts["snapshot"] += 1
            assert new_rev == old_rev + 1, f"seed {seed}: snapshot must +1 rev"
            assert len(server.nodes) <= limits.max_nodes, f"seed {seed}: snapshot node cap"
            mirror.on_snapshot(nodes, new_rev)
            continue

        author = rng.randrange(M_AUTHORS)
        if author in last_frame and rng.random() < 0.15:
            frame = last_frame[author]  # occasional deliberate duplicate resend
        else:
            frame = _new_proposal(rng, author, server, seqs)
            last_frame[author] = frame
        result = _apply(server, frame, author)
        _record(result, frame, server, mirror, counts, old_rev, before, limits, seed)

    # Invariant (2): the mirror reproduces the server's document exactly.
    server_snap = server.snapshot()
    server_nodes = {
        n["id"]: (n["kind"], n["text"], n["version"], n["parentId"])
        for n in server_snap["nodes"]
    }
    assert mirror.nodes == server_nodes, f"seed {seed}: mirror diverged from server nodes"
    assert mirror.rev == server_snap["rev"], f"seed {seed}: mirror rev != server rev"
    return counts


def run_statelane(seed: int) -> int:
    """Drive one seeded StateLane LWW run; return the total op count."""
    rng = Random(seed + 1000)
    limits = Limits()
    server = StateLane(limits)
    shadow: dict[str, object] = {}  # independent last-write-wins oracle

    keys = [f"k{i}" for i in range(8)]

    schedule: list[str] = []
    for writer in STATE_WRITERS:
        schedule += [writer] * STATE_OPS_PER_WRITER
    rng.shuffle(schedule)  # a single server total order over interleaved writers

    for seq, writer in enumerate(schedule, start=1):
        key = rng.choice(keys)
        value = rng.randrange(1_000_000)
        server.apply(key, value, seq, by=writer)
        shadow[key] = value  # last write in server-arrival order wins

    # The shadow dict is the real LWW oracle: the server's map must equal the
    # last value written per key in server-arrival order.
    server_values = {e["id"]: e["value"] for e in server.snapshot()}
    assert server_values == shadow, f"seed {seed}: LWW last-write-wins mismatch"
    return len(schedule)


def run_polydoc_capped(seed: int) -> int:
    """Drive a small-cap (``max_nodes=6``) PolyDoc run; return the ``limit``-reject count.

    Every proposal is a flat (``parentId=None``) upsert at the current ``base_rev``,
    so neither the orphan nor the stale path is reachable: the only rejection is
    the node cap. Once the doc holds ``max_nodes`` distinct ids, an upsert of a
    fresh id is rejected ``limit`` and must not mutate the doc, and the cap is
    never exceeded.
    """
    rng = Random(seed)
    limits = Limits(max_nodes=6)
    server = PolyDoc(limits)
    pool = [f"n{i}" for i in range(20)]  # more ids than the cap -> new-id rejects
    limit_rejects = 0

    for seq in range(1, 201):
        before = server.snapshot()
        result = server.upsert(
            base_rev=server.rev,
            node_id=rng.choice(pool),
            kind="grp",
            text=f"t{seq}",
            parent_id=None,
            author="a",
            author_seq=seq,
        )
        assert len(server.nodes) <= limits.max_nodes, f"seed {seed}: node cap exceeded"
        if result.status == "rejected":
            assert result.reason == "limit", f"seed {seed}: unexpected reject {result.reason!r}"
            assert server.snapshot() == before, f"seed {seed}: rejected op mutated the doc"
            limit_rejects += 1

    return limit_rejects


# --------------------------------------------------------------------------- #
# Parametrized tests (seed printed in the test id)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed-{s}")
def test_polydoc_convergence(seed: int) -> None:
    counts = run_polydoc(seed)
    assert counts["upsert_applied"] >= 1, f"seed {seed}: no upsert applied"
    assert counts["upsert_duplicate"] >= 1, f"seed {seed}: dedup path never exercised"
    assert counts["snapshot"] == 5, f"seed {seed}: expected 5 owner snapshots"


@pytest.mark.parametrize("seed", SEEDS, ids=lambda s: f"seed-{s}")
def test_statelane_last_write_wins(seed: int) -> None:
    assert run_statelane(seed) == len(STATE_WRITERS) * STATE_OPS_PER_WRITER


def test_polydoc_node_cap_never_exceeded_and_limit_reject_reached() -> None:
    """A small-cap run drives the doc past ``max_nodes`` so at least one ``limit``
    reject is reachable, while the node cap is never exceeded (asserted inline)."""
    assert run_polydoc_capped(seed=7) >= 1
