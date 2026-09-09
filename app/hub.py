"""The :class:`Hub` — seance's session registry and lifecycle owner.

The hub is the single async authority over the live-session map. It:

* **creates** sessions — authorizing the creator (kind + per-identity rate) and
  enforcing the global persisted-session cap, minting a collision-free 6-char id, then
  persisting the session immediately in frozen form so it survives before the
  first join;
* **connects** a client — joining a live session, or thawing a frozen one from
  the :class:`app.store.Store` on first connect (bans travel from the store),
  under a global connection cap;
* **freezes** idle sessions back to the store after a grace period, and
  **checkpoints** live sessions periodically for crash durability, via
  :meth:`scan` (which tests drive directly) and the background :meth:`_run` loop;
* **bridges** the synchronous :class:`app.session.Session` audit / ban callbacks
  to the asynchronous store.

Time is injected as ``clock`` so tests advance logic time without sleeping.

Sync-to-async bridge: :class:`Session` fires ``audit_cb`` / ``ban_sink``
synchronously. :meth:`_schedule` turns each into a store write — an
``asyncio.Task`` when a loop is running (retained in ``_bg`` so it is not
garbage-collected mid-flight), or, with no running loop, a coroutine deferred
onto ``_pending`` and awaited at the start of the next async hub call.
:meth:`scan` and :meth:`stop` settle ``_bg`` before freezing, so a ban write
always lands in the store before the session it belongs to is frozen.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time
from collections.abc import Callable

from app import protocol
from app.audit import AuditEvent
from app.config import Config
from app.engine import EngineLimit
from app.identity import Identity, Kind
from app.ratelimit import KeyedLimiter
from app.session import ConnLike, JoinRefused, Session
from app.store import Store
from app.textdoc import TextDocReject

log = logging.getLogger("seance.hub")

_ID_ALPHABET = string.ascii_letters + string.digits
_ID_LENGTH = 6

# Create-path snapshot node caps, mirroring app.protocol._NODE_ID_MAX / _KIND_MAX
# (the WS validators). The POST /v1/sessions snapshot bypasses those validators
# and reaches the engine directly, so id/kind lengths are enforced here first.
_SNAPSHOT_NODE_ID_MAX = 200
_SNAPSHOT_KIND_MAX = 32


class HubError(Exception):
    """An HTTP-facing hub failure. ``status`` is the HTTP status (403/429/503)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Hub:
    """The live-session registry: create, connect, freeze, thaw, checkpoint."""

    def __init__(self, config: Config, store: Store, clock: Callable[[], float] = time.time):
        self.config = config
        self.limits = config.limits
        self.store = store
        self.clock = clock
        self.live: dict[str, Session] = {}
        self.empty_since: dict[str, float] = {}
        self.last_saved_seq: dict[str, int] = {}
        self.last_saved_at: dict[str, float] = {}
        self._creates = KeyedLimiter(self.limits.creates_per_identity_hour, 3600.0, clock)
        self._create_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._pending: list = []
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._loop_task: asyncio.Task | None = None
        self._interval = max(0.5, min(self.limits.freeze_grace, self.limits.checkpoint_secs) / 2)
        self._frozen_session_ttl = float(self.limits.frozen_session_ttl)
        self._unclaimed_session_ttl = float(self.limits.unclaimed_session_ttl)

    # --------------------------------------------------------------------- #
    # Properties
    # --------------------------------------------------------------------- #

    @property
    def live_count(self) -> int:
        """Number of sessions currently held live in memory."""
        return len(self.live)

    @property
    def connection_count(self) -> int:
        """Total client connections across every live session (all tabs)."""
        return sum(len(session.conns) for session in self.live.values())

    # --------------------------------------------------------------------- #
    # Sync -> async scheduling
    # --------------------------------------------------------------------- #

    async def _guarded(self, coro) -> None:
        """Await a bridged store write, logging (never dropping) a failure.

        Consuming the exception here keeps a failed ``add_ban``/``audit`` from
        vanishing silently and from surfacing as an unretrieved task exception.
        """
        try:
            await coro
        except Exception:
            log.exception("background store write failed")

    def _schedule(self, coro) -> None:
        """Run ``coro`` as a guarded background store write.

        With a running loop the coroutine becomes a task retained in ``_bg``
        (and discarded on completion) so it survives to run. With no running
        loop it is deferred onto ``_pending`` and awaited by the next async call.
        """
        guarded = self._guarded(coro)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending.append(guarded)
            return
        task = loop.create_task(guarded)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _drain_pending(self) -> None:
        """Await every coroutine deferred while no loop was running."""
        while self._pending:
            batch = self._pending
            self._pending = []
            for coro in batch:
                await coro

    async def _settle_bg(self) -> None:
        """Await all in-flight scheduled tasks (e.g. pending ban write-throughs)."""
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # --------------------------------------------------------------------- #
    # Session callback bridges
    # --------------------------------------------------------------------- #

    def _audit_bridge(self, event: AuditEvent) -> None:
        self._schedule(
            self.store.audit(
                ts=event.ts,
                session_id=event.session_id,
                actor=event.actor,
                action=event.action,
                target=event.target,
                detail=event.detail,
            )
        )

    def _ban_bridge(self, session_id: str, user_id: str, by: str, banned: bool) -> None:
        if banned:
            self._schedule(self.store.add_ban(session_id, user_id, by, int(self.clock())))
        else:
            self._schedule(self.store.remove_ban(session_id, user_id))

    # --------------------------------------------------------------------- #
    # Create
    # --------------------------------------------------------------------- #

    async def _new_session_id(self) -> str:
        """Mint a 6-char id absent from both the live map and the store."""
        while True:
            candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            if candidate in self.live:
                continue
            if await self.store.load_session(candidate) is not None:
                continue
            return candidate

    async def create_session(
        self, identity: Identity, snapshot: dict | None = None, dialect: str | None = None
    ) -> str:
        """Authorize, mint, seed, and persist a new session (frozen); return its id."""
        await self._drain_pending()
        if identity.kind == Kind.GS_EPHEMERAL:
            raise HubError(403, "ephemeral members cannot create sessions")
        if identity.kind == Kind.ANON and not self.config.anon_can_create:
            raise HubError(403, "anonymous users cannot create sessions")
        if not self._creates.take(identity.user_id):
            raise HubError(429, "session create rate limit exceeded")
        async with self._create_lock:
            if await self.store.count_sessions() >= self.limits.max_sessions:
                raise HubError(503, "server session capacity reached")

            session_id = await self._new_session_id()
            session = Session(
                session_id,
                created_by=identity.user_id,
                limits=self.limits,
                clock=self.clock,
                audit_cb=self._audit_bridge,
                ban_sink=self._ban_bridge,
                dialect=dialect or protocol.DEFAULT_DIALECT,
            )
            if snapshot:
                try:
                    self._apply_snapshot(session, snapshot)
                except (KeyError, TypeError, AttributeError, EngineLimit, TextDocReject) as exc:
                    raise HubError(400, "invalid snapshot") from exc
            session.recalculate_content_budget()
            if not session.content_within_budget:
                raise HubError(413, "session snapshot exceeds aggregate content limit")
            payload = session.freeze_snapshot()
            payload["frozen_at"] = int(self.clock())
            await self.store.save_session(session_id, payload)
            return session_id

    def _apply_snapshot(self, session: Session, snapshot: dict) -> None:
        """Seed a fresh session's engine lanes from an optional creator snapshot.

        The engine's ``apply_snapshot`` caps node text/count and enforces field
        types but not id/kind **lengths** — the WS path gets those from the
        protocol validators, which the create-path snapshot bypasses. So each
        node's id (≤200) and kind (≤32) length is enforced here first; a breach
        raises :class:`EngineLimit`, which :meth:`create_session` maps to the
        400 "invalid snapshot" response.
        """
        state = snapshot.get("state")
        if state:
            seq = session.seq + 1
            session.state.bulk_set(state, seq, by=session.created_by)
            session.seq = seq
        poly = snapshot.get("poly")
        if poly:
            nodes = poly.get("nodes", [])
            self._validate_snapshot_nodes(nodes)
            session.seq += 1
            session.poly.apply_snapshot(poly["programText"], nodes, poly.get("frame"))
        docs = snapshot.get("docs")
        if docs:
            if not isinstance(docs, list):
                raise TypeError("docs must be a list")
            for doc in docs:
                if not isinstance(doc, dict):
                    raise TypeError("doc must be an object")
                session.docs.create_doc(
                    doc["id"],
                    doc["title"],
                    doc["kind"],
                    doc["text"],
                    doc.get("default", False),
                )

    @staticmethod
    def _validate_snapshot_nodes(nodes) -> None:
        """Enforce per-node id/kind length caps for a create-path snapshot.

        Non-list ``nodes`` and non-dict entries are left for the engine to reject
        (its type checks surface via the same 400 path); this guard adds only the
        length caps the engine itself does not apply.
        """
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            if isinstance(node_id, str) and len(node_id) > _SNAPSHOT_NODE_ID_MAX:
                raise EngineLimit(f"node id too long ({_SNAPSHOT_NODE_ID_MAX} chars)")
            kind = node.get("kind")
            if isinstance(kind, str) and len(kind) > _SNAPSHOT_KIND_MAX:
                raise EngineLimit(f"node kind too long ({_SNAPSHOT_KIND_MAX} chars)")

    # --------------------------------------------------------------------- #
    # Per-session serialization (connect thaw vs. freeze)
    # --------------------------------------------------------------------- #

    def _acquire_lock_ref(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock, creating it and counting this holder.

        One lock object is shared across every concurrent thaw/freeze caller for
        an id; ref-counting drops the entry once the last caller releases, so the
        map is bounded by the number of ids being actively thawed or frozen.
        """
        lock = self._connect_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._connect_locks[session_id] = lock
        self._lock_refs[session_id] = self._lock_refs.get(session_id, 0) + 1
        return lock

    def _release_lock_ref(self, session_id: str) -> None:
        """Drop this holder's reference; discard the lock when the last one leaves."""
        remaining = self._lock_refs.get(session_id, 0) - 1
        if remaining > 0:
            self._lock_refs[session_id] = remaining
        else:
            self._lock_refs.pop(session_id, None)
            self._connect_locks.pop(session_id, None)

    # --------------------------------------------------------------------- #
    # Connect / disconnect
    # --------------------------------------------------------------------- #

    async def connect(self, session_id: str, conn: ConnLike) -> Session:
        """Join a live session, or thaw a frozen one, admitting ``conn``.

        Raises :class:`app.session.JoinRefused` — ``4404`` for an unknown
        session, ``4429`` when the global connection cap is reached, or whatever
        :meth:`Session.join` raises (banned/locked/guests/roster).
        """
        await self._drain_pending()
        session = self.live.get(session_id)
        if session is not None:
            return self._admit_live(session_id, session, conn)

        # First connect: thaw under a per-id lock so concurrent first-connects
        # share one live Session instead of each thawing a rival copy.
        lock = self._acquire_lock_ref(session_id)
        try:
            async with lock:
                session = self.live.get(session_id)  # re-check under the lock
                if session is not None:
                    return self._admit_live(session_id, session, conn)
                payload = await self.store.load_session(session_id)
                if payload is None:
                    raise JoinRefused(protocol.CLOSE_NOT_FOUND, "unknown session")
                bans = await self.store.get_bans(session_id)
                try:
                    session = Session.thaw(
                        session_id,
                        payload,
                        self.limits,
                        self.clock,
                        audit_cb=self._audit_bridge,
                        bans=bans,
                        ban_sink=self._ban_bridge,
                    )
                except EngineLimit as exc:
                    raise JoinRefused(protocol.CLOSE_LIMIT, "session content limit") from exc
                thawed_seq = session.seq
                if self.connection_count >= self.limits.max_connections:
                    raise JoinRefused(protocol.CLOSE_LIMIT, "server full")
                session.join(conn)  # JoinRefused propagates; a fresh thaw is discarded
                self.live[session_id] = session
                self.last_saved_seq[session_id] = thawed_seq
                self.last_saved_at[session_id] = self.clock()
                self.empty_since.pop(session_id, None)
                return session
        finally:
            self._release_lock_ref(session_id)

    def _admit_live(self, session_id: str, session: Session, conn: ConnLike) -> Session:
        """Admit ``conn`` to an already-live session under the global connection cap."""
        if self.connection_count >= self.limits.max_connections:
            raise JoinRefused(protocol.CLOSE_LIMIT, "server full")
        session.join(conn)  # JoinRefused propagates (banned/locked/guests/roster)
        self.empty_since.pop(session_id, None)
        return session

    def disconnect(self, session: Session, connection_id: str) -> None:
        """Remove one connection; arm the freeze timer once the session is empty."""
        session.leave(connection_id)
        if session.empty:
            self.empty_since[session.session_id] = self.clock()
        else:
            self.empty_since.pop(session.session_id, None)

    def close_connections(self, code: int, reason: str = "") -> None:
        """Schedule a close on every live connection (server shutdown).

        Each transport writer sends the close frame and its handler then tears
        the connection down through the normal path, so aiohttp's shutdown wait
        ends as soon as the handlers return instead of after its full timeout,
        and :meth:`stop` (on cleanup) freezes sessions that are already empty.
        """
        for session in self.live.values():
            for conn in list(session.conns.values()):
                conn.close_soon(code, reason)

    # --------------------------------------------------------------------- #
    # Freeze / checkpoint
    # --------------------------------------------------------------------- #

    async def _freeze(self, session_id: str) -> None:
        """Persist a session in frozen form and evict it from memory.

        Held under the per-id lock across save+evict; the post-save re-check keeps
        a connect that joined during the ``save_session`` await from being orphaned
        by the eviction — an occupied session stays live and the save is superseded
        by the next checkpoint (which clears ``frozen_at``).
        """
        lock = self._acquire_lock_ref(session_id)
        try:
            async with lock:
                session = self.live.get(session_id)
                if session is None:
                    return
                payload = session.freeze_snapshot()
                payload["frozen_at"] = int(self.clock())
                await self.store.save_session(session_id, payload)
                if not session.empty or session_id not in self.empty_since:
                    self.last_saved_seq[session_id] = payload["seq"]
                    self.last_saved_at[session_id] = self.clock()
                    return
                del self.live[session_id]
                self.empty_since.pop(session_id, None)
                self.last_saved_seq.pop(session_id, None)
                self.last_saved_at.pop(session_id, None)
        finally:
            self._release_lock_ref(session_id)

    async def _checkpoint_live(self, session: Session) -> None:
        """Persist a still-live session (``frozen_at=None``) and update trackers."""
        payload = session.freeze_snapshot()
        payload["frozen_at"] = None
        await self.store.save_session(session.session_id, payload)
        # A frame may arrive while SQLite is awaited. Only the captured revision
        # was persisted; later mutations must remain dirty for the next scan.
        self.last_saved_seq[session.session_id] = payload["seq"]
        self.last_saved_at[session.session_id] = self.clock()

    async def checkpoint(self, session: Session) -> None:
        """Force-save one live session for durability, without evicting it."""
        await self._drain_pending()
        await self._settle_bg()
        await self._checkpoint_live(session)

    async def scan(self) -> None:
        """One freeze-grace + checkpoint pass over the live sessions.

        Also prunes frozen rows past ``frozen_session_ttl`` and never-joined rows
        past the shorter ``unclaimed_session_ttl``.

        Settles pending write-throughs first so a ban lands before its session is
        frozen. Freezes every empty session past ``freeze_grace``, then
        checkpoints any live session that has *changed* since its last save and
        is past ``checkpoint_ops`` or ``checkpoint_secs``. A join bumps ``seq``
        (welcome plus snapshot), so the first pass after a connect still saves
        and clears the stale ``frozen_at`` marker.
        """
        await self._drain_pending()
        await self._settle_bg()
        now = self.clock()

        for session_id in list(self.live):
            started = self.empty_since.get(session_id)
            if started is not None and now - started >= self.limits.freeze_grace:
                await self._freeze(session_id)

        for session_id in list(self.live):
            session = self.live[session_id]
            dirty = session.seq - self.last_saved_seq.get(session_id, 0)
            if dirty <= 0:
                # Nothing has happened since the last save. The time test below
                # measures seconds since that save, not since the last activity,
                # so without this an idle session is re-serialized and rewritten
                # every checkpoint_secs forever.
                continue
            since_save = now - self.last_saved_at.get(session_id, 0.0)
            if dirty >= self.limits.checkpoint_ops or since_save >= self.limits.checkpoint_secs:
                await self._checkpoint_live(session)

        if self._frozen_session_ttl > 0:
            cutoff = int(now - self._frozen_session_ttl)
            for session_id in await self.store.list_frozen_older_than(cutoff):
                await self._delete_frozen_if_still_cold(session_id)

        if self._unclaimed_session_ttl > 0:
            # A created session that nobody ever joined holds a row, and rows are
            # what MAX_SESSIONS counts, so leaving them for the full retention
            # window lets cheap creates fill the global cap and answer 503 to
            # everyone. Nothing of value is lost: no client ever saw it.
            cutoff = int(now - self._unclaimed_session_ttl)
            for session_id in await self.store.list_unclaimed_older_than(cutoff):
                await self._delete_frozen_if_still_cold(session_id)

    async def _delete_frozen_if_still_cold(self, session_id: str) -> None:
        """Delete one old frozen row only if it remains non-live under the id lock."""
        lock = self._acquire_lock_ref(session_id)
        try:
            async with lock:
                if session_id in self.live:
                    return
                await self.store.delete_session(session_id)
        finally:
            self._release_lock_ref(session_id)

    # --------------------------------------------------------------------- #
    # Background loop
    # --------------------------------------------------------------------- #

    async def start(self) -> None:
        """Start the background freeze/checkpoint loop (idempotent)."""
        if self._loop_task is not None:
            return
        self._loop_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.scan()
            except Exception:
                log.exception("hub scan failed")

    async def stop(self) -> None:
        """Cancel the loop and freeze every live session to the store (idempotent)."""
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._drain_pending()
        await self._settle_bg()

        now = int(self.clock())
        for session_id in list(self.live):
            payload = self.live[session_id].freeze_snapshot()
            payload["frozen_at"] = now
            await self.store.save_session(session_id, payload)
        self.live.clear()
        self.empty_since.clear()
        self.last_saved_seq.clear()
        self.last_saved_at.clear()
