"""The pure ledger kernel: expansion, the loading unit, the composable signature.

Public issue omrikais/cctally#5, Task 7. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1-§2.

Everything asserted here is a pure function of its inputs, which is the point:
the interpretation rules have exactly one implementation (the Python read path)
and this module is the small, testable bridge between the RAW coordinates a
SQLite trigger can record and the INTERPRETED identity the read path produces.
"""
from __future__ import annotations

import json

import pytest

import _lib_jsonl
import _lib_quota_ledger as kernel


ROOT = "root-kernel"
SLOT = "primary"
RESET = "2026-07-31T15:00:00Z"
MOVED = "2026-08-01T15:00:00Z"
SPARK = "gpt-5.3-codex-spark"

WEEK_KEY = _lib_jsonl._codex_logical_limit_key(ROOT, "codex", SLOT, 10080)
JITTERED_KEY = _lib_jsonl._codex_logical_limit_key(ROOT, "codex", SLOT, 10081)
FIVE_HOUR_KEY = _lib_jsonl._codex_logical_limit_key(ROOT, "codex", SLOT, 300)
SPARK_KEY = _lib_jsonl._codex_logical_limit_key(
    ROOT, "codex", SLOT, 10080, SPARK)


def _row(op, *, old=None, new=None):
    """One ledger row in the shape ``SELECT *`` returns."""
    row = {"op": op}
    for prefix, side in (("old_", old), ("new_", new)):
        root, limit_key, slot, minutes, reset, anchor = (
            side if side is not None else (None,) * 6)
        row.update({
            prefix + "source_root_key": root,
            prefix + "logical_limit_key": limit_key,
            prefix + "observed_slot": slot,
            prefix + "window_minutes": minutes,
            prefix + "resets_at_utc": reset,
            prefix + "canonical_resets_at_utc": anchor,
        })
    return row


def _side(reset=RESET, anchor=None, *, key=FIVE_HOUR_KEY, minutes=300,
          root=ROOT, slot=SLOT):
    return (root, key, slot, minutes, reset, anchor)


# ── expansion ──────────────────────────────────────────────────────────────

def test_an_insert_contributes_only_its_new_side():
    groups = kernel.expand_dirty_groups([_row("insert", new=_side())])
    assert groups == frozenset({(ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)})


def test_a_delete_contributes_only_its_old_side():
    groups = kernel.expand_dirty_groups([_row("delete", old=_side())])
    assert groups == frozenset({(ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)})


def test_an_update_that_moves_a_row_dirties_both_groups():
    """The re-materialize/sweep pair.

    Without the OLD side, the vacated group keeps a block built from members it
    no longer has — and nothing would ever sweep it, because the pass only
    looks at groups it was told about.
    """
    groups = kernel.expand_dirty_groups([
        _row("update", old=_side(RESET), new=_side(MOVED)),
    ])
    assert groups == frozenset({
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET),
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, MOVED),
    })


def test_an_update_that_moves_nothing_yields_one_group():
    groups = kernel.expand_dirty_groups([
        _row("update", old=_side(RESET), new=_side(RESET)),
    ])
    assert len(groups) == 1


def test_the_anchor_wins_over_the_raw_reset():
    """Every read path resolves the reset as
    ``COALESCE(canonical_resets_at_utc, resets_at_utc)``, so the group
    coordinate has to as well or a jittered row lands in its own group."""
    groups = kernel.expand_dirty_groups([
        _row("insert", new=_side("2026-07-31T15:00:07Z", anchor=RESET)),
    ])
    assert groups == frozenset({(ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)})


def test_a_null_anchor_falls_back_to_the_raw_reset():
    groups = kernel.expand_dirty_groups([
        _row("insert", new=_side(RESET, anchor=None)),
    ])
    assert groups == frozenset({(ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)})


@pytest.mark.parametrize("index", [0, 1, 2, 4])
def test_a_side_missing_a_coordinate_contributes_nothing(index):
    """The loader's required-text guard would drop such a row anyway, so there
    is no window for the projector to expand."""
    side = list(_side())
    side[index] = None
    groups = kernel.expand_dirty_groups([_row("insert", new=tuple(side))])
    assert groups == frozenset()


def test_rows_are_deduplicated():
    rows = [_row("insert", new=_side()) for _ in range(5)]
    assert len(kernel.expand_dirty_groups(rows)) == 1


def test_no_rows_means_no_dirty_groups():
    assert kernel.expand_dirty_groups([]) == frozenset()


# ── the snap closure ───────────────────────────────────────────────────────

def test_the_closure_reaches_the_jittered_spelling_of_a_weekly_window():
    """10081 is the provider's real weekly jitter, and it lives in BOTH the
    limit key and a column. Two raw groups therefore interpret into one window,
    and loading only one of them hands the fold a PARTIAL population — a wrong
    block, not a stale one."""
    widened = kernel.snap_equivalent_raw_groups([
        (ROOT, WEEK_KEY, SLOT, 10080, RESET),
    ])
    assert (ROOT, JITTERED_KEY, SLOT, 10081, RESET) in widened
    assert (ROOT, WEEK_KEY, SLOT, 10080, RESET) in widened


def test_the_closure_is_symmetric_from_the_jittered_side():
    from_native = kernel.snap_equivalent_raw_groups([
        (ROOT, WEEK_KEY, SLOT, 10080, RESET)])
    from_jitter = kernel.snap_equivalent_raw_groups([
        (ROOT, JITTERED_KEY, SLOT, 10081, RESET)])
    assert from_native == from_jitter


def test_a_non_snappable_group_closes_to_exactly_itself():
    group = (ROOT, "hand-written-key", SLOT, 4242, RESET)
    assert kernel.snap_equivalent_raw_groups([group]) == frozenset({group})


def test_the_closure_never_crosses_a_real_window_boundary():
    """300 and 10080 are different windows, not jitter of each other."""
    widened = kernel.snap_equivalent_raw_groups([
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)])
    assert all(minutes in (299, 300, 301) for _, _, _, minutes, _ in widened)


# ── the loading unit ───────────────────────────────────────────────────────

def test_a_raw_group_and_its_interpreted_identity_agree_on_the_unit():
    """The bridge property the reverse map depends on.

    A ledger entry names the unit from RAW coordinates; a block stamps the same
    unit from the INTERPRETED identity it was materialized under. If those ever
    disagree, the sweep looks for a key nothing wrote and a stale block survives.
    """
    from_raw = kernel.loading_unit_from_raw(
        (ROOT, JITTERED_KEY, SLOT, 10081, RESET))
    from_identity = kernel.loading_unit_from_identity(
        source_root_key=ROOT,
        # what the read path produces for that row: snapped key, snapped length
        logical_limit_key=_lib_jsonl.snap_window_minutes(JITTERED_KEY),
        observed_slot=SLOT,
        window_minutes=10080,
        canonical_reset_iso=RESET,
    )
    assert from_raw == from_identity


def test_a_model_scoped_identity_maps_back_to_its_raw_unit():
    """A Spark row is stored under the ORDINARY limit key and only acquires its
    pool at read time, so its block must still stamp the unit the ledger will
    name."""
    from_raw = kernel.loading_unit_from_raw(
        (ROOT, WEEK_KEY, SLOT, 10080, RESET))
    from_identity = kernel.loading_unit_from_identity(
        source_root_key=ROOT, logical_limit_key=SPARK_KEY,
        observed_slot=SLOT, window_minutes=10080, canonical_reset_iso=RESET,
    )
    assert from_raw == from_identity


def test_stripping_the_pool_preserves_every_other_member():
    stripped = kernel.strip_model_pool(SPARK_KEY)
    assert json.loads(stripped) == json.loads(WEEK_KEY)
    assert "modelPool" not in stripped


def test_stripping_is_a_no_op_on_an_ordinary_key():
    assert kernel.strip_model_pool(WEEK_KEY) == WEEK_KEY


def test_stripping_fails_open_on_an_unparseable_key():
    assert kernel.strip_model_pool("not-json") == "not-json"


@pytest.mark.parametrize("spelling", [
    "2026-07-31T15:00:00Z",
    "2026-07-31T15:00:00+00:00",
    "2026-07-31T17:00:00+02:00",
])
def test_every_spelling_of_one_instant_yields_one_unit(spelling):
    """The unit key is TEXT compared with ``=``, so it has no ``unixepoch``
    escape.

    The cache retains whichever spelling the writer used: the ingest path writes
    ``Z``, the anchor resolver writes ``+00:00``. A ledger entry naming one and a
    block stamping the other would be two different units, the scoped sweep
    would look for a key nothing wrote, and a vanished window's block would
    survive — observed exactly once, before this was normalized.
    """
    canonical = kernel.physical_group_key_text(kernel.loading_unit_from_raw(
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, RESET)))
    assert kernel.physical_group_key_text(kernel.loading_unit_from_raw(
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, spelling))) == canonical


def test_an_unparseable_reset_still_compares_equal_to_itself():
    unit = kernel.loading_unit_from_raw((ROOT, FIVE_HOUR_KEY, SLOT, 300, "junk"))
    assert unit == kernel.loading_unit_from_raw(
        (ROOT, FIVE_HOUR_KEY, SLOT, 300, "junk"))


def test_the_serialized_key_separates_its_members_unambiguously():
    a = kernel.physical_group_key_text((ROOT, "a", "b", 1, "c"))
    b = kernel.physical_group_key_text((ROOT, "a\x1fb", "", 1, "c"))
    assert a != b


# ── the composable signature ───────────────────────────────────────────────

def _tuples(*percents):
    return [
        (ROOT, WEEK_KEY, f"2026-07-31T1{i}:00:00+00:00", "/p.jsonl", i, p, RESET)
        for i, p in enumerate(percents)
    ]


def test_a_group_digest_is_order_independent():
    forward = kernel.group_digest(_tuples(1.0, 2.0, 3.0))
    backward = kernel.group_digest(reversed(_tuples(1.0, 2.0, 3.0)))
    assert forward == backward


def test_a_group_digest_changes_when_any_observation_changes():
    assert kernel.group_digest(_tuples(1.0, 2.0)) != kernel.group_digest(
        _tuples(1.0, 2.5))


def test_the_root_signature_is_order_independent():
    pairs = [("g1", "d1"), ("g2", "d2"), ("g3", "d3")]
    assert kernel.compose_root_signature(pairs) == (
        kernel.compose_root_signature(reversed(pairs)))


def test_the_root_signature_changes_when_one_group_changes():
    """The whole point of the composition: a bounded pass recomputes ONE
    group's digest and the root value still moves."""
    before = kernel.compose_root_signature([("g1", "d1"), ("g2", "d2")])
    after = kernel.compose_root_signature([("g1", "d1"), ("g2", "CHANGED")])
    assert before != after


def test_the_root_signature_changes_when_a_group_disappears():
    """A swept-to-nothing group must not leave the root's value unchanged."""
    before = kernel.compose_root_signature([("g1", "d1"), ("g2", "d2")])
    after = kernel.compose_root_signature([("g1", "d1")])
    assert before != after


def test_a_duplicated_pair_does_not_change_the_root_signature():
    """One loading unit can hold several interpreted identities, so several
    blocks carry the SAME (key, digest). The composition has to collapse
    them or an account-scoped variant would move the root's value."""
    once = kernel.compose_root_signature([("g1", "d1")])
    twice = kernel.compose_root_signature([("g1", "d1"), ("g1", "d1")])
    assert once == twice


def test_an_empty_root_has_a_stable_signature():
    assert kernel.compose_root_signature([]) == kernel.compose_root_signature(())


# ── what the digest is taken OVER ──────────────────────────────────────────

def test_the_per_observation_signature_tuple_keeps_its_shape():
    """A silent narrowing of ``_signature_tuple`` fails nothing on its own.

    The digest, the root signature and the certificate all stay internally
    consistent whatever the tuple contains — they would simply stop noticing
    whichever field was dropped. Losing ``used_percent``, for instance, would
    leave every re-observation of a window certifying as unchanged, and the
    reconcile would short-circuit past real drift with a green suite. Pin the
    members so a narrowing has to be deliberate.
    """
    import datetime as dt

    import _cctally_quota as quota
    import _lib_quota

    captured = dt.datetime(2026, 7, 31, 14, 0, tzinfo=dt.timezone.utc)
    resets = dt.datetime(2026, 7, 31, 15, 0, tzinfo=dt.timezone.utc)
    identity = _lib_quota.QuotaWindowIdentity(
        source="codex", source_root_key=ROOT, logical_limit_key=FIVE_HOUR_KEY,
        observed_slot=SLOT, window_minutes=300,
    )
    observation = _lib_quota.QuotaObservation(
        identity=identity, captured_at=captured, used_percent=42.5,
        resets_at=resets, source_path="/codex/root/rollout.jsonl",
        line_offset=17,
    )

    assert quota._signature_tuple(observation) == (
        ROOT, FIVE_HOUR_KEY, "2026-07-31T14:00:00+00:00",
        "/codex/root/rollout.jsonl", 17, 42.5, "2026-07-31T15:00:00+00:00",
    )


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
