"""Report byte changes and evidenced transport extents without asserting runtime equivalence.

Rooted reads describe consumed spans, never contiguous subsystem ownership.

Format reference: docs/harmony_ahcm/preparation.md
"""
from __future__ import annotations

import hashlib

from ... import remotes
from . import ahcm
from .readers import Reader, UnsupportedNative

SOURCE = "harmonic-afterglow/concordance@830dd1220809b3485fee7a8295d3d66da2e1e62c"
CONFIG_START, CONFIG_END, SECTOR_BYTES = 0x820000, 0x880000, 0x10000
CHANGE_RANGE_LIMIT = 128


def _validated(payload: bytes, profile) -> ahcm.Image:
    if profile is not None and profile.arch != 9:
        raise ahcm.InvalidImage("AHCM inspection requires an architecture-9 profile")
    image = ahcm.Image(payload)
    if (profile is not None and image.identity is not None
            and image.identity["skin"] != profile.skin):
        raise ahcm.InvalidImage("native skin conflicts with the selected remote profile")
    return image


def validate_payload(payload: bytes, profile=None) -> dict:
    """Keep the established framing/identity validator shared by all preparation hooks."""
    return _validated(payload, profile).report()


def _unread_spans(start: int, end: int, regions: list[dict]) -> list[dict]:
    spans = sorted((max(start, item["offset"]),
                    min(end, item["offset"] + item["size"])) for item in regions)
    result, cursor = [], start
    for left, right in spans:
        if right <= cursor:
            continue
        if left > cursor:
            result.append({"offset": cursor, "size": left - cursor})
        cursor = max(cursor, right)
    if cursor < end:
        result.append({"offset": cursor, "size": end - cursor})
    return result


def _inventory(image: ahcm.Image) -> dict:
    reader = Reader(image)
    try:
        inspected = reader.inspect(reject_invalid=True)
    except UnsupportedNative as exc:
        inspected = {
            "counts": None, "rejected": {str(exc): 1},
            "errors": [{"kind": "inspection", "reason": str(exc)}],
            "complete": False, "regions": reader.evidence(),
        }
    regions = [
        {"kind": item["kind"], "offset": item["offset"], "size": item["size"],
         "sha256": hashlib.sha256(
             image.read(item["offset"], item["size"])).hexdigest()}
        for item in inspected["regions"]
    ]
    identity = image.section(1)
    if identity is not None:
        regions.append({"kind": "native-identity", "offset": identity, "size": 3,
                        "sha256": hashlib.sha256(image.read(identity, 3)).hexdigest()})
    return {
        "scope": "supported-rooted-reads-not-section-ownership",
        "counts": inspected["counts"],
        "counts_scope": ("completed-supported-attempts" if inspected["counts"] is not None
                         else "unavailable-after-inspection-aborted"),
        "unsupported": inspected["rejected"],
        "limitations": inspected["errors"],
        "supported_reads_complete": inspected["complete"],
        "regions": regions,
        "uninspected_present_roots": [
            slot.index for slot in image.slots
            if slot.offset is not None and slot.index not in (0, 1, 5, 6, 7, 9, 10, 11, 13)
        ],
        "unclassified_data_spans": _unread_spans(
            ahcm.HEADER_SIZE, image.checksum_offset, regions),
        "trailing_bytes": len(image.payload) - image.end_offset - 4,
        "runtime_equivalence": "not-established",
    }


def compare_payloads(recovery: bytes, candidate: bytes, profile) -> dict:
    """Validate both frames and supported reads, retaining explicit interpretation gaps."""
    before, after = (_validated(payload, profile) for payload in (recovery, candidate))
    inventories = {"recovery": _inventory(before), "candidate": _inventory(after)}
    ranges, total_ranges, changed_bytes, start = [], 0, 0, None
    for index in range(max(len(recovery), len(candidate)) + 1):
        differs = (index < max(len(recovery), len(candidate))
                   and (index >= min(len(recovery), len(candidate))
                        or recovery[index] != candidate[index]))
        if differs:
            changed_bytes += 1
            if start is None:
                start = index
        elif start is not None:
            total_ranges += 1
            if len(ranges) < CHANGE_RANGE_LIMIT:
                ranges.append({"offset": start, "size": index - start})
            start = None
    roots = [
        {"slot": old.index, "before_address": old.address, "after_address": new.address,
         "before_spare": old.spare, "after_spare": new.spare}
        for old, new in zip(before.slots, after.slots)
        if old.address != new.address or old.spare != new.spare
    ]
    reads = {
        side: {(item["kind"], item["offset"], item["size"]): item
               for item in inventory["regions"]}
        for side, inventory in inventories.items()
    }
    old_reads, new_reads = reads["recovery"], reads["candidate"]
    common = old_reads.keys() & new_reads.keys()
    return {
        "kind": "ahcm-byte-and-rooted-read-comparison",
        "scope": "byte-differences-and-supported-structure-only",
        "before_size": len(recovery), "after_size": len(candidate),
        "before_sha256": hashlib.sha256(recovery).hexdigest(),
        "after_sha256": hashlib.sha256(candidate).hexdigest(),
        "identical": recovery == candidate,
        "changed_bytes": changed_bytes, "changed_ranges": ranges,
        "changed_range_count": total_ranges,
        "omitted_change_ranges": total_ranges - len(ranges),
        "root_changes": roots,
        "header_value_changed": before.header_value != after.header_value,
        "inventories": inventories,
        "rooted_read_changes": {
            "matching": "kind-offset-size-not-semantic-object-identity",
            "added": [new_reads[key] for key in sorted(new_reads.keys() - old_reads.keys())],
            "removed": [old_reads[key] for key in sorted(old_reads.keys() - new_reads.keys())],
            "changed": [
                {"kind": key[0], "offset": key[1], "size": key[2],
                 "before_sha256": old_reads[key]["sha256"],
                 "after_sha256": new_reads[key]["sha256"]}
                for key in sorted(common)
                if old_reads[key]["sha256"] != new_reads[key]["sha256"]
            ],
            "unchanged": sum(old_reads[key]["sha256"] == new_reads[key]["sha256"]
                             for key in common),
        },
        "ownership": "not-inferred-from-root-order",
        "runtime_equivalence": "not-established",
        "hardware_acceptance": "not-established",
    }


def configuration_write_extent(payload: bytes, profile) -> dict:
    """Bound the complete payload and sector erase for the declared arch/flash geometry."""
    image = _validated(payload, profile)
    if (profile is None or profile.arch != 9
            or remotes.comparable("flash", profile.flash) != (0xFF, 0x12)):
        raise ahcm.InvalidImage("configuration transport geometry is not established for profile")
    size = len(payload)
    if size > CONFIG_END - CONFIG_START:
        raise ahcm.InvalidImage("complete payload exceeds the declared configuration transport range")
    # Python integers cannot wrap; compare before forming addresses used by native u24 commands.
    end = CONFIG_START + size
    erase_end = CONFIG_START + ((size + SECTOR_BYTES - 1) // SECTOR_BYTES) * SECTOR_BYTES
    if not CONFIG_START < end <= erase_end <= CONFIG_END <= 1 << 24:
        raise ahcm.InvalidImage("configuration write/erase interval violates declared geometry")
    inventory = _inventory(image)
    return {
        "scope": "intended-configuration-transport-not-executable-size-qualification",
        "evidence": SOURCE,
        "geometry": {"arch": 9, "flash": [0xFF, 0x12], "configuration_start": CONFIG_START,
                     "configuration_end_exclusive": CONFIG_END, "sector_bytes": SECTOR_BYTES},
        "payload_bytes": size,
        "write": {"start": CONFIG_START, "end_exclusive": end},
        "erase": {"start": CONFIG_START, "end_exclusive": erase_end,
                  "sectors": (erase_end - CONFIG_START) // SECTOR_BYTES},
        "erased_but_not_written": {"start": end, "end_exclusive": erase_end},
        "untouched_configuration_tail": {"start": erase_end, "end_exclusive": CONFIG_END},
        "supported_reads_complete": inventory["supported_reads_complete"],
        "uninspected_present_roots": inventory["uninspected_present_roots"],
        "trailing_payload_bytes": inventory["trailing_bytes"],
        "side_effects": ["library initialization USB-connection reset",
                         "native update clock/state synchronization even with noreset=1",
                         "separate Python-requested device reboot by default",
                         "device runtime nonvolatile effects not excluded"],
        "runtime_equivalence": "not-established",
        "hardware_acceptance": "not-established",
    }
