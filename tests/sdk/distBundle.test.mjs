import assert from 'node:assert/strict'
import test from 'node:test'

import {
    createOnlineDslLayer,
    diffText,
    peerColor,
    transformEdit,
} from '../../dist/index.js'

test('dist bundle exports the public browser SDK surface', () => {
    assert.equal(typeof createOnlineDslLayer, 'function')
    assert.equal(typeof diffText, 'function')
    assert.equal(typeof transformEdit, 'function')
    assert.equal(typeof peerColor, 'function')
})
