# Seance wire protocol (v1)

The reference for phase-2 client authors. Every type, field, cap, lane, and code
below is taken from the server source and is the authority the code enforces:

- message types, validators, envelope, and codes — `app/protocol.py`
- handshake, heartbeat, size split, lane limiting, backpressure — `app/transport.py`
- dispatch, fan-out, owner rules, chat — `app/session.py`
- polydoc apply/reject/dedup — `app/engine.py`
- tunable limits and their defaults — `app/config.py` (`Limits`)

Transport is WebSocket, text frames only, exactly one JSON object per frame. A
binary frame is a protocol violation. Protocol version is `1`.

**Document offsets are UTF-16 code units.** Every `start` / `end` in `doc-edit`,
`doc-ack`, `doc-cursor`, and the retained op log counts UTF-16 code units, the
same unit as JavaScript string indexing (`"a".length`, `selectionStart`), so a
browser can use its own offsets unchanged. An astral character such as an emoji
therefore counts as **two**. The server stores document text in the same unit,
and its JSON encoding escapes each surrogate half, which `JSON.parse`
reassembles: a client sees ordinary text. A language whose strings index by code
point (Python, Go, Rust) must convert at the boundary.

**Numbers are JSON numbers a browser can hold.** Every integer field is capped
at 2^53-1, and the non-standard `NaN`, `Infinity` and `-Infinity` literals are
rejected as `bad_frame`, as is a frame nested deeper than 64 levels.

---

## 1. Connection

```
GET /v1/sessions/{id}/ws
```

`{id}` is the 6-character session id returned by `POST /v1/sessions`. The
upgrade request MUST carry an `Origin` header in the server's
`SEANCE_ALLOWED_ORIGINS` allowlist; a missing or disallowed origin is answered
`403` before the upgrade (`transport.py` `websocket_handler`). A per-IP join
limiter (`joins_per_ip_min`, default 30/min) answers `429` pre-upgrade when
exceeded.

Credentials are never sent in the URL or body. Identity is resolved from, in
precedence order (`app/identity.py` `resolve`):

1. a single-use `ticket` in the `hello` frame (gs-proxied member auth);
2. a groundsquirrel `SESSION` cookie (only when the server has a gs serializer
   key configured);
3. an `anon_token` (in the `hello` frame or the `SEANCE_ANON` cookie);
4. otherwise a fresh anonymous identity is minted and returned in the first
   `welcome` as `anon_token`.

For HTTP requests, `X-Seance-Anon` takes precedence over the `SEANCE_ANON`
cookie. Clients that retain a token should send that header when creating a
session and the same token in `hello`, so another tab's cookie cannot create
a room under a different anonymous owner. The cookie is `SameSite=Lax` and
does not supply identity on cross-site app requests.

### Creating and probing a session

```
POST /v1/sessions
GET  /v1/sessions/{id}
```

`POST /v1/sessions` mints a session and returns `201 {"session_id", "anon_token"?}`
(`anon_token` only when the server minted one for you). The body is optional
JSON: `{"snapshot": {...}, "dialect": "<string>"}`. `dialect` names the
session's immutable document contract (default `noisemaker-dsl`; see §2 for how
a client's `hello.dialects` must match it to join) and must match
`^[a-z0-9][a-z0-9._-]{0,63}$` (≤64 chars) — an invalid value is answered `400
{"error": "invalid dialect"}` before a session is minted.

`GET /v1/sessions/{id}` is a lightweight pre-connect probe sharing the join
rate limiter (`joins_per_ip_min`): `200 {"id", "open": bool, "dialect":
"<string>"}` for a known session, live or frozen (`open` is `false` when
locked), or `404 {"error": "unknown session"}`.

## 2. Handshake sequence

```
client                         server
  |  ── WS upgrade (Origin) ───▶ |   Origin allowlist + join limiter (else 403/429)
  |  ── hello ────────────────▶ |   first frame, within 10 s
  |                              |   identity resolve; hub.connect (ban/lock/guest/roster)
  |  ◀──────────── welcome ───── |   your identity, roster, owner, settings, rev
  |  ◀──── session-snapshot ──── |   full state/data/poly/docs/chat
  |  ══════ live traffic ═══════ |
```

- The **first** client frame MUST be `hello`. Any other first frame, a non-text
  first frame, a malformed frame, or no frame within 10 s → an `error` then
  close `4400` (`transport.py` `_read_hello`). A malformed `hello.dialects`
  (not a list, more than `MAX_HELLO_DIALECTS` entries, or an entry not
  matching the dialect pattern — see §3) is the same `bad_frame` / `4400`. A
  present `dialects` must be non-empty.
- On success the joiner receives `welcome` then `session-snapshot`. Other
  participants receive `user-joined` + `system-message` on this user's **first**
  connection (additional tabs of an already-present user are silent).
- A refused join sends an `error` and closes with the reason's code
  (`4403` banned / guests-off, `4423` locked, `4409` dialect mismatch, `4404`
  unknown session, `4429` roster full / the per-user connection cap
  (`max_conns_per_user`, an extra tab of an already-present user) / the global
  connection cap). Identity failure closes `4403` (`unauthorized`).
- The creator and a persisted explicit owner are exempt from the lock and
  guests-off join gates. They must still satisfy bans, dialect, and capacity
  checks, and can return to change the access settings.
- **Dialect compatibility**: a client declares the dialects it speaks in
  `hello.dialects`; the join is refused `4409` (`dialect_mismatch`) unless the
  session's `dialect` is among them. A client that sends no `dialects` is
  treated as declaring exactly `["noisemaker-dsl"]`, so existing clients see no
  behavior change against default-dialect sessions.

`resume.last_seq` in `hello` is **advisory in v1**: the server always answers a
full `session-snapshot` and performs no op-log replay. Send it for
forward-compatibility; do not rely on delta replay.

### `welcome` shape

```jsonc
{
  "type": "welcome",
  "protocol": 1,
  "session": "<id>",
  "dialect": "<string>",               // the session's document-contract tag (§1)
  "you": { "user_id", "username", "kind", "readonly": bool, "is_owner": bool },
  "anon_token": "<token>",           // present only when the server minted one for you
  "roster": [ { "user_id", "username", "kind", "connections": int } ],
  "owner": { "user_id", "username" } | null,
  "rev": int,                         // current polydoc revision
  "settings": { "locked": bool, "guests_allowed": bool, "guests_readonly": bool }
  // + envelope (§4)
}
```

`kind` is one of `member`, `anon`, `gs_ephemeral`, `service`. Persist
`anon_token` (it is minted once) for stable reconnect (§8).

### `session-snapshot` shape

```jsonc
{
  "type": "session-snapshot",
  "state": [ { "id", "value", "seq": int, "by": "<user_id>" } ],   // LWW map, id-sorted
  "data":  { "<id>": { "<role>": { "<key>": value } } },           // nested data map
  "poly":  { "rev": int, "programText": str, "frame": any,
             "nodes": [ { "id", "kind", "text", "version": int, "parentId": str|null } ] },
  "docs":  [ { "id", "title", "kind", "rev": int, "text": str, "default": bool } ],
  "chat":  [ <stored chat-message frames, newest-bounded to chat_history> ]
  // + envelope (§4)
}
```

The same body is re-sent on demand for any `session-state` request (§7).

## 3. Client → server messages

Caps are character length for strings and compact-JSON byte length for `value`.
Text offsets (`edit.start`/`edit.end`, cursor `range.start`/`range.end`) and
the text caps count **UTF-16 code units**, the unit browsers produce
(`String.length`, `selectionStart`); the server stores documents in the same
units (`textdoc.py` `to_utf16_units`). An astral character is two units.
Unlisted keys are dropped by the validator; the server never trusts envelope
fields from a client. "Lane" selects the rate bucket (§6); "Who" is the
authorization gate (`session.py` `handle`).

| type | required fields (validated) | optional | caps | lane | who may send |
|---|---|---|---|---|---|
| `hello` | `protocol` (== 1) | `ticket`, `anon_token`, `resume:{last_seq≥0}`, `dialects:[string]` | `dialects`≤16 entries, each matching `^[a-z0-9][a-z0-9._-]{0,63}$` | — | first frame only |
| `state-set` | `state:[{id, value}]` | — | `id`≤128, `value`≤8192 B | snapshot | owner |
| `state-update` | `id`, `value` | — | `id`≤128, `value`≤8192 B | fast | writers |
| `data-update` | `id`, `role`, `key`, `value` | — | `id`≤128, `role`≤64, `key`≤128, `value`≤8192 B | fast | writers |
| `clicked-button` | (opaque object) | any keys | whole frame ≤65536 B | control | writers |
| `chat-message` | `message` | — | `message`≤2000 | chat | writers |
| `chat-delete` | `message_id` | — | `message_id`≤64 | control | author or owner |
| `chat-recall` | `message_id` | — | `message_id`≤64 | control | author, ≤120 s old |
| `ping` | — | — | — | control | all |
| `session-state` | — | — | — | control | all |
| `poly-snapshot` | `programText`, `nodes:[node]` | `frame` | `programText`≤262144, node caps below | snapshot | owner |
| `poly-token-upsert` | `base_rev≥0`, `id`, `kind`, `text` | `parentId` (str\|null), `hash`, `author_seq≥0` | `id`≤200, `kind`≤32, `text`≤65536, `hash`≤128 | proposal | writers |
| `poly-token-delete` | `base_rev≥0`, `id` | `author_seq≥0` | `id`≤200 | proposal | writers |
| `poly-cursor` | `mode` (`"text"`\|`"node"`) | text→`range:{start≥0,end≥0}`; node→`node_id`, `param` | `node_id`≤200, `param`≤128, `end`≥`start` | fast | all |
| `poly-lock` | `node_id`, `locked` | — | `node_id`≤200 | control | writers |
| `doc-create` | `doc:{id,title,kind,text,default}` | — | `id`≤128, `title`≤128, `text`≤262144 | snapshot | owner |
| `doc-edit` | `docId`, `baseRev≥0`, `authorSeq≥0`, `edit:{start≥0,end≥0,text}` | — | `docId`≤128, `edit.text`≤65536, `end`≥`start` | proposal | writers |
| `doc-cursor` | `docId`, `range:{start≥0,end≥0}` | `direction` | `docId`≤128, `end`≥`start` | fast | all |
| `doc-reset` | `docId`, `text`, `baseRev≥0` | — | `docId`≤128, `text`≤262144 | snapshot | owner |
| `mod-kick` | `target_user` **or** `target_connection` | — | ≤200 | control | owner |
| `mod-ban` | `target_user` | — | ≤200 | control | owner |
| `mod-unban` | `target_user` | — | ≤200 | control | owner |
| `mod-lock` | `locked` | — | — | control | owner |
| `mod-guests` | `allowed` | — | — | control | owner |
| `mod-readonly` | `target_user`, `readonly` | — | ≤200 | control | owner |
| `mod-transfer` | `target_user` | — | ≤200 | control | owner |

`poly-snapshot` / `poly-token-*` node fields: `id`≤200, `kind`≤32,
`text`≤65536, `parentId` is a string or `null`, `version` is an optional int≥0.

**Who may send:**

- **owner** — only the *acting owner* (a user-level role, §5); anyone else gets
  `error {code:"forbidden"}`. Owner verbs are `mod-*`, `state-set`,
  `poly-snapshot`, `doc-create`, and `doc-reset`.
- **writers** — any joined member/anon/gs_ephemeral **unless** the connection is
  read-only: the user is in the per-user readonly set (`mod-readonly`) **or**
  `guests_readonly` is on and the sender is anon/gs_ephemeral. A read-only sender
  gets `error {code:"readonly"}`. Write types are `state-update`, `data-update`,
  `clicked-button`, `chat-message`, `poly-token-upsert`, `poly-token-delete`,
  `poly-lock`, and `doc-edit`. Read-only means **no new persisted content**:
  chat is included because it is kept in the session's chat history and charged
  to the content budget.
- **all** — any joined connection, including read-only ones (`poly-cursor`,
  `doc-cursor`, `ping`, `session-state`). Cursors are ephemeral presence, never
  persisted, so a read-only participant can still point at code. `chat-delete` /
  `chat-recall` are open to read-only senders too (they only remove the sender's
  own message) but carry a further in-handler check (author, or owner for
  delete; author within the recall window for recall) and answer `forbidden`
  otherwise.

`gs_ephemeral` identities can never be owner and cannot create sessions, but by
default may write (subject to the `guests_readonly` dial).

## 4. Envelope

The server stamps **every** outbound frame with these seven fields, overwriting
any client-supplied copy (`protocol.py` `stamp`):

| field | meaning |
|---|---|
| `seq` | per-session monotonic ordering counter (§5) |
| `session` | session id |
| `user_id` | originator's user id |
| `username` | originator's username |
| `connection_id` | originator's connection id |
| `message_id` | fresh uuid4, minted per emitted frame |
| `timestamp` | unix seconds (int) |

For a **relayed user action** the envelope carries the acting user's identity.
For a **server-originated** frame it is `user_id = username = connection_id =
"server"` — with one deliberate exception:

- `user-joined`, `user-parted`, and `owner-changed` stamp the envelope
  `user_id` / `username` with the **subject** of the event (the joiner, leaver,
  or new owner), and `connection_id = "server"`. Their payloads are otherwise
  minimal, so the subject lives in those envelope fields
  (`session.py` `_emit_owner_changed`, `join`, `leave`). Read the
  subject from the envelope, not from a body field.

`chat-deleted` and `chat-recalled` reference the affected message by
`target_message_id`, **not** `message_id`: the envelope stamps a fresh
`message_id` on every server frame, so the original id has to travel in a
distinct field (`session.py` `_do_chat_delete` / `_do_chat_recall`).

## 5. Sequence numbers

`seq` is a single per-session counter, incremented once per accepted or emitted
frame. A **broadcast is one logical event**: all recipients' copies share one
`seq` and one `message_id`. Recipient-specific frames (`welcome`,
`session-snapshot`, `pong`, in-session `error`, `poly-ack`, `poly-reject`,
`doc-ack`, `doc-reject`, `chat-recalled`) each consume their own `seq`
(`session.py` module docstring; `_broadcast` vs `_send`).

Therefore **a single client sees `seq` values with gaps** — seqs consumed by
frames delivered only to other connections are never sent to it. Treat `seq` as
a global session-ordering token for applying broadcasts in order, **not** as a
per-client contiguous counter. Do not alarm on gaps; do not use `seq` deltas to
detect loss.

## 6. Server → client messages

| type | shape (beyond the envelope) | delivery |
|---|---|---|
| `welcome` | see §2 | joiner |
| `session-snapshot` | see §2 | requester |
| `doc-snapshot` | `{docs:[{id,title,kind,rev,text,default}]}` | broadcast after accepted `doc-create` / `doc-reset` |
| `doc-edit` | `{docId, rev, authorSeq, edit:{start,end,text}}` | broadcast (excl. sender) |
| `doc-ack` | `{docId, rev, authorSeq, edit:{start,end,text}}` | proposer |
| `doc-reject` | `{docId, baseRev, authorSeq, reason, snapshot}` (`authorSeq:null` for reset) | proposer |
| `doc-cursor` | `{docId, user, connectionId, range:{start,end}, direction}` | broadcast (excl. sender) |
| `state-update` / `data-update` / `state-set` / `clicked-button` | the validated client body, relayed | broadcast (excl. sender) |
| `chat-message` | `{message}` | broadcast **incl. sender** (the echo) |
| `chat-deleted` | `{target_message_id, by}` | broadcast |
| `chat-recalled` | `{target_message_id, original_message}` | author only |
| `poly-snapshot` / `poly-token-upsert` / `poly-token-delete` / `poly-cursor` / `poly-lock` | relayed body, decorated with `rev` (and `version` on an applied upsert) | broadcast (excl. sender) |
| `poly-ack` | `{rev, applied:[{id,version}], author_seq?}` | proposer |
| `poly-reject` | `{reason, id, rev, author_seq?}`, `reason` ∈ `stale`\|`orphan`\|`limit` | proposer |
| `user-joined` | `{kind}` (subject in envelope) | broadcast (excl. joiner) |
| `user-parted` | `{}` (subject in envelope) | broadcast |
| `owner-changed` | `{}` (subject in envelope) | broadcast |
| `moderation` | `{action, target_user, by, detail}` | broadcast; `mod-readonly` includes `detail.readonly` |
| `system-message` | `{message}` | broadcast |
| `pong` | `{}` | pinger |
| `error` | `{code, detail?, ref_type?, retry_after?}` | sender |

Only the **pure server-origin** types are **rejected inbound** as `bad_frame`
(`protocol.py` `SERVER_TYPES` / `validate_message`); there are 16 — `welcome`,
`session-snapshot`, `doc-snapshot`, `doc-ack`, `doc-reject`, `chat-deleted`,
`chat-recalled`, `poly-ack`, `poly-reject`, `user-joined`, `user-parted`,
`owner-changed`, `moderation`, `system-message`, `pong`, and `error` — and you
must never send one to the server. The other rows
above (`state-update`, `data-update`, `state-set`, `clicked-button`,
`chat-message`, and the `poly-*` / `doc-*` relays) are **relayed client-origin** types:
each shares a name with a §3 client→server message and stays **valid inbound**
exactly as defined there — the server routes on direction, not a distinct type
name.

A retransmitted poly proposal (same `author_seq` from the same author) is
**deduplicated silently** — no `poly-ack` and no `poly-reject`
(`engine.py`; `session.py` `_poly_result`). Rely on your own retry/ack
bookkeeping, not on a guaranteed response per proposal.

Accepted `doc-edit` proposals are canonicalized by the server, increment the
document `rev`, relay to peers as `doc-edit`, and answer the proposer with
`doc-ack`. Validation, stale-window, and document-cap failures answer
`doc-reject` with the current document snapshot for recovery.

## 7. Errors and closes

### Error codes (`ErrorCode`)

| code | when |
|---|---|
| `bad_frame` | malformed JSON, non-object, unknown type, a server-only type sent inbound, wrong first frame |
| `unauthorized` | identity could not be established (accompanies close `4403`) |
| `forbidden` | owner-only verb by a non-owner; unauthorized chat delete/recall; mod self-target or ineligible target |
| `readonly` | a write-lane frame from a read-only connection |
| `rate_limited` | lane bucket exhausted; carries `retry_after` (seconds) |
| `too_large` | a frame, field, or value exceeded its cap |
| `unknown_session` | join to a nonexistent session (accompanies close `4404`) |
| `dialect_mismatch` | join refused — the session's `dialect` is not among the connection's declared `dialects` (accompanies close `4409`) |

`error` is the **one** server frame that may arrive **without** a full envelope:
errors raised at the transport edge (a bad `hello`, an oversize or malformed
frame, a rate-limit or violation during the read loop) are sent bare —
`{type, code, detail?, ref_type?, retry_after?}` with no `seq`/`message_id`.
Errors from in-session dispatch are stamped normally. Do not require envelope
fields on an `error`.

`stale` and `internal` are defined `ErrorCode` values but are **not** currently
emitted as WS `error` frames: polydoc rev-staleness surfaces as
`poly-reject {reason:"stale"}` (§6), and an unexpected server-side dispatch
fault is logged server-side without tearing down the connection
(`transport.py` `_run_read_loop`). (`internal` is used only on the HTTP surface.)

### Close codes

| code | meaning |
|---|---|
| `4400` | protocol violation (bad hello; `max_violations` reached, default 3) |
| `4401` | kicked by the owner (no `error` frame precedes it; the close code is the only signal, and a client must not rejoin on its own) |
| `4403` | banned, guests-not-allowed, or unauthorized identity |
| `4404` | unknown session |
| `4408` | slow consumer (send queue overflowed after cursor shedding) |
| `4409` | session dialect mismatch — the connection's declared `dialects` (default `["noisemaker-dsl"]`) does not include the session's `dialect` |
| `4423` | session locked |
| `4429` | roster full, per-user connection cap (`max_conns_per_user`), global connection cap, or sustained rate abuse |
| `1011` | heartbeat/receive timeout (transport reap) |
| `1009` | a single frame exceeded the WebSocket `max_msg_size` (`max_snapshot_frame`) |

Malformed or oversize frames each increment a violation counter; the connection
is closed `4400` once it reaches `max_violations` (default 3). There is no
`1012` "server restart" frame in v1.

## 8. Rate limits, sizes, heartbeat

### Lane token buckets (per connection; defaults)

| lane | types | rate / s | burst |
|---|---|---|---|
| fast | `state-update`, `data-update`, `poly-cursor`, `doc-cursor` | 60 | 120 |
| proposal | `poly-token-upsert`, `poly-token-delete`, `doc-edit` | 10 | 20 |
| chat | `chat-message` | 1 | 5 |
| control | `clicked-button`, `chat-delete`, `chat-recall`, `ping`, `session-state`, `poly-lock`, all `mod-*` | 5 | 10 |
| snapshot | `state-set`, `poly-snapshot`, `doc-create`, `doc-reset` | 0.2 | 2 |

An over-rate frame is **dropped** (not dispatched) and answered with
`error {code:"rate_limited", retry_after}` (error emission itself is throttled to
about once per second). A lane held continuously exhausted for longer than
`abuse_window` (default 10 s) closes the connection `4429`
(`transport.py` `_run_read_loop`). Pace to stay within a lane's steady rate;
bursts up to the burst ceiling are fine.

### Sizes

- General frame ≤ `max_frame` = **64 KiB**.
- Snapshot-lane frames (`state-set`, `poly-snapshot`, `doc-create`, `doc-reset`) ≤ `max_snapshot_frame` =
  **1 MiB**; this is also the WebSocket `max_msg_size` (a larger single frame
  ends the connection with `1009`).
- `value` ≤ 8 KiB compact JSON. Per-field string caps are in §3.
- JSON numbers must decode to finite values; overflowing exponents such as
  `1e999` are rejected along with `NaN` and `Infinity`.
- These frame-size limits apply to client input. Server snapshots can contain
  up to the aggregate session budget. Each connection has one additional
  snapshot reservation of `max_session_bytes + max_frame` bytes beyond its
  ordinary send queue. That reservation remains occupied until the send
  completes; repeated snapshots cannot accumulate outside the queue budget.

### Heartbeat

The server sends WebSocket PING frames every `ping_interval` (default 20 s) and
reaps a connection (`1011`) with no PONG within `ping_timeout` (default 60 s);
`autoping` is off server-side, so it answers a client's WS PING with a PONG
itself. Let your WebSocket stack auto-answer server PINGs. The app-level
`ping` → `pong` JSON round-trip is separate and useful as an ordering barrier:
because the server processes one connection's frames in order, a received `pong`
proves every earlier frame you sent has been handled.

## 9. Resync

- To resync at any time, send `session-state`; the server replies with a fresh
  full `session-snapshot` (§2). Rebuild local state from it and resume applying
  live broadcasts by `seq`.
- On `poly-reject {reason:"stale"}` your `base_rev` was behind the server. Adopt
  the `rev` in the reject, re-derive your edit against current document state
  (from the latest `poly-*` broadcasts or a fresh `session-state`), and resubmit
  with the new `base_rev`. `reason:"orphan"` means the `parentId` no longer
  exists; `reason:"limit"` means a node/text cap was hit — neither is retryable
  unchanged.
- On `doc-reject`, adopt the supplied `snapshot`, rebase any still-local
  optimistic edits onto that text/revision, and resubmit with the new `baseRev`
  when appropriate.
- The polydoc `rev` is strictly monotonic and server-authoritative; never assume
  a local optimistic apply is final until you see the `poly-ack` (or the relayed,
  `rev`-decorated broadcast).

## 10. Logging

Tokens, cookies, and frame payloads are never logged (`transport.py` module
contract). Document text follows the same rule: `doc-*` text is not logged at
any level, including rejection and recovery paths.

## 11. Reconnect and disconnect

- **Anon token reuse.** Persist the `anon_token` from your first `welcome` and
  send it in `hello` (or as the `SEANCE_ANON` cookie) on reconnect to keep the
  same `user_id` (and thus roster identity, owner eligibility, and any ban)
  across reconnects and server restarts (`transport.py` identity resolve;
  verified by `test_transport.py::test_anon_token_reconnect_is_stable_identity`).
- **Disconnect promptly by aborting, not by a graceful close.** To have your
  departure — and any resulting `user-parted` / `owner-changed` handoff — seen by
  peers immediately, **abort the transport** (drop the connection). A graceful
  in-process `ws.close()` half-closes and can defer the server-observed leave
  (and its fan-out) by up to the transport teardown window (a few seconds).
  Clients should abort rather than close gracefully, or expect delayed parting
  (`tests/helpers.py` `Peer.close`; `transport.py` `_teardown`).
- A **kick** (`4401`) is not a ban: the same identity may rejoin immediately. A
  **ban** (`4403` on rejoin) persists across freeze/thaw and blocks the
  `user_id`.
