# Seance

Seance is a session-scoped real-time collaboration backend for browser-based
creative tools. Clients join shared ad-hoc sessions over a WebSocket transport
and see each other's live document edits, cursor positions, state changes, and
moderation events.

The server is authoritative for all session state. A last-write-wins lane
carries high-frequency parameter changes, and a server-serialized document lane
carries text/tree edits with acknowledgements, stale-edit recovery, and
snapshots. Sessions are owned, so the creator or current owner can kick, ban,
lock, toggle guest access, and mark users read-only. Moderation state persists
across freeze/thaw.

Seance also includes a zero-dependency browser ESM SDK for Handfish-backed DSL
editors. The SDK is optional; the wire protocol is documented for direct
clients.

## Documentation

- Server configuration and operations: [docs/operations.md](docs/operations.md)
- Wire protocol reference: [docs/protocol.md](docs/protocol.md)
- Threat model: [docs/threat-model.md](docs/threat-model.md)
- Browser SDK guide: [sdk/README.md](sdk/README.md)
- Security policy: [SECURITY.md](SECURITY.md)

## Local run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

SEANCE_SECRET=$(.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
SEANCE_DB=./seance.db \
SEANCE_ALLOWED_ORIGINS=http://localhost:3000 \
.venv/bin/python bin/app.py
```

Then `curl -s http://127.0.0.1:8000/up` should return
`{"status": "ok", "service": "seance", "version": "0.2.1"}`.

Configuration is environment-only; every variable is documented in
`docs/operations.md`.

## SDK

The SDK is not published to npm. Use the checked-out source directly:

```js
import { createOnlineDslLayer } from './sdk/index.js'
```

Build the standalone browser bundle:

```bash
npm run build:sdk
```

The generated bundle is written to `dist/index.js`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q            # full suite (slow tests excluded)
.venv/bin/python -m pytest -m slow -q    # load/perf smoke
.venv/bin/python -m ruff check .         # lint
npm run test:sdk                        # SDK bundle + node tests
```

Deployment pipelines can stamp `public/deployment-meta.json` at image build
time. Local runs synthesize a `{"git_hash": "dev"}` placeholder.

## Contributing

Issues and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before opening changes.

## License

Seance is released under the [MIT License](LICENSE). Use of the Seance and
Noise Factor names in derivative products is subject to the
[Trademark Policy](TRADEMARK.md).

Copyright (c) 2026 Noise Factor LLC
