"""Controlled AHCM preparation uses invented bytes and fake transports, never USB."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from afterglow import concord, ezhex, first_write, remotes
from afterglow.backends.harmony_ahcm import ahcm, backend, layout, readers, reporting
from afterglow.backends.harmony_ahcm.readers import Reader


def _payload(*, size=247, value=0x5A, extra_root=None, extra=b"") -> bytes:
    planned = layout.Layout(header_value=0x030201, spares=range(20))
    for slot in range(20):
        planned.root(slot, "identity" if slot == 1 else "extra" if slot == extra_root else None)
    planned.add("identity", b"\x09\x09\x29", owner="identity")
    if extra_root is not None:
        planned.add("extra", extra, owner="synthetic")
    planned.add("unclassified", bytes((value,)) * max(
        0, size - ahcm.HEADER_SIZE - 3 - len(extra) - 6), owner="synthetic")
    return planned.assemble().payload


def _file(path, payload, profile=None):
    profile = profile or remotes.get("harmony-550")
    path.write_bytes(ezhex.header_for_profile(
        profile, len(payload), ezhex.checksum(payload)) + payload)
    return path


def test_native_changes_and_unclassified_data_are_not_reported_as_runtime_equivalence():
    before, after = _payload(value=1), _payload(value=2)
    report = backend.compare_payloads(before, after, remotes.get("harmony-550"))

    assert not report["identical"]
    assert report["changed_bytes"] == sum(a != b for a, b in zip(before, after))
    assert report["root_changes"] == []
    assert report["inventories"]["candidate"]["unclassified_data_spans"]
    assert report["ownership"] == "not-inferred-from-root-order"
    assert report["runtime_equivalence"] == report["hardware_acceptance"] == "not-established"
    assert "hex" not in json.dumps(report)


def test_unknown_roots_and_unsupported_commands_remain_visible_not_invented_sections():
    unknown = _payload(extra_root=18, extra=b"\xCA\xFE")
    # One group/command class intentionally unsupported by the independent reader.
    planned = layout.Layout(header_value=1, spares=range(20))
    planned.add("group-index", b"\x01" + bytes(3), owner="synthetic")
    planned.add("group", b"\x00\x01\x00" + bytes(3), owner="synthetic")
    planned.add("unknown-class", b"\xFA", owner="synthetic")
    planned.relocate("group-index", 1, "group", size=3)
    planned.relocate("group", 3, "unknown-class")
    for slot in range(20):
        planned.root(slot, "group-index" if slot == 5 else None)
    unsupported = planned.assemble().payload
    result = backend.compare_payloads(unknown, unsupported, remotes.get("harmony-550"))

    assert result["inventories"]["recovery"]["uninspected_present_roots"] == [18]
    assert not result["inventories"]["candidate"]["supported_reads_complete"]
    assert result["inventories"]["candidate"]["unsupported"] == {"unsupported IR class 250": 1}
    assert {item["slot"] for item in result["root_changes"]} == {1, 5, 18}


def test_strict_preparation_inspection_rejects_bad_selected_structures_without_changing_inspection():
    planned = layout.Layout(header_value=1, spares=[0] * 20)
    planned.add("fonts", b"\x01\x00" + bytes(3), owner="synthetic")
    planned.add("truncated-font", b"\x01", owner="synthetic")
    planned.relocate("fonts", 2, "truncated-font", size=1)
    for slot in range(20):
        planned.root(slot, "fonts" if slot == 7 else None)
    malformed = planned.assemble().payload
    image = ahcm.Image(malformed)

    assert not Reader(image).inspect()["complete"]
    with pytest.raises(ahcm.InvalidImage):
        Reader(image).inspect(reject_invalid=True)
    with pytest.raises(ahcm.InvalidImage):
        backend.compare_payloads(_payload(), malformed, remotes.get("harmony-550"))


@pytest.mark.parametrize("kind", ["truncated", "checksum", "identity"])
def test_change_reporting_refuses_invalid_frames_and_conflicting_native_identity(kind):
    payload = bytearray(_payload())
    if kind == "truncated":
        payload = payload[:-1]
    elif kind == "checksum":
        payload[-6] ^= 1
    else:
        image = ahcm.Image(bytes(payload))
        payload[image.section(1) + 2] = 42
        payload[image.checksum_offset:image.end_offset] = ahcm.checksum(
            bytes(payload), image.checksum_offset)
    with pytest.raises(ahcm.InvalidImage):
        backend.compare_payloads(_payload(), bytes(payload), remotes.get("harmony-550"))


@pytest.mark.parametrize("length,sectors", [
    (247, 1), (65536, 1), (65537, 2), (75158, 2), (393216, 6),
])
def test_exact_complete_payload_length_determines_write_and_whole_sector_erase(length, sectors):
    result = backend.configuration_write_extent(_payload(size=length), remotes.get("harmony-550"))

    assert result["payload_bytes"] == length
    assert result["write"] == {"start": 0x820000, "end_exclusive": 0x820000 + length}
    assert result["erase"] == {
        "start": 0x820000, "end_exclusive": 0x820000 + sectors * 65536, "sectors": sectors}
    assert result["erased_but_not_written"]["start"] == 0x820000 + length
    assert result["untouched_configuration_tail"] == {
        "start": 0x820000 + sectors * 65536, "end_exclusive": 0x880000}
    assert result["hardware_acceptance"] == "not-established"


def test_trailing_payload_bytes_are_preserved_and_count_toward_transport_limits():
    payload = _payload(size=65536) + b"\x5A"
    result = backend.configuration_write_extent(payload, remotes.get("harmony-550"))

    assert result["trailing_payload_bytes"] == 1
    assert result["erase"]["sectors"] == 2
    with pytest.raises(ahcm.InvalidImage, match="complete payload exceeds"):
        backend.configuration_write_extent(
            _payload(size=393216) + b"\x00", remotes.get("harmony-550"))


@pytest.mark.parametrize("profile", [
    None, replace(remotes.get("harmony-550"), flash=None),
    replace(remotes.get("harmony-550"), flash="0xff:0x11"),
    replace(remotes.get("harmony-550"), arch=8),
])
def test_unknown_transport_geometry_never_falls_back_to_another_models_capacity(profile):
    with pytest.raises(ahcm.InvalidImage):
        backend.configuration_write_extent(_payload(), profile)


class FakeRemote:
    def __init__(self, source, identity, writes, report):
        self.source, self.live = source, identity
        self.writes, self.report = writes, report
        self.last_restart_error = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def identity(self):
        return dict(self.live)

    def save_config(self, target):
        shutil.copyfile(self.writes[-1] if self.writes else self.source, target)

    def _authorize_experimental_config(self, path, expected_identity):
        actual = {field: self.live.get(field) for field in first_write.IDENTITY_FIELDS}
        if actual != expected_identity:
            raise concord.RemoteError("fake connected identity differs; nothing written")

    def _apply_config(self, path):
        assert json.loads(self.report.read_text())["status"] in ("write-started", "restore-started")
        self.writes.append(Path(path))


def _prepared(tmp_path, monkeypatch, *, source=None):
    # The shipped profile is never promoted; only this fake-only test value is experimental.
    profile = replace(remotes.get("harmony-550"), status=remotes.EXPERIMENTAL)
    artifact = _file(tmp_path / "candidate.ezhex", _payload(), profile)
    original = source or _file(tmp_path / "original.ezhex", _payload(size=75158), profile)
    identity = {**remotes.identity_of(ezhex.read(artifact.read_bytes()).header),
                "firmware": "2.6", "can_read": True, "can_write": True, "config_valid": True}
    writes, report = [], tmp_path / "prepared.json"
    monkeypatch.setattr(first_write.ezhex, "profile_of", lambda _path: profile)
    monkeypatch.setattr(first_write.remotes, "get", lambda _id: profile)
    factory = lambda: FakeRemote(original, identity, writes, report)
    prepared = first_write.prepare(
        artifact, tmp_path / "recovery.ezhex", report,
        recovery_copy=tmp_path / "separate-folder" / "recovery.copy.ezhex",
        remote_factory=factory)
    return profile, artifact, original, identity, writes, report, factory, prepared


def test_fake_prepare_apply_readback_restore_retains_the_exact_original_with_its_own_extent(
        tmp_path, monkeypatch):
    profile, artifact, original, identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    original_bytes = original.read_bytes()

    assert prepared["artifact"]["write_extent"]["erase"]["sectors"] == 1
    assert prepared["recovery"]["write_extent"]["erase"]["sectors"] == 2
    assert prepared["changes"]["runtime_equivalence"] == "not-established"
    assert writes == []
    for path in (prepared["recovery"]["path"], prepared["recovery_copy"]["path"]):
        assert Path(path).read_bytes() == original_bytes
    first_write.apply(report, ask=lambda _: prepared["confirmation"], remote_factory=factory)
    assert first_write.readback(
        report, tmp_path / "readback.ezhex", remote_factory=factory)["status"] == "readback-verified"
    phrase = f"RESTORE {profile.id} {prepared['recovery']['sha256'][:12]}"
    assert first_write.restore(report, ask=lambda _: phrase, remote_factory=factory)["status"] == (
        "restored")
    assert writes == [artifact.resolve(), Path(prepared["recovery"]["path"])]
    assert writes[-1].read_bytes() == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == prepared["recovery"]["sha256"]
    shipped = next(item for item in remotes.load_all() if item.id == "harmony-550")
    assert shipped.status == remotes.READ_ONLY


def test_fake_identity_mismatch_after_prepare_never_starts_a_candidate_write(tmp_path, monkeypatch):
    _profile, _artifact, _original, identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    identity["firmware"] = "0.0"
    with pytest.raises(concord.RemoteError, match="identity differs"):
        first_write.apply(report, ask=lambda _: prepared["confirmation"], remote_factory=factory)
    assert writes == []
    assert json.loads(report.read_text())["status"] == "prepared"


def test_changed_extent_report_is_refused_before_confirmation_or_transport(tmp_path, monkeypatch):
    _profile, _artifact, _original, _identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    prepared["artifact"]["write_extent"]["erase"]["end_exclusive"] -= 1
    report.write_text(json.dumps(prepared))
    with pytest.raises(first_write.FirstWriteError, match="extent changed"):
        first_write.apply(report, ask=lambda _: pytest.fail("must not prompt"),
                          remote_factory=factory)
    assert writes == []


def test_fake_unknown_candidate_write_is_not_reapplied_but_recorded_recovery_can_be_restored(
        tmp_path, monkeypatch):
    profile, _artifact, original, identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)

    class Interrupted(FakeRemote):
        def _apply_config(self, path):
            super()._apply_config(path)
            raise OSError("synthetic transfer interruption")

    with pytest.raises(first_write.FirstWriteError, match="do not apply again"):
        first_write.apply(report, ask=lambda _: prepared["confirmation"],
                          remote_factory=lambda: Interrupted(original, identity, writes, report))
    assert json.loads(report.read_text())["status"] == "write-outcome-unknown"
    with pytest.raises(first_write.FirstWriteError, match="not 'prepared'"):
        first_write.apply(report, ask=lambda _: pytest.fail("must not prompt"),
                          remote_factory=factory)
    phrase = f"RESTORE {profile.id} {prepared['recovery']['sha256'][:12]}"
    assert first_write.restore(report, ask=lambda _: phrase, remote_factory=factory)["status"] == (
        "restored")
    assert writes[-1].read_bytes() == original.read_bytes()


def test_invalid_fake_backup_prevents_prepared_report_or_any_write(tmp_path, monkeypatch):
    broken = tmp_path / "broken-original.ezhex"
    broken.write_bytes(b"not an EZHex")
    with pytest.raises(first_write.FirstWriteError, match="cannot read"):
        _prepared(tmp_path, monkeypatch, source=broken)
    assert not (tmp_path / "prepared.json").exists()


def test_read_only_preparation_refuses_before_creating_a_real_or_fake_remote(tmp_path):
    artifact = _file(tmp_path / "read-only.ezhex", _payload())
    with pytest.raises(first_write.FirstWriteError, match="experimental profile"):
        first_write.prepare(
            artifact, tmp_path / "recovery.ezhex", tmp_path / "report.json",
            remote_factory=lambda: pytest.fail("read-only prepare must not initialize"))


def test_fragmented_byte_changes_report_the_exact_total_and_any_omitted_ranges():
    before = _payload(size=1000, value=0)
    after = bytearray(before)
    image = ahcm.Image(before)
    for offset in range(ahcm.HEADER_SIZE + 4, image.checksum_offset, 2):
        after[offset] = 0x55
    after[image.checksum_offset:image.end_offset] = ahcm.checksum(
        bytes(after), image.checksum_offset)
    result = backend.compare_payloads(before, bytes(after), remotes.get("harmony-550"))

    assert result["changed_bytes"] == sum(old != new for old, new in zip(before, after))
    assert len(result["changed_ranges"]) == reporting.CHANGE_RANGE_LIMIT
    assert result["omitted_change_ranges"] > 0
    assert result["changed_range_count"] == (
        len(result["changed_ranges"]) + result["omitted_change_ranges"])


def test_shared_roots_and_children_are_consumed_spans_not_neighbor_section_ownership():
    planned = layout.Layout(header_value=1, spares=[0] * 20)
    planned.add("empty-action", b"\x00", owner="synthetic")
    planned.add("actions", b"\x01\x00" + bytes(3), owner="synthetic")
    planned.relocate("actions", 2, "empty-action")
    for slot in range(20):
        planned.root(slot, "actions" if slot in (10, 18) else None)
    payload = planned.assemble().payload
    result = backend.compare_payloads(payload, payload, remotes.get("harmony-550"))
    inventory = result["inventories"]["candidate"]

    assert result["identical"]
    assert result["changed_bytes"] == 0
    assert inventory["uninspected_present_roots"] == [18]
    regions = inventory["regions"]
    action = next(item for item in regions if item["kind"] == "action-list")
    index = next(item for item in regions if item["kind"] == "slot-10-index")
    assert action["offset"] < index["offset"]
    assert inventory["runtime_equivalence"] == "not-established"


def test_malformed_candidate_extent_fails_before_initializing_any_transport(tmp_path, monkeypatch):
    profile = replace(remotes.get("harmony-550"), status=remotes.EXPERIMENTAL)
    artifact = _file(tmp_path / "too-large.ezhex", _payload(size=393216) + b"x", profile)
    monkeypatch.setattr(first_write.ezhex, "profile_of", lambda _path: profile)
    with pytest.raises(first_write.FirstWriteError, match="write extent"):
        first_write.prepare(
            artifact, tmp_path / "recovery.ezhex", tmp_path / "report.json",
            remote_factory=lambda: pytest.fail("invalid extent must not initialize"))


def test_readback_difference_is_not_accepted_as_structural_or_runtime_equivalence(
        tmp_path, monkeypatch):
    _profile, _artifact, original, identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    first_write.apply(report, ask=lambda _: prepared["confirmation"], remote_factory=factory)
    different = _file(tmp_path / "different.ezhex", _payload(value=0x6B))

    class DifferentReadback(FakeRemote):
        def save_config(self, target):
            shutil.copyfile(different, target)

    with pytest.raises(first_write.FirstWriteError, match="differs from the artifact"):
        first_write.readback(
            report, tmp_path / "readback.ezhex",
            remote_factory=lambda: DifferentReadback(original, identity, writes, report))
    assert json.loads(report.read_text())["status"] == "written-awaiting-readback"


def test_recovery_does_not_depend_on_the_candidate_surviving_and_interrupted_restore_is_retryable(
        tmp_path, monkeypatch):
    profile, artifact, original, identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    first_write.apply(report, ask=lambda _: prepared["confirmation"], remote_factory=factory)
    artifact.unlink()
    phrase = f"RESTORE {profile.id} {prepared['recovery']['sha256'][:12]}"

    class InterruptedRestore(FakeRemote):
        def _apply_config(self, path):
            super()._apply_config(path)
            raise OSError("synthetic restore interrupted")

    with pytest.raises(first_write.FirstWriteError, match="restore again"):
        first_write.restore(
            report, ask=lambda _: phrase,
            remote_factory=lambda: InterruptedRestore(original, identity, writes, report))
    assert json.loads(report.read_text())["status"] == "restore-outcome-unknown"
    assert first_write.restore(report, ask=lambda _: phrase, remote_factory=factory)["status"] == (
        "restored")
    assert writes[-1].read_bytes() == original.read_bytes()


def test_changed_recovery_extent_prevents_restore_without_erasing_the_original(
        tmp_path, monkeypatch):
    profile, _artifact, original, _identity, writes, report, factory, prepared = _prepared(
        tmp_path, monkeypatch)
    first_write.apply(report, ask=lambda _: prepared["confirmation"], remote_factory=factory)
    data = json.loads(report.read_text())
    del data["recovery"]["write_extent"]
    report.write_text(json.dumps(data))
    phrase = f"RESTORE {profile.id} {prepared['recovery']['sha256'][:12]}"
    with pytest.raises(first_write.FirstWriteError, match="extent changed"):
        first_write.restore(report, ask=lambda _: phrase, remote_factory=factory)
    assert len(writes) == 1
    assert Path(data["recovery"]["path"]).read_bytes() == original.read_bytes()


def test_rooted_read_changes_are_addressed_consumption_not_semantically_matched_objects():
    def image(opcode):
        planned = layout.Layout(header_value=1, spares=[0] * 20)
        planned.add("action", b"\x01\x34\x12" + bytes((opcode,)), owner="synthetic")
        planned.add("actions", b"\x01\x00" + bytes(3), owner="synthetic")
        planned.relocate("actions", 2, "action")
        for slot in range(20):
            planned.root(slot, "actions" if slot == 10 else None)
        return planned.assemble().payload

    result = backend.compare_payloads(image(0x31), image(0x32), remotes.get("harmony-550"))
    changes = result["rooted_read_changes"]

    assert [item["kind"] for item in changes["changed"]] == ["action-list"]
    assert changes["added"] == changes["removed"] == []
    assert changes["unchanged"] == 1
    assert changes["matching"] == "kind-offset-size-not-semantic-object-identity"


def test_an_inspector_budget_abort_is_a_reported_unknown_not_an_empty_success(monkeypatch):
    payload = _payload(extra_root=7, extra=b"\x01\x00" + bytes(3))
    monkeypatch.setattr(readers, "OBJECT_BUDGET", 0)
    result = backend.compare_payloads(payload, payload, remotes.get("harmony-550"))
    inventory = result["inventories"]["candidate"]

    assert not inventory["supported_reads_complete"]
    assert inventory["counts"] is None
    assert inventory["counts_scope"] == "unavailable-after-inspection-aborted"
    assert inventory["unsupported"] == {"native-object inspector budget exceeded": 1}
    assert inventory["unclassified_data_spans"]
