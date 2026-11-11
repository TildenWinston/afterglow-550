"""Read AHCM objects only through their owning roots and declared extents.

Shared children and unknown bytes remain evidence, not inferred contiguous sections.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

from collections import Counter

from .ahcm import Image, InvalidImage

EXPANSION_BUDGET = 1_000_000
OBJECT_BUDGET = 100_000
SCREEN_OPERANDS = {1: 6, 2: 5, 3: 9, 4: 5, 16: 1, 17: 3, 22: 1, 23: 0}


class UnsupportedNative(ValueError):
    """Native evidence is valid enough to preserve but not faithfully interpreted."""


class Reader:
    """Independent bounded readers with lossless evidence for every consumed object."""

    def __init__(self, image: Image):
        self.image = image
        self.regions: dict[tuple[str, int], dict] = {}
        self._bodies: dict[int, dict] = {}
        self._symbols: dict[int, list[int]] = {}
        self._expanded_words = 0
        self._expanded_steps = 0
        self._objects_read = 0
        self._indexes: dict[tuple[int, int], list[int]] = {}
        self._action_lists: dict[int, list[dict]] = {}

    def _objects(self, count: int) -> None:
        if count > OBJECT_BUDGET - self._objects_read:
            raise UnsupportedNative("native-object inspector budget exceeded")
        self._objects_read += count

    def capture(self, kind: str, offset: int, size: int) -> bytes:
        raw = self.image.read(offset, size)
        self.regions[kind, offset] = {
            "kind": kind, "offset": offset, "size": size, "hex": raw.hex()}
        return raw

    def addresses(self, slot: int, width: int) -> list[int]:
        key = slot, width
        if key in self._indexes:
            return self._indexes[key]
        root = self.image.section(slot)
        if root is None:
            return []
        count = int.from_bytes(self.image.read(root, width), "little")
        self.image.read(root, width + 3 * count)
        self._objects(count)
        raw = self.capture(f"slot-{slot}-index", root, width + 3 * count)
        result = [int.from_bytes(raw[at:at + 3], "little")
                  for at in range(width, len(raw), 3)]
        self._indexes[key] = result
        return result

    def group(self, address: int) -> dict:
        root = self.image.offset(address, 3)
        count = self.image.u16(root + 1)
        self.image.read(root, 3 + 3 * count)
        self._objects(count)
        raw = self.capture("ir-group", root, 3 + 3 * count)
        return {"address": address, "spare": raw[0],
                "commands": [int.from_bytes(raw[at:at + 3], "little")
                             for at in range(3, len(raw), 3)]}

    def command(self, address: int) -> dict:
        root = self.image.offset(address)
        ir_class = self.capture("ir-command-class", root, 1)[0]
        if ir_class != 5:
            raise UnsupportedNative(f"unsupported IR class {ir_class}")
        start = self.image.pointer(root + 1, 12)
        if start is None or start + 7 != root:
            raise InvalidImage("IR self pointer does not locate its own class byte")
        count = self.image.u8(start + 11)
        raw = self.capture("ir-command-header", start, 12 + 9 * count)
        recordings = []
        for ordinal in range(count):
            phases = []
            for phase in range(3):
                at = 12 + ordinal * 9 + phase * 3
                pointer = int.from_bytes(raw[at:at + 3], "little")
                phases.append(self.body(pointer) if pointer else None)
            recordings.append(phases)
        return {"address": address, "class": ir_class, "spare": raw[0],
                "period_ns": int.from_bytes(raw[1:4], "little"),
                "on_ns": int.from_bytes(raw[4:7], "little"),
                "recordings": recordings, "header_hex": raw.hex()}

    def symbol(self, address: int) -> list[int]:
        if address not in self._symbols:
            root = self.image.offset(address, 2)
            count = self.image.u16(root)
            raw = self.capture("ir-symbol", root, 2 + 2 * count)
            self._symbols[address] = [
                int.from_bytes(raw[at:at + 2], "little")
                for at in range(2, len(raw), 2)]
        return self._symbols[address]

    def body(self, address: int) -> dict:
        if address in self._bodies:
            return self._bodies[address]
        root = self.image.offset(address, 5)
        count = self.image.u16(root + 3)
        raw = self.capture("ir-body", root, 5 + count)
        table_address = int.from_bytes(raw[:3], "little")
        indices = list(raw[5:])
        words: list[int] = []
        if indices:
            table = self.image.offset(table_address)
            symbols = self.image.u8(table)
            pointers = self.capture("ir-symbol-index", table, 1 + 3 * symbols)
            for index in indices:
                if index >= symbols:
                    raise InvalidImage("IR dictionary index is outside its symbol table")
                at = 1 + 3 * index
                symbol = self.symbol(int.from_bytes(pointers[at:at + 3], "little"))
                if self._expanded_words + len(words) + len(symbol) > EXPANSION_BUDGET:
                    raise UnsupportedNative("expanded-duration inspector budget exceeded")
                words.extend(symbol)
        self._expanded_words += len(words)
        result = {"address": address, "table_address": table_address,
                  "indices": indices, "words": words, "hex": raw.hex()}
        self._bodies[address] = result
        return result

    def actions(self, offset: int) -> list[dict]:
        if offset in self._action_lists:
            return self._action_lists[offset]
        count = self.image.u8(offset)
        self.image.read(offset, 1 + 3 * count)
        self._objects(count)
        raw = self.capture("action-list", offset, 1 + 3 * count)
        result = [{"operand": int.from_bytes(raw[at:at + 2], "little"),
                   "opcode": raw[at + 2]}
                  for at in range(1, len(raw), 3)]
        self._action_lists[offset] = result
        return result

    def action(self, ordinal: int) -> list[dict]:
        addresses = self.addresses(10, 2)
        if not 0 <= ordinal < len(addresses):
            raise InvalidImage(f"action list {ordinal} is outside its declared index")
        return self.actions(self.image.offset(addresses[ordinal]))

    def expand(self, opcode: int, operand: int) -> list[dict]:
        """Expand calls in order; repeated calls execute again, recursive calls fail."""
        pending = [(opcode, operand, ())]
        result = []
        while pending:
            operation, value, active = pending.pop()
            self._expanded_steps += 1
            if self._expanded_steps > EXPANSION_BUDGET:
                raise UnsupportedNative("action-expansion inspector budget exceeded")
            if operation != 0x7F:
                result.append({"opcode": operation, "operand": value})
                continue
            if value in active:
                raise UnsupportedNative(f"recursive action list {value}")
            instructions = self.action(value)
            path = (*active, value)
            pending.extend((item["opcode"], item["operand"], path)
                           for item in reversed(instructions))
        return result

    def tagged(self, offset: int) -> list[dict]:
        first = self.image.u8(offset)
        wide = first == 0
        header, stride = (2, 5) if wide else (1, 4)
        count = self.image.u8(offset + 1) if wide else first
        self.image.read(offset, header + count * stride)
        self._objects(count)
        raw = self.capture("tagged-list", offset, header + count * stride)
        result = []
        for index in range(count):
            at = header + index * stride
            flags = raw[at] if wide else 0
            at += int(wide)
            result.append({"flags": flags, "tag": raw[at],
                           "operand": int.from_bytes(raw[at + 1:at + 3], "little"),
                           "opcode": raw[at + 3]})
        return result

    def mode(self, ordinal: int) -> dict:
        addresses = self.addresses(6, 3)
        if not 0 <= ordinal < len(addresses):
            raise InvalidImage(f"mode {ordinal} is outside its declared index")
        root = self.image.offset(addresses[ordinal], 6)
        count = self.image.u16(root + 4)
        self.image.read(root, 6 + 3 * count)
        self._objects(count)
        raw = self.capture("mode", root, 6 + 3 * count)
        binding = self.image.offset(int.from_bytes(raw[1:4], "little"))
        pages = []
        for index in range(count):
            address = int.from_bytes(raw[6 + 3 * index:9 + 3 * index], "little")
            page = self.image.offset(address, 6)
            fields = self.capture("mode-page", page, 6)
            list_offset = self.image.offset(int.from_bytes(fields[:3], "little"))
            program = self.image.offset(int.from_bytes(fields[3:], "little"))
            pages.append({"ordinal": index, "offset": page,
                          "bindings": self.tagged(list_offset), "program": program})
        return {"ordinal": ordinal, "offset": root, "kind": raw[0],
                "bindings": self.tagged(binding), "pages": pages}

    def key_sets(self) -> list[int]:
        return self.addresses(9, 1)

    def state_addresses(self) -> list[int]:
        key = 13, 8
        if key in self._indexes:
            return self._indexes[key]
        root = self.image.section(13)
        if root is None:
            return []
        count = self.image.u16(root)
        self.image.read(root, 8 + 3 * count)
        self._objects(count)
        raw = self.capture("state-index", root, 8 + 3 * count)
        result = [int.from_bytes(raw[at:at + 3], "little")
                  for at in range(8, len(raw), 3)]
        self._indexes[key] = result
        return result

    def state(self, ordinal: int) -> dict:
        addresses = self.state_addresses()
        if not 0 <= ordinal < len(addresses):
            raise InvalidImage(f"state {ordinal} is outside its declared index")
        root = self.image.offset(addresses[ordinal], 7)
        count = self.image.u24(root + 4)
        self._objects(count)
        header = self.capture("state-header", root, 7)
        transitions = []
        cursor = root + 7
        for _ in range(count):
            lead = self.image.u8(cursor)
            kind = lead & 0x7F
            if kind not in (0, 1, 2):
                self.capture("state-known-prefix", root, cursor - root + 1)
                raise UnsupportedNative(
                    f"state transition type {kind} has an unestablished record span")
            raw = self.image.read(cursor, 8)
            transitions.append({
                "lead": lead,
                "from": int.from_bytes(raw[1:3], "little", signed=True),
                "to": int.from_bytes(raw[3:5], "little", signed=True),
                "operand": int.from_bytes(raw[5:7], "little"),
                "opcode": raw[7],
            })
            cursor += 8
        self.capture("state", root, cursor - root)
        return {"ordinal": ordinal, "offset": root,
                "first": int.from_bytes(header[:2], "little"),
                "maximum": int.from_bytes(header[2:4], "little"),
                "transition_count": count, "transitions": transitions}

    def names(self) -> list[dict]:
        root = self.image.section(0)
        if root is None:
            return []
        if self.image.read(root, 2) != b"\xed\xfe":
            raise UnsupportedNative("unrecognised named-symbol framing")
        declared = self.image.u16(root + 2)
        raw = self.capture("named-symbols", root, declared + 2)
        if declared < 5 or raw[declared:] != b"\xef\xbe":
            raise InvalidImage("named-symbol terminator does not match its declared extent")
        result, at = [], 5
        while at < declared:
            self._objects(1)
            if raw[at] != 0xA7 or at + 7 > declared:
                raise UnsupportedNative("unrecognised named-symbol record")
            length = int.from_bytes(raw[at + 1:at + 3], "little")
            end = at + 3 + length
            if length < 4 or end > declared:
                raise InvalidImage("named-symbol record crosses its declared extent")
            result.append({"parent": int.from_bytes(raw[at + 3:at + 5], "little"),
                           "index": int.from_bytes(raw[at + 5:at + 7], "little"),
                           "name": raw[at + 7:end].decode("ascii", "replace"),
                           "name_hex": raw[at + 7:end].hex()})
            at = end
        return result

    def font(self, ordinal: int) -> dict:
        addresses = self.addresses(7, 2)
        if not 0 <= ordinal < len(addresses):
            raise InvalidImage(f"font {ordinal} is outside its declared index")
        root = self.image.offset(addresses[ordinal], 3)
        height, second, third = self.image.read(root, 3)
        first, count = (second, third) if third else (1, second)
        self.image.read(root, 3 + 3 * count)
        self._objects(count)
        raw = self.capture("font", root, 3 + 3 * count)
        return {"ordinal": ordinal, "height": height, "first": first,
                "addresses": [int.from_bytes(raw[at:at + 3], "little")
                              for at in range(3, len(raw), 3)]}

    def glyph(self, font: dict, code: int) -> list[list[int]]:
        index = code - font["first"]
        if not 0 <= index < len(font["addresses"]):
            raise UnsupportedNative(f"glyph {code} is outside its font")
        address = font["addresses"][index]
        if not address:
            raise UnsupportedNative(f"glyph {code} has no representation in its font")
        root = self.image.offset(address)
        width = self.image.u8(root)
        cursor, rows = root + 1, []
        for _row in range(font["height"]):
            leader = self.image.u8(cursor)
            cursor += 1
            if leader >> 4 != 2:
                raise UnsupportedNative("unrecognised monochrome glyph row")
            end = cursor + (leader & 15)
            self.image.read(cursor, end - cursor)
            pixels = []
            while cursor < end:
                command = self.image.u8(cursor)
                cursor += 1
                kind, count = command >> 4, (command & 15) + 1
                if kind == 5:
                    size = (count * 2 + 7) // 8
                    if cursor + size > end:
                        raise InvalidImage("literal glyph pixels cross their row")
                    packed = self.image.read(cursor, size)
                    pixels.extend((packed[index // 4] >> (6 - 2 * (index % 4))) & 3
                                  for index in range(count))
                    cursor += size
                elif kind in (6, 10):
                    pixels.extend([2 if kind == 6 else 1] * count)
                else:
                    raise UnsupportedNative(f"unrecognised glyph operation {kind}")
            if len(pixels) != width:
                raise InvalidImage("glyph pixel count does not match its declared width")
            rows.append(pixels)
        if self.image.u8(cursor) != 0:
            raise InvalidImage("glyph does not close after its declared height")
        self.capture("glyph", root, cursor + 1 - root)
        return rows

    def string(self, offset: int) -> tuple[bytes, int]:
        cursor = offset
        while self.image.u8(cursor):
            self._objects(1)
            width = 2 if self.image.u8(cursor) & 0x80 else 1
            self.image.read(cursor, width)
            cursor += width
        raw = self.capture("glyph-string", offset, cursor + 1 - offset)
        return raw[:-1], cursor + 1

    def screen(self, root: int) -> list[dict]:
        """Read a rooted screen path; conditional branches are evidence, not guesses."""
        result, cursor, visited = [], root, set()
        while True:
            if cursor in visited:
                raise UnsupportedNative("cyclic screen program")
            visited.add(cursor)
            start, opcode = cursor, self.image.u8(cursor)
            self._objects(1)
            cursor += 1
            item = {"offset": start, "opcode": opcode}
            if opcode == 0:
                self.capture("screen-instruction", start, 1)
                return result
            if opcode == 20:
                raw = self.capture("screen-instruction", start, 4)
                cursor = self.image.offset(int.from_bytes(raw[1:], "little"))
                continue
            if opcode in SCREEN_OPERANDS:
                raw = self.capture("screen-instruction", start, 1 + SCREEN_OPERANDS[opcode])
                operands = raw[1:]
                cursor += len(operands)
                item["operands_hex"] = operands.hex()
                if opcode == 4:
                    codes, _end = self.string(
                        self.image.offset(int.from_bytes(operands[2:], "little")))
                    item.update(x=operands[0], y=operands[1], codes_hex=codes.hex())
                result.append(item)
                continue
            if opcode == 5:
                x, y = self.image.read(cursor, 2)
                codes, cursor = self.string(cursor + 2)
                self.capture("screen-instruction", start, cursor - start)
                result.append({**item, "x": x, "y": y, "codes_hex": codes.hex()})
                continue
            if opcode in (18, 19):
                width = 2 if opcode == 19 else 1
                state = self.image.u8(cursor)
                cursor += 1
                branches = []
                for fields in (1, 2):
                    count = int.from_bytes(self.image.read(cursor, width), "little")
                    cursor += width
                    stride = fields * width + 3
                    raw = self.image.read(cursor, count * stride)
                    for at in range(0, len(raw), stride):
                        address = int.from_bytes(raw[at + stride - 3:at + stride], "little")
                        branches.append(self.image.offset(address))
                    cursor += len(raw)
                self.capture("screen-instruction", start, cursor - start)
                result.append({**item, "state": state, "branches": branches})
                return result
            raise UnsupportedNative(f"unrecognised screen opcode 0x{opcode:02X}")

    def evidence(self) -> list[dict]:
        return sorted(self.regions.values(), key=lambda region: (region["offset"], region["kind"]))

    def inspect(self) -> dict:
        counts, rejected = Counter(), Counter()
        errors = []

        def attempt(kind, ordinal, read):
            try:
                read()
                counts[kind] += 1
            except (InvalidImage, UnsupportedNative) as exc:
                rejected[str(exc)] += 1
                errors.append({"kind": kind, "ordinal": ordinal, "reason": str(exc)})

        if self.image.section(0) is not None:
            attempt("named_symbols", 0, self.names)
        for index, address in enumerate(self.addresses(5, 1)):
            def group(address=address):
                record = self.group(address)
                for command, pointer in enumerate(record["commands"]):
                    attempt("commands", command, lambda pointer=pointer: self.command(pointer))
            attempt("groups", index, group)
        for index in range(len(self.addresses(6, 3))):
            def mode(index=index):
                record = self.mode(index)
                for page in record["pages"]:
                    attempt("screens", page["ordinal"],
                            lambda page=page: self.screen(page["program"]))
            attempt("modes", index, mode)
        for index in range(len(self.addresses(10, 2))):
            attempt("actions", index, lambda index=index: self.action(index))
        for index, address in enumerate(self.key_sets()):
            attempt("key_sets", index,
                    lambda address=address: self.tagged(self.image.offset(address)))
        pending = [self.image.offset(address) for address in self.addresses(11, 2)]
        visited = set()
        while pending:
            root = pending.pop()
            if root in visited:
                continue
            visited.add(root)
            def screen(root=root):
                for instruction in self.screen(root):
                    pending.extend(instruction.get("branches", []))
            attempt("screen_programs", root, screen)
        for index in range(len(self.state_addresses())):
            attempt("states", index, lambda index=index: self.state(index))
        for index in range(len(self.addresses(7, 2))):
            def font(index=index):
                record = self.font(index)
                for offset, address in enumerate(record["addresses"]):
                    if address:
                        code = record["first"] + offset
                        attempt("glyphs", code,
                                lambda code=code, record=record: self.glyph(record, code))
            attempt("fonts", index, font)
        return {"scope": "supported-rooted-readers",
                "counts": dict(counts), "rejected": dict(rejected),
                "errors": errors, "complete": not rejected,
                "regions": self.evidence()}
