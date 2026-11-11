# AHCM configurations

AHCM is the architecture-9 native configuration image identified in the Harmony 550
donor. The existing opaque `blob` carrier keeps its payload byte for byte; native
interpretation belongs to the `harmony-ahcm` backend.

- [image.md](image.md) describes measured identity, framing, logical pointers and integrity.
- [reading.md](reading.md) describes rooted native readers, IR, state and glyph evidence.
- [ir-runtime.md](ir-runtime.md) describes bounded nominal IR consumption and its
  explicit physical-timing and scheduler limits.
- [platform.md](platform.md) describes the bounded platform-event action handoff.

The profile remains **read-only**. Native structural validity proves neither firmware
acceptance nor a safe write size. An imported configuration is evidence, never a template
for a future generated configuration.
