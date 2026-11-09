"""Bound native AHCM reads without confusing logical pointers with USB addresses.

Unknown bytes are retained, and structural validation makes no hardware acceptance claim.

Format reference: docs/harmony_ahcm/image.md
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

MAGIC = b"AHCM"
END_MAGIC = b"MCHA"
HEADER_MAGIC = b"CMAH"
LOGICAL_BASE = 0x020000
TABLE_OFFSET = 0x0B
SLOT_COUNT = 20
HEADER_MARKER_OFFSET = TABLE_OFFSET + SLOT_COUNT * 4
HEADER_SIZE = HEADER_MARKER_OFFSET + len(HEADER_MAGIC)


class InvalidImage(ValueError):
    """A native image or a rooted read violates an independently supported bound."""


def _integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidImage(f"{name} must be an integer")


def checksum(payload: bytes, checksum_offset: int) -> bytes:
    """The two XOR lanes over complete pairs before the stored checksum."""
    _integer(checksum_offset, "checksum offset")
    if not 0 <= checksum_offset <= len(payload):
        raise InvalidImage("checksum offset is outside the image")
    low, high = 0x21, 0x43
    for offset in range(0, checksum_offset - 1, 2):
        low ^= payload[offset]
        high ^= payload[offset + 1]
    return bytes((low, high))


@dataclass(frozen=True)
class Slot:
    index: int
    spare: int
    address: int
    offset: int | None


class Image:
    """An immutable payload with independently bounded, rooted native reads."""

    def __init__(self, payload: bytes, *, logical_base: int = LOGICAL_BASE):
        if not isinstance(payload, bytes):
            raise TypeError("an AHCM image must be bytes")
        _integer(logical_base, "logical base")
        if not 0 <= logical_base <= 0xFFFFFF:
            raise InvalidImage("logical base is outside the native u24 address domain")
        if len(payload) < HEADER_SIZE + 6:
            raise InvalidImage("truncated AHCM header or trailer")
        if payload[:4] != MAGIC:
            raise InvalidImage("wrong native magic: expected AHCM")
        if payload[HEADER_MARKER_OFFSET:HEADER_SIZE] != HEADER_MAGIC:
            raise InvalidImage("missing CMAH at the end of all twenty table slots")
        self.payload = payload
        self.base = logical_base
        self.end_vector = int.from_bytes(payload[4:8], "little")
        self.end_offset = self.end_vector - self.base
        self.checksum_offset = self.end_offset - 2
        if not HEADER_SIZE <= self.checksum_offset <= len(payload) - 6:
            raise InvalidImage("end vector is outside the native image")
        if payload[self.end_offset:self.end_offset + 4] != END_MAGIC:
            raise InvalidImage("end vector does not point to MCHA")
        self.stored_checksum = payload[self.checksum_offset:self.end_offset]
        self.computed_checksum = checksum(payload, self.checksum_offset)
        if self.stored_checksum != self.computed_checksum:
            raise InvalidImage(
                f"native checksum mismatch: stored {self.stored_checksum.hex()}, "
                f"computed {self.computed_checksum.hex()}")
        self.header_value = int.from_bytes(payload[8:11], "little")
        self.slots = tuple(self._slot(index) for index in range(SLOT_COUNT))
        identity = self.section(1)
        self.identity = None
        if identity is not None:
            protocol, repeated, skin = self.read(identity, 3)
            if protocol != repeated or protocol != 9:
                raise InvalidImage("native identity does not state architecture 9 twice")
            self.identity = {"arch": protocol, "skin": skin}

    def read(self, offset: int, size: int) -> bytes:
        """Read a file-offset span, excluding the native checksum and end marker."""
        _integer(offset, "file offset")
        _integer(size, "read size")
        if offset < 0 or size < 0 or offset + size > self.checksum_offset:
            raise InvalidImage(
                f"read at 0x{offset:X} of {size} bytes crosses native image bounds")
        return self.payload[offset:offset + size]

    def offset(self, address: int, size: int = 1) -> int:
        """Resolve a non-null logical pointer, never a physical transport address."""
        _integer(address, "logical address")
        _integer(size, "pointed-to size")
        offset = address - self.base
        if size < 0 or offset < HEADER_SIZE or offset + size > self.checksum_offset:
            raise InvalidImage(
                f"logical pointer 0x{address:X} of {size} bytes is outside native data")
        return offset

    def pointer(self, offset: int, size: int = 1) -> int | None:
        """Resolve a u24 pointer while preserving a meaningful null slot."""
        address = self.u24(offset)
        return self.offset(address, size) if address else None

    def u8(self, offset: int) -> int:
        return self.read(offset, 1)[0]

    def u16(self, offset: int) -> int:
        return int.from_bytes(self.read(offset, 2), "little")

    def u24(self, offset: int) -> int:
        return int.from_bytes(self.read(offset, 3), "little")

    def u32(self, offset: int) -> int:
        return int.from_bytes(self.read(offset, 4), "little")

    def _slot(self, index: int) -> Slot:
        start = TABLE_OFFSET + index * 4
        address = self.u24(start + 1)
        return Slot(index, self.u8(start), address,
                    self.offset(address) if address else None)

    def section(self, index: int) -> int | None:
        """A subsystem's root, not a contiguous ownership boundary."""
        _integer(index, "slot index")
        if not 0 <= index < SLOT_COUNT:
            raise InvalidImage(f"slot index {index} is outside the twenty-slot table")
        return self.slots[index].offset

    def report(self) -> dict:
        return {
            "ok": True,
            "validator": "harmony-ahcm/rooted-structural",
            "validation_scope": "native-framing-and-root-addresses",
            "hardware_acceptance": "not-established",
            "logical_base": self.base,
            "end_vector": self.end_vector,
            "end_marker_offset": self.end_offset,
            "header_value": self.header_value,
            "identity": self.identity,
            "slots": [
                {"index": slot.index, "spare": slot.spare,
                 "address": slot.address, "offset": slot.offset}
                for slot in self.slots
            ],
            "present_slots": sum(slot.offset is not None for slot in self.slots),
            "checksum": self.stored_checksum.hex(),
            "checksum_offset": self.checksum_offset,
            "preserved_bytes": len(self.payload),
            "trailing_bytes": len(self.payload) - self.end_offset - len(END_MAGIC),
        }


def validate(payload: bytes) -> dict:
    """Validate the independently supported native framing and pointer bounds."""
    return Image(payload).report()


def inspect(payload: bytes) -> dict:
    """Inspect framing without normalising or discarding any native bytes."""
    from .readers import Reader

    image = Image(payload)
    return {**image.report(), "sha256": hashlib.sha256(payload).hexdigest(),
            "inventory": Reader(image).inspect()}
