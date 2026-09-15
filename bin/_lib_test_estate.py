"""The committed estate artifact: schema, comparison and signed delta (#648).

``bin/_lib_estate_discovery.py`` derives three LIVE sets from a tree. This module
is the other half: it parses the committed artifact that records those sets,
composes the private profile from the public artifact plus a signed overlay, and
reports what a live derivation and the record disagree about.

IT OWNS NO VERDICT. Every function here returns a tagged result or raises
``EstateError``; nothing prints a reason code and nothing exits. The reason-code
literals live only in ``bin/_lib-test-contract.sh``, which maps these results to
``contract_fail`` calls, so the contract's own registration scan can see every
literal at its single emission site.

Stdlib-only leaf module, like the discovery kernel. It imports nothing from
``cctally`` and nothing outside the standard library, and it is named
individually in ``.mirror-allowlist``: the surrounding ``bin/_lib-*`` pattern
uses a HYPHEN and never matches an underscore module, and the public clone is
required to RUN this checker against its own live collection, so an unlisted
entry would not merely hide a file — it would remove the checker the public
suite needs.

THE TWO PROFILES
----------------
``tests/authoritative-estate.json`` is public and carries the rows a public
clone collects. ``tests/authoritative-estate.private.json`` is mirror-private
and carries a SIGNED delta — ``additions`` and ``removals`` per axis.

The delta is signed rather than additions-only because the subset relation
between the profiles is an empirical measurement, not an invariant. Today every
private-only row is an addition, but a public test that adds a parameter only
when a mirror-private file is ABSENT would produce a public-only row, which is
the exact inverse of the ``_share_docs()`` divergence the tree already carries.
An additions-only overlay could not represent that at all.

The overlay records TWO digests: the public artifact it was cut against, and
the ``.mirror-allowlist`` that drew the boundary it describes. ``compose_private``
refuses when either has moved — the second only when the caller supplies the
tree's own allowlist digest, because a public clone has no allowlist to digest
and must still be able to run the checker. Those refusals are what stop a stale
partition — a public artifact regenerated while the overlay was left behind, or
a boundary edited under a delta cut against the previous one — from composing
into a private document that looks complete.

WHAT THE DIGEST COVERS, AND WHY IT EXCLUDES TWO FIELDS
------------------------------------------------------
``active_set_digest`` is taken over the three axes and nothing else.
``generatedFrom`` and ``pytestExecution`` are deliberately outside it, because
D3a binds every outstanding shrink declaration to the digest of the predecessor
active set. Folding provenance or the execution split into that digest would
invalidate every outstanding declaration whenever a commit id was restamped or a
leg selector was rewritten, without one estate row having changed.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

__all__ = [
    "AXES",
    "CAUSES",
    "Committed",
    "ExecutionPlan",
    "COMPLEMENT_SELECTOR",
    "EXECUTION_LEGS",
    "EstateError",
    "Expectation",
    "Finding",
    "HARMFUL",
    "PRIVATE",
    "PRIVATE_LEDGER",
    "PRIVATE_OVERLAY",
    "PUBLIC_ARTIFACT",
    "PUBLIC_LEDGER",
    "PROFILES",
    "PUBLIC",
    "SCHEMA_VERSION",
    "SKIPPED",
    "Report",
    "Uncovered",
    "UncoveredRecord",
    "active_profile",
    "active_set_digest",
    "allowlist_digest",
    "applicable_expectations",
    "axis_diff",
    "classify_selector",
    "collapse_suppressions",
    "committed_document",
    "compare",
    "compose_private",
    "expand_selector",
    "expand_suppressions",
    "live_axes",
    "load_artifact",
    "load_artifact_document",
    "load_ledger",
    "load_ledger_document",
    "main",
    "merge_ledgers",
    "plan_execution",
    "predecessor_revisions",
    "private_expectation",
    "render_report",
    "run_check",
    "uncovered_transitions",
    "validate_owner_paths",
    "validate_transition",
]

SCHEMA_VERSION = 1

# Ordered, because every rendered report and every canonical serialization walks
# the axes in this order.
AXES = ("pytestNodes", "frontendTests", "suppressions")

_ARTIFACT_KEYS = frozenset(
    {"schemaVersion", "generatedFrom", "pytestExecution"} | set(AXES)
)
_OVERLAY_KEYS = frozenset(
    {"schemaVersion", "publicDigest", "allowlistDigest"} | set(AXES)
)
_OVERLAY_OPTIONAL_KEYS = frozenset({"pytestExecution"})
_PRIVATE_EXECUTION_KEYS = frozenset({"benchmarkAdditions"})

# The two legs D7 declares. The `pytest` leg's share is the COMPLEMENT of every
# other leg's, which cannot be written as a selector list, so it carries this
# one literal instead and the checker expands it.
EXECUTION_LEGS = ("benchmark", "pytest")
COMPLEMENT_SELECTOR = "*complement*"

_FRONTEND_KEYS = frozenset({"runner", "id", "expectedStatus"})
_SUPPRESSION_KEYS = frozenset({"key", "count"})
_DELTA_KEYS = frozenset({"additions", "removals"})


class EstateError(RuntimeError):
    """The artifact, the overlay or a ledger is not usable as recorded.

    Raised INSTEAD of returning a document that parses but does not mean what it
    claims. A silently-accepted malformed artifact is indistinguishable from a
    correct one at every later step, which is the failure class this whole
    mechanism exists to prevent.
    """


Uncovered = collections.namedtuple(
    "Uncovered", ("axis", "token", "kind", "message"))
"""One harmful change that no declaration authorizes.

``kind`` is ``"removed"``, ``"added"`` or ``"skipped"``. ``token`` is the row's
declarable identity, carried here so a caller never has to recover it from
``message``.
"""


Finding = collections.namedtuple("Finding", ("code_tag", "axis", "rows"))
"""One disagreement between the record and a live derivation.

``code_tag`` is ``"unexpected"`` (the live tree has rows the record does not) or
``"missing"`` (the record has rows the live tree does not). It is a TAG and not
a reason code: the contract shell owns the literal.
"""


# ---------------------------------------------------------------------------
# Row identity
# ---------------------------------------------------------------------------


def _row_key(row):
    """A stable, sortable identity for one recorded row.

    A pytest node identifier is already a string. A frontend row and a
    suppression row are mappings, which are neither hashable nor orderable, so
    each is reduced to its canonical JSON form. Two rows are the same row when
    every field matches, which is the identity the axes are recorded under: a
    Playwright row whose ``expectedStatus`` flipped is a DIFFERENT row, and that
    is what makes the flip visible at all.
    """
    if isinstance(row, str):
        return row
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _index(rows):
    """``{row key: row}``, first occurrence winning."""
    out = {}
    for row in rows:
        out.setdefault(_row_key(row), row)
    return out


def _sorted_rows(rows):
    return [row for _, row in sorted(((_row_key(r), r) for r in rows),
                                     key=lambda pair: pair[0])]


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def _read_json(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EstateError(f"estate artifact is unreadable at {path}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EstateError(f"estate artifact at {path} is not valid JSON: {exc}")


def _require_keys(doc, allowed, what, optional=()):
    if not isinstance(doc, dict):
        raise EstateError(f"{what} must be a JSON object, not {type(doc).__name__}")
    unknown = sorted(set(doc) - set(allowed) - set(optional))
    if unknown:
        raise EstateError(f"{what} carries unknown key(s): {', '.join(unknown)}")
    missing = sorted(set(allowed) - set(doc))
    if missing:
        raise EstateError(f"{what} is missing key(s): {', '.join(missing)}")
    if doc["schemaVersion"] != SCHEMA_VERSION:
        raise EstateError(
            f"{what} declares schemaVersion {doc['schemaVersion']!r}; "
            f"this checker reads {SCHEMA_VERSION}"
        )


def _check_unique(rows, keyer, what):
    seen = set()
    for row in rows:
        identity = keyer(row)
        if identity in seen:
            raise EstateError(f"{what} repeats the identifier {identity!r}")
        seen.add(identity)


def _check_pytest_nodes(rows, what="pytestNodes"):
    if not isinstance(rows, list):
        raise EstateError(f"{what} must be a list")
    for row in rows:
        if not isinstance(row, str) or not row:
            raise EstateError(f"{what} carries a non-string node identifier: {row!r}")
    _check_unique(rows, lambda r: r, what)


def _check_frontend_tests(rows, what="frontendTests"):
    if not isinstance(rows, list):
        raise EstateError(f"{what} must be a list")
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FRONTEND_KEYS:
            raise EstateError(
                f"{what} row must carry exactly "
                f"{sorted(_FRONTEND_KEYS)}: {row!r}"
            )
        if not isinstance(row["runner"], str) or not isinstance(row["id"], str):
            raise EstateError(f"{what} row has a non-string runner/id: {row!r}")
        status = row["expectedStatus"]
        if status is not None and not isinstance(status, str):
            raise EstateError(
                f"{what} row has a non-string, non-null expectedStatus: {row!r}"
            )
    # Identity is (runner, id). The status is a PROPERTY of the row, so two rows
    # sharing an identity while disagreeing about status is a malformed record
    # rather than two tests.
    _check_unique(rows, lambda r: (r["runner"], r["id"]), what)


def _check_suppressions(rows, what="suppressions"):
    if not isinstance(rows, list):
        raise EstateError(f"{what} must be a list")
    for row in rows:
        if not isinstance(row, dict) or set(row) != _SUPPRESSION_KEYS:
            raise EstateError(
                f"{what} row must carry exactly {sorted(_SUPPRESSION_KEYS)}: {row!r}"
            )
        if not isinstance(row["key"], str) or not row["key"]:
            raise EstateError(f"{what} row has a non-string key: {row!r}")
        count = row["count"]
        # `bool` is an `int` subclass, and `True` would otherwise read as a
        # count of one.
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise EstateError(
                f"{what} row must carry an integer count of at least 1: {row!r}"
            )
    _check_unique(rows, lambda r: r["key"], what)


def _check_execution(block, what="pytestExecution"):
    if not isinstance(block, dict) or set(block) != {"legs"}:
        raise EstateError(f"{what} must be an object carrying exactly 'legs'")
    legs = block["legs"]
    if not isinstance(legs, list):
        raise EstateError(f"{what}.legs must be a list")
    names = []
    for leg in legs:
        if not isinstance(leg, dict) or set(leg) != {"name", "selectors"}:
            raise EstateError(
                f"{what} leg must carry exactly ['name', 'selectors']: {leg!r}"
            )
        selectors = leg["selectors"]
        if not isinstance(selectors, list) or not selectors:
            raise EstateError(f"{what} leg {leg['name']!r} declares no selectors")
        for selector in selectors:
            if not isinstance(selector, str) or not selector:
                raise EstateError(
                    f"{what} leg {leg['name']!r} carries a non-string selector"
                )
        names.append(leg["name"])
    if tuple(names) != EXECUTION_LEGS:
        raise EstateError(
            f"{what}.legs must name exactly {list(EXECUTION_LEGS)} in that order; "
            f"got {names}"
        )
    complement = [leg for leg in legs if leg["name"] == "pytest"][0]["selectors"]
    if complement != [COMPLEMENT_SELECTOR]:
        raise EstateError(
            f"{what}: the 'pytest' leg's share is the complement of every other "
            f"leg's, so it must carry the single selector "
            f"{COMPLEMENT_SELECTOR!r}; got {complement}"
        )


def load_artifact_document(doc, what="the estate artifact"):
    """Validate one already-parsed estate artifact, or raise ``EstateError``.

    Separated from ``load_artifact`` because not every artifact the checker sees
    comes from a file on disk. The gate reads the artifact each committed parent
    RECORDS, through ``git show``, and the generator validates what it is about
    to write before writing it. Both hold a document rather than a path, and
    neither may skip validation for want of a file to point at.

    RETURNS THE WHOLE DOCUMENT, which is deliberately NOT what
    ``load_ledger_document`` returns. Every consumer of an artifact reads
    several top-level keys — the three axes, ``pytestExecution`` and
    ``generatedFrom`` — so returning the document is returning the thing the
    caller asked for. A ledger has exactly one payload key, and every consumer
    of a ledger wants that list, so returning the wrapper there would make each
    caller unwrap it. The asymmetry is stated at both definitions rather than
    removed, because either uniform choice would make one of the two callers
    write ceremony for the other's benefit.
    """
    _require_keys(doc, _ARTIFACT_KEYS, what)
    if not isinstance(doc["generatedFrom"], str) or not doc["generatedFrom"]:
        raise EstateError(f"{what} records no generatedFrom")
    _check_pytest_nodes(doc["pytestNodes"])
    _check_frontend_tests(doc["frontendTests"])
    _check_suppressions(doc["suppressions"])
    _check_execution(doc["pytestExecution"])
    return doc


def load_artifact(path):
    """Parse and validate one estate artifact, or raise ``EstateError``.

    Every violation is a refusal rather than a repair. An artifact that loads
    with a duplicate node identifier, a zero suppression count or a missing
    execution leg would compare cleanly against a live tree that does not match
    it, which is worse than not loading at all.
    """
    return load_artifact_document(_read_json(path),
                                  f"the estate artifact at {path}")


# ---------------------------------------------------------------------------
# The active-set digest
# ---------------------------------------------------------------------------


def _canonical_active_set(artifact):
    """The three axes, order-normalized, and nothing else.

    Provenance and the execution split are excluded on purpose; see the module
    docstring.
    """
    frontend = [
        [row.get("runner"), row.get("id"), row.get("expectedStatus")]
        for row in artifact.get("frontendTests") or []
    ]
    suppressions = [
        [row.get("key"), row.get("count")]
        for row in artifact.get("suppressions") or []
    ]
    return {
        "pytestNodes": sorted(str(node) for node in artifact.get("pytestNodes") or []),
        "frontendTests": sorted(frontend, key=lambda r: (str(r[0]), str(r[1]), str(r[2]))),
        "suppressions": sorted(suppressions, key=lambda r: (str(r[0]), str(r[1]))),
    }


def active_set_digest(artifact):
    """SHA-256 over a canonical serialization of the three axes."""
    payload = json.dumps(
        _canonical_active_set(artifact), sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def allowlist_digest(path):
    """SHA-256 over the bytes of ``.mirror-allowlist``.

    The overlay records this so a partition cut against one boundary cannot be
    composed against another. It lives here rather than in the generator because
    the generator WRITES the recording and the gate CHECKS it, and two
    definitions of one digest is how the two silently stop agreeing.
    """
    path = Path(path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EstateError(f"the mirror allowlist is unreadable at {path}: {exc}")


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def axis_diff(recorded, live, multiset=False):
    """``(live_only, recorded_only)`` for one axis, order-stable.

    ``multiset=False`` compares SETS, which is right for the two identity axes:
    a node identifier either is in the estate or is not.

    ``multiset=True`` compares ``collections.Counter`` objects and expands each
    surplus occurrence into the output, so a count of two against a count of one
    yields one row. Two identical suppressions in one scope are two
    suppressions, and a set would collapse exactly the thing the key model
    exists to preserve.
    """
    recorded = list(recorded)
    live = list(live)
    known = _index(recorded)
    known.update(_index(live))
    if not multiset:
        recorded_keys = {_row_key(row) for row in recorded}
        live_keys = {_row_key(row) for row in live}
        return (
            [known[key] for key in sorted(live_keys - recorded_keys)],
            [known[key] for key in sorted(recorded_keys - live_keys)],
        )
    recorded_counts = collections.Counter(_row_key(row) for row in recorded)
    live_counts = collections.Counter(_row_key(row) for row in live)
    return (
        [known[key] for key in sorted((live_counts - recorded_counts).elements())],
        [known[key] for key in sorted((recorded_counts - live_counts).elements())],
    )


def expand_suppressions(rows):
    """``[{key, count}, ...]`` to the flat multiset of keys it denotes.

    The recorded form carries occurrence counts so the artifact stays readable,
    but the axis IS a multiset of keys, and diffing the ``{key, count}`` objects
    instead would report a count change as one removed row plus one added row
    rather than as the single occurrence that actually moved.

    ONE model of this axis, exported rather than private, because the comparison,
    the transition rule and the signed delta must all denote the same thing. The
    delta used to diff the objects, which round-tripped only while every private
    suppression key was absent from the public profile entirely.
    """
    out = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
            continue
        out.extend([row["key"]] * int(row["count"]))
    return out


def collapse_suppressions(keys):
    """The inverse: a flat multiset of keys back to sorted ``{key, count}``."""
    counts = collections.Counter(keys)
    return [{"key": key, "count": count} for key, count in sorted(counts.items())]


def live_axes(sets):
    """The discovery kernel's live snapshot, in the artifact's own row shapes.

    Accepts either an ``EstateSets`` from ``bin/_lib_estate_discovery.py`` (duck
    typed, so this module need not import the kernel) or a mapping already in
    artifact shape.
    """
    if isinstance(sets, dict):
        return {axis: list(sets.get(axis) or []) for axis in AXES}
    counts = collections.Counter(row.key for row in sets.suppressions)
    return {
        "pytestNodes": [str(node) for node in sets.pytest_nodes],
        "frontendTests": [
            {"runner": row.runner, "id": row.id, "expectedStatus": row.expected_status}
            for row in sets.frontend_tests
        ],
        "suppressions": [
            {"key": key, "count": count} for key, count in sorted(counts.items())
        ],
    }


def compare(artifact, sets):
    """Every disagreement between ``artifact`` and a live derivation.

    Returns a list of ``Finding``. An empty list means the record and the tree
    agree on all three axes. The caller decides what an agreement or a
    disagreement is worth; this function renders no verdict.
    """
    live = live_axes(sets)
    findings = []
    for axis in AXES:
        recorded = list(artifact.get(axis) or [])
        observed = live[axis]
        if axis == "suppressions":
            recorded = expand_suppressions(recorded)
            observed = expand_suppressions(observed)
        live_only, recorded_only = axis_diff(
            recorded, observed, multiset=(axis == "suppressions"),
        )
        if live_only:
            findings.append(Finding("unexpected", axis, live_only))
        if recorded_only:
            findings.append(Finding("missing", axis, recorded_only))
    return findings


# ---------------------------------------------------------------------------
# The signed delta
# ---------------------------------------------------------------------------


def _check_delta(delta, axis):
    if not isinstance(delta, dict) or set(delta) != _DELTA_KEYS:
        raise EstateError(
            f"the overlay's {axis} must carry exactly {sorted(_DELTA_KEYS)}"
        )
    for side in ("additions", "removals"):
        if not isinstance(delta[side], list):
            raise EstateError(f"the overlay's {axis}.{side} must be a list")
    checker = {
        "pytestNodes": _check_pytest_nodes,
        "frontendTests": _check_frontend_tests,
        "suppressions": _check_suppressions,
    }[axis]
    for side in ("additions", "removals"):
        checker(delta[side], f"the overlay's {axis}.{side}")
    # The suppression axis is keyed on the KEY, because `{k, 1}` and `{k, 2}`
    # are two counts of one suppression rather than two rows, and a delta that
    # both added and removed occurrences of the same key would otherwise slip
    # past an identity-shaped comparison.
    if axis == "suppressions":
        both = ({row["key"] for row in delta["additions"]}
                & {row["key"] for row in delta["removals"]})
    else:
        both = {_row_key(row) for row in delta["additions"]} & {
            _row_key(row) for row in delta["removals"]
        }
    if both:
        raise EstateError(
            f"the overlay's {axis} both adds and removes {sorted(both)}"
        )


def compose_private(public, overlay, expect_allowlist_digest=None):
    """The private profile: the public artifact with the signed delta applied.

    Refuses on a stale ``publicDigest``, on an addition already present
    publicly, and on a removal naming a row that is not there. Each of those is
    a partition that no longer describes the two profiles it claims to
    partition, and applying it anyway would produce a private document that
    looks complete and is not.

    ``expect_allowlist_digest`` is the second half of D1's refusal: the overlay
    records the digest of the ``.mirror-allowlist`` it was cut against, and an
    allowlist edit moves rows across the boundary the overlay describes. The
    parameter is OPTIONAL because a public clone carries no allowlist to digest,
    and a checker that could not compose without one would be broken exactly
    where the public profile is the only profile.
    """
    _require_keys(
        overlay, _OVERLAY_KEYS, "the private estate overlay",
        optional=_OVERLAY_OPTIONAL_KEYS,
    )
    for field in ("publicDigest", "allowlistDigest"):
        if not isinstance(overlay[field], str) or not overlay[field]:
            raise EstateError(f"the private estate overlay records no {field}")
    expected = active_set_digest(public)
    if overlay["publicDigest"] != expected:
        raise EstateError(
            "the private estate overlay was generated against a different public "
            f"artifact: it records publicDigest {overlay['publicDigest']} and the "
            f"public artifact in this tree digests to {expected}. Regenerate both "
            "profiles together; a stale partition must not compose."
        )
    if (expect_allowlist_digest is not None
            and overlay["allowlistDigest"] != expect_allowlist_digest):
        raise EstateError(
            "the private estate overlay was generated against a different mirror "
            f"allowlist: it records allowlistDigest {overlay['allowlistDigest']} "
            f"and this tree's allowlist digests to {expect_allowlist_digest}. "
            "Regenerate both profiles together; the boundary the delta describes "
            "has moved."
        )
    combined = dict(public)
    for axis in AXES:
        delta = overlay[axis]
        _check_delta(delta, axis)
        base = list(public.get(axis) or [])
        if axis == "suppressions":
            combined[axis] = _compose_suppressions(base, delta)
            continue
        present = _index(base)
        for row in delta["additions"]:
            if _row_key(row) in present:
                raise EstateError(
                    f"the private estate overlay adds a {axis} row that is already "
                    f"public: {row!r}"
                )
        removals = set()
        for row in delta["removals"]:
            key = _row_key(row)
            if key not in present:
                raise EstateError(
                    f"the private estate overlay removes a {axis} row that is not "
                    f"in the public artifact: {row!r}"
                )
            removals.add(key)
        kept = [row for row in base if _row_key(row) not in removals]
        combined[axis] = _sorted_rows(kept + list(delta["additions"]))
    private_execution = overlay.get(
        "pytestExecution", {"benchmarkAdditions": []})
    if (not isinstance(private_execution, dict)
            or set(private_execution) != _PRIVATE_EXECUTION_KEYS):
        raise EstateError(
            "the private estate overlay's pytestExecution must carry exactly "
            f"{sorted(_PRIVATE_EXECUTION_KEYS)}"
        )
    additions = private_execution["benchmarkAdditions"]
    if not isinstance(additions, list):
        raise EstateError(
            "the private estate overlay's "
            "pytestExecution.benchmarkAdditions must be a list"
        )
    if any(not isinstance(selector, str) or not selector for selector in additions):
        raise EstateError(
            "the private estate overlay's "
            "pytestExecution.benchmarkAdditions carries an empty or non-string "
            "selector"
        )
    if len(set(additions)) != len(additions):
        raise EstateError(
            "the private estate overlay's "
            "pytestExecution.benchmarkAdditions carries a duplicate selector"
        )
    public_benchmark = public["pytestExecution"]["legs"][0]["selectors"]
    overlap = sorted(set(public_benchmark) & set(additions))
    if overlap:
        raise EstateError(
            "the private estate overlay adds benchmark selector(s) already "
            f"public: {overlap}"
        )
    combined["pytestExecution"] = {
        "legs": [
            {
                "name": leg["name"],
                "selectors": list(leg["selectors"]) + (
                    list(additions) if leg["name"] == "benchmark" else []),
            }
            for leg in public["pytestExecution"]["legs"]
        ]
    }
    return combined


def _compose_suppressions(base, delta):
    """Apply the delta to the suppression axis as OCCURRENCES, not as rows.

    The unit on this axis is one occurrence, so an addition naming a key the
    public profile already carries is legitimate — it records that the private
    profile has one more of them — and the identity-axis refusal for an
    already-present addition would report a correct partition as malformed.
    What IS refused is removing an occurrence the public profile does not have.
    """
    counts = collections.Counter(expand_suppressions(base))
    additions = collections.Counter(expand_suppressions(delta["additions"]))
    removals = collections.Counter(expand_suppressions(delta["removals"]))
    missing = removals - counts
    if missing:
        raise EstateError(
            "the private estate overlay removes suppression occurrence(s) the "
            f"public artifact does not carry: {sorted(missing.elements())}"
        )
    return collapse_suppressions((counts + additions - removals).elements())


# ---------------------------------------------------------------------------
# Transition authorization
# ---------------------------------------------------------------------------

# #648 D3. The harmful direction is NOT the same on every axis, and getting
# this backwards was the first draft's most serious defect: a new suppression
# is `live - old`, so an additions-are-free rule accepts it and writes it into
# the artifact, leaving the gate unable to detect the one thing #529
# criterion 1 names. Identity axes are harmed by REMOVAL; the suppression axis
# is harmed by ADDITION.
HARMFUL = {
    "pytestNodes": "removal",
    "frontendTests": "removal",
    "suppressions": "addition",
}

PROFILES = ("public", "private")
CAUSES = ("retired", "reclassified", "renamed")
SKIPPED = "skipped"

_LEDGER_DOC_KEYS = frozenset({"schemaVersion", "declarations"})
_LEDGER_COMMON_ENTRY_KEYS = frozenset({
    "id", "axis", "profile", "cause", "reason", "predecessorDigest",
})
_LEDGER_ROW_ENTRY_KEYS = _LEDGER_COMMON_ENTRY_KEYS | {"rows"}
_LEDGER_RENAME_ENTRY_KEYS = _LEDGER_COMMON_ENTRY_KEYS | {"renames"}
_RENAME_KEYS = frozenset({"from", "to"})

_MISSING = object()


def _row_token(axis, row):
    """The DECLARABLE identity of one row on one axis.

    A declaration names rows as strings, so every axis needs a string identity a
    maintainer can paste into a ledger.

    The frontend token is ``runner::id`` and deliberately omits the status. The
    removal rule matches on it, so re-enabling a skipped test — the beneficial
    direction — is not reported as the removal of the skipped row. Only the
    separate status-transition rule reads the status.
    """
    if isinstance(row, str):
        return row
    if axis == "frontendTests":
        return f"{row['runner']}::{row['id']}"
    if axis == "suppressions":
        return row["key"]
    return str(row)


def _axis_tokens(axis, rows):
    """One axis's rows as the multiset of tokens it denotes.

    The suppression axis expands, because it IS a multiset of keys: a row
    recording ``count: 2`` denotes two suppressions. Diffing the ``{key, count}``
    OBJECTS instead inverts the rule for a count change — ``2 -> 1`` reads as
    the addition of a ``{key, count: 1}`` object and would be reported as a new
    suppression, which is the free direction wearing the harmful one's clothes.
    """
    tokens = []
    for row in rows:
        if axis == "suppressions" and isinstance(row, dict):
            tokens.extend([row["key"]] * int(row["count"]))
        else:
            tokens.append(_row_token(axis, row))
    return tokens


def _status_map(rows):
    return {
        (row["runner"], row["id"]): row.get("expectedStatus")
        for row in rows if isinstance(row, dict)
    }


def _harmful_transitions(previous, current):
    """``[(axis, token, kind, description)]`` for every harmful change.

    The KIND is carried rather than inferred from the description, because two
    consumers read it: the ``reclassified`` clause in ``_covers``, which is only
    meaningful for a row that LEFT this profile, and the generator, which has to
    name the rows in a pasteable declaration without re-parsing English.
    """
    harmful = []
    for axis in AXES:
        before = _axis_tokens(axis, previous.get(axis) or [])
        after = _axis_tokens(axis, current.get(axis) or [])
        live_only, recorded_only = axis_diff(
            before, after, multiset=(axis == "suppressions"),
        )
        if HARMFUL[axis] == "addition":
            for token in live_only:
                harmful.append((axis, token, "added", "was added"))
        else:
            for token in recorded_only:
                harmful.append((axis, token, "removed", "was removed"))
    # The status-transition rule, which the set difference above cannot express
    # because the row's identity did not move.
    before_status = _status_map(previous.get("frontendTests") or [])
    after_status = _status_map(current.get("frontendTests") or [])
    for identity, was in sorted(before_status.items()):
        now = after_status.get(identity, _MISSING)
        if now is _MISSING:
            continue  # already reported as a removal
        if was != SKIPPED and now == SKIPPED:
            harmful.append((
                "frontendTests",
                f"{identity[0]}::{identity[1]}",
                SKIPPED,
                f"moved from {was!r} to {SKIPPED!r}",
            ))
    return harmful


def _declaration_rows(entry):
    if entry.get("cause") == "renamed":
        return [pair["from"] for pair in entry.get("renames") or []]
    return entry.get("rows") or []


def _rename_successor(entry, token):
    for pair in entry.get("renames") or []:
        if pair["from"] == token:
            return pair["to"]
    return None


def _covers(entry, axis, token, kind, previous_digest, profile,
            previous_profile_rows, current_profile_rows, other_profile_rows,
            rename_successor_counts):
    """Whether one declaration authorizes one harmful row.

    Every clause is a conjunct, and the ``predecessorDigest`` clause is what
    stops replay: pasting a fresh copy of a spent declaration to remove the same
    identity again fails, because the later transition's predecessor digest
    differs from the one the declaration was bound to.
    """
    if entry.get("profile") != profile:
        return False
    if entry.get("axis") != axis:
        return False
    if entry.get("predecessorDigest") != previous_digest:
        return False
    if token not in _declaration_rows(entry):
        return False
    cause = entry.get("cause")
    if cause == "retired":
        return True
    if cause == "renamed":
        # A rename is a claim about an identity that LEFT this profile and the
        # new identity that arrived in the SAME transition.  Both clauses are
        # load-bearing: accepting a surviving, pre-existing row as the
        # successor would let an unrelated test conceal a real coverage loss;
        # accepting two old rows that name one successor would do the same.
        if kind != "removed" or HARMFUL[axis] != "removal":
            return False
        successor = _rename_successor(entry, token)
        if successor is None or rename_successor_counts.get(successor) != 1:
            return False
        before = _axis_tokens(axis, previous_profile_rows.get(axis) or [])
        after = _axis_tokens(axis, current_profile_rows.get(axis) or [])
        return successor not in before and successor in after
    if cause != "reclassified":
        return False
    # A `reclassified` claim means the row moved ACROSS the mirror boundary, so
    # it can only describe a row that left this profile. On any other kind the
    # other-profile lookup is vacuous by construction: an added suppression and
    # a newly-skipped Playwright row are both present in the other profile
    # whatever happened, because the private profile is a superset of the public
    # one and a status flip does not move a row between them. Accepting the
    # cause there would authorize a real loss on a lookup that cannot fail.
    if kind != "removed":
        return False
    # A `reclassified` claim is VERIFIED, never trusted. Promoting a file in
    # `.mirror-allowlist` moves rows between profiles and is not a retirement,
    # so demanding one would fire on every promotion; but an unverified label
    # would let a real deletion wear it. With no other profile supplied the
    # claim is unverifiable, and an unverifiable claim is not a verified one.
    if other_profile_rows is None:
        return False
    other = other_profile_rows.get(axis)
    if other is None:
        return False
    return token in _axis_tokens(axis, other)


def _flatten_declarations(ledgers):
    """Accept a flat declaration list, a list of ledgers, or a mix of both."""
    out = []
    for item in ledgers or []:
        if isinstance(item, dict) and "declarations" in item:
            out.extend(item["declarations"] or [])
        elif isinstance(item, dict):
            out.append(item)
        elif isinstance(item, (list, tuple)):
            out.extend(item)
        else:
            raise EstateError(f"not a declaration or a ledger: {item!r}")
    return out


def uncovered_transitions(previous, current, ledgers, profile,
                          other_profile_rows=None):
    """Every harmful, uncovered change from ``previous`` to ``current``.

    Returns a list of ``Uncovered``. An empty list means the transition is
    authorized. It renders no verdict and reads no file — the caller supplies
    both documents and the declarations, which is what lets the gate run this
    against EVERY committed parent of a merge, the case where a row can vanish
    from a source file and from the artifact in one resolution.

    ``previous`` may be ``None``, which is a root commit or the seeding run:
    nothing can have been lost relative to a predecessor that does not exist, so
    the answer is an empty list rather than a comparison against an empty
    document. The difference matters on the suppression axis, where every
    recorded row would otherwise read as a new addition.

    The row token is carried STRUCTURALLY on each record. The generator renders
    a pasteable declaration from these rows, and recovering them by re-splitting
    ``message`` truncated every identifier containing a space.
    """
    if profile not in PROFILES:
        raise EstateError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    if previous is None:
        return []
    declarations = _flatten_declarations(ledgers)
    previous_digest = active_set_digest(previous)
    problems = []
    for axis, token, kind, description in _harmful_transitions(previous, current):
        rename_successor_counts = collections.Counter(
            pair["to"]
            for entry in declarations
            if entry.get("cause") == "renamed"
            and entry.get("profile") == profile
            and entry.get("axis") == axis
            and entry.get("predecessorDigest") == previous_digest
            for pair in entry.get("renames") or []
        )
        if any(
            _covers(entry, axis, token, kind, previous_digest, profile,
                    previous, current, other_profile_rows,
                    rename_successor_counts)
            for entry in declarations
        ):
            continue
        problems.append(Uncovered(
            axis, token, kind,
            f"{axis}: {token} {description} without a covering declaration "
            f"(profile {profile}, predecessor digest {previous_digest})",
        ))
    return problems


def validate_transition(previous, current, ledgers, profile,
                        other_profile_rows=None):
    """``uncovered_transitions`` rendered as messages, for a caller that prints.

    Kept as the message-shaped face of one walk rather than a second walk, so a
    reported problem and a structured record can never disagree.
    """
    return [record.message for record in uncovered_transitions(
        previous, current, ledgers, profile, other_profile_rows)]


# ---------------------------------------------------------------------------
# The declaration ledger
# ---------------------------------------------------------------------------


def _check_declaration(entry, index, what):
    where = f"{what} declaration #{index}"
    if not isinstance(entry, dict):
        raise EstateError(f"{where} is not an object")
    allowed = _LEDGER_ROW_ENTRY_KEYS | _LEDGER_RENAME_ENTRY_KEYS
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise EstateError(f"{where} carries unknown key(s): {', '.join(unknown)}")
    missing = sorted(_LEDGER_COMMON_ENTRY_KEYS - set(entry))
    if missing:
        raise EstateError(f"{where} is missing key(s): {', '.join(missing)}")
    if not isinstance(entry["id"], str) or not entry["id"]:
        raise EstateError(f"{where} has no identifier")
    if entry["axis"] not in AXES:
        raise EstateError(f"{where} names an unknown axis {entry['axis']!r}")
    if entry["profile"] not in PROFILES:
        raise EstateError(f"{where} names an unknown profile {entry['profile']!r}")
    if entry["cause"] not in CAUSES:
        raise EstateError(
            f"{where} names an unknown cause {entry['cause']!r}; "
            f"expected one of {list(CAUSES)}"
        )
    expected = (_LEDGER_RENAME_ENTRY_KEYS
                if entry["cause"] == "renamed"
                else _LEDGER_ROW_ENTRY_KEYS)
    unexpected_for_cause = sorted(set(entry) - expected)
    missing_for_cause = sorted(expected - set(entry))
    if unexpected_for_cause:
        raise EstateError(
            f"{where} carries key(s) invalid for cause {entry['cause']!r}: "
            + ", ".join(unexpected_for_cause)
        )
    if missing_for_cause:
        raise EstateError(
            f"{where} is missing key(s) required for cause {entry['cause']!r}: "
            + ", ".join(missing_for_cause)
        )
    reason = entry["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise EstateError(f"{where} carries no reason")
    if entry["cause"] == "renamed":
        if HARMFUL[entry["axis"]] != "removal":
            raise EstateError(
                f"{where} uses renamed on {entry['axis']}, whose harmful "
                "direction is not identity removal"
            )
        renames = entry["renames"]
        if not isinstance(renames, list) or not renames:
            raise EstateError(f"{where} names no renames")
        sources = []
        successors = []
        for pair in renames:
            if not isinstance(pair, dict) or set(pair) != _RENAME_KEYS:
                raise EstateError(f"{where} carries a malformed rename: {pair!r}")
            source = pair["from"]
            successor = pair["to"]
            if (not isinstance(source, str) or not source
                    or not isinstance(successor, str) or not successor):
                raise EstateError(f"{where} carries a non-string rename: {pair!r}")
            if source == successor:
                raise EstateError(f"{where} renames a row to itself: {source!r}")
            sources.append(source)
            successors.append(successor)
        if len(sources) != len(set(sources)):
            raise EstateError(f"{where} repeats a rename source")
        if len(successors) != len(set(successors)):
            raise EstateError(f"{where} maps more than one row to one successor")
    else:
        rows = entry["rows"]
        if not isinstance(rows, list) or not rows:
            raise EstateError(f"{where} names no rows")
        for row in rows:
            if not isinstance(row, str) or not row:
                raise EstateError(f"{where} names a non-string row: {row!r}")
    digest = entry["predecessorDigest"]
    if digest is not None and (not isinstance(digest, str) or not digest):
        raise EstateError(f"{where} has a malformed predecessorDigest: {digest!r}")


def load_ledger(path):
    """Parse and validate one declaration ledger, or raise ``EstateError``.

    Declarations are APPEND-ONLY and are retained after use as tombstones, so a
    ledger only ever grows. A spent declaration is inert rather than deleted,
    because its binding to a predecessor digest already prevents it from
    authorizing anything again.

    A ``predecessorDigest`` of ``null`` is accepted and covers nothing: it can
    never equal a real digest, so it is a placeholder rather than a wildcard.
    """
    return load_ledger_document(_read_json(path),
                                f"the declaration ledger at {path}")


def load_ledger_document(doc, what="the declaration ledger"):
    """Validate one already-parsed ledger, or raise ``EstateError``.

    The document form exists for the same reason the artifact's does: the gate
    reads each ledger as a committed parent RECORDS it, through ``git show``,
    and never as the working tree happens to have it.

    RETURNS THE ``declarations`` LIST, not the document — the reverse of
    ``load_artifact_document``, which returns its whole document. See the note
    there for why the two differ; in short, a ledger has one payload key and
    every caller wants it, while an artifact has five and callers read most of
    them.
    """
    _require_keys(doc, _LEDGER_DOC_KEYS, what)
    declarations = doc["declarations"]
    if not isinstance(declarations, list):
        raise EstateError(f"{what}: declarations must be a list")
    seen = set()
    for index, entry in enumerate(declarations):
        _check_declaration(entry, index, what)
        if entry["id"] in seen:
            raise EstateError(f"{what} repeats the declaration id {entry['id']!r}")
        seen.add(entry["id"])
    return declarations


def merge_ledgers(*ledgers):
    """Every declaration across both profiles, refusing a repeated identifier.

    ``load_ledger`` sees one document, so within-document uniqueness is not the
    whole rule (#648 acceptance criterion 11). The public and mirror-private
    ledgers are separate files, and a declaration pasted from one into the other
    is exactly the replay a per-file check cannot see.
    """
    merged = []
    seen = set()
    for ledger in ledgers:
        for entry in ledger or []:
            identifier = entry["id"]
            if identifier in seen:
                raise EstateError(
                    f"declaration id {identifier!r} appears in more than one "
                    "ledger; identifiers are unique across BOTH"
                )
            seen.add(identifier)
            merged.append(entry)
    return merged


# ---------------------------------------------------------------------------
# The checker the gate runs (#648 D6)
# ---------------------------------------------------------------------------
#
# Everything below is the CALLER half of this module: it reads the tree, reads
# committed history, drives a live derivation and assembles a report. It still
# owns no verdict. `run_check` returns a `Report` of tagged records and
# `render_report` serializes it; `bin/_lib-test-contract.sh` maps each tag to
# the one `contract_fail` literal that names it.

PUBLIC_ARTIFACT = "tests/authoritative-estate.json"
PRIVATE_OVERLAY = "tests/authoritative-estate.private.json"
PUBLIC_LEDGER = "tests/authoritative-estate-retirements.json"
PRIVATE_LEDGER = "tests/authoritative-estate-retirements.private.json"
ALLOWLIST = ".mirror-allowlist"


# ---------------------------------------------------------------------------
# Per-entry expectation visibility (#678, #710)
# ---------------------------------------------------------------------------
#
# Two estate-wide comparisons name a file the mirror does not publish. The
# generated-child loader map in `tests/test_script_loader.py` names
# `tests/test_rewrite_release_notes.py`, and the cache-writer inventory in
# `tests/test_cache_coverage_496_s5b.py` names `bin/cctally-snapshot-measure`.
# Both are exact equality and both are collected by the public clone, where the
# named file is absent, so on that profile each expects one entry the tree
# cannot produce.
#
# Visibility is DECLARED per entry rather than inferred from the tree. An
# unannotated entry is public, which is the fail-closed direction: a loader
# site or a cache writer added later is expected on both profiles until
# somebody classifies it. Only which declarations apply is profile-dependent.
# The comparison itself never is, and is never relaxed to a subset test because
# `.mirror-allowlist` is absent.

PUBLIC = "public"
PRIVATE = "private"

Expectation = collections.namedtuple(
    "Expectation", ("value", "visibility", "owner"))
"""One expectation entry, its declared visibility, and the path that owns it.

``owner`` is a repository-relative path. ``validate_owner_paths`` hands it to
the tree's own allowlist classifier, so a declaration that has drifted away
from `.mirror-allowlist` fails rather than silently narrowing a comparison.
"""


def private_expectation(value, owner):
    """``value``, declared to exist only on the private profile."""
    if not isinstance(owner, str) or not owner.strip():
        raise EstateError(
            "a private expectation must name the repository-relative path that "
            f"owns it; got {owner!r}")
    return Expectation(value, PRIVATE, owner)


def active_profile(repo=None):
    """Which profile this tree is, decided by `.mirror-allowlist`'s presence.

    `bin/_lib-test-contract.sh:440` already states that rule for admission. A
    second, differently-derived signal here would be a second answer to the
    same question, so this reads the same marker.
    """
    root = Path(repo) if repo is not None else _repo_default()
    return PRIVATE if (root / ALLOWLIST).exists() else PUBLIC


def applicable_expectations(entries, *, profile):
    """``entries`` without the declarations ``profile`` does not carry.

    Returns a plain mapping of key to raw value, so the caller compares the
    same shape it always did. An entry that is not an ``Expectation`` is
    public and is passed through unchanged.
    """
    if profile not in PROFILES:
        raise EstateError(
            f"unknown profile {profile!r}; expected one of {PROFILES}")
    applicable = {}
    for key, entry in dict(entries).items():
        if isinstance(entry, Expectation):
            if entry.visibility == PRIVATE and profile != PRIVATE:
                continue
            applicable[key] = entry.value
        else:
            applicable[key] = entry
    return applicable


def _load_allowlist_matcher(path):
    """Load `.githooks/_match.py` WITHOUT registering it in ``sys.modules``.

    Two synthetic trees are classified in one interpreter by this module's own
    tests, and a registered name would answer the second tree's question with
    the first tree's classifier.
    """
    spec = importlib.util.spec_from_file_location(
        "_lib_test_estate_allowlist_matcher", path)
    if spec is None or spec.loader is None:
        raise EstateError(f"the allowlist classifier at {path} is not loadable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise EstateError(
            f"the allowlist classifier at {path} did not load: {exc}") from exc
    if not callable(getattr(module, "classify", None)):
        raise EstateError(
            f"the allowlist classifier at {path} exposes no classify()")
    return module


def validate_owner_paths(entries, repo=None):
    """Refuse a private declaration the real allowlist actually publishes.

    Private profile only. The public projection carries neither
    `.mirror-allowlist` nor `.githooks/_match.py` by design, so it consumes the
    declared visibility directly; what is skipped there is this validation, not
    the comparison the declaration feeds.

    On a tree that claims to be private, an absent classifier is a refusal
    rather than a silent success, matching `contract_admit_visibility`.

    This never reimplements allowlist matching. It reads the tree's own
    `.mirror-allowlist` and hands the text to the tree's own classifier.
    """
    root = Path(repo) if repo is not None else _repo_default()
    if active_profile(root) != PRIVATE:
        return None
    declared = sorted({
        entry.owner for entry in dict(entries).values()
        if isinstance(entry, Expectation) and entry.visibility == PRIVATE
    })
    if not declared:
        return None
    matcher = root / ".githooks" / "_match.py"
    if not matcher.exists():
        raise EstateError(
            f"this tree is the private profile ({ALLOWLIST} is present) but the "
            f"allowlist classifier {matcher} is absent, so a declared owner "
            "path cannot be checked against the real mirror boundary")
    classify = _load_allowlist_matcher(matcher).classify
    text = (root / ALLOWLIST).read_text(encoding="utf-8")
    published = set(classify(list(declared), allowlist_text=text)["public"])
    drifted = [path for path in declared if path in published]
    if drifted:
        raise EstateError(
            "an expectation entry declares private visibility but "
            f"{ALLOWLIST} publishes the path that owns it: "
            f"{', '.join(drifted)}. Either drop the declaration or negate the "
            "path in the allowlist; the two must agree.")
    return None


# One line of the rendered report may not carry either of these, because the
# shell reads it with `IFS=$'\t' read -r`. Rows come from node identifiers,
# Playwright titles and suppression keys, none of which is guaranteed free of
# them, so every field is scrubbed rather than assumed clean.
_FIELD_TRANSLATION = {ord("\t"): " ", ord("\n"): " ", ord("\r"): " "}

# How many rows of one finding the report names individually. The remainder is
# carried as a count: a lost estate is thousands of rows, and a report that
# printed all of them would bury the one line an operator needs.
REPORT_ROW_LIMIT = 20

Committed = collections.namedtuple("Committed", ("status", "document", "message"))
"""What one committed revision records at one path.

``status`` is ``"recorded"``, ``"absent"`` or ``"unreadable"``. The third is NOT
the second: a revision that records no artifact authorizes everything, because
nothing can have been lost relative to a predecessor that does not exist, while
a revision this process could not READ authorizes nothing and must abort the
run. Collapsing the two silently disables the whole transition check on an
unusable repository, which is the inability D9 forbids substituting for.
"""

ExecutionPlan = collections.namedtuple("ExecutionPlan", ("legs", "selectors"))
"""The ``pytestExecution`` declaration expanded against the recorded estate.

``legs`` maps each leg name to the sorted tuple of node identifiers that leg
owns. ``selectors`` maps each leg name to its ``(kind, selector)`` pairs, where
``kind`` is ``"file"`` or ``"node"``; the complement leg carries none.
"""

UncoveredRecord = collections.namedtuple(
    "UncoveredRecord", ("profile", "axis", "token", "kind", "message"))
"""One uncovered harmful transition, with the profile it was found in.

``uncovered_transitions`` is per-profile and does not carry the profile on its
records, because the caller supplied it. The gate checks BOTH profiles in one
pass, so which profile a record came from stops being implied by context.
"""

Report = collections.namedtuple(
    "Report", ("profile", "findings", "uncovered", "inabilities", "notes"))
"""Everything one check observed.

``inabilities`` is a list of ``(tag, message)`` with ``tag`` in
``{"artifact-unreadable", "discovery-failed", "partition-invalid"}``. An empty
``findings``, ``uncovered`` and ``inabilities`` is the only shape that means the
record and the tree agree.
"""


def _scrub(value):
    return str(value).translate(_FIELD_TRANSLATION)


# ---------------------------------------------------------------------------
# Committed history
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=False,
    )


def predecessor_revisions(repo):
    """``("ok", [rev, ...])`` or ``("unreadable", message)``.

    The predecessors of the tree under test are HEAD and — when HEAD is a merge
    — every one of its parents. D3 names the merge case explicitly because it is
    the one where a row vanishes from a source file and from the artifact in a
    single resolution: the merge commit then agrees with itself about what it
    records, and only its parents remember the row.

    An unborn branch returns an empty list, because a repository with no commits
    records no artifact and nothing can have been lost relative to it. A
    directory that is not a git repository returns ``"unreadable"`` instead: not
    being able to read the predecessor is not the same as there being none, and
    a run that treats the two alike silently checks nothing.
    """
    probe = _git(repo, "rev-parse", "--git-dir")
    if probe.returncode != 0:
        return ("unreadable",
                f"git could not read a repository at {repo}: "
                f"{probe.stderr.strip() or 'no diagnostic'}")
    head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if head.returncode != 0:
        return ("ok", [])
    sha = head.stdout.strip()
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha)
    if parents.returncode != 0:
        return ("unreadable",
                f"git could not read the parents of {sha[:12]}: "
                f"{parents.stderr.strip() or 'no diagnostic'}")
    fields = parents.stdout.split()
    revisions = [sha]
    if len(fields) > 2:
        revisions.extend(fields[1:])
    return ("ok", revisions)


def committed_document(repo, rev, path):
    """The JSON document ``rev`` RECORDS at ``path``, as a ``Committed``.

    Read through git rather than off disk, because the working tree's copy is
    exactly what the transition check is judging. An edit that already dropped a
    row would otherwise establish itself as its own baseline, and the removal
    the ledger exists to authorize would never be seen at all.

    ``git ls-tree`` is what separates absent from unreadable. ``git show``
    cannot: it exits non-zero both for a path a revision does not carry and for
    a revision it cannot resolve, and one of those is free while the other must
    abort the run. ``ls-tree`` exits 0 with empty output for the first and
    non-zero for the second.
    """
    listing = _git(repo, "ls-tree", "--name-only", rev, "--", path)
    if listing.returncode != 0:
        return Committed(
            "unreadable", None,
            f"git could not read {rev[:12]} in {repo}: "
            f"{listing.stderr.strip() or 'no diagnostic'}")
    if not listing.stdout.strip():
        return Committed("absent", None, f"{rev[:12]} records no {path}")
    show = _git(repo, "show", f"{rev}:{path}")
    if show.returncode != 0:
        return Committed(
            "unreadable", None,
            f"git could not read {rev[:12]}:{path}: "
            f"{show.stderr.strip() or 'no diagnostic'}")
    try:
        return Committed("recorded", json.loads(show.stdout), "")
    except json.JSONDecodeError as exc:
        return Committed("unreadable", None,
                         f"{rev[:12]}:{path} is not valid JSON: {exc}")


# ---------------------------------------------------------------------------
# The execution declaration (#648 D7)
# ---------------------------------------------------------------------------


def classify_selector(selector):
    """``"node"`` for an exact node identifier, ``"file"`` for a module path.

    The distinction is what the aggregator needs to build argv: a file is
    excluded from the bulk leg with ``--ignore`` and a node with ``--deselect``,
    and the two are not interchangeable.
    """
    return "node" if "::" in selector else "file"


def expand_selector(selector, nodes):
    """Every recorded node identifier one selector names."""
    if classify_selector(selector) == "node":
        return [node for node in nodes if node == selector]
    prefix = selector + "::"
    return [node for node in nodes if node == selector or node.startswith(prefix)]


def plan_execution(artifact):
    """Expand ``pytestExecution`` against the recorded estate, or raise.

    Three things are PROVEN rather than assumed, because D7 makes this
    declaration the single source both legs are built from and a declaration
    nobody checks is a second set of literals wearing a schema:

    * every selector matches at least one recorded node, so a renamed or deleted
      target becomes an admission failure instead of a leg that quietly runs
      nothing;
    * every required leg's share is non-empty, which the selector rule alone
      cannot see for the complement leg, since it carries no selector; and
    * the shares are pairwise disjoint and their union is the complete estate,
      so no node is run twice and none is run at all.
    """
    nodes = list(artifact.get("pytestNodes") or [])
    known = set(nodes)
    legs = collections.OrderedDict()
    selectors = collections.OrderedDict()
    claimed = set()
    for leg in artifact["pytestExecution"]["legs"]:
        name = leg["name"]
        if leg["selectors"] == [COMPLEMENT_SELECTOR]:
            legs[name] = None  # resolved below, once every share is known
            selectors[name] = ()
            continue
        share = set()
        pairs = []
        for selector in leg["selectors"]:
            matched = expand_selector(selector, nodes)
            if not matched:
                raise EstateError(
                    f"pytestExecution leg {name!r} declares the selector "
                    f"{selector!r}, which matches no node in the recorded "
                    f"estate. A target that no longer exists is an admission "
                    f"failure, never a leg that silently runs less."
                )
            share.update(matched)
            pairs.append((classify_selector(selector), selector))
        already = share & claimed
        if already:
            raise EstateError(
                f"pytestExecution leg {name!r} claims node(s) another leg "
                f"already claims: {sorted(already)[:5]}"
            )
        claimed.update(share)
        legs[name] = tuple(sorted(share))
        selectors[name] = tuple(pairs)
    complement = tuple(sorted(known - claimed))
    for name, share in list(legs.items()):
        if share is None:
            legs[name] = complement
    for name, share in legs.items():
        if not share:
            raise EstateError(
                f"pytestExecution leg {name!r} would run no node at all. Every "
                f"declared leg is REQUIRED, so an empty share is a leg that "
                f"silently verifies nothing."
            )
    union = set()
    for share in legs.values():
        union.update(share)
    if union != known:
        missing = sorted(known - union)
        raise EstateError(
            f"pytestExecution does not partition the recorded estate: "
            f"{len(missing)} node(s) belong to no leg, beginning with "
            f"{missing[:3]}"
        )
    return ExecutionPlan(legs, selectors)


# ---------------------------------------------------------------------------
# The live derivation
# ---------------------------------------------------------------------------


def _load_discovery_kernel(repo):
    """Import ``bin/_lib_estate_discovery.py`` by path.

    Imported HERE and not at module scope so this file stays the stdlib-only
    leaf its docstring claims. Every library consumer — the generator, the
    tests, a public clone reading the schema — gets a module with no sibling
    import at all; only the checker, which by definition needs a live tree,
    pays for one.
    """
    path = Path(repo) / "bin" / "_lib_estate_discovery.py"
    spec = importlib.util.spec_from_file_location("_lib_estate_discovery", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"the estate discovery kernel is absent at {path}")
    module = importlib.util.module_from_spec(spec)
    # The module that is REGISTERED is the module that is EXECUTED, and it is
    # registered first. `sys.modules.setdefault` did neither: it returned any
    # pre-existing entry, which this function discarded, and then executed into
    # the new object regardless, leaving the name bound to a module the caller
    # never receives. The kernel combines `from __future__ import annotations`
    # with `@dataclass`, so `dataclasses` resolves each field's annotation
    # through `sys.modules[cls.__module__]` while the class body executes;
    # binding the wrong object there is a live hazard rather than a tidiness
    # point.
    #
    # A pre-existing entry is OVERWRITTEN rather than returned, because the
    # kernel this function must load is the one belonging to `repo`, and
    # returning a cached module would hand a caller checking one tree the
    # kernel of another. Whatever was bound before is restored if the load
    # fails, so a failure leaves the interpreter as it found it.
    previous = sys.modules.get("_lib_estate_discovery")
    sys.modules["_lib_estate_discovery"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop("_lib_estate_discovery", None)
        else:
            sys.modules["_lib_estate_discovery"] = previous
        raise
    return module


def discover_live(repo):
    """All three live axes for ``repo``, in the artifact's own row shapes.

    Builds the e2e fixture runtime in a TEMPORARY directory and passes it
    through the kernel's ``runtime_dir`` seam, so enumerating the tree writes
    nothing into it. Nine Playwright spec files read that manifest at module
    load, and without it the runner reports an empty suite tree rather than
    failing, which is exactly the smaller-set-that-looks-live this whole
    mechanism exists to refuse.
    """
    repo = Path(repo)
    discovery = _load_discovery_kernel(repo)
    builder = repo / "bin" / "build-e2e-fixtures.py"
    if not builder.is_file():
        raise RuntimeError(f"the e2e fixture builder is absent at {builder}")
    with tempfile.TemporaryDirectory(prefix="cctally-estate-check-") as scratch:
        # Resolved, not merely absolute: on macOS `/var` is a symlink to
        # `/private/var` and the frontend runners report the resolved spelling.
        runtime = Path(scratch).resolve() / "runtime"
        runtime.mkdir(parents=True)
        built = subprocess.run(
            [sys.executable, str(builder), "--out", str(runtime)],
            cwd=str(repo), capture_output=True, text=True, check=False,
        )
        if built.returncode != 0:
            raise RuntimeError(
                "the e2e fixture runtime could not be built (exit "
                f"{built.returncode}): {(built.stderr or built.stdout)[-2000:]}"
            )
        sets = type("EstateSets", (), {})()
        # Pytest and frontend collection are independent subprocess trees.
        # Serial collection pushed the projected-public-tree acceptance node
        # beyond the authoritative suite's 120-second per-test cap as the
        # estate grew. Overlap those two waits while the in-process AST
        # suppression scan runs; result ordering remains fixed below.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pytest_nodes = pool.submit(discovery.collect_pytest_nodes, repo)
            frontend_tests = pool.submit(
                discovery.collect_frontend_tests, repo, runtime_dir=runtime)
            sets.suppressions = tuple(
                discovery.scan_suppressions(repo / "tests", base=repo))
            sets.pytest_nodes = tuple(pytest_nodes.result())
            sets.frontend_tests = tuple(frontend_tests.result())
    return live_axes(sets)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _load_tree_ledgers(repo):
    """Both ledgers as the TREE has them, not as any predecessor recorded them.

    Deliberately the working tree and not committed history, which is the one
    place this check departs from `committed_document`'s rule. A shrink and the
    declaration that authorizes it land together — the maintainer edits both
    files in one change — so reading the predecessor's ledger would make a
    working-tree shrink unauthorizable by construction: the entry covering it
    cannot exist in a commit that predates it. The predecessor supplies the
    baseline; the tree supplies the claim.
    """
    ledgers = []
    for name in (PUBLIC_LEDGER, PRIVATE_LEDGER):
        path = Path(repo) / name
        if path.exists():
            ledgers.append(load_ledger(path))
    return merge_ledgers(*ledgers) if ledgers else []


def _other_profile_rows(document):
    return {axis: document.get(axis) or [] for axis in AXES}


def run_check(repo, profile, discover=None, *, transitions_only=False):
    """Compare the record against the tree and against committed history.

    ``profile`` is the tree's own profile, supplied by the caller rather than
    inferred from which files happen to exist. Inferring it would make three of
    D1's refusals unreachable: a private tree whose overlay went missing would
    silently check as a public clone, and an overlay that appeared on a public
    tree would silently promote it.

    The LIVE comparison is against the active profile alone, because deriving
    the other profile's live set means materializing a projection, which is a
    maintainer-only operation. The TRANSITION check runs on BOTH profiles,
    because it is pure document arithmetic and because a private tree that
    validated only its own profile would accept a public shrink compensated by
    an overlay addition — the private profile is then unchanged, and the loss
    surfaces only in the mirror's own CI.
    """
    repo = Path(repo)
    discover = discover or discover_live
    findings = []
    uncovered = []
    inabilities = []
    notes = []

    def _unable(tag, message):
        inabilities.append((tag, _scrub(message)))

    if profile not in PROFILES:
        _unable("partition-invalid", f"unknown profile {profile!r}")
        return Report(profile, findings, uncovered, inabilities, notes)

    try:
        public = load_artifact(repo / PUBLIC_ARTIFACT)
    except EstateError as exc:
        _unable("artifact-unreadable", exc)
        return Report(profile, findings, uncovered, inabilities, notes)

    overlay_path = repo / PRIVATE_OVERLAY
    overlay_present = overlay_path.exists()
    active = public
    if profile == "private" and not overlay_present:
        _unable("partition-invalid",
                f"this tree classifies as the private profile and records no "
                f"{PRIVATE_OVERLAY}; the private profile is the public artifact "
                f"plus a required signed delta, and a missing delta is a "
                f"partition that cannot be applied rather than an empty one")
        return Report(profile, findings, uncovered, inabilities, notes)
    if profile == "public" and overlay_present:
        _unable("partition-invalid",
                f"this tree classifies as the public profile and yet records "
                f"{PRIVATE_OVERLAY}; an overlay on a public tree describes a "
                f"boundary this profile does not have")
        return Report(profile, findings, uncovered, inabilities, notes)
    if profile == "private":
        allowlist = repo / ALLOWLIST
        try:
            expected = allowlist_digest(allowlist) if allowlist.exists() else None
            active = compose_private(public, _read_json(overlay_path),
                                     expect_allowlist_digest=expected)
        except EstateError as exc:
            _unable("partition-invalid", exc)
            return Report(profile, findings, uncovered, inabilities, notes)

    try:
        ledgers = _load_tree_ledgers(repo)
    except EstateError as exc:
        _unable("artifact-unreadable", exc)
        ledgers = None

    if ledgers is not None:
        status, revisions = predecessor_revisions(repo)
        if status != "ok":
            _unable("artifact-unreadable", revisions)
        else:
            uncovered.extend(
                _transitions_against(repo, revisions, public, active, profile,
                                     ledgers, _unable))

    if not transitions_only:
        try:
            live = discover(repo)
        except Exception as exc:  # the kernel raises its own error type
            _unable("discovery-failed", f"{type(exc).__name__}: {exc}")
        else:
            findings.extend(compare(active, live))

    for finding in findings:
        notes.append(
            f"estate: {len(finding.rows)} {finding.axis} row(s) are "
            f"{'in the tree and not in the record' if finding.code_tag == 'unexpected' else 'in the record and not in the tree'}"
        )
    return Report(profile, findings, uncovered, inabilities, notes)


def _transitions_against(repo, revisions, public, active, profile, ledgers,
                         unable):
    """Every uncovered harmful transition from every predecessor, both profiles."""
    found = []
    for rev in revisions:
        recorded = committed_document(repo, rev, PUBLIC_ARTIFACT)
        if recorded.status == "unreadable":
            unable("artifact-unreadable", recorded.message)
            continue
        if recorded.status == "absent":
            continue
        try:
            previous_public = load_artifact_document(
                recorded.document, f"the artifact {rev[:12]} records")
        except EstateError as exc:
            unable("artifact-unreadable", exc)
            continue
        previous_private = previous_public
        previous_overlay = committed_document(repo, rev, PRIVATE_OVERLAY)
        if previous_overlay.status == "unreadable":
            unable("artifact-unreadable", previous_overlay.message)
            continue
        if previous_overlay.status == "recorded":
            try:
                # No allowlist digest is supplied. The boundary at a PREDECESSOR
                # is the one that revision recorded, and comparing it against
                # this tree's allowlist would refuse every commit that legally
                # moved a file across the boundary.
                previous_private = compose_private(
                    previous_public, previous_overlay.document)
            except EstateError as exc:
                unable("artifact-unreadable", exc)
                continue
        pairs = [("public", previous_public, public, active)]
        if profile == "private":
            pairs.append(("private", previous_private, active, public))
        for name, previous, current, other in pairs:
            for record in uncovered_transitions(
                previous, current, ledgers, name,
                other_profile_rows=_other_profile_rows(other),
            ):
                found.append(UncoveredRecord(
                    name, record.axis, record.token, record.kind,
                    f"{rev[:12]}: {record.message}"))
    return found


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def render_report(report):
    """One tab-separated record per line, for a Bash 3.2 reader.

    The shell owns every reason-code literal, so this carries TAGS: the
    ``code_tag`` and axis of a finding, the inability's own name, and nothing
    that looks like a verdict. The final ``end`` line is load-bearing in the
    fail-closed direction — a report that stops early has no ``end`` line, and
    the caller treats its absence as an inability rather than as agreement.
    """
    lines = ["version\t1", f"profile\t{_scrub(report.profile)}"]
    for tag, message in report.inabilities:
        lines.append(f"inability\t{tag}\t{_scrub(message)}")
    for finding in report.findings:
        rows = [_scrub(_row_token(finding.axis, row)) for row in finding.rows]
        lines.append(
            f"finding\t{finding.code_tag}\t{finding.axis}\t{len(rows)}\t{rows[0]}")
        for row in rows[:REPORT_ROW_LIMIT]:
            lines.append(f"row\t{finding.code_tag}\t{finding.axis}\t{row}")
    if report.uncovered:
        first = report.uncovered[0]
        lines.append(f"unauthorized\t{len(report.uncovered)}\t"
                     f"{_scrub(first.profile)}\t{_scrub(first.axis)}\t"
                     f"{_scrub(first.token)}")
        for record in report.uncovered[:REPORT_ROW_LIMIT]:
            lines.append(
                f"uncovered\t{_scrub(record.profile)}\t{_scrub(record.axis)}\t"
                f"{_scrub(record.kind)}\t{_scrub(record.token)}\t"
                f"{_scrub(record.message)}")
    for note in report.notes:
        lines.append(f"note\t{_scrub(note)}")
    problems = bool(report.findings or report.uncovered or report.inabilities)
    lines.append("end\tproblems" if problems else "end\tok")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _repo_default():
    return Path(__file__).resolve().parent.parent


def _active_artifact(repo, profile):
    """The artifact for ``profile``, composed when the profile is private."""
    public = load_artifact(Path(repo) / PUBLIC_ARTIFACT)
    if profile != "private":
        return public
    overlay_path = Path(repo) / PRIVATE_OVERLAY
    if not overlay_path.exists():
        raise EstateError(f"the private profile records no {PRIVATE_OVERLAY}")
    allowlist = Path(repo) / ALLOWLIST
    expected = allowlist_digest(allowlist) if allowlist.exists() else None
    return compose_private(public, _read_json(overlay_path),
                           expect_allowlist_digest=expected)


def _emit_plan(repo, profile, out_dir, stream):
    """Print the leg records, and write each leg's node identifiers when asked.

    Separate from ``--check`` on purpose (#648 D12). A ``--harness`` subset runs
    no estate check and still runs the pytest phase under ``--with-pytest``, so
    the aggregator needs the leg expansion on a path that costs no derivation.
    This one reads the committed artifact and nothing else.

    ``out_dir`` is ``None`` for the admission-time validation, which exists to
    make a selector that matches nothing an ADMISSION failure rather than a leg
    that silently runs less (acceptance criterion 17). That call has nowhere to
    put node files and needs none: admission precedes ``LOGDIR``, and creating a
    directory there would leave one behind on every refusal.
    """
    plan = plan_execution(_active_artifact(repo, profile))
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    for name, nodes in plan.legs.items():
        target = ""
        if out_dir is not None:
            target = str(out_dir / f"{name}.txt")
            Path(target).write_text(
                "".join(node + "\n" for node in nodes), encoding="utf-8")
        print(f"leg\t{name}\t{len(nodes)}\t{target}", file=stream)
        for kind, selector in plan.selectors[name]:
            print(f"selector\t{name}\t{kind}\t{_scrub(selector)}", file=stream)
    print("end\tok", file=stream)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="_lib_test_estate.py",
        description="Compare the committed estate artifact against this tree.",
    )
    parser.add_argument("--repo", default=None,
                        help="repository root (default: this script's parent)")
    parser.add_argument("--profile", choices=list(PROFILES), required=True,
                        help="the tree's profile, as the caller resolved it")
    parser.add_argument("--check", action="store_true",
                        help="run the complete structural check and print a "
                             "tagged report")
    parser.add_argument("--check-transitions", action="store_true",
                        help="check committed predecessor transitions only; "
                             "perform no live test discovery")
    parser.add_argument("--plan-legs", metavar="DIR", default=None,
                        help="expand pytestExecution into DIR and print the "
                             "leg records; performs no live derivation")
    parser.add_argument("--validate-legs", action="store_true",
                        help="expand pytestExecution and print the leg records "
                             "without writing anything; the admission-time form")
    return parser


def main(argv=None):
    """Exit 0 when a report was produced, 2 on usage, 3 when none could be.

    The exit code is NOT the verdict. A tree that disagrees with its record
    still exits 0 here and says so in the report, because the contract shell
    owns the verdict and the reason code. Exit 3 means this process could not
    produce a report at all, which the shell fails closed on.
    """
    args = build_parser().parse_args(argv)
    repo = Path(args.repo) if args.repo else _repo_default()
    if (not args.check and not args.check_transitions
            and args.plan_legs is None and not args.validate_legs):
        print("_lib_test_estate.py: pass --check, --check-transitions, "
              "--plan-legs or --validate-legs",
              file=sys.stderr)
        return 2
    try:
        if args.plan_legs is not None or args.validate_legs:
            _emit_plan(repo, args.profile, args.plan_legs, sys.stdout)
        if args.check:
            sys.stdout.write(render_report(run_check(repo, args.profile)))
        if args.check_transitions:
            sys.stdout.write(render_report(run_check(
                repo, args.profile, transitions_only=True)))
    except EstateError as exc:
        print(f"_lib_test_estate.py: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"_lib_test_estate.py: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
