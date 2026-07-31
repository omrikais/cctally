"""Pure kernel for the Codex quota change ledger (public issue omrikais/cctally#5).

Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1-§2.

Three key spaces meet here and confusing any two of them is silent:

* **Raw coordinates** are what the SQLite triggers record and what the loader's
  exact filter matches: the stored ``(source_root_key, logical_limit_key,
  observed_slot, window_minutes, canonical-or-raw reset)`` of one row image.
  Nothing is interpreted, because interpretation is population-dependent (the
  account fold reads every observation of a window) and cannot be expressed in
  SQL at all.

* **The loading unit** is the raw group closed over the ``window_minutes`` snap.
  The provider occasionally reports a weekly window as ``10081``, and that one
  minute lives in BOTH the limit key and a column of its own, so two raw groups
  can interpret into one window. Loading a raw group alone would then hand the
  projector a PARTIAL population — a wrong block, not a stale one. The loading
  unit is also the reverse map persisted as ``quota_window_blocks.
  physical_group_key`` and the unit the per-group digest is taken over.

* **The interpreted identity** is what ``QuotaObservation`` carries after the
  read path snaps the length and rewrites the limit key for a model-scoped pool.
  One loading unit can contain several interpreted identities (a Spark window
  and an ordinary one), which is fine and deliberate: the unit is a partition of
  the root's observations, so loading it whole gives every identity inside it
  its complete population.

The bridge between the last two is ``strip_model_pool``. A model-scoped
interpreted key is the ordinary key with a ``modelPool`` member added, so
removing that member recovers the loading unit's key — which is what lets a
block stamp its own unit without the loader having to carry raw coordinates
alongside every observation.

Everything here is pure: no SQLite, no clock, no I/O.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Iterable, Mapping, Sequence

from _lib_jsonl import (
    _codex_canonical_json,
    codex_snap_equivalent_limit_keys,
    codex_snap_equivalent_window_minutes,
    snap_codex_window_minutes,
    snap_window_minutes,
)


#: The ledger's row-image column suffixes, in group order.
LEDGER_GROUP_SUFFIXES = (
    "source_root_key",
    "logical_limit_key",
    "observed_slot",
    "window_minutes",
    "resets_at_utc",
    "canonical_resets_at_utc",
)

#: Field separator for the serialized group key. A unit separator cannot occur
#: in a canonical-JSON limit key, an ISO timestamp, a slot name or a root key,
#: so the join is unambiguous without escaping.
_KEY_SEPARATOR = "\x1f"


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _side_group(row: Mapping[str, object], prefix: str) -> tuple | None:
    """One row image's raw group, or ``None`` when it cannot form one.

    A side with no root, slot, limit key, length or reset is a row the loader
    would skip anyway (its required-text guard drops it before it can become a
    quota identity), so there is nothing for the projector to expand.
    """
    root = _text(row.get(prefix + "source_root_key"))
    limit_key = _text(row.get(prefix + "logical_limit_key"))
    slot = _text(row.get(prefix + "observed_slot"))
    minutes = row.get(prefix + "window_minutes")
    # COALESCE, exactly as the loader's predicate does: a row whose anchor was
    # never resolved falls back to its raw reset on every read path, so the
    # group coordinate has to fall back with it.
    reset = (
        _text(row.get(prefix + "canonical_resets_at_utc"))
        or _text(row.get(prefix + "resets_at_utc"))
    )
    if None in (root, limit_key, slot, reset) or minutes is None:
        return None
    try:
        minutes_int = int(minutes)
    except (TypeError, ValueError):
        return None
    return (root, limit_key, slot, minutes_int, reset)


def expand_dirty_groups(
    ledger_rows: Iterable[Mapping[str, object]],
) -> frozenset[tuple]:
    """Map ledger entries to the union of their old and new raw group coordinates.

    Both sides, always. A semantic UPDATE can move rows BETWEEN groups, so the
    pass has to re-materialize the new group AND sweep the old one — a group
    that has lost all its members is swept to nothing, and that only works if
    the sweep knows where the rows came from. An insert contributes only a new
    side and a delete only an old one, which falls out of the same rule rather
    than needing a case per op.
    """
    groups: set[tuple] = set()
    for row in ledger_rows:
        for prefix in ("old_", "new_"):
            group = _side_group(row, prefix)
            if group is not None:
                groups.add(group)
    return frozenset(groups)


def snap_equivalent_raw_groups(groups: Iterable[tuple]) -> frozenset[tuple]:
    """Close a raw group set over the ``window_minutes`` snap.

    Rows are STORED under whichever spelling the provider sent, so the exact
    filter has to ask for each of them by name. The set is bounded at nine per
    input group (three equivalent limit keys x three equivalent lengths) and
    contains the input itself, so a non-snappable group resolves to exactly
    itself. Asking for a combination no row carries costs one indexed miss.

    The cross product rather than a zip is deliberate: a key whose
    ``windowMinutes`` member disagrees with the column (only reachable by hand
    repair) would otherwise fall outside its own closure.
    """
    widened: set[tuple] = set()
    for root, limit_key, slot, minutes, reset in groups:
        for key in codex_snap_equivalent_limit_keys(limit_key):
            for equivalent in codex_snap_equivalent_window_minutes(minutes):
                widened.add((root, key, slot, int(equivalent), reset))
    return frozenset(widened)


def strip_model_pool(logical_limit_key: str) -> str:
    """Return the limit key with any ``modelPool`` member removed.

    A model-scoped interpreted key is the ordinary key plus that member, so
    dropping it recovers the loading unit's key — the value a ledger entry's raw
    coordinates snap to. Fails OPEN on shape: a key this cannot parse is
    returned exactly as it arrived rather than rebuilt from guessed members,
    which keeps a hand-written or legacy key comparing equal to itself.
    """
    if not isinstance(logical_limit_key, str):
        return logical_limit_key
    try:
        payload = json.loads(logical_limit_key)
    except (json.JSONDecodeError, TypeError, ValueError):
        return logical_limit_key
    if not isinstance(payload, dict) or "modelPool" not in payload:
        return logical_limit_key
    payload.pop("modelPool")
    try:
        return _codex_canonical_json(payload)
    except (TypeError, ValueError):  # pragma: no cover - non-serializable member
        return logical_limit_key


def normalize_reset(value: object) -> str:
    """One spelling for one instant.

    The cache retains whatever the provider sent — ``2026-07-01T05:00:00Z`` from
    one writer, ``…+00:00`` from another — and every SQL comparison in this
    subsystem wraps the column in ``unixepoch()`` for exactly that reason. The
    loading-unit key is a TEXT key compared with ``=``, so it has no such
    escape: a ledger entry naming the ``Z`` spelling and a block stamping the
    ``+00:00`` one would be two different units, the scoped sweep would look for
    a key nothing wrote, and a vanished window's block would survive.

    Fails OPEN: a value this cannot parse is returned as text, so it still
    compares equal to itself.
    """
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return text
    return parsed.astimezone(dt.timezone.utc).isoformat()


def loading_unit_from_raw(group: Sequence) -> tuple:
    """The loading unit one raw group belongs to."""
    root, limit_key, slot, minutes, reset = group
    return (
        str(root),
        snap_window_minutes(str(limit_key)),
        str(slot),
        int(snap_codex_window_minutes(int(minutes))),
        normalize_reset(reset),
    )


def loading_unit_from_identity(
    *,
    source_root_key: str,
    logical_limit_key: str,
    observed_slot: str,
    window_minutes: int,
    canonical_reset_iso: str,
) -> tuple:
    """The loading unit an INTERPRETED identity belongs to.

    The identity's length is already snapped and its key already carries any
    model pool, so the only transform left is removing that pool member. Both
    this and ``loading_unit_from_raw`` must land on the same value for the same
    physical rows — that identity is what lets a block record its own unit while
    a ledger entry names the same unit from raw coordinates alone.
    """
    return (
        str(source_root_key),
        strip_model_pool(str(logical_limit_key)),
        str(observed_slot),
        int(window_minutes),
        normalize_reset(canonical_reset_iso),
    )


def physical_group_key_text(unit: Sequence) -> str:
    """Serialize a loading unit for storage and for an SQL ``IN`` match."""
    root, limit_key, slot, minutes, reset = unit
    return _KEY_SEPARATOR.join(
        (str(root), str(limit_key), str(slot), str(int(minutes)), str(reset)))


def group_digest(tuples: Iterable[Sequence]) -> str:
    """Digest one loading unit's observation tuples.

    The tuple shape is the whole-root signature's, unchanged, so a group's
    contribution is exactly the rows it owns.
    """
    encoded = json.dumps(
        sorted([list(item) for item in tuples]),
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compose_root_signature(pairs: Iterable[tuple[str, str]]) -> str:
    """Compose a root's physical signature from its per-group digests.

    This is what makes the signature ASSOCIATIVE. The previous value was a
    sha256 over the sorted per-observation tuples of a whole root, which a
    bounded pass physically cannot reproduce — and the measured alternatives
    were already rejected at 0.70s exact and 0.23s inexact against a 500ms
    budget. Digesting the root's sorted ``(group key, group digest)`` pairs
    instead is O(groups): 608 on the real store, against 211K observations.

    The VALUE differs from the old whole-root digest; the SEMANTICS do not. It
    stays an exact-equality function of the physical evidence alone, so
    ``_stats_projection_signatures_match`` and the cache certificate keep meaning
    what they mean today, and a bounded pass and a whole-history pass over the
    same store must produce the same string. The transition is covered by the
    interpretation-version bump, which invalidates every certificate written
    under the old scheme.
    """
    encoded = json.dumps(
        sorted({(str(key), str(digest)) for key, digest in pairs}),
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
