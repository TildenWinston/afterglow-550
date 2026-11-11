# AHCM platform event handoff

## Scope and evidence

Private owner-approved offline architecture-9 analysis supports the bounded
event-table selection and record-staging contract below. Synthetic image-local
host probes exercise the measured selection, pointer/index arithmetic and record
staging with raw external-byte reads, transaction setup and the action-queue sink
abstracted. Both linked and user configuration views and count/index carry cases
are covered. This is conditional host-model evidence, not hardware execution.
Low-access temporaries are modeled with ordinary PIC18 access addressing; the
physical instruction-extension configuration remains unobserved. Peripheral
transaction behavior is not executed by those probes.

The public implementation is independently authored from that contract. It
contains no firmware bytes, recovered programs or donor setup. Root framing and
logical address domains are described in [image.md](image.md).

## Counted inline actions

Root slot 8 points to a u8 count followed by that many inline action records:
little-endian u16 operand, then u8 opcode. It is not a u24 pointer vector and is
not the counted action-list representation.

An event selector smaller than the count chooses its ordinal record. A selector
equal to or larger than the count stages nothing. The selected three bytes are
staged directly; no further pointer is dereferenced. An all-zero record remains
a stored action, distinct from an absent table or out-of-range selector. Whether
the queue accepts or executes that record belongs to the action runtime.

`platform.event_action` follows the image's explicit logical base, checks only
the selected record span, and reports a missing root rather than inventing a
default table. A malformed unselected record is not made a stricter prerequisite
than the measured selection path. Truncated selected records are refused.

`platform.encode_event_table` packs an explicit ordered sequence of `EventAction`
values. It supplies no default opcode, startup action or initializer. Its u8 count
and operand/opcode field limits are storage-width constraints, not safe firmware
capacity or hardware-acceptance limits.

## Remaining mandatory platform evidence

An event table is only one handoff. Its presence does not establish a complete
startup sequence, configuration selection/recovery behavior, state/cache
initialization, peripheral setup, display geometry, numeric physical-key identities,
glyph/drawing execution, or a generated platform's required records. An observed
selector request does not prove that supplying that record is sufficient to boot.

The backend remains read-only. This primitive does not enable project generation,
normal writes, firmware operations, status promotion or template-based construction.

## Explicit monochrome glyph sources

The [glyph reader](reading.md) retains clear, ink and transparent source values.
Scoped synthetic image-local probes additionally corroborate the five-bit row/run
counts, clear/ink/transparent effects and one-/two-bit literal packing against the
captured renderer's RAM-painting path. The probes abstract raw byte input and
transaction setup and model ordinary access addressing. They do not operate an LCD
or establish physical geometry, clock/peripheral behavior or glyph-code identities.

`screens.encode_literal_glyph` constructs only caller-authored two-bit source rows:
zero clears, one sets ink, and two or three leave the destination unchanged.
It preserves those source values exactly rather than flattening transparent pixels
onto a guessed background. Width is explicit, and the caller must supply the
matching height in its separately constructed font.

The encoder emits only literals, not compression/backreference defaults. A row
whose literal operations cannot fit the five-bit operation-byte length is reported
as an unsupported encoding choice, not declared invalid for the remote: another
compression form can represent a wider simple row. Width/height field checks are
storage bounds, not a display-size or safe-image-size claim.

Unknown glyph operations remain errors, and no existing glyph artwork, startup
program, screen or household configuration is copied by this primitive. It neither
assembles a complete font/screen platform nor changes the read-only gates.
