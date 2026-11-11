# Reading AHCM behavior

## Evidence boundary

Native readers follow the explicit grammar measured by
[harmony-decompiler](https://github.com/trelowney/harmony-decompiler/blob/8266840630ef521dfd39747af847924d9554d28e/docs/FORMAT.md)
and independently described in
[harmony-explorations' format reference](https://github.com/dannybloe/harmony-explorations/tree/main/reference/config-format).
These architecture-9 structures can be read without a template or a runtime dependency
on either tool. A donor's actual behavior and a 550's physical key map still require
the owner's inventory and hardware observations. A 525/555 key name, firmware limit
or display layout is not a 550 finding.

## Owned roots

| Slot | Count and records |
|---|---|
| 0 | `FEED`, u16 terminator offset, spare byte, counted `A7` named-symbol records, `BEEF` |
| 5 | u8 group count and u24 group addresses; each group has a spare, u16 command count and u24 command addresses |
| 6 | u24 mode count and u24 addresses; each target has kind, u24 binding-list address, u16 page count and u24 page addresses |
| 7 | u16 font count and u24 font addresses |
| 9 | u8 key-map count and u24 tagged-list addresses |
| 10 | u16 action-list count and u24 action-list addresses |
| 11 | u16 screen-program count and u24 program addresses |
| 13 | four u16 header words, then u24 state-record addresses |

All followed addresses use the [logical domain](image.md), and every read is bounded
by the actual native data. Children may precede their table, be shared, or lie beyond
the next subsystem's root. Counts are read at their declared widths, not selected by
an array heuristic.

A narrow tagged list has a nonzero u8 count and `{u8 tag; u16 operand; u8 opcode}`
entries. A zero lead selects a second u8 count and wide entries with a leading flag.
An empty wide list therefore occupies two bytes. Flags remain evidence; a flagged
binding must not be mistaken for an unconditional terminating binding.

An ordinary action list is a u8 count followed by `{u16 operand; u8 opcode}` entries.
`0x7F` calls the indexed list. Calls are expanded in order, including repeated calls
to the same list; an active recursive call is reported, not silently deduplicated.
`0x7D` identifies an IR group in the operand's high byte and a command in its low byte.
`0x7C` is a per-device quantity in tenths of a second, not an unconditional global pause.

## Class-5 IR evidence

A command address lands on its class byte. For class 5, the following u24 points to
the record's own start, whose class byte is seven bytes later. The record carries a
spare, u24 period and on-time in nanoseconds, class, self pointer, u8 pointer-group
count, then three u24 body pointers per group. Null phase pointers remain significant.

A body has u24 symbol-table address, u16 index count and that many u8 indices.
The symbol table is a u8 count and u24 symbol addresses. A symbol is a u16 pulse count
followed by that many u16 words; bit 15 distinguishes mark from space and the low bits
carry microseconds. The normally stored word after those pulses is not consumed by
the measured dictionary reader, so its value is preserved without imposing a stricter
runtime gate. An empty index stream does not dereference its symbol-table address.

All groups and phase boundaries are retained. More than two pointer groups, unknown
classes, zero-duration words and phase shapes that the portable envelope cannot express
remain opaque with a reason rather than being normalized away. The actual 550 emitter's
toggle initialization and behavior still require observation.

Storage words are not physical pulse boundaries: a long space occupies several words
of the same polarity. A portable waveform coalesces adjacent words into the physical
interval and keeps press, hold and release separate. A press's initial silence is a
transmitter lead-in, not an optical burst interval. It is retained per recording as
`native[harmony-ahcm].leading_silence_us`, with the exact original words, and reported
as native-only timing rather than silently discarded or assigned to a universal device
delay. Held or release phases with an unexplained initial silence stay opaque.

The inspector has software budgets of one hundred thousand decoded reference/list
items and string codes, one million expanded duration words, one million expanded
action steps and one million exported portable pulses. These bound sharing and
compression amplification, not the firmware. Hitting a budget is an inspection
limitation, not native invalidity or a remote capacity claim; the original bytes
remain unchanged. Cached dictionaries are counted once, but repeated action calls
still execute and consume expansion work.

## State and text

The state table's words are total count, narrow count, wide count and narrow count again.
Its records contain u16 initial value, u16 maximum and a u24 transition count.
The count's high byte is not an unrelated spare. The established fixed-size layouts
(transition types 0, 1 and 2, selected by the lead byte's low seven bits) contain
eight bytes: lead, signed u16 from/to values, u16 operand and opcode. Flags remain
raw evidence, not automatically portable behavior.

Other transition types do not have an established fixed-size span in this reader.
They are reported as inspection limitations rather than read with an assumed
eight-byte stride that could turn following data into invented transitions.
The complete native payload remains evidence, and one unreadable state does not
discard its siblings. Transition-count work consumes the software object budget;
it is not a native capacity claim. Complex transition execution and sentinel behavior
must not be replaced with guessed portable state behavior.

A font starts with height, first code and count. When the third byte is zero, the second
byte is the count and the first code is 1. Each non-null u24 glyph pointer names a width
and exactly the font's height of counted rows, followed by zero. Pixel counts must
close at the glyph width. A glyph need not precede its font table.

Row leaders use the `0x20..0x3F` family: their low five bits count operation bytes,
not pixels. Clear (`0x80..0x9F`), ink (`0xA0..0xBF`) and transparent skip
(`0x60..0x7F`) runs use a five-bit pixel count plus one. Literal operations carry
one-bit (`0x4n`) or two-bit (`0x5n`) MSB-first source pixels, with `n + 1` pixels.
Zero clears, one sets ink, and either two-bit value with its high bit set leaves
the destination unchanged. The reader retains both two-bit values without merging
their original representation; unsupported operations remain explicit limitations.

Glyph codes are identified by the actual decoded shape and an unambiguous reference
fingerprint, not by ordinal or repertoire size. The bundled catalogue contains
fingerprints only; its attribution is beside it. Ambiguous and unknown shapes,
unestablished extended codes and conditional screen labels are reported. No donor label
is guessed.

Known screen instructions are followed from mode pages or slot 11, including shared
jump tails. Strings are read at glyph-code boundaries. Unknown opcodes and cyclic
programs stop that interpretation with evidence retained, rather than scanning for
another plausible instruction.

## Portable import and explicit limitations

`ImportResult` contains canonical `afterglow-project-device/1` devices, waveform
signals for faithfully represented class-5 recordings, and backend-opaque signals
for other commands. A rejected command does not discard its siblings. The complete
payload is retained as base64 evidence, with rooted raw regions, failure locations,
counts and warnings. The shared import dispatcher carries those warnings to Import
Complete. Neither that evidence nor a prior image is a build template.

Device names are read from uniquely owned named-state relationships rather than
guessed from brands or another configuration. Commands are named by readable labels
or established physical controls, otherwise by their native ordinal. An unambiguous
rooted command mode is required before controls are projected.

A profile may declare `vocabulary.hard_key_codes` as canonical key name to u8 event
code, and `vocabulary.screen_key_cells` as `{code, bounds: [x, y, width, height]}`
entries in page order. These declarations must come from evidence for that model.
The initial 550 profile declares neither: unknown key codes and LCD cells are
preserved and reported rather than populated with a 525's answer.

Page bindings override a mode's own bindings, which override the running key set.
A hard key is projected only when its effective action agrees across every page.
Flagged search behavior, page-dependent routes and device-key macros that cannot be
represented are reported. A key's repeated calls are not deduplicated.

Simple binary power transitions and transition-independent direct inputs can be
represented through their owned commands. Multi-state input selection, unestablished
transition sentinels, per-device queue delays, repick behavior, conditional action
flow, platform preferences and unsupported event types remain native evidence with
warnings. A per-device quantity is never relabelled as a global pause. Activity values
come from the named counter and their enter handlers, with its initial value defining
idle; unresolved activity labels remain plain placeholders.

Framing-only validation reports its scope explicitly. A rooted inventory's
`complete` flag refers only to the supported readers attempted, not to every native
subsystem, portability, bootability or hardware acceptance.
