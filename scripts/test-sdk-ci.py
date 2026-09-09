"""Run every SDK test against an isolated local Seance server."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("SDK tests require Node.js and npm on PATH")
    # Never inherit a developer's database, credentials, or production URL.
    env = {key: value for key, value in os.environ.items() if not key.startswith("SEANCE_")}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with tempfile.TemporaryDirectory(prefix="seance-sdk-tests-") as directory:
        env.update({
            "SEANCE_BIND": f"127.0.0.1:{port}",
            "SEANCE_DB": str(Path(directory) / "sessions.db"),
            "SEANCE_SECRET": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
            "SEANCE_ALLOWED_ORIGINS": "http://localhost:5173",
            # Tests create distinct identities and rooms in one short run.
            "SEANCE_LIMIT_ANON_MINTS_PER_IP_HOUR": "1000",
            "SEANCE_LIMIT_CREATES_PER_IP_HOUR": "1000",
            "SEANCE_LIMIT_CREATES_PER_IDENTITY_HOUR": "1000",
            "SEANCE_LIMIT_JOINS_PER_IP_MIN": "1000",
            "SEANCE_LIVE_URL": base_url,
            "SEANCE_PERF": "1",
        })
        with (Path(directory) / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [sys.executable, "bin/app.py"], cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT,
            )
            result = 1
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError("SDK test server exited during startup")
                    try:
                        with opener.open(f"{base_url}/up", timeout=1) as response:
                            if json.load(response).get("service") == "seance":
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("SDK test server did not become ready")

                # Executables are resolved locally; arguments come from this checkout.
                build = subprocess.run(  # noqa: S603
                    [npm, "run", "build:sdk"], cwd=ROOT, env=env, timeout=60,
                )
                if build.returncode:
                    return build.returncode
                tests = sorted(
                    str(path.relative_to(ROOT)) for path in (ROOT / "tests/sdk").glob("*.test.mjs")
                )
                result = subprocess.run(  # noqa: S603
                    [node, "--expose-gc", "--test", *tests], cwd=ROOT, env=env, timeout=180,
                ).returncode
                return result
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                if result:
                    log.seek(0)
                    print(log.read(), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
