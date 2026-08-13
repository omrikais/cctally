"""The ONE `requires_agentmem` gate object (#529 S6, M4).

Two test modules used to define this mark independently, with identical
bodies. That is two look-alike objects rather than one object, and
`tests/conftest.py` has to count the collected items carrying it — a count
over "either of two marks that happen to match" is a count over a coincidence,
and it silently stops matching the moment one copy is edited.

NO CUSTOM MARKER IS REGISTERED HERE, and none may be. `pytest.ini` records a
settled decision that this estate uses only pytest built-ins and registers no
marker speculatively; the pre-plan review recommended a semantic marker for
exactly this counting problem and was rejected on that ground. This stays a
plain `pytest.mark.skipif`, and the controller counts items by IDENTITY against
the single object below.

The condition and both reason strings are unchanged from the two definitions
this replaces, so which tests skip, and what they say when they do, is exactly
what it was.
"""
from __future__ import annotations

import os
import shutil

import pytest

# Verification lanes declare CCTALLY_AGENTMEM_TEST_POLICY: the LAN/self-hosted
# authorities require the pinned dependency, hosted CI records its explicit
# private-repository boundary, and an ordinary developer machine remains
# optional. Since #529 S6 the local escape hatch declares it too, so
# `CCTALLY_TEST_LOCAL=1` runs the same contract the path it escapes runs.
_agentmem_policy = os.environ.get("CCTALLY_AGENTMEM_TEST_POLICY", "optional-local")

AGENTMEM_PRESENT = shutil.which("agentmem") is not None

requires_agentmem = pytest.mark.skipif(
    not AGENTMEM_PRESENT,
    reason=(
        "agentmem intentionally unavailable at the hosted private-repository boundary"
        if _agentmem_policy == "hosted-private-unavailable"
        else "agentmem not installed; optional on this developer machine"
    ),
)
