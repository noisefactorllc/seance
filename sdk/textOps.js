export function diffText(previous, next) {
    const before = String(previous ?? '')
    const after = String(next ?? '')
    if (before === after) return null

    let start = 0
    const maxPrefix = Math.min(before.length, after.length)
    while (start < maxPrefix && before[start] === after[start]) {
        start += 1
    }

    let oldEnd = before.length
    let newEnd = after.length
    while (
        oldEnd > start &&
        newEnd > start &&
        before[oldEnd - 1] === after[newEnd - 1]
    ) {
        oldEnd -= 1
        newEnd -= 1
    }

    return {
        start,
        end: oldEnd,
        text: after.slice(start, newEnd),
    }
}

export function applyTextEdit(text, edit) {
    const value = String(text ?? '')
    validateEdit(value, edit)
    return value.slice(0, edit.start) + edit.text + value.slice(edit.end)
}

export function validateEdit(text, edit) {
    if (!edit || !Number.isInteger(edit.start) || !Number.isInteger(edit.end)) {
        throw new TypeError('edit.start and edit.end must be integers')
    }
    if (edit.start < 0 || edit.end < edit.start || edit.end > text.length) {
        throw new RangeError('edit range is outside the text')
    }
    if (typeof edit.text !== 'string') {
        throw new TypeError('edit.text must be a string')
    }
}

export function transformOffset(offset, edit, affinity = 'after') {
    return affinity === 'before'
        ? transformRangeStart(offset, edit)
        : transformRangeEnd(offset, edit)
}

export function transformSelection(selection, edit) {
    const start = transformOffset(selection.start, edit, 'before')
    const end = transformOffset(selection.end, edit, 'after')
    return {
        start: Math.min(start, end),
        end: Math.max(start, end),
        direction: selection.direction || 'none',
    }
}

export function transformEdit(localEdit, remoteEdit) {
    if (
        localEdit.start === localEdit.end &&
        remoteEdit.start === remoteEdit.end &&
        localEdit.start === remoteEdit.start
    ) {
        const offset = remoteEdit.start + remoteEdit.text.length
        return { start: offset, end: offset, text: localEdit.text }
    }
    if (localEdit.start === localEdit.end) {
        const offset = transformInsertPoint(localEdit.start, remoteEdit)
        return { start: offset, end: offset, text: localEdit.text }
    }
    const start = transformRangeStart(localEdit.start, remoteEdit)
    const end = transformRangeEnd(localEdit.end, remoteEdit)
    return {
        start: Math.min(start, end),
        end: Math.max(start, end),
        text: localEdit.text,
    }
}

export function rebaseTextWithLocalEdit(oldServerText, localText, remoteEdit) {
    const localEdit = diffText(oldServerText, localText)
    const newServerText = applyTextEdit(oldServerText, remoteEdit)
    if (!localEdit) return newServerText
    return applyTextEdit(newServerText, transformEdit(localEdit, remoteEdit))
}

function transformInsertPoint(pos, applied) {
    const newLength = applied.text.length
    const oldLength = applied.end - applied.start
    const delta = newLength - oldLength
    if (pos < applied.start) return pos
    if (applied.start === applied.end) return pos + newLength
    if (pos === applied.start) return applied.start
    if (pos <= applied.end) return applied.start + newLength
    return pos + delta
}

function transformRangeStart(pos, applied) {
    const newLength = applied.text.length
    const oldLength = applied.end - applied.start
    const delta = newLength - oldLength
    if (pos < applied.start) return pos
    if (applied.start === applied.end) return pos + newLength
    if (pos === applied.start) return applied.start
    if (pos < applied.end) return applied.start
    if (pos === applied.end) return applied.start + newLength
    return pos + delta
}

function transformRangeEnd(pos, applied) {
    const newLength = applied.text.length
    const oldLength = applied.end - applied.start
    const delta = newLength - oldLength
    if (pos < applied.start) return pos
    if (applied.start === applied.end) return pos + newLength
    if (pos <= applied.start) return pos
    if (pos <= applied.end) return applied.start + newLength
    return pos + delta
}
