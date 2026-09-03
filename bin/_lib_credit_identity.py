"""Durable identity for one Anthropic weekly credit (#703 + #707).

An Anthropic weekly credit is a counter discontinuity inside an unchanged
window, and its one durable representation is a row in ``week_reset_events``.
Before this module that row was identified by the pair of week boundaries,
which is why several credits in one week could not be represented: the live
detector suppressed any later same-window event, and the row constraint had
nowhere to put a second one.

Identity therefore moves OFF the boundaries and onto ``credit_key``, derived
here from the journal record that caused the credit. Five source kinds reach
this module:

  ``immediate``  the triggering observation's ``snapshot_accept`` logical
                 identity, for an automatic credit that fires on the tick.
  ``debounced``  the first-zero observation identity retained in the
                 reset-to-zero marker, for a confirmed reset-to-zero.
  ``backfill``   the triggering snapshot's ``journal_id``, or its pre-cutover
                 ``b:weekly_usage_snapshots:<rowid>`` bootstrap identity.
  ``manual``     the ``weekly_credit_floor`` op identifier, for
                 ``record-credit``.
  ``legacy``     the enclosing event or op id of a row that predates the field.

The four live kinds keep the identity verbatim, because these are already
content-stable journal identities (``_lib_journal.content_id`` /
``_lib_journal.bootstrap_id``), so the key is reproducible from the journal
rather than from projection state. The ``legacy`` kind is prefixed, because a
row folded from a pre-change line has no source record of its own and must not
be able to collide with the live identity of one.

``credit_order`` is that SAME source record's own journal INSTANT, in Unix epoch
seconds — see ``credit_order_from_instant`` for why it is the instant rather
than the absolute sequence position the spec's first draft named. It is
deliberately not the fold position of the derived row. Fold orders express
dependency rather than occurrence: after unification a manual credit folds at op
order 5 while an automatic one folds at event order 30, and a rebuild sorts by
fold family before sequence, so a fold-derived order can reverse the real
chronology of two credits and make the epoch resolver choose a different epoch
than the writer did (spec section 5.3).

Pure: no database access, no file I/O, no ``_cctally_*`` import. The whole
point of deriving identity here is that replay reproduces it from the journal
alone.

Spec: docs/superpowers/specs/2026-09-02-703-707-anthropic-same-window-credit.md
sections 3.1, 4 and 5.3.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass


#: Every source kind this module accepts. ``legacy`` is the only prefixed one.
CREDIT_SOURCE_KINDS = frozenset({
    "immediate", "debounced", "backfill", "manual", "legacy",
})

#: The prefix a legacy row's key carries, so a row folded from a pre-change
#: line can never collide with the live identity of a real source record.
LEGACY_KEY_PREFIX = "legacy:"


@dataclass(frozen=True)
class CreditSource:
    """The journal record that caused one credit.

    ``kind`` is one of ``CREDIT_SOURCE_KINDS``; ``identity`` is that record's
    journal identity; ``order`` is its canonical journal sequence position.
    """

    kind: str
    identity: str
    order: int


def _validate(source: CreditSource) -> None:
    """Reject an unknown kind or an empty identity rather than defaulting.

    A silent default reintroduces exactly the defect this module removes: two
    credits sharing one key stop being two credits. An empty identity is not an
    identity either — it collides with every other empty one.
    """
    if source.kind not in CREDIT_SOURCE_KINDS:
        raise ValueError(
            f"unknown credit source kind {source.kind!r}; "
            f"expected one of {sorted(CREDIT_SOURCE_KINDS)}"
        )
    if not isinstance(source.identity, str) or not source.identity:
        raise ValueError(
            f"credit source identity must be a non-empty string, "
            f"got {source.identity!r}"
        )


def derive_credit_key(source: CreditSource) -> str:
    """The row's durable identity, from the source record and never the
    boundaries."""
    _validate(source)
    if source.kind == "legacy":
        return f"{LEGACY_KEY_PREFIX}{source.identity}"
    return source.identity


def derive_credit_order(source: CreditSource) -> int:
    """The source record's own journal instant, in Unix epoch seconds.

    Every live caller builds ``CreditSource.order`` with
    ``credit_order_from_instant``, whose docstring states why the value is the
    instant rather than the absolute journal sequence position, and names the
    two orderings it cannot distinguish.
    """
    _validate(source)
    return int(source.order)


def credit_order_from_instant(instant: str) -> int:
    """The integer ``credit_order`` for a source record at ``instant``.

    ``instant`` is the source record's own journal instant — the observation's
    ``at`` for an automatic credit, the retained first-zero instant for a
    debounced one, the snapshot's ``captured_at_utc`` for a backfilled one, the
    op's ``at`` for a manual one — as an ISO-8601 string. The return value is
    that instant as a Unix epoch second.

    Why an instant rather than a row number, stated plainly because the spec
    asks for "the canonical journal sequence position of the source record".
    That absolute numbering exists (``_lib_selector_state``), but it is not
    reachable from the write site: the incremental selection that derives it
    returns None on many ordinary ticks — absent selector state, a version
    mismatch, a durable prefix ahead of the cursor, an oversized gap — and a
    durable identity column cannot be populated by something that is sometimes
    unavailable. The source record's own instant satisfies every property the
    spec requires of the value: it is a fact OF THE SOURCE RECORD, so a rebuild
    reproduces it byte-identically; it is an integer; and it orders by
    occurrence rather than by fold family, which is the reversal §5.3 exists to
    prevent.

    Two orderings it cannot distinguish, both recorded in spec §5.3 rather than
    left implicit. Two source instants inside one SECOND tie, and the resolver
    then falls through to ``credit_key DESC`` — a content hash, so the tiebreak
    is deterministic but not chronological. And under a BACKWARD CLOCK STEP the
    instant order and the append order disagree, so a credit appended later can
    sort earlier. Neither loss is avoided by the absolute sequence position,
    which is simply unavailable at the write site.

    Raises ``ValueError`` on an unparseable instant rather than defaulting to
    zero, because a zero would sort every affected credit to the bottom.
    """
    if not isinstance(instant, str) or not instant:
        raise ValueError(f"credit order instant must be a non-empty string, "
                         f"got {instant!r}")
    text = instant.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp())
