"""Shutdown: connected clients are closed ``1001`` at once and the app stops fast.

Without the ``on_shutdown`` close, aiohttp waits its full ``shutdown_timeout``
(60 s by default) for the WebSocket handlers, which never return on their own,
and the clients only learn about it from a heartbeat timeout.
"""

from __future__ import annotations

import time

import pytest

from tests.helpers import PeerClosed, create_session, world  # noqa: F401  (re-exported fixture)

_CLOSE_GOING_AWAY = 1001


async def test_shutdown_closes_connected_clients_going_away_promptly(world):  # noqa: F811
    ctx = await world.app()
    session_id, _ = await create_session(ctx)
    peer = await world.peer().connect(ctx.server, session_id)

    started = time.monotonic()
    await ctx.close()  # runs on_shutdown, aiohttp's handler wait, then on_cleanup
    elapsed = time.monotonic() - started

    with pytest.raises(PeerClosed) as closed:
        await peer.expect("never-sent", timeout=5.0)
    assert closed.value.close_code == _CLOSE_GOING_AWAY
    assert peer.close_code == _CLOSE_GOING_AWAY
    assert elapsed < 5.0, f"shutdown took {elapsed:.1f}s"
