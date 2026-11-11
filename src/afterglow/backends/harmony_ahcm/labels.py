"""Identify monochrome text from each image's glyphs, never fixed glyph ordinals.

Unknown and ambiguous shapes are reported rather than supplied with guessed labels.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from .ahcm import InvalidImage
from .readers import Reader, UnsupportedNative

UNKNOWN = "\N{REPLACEMENT CHARACTER}"


def fingerprint(rows: list[list[int]]) -> str:
    shape = f"{len(rows)}:" + "/".join(
        "".join("#" if pixel == 1 else "." for pixel in row) for row in rows)
    return hashlib.sha1(shape.encode("ascii")).hexdigest()[:16]


class Text:
    def __init__(self, reader: Reader, profile):
        self.reader = reader
        self.unreadable: Counter[str] = Counter()
        self.details: dict[tuple[int, int], str] = {}
        self._fonts: dict[int, dict] = {}
        self._characters: dict[tuple[int, int], str] = {}
        self.shapes: dict[str, str] = {}
        path = Path(profile.folder) / "glyph-shapes.json" if profile.folder else None
        if path is None or not path.is_file():
            self.unreadable["missing glyph fingerprints"] += 1
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                if not re.fullmatch("[0-9a-f]{16}", key) or not isinstance(value, str):
                    raise ValueError("invalid glyph fingerprint catalogue entry")
                self.shapes[key] = value

    def character(self, font_index: int, code: int) -> str:
        key = font_index, code
        if key not in self._characters:
            try:
                if font_index not in self._fonts:
                    self._fonts[font_index] = self.reader.font(font_index)
                pixels = self.reader.glyph(self._fonts[font_index], code)
                if not pixels or not pixels[0]:
                    raise UnsupportedNative("empty glyph shape cannot identify a character")
                if not any(pixel == 1 for row in pixels for pixel in row):
                    character = " "
                else:
                    candidates = self.shapes.get(fingerprint(pixels), "")
                    if len(candidates) != 1:
                        raise UnsupportedNative(
                            "ambiguous glyph shape" if candidates else "unknown glyph shape")
                    character = candidates
            except (InvalidImage, UnsupportedNative) as exc:
                self.details[key] = str(exc)
                character = UNKNOWN
            self._characters[key] = character
        character = self._characters[key]
        if character == UNKNOWN:
            self.unreadable[self.details[key]] += 1
        return character

    def decode(self, codes: bytes, font_index: int) -> str:
        characters, cursor = [], 0
        while cursor < len(codes):
            code = codes[cursor]
            if code & 0x80:
                self.unreadable["extended glyph encoding not established"] += 1
                characters.append(UNKNOWN)
                cursor += 2
            else:
                characters.append(self.character(font_index, code))
                cursor += 1
        return "".join(characters)

    def draws(self, root: int) -> list[dict]:
        font = 0
        out = []
        for item in self.reader.screen(root):
            opcode = item["opcode"]
            if opcode == 16:
                font = bytes.fromhex(item["operands_hex"])[0]
            elif opcode in (4, 5):
                out.append({**item, "font": font,
                            "text": self.decode(bytes.fromhex(item["codes_hex"]), font)})
            elif opcode in (18, 19):
                self.unreadable["conditional screen labels not imported"] += 1
                raise UnsupportedNative(
                    "a conditional screen's final label is not established")
        return out
