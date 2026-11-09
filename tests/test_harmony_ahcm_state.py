"""State storage boundaries use synthetic records without firmware or household data."""
from __future__ import annotations

import base64
import struct

import pytest

from afterglow import remotes
from afterglow.backends.harmony_ahcm import ahcm, importing, layout, readers
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative


def _record(*transitions, count=None):
    count = len(transitions) if count is None else count
    return struct.pack("<HH", 0x1234, 5) + count.to_bytes(3, "little") + b"".join(transitions)


def _transition(lead=0, before=-2, after=1, operand=0x1234, opcode=0x7F):
    return struct.pack("<BhhHB", lead, before, after, operand, opcode)


def _image(*records):
    planned = layout.Layout(header_value=0x1400, spares=[0] * 20)
    planned.add("identity", b"\x09\x09\x29", owner="identity")
    planned.add("state-index", struct.pack("<4H", len(records), len(records), 0, len(records))
                + bytes(3 * len(records)), owner="states")
    for ordinal, record in enumerate(records):
        planned.add(f"state-{ordinal}", record, owner="states")
    for ordinal in range(len(records)):
        planned.relocate("state-index", 8 + 3 * ordinal, f"state-{ordinal}", size=7)
    for index in range(20):
        planned.root(index, "identity" if index == 1 else "state-index" if index == 13 else None)
    return ahcm.Image(planned.assemble().payload)


def test_the_high_byte_of_a_transition_count_is_not_an_opaque_header_field():
    reader = Reader(_image(_record(count=0x10000)))

    with pytest.raises(ahcm.InvalidImage, match="crosses"):
        reader.state(0)


def test_all_declared_transitions_are_read_past_a_16_bit_count_boundary():
    count = 0x10000
    body = struct.pack("<HH", 0x1234, 5) + count.to_bytes(3, "little")
    reader = Reader(_image(body + _transition() * count))

    state = reader.state(0)

    assert state["transition_count"] == count
    assert len(state["transitions"]) == count
    assert state["first"] == 0x1234
    assert state["maximum"] == 5
    assert state["transitions"][0] == state["transitions"][-1]
    assert state["transitions"][-1]["operand"] == 0x1234


def test_state_transition_work_consumes_an_inspector_budget_not_a_hardware_limit(monkeypatch):
    reader = Reader(_image(_record(_transition(), _transition())))
    monkeypatch.setattr(readers, "OBJECT_BUDGET", 2)

    with pytest.raises(UnsupportedNative, match="inspector budget"):
        reader.state(0)


@pytest.mark.parametrize("lead", [0, 1, 2, 0x80, 0x81, 0x82])
def test_established_state_layouts_retain_their_flags_and_raw_action(lead):
    reader = Reader(_image(_record(_transition(lead, -3, -2, 0xFEDC, 0x7D))))

    state = reader.state(0)

    assert state["transition_count"] == 1
    assert state["transitions"] == [
        {"lead": lead, "from": -3, "to": -2, "operand": 0xFEDC, "opcode": 0x7D}]


@pytest.mark.parametrize("lead", [3, 4, 0x83, 0xFF])
def test_an_unestablished_transition_layout_is_not_read_as_a_fixed_size_record(lead):
    reader = Reader(_image(_record(_transition(lead), _transition())))

    with pytest.raises(UnsupportedNative, match="unestablished record span"):
        reader.state(0)


def test_an_unknown_span_stops_before_claiming_following_bytes_as_a_state():
    reader = Reader(_image(_record(b"\x03")))

    with pytest.raises(UnsupportedNative, match="unestablished record span"):
        reader.state(0)

    prefixes = [region for (kind, _), region in reader.regions.items()
                if kind == "state-known-prefix"]
    assert [region["size"] for region in prefixes] == [8]
    assert not any(kind == "state" for kind, _ in reader.regions)


def test_an_unreadable_state_does_not_discard_its_siblings_or_native_evidence():
    image = _image(_record(_transition(3)), _record(_transition()))
    imported = importing.Importer(image, remotes.get("harmony-550"))

    imported.read_states()
    result = imported.result({})

    assert 0 not in imported.states
    assert 1 in imported.states
    assert result.rejected["unreadable_states"] == 1
    assert base64.b64decode(result.preservation["payload_base64"]) == image.payload
    assert any("unestablished record span" in note for note in result.warnings)
