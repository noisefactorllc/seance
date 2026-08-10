# Seance operations

Everything needed to configure, run, observe, and self-host the seance server.
Config truth is `app/config.py`; every variable and default below is generated
from it.

---

## 1. Configuration reference

Config is env-driven and fail-fast: a missing required variable or a malformed
value raises `ConfigError` naming the variable, and the process exits non-zero
(`main.py` `run`). An **empty string is treated as unset**. For any secret, a
`<NAME>_FILE` variant (pointing at a file) wins over the inline `<NAME>`.
Malformed `SEANCE_SERVICE_SECRETS` errors identify only the entry number; the
raw entry is never echoed because it may contain a credential.

### 1.1 Core variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SEANCE_BIND` | no | `0.0.0.0:8000` | `host:port` to bind (IPv6 literals may be bracketed). |
| `SEANCE_SECRET` / `SEANCE_SECRET_FILE` | **yes** | — | Fernet key signing anon tokens and tickets. Must be a valid Fernet key or startup fails. |
| `SEANCE_DB` | **yes** | — | SQLite database path (WAL). |
| `SEANCE_GS_SERIALIZER_KEY` / `_FILE` | no | unset | gs `SESSION`-cookie Fernet key. **Setting it enables member cookie auth**; unset disables it. |
| `SEANCE_DIRECTORY_DSN` | no | unset (no directory) | Member directory: `postgres://…` / `postgresql://…`, `sqlite:///<path>`, `static:` (empty in-memory), or unset. Any other value fails fast. |
| `SEANCE_GS_TRUSTED_NETS` | no | empty (endpoint disabled) | CSV of CIDRs allowed to mint tickets at `/v1/ticket` (checked against the **raw socket peer**). Empty = `/v1/ticket` returns 403. |
| `SEANCE_TRUSTED_PROXIES` | no | empty (headers ignored) | CSV of CIDRs whose forwarding headers (`X-Forwarded-For` → `X-Real-IP` → `Forwarded`) are honored for client-IP resolution. Empty = always use the socket peer. Keep tight (see threat-model §2.3). |
| `SEANCE_ALLOWED_ORIGINS` | for browsers | empty | CSV exact-origin allowlist for `/v1` CORS **and** the WS handshake. No wildcards; path/query/userinfo rejected. A browser client cannot connect without a matching origin. |
| `SEANCE_ANON_CAN_CREATE` | no | `true` | Whether anonymous identities may create sessions (`POST /v1/sessions`). |
| `SEANCE_SERVICE_SECRETS` | no | empty | CSV `name:secret` pairs for future service connections (phase-2 hook; parsed and validated now). |
| `SEANCE_STATS_ENABLED` | no | `false` | Gate for `GET /v1/stats` (§6). Off in production unless needed. |

Booleans accept `1/true/yes/on` and `0/false/no/off` (case-insensitive).

### 1.2 Limits and timeouts

Every field of the `Limits` dataclass is overridable by
`SEANCE_LIMIT_<FIELD>` (the field name upper-cased). A subset also has a
dedicated alias; when both are set, `SEANCE_LIMIT_*` **wins**. Aliases:
`SEANCE_ANON_TTL`, `SEANCE_TICKET_TTL`, `SEANCE_GS_SESSION_TTL`,
`SEANCE_FREEZE_GRACE`, `SEANCE_PING_INTERVAL`, `SEANCE_PING_TIMEOUT`,
`SEANCE_CHECKPOINT_OPS`, `SEANCE_CHECKPOINT_SECS`.

| Field (`SEANCE_LIMIT_<FIELD>`) | Default | Meaning |
|---|---|---|
| `MAX_FRAME` | 65536 | Max bytes for a general (non-snapshot) frame. |
| `MAX_SNAPSHOT_FRAME` | 1048576 | Max bytes for snapshot-lane frames; also the WebSocket `max_msg_size`. |
| `FAST_RATE` | 60.0 | Fast-lane refill tokens/s (`state-update`, `data-update`, `poly-cursor`). |
| `FAST_BURST` | 120 | Fast-lane burst ceiling. |
| `PROPOSAL_RATE` | 10.0 | Proposal-lane tokens/s (`poly-token-upsert`/`delete`). |
| `PROPOSAL_BURST` | 20 | Proposal-lane burst. |
| `CHAT_RATE` | 1.0 | Chat-lane tokens/s (`chat-message`). |
| `CHAT_BURST` | 5 | Chat-lane burst. |
| `CONTROL_RATE` | 5.0 | Control-lane tokens/s (`ping`, `session-state`, `mod-*`, …). |
| `CONTROL_BURST` | 10 | Control-lane burst. |
| `SNAPSHOT_RATE` | 0.2 | Snapshot-lane tokens/s (`state-set`, `poly-snapshot`). |
| `SNAPSHOT_BURST` | 2 | Snapshot-lane burst. |
| `ABUSE_WINDOW` | 10.0 | Seconds a lane may stay continuously exhausted before close `4429`. |
| `ANON_MINTS_PER_IP_HOUR` | 10 | `/v1/anon` mints per IP per hour. |
| `CREATES_PER_IP_HOUR` | 10 | Anonymous session creates per resolved client IP per hour, independent of cookies/tokens. |
| `JOINS_PER_IP_MIN` | 30 | WS joins and session probes per IP per minute. |
| `CREATES_PER_IDENTITY_HOUR` | 30 | Session creates per identity per hour. |
| `MAX_CLIENTS` | 16 | Max distinct users per session; extra tabs of a present user do not count toward it (but see `MAX_CONNS_PER_USER`). |
| `MAX_CONNS_PER_USER` | 8 | Max concurrent connections (tabs) for one user in a session; an additional tab past this is refused `4429`. |
| `MAX_STATE_KEYS` | 4096 | Max keys in the LWW state map. |
| `MAX_NODES` | 2000 | Max polydoc nodes. |
| `MAX_NODE_TEXT` | 65536 | Max chars for a node's `text`. |
| `MAX_PROGRAM_TEXT` | 262144 | Max chars for `programText`. |
| `MAX_DOCS_PER_SESSION` | 8 | Max text documents per session. |
| `MAX_DOC_ID_LEN` | 128 | Max chars for a document id. |
| `MAX_DOC_TITLE_LEN` | 128 | Max chars for a document title. |
| `MAX_DOC_TEXT` | 262144 | Max chars for a full document body. |
| `MAX_DOC_EDIT_TEXT` | 65536 | Max chars inserted by one document edit. |
| `MAX_DOC_OPLOG` | 500 | Max retained document edit operations per document for stale-edit recovery. |
| `MAX_DOC_OPLOG_BYTES` | 1048576 | Max retained document edit-op bytes per document for stale-edit recovery. |
| `MAX_SESSION_BYTES` | 8388608 | Aggregate compact-JSON bytes for persisted state, data, polydoc, documents (including op logs), and chat. Growing mutations are transactional and rejected when this cap would be crossed. |
| `MAX_VALUE_BYTES` | 8192 | Max compact-JSON bytes for a `value`. |
| `MAX_STATE_ID_LEN` | 128 | Max chars for a state/data id or key. |
| `CHAT_HISTORY` | 200 | Retained chat frames per session. |
| `MAX_CHAT_LEN` | 2000 | Max chars for a chat message. |
| `RECALL_WINDOW` | 120.0 | Seconds a chat author may recall a message. |
| `MAX_VIOLATIONS` | 3 | Protocol violations before close `4400`. |
| `PING_INTERVAL` | 20.0 | Seconds between server WS pings. |
| `PING_TIMEOUT` | 60.0 | Seconds without a pong before reap (`1011`). |
| `FREEZE_GRACE` | 60.0 | Seconds an empty session stays live before freezing to the store. |
| `CHECKPOINT_OPS` | 500 | seq delta (counts all emitted events, including server frames) that triggers a live checkpoint. |
| `CHECKPOINT_SECS` | 30.0 | Idle seconds that triggers a live checkpoint. |
| `FROZEN_SESSION_TTL` | 86400.0 | Seconds a frozen session is retained before the in-process sweep may delete it; `<= 0` disables the sweep. |
| `MAX_SESSIONS` | 1000 | Global persisted-session cap (`503` on create when reached), including frozen rows. |
| `MAX_CONNECTIONS` | 4096 | Global connection cap (`4429` on join when reached). |
| `SEND_QUEUE_FRAMES` | 256 | Per-connection send-queue frame budget (overflow → shed cursors, then `4408`). |
| `SEND_QUEUE_BYTES` | 1048576 | Per-connection send-queue byte budget. |
| `ANON_TTL` | 2592000 | Anon token lifetime, seconds (30 d); also the `SEANCE_ANON` cookie `Max-Age`. |
| `TICKET_TTL` | 120 | Ticket lifetime, seconds. |
| `GS_SESSION_TTL` | 604800 | gs cookie fallback max-age, seconds (7 d). |

---

## 2. Local run and tests

Generate a Fernet secret:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Run the server (repo-relative `SEANCE_DB`; entrypoint is the platform
convention `python bin/app.py`):

```sh
export SEANCE_SECRET="<fernet-key>"
export SEANCE_DB="./seance.db"
export SEANCE_ALLOWED_ORIGINS="http://localhost:5173"
python bin/app.py            # serves plain HTTP/WS on 0.0.0.0:8000
```

Verify readiness:

```sh
curl -sf http://127.0.0.1:8000/up
# {"status":"ok","service":"seance","version":"0.2.1"}
```

Tests and lint (dev deps in `requirements-dev.txt`):

```sh
.venv/bin/python -m pytest -q            # full suite; excludes the slow marker
.venv/bin/python -m pytest -m slow -q    # the load smoke (tests/test_load_smoke.py)
.venv/bin/python -m ruff check .         # lint
```

The `slow` marker is excluded by default (`pyproject.toml` `addopts = -m 'not
slow'`); pass `-m slow` to run only the load smoke.

---

## 3. Session lifecycle: freeze, thaw, checkpoint

Sessions are in-memory and server-authoritative; SQLite is the durability layer
(`app/hub.py`, `app/store.py`).

A file-backed database has exactly one Seance process owner. `Store.open()`
takes a non-blocking exclusive lock on `<SEANCE_DB>.lock` for its entire
lifetime and startup fails if another process owns it. Run one process per
database; scale by assigning separate databases, not by placing multiple
workers over the same SQLite file. In-memory test databases are exempt.

- **Create** — `POST /v1/sessions` mints a 6-char id and **persists the session
  immediately in frozen form**, so it survives even before the first join.
- **Thaw** — the first connect to a session not currently live loads its row from
  the store and rebuilds it; bans are loaded from the `bans` table. Concurrent
  first-connects share one thawed instance (per-id lock).
- **Checkpoint** — a background loop runs a scan every
  `max(0.5, min(freeze_grace, checkpoint_secs)/2)` seconds. A **live** session
  past `checkpoint_ops` of **seq delta** (counting all emitted events, including
  server frames) **or** `checkpoint_secs` idle is saved with `frozen_at = NULL`
  (durability without eviction).
- **Freeze** — an **empty** session past `freeze_grace` is saved with
  `frozen_at = <now>` and evicted from memory.
- **Shutdown** — `on_cleanup` freezes every live session and closes the store, so
  a restart thaws the same DB cleanly.

`frozen_at` is the frozen/live marker: an integer timestamp for a frozen row,
`NULL` for a live (checkpointed) row.

---

## 4. Retention

Frozen sessions are pruned by the hub's in-process `scan()` loop when
`FROZEN_SESSION_TTL` is positive. The default is `86400.0` seconds (24 hours).
Set `SEANCE_LIMIT_FROZEN_SESSION_TTL=0` or any negative value to disable the
sweep and retain frozen sessions indefinitely.

The sweep computes `cutoff = now - frozen_session_ttl`, lists rows with
`frozen_at IS NOT NULL AND frozen_at < cutoff`, then serializes each candidate
against thaw/freeze using the same per-session lock as `connect()`. Under that
lock it re-checks `hub.live`, skips live ids, and deletes only still-frozen rows
with `Store.delete_session(id)`.

The store exposes the two primitives used by that sweep:

- `Store.list_frozen_older_than(ts)` → ids of rows with `frozen_at IS NOT NULL
  AND frozen_at < ts`, sorted.
- `Store.delete_session(id)` → delete one session row and its session-scoped
  bans. Audit rows are intentionally retained.

**Caveat — any retention sweep MUST skip live session ids.** A thawed (live)
session keeps its **stale** `frozen_at` value in the DB row until the **first
checkpoint** rewrites it to `NULL` (thaw reads the row but does not immediately
clear `frozen_at`; only `_checkpoint_live` does, within ~`checkpoint_secs`). So
`list_frozen_older_than(cutoff)` can return the id of a session that is currently
**live**. Deleting such a row would pull persistence out from under a running
session.

The shipped in-process sweep performs that lock-protected live-id guard. An
external cron that just calls `list_frozen_older_than` + `delete_session` remains
unsafe unless it coordinates with the running seance process.

---

## 5. Audit trail

Every moderation/security action is written durably and mirrored to stdout
(`app/moderation.py` → `Session.record_audit` → hub bridge → `Store.audit` →
`app/audit.py` `log_audit`).

**SQLite table** `audit(id INTEGER PK AUTOINCREMENT, ts INTEGER, session_id TEXT,
actor TEXT, action TEXT, target TEXT, detail TEXT)` where `detail` is JSON. Query
directly:

```sh
sqlite3 "$SEANCE_DB" \
  "SELECT ts, session_id, actor, action, target, detail
     FROM audit ORDER BY id DESC LIMIT 50;"

# all bans in a session:
sqlite3 "$SEANCE_DB" \
  "SELECT ts, actor, target FROM audit
    WHERE session_id='ABC123' AND action='mod-ban' ORDER BY id;"
```

**Stdout line** — one compact, **key-sorted** JSON object per event on the
`seance.audit` logger at INFO:

```
{"action":"mod-ban","actor":"ada","detail":{},"session":"ABC123","target":"<user_id>","ts":1751500000}
```

With the default log format it is prefixed by timestamp/logger/level, e.g.:

```
2026-07-02 12:00:00,000 seance.audit INFO {"action":"mod-ban",...}
```

`main.py` `_configure_logging` **pins `seance.audit` to INFO unconditionally**,
even if the root level is raised: the audit trail is a security/moderation record
and must always reach stdout/journald regardless of global log verbosity. Audit
lines are identity-only — `actor`/`target` carry `user_id`/username material and
nothing else; no tokens, cookies, IPs, or message bodies.

---

## 6. Logging, health, and stats

**Logging.** Structured single-line logs to **stdout** (journald convention),
INFO by default, format `%(asctime)s %(name)s %(levelname)s %(message)s`. Loggers:
`seance.main`, `seance.hub`, `seance.http`, `seance.transport`, `seance.audit`.
Tokens, cookies, IPs, and frame payloads are never logged (`transport.py` never
logs payloads; client IPs live only in RAM for rate limiting). Unexpected HTTP
errors log only the method and matched route template, never the raw request
path or a session id. Under
systemd/journald, capture stdout and grep audit lines, e.g.
`journalctl -u <unit> -o cat | grep '"action"'`.

**Health — `GET /up`.** Returns `200 {"status":"ok","service":"seance",
"version":"<ver>"}` **only after a real readiness probe**: it issues a sentinel
`store.load_session` and, if that raises (DB unreachable/closed), returns `503
{"status":"error",...}` (`httpapi.py` `up`; proven by
`test_httpapi.py::test_up_unhealthy_when_store_closed`). It is a true readiness
check, not a static 200.

**Deployment meta — `GET /deployment-meta.json`.** Serves
`public/deployment-meta.json` when present (baked into the image by CI — the
Dockerfile copies `public/`), otherwise a dev placeholder
`{"git_hash":"dev","date":"<startup-iso>"}`.

**Stats — `GET /v1/stats`.** Gated by `SEANCE_STATS_ENABLED`. When **off**
(default) it returns `404 {"error":"not found"}`; when **on** it returns
`{"live_sessions": <int>, "connections": <int>}` where `connections` is the hub's
total client connection count across all live sessions
(`test_httpapi.py::test_stats_disabled_not_found`,
`::test_stats_enabled_reports_counts`). Keep it off in production unless a
monitor needs it — it discloses live counts.

---

## 7. Diagnostics quick reference

| Symptom | Where to look |
|---|---|
| Startup exits non-zero | `ConfigError` on stderr names the bad/missing variable (`main.py`). |
| WS connect returns 403 pre-upgrade | `Origin` not in `SEANCE_ALLOWED_ORIGINS`. |
| WS connect returns 429 pre-upgrade | `joins_per_ip_min` exceeded for the client IP. |
| Member cookie ignored | `SEANCE_GS_SERIALIZER_KEY` unset, or IP binding mismatch (check `trusted_proxies`), or member deleted (→ falls through to anon; threat-model §2.4). |
| `/v1/ticket` returns 403 | Socket peer not in `SEANCE_GS_TRUSTED_NETS` (or that var is empty → endpoint disabled). |
| Clients closing `4408` | Slow consumer; raise `SEND_QUEUE_FRAMES`/`SEND_QUEUE_BYTES` or investigate a stalled client. |
| Clients closing `4429` | Roster/connection cap, aggregate stored-session cap during thaw, or sustained lane abuse past `ABUSE_WINDOW`. |
| Session vanished after restart | Expected only if never persisted; otherwise it thaws on next connect. Check the `sessions` row and `frozen_at`. |

---

## 8. Deployment notes

Seance is a plain HTTP/WebSocket service. In production, run it behind a TLS
terminating reverse proxy and pass WebSocket upgrades directly to the app.

Build the image:

```sh
docker build -t seance:local .
```

The included `Dockerfile` pins the official `python:3.14-slim` image by digest,
installs the transitive hash lock with `--require-hashes`, and runs as uid/gid
10001. `/app` is root-owned and read-only to that runtime user; `/data` is the
only application-writable directory. It starts `python bin/app.py` on port 8000.

Example Caddy route:

```caddy
seance.example.com {
    reverse_proxy seance:8000
}
```

Deployment checklist:

- Store secrets in environment variables or `*_FILE` secret paths; never bake
  them into the image.
- Persist `SEANCE_DB` on durable storage. The image creates `/data` as `0700`
  for uid 10001; mounted volumes should also be owned by the runtime user and
  owner-only, especially when SQLite WAL sidecars are enabled.
- Run exactly one Seance process against each SQLite database; the lifetime
  `<SEANCE_DB>.lock` makes accidental multi-process startup fail closed.
- Set `SEANCE_ALLOWED_ORIGINS` to exact browser origins. Wildcards are not
  supported.
- If using forwarding headers, set `SEANCE_TRUSTED_PROXIES` only to reverse
  proxies that correctly control `X-Forwarded-For`, `X-Real-IP`, and `Forwarded`.
- Set `SEANCE_GS_TRUSTED_NETS` only to direct Groundsquirrel/ticket-minting
  peers on private networks. Do not include public reverse proxies or broad
  client-address ranges.
- Keep `/up` wired to readiness monitoring. It performs a real store probe.
- Optionally stamp `public/deployment-meta.json` during image build with
  `bin/stamp_deployment_meta.py`; otherwise Seance serves a `dev` placeholder.

Noise Factor's hosted Seance deployments are operated by separate deployment
automation, not by this public repository's CI.
