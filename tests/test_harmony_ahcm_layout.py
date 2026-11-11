"""Native allocation tests prove framing and references, not 550 execution."""
from __future__ import annotations

import pytest

from afterglow import remotes
from afterglow.backends.harmony_ahcm import ahcm, backend, layout

IDENTITY = b"\x09\x09\x29\xa5\x00\xff"


def _layout(*, roots=None, header_value=0x1400):
    planned = {1: "identity", **(roots or {})}
    result = layout.Layout(header_value=header_value, spares=tuple(range(20)))
    for index in range(20):
        result.root(index, planned.get(index))
    result.add("identity", IDENTITY, owner="identity")
    return result


def test_repeated_assembly_is_byte_identical_with_explicit_fixed_input_bytes():
    planned = _layout(header_value=0x030201)
    planned.add("fixed-stamp", b"2026-01-01", owner="platform")

    first = planned.assemble(trailing=b"\x00\xfe")
    second = planned.assemble(trailing=b"\x00\xfe")
    image = ahcm.Image(first.payload)

    assert first == second
    assert image.header_value == 0x030201
    assert [slot.spare for slot in image.slots] == list(range(20))
    assert image.read(first.offset("identity"), len(IDENTITY)) == IDENTITY
    assert first.payload[image.end_offset + 4:] == b"\x00\xfe"
    assert first.report()["validation"]["hardware_acceptance"] == "not-established"


def test_an_assembly_seals_only_complete_pairs_with_the_native_checksum():
    planned = _layout()
    planned.add("odd-tail", b"\x01\x02", owner="platform")
    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)
    low, high = 0x21, 0x43
    for first, second in zip(assembled.payload[:image.checksum_offset:2],
                             assembled.payload[1:image.checksum_offset:2]):
        low ^= first
        high ^= second

    assert image.checksum_offset % 2 == 1
    assert assembled.payload[image.checksum_offset:image.end_offset] == bytes((low, high))
    assert assembled.payload[image.end_offset:image.end_offset + 4] == b"MCHA"
    assert image.end_vector == ahcm.LOGICAL_BASE + image.end_offset


def test_a_forward_reference_changes_only_its_declared_pointer_field():
    planned = _layout(roots={5: "record"})
    original = b"\xaa\xde\xad\xbe\xbb\xcc"
    planned.add("record", original, owner="commands")
    planned.relocate("record", 1, "waveform", target_offset=2, size=2)
    planned.add("waveform", b"\x10\x20\x30\x40", owner="signals")

    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)
    at = assembled.offset("record")

    assert image.pointer(at + 1, 2) == assembled.offset("waveform") + 2
    assert image.read(at, 1) == original[:1]
    assert image.read(at + 4, 2) == original[4:]
    assert image.section(5) == at


def test_unsorted_roots_and_shared_children_are_reported_as_references_not_section_ranges():
    planned = _layout(roots={5: "parent", 7: "child", 18: "parent"})
    planned.add("child", b"\x11\x22", owner="shared")
    planned.add("parent", bytes(6), owner="devices")
    planned.relocate("parent", 0, "child", size=2)
    planned.relocate("parent", 3, "child", size=2)

    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)
    report = assembled.report()

    assert image.section(5) > image.section(7)
    assert image.section(5) == image.section(18)
    assert image.section(0) is None
    assert image.section(19) is None
    assert report["ownership"] == "named-allocations-and-references"
    assert [item["target"] for item in report["references"]] == ["child", "child"]
    assert [item["address"] for item in report["references"]] == [
        assembled.address("child"), assembled.address("child")]
    assert report["section_roots"][18]["target"] == "parent"
    assert "section_ranges" not in report


def test_pointer_relocation_crosses_a_16_bit_file_offset_without_window_padding():
    planned = _layout(roots={5: "source"})
    planned.add("source", bytes(3), owner="commands")
    planned.add("padding", bytes(0x10000), owner="synthetic")
    planned.add("target", b"\x55\x66", owner="signals")
    planned.relocate("source", 0, "target", size=2)

    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)

    assert assembled.offset("source") == ahcm.HEADER_SIZE + len(IDENTITY)
    assert assembled.offset("target") == assembled.offset("source") + 3 + 0x10000
    assert image.pointer(assembled.offset("source"), 2) == assembled.offset("target")
    assert image.u24(assembled.offset("source")) == assembled.address("target")


def test_a_u24_target_reaches_its_exact_field_limit_and_refuses_overflow():
    planned = _layout(roots={5: "at-limit"})
    planned.add("source", bytes(3), owner="commands")
    target_offset = 0xFFFFFF - ahcm.LOGICAL_BASE
    planned.add("padding", bytes(target_offset - ahcm.HEADER_SIZE - len(IDENTITY) - 3),
                owner="synthetic")
    planned.add("at-limit", b"\x55", owner="synthetic")

    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)

    assert assembled.address("at-limit") == 0xFFFFFF
    assert image.slots[5].address == 0xFFFFFF
    assert assembled.report()["validation"]["hardware_acceptance"] == "not-established"
    planned.add("overflow", b"\x66", owner="synthetic")
    planned.relocate("source", 0, "overflow")
    with pytest.raises(ValueError, match="pointer to 'overflow'.*24-bit"):
        planned.assemble()


def test_a_zero_size_reference_can_name_the_end_of_its_allocation():
    planned = _layout()
    planned.add("source", bytes(3), owner="commands")
    planned.add("target", b"\x55", owner="signals")
    planned.relocate("source", 0, "target", target_offset=1, size=0)

    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)

    assert image.pointer(assembled.offset("source"), 0) == image.checksum_offset


def test_an_explicit_null_reference_is_encoded_without_a_target_allocation():
    planned = _layout()
    planned.add("source", b"\xff\xff\xff", owner="commands")
    planned.relocate("source", 0, None)

    assembled = planned.assemble()

    assert ahcm.Image(assembled.payload).pointer(assembled.offset("source")) is None


def test_an_assembly_never_opens_a_source_configuration(monkeypatch):
    planned = _layout()

    def reject_open(*_args, **_kwargs):
        raise AssertionError("native assembly must not read a previous configuration")

    with monkeypatch.context() as guarded:
        guarded.setattr("builtins.open", reject_open)
        assembled = planned.assemble()

    assert ahcm.validate(assembled.payload)["ok"] is True


def test_a_valid_native_assembly_does_not_enable_project_builds_or_remote_writes():
    assembled = _layout().assemble()
    profile = remotes.get("harmony-550")

    assert backend.validate_payload(assembled.payload, profile)["ok"] is True
    assert profile.status == remotes.READ_ONLY
    with pytest.raises(remotes.NotBuildable):
        backend.build_project({}, profile, None)
    with pytest.raises(remotes.NotWritable):
        profile.require_writable()


@pytest.mark.parametrize("value", [True, -1, 0x1000000, "0x1400"])
def test_invalid_header_values_are_refused_without_truncation(value):
    with pytest.raises(ValueError, match="header value"):
        layout.Layout(header_value=value, spares=[0] * 20)


@pytest.mark.parametrize("spares", [[0] * 19, [0] * 21, [True] * 20, [-1] * 20, [256] * 20])
def test_all_twenty_spare_bytes_must_be_supplied_in_their_native_width(spares):
    with pytest.raises(ValueError, match="spare"):
        layout.Layout(header_value=0x1400, spares=spares)


def test_undeclared_section_roots_are_not_silently_filled_in():
    planned = layout.Layout(header_value=0x1400, spares=[0] * 20)
    planned.root(19, None)

    with pytest.raises(ValueError, match="roots must be declared explicitly; missing"):
        planned.assemble()


@pytest.mark.parametrize("index", [True, -1, 20])
def test_section_indices_outside_the_native_table_are_refused(index):
    planned = layout.Layout(header_value=0x1400, spares=[0] * 20)
    with pytest.raises(ValueError, match="section index"):
        planned.root(index, None)


def test_a_section_root_cannot_be_overwritten_by_a_later_declaration():
    planned = _layout()
    with pytest.raises(ValueError, match="section 1 root is already declared"):
        planned.root(1, None)
    assert ahcm.Image(planned.assemble().payload).identity["skin"] == 41


@pytest.mark.parametrize(("name", "body", "owner", "exception"), [
    ("", b"\x01", "signals", ValueError),
    ("signal", b"\x01", "", ValueError),
    ("signal", bytearray(b"\x01"), "signals", TypeError),
])
def test_allocations_require_names_owners_and_immutable_input_bytes(name, body, owner, exception):
    with pytest.raises(exception):
        _layout().add(name, body, owner=owner)


def test_a_duplicate_allocation_does_not_replace_its_original_bytes():
    planned = _layout()
    with pytest.raises(ValueError, match="allocation 'identity' is already present"):
        planned.add("identity", b"\x08\x08\x29", owner="replacement")
    assembled = planned.assemble()
    image = ahcm.Image(assembled.payload)
    assert image.read(assembled.offset("identity"), len(IDENTITY)) == IDENTITY


def test_unknown_reference_targets_are_reported_and_can_be_supplied_before_retry():
    planned = _layout()
    planned.add("source", bytes(3), owner="commands")
    planned.relocate("source", 0, "missing")
    with pytest.raises(ValueError, match="unknown reference target 'missing'"):
        planned.assemble()
    planned.add("missing", b"\x55", owner="signals")
    assembled = planned.assemble()
    assert ahcm.Image(assembled.payload).pointer(assembled.offset("source")) == assembled.offset(
        "missing")


def test_unknown_relocation_sources_are_reported_before_registering_a_pointer():
    with pytest.raises(ValueError, match="unknown relocation source 'missing'"):
        _layout().relocate("missing", 0, None)


@pytest.mark.parametrize(("offset", "size"), [(True, 1), (-1, 1), (1, 1), (0, -1), (0, True)])
def test_pointer_fields_and_span_sizes_are_checked_without_partial_registration(offset, size):
    planned = _layout()
    planned.add("source", bytes(3), owner="commands")
    with pytest.raises(ValueError):
        planned.relocate("source", offset, None, size=size)
    planned.relocate("source", 0, None)
    assert ahcm.Image(planned.assemble().payload).pointer(
        ahcm.HEADER_SIZE + len(IDENTITY)) is None


@pytest.mark.parametrize("offset", [0, 1, 2])
def test_overlapping_relocations_cannot_claim_the_same_pointer_bytes(offset):
    planned = _layout()
    planned.add("source", bytes(6), owner="commands")
    planned.relocate("source", 0, None)
    with pytest.raises(ValueError, match="overlaps another"):
        planned.relocate("source", offset, None)


def test_a_reference_cannot_read_past_its_named_allocation():
    planned = _layout()
    planned.add("source", bytes(3), owner="commands")
    planned.add("target", b"\x55", owner="signals")
    planned.add("unrelated", b"\x66", owner="other")
    planned.relocate("source", 0, "target", size=2)
    with pytest.raises(ValueError, match="referenced span.*crosses 'target'"):
        planned.assemble()


def test_a_subsystem_root_cannot_point_at_an_empty_allocation():
    planned = _layout(roots={5: "empty"})
    planned.add("empty", b"", owner="synthetic")
    with pytest.raises(ValueError, match="referenced span.*crosses 'empty'"):
        planned.assemble()


@pytest.mark.parametrize("target_offset", [True, -1, 1])
def test_a_null_reference_cannot_carry_an_invalid_target_offset(target_offset):
    planned = layout.Layout(header_value=0x1400, spares=[0] * 20)
    with pytest.raises(ValueError, match="target offset"):
        planned.root(0, None, target_offset=target_offset)


def test_mutable_bytes_after_the_native_end_marker_are_refused():
    with pytest.raises(TypeError, match="end marker.*immutable"):
        _layout().assemble(trailing=bytearray(b"\x00"))


def test_native_validation_rejects_an_inconsistent_identity_in_declared_bytes():
    planned = layout.Layout(header_value=0x1400, spares=[0] * 20)
    for index in range(20):
        planned.root(index, "identity" if index == 1 else None)
    planned.add("identity", b"\x08\x08\x29", owner="identity")
    with pytest.raises(ahcm.InvalidImage, match="architecture 9"):
        planned.assemble()


def test_looking_up_an_unallocated_structure_reports_its_name():
    with pytest.raises(KeyError, match="unknown allocation 'missing'"):
        _layout().assemble().offset("missing")
