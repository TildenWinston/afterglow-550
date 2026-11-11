"""Model only the established configuration-to-event action handoff.

Platform boot, physical event identities and action execution remain separate contracts.

Format reference: docs/harmony_ahcm/platform.md
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .ahcm import Image
from .readers import UnsupportedNative


def _unsigned(value: int, bits: int, field: str) -> int:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"{field} must be an unsigned {bits}-bit integer")
    return value


@dataclass(frozen=True)
class EventAction:
    """A stored action, including a meaningful all-zero record; not an executed action."""

    operand: int
    opcode: int

    def __post_init__(self):
        _unsigned(self.operand, 16, "event operand")
        _unsigned(self.opcode, 8, "event opcode")


def event_action(image: Image, selector: int) -> EventAction | None:
    """Read the selected inline action, or no action when outside the stored count.

    Only the selected span is read: an unselected record is not a runtime prerequisite.
    """
    _unsigned(selector, 8, "event selector")
    root = image.section(8)
    if root is None:
        raise UnsupportedNative("the platform event table is absent")
    count = image.u8(root)
    if selector >= count:
        return None
    record = root + 1 + 3 * selector
    raw = image.read(record, 3)
    return EventAction(int.from_bytes(raw[:2], "little"), raw[2])


def encode_event_table(actions: Iterable[EventAction]) -> bytes:
    """Encode explicit ordered actions without supplying startup or opcode defaults."""
    data = bytearray((0,))
    for action in actions:
        if not isinstance(action, EventAction):
            raise TypeError("event records must be EventAction values")
        if data[0] == 255:
            raise ValueError("the event table count exceeds its unsigned 8-bit field")
        data.extend(action.operand.to_bytes(2, "little"))
        data.append(action.opcode)
        data[0] += 1
    return bytes(data)
