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
