"""Tests for app.config — env parsing and fail-fast validation."""

import dataclasses
import ipaddress

import pytest
from cryptography.fernet import Fernet

from app.config import Config, ConfigError, Limits


def _key() -> str:
    return Fernet.generate_key().decode()


def _minimal_env(**overrides: str) -> dict[str, str]:
    env = {"SEANCE_SECRET": _key(), "SEANCE_DB": "seance.db"}
    env.update(overrides)
    return env


# Canonical Limits table (Global Constraints §, verbatim names/values/types).
_EXPECTED_LIMITS = {
    "max_frame": (65536, int),
    "max_snapshot_frame": (1_048_576, int),
    "fast_rate": (60.0, float),
    "fast_burst": (120, int),
    "proposal_rate": (10.0, float),
    "proposal_burst": (20, int),
    "chat_rate": (1.0, float),
    "chat_burst": (5, int),
    "control_rate": (5.0, float),
    "control_burst": (10, int),
    "snapshot_rate": (0.2, float),
    "snapshot_burst": (2, int),
    "abuse_window": (10.0, float),
    "anon_mints_per_ip_hour": (10, int),
    "creates_per_ip_hour": (10, int),
    "joins_per_ip_min": (30, int),
    "creates_per_identity_hour": (30, int),
    "max_clients": (16, int),
    "max_conns_per_user": (8, int),
    "max_state_keys": (4096, int),
    "max_nodes": (2000, int),
    "max_node_text": (65536, int),
    "max_program_text": (262144, int),
    "max_docs_per_session": (8, int),
    "max_doc_id_len": (128, int),
    "max_doc_title_len": (128, int),
    "max_doc_text": (262144, int),
    "max_doc_edit_text": (65536, int),
    "max_doc_oplog": (500, int),
    "max_doc_oplog_bytes": (1_048_576, int),
    "max_session_bytes": (8_388_608, int),
    "max_value_bytes": (8192, int),
    "max_state_id_len": (128, int),
    "chat_history": (200, int),
    "max_chat_len": (2000, int),
    "recall_window": (120.0, float),
    "max_violations": (3, int),
    "ping_interval": (20.0, float),
    "ping_timeout": (60.0, float),
    "freeze_grace": (60.0, float),
    "checkpoint_ops": (500, int),
    "checkpoint_secs": (30.0, float),
    "frozen_session_ttl": (86400.0, float),
    "unclaimed_session_ttl": (3600.0, float),
    "max_sessions": (1000, int),
    "max_connections": (4096, int),
    "send_queue_frames": (256, int),
    "send_queue_bytes": (1_048_576, int),
    "anon_ttl": (2_592_000, int),
    "ticket_ttl": (120, int),
    "gs_session_ttl": (604800, int),
}


def test_limits_defaults_exact():
    """Every canonical Limits field exists with the exact name, value, and type."""
    limits = Config.from_env(_minimal_env()).limits
    got = {f.name: getattr(limits, f.name) for f in dataclasses.fields(Limits)}
    assert set(got) == set(_EXPECTED_LIMITS)
    for name, (value, typ) in _EXPECTED_LIMITS.items():
        assert got[name] == value, name
        assert type(got[name]) is typ, name


def test_minimal_valid_env_parses_with_defaults():
    """Minimal valid env yields every documented default."""
    cfg = Config.from_env(_minimal_env())
    assert cfg.bind_host == "0.0.0.0"  # noqa: S104 — documented bind-all default
    assert cfg.bind_port == 8000
    assert isinstance(cfg.secret, bytes)
    assert cfg.db_path == "seance.db"
    assert cfg.gs_serializer_key is None
    assert cfg.directory_dsn is None
    assert cfg.gs_trusted_nets == ()
    assert cfg.trusted_proxies == ()
    assert cfg.allowed_origins == frozenset()
    assert cfg.anon_can_create is True
    assert cfg.service_secrets == {}
    assert cfg.stats_enabled is False
    assert isinstance(cfg.limits, Limits)
    assert cfg.gs_session_ttl == 604800


def test_missing_secret_raises_naming_it():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({"SEANCE_DB": "seance.db"})
    assert "SEANCE_SECRET" in str(exc.value)


def test_missing_db_raises_naming_it():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({"SEANCE_SECRET": _key()})
    assert "SEANCE_DB" in str(exc.value)


def test_malformed_fernet_key_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_SECRET="not-a-valid-fernet-key"))
    assert "SEANCE_SECRET" in str(exc.value)


def test_secret_file_beats_inline(tmp_path):
    """SEANCE_SECRET_FILE wins over inline and tolerates a trailing newline."""
    file_key = _key()
    inline_key = _key()
    path = tmp_path / "secret.key"
    path.write_text(file_key + "\n")
    cfg = Config.from_env(
        _minimal_env(SEANCE_SECRET=inline_key, SEANCE_SECRET_FILE=str(path))
    )
    assert cfg.secret == file_key.encode()


def test_secret_file_unreadable_raises_naming_file(tmp_path):
    missing = tmp_path / "does-not-exist.key"
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_SECRET_FILE=str(missing)))
    assert "SEANCE_SECRET_FILE" in str(exc.value)


def test_gs_serializer_key_optional_and_validated():
    gs_key = _key()
    cfg = Config.from_env(_minimal_env(SEANCE_GS_SERIALIZER_KEY=gs_key))
    assert cfg.gs_serializer_key == gs_key.encode()


def test_gs_serializer_key_malformed_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_GS_SERIALIZER_KEY="bogus"))
    assert "SEANCE_GS_SERIALIZER_KEY" in str(exc.value)


def test_limit_override_max_clients():
    cfg = Config.from_env(_minimal_env(SEANCE_LIMIT_MAX_CLIENTS="32"))
    assert cfg.limits.max_clients == 32


def test_float_limit_override():
    cfg = Config.from_env(_minimal_env(SEANCE_LIMIT_FAST_RATE="45.5"))
    assert cfg.limits.fast_rate == 45.5
    assert isinstance(cfg.limits.fast_rate, float)


def test_frozen_session_ttl_limit_override():
    cfg = Config.from_env(_minimal_env(SEANCE_LIMIT_FROZEN_SESSION_TTL="100.5"))
    assert cfg.limits.frozen_session_ttl == 100.5


def test_bad_limit_value_raises_naming_var():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_LIMIT_MAX_CLIENTS="not-a-number"))
    assert "SEANCE_LIMIT_MAX_CLIENTS" in str(exc.value)


def test_dedicated_aliases_map_to_limits():
    env = _minimal_env(
        SEANCE_ANON_TTL="3600",
        SEANCE_TICKET_TTL="60",
        SEANCE_GS_SESSION_TTL="1000",
        SEANCE_FREEZE_GRACE="12.5",
        SEANCE_PING_INTERVAL="15",
        SEANCE_PING_TIMEOUT="45",
        SEANCE_CHECKPOINT_OPS="250",
        SEANCE_CHECKPOINT_SECS="7.5",
    )
    cfg = Config.from_env(env)
    assert cfg.limits.anon_ttl == 3600
    assert cfg.limits.ticket_ttl == 60
    assert cfg.limits.gs_session_ttl == 1000
    assert cfg.limits.freeze_grace == 12.5
    assert cfg.limits.ping_interval == 15.0
    assert cfg.limits.ping_timeout == 45.0
    assert cfg.limits.checkpoint_ops == 250
    assert cfg.limits.checkpoint_secs == 7.5


def test_limit_star_beats_alias():
    env = _minimal_env(SEANCE_ANON_TTL="1000", SEANCE_LIMIT_ANON_TTL="2000")
    assert Config.from_env(env).limits.anon_ttl == 2000


def test_gs_session_ttl_property_mirrors_limits():
    cfg = Config.from_env(_minimal_env(SEANCE_LIMIT_GS_SESSION_TTL="999"))
    assert cfg.gs_session_ttl == 999
    assert cfg.limits.gs_session_ttl == 999


def test_bad_cidr_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_TRUSTED_PROXIES="10.0.0.0/8,not-a-cidr"))
    assert "SEANCE_TRUSTED_PROXIES" in str(exc.value)


def test_cidrs_parse_to_networks():
    env = _minimal_env(
        SEANCE_GS_TRUSTED_NETS="10.0.0.0/8, 192.168.1.0/24",
        SEANCE_TRUSTED_PROXIES="::1/128",
    )
    cfg = Config.from_env(env)
    assert cfg.gs_trusted_nets == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("192.168.1.0/24"),
    )
    assert cfg.trusted_proxies == (ipaddress.ip_network("::1/128"),)


def test_origin_with_path_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_ALLOWED_ORIGINS="https://x.y/app"))
    assert "SEANCE_ALLOWED_ORIGINS" in str(exc.value)


def test_wildcard_origin_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_ALLOWED_ORIGINS="*"))
    assert "SEANCE_ALLOWED_ORIGINS" in str(exc.value)


def test_origins_normalized():
    """Scheme+host lowercased, single trailing slash stripped, port preserved."""
    env = _minimal_env(
        SEANCE_ALLOWED_ORIGINS="HTTPS://App.Noisedeck.APP/, http://localhost:3000"
    )
    cfg = Config.from_env(env)
    assert cfg.allowed_origins == frozenset(
        {"https://app.noisedeck.app", "http://localhost:3000"}
    )


def test_service_secrets_parse():
    cfg = Config.from_env(_minimal_env(SEANCE_SERVICE_SECRETS="bot:abc,ehsre:def"))
    assert cfg.service_secrets == {"bot": "abc", "ehsre": "def"}


def test_empty_string_env_treated_as_unset():
    env = _minimal_env(
        SEANCE_BIND="",
        SEANCE_GS_SERIALIZER_KEY="",
        SEANCE_DIRECTORY_DSN="",
        SEANCE_ALLOWED_ORIGINS="",
        SEANCE_TRUSTED_PROXIES="",
        SEANCE_SERVICE_SECRETS="",
        SEANCE_LIMIT_MAX_CLIENTS="",
    )
    cfg = Config.from_env(env)
    assert cfg.bind_host == "0.0.0.0"  # noqa: S104 — documented bind-all default
    assert cfg.bind_port == 8000
    assert cfg.gs_serializer_key is None
    assert cfg.directory_dsn is None
    assert cfg.allowed_origins == frozenset()
    assert cfg.trusted_proxies == ()
    assert cfg.service_secrets == {}
    assert cfg.limits.max_clients == 16


def test_bind_parsing():
    cfg = Config.from_env(_minimal_env(SEANCE_BIND="127.0.0.1:9000"))
    assert cfg.bind_host == "127.0.0.1"
    assert cfg.bind_port == 9000


def test_bind_bad_port_raises():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(_minimal_env(SEANCE_BIND="127.0.0.1:notaport"))
    assert "SEANCE_BIND" in str(exc.value)


def test_boolean_flags_parse():
    cfg = Config.from_env(
        _minimal_env(SEANCE_ANON_CAN_CREATE="false", SEANCE_STATS_ENABLED="true")
    )
    assert cfg.anon_can_create is False
    assert cfg.stats_enabled is True


def test_malformed_service_secret_error_redacts_raw_entry():
    raw_secret = "do-not-print-this-secret"
    with pytest.raises(ConfigError) as exc:
        Config.from_env(
            _minimal_env(SEANCE_SERVICE_SECRETS=f"worker:valid,{raw_secret}")
        )

    message = str(exc.value)
    assert message == "SEANCE_SERVICE_SECRETS: invalid entry 2; expected name:secret"
    assert raw_secret not in message


def test_directory_dsn_passthrough():
    cfg = Config.from_env(_minimal_env(SEANCE_DIRECTORY_DSN="postgres://h/db"))
    assert cfg.directory_dsn == "postgres://h/db"


def test_config_is_frozen():
    cfg = Config.from_env(_minimal_env())
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.bind_port = 1  # type: ignore[misc]
