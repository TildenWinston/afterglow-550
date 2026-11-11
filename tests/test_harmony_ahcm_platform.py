"""Synthetic platform handoff checks establish record selection, not remote execution."""
from __future__ import annotations

import pytest

from afterglow import remotes
from afterglow.backends.harmony_ahcm import ahcm, layout, platform
from afterglow.backends.harmony_ahcm.readers import UnsupportedNative


def _image(events: bytes | None, *, filler: int = 0) -> ahcm.Image:
    planned = layout.Layout(header_value=0x030201, spares=tuple(range(20)))
    for slot in range(20):
        planned.root(slot, "events" if slot == 8 and events is not None else None)
    if filler:
        planned.add("filler", bytes(filler), owner="synthetic")
    if events is not None:
        planned.add("events", events, owner="platform-events")
    return ahcm.Image(planned.assemble().payload)


def test_event_selection_stages_the_inline_action_not_an_address_or_an_action_list():
    first = platform.EventAction(0x5678, 0x31)
    second = platform.EventAction(0xABCD, 0x93)
    image = _image(platform.encode_event_table([first, second]))

    assert platform.event_action(image, 0) == first
    assert platform.event_action(image, 1) == second
    assert platform.event_action(image, 2) is None
    assert platform.event_action(image, 255) is None


def test_an_all_zero_event_is_distinct_from_an_absent_or_out_of_range_event():
    image = _image(platform.encode_event_table([platform.EventAction(0, 0)]))

    assert platform.event_action(image, 0) == platform.EventAction(0, 0)
    assert platform.event_action(image, 1) is None
    assert platform.event_action(_image(b"\x00"), 0) is None
    with pytest.raises(UnsupportedNative, match="event table is absent"):
        platform.event_action(_image(None), 0)


def test_event_selection_uses_the_full_logical_root_and_crosses_byte_carries():
    actions = [platform.EventAction(index * 17, index) for index in range(255)]
    image = _image(platform.encode_event_table(actions), filler=0x10000)

    assert image.section(8) > 0xFFFF
    assert platform.event_action(image, 87) == actions[87]
    assert platform.event_action(image, 254) == actions[254]
    assert platform.event_action(image, 255) is None


def test_a_selected_truncated_action_fails_without_rejecting_an_unselected_tail():
    image = _image(b"\x03\x34\x12\x56")

    assert platform.event_action(image, 0) == platform.EventAction(0x1234, 0x56)
    assert platform.event_action(image, 3) is None
    with pytest.raises(ahcm.InvalidImage, match="crosses native image bounds"):
        platform.event_action(image, 1)


def test_event_selection_respects_an_explicit_linked_image_address_domain():
    image = _image(platform.encode_event_table([platform.EventAction(0x1234, 0x56)]))
    payload = bytearray(image.payload)
    linked_base = 0x18000
    payload[4:8] = (linked_base + image.end_offset).to_bytes(4, "little")
    pointer = ahcm.TABLE_OFFSET + 8 * 4 + 1
    payload[pointer:pointer + 3] = (linked_base + image.section(8)).to_bytes(3, "little")
    low, high = 0x21, 0x43
    for at in range(0, image.checksum_offset - 1, 2):
        low ^= payload[at]
        high ^= payload[at + 1]
    payload[image.checksum_offset:image.checksum_offset + 2] = bytes((low, high))
    linked = ahcm.Image(bytes(payload), logical_base=linked_base)

    assert platform.event_action(linked, 0) == platform.EventAction(0x1234, 0x56)


@pytest.mark.parametrize("value", [-1, 256, True, 1.5])
def test_invalid_host_selectors_are_not_silently_truncated(value):
    with pytest.raises(ValueError, match="event selector"):
        platform.event_action(_image(b"\x00"), value)


def test_encoding_retains_order_and_unknown_opcodes_without_inventing_defaults():
    assert platform.encode_event_table([]) == b"\x00"
    assert platform.encode_event_table(
        [platform.EventAction(0xABCD, 0xFA), platform.EventAction(0, 0)]
    ) == b"\x02\xcd\xab\xfa\x00\x00\x00"
    with pytest.raises(ValueError, match="count"):
        platform.encode_event_table(platform.EventAction(0, 0) for _ in range(256))
    with pytest.raises(TypeError, match="EventAction"):
        platform.encode_event_table([(0, 0)])


@pytest.mark.parametrize("operand, opcode", [(-1, 0), (0x10000, 0), (0, 256), (0, True)])
def test_out_of_width_host_actions_cannot_be_encoded_as_something_else(operand, opcode):
    with pytest.raises(ValueError, match="event (operand|opcode)"):
        platform.EventAction(operand, opcode)


def test_a_platform_handoff_does_not_enable_building_or_writing_the_read_only_550():
    assert platform.event_action(_image(b"\x01\x34\x12\x56"), 0) is not None
    profile = remotes.get("harmony-550")

    with pytest.raises(remotes.NotBuildable):
        profile.require_buildable()
    with pytest.raises(remotes.NotWritable):
        profile.require_writable()


def test_key_changes_report_old_release_before_new_press_without_inventing_a_key_name():
    changed = platform.key_transition(3, 5, emit_release=True)

    assert changed == platform.KeyChange(5, (0x43, 0x85), True, True)
    assert platform.key_transition(0, 5, emit_release=True) == (
        platform.KeyChange(5, (0x85,), False, True))
    assert platform.key_transition(5, 0, emit_release=True) == (
        platform.KeyChange(0, (0x45,), True, False))


def test_suppressed_release_events_still_clear_the_old_ir_held_source_on_change():
    assert platform.key_transition(3, 5, emit_release=False) == (
        platform.KeyChange(5, (0x85,), True, True))
    assert platform.key_transition(5, 0, emit_release=False) == (
        platform.KeyChange(0, (), True, False))


def test_a_stable_scan_is_not_an_invented_repeat_or_a_request_to_clear_ir_hold():
    assert platform.key_transition(5, 5, emit_release=True) == (
        platform.KeyChange(5, (), False, False))
    assert platform.key_transition(0, 0, emit_release=False) == (
        platform.KeyChange(0, (), False, False))


def test_raw_event_codes_are_not_masked_into_a_guessed_six_bit_key_map():
    assert platform.key_transition(64, 0, emit_release=True).events == (64,)
    assert platform.key_transition(0, 64, emit_release=False).events == (192,)


@pytest.mark.parametrize("old, new, release", [
    (-1, 0, True), (0, 256, True), (True, 1, False), (0, 0, 1),
])
def test_ingress_does_not_silently_truncate_invalid_codes_or_default_its_release_policy(
        old, new, release):
    with pytest.raises(ValueError):
        platform.key_transition(old, new, emit_release=release)
