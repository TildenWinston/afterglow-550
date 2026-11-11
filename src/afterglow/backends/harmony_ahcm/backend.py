"""Read-only AHCM support exposes unknown operations rather than pretending success.

Format reference: docs/harmony_ahcm/image.md
"""
from __future__ import annotations

from . import ahcm
from ..contracts import ImportResult


class UnsupportedOperation(NotImplementedError):
    """This architecture has no implemented operation with proven semantics."""


def validate_payload(payload: bytes, profile=None) -> dict:
    from . import reporting

    return reporting.validate_payload(payload, profile)


def compare_payloads(recovery: bytes, candidate: bytes, profile) -> dict:
    from . import reporting

    return reporting.compare_payloads(recovery, candidate, profile)


def configuration_write_extent(payload: bytes, profile) -> dict:
    from . import reporting

    return reporting.configuration_write_extent(payload, profile)


def build_project(project, profile, context):
    profile.require_buildable()
    raise UnsupportedOperation(
        "AHCM generation from a project is not implemented; an imported image "
        "must never be used as a build template")


def import_project(source, profile, context) -> ImportResult:
    from ... import payloads
    from . import importing

    payload = payloads.get(profile.payload).build(source)
    validate_payload(payload, profile)
    image = ahcm.Image(payload)
    if image.section(5) is None:
        raise UnsupportedOperation(
            "this AHCM image has no rooted IR group index; no portable setup is decoded")
    result = importing.import_project(image, profile, context)
    if not result.project["devices"] and not result.project["activities"]:
        raise UnsupportedOperation(
            "no portable devices or activities could be decoded; use native inspection")
    return result


def capability(signal: dict, profile, *, library=None) -> dict:
    from ... import ir_signal

    ir_signal.validate(signal)
    return {"supported": False, "strategy": "unavailable",
            "reason": "AHCM signal generation has not been implemented or qualified"}
