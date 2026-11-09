"""The partial PIC18 formatter must recognize arithmetic without private firmware."""
from __future__ import annotations

from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def disassemble():
    return runpy.run_path(str(ROOT / "tools" / "harmony_one_firmware.py"))["disassemble"]


@pytest.mark.parametrize("word, expected", [
    (0x0207, "MULWF 0x07, 0"),
    (0x0307, "MULWF 0x07, 1"),
    (0x02FF, "MULWF 0xff, 0"),
    (0x03FF, "MULWF 0xff, 1"),
])
def test_multiplication_selects_its_access_bank_without_a_destination_bit(
        disassemble, word, expected):
    code = word.to_bytes(2, "little") + b"\x12\x00"

    assert list(disassemble(code, 0x1000, 0, 2)) == [
        (0x1000, expected), (0x1002, "RETURN")]


def test_the_multiplication_fix_preserves_neighboring_literal_and_byte_instructions(disassemble):
    code = b"\x04\x0d\x07\x04\x40\x50\x12\x00"

    assert list(disassemble(code, 0x1000, 0, 4)) == [
        (0x1000, "MULLW 0x04"),
        (0x1002, "DECF 0x07, W, 0"),
        (0x1004, "MOVF 0x40, W, 0"),
        (0x1006, "RETURN"),
    ]
