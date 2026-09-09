import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import * as entry from '../../sdk/index.js'
import * as bundle from '../../dist/index.js'

test('dist bundle exports exactly the public browser SDK surface', () => {
    assert.deepEqual(Object.keys(bundle).sort(), Object.keys(entry).sort())
    for (const name of Object.keys(entry)) assert.equal(typeof bundle[name], typeof entry[name], name)
})

test('dist bundle is self-contained', () => {
    const source = readFileSync(new URL('../../dist/index.js', import.meta.url), 'utf8')
    assert.doesNotMatch(source, /^[ \t]*import[\s{'"*]/m, 'no import statements')
    assert.doesNotMatch(source, /^[ \t]*export\s+(?:\{[^}]*\}|\*)\s+from/m, 'no re-exports')
    assert.doesNotMatch(source, /\brequire\(|\bprocess\.env\b/, 'no Node-only globals')
})

test('dist bundle builds a working layer', async () => {
    const layer = bundle.createOnlineDslLayer({ seanceUrl: 'https://seance.test', WebSocket: class {}, fetch: async () => ({ ok: false, status: 500 }) })
    assert.equal(layer.getStatus(), 'offline')
    assert.equal(bundle.diffText('abc', 'abXc').start, 2)
    assert.equal(bundle.peerColor('u1'), entry.peerColor('u1'))
})
