"""Construct explicit source-pixel glyphs without choosing a display or copying artwork.

The literal-only encoder deliberately reports rows requiring another compression form.

Format reference: docs/harmony_ahcm/platform.md
"""
from __future__ import annotations

from collections.abc import Sequence

from .readers import UnsupportedNative


def encode_literal_glyph(rows: Sequence[Sequence[int]]) -> bytes:
    """Encode caller-authored two-bit source pixels, not text or a complete font."""
    if not rows or not rows[0]:
        raise ValueError("a literal glyph requires at least one nonempty row")
    width = len(rows[0])
    if width > 255 or len(rows) > 255:
        raise ValueError("glyph width and external font height must fit unsigned 8-bit fields")
    result = bytearray((width,))
    for row in rows:
        if len(row) != width:
            raise ValueError("glyph rows must have the same width")
        encoded = bytearray()
        for start in range(0, width, 16):
            pixels = row[start:start + 16]
            packed = bytearray((len(pixels) * 2 + 7) // 8)
            for index, value in enumerate(pixels):
                if type(value) is not int or not 0 <= value <= 3:
                    raise ValueError("glyph source pixels must be integers from 0 through 3")
                packed[index // 4] |= value << (6 - 2 * (index % 4))
            encoded.append(0x50 | (len(pixels) - 1))
            encoded.extend(packed)
        if len(encoded) > 31:
            raise UnsupportedNative(
                "literal-only glyph row exceeds the five-bit operation-byte count; "
                "another compression form is required")
        result.append(0x20 | len(encoded))
        result.extend(encoded)
    result.append(0)
    return bytes(result)
