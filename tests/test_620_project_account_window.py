"""#620 S1 D1 / A2 — `project --account <ref>` must bucket that account's
dollars against that account's own subscription boundaries.

`_compute_subscription_weeks` takes an account context precisely so one
account's resets cannot re-anchor another's week walk. `cmd_project` called it
twice with `account_key=None` and resolved `--account` only afterwards, so an
account-filtered read bucketed one account's cost against the merged boundary
set — which, when two accounts reset on different weekdays, is neither
account's.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


# Two accounts whose weeks reset on different weekdays.
#   alice  Thu 09:00Z:  [05-14T09, 05-21T09) 40%   [05-21T09, 05-28T09) 50%
#   bob    Mon 00:00Z:  [05-18T00, 05-25T00) 90%   [05-25T00, 06-01T00) 20%
#
# Merging the two anchor sets and clamping each end to the next start yields
# [05-14T09, 05-18T00), [05-18T00, 05-21T09), [05-21T09, 05-25T00),
# [05-25T00, 06-01T00) — a boundary set neither account has.
_ALICE_WEEKS = [
    (dt.datetime(2026, 5, 14, 9, tzinfo=dt.timezone.utc),
     dt.datetime(2026, 5, 21, 9, tzinfo=dt.timezone.utc), 40.0),
    (dt.datetime(2026, 5, 21, 9, tzinfo=dt.timezone.utc),
     dt.datetime(2026, 5, 28, 9, tzinfo=dt.timezone.utc), 50.0),
]
_BOB_WEEKS = [
    (dt.datetime(2026, 5, 18, 0, tzinfo=dt.timezone.utc),
     dt.datetime(2026, 5, 25, 0, tzinfo=dt.timezone.utc), 90.0),
    (dt.datetime(2026, 5, 25, 0, tzinfo=dt.timezone.utc),
     dt.datetime(2026, 6, 1, 0, tzinfo=dt.timezone.utc), 20.0),
]

# One entry inside each of alice's weeks. The first also lands inside bob's
# first week, which is what makes the merged read visibly wrong.
_ALICE_ENTRIES = [
    ("e1", dt.datetime(2026, 5, 19, 12, tzinfo=dt.timezone.utc)),
    ("e2", dt.datetime(2026, 5, 22, 12, tzinfo=dt.timezone.utc)),
]

AS_OF = "2026-05-26T12:00:00Z"


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    return sys.modules["cctally"]


def _key(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _seed_registry():
    import _cctally_journal as jr
    import _lib_journal as lj
    ka, kb = _key("uuid-A"), _key("uuid-B")
    for kw in (
        dict(at="2026-05-01T00:00:00Z", account_key=ka, provider="claude",
             natural_id="uuid-A", email="alice@x.com", plan_type="max",
             label="alice", label_source="auto"),
        dict(at="2026-05-02T00:00:00Z", account_key=kb, provider="claude",
             natural_id="uuid-B", email="bob@x.com", plan_type="pro",
             label="bob", label_source="auto"),
    ):
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    return ka, kb


def _seed_snapshots(app, weeks, account_key):
    conn = app.open_db()
    try:
        for start, end, pct in weeks:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots("
                "  captured_at_utc, week_start_date, week_end_date, "
                "  week_start_at, week_end_at, weekly_percent, source, "
                "  payload_json, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    (end - dt.timedelta(hours=1)).isoformat().replace(
                        "+00:00", "Z"),
                    start.date().isoformat(),
                    end.date().isoformat(),
                    start.isoformat().replace("+00:00", "Z"),
                    end.isoformat().replace("+00:00", "Z"),
                    pct, "fixture", json.dumps({"fixture": True}), account_key,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_entries(app, account_key):
    conn = app.open_cache_db()
    try:
        for msg_id, ts in _ALICE_ENTRIES:
            path = f"/fake/repos/solo/{msg_id}.jsonl"
            conn.execute(
                "INSERT INTO session_files(path, size_bytes, mtime_ns, "
                " last_byte_offset, last_ingested_at, session_id, "
                " project_path) VALUES (?,?,?,?,?,?,?)",
                (path, 0, 0, 0, "2026-05-26T00:00:00Z", f"sess-{msg_id}",
                 "/fake/repos/solo"),
            )
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, 0, ts.isoformat(), "claude-opus-4-7", msg_id,
                 "r-" + msg_id, 100_000, 20_000, 0, 0, None, account_key),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def two_account_store(app):
    ka, kb = _seed_registry()
    _seed_snapshots(app, _ALICE_WEEKS, ka)
    _seed_snapshots(app, _BOB_WEEKS, kb)
    _seed_entries(app, ka)
    return app, ka, kb


def _run_project_json(app, capsys, *extra):
    rc = app.main(["project", "--weeks", "2", "--json", *extra])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_the_two_accounts_really_have_different_boundaries(two_account_store):
    """Guards the guard: if the merged walk and alice's own walk agreed, the
    test below could not detect anything."""
    app, ka, _kb = two_account_store
    conn = app.open_db()
    try:
        since = dt.datetime(2026, 5, 14, tzinfo=dt.timezone.utc)
        until = dt.datetime(2026, 5, 26, 12, tzinfo=dt.timezone.utc)
        merged = app._compute_subscription_weeks(
            conn, since, until, account_key=None)
        scoped = app._compute_subscription_weeks(
            conn, since, until, account_key=ka)
    finally:
        conn.close()
    merged_starts = [w.start_ts for w in merged]
    scoped_starts = [w.start_ts for w in scoped]
    assert merged_starts != scoped_starts, (
        f"merged {merged_starts} must differ from alice-scoped {scoped_starts}"
    )
    assert any(s.startswith("2026-05-18T00:00") for s in merged_starts), (
        "the merged walk must pick up bob's Monday anchor, or alice's dollars "
        "could not be bucketed against a boundary that is not hers"
    )
    assert not any(s.startswith("2026-05-18T00:00") for s in scoped_starts)


def test_account_filtered_project_uses_its_own_boundaries(
    two_account_store, capsys,
):
    """A2 — `--account alice` buckets alice's cost into alice's weeks.

    Alice's two entries sit one per alice-week, and alice is the only project,
    so each week attributes the whole week to it: 40.0 + 50.0 = 90.0. Under
    the merged boundary set the first entry falls into bob's [05-18T00,
    05-25T00) window instead, which is stamped 90.0, giving 90.0 + 50.0 =
    140.0 — alice's dollars measured against bob's quota.
    """
    app, _ka, _kb = two_account_store
    payload = _run_project_json(app, capsys, "--account", "alice")

    assert payload["weeksInRange"] == 2, (
        "an account-filtered read spans alice's two weeks, not the merged "
        f"boundary set: got {payload['weeksInRange']}"
    )
    assert len(payload["projects"]) == 1
    row = payload["projects"][0]
    assert row["attributedUsedPercent"] == pytest.approx(90.0, abs=1e-6), (
        f"alice's cost must be attributed against alice's own weeks "
        f"(40.0 + 50.0 = 90.0); got {row['attributedUsedPercent']}"
    )
    assert payload["totals"]["usedPercent"] == pytest.approx(90.0, abs=1e-6)


def test_unfiltered_project_output_stays_merged(two_account_store, capsys):
    """The merged read stays merged: `--account` is what narrows the WALK,
    and an invocation without it keeps the all-accounts boundaries.

    The window it spans did change, and deliberately (#620 S1). `--weeks 2`
    now spans exactly two merged intervals — [05-21T09, 05-25T00) and
    [05-25T00, …) — where `cw_start - 7d` reached back to 05-18T00 and
    therefore spanned three. Alice's first entry at 05-19T12 sits in the
    third one, so it is outside a two-week window and no longer contributes;
    it previously did, measured against bob's 90.0. This is the contract
    change the correction announces, not a regression: the old window
    contained a week the user did not ask for.
    """
    app, _ka, _kb = two_account_store
    payload = _run_project_json(app, capsys)
    assert payload["weeksInRange"] == 2, (
        "`--weeks 2` spans two merged intervals; got "
        f"{payload['weeksInRange']}"
    )
    assert payload["rangeStart"] == "2026-05-21", payload["rangeStart"]
    row = payload["projects"][0]
    assert row["attributedUsedPercent"] == pytest.approx(50.0, abs=1e-6), (
        "only the entry inside the two-interval window contributes; got "
        f"{row['attributedUsedPercent']}"
    )
