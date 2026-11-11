# AHCM allocation and relocation

## Scope

The native frame and logical pointer grammar are described in [image.md](image.md).
The backend's `layout` module constructs that frame from explicitly supplied bytes.
It is an offline assembly primitive, not a project-to-configuration generator and
not evidence that a generated platform boots on a Harmony 550.

No firmware screens, mandatory platform records, key codes, monochrome drawing
instructions, idle behavior or IR/activity semantics are supplied by this primitive.
Those require their own measured evidence. It does not choose a scaffold mode,
ship platform assets, read an imported configuration or promote a remote's status.

## Explicit frame inputs

The unknown three-byte header value and all twenty spare bytes are caller inputs;
neither is assigned an invented universal default. Every subsystem root must be
declared, including null roots. Requiring explicit declarations is a construction
API invariant, not a claim that every subsystem is mandatory at runtime.

Allocations contain immutable bytes and a name and owner supplied by their backend
producer. Bytes immediately after `CMAH`, as anywhere else in native data, are
ordinary explicit allocations. Bytes after `MCHA` may be supplied separately and
are retained. No unknown bytes are normalized, reordered within their allocation,
or changed except at explicitly registered pointer fields.

## Placement and references

Allocations are placed immediately after the header in request order, without
automatic padding or a borrowed address-window rule. A relocation identifies its
source allocation, three-byte field offset, target allocation, offset within the
target and the size of the referenced span. Targets may be registered later than
their referring structures. Null references are explicit.

The referenced span must fit its named allocation; a zero-size reference may name
its end. Pointer fields must fit their source allocation and must not overlap.
Addresses are computed in the native logical domain from `0x020000`, not in the
physical USB transport address domain. A relocation must fit its u24 field and the
end vector its u32 field. These field-width checks are **not** a safe execution-size
limit or a flash-capacity qualification.

After relocation, assembly seals the native complete-pair checksum and validates
the frame with the independent native reader. Repeated assembly of the same explicit
inputs is byte-identical; the assembler reads no clock or source configuration.

## Ownership evidence

The assembly result records each named allocation and every declared reference.
Section roots may be unsorted, share targets or reach children placed before them.
They do not define exclusive contiguous ownership ranges.

Reports therefore describe **allocations and references**, with the full native
validation result. `hardware_acceptance` remains `not-established`. A structurally
valid assembly does not bypass the profile's read-only project-build and write gates.

## Observed Devices-menu constraint

On a live Harmony 550 reporting firmware 2.6, the owner checked the existing Devices
LCD menu with USB disconnected, without starting an activity:

- Four selectable slots are visible, two on each side, selected by four side buttons.
- The left/right arrow buttons page that Devices menu.

These are constraints for that menu, not evidence of pixel dimensions, numeric native
event IDs, native LCD-cell/control associations or every activity/menu context.
They do not enable new profile capabilities or provide an encoded page implementation.

An authorized private capture passes the current identity and native-framing checks.
Repeated identical reads establish capture consistency, not independent configurations
or generated-image execution. No mandatory carried platform structure has yet been
qualified, and the bundled-versus-synthesized platform decision remains unresolved.

## IR duration-word primitive

`ir_emit.duration_words` packs the signed microsecond intervals described in
[reading.md](reading.md) without choosing command-record or transmitter defaults.
Intervals exceeding the storage word's magnitude are split into consecutive words
of the same polarity. The independent converter coalesces them back into the
represented interval. No zero-duration words or truncated intervals are emitted.
Explicit recording lead-in words are retained; they are not silently reassigned to
a device delay or discarded.

The encoder's configurable word budget bounds software allocation, not native
validity or hardware capacity. It does not choose a symbol-size ceiling, allocate
dictionary records, initialize toggles or qualify press/hold/release behavior.
Those are separate construction and evidence gates. Word packing does not enable
project generation or change the profile's read-only status.
