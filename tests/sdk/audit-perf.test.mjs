// Audit: per-keystroke cost on a large document, frame sizes, memory across join/leave cycles.
import assert from 'node:assert/strict'
import test from 'node:test'
import { performance } from 'node:perf_hooks'

import { createOnlineDslLayer } from '../../sdk/index.js'
import { diffText, applyTextEdit } from '../../sdk/textOps.js'
import { FakeWebSocket, FakeEditor, tick } from './audit-harness.mjs'

const skip = process.env.SEANCE_PERF ? false : 'set SEANCE_PERF=1 to run the benchmarks'

function bigDoc(targetChars) {
    const line = 'noise({ seed: 1234, scale: 0.5 }) | shape(3) | blend(0.25) // 🎨 comment\n'
    let out = ''
    while (out.length < targetChars) out += line
    return out
}

test('perf: 200KB document, 1,000 sequential keystrokes with immediate acks', { skip }, async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test', WebSocket: FakeWebSocket,
        fetch: async () => { throw new Error('no fetch') }, proposalThrottleMs: 0, cursorThrottleMs: 0,
    })
    const text = bigDoc(200 * 1024)
    const editor = new FakeEditor(text)
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'welcome', seq: 1, you: { user_id: 'u', username: 'u', readonly: false } })
    socket.receive({ type: 'session-snapshot', seq: 2, docs: [{ id: 'main', title: 'P', kind: 'dsl', rev: 0, text, default: true }] })
    await promise

    let rev = 0
    let acked = 0
    let bytes = 0
    const origSend = socket.send.bind(socket)
    socket.send = (data) => {
        bytes += data.length
        origSend(data)
        const frame = JSON.parse(data)
        if (frame.type === 'doc-edit') {
            rev += 1
            acked += 1
            queueMicrotask(() => socket.receive({ type: 'doc-ack', seq: 10 + rev, docId: 'main', rev, authorSeq: frame.authorSeq, edit: frame.edit }))
        }
    }
    const pos = Math.floor(text.length / 2)
    const t0 = performance.now()
    for (let i = 0; i < 1000; i += 1) {
        const at = pos + i
        editor.value = editor.value.slice(0, at) + 'x' + editor.value.slice(at)
        layer.updateLocalText('main', editor.value, { source: 'editor' })
        await tick()
    }
    const t1 = performance.now()
    const doc = layer.docs.get('main')
    assert.equal(doc.serverText, editor.value)
    console.log(`perf: 1000 keystrokes on ${(text.length / 1024).toFixed(0)}KB: ${(t1 - t0).toFixed(0)}ms total, ${((t1 - t0) / 1000).toFixed(2)}ms/keystroke, ${acked} doc-edit frames, ${(bytes / acked).toFixed(0)} bytes/frame avg`)

    // isolate the primitives
    const a = performance.now()
    for (let i = 0; i < 1000; i += 1) diffText(text, editor.value)
    const b = performance.now()
    for (let i = 0; i < 1000; i += 1) applyTextEdit(text, { start: pos, end: pos, text: 'x' })
    const c = performance.now()
    console.log(`perf: diffText(200KB) ${((b - a) / 1000).toFixed(3)}ms/call, applyTextEdit(200KB) ${((c - b) / 1000).toFixed(3)}ms/call`)
    layer.goOffline()
})

test('perf: snapshot frame size vs delta size for a 200KB doc', { skip }, () => {
    const text = bigDoc(200 * 1024)
    const snapshot = JSON.stringify({ type: 'session-snapshot', docs: [{ id: 'main', title: 'P', kind: 'dsl', rev: 1, text, default: true }] })
    const delta = JSON.stringify({ type: 'doc-edit', docId: 'main', baseRev: 1, authorSeq: 1, edit: { start: 100, end: 100, text: 'x' } })
    console.log(`perf: snapshot ${snapshot.length} bytes, delta ${delta.length} bytes; a doc-reject carries a full snapshot each time`)
    assert.ok(delta.length < 200)
})

test('perf: heap growth over 200 join/leave cycles', { skip }, async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test', WebSocket: FakeWebSocket,
        fetch: async () => { throw new Error('no fetch') }, proposalThrottleMs: 0, cursorThrottleMs: 0,
    })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const cycle = async () => {
        const p = layer.joinSession('room')
        const socket = FakeWebSocket.instances.at(-1)
        socket.open()
        socket.receive({ type: 'welcome', seq: 1, you: { user_id: 'u', username: 'u', readonly: false } })
        socket.receive({ type: 'session-snapshot', seq: 2, docs: [{ id: 'main', title: 'P', kind: 'dsl', rev: 0, text: 'abc', default: true }] })
        await p
        layer.updateLocalText('main', 'abcd', { source: 'editor' })
        await tick()
        layer.goOffline()
        FakeWebSocket.instances.length = 0   // the harness array is the only holder of old sockets
    }
    for (let i = 0; i < 20; i += 1) await cycle()
    globalThis.gc?.()
    const before = process.memoryUsage().heapUsed
    for (let i = 0; i < 200; i += 1) await cycle()
    globalThis.gc?.()
    const after = process.memoryUsage().heapUsed
    console.log(`perf: heap delta over 200 cycles: ${((after - before) / 1024).toFixed(0)} KB (gc ${globalThis.gc ? 'exposed' : 'not exposed'}); docs=${layer.docs.size} listeners=${[...layer.listeners.values()].reduce((n, s) => n + s.size, 0)}`)
    assert.ok(after - before < 5 * 1024 * 1024)
})
