"""Invented glyphs exercise format effects without vendor artwork or display assumptions."""
from __future__ import annotations

import pytest

from afterglow.backends.harmony_ahcm import ahcm, layout, screens
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative


def _glyph(raw: bytes, height: int) -> tuple[Reader, dict]:
    planned = layout.Layout(header_value=0x010203, spares=tuple(range(20)))
    for slot in range(20):
        planned.root(slot, "fonts" if slot == 7 else None)
    planned.add("glyph", raw, owner="synthetic-glyph")
    planned.add("font", bytes((height, 7, 1)) + bytes(3), owner="synthetic-font")
    planned.relocate("font", 3, "glyph")
    planned.add("fonts", b"\x01\x00" + bytes(3), owner="synthetic-font")
    planned.relocate("fonts", 2, "font")
    reader = Reader(ahcm.Image(planned.assemble().payload))
    return reader, reader.font(0)


def test_a_row_length_keeps_its_fifth_bit_instead_of_discarding_longer_glyph_rows():
    operations = b"\x5f\x1b\x1b\x1b\x1b" * 5
    reader, font = _glyph(bytes((80, 0x20 + len(operations))) + operations + b"\x00", 1)

    assert reader.glyph(font, 7) == [[0, 1, 2, 3] * 20]
    captured = [region for region in reader.evidence() if region["kind"] == "glyph"]
    assert len(captured) == 1
    assert captured[0]["hex"] == (
        bytes((80, 0x20 + len(operations))) + operations + b"\x00"
    ).hex()


@pytest.mark.parametrize("operation, expected", [
    (b"\x9f", [0] * 32),
    (b"\xbf", [1] * 32),
    (b"\x7f", [2] * 32),
    (b"\x53\x1b", [0, 1, 2, 3]),
    (b"\x43\x50", [0, 1, 0, 1]),
])
def test_supported_clear_ink_transparent_and_literal_sources_preserve_their_pixel_values(
        operation, expected):
    reader, font = _glyph(
        bytes((len(expected), 0x20 + len(operation))) + operation + b"\x00", 1)

    assert reader.glyph(font, 7) == [expected]


def test_a_literal_cannot_consume_the_following_row_as_its_missing_pixel_bytes():
    reader, font = _glyph(b"\x10\x21\x5f\x20\x00", 2)

    with pytest.raises(ahcm.InvalidImage, match="cross their row"):
        reader.glyph(font, 7)


@pytest.mark.parametrize("width", [1, 3, 4, 15, 16, 17, 80, 96])
def test_caller_authored_literal_rows_roundtrip_without_borrowed_artwork_or_geometry(width):
    rows = [[index % 4 for index in range(width)],
            [(index + 2) % 4 for index in range(width)]]
    raw = screens.encode_literal_glyph(rows)
    reader, font = _glyph(raw, len(rows))

    assert reader.glyph(font, 7) == rows
    assert screens.encode_literal_glyph(rows) == raw


def test_the_literal_only_encoder_reports_its_storage_limit_not_a_native_width_limit():
    with pytest.raises(UnsupportedNative, match="another compression form"):
        screens.encode_literal_glyph([[1] * 97])
    reader, font = _glyph(b"\x61\x24\xbf\xbf\xbf\xa0\x00", 1)

    assert reader.glyph(font, 7) == [[1] * 97]


@pytest.mark.parametrize("rows", [
    [], [[]], [[0], [1, 2]], [[-1]], [[4]], [[True]], [[1.0]],
    [[0] * 256], [[0]] * 256,
])
def test_invalid_host_glyphs_are_not_truncated_or_reshaped(rows):
    with pytest.raises(ValueError):
        screens.encode_literal_glyph(rows)


def test_unmodeled_glyph_operations_remain_explicit_instead_of_skipped():
    reader, font = _glyph(b"\x01\x21\xc0\x00", 1)

    with pytest.raises(UnsupportedNative, match="unrecognised glyph operation"):
        reader.glyph(font, 7)
