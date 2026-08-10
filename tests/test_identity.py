"""Tests for app.identity — gs-compatible serializer, cookie/anon/ticket auth."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Config
from app.identity import (
    AuthError,
    GsSerializer,
    Identity,
    IdentityService,
    Kind,
    anon_username,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gs_serializer_vectors.json"

# Values baked into the golden ``member_cookie`` / ``ephemeral_cookie`` vectors.
_MEMBER_ID = "0f9b2d7e-1111-2222-3333-444455556666"
_MEMBER_IP = "203.0.113.7"
_EPHEMERAL_ID = "abcd1234-aaaa-bbbb-cccc-ddddeeeeffff"


@pytest.fixture
def vectors() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def gs_key(vectors) -> bytes:
    return vectors["key"].encode()


def _vector(vectors: dict, name: str) -> dict:
    return next(v for v in vectors["vectors"] if v["name"] == name)


# --- Directory stub: duck-typed lookup -> None | record{.username, .deleted} ---
@dataclass
class _Member:
    username: str
    deleted: bool = False


class StaticDir:
    def __init__(self, members: dict[str, _Member]):
        self._members = dict(members)

    async def lookup(self, member_id: str):
        return self._members.get(member_id)


class BoomDir:
    """A directory backend whose lookup raises a non-AuthError (must propagate)."""

    async def lookup(self, member_id: str):
        raise RuntimeError("directory backend exploded")


def _config(gs_key: bytes | None = None, **overrides: str) -> Config:
    env = {
        "SEANCE_SECRET": Fernet.generate_key().decode(),
        "SEANCE_DB": "x",
        "SEANCE_ALLOWED_ORIGINS": "http://t",
    }
    if gs_key is not None:
        env["SEANCE_GS_SERIALIZER_KEY"] = gs_key.decode()
    env.update(overrides)
    return Config.from_env(env)


def _service(clock, *, gs_key=None, directory=None) -> IdentityService:
    return IdentityService(_config(gs_key=gs_key), directory, clock=clock)


# --------------------------------------------------------------------------- #
# GsSerializer — byte-compatibility with groundsquirrel's serializer          #
# --------------------------------------------------------------------------- #


def test_gs_serializer_loads_golden_vectors(vectors, gs_key, clock):
    """Every gs-minted golden token decrypts byte-for-byte at the frozen ts."""
    assert clock.now == vectors["fixed_ts"]
    ser = GsSerializer(gs_key, clock=clock)
    for v in vectors["vectors"]:
        assert ser.loads(v["token"], max_age=604800) == v["payload"]


def test_gs_serializer_dumps_roundtrip_and_prefix(gs_key, clock):
    """dumps emits gs-format plaintext (``{ts}:`` prefix) our loads reads back."""
    ser = GsSerializer(gs_key, clock=clock)
    ts = int(clock.now)

    token = ser.dumps("payload-xyz")
    assert ser.loads(token) == "payload-xyz"
    raw = Fernet(gs_key).decrypt(token.encode())
    assert raw == f"{ts}:payload-xyz".encode()

    token_ttl = ser.dumps("with-ttl", expires_in=600)
    raw_ttl = Fernet(gs_key).decrypt(token_ttl.encode())
    assert raw_ttl == f"{ts}:600:with-ttl".encode()
    assert ser.loads(token_ttl) == "with-ttl"


def test_gs_serializer_tamper_raises(vectors, gs_key, clock):
    ser = GsSerializer(gs_key, clock=clock)
    token = _vector(vectors, "member_cookie")["token"]
    i = len(token) // 2
    repl = "A" if token[i] != "A" else "B"
    tampered = token[:i] + repl + token[i + 1 :]
    with pytest.raises(ValueError):
        ser.loads(tampered, max_age=604800)


def test_gs_serializer_wrong_key_raises(gs_key, clock):
    minted = GsSerializer(gs_key, clock=clock).dumps("hidden")
    other = GsSerializer(Fernet.generate_key(), clock=clock)
    with pytest.raises(ValueError):
        other.loads(minted)


def test_gs_serializer_garbage_raises(gs_key, clock):
    ser = GsSerializer(gs_key, clock=clock)
    with pytest.raises(ValueError):
        ser.loads("not-a-valid-token")


def test_gs_serializer_bad_framing_no_colon_raises(gs_key, clock):
    # Validly encrypted, but the plaintext lacks the "{ts}:" framing.
    forged = Fernet(gs_key).encrypt(b"no-colon-here").decode()
    with pytest.raises(ValueError):
        GsSerializer(gs_key, clock=clock).loads(forged)


def test_gs_serializer_bad_framing_non_digit_ts_raises(gs_key, clock):
    forged = Fernet(gs_key).encrypt(b"notdigits:payload").decode()
    with pytest.raises(ValueError):
        GsSerializer(gs_key, clock=clock).loads(forged)


def test_gs_serializer_non_utf8_payload_raises(gs_key, clock):
    forged = Fernet(gs_key).encrypt(b"1751500000:\xff\xfe").decode()
    with pytest.raises(ValueError):
        GsSerializer(gs_key, clock=clock).loads(forged)


def test_gs_serializer_embedded_ttl_expires(vectors, gs_key, clock):
    """Embedded ttl governs: valid at ts+ttl boundary, expired at ts+ttl+1."""
    assert vectors["semantics"]["embedded_ttl_expires"] is True
    ser = GsSerializer(gs_key, clock=clock)
    token = ser.dumps("data", expires_in=10)
    assert ser.loads(token) == "data"  # now == ts
    clock.advance(10)  # now == ts+10 (boundary, still valid)
    assert ser.loads(token) == "data"
    clock.advance(1)  # now == ts+11 (expired)
    with pytest.raises(ValueError):
        ser.loads(token)


def test_gs_serializer_max_age_honored_and_expired(vectors, gs_key, clock):
    assert vectors["semantics"]["max_age_within"] is True
    assert vectors["semantics"]["max_age_expired_raises"] is True
    ser = GsSerializer(gs_key, clock=clock)
    token = ser.dumps("noexpiry")  # no embedded ttl
    assert ser.loads(token, max_age=100) == "noexpiry"
    clock.advance(100)  # boundary
    assert ser.loads(token, max_age=100) == "noexpiry"
    clock.advance(1)
    with pytest.raises(ValueError):
        ser.loads(token, max_age=100)


def test_gs_serializer_embedded_ttl_overrides_max_age(gs_key, clock):
    ser = GsSerializer(gs_key, clock=clock)
    token = ser.dumps("data", expires_in=10)  # embedded 10
    clock.advance(50)  # past embedded ttl, well within max_age
    with pytest.raises(ValueError):
        ser.loads(token, max_age=10_000)  # embedded ttl wins -> expired


def test_gs_serializer_default_max_age_fallback(gs_key, clock):
    ser = GsSerializer(gs_key, clock=clock, default_max_age=100)
    token = ser.dumps("data")  # no embedded ttl, no max_age arg -> default governs
    assert ser.loads(token) == "data"
    clock.advance(101)
    with pytest.raises(ValueError):
        ser.loads(token)


def test_gs_serializer_no_expiry_when_all_absent(gs_key, clock):
    ser = GsSerializer(gs_key, clock=clock)  # default_max_age None
    token = ser.dumps("eternal")  # no embedded ttl
    clock.advance(10_000_000)
    assert ser.loads(token) == "eternal"  # never expires


# --------------------------------------------------------------------------- #
# Kind / Identity / anon_username                                             #
# --------------------------------------------------------------------------- #


def test_kind_values():
    assert Kind.MEMBER == "member"
    assert Kind.GS_EPHEMERAL == "gs_ephemeral"
    assert Kind.ANON == "anon"
    assert Kind.SERVICE == "service"


def test_anon_username():
    assert anon_username("abcdef1234567890") == "guest-abcdef"
    assert anon_username("ab") == "guest-ab"


# --------------------------------------------------------------------------- #
# validate_gs_cookie                                                          #
# --------------------------------------------------------------------------- #


async def test_validate_gs_cookie_member_happy(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    token = _vector(vectors, "member_cookie")["token"]
    ident = await svc.validate_gs_cookie(token, _MEMBER_IP)
    assert ident == Identity(user_id=_MEMBER_ID, username="alice", kind=Kind.MEMBER)


async def test_validate_gs_cookie_wrong_ip(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    token = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(token, "198.51.100.9")


async def test_validate_gs_cookie_deleted_member(vectors, gs_key, clock):
    svc = _service(
        clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice", deleted=True)})
    )
    token = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(token, _MEMBER_IP)


async def test_validate_gs_cookie_missing_member(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({}))
    token = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(token, _MEMBER_IP)


async def test_validate_gs_cookie_non_uuid_member(gs_key, clock):
    cookie = GsSerializer(gs_key, clock=clock).dumps(f"not-a-uuid;{_MEMBER_IP}")
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({"not-a-uuid": _Member("x")}))
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(cookie, _MEMBER_IP)


async def test_validate_gs_cookie_uppercase_uuid_rejected(gs_key, clock):
    # gs member ids are lowercase hex; an uppercase id fails the shape check
    # BEFORE the directory is consulted (even though the member exists here).
    upper_id = _MEMBER_ID.upper()
    cookie = GsSerializer(gs_key, clock=clock).dumps(f"{upper_id};{_MEMBER_IP}")
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({upper_id: _Member("x")}))
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(cookie, _MEMBER_IP)


async def test_validate_gs_cookie_member_without_directory(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=None)
    token = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(token, _MEMBER_IP)


async def test_validate_gs_cookie_ephemeral(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=None)  # no directory needed
    token = _vector(vectors, "ephemeral_cookie")["token"]
    ident = await svc.validate_gs_cookie(token, _MEMBER_IP)
    assert ident.kind is Kind.GS_EPHEMERAL
    assert ident.user_id == _EPHEMERAL_ID
    assert ident.invited_by == _MEMBER_ID
    assert ident.username == "guest-abcd12"


async def test_validate_gs_cookie_ephemeral_no_invited_by(gs_key, clock):
    uid = "eeee1111-2222-3333-4444-555566667777"
    cookie = GsSerializer(gs_key, clock=clock).dumps(
        f"{uid};{_MEMBER_IP};ephemeral", expires_in=5400
    )
    svc = _service(clock, gs_key=gs_key, directory=None)
    ident = await svc.validate_gs_cookie(cookie, _MEMBER_IP)
    assert ident.kind is Kind.GS_EPHEMERAL
    assert ident.invited_by is None
    assert ident.username == "guest-eeee11"


async def test_validate_gs_cookie_ephemeral_empty_invited_by(gs_key, clock):
    uid = "eeee1111-2222-3333-4444-555566667777"
    cookie = GsSerializer(gs_key, clock=clock).dumps(
        f"{uid};{_MEMBER_IP};ephemeral;", expires_in=5400
    )
    svc = _service(clock, gs_key=gs_key, directory=None)
    ident = await svc.validate_gs_cookie(cookie, _MEMBER_IP)
    assert ident.invited_by is None


async def test_validate_gs_cookie_malformed_single_part(gs_key, clock):
    cookie = GsSerializer(gs_key, clock=clock).dumps("only-one-part")
    svc = _service(clock, gs_key=gs_key, directory=None)
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(cookie, _MEMBER_IP)


async def test_validate_gs_cookie_expired(gs_key, clock):
    cookie = GsSerializer(gs_key, clock=clock).dumps(f"{_MEMBER_ID};{_MEMBER_IP}", expires_in=100)
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    clock.advance(101)
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie(cookie, _MEMBER_IP)


async def test_validate_gs_cookie_no_key_configured(clock):
    svc = _service(clock, gs_key=None, directory=None)
    with pytest.raises(AuthError):
        await svc.validate_gs_cookie("whatever", _MEMBER_IP)


# --------------------------------------------------------------------------- #
# anon mint / verify                                                          #
# --------------------------------------------------------------------------- #


def test_mint_and_verify_anon(clock):
    svc = _service(clock)
    ident, token = svc.mint_anon()
    assert ident.kind is Kind.ANON
    assert ident.username == anon_username(ident.user_id)
    assert svc.verify_anon(token) == ident


def test_verify_anon_expired(clock):
    svc = _service(clock)
    _, token = svc.mint_anon()
    clock.advance(2_592_000 + 1)  # anon_ttl default + 1
    with pytest.raises(AuthError):
        svc.verify_anon(token)


def test_verify_anon_garbage(clock):
    svc = _service(clock)
    with pytest.raises(AuthError):
        svc.verify_anon("garbage")


def test_verify_anon_wrong_flag(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    bad = GsSerializer(config.secret, clock=clock).dumps(
        json.dumps({"anon": False, "id": "x", "v": 1}), expires_in=100
    )
    with pytest.raises(AuthError):
        svc.verify_anon(bad)


def test_verify_anon_missing_id(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    bad = GsSerializer(config.secret, clock=clock).dumps(
        json.dumps({"anon": True, "v": 1}), expires_in=100
    )
    with pytest.raises(AuthError):
        svc.verify_anon(bad)


def test_verify_anon_wrong_key(gs_key, clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    bad = GsSerializer(gs_key, clock=clock).dumps(
        json.dumps({"anon": True, "id": "x", "v": 1}), expires_in=100
    )
    with pytest.raises(AuthError):
        svc.verify_anon(bad)


def test_verify_anon_non_object_json(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    bad = GsSerializer(config.secret, clock=clock).dumps(json.dumps([1, 2, 3]), expires_in=100)
    with pytest.raises(AuthError):
        svc.verify_anon(bad)


def test_verify_anon_empty_id(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    bad = GsSerializer(config.secret, clock=clock).dumps(
        json.dumps({"anon": True, "id": "", "v": 1}), expires_in=100
    )
    with pytest.raises(AuthError):
        svc.verify_anon(bad)


# --------------------------------------------------------------------------- #
# ticket mint / redeem (single-use)                                           #
# --------------------------------------------------------------------------- #


def test_mint_and_redeem_ticket(clock):
    svc = _service(clock)
    token = svc.mint_ticket("u-1", "alice", Kind.MEMBER)
    ident = svc.redeem_ticket(token)
    assert ident == Identity(user_id="u-1", username="alice", kind=Kind.MEMBER)


def test_ticket_single_use(clock):
    svc = _service(clock)
    token = svc.mint_ticket("u-1", "alice", Kind.MEMBER)
    svc.redeem_ticket(token)
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_expired(clock):
    svc = _service(clock)
    token = svc.mint_ticket("u-1", "alice", Kind.MEMBER)
    clock.advance(120 + 1)  # ticket_ttl default + 1
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_invalid_kind(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    payload = json.dumps(
        {"t": 1, "jti": "j-1", "user_id": "u", "username": "a", "kind": "bogus"}
    )
    token = GsSerializer(config.secret, clock=clock).dumps(payload, expires_in=120)
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_garbage(clock):
    svc = _service(clock)
    with pytest.raises(AuthError):
        svc.redeem_ticket("garbage")


def test_ticket_missing_jti(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    payload = json.dumps({"t": 1, "user_id": "u", "username": "a", "kind": "member"})
    token = GsSerializer(config.secret, clock=clock).dumps(payload, expires_in=120)
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_non_object_json(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    token = GsSerializer(config.secret, clock=clock).dumps(json.dumps("scalar"), expires_in=120)
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_non_string_fields(clock):
    config = _config()
    svc = IdentityService(config, None, clock=clock)
    payload = json.dumps(
        {"t": 1, "jti": "j-1", "user_id": 123, "username": "a", "kind": "member"}
    )
    token = GsSerializer(config.secret, clock=clock).dumps(payload, expires_in=120)
    with pytest.raises(AuthError):
        svc.redeem_ticket(token)


def test_ticket_jti_cache_prunes_expired(clock):
    svc = _service(clock)
    t1 = svc.mint_ticket("u-1", "a", Kind.MEMBER)
    svc.redeem_ticket(t1)
    assert len(svc._seen_jti) == 1
    clock.advance(120 + 1)  # t1's jti entry now past expiry
    t2 = svc.mint_ticket("u-2", "b", Kind.ANON)
    svc.redeem_ticket(t2)  # prune runs on redeem, evicting t1's stale jti
    assert len(svc._seen_jti) == 1  # only t2 remains


def test_ticket_jti_cache_evicts_oldest_when_full(clock):
    svc = _service(clock)
    svc._JTI_CAP = 2  # shrink the hard cap for the test
    jtis = []
    for i in range(3):
        token = svc.mint_ticket(f"u-{i}", "n", Kind.ANON)
        # capture the jti minted into this token before it is consumed
        payload = json.loads(GsSerializer(svc._config.secret, clock=clock).loads(token))
        jtis.append(payload["jti"])
        svc.redeem_ticket(token)
        clock.advance(1)  # stagger expiries so the oldest is unambiguous
    assert len(svc._seen_jti) == 2  # capped
    assert jtis[0] not in svc._seen_jti  # oldest-expiry evicted first
    assert jtis[1] in svc._seen_jti
    assert jtis[2] in svc._seen_jti


# --------------------------------------------------------------------------- #
# resolve precedence matrix                                                   #
# --------------------------------------------------------------------------- #


async def test_resolve_ticket_wins(clock):
    svc = _service(clock)
    token = svc.mint_ticket("u-9", "bob", Kind.MEMBER)
    ident, new = await svc.resolve(
        ticket=token, anon_token=None, cookie=None, client_ip=_MEMBER_IP
    )
    assert ident == Identity("u-9", "bob", Kind.MEMBER)
    assert new is None


async def test_resolve_invalid_ticket_raises_no_fallthrough(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    cookie = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(AuthError):
        await svc.resolve(
            ticket="garbage", anon_token=None, cookie=cookie, client_ip=_MEMBER_IP
        )


async def test_resolve_cookie_when_no_ticket(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    cookie = _vector(vectors, "member_cookie")["token"]
    ident, new = await svc.resolve(
        ticket=None, anon_token=None, cookie=cookie, client_ip=_MEMBER_IP
    )
    assert ident.kind is Kind.MEMBER
    assert ident.username == "alice"
    assert new is None


async def test_resolve_directory_error_propagates_no_anon_fallthrough(vectors, gs_key, clock):
    # A valid cookie reaches the directory; a non-AuthError from the backend must
    # propagate out of resolve, never silently downgrade to a fresh anon.
    svc = _service(clock, gs_key=gs_key, directory=BoomDir())
    cookie = _vector(vectors, "member_cookie")["token"]
    with pytest.raises(RuntimeError):
        await svc.resolve(
            ticket=None, anon_token=None, cookie=cookie, client_ip=_MEMBER_IP
        )


async def test_resolve_invalid_cookie_mints_fresh_anon(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    cookie = _vector(vectors, "member_cookie")["token"]  # valid token, wrong IP below
    ident, new = await svc.resolve(
        ticket=None, anon_token=None, cookie=cookie, client_ip="10.9.9.9"
    )
    assert ident.kind is Kind.ANON
    assert new is not None
    assert svc.verify_anon(new).user_id == ident.user_id


async def test_resolve_invalid_cookie_falls_to_anon_token(vectors, gs_key, clock):
    svc = _service(clock, gs_key=gs_key, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    anon_ident, anon_token = svc.mint_anon()
    cookie = _vector(vectors, "member_cookie")["token"]
    ident, new = await svc.resolve(
        ticket=None, anon_token=anon_token, cookie=cookie, client_ip="10.9.9.9"
    )
    assert ident == anon_ident
    assert new is None


async def test_resolve_anon_token_when_no_cookie(clock):
    svc = _service(clock)
    anon_ident, anon_token = svc.mint_anon()
    ident, new = await svc.resolve(
        ticket=None, anon_token=anon_token, cookie=None, client_ip=_MEMBER_IP
    )
    assert ident == anon_ident
    assert new is None


async def test_resolve_invalid_anon_token_mints_fresh(clock):
    svc = _service(clock)
    ident, new = await svc.resolve(
        ticket=None, anon_token="garbage", cookie=None, client_ip=_MEMBER_IP
    )
    assert ident.kind is Kind.ANON
    assert new is not None


async def test_resolve_nothing_mints_fresh(clock):
    svc = _service(clock)
    ident, new = await svc.resolve(
        ticket=None, anon_token=None, cookie=None, client_ip=_MEMBER_IP
    )
    assert ident.kind is Kind.ANON
    assert new is not None
    assert svc.verify_anon(new) == ident


async def test_resolve_cookie_ignored_when_gs_key_unset(vectors, gs_key, clock):
    svc = _service(clock, gs_key=None, directory=StaticDir({_MEMBER_ID: _Member("alice")}))
    anon_ident, anon_token = svc.mint_anon()
    cookie = _vector(vectors, "member_cookie")["token"]
    ident, new = await svc.resolve(
        ticket=None, anon_token=anon_token, cookie=cookie, client_ip=_MEMBER_IP
    )
    assert ident == anon_ident  # cookie path skipped (no gs key), anon used
    assert new is None
