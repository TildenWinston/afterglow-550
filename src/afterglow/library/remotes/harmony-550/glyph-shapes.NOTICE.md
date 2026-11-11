# Glyph fingerprint attribution

`glyph-shapes.json` contains character-identification fingerprints, not glyph pixels.
The fingerprint-to-character entries are from `tools/arch9_glyphs.json` in
[trelowney/harmony-decompiler](https://github.com/trelowney/harmony-decompiler/blob/8266840630ef521dfd39747af847924d9554d28e/tools/arch9_glyphs.json),
distributed under GPL-3.0-or-later, the same license as Afterglow.

These are reference shapes from architecture-9 research, not a fixed glyph-code map
or proof that every 550 font uses those shapes. A character is identified only when
the actual image's glyph has an unambiguous matching fingerprint. Unknown and
ambiguous shapes remain unreadable and are reported.

No configurations, household labels, firmware, glyph pixels or other vendor artwork
are included in this catalogue.
