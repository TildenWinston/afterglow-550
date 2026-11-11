# Bounded class-5 IR reference

The backend's `ir_runtime` module models nominal register transformations and a
bounded class-5 producer. It is an independent reference for testing encoders, not
a transmitter, a complete firmware emulator or a generation capability gate.

The constraints below were checked with independently authored synthetic inputs
against bounded offline application-loader, producer and timer-consumer probes.
The source and derived traces remain private. The assessed application mapping
and ordinary, nonextended access semantics are assumptions of those probes.
Neither current-running-image correspondence nor physical oscillator configuration,
interrupt latency, optical output or physical key lifetime is established.

## Carrier fields and nominal precision

The class-5 loader retains only the low sixteen bits of each u24 period/on-time
field. The nominal conversion adds 125 with sixteen-bit wrap, divides by 250,
and retains one byte. The period register additionally subtracts one before its
byte truncation. For example, period 26315 and on-time 13157 produce period
register 104 and duty register 53.

`carrier_registers(period_ns, on_ns)` exposes these register values, field
truncation and arithmetic wrap. Its nominal rounding quantum is 250 ns **in the
input field's representation**, not an independently measured timer period.
Changing an assumed clock to make an example look correct is not qualification.
The API deliberately does not report a physical frequency or safe carrier range.
Zero/sub-quantum periods and wide values can wrap into plausible registers; that
does not make them supported commands.

## Atomic dictionary admission, not a universal symbol ceiling

One symbol is checked against the buffer's current queued-byte count before any
of its words are copied. The comparison uses `254 - queued_bytes` in sixteen-bit
arithmetic and twice the u16 symbol-word count, also in sixteen-bit arithmetic.

`symbol_admission(word_count, queued_bytes)` reports both the actual required
byte count and the wrapped comparison operand. A two-word symbol fits at queued
count 250 and retries unchanged at 251. A 127-word symbol fits in the tested
empty-buffer state but not with two bytes already queued. This is not a universal
maximum-validity rule: doubling count 32768 wraps to zero and admits under the
same comparison. Abnormal queued counts can also wrap the subtraction.

The rooted reader remains lossless and does not reject native evidence by this
provisional producer model. `Class5Stream` explicitly refuses wrapped admission
instead of claiming that the comparison proves such a symbol safe to stream.
Splitting dictionary symbols to permit streaming is separate from splitting a
long duration into storage words; no complete command writer is qualified here.

## Timer words are not all ordinary durations

`timer_word(word, timer_count=..., duty_register=..., phase_complete=...)`
describes the bounded timer-consumer effect:

- Unmarked zero stops the transmitter.
- Unmarked one is a continue token while a phase is incomplete, and stops after
  completion. It does not update the duty register when continuing.
- Other words select carrier-on/off by the high bit and subtract six from their
  low-fifteen-bit value before updating the sixteen-bit timer count.

The subtraction and preload can wrap. Short unmarked durations therefore collide
with controls or underflow; marking the same low value changes control dispatch.
The six-tick compensation is not a measured interrupt cost, and its presence does
not prove that concatenating storage words preserves physical timing.

In particular, a storage-only duration splitter can represent a long silence
exactly as numbers while leaving a final unmarked word of one. That word is not
executed as an ordinary one-unit silence. Round-tripping through the portable
waveform reader alone is insufficient to qualify that encoding for transmission.

## Recording selection and phase lifecycle

`RecordingSelector.select(group, recordings)` models a volatile per-group toggle
at **dequeue**, not request acceptance or retry. The selection bit uses the group
byte's low nibble. A fresh selector chooses the first of exactly two recordings;
the next selection in that group chooses the second. Other groups have independent
bits, with low-nibble aliases retained rather than interpreted as a safe group
capacity. A one-recording command still advances the group's bit. This is not a
claim about persistence, a reserved native state-variable index, or the physical
key release that causes a new dequeue.

`Class5Stream.from_command(reader, address, recording=...)` follows rooted native
records and retains dictionary-symbol boundaries. Its constructor also accepts
synthetic press/hold/release symbol sequences; `None` means an absent body, whereas
an empty sequence means a present body with no symbols.

`step(queued_bytes=..., held=...)` produces one complete symbol or a phase
boundary. Blocked symbols retry without advancing. With no held flag at the press
boundary, the hold phase is skipped. A present hold body reloads while held;
an absent hold body does not. Leaving a held body with a present release body
inserts a stop token and suspends in `drain`. Release resumes only through
`resume_after_drain(queued_bytes=0, transmitter_stopped=True)`. It cannot be
modeled as simply concatenating press, repeat and release bytes.

Terminal stop tokens are modeled separately from dictionary admission, including
their byte-counter wrap. They are not implicitly protected by a symbol's capacity
check. The caller must bound repeated stepping, especially a held empty body.
This reference does not simulate timer interrupts, real-time draining, accepted
key events, intercommand delays or complete scheduler behavior.

## Remaining generation boundary

No capability, status or write gate is enabled by these APIs. Physical clock and
timing correspondence, interrupt scheduling, exact key/hold/release ingress and
error recovery still block a claim of equivalent emitted waveforms. Imported
payloads and the embedded bank remain evidence, never generator templates.
