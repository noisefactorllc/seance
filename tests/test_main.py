"""Process entrypoint behavior."""

import logging
import subprocess
import sys

import pytest

from app import __version__
from app.main import _configure_logging, _HealthProbeFilter, run


def test_cli_help_exits_without_requiring_runtime_configuration(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["seance", "--help"])

    with pytest.raises(SystemExit) as exc:
        run()

    assert exc.value.code == 0
    assert "usage: seance" in capsys.readouterr().out


def test_runtime_version_is_release_version():
    assert __version__ == "0.2.1"


def test_run_bounds_the_aiohttp_shutdown_wait(monkeypatch):
    """SIGTERM must not sit through aiohttp's default 60 s handler wait."""
    from cryptography.fernet import Fernet

    import app.main as main

    captured = {}

    def fake_run_app(app_coro, **kwargs):
        captured.update(kwargs)
        app_coro.close()

    monkeypatch.setattr(main.web, "run_app", fake_run_app)
    monkeypatch.setenv("SEANCE_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("SEANCE_DB", ":memory:")
    monkeypatch.setattr(sys, "argv", ["seance"])

    run()

    assert captured["shutdown_timeout"] == main._SHUTDOWN_TIMEOUT
    assert main._SHUTDOWN_TIMEOUT <= 10.0


def _access_record(request_line: str, status: int) -> logging.LogRecord:
    record = logging.LogRecord(
        "aiohttp.access", logging.INFO, __file__, 0, "%s", ("line",), None
    )
    record.first_request_line = request_line
    record.response_status = status
    return record


def test_successful_health_probes_are_filtered_out_of_the_access_log():
    """Probes every 20 s would otherwise bury real errors in the journal."""
    probe_filter = _HealthProbeFilter()

    assert probe_filter.filter(_access_record("GET /up HTTP/1.1", 200)) is False
    # A failing probe is news, and every other route stays logged.
    assert probe_filter.filter(_access_record("GET /up HTTP/1.1", 503)) is True
    assert probe_filter.filter(_access_record("POST /v1/sessions HTTP/1.1", 201)) is True
    assert probe_filter.filter(_access_record("GET /upload HTTP/1.1", 200)) is True
    # Records without aiohttp's access extras must survive.
    assert probe_filter.filter(logging.LogRecord("x", logging.INFO, __file__, 0, "m", (), None))


def test_configure_logging_installs_one_health_probe_filter():
    access_log = logging.getLogger("aiohttp.access")
    for existing in list(access_log.filters):
        if isinstance(existing, _HealthProbeFilter):
            access_log.removeFilter(existing)
    try:
        _configure_logging()
        _configure_logging()
        installed = [f for f in access_log.filters if isinstance(f, _HealthProbeFilter)]
        assert len(installed) == 1
    finally:
        for existing in list(access_log.filters):
            if isinstance(existing, _HealthProbeFilter):
                access_log.removeFilter(existing)


def test_run_reports_a_locked_database_as_a_startup_error(monkeypatch, tmp_path, capsys):
    """A second process on one database is an operator mistake, not a crash."""
    from cryptography.fernet import Fernet

    from app.store import _acquire_store_lock, _release_store_lock

    db_path = str(tmp_path / "locked.db")
    held = _acquire_store_lock(db_path)  # stand in for the first process
    monkeypatch.setenv("SEANCE_SECRET", Fernet.generate_key().decode())
    monkeypatch.setenv("SEANCE_DB", db_path)
    monkeypatch.setattr(sys, "argv", ["seance"])
    try:
        with pytest.raises(SystemExit) as exc:
            run()
    finally:
        _release_store_lock(held)

    assert exc.value.code == 1
    assert capsys.readouterr().err.strip() == f"seance: database already in use: {db_path}"


def test_failed_application_startup_closes_store_and_exits(tmp_path):
    # aiosqlite owns a non-daemon thread. An exception after opening the store
    # must release it, or even an already-failed startup never exits.
    script = """
import asyncio
import sys
from cryptography.fernet import Fernet
from app.main import create_app
asyncio.run(create_app({
    "SEANCE_SECRET": Fernet.generate_key().decode(),
    "SEANCE_DB": sys.argv[1],
    "SEANCE_DIRECTORY_DSN": "unsupported:test-directory",
}))
"""
    try:
        result = subprocess.run(  # noqa: S603 - fixed script and pytest-owned temporary path.
            [sys.executable, "-c", script, str(tmp_path / "startup.db")],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("failed application startup leaked its SQLite worker and did not exit")
    assert result.returncode != 0
    assert "ConfigError" in result.stderr
