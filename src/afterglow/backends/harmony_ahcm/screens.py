"""Construct explicit source-pixel glyphs without choosing a display or copying artwork.

The literal-only encoder deliberately reports rows requiring another compression form.

Format reference: docs/harmony_ahcm/platform.md
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .layout import Layout
from .readers import UnsupportedNative


def _byte(value: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError(f"{field} must be an unsigned 8-bit integer")
    return value


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


def add_font(planned: Layout, name: str, *, first_code: int,
             glyphs: Sequence[Sequence[Sequence[int]]]) -> str:
    """Allocate caller-authored glyphs and their explicit font; return its named target."""
    _byte(first_code, "first glyph code")
    if not first_code or not glyphs or len(glyphs) > 255:
        raise ValueError("a font needs a nonzero first code and one to 255 explicit glyphs")
    if first_code + len(glyphs) > 256:
        raise ValueError("the font's glyph codes exceed the unsigned 8-bit range")
    encoded = [encode_literal_glyph(rows) for rows in glyphs]
    height = len(glyphs[0])
    if any(len(rows) != height for rows in glyphs):
        raise ValueError("all glyphs in a font must have the same explicit height")
    for index, raw in enumerate(encoded):
        planned.add(f"{name}.glyph-{index}", raw, owner="screen-glyphs")
    planned.add(name, bytes((height, first_code, len(glyphs))) + bytes(3 * len(glyphs)),
                owner="screen-fonts")
    for index in range(len(glyphs)):
        planned.relocate(name, 3 + 3 * index, f"{name}.glyph-{index}",
                         size=len(encoded[index]))
    return name


def add_font_index(planned: Layout, name: str, fonts: Sequence[str | None]) -> str:
    """Allocate ordered named font references without choosing or replacing a section root."""
    if len(fonts) > 65535:
        raise ValueError("font index count must fit its unsigned 16-bit field")
    planned.add(name, len(fonts).to_bytes(2, "little") + bytes(3 * len(fonts)),
                owner="screen-fonts")
    for index, font in enumerate(fonts):
        planned.relocate(name, 2 + 3 * index, font, size=3)
    return name


@dataclass(frozen=True)
class TextLine:
    """Explicit source glyph codes and placement; not a Unicode-to-font mapping."""

    x: int
    y: int
    codes: bytes

    def __post_init__(self):
        _byte(self.x, "text x coordinate")
        _byte(self.y, "text y coordinate")
        if not isinstance(self.codes, bytes):
            raise TypeError("text glyph codes must be immutable bytes")
        if any(not 1 <= code <= 127 for code in self.codes):
            raise UnsupportedNative(
                "inline text supports only nonzero single-byte glyph codes; "
                "extended-code selection is not established")


def encode_text_program(*, display_page: int, font_index: int,
                        lines: Sequence[TextLine]) -> bytes:
    """Encode explicit LCD-page/font selection and text; LCD pages are not mode pages."""
    _byte(display_page, "selected display page")
    _byte(font_index, "selected font index")
    result = bytearray((22, display_page, 16, font_index))
    for line in lines:
        if not isinstance(line, TextLine):
            raise TypeError("text program lines must be TextLine values")
        result.extend((5, line.x, line.y))
        result.extend(line.codes)
        result.append(0)
    result.append(0)
    return bytes(result)
