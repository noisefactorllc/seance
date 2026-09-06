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
```

- `upsertNode(id, { kind, text, parentId }, { resubmit = true })` and
  `deleteNode(id, { resubmit = true })` queue onto a paced FIFO sender
  (~120 ms minimum spacing). They resolve their `base_rev` against the latest
  known node version at send time, not at call time.
  Thus, they automatically include a relayed remote change that arrives first.
- The SDK automatically retries a `poly-reject { reason: 'stale' }` against a
  refreshed `base_rev` (up to 3 send attempts total).
  `'orphan'` and `'limit'` rejects never retry.
  Exhausting the retry budget or receiving a non-retryable reason emits `'node-reject'`.
- `'node-snapshot'` fires once per adopted `session-snapshot`, including on
  reconnect, carrying the full node set.
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
  `'readonly-write'` instead of queuing it.
- `takeOnline({ poly: { programText, nodes } })` seeds the node lane on
  session creation. See "Menu actions" above.

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
