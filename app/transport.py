"""The WebSocket transport — seance's live edge over the pure Session core.

This module owns everything between a raw upgrade request and the synchronous
:class:`app.session.Session` state machine:

* the **handshake** — Origin allowlisting and a per-IP join limiter (both
  answered pre-upgrade with plain HTTP), the ``hello`` first-frame contract, and
  identity resolution — culminating in :meth:`Hub.connect`;
* :class:`WsConn` — the :class:`app.session.ConnLike` a Session drives. Its
  ``send_json`` is synchronous and non-blocking: it serializes once and enqueues
  into a bounded buffer that a dedicated writer task drains. On overflow it first
  sheds stale cursor frames (keeping only the newest per user and cursor type),
  then closes ``4408`` if still over budget. It injects a freshly-minted anon
  token into the first ``welcome`` exactly once;
* the **read loop** — frame parsing with the snapshot/normal size split, the
  per-lane token-bucket limiter with the sustained-abuse ``4429`` rule, the
  violation counter with the ``4400`` cutoff, and dispatch into ``Session.handle``;
* the **heartbeat** — an explicit ``ping`` loop (aiohttp autoping is off) with a
  pong-liveness deadline; and
* deterministic **teardown** — the writer flushes any queued close frame, the
  helper tasks are stopped, and the hub is told the connection left.

Time is injected as ``clock``. Tokens, cookies, and frame payloads are never
logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from app import protocol
from app.clientip import resolve_client_ip
from app.identity import AuthError
from app.protocol import ErrorCode, ProtocolError
from app.ratelimit import KeyedLimiter, LaneLimiter
from app.session import JoinRefused

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.config import Config, Limits
    from app.hub import Hub
    from app.identity import Identity, IdentityService
    from app.session import Session

_LOG = logging.getLogger("seance.transport")

_HELLO_TIMEOUT = 10.0
_JOIN_WINDOW = 60.0
_TEARDOWN_TIMEOUT = 5.0
_RATE_ERROR_THROTTLE = 1.0
_CLOSE_HEARTBEAT = 1011  # RFC 6455 "internal error / going away" for a dead peer
_MAX_CLOSE_REASON = 123  # RFC 6455 control-frame payload ceiling


# --------------------------------------------------------------------------- #
# Buffered send queue items
# --------------------------------------------------------------------------- #


class _Frame:
    """A serialized outbound frame retained with the metadata shedding needs."""

    __slots__ = ("mtype", "nbytes", "text", "user_id")

    def __init__(self, text: str, nbytes: int, mtype: str | None, user_id: str) -> None:
        self.text = text
        self.nbytes = nbytes
        self.mtype = mtype
        self.user_id = user_id


class _CloseSentinel:
    """A queued instruction telling the writer to close the socket, then stop."""

    __slots__ = ("code", "reason")

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------- #
# WsConn
# --------------------------------------------------------------------------- #


class WsConn:
    """A live WebSocket connection implementing the Session ``ConnLike`` contract.

    ``send_json`` is synchronous and never blocks the caller (the Session runs it
    inline during fan-out): it serializes the frame once and appends it to a
    :class:`collections.deque` bounded by ``send_queue_frames`` and
    ``send_queue_bytes``. A single :meth:`run_writer` task drains the buffer with
    ``ws.send_str``. On overflow the queue first sheds superseded cursor frames
    — keeping only the newest per originator and cursor type — and, if still
    over either budget, schedules a ``4408`` close and refuses the frame.
    """

    def __init__(
        self,
        ws: web.WebSocketResponse,
        identity: Identity,
        connection_id: str,
        limits: Limits,
        clock: Callable[[], float],
    ) -> None:
        self._ws = ws
        self.identity = identity
        self.connection_id = connection_id
        self._clock = clock
        self._max_frames = limits.send_queue_frames
        self._max_bytes = limits.send_queue_bytes
        self._buffer: deque[_Frame | _CloseSentinel] = deque()
        self._event = asyncio.Event()
        self._queued_bytes = 0
        self._closing = False
        self._stopped = False
        self._close_info: tuple[int, str] | None = None
        self.pending_anon_token: str | None = None
        # Set from hello.get("dialects") right after construction; None = a
        # legacy client that declared no dialects (Session.join treats this as
        # [DEFAULT_DIALECT]).
        self.declared_dialects: list[str] | None = None
        self.last_pong = clock()

    # -- introspection (used by the transport + tests) --------------------- #

    @property
    def closing(self) -> bool:
        """True once a close has been scheduled (or the writer failed)."""
        return self._closing

    @property
    def close_info(self) -> tuple[int, str] | None:
        """The scheduled ``(code, reason)`` close, or ``None`` if not closing."""
        return self._close_info

    def queued_frames(self) -> list[dict]:
        """Decode the currently-buffered data frames (close sentinels excluded)."""
        return [json.loads(item.text) for item in self._buffer if isinstance(item, _Frame)]

    # -- ConnLike ---------------------------------------------------------- #

    def send_json(self, msg: dict) -> bool:
        """Enqueue ``msg`` for delivery; return ``False`` if the socket is closing.

        Injects a pending anon token into the first ``welcome`` (once), serializes
        once, and enforces the queue budget with cursor shedding then a ``4408``
        close. Never blocks and never raises.
        """
        if self._closing:
            return False
        if self.pending_anon_token is not None and msg.get("type") == "welcome":
            msg = dict(msg)
            msg["anon_token"] = self.pending_anon_token
            self.pending_anon_token = None
        text = json.dumps(msg, separators=(",", ":"))
        nbytes = len(text.encode("utf-8"))
        if self._would_overflow(nbytes):
            self._compact_cursors()
            if self._would_overflow(nbytes):
                self.close_soon(protocol.CLOSE_SLOW, "slow consumer")
                return False
        self._buffer.append(_Frame(text, nbytes, msg.get("type"), msg.get("user_id", "")))
        self._queued_bytes += nbytes
        self._event.set()
        return True

    def close_soon(self, code: int, reason: str = "") -> None:
        """Idempotently schedule a close; the writer sends it and then stops."""
        if self._closing:
            return
        self._closing = True
        self._close_info = (code, reason)
        self._buffer.append(_CloseSentinel(code, reason))
        self._event.set()

    # -- writer + lifecycle ------------------------------------------------ #

    def request_stop(self) -> None:
        """Ask the writer to drain what remains and then return (teardown)."""
        self._stopped = True
        self._event.set()

    async def run_writer(self) -> None:
        """Drain the send buffer to the socket until closed or asked to stop."""
        while True:
            await self._event.wait()
            self._event.clear()
            while self._buffer:
                item = self._buffer.popleft()
                if isinstance(item, _CloseSentinel):
                    await self._close_ws(item.code, item.reason)
                    return
                self._queued_bytes -= item.nbytes
                try:
                    await self._ws.send_str(item.text)
                except Exception:
                    self._closing = True
                    return
            if self._stopped:
                return

    async def _close_ws(self, code: int, reason: str) -> None:
        if self._ws.closed:
            return
        with contextlib.suppress(Exception):
            await self._ws.close(code=code, message=reason.encode("utf-8")[:_MAX_CLOSE_REASON])

    # -- queue accounting -------------------------------------------------- #

    def _would_overflow(self, nbytes: int) -> bool:
        return (
            len(self._buffer) + 1 > self._max_frames
            or self._queued_bytes + nbytes > self._max_bytes
        )

    def _compact_cursors(self) -> None:
        """Drop superseded cursor frames, keeping the newest per user and type."""
        last_index: dict[tuple[str, str], int] = {}
        for i, item in enumerate(self._buffer):
            if isinstance(item, _Frame) and item.mtype in {"poly-cursor", "doc-cursor"}:
                last_index[(item.mtype, item.user_id)] = i
        if not last_index:
            return
        kept: deque[_Frame | _CloseSentinel] = deque()
        freed = 0
        for i, item in enumerate(self._buffer):
            if (
                isinstance(item, _Frame)
                and item.mtype in {"poly-cursor", "doc-cursor"}
                and last_index.get((item.mtype, item.user_id)) != i
            ):
                freed += item.nbytes
                continue
            kept.append(item)
        self._buffer = kept
        self._queued_bytes -= freed


# --------------------------------------------------------------------------- #
# Handler factory
# --------------------------------------------------------------------------- #


def make_websocket_handler(
    config: Config,
    hub: Hub,
    identity_service: IdentityService,
    clock: Callable[[], float] = time.time,
) -> Callable[[web.Request], object]:
    """Build the ``GET /v1/sessions/{id}/ws`` handler bound to the app's services."""
    limits = config.limits
    allowed_origins = config.allowed_origins
    trusted_proxies = config.trusted_proxies
    join_limiter = KeyedLimiter(limits.joins_per_ip_min, _JOIN_WINDOW, clock)

    async def websocket_handler(request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin is None or origin not in allowed_origins:
            return web.json_response({"error": "origin not allowed"}, status=403)

        client_ip = resolve_client_ip(request.headers, _peer_ip(request), trusted_proxies)
        if not join_limiter.take(client_ip):
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_JOIN_WINDOW)}, status=429
            )

        ws = web.WebSocketResponse(autoping=False, max_msg_size=limits.max_snapshot_frame)
        await ws.prepare(request)

        hello = await _read_hello(ws, limits)
        if hello is None:
            return ws

        try:
            identity, minted = await identity_service.resolve(
                ticket=hello.get("ticket"),
                anon_token=hello.get("anon_token") or request.cookies.get("SEANCE_ANON"),
                cookie=request.cookies.get("SESSION"),
                client_ip=client_ip,
            )
        except AuthError:
            await _send_and_close(
                ws,
                protocol.error_frame(ErrorCode.unauthorized, detail="unauthorized"),
                protocol.CLOSE_FORBIDDEN,
            )
            return ws

        conn = WsConn(ws, identity, protocol.new_id(), limits, clock)
        if minted is not None:
            conn.pending_anon_token = minted
        conn.declared_dialects = hello.get("dialects")

        session_id = request.match_info["id"]
        try:
            session = await hub.connect(session_id, conn)
        except JoinRefused as exc:
            if exc.close_code == protocol.CLOSE_NOT_FOUND:
                code = ErrorCode.unknown_session
            elif exc.close_code == protocol.CLOSE_DIALECT:
                code = ErrorCode.dialect_mismatch
            else:
                code = ErrorCode.forbidden
            await _send_and_close(
                ws, protocol.error_frame(code, detail=exc.reason), exc.close_code
            )
            return ws

        writer_task = asyncio.create_task(conn.run_writer())
        heartbeat_task = asyncio.create_task(_run_heartbeat(ws, conn, clock, limits))
        try:
            await _run_read_loop(ws, conn, session, limits, clock)
        finally:
            await _teardown(ws, conn, session, hub, heartbeat_task, writer_task)
        return ws

    return websocket_handler


# --------------------------------------------------------------------------- #
# Handshake helpers
# --------------------------------------------------------------------------- #


def _peer_ip(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if not peername:
        return ""
    return peername[0]


async def _read_hello(ws: web.WebSocketResponse, limits: Limits) -> dict | None:
    """Read and validate the mandatory first ``hello`` frame within the deadline.

    On timeout, a non-text first frame, a malformed frame, a validation failure,
    or any non-``hello`` type, sends an ``error`` and closes ``4400``, returning
    ``None`` so the caller aborts.
    """
    try:
        async with asyncio.timeout(_HELLO_TIMEOUT):
            msg = await ws.receive()
    except TimeoutError:
        await _send_and_close(
            ws, protocol.error_frame(ErrorCode.bad_frame, detail="hello timeout"),
            protocol.CLOSE_PROTOCOL,
        )
        return None

    if msg.type is not WSMsgType.TEXT:
        await _send_and_close(
            ws, protocol.error_frame(ErrorCode.bad_frame, detail="expected a hello frame"),
            protocol.CLOSE_PROTOCOL,
        )
        return None

    try:
        raw = protocol.parse_frame(msg.data, max_len=limits.max_frame)
        hello = protocol.validate_message(raw, limits)
    except ProtocolError as exc:
        await _send_and_close(
            ws, protocol.error_frame(exc.code, detail=exc.detail, ref_type=exc.ref_type),
            protocol.CLOSE_PROTOCOL,
        )
        return None

    if hello.get("type") != "hello":
        await _send_and_close(
            ws,
            protocol.error_frame(
                ErrorCode.bad_frame, detail="first frame must be hello", ref_type=hello.get("type")
            ),
            protocol.CLOSE_PROTOCOL,
        )
        return None
    return hello


async def _send_and_close(ws: web.WebSocketResponse, frame: dict, close_code: int) -> None:
    """Best-effort send one frame then close — used before the writer task exists."""
    with contextlib.suppress(Exception):
        await ws.send_str(json.dumps(frame, separators=(",", ":")))
    with contextlib.suppress(Exception):
        await ws.close(code=close_code, message=b"")


# --------------------------------------------------------------------------- #
# Heartbeat + read loop
# --------------------------------------------------------------------------- #


async def _run_heartbeat(
    ws: web.WebSocketResponse, conn: WsConn, clock: Callable[[], float], limits: Limits
) -> None:
    """Ping every ``ping_interval``; close ``1011`` if no pong within ``ping_timeout``."""
    interval = limits.ping_interval
    timeout = limits.ping_timeout
    while True:
        await asyncio.sleep(interval)
        if conn.closing:
            return
        if clock() - conn.last_pong > timeout:
            conn.close_soon(_CLOSE_HEARTBEAT, "heartbeat timeout")
            return
        try:
            await ws.ping()
        except Exception:
            conn.close_soon(_CLOSE_HEARTBEAT, "heartbeat send failed")
            return


def _lane_for(mtype: str | None) -> str:
    if mtype in protocol.SNAPSHOT_LANE:
        return "snapshot"
    if mtype in protocol.PROPOSAL_LANE:
        return "proposal"
    if mtype in protocol.CHAT_LANE:
        return "chat"
    if mtype in protocol.FAST_LANE:
        return "fast"
    return "control"


async def _run_read_loop(
    ws: web.WebSocketResponse,
    conn: WsConn,
    session: Session,
    limits: Limits,
    clock: Callable[[], float],
) -> None:
    """Read frames, enforce size/lane/violation limits, and dispatch to the Session."""
    limiter = LaneLimiter(limits, clock)
    violations = 0
    last_rate_error = 0.0

    while True:
        try:
            msg = await ws.receive(timeout=limits.ping_timeout)
        except TimeoutError:
            conn.close_soon(_CLOSE_HEARTBEAT, "receive timeout")
            return

        kind = msg.type
        if kind is WSMsgType.PING:
            # RFC 6455 §5.5.2: a PING MUST be answered with a PONG carrying the
            # same payload. aiohttp autoping is off, so the read loop replies.
            await ws.pong(msg.data)
            conn.last_pong = clock()
            continue
        if kind is WSMsgType.PONG:
            conn.last_pong = clock()
            continue

        if kind is WSMsgType.BINARY:
            conn.send_json(
                protocol.error_frame(ErrorCode.bad_frame, detail="binary frames are not accepted")
            )
            violations += 1
            if violations >= limits.max_violations:
                conn.close_soon(protocol.CLOSE_PROTOCOL, "too many violations")
                return
            continue

        if kind is not WSMsgType.TEXT:
            return  # CLOSE / CLOSING / CLOSED / ERROR

        raw = msg.data
        try:
            parsed = protocol.parse_frame(raw, max_len=limits.max_snapshot_frame)
            mtype = parsed.get("type")
            if mtype not in protocol.SNAPSHOT_LANE and len(raw.encode("utf-8")) > limits.max_frame:
                raise ProtocolError(ErrorCode.too_large, "frame exceeds maximum size")
        except ProtocolError as exc:
            conn.send_json(
                protocol.error_frame(exc.code, detail=exc.detail, ref_type=exc.ref_type)
            )
            violations += 1
            if violations >= limits.max_violations:
                conn.close_soon(protocol.CLOSE_PROTOCOL, "too many violations")
                return
            continue

        lane = _lane_for(mtype)
        if not limiter.take(lane):
            now = clock()
            if now - last_rate_error >= _RATE_ERROR_THROTTLE:
                conn.send_json(
                    protocol.error_frame(
                        ErrorCode.rate_limited, retry_after=limiter.retry_after(lane)
                    )
                )
                last_rate_error = now
            exhausted = limiter.exhausted_since(lane)
            if exhausted is not None and now - exhausted > limits.abuse_window:
                conn.close_soon(protocol.CLOSE_LIMIT, "rate limit abuse")
                return
            continue  # rate-limited frames are dropped, not dispatched

        try:
            session.handle(conn.connection_id, parsed)
        except Exception:
            _LOG.exception("error handling frame on connection %s", conn.connection_id)


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #


async def _teardown(
    ws: web.WebSocketResponse,
    conn: WsConn,
    session: Session,
    hub: Hub,
    heartbeat_task: asyncio.Task,
    writer_task: asyncio.Task,
) -> None:
    """Stop the helper tasks (letting the writer flush a queued close), then leave.

    ``hub.disconnect`` (which notifies the remaining participants) and the socket
    close run in a ``finally`` so a cancellation delivered to the handler while a
    helper task drains still tears the connection down cleanly.
    """
    heartbeat_task.cancel()
    conn.request_stop()
    try:
        await _drain_task(writer_task)
        await _drain_task(heartbeat_task)
    finally:
        hub.disconnect(session, conn.connection_id)
        if not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()


async def _drain_task(task: asyncio.Task) -> None:
    """Bounded-await a per-connection helper task, cancelling one that overruns.

    ``gather(return_exceptions=True)`` reaps the child task's ``CancelledError``
    (or any other terminal exception); a cancellation aimed at the calling
    handler is left to propagate rather than being swallowed here.
    """
    try:
        async with asyncio.timeout(_TEARDOWN_TIMEOUT):
            await asyncio.gather(task, return_exceptions=True)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
