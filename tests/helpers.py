"""Async integration harness for the end-to-end scenario suite (spec §16).

This module owns everything the scenario tests need to drive a *real* seance
server over *real* WebSocket clients:

* :class:`Peer` — an ``aiohttp`` WebSocket client wrapping one connection. Its
  :meth:`~Peer.connect` performs the ``hello`` handshake and collects the
  ``welcome`` + ``session-snapshot`` pair; a background reader task then drains
  every subsequent frame into :attr:`~Peer.inbox`. :meth:`~Peer.expect` waits for
  (and consumes) the next matching frame; :meth:`~Peer.drain_quiet` waits out a
  quiet period; :meth:`~Peer.barrier` round-trips a ``ping``/``pong`` so a caller
  knows every earlier frame it sent has been processed server-side.
* the :func:`world` fixture — a factory that builds fully-wired apps through
  :func:`app.main.create_app` (one ``SEANCE_SECRET`` per module so tokens and
  bans survive a stop/restart), wraps each in an ``aiohttp`` ``TestServer`` behind
  a ``TestClient``, and tears every app and peer down in the right order.
* HTTP shims (:func:`create_session`, :func:`mint_ticket`) and a member-directory
  seed (:func:`make_members_db`) plus a gs-cookie minter (:func:`gs_cookie`).

All real-socket waits are bounded with :class:`asyncio.timeout` (≤ 5 s); nothing
sleeps to advance logic, only to bound a socket read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from app.identity import GsSerializer
from app.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

ORIGIN = "http://testorigin"
_CLOSE_TYPES = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)

# One Fernet key for the whole module: a stop/restart (scenario d) rebuilds the
# app over the same DB, and an anon token minted by the first instance must still
# verify against the second — which only holds when both share this secret.
_SECRET = Fernet.generate_key().decode()


class PeerClosed(Exception):
    """The socket closed before an awaited frame arrived. ``close_code`` is the code.

    A refused join (banned/locked/guests) surfaces here: the server sends an
    ``error`` then closes, so ``await peer.connect(...)`` raises this with the
    protocol close code (4403 / 4423 / 4404 / …).
    """

    def __init__(self, close_code: int | None) -> None:
        super().__init__(f"socket closed (code={close_code})")
        self.close_code = close_code


class Peer:
    """One live WebSocket client: handshake, inbox, and bounded frame waits."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self.inbox: list[dict] = []
        self.welcome: dict | None = None
        self.snapshot: dict | None = None
        self.rev = 0
        self.close_code: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._event = asyncio.Event()
        self._consumed: set[int] = set()
        self._closed = False

    # -- identity views ---------------------------------------------------- #

    @property
    def user_id(self) -> str:
        assert self.welcome is not None, "peer has not completed its handshake"
        return self.welcome["you"]["user_id"]

    @property
    def username(self) -> str:
        assert self.welcome is not None, "peer has not completed its handshake"
        return self.welcome["you"]["username"]

    @property
    def is_owner(self) -> bool:
        assert self.welcome is not None, "peer has not completed its handshake"
        return self.welcome["you"]["is_owner"]

    # -- connection lifecycle ---------------------------------------------- #

    async def connect(
        self,
        server: TestServer,
        session_id: str,
        *,
        origin: str = ORIGIN,
        anon_token: str | None = None,
        ticket: str | None = None,
        cookies: dict[str, str] | None = None,
        protocol: int = 1,
        dialects: list[str] | None = None,
    ) -> Peer:
        """Open the socket, send ``hello``, and collect welcome + snapshot.

        ``dialects`` (when given) is sent as the ``hello`` ``dialects`` list
        (layers-dialect design §3); omitted, the connection is a legacy client
        (server-side default ``[DEFAULT_DIALECT]``).

        Raises :class:`PeerClosed` if the join is refused (the socket closes
        before a ``welcome`` arrives); its ``close_code`` is the protocol code.
        """
        headers = {"Origin": origin}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        # A dummy jar keeps the exact Cookie header we send: no server-set
        # SEANCE_ANON cookie can leak into a later request on this session.
        self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        url = server.make_url(f"/v1/sessions/{session_id}/ws")
        self._ws = await self._session.ws_connect(url, headers=headers)
        hello: dict = {"type": "hello", "protocol": protocol}
        if ticket is not None:
            hello["ticket"] = ticket
        if anon_token is not None:
            hello["anon_token"] = anon_token
        if dialects is not None:
            hello["dialects"] = dialects
        await self._ws.send_str(json.dumps(hello))
        self._reader = asyncio.create_task(self._read_loop())
        self.welcome = await self.expect("welcome", timeout=5.0)  # noqa: ASYNC109 (mandated API)
        self.snapshot = await self.expect("session-snapshot", timeout=5.0)  # noqa: ASYNC109
        return self

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                msg = await self._ws.receive()
                if msg.type is WSMsgType.TEXT:
                    frame = json.loads(msg.data)
                    self.inbox.append(frame)
                    self._absorb_rev(frame)
                    self._event.set()
                elif msg.type in _CLOSE_TYPES:
                    self.close_code = self._ws.close_code
                    self._closed = True
                    self._event.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._closed = True
            self._event.set()

    async def close(self) -> None:
        """Abort the connection so the server sees the disconnect at once (idempotent).

        The client session is closed rather than the WebSocket gracefully closed:
        an in-process graceful ``ws.close()`` half-closes and defers the
        server-side ``leave`` (and its ``user-parted`` / ``owner-changed`` fan-out)
        until this side also stops reading, which would strand a peer awaiting the
        handoff. Aborting the transport makes the server's read loop return at once.
        """
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._ws = None

    # -- sending ----------------------------------------------------------- #

    async def send(self, msg: dict) -> None:
        """Serialize and send one client frame."""
        assert self._ws is not None, "peer is not connected"
        await self._ws.send_str(json.dumps(msg))

    async def barrier(self, timeout: float = 3.0) -> None:  # noqa: ASYNC109 (mandated API)
        """Round-trip a ``ping``/``pong`` so all earlier sends are now applied.

        The server processes one connection's frames in order, so a ``pong``
        proves every frame this peer sent before the ``ping`` has been handled.
        """
        await self.send({"type": "ping"})
        await self.expect("pong", timeout=timeout)  # noqa: ASYNC109 (mandated API)

    async def session_state(self, timeout: float = 3.0) -> dict:  # noqa: ASYNC109 (mandated API)
        """Request and return a fresh ``session-snapshot`` for this peer."""
        await self.send({"type": "session-state"})
        snap = await self.expect("session-snapshot", timeout=timeout)  # noqa: ASYNC109
        self._absorb_rev(snap)
        return snap

    # -- receiving --------------------------------------------------------- #

    async def expect(
        self,
        type_: str,
        timeout: float = 3.0,  # noqa: ASYNC109 (mandated API)
        where: Callable[[dict], bool] | None = None,
    ) -> dict:
        """Wait for and consume the next unconsumed frame of ``type_``.

        ``where`` is an optional predicate the frame must also satisfy. Each frame
        is returned to at most one :meth:`expect` call (consumed by index), so
        interleaved frame types never mask one another. Raises
        :class:`PeerClosed` if the socket closes first, ``TimeoutError`` on
        deadline.
        """
        async with asyncio.timeout(timeout):
            while True:
                self._event.clear()
                found = self._scan(type_, where)
                if found is not None:
                    return found
                if self._closed:
                    raise PeerClosed(self.close_code)
                await self._event.wait()

    def _scan(self, type_: str, where: Callable[[dict], bool] | None) -> dict | None:
        for i, frame in enumerate(self.inbox):
            if i in self._consumed or frame.get("type") != type_:
                continue
            if where is not None and not where(frame):
                continue
            self._consumed.add(i)
            return frame
        return None

    async def drain_quiet(
        self, quiet: float = 0.3, timeout: float = 3.0  # noqa: ASYNC109 (mandated API)
    ) -> None:
        """Return once no new frame has arrived for ``quiet`` seconds."""
        async with asyncio.timeout(timeout):
            while True:
                self._event.clear()
                try:
                    async with asyncio.timeout(quiet):
                        await self._event.wait()
                except TimeoutError:
                    return

    def frames(self, type_: str) -> list[dict]:
        """All frames of ``type_`` currently in the inbox (consumed or not)."""
        return [f for f in self.inbox if f.get("type") == type_]

    def _absorb_rev(self, frame: dict) -> None:
        """Track the highest polydoc ``rev`` seen, for base_rev bookkeeping."""
        rev = frame.get("rev")
        if isinstance(rev, int) and not isinstance(rev, bool):
            self.rev = max(self.rev, rev)
        poly = frame.get("poly")
        if isinstance(poly, dict):
            prev = poly.get("rev")
            if isinstance(prev, int) and not isinstance(prev, bool):
                self.rev = max(self.rev, prev)


# --------------------------------------------------------------------------- #
# App harness
# --------------------------------------------------------------------------- #


class AppCtx:
    """A running app: its ``TestClient``, the ``TestServer``, and a clean close.

    ``close`` drives the ``TestClient`` shutdown, which runs the app's
    ``on_cleanup`` — the hub freezes every live session to the store — so a
    scenario can stop one instance and thaw the same DB in the next.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.server = client.server
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.client.close()


def build_env(db_path: str, **overrides: str) -> dict[str, str]:
    """The base env for a scenario app, with per-test overrides layered on top."""
    env = {
        "SEANCE_SECRET": _SECRET,
        "SEANCE_DB": db_path,
        "SEANCE_ALLOWED_ORIGINS": ORIGIN,
    }
    env.update(overrides)
    return env


async def create_session(
    ctx: AppCtx,
    *,
    origin: str = ORIGIN,
    cookies: dict[str, str] | None = None,
    snapshot: dict | None = None,
    dialect: str | None = None,
) -> tuple[str, str | None]:
    """POST /v1/sessions; return ``(session_id, anon_token)`` (token may be None).

    With no credential the server mints an anon identity as ``created_by`` and
    returns its token, so a peer that reconnects with that token is the owner.
    ``dialect`` (when given) is sent alongside ``snapshot`` in the JSON body
    (layers-dialect design §3); omitted, the session gets ``DEFAULT_DIALECT``.
    """
    headers = {"Origin": origin}
    kwargs: dict = {"headers": headers}
    if cookies is not None:
        kwargs["cookies"] = cookies
    if snapshot is not None or dialect is not None:
        request_body: dict = {}
        if snapshot is not None:
            request_body["snapshot"] = snapshot
        if dialect is not None:
            request_body["dialect"] = dialect
        headers["Content-Type"] = "application/json"
        kwargs["data"] = json.dumps(request_body)
    resp = await ctx.client.post("/v1/sessions", **kwargs)
    assert resp.status == 201, f"create_session failed: {resp.status} {await resp.text()}"
    body = await resp.json()
    return body["session_id"], body.get("anon_token")


async def mint_ticket(ctx: AppCtx, user_id: str, username: str) -> str:
    """POST /v1/ticket as a trusted (loopback) peer; return the redeemable ticket."""
    resp = await ctx.client.post(
        "/v1/ticket",
        headers={"X-GS-User-Id": user_id, "X-GS-Username": username},
    )
    assert resp.status == 200, f"mint_ticket failed: {resp.status} {await resp.text()}"
    return (await resp.json())["ticket"]


def make_members_db(path: str, members: list[tuple[str, str | None, int | None]]) -> None:
    """Create a gs-style ``members`` table at ``path`` and seed ``(id, username, deleted_at)``."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE members (id TEXT PRIMARY KEY, username TEXT, deleted_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO members (id, username, deleted_at) VALUES (?, ?, ?)", members
        )
        conn.commit()
    finally:
        conn.close()


def gs_cookie(key: bytes, user_id: str, client_ip: str = "127.0.0.1") -> str:
    """Mint a gs SESSION cookie binding ``user_id`` to ``client_ip`` (default time)."""
    return GsSerializer(key).dumps(f"{user_id};{client_ip}")


@pytest.fixture
async def world(tmp_path):
    """Factory for real apps and peers, torn down peers-first then apps-last."""
    ctxs: list[AppCtx] = []
    peers: list[Peer] = []
    counter = {"n": 0}

    async def make_app(*, db: str | None = None, **overrides: str) -> AppCtx:
        counter["n"] += 1
        db_path = db if db is not None else str(tmp_path / f"seance-{counter['n']}.db")
        app = await create_app(build_env(db_path, **overrides))
        client = TestClient(TestServer(app))
        await client.start_server()
        ctx = AppCtx(client)
        ctxs.append(ctx)
        return ctx

    def make_peer(name: str | None = None) -> Peer:
        peer = Peer(name)
        peers.append(peer)
        return peer

    yield SimpleNamespace(app=make_app, peer=make_peer, tmp_path=tmp_path)

    for peer in peers:
        await peer.close()
    for ctx in ctxs:
        await ctx.close()
