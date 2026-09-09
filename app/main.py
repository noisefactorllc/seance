"""Application wiring and the process entrypoint for the seance server.

:func:`create_app` is the single composition root: it parses the environment into
a :class:`app.config.Config`, opens the :class:`app.store.Store`, resolves the
member :class:`app.directory.MemberDirectory`, and constructs the
:class:`app.identity.IdentityService`, the :class:`app.hub.Hub`, and the transport
WebSocket handler, then assembles them with :func:`app.httpapi.build_app`. The
hub's background freeze/checkpoint loop is started on app startup; on shutdown
every WebSocket is closed with ``1001`` (going away) so aiohttp's handler wait
ends promptly, and on cleanup every live session is frozen and the store closed.

:func:`run` is the ``bin/app.py`` entrypoint. It fails fast on a configuration
error (printing it and exiting non-zero) and otherwise hands the app coroutine to
:func:`aiohttp.web.run_app`. Startup logging is structured and never emits any
secret material.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping

from aiohttp import web

from app import __version__
from app.config import Config, ConfigError
from app.directory import make_directory
from app.httpapi import build_app
from app.hub import Hub
from app.identity import IdentityService
from app.store import Store
from app.transport import make_websocket_handler

_LOG = logging.getLogger("seance.main")

# RFC 6455 "going away": sent to every connected client when the server stops.
_CLOSE_GOING_AWAY = 1001
# How long aiohttp waits for in-flight handlers after ``on_shutdown``. The
# handlers finish within a second once their sockets are closed above, so this
# only bounds a stalled peer; it must stay well inside the container's stop
# grace period, which also has to cover freezing every live session.
_SHUTDOWN_TIMEOUT = 10.0


async def create_app(env: Mapping[str, str]) -> web.Application:
    """Compose the fully-wired seance application from an environment mapping."""
    config = Config.from_env(env)
    _configure_logging()

    store = await Store.open(config.db_path)
    directory = await make_directory(config.directory_dsn)
    identity = IdentityService(config, directory)
    hub = Hub(config, store)
    ws_handler = make_websocket_handler(config, hub, identity)
    app = build_app(config, hub, identity, ws_handler=ws_handler)

    async def _on_startup(_app: web.Application) -> None:
        await hub.start()
        _LOG.info(
            "seance %s listening on %s:%s", __version__, config.bind_host, config.bind_port
        )

    async def _on_shutdown(_app: web.Application) -> None:
        # Runs before aiohttp waits for in-flight handlers: closing every
        # WebSocket with 1001 lets those handlers return at once, so a SIGTERM
        # completes in seconds (freeze included) rather than after the wait.
        hub.close_connections(_CLOSE_GOING_AWAY, "server shutting down")

    async def _on_cleanup(_app: web.Application) -> None:
        await hub.stop()
        await store.close()
        if directory is not None:
            await directory.close()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    app.on_cleanup.append(_on_cleanup)
    return app


def _configure_logging() -> None:
    """Install a stdout log handler once; keep the audit logger at INFO always."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stdout,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
    logging.getLogger("seance.audit").setLevel(logging.INFO)


def run() -> None:
    """Process entrypoint: validate config, then serve (fail fast on config errors)."""
    parser = argparse.ArgumentParser(
        prog="seance", description="Seance real-time collaboration server"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()
    try:
        config = Config.from_env(os.environ)
    except ConfigError as exc:
        print(f"seance: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    web.run_app(
        create_app(os.environ),
        host=config.bind_host,
        port=config.bind_port,
        shutdown_timeout=_SHUTDOWN_TIMEOUT,
    )
