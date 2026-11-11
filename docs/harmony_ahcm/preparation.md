# AHCM offline preparation reports

## Scope and policy

The backend supplies `compare_payloads(recovery, candidate, profile)` and
`configuration_write_extent(payload, profile)` for controlled-write preparation.
Both are pure offline inspection/reporting; neither enables construction,
changes a profile, opens USB or authorizes a write. The shipped 550 stays read-only.
The [controlled first-write process](../first-write.md) still requires its existing
experimental-profile and owner-confirmation gates.

## Byte changes versus rooted interpretation

The comparison independently validates both native frames, identity agreement
where stated, and supported rooted reads. Invalid selected structures fail explicitly.
An unsupported native class/operation or software inspection budget remains a counted
interpretation limitation rather than invalidity or successful semantic equivalence.
Ordinary inspection retains its evidence-first behavior; strict preparation inspection
re-raises malformed bounded reads, not `UnsupportedNative`.

The report includes exact payload sizes/digests, changed byte counts and file-offset
runs, root pointer/spare changes, and hashes of consumed rooted spans. Large numbers
of change runs are summarized with an explicit omitted-run count. It emits no raw
structure bytes, glyphs, command labels or household inventory.

Roots are not contiguous section boundaries. A consumed span can be shared or lie
before its root; its hash describes those bytes only. Present roots not inspected,
unsupported structures, unclassified data spans and bytes after the native end marker
remain visible. Their contents are not silently dropped or labeled unchanged
behavior. Matching bytes/checksums do not prove execution, startup, optical output,
safe size or hardware acceptance. Readback remains exact-payload comparison unless a
separately evidenced backend readback policy is implemented; this change adds none.

## Evidenced configuration transport geometry

The extent hook recognizes only architecture 9 with the profile-declared flash
manufacturer/device `0xff:0x12`. Other geometries are refused, not inferred from a
model alias, donor length or live capacity default. Evidence is the pinned
[Concordance arch/flash table](https://github.com/harmonic-afterglow/concordance/blob/830dd1220809b3485fee7a8295d3d66da2e1e62c/libconcord/remote_info.h)
and [erase implementation](https://github.com/harmonic-afterglow/concordance/blob/830dd1220809b3485fee7a8295d3d66da2e1e62c/libconcord/remote.cpp).

That transport's configuration interval is `[0x820000, 0x880000)`, with 64-KiB
sectors. The write begins at its configuration base and uses the **entire binary
payload length**, including any preserved bytes after the native end marker.
The end is checked with non-wrapping host arithmetic against the declared interval
and native address-field width. Erase is rounded outward to whole sectors.
The report separately records erased-but-not-written bytes and the untouched
configuration tail. A shorter write neither erases nor preserves the entire prior
configuration region as one unit.

These are intended transport-address/range checks, not a universal executable-image
size limit or proof that firmware honors the request. Normal firmware and preceding
flash are outside that declared configuration interval; no device write is performed
by the hook. An oversized payload or unknown geometry fails before transport use.

## Candidate and original recovery remain independent

The shared first-write layer calls an optional backend extent hook while validating
each artifact. Reports retain distinct candidate and original-recovery extents; a
smaller candidate cannot redefine the restore footprint. Recovery is the exact
captured EZHex, copied/hash-checked without normalization or regeneration.

Apply revalidates the recorded candidate/recovery bytes and any extent report before
confirmation/transport. Restore independently revalidates the recorded original and
its extent; it does not require the candidate to remain usable. A missing/stale extent
from an older report cannot silently authorize an operation after a backend adds this
hook: review/reprepare appropriately without bypassing the gate.

Fake-only regressions exercise preparation, write-started persistence, interrupted
candidate refusal, exact readback, original restore and existing retry rules.
Synthetic experimental profile values exist only inside those tests, never as a
shipped 550 promotion. Matching identities constrain compatibility, not unique unit
identity or demonstrated recovery after malformed configuration.

## Disclosed unchanged transport effects

The report identifies the existing library initialization USB-connection reset,
native configuration update's clock/state synchronization even with `noreset=1`,
and the Python wrapper's separate requested reboot by default. These differ from
each other; the hooks do not suppress or add them. Firmware-internal nonvolatile
effects during startup/runtime are not excluded by a configuration-only host call.

Independent backup media, stable power, exact-unit recovery feasibility, candidate
behavior review and fresh candidate/recovery-specific consent remain owner/test
preconditions. Offline report success resolves tooling, not those preconditions.
