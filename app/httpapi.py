"""The HTTP surface for the seance server.

:func:`build_app` assembles the aiohttp application: the health/meta endpoints,
the identity-minting and session lifecycle routes under ``/v1``, and the two
cross-cutting middlewares (security headers + CORS/origin enforcement). The
WebSocket route is registered here but its handler is supplied by the transport
layer; when absent it answers ``501`` so the surface is complete on its own.

Design rules enforced here:

* **Server-derived identity only** — every ``/v1`` credential path defers to
  :class:`app.identity.IdentityService`; nothing identity-bearing is trusted from
  a request body. The client IP is resolved through :func:`resolve_client_ip`
  (honouring forwarding headers only from trusted proxies), and the gs-proxied
  ticket endpoint keys off the *raw* socket peer, never a forwarding header.
* **JSON errors only** — every failure, including router 404/405s, is normalised
  to a ``{"error": ...}`` JSON body so no aiohttp HTML page leaks. Tokens are
  never logged and appear only in their intended response fields/cookies.
* **Injected clock** — the startup timestamp and both keyed rate limiters take the
  same ``clock`` the rest of the server uses, so tests advance logic time without
  sleeping.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path

from aiohttp import web

from app import __version__, protocol
from app.clientip import resolve_client_ip
from app.config import Config
from app.hub import Hub, HubError
from app.identity import IdentityService, Kind, MintLimited
from app.ratelimit import KeyedLimiter

# The create body seeds session content directly (state values, poly frame).
# Python's json accepts the non-standard NaN / Infinity literals and unbounded
# nesting; both would be persisted and later fan out to browser clients whose
# JSON.parse (or the server's own deepcopy) cannot handle them. Mirror the
# WebSocket frame edge: refuse non-finite constants and deep nesting here.
_MAX_BODY_DEPTH = 64


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


_SERVICE = "seance"
_HEALTH_PROBE_ID = "______"

_LOG = logging.getLogger("seance.http")

_CORS_METHODS = "GET, POST, OPTIONS"
_CORS_ALLOW_HEADERS = "Content-Type, X-Seance-Anon"

# (method, path) pairs that only a browser should call: a missing Origin means a
# non-browser client hit a browser-only endpoint and is rejected.
_BROWSER_ONLY = frozenset(
    {("POST", "/v1/anon"), ("POST", "/v1/sessions"), ("GET", "/v1/me")}
)

_DEFAULT_META_PATH = Path("public/deployment-meta.json")

_ANON_WINDOW = 3600.0
_JOIN_WINDOW = 60.0

_ALLOWED_ORIGINS_KEY = web.AppKey("allowed_origins", frozenset)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _HttpApi:
    """Bound request handlers sharing the app's config, hub, identity, and limiters."""

    def __init__(
        self,
        config: Config,
        hub: Hub,
        identity: IdentityService,
        ws_handler: Handler | None,
        clock: Callable[[], float],
        meta_path: Path,
    ) -> None:
        self._config = config
        self._hub = hub
        self._identity = identity
        self._clock = clock
        self._meta_path = meta_path
        self._ws_handler = ws_handler
        self._create_limiter = KeyedLimiter(
            config.limits.creates_per_ip_hour, _ANON_WINDOW, clock
        )
        self._probe_limiter = KeyedLimiter(config.limits.joins_per_ip_min, _JOIN_WINDOW, clock)
        self._startup_iso = datetime.fromtimestamp(clock(), tz=UTC).isoformat()
        self._meta_body = self._read_meta_body()

    def _read_meta_body(self) -> bytes:
        """Read the deployment-meta file once at startup, or synthesize the dev JSON.

        The file is small and immutable for the process lifetime, so it is read
        here and served from memory — keeping ``GET /deployment-meta.json`` off
        the event-loop thread (no per-request ``stat``/``read``).
        """
        if self._meta_path.is_file():
            return self._meta_path.read_bytes()
        return json.dumps({"git_hash": "dev", "date": self._startup_iso}).encode("utf-8")

    # -- helpers ----------------------------------------------------------- #

    def _peer_ip(self, request: web.Request) -> str:
        peername = request.transport.get_extra_info("peername") if request.transport else None
        if not peername:
            return ""
        return peername[0]

    def _client_ip(self, request: web.Request) -> str:
        return resolve_client_ip(
            request.headers, self._peer_ip(request), self._config.trusted_proxies
        )

    def _peer_trusted(self, peer_ip: str) -> bool:
        nets = self._config.gs_trusted_nets
        if not nets:
            return False
        try:
            addr = ip_address(peer_ip)
        except ValueError:
            return False
        return any(addr in net for net in nets)

    def _anon_credential(self, request: web.Request) -> str | None:
        # A tab's explicit SDK identity must agree with its WebSocket hello;
        # another tab may have replaced the shared cookie in the meantime.
        return request.headers.get("X-Seance-Anon") or request.cookies.get("SEANCE_ANON")

    def _set_anon_cookie(self, response: web.Response, token: str) -> None:
        response.set_cookie(
            "SEANCE_ANON",
            token,
            max_age=self._config.limits.anon_ttl,
            path="/",
            httponly=True,
            secure=True,
            samesite="Lax",
        )

    # -- health / meta ----------------------------------------------------- #

    async def up(self, request: web.Request) -> web.StreamResponse:
        try:
            await self._hub.store.load_session(_HEALTH_PROBE_ID)
        except Exception:
            return web.json_response(
                {"status": "error", "service": _SERVICE, "version": __version__}, status=503
            )
        return web.json_response({"status": "ok", "service": _SERVICE, "version": __version__})

    async def deployment_meta(self, request: web.Request) -> web.StreamResponse:
        return web.Response(body=self._meta_body, content_type="application/json")

    # -- identity minting -------------------------------------------------- #

    async def anon(self, request: web.Request) -> web.StreamResponse:
        client_ip = self._client_ip(request)
        # The same budget every other minting path charges (IdentityService).
        if not self._identity.take_mint(client_ip):
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_ANON_WINDOW)}, status=429
            )
        identity, token = self._identity.mint_anon()
        response = web.json_response(
            {"anon_token": token, "user_id": identity.user_id, "username": identity.username}
        )
        self._set_anon_cookie(response, token)
        return response

    async def ticket(self, request: web.Request) -> web.StreamResponse:
        if not self._peer_trusted(self._peer_ip(request)):
            return web.json_response({"error": "forbidden"}, status=403)
        user_id = (request.headers.get("X-GS-User-Id") or "").strip()
        username = (request.headers.get("X-GS-Username") or "").strip()
        if not user_id or not username:
            return web.json_response(
                {"error": "X-GS-User-Id and X-GS-Username are required"}, status=400
            )
        token = self._identity.mint_ticket(user_id, username, Kind.MEMBER)
        return web.json_response({"ticket": token})

    async def me(self, request: web.Request) -> web.StreamResponse:
        try:
            identity, minted = await self._identity.resolve(
                ticket=None,
                anon_token=self._anon_credential(request),
                cookie=request.cookies.get("SESSION"),
                client_ip=self._client_ip(request),
            )
        except MintLimited:
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_ANON_WINDOW)}, status=429
            )
        body = {
            "user_id": identity.user_id,
            "username": identity.username,
            "kind": identity.kind.value,
        }
        if minted is not None:
            body["anon_token"] = minted
        response = web.json_response(body)
        if minted is not None:
            self._set_anon_cookie(response, minted)
        return response

    # -- session lifecycle ------------------------------------------------- #

    async def create_session(self, request: web.Request) -> web.StreamResponse:
        client_ip = self._client_ip(request)
        try:
            identity, minted = await self._identity.resolve(
                ticket=None,
                anon_token=self._anon_credential(request),
                cookie=request.cookies.get("SESSION"),
                client_ip=client_ip,
            )
        except MintLimited:
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_ANON_WINDOW)}, status=429
            )
        if identity.kind == Kind.ANON and not self._create_limiter.take(client_ip):
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_ANON_WINDOW)}, status=429
            )
        raw = await request.read()
        snapshot = None
        dialect = None
        if raw:
            try:
                payload = json.loads(raw, parse_constant=_reject_constant)
                protocol.validate_json_values(payload, _MAX_BODY_DEPTH)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return web.json_response({"error": "invalid json"}, status=400)
            if not isinstance(payload, dict):
                return web.json_response({"error": "body must be a json object"}, status=400)
            snapshot = payload.get("snapshot")
            if "dialect" in payload:
                dialect = payload["dialect"]
                if not isinstance(dialect, str) or not protocol.DIALECT_RE.match(dialect):
                    return web.json_response({"error": "invalid dialect"}, status=400)
        try:
            session_id = await self._hub.create_session(identity, snapshot, dialect)
        except HubError as exc:
            return web.json_response({"error": exc.detail}, status=exc.status)
        body = {"session_id": session_id}
        if minted is not None:
            body["anon_token"] = minted
        response = web.json_response(body, status=201)
        if minted is not None:
            self._set_anon_cookie(response, minted)
        return response

    async def session_probe(self, request: web.Request) -> web.StreamResponse:
        if not self._probe_limiter.take(self._client_ip(request)):
            return web.json_response(
                {"error": "rate_limited", "retry_after": int(_JOIN_WINDOW)}, status=429
            )
        session_id = request.match_info["id"]
        live = self._hub.live.get(session_id)
        if live is not None:
            return web.json_response(
                {"id": session_id, "open": not live.settings.locked, "dialect": live.dialect}
            )
        payload = await self._hub.store.load_session(session_id)
        if payload is None:
            return web.json_response({"error": "unknown session"}, status=404)
        return web.json_response(
            {
                "id": session_id,
                "open": not payload["settings"]["locked"],
                "dialect": payload.get("dialect", protocol.DEFAULT_DIALECT),
            }
        )

    async def ws_not_implemented(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"error": "not implemented"}, status=501)

    # -- stats ------------------------------------------------------------- #

    async def stats(self, request: web.Request) -> web.StreamResponse:
        if not self._config.stats_enabled:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(
            {"live_sessions": self._hub.live_count, "connections": self._hub.connection_count}
        )


# --------------------------------------------------------------------------- #
# Middlewares
# --------------------------------------------------------------------------- #


@web.middleware
async def _security_headers_mw(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Stamp the fixed security headers on every response; ``no-store`` on ``/v1``."""
    response = await handler(request)
    if not getattr(response, "prepared", False):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def _error_json_mw(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Normalise every failure to a JSON body so no aiohttp HTML/text page leaks.

    Raised ``HTTPException``s (router 404/405, etc.) map to ``{"error": reason}``.
    Any other unexpected exception is logged server-side with only the method and
    matched route template — never a raw path, tokens, headers, or cookies — and
    answered with a generic ``{"error": "internal"}`` 500. ``asyncio.CancelledError``
    is a ``BaseException`` and is not caught, so task cancellation still propagates.
    """
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400:
            raise
        return web.json_response({"error": exc.reason}, status=exc.status)
    except Exception:
        route = request.match_info.route
        resource = getattr(route, "resource", None)
        route_template = getattr(resource, "canonical", None) or "<unmatched>"
        _LOG.exception("unhandled error serving %s %s", request.method, route_template)
        return web.json_response({"error": "internal"}, status=500)


@web.middleware
async def _cors_mw(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Origin allowlisting + preflight for ``/v1`` (the ws route passes through)."""
    path = request.path
    if not path.startswith("/v1/"):
        return await handler(request)

    origin = request.headers.get("Origin")
    allowed = origin is not None and origin in request.app[_ALLOWED_ORIGINS_KEY]

    if request.method == "OPTIONS":
        response = web.Response(status=204)
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
        response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    # The upgrade route enforces Origin inside the ws handler, not here.
    if path.endswith("/ws"):
        return await handler(request)

    if origin is None:
        if (request.method, path) in _BROWSER_ONLY:
            return web.json_response({"error": "origin not allowed"}, status=403)
        return await handler(request)

    if not allowed:
        if request.method != "GET":
            return web.json_response({"error": "origin not allowed"}, status=403)
        # A safe method with a disallowed Origin: omit CORS headers; the browser
        # blocks the read. No 403 — that would break non-browser GET probes.
        return await handler(request)

    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    return response


# --------------------------------------------------------------------------- #
# Application assembly
# --------------------------------------------------------------------------- #


def build_app(
    config: Config,
    hub: Hub,
    identity: IdentityService,
    ws_handler: Handler | None = None,
    clock: Callable[[], float] = time.time,
    meta_path: Path = _DEFAULT_META_PATH,
) -> web.Application:
    """Assemble the seance HTTP application.

    ``ws_handler`` is the transport's WebSocket handler; when ``None`` the ws route
    answers ``501``. ``meta_path`` is the deployment-meta file location (overridable
    for tests). Middleware order is outermost-first: security headers wrap the CORS
    layer, which wraps the JSON error normaliser closest to the handlers.
    """
    api = _HttpApi(config, hub, identity, ws_handler, clock, meta_path)
    app = web.Application(middlewares=[_security_headers_mw, _cors_mw, _error_json_mw])
    app[_ALLOWED_ORIGINS_KEY] = config.allowed_origins

    app.router.add_get("/up", api.up)
    app.router.add_get("/deployment-meta.json", api.deployment_meta)
    app.router.add_post("/v1/anon", api.anon)
    app.router.add_post("/v1/ticket", api.ticket)
    app.router.add_get("/v1/me", api.me)
    app.router.add_post("/v1/sessions", api.create_session)
    app.router.add_get("/v1/sessions/{id}", api.session_probe)
    app.router.add_get("/v1/sessions/{id}/ws", ws_handler or api.ws_not_implemented)
    app.router.add_get("/v1/stats", api.stats)
    return app
