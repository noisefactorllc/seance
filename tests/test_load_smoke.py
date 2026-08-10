"""Load smoke test (spec §16, marked ``slow``): 20 clients, one session, 20 s.

Excluded from the default suite by the ``-m 'not slow'`` addopts; run explicitly:

    .venv/bin/python -m pytest tests/test_load_smoke.py -m slow -q

It stands up one real app through :func:`app.main.create_app` (via the
:func:`tests.helpers.world` factory), opens ONE session, and drives 20 real
WebSocket clients that each emit 30 fast-lane ``state-update`` ops/s for 20 s
(600 ops each, paced to stay well under the fast-lane burst). Every op's value
carries the sender's monotonic send time; because every client shares this one
process, the observer's arrival timestamps are directly comparable to those send
times, so relay latency is measured without a shared wall clock.

Assertions (spec §16 load-smoke bar):

* zero unexpected disconnects — all 20 sockets are still open at the end;
* convergence — every client's final ``session-state`` snapshot is byte-identical
  (full state/data/poly/chat equality across all 20);
* observer p99 relay latency < 250 ms over a sampled subset of >= 1000 frames;
* the hub's ``connection_count`` (read through ``GET /v1/stats``, which reports
  exactly that value) returns to 0 within 5 s of every client closing.

Total runtime is ~25-30 s. Every real-socket wait is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import aiohttp
import pytest
from aiohttp import WSMsgType

from tests.helpers import ORIGIN, create_session, world  # noqa: F401  (re-exported fixture)

_CLOSE_TYPES = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)

NUM_CLIENTS = 20
OPS_PER_SEC = 30
DURATION_S = 20
OPS_PER_CLIENT = OPS_PER_SEC * DURATION_S  # 600
P99_BUDGET_S = 0.250
MIN_LATENCY_SAMPLES = 1000
HANDSHAKE_TIMEOUT_S = 5.0
DRAIN_TIMEOUT_S = 10.0
DISCONNECT_POLL_S = 5.0

# Generous limits so the smoke exercises throughput, not the guardrails: room for
# 20 distinct users (default max_clients is 16), a send queue deep enough that a
# momentary writer lag never trips the 4408 slow-consumer close, and heartbeat /
# join windows pushed out of the way for the run.
_LOAD_ENV = {
    "SEANCE_STATS_ENABLED": "1",
    "SEANCE_LIMIT_MAX_CLIENTS": "32",
    "SEANCE_LIMIT_SEND_QUEUE_FRAMES": "1000000",
    "SEANCE_LIMIT_SEND_QUEUE_BYTES": "1073741824",
    "SEANCE_LIMIT_JOINS_PER_IP_MIN": "1000",
    "SEANCE_LIMIT_PING_INTERVAL": "600",
    "SEANCE_LIMIT_PING_TIMEOUT": "600",
}


class LoadClient:
    """A minimal real WebSocket client: handshake, background reader, paced sender.

    ``arrivals`` records ``(recv_monotonic, send_monotonic)`` pairs for every
    relayed ``state-update`` (recorded only when ``record_latency`` is set, i.e.
    for the single observer). ``session_state`` round-trips a fresh snapshot,
    which — since the server processes one connection's frames in order — also
    proves every earlier op from this client has been applied.
    """

    def __init__(self, index: int, *, record_latency: bool = False) -> None:
        self.index = index
        self.state_id = f"k{index}"
        self.record_latency = record_latency
        self.arrivals: list[tuple[float, float]] = []
        self.closed = False
        self.close_code: int | None = None
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._snapshot: dict | None = None
        self._snap_event = asyncio.Event()

    async def connect(self, server, session_id: str) -> LoadClient:
        url = server.make_url(f"/v1/sessions/{session_id}/ws")
        self._ws = await self._session.ws_connect(url, headers={"Origin": ORIGIN})
        await self._ws.send_str(json.dumps({"type": "hello", "protocol": 1}))
        await self._read_until("welcome")
        await self._read_until("session-snapshot")
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def _read_until(self, mtype: str) -> dict:
        assert self._ws is not None
        async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
            while True:
                msg = await self._ws.receive()
                if msg.type is WSMsgType.TEXT:
                    frame = json.loads(msg.data)
                    if frame.get("type") == mtype:
                        return frame
                elif msg.type in _CLOSE_TYPES:
                    raise AssertionError(
                        f"client {self.index} socket closed "
                        f"(code={self._ws.close_code}) awaiting {mtype!r}"
                    )

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                msg = await self._ws.receive()
                if msg.type is WSMsgType.TEXT:
                    now = time.monotonic()
                    frame = json.loads(msg.data)
                    ftype = frame.get("type")
                    if ftype == "state-update":
                        if self.record_latency:
                            value = frame.get("value")
                            if isinstance(value, dict) and "t" in value:
                                self.arrivals.append((now, value["t"]))
                    elif ftype == "session-snapshot":
                        self._snapshot = frame
                        self._snap_event.set()
                elif msg.type in _CLOSE_TYPES:
                    self.closed = True
                    self.close_code = self._ws.close_code
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.closed = True

    async def run_sender(self) -> None:
        """Emit ``OPS_PER_CLIENT`` state-updates paced at ``OPS_PER_SEC``/s."""
        assert self._ws is not None
        loop = asyncio.get_running_loop()
        interval = 1.0 / OPS_PER_SEC
        start = loop.time()
        for i in range(OPS_PER_CLIENT):
            payload = {
                "type": "state-update",
                "id": self.state_id,
                "value": {"t": time.monotonic(), "i": i, "c": self.index},
            }
            await self._ws.send_str(json.dumps(payload))
            delay = (start + (i + 1) * interval) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def session_state(self) -> dict:
        """Request a fresh snapshot and return it (also a per-connection barrier)."""
        assert self._ws is not None
        self._snap_event.clear()
        await self._ws.send_str(json.dumps({"type": "session-state"}))
        async with asyncio.timeout(DRAIN_TIMEOUT_S):
            await self._snap_event.wait()
        assert self._snapshot is not None
        return self._snapshot

    async def close(self) -> None:
        """Abort the transport so the server observes the disconnect immediately.

        The client session is closed rather than a graceful ``ws.close()`` — a
        graceful close half-closes and defers the server-side leave, whereas
        aborting makes the server read loop return at once (see helpers.Peer).
        """
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._ws = None


def _comparable(snapshot: dict) -> dict:
    """The order-stable, recipient-independent content of a session-snapshot."""
    return {key: snapshot[key] for key in ("state", "data", "poly", "chat")}


async def _connections(ctx) -> int:
    """The hub's live connection count, read through the public stats endpoint."""
    resp = await ctx.client.get("/v1/stats")
    assert resp.status == 200
    body = await resp.json()
    return body["connections"]


@pytest.mark.slow
async def test_load_smoke_20_clients(world):  # noqa: F811
    ctx = await world.app(**_LOAD_ENV)
    session_id, _owner_token = await create_session(ctx)

    clients = [LoadClient(i, record_latency=(i == 0)) for i in range(NUM_CLIENTS)]
    for client in clients:
        await client.connect(ctx.server, session_id)
    observer = clients[0]

    try:
        # 20 clients emit concurrently for DURATION_S seconds.
        async with asyncio.timeout(DURATION_S + DRAIN_TIMEOUT_S):
            await asyncio.gather(*(client.run_sender() for client in clients))

        # First round settles the server: once every client's own session-state
        # has returned, the server has applied all 20 x 600 ops (per-connection
        # in-order processing). The second round then captures identical
        # snapshots, since no writes occur in between.
        await asyncio.gather(*(client.session_state() for client in clients))
        snapshots = await asyncio.gather(*(client.session_state() for client in clients))

        # Zero unexpected disconnects: every socket is still open.
        assert all(not client.closed for client in clients), [
            (c.index, c.close_code) for c in clients if c.closed
        ]

        # Convergence: every client's final snapshot is byte-identical.
        expected = _comparable(snapshots[0])
        assert len(expected["state"]) == NUM_CLIENTS
        for i, snapshot in enumerate(snapshots):
            assert _comparable(snapshot) == expected, f"client {i} diverged"

        # Observer p99 relay latency over a sampled subset of >= 1000 frames.
        latencies = sorted(recv - sent for recv, sent in observer.arrivals)
        n = len(latencies)
        assert n >= MIN_LATENCY_SAMPLES, f"only {n} latency samples"
        p99 = latencies[min(n - 1, int(n * 0.99))]
        print(  # surfaced under -s for the run report
            f"\n[load-smoke] samples={n} "
            f"p50={latencies[n // 2] * 1000:.1f}ms "
            f"p99={p99 * 1000:.1f}ms max={latencies[-1] * 1000:.1f}ms"
        )
        assert p99 < P99_BUDGET_S, f"p99 {p99 * 1000:.1f}ms exceeds budget"
    finally:
        for client in clients:
            await client.close()

    # The hub's connection_count returns to 0 within the bound after all close.
    # A bounded poll of the server-side teardown: there is no client-visible event
    # to await, so the stats endpoint is sampled until it reports zero.
    async with asyncio.timeout(DISCONNECT_POLL_S + 1.0):
        while await _connections(ctx) != 0:  # noqa: ASYNC110 (bounded poll of teardown)
            await asyncio.sleep(0.1)
