# Seance Online DSL SDK

Zero-dependency browser ESM layer for taking a Handfish-backed DSL editor online.

The SDK is not published to npm. Use the checked-out source directly:

```js
import { createOnlineDslLayer } from './sdk/index.js'

const online = createOnlineDslLayer({
  seanceUrl: 'https://seance.noisefactor.io',
  publicAppUrl: window.location.href,
})

online.bindEditor({
  editor: document.querySelector('code-editor'),
  validateText(text, context) {
    return true
  },
  onRemoteText(text, context) {},
  onAcceptedText(text, context) {},
})
```

Omit `docId` for single-editor apps. On join, the SDK binds that editor to the
server snapshot's default document. This lets a single-editor app collaborate
with Visualize's `deck:A` default document without product-specific routing. Pass an
explicit `docId` only when the app owns a named document such as `deck:B`.

Pass `dialect` (default `'noisemaker-dsl'`) to declare the kind of document
this session's frames carry.
Pass `dialects` (default `[dialect]`) to declare every dialect this client
is willing to join. `hello` always sends the declared `dialects` list.
The server refuses a join when the session's dialect is absent from that list.
It closes with an `error` frame whose `code` is `'dialect_mismatch'`.
Match on `error.code` (not the message text) to show a friendly "wrong kind
of session" message instead of a generic connection failure. `online.getSessionDialect()` returns the dialect the server confirmed
in `welcome`, or `null` before that arrives.

Menu actions:

- `online.takeOnline([{ id, title, kind, text, default: true }])` creates a new
  Seance session and is the only API that seeds local text into server state.
  Pass an object `{ docs, poly }` instead of an array to seed the node lane
  (`poly`) alongside or instead of text docs — see "Node lane" below.
- `online.joinSession(sessionId)` connects to an existing session and adopts the
  server snapshot without sending local text. An explicit join clears the previous
  room's pending writes and sequence counter. Bound documents absent from the new
  snapshot retain their visible local text and do not send proposals. Attempting
  to change one emits `'doc-reject'` with reason `'missing_document'` once, so the
  app can explain that this document is not part of the session.
- `online.goOffline()` disconnects, clears remote selections, and leaves visible
  local editor text unchanged.

URL wiring:

- `online.readSessionFromUrl(window.location)` reads `?seance=<sessionId>`.
- `online.writeSessionToUrl(window.location, sessionId)` preserves existing app
  params while adding/replacing `seance`.
- `online.getShareUrl()` returns a copyable share URL for the active session.

Programmatic writers such as graph editors, parameter controls, randomizers, or
loaders must call:

```js
online.updateLocalText('main', nextDslText, { source: 'graph' })
```

`updateLocalText()` compares the text with the local shadow. It queues one
proposal behind any in-flight edit. It combines rewrites from frequent drag
updates so the final accepted document converges to the latest local text.
This prevents those rewrites from overwhelming Seance's proposal lane.

When Seance marks the local user read-only, `updateLocalText()` returns `null`
and never sends `doc-edit` frames. Cursor broadcasts still work so viewers can point at code.
`goOffline()` restores the bound editor to editable local state.

Binding callbacks:

- `validateText(text, context)` runs before local text enters the SDK shadow.
  To allow the update, return `true`, `undefined`, or any object without `ok: false`.
  To reject it, return `false`, a string reason, `{ ok: false, reason }`, or throw.
  Rejection emits `validation-error`.
- `onRemoteText(text, context)` runs after snapshots, remote edits, and reject
  recovery updates are applied to the editor.
- `onAcceptedText(text, context)` runs after Seance acknowledges a local edit.

The SDK rebases overlapping remote edits against local optimistic text and keeps
the visible editor aligned with the rebased local shadow. If Seance rejects a
stale edit with a recovery snapshot, pending local text is rebased on top of
that snapshot. The SDK resubmits the text from the new revision.

Handfish integration:

- Uses `collabApiVersion` feature detection.
- Prefers `applyTextEdit()` / `replaceRange()` for remote edits and falls back to
  assigning `.value` for stale vendored editor bundles.
- Respects a `{ deferred: true }` edit result during IME composition, leaving the
  caret and rendering callback untouched until the editor commits its combined
  text through the normal input event. Full-value writes likewise notify only
  when reading the editor returns the requested text; a deferred setter cannot
  trigger an immediate callback that bypasses composition.
- Uses `selectionchange` for cursor broadcast.
- Uses `setRemoteSelection()` and `clearRemoteSelections()` when available.
- Sets readonly editor behavior when Seance marks the local user readonly while
  still applying remote document updates.

Node lane:

Seance's `poly` lane is a server-serialized node tree (flat, dotted-id parent
paths) independent of the text-doc lane above. Layers-dialect sessions use
it to represent a composition. DSL products can ignore it entirely.

```js
online.upsertNode('L1', { kind: 'layers-layer', text: JSON.stringify(layer) })
online.upsertNode('L1.C1', { kind: 'layers-child', text: '...', parentId: 'L1' })
online.deleteNode('L1')
online.getNodes()    // -> [{ id, kind, text, parentId, version }]
online.getNodeRev()  // -> current poly rev
online.getPendingNodeWrites() // -> [{ op, id, kind?, text?, parentId? }], in send order
```

- `upsertNode(id, { kind, text, parentId }, { resubmit = true })` and
  `deleteNode(id, { resubmit = true })` queue onto a paced FIFO sender
  (~120 ms minimum spacing), with one pending node proposal at a time. A stale
  retry completes before later writes, preserving the caller's update order.
  Node and text proposals share the connection's rate allowance.
  They resolve their `base_rev` against the latest
  known node version at send time, not at call time.
  Thus, they automatically include a relayed remote change that arrives first.
- The SDK automatically retries a `poly-reject { reason: 'stale' }` against a
  refreshed `base_rev` (up to 3 send attempts total).
  `'orphan'` and `'limit'` rejects never retry.
  Exhausting the retry budget or receiving a non-retryable reason emits `'node-reject'`.
- A node proposal unanswered for `inFlightTimeoutMs` triggers reconnect and
  snapshot recovery. A rate-limit error shortens that wait to `retry_after`.
  Transport validation errors emit `'node-reject'` and release the failed write.
- `'node-ack'` fires after an own acknowledged write updates `getNodes()`.
  `getPendingNodeWrites()` returns copies of outstanding writes in send order.
  Use these to overlay local pending values; a peer's node version advancing
  does not prove that a local write was accepted.
- `'node-snapshot'` fires once per adopted `session-snapshot`, including on
  reconnect, and after an owner's relayed `poly-snapshot`, carrying the full node set.
  An involuntary reconnect (socket drop, not `goOffline()`) preserves locally
  queued writes. The SDK resends any upsert/delete that was queued or sent
  but unacknowledged when the socket dropped. It uses the newly adopted node
  versions, so nothing is silently lost.
  The server snapshot remains authoritative for everything else.
  Compare your local model against `'node-snapshot'` again.
  Do not assume anything beyond queued writes survived.
- `deleteNode()` removes the target id and every id with a dotted `<id>.`
  prefix (its descendants). A relayed delete from another connection does the same.
- Like `updateLocalText()`, a read-only connection drops the write and emits
  `'readonly-write'` instead of queuing it. Becoming read-only discards already
  pending node writes and emits `'node-reject'` with reason `'readonly'`.
- `takeOnline({ poly: { programText, nodes } })` seeds the node lane on
  session creation. See "Menu actions" above.

Status:

`online.getStatus()` and the `'status'` event use four values: `'offline'`,
`'connecting'` (first connection and every automatic reconnect), `'online'`,
and `'readonly'` (joined, but Seance refuses this user's writes). Treat
`'readonly'` as its own UI state: `updateLocalText()`, `upsertNode()` and
`deleteNode()` drop silently and emit `'readonly-write'` while it lasts.

Connection lifecycle:

- An involuntary socket drop (server restart, proxy reload, sleep, network
  blip) reconnects automatically with exponential backoff from
  `reconnectBaseMs` (500) to `reconnectMaxMs` (8000), with 25 % jitter
  (`reconnectJitter`) and no attempt cap. The same `anon_token` is sent, so
  the identity is stable across reconnects while this layer instance lives.
  Local text typed while reconnecting is kept. For an in-flight edit, automatic
  recovery requires the snapshot to exactly match either the last acknowledged
  text or that text with the in-flight edit applied. The former is treated as
  unapplied and the latter as applied. This assumes a peer did not independently
  recreate either exact text; v1 has no acceptance receipt to prove that history.
  If neither matches, the SDK retains the draft and emits `'doc-reject'` with
  `reason: 'reconnect_ambiguous'` and the authoritative `snapshot`. It holds all
  proposals for that document, including later typing, across peer updates and
  automatic reconnects. Surface this event: the user must copy/save the draft,
  explicitly rejoin to adopt server state, then deliberately apply their reviewed
  merge. An explicit `joinSession()` or `takeOnline()` clears the hold.
- Losing write access with unacknowledged text preserves the visible draft and
  emits `'doc-reject'` with `reason: 'readonly_draft'`. Peer updates, late
  rejections, reconnects, and restored write access do not erase or automatically
  publish that draft. Use the same save, explicit rejoin, and reviewed merge
  recovery described above.
- Server close codes decide whether the SDK comes back. `4401` (kicked),
  `4403` (banned or guests not allowed), `4423` (locked), `4404`, `4409`
  (dialect mismatch) and `4400` are terminal: the SDK goes `'offline'` and
  stays there. Everything else (`1006`, `1011`, `4408`, `4429`) reconnects.
- Every involuntary close emits `'disconnect'` with
  `{ code, reason, kind, willReconnect, attempt }`, where `kind` is one of
  `'kicked'`, `'forbidden'`, `'locked'`, `'limit'`, `'unknown-session'`,
  `'dialect-mismatch'`, `'protocol'`, `'slow-consumer'` or `null`. Show the
  user something when `willReconnect` is `false`.
- A join refused during the handshake rejects `takeOnline()` /
  `joinSession()` with an `Error` carrying `code` (the wire error code, for
  example `'dialect_mismatch'`, `'unknown_session'`, `'forbidden'`) and
  `closeCode`.
- A proposal that gets no answer is retransmitted with its original
  `authorSeq` (Seance answers retransmits from its retry cache, so nothing is
  applied twice); after `inFlightRetransmits` (3) unanswered resends spaced
  `inFlightTimeoutMs` (10000) apart the socket is dropped and the reconnect
  path takes over. A `rate_limited` error resends after its `retry_after`.
- Large pastes are sent as sequential chunks of at most 15,000 UTF-16 units,
  reduced further when JSON escaping requires it, to fit Seance's 64 KiB cap.
- `goOffline()` forgets the session (`lastSeq`, retry state); the next join
  starts clean.

Identity:

The SDK keeps anonymous identity in browser `sessionStorage` by default, so
reloading the same tab preserves the creator's identity even when cross-site
cookies are unavailable. Storage is scoped to the normalized Seance base URL,
including its scheme, port and path. Different application origins have separate
browser storage. Closing the tab ends this default persistence.

Pass `anonTokenStorage: null` (or `false`) to keep the token in memory only, or
inject a synchronous storage object with `getItem(key)` and `setItem(key, value)`.
An explicit `anonToken` takes priority over the stored value and is saved to the
selected storage. If browser storage access or a read/write fails, the SDK keeps
working with its in-memory token.

The token is sent in the `X-Seance-Anon` header when creating a session and in
the WebSocket `hello` frame when joining; it is never added to a URL. A replacement
token issued by the server is saved automatically. Going offline or receiving
a forbidden/ban response does not clear identity or retry as a new anonymous user.

Text offsets:

Every offset in `edit` and cursor `range` objects is a UTF-16 code unit
index, exactly what `String.prototype.slice`, `selectionStart` and Handfish
produce. Seance stores documents in the same units.

Events:

```js
online.on('status', status => {})                 // 'offline' | 'connecting' | 'online' | 'readonly'
online.on('disconnect', ({ code, reason, kind, willReconnect, attempt }) => {})
online.on('offline', () => {})                    // after goOffline()
online.on('welcome', frame => {})                 // identity, roster, owner, settings, dialect
online.on('snapshot', ({ docs }) => {})
online.on('remote-edit', ({ docId, edit, rev }) => {})
online.on('local-text', ({ docId, text, meta }) => {})
online.on('doc-ack', frame => {})
online.on('doc-reject', frame => {})              // stale retries; reconnect_ambiguous requires explicit recovery
online.on('validation-error', ({ docId, text, meta, reason }) => {})
online.on('readonly-write', payload => {})
online.on('moderation', frame => {})
online.on('node-snapshot', ({ rev, nodes }) => {})
online.on('remote-node', ({ op, node, id, removed }) => {})
online.on('node-ack', ({ id, op, rev, author_seq, applied }) => {})
online.on('node-reject', ({ id, reason, attempts }) => {})
online.on('error', payload => {})                 // a server error frame ({ type: 'error', code, detail }), an Error (code, detail, frame), or a WebSocket error event
```

`'error'` payloads are not uniform: read `payload.code` when present and never
assume `payload.message` exists. Surface `doc-reject` to the user; a held
proposal means the document on screen is ahead of the session.

Teardown:

```js
const unbind = online.bindEditor({ docId: 'main', editor })
unbind()
online.goOffline()
```

Binding the same document twice replaces the earlier binding (its listeners
are removed first).

Do not call `takeOnline()` twice concurrently: each call creates a server
session (creation is rate limited per address) and the earlier connection is
superseded and rejected. `goOffline()` and newer connection requests invalidate
an outstanding creation response, so it cannot reopen a socket after cancellation.
An already processed creation may still leave an unused server session.
