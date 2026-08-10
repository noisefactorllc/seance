# Seance Online DSL SDK

Zero-dependency browser ESM layer for taking a Handfish-backed DSL editor online.

Install the `0.2.1` release from npm, or import the checked-out source directly:

```sh
npm install @noisefactor/seance@0.2.1
```

```js
import { createOnlineDslLayer } from '@noisefactor/seance/sdk'

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
server snapshot's default document, so a single-editor app can collaborate with
Visualize's `deck:A` default document without product-specific routing. Pass an
explicit `docId` only when the app owns a named document such as `deck:B`.

Pass `dialect` (default `'noisemaker-dsl'`) to declare the kind of document
this session's frames carry, and `dialects` (default `[dialect]`) to declare
every dialect this client is willing to join. `hello` always sends the
declared `dialects` list; the server refuses a join when the session's dialect
isn't among them, closing with an `error` frame whose `code` is
`'dialect_mismatch'` — match on `error.code` (not the message text) to show a
friendly "wrong kind of session" message instead of a generic connection
failure. `online.getSessionDialect()` returns the dialect the server confirmed
in `welcome`, or `null` before that arrives.

Menu actions:

- `online.takeOnline([{ id, title, kind, text, default: true }])` creates a new
  Seance session and is the only API that seeds local text into server state.
  Pass an object `{ docs, poly }` instead of an array to seed the node lane
  (`poly`) alongside or instead of text docs — see "Node lane" below.
- `online.joinSession(sessionId)` connects to an existing session and adopts the
  server snapshot without sending local text.
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

`updateLocalText()` diffs against the local shadow, queues one proposal behind
any in-flight edit, and coalesces drag-frequency rewrites so the final accepted
document converges to the latest local text without overwhelming Seance's
proposal lane.

When Seance marks the local user read-only, `updateLocalText()` returns `null`
and never sends `doc-edit` frames. Cursor broadcasts still work so viewers can
point at code, and `goOffline()` restores the bound editor to editable local
state.

Binding callbacks:

- `validateText(text, context)` runs before local text enters the SDK shadow.
  Return `true`, `undefined`, or any object without `ok: false` to allow the
  update. Return `false`, a string reason, `{ ok: false, reason }`, or throw to
  reject the local update and emit `validation-error`.
- `onRemoteText(text, context)` runs after snapshots, remote edits, and reject
  recovery updates are applied to the editor.
- `onAcceptedText(text, context)` runs after Seance acknowledges a local edit.

The SDK rebases overlapping remote edits against local optimistic text and keeps
the visible editor aligned with the rebased local shadow. If Seance rejects a
stale edit with a recovery snapshot, pending local text is rebased on top of
that snapshot and resubmitted from the new revision.

Handfish integration:

- Uses `collabApiVersion` feature detection.
- Prefers `applyTextEdit()` / `replaceRange()` for remote edits and falls back to
  assigning `.value` for stale vendored editor bundles.
- Uses `selectionchange` for cursor broadcast.
- Uses `setRemoteSelection()` and `clearRemoteSelections()` when available.
- Sets readonly editor behavior when Seance marks the local user readonly while
  still applying remote document updates.

Node lane:

Seance's `poly` lane is a server-serialized node tree (flat, dotted-id parent
paths) independent of the text-doc lane above. It's how Layers-dialect
sessions represent a composition; DSL products can ignore it entirely.

```js
online.upsertNode('L1', { kind: 'layers-layer', text: JSON.stringify(layer) })
online.upsertNode('L1.C1', { kind: 'layers-child', text: '...', parentId: 'L1' })
online.deleteNode('L1')
online.getNodes()    // -> [{ id, kind, text, parentId, version }]
online.getNodeRev()  // -> current poly rev
```

- `upsertNode(id, { kind, text, parentId }, { resubmit = true })` and
  `deleteNode(id, { resubmit = true })` queue onto a paced FIFO sender
  (~120 ms minimum spacing) and resolve their `base_rev` against the freshest
  known node version at send time, not at call time — a relayed remote change
  that lands first is picked up automatically.
- A `poly-reject { reason: 'stale' }` is retried automatically (up to 3 send
  attempts total) against a refreshed `base_rev`; `'orphan'` and `'limit'`
  rejects never retry. Either way, exhausting the retry budget or hitting a
  non-retryable reason emits `'node-reject'`.
- `'node-snapshot'` fires once per adopted `session-snapshot`, including on
  reconnect, carrying the full node set. An involuntary reconnect (socket drop,
  not `goOffline()`) preserves locally queued writes: any upsert/delete still
  queued or already sent-but-unacked when the socket dropped is resent against
  the newly adopted node versions, so nothing is silently lost. The server
  snapshot is still authoritative for everything else — re-diff your local
  model against `'node-snapshot'` rather than assume anything beyond queued
  writes survived.
- `deleteNode()` — and a relayed delete from another connection — both remove
  the target id and every id with a dotted `<id>.` prefix (its descendants).
- Like `updateLocalText()`, a read-only connection drops the write and emits
  `'readonly-write'` instead of queuing it.
- `takeOnline({ poly: { programText, nodes } })` seeds the node lane on
  session create; see "Menu actions" above.

Events:

```js
online.on('status', status => {})
online.on('snapshot', ({ docs }) => {})
online.on('remote-edit', ({ docId, edit, rev }) => {})
online.on('doc-ack', frame => {})
online.on('doc-reject', frame => {})
online.on('node-snapshot', ({ rev, nodes }) => {})
online.on('remote-node', ({ op, node, id, removed }) => {})
online.on('node-reject', ({ id, reason, attempts }) => {})
online.on('error', frameOrEvent => {})
```

Teardown:

```js
const unbind = online.bindEditor({ docId: 'main', editor })
unbind()
online.goOffline()
```
