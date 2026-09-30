// Content-addressed raster assets. Encoding changes neither bytes nor image quality.
const IMAGE_MAX_BYTES = 8 * 1024 * 1024
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

function imageExifOrientation(bytes) {
    if (String.fromCharCode(...bytes.subarray(0, 6)) !== 'Exif\0\0') return 1
    const tiff = bytes.subarray(6)
    if (tiff.length < 8) return 1
    const little = tiff[0] === 0x49 && tiff[1] === 0x49
    if (!little && !(tiff[0] === 0x4d && tiff[1] === 0x4d)) return 1
    const view = new DataView(tiff.buffer, tiff.byteOffset, tiff.byteLength)
    try {
        if (view.getUint16(2, little) !== 42) return 1
        const offset = view.getUint32(4, little)
        const count = Math.min(view.getUint16(offset, little), Math.floor((tiff.length - offset - 2) / 12))
        for (let i = 0; i < count; i++) {
            const at = offset + 2 + i * 12
            if (view.getUint16(at, little) === 0x112 && view.getUint16(at + 2, little) === 3 && view.getUint32(at + 4, little) === 1) return view.getUint16(at + 8, little)
        }
    } catch { /* Invalid EXIF cannot change the bounded raster dimensions. */ }
    return 1
}

function imageDimensions(bytes, mimeType) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const ascii = (at, size) => String.fromCharCode(...bytes.subarray(at, at + size))
    const u24 = at => bytes[at] | bytes[at + 1] << 8 | bytes[at + 2] << 16
    try {
        if (mimeType === 'image/png' && ascii(0, 8) === '\x89PNG\r\n\x1a\n' && bytes.length >= 33 && view.getUint32(8) === 13 && ascii(12, 4) === 'IHDR') return [view.getUint32(16), view.getUint32(20)]
        if (mimeType === 'image/gif' && ['GIF87a', 'GIF89a'].includes(ascii(0, 6)) && bytes.length >= 13) return [view.getUint16(6, true), view.getUint16(8, true)]
        if (mimeType === 'image/webp' && ascii(0, 4) === 'RIFF' && ascii(8, 4) === 'WEBP' && view.getUint32(4, true) + 8 <= bytes.length) {
            const kind = ascii(12, 4)
            if (kind === 'VP8X' && bytes.length >= 30) return [u24(24) + 1, u24(27) + 1]
            if (kind === 'VP8L' && bytes.length >= 25 && bytes[20] === 0x2f) {
                const bits = view.getUint32(21, true)
                return [(bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1]
            }
            if (kind === 'VP8 ' && bytes.length >= 30 && ascii(23, 3) === '\x9d\x01\x2a') return [view.getUint16(26, true) & 0x3fff, view.getUint16(28, true) & 0x3fff]
        }
        if (mimeType === 'image/jpeg' && bytes[0] === 0xff && bytes[1] === 0xd8) {
            let offset = 2, orientation = 1, dimensions = null
            while (offset < bytes.length && bytes[offset] === 0xff) {
                while (bytes[offset] === 0xff) offset++
                const marker = bytes[offset++]
                if (marker === 0xd9 || marker === 0xda) break
                if (marker === 1 || marker >= 0xd0 && marker <= 0xd7) continue
                const size = view.getUint16(offset)
                if (size < 2 || offset + size > bytes.length) throw new Error('Invalid image')
                if (marker === 0xe1 && ascii(offset + 2, 6) === 'Exif\0\0') orientation = imageExifOrientation(bytes.subarray(offset + 2, offset + size))
                if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) dimensions = [view.getUint16(offset + 5), view.getUint16(offset + 3)]
                offset += size
            }
            if (dimensions) return [5, 6, 7, 8].includes(orientation) ? dimensions.reverse() : dimensions
        }
    } catch { throw new Error('Invalid image') }
    throw new Error('Invalid image; use PNG, JPEG, GIF or WebP')
}

export async function prepareImage(blob) {
    if (!blob || typeof blob.arrayBuffer !== 'function' || (blob.type && !IMAGE_TYPES.has(blob.type))) throw new Error('Images only; use PNG, JPEG, GIF or WebP')
    if (!blob.size || blob.size > IMAGE_MAX_BYTES) throw new Error('Image exceeds the 8 MiB limit')
    const bytes = new Uint8Array(await blob.arrayBuffer())
    let mimeType = blob.type
    if (!mimeType) {
        for (const candidate of IMAGE_TYPES) {
            try { imageDimensions(bytes, candidate); mimeType = candidate; break } catch { /* Try the next raster signature. */ }
        }
    }
    const [width, height] = imageDimensions(bytes, mimeType)
    if (!(width > 0 && width <= 16384 && height > 0 && height <= 16384 && width * height <= 64000000)) throw new Error('Image dimensions exceed limits')
    const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes))
    const id = Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('')
    let binary = ''
    for (let at = 0; at < bytes.length; at += 16384) binary += String.fromCharCode(...bytes.subarray(at, at + 16384))
    return { id, dataUrl: `data:${mimeType};base64,${btoa(binary)}`, width, height, mimeType }
}
