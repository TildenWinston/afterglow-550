"""AHCM framing and carrier regressions use synthetic data, never household dumps."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

from afterglow import backends, ezhex, payloads, remotes
from afterglow.backends.harmony_ahcm import ahcm, backend

ROOT = Path(__file__).resolve().parent.parent


def seal(payload: bytearray) -> bytes:
    marker = int.from_bytes(payload[4:8], "little") - ahcm.LOGICAL_BASE
    payload[marker - 2:marker] = ahcm.checksum(payload, marker - 2)
    return bytes(payload)


def native_image(size=0x300) -> bytes:
    payload = bytearray(size)
    payload[:4] = ahcm.MAGIC
    payload[4:8] = (ahcm.LOGICAL_BASE + size - 4).to_bytes(4, "little")
    payload[8:11] = (0x1400).to_bytes(3, "little")
    payload[ahcm.HEADER_MARKER_OFFSET:ahcm.HEADER_SIZE] = ahcm.HEADER_MAGIC
    payload[-4:] = ahcm.END_MAGIC
    identity = 0x80
    slot = ahcm.TABLE_OFFSET + 4
    payload[slot + 1:slot + 4] = (ahcm.LOGICAL_BASE + identity).to_bytes(3, "little")
    payload[identity:identity + 7] = b"\x09\x09\x29\x0d\x00\x00\x00"
    return seal(payload)


def test_all_twenty_spare_and_pointer_slots_survive_native_inspection():
    payload = bytearray(native_image())
    for index in range(ahcm.SLOT_COUNT):
        payload[ahcm.TABLE_OFFSET + 4 * index] = index + 1
    image = ahcm.Image(seal(payload))

    assert len(image.slots) == 20
    assert [slot.spare for slot in image.slots] == list(range(1, 21))
    assert image.slots[1].offset == 0x80
    assert image.slots[18].offset is None
    assert image.slots[19].offset is None
    assert image.payload == seal(payload)


def test_a_null_native_slot_does_not_hide_later_subsystem_roots():
    image = ahcm.Image(native_image())

    assert image.section(0) is None
    assert image.section(1) == 0x80
    assert image.report()["present_slots"] == 1


@pytest.mark.parametrize("size", [0, 4, 11, 0x5B, ahcm.HEADER_SIZE + 5])
def test_truncated_native_headers_and_trailers_are_refused(size):
    with pytest.raises(ahcm.InvalidImage, match="truncated"):
        ahcm.validate(native_image()[:size])


@pytest.mark.parametrize("offset", [0, ahcm.HEADER_MARKER_OFFSET, 0x2FC, 0x2FA])
def test_native_magic_marker_and_checksum_corruption_is_refused(offset):
    payload = bytearray(native_image())
    payload[offset] ^= 1

    with pytest.raises(ahcm.InvalidImage):
        ahcm.validate(bytes(payload))


def test_an_end_vector_in_the_wrong_address_domain_is_refused():
    payload = bytearray(native_image())
    payload[4:8] = (0x820000 + len(payload) - 4).to_bytes(4, "little")

    with pytest.raises(ahcm.InvalidImage, match="end vector"):
        ahcm.validate(bytes(payload))


@pytest.mark.parametrize("address", [1, ahcm.LOGICAL_BASE, 0x820080, 0x0202FA])
def test_native_subsystem_pointers_cannot_enter_the_header_or_trailer(address):
    payload = bytearray(native_image())
    payload[ahcm.TABLE_OFFSET + 1:ahcm.TABLE_OFFSET + 4] = address.to_bytes(3, "little")

    with pytest.raises(ahcm.InvalidImage, match="logical pointer"):
        ahcm.validate(seal(payload))


def test_rooted_reads_allow_shared_children_before_their_subsystem_table():
    image = ahcm.Image(native_image())

    assert image.offset(ahcm.LOGICAL_BASE + 0x80, 7) == 0x80
    assert image.read(0x80, 7) == b"\x09\x09\x29\x0d\x00\x00\x00"
    with pytest.raises(ahcm.InvalidImage, match="crosses"):
        image.read(image.checksum_offset - 1, 2)


def test_an_unpaired_final_byte_is_not_part_of_the_native_checksum():
    payload = bytearray(native_image(0x301))
    checksum_offset = len(payload) - 6
    payload[checksum_offset - 1] ^= 0xFF

    assert ahcm.validate(bytes(payload))["ok"] is True


def test_native_bytes_after_the_end_marker_are_preserved_and_reported():
    payload = native_image() + b"\x00\xff\x00"

    assert ahcm.inspect(payload)["trailing_bytes"] == 3
    assert ahcm.Image(payload).payload == payload


def test_unknown_header_values_are_reported_rather_than_invented_as_constants():
    payload = bytearray(native_image())
    payload[8:11] = b"\x01\x02\x03"

    assert ahcm.validate(seal(payload))["header_value"] == 0x030201


def test_native_identity_and_selected_profile_must_agree():
    payload = bytearray(native_image())
    payload[0x80] = 8
    with pytest.raises(ahcm.InvalidImage, match="architecture"):
        ahcm.validate(seal(payload))
    payload[0x80] = 9
    payload[0x82] = 22
    with pytest.raises(ahcm.InvalidImage, match="skin"):
        backend.validate_payload(seal(payload), remotes.get("harmony-550"))
    with pytest.raises(ahcm.InvalidImage, match="architecture"):
        backend.validate_payload(native_image(), remotes.get("harmony-one"))


def test_the_donor_identity_selects_only_the_read_only_550_profile():
    profile = remotes.get("harmony-550")
    header = ezhex.header_for_profile(profile)

    assert remotes.identify(header).id == "harmony-550"
    assert profile.status == remotes.READ_ONLY
    assert profile.other_skins == ()
    assert profile.other_boards == ()
    assert profile.firmware_min is None and profile.firmware_max is None
    assert backends.for_profile(profile) is backend
    with pytest.raises(remotes.NotBuildable):
        profile.require_buildable()
    with pytest.raises(remotes.NotWritable):
        profile.require_writable()
    for field, value in (("arch", 8), ("skin", 22), ("software_type", 1),
                         ("board", "1.5.0"), ("flash", "0xFF:0x13")):
        identity = remotes.identity_of(header)
        identity[field] = value
        assert not profile.matches(identity)


def test_a_read_only_backend_refuses_building_before_touching_project_or_context():
    with pytest.raises(remotes.NotBuildable):
        backend.build_project({}, remotes.get("harmony-550"), None)


def test_an_unknown_remote_header_roundtrips_through_dynamic_carrier_discovery(tmp_path):
    payload = native_image()
    header = (b"<INFORMATION>\r\n<INTENDEDVERSION><PROTOCOL>99</PROTOCOL>"
              b"<SKIN>999</SKIN></INTENDEDVERSION>\r\n<BINARYDATASIZE>"
              + str(len(payload)).encode() + b"</BINARYDATASIZE>\r\n<CHECKSUM>"
              + str(ezhex.checksum(payload)).encode() + b"</CHECKSUM>\r\n"
              b"</INFORMATION>\r\n")
    original = tmp_path / "original.ezhex"
    original.write_bytes(header + payload)
    tree, repacked = tmp_path / "tree", tmp_path / "repacked.ezhex"
    with contextlib.redirect_stdout(io.StringIO()):
        ezhex.unpack(str(original), str(tree))
        ezhex.rehash(str(tree))
        ezhex.pack_standalone(str(tree), str(repacked))

    assert repacked.read_bytes() == original.read_bytes()
    assert payloads.identify_tree(str(tree)).NAME == "blob"


def test_an_unchanged_carrier_keeps_the_original_numeric_header_representation(tmp_path):
    payload = native_image()
    profile = remotes.get("harmony-550")
    header = ezhex.header_for_profile(profile, len(payload), ezhex.checksum(payload))
    header = header.replace(b"<BINARYDATASIZE>", b"<BINARYDATASIZE>000")
    header = header.replace(b"<CHECKSUM>", b"<CHECKSUM>000")
    original, output = tmp_path / "original.ezhex", tmp_path / "repacked.ezhex"
    original.write_bytes(header + payload)
    tree = tmp_path / "tree"
    with contextlib.redirect_stdout(io.StringIO()):
        ezhex.unpack(str(original), str(tree))
        ezhex.pack_standalone(str(tree), str(output), do_rehash=False)

    assert output.read_bytes() == original.read_bytes()
    assert "arch 9" in payloads.get(profile.payload).describe(payload)


@pytest.fixture(scope="module")
def private_550_configs():
    approved = os.environ.get("AFTERGLOW_550_CONFIG")
    if approved:
        path = Path(approved)
        if not path.is_file():
            pytest.fail("the explicitly provided private 550 fixture is unavailable")
        return [path]
    directory = ROOT / "configs" / "550"
    found = sorted(path for path in directory.iterdir()
                   if path.is_file() and path.suffix.casefold() == ".ezhex") \
        if directory.is_dir() else []
    if not found:
        pytest.skip("no approved private Harmony 550 fixtures in configs/550")
    return found


def test_private_550_carriers_are_byte_identical_after_unpack_and_pack(
        private_550_configs, tmp_path):
    for index, original in enumerate(private_550_configs):
        tree, repacked = tmp_path / f"tree-{index}", tmp_path / f"repacked-{index}.ezhex"
        with contextlib.redirect_stdout(io.StringIO()):
            ezhex.unpack(str(original), str(tree))
            ezhex.pack_standalone(str(tree), str(repacked), do_rehash=False)
        assert repacked.read_bytes() == original.read_bytes()
        contents = ezhex.read(original.read_bytes())
        assert remotes.identify(contents.header).id == "harmony-550"
        assert backend.validate_payload(contents.payload, remotes.get("harmony-550"))["ok"]


def test_private_550_import_keeps_every_indexed_command_and_all_native_evidence(
        private_550_configs, tmp_path):
    import base64
    from afterglow import importer, project_devices
    from afterglow.backends.harmony_ahcm.readers import Reader

    for index, original in enumerate(private_550_configs):
        contents = ezhex.read(original.read_bytes())
        reader = Reader(ahcm.Image(contents.payload))
        expected = sum(len(reader.group(address)["commands"])
                       for address in reader.addresses(5, 1))
        tree = tmp_path / f"portable-{index}"
        with contextlib.redirect_stdout(io.StringIO()):
            ezhex.unpack(str(original), str(tree))
        result = importer.import_result(str(tree))
        assert sum(len(device["commands"]) for device in result.project["devices"]) == expected
        for device in result.project["devices"]:
            project_devices.validate(device)
        assert base64.b64decode(result.preservation["payload_base64"]) == contents.payload
        assert remotes.get("harmony-550").status == remotes.READ_ONLY


def _assert_observed_fields(actual, expected):
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, f"observed field {key!r} is missing"
            _assert_observed_fields(actual[key], value)
    elif isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected), "observed population or sequence differs"
        for value, observed in zip(actual, expected):
            _assert_observed_fields(value, observed)
    else:
        assert actual == expected


def test_private_550_import_matches_an_independent_owner_observed_inventory(
        private_550_configs, tmp_path):
    from afterglow import importer, project_devices

    inventories = [path.with_suffix(".expected-project.json") for path in private_550_configs]
    if any(not path.is_file() for path in inventories):
        pytest.skip("independent owner-observed 550 behavioral inventories are unavailable")
    for index, (original, inventory) in enumerate(zip(private_550_configs, inventories)):
        expected = json.loads(inventory.read_text(encoding="utf-8"))
        assert expected["source"] == "owner-observed"
        assert {"devices", "activities"} <= expected["project"].keys()
        tree = tmp_path / f"import-{index}"
        with contextlib.redirect_stdout(io.StringIO()):
            ezhex.unpack(str(original), str(tree))
        result = importer.import_result(str(tree))
        _assert_observed_fields(result.project, expected["project"])
        for device in result.project["devices"]:
            project_devices.validate(device)
