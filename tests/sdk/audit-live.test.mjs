// Audit: the real SDK against a local Seance server (run-server.sh on 127.0.0.1:8765). Never production.
import assert from 'node:assert/strict'
import test from 'node:test'

import { createOnlineDslLayer } from '../../sdk/index.js'
import { FakeEditor, sleep } from './audit-harness.mjs'

const URL_ = process.env.SEANCE_LIVE_URL || 'http://127.0.0.1:8765'
const skip = process.env.SEANCE_LIVE_URL ? false : 'set SEANCE_LIVE_URL=http://127.0.0.1:8765 (see run-server.sh) to run the live tests'
const ORIGIN = 'http://localhost:5173'
const SNAPS = []

class OriginWebSocket extends WebSocket {
    constructor(url) {
        super(url, { headers: { Origin: ORIGIN } })
        this.sentFrames = []
        this.closeInfo = null
        this.addEventListener('close', (e) => { this.closeInfo = { code: e.code, reason: e.reason } })
    }
    send(data) { this.sentFrames.push(JSON.parse(data)); super.send(data) }
}
const originFetch = (url, init = {}) => fetch(url, { ...init, headers: { ...(init.headers || {}), Origin: ORIGIN } })

function mk(extra = {}) {
    const sockets = []
    class Tracking extends (extra.Socket || OriginWebSocket) {
        constructor(url) { super(url); sockets.push(this) }
    }
    const layer = createOnlineDslLayer({
        seanceUrl: URL_, fetch: originFetch, WebSocket: Tracking, reconnectBaseMs: 50, reconnectMaxMs: 200, ...extra,
    })
    const events = []
    for (const name of ['status', 'error', 'offline', 'moderation', 'doc-reject', 'doc-ack', 'readonly-write', 'disconnect']) {
        layer.on(name, (p) => events.push({ name, p }))
    }
    return { layer, sockets, events }
}

// Read the server's truth through a throwaway raw socket using a known identity token.
async function serverDocs(sessionId, anonToken) {
    return new Promise((resolve, reject) => {
        const ws = new WebSocket(`${URL_.replace(/^http/, 'ws')}/v1/sessions/${sessionId}/ws`, { headers: { Origin: ORIGIN } })
        const timer = setTimeout(() => reject(new Error('snapshot timeout')), 5000)
        ws.addEventListener('open', () => ws.send(JSON.stringify({ type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: anonToken })))
        ws.addEventListener('message', (e) => {
            const m = JSON.parse(e.data)
            if (m.type === 'session-snapshot') { clearTimeout(timer); ws.close(); resolve(m) }
            if (m.type === 'error') { clearTimeout(timer); ws.close(); reject(new Error(`${m.code}: ${m.detail}`)) }
        })
    })
}

const DOC = (text, id = 'main', dflt = true) => ({ id, title: 'P', kind: 'noisemaker-dsl', text, default: dflt })
let SHARED_TOKEN = null   // one anon identity reused across tests (anon mints are capped per IP)

test('live-0 server is up', { skip }, async () => {
    const r = await fetch(`${URL_}/up`)
    assert.equal(r.status, 200)
    console.log('server', await r.json())
})

test('RED: live-1 SDK-14 astral character before the caret: server text diverges from both editors, edits at the end reject forever', { skip }, async () => {
    const { layer, sockets, events } = mk()
    const editor = new FakeEditor('🎉 party')
    layer.bindEditor({ docId: 'main', editor })
    await layer.takeOnline([DOC('🎉 party')])
    SHARED_TOKEN = layer.anonToken
    const id = layer.getSessionId()

    // 1. insert in the middle (after "🎉 p"): UTF-16 offset 4, code-point offset 3
    editor.value = '🎉 pXarty'
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await sleep(400)
    const server1 = (await serverDocs(id, SHARED_TOKEN)).docs[0].text
    console.log('live-1 after mid insert: editor', JSON.stringify(editor.value), 'sdk.serverText', JSON.stringify(layer.docs.get('main').serverText), 'SERVER', JSON.stringify(server1))

    // 2. append at the end: UTF-16 end offset 9 > code-point length 8: the server rejects, the SDK resubmits
    const sentBefore = sockets[0].sentFrames.filter((f) => f.type === 'doc-edit').length
    editor.value = '🎉 pXarty!'
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await sleep(2000)
    const sentAfter = sockets[0].sentFrames.filter((f) => f.type === 'doc-edit').length
    const rejects = events.filter((e) => e.name === 'doc-reject').map((e) => e.p.reason)
    const server2 = (await serverDocs(id, SHARED_TOKEN)).docs[0].text
    console.log(`live-1 append: ${sentAfter - sentBefore} doc-edit frames in 2s, rejects=${JSON.stringify(rejects.slice(0, 3))}x${rejects.length}, SERVER ${JSON.stringify(server2)}, status ${layer.getStatus()}`)
    layer.goOffline()
    assert.equal(server1, editor.value.replace('!', ''), 'server text should match the editor after a mid-document insert')
    assert.ok(sentAfter - sentBefore <= 1, `SDK sent ${sentAfter - sentBefore} identical proposals in 2 seconds`)
})

test('RED: live-2 SDK-4 kicked participant rejoins automatically within a second', { skip }, async () => {
    const owner = mk({ anonToken: SHARED_TOKEN })
    await owner.layer.takeOnline([DOC('abc')])
    const id = owner.layer.getSessionId()
    const victim = mk()                                    // fresh identity (one anon mint)
    await victim.layer.joinSession(id)
    const victimUser = victim.layer.user.user_id
    assert.notEqual(victimUser, owner.layer.user.user_id)

    owner.layer._send({ type: 'mod-kick', target_user: victimUser })
    await sleep(1200)
    const closes = victim.sockets.map((s) => s.closeInfo)
    const statuses = victim.events.filter((e) => e.name === 'status').map((e) => e.p)
    console.log(`live-2 victim sockets=${victim.sockets.length} closes=${JSON.stringify(closes)} statuses=${JSON.stringify(statuses)} final=${victim.layer.getStatus()} moderationEvents=${victim.events.filter((e) => e.name === 'moderation').length}`)
    const disconnect = victim.events.find((e) => e.name === 'disconnect')?.p
    victim.layer.goOffline(); owner.layer.goOffline()
    assert.equal(closes[0]?.code, 4401)
    assert.equal(victim.sockets.length, 1, 'a kicked client must not rejoin on its own')
    assert.equal(disconnect?.kind, 'kicked')
    assert.equal(victim.layer.getStatus(), 'offline')
})

test('RED: live-3 SDK-3 two documents edited continuously exhaust the proposal lane; the dropped frame wedges one doc', { skip }, async () => {
    const { layer, events } = mk({ anonToken: SHARED_TOKEN, defaultDocId: 'deck:A' })
    const a = new FakeEditor('a'); const b = new FakeEditor('b')
    layer.bindEditor({ editor: a })                          // implicit default doc, like Visualize deck A
    layer.bindEditor({ docId: 'deck:B', editor: b })
    await layer.takeOnline([DOC('a', 'deck:A', true), DOC('b', 'deck:B', false)])
    const id = layer.getSessionId()

    const t0 = Date.now()
    while (Date.now() - t0 < 4500) {                          // a drag/randomizer rewriting both decks for 4.5s
        a.value += 'x'; b.value += 'y'
        layer.updateLocalText('deck:A', a.value, { source: 'graph' })
        layer.updateLocalText('deck:B', b.value, { source: 'graph' })
        await sleep(30)
    }
    await sleep(1500)
    const server = await serverDocs(id, SHARED_TOKEN)
    const rate = events.filter((e) => e.name === 'error' && e.p?.code === 'rate_limited').length
    const report = ['deck:A', 'deck:B'].map((d) => {
        const doc = layer.docs.get(d)
        const srv = server.docs.find((x) => x.id === d).text
        return `${d}: inFlight=${doc.inFlight ? 'authorSeq ' + doc.inFlight.authorSeq : 'none'} local=${doc.text.length} sdkServer=${doc.serverText.length} SERVER=${srv.length} converged=${doc.text === srv}`
    })
    console.log(`live-3 rate_limited errors=${rate}; ${report.join(' | ')}`)
    layer.goOffline()
    assert.equal(rate, 0, 'proposals exceeded the 10/s lane')
    for (const d of ['deck:A', 'deck:B']) assert.equal(layer.docs.get(d).inFlight, null, `${d} wedged`)
})

test('RED: live-4 SDK-3b a paste larger than max_doc_edit_text gets an error frame, never an ack; the doc lane stays wedged', { skip }, async () => {
    const { layer, sockets, events } = mk({ anonToken: SHARED_TOKEN })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    await layer.takeOnline([DOC('abc')])
    const id = layer.getSessionId()

    editor.value = 'abc' + 'x'.repeat(70000)
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await sleep(600)
    const errs = events.filter((e) => e.name === 'error').map((e) => `${e.p?.code}:${e.p?.detail}`)
    editor.value += 'Z'
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await sleep(600)
    const doc = layer.docs.get('main')
    const server = (await serverDocs(id, SHARED_TOKEN)).docs[0].text
    console.log(`live-4 errors=${JSON.stringify(errs)} sent doc-edits=${sockets[0].sentFrames.filter((f) => f.type === 'doc-edit').length} inFlight=${Boolean(doc.inFlight)} close=${JSON.stringify(sockets[0].closeInfo)} SERVER len=${server.length} local len=${doc.text.length} status=${layer.getStatus()}`)
    const wedged = doc.inFlight
    layer.goOffline()
    assert.equal(wedged, null, 'doc lane wedged after the oversize paste')
    assert.equal(server.length, doc.text.length, 'server never received the paste or the following keystroke')
})

test('RED: live-5 SDK-1 end to end: ack lost on a dropped socket, reconnect snapshot duplicates the keystroke on the server', { skip }, async () => {
    class Blackhole extends OriginWebSocket {
        static tripped = false
        addEventListener(type, handler) {
            if (type !== 'message') return super.addEventListener(type, handler)
            return super.addEventListener(type, (e) => { if (!this.blackhole) handler(e) })
        }
        send(data) {
            super.send(data)
            if (!Blackhole.tripped && JSON.parse(data).type === 'doc-edit') {
                Blackhole.tripped = true
                this.blackhole = true                       // the ack is lost
                setTimeout(() => this.close(), 200)         // then the connection drops
            }
        }
    }
    const { layer, sockets } = mk({ anonToken: SHARED_TOKEN, Socket: Blackhole })
    const editor = new FakeEditor('abc')
    layer.bindEditor({ docId: 'main', editor })
    await layer.takeOnline([DOC('abc')])
    const id = layer.getSessionId()
    editor.value = 'abXc'
    layer.updateLocalText('main', editor.value, { source: 'editor' })
    await sleep(1500)
    const server = (await serverDocs(id, SHARED_TOKEN)).docs[0].text
    console.log(`live-5 sockets=${sockets.length} status=${layer.getStatus()} editor=${JSON.stringify(editor.value)} SERVER=${JSON.stringify(server)}`)
    layer.goOffline()
    assert.equal(server, 'abXc')
    assert.equal(editor.value, 'abXc')
})

test('live-6 persisted identity preserves creator ownership across a new layer and another create', { skip }, async (t) => {
    const values = new Map()
    const anonTokenStorage = {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    }
    const creator = mk({ anonTokenStorage })
    const guest = mk({ anonTokenStorage: null })
    const reloaded = mk({ anonTokenStorage: null })
    t.after(() => { creator.layer.goOffline(); guest.layer.goOffline(); reloaded.layer.goOffline() })
    await creator.layer.takeOnline([DOC('abc')])
    const id = creator.layer.getSessionId()
    const userId = creator.layer.user.user_id
    await guest.layer.joinSession(id)
    creator.layer.goOffline()
    // A fresh layer reads the same tab's storage while another user holds the room open.
    const restored = mk({ anonTokenStorage })
    t.after(() => restored.layer.goOffline())
    await restored.layer.joinSession(id)
    assert.equal(restored.layer.user.user_id, userId)
    assert.equal(restored.layer.user.is_owner, true)
    restored.layer.goOffline()
    await restored.layer.takeOnline([DOC('second room')])
    assert.equal(restored.layer.user.user_id, userId)
    assert.equal(restored.layer.user.is_owner, true)
    // An explicit opt-out still makes a distinct anonymous visitor.
    await reloaded.layer.joinSession(restored.layer.getSessionId())
    assert.notEqual(reloaded.layer.user.user_id, userId)
    assert.equal(reloaded.layer.user.is_owner, false)
})

test('live identity rotation replaces an invalid persisted token before later joins', { skip }, async (t) => {
    const owner = mk()
    t.after(() => owner.layer.goOffline())
    await owner.layer.takeOnline([DOC('abc')])
    const values = new Map()
    const anonTokenStorage = {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    }
    const expired = mk({ anonTokenStorage, anonToken: 'invalid-expired-token' })
    t.after(() => expired.layer.goOffline())
    await expired.layer.joinSession(owner.layer.getSessionId())
    const userId = expired.layer.user.user_id
    assert.notEqual(expired.layer.anonToken, 'invalid-expired-token')
    assert.equal([...values.values()][0], expired.layer.anonToken)
    const next = mk({ anonTokenStorage })
    t.after(() => next.layer.goOffline())
    await next.layer.joinSession(owner.layer.getSessionId())
    assert.equal(next.layer.user.user_id, userId)
})

test('live a banned persisted identity remains forbidden after layer recreation', { skip }, async (t) => {
    const owner = mk()
    const values = new Map()
    const anonTokenStorage = {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value),
    }
    const visitor = mk({ anonTokenStorage })
    t.after(() => { owner.layer.goOffline(); visitor.layer.goOffline() })
    await owner.layer.takeOnline([DOC('abc')])
    const id = owner.layer.getSessionId()
    await visitor.layer.joinSession(id)
    const token = visitor.layer.anonToken
    owner.layer._send({ type: 'mod-ban', target_user: visitor.layer.user.user_id })
    await waitFor(() => visitor.layer.getStatus() === 'offline')
    assert.ok(visitor.layer.anonToken === token)
    const next = mk({ anonTokenStorage })
    t.after(() => next.layer.goOffline())
    await assert.rejects(next.layer.joinSession(id),
        (error) => error.code === 'forbidden' && error.frame?.detail === 'banned')
    await waitFor(() => next.sockets[0].closeInfo !== null)
    assert.equal(next.sockets[0].closeInfo.code, 4403)
    assert.ok(next.layer.anonToken === token)
    assert.ok([...values.values()][0] === token)
    await sleep(250)
    assert.equal(next.sockets.length, 1)
})

async function waitFor(condition) {
    const deadline = Date.now() + 5000
    while (!condition() && Date.now() < deadline) await sleep(10)
    assert.ok(condition(), 'local integration did not settle before its deadline')
}

test('live-7 JSON-escaped paste chunks converge below the real transport byte cap', { skip }, async (t) => {
    const { layer, sockets } = mk()
    t.after(() => layer.goOffline())
    await layer.takeOnline([DOC('abc')])
    const text = `a${'\u0000'.repeat(20000)}bc`
    layer.updateLocalText('main', text)
    await waitFor(() => layer.docs.get('main').serverText === text)
    const server = await serverDocs(layer.getSessionId(), layer.anonToken)
    assert.equal(server.docs[0].text, text)
    const frames = sockets[0].sentFrames.filter((frame) => frame.type === 'doc-edit')
    assert.ok(frames.length > 1)
    assert.ok(frames.every((frame) => Buffer.byteLength(JSON.stringify(frame)) <= 65536))
})

test('live-8 unanswered node writes recover from a real snapshot and preserve the latest local value', { skip }, async (t) => {
    class BlackholeNode extends OriginWebSocket {
        static tripped = false
        addEventListener(type, handler) {
            if (type !== 'message') return super.addEventListener(type, handler)
            return super.addEventListener(type, (event) => { if (!this.blackhole) handler(event) })
        }
        send(data) {
            super.send(data)
            if (!BlackholeNode.tripped && JSON.parse(data).type === 'poly-token-upsert') {
                BlackholeNode.tripped = true
                this.blackhole = true
            }
        }
    }
    const { layer, sockets } = mk({ Socket: BlackholeNode, inFlightTimeoutMs: 100 })
    t.after(() => layer.goOffline())
    await layer.takeOnline({ poly: { nodes: [{ id: 'layer', kind: 'layer', text: 'original' }] } })
    layer.upsertNode('layer', { kind: 'layer', text: 'first' })
    layer.upsertNode('layer', { kind: 'layer', text: 'latest' })
    await waitFor(() => sockets.length > 1 && layer.getPendingNodeWrites().length === 0 && layer.getNodes()[0]?.text === 'latest')
    const server = await serverDocs(layer.getSessionId(), layer.anonToken)
    assert.equal(server.poly.nodes[0].text, 'latest')
})

test('live-9 changing rooms cannot publish queued node content into the target session', { skip }, async (t) => {
    const source = mk({ nodeThrottleMs: 200 })
    const target = mk()
    t.after(() => { source.layer.goOffline(); target.layer.goOffline() })
    await source.layer.takeOnline({ poly: { nodes: [] } })
    await target.layer.takeOnline({ poly: { nodes: [] } })
    source.layer.upsertNode('private', { kind: 'layer', text: 'source room content' })
    await waitFor(() => source.layer.getNodes().some((node) => node.id === 'private'))
    source.layer.upsertNode('queued', { kind: 'layer', text: 'private queued content' })
    await source.layer.joinSession(target.layer.getSessionId())
    await sleep(300)
    const server = await serverDocs(target.layer.getSessionId(), target.layer.anonToken)
    assert.deepEqual(server.poly.nodes, [])
})

test('live-10 ambiguous reconnect holds the draft after an accepted edit was replaced by a peer', { skip }, async (t) => {
    class BlackholeText extends OriginWebSocket {
        static tripped = false
        addEventListener(type, handler) {
            if (type !== 'message') return super.addEventListener(type, handler)
            return super.addEventListener(type, (event) => { if (!this.blackhole) handler(event) })
        }
        send(data) {
            super.send(data)
            if (!BlackholeText.tripped && JSON.parse(data).type === 'doc-edit') {
                BlackholeText.tripped = true
                this.blackhole = true
            }
        }
    }
    const author = mk({ Socket: BlackholeText })
    const peer = mk()
    t.after(() => { author.layer.goOffline(); peer.layer.goOffline() })
    const editor = new FakeEditor('abc')
    author.layer.bindEditor({ docId: 'main', editor })
    await author.layer.takeOnline([DOC('abc')])
    const sessionId = author.layer.getSessionId()
    await peer.layer.joinSession(sessionId)
    editor.value = 'axbc'
    author.layer.updateLocalText('main', editor.value)
    await waitFor(() => peer.layer.docs.get('main').serverText === 'axbc')
    peer.layer.updateLocalText('main', 'azbc')
    await waitFor(() => peer.layer.docs.get('main').serverText === 'azbc')
    author.sockets[0].close()
    await waitFor(() => author.sockets.length > 1 && author.layer.getStatus() === 'online')
    const server = await serverDocs(sessionId, author.layer.anonToken)
    assert.equal(server.docs[0].text, 'azbc')
    assert.equal(editor.value, 'axbc')
    assert.equal(author.events.find(({ name, p }) => name === 'doc-reject' && p.reason === 'reconnect_ambiguous')?.p.snapshot.text, 'azbc')
    assert.equal(author.sockets[1].sentFrames.some((frame) => frame.type === 'doc-edit'), false)
})
