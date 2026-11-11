"""Synthetic references separate nominal IR storage execution from hardware claims."""
from __future__ import annotations

import pytest

from afterglow import ir_signal, remotes
from afterglow.backends.harmony_ahcm import ahcm, backend, ir_runtime
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative
from test_harmony_ahcm_import import Native, pointer


@pytest.mark.parametrize(("period", "on", "register", "duty", "wrapped"), [
    (25000, 12500, 99, 50, False),
    (26315, 13157, 104, 53, False),
    (374, 125, 0, 1, False),
    (375, 124, 1, 0, False),
    (124, 62, 255, 0, True),
    (64000, 32000, 255, 128, False),
    (64125, 32062, 0, 128, True),
    (65535, 32767, 255, 131, True),
])
def test_nominal_carrier_rounding_exposes_register_wrap_instead_of_claiming_a_frequency(
        period, on, register, duty, wrapped):
    result = ir_runtime.carrier_registers(period, on)

    assert result.period_register == register
    assert result.duty_register == duty
    assert result.arithmetic_wrapped is wrapped
    assert result.fields_truncated is False


def test_wide_carrier_fields_expose_their_truncation_before_nominal_conversion():
    result = ir_runtime.carrier_registers(0x10000 + 25000, 0x20000 + 12500)

    assert (result.period_register, result.duty_register) == (99, 50)
    assert result.fields_truncated


@pytest.mark.parametrize(("count", "queued", "admitted", "wrapped"), [
    (0, 254, True, False),
    (1, 252, True, False),
    (1, 253, False, False),
    (2, 250, True, False),
    (2, 251, False, False),
    (127, 0, True, False),
    (127, 2, False, False),
    (128, 0, False, False),
    (32768, 0, True, True),
    (32769, 252, True, True),
    (65535, 0, False, True),
])
def test_symbol_admission_is_state_dependent_and_retains_the_large_count_wrap_caveat(
        count, queued, admitted, wrapped):
    result = ir_runtime.symbol_admission(count, queued)

    assert result.admitted is admitted
    assert result.counter_wrapped is wrapped
    assert result.required_bytes == 2 * count


def test_a_blocked_symbol_retries_whole_without_losing_or_duplicating_its_first_word():
    stream = ir_runtime.Class5Stream([[(0x8111, 0x0222)], None, None])

    blocked = stream.step(queued_bytes=251, held=False)
    accepted = stream.step(queued_bytes=0, held=False)

    assert not blocked.progressed and blocked.words == ()
    assert blocked.queued_bytes == 251
    assert accepted.words == (0x8111, 0x0222)
    assert accepted.queued_bytes == 4
    assert stream.step(queued_bytes=4, held=False).state == "release"


def test_a_wrapped_guard_is_not_mistaken_for_a_safe_symbol_to_stream():
    stream = ir_runtime.Class5Stream([[(0x8111,) * 32768], None, None])

    with pytest.raises(UnsupportedNative, match="wrapped symbol admission"):
        stream.step(queued_bytes=0, held=False)


def test_releasing_before_the_press_boundary_skips_hold_but_keeps_release():
    stream = ir_runtime.Class5Stream([[(0x8111,)], [(0x8222,)], [(0x8333,)]])

    steps = [stream.step(queued_bytes=0, held=False) for _ in range(5)]

    assert [step.words for step in steps] == [(0x8111,), (), (0x8333,), (0,), ()]
    assert [step.state for step in steps] == ["press", "release", "release", "done", "done"]


def test_release_after_a_held_phase_requires_external_drain_and_restart():
    stream = ir_runtime.Class5Stream([[(0x8111,)], [(0x8222,)], [(0x8333,)]])
    steps = [stream.step(queued_bytes=0, held=True) for _ in range(5)]

    assert [step.words for step in steps] == [(0x8111,), (), (0x8222,), (), (0x8222,)]
    boundary = stream.step(queued_bytes=0, held=False)
    assert boundary.state == "drain" and boundary.words == (0,)
    assert not stream.step(queued_bytes=0, held=False).progressed
    with pytest.raises(UnsupportedNative, match="empty, stopped transmitter"):
        stream.resume_after_drain(queued_bytes=2, transmitter_stopped=True)
    with pytest.raises(UnsupportedNative, match="empty, stopped transmitter"):
        stream.resume_after_drain(queued_bytes=0, transmitter_stopped=False)
    stream.resume_after_drain(queued_bytes=0, transmitter_stopped=True)
    assert stream.step(queued_bytes=0, held=False).words == (0x8333,)


def test_absent_hold_is_not_an_empty_record_that_can_repeat_forever():
    absent = ir_runtime.Class5Stream([None, None, [(0x8333,)]])
    empty = ir_runtime.Class5Stream([None, [], [(0x8333,)]])
    assert absent.step(queued_bytes=0, held=True).state == "hold"
    assert empty.step(queued_bytes=0, held=True).state == "hold"
    assert absent.step(queued_bytes=0, held=True).state == "release"
    assert empty.step(queued_bytes=0, held=True).state == "hold"


def test_the_oracle_reads_dictionary_symbols_independently_of_their_encoder():
    native = Native()
    command = native.command([[[0x8111, 0x0222], None, [0x8333]]])
    native.groups([[command]])
    reader = Reader(ahcm.Image(native.finish()))

    stream = ir_runtime.Class5Stream.from_command(reader, command, recording=0)

    assert stream.step(queued_bytes=0, held=False).words == (0x8111, 0x0222)
    assert stream.step(queued_bytes=0, held=False).state == "release"
    assert stream.step(queued_bytes=0, held=False).words == (0x8333,)


def test_rooted_streaming_retains_symbol_boundaries_and_repeated_dictionary_indices():
    native = Native()
    first = native.symbol([0x8111])
    second = native.symbol([0x8222, 0x0333])
    table = native.add(b"\x02" + pointer(first) + pointer(second))
    body = native.add(pointer(table) + b"\x03\x00\x01\x00\x01")
    command = native.command([[None, None, None]])
    at = command - 0x020000 + 5
    native.data[at:at + 3] = pointer(body)
    native.groups([[command]])
    stream = ir_runtime.Class5Stream.from_command(
        Reader(ahcm.Image(native.finish())), command, recording=0)

    assert stream.step(queued_bytes=0, held=False).words == (0x8222, 0x0333)
    assert stream.step(queued_bytes=0, held=False).words == (0x8111,)
    assert stream.step(queued_bytes=0, held=False).words == (0x8222, 0x0333)


def test_toggles_are_shared_within_a_group_but_not_across_groups():
    selector = ir_runtime.RecordingSelector()

    assert selector.select(0, 2) == 0
    assert selector.select(1, 2) == 0
    assert selector.select(0, 2) == 1
    assert selector.select(1, 2) == 1
    assert selector.select(0, 2) == 0


def test_group_selection_uses_a_volatile_low_nibble_toggle_not_a_persistent_state_index():
    selector = ir_runtime.RecordingSelector()

    assert selector.select(0, 2) == 0
    assert selector.select(16, 2) == 1
    assert ir_runtime.RecordingSelector().select(0, 2) == 0
    with pytest.raises(UnsupportedNative, match="zero-recording"):
        selector.select(0, 0)
    assert selector.select(0, 2) == 0


def test_a_single_recording_still_advances_its_groups_alternate_selection():
    selector = ir_runtime.RecordingSelector()

    assert selector.select(3, 1) == 0
    assert selector.select(3, 2) == 1


def test_stop_boundaries_are_not_implicitly_guarded_by_dictionary_admission():
    stream = ir_runtime.Class5Stream([None, None, None])

    assert stream.step(queued_bytes=254, held=False).state == "release"
    stop = stream.step(queued_bytes=254, held=False)
    assert stop.words == (0,) and stop.queued_bytes == 0
    assert stop.state == "done"


@pytest.mark.parametrize(("word", "kind", "delta", "preload", "duty"), [
    (0, "stop", None, None, 0),
    (1, "continue", None, None, None),
    (2, "duration", 65532, 0x9004, 0),
    (5, "duration", 65535, 0x9001, 0),
    (6, "duration", 0, 0x9000, 0),
    (7, "duration", 1, 0x8FFF, 0),
    (0x8001, "duration", 65531, 0x9005, 50),
    (0xFFFF, "duration", 32761, 0x1007, 50),
])
def test_timer_words_keep_control_collisions_and_tick_underflow_explicit(
        word, kind, delta, preload, duty):
    result = ir_runtime.timer_word(
        word, timer_count=0x9000, duty_register=50, phase_complete=False)

    assert (result.kind, result.delta_ticks, result.preload, result.duty_register) == (
        kind, delta, preload, duty)


def test_the_continue_token_stops_once_the_phase_is_complete():
    result = ir_runtime.timer_word(
        1, timer_count=0x9000, duty_register=50, phase_complete=True)

    assert result.kind == "stop" and result.duty_register == 0


@pytest.mark.parametrize("value", [True, -1, 0x1000000, 1.5])
def test_invalid_native_fields_are_refused_instead_of_silently_coerced(value):
    with pytest.raises(ValueError, match="unsigned 24-bit"):
        ir_runtime.carrier_registers(value, 12500)


def test_offline_oracles_do_not_promote_the_profile_or_qualify_command_generation():
    profile = remotes.get("harmony-550")
    signal = ir_signal.waveform([560, -40000], carrier_hz=40000)
    ir_runtime.carrier_registers(25000, 12500)

    assert backend.capability(signal, profile)["supported"] is False
    assert profile.status == remotes.READ_ONLY
