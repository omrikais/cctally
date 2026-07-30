"""Codex window-scoped spend adoption (pure kernel).

Spec: ``docs/superpowers/specs/2026-07-30-codex-window-scoped-spend-adoption.md``

``adopt_unidentified_observations`` (``bin/_lib_quota.py``) applies the #341 §2
window-account continuity rule to the OBSERVATION axis: inside one physical
quota window, unidentified observations are adopted by the window's account iff
exactly one identified account is ever observed for that window key.  This
kernel applies the same inference, with the same grouping key and the same
guard, to the SPEND axis — ``codex_session_entries.account_key``.

A pure leaf module: stdlib only, no cctally imports.  The caller supplies window
descriptors (already grouped on ``_lib_quota._physical_window_key`` and already
folded, so ``identified_accounts`` is the window's post-fold identified set) and
candidate entries; this kernel decides, and only decides.  Every SQL read and
every write stays in the glue layer.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable


#: Native length of the account-level Codex weekly quota window, in minutes.
ACCOUNT_WEEKLY_WINDOW_MINUTES = 10_080


#: The reserved "account could not be determined" sentinel
#: (``_lib_accounts.UNATTRIBUTED``), spelled here so this leaf stays import-free.
UNATTRIBUTED_SENTINEL = "unattributed"


def entry_is_unattributed(account_key: object) -> bool:
    """Whether a ``codex_session_entries`` row is still up for adoption.

    ``NULL`` is the stamp every never-decided row carries (#416 spec D1:
    ``stably_absent`` -> ``NULL``), and the empty string is its degenerate
    spelling.  The literal ``unattributed`` sentinel is admitted too: no producer
    writes it to this column today, but ``_codex_cache_account_predicate`` counts
    it in the ``unattributed`` BUCKET, so excluding it here would make such a row
    permanently unadoptable — visible as nobody's money and ineligible for the
    only mechanism that could give it an owner.
    """
    return (
        account_key is None
        or account_key == ""
        or account_key == UNATTRIBUTED_SENTINEL
    )


@dataclass(frozen=True)
class SpendAdoptionWindow:
    """One physical Codex quota window, as the fold left it.

    ``canonical_resets_at`` is the tolerance-anchored reset (#416 §4.1), never a
    raw jittered provider value — the same anchor
    ``_lib_quota._physical_window_key`` groups on.  ``identified_accounts`` is
    the set of non-``unattributed`` account keys observed for that key.
    """

    source_root_key: str
    window_minutes: int
    canonical_resets_at: dt.datetime
    identified_accounts: frozenset[str] = frozenset()
    model_scoped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source_root_key, str) or not self.source_root_key:
            raise ValueError("source_root_key must be a non-empty string")
        if (not isinstance(self.window_minutes, int)
                or isinstance(self.window_minutes, bool)
                or self.window_minutes <= 0):
            raise ValueError("window_minutes must be a positive integer")
        reset = self.canonical_resets_at
        if reset.tzinfo is None or reset.utcoffset() is None:
            raise ValueError("canonical_resets_at must be timezone-aware")
        object.__setattr__(
            self, "identified_accounts", frozenset(self.identified_accounts))

    @property
    def nominal_start_at(self) -> dt.datetime:
        return self.canonical_resets_at - dt.timedelta(
            minutes=self.window_minutes)

    @property
    def in_scope(self) -> bool:
        """Account-level weekly windows only.

        A 5h window nests inside the weekly one and adds no evidence; a
        model-scoped pool such as GPT-5.3-Codex-Spark is never account weekly
        quota (#373).  Both are excluded from candidacy entirely, so neither
        stamps nor blocks.
        """
        return (
            self.window_minutes == ACCOUNT_WEEKLY_WINDOW_MINUTES
            and not self.model_scoped
        )

    def covers(self, timestamp: dt.datetime) -> bool:
        """Whether ``timestamp`` falls in the NOMINAL ``[start, reset)`` range.

        Nominal rather than first-observation: spend before the window's first
        retained observation is still spend inside the cycle.
        """
        return self.nominal_start_at <= timestamp < self.canonical_resets_at


@dataclass(frozen=True)
class SpendAdoptionCandidate:
    """One ``codex_session_entries`` row offered to the pass."""

    entry_id: int
    source_root_key: str
    timestamp: dt.datetime
    account_key: "str | None" = None


@dataclass(frozen=True)
class SpendAdoptionStamp:
    """One decided write: give ``entry_id`` this account."""

    entry_id: int
    account_key: str


def build_spend_adoption_plan(
    windows: Iterable[SpendAdoptionWindow],
    candidates: Iterable[SpendAdoptionCandidate],
) -> tuple[SpendAdoptionStamp, ...]:
    """Return the stamping plan, ordered by ``entry_id``.

    An in-scope window CLAIMS every candidate its nominal range covers on its own
    root.  A candidate is stamped iff the UNION of identified accounts across
    every claiming window is exactly one.

    A claiming window that identifies no account contributes nothing to that
    union and therefore does NOT block: absence of evidence is not evidence of
    ambiguity.  That is the same shape ``adopt_unidentified_observations`` uses on
    the observation axis, which likewise resolves a window from its *identified*
    observations only and treats an unidentified population as no evidence.  The
    first implementation blocked on any claiming window that resolved to nothing
    and measured ZERO stamped rows on a real store: weekly resets move by days,
    so one cycle overlaps many neighbours and pre-attribution history is
    unidentified by construction — one such neighbour was always enough to veto.

    Two claiming windows naming DIFFERENT accounts still leave the entry alone
    (union of two), and so does a single window that itself saw two accounts
    (#341 never-combine).  An already-identified row is never re-stamped, so
    re-running over this kernel's own output returns an empty plan.
    """
    by_root: dict[str, list[SpendAdoptionWindow]] = {}
    for window in windows:
        if window.in_scope:
            by_root.setdefault(window.source_root_key, []).append(window)

    stamps: list[SpendAdoptionStamp] = []
    for candidate in candidates:
        if not entry_is_unattributed(candidate.account_key):
            continue
        identified: set[str] = set()
        for window in by_root.get(candidate.source_root_key, ()):
            if window.covers(candidate.timestamp):
                identified |= window.identified_accounts
                if len(identified) > 1:
                    break
        if len(identified) != 1:
            continue
        account = next(iter(identified))
        stamps.append(SpendAdoptionStamp(
            entry_id=candidate.entry_id, account_key=account))
    stamps.sort(key=lambda stamp: (stamp.entry_id, stamp.account_key))
    return tuple(stamps)
