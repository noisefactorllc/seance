const PALETTE = [
    '#4dabf7',
    '#ff6b6b',
    '#51cf66',
    '#ffd43b',
    '#cc5de8',
    '#20c997',
    '#ff922b',
    '#748ffc',
    '#f06595',
    '#94d82d',
]

export function peerColor(userId) {
    const text = String(userId || 'peer')
    let hash = 2166136261
    for (let index = 0; index < text.length; index += 1) {
        hash ^= text.charCodeAt(index)
        hash = Math.imul(hash, 16777619)
    }
    return PALETTE[Math.abs(hash) % PALETTE.length]
}

export function peerPalette() {
    return [...PALETTE]
}
