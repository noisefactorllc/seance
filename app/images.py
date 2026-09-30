"""Bounded, content-addressed raster images; stored bytes are never re-encoded."""

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass

from app.config import Limits

IMAGE_ID_RE = re.compile(r"[a-f0-9]{64}\Z")
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/]*={0,2})\Z")


class ImageError(ValueError):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True)
class ImageAsset:
    id: str
    mime_type: str
    width: int
    height: int
    data: bytes


def _jpeg_orientation(segment: bytes) -> int:
    if not segment.startswith(b"Exif\0\0"):
        return 1
    tiff = segment[6:]
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return 1
    order = "<" if tiff[:2] == b"II" else ">"
    try:
        if struct.unpack_from(order + "H", tiff, 2)[0] != 42:
            return 1
        offset = struct.unpack_from(order + "I", tiff, 4)[0]
        count = struct.unpack_from(order + "H", tiff, offset)[0]
        for i in range(min(count, (len(tiff) - offset - 2) // 12)):
            at = offset + 2 + i * 12
            tag, kind, n = struct.unpack_from(order + "HHI", tiff, at)
            if tag == 0x112 and kind == 3 and n == 1:
                return struct.unpack_from(order + "H", tiff, at + 8)[0]
    except struct.error:
        pass
    return 1


def image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    try:
        if mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            if data[8:16] == b"\0\0\0\rIHDR" and len(data) >= 33:
                return struct.unpack_from(">II", data, 16)
        elif mime == "image/gif" and data[:6] in (b"GIF87a", b"GIF89a"):
            if len(data) >= 13:
                return struct.unpack_from("<HH", data, 6)
        elif mime == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            size = struct.unpack_from("<I", data, 4)[0] + 8
            if size > len(data):
                raise ImageError("truncated image")
            kind = data[12:16]
            if kind == b"VP8X" and len(data) >= 30:
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2f:
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                width, height = struct.unpack_from("<HH", data, 26)
                return width & 0x3fff, height & 0x3fff
        elif mime == "image/jpeg" and data[:2] == b"\xff\xd8":
            offset, orientation = 2, 1
            dimensions = None
            while offset < len(data):
                if data[offset] != 0xff:
                    break
                while offset < len(data) and data[offset] == 0xff:
                    offset += 1
                marker = data[offset]
                offset += 1
                if marker in (0xd9, 0xda):
                    break
                if marker == 1 or 0xd0 <= marker <= 0xd7:
                    continue
                size = struct.unpack_from(">H", data, offset)[0]
                if size < 2 or offset + size > len(data):
                    raise ImageError("truncated image")
                payload = data[offset + 2:offset + size]
                if marker == 0xe1 and payload.startswith(b"Exif\0\0"):
                    orientation = _jpeg_orientation(payload)
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                              0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                    height, width = struct.unpack_from(">HH", payload, 1)
                    dimensions = width, height
                offset += size
            if dimensions:
                return dimensions[::-1] if orientation in (5, 6, 7, 8) else dimensions
    except (struct.error, IndexError) as exc:
        raise ImageError("invalid image") from exc
    raise ImageError("invalid or unsupported image; use PNG, JPEG, GIF or WebP")


def validate_image(value, limits: Limits) -> ImageAsset:
    if not isinstance(value, dict) or not isinstance(value.get("dataUrl"), str):
        raise ImageError("image must contain an id and dataUrl")
    url = value["dataUrl"]
    if len(url) > 64 + ((limits.max_image_bytes + 2) // 3) * 4:
        raise ImageError("image exceeds size limit", 413)
    match = _DATA_URL.fullmatch(url)
    if not match:
        raise ImageError("images only; use a PNG, JPEG, GIF or WebP data URL")
    mime, encoded = match.groups()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("invalid image encoding") from exc
    if len(data) > limits.max_image_bytes:
        raise ImageError("image exceeds size limit", 413)
    digest = hashlib.sha256(data).hexdigest()
    if value.get("id") != digest:
        raise ImageError("image id does not match its bytes")
    width, height = image_dimensions(data, mime)
    if not (0 < width <= 16384 and 0 < height <= 16384 and width * height <= 64_000_000):
        raise ImageError("image dimensions exceed limits", 413)
    return ImageAsset(digest, mime, width, height, data)


def validate_images(values, limits: Limits) -> list[ImageAsset]:
    if not isinstance(values, list):
        raise ImageError("images must be an array")
    if len(values) > limits.max_images:
        raise ImageError("too many images", 413)
    images = {image.id: image for value in values if (image := validate_image(value, limits))}
    if sum(len(image.data) for image in images.values()) > limits.max_image_session_bytes:
        raise ImageError("session images exceed size limit", 413)
    return list(images.values())
