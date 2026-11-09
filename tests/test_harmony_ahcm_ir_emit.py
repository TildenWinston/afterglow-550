"""Native duration encoding is checked against the independent portable reader."""
from __future__ import annotations

import struct

import pytest

from afterglow import ir_signal, remotes
from afterglow.backends.harmony_ahcm import backend, importing, ir_emit
from afterglow.backends.harmony_ahcm.readers import UnsupportedNative


def _record(*recordings):
    return {"period_ns": 25000, "on_ns": 12500,
            "recordings": [[{"words": list(ir_emit.duration_words(phase))}
                            if phase is not None else None for phase in recording]
                           for recording in recordings]}


@pytest.mark.parametrize(("pulses", "expected"), [
    ([1, -1], (0x8001, 0x0001)),
    ([32767, -32767], (0xFFFF, 0x7FFF)),
    ([32768, -32768], (0xFFFF, 0x8001, 0x7FFF, 0x0001)),
    ([65534, -65535], (0xFFFF, 0xFFFF, 0x7FFF, 0x7FFF, 0x0001)),
])
def test_duration_words_preserve_polarity_at_the_exact_storage_field_boundaries(pulses, expected):
    words = ir_emit.duration_words(pulses)

    assert words == expected
    assert struct.unpack(f"<{len(words)}H", struct.pack(f"<{len(words)}H", *words)) == words
    assert all(word & 0x7FFF for word in words)
    decoded = importing.waveform(_record((pulses, None, None)), {})
    assert decoded["pulses_us"] == pulses


def test_long_intervals_decode_to_the_original_press_hold_and_release_durations():
    press = [9000, -4500, 560, -40000]
    hold = [560, -96077]
    release = [2795]

    decoded = importing.waveform(_record((press, hold, release)), {})

    assert decoded["pulses_us"] == press + hold + release
    assert decoded["sections"] == {
        "intro_pulses": 4, "repeat_pulses": 2, "release_pulses": 1}
    assert decoded["native"]["harmony-ahcm"]["leading_silence_us"] == [0]
    assert decoded["carrier_hz"] == 40000


def test_word_encoding_does_not_merge_or_discard_an_explicit_recording_lead_in():
    press = [-100000, 9000, -40000]
    words = ir_emit.duration_words(press)

    decoded = importing.waveform(_record((press, None, None)), {})

    assert words[:4] == (32767, 32767, 32767, 1699)
    assert decoded["pulses_us"] == [9000, -40000]
    assert decoded["native"]["harmony-ahcm"]["leading_silence_us"] == [100000]


def test_each_recording_is_encoded_without_choosing_its_transmitter_toggle_order():
    first = [9000, -4500, 560, -40000]
    alternate = [9000, -4500, 1690, -40000]

    decoded = importing.waveform(
        _record((first, None, None), (alternate, None, None)), {})

    assert decoded["pulses_us"] == first
    assert decoded["alternate"]["pulses_us"] == alternate


def test_adjacent_words_retain_the_represented_interval_instead_of_introducing_gaps():
    decoded = importing.waveform(_record(([100, 200, -300, -400], None, None)), {})

    assert decoded["pulses_us"] == [300, -700]


def test_encoding_a_non_press_lead_in_does_not_make_it_portable():
    with pytest.raises(UnsupportedNative, match="non-press leading silence"):
        importing.waveform(_record(([9000, -40000], [-2000, 560, -40000], None)), {})


@pytest.mark.parametrize("pulse", [0, True, False, 1.5, "560", None])
def test_invalid_pulses_are_refused_instead_of_coerced_or_truncated(pulse):
    with pytest.raises(ValueError, match="pulse 1 must be a non-zero integer"):
        ir_emit.duration_words([560, pulse])


@pytest.mark.parametrize("budget", [True, -1, 1.5, "2", None])
def test_invalid_software_budgets_are_refused(budget):
    with pytest.raises(ValueError, match="software budget must be a non-negative integer"):
        ir_emit.duration_words([560], budget=budget)


def test_the_software_budget_is_checked_against_the_expanded_word_count():
    assert ir_emit.duration_words([32768], budget=2) == (0xFFFF, 0x8001)
    with pytest.raises(ValueError, match="software budget exceeded.*not a hardware limit"):
        ir_emit.duration_words([32768], budget=1)
    with pytest.raises(ValueError, match="software budget exceeded"):
        ir_emit.duration_words([32767, -1], budget=1)


def test_a_huge_interval_is_rejected_before_an_unbounded_allocation():
    with pytest.raises(ValueError, match="software budget exceeded"):
        ir_emit.duration_words([10 ** 100], budget=1)


def test_an_empty_phase_has_no_words_and_does_not_need_a_budget():
    assert ir_emit.duration_words([], budget=0) == ()


def test_word_encoding_accepts_a_stream_without_consuming_it_twice():
    seen = []

    def pulses():
        for value in (560, -40000):
            seen.append(value)
            yield value

    assert ir_emit.duration_words(pulses()) == (0x8230, 32767, 7233)
    assert seen == [560, -40000]


def test_duration_word_support_does_not_advertise_qualified_signal_generation():
    signal = ir_signal.waveform([560, -40000], carrier_hz=40000)
    profile = remotes.get("harmony-550")

    assert ir_emit.duration_words(signal["pulses_us"])
    assert backend.capability(signal, profile)["supported"] is False
    assert profile.status == remotes.READ_ONLY
    with pytest.raises(remotes.NotBuildable):
        backend.build_project({}, profile, None)
