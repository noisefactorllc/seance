import assert from 'node:assert/strict'
import test from 'node:test'

import { applyTextEdit } from '../../sdk/index.js'
import { FakeEditor, harness, finishHandshake, tick, sleep, docEdits } from './audit-harness.mjs'

const snapshot = (text = 'abc', id = 'main') => ({ id, title: id, kind: 'dsl', rev: 0, text, default: id === 'main' })
const poly = { rev: 1, nodes: [{ id: 'layer', kind: 'layer', text: 'original', parentId: null, version: 1 }] }
const nodeFrames = (socket) => socket.sent.filter((frame) => frame.type === 'poly-token-upsert' || frame.type === 'poly-token-delete')
async function until(predicate) {
    const deadline = Date.now() + 2000
    while (!predicate() && Date.now() < deadline) await sleep(5)
    assert.ok(predicate(), 'condition did not settle before the deadline')
}

async function joined(t, options = {}, docs = [snapshot()], nodes = poly) {
    const context = harness(options)
    t.after(() => context.layer.goOffline())
    const promise = context.layer.joinSession('first')
    await finishHandshake(promise, context.sockets[0], docs, null, nodes)
    return context
}

test('explicit session switch discards node writes queued for the previous room', async (t) => {
    const { layer, sockets } = await joined(t, { nodeThrottleMs: 30 })
    layer.upsertNode('layer', { kind: 'layer', text: 'private first-room content' })
    await tick()
    layer.upsertNode('another', { kind: 'layer', text: 'private queued content' })
    assert.equal(nodeFrames(sockets[0]).length, 1)

    const next = layer.joinSession('second')
    await finishHandshake(next, sockets[1], [snapshot('public')], null, { rev: 0, nodes: [] })
    await sleep(50)
    assert.deepEqual(nodeFrames(sockets[1]), [], 'a join must never seed another room with old queued content')
    assert.equal(layer._nodePending.size, 0, 'old pending writes must not replay on a later reconnect')
    assert.equal(sockets[1].sent[0].resume, undefined, 'sequence numbers belong to one session')
})

test('joining another room never proposes retained documents absent from its snapshot', async (t) => {
    const { layer, sockets } = await joined(t, {}, [snapshot(), snapshot('private', 'notes')])
    const editor = new FakeEditor('private')
    layer.bindEditor({ docId: 'notes', editor })
    const rejects = []
    layer.on('doc-reject', (frame) => rejects.push(frame))
    const next = layer.joinSession('second')
    editor.value = 'private unsent edits'
    layer.updateLocalText('notes', editor.value)
    await finishHandshake(next, sockets[1], [snapshot('public')], null, { rev: 0, nodes: [] })
    await tick()
    assert.deepEqual(docEdits(sockets[1]), [], 'absent documents must not be sent to the new room')
    assert.equal(editor.value, 'private unsent edits', 'an absent bound editor retains visible local text')
    assert.equal(rejects.at(-1)?.reason, 'missing_document', 'the user must hear why the document is not sending')
    layer.updateLocalText('notes', 'continued local typing')
    assert.equal(rejects.length, 1, 'report the absent document once, not on every keystroke')
})

test('large JSON-escaped pastes fit the wire byte cap and converge after chunk acknowledgements', async (t) => {
    const { layer, sockets } = await joined(t)
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    editor.value = `a${'\u0000'.repeat(20000)}bc`
    layer.updateLocalText('main', editor.value)
    let server = 'abc'
    let processed = 0
    for (let round = 0; round < 8; round += 1) {
        await tick()
        const frame = docEdits(sockets[0])[processed]
        if (!frame) break
        assert.ok(Buffer.byteLength(JSON.stringify(frame)) <= 65536, 'JSON escapes count toward the 64 KiB transport cap')
        server = applyTextEdit(server, frame.edit)
        processed += 1
        sockets[0].receive({ type: 'doc-ack', docId: 'main', rev: processed, authorSeq: frame.authorSeq, edit: frame.edit })
    }
    assert.equal(server, editor.value)
    assert.equal(layer.docs.get('main').serverText, server)
    assert.equal(layer.docs.get('main').inFlight, null)
})

test('stale node retry cannot overwrite a newer queued local value', async (t) => {
    const { layer, sockets } = await joined(t)
    layer.upsertNode('layer', { kind: 'layer', text: 'older local value' })
    await tick()
    layer.upsertNode('layer', { kind: 'layer', text: 'latest local value' })
    sockets[0].receive({ type: 'poly-token-upsert', id: 'layer', kind: 'layer', text: 'remote value', parentId: null, version: 2, rev: 2 })
    await tick()
    let serverRev = 2
    let serverText = 'remote value'
    let processed = 0
    for (let round = 0; round < 10; round += 1) {
        // Like Seance, process and deliver responses in socket order. A second
        // write can already be on the wire before the first rejection arrives.
        for (const frame of nodeFrames(sockets[0]).slice(processed)) {
            processed += 1
            if (frame.base_rev < serverRev) {
                sockets[0].receive({ type: 'poly-reject', author_seq: frame.author_seq, id: frame.id, reason: 'stale', rev: serverRev })
            } else {
                serverText = frame.text
                serverRev += 1
                sockets[0].receive({ type: 'poly-ack', author_seq: frame.author_seq, rev: serverRev, applied: [{ id: frame.id, version: serverRev }] })
            }
        }
        await tick()
        if (!layer._nodeQueue.length && !layer._nodePending.size) break
    }
    assert.equal(serverText, 'latest local value', 'the latest local value must win after retries settle')
    assert.equal(layer.getNodes()[0].text, 'latest local value')
})

test('going offline cancels an outstanding session creation before it can connect', async (t) => {
    let completeCreate
    const { layer, sockets } = harness({ fetch: () => new Promise((resolve) => { completeCreate = resolve }) })
    t.after(() => layer.goOffline())
    const creating = layer.takeOnline([snapshot()])
    const outcome = creating.then(() => null, (error) => error)
    layer.goOffline()
    completeCreate({ ok: true, json: async () => ({ session_id: 'late-room', anon_token: 'late-token' }) })
    await tick()
    assert.equal(sockets.length, 0, 'a cancelled creation must never connect when its response arrives')
    assert.match((await outcome)?.message, /cancel|supersed|closed/)
    assert.equal(layer.getStatus(), 'offline')
})

test('an unanswered node proposal reconnects and resumes the queued values in order', async (t) => {
    const { layer, sockets } = await joined(t, { inFlightTimeoutMs: 20 })
    layer.upsertNode('layer', { kind: 'layer', text: 'first' })
    layer.upsertNode('layer', { kind: 'layer', text: 'latest' })
    await until(() => sockets.length > 1)
    assert.equal(sockets[0].closed, true, 'unanswered node writes must not stay pending forever')
    assert.ok(sockets[1], 'the connection must recover from a fresh authoritative snapshot')
    await finishHandshake(Promise.resolve(), sockets[1], [snapshot()], null, poly)
    await tick()
    const first = nodeFrames(sockets[1])[0]
    assert.equal(first.text, 'first')
    sockets[1].receive({ type: 'poly-ack', author_seq: first.author_seq, rev: 2, applied: [{ id: 'layer', version: 2 }] })
    await tick()
    const latest = nodeFrames(sockets[1])[1]
    assert.equal(latest.text, 'latest')
    sockets[1].receive({ type: 'poly-ack', author_seq: latest.author_seq, rev: 3, applied: [{ id: 'layer', version: 3 }] })
    assert.equal(layer.getNodes()[0].text, 'latest')
})

test('node and text sends share the connection proposal throttle', async (t) => {
    const { layer, sockets } = await joined(t, { proposalThrottleMs: 35, nodeThrottleMs: 35 })
    const sends = []
    const socket = sockets[0]
    const send = socket.send.bind(socket)
    socket.send = (data) => {
        const frame = JSON.parse(data)
        // Model time spent serializing/dispatching a frame before it reaches the
        // transport. Pacing must be measured from delivery, not reservation.
        if (!sends.length) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20)
        if (frame.type === 'doc-edit' || frame.type === 'poly-token-upsert') sends.push(Date.now())
        send(data)
    }
    layer.updateLocalText('main', 'abcd')
    layer.upsertNode('layer', { kind: 'layer', text: 'changed' })
    await until(() => sends.length === 2)
    assert.equal(sends.length, 2)
    assert.ok(sends[1] - sends[0] >= 35, `both frame types share one lane: observed ${sends[1] - sends[0]} ms spacing`)
})

test('a node transport size rejection releases the pending item and surfaces its failure', async (t) => {
    const { layer, sockets } = await joined(t)
    const rejects = []
    layer.on('node-reject', (event) => rejects.push(event))
    layer.upsertNode('layer', { kind: 'layer', text: 'oversize' })
    await tick()
    sockets[0].receive({ type: 'error', code: 'too_large', ref_type: 'poly-token-upsert' })
    layer.upsertNode('layer', { kind: 'layer', text: 'valid replacement' })
    await tick()
    assert.deepEqual(rejects.map(({ id, reason }) => ({ id, reason })), [{ id: 'layer', reason: 'too_large' }])
    assert.equal(nodeFrames(sockets[0]).at(-1).text, 'valid replacement')
})

test('an ambiguous reconnect preserves the draft and holds writes for explicit recovery', async (t) => {
    const { layer, sockets } = await joined(t)
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const rejects = []
    layer.on('doc-reject', (frame) => rejects.push(frame))
    editor.value = 'axbc'
    layer.updateLocalText('main', editor.value)
    await tick()
    sockets[0].closeWith(1006)
    await tick()
    await tick()
    // Our x may have landed and then been replaced with z by a peer, or our
    // proposal may never have landed. Text alone cannot distinguish the two.
    await finishHandshake(Promise.resolve(), sockets[1], [{ ...snapshot('azbc'), rev: 2 }], null, poly)
    await tick()
    assert.equal(editor.value, 'axbc', 'keep the actual local draft available to the user')
    assert.equal(layer.docs.get('main').serverText, 'azbc')
    assert.deepEqual(docEdits(sockets[1]), [], 'do not guess and publish a merged document')
    assert.equal(rejects.at(-1)?.reason, 'reconnect_ambiguous')
    assert.equal(rejects.at(-1)?.snapshot.text, 'azbc', 'expose the authoritative alternative for recovery')
    sockets[1].receive({ type: 'doc-edit', docId: 'main', rev: 3, edit: { start: 4, end: 4, text: '!' } })
    editor.value = 'axbc draft'
    layer.updateLocalText('main', editor.value)
    await tick()
    assert.deepEqual(docEdits(sockets[1]), [], 'ordinary typing or peer edits must not silently resolve the uncertain history')
    assert.equal(layer.docs.get('main').serverText, 'azbc!')
    assert.equal(editor.value, 'axbc draft')
})

test('pending node writes remain visible until their own acknowledgement', async (t) => {
    const { layer, sockets } = await joined(t)
    const acknowledgements = []
    layer.on('node-ack', (frame) => acknowledgements.push(frame))
    layer.upsertNode('layer', { kind: 'layer', text: 'own first' })
    await tick()
    layer.upsertNode('layer', { kind: 'layer', text: 'own latest' })
    sockets[0].receive({ type: 'poly-token-upsert', id: 'layer', kind: 'layer', text: 'peer text', parentId: null, version: 2, rev: 2 })
    assert.deepEqual(layer.getPendingNodeWrites().map(({ text }) => text), ['own first', 'own latest'])
    const exposed = layer.getPendingNodeWrites()
    exposed[0].text = 'caller mutation'
    assert.equal(layer.getPendingNodeWrites()[0].text, 'own first')
    const first = nodeFrames(sockets[0])[0]
    sockets[0].receive({ type: 'poly-ack', author_seq: first.author_seq, rev: 3, applied: [{ id: 'layer', version: 3 }] })
    assert.deepEqual(layer.getPendingNodeWrites().map(({ text }) => text), ['own latest'])
    assert.equal(acknowledgements.length, 1)
    assert.equal(acknowledgements[0].id, 'layer')
    assert.equal(acknowledgements[0].op, 'upsert')
})

test('readonly moderation discards pending node writes and restores a working lane when lifted', async (t) => {
    const { layer, sockets } = await joined(t)
    layer.upsertNode('layer', { kind: 'layer', text: 'before readonly' })
    await tick()
    layer.upsertNode('layer', { kind: 'layer', text: 'queued before readonly' })
    sockets[0].receive({ type: 'moderation', action: 'readonly', target_user: 'u1', detail: { readonly: true } })
    assert.deepEqual(layer.getPendingNodeWrites(), [], 'revoked writes must not replay after readonly is lifted')
    sockets[0].receive({ type: 'moderation', action: 'readonly', target_user: 'u1', detail: { readonly: false } })
    layer.upsertNode('layer', { kind: 'layer', text: 'after readonly' })
    await tick()
    assert.equal(nodeFrames(sockets[0]).at(-1).text, 'after readonly')
})

test('an owner poly snapshot replaces the SDK node model and notifies its consumer', async (t) => {
    const { layer, sockets } = await joined(t)
    const snapshots = []
    layer.on('node-snapshot', (frame) => snapshots.push(frame))
    const replacement = [{ id: 'replacement', kind: 'layer', text: 'owner reset', parentId: null, version: 4 }]
    sockets[0].receive({ type: 'poly-snapshot', rev: 4, programText: '', nodes: replacement })
    assert.deepEqual(layer.getNodes(), replacement)
    assert.equal(layer.getNodeRev(), 4)
    assert.deepEqual(snapshots, [{ rev: 4, nodes: replacement }])
})

for (const method of ['applyTextEdit', 'replaceRange']) {
    test(`a deferred ${method} preserves IME text until the editor commits composition`, async (t) => {
        const { layer, sockets } = await joined(t)
        const editor = new FakeEditor('abc')
        const remoteTexts = []
        layer.bindEditor({ docId: 'main', editor, onRemoteText(text) { remoteTexts.push(text); editor.value = text } })
        if (method === 'replaceRange') editor.applyTextEdit = undefined
        editor[method] = () => ({ value: editor.value, deferred: true })
        editor.value = 'a漢bc' // uncommitted composition is deliberately absent from the SDK shadow
        editor.setSelectionRange(2, 2)
        sockets[0].receive({ type: 'doc-edit', docId: 'main', rev: 1, edit: { start: 0, end: 0, text: '>' } })
        assert.equal(editor.value, 'a漢bc', 'an onRemoteText callback must not overwrite deferred composition')
        assert.equal(editor.selectionStart, 2, 'the IME owns the caret until the deferred edit is applied')
        assert.deepEqual(remoteTexts, [], 'notify only after the editor has applied the remote text')
        // Handfish flushes the deferred remote edit before its compositionend
        // input event, so the SDK must send only the newly composed character.
        editor.value = '>a漢bc'
        editor.dispatchEvent(new Event('input'))
        await tick()
        assert.deepEqual(docEdits(sockets[0])[0].edit, { start: 2, end: 2, text: '漢' })
    })
}

test('reconnecting into view-only access preserves text typed before the disconnect', async (t) => {
    const { layer, sockets } = await joined(t)
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    editor.value = 'abc draft'
    layer.updateLocalText('main', editor.value)
    sockets[0].closeWith(1006)
    await until(() => sockets.length === 2)
    await finishHandshake(layer._connectDeferred.promise, sockets[1], [snapshot('peer')], null, undefined,
        { user_id: 'u1', readonly: true })
    assert.equal(editor.value, 'abc draft')
    assert.equal(layer.docs.get('main').recoveryReason, 'readonly_draft')
})

for (const state of ['queued', 'in flight']) {
    test(`read-only moderation preserves ${state} text until explicit recovery`, async (t) => {
        const { layer, sockets } = await joined(t)
        const editor = new FakeEditor('abc')
        layer.bindEditor({ docId: 'main', editor })
        const rejects = []
        layer.on('doc-reject', frame => rejects.push(frame))
        editor.value = 'ab draft c'
        layer.updateLocalText('main', editor.value)
        if (state === 'in flight') await tick()
        const sent = docEdits(sockets[0]).length
        sockets[0].receive({ type: 'moderation', action: 'readonly', target_user: 'u1', detail: { readonly: true } })
        assert.equal(editor.value, 'ab draft c', 'permission loss must not erase unacknowledged typing')
        assert.equal(rejects.at(-1)?.reason, 'readonly_draft')
        sockets[0].receive({ type: 'doc-reject', docId: 'main', reason: 'readonly', snapshot: { ...snapshot('peer'), rev: 1 } })
        sockets[0].receive({ type: 'doc-edit', docId: 'main', rev: 2, edit: { start: 4, end: 4, text: '!' } })
        assert.equal(editor.value, 'ab draft c', 'late rejections and peer edits must preserve the held draft')
        sockets[0].receive({ type: 'moderation', action: 'readonly', target_user: 'u1', detail: { readonly: false } })
        await tick()
        assert.equal(docEdits(sockets[0]).length, sent, 'write access returning must not publish an unreviewed merge')
        sockets[0].closeWith(1006)
        await until(() => sockets.length === 2)
        await finishHandshake(layer._connectDeferred.promise, sockets[1], [{ ...snapshot('peer!'), rev: 2 }], null)
        assert.equal(editor.value, 'ab draft c')
        assert.equal(rejects.at(-1)?.reason, 'readonly_draft')
        const rejoin = layer.joinSession('first')
        await finishHandshake(rejoin, sockets[2], [{ ...snapshot('peer!'), rev: 2 }], null)
        assert.equal(editor.value, 'peer!', 'explicit recovery adopts the server after the user saves their draft')
    })
}

test('a deferred snapshot setter does not invoke a callback that queues the same snapshot again', async (t) => {
    const { layer, sockets } = await joined(t)
    const editor = new FakeEditor('abc')
    const remoteTexts = []
    layer.bindEditor({ docId: 'main', editor, onRemoteText(text) { remoteTexts.push(text); editor.value = text } })
    let visible = 'a漢bc'
    const deferredValues = []
    Object.defineProperty(editor, 'value', { get: () => visible, set: (text) => { deferredValues.push(text) } })
    sockets[0].receive({ type: 'session-snapshot', docs: [{ ...snapshot('>abc'), rev: 1 }] })
    assert.deepEqual(deferredValues, ['>abc'], 'a callback must not duplicate the deferred snapshot write')
    assert.deepEqual(remoteTexts, [], 'do not report a snapshot as rendered before the editor applies it')
    assert.equal(editor.value, 'a漢bc')
    visible = '>a漢bc'
    editor.dispatchEvent(new Event('input'))
    await tick()
    assert.deepEqual(docEdits(sockets[0])[0].edit, { start: 2, end: 2, text: '漢' })
})
