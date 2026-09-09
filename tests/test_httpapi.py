"""Tests for app.httpapi — the HTTP surface (routes + CORS/security middleware).

Every test builds a real :class:`aiohttp.web.Application` via :func:`build_app`
wrapped around a real :class:`app.hub.Hub` over a ``tmp_path`` store and a real
:class:`app.identity.IdentityService`, then drives it through an in-process
``aiohttp`` TestClient on a 127.0.0.1 socket. Logic time is the injected
:class:`tests.conftest.FakeClock`; no sleeping, no external network.
"""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from app import __version__, protocol
from app.config import Config
from app.directory import MemberRecord, StaticDirectory
from app.httpapi import build_app
from app.hub import Hub
from app.identity import GsSerializer, Identity, IdentityService, Kind
from app.store import Store
from tests.conftest import FakeConn

ALLOWED = "http://allowed.test"
EVIL = "http://evil.test"
MEMBER_UUID = "0f9b2d7e-1111-2222-3333-444455556666"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "SEANCE_SECRET": Fernet.generate_key().decode(),
        "SEANCE_DB": ":memory:",
        "SEANCE_ALLOWED_ORIGINS": ALLOWED,
    }
    env.update(overrides)
    return env


@pytest.fixture
async def app_factory(tmp_path, clock):
    """Coroutine factory building isolated (client, hub, store, identity) contexts."""
    created: list[tuple[TestClient, Store]] = []

    async def _make(*, ws_handler=None, meta_path=None, directory=None, **env):
        store = await Store.open(str(tmp_path / f"httpapi-{len(created)}.db"))
        config = Config.from_env(_env(**env))
        hub = Hub(config, store, clock)
        identity = IdentityService(config, directory, clock=clock)
        kwargs = {"ws_handler": ws_handler, "clock": clock}
        if meta_path is not None:
            kwargs["meta_path"] = meta_path
        app = build_app(config, hub, identity, **kwargs)
        client = TestClient(TestServer(app))
        await client.start_server()
        created.append((client, store))
        return SimpleNamespace(
            client=client, hub=hub, store=store, identity=identity, config=config
        )

    yield _make
    for client, store in created:
        await client.close()
        await store.close()


def _member(uid: str = MEMBER_UUID, name: str = "Ada") -> Identity:
    return Identity(user_id=uid, username=name, kind=Kind.MEMBER)


# --------------------------------------------------------------------------- #
# /up
# --------------------------------------------------------------------------- #


async def test_up_healthy_exact_contract(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/up")
    assert r.status == 200
    assert await r.json() == {"status": "ok", "service": "seance", "version": __version__}


async def test_up_carries_security_headers(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/up")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


async def test_up_unhealthy_when_store_closed(app_factory):
    ctx = await app_factory()
    await ctx.store.close()
    r = await ctx.client.get("/up")
    assert r.status == 503
    assert await r.json() == {"status": "error", "service": "seance", "version": __version__}


# --------------------------------------------------------------------------- #
# /deployment-meta.json
# --------------------------------------------------------------------------- #


async def test_deployment_meta_serves_file_when_present(app_factory, tmp_path):
    meta = tmp_path / "deployment-meta.json"
    meta.write_bytes(b'{"git_hash": "abc123", "date": "2026-07-02T00:00:00+00:00"}')
    ctx = await app_factory(meta_path=meta)
    r = await ctx.client.get("/deployment-meta.json")
    assert r.status == 200
    assert r.content_type == "application/json"
    assert await r.json() == {"git_hash": "abc123", "date": "2026-07-02T00:00:00+00:00"}


async def test_deployment_meta_default_when_absent(app_factory, tmp_path, clock):
    ctx = await app_factory(meta_path=tmp_path / "does-not-exist.json")
    r = await ctx.client.get("/deployment-meta.json")
    assert r.status == 200
    body = await r.json()
    assert body["git_hash"] == "dev"
    assert body["date"] == datetime.fromtimestamp(clock.now, tz=UTC).isoformat()


# --------------------------------------------------------------------------- #
# POST /v1/anon
# --------------------------------------------------------------------------- #


async def test_anon_mint_returns_token_and_cookie(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post("/v1/anon", headers={"Origin": ALLOWED})
    assert r.status == 200
    body = await r.json()
    assert set(body) == {"anon_token", "user_id", "username"}
    # Token is a real, verifiable anon credential.
    verified = ctx.identity.verify_anon(body["anon_token"])
    assert verified.user_id == body["user_id"]
    assert verified.kind == Kind.ANON

    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("SEANCE_ANON=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert f"Max-Age={ctx.config.limits.anon_ttl}" in cookie


async def test_anon_mint_mirrors_allowed_origin(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post("/v1/anon", headers={"Origin": ALLOWED})
    assert r.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert r.headers["Access-Control-Allow-Credentials"] == "true"
    assert r.headers["Vary"] == "Origin"


async def test_anon_mint_rate_limited(app_factory):
    ctx = await app_factory(SEANCE_LIMIT_ANON_MINTS_PER_IP_HOUR="2")
    first = await ctx.client.post("/v1/anon", headers={"Origin": ALLOWED})
    second = await ctx.client.post("/v1/anon", headers={"Origin": ALLOWED})
    third = await ctx.client.post("/v1/anon", headers={"Origin": ALLOWED})
    assert first.status == 200
    assert second.status == 200
    assert third.status == 429
    body = await third.json()
    assert body["error"] == "rate_limited"
    assert body["retry_after"] > 0


# --------------------------------------------------------------------------- #
# POST /v1/ticket
# --------------------------------------------------------------------------- #


async def test_ticket_trusted_peer_mints_redeemable_ticket(app_factory):
    ctx = await app_factory(SEANCE_GS_TRUSTED_NETS="127.0.0.0/8")
    r = await ctx.client.post(
        "/v1/ticket",
        headers={"X-GS-User-Id": "member-77", "X-GS-Username": "Grace"},
    )
    assert r.status == 200
    token = (await r.json())["ticket"]
    redeemed = ctx.identity.redeem_ticket(token)
    assert redeemed == Identity(user_id="member-77", username="Grace", kind=Kind.MEMBER)


async def test_ticket_untrusted_peer_forbidden(app_factory):
    ctx = await app_factory(SEANCE_GS_TRUSTED_NETS="203.0.113.0/24")
    r = await ctx.client.post(
        "/v1/ticket",
        headers={"X-GS-User-Id": "member-77", "X-GS-Username": "Grace"},
    )
    assert r.status == 403
    assert (await r.json())["error"] == "forbidden"


async def test_ticket_empty_trusted_nets_disabled(app_factory):
    ctx = await app_factory()  # no SEANCE_GS_TRUSTED_NETS -> empty -> always 403
    r = await ctx.client.post(
        "/v1/ticket",
        headers={"X-GS-User-Id": "member-77", "X-GS-Username": "Grace"},
    )
    assert r.status == 403


async def test_ticket_missing_headers_bad_request(app_factory):
    ctx = await app_factory(SEANCE_GS_TRUSTED_NETS="127.0.0.0/8")
    r = await ctx.client.post("/v1/ticket", headers={"X-GS-User-Id": "member-77"})
    assert r.status == 400


# --------------------------------------------------------------------------- #
# GET /v1/me
# --------------------------------------------------------------------------- #


async def test_me_anon_mints_and_sets_cookie(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/me", headers={"Origin": ALLOWED})
    assert r.status == 200
    body = await r.json()
    assert body["kind"] == "anon"
    assert body["anon_token"]
    assert r.headers["Set-Cookie"].startswith("SEANCE_ANON=")


async def test_me_member_cookie_resolves_directory(app_factory, clock):
    gs_key = Fernet.generate_key()
    directory = StaticDirectory({MEMBER_UUID: MemberRecord(MEMBER_UUID, "Ada", False)})
    ctx = await app_factory(
        SEANCE_GS_SERIALIZER_KEY=gs_key.decode(), directory=directory
    )
    cookie = GsSerializer(gs_key, clock=clock).dumps(f"{MEMBER_UUID};127.0.0.1")
    r = await ctx.client.get(
        "/v1/me", headers={"Origin": ALLOWED}, cookies={"SESSION": cookie}
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"user_id": MEMBER_UUID, "username": "Ada", "kind": "member"}
    assert "Set-Cookie" not in r.headers


async def test_me_without_origin_forbidden(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/me")
    assert r.status == 403
    assert (await r.json())["error"] == "origin not allowed"


# --------------------------------------------------------------------------- #
# POST /v1/sessions
# --------------------------------------------------------------------------- #


async def test_create_session_member(app_factory, clock):
    gs_key = Fernet.generate_key()
    directory = StaticDirectory({MEMBER_UUID: MemberRecord(MEMBER_UUID, "Ada", False)})
    ctx = await app_factory(
        SEANCE_GS_SERIALIZER_KEY=gs_key.decode(), directory=directory
    )
    cookie = GsSerializer(gs_key, clock=clock).dumps(f"{MEMBER_UUID};127.0.0.1")
    r = await ctx.client.post(
        "/v1/sessions", headers={"Origin": ALLOWED}, cookies={"SESSION": cookie}
    )
    assert r.status == 201
    session_id = (await r.json())["session_id"]
    assert await ctx.store.load_session(session_id) is not None


async def test_create_session_anon_returns_token(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post("/v1/sessions", headers={"Origin": ALLOWED})
    assert r.status == 201
    body = await r.json()
    assert body["session_id"]
    assert body["anon_token"]
    assert r.headers["Set-Cookie"].startswith("SEANCE_ANON=")


async def test_create_session_anon_is_rate_limited_per_ip_without_cookies(app_factory):
    """Fresh identities must not bypass the source-IP creation budget."""
    ctx = await app_factory(SEANCE_LIMIT_CREATES_PER_IP_HOUR="2")

    statuses = []
    for _ in range(3):
        ctx.client.session.cookie_jar.clear()
        response = await ctx.client.post("/v1/sessions", headers={"Origin": ALLOWED})
        statuses.append(response.status)

    assert statuses == [201, 201, 429]


async def test_create_session_anon_disabled_forbidden(app_factory):
    ctx = await app_factory(SEANCE_ANON_CAN_CREATE="false")
    r = await ctx.client.post("/v1/sessions", headers={"Origin": ALLOWED})
    assert r.status == 403
    assert "anon" in (await r.json())["error"].lower()


async def test_create_session_invalid_json(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions", headers={"Origin": ALLOWED}, data="not json"
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid json"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_create_session_rejects_non_finite_json_in_body(literal, app_factory):
    """The create body seeds session content, so it takes the frame rules too."""
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data='{"snapshot":{"state":[{"id":"k","value":' + literal + "}]}}",
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid json"
    assert await ctx.store.count_sessions() == 0


async def test_create_session_rejects_deeply_nested_body(app_factory):
    ctx = await app_factory()
    frame = "[" * 600 + "]" * 600
    r = await ctx.client.post(
        "/v1/sessions",
        headers={"Origin": ALLOWED, "Content-Type": "application/json"},
        data='{"snapshot":{"poly":{"programText":"","nodes":[],"frame":' + frame + "}}}",
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid json"
    assert await ctx.store.count_sessions() == 0


async def test_create_session_non_object_json_rejected(app_factory):
    # Well-formed JSON that is not an object is a distinct, more specific 400 than
    # a parse failure — the body slot must be a snapshot-bearing object.
    ctx = await app_factory()
    r = await ctx.client.post(
        "/v1/sessions", headers={"Origin": ALLOWED}, data="[1, 2, 3]"
    )
    assert r.status == 400
    assert (await r.json())["error"] == "body must be a json object"


# --------------------------------------------------------------------------- #
# GET /v1/sessions/{id}
# --------------------------------------------------------------------------- #


async def test_session_probe_frozen_open(app_factory):
    ctx = await app_factory()
    session_id = await ctx.hub.create_session(_member())
    r = await ctx.client.get(f"/v1/sessions/{session_id}")
    assert r.status == 200
    assert await r.json() == {
        "id": session_id, "open": True, "dialect": protocol.DEFAULT_DIALECT,
    }


async def test_session_probe_live_open(app_factory):
    ctx = await app_factory()
    session_id = await ctx.hub.create_session(_member())
    await ctx.hub.connect(session_id, FakeConn(_member()))
    r = await ctx.client.get(f"/v1/sessions/{session_id}")
    assert r.status == 200
    assert await r.json() == {
        "id": session_id, "open": True, "dialect": protocol.DEFAULT_DIALECT,
    }


async def test_session_probe_locked_closed(app_factory):
    ctx = await app_factory()
    session_id = await ctx.hub.create_session(_member())
    await ctx.hub.connect(session_id, FakeConn(_member()))
    ctx.hub.live[session_id].settings.locked = True
    r = await ctx.client.get(f"/v1/sessions/{session_id}")
    assert r.status == 200
    assert await r.json() == {
        "id": session_id, "open": False, "dialect": protocol.DEFAULT_DIALECT,
    }


async def test_session_probe_unknown_not_found(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/sessions/zzzzzz")
    assert r.status == 404
    assert (await r.json())["error"] == "unknown session"


async def test_session_probe_rate_limited(app_factory):
    # With the per-IP join budget pinned to 1, the first probe consumes it (still
    # reaching the 404), and the second is refused before any store lookup.
    ctx = await app_factory(SEANCE_LIMIT_JOINS_PER_IP_MIN="1")
    first = await ctx.client.get("/v1/sessions/zzzzzz")
    second = await ctx.client.get("/v1/sessions/zzzzzz")
    assert first.status == 404
    assert (await first.json())["error"] == "unknown session"
    assert second.status == 429
    assert await second.json() == {"error": "rate_limited", "retry_after": 60}


# --------------------------------------------------------------------------- #
# GET /v1/sessions/{id}/ws
# --------------------------------------------------------------------------- #


async def test_ws_route_not_implemented_without_handler(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/sessions/abc123/ws")
    assert r.status == 501
    assert (await r.json())["error"] == "not implemented"


# --------------------------------------------------------------------------- #
# GET /v1/stats
# --------------------------------------------------------------------------- #


async def test_stats_disabled_not_found(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/stats")
    assert r.status == 404


async def test_stats_enabled_reports_counts(app_factory):
    ctx = await app_factory(SEANCE_STATS_ENABLED="true")
    session_id = await ctx.hub.create_session(_member())
    await ctx.hub.connect(session_id, FakeConn(_member()))
    r = await ctx.client.get("/v1/stats")
    assert r.status == 200
    assert await r.json() == {"live_sessions": 1, "connections": 1}


# --------------------------------------------------------------------------- #
# CORS / security middleware branches
# --------------------------------------------------------------------------- #


async def test_cors_disallowed_origin_post_forbidden(app_factory):
    ctx = await app_factory()
    r = await ctx.client.post("/v1/anon", headers={"Origin": EVIL})
    assert r.status == 403
    assert (await r.json())["error"] == "origin not allowed"


async def test_cors_disallowed_origin_get_omits_headers(app_factory):
    ctx = await app_factory(SEANCE_STATS_ENABLED="true")
    r = await ctx.client.get("/v1/stats", headers={"Origin": EVIL})
    assert r.status == 200
    assert "Access-Control-Allow-Origin" not in r.headers


async def test_cors_preflight_options(app_factory):
    ctx = await app_factory()
    r = await ctx.client.request("OPTIONS", "/v1/anon", headers={"Origin": ALLOWED})
    assert r.status == 204
    assert r.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert r.headers["Access-Control-Allow-Headers"] == "Content-Type, X-Seance-Anon"
    assert r.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert r.headers["Access-Control-Allow-Credentials"] == "true"
    assert r.headers["Vary"] == "Origin"


async def test_cors_preflight_disallowed_origin_grants_nothing(app_factory):
    # A preflight from a disallowed Origin still answers 204 (advertising methods),
    # but must never echo the credentialed-CORS grant back to that origin.
    ctx = await app_factory()
    r = await ctx.client.request("OPTIONS", "/v1/anon", headers={"Origin": EVIL})
    assert r.status == 204
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Access-Control-Allow-Credentials" not in r.headers


async def test_v1_response_has_no_store_cache_control(app_factory):
    ctx = await app_factory(SEANCE_STATS_ENABLED="true")
    r = await ctx.client.get("/v1/stats")
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_ticket_allows_missing_origin(app_factory):
    # Server-to-server endpoint: no Origin header must not be rejected by CORS.
    ctx = await app_factory(SEANCE_GS_TRUSTED_NETS="127.0.0.0/8")
    r = await ctx.client.post(
        "/v1/ticket",
        headers={"X-GS-User-Id": "member-9", "X-GS-Username": "Kay"},
    )
    assert r.status == 200


async def test_unmatched_route_returns_json(app_factory):
    ctx = await app_factory()
    r = await ctx.client.get("/v1/nonexistent")
    assert r.status == 404
    assert "error" in await r.json()


async def test_unhandled_handler_error_returns_internal_500(app_factory, caplog):
    # A store/hub failure surfacing through a route must not escape to aiohttp's
    # default text/plain 500: the error middleware catches it, logs the route only,
    # and returns a generic JSON body that still carries the security headers.
    ctx = await app_factory()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    ctx.hub.create_session = _boom
    with caplog.at_level(logging.ERROR, logger="seance.http"):
        r = await ctx.client.post("/v1/sessions", headers={"Origin": ALLOWED})

    assert r.status == 500
    assert await r.json() == {"error": "internal"}
    # Proves the normalised 500 passed back out through the outer security mw.
    assert r.headers["X-Content-Type-Options"] == "nosniff"

    records = [rec for rec in caplog.records if rec.name == "seance.http"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == "unhandled error serving POST /v1/sessions"


async def test_unhandled_error_log_uses_route_template_not_session_id(app_factory, caplog):
    ctx = await app_factory()
    raw_session_id = "private-session-id"

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    ctx.store.load_session = _boom
    with caplog.at_level(logging.ERROR, logger="seance.http"):
        r = await ctx.client.get(f"/v1/sessions/{raw_session_id}")

    assert r.status == 500
    records = [rec for rec in caplog.records if rec.name == "seance.http"]
    assert len(records) == 1
    assert records[0].getMessage() == "unhandled error serving GET /v1/sessions/{id}"
    assert raw_session_id not in records[0].getMessage()
