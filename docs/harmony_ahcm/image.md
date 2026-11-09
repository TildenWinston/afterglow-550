# AHCM native image

## Evidence and scope

The Harmony 550 donor in
[harmonic-afterglow/afterglow#16](https://github.com/harmonic-afterglow/afterglow/issues/16),
as recorded in [#3](https://github.com/TildenWinston/afterglow-550/issues/3), states
architecture 9, skin 41, software type 0, flash `0xFF:0x12` and board `2.5.0`.
Its AHCM payload is 75,158 bytes, with logical end vector `0x032592`, header value
`0x1400`, `CMAH` at offset `0x5B`, and checksum bytes `9e bb`.

The current corrections in
[harmony-decompiler's format research](https://github.com/trelowney/harmony-decompiler/blob/8266840630ef521dfd39747af847924d9554d28e/docs/FORMAT.md)
(sections 4m, 5s and 5u), cross-checked there against
[harmony-explorations](https://github.com/dannybloe/harmony-explorations), explain the
framing and complete-pair checksum. Findings about a different model's keys, firmware
limits or behavior do not establish those facts for a 550.

No model aliases, firmware bounds, universally required header value, safe writable
size or hardware acceptance are inferred from this donor.

## Header and address domains

| File offset | Representation | Meaning |
|---|---|---|
| `0x00` | four bytes `AHCM` | Native image signature |
| `0x04` | little-endian u32 | Logical address of the `MCHA` end marker |
| `0x08..0x0A` | three bytes | Unknown header value; preserved verbatim |
| `0x0B` | twenty `{u8 spare; u24 address}` slots | Subsystem roots, including null slots |
| `0x5B` | four bytes `CMAH` | End of the native header |

The first slot's spare byte is at `0x0B`, where an earlier u32 reading of the unknown
header field included it. Each address is little-endian and 24 bits; its spare byte
must not be treated as part of the next address. The donor has eighteen present
roots and null slots 18 and 19. Null is absence of that subsystem, not an instruction
to stop reading the table.

The supported logical image base is `0x020000`: subtract it from a native address
to obtain a file offset. Concordance's architecture-9 physical transport configuration
address is `0x820000`, a different domain. A physical transport address is not a
valid native pointer.

An independently established linked image may be inspected with an explicit
`Image(..., logical_base=...)`. This is a bounded address view, not automatic base
inference or evidence that the image executes. Ordinary configuration validation and
profile-selected import still require the configuration base above.

Subsystem roots are not boundaries around contiguous sections. Follow owned pointers
and counts even when children precede their root, roots are not sorted, or children
are shared. Every followed span must remain within native data, after `CMAH` and
before the stored checksum. Do not scan for coincidental byte patterns.

## Native trailer and checksum

The logical end vector points to four bytes `MCHA`. The two immediately preceding
bytes store the native XOR checksum: start the two lanes at `0x21` and `0x43`, then
XOR complete byte pairs from offset zero up to, but excluding, the stored checksum.
An unpaired final byte in that span is not included. Neither the stored checksum nor
the end marker participates.

The container's separate checksum remains the XOR of every payload byte seeded at
`0x69`; it is not a substitute for native validation. Bytes following the logical
end marker are retained and reported, not silently removed.

Validation checks signatures, the logical end vector, all twenty slot addresses and
the complete-pair checksum. It preserves the unknown header value and spare bytes
rather than declaring them universal constants. A valid report is a bounded structural
finding, not proof that a remote will boot or execute the image.

When slot 1 is present, its first three bytes state the protocol twice and the skin.
Both protocol bytes must be architecture 9; a profile-selected validation also checks
the skin against the container's selected remote. Remaining identity-record bytes are
unknown and preserved rather than assigned invented meanings.
