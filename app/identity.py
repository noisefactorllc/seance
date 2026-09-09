"""Identity resolution for the seance server.

Every connection resolves to an :class:`Identity` whose ``user_id`` and
``username`` are *server-derived only* — nothing identity-bearing is ever trusted
from the client payload. Three credential sources, in precedence order:

1. a single-use, short-lived :meth:`IdentityService.mint_ticket` token, used for
   gs-proxied member auth and redeemed exactly once
   (:meth:`IdentityService.redeem_ticket`);
2. a groundsquirrel ``SESSION`` cookie, validated byte-for-byte against gs's own
   serializer format and its client-IP binding (:class:`GsSerializer`,
   :meth:`IdentityService.validate_gs_cookie`), enabled only when a gs serializer
   key is configured;
3. a self-minted anonymous token (:meth:`IdentityService.mint_anon` /
   :meth:`IdentityService.verify_anon`).

When none is present, :meth:`IdentityService.resolve` mints a fresh anonymous
identity. :exc:`AuthError` reasons are short strings for logs and never carry
token or cookie material.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

from app.ratelimit import KeyedLimiter

if TYPE_CHECKING:
    from app.config import Config
    from app.directory import MemberDirectory

# gs member ids are lowercase-hex uuids; an uppercase id fails the shape check
# here rather than reaching the directory.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# The window ``anon_mints_per_ip_hour`` is counted over.
_MINT_WINDOW = 3600.0


class Kind(StrEnum):
    """Identity kind. ``str(kind)`` yields the wire string."""

    MEMBER = "member"
    GS_EPHEMERAL = "gs_ephemeral"
    ANON = "anon"
    SERVICE = "service"


@dataclass(frozen=True)
class Identity:
    """A resolved identity. ``user_id`` is the authoritative key everywhere."""

    user_id: str
    username: str
    kind: Kind
    invited_by: str | None = None


class AuthError(Exception):
    """Raised when no valid identity can be established from a credential.

    The message is a short reason code for logs; it never contains token or
    cookie material.
    """


class MintLimited(AuthError):
    """A source asked for more fresh anonymous identities than its hourly budget.

    A subclass of :class:`AuthError` so any caller that only knows how to refuse
    a credential still refuses; callers that can say "slow down" answer with the
    rate-limited status or close code instead.
    """


def anon_username(user_id: str) -> str:
    """Display name for anonymous and gs-ephemeral identities (gs convention)."""
    return f"guest-{user_id[:6]}"


class GsSerializer:
    """Byte-compatible re-implementation of groundsquirrel's timed serializer.

    A token is ``Fernet(f"{ts}:{payload}")`` or, with a ttl,
    ``Fernet(f"{ts}:{ttl}:{payload}")`` where ``ts = int(clock())``. On load the
    leading ``ts`` is split off; if the next colon-delimited field is all digits
    it is treated as an embedded ttl and the remainder is the payload. The
    effective ttl is the embedded ttl, else the ``max_age`` argument, else
    ``default_max_age``; when all three are absent the token never expires.
    ``int(clock()) > ts + ttl`` means expired.

    Every failure mode — bad Fernet token, tampering, malformed framing, or
    expiry — raises :exc:`ValueError`, so callers catch a single exception type.
    """

    def __init__(
        self,
        key: bytes,
        clock: Callable[[], float] = time.time,
        default_max_age: int | None = None,
    ) -> None:
        self._fernet = Fernet(key)
        self._clock = clock
        self._default_max_age = default_max_age

    def dumps(self, data: str, expires_in: int | None = None) -> str:
        ts = int(self._clock())
        if expires_in is None:
            plaintext = f"{ts}:{data}"
        else:
            plaintext = f"{ts}:{expires_in}:{data}"
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def loads(self, token: str, max_age: int | None = None) -> str:
        try:
            raw = self._fernet.decrypt(token.encode("utf-8"))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError("Invalid token") from exc

        head, sep, remainder = raw.partition(b":")
        if not sep or not head.isdigit():
            raise ValueError("Invalid token format")
        ts = int(head)

        embedded_ttl: int | None = None
        payload = remainder
        candidate, sep2, rest = remainder.partition(b":")
        if sep2 and candidate.isdigit():
            embedded_ttl = int(candidate)
            payload = rest

        if embedded_ttl is not None:
            effective_ttl: int | None = embedded_ttl
        elif max_age is not None:
            effective_ttl = max_age
        else:
            effective_ttl = self._default_max_age

        if effective_ttl is not None and int(self._clock()) > ts + effective_ttl:
            raise ValueError("Token has expired")

        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid token payload") from exc


class IdentityService:
    """Resolves and mints identities from tickets, gs cookies, and anon tokens."""

    _JTI_CAP = 10_000

    def __init__(
        self,
        config: Config,
        directory: MemberDirectory | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._directory = directory
        self._clock = clock
        self._limits = config.limits
        # One budget for every path that mints a fresh anonymous identity: the
        # /v1/anon endpoint, /v1/me, session create, and the websocket hello.
        # Metering only one of them capped a quarter of the supply.
        self._mints = KeyedLimiter(self._limits.anon_mints_per_ip_hour, _MINT_WINDOW, clock)
        # Anon tokens and tickets are signed with seance's own key.
        self._local = GsSerializer(config.secret, clock=clock)
        # gs cookies use gs's serializer key; mirror gs's fall back to its
        # session ttl when neither an embedded ttl nor a max_age is supplied.
        gs_key = config.gs_serializer_key
        self._gs = (
            GsSerializer(gs_key, clock=clock, default_max_age=config.gs_session_ttl)
            if gs_key is not None
            else None
        )
        self._seen_jti: dict[str, float] = {}

    # -- anon ---------------------------------------------------------------- #

    def mint_anon(self) -> tuple[Identity, str]:
        user_id = str(uuid.uuid4())
        payload = json.dumps({"anon": True, "id": user_id, "v": 1})
        token = self._local.dumps(payload, expires_in=self._limits.anon_ttl)
        identity = Identity(user_id=user_id, username=anon_username(user_id), kind=Kind.ANON)
        return identity, token

    def verify_anon(self, token: str) -> Identity:
        try:
            data = json.loads(self._local.loads(token))
        except ValueError as exc:
            raise AuthError("invalid anon token") from exc
        if not isinstance(data, dict) or data.get("anon") is not True:
            raise AuthError("invalid anon token")
        user_id = data.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise AuthError("invalid anon token")
        return Identity(user_id=user_id, username=anon_username(user_id), kind=Kind.ANON)

    # -- ticket -------------------------------------------------------------- #

    def mint_ticket(self, user_id: str, username: str, kind: Kind) -> str:
        payload = json.dumps(
            {
                "t": 1,
                "jti": str(uuid.uuid4()),
                "user_id": user_id,
                "username": username,
                "kind": kind.value,
            }
        )
        return self._local.dumps(payload, expires_in=self._limits.ticket_ttl)

    def redeem_ticket(self, token: str) -> Identity:
        try:
            data = json.loads(self._local.loads(token))
        except ValueError as exc:
            raise AuthError("invalid ticket") from exc
        if not isinstance(data, dict):
            raise AuthError("invalid ticket")
        jti = data.get("jti")
        if not isinstance(jti, str) or not jti:
            raise AuthError("invalid ticket")

        now = self._clock()
        self._prune_jti(now)
        if jti in self._seen_jti:
            raise AuthError("ticket already redeemed")

        user_id = data.get("user_id")
        username = data.get("username")
        kind_raw = data.get("kind")
        if (
            not isinstance(user_id, str)
            or not isinstance(username, str)
            or not isinstance(kind_raw, str)
        ):
            raise AuthError("invalid ticket")
        try:
            kind = Kind(kind_raw)
        except ValueError as exc:
            raise AuthError("invalid ticket kind") from exc

        self._record_jti(jti, now + self._limits.ticket_ttl)
        return Identity(user_id=user_id, username=username, kind=kind)

    def _prune_jti(self, now: float) -> None:
        expired = [jti for jti, exp in self._seen_jti.items() if exp <= now]
        for jti in expired:
            del self._seen_jti[jti]

    def _record_jti(self, jti: str, expiry: float) -> None:
        if len(self._seen_jti) >= self._JTI_CAP:
            oldest = min(self._seen_jti, key=self._seen_jti.__getitem__)
            del self._seen_jti[oldest]
        self._seen_jti[jti] = expiry

    # -- gs cookie ----------------------------------------------------------- #

    async def validate_gs_cookie(self, cookie: str, client_ip: str) -> Identity:
        if self._gs is None:
            raise AuthError("gs cookies not configured")
        try:
            payload = self._gs.loads(cookie, max_age=self._config.gs_session_ttl)
        except ValueError as exc:
            raise AuthError("invalid gs cookie") from exc

        parts = payload.split(";")
        if len(parts) < 2:
            raise AuthError("malformed gs cookie")
        if parts[1] != client_ip:
            raise AuthError("gs cookie ip mismatch")

        if len(parts) > 2 and parts[2] == "ephemeral":
            user_id = parts[0]
            invited_by = parts[3] if len(parts) > 3 and parts[3] else None
            return Identity(
                user_id=user_id,
                username=anon_username(user_id),
                kind=Kind.GS_EPHEMERAL,
                invited_by=invited_by,
            )

        member_id = parts[0]
        if not _UUID_RE.match(member_id):
            raise AuthError("gs cookie not a member id")
        if self._directory is None:
            raise AuthError("member directory unavailable")
        record = await self._directory.lookup(member_id)
        if record is None or record.deleted:
            raise AuthError("member not found")
        return Identity(user_id=member_id, username=record.username, kind=Kind.MEMBER)

    # -- resolve ------------------------------------------------------------- #

    async def resolve(
        self,
        *,
        ticket: str | None,
        anon_token: str | None,
        cookie: str | None,
        client_ip: str,
    ) -> tuple[Identity, str | None]:
        if ticket is not None:
            # Ticket presence is a definite auth intent: no silent fallthrough.
            return self.redeem_ticket(ticket), None

        if cookie is not None and self._config.gs_serializer_key is not None:
            try:
                return await self.validate_gs_cookie(cookie, client_ip), None
            except AuthError:
                # validate_gs_cookie normalizes every credential failure to
                # AuthError; a non-AuthError (e.g. a directory backend fault)
                # must propagate, not silently downgrade to anon.
                pass  # invalid/expired cookie -> try the next source

        if anon_token is not None:
            try:
                return self.verify_anon(anon_token), None
            except AuthError:
                pass  # invalid/expired anon token -> mint a fresh one

        # Only a mint is metered: an identity that comes back with a valid
        # credential costs nothing and must never be refused for rate.
        if not self.take_mint(client_ip):
            raise MintLimited("anon mint rate limit exceeded")
        return self.mint_anon()

    def take_mint(self, client_ip: str) -> bool:
        """Charge one fresh anonymous identity to ``client_ip``; False when spent."""
        return self._mints.take(client_ip)
