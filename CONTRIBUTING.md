# Contributing

## AHCM and the Harmony 550

The architecture-9 implementation lives entirely in
`src/afterglow/backends/harmony_ahcm/`. Shared callers select `harmony-ahcm` through
the backend registry and the profile, never by importing a concrete backend.
`build_project` and `import_project` are the required operations; generation remains
explicitly unsupported and the 550 profile stays read-only.

`ahcm.Image` owns immutable framing and bounded logical reads. `readers.Reader` follows
native command, mode/page, binding, action, state and font roots independently.
`labels.Text` identifies glyphs from their actual shapes. `importing` produces
`ImportResult`, canonical portable signals and explicit loss accounting.
The format and evidence boundaries are indexed under `docs/harmony_ahcm/`.

Synthetic regressions run without hardware or private configurations. Approved private
550 fixtures belong only in the ignored `configs/550/` folder. The carrier roundtrip
gate skips when none are available. `AFTERGLOW_550_CONFIG` may point the tests at one
explicitly approved private capture elsewhere, without copying it into the repository.
That option is test-only and neither changes nor publishes the source capture.

Behavioral acceptance additionally needs an owner-observed inventory beside each
configuration, named `<stem>.expected-project.json`, with `"source": "owner-observed"`
and a `"project"` object containing `"devices"` and `"activities"`. Include the observed
portable fields, labels, controls, power/input behavior and sequences: dictionaries
are compared on those fields, while lists must match in population and order.
Do not generate this oracle from the importer under test. Missing inventories skip
honestly; synthetic success is not donor or hardware acceptance.

Run the AHCM tests and the backend-boundary, removability, documentation and package-data
gates on Python 3.11 before handing off. Do not commit fixtures, inventories, decoded
glyph pixels, firmware or generated configuration images. No native structural test
authorizes a USB write or a profile promotion.
