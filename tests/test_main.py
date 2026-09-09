"""Process entrypoint behavior."""

import sys

import pytest

from app import __version__
from app.main import run


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
