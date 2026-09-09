"""Typed, env-driven configuration for the seance server.

Parses a process-environment mapping into an immutable :class:`Config`, the
foundation every other module consumes. Validation is fail-fast: a missing
required variable or a malformed value raises :class:`ConfigError` naming the
offending variable. The only I/O performed is reading ``*_FILE`` secret paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

DEFAULT_BIND = "0.0.0.0:8000"

# Dedicated env aliases that map onto a Limits field. A matching
# ``SEANCE_LIMIT_<FIELD>`` always wins over its alias when both are set.
_LIMIT_ALIASES = {
    "SEANCE_ANON_TTL": "anon_ttl",
    "SEANCE_TICKET_TTL": "ticket_ttl",
    "SEANCE_GS_SESSION_TTL": "gs_session_ttl",
    "SEANCE_FREEZE_GRACE": "freeze_grace",
    "SEANCE_PING_INTERVAL": "ping_interval",
    "SEANCE_PING_TIMEOUT": "ping_timeout",
    "SEANCE_CHECKPOINT_OPS": "checkpoint_ops",
    "SEANCE_CHECKPOINT_SECS": "checkpoint_secs",
}

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised when the environment cannot be parsed into a valid Config."""


@dataclass(frozen=True)
class Limits:
    """Tunable operational limits; every field is env-overridable."""

    max_frame: int = 65536
    max_snapshot_frame: int = 1_048_576
    fast_rate: float = 60.0
    fast_burst: int = 120
    proposal_rate: float = 10.0
    proposal_burst: int = 20
    chat_rate: float = 1.0
    chat_burst: int = 5
    control_rate: float = 5.0
    control_burst: int = 10
    snapshot_rate: float = 0.2
    snapshot_burst: int = 2
    abuse_window: float = 10.0
    anon_mints_per_ip_hour: int = 10
    creates_per_ip_hour: int = 10
    joins_per_ip_min: int = 30
    creates_per_identity_hour: int = 30
    max_clients: int = 16
    max_conns_per_user: int = 8
    max_state_keys: int = 4096
    max_nodes: int = 2000
    max_node_text: int = 65536
    max_program_text: int = 262144
    max_docs_per_session: int = 8
    max_doc_id_len: int = 128
    max_doc_title_len: int = 128
    max_doc_text: int = 262144
    max_doc_edit_text: int = 65536
    max_doc_oplog: int = 500
    max_doc_oplog_bytes: int = 1_048_576
    max_session_bytes: int = 8_388_608
    max_value_bytes: int = 8192
    max_state_id_len: int = 128
    chat_history: int = 200
    max_chat_len: int = 2000
    recall_window: float = 120.0
    max_violations: int = 3
    ping_interval: float = 20.0
    ping_timeout: float = 60.0
    freeze_grace: float = 60.0
    checkpoint_ops: int = 500
    checkpoint_secs: float = 30.0
    frozen_session_ttl: float = 86_400.0
    unclaimed_session_ttl: float = 3_600.0
    max_sessions: int = 1000
    max_connections: int = 4096
    send_queue_frames: int = 256
    send_queue_bytes: int = 1_048_576
    anon_ttl: int = 2_592_000
    ticket_ttl: int = 120
    gs_session_ttl: int = 604800


@dataclass(frozen=True)
class Config:
    """Fully-validated runtime configuration."""

    bind_host: str
    bind_port: int
    secret: bytes
    db_path: str
    gs_serializer_key: bytes | None
    directory_dsn: str | None
    gs_trusted_nets: tuple[IPv4Network | IPv6Network, ...]
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...]
    allowed_origins: frozenset[str]
    anon_can_create: bool
    service_secrets: dict[str, str]
    stats_enabled: bool
    limits: Limits

    @property
    def gs_session_ttl(self) -> int:
        """Convenience mirror of ``limits.gs_session_ttl``."""
        return self.limits.gs_session_ttl

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        bind_host, bind_port = _parse_bind(env)
        db_path = _get(env, "SEANCE_DB")
        if db_path is None:
            raise ConfigError("SEANCE_DB is required")
        return cls(
            bind_host=bind_host,
            bind_port=bind_port,
            secret=_require_fernet(env, "SEANCE_SECRET"),
            db_path=db_path,
            gs_serializer_key=_optional_fernet(env, "SEANCE_GS_SERIALIZER_KEY"),
            directory_dsn=_get(env, "SEANCE_DIRECTORY_DSN"),
            gs_trusted_nets=_parse_cidrs(env, "SEANCE_GS_TRUSTED_NETS"),
            trusted_proxies=_parse_cidrs(env, "SEANCE_TRUSTED_PROXIES"),
            allowed_origins=_parse_origins(env, "SEANCE_ALLOWED_ORIGINS"),
            anon_can_create=_parse_bool(env, "SEANCE_ANON_CAN_CREATE", default=True),
            service_secrets=_parse_service_secrets(env, "SEANCE_SERVICE_SECRETS"),
            stats_enabled=_parse_bool(env, "SEANCE_STATS_ENABLED", default=False),
            limits=_build_limits(env),
        )


def _get(env: Mapping[str, str], key: str) -> str | None:
    """Return the env value for ``key``, treating an empty string as unset."""
    value = env.get(key)
    if value is None or value == "":
        return None
    return value


def _parse_bind(env: Mapping[str, str]) -> tuple[str, int]:
    raw = _get(env, "SEANCE_BIND") or DEFAULT_BIND
    host, sep, port_text = raw.rpartition(":")
    if not sep:
        raise ConfigError(f"SEANCE_BIND: expected host:port, got {raw!r}")
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ConfigError(f"SEANCE_BIND: missing host in {raw!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"SEANCE_BIND: invalid port in {raw!r}") from exc
    if not 0 < port <= 65535:
        raise ConfigError(f"SEANCE_BIND: port out of range in {raw!r}")
    return host, port


def _read_secret_material(env: Mapping[str, str], base_key: str) -> tuple[str | None, str]:
    """Resolve secret material for ``base_key``; the ``*_FILE`` variant wins."""
    file_key = base_key + "_FILE"
    file_path = _get(env, file_key)
    if file_path is not None:
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{file_key}: cannot read {file_path!r}: {exc}") from exc
        return content.strip(), file_key
    inline = _get(env, base_key)
    if inline is not None:
        return inline.strip(), base_key
    return None, base_key


def _validate_fernet(material: str, var_name: str) -> bytes:
    try:
        key = material.encode("ascii")
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{var_name}: invalid Fernet key") from exc
    return key


def _require_fernet(env: Mapping[str, str], base_key: str) -> bytes:
    material, source = _read_secret_material(env, base_key)
    if material is None:
        raise ConfigError(f"{base_key} (or {base_key}_FILE) is required")
    return _validate_fernet(material, source)


def _optional_fernet(env: Mapping[str, str], base_key: str) -> bytes | None:
    material, source = _read_secret_material(env, base_key)
    if material is None:
        return None
    return _validate_fernet(material, source)


def _parse_cidrs(env: Mapping[str, str], key: str) -> tuple[IPv4Network | IPv6Network, ...]:
    raw = _get(env, key)
    if raw is None:
        return ()
    networks: list[IPv4Network | IPv6Network] = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            networks.append(ip_network(text, strict=False))
        except ValueError as exc:
            raise ConfigError(f"{key}: invalid CIDR {text!r}") from exc
    return tuple(networks)


def _parse_origins(env: Mapping[str, str], key: str) -> frozenset[str]:
    raw = _get(env, key)
    if raw is None:
        return frozenset()
    origins = {_normalize_origin(item.strip(), key) for item in raw.split(",") if item.strip()}
    return frozenset(origins)


def _normalize_origin(value: str, key: str) -> str:
    if "*" in value:
        raise ConfigError(f"{key}: wildcard origin not allowed: {value!r}")
    trimmed = value[:-1] if value.endswith("/") else value
    try:
        parts = urlsplit(trimmed)
        scheme = parts.scheme
        host = parts.hostname
        port = parts.port
        has_extra = bool(parts.path or parts.query or parts.fragment)
        has_userinfo = bool(parts.username or parts.password)
    except ValueError as exc:
        raise ConfigError(f"{key}: invalid origin {value!r}") from exc
    if scheme not in ("http", "https"):
        raise ConfigError(f"{key}: origin scheme must be http or https: {value!r}")
    if not host:
        raise ConfigError(f"{key}: origin missing host: {value!r}")
    if has_extra:
        raise ConfigError(f"{key}: origin must not include path/query/fragment: {value!r}")
    if has_userinfo:
        raise ConfigError(f"{key}: origin must not include userinfo: {value!r}")
    host = host.lower()
    if ":" in host:  # IPv6 literal — re-bracket for a canonical origin string.
        host = f"[{host}]"
    if port is not None:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _parse_bool(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{key}: invalid boolean {raw!r}")


def _parse_service_secrets(env: Mapping[str, str], key: str) -> dict[str, str]:
    raw = _get(env, key)
    if raw is None:
        return {}
    result: dict[str, str] = {}
    for index, item in enumerate(raw.split(","), start=1):
        text = item.strip()
        if not text:
            continue
        name, sep, secret = text.partition(":")
        name = name.strip()
        secret = secret.strip()
        if not sep or not name or not secret:
            raise ConfigError(f"{key}: invalid entry {index}; expected name:secret")
        result[name] = secret
    return result


def _build_limits(env: Mapping[str, str]) -> Limits:
    base = Limits()
    defaults = {f.name: getattr(base, f.name) for f in fields(Limits)}
    overrides: dict[str, int | float] = {}
    for var_name, field_name in _LIMIT_ALIASES.items():
        raw = _get(env, var_name)
        if raw is not None:
            overrides[field_name] = _coerce_number(raw, defaults[field_name], var_name)
    for field_name, default_value in defaults.items():
        var_name = "SEANCE_LIMIT_" + field_name.upper()
        raw = _get(env, var_name)
        if raw is not None:
            overrides[field_name] = _coerce_number(raw, default_value, var_name)
    if not overrides:
        return base
    return replace(base, **overrides)


def _coerce_number(raw: str, default_value: int | float, var_name: str) -> int | float:
    try:
        if isinstance(default_value, float):
            return float(raw)
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{var_name}: invalid number {raw!r}") from exc
