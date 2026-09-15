"""#755 — the statusline drop consensus must terminate.

Every case here asserts **database acceptance**, not kernel publication.
`_cctally_statusline.py:1257` shows that a publication can deliberately leave
the database unchanged, and the production incident of 2026-09-04 is exactly
that outcome: the low readings reached the journal as observations while none
of them produced an accepted snapshot. A test that asserted the kernel had
published would therefore have asserted nothing about what the user sees.

The stall reproduced by the first two cases is the one Section 4 of the spec
describes. `_reduced_candidate` selects the MAXIMUM across active contributors,
so with the database at 63 and the live candidate set holding `{63, 0}` the
reducer selects 63, the equality branch treats reduced-equals-database as
nothing to do, and it clears the pending state before any drop consensus can be
reached. The database stays at 63 for as long as one session keeps reporting
the stale high value.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import types

import pytest

from conftest import load_script, redirect_paths


WEEKLY_RESET_IN = 3 * 86400
#: D5: a non-extendable 180-second wall clock from the first pending drop.
DEADLINE_SECONDS = 180
#: One statusline render every ten seconds is a realistic tick cadence and
#: keeps every candidate inside its 90-second active window.
TICK_SECONDS = 10


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


def _iso(epoch: int) -> str:
    return (
        dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class _Clock:
    """A pinned statusline clock plus the matching capture instant.

    `_statusline_persist` and `_statusline_reduce_and_publish` both read
    `_cctally_statusline.time.time()`, so patching the IMPORTER's reference (per
    #630 S2, never the shared stdlib module object) moves every evaluation tick.
    `CCTALLY_AS_OF` moves `cmd_record_usage`'s capture instant with it, because
    an observation identity is a digest over its capture time and two
    publications inside one wall-clock second are literally one observation.
    """

    def __init__(self, app, monkeypatch, start: int):
        self._app = app
        self._monkeypatch = monkeypatch
        self.value = start
        pinned = types.SimpleNamespace(**vars(app._cctally_statusline.time))
        pinned.time = lambda: self.value
        monkeypatch.setattr(app._cctally_statusline, "time", pinned)
        monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
        self._pin()

    def _pin(self) -> None:
        stamp = dt.datetime.fromtimestamp(self.value, tz=dt.timezone.utc)
        self._monkeypatch.setenv(
            "CCTALLY_AS_OF", stamp.isoformat().replace("+00:00", "Z")
        )

    def advance(self, seconds: int) -> None:
        self.value += seconds
        self._pin()


def _record_ns(*, percent, resets_at, **extra):
    ns = {
        "percent": percent,
        "resets_at": str(int(resets_at)),
        "five_hour_percent": None,
        "five_hour_resets_at": None,
    }
    ns.update(extra)
    return argparse.Namespace(**ns)


def _status_input(app, *, session_id, seven_pct, seven_resets_epoch):
    payload = {
        "session_id": session_id,
        "rate_limits": {
            "seven_day": {
                "used_percentage": seven_pct,
                "resets_at": _iso(seven_resets_epoch),
            }
        },
    }
    return app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())


def _spool(app, *, session_id, percent, reset_epoch):
    """Write one session's candidate without letting it drive an evaluation."""
    app._cctally_statusline._write_candidate(
        app._cctally_statusline._candidate_from_input(
            _status_input(
                app,
                session_id=session_id,
                seven_pct=percent,
                seven_resets_epoch=reset_epoch,
            ),
            received_at=int(app._cctally_statusline.time.time()),
        )
    )


def _weekly_percent(app):
    conn = app.open_db()
    try:
        row = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
            " ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _accepted(app, percent):
    """Did any snapshot row accept `percent`? This is database acceptance."""
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE ABS(weekly_percent - ?) < 1e-9",
            (float(percent),),
        ).fetchone()[0] > 0
    finally:
        conn.close()


def _pending_drop(app, clock, axis="sevenDay"):
    """The persisted pending drop for one axis, as the next tick will read it."""
    control = app._read_control_state(now_epoch=clock.value)
    return None if control is None else control.pending_drops[axis]


def _drive(
    app, clock, *, reporters, reset_epoch, ticks, accept=None, tick_seconds=TICK_SECONDS
):
    """Re-render every reporter and run one evaluation, `ticks` times.

    Returns the elapsed seconds at which `accept` first reached the database, or
    None when it never did. The drive stops at that point, because what happens
    to the database afterwards is decided by the contributors that keep
    reporting and is not what this section is about.
    """
    start = clock.value
    for _ in range(ticks):
        for session_id, percent in reporters:
            _spool(app, session_id=session_id, percent=percent, reset_epoch=reset_epoch)
        app._statusline_reduce_and_publish()
        if accept is not None and _accepted(app, accept):
            return clock.value - start
        clock.advance(tick_seconds)
    return None


def _seed_database(app, clock, *, percent):
    reset_epoch = clock.value + WEEKLY_RESET_IN
    assert app.cmd_record_usage(
        _record_ns(percent=percent, resets_at=reset_epoch, source="api")
    ) == 0
    assert _weekly_percent(app) == percent
    return reset_epoch


def test_a_reset_to_zero_reaches_the_database_while_one_session_reports_the_stale_high(
    app, monkeypatch
):
    """The 2026-09-04 shape: database at 63, live candidates `{63, 0}`.

    The weekly reset is unchanged across the whole run, so the new-window path
    that would publish on a key advance is unavailable and the only way to 0 is
    through drop consensus.
    """
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)

    elapsed = _drive(
        app,
        clock,
        reporters=(("stale", 63), ("reset", 0)),
        reset_epoch=reset_epoch,
        ticks=1 + (DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=0,
    )

    assert elapsed is not None, (
        "the drop consensus never terminated: the database never accepted the "
        "reset after more than twice the 180-second deadline"
    )
    # The stronger form, which is what produced the RED: the LATEST snapshot
    # reads 0, not merely some row somewhere. `_accepted` answers whether the
    # value ever reached the database and is what the drive stops on; this
    # answers what the user's next read returns.
    assert _weekly_percent(app) == 0
    assert DEADLINE_SECONDS <= elapsed <= DEADLINE_SECONDS + 3 * TICK_SECONDS, (
        f"acceptance landed {elapsed}s after the first pending drop; D5 fixes it "
        f"at the first eligible evaluation after {DEADLINE_SECONDS}s"
    )


def test_the_heterogeneous_incident_set_still_reaches_the_database(app, monkeypatch):
    """`{63, 48, 28, 0}` — the set the production journal actually recorded."""
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)

    elapsed = _drive(
        app,
        clock,
        reporters=(("a", 63), ("b", 48), ("c", 28), ("d", 0)),
        reset_epoch=reset_epoch,
        ticks=1 + (DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=0,
    )

    assert elapsed is not None, (
        "the heterogeneous candidate set from the real incident never reached "
        "database acceptance"
    )
    assert DEADLINE_SECONDS <= elapsed <= DEADLINE_SECONDS + 3 * TICK_SECONDS, (
        f"acceptance landed {elapsed}s after the first pending drop"
    )


def test_the_unanimous_fast_path_still_publishes_well_before_the_deadline(app, monkeypatch):
    """Everyone agrees, so the healthy path must not wait out the deadline."""
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)

    elapsed = _drive(
        app,
        clock,
        reporters=(("one", 0), ("two", 0)),
        reset_epoch=reset_epoch,
        ticks=1 + (DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=0,
    )

    assert elapsed is not None and elapsed < DEADLINE_SECONDS, (
        f"unanimous agreement took {elapsed}s to reach the database; it must not "
        f"depend on the {DEADLINE_SECONDS}-second deadline"
    )


def test_an_unsupported_nonzero_drop_stays_suppressed(app, monkeypatch):
    """Publication is not acceptance, and this drop must not be accepted.

    An unarmed weekly report of 63 to 60 satisfies neither the immediate-reset
    nor the zero-debounce criterion (`_lib_record.py:79`), so the recording
    policy rejects it by design. #755 changes the consensus layer, not that
    policy, and the fix for a failure here would be in the consensus layer
    rather than in the recording criteria.
    """
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)
    reporters = (("stale", 63), ("lower", 60))

    assert _drive(
        app, clock, reporters=reporters, reset_epoch=reset_epoch, ticks=1, accept=60
    ) is None
    armed = _pending_drop(app, clock)
    assert armed is not None and armed.retained.percent == 60

    elapsed = _drive(
        app,
        clock,
        reporters=reporters,
        reset_epoch=reset_epoch,
        ticks=(DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=60,
    )

    assert elapsed is None, "an unsupported 3-point weekly drop was accepted"
    assert _weekly_percent(app) == 63

    # The record must also TERMINATE. Its two bounded kernel attempts are spent
    # against an unchanged signature, so nothing it retains can advance it, and
    # keeping it armed rewrites the control document on every tick whose
    # contributor set differs and lets a later signature change re-publish
    # evidence that is by then arbitrarily old. A drop that arms again from live
    # candidates afterwards is a NEW record, which is why this asserts the
    # original one is gone rather than asserting the axis is idle.
    final = _pending_drop(app, clock)
    assert final is None or final.first_seen_at > armed.first_seen_at, (
        "the pending record survived its own spent attempts: it is still the "
        f"one first seen at {armed.first_seen_at}"
    )


def test_a_reset_after_a_spent_attempt_still_reaches_the_database(app, monkeypatch):
    """Expiry arriving after an earlier unsuccessful attempt.

    The unsupported 63-to-60 drop spends its two bounded kernel attempts and
    lands in `suppressed`, where the retry helper refuses an unchanged
    signature. A genuine reset arriving afterwards must still be accepted, which
    is where that refusal meets the new deadline.
    """
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)

    assert _drive(
        app, clock,
        reporters=(("stale", 63), ("lower", 60)),
        reset_epoch=reset_epoch,
        ticks=1 + (DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=60,
    ) is None

    elapsed = _drive(
        app, clock,
        reporters=(("stale", 63), ("lower", 0)),
        reset_epoch=reset_epoch,
        ticks=1 + DEADLINE_SECONDS // TICK_SECONDS,
        accept=0,
    )
    assert elapsed is not None, (
        "the suppressed-signature refusal outlived the drop it was refusing"
    )


def test_a_previous_binary_control_state_is_read_and_reconciled(app, monkeypatch):
    """Task 4.7 — the control document version moved; the spool version did not.

    A schema-version-1 control document carries neither `deadlineAt` nor
    `retained`. It must still parse, so a session running the previous binary
    does not make this one discard the whole document and restart from nothing.

    Its pending drops do not survive the read, and that is the compatibility
    path rather than a gap in it. Neither missing field can be recovered — the
    evidence is not invented, and `firstSeenAt` was stamped under the pre-#755
    arming rule, so it does not mean "when this low was first seen" here. The
    projection is what the document is read for; the drop re-arms from live
    evidence with its own deadline.
    """
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)
    projection = app._read_db_projection_stable()
    document = app._cctally_statusline._control_document(
        app._cctally_statusline._candidates.ControlState(
            projection, {"fiveHour": None, "sevenDay": None}
        )
    )
    document["schemaVersion"] = 1
    document["pendingDrops"]["sevenDay"] = {
        "canonicalKey": projection.seven_day.canonical_key,
        "reducedPercent": 20.0,
        "firstSeenAt": clock.value - 10,
        "kernelStage": "settling",
        "attempts": 0,
        "contributors": {},
        "retrySignature": None,
    }
    app.STATUSLINE_SELECTED_PATH.write_text(json.dumps(document))

    control = app._read_control_state(now_epoch=clock.value)
    assert control is not None, "a previous-binary control document failed to parse"
    assert control.db_projection.seven_day is not None, (
        "the projection the document is read for did not survive the read"
    )
    assert control.pending_drops["sevenDay"] is None, (
        "a version-1 pending drop was carried under the version-2 contract"
    )

    # The consequence: the next evaluation arms from live evidence, with a
    # deadline measured from now rather than one reconstructed from a
    # first-seen instant that meant something else.
    _spool(app, session_id="reset", percent=0, reset_epoch=reset_epoch)
    app._statusline_reduce_and_publish()
    armed = _pending_drop(app, clock)
    assert armed is not None
    assert armed.first_seen_at == clock.value
    assert armed.deadline_at == clock.value + DEADLINE_SECONDS
    assert armed.retained is not None and armed.retained.percent == 0

    # The candidate document version is unchanged, so a spool document written
    # by the previous binary is still read by this one.
    assert app._cctally_statusline._candidates.SCHEMA_VERSION == 1


def test_a_peer_reporting_above_the_baseline_does_not_delay_the_reset(app, monkeypatch):
    """The reset is accepted at the drop's own deadline, not at a later one.

    An upward report is ordinary usage growth and must still publish, but it
    used to discard the pending drop with it. The database then moved up, the
    next tick armed a fresh record, and the reset landed a generation late — or
    never, while a peer kept reporting above whatever the database held.
    """
    clock = _Clock(app, monkeypatch, int(time.time()))
    reset_epoch = _seed_database(app, clock, percent=63)

    # One tick alone, so the rise that follows meets a pending record rather
    # than an empty axis.
    assert _drive(
        app, clock, reporters=(("reset", 0),), reset_epoch=reset_epoch,
        ticks=1, accept=0,
    ) is None
    armed = _pending_drop(app, clock)
    assert armed is not None and armed.retained.percent == 0

    reporters = (("stale", 70), ("reset", 0))
    assert _drive(
        app, clock, reporters=reporters, reset_epoch=reset_epoch, ticks=1, accept=0
    ) is None
    assert _weekly_percent(app) == 70, "the rise itself must still be recorded"
    across = _pending_drop(app, clock)
    assert across is not None, "the upward report discarded the pending reset"
    assert across.first_seen_at == armed.first_seen_at, (
        f"the rise re-armed the drop at {across.first_seen_at} instead of keeping "
        f"the one first seen at {armed.first_seen_at}, so its deadline moved from "
        f"{armed.deadline_at} to {across.deadline_at}"
    )
    assert across.deadline_at == armed.deadline_at
    assert across.retained.percent == 0

    elapsed = _drive(
        app,
        clock,
        reporters=reporters,
        reset_epoch=reset_epoch,
        ticks=1 + (DEADLINE_SECONDS * 2) // TICK_SECONDS,
        accept=0,
    )

    assert elapsed is not None, "the reset never reached the database"
    assert _weekly_percent(app) == 0
    assert armed.deadline_at <= clock.value <= armed.deadline_at + TICK_SECONDS, (
        f"acceptance landed at {clock.value}, and the drop's single deadline is "
        f"{armed.deadline_at}"
    )
