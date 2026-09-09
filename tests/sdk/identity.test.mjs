import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, finishHandshake, tick } from './audit-harness.mjs'

function memoryStorage(initial = []) {
    const values = new Map(initial)
    return {
        values,
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    }
}

async function join(layer, sockets, token = undefined, issuedToken = undefined) {
    const pending = layer.joinSession('abc123')
    const socket = sockets.at(-1)
    socket.open()
    socket.receive({ type: 'welcome', seq: 1, you: { user_id: 'u1', readonly: false }, ...(issuedToken ? { anon_token: issuedToken } : {}) })
    socket.receive({ type: 'session-snapshot', seq: 2, docs: [] })
    await pending
    assert.deepEqual(socket.sent[0], {
        type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'],
        ...(token ? { anon_token: token } : {}),
    })
    return socket
}

async function create(layer, sockets, token = 'anon-token') {
    const pending = layer.takeOnline([])
    await tick()
    await finishHandshake(pending, sockets.at(-1), [], {
        type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: token,
    })
}

test('session creation sends the current anonymous identity header, without putting it in the URL', async (t) => {
    const { layer, sockets, fetchCalls } = harness({ anonToken: 'existing-secret' })
    t.after(() => layer.goOffline())
    await create(layer, sockets)
    assert.equal(fetchCalls[0].init.headers['X-Seance-Anon'], 'existing-secret')
    assert.equal(fetchCalls[0].url, 'https://seance.test/v1/sessions')
    assert.equal(sockets[0].url, 'wss://seance.test/v1/sessions/abc123/ws')
    assert.ok(!layer.getShareUrl().includes('secret'))
})

test('created identity survives layer recreation through injected storage', async (t) => {
    const storage = memoryStorage()
    const first = harness({ anonTokenStorage: storage })
    t.after(() => first.layer.goOffline())
    await create(first.layer, first.sockets)
    const next = harness({ anonTokenStorage: storage })
    t.after(() => next.layer.goOffline())
    await join(next.layer, next.sockets, 'anon-token')
    assert.equal(storage.values.size, 1)
})

test('welcome rotates the persisted token and explicit identity overrides stored identity', async (t) => {
    const storage = memoryStorage()
    const first = harness({ anonTokenStorage: storage, anonToken: 'explicit-token' })
    t.after(() => first.layer.goOffline())
    await join(first.layer, first.sockets, 'explicit-token', 'rotated-token')
    const next = harness({ anonTokenStorage: storage })
    t.after(() => next.layer.goOffline())
    await join(next.layer, next.sockets, 'rotated-token')
    const override = harness({ anonTokenStorage: storage, anonToken: 'override-token' })
    t.after(() => override.layer.goOffline())
    await join(override.layer, override.sockets, 'override-token')
    assert.deepEqual([...storage.values.values()], ['override-token'])
})

test('storage scopes identity to the normalized exact server URL, including path, scheme and port', () => {
    const storage = memoryStorage()
    harness({ anonTokenStorage: storage, seanceUrl: 'https://SEANCE.test:443/one/', anonToken: 'one' })
    assert.equal(harness({ anonTokenStorage: storage, seanceUrl: 'https://seance.test/one' }).layer.anonToken, 'one')
    for (const seanceUrl of ['https://seance.test/two', 'http://seance.test/one', 'https://seance.test:8443/one', 'https://elsewhere.test/one']) {
        assert.equal(harness({ anonTokenStorage: storage, seanceUrl }).layer.anonToken, null)
    }
    assert.equal([...storage.values.keys()][0], 'seance:anon-token:https://seance.test/one')
})

test('the implicit same-origin server shares identity across application paths and query changes', (t) => {
    const previous = Object.getOwnPropertyDescriptor(globalThis, 'location')
    t.after(() => { if (previous) Object.defineProperty(globalThis, 'location', previous); else delete globalThis.location })
    const location = { href: 'https://app.test/editor?code=abc' }
    Object.defineProperty(globalThis, 'location', { configurable: true, value: location })
    const storage = memoryStorage()
    harness({ seanceUrl: '', anonTokenStorage: storage, anonToken: 'same-origin-token' })
    location.href = 'https://app.test/other?seance=abc123'
    assert.equal(harness({ seanceUrl: '', anonTokenStorage: storage }).layer.anonToken, 'same-origin-token')
    assert.equal([...storage.values.keys()][0], 'seance:anon-token:https://app.test')
})

test('browser sessionStorage is the default and null/false each opt out without accessing it', async (t) => {
    const storage = memoryStorage()
    const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
    t.after(() => { if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous); else delete globalThis.sessionStorage })
    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get: () => storage })
    harness({ anonToken: 'browser-token' })
    assert.equal(harness().layer.anonToken, 'browser-token')
    let accesses = 0
    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, get: () => { accesses++; throw Error('blocked') } })
    for (const anonTokenStorage of [null, false]) {
        const { layer, sockets } = harness({ anonTokenStorage })
        t.after(() => layer.goOffline())
        await join(layer, sockets, undefined, 'transient-token')
        assert.equal(layer.anonToken, 'transient-token')
    }
    assert.equal(accesses, 0)
    assert.deepEqual([...storage.values.values()], ['browser-token'])
    const fallback = harness()
    t.after(() => fallback.layer.goOffline())
    await join(fallback.layer, fallback.sockets, undefined, 'fallback-token')
    assert.equal(fallback.layer.anonToken, 'fallback-token')
})

test('storage read/write errors preserve a usable in-memory identity', async (t) => {
    const storage = { getItem() { throw Error('read denied') }, setItem() { throw Error('quota exceeded') } }
    const { layer, sockets, fetchCalls } = harness({ anonTokenStorage: storage })
    t.after(() => layer.goOffline())
    await join(layer, sockets, undefined, 'memory-token')
    layer.goOffline()
    await create(layer, sockets)
    assert.equal(fetchCalls[0].init.headers['X-Seance-Anon'], 'memory-token')
    assert.equal(layer.anonToken, 'anon-token')
})

test('forbidden close retains stored identity and never retries with a new anonymous user', async (t) => {
    const storage = memoryStorage()
    const first = harness({ anonTokenStorage: storage, anonToken: 'banned-token' })
    t.after(() => first.layer.goOffline())
    const socket = await join(first.layer, first.sockets, 'banned-token')
    socket.closeWith(4403, 'banned')
    await tick()
    assert.equal(first.sockets.length, 1)
    assert.equal(first.fetchCalls.length, 0)
    assert.equal(first.layer.anonToken, 'banned-token')
    const next = harness({ anonTokenStorage: storage })
    t.after(() => next.layer.goOffline())
    await join(next.layer, next.sockets, 'banned-token')
})
