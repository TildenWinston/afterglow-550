"""Encode duration words without choosing unproven transmitter or record defaults.

Word packing is independent of symbol allocation, command records and key lifecycle.
Its resource budget is a software guard, not a native capacity or hardware claim.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

from collections.abc import Iterable

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
