"""Portable AHCM import is exercised with independently framed synthetic objects."""
from __future__ import annotations

import base64
from dataclasses import replace
import json
import struct

import pytest

from afterglow import ezhex, importer, payloads, project_devices, remotes
from afterglow.backends.harmony_ahcm import ahcm, backend, labels
from afterglow.backends.harmony_ahcm import readers
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative


def pointer(address: int) -> bytes:
    return address.to_bytes(3, "little")


class Native:
    def __init__(self):
        self.data = bytearray(0x5F)
        self.data[:4] = b"AHCM"
        self.data[8:11] = b"\x00\x14\x00"
        self.data[0x5B:0x5F] = b"CMAH"
        self.slot(1, self.add(b"\x09\x09\x29\x0d\x00\x00\x00"))

    def add(self, raw: bytes) -> int:
        address = 0x020000 + len(self.data)
        self.data.extend(raw)
        return address

    def slot(self, ordinal: int, address: int) -> None:
        at = 0x0B + 4 * ordinal
        self.data[at + 1:at + 4] = pointer(address)

    def index(self, slot: int, width: int, addresses: list[int]) -> int:
        root = self.add(len(addresses).to_bytes(width, "little")
                        + b"".join(pointer(address) for address in addresses))
        self.slot(slot, root)
        return root

    def symbol(self, words: list[int]) -> int:
        return self.add(struct.pack("<H", len(words))
                        + b"".join(struct.pack("<H", word) for word in words) + b"\x00\x00")

    def body(self, words: list[int]) -> int:
        symbol = self.symbol(words)
        table = self.add(b"\x01" + pointer(symbol))
        return self.add(pointer(table) + b"\x01\x00\x00")

    def command(self, recordings=None, ir_class=5) -> int:
        if recordings is None:
            recordings = [[[0xA328, 4500, 0x8230, 560], [0x8230, 20000], None]]
        bodies = [[self.body(words) if words is not None else 0 for words in phases]
                  for phases in recordings]
        start = 0x020000 + len(self.data)
        header = (b"\x00" + pointer(26315) + pointer(26315 // 2)
                  + bytes((ir_class,)) + pointer(start) + bytes((len(bodies),))
                  + b"".join(pointer(address) for phases in bodies for address in phases))
        self.add(header)
        return start + 7

    def groups(self, commands: list[list[int]]) -> None:
        addresses = [self.add(b"\x00" + struct.pack("<H", len(group))
                             + b"".join(pointer(address) for address in group))
                     for group in commands]
        self.index(5, 1, addresses)

    def action(self, *instructions) -> int:
        return self.add(bytes((len(instructions),))
                        + b"".join(struct.pack("<HB", operand, opcode)
                                   for opcode, operand in instructions))

    def tagged(self, *instructions, wide=False) -> int:
        if wide:
            raw = b"\x00" + bytes((len(instructions),))
            raw += b"".join(struct.pack("<BBHB", flags, tag, operand, opcode)
                            for flags, tag, opcode, operand in instructions)
        else:
            raw = bytes((len(instructions),)) if instructions else b"\x00\x00"
            raw += b"".join(struct.pack("<BHB", tag, operand, opcode)
                            for tag, opcode, operand in instructions)
        return self.add(raw)

    def program(self, *draws) -> int:
        raw = b"\x10\x00"
        for x, y, codes in draws:
            raw += b"\x05" + bytes((x, y)) + codes + b"\x00"
        return self.add(raw + b"\x00")

    def mode(self, hard: int, pages: list[tuple[int, int]]) -> int:
        page_addresses = [self.add(pointer(bindings) + pointer(program))
                          for bindings, program in pages]
        return self.add(b"\x00" + pointer(hard) + struct.pack("<H", len(pages))
                        + b"".join(pointer(page) for page in page_addresses))

    def state(self, first: int, maximum: int, *transitions) -> int:
        return self.add(struct.pack("<HHHB", first, maximum, len(transitions), 0)
                        + b"".join(struct.pack("<BhhHB", 0, before, after, operand, opcode)
                                   for before, after, opcode, operand in transitions))

    def states(self, addresses: list[int]) -> None:
        count = len(addresses)
        root = self.add(struct.pack("<4H", count, count, 0, count)
                        + b"".join(pointer(address) for address in addresses))
        self.slot(13, root)

    def names(self, *names) -> None:
        raw = bytearray(b"\xed\xfe\x00\x00\x00")
        for index, name in names:
            encoded = name.encode("ascii")
            raw.extend(b"\xa7" + struct.pack("<HHH", 4 + len(encoded), 1, index) + encoded)
        struct.pack_into("<H", raw, 2, len(raw))
        raw.extend(b"\xef\xbe")
        self.slot(0, self.add(bytes(raw)))

    def glyph(self, pixels: list[list[int]]) -> int:
        width = len(pixels[0])
        raw = bytearray((width,))
        for row in pixels:
            packed = bytearray((2 * width + 7) // 8)
            for index, pixel in enumerate(row):
                packed[index // 4] |= pixel << (6 - 2 * (index % 4))
            operation = bytes((0x50 | (width - 1),)) + packed
            raw.extend(bytes((0x20 | len(operation),)) + operation)
        raw.append(0)
        return self.add(bytes(raw))

    def font(self, first: int, shapes: list[list[list[int]]]) -> None:
        glyphs = [self.glyph(pixels) for pixels in shapes]
        font = self.add(bytes((len(shapes[0]), first, len(glyphs)))
                        + b"".join(pointer(glyph) for glyph in glyphs))
        self.index(7, 2, [font])

    def finish(self) -> bytes:
        raw = bytearray(self.data)
        raw[4:8] = (0x020000 + len(raw) + 2).to_bytes(4, "little")
        accumulator = 0x4321
        for index in range(0, len(raw) - 1, 2):
            accumulator ^= raw[index] | raw[index + 1] << 8
        return bytes(raw) + struct.pack("<H", accumulator) + b"MCHA"


GLYPH_A = [[2, 1, 2], [1, 2, 1], [1, 1, 1]]
GLYPH_B = [[1, 1, 2], [1, 1, 1], [1, 1, 2]]


def synthetic_profile(tmp_path, *, known_shapes=True):
    folder = tmp_path / "synthetic-profile"
    folder.mkdir()
    shapes = {labels.fingerprint(GLYPH_A): "A", labels.fingerprint(GLYPH_B): "B"}
    (folder / "glyph-shapes.json").write_text(
        json.dumps(shapes if known_shapes else {}), encoding="utf-8")
    return replace(
        remotes.get("harmony-550"), folder=folder,
        vocabulary={
            "hard_keys": ["VolumeUp", "VolumeDown"],
            "hard_key_codes": {"VolumeUp": 0x80, "VolumeDown": 0x81},
            "screen_key_cells": [
                {"code": 0x83, "bounds": [0, 0, 8, 10]},
                {"code": 0x84, "bounds": [10, 0, 8, 10]},
            ],
        })


def setup_image(*, unknown_start=False, different_page_key=False, flagged_leave=False) -> bytes:
    native = Native()
    commands = [[native.command() for _ in range(4)],
                [native.command() for _ in range(2)]]
    native.groups(commands)
    action_roots = []
    for group, records in enumerate(commands):
        for ordinal in range(len(records)):
            action_roots.append(native.action((0x7D, group << 8 | ordinal), (0x7C, group << 8)))
    start = [(0x80, 1), (0x81, 0), (0x82, 1), (0x83, 1), (0x7E, 2)]
    if unknown_start:
        start.insert(0, (0x66, 123))
    action_roots.append(native.action(*start))
    action_roots.append(native.action((0x1F, 0xFF01)))
    native.index(10, 2, action_roots)
    native.font(7, [GLYPH_A, GLYPH_B])
    two_labels = native.program((0, 0, b"\x07"), (10, 0, b"\x08"))
    one_label = native.program((0, 0, b"\x07"))
    soft = native.tagged((0x83, 0x7F, 2), (0x84, 0x7F, 3))
    mode0 = native.mode(native.tagged((0x80, 0x7F, 2), (0x81, 0x7F, 3)),
                        [(soft, two_labels)])
    mode1 = native.mode(native.tagged((0x80, 0x7F, 4), (0x81, 0x7F, 5)),
                        [(native.tagged(), native.program())])
    pages = [(native.tagged((0x83, 0x7F, 3)), one_label)]
    if different_page_key:
        pages.append((native.tagged((0x80, 0x7F, 2)), one_label))
    mode2 = native.mode(native.tagged((0x81, 0x7F, 2)), pages)
    menu = native.mode(native.tagged(), [(native.tagged((0x83, 0x7F, 7)), one_label)])
    native.index(6, 3, [mode0, mode1, mode2, menu])
    native.index(11, 2, [two_labels, one_label])
    idle = native.tagged((1, 0x83, 0))
    active = (native.tagged((0, 1, 0x7F, 6), (1, 2, 0x7F, 1), (0, 0x80, 0x7F, 4), wide=True)
              if flagged_leave else
              native.tagged((1, 0x7F, 6), (2, 0x7F, 1), (0x80, 0x7F, 4)))
    native.index(9, 1, [idle, active])
    native.states([
        native.state(0, 1, (0, 1, 0x7F, 0), (1, 0, 0x7F, 1)),
        native.state(0, 1, (0, 1, 0x7F, 4), (1, 0, 0x7F, 5)),
        native.state(0, 1, (-2, 0, 0x7F, 2), (-2, 1, 0x7F, 3)),
        native.state(0, 1),
    ])
    native.names((0, "Display_Power_2"), (1, "Receiver_Power_2"),
                 (2, "Display_Input_2"), (3, "CurrentActivityState_0_2"))
    return native.finish()


def read_project(tmp_path, payload: bytes, profile=None):
    profile = profile or remotes.get("harmony-550")
    source = tmp_path / "source"
    source.mkdir()
    payloads.get(profile.payload).unpack(payload, str(source))
    return backend.import_project(str(source), profile, {})


def test_rooted_import_recovers_controls_activity_routing_power_and_direct_inputs(tmp_path):
    profile = synthetic_profile(tmp_path)
    payload = setup_image()

    result = read_project(tmp_path, payload, profile)

    display, receiver = result.project["devices"]
    assert display["label"] == "Display"
    assert receiver["label"] == "Receiver"
    assert display["power_on_cmd"] == "Command 1"
    assert display["power_off_cmd"] == "Command 2"
    assert display["inputs"] == [["A", "A"], ["B", "B"]]
    assert display["screen_buttons"] == [["A", "A"], ["B", "B"]]
    assert {row[0]: row[4] for row in display["commands"]}["A"] == "VolumeUp"
    assert {row[0]: row[4] for row in display["commands"]}["B"] == "VolumeDown"
    activity = result.project["activities"][0]
    assert activity["label"] == "A"
    assert activity["power_on_devices"] == ["0"]
    assert activity["enter"] == [["input", "0", "B"]]
    assert activity["leave"] == [["command", "0", "Command 2"]]
    assert activity["hard_macros"]["VolumeUp"] == [["command", "1", "VolumeUp"]]
    assert activity["hard_macros"]["VolumeDown"] == [["command", "0", "A"]]
    assert activity["soft_buttons"] == [{"label": "A", "macro": [["command", "0", "B"]]}]
    assert base64.b64decode(result.preservation["payload_base64"]) == payload
    assert not result.rejected
    for device in result.project["devices"]:
        project_devices.validate(device)
        assert not project_devices.NATIVE_FIELDS.intersection(device)


def test_press_hold_release_and_toggle_recordings_are_preserved_as_portable_waveforms(tmp_path):
    native = Native()
    once = [0xA328, 4500, 0x8230, 560]
    held, tail = [0x8230, 20000], [0x812C, 1000]
    alternate = [0xA328, 4500, 0x8262, 560]
    native.groups([[native.command([[once, held, tail], [alternate, held, tail]])]])

    result = read_project(tmp_path, native.finish())
    signal = result.project["devices"][0]["signals"]["Command 1"]

    assert signal["kind"] == "waveform"
    assert signal["pulses_us"] == [9000, -4500, 560, -560, 560, -20000, 300, -1000]
    assert signal["sections"] == {
        "intro_pulses": 4, "repeat_pulses": 2, "release_pulses": 2}
    assert signal["alternate"]["pulses_us"][2] == 610
    assert signal["alternate"]["sections"] == signal["sections"]
    assert signal["native"]["harmony-ahcm"]["record"]["recordings"][0][2] is not None


def test_split_duration_words_become_physical_intervals_without_losing_native_leadin(tmp_path):
    native = Native()
    once = [32767, 17233, 0xA328, 4500, 0x8230, 32767, 17233]
    held = [0x8230, 32767, 17233]
    native.groups([[native.command([[once, held, None]])]])

    result = read_project(tmp_path, native.finish())
    signal = result.project["devices"][0]["signals"]["Command 1"]

    assert signal["kind"] == "waveform"
    assert signal["pulses_us"] == [9000, -4500, 560, -50000, 560, -50000]
    assert signal["sections"] == {
        "intro_pulses": 4, "repeat_pulses": 2, "release_pulses": 0}
    evidence = signal["native"]["harmony-ahcm"]
    assert evidence["leading_silence_us"] == [50000]
    assert evidence["record"]["recordings"][0][0]["words"] == once
    assert result.rejected["native_leadin_timing"] == 1
    assert any("transmitter lead-in" in warning for warning in result.warnings)


def test_one_unsupported_command_does_not_discard_a_device_or_its_other_commands(tmp_path):
    native = Native()
    native.groups([[native.command(), native.command(ir_class=9), native.command()]])
    payload = native.finish()

    result = read_project(tmp_path, payload)
    device = result.project["devices"][0]

    assert len(device["commands"]) == 3
    assert [device["signals"][f"Command {index}"]["kind"] for index in range(1, 4)] == [
        "waveform", "backend-opaque", "waveform"]
    assert result.rejected["opaque_commands"] == 1
    assert any("unsupported IR class 9" in warning for warning in result.warnings)
    assert base64.b64decode(result.preservation["payload_base64"]) == payload
    project_devices.validate(device)


def test_an_invalid_command_pointer_is_accounted_for_without_hiding_its_neighbors(tmp_path):
    native = Native()
    native.groups([[native.command(), 0x820000, native.command()]])

    result = read_project(tmp_path, native.finish())

    assert len(result.project["devices"][0]["commands"]) == 3
    assert result.rejected["opaque_commands"] == 1
    assert any("logical pointer" in failure["reason"]
               for failure in result.preservation["failures"])


def test_unreadable_glyphs_get_placeholders_and_never_guessed_menu_labels(tmp_path):
    profile = synthetic_profile(tmp_path, known_shapes=False)

    result = read_project(tmp_path, setup_image(), profile)

    assert result.project["activities"][0]["label"] == "Activity 2"
    assert result.rejected["unreadable_glyphs"] > 0
    assert result.rejected["unreadable_activity_labels"] == 1
    assert any("unknown glyph shape" in warning for warning in result.warnings)
    assert result.preservation["glyph_failures"]
    assert labels.UNKNOWN not in result.project["activities"][0]["label"]


def test_glyph_codes_are_resolved_from_shapes_in_each_image_not_one_fixed_map(tmp_path):
    profile = synthetic_profile(tmp_path)
    for first in (7, 27):
        native = Native()
        native.font(first, [GLYPH_A, GLYPH_B])
        text = labels.Text(Reader(ahcm.Image(native.finish())), profile)

        assert text.decode(bytes((first, first + 1)), 0) == "AB"
        assert not text.unreadable


def test_a_conditional_screen_never_supplies_a_partial_label_as_its_final_label(tmp_path):
    profile = synthetic_profile(tmp_path)
    native = Native()
    native.font(7, [GLYPH_A, GLYPH_B])
    branch = native.program((0, 0, b"\x08"))
    root = native.add(b"\x10\x00\x05\x00\x00\x07\x00"
                      + bytes((18, 0, 1, 0)) + pointer(branch) + b"\x00")
    reader = Reader(ahcm.Image(native.finish()))
    text = labels.Text(reader, profile)

    with pytest.raises(UnsupportedNative, match="conditional screen"):
        text.draws(reader.image.offset(root))
    assert text.unreadable["conditional screen labels not imported"] == 1
    assert reader.evidence()


def test_unknown_start_actions_are_preserved_and_do_not_become_a_false_macro(tmp_path):
    profile = synthetic_profile(tmp_path)
    payload = setup_image(unknown_start=True)

    result = read_project(tmp_path, payload, profile)

    assert result.project["activities"][0]["enter"] == []
    assert result.project["activities"][0]["soft_buttons"] == []
    assert result.rejected["unsupported_activity_start"] == 1
    assert any("unrepresented action 0x66" in warning for warning in result.warnings)
    assert base64.b64decode(result.preservation["payload_base64"]) == payload


def test_flagged_native_lists_preserve_their_flags_instead_of_assuming_a_plain_binding():
    native = Native()
    address = native.tagged((1, 0x80, 0x7D, 7), wide=True)
    reader = Reader(ahcm.Image(native.finish()))

    assert reader.tagged(reader.image.offset(address)) == [
        {"flags": 1, "tag": 0x80, "opcode": 0x7D, "operand": 7}]


def test_a_flagged_leave_handler_is_not_projected_as_an_unconditional_macro(tmp_path):
    profile = synthetic_profile(tmp_path)

    result = read_project(tmp_path, setup_image(flagged_leave=True), profile)

    assert result.project["activities"][0]["leave"] == []
    assert result.rejected["unsupported_activity_bindings"] == 1
    assert any("flagged activity event" in warning for warning in result.warnings)


def test_page_dependent_hard_keys_do_not_overwrite_an_activity_with_a_guessed_route(tmp_path):
    profile = synthetic_profile(tmp_path)

    result = read_project(tmp_path, setup_image(different_page_key=True), profile)

    activity = result.project["activities"][0]
    assert "VolumeUp" not in activity["hard_macros"]
    assert any("page-dependent hard key" in warning for warning in result.warnings)


def test_repeated_action_calls_execute_twice_but_active_recursion_is_reported():
    native = Native()
    first = native.action((0x7D, 3))
    second = native.action((0x7F, 0), (0x7F, 0))
    recursive = native.action((0x7F, 2))
    native.index(10, 2, [first, second, recursive])
    reader = Reader(ahcm.Image(native.finish()))

    assert reader.expand(0x7F, 1) == [{"opcode": 0x7D, "operand": 3}] * 2
    with pytest.raises(UnsupportedNative, match="recursive action list"):
        reader.expand(0x7F, 2)


def test_an_unused_dictionary_pointer_is_not_dereferenced():
    native = Native()
    body = native.add(pointer(0x820000) + b"\x00\x00")
    reader = Reader(ahcm.Image(native.finish()))

    assert reader.body(body)["words"] == []


def test_symbol_lengths_are_counted_and_unused_trailing_words_are_not_a_runtime_gate():
    native = Native()
    words = [0x8230, 0, 0x8230, 560]
    address = native.symbol(words)
    native.data[-2:] = b"\x34\x12"
    reader = Reader(ahcm.Image(native.finish()))

    assert reader.symbol(address) == words


def test_more_than_two_native_pointer_groups_are_opaque_not_clamped_to_one(tmp_path):
    native = Native()
    phases = [[0x8230, 560], None, None]
    native.groups([[native.command([phases, phases, phases])]])

    result = read_project(tmp_path, native.finish())

    signal = result.project["devices"][0]["signals"]["Command 1"]
    assert signal["kind"] == "backend-opaque"
    assert len(signal["native"]["harmony-ahcm"]["record"]["recordings"]) == 3
    assert any("pointer-group count" in warning for warning in result.warnings)


def test_inspector_duration_budget_is_exact_and_does_not_reject_native_framing():
    for indices, accepted in ((1000, True), (1001, False)):
        native = Native()
        symbol = native.symbol([0x8230, 560] * 500)
        table = native.add(b"\x01" + pointer(symbol))
        body = native.add(pointer(table) + struct.pack("<H", indices) + bytes(indices))
        payload = native.finish()
        reader = Reader(ahcm.Image(payload))

        assert ahcm.validate(payload)["ok"] is True
        if accepted:
            assert len(reader.body(body)["words"]) == readers.EXPANSION_BUDGET
            assert reader.body(body)["words"] is reader.body(body)["words"]
        else:
            with pytest.raises(UnsupportedNative, match="expanded-duration"):
                reader.body(body)


def test_inspector_object_budget_is_explicit_not_a_remote_capacity_claim():
    for count, accepted in ((readers.OBJECT_BUDGET, True), (readers.OBJECT_BUDGET + 1, False)):
        native = Native()
        target = native.add(b"\x00\x00")
        native.index(6, 3, [target] * count)
        payload = native.finish()
        reader = Reader(ahcm.Image(payload))

        assert ahcm.validate(payload)["ok"] is True
        if accepted:
            assert len(reader.addresses(6, 3)) == readers.OBJECT_BUDGET
        else:
            with pytest.raises(UnsupportedNative, match="native-object"):
                reader.addresses(6, 3)


def test_zero_pointer_groups_remain_opaque_without_acquiring_a_default_recording(tmp_path):
    native = Native()
    native.groups([[native.command([])]])

    result = read_project(tmp_path, native.finish())

    assert result.project["devices"][0]["signals"]["Command 1"]["kind"] == "backend-opaque"
    assert any("pointer-group count" in warning for warning in result.warnings)


def test_ambiguous_glyph_fingerprints_are_not_resolved_by_guessing(tmp_path):
    profile = synthetic_profile(tmp_path)
    (profile.folder / "glyph-shapes.json").write_text(
        json.dumps({labels.fingerprint(GLYPH_A): "Il"}), encoding="utf-8")
    native = Native()
    native.font(7, [GLYPH_A])
    text = labels.Text(Reader(ahcm.Image(native.finish())), profile)

    assert text.decode(b"\x07", 0) == labels.UNKNOWN
    assert text.unreadable["ambiguous glyph shape"] == 1


def test_a_per_device_queue_quantity_never_becomes_a_global_portable_delay(tmp_path):
    profile = synthetic_profile(tmp_path)
    native = Native()
    native.groups([[native.command()]])
    native.index(10, 2, [native.action((0x7D, 0), (0x7C, 1))])
    active = native.tagged((1, 0x7F, 1), (0x80, 0x7F, 0))
    start = native.action((0x80, 1))
    native.index(10, 2, [native.action((0x7D, 0), (0x7C, 1)), start])
    native.index(9, 1, [active])
    native.states([native.state(0, 1)])
    native.names((0, "CurrentActivityState_0_2"))

    result = read_project(tmp_path, native.finish(), profile)

    activity = result.project["activities"][0]
    assert "VolumeUp" not in activity["hard_macros"]
    assert any("not a global portable pause" in warning for warning in result.warnings)
    assert not any(step[0] == "delay" for step in activity["enter"])


def test_native_framing_alone_never_returns_an_empty_success_shaped_import(tmp_path):
    native = Native()

    with pytest.raises(backend.UnsupportedOperation, match="no rooted IR group index"):
        read_project(tmp_path, native.finish())


def test_an_empty_group_index_is_not_an_empty_success_shaped_undecoded_setup(tmp_path):
    native = Native()
    native.groups([])

    with pytest.raises(backend.UnsupportedOperation, match="no portable devices"):
        read_project(tmp_path, native.finish())


def test_shared_import_dispatch_keeps_native_evidence_warnings_and_read_only_status(tmp_path):
    profile = remotes.get("harmony-550")
    payload = setup_image(unknown_start=True)
    source = tmp_path / "source"
    payloads.get(profile.payload).unpack(payload, str(source))
    (source / ".ezhex_header").write_bytes(ezhex.header_for_profile(profile))

    result = importer.import_result(str(source))

    assert result.project["settings"]["remote"] == "harmony-550"
    assert result.warnings
    assert result.rejected["unsupported_activity_start"] == 1
    assert base64.b64decode(result.preservation["payload_base64"]) == payload
    assert all(device[project_devices.KEYS_FILLED] for device in result.project["devices"])
    assert remotes.get("harmony-550").status == remotes.READ_ONLY
    with pytest.raises(remotes.NotBuildable):
        backend.build_project(result.project, profile, None)
