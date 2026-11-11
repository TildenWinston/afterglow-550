"""Navigation records and callback flow use invented data, not physical key assumptions."""
from __future__ import annotations

import struct

import pytest

from afterglow.backends.harmony_ahcm import ahcm, layout, modes
from afterglow.backends.harmony_ahcm.platform import EventAction
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative
from afterglow.backends.harmony_ahcm.runtime import ModeState, Paint, Request, Runtime


def _image(*, count=2, first_kind=0, wide=False, noop=False):
    plan = layout.Layout(header_value=0x1400, spares=[0] * 20)
    plan.add("states", struct.pack("<4H", 1, 1, 0, 1) + bytes(3), owner="states")
    plan.add("state", bytes(7), owner="states")
    plan.relocate("states", 8, "state", size=7)
    selected = []
    for mode in range(2):
        prefix = f"mode-{mode}"
        bindings = modes.add_bindings(
            plan, f"{prefix}-bindings",
            [modes.Binding(tag, EventAction(0x0300 + tag, 0x7D)) for tag in (6, 7, 41, 42)],
            wide=False)
        pages = []
        for page in range(count):
            program = f"{prefix}-program-{page}"
            plan.add(program, b"\x17", owner="programs")
            page_bindings = modes.add_bindings(
                plan, f"{prefix}-bindings-{page}",
                [modes.Binding(41, EventAction(0 if noop else 0x0340 + page,
                                               0 if noop else 0x7D),
                               flags=int(wide))], wide=wide)
            pages.append(modes.add_page(plan, f"{prefix}-page-{page}",
                                        bindings=page_bindings, program=program))
        selected.append(modes.add_mode(plan, prefix, kind=first_kind if mode == 0 else 0,
                                       bindings=bindings, pages=pages))
    index = modes.add_mode_index(plan, "mode-index", selected)
    for slot in range(20):
        plan.root(slot, {6: index, 13: "states"}.get(slot))
    assembly = plan.assemble()
    return Reader(ahcm.Image(assembly.payload)), assembly


def _runtime(reader, *, mode_state=ModeState(None, None), paint=lambda request: None):
    return Runtime(reader, values=[0], context=None, accept=lambda request: True,
                   mode_state=mode_state, paint=paint)


def test_generated_navigation_records_follow_named_references_without_a_template():
    reader, assembly = _image()
    mode = reader.mode(0)

    assert mode["kind"] == 0
    assert [page["ordinal"] for page in mode["pages"]] == [0, 1]
    assert mode["pages"][1]["program"] == assembly.offset("mode-0-program-1")
    assert mode["bindings"][0] == {"flags": 0, "tag": 6, "operand": 0x0306, "opcode": 0x7D}
    assert _image()[1].payload == assembly.payload


def test_same_mode_selection_runs_leave_and_enter_callbacks_instead_of_deduplicating():
    reader, assembly = _image()
    runtime = _runtime(reader)

    first = runtime.run([(0x7E, 0)])
    repeated = runtime.run([(0x7E, 0)])

    assert first.requests == (Request("send", 3, 6), Request("send", 3, 0x40))
    assert repeated.requests == tuple(Request("send", 3, value)
                                      for value in (42, 7, 6, 0x40))
    assert repeated.mode_state == ModeState(0, 0)
    assert repeated.paints == (Paint(0, 0, assembly.address("mode-0-program-0")),)


def test_page_steps_wrap_and_invalid_direct_selection_still_redraws_the_current_page():
    reader, assembly = _image()
    runtime = _runtime(reader, mode_state=ModeState(0, 0))

    previous = runtime.run([(0x0F, 0xA0)])
    invalid = runtime.run([(0x76, 99)])
    following = runtime.run([(0x0F, 0xA1)])

    assert previous.mode_state == invalid.mode_state == ModeState(0, 1)
    assert previous.requests == invalid.requests == (
        Request("send", 3, 42), Request("send", 3, 0x41))
    assert invalid.paints == (Paint(0, 1, assembly.address("mode-0-program-1")),)
    assert following.mode_state == ModeState(0, 0)


def test_wide_page_fallthrough_requests_the_mode_action_but_a_zero_narrow_match_stops_it():
    reader, _ = _image(wide=True)
    fallthrough = _runtime(reader).run([(0x7E, 0)])
    reader, _ = _image(noop=True)
    terminated = _runtime(reader).run([(0x7E, 0)])

    assert fallthrough.requests == tuple(Request("send", 3, value) for value in (6, 0x40, 41))
    assert terminated.requests == (Request("send", 3, 6),)


def test_kind_bit_suppresses_only_initial_paint_not_explicit_page_paint():
    reader, assembly = _image(first_kind=1)
    runtime = _runtime(reader)

    selected = runtime.run([(0x7E, 0)])
    page = runtime.run([(0x76, 1)])

    assert selected.paints == ()
    assert page.paints == (Paint(0, 1, assembly.address("mode-0-program-1")),)


def test_paint_callback_failure_stops_the_instance_without_claiming_completed_navigation():
    def failed(request):
        raise RuntimeError("synthetic renderer unavailable")

    reader, _ = _image()
    runtime = _runtime(reader, paint=failed)

    with pytest.raises(RuntimeError, match="renderer unavailable"):
        runtime.run([(0x7E, 0)])
    with pytest.raises(UnsupportedNative, match="stopped previously"):
        runtime.run()


def test_mode_state_and_renderer_are_not_inferred_for_navigation():
    reader, _ = _image()
    runtime = Runtime(reader, values=[0], context=None, accept=lambda request: True)

    with pytest.raises(UnsupportedNative, match="initial mode state"):
        runtime.run([(0x7E, 0)])
    with pytest.raises(TypeError, match="explicit paint"):
        _runtime(reader, paint=None)


def test_page_steps_carry_and_borrow_across_a_byte_without_truncating_the_page_count():
    reader, _ = _image(count=257)
    runtime = _runtime(reader, mode_state=ModeState(0, 255))

    carried = runtime.run([(0x0F, 0xA1)])
    wrapped = runtime.run([(0x0F, 0xA1)])
    borrowed = runtime.run([(0x0F, 0xA0)])

    assert carried.mode_state == borrowed.mode_state == ModeState(0, 256)
    assert wrapped.mode_state == ModeState(0, 0)


def test_invalid_binding_layouts_fail_before_allocating_a_partial_record():
    plan = layout.Layout(header_value=0x1400, spares=[0] * 20)
    with pytest.raises(ValueError, match="empty"):
        modes.add_bindings(plan, "bindings", [], wide=False)
    with pytest.raises(ValueError, match="flags"):
        modes.add_bindings(plan, "bindings",
                           [modes.Binding(1, EventAction(0, 0), flags=1)], wide=False)
    modes.add_bindings(plan, "bindings", [], wide=True)
