import assert from 'node:assert/strict'
import test from 'node:test'

import { createOnlineDslLayer } from '../../sdk/index.js'

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))

class FakeWebSocket {
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
        this.sent.push(JSON.parse(data))
    }

    close() {
        this.closed = true
        this.emit('close', {})
    }

    open() {
        this.emit('open', {})
    }

    receive(frame) {
        this.emit('message', { data: JSON.stringify(frame) })
    }

    emit(type, event) {
        for (const handler of this.listeners.get(type) || []) handler(event)
    }
}

class DelayedCloseFakeWebSocket extends FakeWebSocket {
    close() {
        this.closed = true
    }
}

class FakeEditor extends EventTarget {
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
    }

    getSelectionRange() {
        return {
            start: this.selectionStart,
            end: this.selectionEnd,
            direction: this.selectionDirection,
        }
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

    setRemoteSelection(selection) {
        this.remoteSelections.push(selection)
    }

    clearRemoteSelections() {
        this.remoteSelections = []
        this.clearedRemoteSelections += 1
    }
}

function harness(fetchImpl = null) {
    FakeWebSocket.instances = []
    const fetchCalls = []
    const fetch = fetchImpl || (async (url, init) => {
        fetchCalls.push({ url, init, body: JSON.parse(init.body) })
        return {
            ok: true,
            status: 201,
            json: async () => ({ session_id: 'abc123', anon_token: 'anon-token' }),
        }
    })
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play?code=keep#frag',
        fetch,
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 0,
    })
    return { layer, fetchCalls, sockets: FakeWebSocket.instances }
}

async function finishHandshake(
    promise,
    socket,
    docs = [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abc', default: true }],
    hello = { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'] },
    poly = undefined,
) {
    socket.open()
    assert.deepEqual(socket.sent[0], hello)
    socket.receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: false },
    })
    socket.receive({
        type: 'session-snapshot',
        seq: 2,
        docs,
        ...(poly !== undefined ? { poly } : {}),
    })
    await promise
}

test('URL helpers preserve existing app params while reading and writing seance', () => {
    const { layer } = harness()
    const url = 'https://app.test/play?code=abc&seance=room1&theme=dark#frag'
    assert.equal(layer.readSessionFromUrl(url), 'room1')
    assert.equal(
        layer.writeSessionToUrl(url, 'room2'),
        'https://app.test/play?seance=room2&theme=dark#frag',
    )
})

test('share URL generation preserves durable params, strips volatile params, and keeps hash', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('share7')
    await tick()
    await finishHandshake(promise, sockets[0])
    assert.equal(layer.getShareUrl(), 'https://app.test/play?seance=share7#frag')
})

test('URL helpers support custom session and volatile param names', () => {
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play?draft=1&room=old&keep=1',
        WebSocket: FakeWebSocket,
        urlParam: 'room',
        stripUrlParams: ['draft'],
    })
    assert.equal(layer.readSessionFromUrl('https://app.test/play?room=abc123'), 'abc123')
    assert.equal(
        layer.writeSessionToUrl('https://app.test/play?draft=1&keep=1', 'room2'),
        'https://app.test/play?keep=1&room=room2',
    )
})

test('hello frame always declares the default dialects list', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    sockets[0].open()
    assert.deepEqual(sockets[0].sent[0], { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'] })
    sockets[0].close()
    await assert.rejects(promise)
})

test('hello frame declares a custom dialect as its own singleton dialects list', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        WebSocket: FakeWebSocket,
        dialect: 'layers',
    })
    const promise = layer.joinSession('room')
    await tick()
    FakeWebSocket.instances[0].open()
    assert.deepEqual(FakeWebSocket.instances[0].sent[0], { type: 'hello', protocol: 1, dialects: ['layers'] })
    FakeWebSocket.instances[0].close()
    await assert.rejects(promise)
})

test('hello frame respects an explicit dialects list independent of dialect', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        WebSocket: FakeWebSocket,
        dialect: 'layers',
        dialects: ['layers', 'noisemaker-dsl'],
    })
    const promise = layer.joinSession('room')
    await tick()
    FakeWebSocket.instances[0].open()
    assert.deepEqual(FakeWebSocket.instances[0].sent[0], {
        type: 'hello',
        protocol: 1,
        dialects: ['layers', 'noisemaker-dsl'],
    })
    FakeWebSocket.instances[0].close()
    await assert.rejects(promise)
})

test('takeOnline is the local text seeding path', async () => {
    const { layer, fetchCalls, sockets } = harness()
    const editor = new FakeEditor('seed()')
    layer.bindEditor({ docId: 'main', editor })

    const promise = layer.takeOnline([{ id: 'main', title: 'Program', kind: 'dsl', text: 'seed()', default: true }])
    await tick()
    assert.equal(fetchCalls.length, 1)
    assert.deepEqual(fetchCalls[0].body.snapshot.docs[0], {
        id: 'main',
        title: 'Program',
        kind: 'dsl',
        text: 'seed()',
        default: true,
    })
    await finishHandshake(
        promise,
        sockets[0],
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'seed()', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token' },
    )
    assert.equal(layer.getSessionId(), 'abc123')
    assert.equal(layer.getStatus(), 'online')
})

test('takeOnline can seed from bound editor text when seed docs are omitted', async () => {
    const { layer, fetchCalls, sockets } = harness()
    layer.bindEditor({ docId: 'main', editor: new FakeEditor('implicit()') })

    const promise = layer.takeOnline()
    await tick()
    assert.equal(fetchCalls[0].body.snapshot.docs[0].text, 'implicit()')
    await finishHandshake(
        promise,
        sockets[0],
        [{ id: 'main', title: 'main', kind: 'dsl', rev: 0, text: 'implicit()', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token' },
    )
})

test('takeOnline posts the exact snapshot and dialect for a legacy array seed', async () => {
    const { layer, fetchCalls, sockets } = harness()
    const promise = layer.takeOnline([{ id: 'main', title: 'Program', kind: 'dsl', text: 'seed()', default: true }])
    await tick()
    assert.deepEqual(fetchCalls[0].body, {
        snapshot: { docs: [{ id: 'main', title: 'Program', kind: 'dsl', text: 'seed()', default: true }] },
        dialect: 'noisemaker-dsl',
    })
    await finishHandshake(
        promise,
        sockets[0],
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'seed()', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token' },
    )
})

test('takeOnline normalizes an object seed of poly nodes, defaults programText, and omits absent docs', async () => {
    FakeWebSocket.instances = []
    const fetchCalls = []
    const fetch = async (url, init) => {
        fetchCalls.push({ url, init, body: JSON.parse(init.body) })
        return { ok: true, status: 201, json: async () => ({ session_id: 'layers1', anon_token: 'anon-token' }) }
    }
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch,
        WebSocket: FakeWebSocket,
        dialect: 'layers',
    })

    const promise = layer.takeOnline({
        poly: {
            nodes: [
                { id: 'meta', kind: 'layers-meta', text: '{"v":1}' },
                { id: 'meta.C1', kind: 'layers-child', text: '{}', parentId: 'meta' },
            ],
        },
    })
    await tick()
    assert.deepEqual(fetchCalls[0].body, {
        snapshot: {
            poly: {
                programText: '',
                nodes: [
                    { id: 'meta', kind: 'layers-meta', text: '{"v":1}', parentId: null },
                    { id: 'meta.C1', kind: 'layers-child', text: '{}', parentId: 'meta' },
                ],
            },
        },
        dialect: 'layers',
    })
    await finishHandshake(
        promise,
        FakeWebSocket.instances[0],
        [],
        { type: 'hello', protocol: 1, dialects: ['layers'], anon_token: 'anon-token' },
        { rev: 0, programText: '', frame: null, nodes: [] },
    )
})

test('takeOnline normalizes an object seed carrying both docs and poly', async () => {
    FakeWebSocket.instances = []
    const fetchCalls = []
    const fetch = async (url, init) => {
        fetchCalls.push({ url, init, body: JSON.parse(init.body) })
        return { ok: true, status: 201, json: async () => ({ session_id: 'mixed1', anon_token: 'anon-token' }) }
    }
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch,
        WebSocket: FakeWebSocket,
    })

    const promise = layer.takeOnline({
        docs: [{ id: 'main', title: 'Program', kind: 'dsl', text: 'seed()', default: true }],
        poly: { programText: 'frame()', nodes: [] },
    })
    await tick()
    assert.deepEqual(fetchCalls[0].body, {
        snapshot: {
            docs: [{ id: 'main', title: 'Program', kind: 'dsl', text: 'seed()', default: true }],
            poly: { programText: 'frame()', nodes: [] },
        },
        dialect: 'noisemaker-dsl',
    })
    await finishHandshake(
        promise,
        FakeWebSocket.instances[0],
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'seed()', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token' },
        { rev: 0, programText: 'frame()', frame: null, nodes: [] },
    )
})

test('joinSession only adopts server state and never posts local text', async () => {
    const { layer, fetchCalls, sockets } = harness()
    const editor = new FakeEditor('local()')
    layer.bindEditor({ docId: 'main', editor })

    const promise = layer.joinSession('join42')
    await tick()
    assert.equal(fetchCalls.length, 0)
    await finishHandshake(promise, sockets[0], [{ id: 'main', title: 'Program', kind: 'dsl', rev: 4, text: 'server()', default: true }])
    assert.equal(editor.value, 'server()')
    assert.equal(sockets[0].sent.filter((msg) => msg.type === 'doc-create' || msg.type === 'doc-reset').length, 0)
})

test('bindEditor ignores bubbled native input events from nested editor controls', async () => {
    const { layer } = harness()
    const listeners = new Map()
    const editor = {
        value: 'abc',
        collabApiVersion: 1,
        addEventListener(type, handler) {
            listeners.set(type, handler)
        },
        removeEventListener() {},
    }
    layer.bindEditor({ docId: 'main', editor })
    editor.value = 'abcd'

    listeners.get('input')({ target: { value: 'abcd' }, detail: { source: 'native-textarea' } })
    assert.equal(layer.docs.get('main').text, 'abc')

    listeners.get('input')({ target: editor, detail: { source: 'code-editor' } })
    assert.equal(layer.docs.get('main').text, 'abcd')
})

test('failed joins reject instead of hanging forever', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('missing')
    await tick()
    sockets[0].open()
    sockets[0].receive({
        type: 'error',
        seq: 1,
        code: 'not_found',
        detail: 'no such session',
    })
    await assert.rejects(promise, /no such session/)
    sockets[0].close()
    assert.equal(layer.getStatus(), 'offline')
})

test('a dialect_mismatch error frame rejects connect() with the wire code exposed on error.code', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('mismatched')
    await tick()
    sockets[0].open()
    sockets[0].receive({
        type: 'error',
        seq: 1,
        code: 'dialect_mismatch',
        detail: "session dialect is 'layers'",
    })
    let caught = null
    try {
        await promise
    } catch (error) {
        caught = error
    }
    assert.equal(caught?.code, 'dialect_mismatch')
})

test('welcome captures the session dialect and exposes it via getSessionDialect', async () => {
    const { layer, sockets } = harness()
    assert.equal(layer.getSessionDialect(), null)
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])
    assert.equal(layer.getSessionDialect(), null)
})

test('welcome captures a declared session dialect distinct from the default', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        WebSocket: FakeWebSocket,
        dialect: 'layers',
    })
    const promise = layer.joinSession('room')
    await tick()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: false },
        dialect: 'layers',
    })
    socket.receive({
        type: 'session-snapshot',
        seq: 2,
        docs: [],
        poly: { rev: 0, programText: '', frame: null, nodes: [] },
    })
    await promise
    assert.equal(layer.getSessionDialect(), 'layers')
})

test('default editor binding adopts the server default document id on join', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('local()')
    layer.bindEditor({ editor })

    const promise = layer.joinSession('visualize')
    await tick()
    await finishHandshake(promise, sockets[0], [
        { id: 'deck:A', title: 'Deck A', kind: 'dsl', rev: 4, text: 'deckA()', default: true },
        { id: 'deck:B', title: 'Deck B', kind: 'dsl', rev: 9, text: 'deckB()', default: false },
    ])

    assert.equal(editor.value, 'deckA()')
    assert.equal(layer.docs.has('main'), false)
    assert.equal(layer.docs.get('deck:A').binding.docId, 'deck:A')

    layer.updateLocalText('main', 'deckA(1)', { source: 'control' })
    await tick()
    const edit = sockets[0].sent.find((msg) => msg.type === 'doc-edit')
    assert.equal(edit.docId, 'deck:A')
})

test('runtime readonly moderation toggles editor state and blocks proposals', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    sockets[0].receive({
        type: 'moderation',
        seq: 3,
        action: 'readonly',
        target_user: 'u1',
        by: 'owner',
        detail: { readonly: true },
    })
    assert.equal(layer.getStatus(), 'readonly')
    assert.equal(editor.readOnly, true)

    layer.updateLocalText('main', 'abcd', { source: 'control' })
    await tick()
    assert.equal(sockets[0].sent.some((msg) => msg.type === 'doc-edit'), false)

    sockets[0].receive({
        type: 'moderation',
        seq: 4,
        action: 'readonly',
        target_user: 'u1',
        by: 'owner',
        detail: { readonly: false },
    })
    assert.equal(layer.getStatus(), 'online')
    assert.equal(editor.readOnly, false)
})

test('read-only welcome locks local editor while preserving remote rendering', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('readonly')
    await tick()
    sockets[0].open()
    sockets[0].receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: true },
    })
    sockets[0].receive({
        type: 'session-snapshot',
        seq: 2,
        docs: [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abc', default: true }],
    })
    await promise

    assert.equal(layer.getStatus(), 'readonly')
    assert.equal(editor.readOnly, true)
    sockets[0].receive({
        type: 'doc-edit',
        seq: 3,
        docId: 'main',
        rev: 1,
        edit: { start: 3, end: 3, text: '!' },
    })
    assert.equal(editor.value, 'abc!')
})

test('read-only sessions do not send document edits while still sending cursors', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('readonly')
    await tick()
    sockets[0].open()
    sockets[0].receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: true },
    })
    sockets[0].receive({
        type: 'session-snapshot',
        seq: 2,
        docs: [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abc', default: true }],
    })
    await promise

    layer.updateLocalText('main', 'abcd', { source: 'control' })
    await tick()
    assert.equal(sockets[0].sent.some((msg) => msg.type === 'doc-edit'), false)

    editor.setSelectionRange(1, 2, 'forward')
    editor.dispatchEvent(new CustomEvent('selectionchange', { detail: editor.getSelectionRange() }))
    await tick()
    assert.deepEqual(sockets[0].sent.find((msg) => msg.type === 'doc-cursor'), {
        type: 'doc-cursor',
        docId: 'main',
        range: { start: 1, end: 2 },
        direction: 'forward',
    })
})

test('goOffline disconnects, clears remote selections, and keeps visible text', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])
    editor.setRemoteSelection({ id: 'peer', start: 0, end: 1 })

    layer.goOffline()
    assert.equal(sockets[0].closed, true)
    assert.equal(editor.clearedRemoteSelections, 1)
    assert.equal(editor.value, 'abc')
})

test('goOffline restores editing after a read-only session', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('readonly')
    await tick()
    sockets[0].open()
    sockets[0].receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: true },
    })
    sockets[0].receive({
        type: 'session-snapshot',
        seq: 2,
        docs: [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abc', default: true }],
    })
    await promise

    assert.equal(editor.readOnly, true)
    layer.goOffline()
    assert.equal(editor.readOnly, false)
})

test('goOffline clears pending proposal timers before a later join adopts state', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 20,
        cursorThrottleMs: 20,
    })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, FakeWebSocket.instances[0])

    layer.updateLocalText('main', 'abcd', { source: 'control' })
    editor.setSelectionRange(1, 1, 'none')
    editor.dispatchEvent(new CustomEvent('selectionchange', { detail: editor.getSelectionRange() }))
    layer.goOffline()
    await new Promise((resolve) => setTimeout(resolve, 30))

    const second = layer.joinSession('room')
    await tick()
    await finishHandshake(
        second,
        FakeWebSocket.instances[1],
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 7, text: 'server()', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], resume: { last_seq: 2 } },
    )
    assert.equal(FakeWebSocket.instances[1].sent.some((msg) => msg.type === 'doc-edit'), false)
    assert.equal(FakeWebSocket.instances[1].sent.some((msg) => msg.type === 'doc-cursor'), false)
    assert.equal(editor.value, 'server()')
})

test('joining a new session closes the prior socket and ignores its delayed close', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: DelayedCloseFakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
    })
    layer.bindEditor({ docId: 'main', editor: new FakeEditor('first()') })

    const first = layer.joinSession('room-one')
    await tick()
    await finishHandshake(first, FakeWebSocket.instances[0], [
        { id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'first()', default: true },
    ])

    const second = layer.joinSession('room-two')
    await tick()
    assert.equal(FakeWebSocket.instances[0].closed, true)
    await finishHandshake(second, FakeWebSocket.instances[1], [
        { id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'second()', default: true },
    ], { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], resume: { last_seq: 2 } })

    FakeWebSocket.instances[0].emit('close', {})
    assert.equal(layer.getStatus(), 'online')
    assert.equal(layer.socket, FakeWebSocket.instances[1])

    FakeWebSocket.instances[0].receive({
        type: 'doc-edit',
        seq: 99,
        docId: 'main',
        rev: 99,
        edit: { start: 0, end: 0, text: 'stale-' },
    })
    assert.equal(layer.docs.get('main').text, 'second()')
})

test('socket interruption reconnects, adopts recovery snapshot, and resubmits local text', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        reconnectBaseMs: 0,
        reconnectMaxMs: 0,
    })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, FakeWebSocket.instances[0])

    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    assert.equal(FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'doc-edit').length, 1)

    FakeWebSocket.instances[0].close()
    await tick()
    await tick()
    const reconnectSocket = FakeWebSocket.instances[1]
    await finishHandshake(
        Promise.resolve(),
        reconnectSocket,
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 2, text: 'abYc', default: true }],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], resume: { last_seq: 2 } },
    )
    await tick()

    const edits = reconnectSocket.sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 1)
    assert.equal(edits[0].baseRev, 2)
    assert.deepEqual(edits[0].edit, { start: 3, end: 3, text: 'X' })
    assert.equal(editor.value, 'abYXc')
    assert.equal(layer.getStatus(), 'online')
})

test('updateLocalText diffs programmatic rewrites without echoing through editor binding', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    layer.updateLocalText('main', 'abXc', { source: 'control' })
    await tick()

    const edit = sockets[0].sent.find((msg) => msg.type === 'doc-edit')
    assert.deepEqual(edit, {
        type: 'doc-edit',
        docId: 'main',
        baseRev: 0,
        authorSeq: 1,
        edit: { start: 2, end: 2, text: 'X' },
    })
    assert.equal(editor.value, 'abc')
})

test('binding validation can reject invalid local text before it reaches the socket', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    const validationErrors = []
    layer.on('validation-error', (event) => validationErrors.push(event))
    layer.bindEditor({
        docId: 'main',
        editor,
        validateText: (text) => !text.includes('bad') || 'invalid DSL',
    })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    const edit = layer.updateLocalText('main', 'bad()', { source: 'control' })
    await tick()

    assert.equal(edit, null)
    assert.equal(sockets[0].sent.some((msg) => msg.type === 'doc-edit'), false)
    assert.equal(layer.docs.get('main').text, 'abc')
    assert.equal(validationErrors[0].reason, 'invalid DSL')
})

test('binding callbacks observe remote snapshots and accepted text', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    const remoteEvents = []
    const acceptedEvents = []
    layer.bindEditor({
        docId: 'main',
        editor,
        onRemoteText: (text, context) => remoteEvents.push({ text, context }),
        onAcceptedText: (text, context) => acceptedEvents.push({ text, context }),
    })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    layer.updateLocalText('main', 'abcd', { source: 'control' })
    await tick()
    sockets[0].receive({
        type: 'doc-ack',
        seq: 3,
        docId: 'main',
        rev: 1,
        authorSeq: 1,
        edit: { start: 3, end: 3, text: 'd' },
    })

    assert.equal(remoteEvents[0].text, 'abc')
    assert.equal(remoteEvents[0].context.source, 'snapshot')
    assert.equal(acceptedEvents[0].text, 'abcd')
    assert.equal(acceptedEvents[0].context.source, 'ack')
})

test('overlapping remote edits rebase local optimistic text and keep editor visible state aligned', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    editor.value = 'aXc'
    layer.updateLocalText('main', 'aXc', { source: 'editor' })
    await tick()
    sockets[0].receive({
        type: 'doc-edit',
        seq: 3,
        docId: 'main',
        rev: 1,
        edit: { start: 1, end: 2, text: 'Y' },
    })

    assert.equal(layer.docs.get('main').text, 'aXc')
    assert.equal(editor.value, 'aXc')
})

test('canonical ack after overlapping remote insert does not resubmit resurrected text', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abcd')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(
        promise,
        sockets[0],
        [{ id: 'main', title: 'Program', kind: 'dsl', rev: 0, text: 'abcd', default: true }],
    )

    layer.updateLocalText('main', 'aXd', { source: 'editor' })
    await tick()
    const firstEdit = sockets[0].sent.find((msg) => msg.type === 'doc-edit')
    assert.deepEqual(firstEdit.edit, { start: 1, end: 3, text: 'X' })

    sockets[0].receive({
        type: 'doc-edit',
        seq: 3,
        docId: 'main',
        rev: 1,
        edit: { start: 3, end: 3, text: 'Y' },
    })
    assert.equal(layer.docs.get('main').text, 'aXd')

    sockets[0].receive({
        type: 'doc-ack',
        seq: 4,
        docId: 'main',
        rev: 2,
        authorSeq: 1,
        edit: { start: 1, end: 4, text: 'X' },
    })
    await tick()

    const edits = sockets[0].sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 1)
    assert.equal(layer.docs.get('main').serverText, 'aXd')
})

test('doc rejects with snapshots rebase and resubmit local optimistic edits', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    editor.value = 'abXc'
    layer.updateLocalText('main', 'abXc', { source: 'editor' })
    await tick()
    const firstEdit = sockets[0].sent.find((msg) => msg.type === 'doc-edit')
    assert.equal(firstEdit.baseRev, 0)

    sockets[0].receive({
        type: 'doc-reject',
        seq: 3,
        docId: 'main',
        authorSeq: 1,
        reason: 'stale-revision',
        snapshot: { id: 'main', rev: 2, text: 'abYc' },
    })
    await tick()

    const edits = sockets[0].sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 2)
    assert.equal(edits[1].baseRev, 2)
    assert.deepEqual(edits[1].edit, { start: 3, end: 3, text: 'X' })
    assert.equal(layer.docs.get('main').text, 'abYXc')
    assert.equal(editor.value, 'abYXc')
})

test('proposal coalescing keeps drag-frequency rewrites below the proposal lane burst', async () => {
    const { layer, sockets } = harness()
    layer.bindEditor({ docId: 'main', editor: new FakeEditor('abc') })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    layer.updateLocalText('main', 'value-0', { source: 'drag' })
    await tick()
    let edits = sockets[0].sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 1)

    for (let index = 1; index < 30; index += 1) {
        layer.updateLocalText('main', `value-${index}`, { source: 'drag' })
    }
    await tick()
    edits = sockets[0].sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 1)

    sockets[0].receive({ type: 'doc-ack', seq: 3, docId: 'main', rev: 1, authorSeq: 1, edit: edits[0].edit })
    await tick()
    edits = sockets[0].sent.filter((msg) => msg.type === 'doc-edit')
    assert.equal(edits.length, 2)
    assert.ok(edits.length < 20)

    sockets[0].receive({ type: 'doc-ack', seq: 4, docId: 'main', rev: 2, authorSeq: 2, edit: edits[1].edit })
    assert.equal(layer.docs.get('main').serverText, 'value-29')
})

test('remote edits render through stale Handfish fallback when collaboration APIs are absent', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    editor.applyTextEdit = undefined
    editor.collabApiVersion = 0
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    sockets[0].receive({
        type: 'doc-edit',
        seq: 3,
        docId: 'main',
        rev: 1,
        authorSeq: 1,
        edit: { start: 1, end: 2, text: 'X' },
    })
    assert.equal(editor.value, 'aXc')
})

test('cursor throttling and peer cursor rendering use Handfish remote selections', async () => {
    const { layer, sockets } = harness()
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0])

    editor.setSelectionRange(1, 2, 'forward')
    editor.dispatchEvent(new CustomEvent('selectionchange', { detail: editor.getSelectionRange() }))
    await tick()
    assert.deepEqual(sockets[0].sent.find((msg) => msg.type === 'doc-cursor'), {
        type: 'doc-cursor',
        docId: 'main',
        range: { start: 1, end: 2 },
        direction: 'forward',
    })

    sockets[0].receive({
        type: 'doc-cursor',
        seq: 3,
        docId: 'main',
        user: 'peer-user',
        username: 'Bea',
        connectionId: 'peer-conn',
        range: { start: 0, end: 1 },
        direction: 'forward',
    })
    assert.equal(editor.remoteSelections.length, 1)
    assert.equal(editor.remoteSelections[0].id, 'peer-conn')
    assert.equal(editor.remoteSelections[0].label, 'Bea')
})

test('session-snapshot adopts poly nodes and emits node-snapshot', async () => {
    const { layer, sockets } = harness()
    const nodeSnapshots = []
    layer.on('node-snapshot', (event) => nodeSnapshots.push(event))
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, {
        rev: 3,
        programText: '',
        frame: null,
        nodes: [
            { id: 'meta', kind: 'layers-meta', text: '{}', version: 3, parentId: null },
            { id: 'meta.C1', kind: 'layers-child', text: '{}', version: 2, parentId: 'meta' },
        ],
    })
    assert.equal(layer.getNodeRev(), 3)
    assert.deepEqual(
        layer.getNodes().sort((a, b) => a.id.localeCompare(b.id)),
        [
            { id: 'meta', kind: 'layers-meta', text: '{}', version: 3, parentId: null },
            { id: 'meta.C1', kind: 'layers-child', text: '{}', version: 2, parentId: 'meta' },
        ],
    )
    assert.equal(nodeSnapshots.length, 1)
    assert.equal(nodeSnapshots[0].rev, 3)
    assert.equal(nodeSnapshots[0].nodes.length, 2)
})

test('upsertNode sends a poly-token-upsert wire frame and the ack applies the returned version', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, { rev: 0, programText: '', frame: null, nodes: [] })

    layer.upsertNode('meta', { kind: 'layers-meta', text: '{"v":1}' })
    await tick()
    const sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 1)
    assert.deepEqual(sent[0], {
        type: 'poly-token-upsert',
        base_rev: 0,
        id: 'meta',
        kind: 'layers-meta',
        text: '{"v":1}',
        parentId: null,
        author_seq: 1,
    })

    sockets[0].receive({
        type: 'poly-ack',
        seq: 3,
        rev: 1,
        applied: [{ id: 'meta', version: 1 }],
        author_seq: 1,
    })
    assert.deepEqual(layer.getNodes(), [
        { id: 'meta', kind: 'layers-meta', text: '{"v":1}', parentId: null, version: 1 },
    ])
    assert.equal(layer.getNodeRev(), 1)
})

test('deleteNode sends a poly-token-delete wire frame and the ack removes cascaded ids locally', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, {
        rev: 2,
        programText: '',
        frame: null,
        nodes: [
            { id: 'L1', kind: 'layers-layer', text: '{}', version: 2, parentId: null },
            { id: 'L1.C1', kind: 'layers-child', text: '{}', version: 1, parentId: 'L1' },
        ],
    })

    layer.deleteNode('L1')
    await tick()
    const sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-delete')
    assert.equal(sent.length, 1)
    assert.deepEqual(sent[0], { type: 'poly-token-delete', base_rev: 2, id: 'L1', author_seq: 1 })

    sockets[0].receive({
        type: 'poly-ack',
        seq: 3,
        rev: 3,
        applied: [{ id: 'L1', version: 3 }, { id: 'L1.C1', version: 3 }],
        author_seq: 1,
    })
    assert.deepEqual(layer.getNodes(), [])
    assert.equal(layer.getNodeRev(), 3)
})

test('a relayed upsert refreshes the base_rev used by a subsequent local upsert on the same node', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, {
        rev: 5,
        programText: '',
        frame: null,
        nodes: [{ id: 'meta', kind: 'layers-meta', text: '{}', version: 5, parentId: null }],
    })

    const remoteEvents = []
    layer.on('remote-node', (event) => remoteEvents.push(event))

    sockets[0].receive({
        type: 'poly-token-upsert',
        seq: 3,
        rev: 6,
        version: 6,
        base_rev: 5,
        id: 'meta',
        kind: 'layers-meta',
        text: '{"v":2}',
        parentId: null,
    })
    assert.equal(remoteEvents.length, 1)
    assert.equal(remoteEvents[0].op, 'upsert')
    assert.equal(remoteEvents[0].node.version, 6)
    assert.equal(layer.getNodes().find((node) => node.id === 'meta').version, 6)
    assert.equal(layer.getNodeRev(), 6)

    layer.upsertNode('meta', { kind: 'layers-meta', text: '{"v":3}' })
    await tick()
    const sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert' && msg.text === '{"v":3}')
    assert.equal(sent.length, 1)
    assert.equal(sent[0].base_rev, 6)
})

test('a stale reject refreshes base_rev and resubmits up to the retry limit, then emits node-reject', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, { rev: 0, programText: '', frame: null, nodes: [] })

    const rejects = []
    layer.on('node-reject', (event) => rejects.push(event))

    layer.upsertNode('meta', { kind: 'layers-meta', text: 'v1' })
    await tick()
    let sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 1)
    assert.equal(sent[0].author_seq, 1)
    assert.equal(sent[0].base_rev, 0)

    sockets[0].receive({ type: 'poly-reject', seq: 3, reason: 'stale', id: 'meta', rev: 4, author_seq: 1 })
    await tick()
    sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 2)
    assert.equal(sent[1].author_seq, 2)
    assert.equal(sent[1].base_rev, 4)
    assert.equal(rejects.length, 0)

    sockets[0].receive({ type: 'poly-reject', seq: 4, reason: 'stale', id: 'meta', rev: 7, author_seq: 2 })
    await tick()
    sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 3)
    assert.equal(sent[2].author_seq, 3)
    assert.equal(sent[2].base_rev, 7)
    assert.equal(rejects.length, 0)

    sockets[0].receive({ type: 'poly-reject', seq: 5, reason: 'stale', id: 'meta', rev: 9, author_seq: 3 })
    await tick()
    sent = sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 3)
    assert.equal(rejects.length, 1)
    assert.deepEqual(rejects[0], { id: 'meta', reason: 'stale', attempts: 3 })
})

test('orphan and limit reject reasons emit node-reject immediately without resubmitting', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, { rev: 0, programText: '', frame: null, nodes: [] })

    const rejects = []
    layer.on('node-reject', (event) => rejects.push(event))

    layer.upsertNode('orphan-child', { kind: 'layers-child', text: '{}', parentId: 'missing-parent' })
    await tick()
    assert.equal(sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert').length, 1)

    sockets[0].receive({ type: 'poly-reject', seq: 3, reason: 'orphan', id: 'orphan-child', rev: 0, author_seq: 1 })
    await tick()
    assert.equal(sockets[0].sent.filter((msg) => msg.type === 'poly-token-upsert').length, 1)
    assert.deepEqual(rejects[0], { id: 'orphan-child', reason: 'orphan', attempts: 1 })

    layer.upsertNode('too-big', { kind: 'layers-strokes', text: 'x'.repeat(10) })
    await tick()
    sockets[0].receive({ type: 'poly-reject', seq: 4, reason: 'limit', id: 'too-big', rev: 0, author_seq: 2 })
    await tick()
    assert.deepEqual(rejects[1], { id: 'too-big', reason: 'limit', attempts: 1 })
})

test('a relayed delete removes the node and dotted-id descendants, emitting remote-node', async () => {
    const { layer, sockets } = harness()
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, sockets[0], [], undefined, {
        rev: 4,
        programText: '',
        frame: null,
        nodes: [
            { id: 'L1', kind: 'layers-layer', text: '{}', version: 4, parentId: null },
            { id: 'L1.C1', kind: 'layers-child', text: '{}', version: 3, parentId: 'L1' },
            { id: 'L1.S0', kind: 'layers-strokes', text: '{}', version: 2, parentId: 'L1' },
            { id: 'L2', kind: 'layers-layer', text: '{}', version: 1, parentId: null },
        ],
    })

    const remoteEvents = []
    layer.on('remote-node', (event) => remoteEvents.push(event))

    sockets[0].receive({
        type: 'poly-token-delete',
        seq: 3,
        rev: 5,
        base_rev: 4,
        id: 'L1',
    })

    assert.deepEqual(layer.getNodes().map((node) => node.id).sort(), ['L2'])
    assert.equal(layer.getNodeRev(), 5)
    assert.equal(remoteEvents.length, 1)
    assert.equal(remoteEvents[0].op, 'delete')
    assert.equal(remoteEvents[0].id, 'L1')
    assert.deepEqual(remoteEvents[0].removed.sort(), ['L1', 'L1.C1', 'L1.S0'])
})

test('rapid upserts are paced with the configured minimum spacing between sends', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 30,
    })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(
        promise,
        FakeWebSocket.instances[0],
        [],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'] },
        { rev: 0, programText: '', frame: null, nodes: [] },
    )

    layer.upsertNode('a', { kind: 'layers-meta', text: '1' })
    layer.upsertNode('b', { kind: 'layers-meta', text: '2' })
    await tick()
    let sent = FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 1)
    assert.equal(sent[0].id, 'a')

    await new Promise((resolve) => setTimeout(resolve, 40))
    sent = FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sent.length, 2)
    assert.equal(sent[1].id, 'b')
})

test('upsertNode and deleteNode drop while read-only and emit readonly-write', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 0,
    })
    const promise = layer.joinSession('readonly')
    await tick()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({
        type: 'welcome',
        seq: 1,
        you: { user_id: 'u1', username: 'Ada', readonly: true },
    })
    socket.receive({
        type: 'session-snapshot',
        seq: 2,
        docs: [],
        poly: { rev: 0, programText: '', frame: null, nodes: [] },
    })
    await promise

    const readonlyWrites = []
    layer.on('readonly-write', (event) => readonlyWrites.push(event))

    layer.upsertNode('meta', { kind: 'layers-meta', text: '{}' })
    layer.deleteNode('meta')
    await tick()

    assert.equal(socket.sent.some((msg) => msg.type === 'poly-token-upsert' || msg.type === 'poly-token-delete'), false)
    assert.equal(readonlyWrites.length, 2)
    assert.deepEqual(readonlyWrites[0], { op: 'upsert', node: { id: 'meta', kind: 'layers-meta', text: '{}', parentId: null } })
    assert.deepEqual(readonlyWrites[1], { op: 'delete', id: 'meta' })
})

test('goOffline clears the node queue so pending upserts are not resent after rejoining', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 0,
    })
    const promise = layer.joinSession('room')
    await tick()
    await finishHandshake(promise, FakeWebSocket.instances[0], [], undefined, { rev: 0, programText: '', frame: null, nodes: [] })

    layer.upsertNode('meta', { kind: 'layers-meta', text: 'v1' })
    await tick()
    assert.equal(FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'poly-token-upsert').length, 1)

    layer.upsertNode('meta', { kind: 'layers-meta', text: 'v2' })
    layer.goOffline()

    const second = layer.joinSession('room')
    await tick()
    await finishHandshake(
        second,
        FakeWebSocket.instances[1],
        [],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], resume: { last_seq: 2 } },
        { rev: 0, programText: '', frame: null, nodes: [] },
    )
    assert.equal(FakeWebSocket.instances[1].sent.some((msg) => msg.type === 'poly-token-upsert'), false)
})

test('socket interruption reconnects, adopts recovery snapshot, and resubmits a pending node write', async () => {
    FakeWebSocket.instances = []
    const layer = createOnlineDslLayer({
        seanceUrl: 'https://seance.test',
        publicAppUrl: 'https://app.test/play',
        fetch: async () => {
            throw new Error('unexpected fetch')
        },
        WebSocket: FakeWebSocket,
        proposalThrottleMs: 0,
        cursorThrottleMs: 0,
        nodeThrottleMs: 0,
        reconnectBaseMs: 0,
        reconnectMaxMs: 0,
    })
    const first = layer.joinSession('room')
    await tick()
    await finishHandshake(first, FakeWebSocket.instances[0], [], undefined, {
        rev: 5,
        programText: '',
        frame: null,
        nodes: [{ id: 'meta', kind: 'layers-meta', text: '{"v":1}', version: 5, parentId: null }],
    })

    layer.upsertNode('meta', { kind: 'layers-meta', text: '{"v":2}' })
    await tick()
    const sentBeforeDrop = FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(sentBeforeDrop.length, 1)
    assert.deepEqual(sentBeforeDrop[0], {
        type: 'poly-token-upsert',
        base_rev: 5,
        id: 'meta',
        kind: 'layers-meta',
        text: '{"v":2}',
        parentId: null,
        author_seq: 1,
    })
    assert.equal(layer._nodePending.size, 1)
    assert.equal(layer._nodeQueue.length, 0)

    FakeWebSocket.instances[0].close()
    await tick()
    await tick()
    const reconnectSocket = FakeWebSocket.instances[1]
    await finishHandshake(
        Promise.resolve(),
        reconnectSocket,
        [],
        { type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], resume: { last_seq: 2 } },
        {
            rev: 9,
            programText: '',
            frame: null,
            nodes: [{ id: 'meta', kind: 'layers-meta', text: '{"v":1}', version: 9, parentId: null }],
        },
    )
    await tick()

    const resent = reconnectSocket.sent.filter((msg) => msg.type === 'poly-token-upsert')
    assert.equal(resent.length, 1)
    assert.deepEqual(resent[0], {
        type: 'poly-token-upsert',
        base_rev: 9,
        id: 'meta',
        kind: 'layers-meta',
        text: '{"v":2}',
        parentId: null,
        author_seq: 2,
    })
    // The pre-reconnect send never got a reply on the closed socket, and recovery
    // must not leave that original entry sitting in _nodePending forever: after
    // recovery there is exactly one pending entry (the fresh resend), not two.
    assert.equal(layer._nodePending.size, 1)
    assert.equal(layer._nodeQueue.length, 0)
    assert.equal(FakeWebSocket.instances[0].sent.filter((msg) => msg.type === 'poly-token-upsert').length, 1)
    assert.equal(layer.getStatus(), 'online')
})
