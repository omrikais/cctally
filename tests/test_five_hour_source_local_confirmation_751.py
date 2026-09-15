"""#769 S2 Section 3 — #751(b): source-local confirmation for five-hour credits.

The retained 2026-09-04 incident is reproduced verbatim from the spec's
"Production evidence" section. Seven `source=api` readings at 12% establish the
API baseline; one stale `source=statusline` reading at 7% then fabricated a
`five_hour_credit` in the same instant, with no confirmation from any source and
none from its own. The milestone that followed was keyed on the fabricated
credit's identity, which is how a duplicate threshold-12 milestone appeared in a
new segment.

The rule this module pins: a five-hour credit requires a same-source observed
descent followed by a distinct same-source confirmation that is still below that
source's own pre-drop baseline, inside one physical window. The discriminator is
`payload.source`, NOT the shared `src`, which every observation carries
identically as `record-usage`.

The genuine-credit case (`API 12 -> API 7 -> API 8`) is a first-class assertion
rather than a smoke check: it is what stops the rule overcorrecting into
suppression of real credits.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 4, 6, 0, 0, tzinfo=dt.timezone.utc)

#: The account the incident observations carry.
ACCOUNT = "c719887886403b0a1e3004e967dbd20e"

#: The five-hour window's reset, still in the future for every observation
#: below — the detection branch requires `prior_5h_resets_dt > now_utc`. The
#: value is the one that reproduces the retained incident's window key: the
#: canonical key is the ten-minute floor of this instant's epoch, and the
#: retained `five_hour_credit` evt id names `1788506400`, which is
#: `2026-09-04T07:20:00+00:00`.
FIVE_HOUR_RESETS_AT = "2026-09-04T07:20:00+00:00"

#: The weekly boundary. It never moves across the sequence, so the weekly
#: branch stays inert and only the five-hour branch is under test.
WEEK_RESETS_AT = int(
    dt.datetime(2026, 9, 8, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp()
)


def _siblings():
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal


#: Passed as `source` to omit the key from the payload entirely, which is what
#: a caller that never learned to set it produces.
ABSENT = object()


def _obs(J, *, at, source, five_hour_percent, weekly_percent=63.0):
    """One raw Claude rate-limit observation.

    `src` is `record-usage` for every line, exactly as production emits it —
    the source discriminator lives in `payload.source` alone. `source=ABSENT`
    omits the key.
    """
    payload = {
        "weekly_percent": weekly_percent,
        "resets_at": WEEK_RESETS_AT,
        "captured_at": at,
        "five_hour_percent": five_hour_percent,
        "five_hour_resets_at": FIVE_HOUR_RESETS_AT,
    }
    if source is not ABSENT:
        payload["source"] = source
    return J.make_obs(
        at=at,
        src="record-usage",
        provider="claude",
        payload=payload,
        account=ACCOUNT,
    )


def _ingest(jr, J, sequence):
    """Append `(at, source, five_hour_percent)` triples and fold them.

    One ingest cycle per record, because production folds each status-line tick
    as it arrives and the detection state machine is per-observation.

    A fourth element sets that record's weekly percent. It is needed only when
    a test wants every tick to produce its own snapshot row: the write clamp
    raises a low five-hour reading back to the in-window maximum, and a tick
    whose two percents both then match the latest stored row is a dedup skip
    that writes nothing at all.
    """
    for entry in sequence:
        at, source, pct = entry[:3]
        weekly = entry[3] if len(entry) > 3 else 63.0
        jr.append_record(
            _obs(J, at=at, source=source, five_hour_percent=pct,
                 weekly_percent=weekly),
            now_utc=FIXED,
        )
        jr.run_stats_ingest(mode="authoritative")


def _credit_events(ns):
    conn = ns["open_db"]()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT prior_percent, post_percent, effective_reset_at_utc "
                "  FROM five_hour_reset_events ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _milestones(ns):
    conn = ns["open_db"]()
    try:
        return [
            (int(row["percent_threshold"]), int(row["reset_event_id"]))
            for row in conn.execute(
                "SELECT percent_threshold, reset_event_id "
                "  FROM five_hour_milestones "
                " ORDER BY reset_event_id, percent_threshold"
            )
        ]
    finally:
        conn.close()


#: The retained incident, to the second (spec "The fabricated five-hour credit").
INCIDENT = [
    ("2026-09-04T05:28:09Z", "api", 12.0),
    ("2026-09-04T05:28:39Z", "api", 12.0),
    ("2026-09-04T05:29:09Z", "api", 12.0),
    ("2026-09-04T05:29:39Z", "api", 12.0),
    ("2026-09-04T05:30:09Z", "api", 12.0),
    ("2026-09-04T05:30:39Z", "api", 12.0),
    ("2026-09-04T05:31:09Z", "api", 12.0),
    ("2026-09-04T05:31:27Z", "statusline", 7.0),
    ("2026-09-04T05:31:37Z", "api", 12.0),
    ("2026-09-04T05:32:07Z", "api", 12.0),
    ("2026-09-04T05:32:37Z", "api", 12.0),
    ("2026-09-04T05:33:07Z", "api", 12.0),
    ("2026-09-04T05:33:31Z", "api", 13.0),
    ("2026-09-04T05:33:48Z", "statusline", 12.0),
]


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_the_stale_statusline_sample_fabricates_no_credit(ns):
    """The whole retained sequence must leave the five-hour axis untouched.

    A single statusline reading below an API-established baseline is not
    evidence of a credit: statusline never confirmed its own descent, and the
    API readings that bracket it never descended at all.
    """
    jr, J = _siblings()
    _ingest(jr, J, INCIDENT)

    assert _credit_events(ns) == [], (
        "a five_hour_credit was fabricated from one unconfirmed statusline "
        "sample"
    )


def test_the_stale_statusline_sample_opens_no_new_segment(ns):
    """No credit means no divider, so every milestone stays in segment 0 and
    threshold 12 is recorded exactly once."""
    jr, J = _siblings()
    _ingest(jr, J, INCIDENT)

    rows = _milestones(ns)
    segments = {seg for _, seg in rows}
    assert segments == {0}, f"a credit divider opened a new segment: {rows}"
    twelves = [t for t, _ in rows if t == 12]
    assert len(twelves) == 1, f"threshold 12 was recorded twice: {rows}"


def test_a_genuine_same_source_credit_is_still_detected(ns):
    """`API 12 -> API 7 -> API 8` inside one window still produces the credit.

    This is the overcorrection guard. It holds both before and after the
    source-local rule lands: before, the descent fires on its own; after, the
    third observation confirms it. Either way exactly one event exists and its
    pre-drop baseline is the API baseline of 12.
    """
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:30:09Z", "api", 8.0),
    ])

    events = _credit_events(ns)
    assert len(events) == 1, f"the genuine credit was suppressed: {events}"
    assert events[0]["prior_percent"] == 12.0


def test_replaying_the_arming_observation_cannot_confirm_it(ns):
    """A byte-identical replay of the descent is the same observation.

    Crash replay produces one by construction, and the arming row names the
    observation that armed it precisely so that line cannot answer for a second
    contributor tick. This is the five-hour twin of the weekly debounce's
    `first_zero_observation_id` rule.
    """
    jr, J = _siblings()
    _ingest(jr, J, [("2026-09-04T05:28:09Z", "api", 12.0)])

    descent = _obs(J, at="2026-09-04T05:29:09Z", source="api",
                   five_hour_percent=7.0)
    for _ in range(2):
        jr.append_record(descent, now_utc=FIXED)
        jr.run_stats_ingest(mode="authoritative")

    assert _credit_events(ns) == [], (
        "the arming observation confirmed itself"
    )

    # And the state is still armed, so a genuinely distinct observation from
    # the same source still confirms it.
    _ingest(jr, J, [("2026-09-04T05:30:09Z", "api", 8.0)])
    events = _credit_events(ns)
    assert len(events) == 1, f"the replay left the descent unusable: {events}"
    assert events[0]["prior_percent"] == 12.0


def test_a_different_source_cannot_confirm_a_pending_descent(ns):
    """The statusline reading is below the API baseline and arrives after the
    API descent, so a source-blind rule would take it as the confirmation. It
    is a different contributor, so it confirms nothing and instead establishes
    its own baseline."""
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:29:39Z", "statusline", 6.0),
    ])

    assert _credit_events(ns) == [], (
        "a foreign source confirmed the API descent"
    )

    _ingest(jr, J, [("2026-09-04T05:30:09Z", "api", 8.0)])
    assert len(_credit_events(ns)) == 1, (
        "the pending descent was lost rather than left pending"
    )


def test_a_return_to_the_baseline_cancels_the_pending_descent(ns):
    """A descent the same source walks back is a transient reading, not a
    credit, and the later low value must not confirm the cancelled one."""
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:29:39Z", "api", 12.0),
    ])
    assert _credit_events(ns) == [], "a walked-back descent produced a credit"

    # The next low reading ARMS again; it does not confirm the cancelled one.
    _ingest(jr, J, [("2026-09-04T05:30:09Z", "api", 7.0)])
    assert _credit_events(ns) == [], (
        "the cancelled descent was resurrected by the next low reading"
    )


def test_the_credit_is_stamped_at_the_descent_not_at_its_confirmation(ns):
    """The credit happened when the drop was observed.

    Anchoring on the confirming observation would also leave the arming
    observation's own snapshot behind: the write clamp raised that reading back
    to the pre-credit baseline, and the stale-replica DELETE only reaches rows
    captured at or after `effective_reset_at_utc`.
    """
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:21:09Z", "api", 12.0),
    ])
    # The descent and its confirmation sit in DIFFERENT ten-minute slots.
    _ingest(jr, J, [
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:41:09Z", "api", 8.0),
    ])

    events = _credit_events(ns)
    assert len(events) == 1, events
    assert events[0]["effective_reset_at_utc"] == "2026-09-04T05:20:00+00:00"


# ── Initialization after rebuild: the admitted cold-start gap ──────────────
# The confirmation state is disposable operational state, so a rebuild
# publishes a fresh index carrying none of it. `_read_five_hour_source_state`
# in `bin/_cctally_record.py` states the gap and why it is admitted rather than
# closed. These two tests pin the admitted behaviour so a later change that
# closes it has to change them deliberately, and so the gap cannot quietly
# widen. Neither case fabricates a credit; both miss one.


def _rebuild(jr):
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


def test_a_rebuild_between_baseline_and_descent_misses_the_credit(ns):
    """`API 12 -> rebuild -> API 7 -> API 8`.

    Non-vacuous against `test_a_genuine_same_source_credit_is_still_detected`,
    which drives the identical sequence without the rebuild and gets the
    credit. The rebuild drops the API baseline of 12, so the reading of 7
    establishes a fresh baseline and the descent is never seen.
    """
    jr, J = _siblings()
    _ingest(jr, J, [("2026-09-04T05:28:09Z", "api", 12.0)])
    _rebuild(jr)
    _ingest(jr, J, [
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:30:09Z", "api", 8.0),
    ])

    assert _credit_events(ns) == [], (
        "the cold-start gap is admitted, not closed — if this now detects, "
        "the admission in `_read_five_hour_source_state` is stale"
    )


def test_a_rebuild_between_arming_and_confirmation_misses_the_credit(ns):
    """`API 12 -> API 7 (arms) -> rebuild -> API 8`.

    The pending descent is lost with the rest of the state, and the raw low
    value cannot be recovered from the index: the write clamp stored the
    arming observation at the pre-drop baseline, not at 7.
    """
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
    ])
    _rebuild(jr)
    _ingest(jr, J, [("2026-09-04T05:30:09Z", "api", 8.0)])

    assert _credit_events(ns) == [], (
        "the cold-start gap is admitted, not closed — if this now detects, "
        "the admission in `_read_five_hour_source_state` is stale"
    )


# ── The recorded credit against R-5HC1 ────────────────────────────────────
# `bin/cctally-reconcile-test` declares R-5HC1 over `five_hour_reset_events`,
# but it asserts the predicate against a database it synthesizes and seeds
# itself, so it never reads a row the production writer produced. The two tests
# below drive the writer and then apply R-5HC1's own predicate to its output,
# which is the connection that was missing.

#: R-5HC1, copied from `bin/cctally-reconcile-test`: every event row must carry
#: a drop of at least the five-hour eligibility threshold, and strictly.
_R_5HC1_VIOLATIONS = (
    "SELECT COUNT(*) FROM five_hour_reset_events "
    " WHERE NOT (prior_percent - post_percent >= 5.0 "
    "        AND prior_percent > post_percent)"
)


def _r_5hc1_violations(ns):
    conn = ns["open_db"]()
    try:
        return int(conn.execute(_R_5HC1_VIOLATIONS).fetchone()[0])
    finally:
        conn.close()


def test_the_canonical_genuine_credit_satisfies_r_5hc1(ns):
    """`API 12 -> API 7 -> API 8`, the spec's own genuine-credit sequence.

    The event records the drop this source observed, so `post_percent` is the
    armed low of 7 and not the confirming tick's 8. Recording 8 makes the drop
    4.0pp, which is below the five-hour eligibility threshold that R-5HC1
    enforces, so the spec's own example would write a row violating the
    repository's stated invariant.
    """
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:30:09Z", "api", 8.0),
    ])

    events = _credit_events(ns)
    assert len(events) == 1, f"the genuine credit was suppressed: {events}"
    assert events[0]["prior_percent"] == 12.0
    assert events[0]["post_percent"] == 7.0, (
        "post_percent must be the armed low this source observed, not the "
        "confirming tick's reading"
    )
    assert _r_5hc1_violations(ns) == 0, (
        "the production writer produced a row that violates R-5HC1"
    )


def test_a_confirmation_just_below_the_baseline_still_records_the_low(ns):
    """`API 12 -> API 7 (arms) -> API 11.9 (confirms)`.

    Arming needs the full eligibility threshold; confirmation only needs a
    reading below the pre-drop baseline. Binding the event to the confirming
    tick therefore records a 0.1pp credit for a 5pp drop, which the renderers
    display as the credited amount and which collapses the stale-replica
    DELETE's separation from that threshold.
    """
    jr, J = _siblings()
    _ingest(jr, J, [
        ("2026-09-04T05:28:09Z", "api", 12.0),
        ("2026-09-04T05:29:09Z", "api", 7.0),
        ("2026-09-04T05:30:09Z", "api", 11.9),
    ])

    events = _credit_events(ns)
    assert len(events) == 1, f"the credit was suppressed: {events}"
    assert events[0]["post_percent"] == 7.0, (
        "the confirming tick's reading was recorded as the post-credit level"
    )
    assert _r_5hc1_violations(ns) == 0, (
        "the production writer produced a row that violates R-5HC1"
    )


# ── The stale-replica DELETE's band centre ────────────────────────────────
# The event's `prior_percent` and the DELETE's band centre are two different
# questions. `prior_percent` is what this contributor observed before its own
# drop. The DELETE removes the snapshot rows that hold the five-hour surfaces
# at the pre-credit level, and those rows carry the MAX-clamped percent across
# every contributor, which is at or above this contributor's own baseline.


def _snapshots(ns):
    conn = ns["open_db"]()
    try:
        return [
            (row["captured_at_utc"], float(row["five_hour_percent"]))
            for row in conn.execute(
                "SELECT captured_at_utc, five_hour_percent "
                "  FROM weekly_usage_snapshots "
                " WHERE five_hour_percent IS NOT NULL "
                " ORDER BY captured_at_utc, id"
            )
        ]
    finally:
        conn.close()


#: One contributor peaks above another's baseline inside the same window, and
#: the contributor that peaked never samples the window again. The MAX clamp
#: raises every later accepted row to that peak, so the credited contributor's
#: own baseline is strictly below what the stale rows carry.
_FOREIGN_PEAK = [
    ("2026-09-04T05:28:09Z", "api", 12.0, 63.0),
    ("2026-09-04T05:28:39Z", "statusline", 14.0, 64.0),
    ("2026-09-04T05:31:09Z", "api", 3.0, 65.0),
    ("2026-09-04T05:32:09Z", "api", 4.0, 66.0),
]


def test_the_credit_clears_replicas_carrying_a_foreign_peak(ns):
    """`statusline 14` peaks above the API baseline of 12; `API 12 -> 3 -> 4`
    then credits.

    The arming tick's own reading of 3 is stored at 14, because the clamp
    raises it to the in-window maximum. That row sits at or after the credit's
    effective instant, so it is a stale replica by the DELETE's own definition;
    banding the DELETE on the API baseline of 12 leaves it standing. The
    post-credit clamp then floors at the same effective instant, finds a
    maximum of 14, and raises every post-credit reading back to 14 — the wedge
    the DELETE exists to prevent.
    """
    jr, J = _siblings()
    _ingest(jr, J, _FOREIGN_PEAK)

    events = _credit_events(ns)
    assert len(events) == 1, f"the genuine credit was suppressed: {events}"
    assert events[0]["prior_percent"] == 12.0, (
        "prior_percent is this contributor's own pre-drop baseline"
    )
    assert events[0]["post_percent"] == 3.0

    effective = events[0]["effective_reset_at_utc"]
    survivors = [
        (at, pct) for at, pct in _snapshots(ns)
        if at >= "2026-09-04T05:30" and pct > 12.0
    ]
    assert survivors == [], (
        f"stale replicas at the foreign peak survived the credit at "
        f"{effective}: {survivors}"
    )

    latest = _snapshots(ns)[-1]
    assert latest[1] == 4.0, (
        f"the post-credit reading was clamped back to the pre-credit level: "
        f"{latest}"
    )


# ── One payload source, resolved once ─────────────────────────────────────
# `_pipeline_claude_usage` resolves `payload.source` for the confirmation
# state, and `_usage_snapshot_columns` resolves it again for the snapshot row.
# Nothing joins the two tables on it today, so a disagreement is not yet a
# wrong number — but the confirmation state's `source` is part of a PRIMARY
# KEY, and one of the two resolutions had no `isinstance` guard at all.


def _recorded_sources(ns):
    conn = ns["open_db"]()
    try:
        snapshots = [
            row[0] for row in conn.execute(
                "SELECT source FROM weekly_usage_snapshots ORDER BY id")
        ]
        state = [
            row[0] for row in conn.execute(
                "SELECT source FROM five_hour_credit_confirmation_state")
        ]
        return snapshots, state
    finally:
        conn.close()


def _ingest_one(jr, J, **kwargs):
    jr.append_record(_obs(J, **kwargs), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")


def test_a_payload_with_no_source_is_filed_the_same_way_in_both_places(ns):
    """The production ingest caller supplies its own default for a missing
    key, so `FIVE_HOUR_UNKNOWN_SOURCE` is never what it files."""
    jr, J = _siblings()
    _ingest_one(jr, J, at="2026-09-04T05:28:09Z", source=ABSENT,
                five_hour_percent=12.0)

    snapshots, state = _recorded_sources(ns)
    assert snapshots == ["statusline"]
    assert state == snapshots, (
        f"the snapshot row and the confirmation state disagree: "
        f"{snapshots} vs {state}"
    )


def test_a_non_string_payload_source_is_filed_the_same_way_in_both_places(ns):
    """A present-but-unusable `source` reaches both resolutions.

    The pipeline's `payload.get("source", "statusline")` returns the value
    itself whenever the key exists, so a non-string one passes through
    unguarded and becomes part of the confirmation state's PRIMARY KEY, while
    `_usage_snapshot_columns` rejects it and records `userscript`.
    """
    jr, J = _siblings()
    _ingest_one(jr, J, at="2026-09-04T05:28:09Z", source=None,
                five_hour_percent=12.0)

    snapshots, state = _recorded_sources(ns)
    assert snapshots == ["statusline"]
    assert state == snapshots, (
        f"the snapshot row and the confirmation state disagree: "
        f"{snapshots} vs {state}"
    )
    assert all(isinstance(value, str) for value in state), (
        f"a non-string value reached the confirmation state key: {state}"
    )


# ── The reproduction's identity ───────────────────────────────────────────
#: The five-hour window key the retained incident's `five_hour_credit` evt id
#: carries: `fhc:c719887886403b0a1e3004e967dbd20e:1788506400:2026-09-04T05:30:00
#: +00:00`. The account and the effective instant are reproduced above to the
#: second, so the window key is reproduced too or the claim to reproduce that
#: identity is true of only two of its three components.
RETAINED_WINDOW_KEY = 1788506400


def test_the_reproduction_carries_the_retained_window_key(ns):
    """`FIVE_HOUR_RESETS_AT` is what the canonical window key is derived from,
    so it is what decides whether these observations land in the window the
    incident actually happened in."""
    jr, J = _siblings()
    _ingest(jr, J, INCIDENT[:1])

    conn = ns["open_db"]()
    try:
        keys = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT five_hour_window_key "
                "  FROM weekly_usage_snapshots "
                " WHERE five_hour_window_key IS NOT NULL"
            )
        }
    finally:
        conn.close()
    assert keys == {RETAINED_WINDOW_KEY}
