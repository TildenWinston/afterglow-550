"""Execute a bounded action/state subset without claiming boot or peripheral emulation.

Initial cache/context state and peripheral acceptance are supplied explicitly. Unknown
instructions stop interpretation; a successful host run is not hardware qualification.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Literal

from .ahcm import InvalidImage
from .readers import Reader, UnsupportedNative


@dataclass(frozen=True)
class Request:
    kind: Literal["send", "device-delay"]
    group: int
    value: int


@dataclass(frozen=True)
class Execution:
    steps: int
    requests: tuple[Request, ...]
    pending: bool
    values: tuple[int, ...]
    context: int | None


def _integer(value: int, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 0 to {maximum}")


class Runtime:
    """Conditional queue/state interpreter, with no startup or automatic IR completion."""

    def __init__(self, reader: Reader, *, values: Sequence[int], context: int | None,
                 accept: Callable[[Request], bool]):
        root = reader.image.section(13)
        if root is None:
            raise UnsupportedNative("runtime requires an explicit state table")
        words = [reader.image.u16(root + index * 2) for index in range(4)]
        total, narrow, wide, repeated = words
        if total != narrow + wide or repeated != narrow or max(words) > 255:
            raise UnsupportedNative("state header aliasing is outside the runtime model")
        if narrow + 2 * wide > 64:
            raise UnsupportedNative("state cache aliases runtime storage outside this model")
        if len(values) != total:
            raise ValueError("initial cache must supply every declared state value")
        for ordinal, value in enumerate(values):
            _integer(value, 255 if ordinal < narrow else 65535, f"initial state {ordinal}")
        if not callable(accept):
            raise TypeError("peripheral acceptance must be supplied explicitly")
        self.reader = reader
        self._values = list(values)
        self._narrow = narrow
        self._contexts = reader.key_sets()
        if context is not None:
            self._context_index(context)
        self.context = context
        self._accept = accept
        self._silent = False
        self._queue: deque[tuple[int, int]] = deque()
        self._failed = False

    @property
    def values(self) -> tuple[int, ...]:
        return tuple(self._values)

    def _context_index(self, ordinal: int) -> None:
        _integer(ordinal, 254, "context")
        if ordinal >= len(self._contexts):
            raise InvalidImage(f"context {ordinal} is outside its declared index")

    def _prepend(self, instructions: Iterable[tuple[int, int]]) -> None:
        pending = []
        for opcode, operand in instructions:
            _integer(opcode, 255, "opcode")
            _integer(operand, 65535, "operand")
            if opcode == 0x1F and operand >> 8 == 0xFC:
                raise UnsupportedNative("asynchronous key-event routing is not modeled")
            if opcode or operand:
                pending.append((opcode, operand))
            if len(self._queue) + len(pending) > 40:
                raise UnsupportedNative(
                    "action queue would discard work; faithful execution cannot be reported")
        self._queue.extendleft(reversed(pending))

    def _context_actions(self, context: int | None, tag: int) -> list[tuple[int, int]]:
        if context is None:
            return []
        self._context_index(context)
        entries = self.reader.tagged(self.reader.image.offset(self._contexts[context]))
        match = next((entry for entry in entries if entry["tag"] == tag), None)
        return [(match["opcode"], match["operand"])] if match is not None else []

    def _select_context(self, selected: int) -> None:
        context = None if selected == 255 else selected
        if context is not None:
            self._context_index(context)
        if context == self.context:
            pending = self._context_actions(context, 5)
        else:
            pending = self._context_actions(self.context, 2)
            pending += self._context_actions(context, 1)
        self.context = context
        self._prepend(pending)

    @staticmethod
    def _matches(condition: int, value: int, before: int, after: int) -> bool:
        if condition == -2:
            return True
        if condition == -3:
            return before != after
        return condition & 0xFFFF == value

    def _write(self, ordinal: int, value: int, *, delta: int | None = None) -> None:
        if ordinal >= len(self._values):
            raise InvalidImage(f"state {ordinal} is outside its declared index")
        if delta is None and self._silent and ordinal == 0:
            raise UnsupportedNative("silent state-zero platform refresh is not modeled")
        state = self.reader.state(ordinal)
        before = self._values[ordinal]
        if delta is not None:
            if before > state["maximum"]:
                raise UnsupportedNative("relative state adjustment outside its declared range")
            value = (before + delta) % (state["maximum"] + 1)
        self._values[ordinal] = value & (255 if ordinal < self._narrow else 65535)
        if not self._silent:
            pending = []
            for transition in state["transitions"]:
                if transition["lead"] & 0x7F:
                    raise UnsupportedNative("complex state-transition execution is not modeled")
                matched = (
                    self._matches(transition["from"], before, before, value)
                    and self._matches(transition["to"], value, before, value))
                if matched:
                    pending.append((transition["opcode"], transition["operand"]))
                    if transition["lead"] & 0x80:
                        break
            self._prepend(pending)
        self._silent = False

    def run(self, instructions: Iterable[tuple[int, int]] = (), *,
            max_steps: int = 10000) -> Execution:
        """Drain requests in native queue order, or pause at an unaccepted boundary.

        A false callback leaves the same instruction pending for an explicit later run.
        No clock, device state, phase completion or automatic retry is inferred.
        """
        _integer(max_steps, 1_000_000, "step budget")
        if not max_steps:
            raise ValueError("step budget must be positive")
        if self._failed:
            raise UnsupportedNative("runtime stopped previously; create a new explicit state")
        requests: list[Request] = []
        steps = 0
        finished = False
        try:
            incoming = tuple(islice(instructions, 41))
            if len(incoming) > 40:
                raise UnsupportedNative("ingress inspector budget exceeded")
            if incoming and self._queue:
                raise UnsupportedNative("concurrent ingress while work is pending is not modeled")
            self._prepend(incoming)
            while self._queue:
                if steps >= max_steps:
                    raise UnsupportedNative("action-execution inspector step budget exceeded")
                opcode, operand = self._queue.popleft()
                steps += 1
                if opcode >= 0x80:
                    self._write(opcode & 0x7F, operand)
                elif opcode == 0x7F:
                    self._prepend((item["opcode"], item["operand"])
                                  for item in self.reader.action(operand))
                elif opcode in (0x7C, 0x7D):
                    request = Request("send" if opcode == 0x7D else "device-delay",
                                      operand >> 8, operand & 255)
                    requests.append(request)
                    accepted = self._accept(request)
                    if not isinstance(accepted, bool):
                        raise TypeError("peripheral acceptance must return a boolean")
                    if not accepted:
                        self._queue.appendleft((opcode, operand))
                        break
                elif opcode == 0x1F and operand >> 8 == 0xFF:
                    self._select_context(operand & 255)
                elif opcode == 0x1F and operand >> 8 in (0xF1, 0xF2):
                    self._write(operand & 255, 0, delta=1 if operand >> 8 == 0xF2 else -1)
                elif opcode == 7 and operand == 255:
                    self._silent = True
                else:
                    raise UnsupportedNative(
                        f"action opcode 0x{opcode:02X}, operand 0x{operand:04X} is not modeled")
            finished = True
        finally:
            self._failed = not finished
        return Execution(steps, tuple(requests), bool(self._queue), self.values, self.context)
