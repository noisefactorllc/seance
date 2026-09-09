"""Tests for the rate-limiting primitives (app.ratelimit).

All timing is driven by the injected ``clock`` FakeClock fixture (advance logic
time, never sleep). Eviction tests peek at ``KeyedLimiter._state`` to assert the
bounded key set directly, mirroring how tests/test_store.py inspects internals.
"""

import pytest

from app.config import Limits
from app.ratelimit import Bucket, KeyedLimiter, LaneLimiter

# --- Bucket -----------------------------------------------------------------


def test_bucket_burst_then_refusal(clock):
    bucket = Bucket(rate=1.0, burst=3, clock=clock)
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False  # burst exhausted, no refill yet


def test_bucket_take_n_is_all_or_nothing(clock):
    bucket = Bucket(rate=1.0, burst=5, clock=clock)
    assert bucket.take(3) is True  # 5 -> 2
    assert bucket.take(3) is False  # 2 < 3, no partial consumption
    assert bucket.take(2) is True  # 2 -> 0, proves the refusal took nothing


def test_bucket_refills_after_advance(clock):
    bucket = Bucket(rate=2.0, burst=4, clock=clock)
    assert bucket.take(4) is True
    assert bucket.take() is False
    clock.advance(1.0)  # 1s * 2/s = 2 tokens
    assert bucket.take() is True
    assert bucket.take() is True
    assert bucket.take() is False


def test_bucket_refill_capped_at_burst(clock):
    bucket = Bucket(rate=10.0, burst=5, clock=clock)
    assert bucket.take(5) is True
    clock.advance(100.0)  # would add 1000 tokens; must cap at burst=5
    assert bucket.take(5) is True
    assert bucket.take() is False


def test_bucket_retry_after_empty(clock):
    bucket = Bucket(rate=1.0, burst=2, clock=clock)
    assert bucket.take(2) is True
    assert bucket.take() is False
    assert bucket.retry_after() == pytest.approx(1.0)  # (1 - 0) / 1


def test_bucket_retry_after_partial(clock):
    bucket = Bucket(rate=2.0, burst=4, clock=clock)
    assert bucket.take(4) is True
    clock.advance(0.25)  # refills 0.5 token
    assert bucket.take() is False  # 0.5 < 1, balance stays 0.5
    assert bucket.retry_after() == pytest.approx(0.25)  # (1 - 0.5) / 2


def test_bucket_retry_after_zero_when_available(clock):
    bucket = Bucket(rate=1.0, burst=2, clock=clock)
    assert bucket.retry_after() == 0.0  # full bucket -> token already present


def test_bucket_retry_after_inf_when_rate_zero(clock):
    bucket = Bucket(rate=0.0, burst=1, clock=clock)
    assert bucket.take() is True
    assert bucket.take() is False
    assert bucket.retry_after() == float("inf")  # cannot ever refill


# --- LaneLimiter ------------------------------------------------------------


def _drain_lane(limiter: LaneLimiter, lane: str, burst: int) -> None:
    for _ in range(burst):
        assert limiter.take(lane) is True
    assert limiter.take(lane) is False


def test_lane_limiter_maps_each_lane_to_its_limits(clock):
    limits = Limits(
        fast_rate=1.0,
        fast_burst=2,
        proposal_rate=1.0,
        proposal_burst=3,
        chat_rate=1.0,
        chat_burst=4,
        control_rate=1.0,
        control_burst=5,
        snapshot_rate=1.0,
        snapshot_burst=6,
    )
    limiter = LaneLimiter(limits, clock)
    # Each lane drains exactly its own burst (independent buckets, static clock).
    _drain_lane(limiter, "fast", 2)
    _drain_lane(limiter, "proposal", 3)
    _drain_lane(limiter, "chat", 4)
    _drain_lane(limiter, "control", 5)
    _drain_lane(limiter, "snapshot", 6)


def test_lane_limiter_unknown_lane_raises(clock):
    limiter = LaneLimiter(Limits(), clock)
    with pytest.raises(KeyError):
        limiter.take("bogus")


def test_lane_limiter_retry_after_delegates(clock):
    limiter = LaneLimiter(Limits(chat_rate=1.0, chat_burst=1), clock)
    assert limiter.take("chat") is True
    assert limiter.take("chat") is False
    assert limiter.retry_after("chat") == pytest.approx(1.0)


def test_lane_exhausted_since_tracks_first_refusal(clock):
    limiter = LaneLimiter(Limits(fast_rate=1.0, fast_burst=2), clock)
    assert limiter.exhausted_since("fast") is None  # nothing refused yet
    assert limiter.take("fast") is True
    assert limiter.take("fast") is True
    assert limiter.exhausted_since("fast") is None  # last take succeeded

    first_refusal = clock()
    assert limiter.take("fast") is False
    assert limiter.exhausted_since("fast") == first_refusal  # streak opens

    clock.advance(0.5)
    assert limiter.take("fast") is False
    # Continuous streak -> still the FIRST refusal timestamp, not the latest.
    assert limiter.exhausted_since("fast") == first_refusal

    clock.advance(10.0)  # refill past burst
    assert limiter.take("fast") is True
    assert limiter.exhausted_since("fast") is None  # success clears the streak


def test_lane_exhausted_since_independent_per_lane(clock):
    limiter = LaneLimiter(
        Limits(chat_rate=1.0, chat_burst=1, fast_rate=1.0, fast_burst=1), clock
    )
    assert limiter.take("chat") is True
    assert limiter.take("chat") is False
    assert limiter.exhausted_since("chat") == clock()
    # An untouched lane is unaffected by another lane's exhaustion.
    assert limiter.exhausted_since("fast") is None
    assert limiter.take("fast") is True
    assert limiter.exhausted_since("fast") is None


def test_lane_exhausted_since_restamps_after_recovery(clock):
    limiter = LaneLimiter(Limits(fast_rate=1.0, fast_burst=1), clock)
    # exhaust the lane -> the first refusal stamps the streak at the current clock
    assert limiter.take("fast") is True
    first_refusal = clock()
    assert limiter.take("fast") is False
    assert limiter.exhausted_since("fast") == first_refusal

    # advance to refill; the next take succeeds and clears the streak
    clock.advance(5.0)
    assert limiter.take("fast") is True
    assert limiter.exhausted_since("fast") is None

    # the very next take (no refill available) refuses again, opening a NEW streak
    # stamped at the LATER clock value — not the original first_refusal.
    second_refusal = clock()
    assert limiter.take("fast") is False
    assert limiter.exhausted_since("fast") == second_refusal
    assert second_refusal != first_refusal


# --- KeyedLimiter -----------------------------------------------------------


def test_keyed_limit_within_window(clock):
    limiter = KeyedLimiter(limit=2, window_secs=10.0, clock=clock)
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is False  # third event in the window is over cap
    assert limiter.take("ip2") is True  # a different key has its own window


def test_keyed_window_rollover(clock):
    limiter = KeyedLimiter(limit=2, window_secs=10.0, clock=clock)
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is False
    clock.advance(9.0)
    assert limiter.take("ip1") is False  # still inside the original window
    clock.advance(1.0)  # now exactly window_secs from the start -> reset
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is True
    assert limiter.take("ip1") is False


def test_keyed_eviction_drops_expired_first(clock):
    limiter = KeyedLimiter(limit=5, window_secs=10.0, clock=clock, max_keys=2)
    limiter.take("a")
    limiter.take("b")
    clock.advance(11.0)  # a and b windows are now expired
    limiter.take("c")  # len -> 3 > max_keys, expired keys dropped first
    assert "a" not in limiter._state
    assert "b" not in limiter._state
    assert "c" in limiter._state


def test_keyed_eviction_drops_oldest_when_unexpired(clock):
    limiter = KeyedLimiter(limit=5, window_secs=100.0, clock=clock, max_keys=2)
    limiter.take("a")  # window_start t+0
    clock.advance(1.0)
    limiter.take("b")  # window_start t+1
    clock.advance(1.0)
    limiter.take("c")  # window_start t+2; len -> 3, nothing expired -> drop oldest
    assert "a" not in limiter._state  # oldest window_start evicted
    assert "b" in limiter._state
    assert "c" in limiter._state
    assert len(limiter._state) == 2


def test_keyed_reopened_window_moves_the_key_to_the_back(clock):
    """Eviction pops the front, so a fresh window has to become the newest entry."""
    limiter = KeyedLimiter(limit=5, window_secs=10.0, clock=clock, max_keys=100)
    limiter.take("a")
    clock.advance(1.0)
    limiter.take("b")
    clock.advance(20.0)
    limiter.take("a")  # a's window reopens: it is now the newest, not the oldest

    assert list(limiter._state) == ["b", "a"]
    starts = [start for start, _ in limiter._state.values()]
    assert starts == sorted(starts)


def test_keyed_eviction_keeps_the_newest_windows_under_key_churn(clock):
    """A burst of fresh keys must not cost more than the keys it drops."""
    limiter = KeyedLimiter(limit=5, window_secs=1_000.0, clock=clock, max_keys=50)
    for n in range(50):
        clock.advance(1.0)
        limiter.take(f"old-{n}")
    for n in range(200):
        clock.advance(1.0)
        limiter.take(f"new-{n}")

    assert len(limiter._state) == 50
    assert list(limiter._state) == [f"new-{n}" for n in range(150, 200)]
    starts = [start for start, _ in limiter._state.values()]
    assert starts == sorted(starts)


# --- default clock wiring ---------------------------------------------------


def test_default_clock_grants_initial_burst():
    # No injected clock: real time.time backs the default and the initial burst
    # is available immediately (no sleeping required).
    assert Bucket(rate=1.0, burst=1).take() is True
    assert LaneLimiter(Limits(chat_burst=1)).take("chat") is True
    assert KeyedLimiter(limit=1, window_secs=60.0).take("k") is True


def test_bucket_backwards_clock_step_counts_as_zero_elapsed(clock):
    """A wall-clock correction must not drive a lane into a refusal streak."""
    bucket = Bucket(rate=60.0, burst=120, clock=clock)
    for _ in range(10):
        assert bucket.take()
    clock.advance(-30.0)
    assert bucket.take()  # not fail-closed for the next 30 s
    assert bucket.tokens == pytest.approx(109.0)
    clock.advance(1.0)  # refills from the stepped-back instant onward
    assert bucket.take()
    assert bucket.tokens == pytest.approx(119.0)


def test_lane_limiter_no_exhaustion_streak_after_backwards_step(clock):
    limiter = LaneLimiter(Limits(), clock)
    for _ in range(10):
        assert limiter.take("fast")
    clock.advance(-30.0)
    assert limiter.take("fast")
    assert limiter.exhausted_since("fast") is None
