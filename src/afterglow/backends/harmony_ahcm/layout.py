"""Allocate native structures without assigning unproven platform or runtime meanings.

Named allocations and explicit references keep placement deterministic and distinguish
shared children from subsystem roots. This is an offline framing primitive, not a
project generator or a claim that its output boots.

Format reference: docs/harmony_ahcm/assembly.md
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

from . import ahcm


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _word(value: int, width: int, label: str) -> bytes:
    _nonnegative(value, label)
    if value >= 1 << (8 * width):
        raise ValueError(f"{label} does not fit its {8 * width}-bit native field")
    return value.to_bytes(width, "little")


def _name(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty name")


@dataclass(frozen=True)
class Allocation:
    name: str
    owner: str
    offset: int
    size: int


@dataclass(frozen=True)
class Relocation:
    source: str
    offset: int
    target: str | None
    target_offset: int
    size: int


@dataclass(frozen=True)
class Assembly:
    payload: bytes
    allocations: tuple[Allocation, ...]
    references: tuple[Relocation, ...]
    roots: tuple[tuple[str | None, int], ...]

    def offset(self, name: str) -> int:
        """The file offset of a named allocation."""
        for allocation in self.allocations:
            if allocation.name == name:
                return allocation.offset
        raise KeyError(f"unknown allocation {name!r}")

    def address(self, name: str) -> int:
        """The logical native address of a named allocation."""
        address = ahcm.LOGICAL_BASE + self.offset(name)
        _word(address, 3, f"address of {name!r}")
        return address

    def report(self) -> dict:
        """Allocation/reference evidence, never exclusive contiguous section ranges."""
        image = ahcm.Image(self.payload)
        positions = {allocation.name: allocation.offset for allocation in self.allocations}
        return {
            "ownership": "named-allocations-and-references",
            "allocations": [asdict(allocation) for allocation in self.allocations],
            "references": [
                {**asdict(reference),
                 "file_offset": positions[reference.source] + reference.offset,
                 "address": image.u24(positions[reference.source] + reference.offset)}
                for reference in self.references
            ],
            "section_roots": [
                {"index": slot.index, "spare": slot.spare, "address": slot.address,
                 "target": self.roots[slot.index][0],
                 "target_offset": self.roots[slot.index][1]}
                for slot in image.slots
            ],
            "validation": image.report(),
        }


class Layout:
    """Explicit header fields, named byte allocations, and deferred native pointers."""

    def __init__(self, *, header_value: int, spares: Sequence[int]):
        _word(header_value, 3, "header value")
        spares = tuple(spares)
        if len(spares) != ahcm.SLOT_COUNT:
            raise ValueError("all twenty section spare bytes must be supplied")
        for index, spare in enumerate(spares):
            _word(spare, 1, f"section {index} spare")
        self._header_value = header_value
        self._spares = spares
        self._blocks: dict[str, tuple[str, bytes]] = {}
        self._roots: dict[int, tuple[str | None, int]] = {}
        self._references: list[Relocation] = []
        self._fields: dict[str, set[int]] = {}

    def add(self, name: str, body: bytes, *, owner: str) -> None:
        """Allocate immutable bytes in request order; only declared pointers change."""
        _name(name, "allocation")
        _name(owner, f"owner of {name!r}")
        if name in self._blocks:
            raise ValueError(f"allocation {name!r} is already present")
        if not isinstance(body, bytes):
            raise TypeError(f"body of {name!r} must be immutable bytes")
        self._blocks[name] = owner, body
        self._fields[name] = set()

    def root(self, index: int, target: str | None, *, target_offset: int = 0) -> None:
        """Declare one subsystem root, including an explicitly absent subsystem."""
        _nonnegative(index, "section index")
        if index >= ahcm.SLOT_COUNT:
            raise ValueError(f"section index {index} is outside the twenty-slot table")
        if index in self._roots:
            raise ValueError(f"section {index} root is already declared")
        self._target(target, target_offset)
        self._roots[index] = target, target_offset

    def relocate(self, source: str, offset: int, target: str | None, *,
                 target_offset: int = 0, size: int = 1) -> None:
        """Patch a u24 field after all allocations, including forward targets, exist."""
        _name(source, "relocation source")
        if source not in self._blocks:
            raise ValueError(f"unknown relocation source {source!r}")
        _nonnegative(offset, f"pointer offset in {source!r}")
        _nonnegative(size, f"pointed-to size in {source!r}")
        self._target(target, target_offset)
        if offset + 3 > len(self._blocks[source][1]):
            raise ValueError(f"pointer field at {offset} crosses allocation {source!r}")
        field = set(range(offset, offset + 3))
        if field & self._fields[source]:
            raise ValueError(f"pointer field at {offset} overlaps another in {source!r}")
        self._fields[source].update(field)
        self._references.append(Relocation(source, offset, target, target_offset, size))

    @staticmethod
    def _target(target: str | None, offset: int) -> None:
        _nonnegative(offset, "target offset")
        if target is None:
            if offset:
                raise ValueError("a null reference cannot have a target offset")
        else:
            _name(target, "reference target")

    @staticmethod
    def _address(positions: dict[str, Allocation], target: str | None,
                 offset: int, size: int) -> bytes:
        if target is None:
            return bytes(3)
        if target not in positions:
            raise ValueError(f"unknown reference target {target!r}")
        allocation = positions[target]
        if offset + size > allocation.size:
            raise ValueError(f"referenced span at {offset} of {size} bytes crosses {target!r}")
        return _word(ahcm.LOGICAL_BASE + allocation.offset + offset, 3,
                     f"pointer to {target!r}")

    def assemble(self, *, trailing: bytes = b"") -> Assembly:
        """Seal new native framing from declared bytes, without opening any source."""
        missing = sorted(set(range(ahcm.SLOT_COUNT)) - self._roots.keys())
        if missing:
            raise ValueError(f"section roots must be declared explicitly; missing {missing}")
        if not isinstance(trailing, bytes):
            raise TypeError("bytes after the native end marker must be immutable bytes")
        out = bytearray(ahcm.HEADER_SIZE)
        positions: dict[str, Allocation] = {}
        for name, (owner, body) in self._blocks.items():
            positions[name] = Allocation(name, owner, len(out), len(body))
            out.extend(body)
        out[:4] = ahcm.MAGIC
        out[4:8] = _word(ahcm.LOGICAL_BASE + len(out) + 2, 4, "end vector")
        out[8:11] = _word(self._header_value, 3, "header value")
        out[ahcm.HEADER_MARKER_OFFSET:ahcm.HEADER_SIZE] = ahcm.HEADER_MAGIC
        roots = tuple(self._roots[index] for index in range(ahcm.SLOT_COUNT))
        for index, (target, offset) in enumerate(roots):
            at = ahcm.TABLE_OFFSET + index * 4
            out[at] = self._spares[index]
            out[at + 1:at + 4] = self._address(positions, target, offset, 1)
        for reference in self._references:
            at = positions[reference.source].offset + reference.offset
            out[at:at + 3] = self._address(
                positions, reference.target, reference.target_offset, reference.size)
        body = bytes(out)
        payload = body + ahcm.checksum(body, len(body)) + ahcm.END_MAGIC + trailing
        ahcm.validate(payload)
        return Assembly(payload, tuple(positions.values()), tuple(self._references), roots)
