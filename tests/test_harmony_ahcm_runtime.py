"""Synthetic action/state scenarios do not require a donor or claim peripheral execution."""
from __future__ import annotations

import struct
from itertools import repeat

import pytest

from afterglow.backends.harmony_ahcm import ahcm, layout
from afterglow.backends.harmony_ahcm.platform import EventAction, encode_event_table, event_action
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative
from afterglow.backends.harmony_ahcm.runtime import Request, Runtime


def _action(opcode, operand):
    return struct.pack("<HB", operand, opcode)


def _transition(before, after, opcode, operand, *, lead=0):
    return struct.pack("<Bhh", lead, before, after) + _action(opcode, operand)


def _reader(states, actions=(), contexts=(), *, narrow=None, events=None):
    plan = layout.Layout(header_value=0x1400, spares=[0] * 20)
    narrow = len(states) if narrow is None else narrow
    plan.add("states", struct.pack("<4H", len(states), narrow, len(states) - narrow, narrow)
             + bytes(3 * len(states)), owner="states")
    for ordinal, transitions in enumerate(states):
        body = struct.pack("<HH", 0, 7) + len(transitions).to_bytes(3, "little")
        plan.add(f"state-{ordinal}", body + b"".join(transitions), owner="states")
        plan.relocate("states", 8 + 3 * ordinal, f"state-{ordinal}", size=7)
    plan.add("actions", len(actions).to_bytes(2, "little") + bytes(3 * len(actions)),
             owner="actions")
    for ordinal, items in enumerate(actions):
        plan.add(f"action-{ordinal}", bytes((len(items),))
                 + b"".join(_action(*item) for item in items), owner="actions")
        plan.relocate("actions", 2 + 3 * ordinal, f"action-{ordinal}")
    plan.add("contexts", bytes((len(contexts),)) + bytes(3 * len(contexts)), owner="contexts")
    for ordinal, entries in enumerate(contexts):
        plan.add(f"context-{ordinal}", bytes((len(entries),))
                 + b"".join(bytes((tag,)) + _action(opcode, operand)
                            for tag, opcode, operand in entries), owner="contexts")
        plan.relocate("contexts", 1 + 3 * ordinal, f"context-{ordinal}")
    if events is not None:
        plan.add("events", encode_event_table(events), owner="events")
    for slot in range(20):
        target = {9: "contexts", 10: "actions", 13: "states"}.get(slot)
        plan.root(slot, "events" if slot == 8 and events is not None else target)
    return Reader(ahcm.Image(plan.assemble().payload))


def _runtime(reader, values, *, context=None, accept=lambda request: True):
    return Runtime(reader, values=values, context=context, accept=accept)


def test_nested_lists_and_state_actions_precede_their_queued_continuation():
    reader = _reader(
        [[_transition(0, 1, 0x7D, 0x0307)]],
        [[(0x7F, 1), (0x7D, 0x0309)], [(0x80, 1), (0x7D, 0x0308)]])
    result = _runtime(reader, [0]).run([(0x7F, 0), (0x7D, 0x030A)])

    assert result.requests == tuple(Request("send", 3, value) for value in (7, 8, 9, 10))
    assert result.values == (1,)
    assert not result.pending


def test_first_repeated_and_shared_device_entry_preserve_power_and_change_only_the_input():
    reader = _reader(
        [[_transition(0, 1, 0x7D, 0x0201)],
         [_transition(-3, 1, 0x7D, 0x0202), _transition(-3, 2, 0x7D, 0x0203)]],
        [[(0x80, 1), (0x81, 1)], [(0x80, 1), (0x81, 2)]],
        [[(1, 0x7F, 0), (2, 0x7D, 0x0204), (5, 0x7D, 0x0205)], [(1, 0x7F, 1)]])
    runtime = _runtime(reader, [0, 0])

    first = runtime.run([(0x1F, 0xFF00)])
    repeated = runtime.run([(0x1F, 0xFF00)])
    changed = runtime.run([(0x1F, 0xFF01)])

    assert first.requests == (Request("send", 2, 1), Request("send", 2, 2))
    assert repeated.requests == (Request("send", 2, 5),)
    assert changed.requests == (Request("send", 2, 4), Request("send", 2, 3))
    assert changed.values == (1, 2)
    assert changed.context == 1


def test_an_explicit_off_plan_uses_transitions_without_claiming_the_platform_off_key():
    reader = _reader([[_transition(1, 0, 0x7D, 0x0402)],
                      [_transition(-3, 0, 0x7D, 0x0403)]],
                     [[(0x80, 0), (0x81, 0)]])
    result = _runtime(reader, [1, 2]).run([(0x7F, 0)])

    assert result.requests == (Request("send", 4, 2), Request("send", 4, 3))
    assert result.values == (0, 0)


def test_a_matching_terminal_transition_stops_but_an_unmatched_one_does_not():
    reader = _reader([[
        _transition(1, 2, 0x7D, 1, lead=0x80),
        _transition(-2, -3, 0x7D, 2, lead=0x80),
        _transition(-2, -2, 0x7D, 3)]])
    result = _runtime(reader, [0]).run([(0x80, 2)])

    assert result.requests == (Request("send", 0, 2),)


def test_exact_equal_value_transitions_are_not_silently_deduplicated():
    reader = _reader([[_transition(1, 1, 0x7D, 9)]])
    result = _runtime(reader, [1]).run([(0x80, 1)])

    assert result.requests == (Request("send", 0, 9),)


def test_a_narrow_cache_truncates_storage_but_matches_the_full_requested_value():
    reader = _reader([[_transition(0, 0x101, 0x7D, 5)], []], narrow=1)
    result = _runtime(reader, [0, 0]).run([(0x80, 0x101), (0x81, 0x1234)])

    assert result.values == (1, 0x1234)
    assert result.requests == (Request("send", 0, 5),)


def test_suppression_changes_one_cache_value_without_sending_or_suppressing_the_next_write():
    reader = _reader([[], [_transition(0, 1, 0x7D, 8), _transition(1, 0, 0x7D, 9)]])
    runtime = _runtime(reader, [0, 0])

    silent = runtime.run([(7, 255), (0x81, 1)])
    following = runtime.run([(0x81, 0)])

    assert silent.values == (0, 1)
    assert silent.requests == ()
    assert following.requests == (Request("send", 0, 9),)


@pytest.mark.parametrize("opcode,kind", [(0x7D, "send"), (0x7C, "device-delay")])
def test_an_unaccepted_boundary_keeps_the_same_request_pending_without_advancing(opcode, kind):
    accepted = False
    reader = _reader([[]])
    runtime = _runtime(reader, [0], accept=lambda request: accepted)

    blocked = runtime.run([(opcode, 0x0506), (0x80, 1)])
    accepted = True
    resumed = runtime.run()

    assert blocked.pending and blocked.values == (0,)
    assert blocked.requests == (Request(kind, 5, 6),)
    assert resumed.requests == blocked.requests
    assert not resumed.pending and resumed.values == (1,)


@pytest.mark.parametrize("instruction,match", [
    ((0x75, 1), "not modeled"),
    ((0x1F, 0xFC01), "asynchronous"),
    ((7, 255), "state-zero"),
])
def test_unsupported_effects_fail_explicitly_and_cannot_be_resumed_as_success(instruction, match):
    runtime = _runtime(_reader([[]]), [0])
    actions = [instruction, (0x80, 1)] if instruction[0] == 7 else [instruction]

    with pytest.raises(UnsupportedNative, match=match):
        runtime.run(actions)
    with pytest.raises(UnsupportedNative, match="stopped previously"):
        runtime.run()


def test_complex_transitions_are_not_replaced_with_ordinary_predicates():
    runtime = _runtime(_reader([[_transition(-2, -2, 0x7D, 1, lead=1)]]), [0])

    with pytest.raises(UnsupportedNative, match="complex"):
        runtime.run([(0x80, 1)])


def test_recursion_and_queue_overflow_cannot_produce_a_complete_behavior_report():
    recursive = _runtime(_reader([[]], [[(0x7F, 0)]]), [0])
    overflowing = _runtime(_reader([[]], [[(0x7D, 1)] * 41]), [0])

    with pytest.raises(UnsupportedNative, match="step budget"):
        recursive.run([(0x7F, 0)], max_steps=9)
    with pytest.raises(UnsupportedNative, match="discard work"):
        overflowing.run([(0x7F, 0)])


def test_missing_initial_cache_values_and_unmodeled_storage_aliases_are_not_defaulted():
    with pytest.raises(ValueError, match="every declared"):
        _runtime(_reader([[]]), [])
    with pytest.raises(UnsupportedNative, match="aliases"):
        _runtime(_reader([[]] * 65), [0] * 65)


def test_a_failed_peripheral_callback_cannot_lose_pending_work_and_then_report_success():
    def failed(request):
        raise RuntimeError("synthetic peripheral unavailable")

    runtime = _runtime(_reader([[]]), [0], accept=failed)

    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.run([(0x7D, 1), (0x80, 1)])
    with pytest.raises(UnsupportedNative, match="stopped previously"):
        runtime.run()


def test_an_unbounded_ingress_stream_stops_at_an_inspector_budget():
    runtime = _runtime(_reader([[]]), [0])

    with pytest.raises(UnsupportedNative, match="ingress inspector budget"):
        runtime.run(repeat((0, 0)))


def test_a_selected_platform_event_hands_off_raw_actions_without_claiming_boot_or_ir_completion():
    reader = _reader([[]], [[(0x80, 1), (0x7D, 0x0207)]],
                     events=[EventAction(0, 0), EventAction(0, 0x7F)])
    selected = event_action(reader.image, 1)
    runtime = _runtime(reader, [0], accept=lambda request: False)

    assert selected is not None
    result = runtime.run([(selected.opcode, selected.operand)])

    assert result.values == (1,)
    assert result.requests == (Request("send", 2, 7),)
    assert result.pending


@pytest.mark.parametrize("initial,delta,expected", [(0, -1, 7), (7, 1, 0), (3, 1, 4)])
def test_relative_input_steps_wrap_through_the_declared_maximum(initial, delta, expected):
    reader = _reader([[_transition(-3, expected, 0x7D, 5)]])
    runtime = _runtime(reader, [initial])
    result = runtime.run([(0x1F, 0xF200 if delta == 1 else 0xF100)])

    assert result.values == (expected,)
    assert result.requests == (Request("send", 0, 5),)


def test_relative_adjustment_does_not_invent_wrapping_for_an_out_of_range_cache():
    runtime = _runtime(_reader([[]]), [8])

    with pytest.raises(UnsupportedNative, match="outside its declared range"):
        runtime.run([(0x1F, 0xF200)])
