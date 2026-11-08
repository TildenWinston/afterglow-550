"""Read-only AHCM support exposes unknown operations rather than pretending success.

Format reference: docs/harmony_ahcm/image.md
"""
from __future__ import annotations

from . import ahcm


class UnsupportedOperation(NotImplementedError):
    """This architecture has no implemented operation with proven semantics."""


def validate_payload(payload: bytes, profile=None) -> dict:
    if profile is not None and profile.arch != 9:
        raise ahcm.InvalidImage("AHCM inspection requires an architecture-9 profile")
    report = ahcm.validate(payload)
    identity = report["identity"]
    if profile is not None and identity is not None and identity["skin"] != profile.skin:
        raise ahcm.InvalidImage("native skin conflicts with the selected remote profile")
    return report


def build_project(project, profile, context):
    profile.require_buildable()
    raise UnsupportedOperation(
        "AHCM generation from a project is not implemented; an imported image "
        "must never be used as a build template")


def import_project(source, profile, context):
    raise UnsupportedOperation(
        "AHCM portable import is not implemented; use native inspection instead")


def capability(signal: dict, profile, *, library=None) -> dict:
    from ... import ir_signal

    ir_signal.validate(signal)
    return {"supported": False, "strategy": "unavailable",
            "reason": "AHCM signal generation has not been implemented or qualified"}
