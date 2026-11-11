"""Source-owned components compose without claiming a complete platform or boot image."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from afterglow import ir_signal, project_devices, remotes
from afterglow.backends.harmony_ahcm import (
    ahcm, backend, importing, ir_emit, ir_runtime, layout, modes, platform, screens,
)
from afterglow.backends.harmony_ahcm.readers import Reader
from afterglow.backends.harmony_ahcm.runtime import KeyState, ModeState, Paint, Request, Runtime


def _components():
    device = {
        "schema": project_devices.SCHEMA, "id": "synthetic", "label": "Synthetic device",
        "commands": [["Short", ""], ["Long", ""]],
        "signals": {
            "Short": ir_signal.waveform([560, -560, 1120, -560], carrier_hz=40000),
            "Long": ir_signal.waveform([9000, -4500, 560, -32768], carrier_hz=40000),
        },
    }
    project_devices.validate(device, allow_opaque=False)
    plan = layout.Layout(header_value=0x1400, spares=[0] * 20)
    plan.add("identity", bytes((9, 9, 41)), owner="synthetic-identity")
    group = ir_emit.lower_group(
        plan, "device", {name: ir_emit.Command(device["signals"][name], 25000, 12500, 0)
                         for name, _ in device["commands"]},
        command_order=["Short", "Long"], spare=0)
    ir_emit.group_index(plan, "groups", [group])
    font = screens.add_font(plan, "font", first_code=1,
                            glyphs=[[[1], [1], [1]], [[1], [0], [1]]])
    fonts = screens.add_font_index(plan, "fonts", [font])
    mode_bindings = modes.add_bindings(
        plan, "mode-bindings",
        [modes.Binding(0x81, platform.EventAction(0xA1, 0x0F))], wide=False)
    pages = []
    for ref, code in zip(group.commands, (1, 2)):
        bindings = modes.add_bindings(
            plan, f"page-bindings-{ref.ordinal}",
            [modes.Binding(0x82, platform.EventAction(ref.ordinal, 0x7D))], wide=False)
        program = f"program-{ref.ordinal}"
        plan.add(program, screens.encode_text_program(
            display_page=0, font_index=0, lines=[screens.TextLine(0, 0, bytes((code,)))]),
            owner="synthetic-programs")
        pages.append(modes.add_page(plan, f"page-{ref.ordinal}",
                                    bindings=bindings, program=program))
    mode = modes.add_mode(plan, "device-mode", kind=0, bindings=mode_bindings, pages=pages)
    index = modes.add_mode_index(plan, "modes", [mode])
    keys = modes.add_key_sets(plan, "keys", [])
    plan.add("events", platform.encode_event_table([
        platform.EventAction(0, 0), platform.EventAction(0, 0x7F),
        platform.EventAction(0, 0), platform.EventAction(0, 0),
        platform.EventAction(0, 0x7F)]), owner="synthetic-events")
    plan.add("actions", b"\x01\x00" + bytes(3), owner="synthetic-actions")
    plan.add("enter", b"\x02" + struct.pack("<HBHB", 0xFEFD, 0x1F, 0, 0x7E),
             owner="synthetic-actions")
    plan.relocate("actions", 2, "enter")
    plan.add("states", bytes(8), owner="synthetic-states")
    for slot in range(20):
        if slot != 5:
            plan.root(slot, {1: "identity", 6: index, 7: fonts, 8: "events",
                             9: keys, 10: "actions", 13: "states"}.get(slot))
    return plan.assemble(), device


def test_source_owned_navigation_and_one_device_have_deterministic_independently_readable_shape():
    assembly, device = _components()
    reader = Reader(ahcm.Image(assembly.payload))

    assert _components()[0].payload == assembly.payload
    group, = reader.addresses(5, 1)
    commands = reader.group(group)["commands"]
    assert len(commands) == len(device["commands"])
    for address, (name, _) in zip(commands, device["commands"]):
        decoded = importing.waveform(reader.command(address), {})
        assert decoded["pulses_us"] == device["signals"][name]["pulses_us"]
        assert decoded["carrier_hz"] == device["signals"][name]["carrier_hz"]
    mode = reader.mode(0)
    assert len(mode["pages"]) == 2
    for page, code in zip(mode["pages"], ("01", "02")):
        instructions = reader.screen(page["program"])
        assert [item["opcode"] for item in instructions] == [22, 16, 5]
        assert instructions[0]["operands_hex"] == "00"
        assert instructions[2]["codes_hex"] == code
    assert reader.inspect()["rejected"] == {}


@pytest.mark.parametrize("selector", [1, 4])
def test_raw_event_handoff_routes_generated_pages_to_the_lowered_portable_command(selector):
    assembly, device = _components()
    reader = Reader(ahcm.Image(assembly.payload))
    runtime = Runtime(reader, values=[], context=None, accept=lambda request: True,
                      mode_state=ModeState(None, None), paint=lambda request: None,
                      keys=KeyState((), None, False))
    selected = platform.event_action(reader.image, selector)
    assert selected is not None
    entered = runtime.run([(selected.opcode, selected.operand)])
    assert entered.paints == (Paint(0, 0, assembly.address("program-0")),)
    assert entered.keys == KeyState((0xFD,), None, False)

    change = platform.key_transition(0, 1, emit_release=False)
    navigation = runtime.run([(0x1F, 0xFC00 | event) for event in change.events])
    assert navigation.mode_state == ModeState(0, 1)
    assert navigation.paints == (Paint(0, 1, assembly.address("program-1")),)

    change = platform.key_transition(change.active_key, 2, emit_release=True)
    sent = runtime.run([(0x1F, 0xFC00 | event) for event in change.events])
    assert sent.requests == (Request("send", 0, 1),)
    address = reader.group(reader.addresses(5, 1)[0])["commands"][1]
    stream = ir_runtime.Class5Stream.from_command(reader, address, recording=0)
    press = stream.step(queued_bytes=0, held=False)
    decoded, leadin = importing._phase(list(press.words), press=True)
    assert decoded == device["signals"]["Long"]["pulses_us"] and leadin == 0
    assert stream.step(queued_bytes=press.queued_bytes, held=False).state == "release"
    assert stream.step(queued_bytes=press.queued_bytes, held=False).state == "done"


def test_component_integration_does_not_grant_whole_platform_or_transmitter_authority():
    assembly, device = _components()
    profile = remotes.get("harmony-550")

    assert ahcm.Image(assembly.payload).report()["hardware_acceptance"] == "not-established"
    assert backend.capability(device["signals"]["Short"], profile)["supported"] is False
    with pytest.raises(remotes.NotBuildable):
        backend.build_project({"devices": [device]}, profile, None)


def test_component_construction_has_no_access_to_a_donor_or_other_files(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("source-owned construction must not read a previous configuration")

    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", forbidden)
        guard.setattr(Path, "open", forbidden)
        assembly, _ = _components()

    assert assembly.payload.startswith(ahcm.MAGIC)
