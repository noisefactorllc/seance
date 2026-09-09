"""In-process rate limiting for the seance server.

Two complementary, pure, clock-injectable primitives:

* :class:`Bucket` / :class:`LaneLimiter` — token buckets applied per connection,
  one bucket per protocol lane (``fast``, ``proposal``, ``chat``, ``control``,
  ``snapshot``). A lane refills continuously at its configured rate up to its
  burst ceiling. :meth:`LaneLimiter.exhausted_since` reports how long a lane has
  been *continuously* refused, which the transport turns into the abuse rule:
  close ``4429`` once the streak exceeds ``limits.abuse_window``.
* :class:`KeyedLimiter` — keyed fixed-window counters for coarse per-IP and
  per-identity caps (anon mints, joins, session creates). Each key counts events
  inside a window that resets once ``window_secs`` elapse from its start. Memory
  is bounded: past ``max_keys`` entries, expired windows are dropped first, then
  the oldest windows.

No locks: every call is synchronous and the server runs a single event loop.
Time is injected as ``clock`` so tests advance logic time without sleeping; the
transport injects a monotonic clock, and a backwards step is treated as zero
elapsed time rather than as a debt.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.config import Limits


class Bucket:
    """A continuously-refilling token bucket.

    ``tokens`` starts full (``burst``) and refills at ``rate`` tokens/second,
    capped at ``burst``. :meth:`take` refills lazily from the elapsed time on
    each call, so no background task is needed.
    """

    __slots__ = ("burst", "clock", "last", "rate", "tokens")

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.time) -> None:
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.tokens = float(burst)
        self.last = clock()

    def take(self, n: int = 1) -> bool:
        """Consume ``n`` tokens; return ``True`` iff enough were available.

        Refills for the time elapsed since the previous call, then grants the
        request only if the post-refill balance covers ``n`` in full — there are
        no partial grants.

        A request for ``n > burst`` can never succeed, because the balance is
        capped at ``burst``. A backwards clock step (``now < last``) counts as
        zero elapsed time: it neither refills nor drains the bucket, so a
        wall-clock correction cannot turn every lane into a refusal streak.
        """
        now = self.clock()
        elapsed = max(0.0, now - self.last)
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def retry_after(self) -> float:
        """Seconds until at least one token exists, given the current balance.

        ``0.0`` when a token is already available; ``inf`` when the bucket cannot
        refill (``rate <= 0``).
        """
        if self.rate <= 0:
            return float("inf")
        return max(0.0, (1.0 - self.tokens) / self.rate)


class LaneLimiter:
    """One :class:`Bucket` per protocol lane for a single connection."""

    __slots__ = ("_buckets", "_exhausted_since", "clock")

    def __init__(self, limits: Limits, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._buckets: dict[str, Bucket] = {
            "fast": Bucket(limits.fast_rate, limits.fast_burst, clock),
            "proposal": Bucket(limits.proposal_rate, limits.proposal_burst, clock),
            "chat": Bucket(limits.chat_rate, limits.chat_burst, clock),
            "control": Bucket(limits.control_rate, limits.control_burst, clock),
            "snapshot": Bucket(limits.snapshot_rate, limits.snapshot_burst, clock),
        }
        self._exhausted_since: dict[str, float | None] = dict.fromkeys(self._buckets, None)

    def take(self, lane: str) -> bool:
        """Consume one token from ``lane``; unknown lane names raise ``KeyError``.

        Tracks the exhaustion streak: the first refusal stamps
        :meth:`exhausted_since`; any grant clears it.
        """
        granted = self._buckets[lane].take(1)
        if granted:
            self._exhausted_since[lane] = None
        elif self._exhausted_since[lane] is None:
            self._exhausted_since[lane] = self.clock()
        return granted

    def retry_after(self, lane: str) -> float:
        """Seconds until ``lane`` can grant again; unknown lane raises ``KeyError``."""
        return self._buckets[lane].retry_after()

    def exhausted_since(self, lane: str) -> float | None:
        """Timestamp of the first refusal in the current continuous-refusal streak.

        ``None`` whenever the most recent :meth:`take` for ``lane`` succeeded, or
        when no refusal has occurred yet. Unknown lane names raise ``KeyError``.
        """
        return self._exhausted_since[lane]


class KeyedLimiter:
    """Fixed-window event counter keyed by IP or identity.

    Each key accrues a count inside a window that opens on its first event and
    resets once ``window_secs`` have elapsed from that start. The key set is
    bounded to ``max_keys``: on overflow, keys whose window has expired are
    dropped first, then the oldest windows, until the cap is met.
    """

    __slots__ = ("_state", "clock", "limit", "max_keys", "window_secs")

    def __init__(
        self,
        limit: int,
        window_secs: float,
        clock: Callable[[], float] = time.time,
        max_keys: int = 100_000,
    ) -> None:
        self.limit = limit
        self.window_secs = window_secs
        self.clock = clock
        self.max_keys = max_keys
        self._state: dict[str, tuple[float, int]] = {}

    def take(self, key: str) -> bool:
        """Count one event for ``key``; return ``True`` iff under ``limit`` this window."""
        now = self.clock()
        entry = self._state.get(key)
        if entry is None or now - entry[0] >= self.window_secs:
            window_start, count = now, 0
        else:
            window_start, count = entry
        granted = count < self.limit
        if granted:
            count += 1
        self._state[key] = (window_start, count)
        if len(self._state) > self.max_keys:
            self._evict(now)
        return granted

    def _evict(self, now: float) -> None:
        """Bound the key set: drop expired windows, then the oldest, to ``max_keys``."""
        expired = [k for k, (start, _) in self._state.items() if now - start >= self.window_secs]
        for key in expired:
            del self._state[key]
        overflow = len(self._state) - self.max_keys
        if overflow <= 0:
            return
        oldest = sorted(self._state.items(), key=lambda item: item[1][0])[:overflow]
        for key, _ in oldest:
            del self._state[key]
