"""Operator attribution of recorded Codex quota windows (pure kernel).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``

The operator asserts that a physical Codex quota window group belongs to an
account.  This kernel decides, and only decides: which current group each
assertion owns, and what each observation's account becomes as a result.  Every
SQL read and every write stays in the glue layer.

Two phases, and they are not interchangeable (spec §6.4.1).  RESOLUTION decides
which current group an assertion owns and must always run against COMPLETE group
evidence, because witness matching is population-dependent.  APPLICATION maps the
resolved ownership onto whatever rows the caller actually asked for.  A bounded
read may show fewer rows than a full read; it must never show a different owner.

Binding is the four normalized axes plus the tolerance-connected component
witnessed by raw reset values (spec §5.1).  The canonical anchor is NEVER matched
on: a later bridging observation can union two components and retire it.

A pure leaf: stdlib only, no ``_cctally_*`` import, no I/O, no writes — the same
contract ``_lib_codex_account_adoption`` carries, and for the same reason.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

# The ONE leaf import this module makes, and it exists to avoid a fourth
# spelling of two constants that already have three (#500 review round 2,
# finding R2-3).  ``_lib_codex_account_adoption`` imports nothing but the
# standard library, so binding it here introduces no cycle, no I/O and no
# ``_cctally_*`` dependency — the leaf contract is about those, not about
# whether one leaf may name another.  ``_lib_journal`` and
# ``_lib_codex_account_adoption`` must stay respelled with respect to each other
# (the journal module is loaded on paths this kernel is not), which is what
# ``test_the_two_window_constants_have_one_value_across_both_leaves`` pins.
import _lib_codex_account_adoption as _adoption


#: Native length of the account-level Codex weekly quota window, in minutes.
ACCOUNT_WEEKLY_WINDOW_MINUTES = _adoption.ACCOUNT_WEEKLY_WINDOW_MINUTES

#: The reserved "account could not be determined" sentinel
#: (``_lib_accounts.UNATTRIBUTED``).
UNATTRIBUTED_SENTINEL = _adoption.UNATTRIBUTED_SENTINEL

#: Resolution outcomes.  ``RESOLVED`` is the only one that applies anything.
RESOLVED = "resolved"
DORMANT = "dormant"
SPLIT = "split"
SUPPRESSED_NATIVE = "suppressed_native"
SUPPRESSED_CONFLICT = "suppressed_conflict"
#: Covers BOTH out-of-scope shapes the spec's precedence table pairs on one row:
#: a model-scoped pool (#373) and a window that is not account weekly quota.
#: They are one outcome because they have one remedy and one meaning — "this
#: window is not account-level standard quota, so no assertion can file it as
#: such" — and separating them would put two codes on a distinction no operator
#: acts on differently.
SUPPRESSED_MODEL_SCOPED = "suppressed_model_scoped"


@dataclass(frozen=True)
class WindowAssertion:
    """One active operator assertion, as the derived table holds it."""

    op_id: str
    account_key: str
    source_root_key: str
    logical_limit_key: str
    observed_slot: str
    window_minutes: int
    raw_resets_at_utc: "frozenset[str]"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_resets_at_utc", frozenset(self.raw_resets_at_utc))
        if not self.raw_resets_at_utc:
            raise ValueError("an assertion must carry at least one witness")

    @property
    def axes(self) -> tuple:
        return (self.source_root_key, self.logical_limit_key,
                self.observed_slot, self.window_minutes)


@dataclass(frozen=True)
class WindowGroup:
    """One current physical window group, as the loader's complete evidence
    describes it.

    ``identified_accounts`` is the set of non-sentinel accounts natively present.
    ``model_scoped`` is the authoritative ``_lib_codex_pools`` verdict, re-checked
    at every evaluation because ``limit_name`` sits outside identity equality and
    can change on re-materialization (spec §7).
    """

    group_key: tuple
    source_root_key: str
    logical_limit_key: str
    observed_slot: str
    window_minutes: int
    raw_resets_at_utc: "frozenset[str]"
    identified_accounts: "frozenset[str]" = frozenset()
    model_scoped: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_resets_at_utc", frozenset(self.raw_resets_at_utc))
        object.__setattr__(
            self, "identified_accounts", frozenset(self.identified_accounts))

    @property
    def axes(self) -> tuple:
        return (self.source_root_key, self.logical_limit_key,
                self.observed_slot, self.window_minutes)

    @property
    def in_scope(self) -> bool:
        return (self.window_minutes == ACCOUNT_WEEKLY_WINDOW_MINUTES
                and not self.model_scoped)


@dataclass(frozen=True)
class AssertionResolution:
    """What one assertion resolves to against the current population."""

    op_id: str
    account_key: str
    outcome: str
    group_key: "tuple | None" = None
    matched_group_count: int = 0
    conflicting_account_keys: "frozenset[str]" = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "conflicting_account_keys",
            frozenset(self.conflicting_account_keys))

    @property
    def applies(self) -> bool:
        return self.outcome == RESOLVED


def resolve_window_attributions(
    assertions: Iterable[WindowAssertion],
    groups: Iterable[WindowGroup],
) -> "tuple[tuple[AssertionResolution, ...], Mapping[tuple, str]]":
    """Resolve every assertion against COMPLETE current group evidence.

    Returns ``(resolutions, ownership)`` where ``ownership`` maps a group key to
    the account that assertion supplies.  Only ``RESOLVED`` assertions appear in
    ``ownership``.

    Precedence, in order (spec §7):

    1. Zero matching groups is DORMANT — recorded, honest, applying to nothing.
       This is the correct outcome when the underlying rollout evidence has
       evaporated; the alternative would be fabricating a window to attach to.
    2. More than one matching group is SPLIT.  The component has since divided in
       a way the assertion cannot adjudicate, so it applies nothing.
    3. Model-scoped and non-weekly groups are out of scope entirely (#373).
    4. Native evidence always wins.  A group already naming a real account is
       authoritative and is never re-assigned, so the assertion is suppressed
       rather than fighting it.  This holds whether the native account agrees or
       disagrees; a matching one simply has nothing to do.
    5. Two assertions naming DIFFERENT accounts for one group FAIL CLOSED and
       neither applies.  Journal order is the wrong tiebreaker for an operator
       assertion: a stale or mistaken second assertion would silently displace a
       correct first one with no signal.  A visible conflict forces a retraction.

    Order-independent in its verdicts: the conflict pass below runs after every
    assertion has been matched, so two assertions disagreeing about one group
    reach ``SUPPRESSED_CONFLICT`` whichever order they arrive in.
    """
    group_list = tuple(groups)
    resolutions: "list[AssertionResolution]" = []
    claims: "dict[tuple, set[str]]" = {}

    for assertion in assertions:
        candidates = [
            group for group in group_list
            if group.axes == assertion.axes
            and (group.raw_resets_at_utc & assertion.raw_resets_at_utc)
        ]
        if not candidates:
            resolutions.append(AssertionResolution(
                op_id=assertion.op_id, account_key=assertion.account_key,
                outcome=DORMANT))
            continue
        if len(candidates) > 1:
            resolutions.append(AssertionResolution(
                op_id=assertion.op_id, account_key=assertion.account_key,
                outcome=SPLIT, matched_group_count=len(candidates)))
            continue
        group = candidates[0]
        if not group.in_scope:
            resolutions.append(AssertionResolution(
                op_id=assertion.op_id, account_key=assertion.account_key,
                outcome=SUPPRESSED_MODEL_SCOPED, group_key=group.group_key,
                matched_group_count=1))
            continue
        if group.identified_accounts:
            resolutions.append(AssertionResolution(
                op_id=assertion.op_id, account_key=assertion.account_key,
                outcome=SUPPRESSED_NATIVE, group_key=group.group_key,
                matched_group_count=1,
                conflicting_account_keys=group.identified_accounts))
            continue
        claims.setdefault(group.group_key, set()).add(assertion.account_key)
        resolutions.append(AssertionResolution(
            op_id=assertion.op_id, account_key=assertion.account_key,
            outcome=RESOLVED, group_key=group.group_key, matched_group_count=1))

    final: "list[AssertionResolution]" = []
    ownership: "dict[tuple, str]" = {}
    for resolution in resolutions:
        if not resolution.applies:
            final.append(resolution)
            continue
        claimants = claims.get(resolution.group_key, set())
        if len(claimants) > 1:
            final.append(replace(
                resolution, outcome=SUPPRESSED_CONFLICT,
                conflicting_account_keys=frozenset(claimants)))
            continue
        ownership[resolution.group_key] = resolution.account_key
        final.append(resolution)
    return tuple(final), ownership


def apply_resolution(
    ownership: "Mapping[tuple, str]",
    observations: Iterable,
    group_key_of,
    account_key_of,
    with_account,
) -> tuple:
    """Stamp resolved ownership onto currently-unattributed observations.

    Deliberately caller-parameterized on ``group_key_of`` / ``account_key_of`` /
    ``with_account`` so this leaf never imports the observation type.  Runs
    BEFORE the ordinary continuity fold, which then finds nothing left to adopt
    in an attributed group.

    An already-identified observation is never re-stamped, matching
    ``adopt_unidentified_observations``.  ``None`` and the empty string are
    admitted as unattributed alongside the literal sentinel, because the cache
    column is nullable and all three spellings mean the same thing on the read
    path (``entry_is_unattributed`` states the same rule for the spend axis).
    """
    result = []
    for observation in observations:
        account = account_key_of(observation)
        if account and account != UNATTRIBUTED_SENTINEL:
            result.append(observation)
            continue
        owner = ownership.get(group_key_of(observation))
        result.append(with_account(observation, owner) if owner else observation)
    return tuple(result)
