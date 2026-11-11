"""Construct explicit native navigation records without choosing keys or platform defaults.

Bindings, page programs and mode kinds remain caller-owned; every pointer is named.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .layout import Layout
from .platform import EventAction


def _uint(value: int, maximum: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 0 to {maximum}")


def _reference(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("navigation references must be nonempty allocation names")


@dataclass(frozen=True)
class Binding:
    tag: int
    action: EventAction
    flags: int = 0

    def __post_init__(self):
        _uint(self.tag, 255, "binding tag")
        _uint(self.flags, 255, "binding flags")
        if not isinstance(self.action, EventAction):
            raise TypeError("binding action must be an EventAction")


def add_bindings(plan: Layout, name: str, entries: Sequence[Binding], *, wide: bool) -> str:
    if not isinstance(wide, bool):
        raise TypeError("binding layout must be selected explicitly as a boolean")
    _uint(len(entries), 255, "binding count")
    if not entries and not wide:
        raise ValueError("an empty tagged list requires the explicit wide layout")
    body = bytearray((0, len(entries)) if wide else (len(entries),))
    for entry in entries:
        if not isinstance(entry, Binding):
            raise TypeError("tagged lists require Binding values")
        if not wide and entry.flags:
            raise ValueError("narrow bindings cannot carry flags")
        if wide:
            body.append(entry.flags)
        body.append(entry.tag)
        body.extend(entry.action.operand.to_bytes(2, "little"))
        body.append(entry.action.opcode)
    plan.add(name, bytes(body), owner="navigation")
    return name


def add_page(plan: Layout, name: str, *, bindings: str, program: str) -> str:
    for reference in (bindings, program):
        _reference(reference)
    plan.add(name, bytes(6), owner="navigation")
    plan.relocate(name, 0, bindings)
    plan.relocate(name, 3, program)
    return name


def add_mode(plan: Layout, name: str, *, kind: int, bindings: str,
             pages: Sequence[str]) -> str:
    _uint(kind, 255, "mode kind")
    _uint(len(pages), 65535, "page count")
    for reference in (bindings, *pages):
        _reference(reference)
    plan.add(name, bytes((kind,)) + bytes(3) + len(pages).to_bytes(2, "little")
             + bytes(3 * len(pages)), owner="navigation")
    plan.relocate(name, 1, bindings)
    for ordinal, page in enumerate(pages):
        plan.relocate(name, 6 + 3 * ordinal, page, size=6)
    return name


def add_mode_index(plan: Layout, name: str, modes: Sequence[str]) -> str:
    _uint(len(modes), 0xFFFFFF, "mode count")
    for reference in modes:
        _reference(reference)
    plan.add(name, len(modes).to_bytes(3, "little") + bytes(3 * len(modes)),
             owner="navigation")
    for ordinal, mode in enumerate(modes):
        plan.relocate(name, 3 + 3 * ordinal, mode, size=6)
    return name


def add_key_sets(plan: Layout, name: str, bindings: Sequence[str]) -> str:
    _uint(len(bindings), 255, "key-set count")
    for reference in bindings:
        _reference(reference)
    plan.add(name, bytes((len(bindings),)) + bytes(3 * len(bindings)), owner="navigation")
    for ordinal, reference in enumerate(bindings):
        plan.relocate(name, 1 + 3 * ordinal, reference)
    return name
