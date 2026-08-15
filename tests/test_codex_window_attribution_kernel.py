"""The pure resolve/apply kernel for operator Codex window attribution (#500).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``
§6.4, §6.4.1, §7.

The kernel decides two things and only two: which current physical window group
each active assertion owns (RESOLUTION, always against complete evidence), and
what each observation's account becomes as a result (APPLICATION, over whatever
rows the caller asked for). Every SQL read and every write stays in the glue.
"""
from __future__ import annotations

import importlib

import pytest

wa = importlib.import_module("_lib_codex_window_attribution")


WEEKLY = 10_080
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
KEY_WEEKLY = '{"kind":"weekly","windowMinutes":10080}'
KEY_SPARK = '{"kind":"weekly","modelPool":"gpt-5.3-codex-spark","windowMinutes":10080}'


def _assertion(
    op_id="o:1", account_key=ACCOUNT_A, root="root-a",
    limit_key=KEY_WEEKLY, slot="primary", minutes=WEEKLY,
    witnesses=("2026-01-01T09:45:35Z",),
):
    return wa.WindowAssertion(
        op_id=op_id, account_key=account_key, source_root_key=root,
        logical_limit_key=limit_key, observed_slot=slot,
        window_minutes=minutes, raw_resets_at_utc=witnesses,
    )


def _group(
    group_key=("g1",), root="root-a", limit_key=KEY_WEEKLY, slot="primary",
    minutes=WEEKLY, witnesses=("2026-01-01T09:45:35Z", "2026-01-01T09:45:41Z"),
    identified=(), model_scoped=False,
):
    return wa.WindowGroup(
        group_key=group_key, source_root_key=root,
        logical_limit_key=limit_key, observed_slot=slot,
        window_minutes=minutes, raw_resets_at_utc=witnesses,
        identified_accounts=identified, model_scoped=model_scoped,
    )


# ── outcome constants ────────────────────────────────────────────────────────

def test_outcome_constants_are_distinct_and_named():
    outcomes = (
        wa.RESOLVED, wa.DORMANT, wa.SPLIT, wa.SUPPRESSED_NATIVE,
        wa.SUPPRESSED_CONFLICT, wa.SUPPRESSED_MODEL_SCOPED,
    )
    assert len(set(outcomes)) == len(outcomes)
    assert wa.ACCOUNT_WEEKLY_WINDOW_MINUTES == WEEKLY
    assert wa.UNATTRIBUTED_SENTINEL == "unattributed"


# ── resolution ───────────────────────────────────────────────────────────────

def test_clean_unattributed_group_resolves_and_owns_the_group():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion()], [_group()])
    assert [r.outcome for r in resolutions] == [wa.RESOLVED]
    assert resolutions[0].applies is True
    assert resolutions[0].group_key == ("g1",)
    assert ownership == {("g1",): ACCOUNT_A}


def test_a_group_with_a_native_real_account_suppresses_the_assertion():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion()], [_group(identified=(ACCOUNT_B,))])
    assert resolutions[0].outcome == wa.SUPPRESSED_NATIVE
    assert resolutions[0].conflicting_account_keys == frozenset({ACCOUNT_B})
    assert ownership == {}


def test_a_native_account_equal_to_the_asserted_one_still_suppresses():
    """§7: native evidence is authoritative whether it agrees or disagrees; a
    matching native account simply leaves the assertion nothing to do."""
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion()], [_group(identified=(ACCOUNT_A,))])
    assert resolutions[0].outcome == wa.SUPPRESSED_NATIVE
    assert ownership == {}


def test_a_native_population_naming_two_accounts_suppresses():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion()], [_group(identified=(ACCOUNT_A, ACCOUNT_B))])
    assert resolutions[0].outcome == wa.SUPPRESSED_NATIVE
    assert resolutions[0].conflicting_account_keys == frozenset(
        {ACCOUNT_A, ACCOUNT_B})
    assert ownership == {}


def test_two_assertions_naming_different_accounts_both_suppress():
    """§7: conflicting assertions FAIL CLOSED — neither applies. Journal order
    is the wrong tiebreaker, because a stale second assertion would silently
    displace a correct first one with no signal."""
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(op_id="o:1", account_key=ACCOUNT_A),
         _assertion(op_id="o:2", account_key=ACCOUNT_B)],
        [_group()],
    )
    assert [r.outcome for r in resolutions] == [
        wa.SUPPRESSED_CONFLICT, wa.SUPPRESSED_CONFLICT]
    assert all(
        r.conflicting_account_keys == frozenset({ACCOUNT_A, ACCOUNT_B})
        for r in resolutions
    )
    assert ownership == {}


def test_two_assertions_naming_the_same_account_both_apply():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(op_id="o:1"), _assertion(op_id="o:2")], [_group()])
    assert [r.outcome for r in resolutions] == [wa.RESOLVED, wa.RESOLVED]
    assert ownership == {("g1",): ACCOUNT_A}


def test_zero_matching_groups_is_dormant():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(witnesses=("2026-02-02T00:00:00Z",))], [_group()])
    assert resolutions[0].outcome == wa.DORMANT
    assert resolutions[0].group_key is None
    assert resolutions[0].matched_group_count == 0
    assert ownership == {}


def test_more_than_one_matching_group_is_split():
    left = _group(group_key=("g1",), witnesses=("2026-01-01T09:45:35Z",))
    right = _group(group_key=("g2",), witnesses=("2026-01-01T09:45:41Z",))
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(witnesses=(
            "2026-01-01T09:45:35Z", "2026-01-01T09:45:41Z"))],
        [left, right],
    )
    assert resolutions[0].outcome == wa.SPLIT
    assert resolutions[0].matched_group_count == 2
    assert ownership == {}


def test_a_model_scoped_group_suppresses_the_assertion():
    """§6.5/#373: a Spark pool is never filed as account weekly quota, and the
    verdict is re-checked at every evaluation because ``limit_name`` sits
    outside identity equality and can change on re-materialization."""
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion()], [_group(model_scoped=True)])
    assert resolutions[0].outcome == wa.SUPPRESSED_MODEL_SCOPED
    assert ownership == {}


def test_a_non_weekly_group_is_never_in_scope():
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(minutes=300, limit_key='{"kind":"5h"}')],
        [_group(minutes=300, limit_key='{"kind":"5h"}')],
    )
    assert resolutions[0].outcome == wa.SUPPRESSED_MODEL_SCOPED
    assert ownership == {}


def test_axes_must_agree_even_when_a_witness_matches():
    """The binding is the four axes PLUS the witness intersection. A different
    slot at the same reset instant is a different window."""
    resolutions, ownership = wa.resolve_window_attributions(
        [_assertion(slot="primary")], [_group(slot="secondary")])
    assert resolutions[0].outcome == wa.DORMANT
    assert ownership == {}


def test_a_conflict_on_one_group_does_not_suppress_a_clean_second_group():
    clean = _group(group_key=("g2",), slot="secondary")
    resolutions, ownership = wa.resolve_window_attributions(
        [
            _assertion(op_id="o:1", account_key=ACCOUNT_A),
            _assertion(op_id="o:2", account_key=ACCOUNT_B),
            _assertion(op_id="o:3", account_key=ACCOUNT_A, slot="secondary"),
        ],
        [_group(), clean],
    )
    by_id = {r.op_id: r for r in resolutions}
    assert by_id["o:1"].outcome == wa.SUPPRESSED_CONFLICT
    assert by_id["o:2"].outcome == wa.SUPPRESSED_CONFLICT
    assert by_id["o:3"].outcome == wa.RESOLVED
    assert ownership == {("g2",): ACCOUNT_A}


def test_resolution_is_order_independent_for_a_conflict():
    forward, own_forward = wa.resolve_window_attributions(
        [_assertion(op_id="o:1", account_key=ACCOUNT_A),
         _assertion(op_id="o:2", account_key=ACCOUNT_B)], [_group()])
    reverse, own_reverse = wa.resolve_window_attributions(
        [_assertion(op_id="o:2", account_key=ACCOUNT_B),
         _assertion(op_id="o:1", account_key=ACCOUNT_A)], [_group()])
    assert {r.op_id: r.outcome for r in forward} == {
        r.op_id: r.outcome for r in reverse}
    assert own_forward == own_reverse == {}


def test_an_assertion_must_carry_at_least_one_witness():
    with pytest.raises(ValueError):
        _assertion(witnesses=())


# ── application ──────────────────────────────────────────────────────────────

class _Obs:
    """A stand-in observation. The kernel never imports the real type — the
    caller supplies the three accessors, which is what keeps this a leaf."""

    def __init__(self, group_key, account_key):
        self.group_key = group_key
        self.account_key = account_key

    def __eq__(self, other):
        return (isinstance(other, _Obs)
                and (self.group_key, self.account_key)
                == (other.group_key, other.account_key))

    def __repr__(self):  # pragma: no cover - diagnostics only
        return f"_Obs({self.group_key!r}, {self.account_key!r})"


def _apply(ownership, observations):
    return wa.apply_resolution(
        ownership, observations,
        group_key_of=lambda o: o.group_key,
        account_key_of=lambda o: o.account_key,
        with_account=lambda o, account: _Obs(o.group_key, account),
    )


def test_application_stamps_only_the_currently_unattributed_observations():
    observations = [
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
        _Obs(("g1",), ACCOUNT_B),
        _Obs(("g1",), None),
        _Obs(("g1",), ""),
    ]
    result = _apply({("g1",): ACCOUNT_A}, observations)
    assert [o.account_key for o in result] == [
        ACCOUNT_A, ACCOUNT_B, ACCOUNT_A, ACCOUNT_A]


def test_application_leaves_an_unowned_group_untouched():
    observations = [_Obs(("g2",), wa.UNATTRIBUTED_SENTINEL)]
    result = _apply({("g1",): ACCOUNT_A}, observations)
    assert result == tuple(observations)


def test_application_preserves_order_and_returns_a_tuple():
    observations = [
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
        _Obs(("g2",), wa.UNATTRIBUTED_SENTINEL),
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
    ]
    result = _apply({("g1",): ACCOUNT_A}, observations)
    assert isinstance(result, tuple)
    assert [o.group_key for o in result] == [("g1",), ("g2",), ("g1",)]


def test_application_over_a_bounded_subset_reports_the_same_owner():
    """§6.4.1: resolution runs against complete evidence, application against
    whatever rows the caller asked for. A bounded read may show FEWER rows; it
    must never show a DIFFERENT owner."""
    complete = [
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
        _Obs(("g1",), wa.UNATTRIBUTED_SENTINEL),
    ]
    _, ownership = wa.resolve_window_attributions([_assertion()], [_group()])
    full = _apply(ownership, complete)
    bounded = _apply(ownership, complete[:1])
    assert {o.account_key for o in full} == {ACCOUNT_A}
    assert {o.account_key for o in bounded} == {ACCOUNT_A}


def test_application_is_idempotent():
    observations = [_Obs(("g1",), wa.UNATTRIBUTED_SENTINEL)]
    once = _apply({("g1",): ACCOUNT_A}, observations)
    twice = _apply({("g1",): ACCOUNT_A}, once)
    assert once == twice


def test_empty_ownership_is_a_byte_stable_no_op():
    observations = [_Obs(("g1",), wa.UNATTRIBUTED_SENTINEL)]
    assert _apply({}, observations) == tuple(observations)


# ── the leaf contract ────────────────────────────────────────────────────────

def test_module_is_a_pure_leaf():
    """Mirrors ``_lib_codex_account_adoption``'s contract exactly: stdlib only,
    no ``_cctally_*`` import, no I/O.

    Parsed rather than grepped, so prose about the contract in a docstring is
    not mistaken for a violation of it — a text scan reported the module's own
    explanation of the rule as a breach."""
    import ast
    import pathlib
    import sys

    tree = ast.parse(pathlib.Path(wa.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import inside bin/ is not a thing
                raise AssertionError("relative import in a leaf module")
            imported.add(str(node.module).split(".")[0])
    assert not any(name.startswith("_cctally") for name in imported), imported
    # Exactly one sibling leaf may be named, and only this one: it is the home
    # of the two constants a fourth spelling would silently fork, and it imports
    # nothing but the standard library itself.
    assert imported & {"_lib_codex_account_adoption"} == (
        {name for name in imported if name.startswith("_lib_")})
    for name in imported - {"_lib_codex_account_adoption"}:
        assert name in sys.stdlib_module_names, name
    for io_module in ("sqlite3", "os", "pathlib", "socket", "subprocess"):
        assert io_module not in imported, io_module


def test_the_kernel_shares_the_two_constants_rather_than_respelling_them():
    """The three existing spellings are pinned by an equality test in
    ``tests/test_codex_window_attributions_table.py``. A fourth would need a
    fourth arm on that pin; binding the object instead needs none."""
    import _lib_codex_account_adoption as adoption

    assert (wa.ACCOUNT_WEEKLY_WINDOW_MINUTES
            is adoption.ACCOUNT_WEEKLY_WINDOW_MINUTES)
    assert wa.UNATTRIBUTED_SENTINEL is adoption.UNATTRIBUTED_SENTINEL
