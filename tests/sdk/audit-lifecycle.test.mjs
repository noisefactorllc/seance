// Audit tests: connection lifecycle, reject handling, in-flight bookkeeping, leaks.
// Tests named "RED:" assert the behaviour the SDK *should* have; they fail on HEAD 3f74d11 and are the evidence.
import assert from 'node:assert/strict'
import test from 'node:test'

import { createOnlineDslLayer } from '../../sdk/index.js'
import { FakeWebSocket, FakeEditor, harness, finishHandshake, tick, sleep, docEdits } from './audit-harness.mjs'

const SNAP = (text, rev = 0, id = 'main') => ({ id, title: 'Program', kind: 'dsl', rev, text, default: true })
const HELLO = { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'] }
const HELLO_RESUME = (seq) => ({ ...HELLO, resume: { last_seq: seq } })

test('RED: SDK-1 reconnect duplicates an in-flight insert the server had already applied', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])

    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    assert.equal(docEdits(sockets[0]).length, 1)
    assert.deepEqual(docEdits(sockets[0])[0].edit, { start: 2, end: 2, text: 'X' })

    // The server applied the edit (rev 1) but the socket dropped before doc-ack arrived.
    sockets[0].closeWith(1006, '')
    await tick(); await tick()
    const s2 = sockets[1]
    assert.ok(s2, 'reconnect socket created')
    await finishHandshake(Promise.resolve(), s2, [SNAP('abXc', 1)], HELLO_RESUME(2))
    await tick()

    assert.equal(layer.docs.get('main').serverText, 'abXc')
    assert.equal(editor.value, 'abXc', `editor shows ${JSON.stringify(editor.value)}: the insert was applied twice`)
    assert.equal(docEdits(s2).length, 0, `SDK re-proposed after reconnect: ${JSON.stringify(docEdits(s2))}`)
})

test('RED: SDK-1b reconnect duplicates even when the user kept typing after the in-flight edit', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])

    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    editor.value = 'abXYc'
    layer.updateLocalText('main', 'abXYc', { source: 'editor' })   // queued behind the in-flight X
    await tick()
    assert.equal(docEdits(sockets[0]).length, 1)

    sockets[0].closeWith(1006, '')
    await tick(); await tick()
    const s2 = sockets[1]
    await finishHandshake(Promise.resolve(), s2, [SNAP('abXc', 1)], HELLO_RESUME(2))
    await tick()
    assert.equal(editor.value, 'abXYc', `editor shows ${JSON.stringify(editor.value)}`)
})

test('RED: SDK-2 a non-retryable doc-reject (too_large) is resubmitted forever', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])

    const rejects = []
    layer.on('doc-reject', (frame) => rejects.push(frame))
    editor.value = 'abc' + 'X'.repeat(8)
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await tick()
    for (let round = 0; round < 6; round += 1) {
        const last = docEdits(sockets[0]).at(-1)
        sockets[0].receive({
            type: 'doc-reject', seq: 10 + round, docId: 'main', baseRev: last.baseRev, authorSeq: last.authorSeq,
            reason: 'too_large', snapshot: SNAP('abc', 0),
        })
        await tick()
    }
    const sent = docEdits(sockets[0])
    assert.equal(rejects.length, 6)
    assert.equal(sent.length, 1, `expected one attempt, SDK sent ${sent.length} doc-edit frames (one per reject, identical edit)`)
    assert.equal(editor.value, 'abc' + 'X'.repeat(8), 'local text is kept visible while held')
    // the hold lifts as soon as the local text changes
    editor.value = 'abcd'
    layer.updateLocalText('main', 'abcd', { source: 'editor' })
    await tick()
    assert.equal(docEdits(sockets[0]).length, 2)
})

test('RED: SDK-2b a doc-reject for an unknown document (snapshot: null) loops the same way', async () => {
    const { layer, sockets } = harness({ defaultDocId: 'deck:A' })
    const editorB = new FakeEditor('')
    layer.bindEditor({ docId: 'deck:B', editor: editorB })
    const first = layer.joinSession('room')
    await tick()
    // A session created by a single-editor app: only "main" exists server-side.
    await finishHandshake(first, sockets[0], [SNAP('abc', 0, 'main')])

    editorB.value = 'deck b text'
    layer.updateLocalText('deck:B', editorB.value, { source: 'editor' })
    await tick()
    for (let round = 0; round < 5; round += 1) {
        const last = docEdits(sockets[0]).at(-1)
        sockets[0].receive({
            type: 'doc-reject', seq: 10 + round, docId: 'deck:B', baseRev: last.baseRev, authorSeq: last.authorSeq,
            reason: 'invalid', snapshot: null,
        })
        await tick()
    }
    assert.equal(docEdits(sockets[0]).length, 1, `SDK sent ${docEdits(sockets[0]).length} identical proposals for a document the server does not have`)
})

test('RED: SDK-3 a dropped doc-edit (error rate_limited, no ack) is retransmitted with the same authorSeq and the lane recovers', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])

    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    assert.equal(docEdits(sockets[0]).length, 1)
    // transport dropped the frame and answered a bare error (protocol.md section 8): no doc-ack, no doc-reject ever comes
    sockets[0].receive({ type: 'error', code: 'rate_limited', ref_type: 'doc-edit', retry_after: 0.05 })
    editor.value = 'abXYc'
    layer.updateLocalText('main', 'abXYc', { source: 'editor' })
    await sleep(120)
    const sent = docEdits(sockets[0])
    assert.equal(sent.length, 2, `expected a retransmit after retry_after, got ${sent.length} frames`)
    assert.equal(sent[1].authorSeq, sent[0].authorSeq, 'retransmit reuses the authorSeq so the server retry cache dedups it')
    assert.deepEqual(sent[1].edit, { start: 2, end: 2, text: 'X' })
    sockets[0].receive({ type: 'doc-ack', seq: 5, docId: 'main', rev: 1, authorSeq: sent[0].authorSeq, edit: sent[0].edit })
    await tick()
    const doc = layer.docs.get('main')
    assert.equal(docEdits(sockets[0]).length, 3, 'the queued follow-up edit flows after the ack')
    assert.deepEqual(docEdits(sockets[0])[2].edit, { start: 3, end: 3, text: 'Y' })
    assert.equal(doc.serverText, 'abXc')
})

test('SDK-3c an in-flight proposal with no answer at all is retransmitted, then the dead socket is dropped for reconnect', async () => {
    const { layer, sockets } = harness({ inFlightTimeoutMs: 15, inFlightRetransmits: 2 })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    const disconnects = []
    layer.on('disconnect', (d) => disconnects.push(d))
    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await sleep(80)
    const sent = docEdits(sockets[0])
    assert.equal(sent.length, 3, 'original + 2 retransmits')
    assert.ok(sent.every((f) => f.authorSeq === sent[0].authorSeq))
    assert.equal(sockets[0].closed, true, 'socket dropped after the retransmit budget')
    assert.equal(disconnects.length, 1)
    assert.equal(disconnects[0].willReconnect, true)
    assert.ok(sockets[1], 'reconnect attempted')
    layer.goOffline()
})

test('SDK-3d a paste larger than one frame is shipped as sequential chunks', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    editor.value = 'ab' + 'x'.repeat(40000) + 'c'
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    let rev = 0
    for (let round = 0; round < 4; round += 1) {
        await tick()
        const frames = docEdits(sockets[0])
        const last = frames[frames.length - 1]
        if (!last || last.acked) break
        assert.ok(JSON.stringify(last).length < 65536, 'every frame stays under max_frame')
        last.acked = true
        rev += 1
        sockets[0].receive({ type: 'doc-ack', seq: 10 + rev, docId: 'main', rev, authorSeq: last.authorSeq, edit: last.edit })
    }
    await tick()
    const doc = layer.docs.get('main')
    assert.equal(docEdits(sockets[0]).length, 3, '40,000 chars => 3 chunks of at most 15,000')
    assert.equal(doc.serverText, editor.value)
    assert.equal(doc.inFlight, null)
})

test('RED: SDK-4 a 4401 kick is terminal: no rejoin, a typed disconnect event, status offline', async () => {
    const { layer, sockets } = harness()
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    const events = []
    for (const name of ['status', 'error', 'offline', 'moderation', 'welcome', 'disconnect']) layer.on(name, (p) => events.push([name, p]))

    sockets[0].closeWith(4401, 'kicked')
    await tick(); await tick()
    const seen = events.map(([n, p]) => `${n}:${typeof p === 'string' ? p : (p?.kind ?? p?.code ?? p?.type ?? 'obj')}`)
    assert.equal(sockets.length, 1, `SDK opened a new socket after a kick (${sockets.length} sockets; events: ${seen.join(' ')})`)
    const disconnect = events.find(([n]) => n === 'disconnect')?.[1]
    assert.deepEqual(disconnect, { code: 4401, reason: 'kicked', kind: 'kicked', willReconnect: false, attempt: 0 })
    assert.equal(layer.getStatus(), 'offline')
})

test('RED: SDK-4b a 4403 ban answered to a reconnect attempt stops the retry loop', async () => {
    const { layer, sockets } = harness()
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    const errors = []
    const disconnects = []
    layer.on('error', (e) => errors.push(e))
    layer.on('disconnect', (d) => disconnects.push(d))

    sockets[0].closeWith(1006, '')                 // network blip; meanwhile the owner banned us
    await tick(); await tick()
    const s = sockets[1]
    assert.ok(s, 'one reconnect attempt')
    s.open()
    s.receive({ type: 'error', code: 'forbidden', detail: 'banned' })
    s.closeWith(4403, 'banned')
    await tick(); await tick(); await tick()
    assert.equal(sockets.length, 2, `SDK kept reconnecting through the ban: ${sockets.length} sockets`)
    assert.equal(layer.getStatus(), 'offline')
    assert.deepEqual(disconnects.map((d) => [d.code, d.kind, d.willReconnect]), [[1006, null, true], [4403, 'forbidden', false]])
    assert.equal(errors.length, 1, 'the error frame is emitted once, not twice')
})

test('SDK-4c locked / full / dialect closes during a first join reject connect() with a typed code', async () => {
    for (const [closeCode, frameCode, detail, kind] of [[4423, 'forbidden', 'locked', 'locked'], [4429, 'forbidden', 'roster full', 'limit'], [4409, 'dialect_mismatch', 'session dialect is layers', 'dialect-mismatch']]) {
        const { layer, sockets } = harness()
        const disconnects = []
        layer.on('disconnect', (d) => disconnects.push(d))
        const promise = layer.joinSession('room')
        const outcome = promise.then(() => null, (e) => e)
        await tick()
        sockets[0].open()
        sockets[0].receive({ type: 'error', code: frameCode, detail })
        sockets[0].closeWith(closeCode, detail)
        const error = await outcome
        assert.equal(error.code, frameCode)
        assert.equal(error.closeCode, closeCode, `closeCode for ${kind}`)
        assert.deepEqual(disconnects.map((d) => [d.code, d.kind, d.willReconnect]), [[closeCode, kind, false]])
        assert.equal(layer.getStatus(), 'offline')
        assert.equal(sockets.length, 1)
    }
})

test('SDK-5 reconnect backoff: 500ms doubling to an 8s cap (jitter 25% by default, disabled here), unlimited attempts, one error event per failed attempt', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test', WebSocket: FakeWebSocket,
        fetch: async () => { throw new Error('no fetch') }, proposalThrottleMs: 0, cursorThrottleMs: 0, reconnectJitter: 0,
    })
    assert.equal(createOnlineDslLayer({}).options.reconnectJitter, 0.25)
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, FakeWebSocket.instances[0])
    const errors = []
    const statuses = []
    layer.on('error', (e) => errors.push(e))
    layer.on('status', (s) => statuses.push(s))

    const delays = []
    const realSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = (fn, ms, ...rest) => { delays.push(ms); return realSetTimeout(fn, 0, ...rest) }
    try {
        FakeWebSocket.instances[0].closeWith(1006, '')
        for (let attempt = 1; attempt <= 8; attempt += 1) {
            await tick(); await tick()
            const s = FakeWebSocket.instances[attempt]
            assert.ok(s, `attempt ${attempt}`)
            s.fail()                 // browser fires error then close when the upgrade fails
            s.closeWith(1006, '')
        }
        await tick(); await tick()
    } finally {
        globalThis.setTimeout = realSetTimeout
    }
    const reconnectDelays = delays.filter((ms) => ms >= 500)
    assert.deepEqual(reconnectDelays.slice(0, 8), [500, 1000, 2000, 4000, 8000, 8000, 8000, 8000])
    assert.equal(FakeWebSocket.instances.length, 10, 'still trying after 9 failures')
    assert.equal(statuses[0], 'connecting')
    assert.equal(layer.getStatus(), 'connecting')
    // one 'error' emit per failed attempt (the socket error event); the rejected connect() promise is not re-emitted
    assert.equal(errors.length, 8, `errors per attempt: ${errors.length / 8}`)
    layer.goOffline()
})

test('RED: SDK-6 bindEditor twice on one doc replaces the first binding without leaking listeners', async () => {
    const { layer } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    layer.bindEditor({ docId: 'main', editor })
    assert.equal(editor.listenerCount, 2, 'the first input/selectionchange pair was removed')
    layer.unbindEditor('main')
    assert.equal(editor.listenerCount, 0, `${editor.listenerCount} listeners remain after unbind`)
})

test('RED: SDK-7 a throwing WebSocket constructor leaves status stuck at connecting', async () => {
    class ThrowingSocket { constructor() { throw new Error('SecurityError: insecure WebSocket from https page') } }
    const layer = createOnlineDslLayer({
        seanceUrl: 'http://seance.test', WebSocket: ThrowingSocket,
        fetch: async () => ({ ok: true, status: 201, json: async () => ({ session_id: 'abc123' }) }),
    })
    const statuses = []
    layer.on('status', (s) => statuses.push(s))
    await assert.rejects(layer.takeOnline([{ id: 'main', text: 'x' }]), /SecurityError/)
    assert.equal(layer.getStatus(), 'offline', `status is ${layer.getStatus()} with no socket and no reconnect timer`)
})

test('SDK-8 an out-of-range remote edit or a non-JSON frame is contained: typed error event plus a session-state resync', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    const errors = []
    layer.on('error', (e) => errors.push(e))
    assert.doesNotThrow(() => sockets[0].receive({ type: 'doc-edit', seq: 3, docId: 'main', rev: 1, authorSeq: 1, edit: { start: 10, end: 10, text: 'x' } }))
    assert.doesNotThrow(() => sockets[0].receiveRaw('not json'))
    assert.deepEqual(errors.map((e) => e.code), ['client_desync', 'bad_frame'])
    assert.equal(sockets[0].sent.filter((m) => m.type === 'session-state').length, 1, 'one resync request')
    // local typing during the resync survives the snapshot
    editor.value = 'abcL'
    layer.updateLocalText('main', 'abcL', { source: 'editor' })
    sockets[0].receive({ type: 'session-snapshot', seq: 9, docs: [SNAP('>abcx', 2)] })
    await tick()
    const doc = layer.docs.get('main')
    assert.equal(doc.rev, 2)
    assert.equal(doc.serverText, '>abcx')
    assert.equal(editor.value, '>abcxL')
})

test('SDK-9 unknown frame types and unknown reject reasons are tolerated (verified non-issue)', async () => {
    const { layer, sockets } = harness()
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    assert.doesNotThrow(() => sockets[0].receive({ type: 'user-joined', seq: 3, kind: 'anon' }))
    assert.doesNotThrow(() => sockets[0].receive({ type: 'future-type', seq: 4, payload: { __proto__: { polluted: true } } }))
    assert.doesNotThrow(() => sockets[0].receive({ type: 'pong', seq: 5 }))
    assert.equal(({}).polluted, undefined)
    assert.equal(layer.lastSeq, 5)
})

test('SDK-10 prototype-pollution probes via docId / node ids land in Maps, not object prototypes (verified non-issue)', async () => {
    const { layer, sockets } = harness()
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0], [SNAP('abc', 0, 'main'), SNAP('x', 0, '__proto__')], HELLO, {
        rev: 1, programText: '', nodes: [{ id: '__proto__', kind: 'k', text: 't', version: 1, parentId: null }, { id: 'constructor', kind: 'k', text: 't', version: 1, parentId: null }],
    })
    sockets[0].receive({ type: 'doc-edit', seq: 3, docId: '__proto__', rev: 1, authorSeq: 1, edit: { start: 0, end: 0, text: 'y' } })
    sockets[0].receive({ type: 'poly-token-upsert', seq: 4, rev: 2, id: 'constructor', kind: 'k', text: 'z', version: 2, parentId: null })
    assert.equal(layer.docs.get('__proto__').serverText, 'yx')
    assert.equal(layer.nodes.get('constructor').text, 'z')
    assert.equal(Object.prototype.polluted, undefined)
    assert.equal(({}).constructor, Object)
})

test('SDK-11 error event payload shapes: server frame object and WebSocket error event (rejected reconnects are not re-emitted)', async () => {
    const { layer, sockets } = harness()
    const payloads = []
    layer.on('error', (e) => payloads.push(e))
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0])
    sockets[0].receive({ type: 'error', code: 'rate_limited', retry_after: 1 })
    sockets[0].fail()
    sockets[0].closeWith(1006, '')
    await tick(); await tick()
    sockets[1].fail()
    sockets[1].closeWith(1006, '')
    await tick(); await tick()
    const kinds = payloads.map((p) => (p instanceof Error ? 'Error' : p?.type === 'error' && p.code ? 'frame' : 'event'))
    assert.deepEqual(kinds, ['frame', 'event', 'event'])
    assert.equal(String(payloads[0].message), 'undefined', 'a frame has no .message: apps rendering err.message show undefined/[object Object]')
    layer.goOffline()
})

test('SDK-12 goOffline is idempotent and clears timers; join/leave cycles do not grow docs or listeners', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    for (let cycle = 0; cycle < 50; cycle += 1) {
        const p = layer.joinSession('room')
        await tick()
        await finishHandshake(p, sockets.at(-1))   // goOffline() forgets lastSeq: no resume on a fresh join
        editor.value = `abc${cycle}`
        layer.updateLocalText('main', editor.value, { source: 'editor' })
        layer.goOffline()
        layer.goOffline()
    }
    assert.equal(layer.docs.size, 1)
    assert.equal(layer.getStatus(), 'offline')
    assert.equal(layer._reconnectTimer, null)
    assert.equal(layer.docs.get('main').proposalTimer, null)
    assert.equal(editor.listenerCount, 2)
})

test('SDK-13 takeOnline double-click creates two server sessions and rejects the first caller', async () => {
    const { layer, fetchCalls, sockets } = harness()
    const a = layer.takeOnline([{ id: 'main', text: 'x' }])
    const b = layer.takeOnline([{ id: 'main', text: 'x' }])
    const aOutcome = a.then(() => 'resolved', (err) => err.message)
    await tick(); await tick()
    assert.equal(fetchCalls.length, 2, 'two POST /v1/sessions')
    assert.equal(await aOutcome, 'connection superseded')
    await finishHandshake(b, sockets.at(-1), [SNAP('x')], null)
    assert.equal(layer.getStatus(), 'online')
})

test('SDK-15 a doc-snapshot broadcast leaves unchanged documents (and their in-flight edit) alone', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, sockets[0], [SNAP('abc', 0, 'main'), SNAP('xyz', 0, 'other')])
    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    const sent = docEdits(sockets[0])
    assert.equal(sent.length, 1)
    // the owner reset "other"; the server rebroadcasts the whole collection
    sockets[0].receive({ type: 'doc-snapshot', seq: 4, docs: [SNAP('abc', 0, 'main'), { ...SNAP('xyz2', 1, 'other'), default: false }] })
    const main = layer.docs.get('main')
    assert.equal(main.inFlight?.authorSeq, sent[0].authorSeq, 'in-flight edit survives')
    assert.equal(editor.value, 'abXc', 'local text survives')
    assert.equal(layer.docs.get('other').serverText, 'xyz2')
    sockets[0].receive({ type: 'doc-ack', seq: 5, docId: 'main', rev: 1, authorSeq: sent[0].authorSeq, edit: sent[0].edit })
    await tick()
    assert.equal(main.serverText, 'abXc')
    assert.equal(docEdits(sockets[0]).length, 1, 'no duplicate proposal')
})

test('SDK-16 a non-JSON frame during the handshake rejects connect() instead of hanging (doc-lanes DL-1)', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    sockets[0].open()
    sockets[0].receive({ type: 'welcome', seq: 1, you: { user_id: 'u1', username: 'Ada', readonly: false } })
    sockets[0].receiveRaw('{"type":"session-snapshot","seq":2,"state":[{"id":"x","value":NaN}],"docs":[]}')
    await assert.rejects(promise, (e) => e.code === 'bad_frame')
    sockets[0].closeWith(1006, '')
    await tick()
    assert.equal(layer.getStatus(), 'offline')
})
