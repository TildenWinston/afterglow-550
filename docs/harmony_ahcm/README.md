# AHCM configurations

AHCM is the architecture-9 native configuration image identified in the Harmony 550
donor. The existing opaque `blob` carrier keeps its payload byte for byte; native
interpretation belongs to the `harmony-ahcm` backend.

- [image.md](image.md) describes measured identity, framing, logical pointers and integrity.

The profile remains **read-only**. Native structural validity proves neither firmware
acceptance nor a safe write size. An imported configuration is evidence, never a template
for a future generated configuration.
