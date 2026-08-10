"""Seeded property tests for the pure text document engine."""

from random import Random

import pytest

from app.config import Limits
from app.textdoc import TextDocCollection, TextDocStaleWindow, TextEdit

SEEDS = list(range(10))
OPS_PER_SEED = 150
ALPHABET = "abcdefXYZ 123()[]"


def _collection() -> TextDocCollection:
    return TextDocCollection(Limits(max_doc_oplog=32, max_doc_oplog_bytes=4096))


def _doc_snapshot(collection: TextDocCollection) -> dict:
    return collection.snapshot()[0]


def _apply(text: str, edit: TextEdit) -> str:
    return text[: edit.start] + edit.text + text[edit.end :]


def _base_rev(rng: Random, current_rev: int) -> int:
    if current_rev == 0:
        return 0
    pick = rng.random()
    if pick < 0.55:
        return current_rev
    if pick < 0.85:
        return rng.randint(max(0, current_rev - 5), current_rev)
    return rng.randint(0, current_rev)


def _random_text(rng: Random, *, allow_empty: bool = False) -> str:
    low = 0 if allow_empty else 1
    size = rng.randint(low, 6)
    return "".join(rng.choice(ALPHABET) for _ in range(size))


def _random_edit(rng: Random, text: str) -> TextEdit:
    if not text or rng.random() < 0.35:
        pos = rng.randint(0, len(text))
        return TextEdit(pos, pos, _random_text(rng))
    start = rng.randint(0, len(text) - 1)
    end = rng.randint(start + 1, len(text))
    if rng.random() < 0.4:
        return TextEdit(start, end, "")
    return TextEdit(start, end, _random_text(rng, allow_empty=True))


@pytest.mark.parametrize("seed", SEEDS, ids=[f"seed-{seed}" for seed in SEEDS])
def test_textdoc_accepted_edits_match_sequential_oracle(seed: int) -> None:
    rng = Random(seed)
    docs = _collection()
    docs.create_doc("main", "Program", "dsl", "seed", default=True)

    oracle = "seed"
    texts_by_rev = ["seed"]

    for author_seq in range(1, OPS_PER_SEED + 1):
        current = _doc_snapshot(docs)
        before_rev = current["rev"]
        before_text = current["text"]
        base_rev = _base_rev(rng, before_rev)
        base_text = texts_by_rev[base_rev]
        proposal = _random_edit(rng, base_text)

        try:
            accepted = docs.apply_edit(
                "main",
                base_rev=base_rev,
                edit=proposal,
                author_id=f"user-{seed % 3}",
                connection_id=f"conn-{seed % 2}",
                author_seq=author_seq,
            )
        except TextDocStaleWindow:
            assert _doc_snapshot(docs)["text"] == before_text, f"seed {seed}: reject mutated text"
            assert _doc_snapshot(docs)["rev"] == before_rev, f"seed {seed}: reject bumped rev"
            continue

        oracle = _apply(oracle, accepted.edit)
        snap = _doc_snapshot(docs)
        assert accepted.rev == before_rev + 1, f"seed {seed}: accepted rev did not increment"
        assert snap["text"] == oracle, f"seed {seed}: canonical edit diverged from oracle"
        assert snap["rev"] == accepted.rev, f"seed {seed}: snapshot rev mismatch"
        texts_by_rev.append(snap["text"])
