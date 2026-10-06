import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import * as sdk from '../../sdk/index.js'
import { harness, finishHandshake, tick } from './audit-harness.mjs'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9XkAAAAASUVORK5CYII=', 'base64')
const id = 'c21a896d2f3eba71a6310925059413201b176efcbb66f05491e7b5b86b7e3915'
const legacyAsset = { id, dataUrl: `data:image/png;base64,${png.toString('base64')}`, width: 1, height: 1, mimeType: 'image/png' }
const bytesOf = async blob => Buffer.from(await blob.arrayBuffer())
// Images are images: an asset carries its bytes as a Blob, never as a data URL.
async function assertAsset(result, bytes, mimeType, size = [1, 1]) {
    assert.deepEqual(Object.keys(result).sort(), ['blob', 'height', 'id', 'mimeType', 'width'])
    assert.ok(result.blob instanceof Blob)
    assert.equal(result.blob.type, mimeType)
    assert.deepEqual(await bytesOf(result.blob), bytes)
    assert.equal(result.mimeType, mimeType)
    assert.deepEqual([result.width, result.height], size)
}

for (const fixture of JSON.parse(readFileSync(new URL('../fixtures/images.json', import.meta.url)))) {
    test(`prepareImage preserves ${fixture.mimeType} bytes and oriented dimensions${fixture.description ? ` (${fixture.description})` : ''}`, async () => {
        const result = await sdk.prepareImage(new Blob([Buffer.from(fixture.base64, 'base64')], { type: fixture.mimeType }))
        await assertAsset(result, Buffer.from(fixture.base64, 'base64'), fixture.mimeType, [fixture.width, fixture.height])
    })
}

test('prepareImage preserves original raster bytes and derives their content ID and dimensions', async () => {
    assert.equal(typeof sdk.prepareImage, 'function')
    const result = await sdk.prepareImage(new Blob([png], { type: 'image/png' }))
    assert.equal(result.id, id)
    await assertAsset(result, png, 'image/png')
})

test('prepareImage identifies raster blobs without a MIME type', async () => {
    const result = await sdk.prepareImage(new Blob([png]))
    assert.equal(result.id, id)
    await assertAsset(result, png, 'image/png')
    for (const fixture of JSON.parse(readFileSync(new URL('../fixtures/images.json', import.meta.url)))) {
        const result = await sdk.prepareImage(new Blob([Buffer.from(fixture.base64, 'base64')]))
        await assertAsset(result, Buffer.from(fixture.base64, 'base64'), fixture.mimeType, [fixture.width, fixture.height])
    }
    await assert.rejects(sdk.prepareImage(new Blob(['video data'])), /image/i)
})

test('prepareImage refuses video, invalid raster data and excessive allocations', async () => {
    assert.equal(typeof sdk.prepareImage, 'function')
    for (const blob of [new Blob([png], { type: 'video/mp4' }), new Blob(['bad'], { type: 'image/png' }), new Blob([new Uint8Array(8 * 1024 * 1024 + 1)], { type: 'image/png' })]) {
        await assert.rejects(sdk.prepareImage(blob))
    }
    const oversized = Buffer.from(png)
    oversized.writeUInt32BE(20000, 16)
    await assert.rejects(sdk.prepareImage(new Blob([oversized], { type: 'image/png' })), /dimension/i)
})

// Seed images go as binary file parts beside the JSON seed, whatever form the app hands over.
for (const [form, seed] of [
    ['a Blob', () => new Blob([png], { type: 'image/png' })],
    ['a prepared asset', () => sdk.prepareImage(new Blob([png], { type: 'image/png' }))],
    ['a data URL record from an earlier application', () => legacyAsset],
]) {
    test(`takeOnline sends ${form} seed image as binary bytes outside the text snapshot`, async (t) => {
        const { layer, sockets, fetchCalls } = harness()
        t.after(() => layer.goOffline())
        const pending = layer.takeOnline({ docs: [], images: [await seed()] })
        await tick(); await tick()
        await finishHandshake(pending, sockets.at(-1), [], {
            type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token',
        })
        const body = fetchCalls[0].body
        assert.ok(body instanceof FormData)
        assert.equal(fetchCalls[0].init.headers['Content-Type'], undefined)
        const session = JSON.parse(await body.get('session').text())
        assert.equal(session.images, undefined)
        assert.equal(session.snapshot.images, undefined)
        const images = body.getAll('image')
        assert.equal(images.length, 1)
        assert.equal(images[0].type, 'image/png')
        assert.deepEqual(await bytesOf(images[0]), png)
        assert.doesNotMatch(JSON.stringify(session), /data:image/)
    })
}

test('takeOnline without images keeps its JSON body', async (t) => {
    const { layer, sockets, fetchCalls } = harness()
    t.after(() => layer.goOffline())
    const pending = layer.takeOnline({ docs: [] })
    await tick()
    await finishHandshake(pending, sockets.at(-1), [], {
        type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'anon-token',
    })
    assert.equal(fetchCalls[0].init.headers['Content-Type'], 'application/json')
    assert.equal(fetchCalls[0].body.images, undefined)
})

test('image transfer uses session identity and returns byte-identical blobs', async (t) => {
    const { layer, sockets } = harness({ anonToken: 'test-token' })
    t.after(() => layer.goOffline())
    const pending = layer.connect('abc123')
    await finishHandshake(pending, sockets.at(-1), [], {
        type: 'hello', protocol: 1, dialects: ['noisemaker-dsl'], anon_token: 'test-token',
    })
    const requests = []
    layer.fetch = async (url, init) => {
        requests.push({ url, init })
        return init.method === 'POST'
            ? new Response(JSON.stringify({ id }), { status: 201 })
            : new Response(png, { headers: { 'Content-Type': 'image/png' } })
    }
    assert.equal(typeof layer.uploadImage, 'function')
    assert.equal(await layer.uploadImage(new Blob([png], { type: 'image/png' })), id)
    assert.deepEqual(Buffer.from(await (await layer.getImage(id)).arrayBuffer()), png)
    assert.equal(requests[0].url, 'https://seance.test/v1/sessions/abc123/images')
    assert.equal(requests[1].url, `https://seance.test/v1/sessions/abc123/images/${id}`)
    for (const { init } of requests) {
        assert.equal(init.headers['X-Seance-Anon'], 'test-token')
        assert.equal(init.credentials, 'include')
    }
    assert.equal(requests[0].init.headers['Content-Type'], 'image/png')
    assert.ok(requests[0].init.body instanceof Blob)
    assert.deepEqual(await bytesOf(requests[0].init.body), png)
    await assert.rejects(layer.getImage('../secret'), /image/i)
    layer.fetch = async () => new Response('bad', { headers: { 'Content-Type': 'image/png' } })
    await assert.rejects(layer.getImage(id), /image/i)
})

test('a download finishing after leaving the session cannot be adopted', async (t) => {
    const { layer, sockets } = harness()
    t.after(() => layer.goOffline())
    const pending = layer.connect('abc123')
    await finishHandshake(pending, sockets.at(-1), [])
    let complete
    layer.fetch = () => new Promise(resolve => { complete = resolve })
    assert.equal(typeof layer.getImage, 'function')
    const download = layer.getImage(id)
    layer.goOffline()
    complete(new Response(png, { headers: { 'Content-Type': 'image/png' } }))
    await assert.rejects(download, /superseded/)
})
