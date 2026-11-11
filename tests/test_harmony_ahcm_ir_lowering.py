"""Native candidates decode and execute independently without gaining write authority."""
from __future__ import annotations

from fractions import Fraction

import pytest

from afterglow import ir_signal, remotes
from afterglow.backends.harmony_ahcm import ahcm, backend, importing, ir_emit, ir_runtime
from afterglow.backends.harmony_ahcm.layout import Layout
from afterglow.backends.harmony_ahcm.readers import Reader, UnsupportedNative


def layout():
    result = Layout(header_value=0x1400, spares=[0] * 20)
    for root in range(20):
        if root != 5:
            result.root(root, None)
    return result


def command(pulses, **signal_options):
    return ir_emit.Command(
        ir_signal.waveform(pulses, carrier_hz=40000, **signal_options),
        period_ns=25000, on_ns=12500, spare=0)


def assembled(commands, order):
    target = layout()
    group = ir_emit.lower_group(
        target, "synthetic-ir", commands, command_order=order, spare=0x12)
    ir_emit.group_index(target, "groups", [group])
    return target.assemble(), group


@pytest.mark.parametrize(("pulses", "expected"), [
    ([32767, -32767], (0xFFFF, 0x7FFF)),
    ([32768, -32768], (0xFFF9, 0x8007, 0x7FF9, 0x0007)),
    ([65540, -65540], (0xFFFF, 0xFFFE, 0x8007, 0x7FFF, 0x7FFE, 0x0007)),
    ([7, -7], (0x8007, 7)),
])
def test_executable_splits_rebalance_short_tails_without_changing_polarity_or_total(
        pulses, expected):
    words = ir_emit.executable_words(pulses)

    assert words == expected
    decoded, leadin = importing._phase(list(words), press=True)
    assert decoded == pulses and leadin == 0
    assert all((word & 0x7FFF) >= 7 for word in words)
    assert all(ir_runtime.timer_word(
        word, timer_count=0, duty_register=50, phase_complete=False).kind == "duration"
               for word in words)


@pytest.mark.parametrize("pulse", [1, -1, 2, -2, 6, -6])
def test_short_pulses_are_explicitly_outside_the_executable_subset(pulse):
    with pytest.raises(UnsupportedNative, match="shorter than"):
        ir_emit.executable_words([pulse])


def test_the_storage_only_splitter_still_retains_exact_unqualified_evidence():
    assert ir_emit.duration_words([-32768]) == (32767, 1)
    assert ir_emit.executable_words([-32768]) == (32761, 7)
    assert ir_runtime.timer_word(
        1, timer_count=0, duty_register=50, phase_complete=False).kind == "continue"


def test_explicit_command_order_drives_native_ordinals_not_mapping_or_alphabetic_order():
    assembly, group = assembled(
        {"Z": command([9000, -4500]), "A": command([560, -40000])}, ["A", "Z"])
    reader = Reader(ahcm.Image(assembly.payload))
    native = reader.group(reader.addresses(5, 1)[0])

    assert [ref.name for ref in group.commands] == ["A", "Z"]
    assert [ref.ordinal for ref in group.commands] == [0, 1]
    assert native["spare"] == 0x12
    for ref, address, pulses in zip(
            group.commands, native["commands"], ([560, -40000], [9000, -4500])):
        assert address == assembly.address(ref.allocation) + ref.target_offset
        decoded = importing.waveform(reader.command(address), {})
        assert decoded["pulses_us"] == pulses
        assert decoded["carrier_hz"] == 40000
        stream = ir_runtime.Class5Stream.from_command(reader, address, recording=0)
        press = stream.step(queued_bytes=0, held=False)
        assert press.progressed and press.queued_bytes <= 252
        assert stream.step(queued_bytes=press.queued_bytes, held=False).state == "release"
        stop = stream.step(queued_bytes=press.queued_bytes, held=False)
        assert stop.words == (0,) and stop.queued_bytes <= 254
        assert stop.state == "done"


def test_a_one_shot_candidate_keeps_the_stop_token_outside_the_symbol_and_reserves_its_space():
    pulses = [560, -560] * 63
    assembly, _ = assembled({"Burst": command(pulses)}, ["Burst"])
    reader = Reader(ahcm.Image(assembly.payload))
    address, = reader.group(reader.addresses(5, 1)[0])["commands"]
    record = reader.command(address)

    assert record["recordings"][0][1:] == [None, None]
    assert len(record["recordings"][0][0]["words"]) == 126
    assert not any(word in (0, 1) for word in record["recordings"][0][0]["words"])
    assert importing.waveform(record, {})["pulses_us"] == pulses
    with pytest.raises(UnsupportedNative, match="command 'Long'.*software budget"):
        assembled({"Long": command(pulses + [560])}, ["Long"])


@pytest.mark.parametrize("unsupported", [
    command([560, -560], alternate={"pulses_us": [1120, -560]}),
    command([560, -560, 560, -560], sections={"intro_pulses": 2, "repeat_pulses": 2}),
    command([560, -560, 560, -560], sections={
        "intro_pulses": 2, "repeat_pulses": 0, "release_pulses": 2}),
    command([-560, 560]),
    command([560, -560], native={"harmony-ahcm": {"leading_silence_us": [1000]}}),
    ir_emit.Command(ir_signal.protocol_signal("unknown", {}), 25000, 12500, 0),
    ir_emit.Command(ir_signal.backend_opaque({"unread": True}), 25000, 12500, 0),
    ir_emit.Command(ir_signal.waveform([560, -560]), 25000, 12500, 0),
    ir_emit.Command(ir_signal.waveform([560, -560], carrier_hz=38000), 25000, 12500, 0),
    ir_emit.Command(ir_signal.waveform([560, -560], carrier_hz=40000), 25000, 10000, 0),
    ir_emit.Command(ir_signal.waveform([560, -560], carrier_hz=8_000_000), 125, 62, 0),
    ir_emit.Command(ir_signal.waveform([560, -560], carrier_hz=4_000_000), 250, 125, 0),
    ir_emit.Command(ir_signal.waveform([560, -560], carrier_hz=100), 0x10000, 12500, 0),
])
def test_an_unsupported_command_rejects_the_whole_group_before_any_allocation(unsupported):
    target = layout()
    empty = layout()
    with pytest.raises(UnsupportedNative, match="command 'Unsupported':"):
        ir_emit.lower_group(target, "ir", {"Good": command([560, -560]),
                                          "Unsupported": unsupported},
                            command_order=["Good", "Unsupported"], spare=0)
    ir_emit.group_index(target, "groups", [])
    ir_emit.group_index(empty, "groups", [])
    assert target.assemble().payload == empty.assemble().payload


@pytest.mark.parametrize("order", [[], ["A", "A"], ["A"], ["A", "C"]])
def test_missing_duplicate_or_undeclared_command_order_is_not_silently_normalized(order):
    with pytest.raises(ValueError):
        ir_emit.lower_group(layout(), "ir", {"A": command([560]), "B": command([1120])},
                            command_order=order, spare=0)


def test_the_same_declared_inputs_assemble_byte_identically_without_any_source_template():
    first, _ = assembled({"Power": command([9000, -4500, 560, -32768])}, ["Power"])
    second, _ = assembled({"Power": command([9000, -4500, 560, -32768])}, ["Power"])

    assert first.payload == second.payload


def test_conditional_clock_math_has_no_assumed_physical_frequency():
    registers = ir_runtime.carrier_registers(25000, 12500)
    at_16 = ir_runtime.clock_timing(
        registers, oscillator_hz=16_000_000, timer0_control=0x81, timer2_control=4)
    at_48 = ir_runtime.clock_timing(
        registers, oscillator_hz=48_000_000, timer0_control=0x81, timer2_control=4)

    assert at_16.carrier_period_ns == 25000
    assert at_16.carrier_on_ns == 12500
    assert at_16.duration_tick_ns == 1000
    assert at_48.carrier_period_ns == Fraction(25000, 3)
    assert at_48.duration_tick_ns == Fraction(1000, 3)


@pytest.mark.parametrize(("word", "count", "wrapped", "missed"), [
    (0x8100, 20, False, False),
    (0x8100, 250, False, True),
    (0x8100, 251, False, True),
    (0x8007, 0, False, False),
    (0x8007, 1, False, True),
    (0x8005, 0, True, None),
])
def test_nominal_words_do_not_hide_late_interrupts_behind_a_plausible_wrapped_preload(
        word, count, wrapped, missed):
    result = ir_runtime.timer_word(
        word, timer_count=count, duty_register=50, phase_complete=False)

    assert result.compensation_wrapped is wrapped
    assert result.deadline_missed is missed


@pytest.mark.parametrize(("timer0", "timer2"), [(0xA1, 4), (0x88, 4), (0x81, 5), (0x81, 0)])
def test_unknown_clock_or_prescaler_configuration_cannot_look_like_qualified_timing(timer0, timer2):
    with pytest.raises(UnsupportedNative, match="clock/control"):
        ir_runtime.clock_timing(ir_runtime.carrier_registers(25000, 12500),
                                oscillator_hz=16_000_000,
                                timer0_control=timer0, timer2_control=timer2)


def test_native_candidate_construction_does_not_enable_whole_project_build_or_writes():
    assembly, _ = assembled({"Power": command([9000, -4500])}, ["Power"])
    profile = remotes.get("harmony-550")

    assert ahcm.validate(assembly.payload)
    assert backend.capability(command([560]).signal, profile)["supported"] is False
    assert profile.status == remotes.READ_ONLY
    with pytest.raises(remotes.NotBuildable):
        backend.build_project({}, profile, None)
