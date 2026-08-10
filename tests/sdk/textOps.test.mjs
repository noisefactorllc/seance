import assert from 'node:assert/strict'
import test from 'node:test'

import {
    applyTextEdit,
    diffText,
    rebaseTextWithLocalEdit,
    transformEdit,
    transformSelection,
} from '../../sdk/textOps.js'

test('diffText returns minimal inserts, deletes, replaces, pastes, replacements, and no-ops', () => {
    assert.deepEqual(diffText('abc', 'abXc'), { start: 2, end: 2, text: 'X' })
    assert.deepEqual(diffText('abXc', 'abc'), { start: 2, end: 3, text: '' })
    assert.deepEqual(diffText('abc', 'aXc'), { start: 1, end: 2, text: 'X' })
    assert.deepEqual(diffText('a\nb', 'a\npasted\nb'), { start: 2, end: 2, text: 'pasted\n' })
    assert.deepEqual(diffText('noise()', 'shape()'), { start: 0, end: 4, text: 'shap' })
    assert.deepEqual(diffText('abc', 'xyz'), { start: 0, end: 3, text: 'xyz' })
    assert.equal(diffText('same', 'same'), null)
})

test('applyTextEdit applies validated replace ranges', () => {
    assert.equal(applyTextEdit('abcd', { start: 1, end: 3, text: 'XYZ' }), 'aXYZd')
    assert.throws(
        () => applyTextEdit('abc', { start: 4, end: 4, text: 'x' }),
        /outside the text/,
    )
})

test('transformEdit rebases pending local edits over remote edits', () => {
    const pending = { start: 3, end: 3, text: '!' }
    const remote = { start: 0, end: 0, text: '>' }
    assert.deepEqual(transformEdit(pending, remote), { start: 4, end: 4, text: '!' })
    assert.equal(
        applyTextEdit(applyTextEdit('abc', remote), transformEdit(pending, remote)),
        '>abc!',
    )
    const localInsert = { start: 2, end: 2, text: 'X' }
    const remoteInsert = { start: 2, end: 2, text: 'Y' }
    assert.deepEqual(transformEdit(localInsert, remoteInsert), { start: 3, end: 3, text: 'X' })
    assert.equal(
        applyTextEdit(applyTextEdit('abc', remoteInsert), transformEdit(localInsert, remoteInsert)),
        'abYXc',
    )

    const localReplace = { start: 1, end: 3, text: 'X' }
    const remoteInsideReplace = { start: 3, end: 3, text: 'Y' }
    assert.deepEqual(
        transformEdit(localReplace, remoteInsideReplace),
        { start: 1, end: 4, text: 'X' },
    )
    assert.equal(
        applyTextEdit(applyTextEdit('abcd', remoteInsideReplace), transformEdit(localReplace, remoteInsideReplace)),
        'aXd',
    )
})

test('rebaseTextWithLocalEdit preserves aggregate local text over a remote edit', () => {
    assert.equal(
        rebaseTextWithLocalEdit('abc', 'abc!', { start: 0, end: 0, text: '>' }),
        '>abc!',
    )
})

test('transformSelection moves editor selections over remote edits', () => {
    assert.deepEqual(
        transformSelection({ start: 2, end: 3, direction: 'forward' }, { start: 0, end: 0, text: 'xy' }),
        { start: 4, end: 5, direction: 'forward' },
    )
})
