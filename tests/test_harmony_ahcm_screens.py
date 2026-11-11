"""Invented glyphs exercise format effects without vendor artwork or display assumptions."""
from __future__ import annotations

import pytest

from afterglow.backends.harmony_ahcm import ahcm, layout, modes, platform, screens
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative
from afterglow.backends.harmony_ahcm.runtime import ModeState, Runtime


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


def test_source_owned_fonts_and_text_are_built_from_explicit_inputs_not_an_imported_image():
    planned = layout.Layout(header_value=0x030201, spares=tuple(range(20)))
    for slot in range(20):
        planned.root(slot, "fonts" if slot == 7 else None)
    shapes = [[[1, 0, 1]], [[0, 1]]]
    target = screens.add_font(planned, "authored-font", first_code=7, glyphs=shapes)
    screens.add_font_index(planned, "fonts", [None, target])
    program = screens.encode_text_program(
        display_page=0, font_index=1, lines=[screens.TextLine(3, 2, b"\x07\x08"),
                             screens.TextLine(8, 4, b"")])
    planned.add("program", program, owner="synthetic-screen")
    first = planned.assemble()
    reader = Reader(ahcm.Image(first.payload))

    assert first == planned.assemble()
    assert reader.addresses(7, 2)[0] == 0
    font = reader.font(1)
    assert [reader.glyph(font, code) for code in (7, 8)] == shapes
    assert reader.screen(first.offset("program")) == [
        {"offset": first.offset("program"), "opcode": 22, "operands_hex": "00"},
        {"offset": first.offset("program") + 2, "opcode": 16, "operands_hex": "01"},
        {"offset": first.offset("program") + 4, "opcode": 5,
         "x": 3, "y": 2, "codes_hex": "0708"},
        {"offset": first.offset("program") + 10, "opcode": 5,
         "x": 8, "y": 4, "codes_hex": ""},
    ]


def test_named_font_references_can_be_forward_declared_without_a_chosen_global_root():
    planned = layout.Layout(header_value=0x010203, spares=tuple(range(20)))
    screens.add_font_index(planned, "fonts", ["later"])
    screens.add_font(planned, "later", first_code=1, glyphs=[[[1]]])
    for slot in range(20):
        planned.root(slot, "fonts" if slot == 7 else None)
    reader = Reader(ahcm.Image(planned.assemble().payload))

    assert reader.glyph(reader.font(0), 1) == [[1]]


@pytest.mark.parametrize("first, glyphs", [
    (0, [[[1]]]), (True, [[[1]]]), (256, [[[1]]]), (1, []),
    (255, [[[1]], [[0]]]), (1, [[[1]], [[1], [1]]]),
])
def test_font_construction_refuses_ambiguous_or_inconsistent_explicit_repertoires(first, glyphs):
    planned = layout.Layout(header_value=0, spares=[0] * 20)
    with pytest.raises(ValueError):
        screens.add_font(planned, "invalid", first_code=first, glyphs=glyphs)


@pytest.mark.parametrize("codes", [b"\x00", b"\x80", b"\xff", b"\x01\x00\x02"])
def test_text_codes_never_introduce_a_hidden_terminator_or_guessed_extended_mapping(codes):
    with pytest.raises(UnsupportedNative, match="single-byte"):
        screens.TextLine(0, 0, codes)


def test_text_programs_require_explicit_valid_font_coordinates_and_typed_source_lines():
    with pytest.raises(ValueError, match="font"):
        screens.encode_text_program(display_page=0, font_index=True, lines=[])
    with pytest.raises(ValueError, match="coordinate"):
        screens.TextLine(-1, 0, b"\x01")
    with pytest.raises(TypeError, match="immutable"):
        screens.TextLine(0, 0, bytearray(b"\x01"))
    with pytest.raises(TypeError, match="TextLine"):
        screens.encode_text_program(display_page=0, font_index=0, lines=[(0, 0, b"\x01")])
    with pytest.raises(ValueError, match="display page"):
        screens.encode_text_program(display_page=-1, font_index=0, lines=[])


def test_source_owned_pages_link_the_event_font_and_paint_contract_without_a_donor_template():
    planned = layout.Layout(header_value=0x030201, spares=tuple(range(20)))
    first_shape, second_shape = [[1, 0, 1]], [[0, 1]]
    screens.add_font(planned, "authored-font", first_code=7,
                     glyphs=[first_shape, second_shape])
    screens.add_font_index(planned, "fonts", ["authored-font"])
    empty = modes.add_bindings(planned, "no-callbacks", [], wide=True)
    pages = []
    for ordinal, code in enumerate((7, 8)):
        name = f"authored-program-{ordinal}"
        planned.add(name, screens.encode_text_program(
            display_page=0, font_index=0,
            lines=[screens.TextLine(3 + ordinal, 2, bytes((code,)))]),
            owner="synthetic-screen")
        pages.append(modes.add_page(
            planned, f"page-{ordinal}", bindings=empty, program=name))
    selected = modes.add_mode(planned, "authored-mode", kind=0, bindings=empty, pages=pages)
    modes.add_mode_index(planned, "modes", [selected])
    planned.add("events", platform.encode_event_table(
        [platform.EventAction(0, 0), platform.EventAction(0, 0x7E)]), owner="platform-events")
    planned.add("explicit-empty-state-table", bytes(8), owner="synthetic-states")
    for slot in range(20):
        planned.root(slot, {6: "modes", 7: "fonts", 8: "events",
                            13: "explicit-empty-state-table"}.get(slot))
    assembled = planned.assemble()
    reader = Reader(ahcm.Image(assembled.payload))
    observed = []

    def paint(request):
        instructions = reader.screen(reader.image.offset(request.program))
        assert instructions[0]["opcode"] == 22
        font_index = bytes.fromhex(instructions[1]["operands_hex"])[0]
        text = instructions[2]
        shapes = [reader.glyph(reader.font(font_index), code)
                  for code in bytes.fromhex(text["codes_hex"])]
        observed.append((request.mode, request.page, text["x"], text["y"], shapes))

    runtime = Runtime(reader, values=[], context=None, accept=lambda _request: False,
                      mode_state=ModeState(None, None), paint=paint)
    handoff = platform.event_action(reader.image, 1)
    assert handoff is not None
    runtime.run([(handoff.opcode, handoff.operand)])
    runtime.run([(0x0F, 0xA1)])
    runtime.run([(0x0F, 0xA1)])

    assert observed == [(0, 0, 3, 2, [first_shape]),
                        (0, 1, 4, 2, [second_shape]),
                        (0, 0, 3, 2, [first_shape])]
    assert planned.assemble() == assembled
