// Shared fakes for the audit tests (copied from tests/sdk/onlineDslLayer.test.mjs, plus close codes).
import assert from 'node:assert/strict'
import { createOnlineDslLayer } from '../../sdk/index.js'

export const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

export class FakeWebSocket {
    static instances = []
    constructor(url) {
        this.url = url
        this.sent = []
        this.listeners = new Map()
        this.closed = false
        FakeWebSocket.instances.push(this)
    }
    addEventListener(type, handler) {
        const set = this.listeners.get(type) || new Set()
        set.add(handler)
        this.listeners.set(type, set)
    }
    send(data) {
        if (this.closed) throw new Error('send on closed socket')
        this.sent.push(JSON.parse(data))
    }
    close() {
        if (this.closed) return
        this.closed = true
        this.emit('close', { code: 1005, reason: '', wasClean: true })
    }
    // Server-initiated close with a protocol close code (4401 kicked, 4403 banned, ...).
    closeWith(code, reason = '') {
        this.closed = true
        this.emit('close', { code, reason, wasClean: true })
    }
    open() { this.emit('open', {}) }
    receive(frame) { this.emit('message', { data: JSON.stringify(frame) }) }
    receiveRaw(data) { this.emit('message', { data }) }
    fail() { this.emit('error', { type: 'error' }) }
    emit(type, event) {
        for (const handler of this.listeners.get(type) || []) handler(event)
    }
}

export class FakeEditor extends EventTarget {
    constructor(value = '') {
        super()
        this.value = value
        this.selectionStart = 0
        this.selectionEnd = 0
        this.selectionDirection = 'none'
        this.collabApiVersion = 1
        this.readOnly = false
        this.applied = []
        this.remoteSelections = []
        this.clearedRemoteSelections = 0
        this.listenerCount = 0
    }
    addEventListener(...args) { this.listenerCount += 1; return super.addEventListener(...args) }
    removeEventListener(...args) { this.listenerCount -= 1; return super.removeEventListener(...args) }
    getSelectionRange() {
        return { start: this.selectionStart, end: this.selectionEnd, direction: this.selectionDirection }
    }
    setSelectionRange(start, end, direction = 'none') {
        this.selectionStart = start
        this.selectionEnd = end
        this.selectionDirection = direction
    }
    applyTextEdit(edit) {
        this.value = this.value.slice(0, edit.start) + edit.text + this.value.slice(edit.end)
        this.applied.push(edit)
    }
    setRemoteSelection(selection) { this.remoteSelections.push(selection) }
    clearRemoteSelections() { this.remoteSelections = []; this.clearedRemoteSelections += 1 }
}

export function harness(extra = {}) {
    FakeWebSocket.instances = []
    const fetchCalls = []
    const fetch = async (url, init) => {
        fetchCalls.push({ url, init, body: JSON.parse(init.body) })
        return { ok: true, status: 201, json: async () => ({ session_id: 'abc123', anon_token: 'anon-token' }) }
    }
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play?code=keep#frag',
        fetch,
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 0,
        reconnectBaseMs: 0,
        reconnectMaxMs: 0,
        ...extra,
    })
    return { layer, fetchCalls, sockets: FakeWebSocket.instances }
}

export async function finishHandshake(
    promise,
    socket,
    docs = [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abc', default: true }],
    hello = { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'] },
    poly = undefined,
    you = { user_id: 'u1', username: 'Ada', readonly: false },
) {
    socket.open()
    if (hello) assert.deepEqual(socket.sent[0], hello)
    socket.receive({ type: 'welcome', seq: 1, you })
    socket.receive({ type: 'session-snapshot', seq: 2, docs, ...(poly !== undefined ? { poly } : {}) })
    await promise
}

export function docEdits(socket) {
    return socket.sent.filter((msg) => msg.type === 'doc-edit')
}
