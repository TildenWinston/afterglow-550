"""Model bounded class-5 storage consumption without claiming physical IR timing.

This independent reference is not a transmitter, a generator capability gate, or a
complete scheduler. Clock configuration, interrupt latency and physical key events
must be established separately.

Format reference: docs/harmony_ahcm/ir-runtime.md
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .readers import EXPANSION_BUDGET, Reader, UnsupportedNative


def _uint(value: int, bits: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{label} must be an unsigned {bits}-bit integer")
    return value


def _flag(value: bool, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


@dataclass(frozen=True)
class CarrierRegisters:
    period_register: int
    duty_register: int
    fields_truncated: bool
    arithmetic_wrapped: bool


def carrier_registers(period_ns: int, on_ns: int) -> CarrierRegisters:
    """Nominal register arithmetic, including truncation; never a measured frequency."""
    period = _uint(period_ns, 24, "carrier period") & 0xFFFF
    on = _uint(on_ns, 24, "carrier on-time") & 0xFFFF
    period_count = ((period + 125) & 0xFFFF) // 250
    duty_count = ((on + 125) & 0xFFFF) // 250
    return CarrierRegisters(
        (period_count - 1) & 0xFF, duty_count & 0xFF,
        period != period_ns or on != on_ns,
        period + 125 > 0xFFFF or on + 125 > 0xFFFF
        or not 1 <= period_count <= 256 or duty_count > 255)


@dataclass(frozen=True)
class SymbolAdmission:
    required_bytes: int
    compared_bytes: int
    available_bytes: int
    admitted: bool
    counter_wrapped: bool


def symbol_admission(word_count: int, queued_bytes: int) -> SymbolAdmission:
    """Inspect one atomic symbol's state-dependent check, not universal validity."""
    count = _uint(word_count, 16, "symbol word count")
    queued = _uint(queued_bytes, 8, "queued byte count")
    required = 2 * count
    compared = required & 0xFFFF
    available = (254 - queued) & 0xFFFF
    return SymbolAdmission(required, compared, available,
                           compared <= available, required != compared)


@dataclass(frozen=True)
class TimerWord:
    kind: str
    delta_ticks: int | None
    preload: int | None
    duty_register: int | None


def timer_word(word: int, *, timer_count: int, duty_register: int,
               phase_complete: bool) -> TimerWord:
    """Reference timer/control effects; tick duration and interrupt cost are unknown."""
    word = _uint(word, 16, "duration word")
    timer = _uint(timer_count, 16, "timer count")
    duty = _uint(duty_register, 8, "duty register")
    _flag(phase_complete, "phase-complete flag")
    if word == 0:
        return TimerWord("stop", None, None, 0)
    if word == 1:
        return TimerWord("stop" if phase_complete else "continue", None, None,
                         0 if phase_complete else None)
    delta = ((word & 0x7FFF) - 6) & 0xFFFF
    return TimerWord("duration", delta, (timer - delta) & 0xFFFF,
                     duty if word & 0x8000 else 0)


class RecordingSelector:
    """A volatile per-group toggle model; no persistent native state is implied."""

    def __init__(self) -> None:
        self._mask = 0

    def select(self, group: int, recordings: int) -> int:
        """Select on dequeue, not on request/retry; caller owns queue acceptance."""
        group = _uint(group, 8, "IR group")
        recordings = _uint(recordings, 8, "recording count")
        if not recordings:
            raise UnsupportedNative("zero-recording command selection is not established")
        bit = 1 << (group & 15)
        self._mask ^= bit
        return 0 if recordings == 2 and self._mask & bit else recordings - 1


@dataclass(frozen=True)
class StreamStep:
    words: tuple[int, ...]
    queued_bytes: int
    state: str
    progressed: bool


class Class5Stream:
    """A bounded producer oracle with an explicit external drain/restart boundary."""

    def __init__(self, phases: Sequence[Sequence[Sequence[int]] | None]):
        if len(phases) != 3:
            raise ValueError("one recording must supply press, hold and release phases")
        prepared = []
        total = 0
        for phase in phases:
            if phase is not None:
                _uint(len(phase), 16, "phase symbol count")
            symbols = None if phase is None else tuple(tuple(symbol) for symbol in phase)
            for symbol in symbols or ():
                _uint(len(symbol), 16, "symbol word count")
                for word in symbol:
                    _uint(word, 16, "duration word")
                total += len(symbol)
                if total > EXPANSION_BUDGET:
                    raise UnsupportedNative("class-5 oracle software budget exceeded")
            prepared.append(symbols)
        self._phases = tuple(prepared)
        self._phase = 0
        self._symbol = 0
        self._drain = False

    @classmethod
    def from_command(cls, reader: Reader, address: int, *, recording: int) -> Class5Stream:
        """Follow one rooted command while retaining its dictionary symbol boundaries."""
        command = reader.command(address)
        _uint(recording, 8, "recording index")
        if recording >= len(command["recordings"]):
            raise ValueError("recording index is outside the command's declared recordings")
        phases = []
        for body in command["recordings"][recording]:
            if body is None:
                phases.append(None)
                continue
            image = reader.image
            table = image.offset(body["table_address"]) if body["indices"] else None
            phases.append([
                reader.symbol(image.u24(table + 1 + 3 * index))
                for index in body["indices"]
            ])
        return cls(phases)

    @property
    def state(self) -> str:
        if self._drain:
            return "drain"
        return ("press", "hold", "release", "done")[self._phase]

    def step(self, *, queued_bytes: int, held: bool) -> StreamStep:
        """Produce one symbol or boundary; a blocked symbol is retried unchanged."""
        queued = _uint(queued_bytes, 8, "queued byte count")
        _flag(held, "held flag")
        if self._drain or self._phase == 3:
            return StreamStep((), queued, self.state, False)
        symbols = self._phases[self._phase]
        if symbols is not None and self._symbol < len(symbols):
            words = symbols[self._symbol]
            admission = symbol_admission(len(words), queued)
            if admission.counter_wrapped:
                raise UnsupportedNative(
                    "wrapped symbol admission is not qualified for bounded streaming")
            if not admission.admitted:
                return StreamStep((), queued, self.state, False)
            self._symbol += 1
            return StreamStep(words, (queued + admission.required_bytes) & 255,
                              self.state, True)
        self._symbol = 0
        words = ()
        if self._phase == 0:
            self._phase = 1 if held else 2
        elif self._phase == 1:
            if not held or symbols is None:
                self._phase = 2
                if not held and self._phases[2] is not None:
                    self._drain = True
                    words = (0,)
        else:
            self._phase = 3
            words = (0,)
        return StreamStep(words, (queued + 2 * len(words)) & 255, self.state, True)

    def resume_after_drain(self, *, queued_bytes: int, transmitter_stopped: bool) -> None:
        """Resume release only after the external scheduler proves both drain conditions."""
        _uint(queued_bytes, 8, "queued byte count")
        _flag(transmitter_stopped, "transmitter-stopped flag")
        if not self._drain:
            raise ValueError("class-5 stream is not waiting at a drain boundary")
        if queued_bytes or not transmitter_stopped:
            raise UnsupportedNative("release restart requires an empty, stopped transmitter")
        self._drain = False
