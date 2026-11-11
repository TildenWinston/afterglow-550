"""Construct explicit nominal IR candidates without asserting transmitter qualification.

Word packing is independent of symbol allocation, command records and key lifecycle.
Its resource budget is a software guard, not a native capacity or hardware claim.

Format reference: docs/harmony_ahcm/assembly.md
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import struct

from ... import ir_signal
from . import ir_runtime
from .layout import Layout
from .readers import UnsupportedNative

MAX_DURATION = 0x7FFF
WORD_BUDGET = 1_000_000


def duration_words(pulses_us: Iterable[int], *, budget: int = WORD_BUDGET) -> tuple[int, ...]:
    """Preserve signed intervals as native words, without zero words or truncation."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("duration-word software budget must be a non-negative integer")
    pulses = []
    required = 0
    for index, pulse in enumerate(pulses_us):
        if isinstance(pulse, bool) or not isinstance(pulse, int) or pulse == 0:
            raise ValueError(f"pulse {index} must be a non-zero integer number of microseconds")
        count = (abs(pulse) + MAX_DURATION - 1) // MAX_DURATION
        if count > budget - required:
            raise ValueError(
                "native-duration encoding software budget exceeded; this is not a hardware limit")
        required += count
        pulses.append(pulse)
    words = []
    for pulse in pulses:
        polarity = 0x8000 if pulse > 0 else 0
        complete, remainder = divmod(abs(pulse), MAX_DURATION)
        words.extend([polarity | MAX_DURATION] * complete)
        if remainder:
            words.append(polarity | remainder)
    return tuple(words)


def executable_words(pulses_us: Iterable[int], *, budget: int = WORD_BUDGET) -> tuple[int, ...]:
    """Split nominal durations without control collisions or short preload underflow.

    The storage-only encoder remains available for lossless evidence. This stricter
    construction subset does not establish physical timing after word splitting.
    """
    words = list(duration_words(pulses_us, budget=budget))
    for index, word in enumerate(words):
        remainder = word & MAX_DURATION
        if remainder >= 7:
            continue
        polarity = word & 0x8000
        if (index == 0 or words[index - 1] & 0x8000 != polarity
                or words[index - 1] & MAX_DURATION != MAX_DURATION):
            raise UnsupportedNative(
                f"duration word {index} is shorter than the bounded timer preload subset")
        transfer = 7 - remainder
        words[index - 1] -= transfer
        words[index] += transfer
    return tuple(words)


@dataclass(frozen=True)
class Command:
    signal: dict
    period_ns: int
    on_ns: int
    spare: int


@dataclass(frozen=True)
class CommandReference:
    name: str
    ordinal: int
    allocation: str
    target_offset: int


@dataclass(frozen=True)
class GroupReference:
    """Named offline candidate, not qualified transmitter support."""

    allocation: str
    commands: tuple[CommandReference, ...]


def _uint(value: int, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 0 to {maximum}")


def _press(command: Command) -> tuple[int, ...]:
    signal = command.signal
    ir_signal.validate(signal)
    if signal["kind"] != "waveform":
        raise UnsupportedNative("only explicit waveform commands have native lowering")
    if signal.get("alternate") is not None:
        raise UnsupportedNative("alternating command construction is not qualified")
    sections = signal.get("sections")
    if sections and (sections["repeat_pulses"] or sections.get("release_pulses", 0)):
        raise UnsupportedNative("hold/release command construction needs scheduler qualification")
    if signal["pulses_us"][0] < 0:
        raise UnsupportedNative("a leading silence needs explicit transmitter-start semantics")
    native = signal.get("native") or {}
    if not isinstance(native, dict):
        raise UnsupportedNative("native waveform evidence must be an object")
    evidence = native.get("harmony-ahcm") or {}
    if not isinstance(evidence, dict):
        raise UnsupportedNative("AHCM waveform evidence must be an object")
    if any(evidence.get("leading_silence_us") or ()):
        raise UnsupportedNative("native-only leading silence cannot be silently discarded")
    _uint(command.period_ns, 65535, "carrier period")
    _uint(command.on_ns, 65535, "carrier on-time")
    _uint(command.spare, 255, "command spare")
    registers = ir_runtime.carrier_registers(command.period_ns, command.on_ns)
    if registers.arithmetic_wrapped or registers.fields_truncated:
        raise UnsupportedNative("carrier fields wrap the nominal register conversion")
    if not 0 < registers.duty_register < registers.period_register + 1:
        raise UnsupportedNative("carrier quantizes to unmodulated output")
    if command.on_ns != command.period_ns // 2:
        raise UnsupportedNative("portable waveform does not specify a non-half duty cycle")
    carrier = signal.get("carrier_hz")
    if (isinstance(carrier, bool) or not isinstance(carrier, int)
            or carrier != round(1_000_000_000 / command.period_ns)):
        raise UnsupportedNative("explicit native period must agree with the waveform carrier")
    # Reserve one stop token in the initially empty byte queue. This is a chosen
    # one-shot construction subset, never a universal native symbol-size gate.
    words = executable_words(signal["pulses_us"], budget=126)
    if not words:
        raise UnsupportedNative("a one-shot command needs a non-empty press body")
    return words


def lower_group(layout: Layout, name: str, commands: Mapping[str, Command], *,
                command_order: Sequence[str], spare: int) -> GroupReference:
    """Allocate an explicit, nominal press-only group; leave section ownership outside.

    Signal validation finishes before any allocation. References preserve the
    caller's declared order for bindings, rather than sorting command names.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("IR group allocation needs a non-empty name")
    _uint(spare, 255, "group spare")
    if isinstance(command_order, (str, bytes)):
        raise TypeError("command_order must be a sequence of complete command names")
    order = tuple(command_order)
    if not order or len(order) > 256:
        raise ValueError("one-shot group must have 1..256 explicitly ordered commands")
    if (any(not isinstance(item, str) or not item.strip() for item in order)
            or len(set(order)) != len(order) or set(order) != set(commands)):
        raise ValueError("command_order must name every command exactly once")
    prepared = []
    for label in order:
        command = commands[label]
        if not isinstance(command, Command):
            raise TypeError(f"command {label!r} must supply explicit native settings")
        try:
            words = _press(command)
        except (ValueError, UnsupportedNative) as exc:
            raise UnsupportedNative(f"command {label!r}: {exc}") from exc
        prepared.append((label, command, words))
    references = []
    for ordinal, (label, command, words) in enumerate(prepared):
        prefix = f"{name}/command-{ordinal}"
        symbol, table, body, record = (
            f"{prefix}/{suffix}" for suffix in ("symbol", "dictionary", "press", "record"))
        layout.add(symbol, struct.pack(f"<H{len(words)}H", len(words), *words), owner=name)
        layout.add(table, b"\x01" + bytes(3), owner=name)
        layout.relocate(table, 1, symbol, size=2 + 2 * len(words))
        layout.add(body, bytes(3) + b"\x01\x00\x00", owner=name)
        layout.relocate(body, 0, table, size=4)
        header = (bytes((command.spare,)) + command.period_ns.to_bytes(3, "little")
                  + command.on_ns.to_bytes(3, "little") + b"\x05" + bytes(3) + b"\x01"
                  + bytes(9))
        layout.add(record, header, owner=name)
        layout.relocate(record, 8, record, size=len(header))
        layout.relocate(record, 12, body, size=6)
        references.append(CommandReference(label, ordinal, record, 7))
    allocation = f"{name}/group"
    layout.add(allocation, bytes((spare,)) + len(order).to_bytes(2, "little")
               + bytes(3 * len(order)), owner=name)
    for ref in references:
        layout.relocate(allocation, 3 + ref.ordinal * 3, ref.allocation,
                        target_offset=ref.target_offset, size=5)
    return GroupReference(allocation, tuple(references))


def group_index(layout: Layout, name: str, groups: Sequence[GroupReference]) -> None:
    """Declare section five from explicit, already allocated group references."""
    groups = tuple(groups)
    if len(groups) > 16:
        raise UnsupportedNative("group toggle aliases are outside the construction subset")
    if any(not isinstance(group, GroupReference) for group in groups):
        raise TypeError("IR group index requires explicit lowered group references")
    layout.add(name, bytes((len(groups),)) + bytes(3 * len(groups)), owner=name)
    for ordinal, group in enumerate(groups):
        layout.relocate(name, 1 + ordinal * 3, group.allocation, size=3)
    layout.root(5, name)
