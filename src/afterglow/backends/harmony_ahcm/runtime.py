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
class ModeState:
    mode: int | None
    page: int | None

    def __post_init__(self):
        if (self.mode is None) != (self.page is None):
            raise ValueError("mode and page must both be explicit or both absent")
        if self.mode is not None and self.page is not None:
            _integer(self.mode, 65535, "mode")
            _integer(self.page, 65535, "page")


@dataclass(frozen=True)
class Paint:
    mode: int
    page: int
    program: int


@dataclass(frozen=True)
class KeyState:
    stack: tuple[int, ...]
    alternate: int | None
    inhibited: bool

    def __post_init__(self):
        if not isinstance(self.stack, tuple):
            raise TypeError("key stack must be an immutable tuple")
        if len(self.stack) > 8:
            raise UnsupportedNative("key stack aliases runtime storage outside this model")
        for entry in self.stack:
            _integer(entry, 255, "key-stack entry")
        if self.alternate is not None:
            _integer(self.alternate, 254, "alternate context")
        if not isinstance(self.inhibited, bool):
            raise TypeError("key inhibition must be explicit boolean state")


@dataclass(frozen=True)
class Execution:
    steps: int
    requests: tuple[Request, ...]
    pending: bool
    values: tuple[int, ...]
    context: int | None
    mode_state: ModeState | None = None
    paints: tuple[Paint, ...] = ()
    keys: KeyState | None = None


def _integer(value: int, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 0 to {maximum}")


class Runtime:
    """Conditional queue/state interpreter, with no startup or automatic IR completion."""

    def __init__(self, reader: Reader, *, values: Sequence[int], context: int | None,
                 accept: Callable[[Request], bool], mode_state: ModeState | None = None,
                 paint: Callable[[Paint], None] | None = None, keys: KeyState | None = None):
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
        if mode_state is not None and not isinstance(mode_state, ModeState):
            raise TypeError("mode state must be supplied as a ModeState")
        if mode_state is not None and not callable(paint):
            raise TypeError("mode interpretation requires an explicit paint callback")
        self.mode_state = mode_state
        self._paint = paint
        self._paints: list[Paint] = []
        if keys is not None and not isinstance(keys, KeyState):
            raise TypeError("key state must be supplied as a KeyState")
        self.keys = keys
        if mode_state is not None and mode_state.mode is not None and mode_state.page is not None:
            self._mode(mode_state.mode, mode_state.page)

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
            if opcode == 0x1F and operand >> 8 == 0xFC and self.keys is None:
                raise UnsupportedNative("asynchronous key-event routing is not modeled")
            if opcode or operand:
                pending.append((opcode, operand))
            if len(self._queue) + len(pending) > 40:
                raise UnsupportedNative(
                    "action queue would discard work; faithful execution cannot be reported")
        self._queue.extendleft(reversed(pending))

    def _context_actions(self, context: int | None, tag: int) -> list[tuple[int, int]]:
        return self._context_binding(context, tag)[0]

    def _context_binding(self, context: int | None, tag: int
                         ) -> tuple[list[tuple[int, int]], bool]:
        if context is None:
            return [], False
        self._context_index(context)
        entries = self.reader.tagged(self.reader.image.offset(self._contexts[context]))
        return self._binding(entries, tag)

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
        self._invalidate_keys()

    def _mode(self, ordinal: int, page: int) -> dict:
        if 3 + 3 * ordinal > 65535 or 6 + 3 * page > 65535:
            raise UnsupportedNative("wrapped navigation index arithmetic is not modeled")
        record = self.reader.mode(ordinal)
        if not record["pages"]:
            raise UnsupportedNative("mode without a page-zero target is not modeled")
        if not 0 <= page < len(record["pages"]):
            raise InvalidImage(f"page {page} is outside mode {ordinal}")
        return record

    def _binding(self, entries: list[dict], tag: int) -> tuple[list[tuple[int, int]], bool]:
        if self.keys is not None and self.keys.inhibited:
            return [], False
        entry = next((entry for entry in entries if entry["tag"] == tag), None)
        if entry is None:
            return [], False
        return [(entry["opcode"], entry["operand"])], not entry["flags"] & 1

    def _page_actions(self, mode: dict, page: int, tag: int) -> list[tuple[int, int]]:
        return self._mode_binding(mode, page, tag)[0]

    def _mode_binding(self, mode: dict, page: int, tag: int
                      ) -> tuple[list[tuple[int, int]], bool]:
        pending, stop = self._binding(mode["pages"][page]["bindings"], tag)
        if not stop:
            fallback, stop = self._binding(mode["bindings"], tag)
            pending += fallback
        return pending, stop

    def _paint_page(self, mode: dict, page: int) -> None:
        if self._paint is None:
            raise UnsupportedNative("painting requires an explicit renderer boundary")
        request = Paint(mode["ordinal"], page,
                        self.reader.image.base + mode["pages"][page]["program"])
        self._paints.append(request)
        self._paint(request)

    def _select_mode(self, selected: int) -> None:
        if self.mode_state is None:
            raise UnsupportedNative("initial mode state and painting are not supplied")
        if selected == 0xFEFE:
            raise UnsupportedNative("selecting the absent-mode sentinel is not modeled")
        mode = self._mode(selected, 0)
        pending = []
        if self.mode_state.mode is not None and self.mode_state.page is not None:
            old = self._mode(self.mode_state.mode, self.mode_state.page)
            pending += self._page_actions(old, self.mode_state.page, 0x2A)
            pending += self._binding(old["bindings"], 7)[0]
        self.mode_state = ModeState(selected, 0)
        pending += self._binding(mode["bindings"], 6)[0]
        pending += self._page_actions(mode, 0, 0x29)
        self._prepend(pending)
        if not mode["kind"] & 1:
            self._paint_page(mode, 0)
        self._invalidate_keys()

    def _select_page(self, selected: int | None, *, delta: int = 0) -> None:
        if (self.mode_state is None or self.mode_state.mode is None
                or self.mode_state.page is None):
            raise UnsupportedNative("page selection requires explicit current mode state")
        old_page = self.mode_state.page
        mode = self._mode(self.mode_state.mode, old_page)
        pending = self._page_actions(mode, old_page, 0x2A)
        if selected is None:
            selected = (old_page + delta) % len(mode["pages"])
        page = selected if selected < len(mode["pages"]) else old_page
        self._mode(self.mode_state.mode, page)
        self.mode_state = ModeState(self.mode_state.mode, page)
        pending += self._page_actions(mode, page, 0x29)
        self._prepend(pending)
        self._paint_page(mode, page)

    def _invalidate_keys(self) -> None:
        self._queue = deque(
            (0, 0) if opcode == 0x1F and operand >> 8 == 0xFC and operand & 0xC0
            else (opcode, operand) for opcode, operand in self._queue)

    def _key_state(self) -> KeyState:
        if self.keys is None:
            raise UnsupportedNative("explicit key-stack state is not supplied")
        return self.keys

    def _route_key(self, tag: int) -> None:
        state = self._key_state()
        if state.inhibited:
            return
        pending = []
        for entry in reversed(state.stack):
            if entry == 0xFD:
                if self.mode_state is None:
                    raise UnsupportedNative("key routing requires explicit current mode state")
                if self.mode_state.mode is None or self.mode_state.page is None:
                    continue
                mode = self._mode(self.mode_state.mode, self.mode_state.page)
                actions, stop = self._mode_binding(mode, self.mode_state.page, tag)
            else:
                selected = (state.alternate if entry == 0xFC else self.context
                            if entry == 0xFE else None if entry == 0xFF else entry)
                actions, stop = self._context_binding(selected, tag)
            pending += actions
            if stop:
                break
        self._prepend(pending)

    def _key_control(self, operation: int, value: int) -> None:
        state = self._key_state()
        stack, alternate = list(state.stack), state.alternate
        if operation == 0xFE:
            stack.append(value)
        elif operation == 0xFD:
            if value in stack:
                stack.remove(value)
        else:
            alternate = None if value == 255 else value
        self.keys = KeyState(tuple(stack), alternate, state.inhibited)
        if operation in (0xFD, 0xFE):
            self._invalidate_keys()

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
        self._paints = []
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
                elif opcode == 0x1F and operand >> 8 == 0xFC:
                    self._route_key(operand & 255)
                elif opcode == 0x1F and operand >> 8 in (0xFE, 0xFD, 0xE8):
                    self._key_control(operand >> 8, operand & 255)
                elif opcode == 0x7E:
                    self._select_mode(operand)
                elif opcode == 0x76:
                    self._select_page(operand)
                elif opcode == 0x0F and operand in (0xA0, 0xA1):
                    self._select_page(None, delta=1 if operand == 0xA1 else -1)
                elif opcode == 7 and operand == 255:
                    self._silent = True
                elif opcode == operand == 0:
                    pass
                else:
                    raise UnsupportedNative(
                        f"action opcode 0x{opcode:02X}, operand 0x{operand:04X} is not modeled")
            finished = True
        finally:
            self._failed = not finished
        return Execution(steps, tuple(requests), bool(self._queue), self.values, self.context,
                         self.mode_state, tuple(self._paints), self.keys)
