"""Pure JSON wire-format helpers: schemaVersion envelope + canonical UTC-Z serializer.

stamp_schema_version() is the single chokepoint for the additive camelCase
``schemaVersion: 1`` envelope adopted across reporting ``--json`` surfaces
(#279 S6 W1; convention: docs/cli-contract.md). _iso_z() is the canonical
None-safe seconds-precision UTC-Z serializer (the forecast/dashboard-envelope
behavior; _lib_doctor._iso_z deliberately diverges — see its docstring).

Pure kernel: stdlib-only, no cctally back-import (split design §5.3).
"""
from __future__ import annotations

import datetime as dt

SCHEMA_VERSION_KEY = "schemaVersion"


def stamp_schema_version(payload: dict, *, version: int = 1,
                         key: str = SCHEMA_VERSION_KEY) -> dict:
    """Return a new dict with ``key: version`` first.

    Always returns a shallow copy; never mutates ``payload``. If ``key`` is
    already present the copy preserves the existing value AND key order
    (value- and order-idempotent no-op). Formatting is the caller's concern:
    this returns a dict, each emitter keeps its own json.dumps arguments.
    """
    if key in payload:
        return dict(payload)
    out = {key: version}
    out.update(payload)
    return out


def _iso_z(d: "dt.datetime | None") -> "str | None":
    """None-safe UTC ISO-8601 with Z suffix, seconds precision."""
    if d is None:
        return None
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
