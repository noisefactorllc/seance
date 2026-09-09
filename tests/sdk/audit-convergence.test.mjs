// Audit: transform parity with the Python server and two-peer convergence under random interleavings.
import assert from 'node:assert/strict'
import test from 'node:test'
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createOnlineDslLayer } from '../../sdk/index.js'
import { transformEdit, applyTextEdit, diffText } from '../../sdk/textOps.js'
import { FakeWebSocket, FakeEditor, tick } from './audit-harness.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const PY = process.env.SEANCE_PY || join(here, '../../../../seance/.venv/bin/python')

function rng(seed) {
    let s = seed >>> 0
    return () => {
        s = (s + 0x6D2B79F5) >>> 0
        let t = s
        t = Math.imul(t ^ (t >>> 15), t | 1)
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
}
const ALPHABET = 'abcXYZ\n '
function randomEdit(rand, length, maxSpan = 3) {
    const start = Math.floor(rand() * (length + 1))
    const end = Math.min(length, start + Math.floor(rand() * (maxSpan + 1)))
    const count = Math.floor(rand() * 4)
    let text = ''
    for (let i = 0; i < count; i += 1) text += ALPHABET[Math.floor(rand() * ALPHABET.length)]
    if (start === end && text === '') return randomEdit(rand, length, maxSpan)
    return { start, end, text }
}

// Port of app/textdoc.py _transform_edit (server side), for the in-process server model.
function pyTransform(edit, applied) {
    const newLen = applied.text.length
    const delta = newLen - (applied.end - applied.start)
    const insertPoint = (pos) => {
        if (pos < applied.start) return pos
        if (applied.start === applied.end) return pos + newLen
        if (pos === applied.start) return applied.start
        if (pos <= applied.end) return applied.start + newLen
        return pos + delta
    }
    const rangeStart = (pos) => {
        if (pos < applied.start) return pos
        if (applied.start === applied.end) return pos + newLen
        if (pos === applied.start) return applied.start
        if (pos < applied.end) return applied.start
        if (pos === applied.end) return applied.start + newLen
        return pos + delta
    }
    const rangeEnd = (pos) => {
        if (pos < applied.start) return pos
        if (applied.start === applied.end) return pos + newLen
        if (pos <= applied.start) return pos
        if (pos <= applied.end) return applied.start + newLen
        return pos + delta
    }
    if (edit.start === edit.end) {
        const p = insertPoint(edit.start)
        return { start: p, end: p, text: edit.text }
    }
    return { start: rangeStart(edit.start), end: rangeEnd(edit.end), text: edit.text }
}

// Port of app/textdoc.py TextDoc (apply_edit with op-log base resolution).
class ModelDoc {
    constructor(text) { this.text = text; this.rev = 0; this.oplog = [] }
    snapshot() { return { id: 'main', title: 'Program', kind: 'dsl', rev: this.rev, text: this.text, default: true } }
    apply(baseRev, edit) {
        if (!Number.isInteger(baseRev) || baseRev < 0 || baseRev > this.rev) return { rejected: 'stale' }
        if (edit.start === edit.end && edit.text === '') return { rejected: 'invalid' }
        let baseText = this.text
        const transforms = []
        if (baseRev !== this.rev) {
            if (this.oplog.length === 0) return { rejected: 'stale' }
            if (baseRev < this.oplog[0].rev - 1) return { rejected: 'stale' }
            for (let i = this.oplog.length - 1; i >= 0; i -= 1) {
                const entry = this.oplog[i]
                if (entry.rev <= baseRev) break
                transforms.push(entry)
                baseText = baseText.slice(0, entry.edit.start) + entry.prior + baseText.slice(entry.edit.start + entry.edit.text.length)
            }
            transforms.reverse()
        }
        if (edit.start < 0 || edit.end < edit.start || edit.end > baseText.length) return { rejected: 'invalid' }
        let canonical = edit
        for (const entry of transforms) canonical = pyTransform(canonical, entry.edit)
        const prior = this.text.slice(canonical.start, canonical.end)
        this.text = this.text.slice(0, canonical.start) + canonical.text + this.text.slice(canonical.end)
        this.rev += 1
        this.oplog.push({ rev: this.rev, edit: canonical, prior })
        return { rev: this.rev, edit: canonical }
    }
}

test('SDK transformEdit matches the server _transform_edit on 20,000 random pairs (Python oracle)', () => {
    const rand = rng(7)
    const cases = []
    for (let i = 0; i < 20000; i += 1) {
        const length = Math.floor(rand() * 12)
        cases.push({ local: randomEdit(rand, length, 5), remote: randomEdit(rand, length, 5) })
    }
    const proc = spawnSync(PY, [join(here, 'audit-transform-oracle.py')], { input: JSON.stringify(cases), encoding: 'utf8', cwd: join(here, '../..') })
    assert.equal(proc.status, 0, proc.stderr)
    const oracle = JSON.parse(proc.stdout)
    const mismatches = []
    cases.forEach((c, i) => {
        const js = transformEdit(c.local, c.remote)
        const py = oracle[i]
        if (js.start !== py.start || js.end !== py.end || js.text !== py.text) mismatches.push({ case: c, js, py })
    })
    assert.deepEqual(mismatches.slice(0, 5), [], `${mismatches.length} mismatches`)
})

async function makePeer(name, initial, userId) {
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test', WebSocket: FakeWebSocket,
        fetch: async () => { throw new Error('no fetch') }, proposalThrottleMs: 0, cursorThrottleMs: 0,
        connectionId: name,
    })
    const editor = new FakeEditor(initial)
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    const socket = FakeWebSocket.instances.at(-1)
    socket.open()
    socket.receive({ type: 'welcome', seq: 1, you: { user_id: userId, username: name, readonly: false } })
    socket.receive({ type: 'session-snapshot', seq: 2, docs: [{ id: 'main', title: 'P', kind: 'dsl', rev: 0, text: initial, default: true }] })
    await promise
    return { name, layer, editor, socket, processed: 0, outbound: [] }
}

async function simulate(seed, steps = 120, initial = 'noise()\nshape(3)\n') {
    const rand = rng(seed)
    FakeWebSocket.instances = []
    const model = new ModelDoc(initial)
    const A = await makePeer('A', initial, 'uA')
    const B = await makePeer('B', initial, 'uB')
    const peers = [A, B]
    let seq = 10
    const trace = []

    const serverProcess = (peer) => {
        if (peer.processed >= peer.socket.sent.length) return false
        const frame = peer.socket.sent[peer.processed]
        peer.processed += 1
        if (frame.type !== 'doc-edit') return true
        const other = peer === A ? B : A
        const result = model.apply(frame.baseRev, frame.edit)
        trace.push(`${peer.name} sent base=${frame.baseRev} ${JSON.stringify(frame.edit)} -> ${result.rejected ? 'REJECT ' + result.rejected : 'rev ' + result.rev + ' ' + JSON.stringify(result.edit)}`)
        if (result.rejected) {
            peer.outbound.push({ type: 'doc-reject', docId: 'main', baseRev: frame.baseRev, authorSeq: frame.authorSeq, reason: result.rejected, snapshot: model.snapshot() })
        } else {
            peer.outbound.push({ type: 'doc-ack', docId: 'main', rev: result.rev, authorSeq: frame.authorSeq, edit: result.edit })
            other.outbound.push({ type: 'doc-edit', docId: 'main', rev: result.rev, authorSeq: frame.authorSeq, edit: result.edit })
        }
        return true
    }
    const deliver = (peer) => {
        const frame = peer.outbound.shift()
        if (!frame) return false
        seq += 1
        trace.push(`-> ${peer.name} ${frame.type} rev=${frame.rev ?? frame.snapshot?.rev}`)
        peer.socket.receive({ ...frame, seq })
        return true
    }
    const type = (peer) => {
        const edit = randomEdit(rand, peer.editor.value.length)
        peer.editor.value = applyTextEdit(peer.editor.value, edit)
        trace.push(`${peer.name} types ${JSON.stringify(edit)} => ${JSON.stringify(peer.editor.value)}`)
        peer.layer.updateLocalText('main', peer.editor.value, { source: 'editor' })
    }

    for (let step = 0; step < steps; step += 1) {
        const r = rand()
        if (r < 0.25) type(A)
        else if (r < 0.5) type(B)
        else if (r < 0.75) serverProcess(peers[Math.floor(rand() * 2)])
        else deliver(peers[Math.floor(rand() * 2)])
        await tick()
    }
    // quiesce
    for (let round = 0; round < 500; round += 1) {
        let progress = false
        for (const p of peers) { while (serverProcess(p)) progress = true }
        for (const p of peers) { while (deliver(p)) progress = true }
        await tick(); await tick()
        if (!progress && peers.every((p) => p.processed === p.socket.sent.length && p.outbound.length === 0)) break
    }
    return { model, A, B, trace }
}

const SEEDS = Number(process.env.SEANCE_FUZZ_SEEDS || 60)

test(`two peers converge with the server model under random interleavings (${SEEDS} seeds)`, async () => {
    const failures = []
    for (let seed = 1; seed <= SEEDS; seed += 1) {
        const { model, A, B, trace } = await simulate(seed)
        const docA = A.layer.docs.get('main')
        const docB = B.layer.docs.get('main')
        const problems = []
        if (docA.serverText !== model.text) problems.push(`A.serverText=${JSON.stringify(docA.serverText)}`)
        if (docB.serverText !== model.text) problems.push(`B.serverText=${JSON.stringify(docB.serverText)}`)
        if (A.editor.value !== model.text) problems.push(`A.editor=${JSON.stringify(A.editor.value)}`)
        if (B.editor.value !== model.text) problems.push(`B.editor=${JSON.stringify(B.editor.value)}`)
        if (docA.text !== model.text) problems.push(`A.text=${JSON.stringify(docA.text)}`)
        if (docB.text !== model.text) problems.push(`B.text=${JSON.stringify(docB.text)}`)
        if (docA.inFlight || docB.inFlight) problems.push('inFlight left over')
        if (problems.length) failures.push({ seed, model: model.text, problems, trace: trace.slice(-12) })
        A.layer.goOffline(); B.layer.goOffline()
    }
    if (failures.length) console.log(JSON.stringify(failures.slice(0, 3), null, 1))
    assert.equal(failures.length, 0, `${failures.length}/${SEEDS} seeds diverged; first seeds: ${failures.slice(0, 10).map((f) => f.seed).join(',')}`)
})

test('SDK-14 diffText is UTF-16 based while the server counts code points (astral characters)', () => {
    const before = '🎉 party'
    const after = '🎉 party!'
    const edit = diffText(before, after)
    assert.deepEqual(edit, { start: 8, end: 8, text: '!' })         // UTF-16 offsets
    const py = spawnSync(PY, ['-c', 'import sys,json; t=json.loads(sys.stdin.read()); print(len(t))'], { input: JSON.stringify(before), encoding: 'utf8' })
    assert.equal(py.stdout.trim(), '7', 'python len() of the same text')  // the server sees end=8 > len=7: out of bounds
})
