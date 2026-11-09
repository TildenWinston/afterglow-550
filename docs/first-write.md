# Controlled first write

The separately authorized qualification route for an experimental remote, from the
command line only. Normal generated-config writes through **Flash to Remote** require
a `verified` profile. Experimental profiles permit building and exporting artifacts,
not normal writes; read-only profiles permit neither building nor normal writes.

For a Harmony 550, first establish its identity and independently validate its native
artifact before entering controlled testing. Its profile must remain read-only until
the maintainer explicitly approves experimental builds. A request to add support, an
available USB connection, or a successful build does not authorize a hardware write.
Obtain the owner's explicit permission for the candidate and target, review the
prepared report, and only then run `apply`. This workflow does not promote a profile.
Keep the same physical remote for preparation, apply, readback and recovery: the recorded
architecture, skin, flash, board, software type and firmware constrain compatibility,
but are not a unique unit identifier.

```bash
afterglow-first-write prepare candidate.ezhex \
  --recovery recovery.ezhex \
  --report first-write.json

# Review first-write.json, especially validation and changes.
afterglow-first-write apply first-write.json

# Wait until the remote has rebooted and reappeared on USB.
afterglow-first-write readback first-write.json --out readback.ezhex

# At any point after a write started: put the remote's own configuration back.
afterglow-first-write restore first-write.json
```

`prepare` requires the artifact's profile to be `experimental`. It validates the EZHex
envelope and the backend-native payload, identifies the attached remote, reads its current
configuration, makes a second recovery copy, and records hashes plus a native change report.
It refuses to overwrite any of those files.

`apply` revalidates all three files and the connected identity, then requires the exact phrase
stored in the reviewed report. Before calling the transport it durably changes the report state
to `write-started`. If the process or USB connection fails, the state becomes
`write-outcome-unknown` and another apply is refused. Determine the actual state with readback;
never retry merely because the remote disappeared while rebooting.

`readback` accepts a completed or uncertain write, checks the recorded target identity, saves a
new configuration without overwriting an existing file, validates it independently, and compares
its payload hash with the candidate. An exact match, or an independent backend
comparison confirming that all written content is present and unchanged, changes the
report to `readback-verified`. This accommodates measured runtime mutation and
repacking without accepting unexplained differences. Preserve a mismatch report and
all artifacts for investigation; do not retry the candidate write.

`readback-verified` proves the readback comparison only. It does not prove the remote
booted or that its controls work.

`restore` writes back only the recovery recorded in the report, after checking its digest and
the attached remote's identity again, and requires its own phrase. Unlike `apply`, an
uncertain restore may be repeated, including a process interrupted in `restore-started`:
writing the remote's own configuration again is the recovery itself. The application's
explicit **Settings > Flash a Configuration File** path also remains available for
putting a backup back; it is separate from the normal generated-config write gate and
must not be used to bypass qualification. Its existing identity checks and explicit
force option are unchanged.

The report is evidence for review, not an automatic promotion. Set a profile to `verified` only
after the remote also boots and the changed controls work on real equipment.
