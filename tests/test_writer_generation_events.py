"""#825/#832 Task 1.2 — writer metadata vs semantic event comparison (F5).

The writer-generation stamp (`wpg`) is transport metadata. A current emission
whose only difference from a retained pre-stamp event is the stamp must be a
DUPLICATE of it — never a same-revision conflict, and never appended a second
time — and live ingest, rebuild and rederive selection must pick the same
effective events for a journal that mixes pre-stamp and stamped copies of one
semantic event.
"""
import datetime as dt

import _cctally_core  # preserved across load_script()
from conftest import load_script, redirect_paths

SEGMENT = "observations-2026-07.jsonl"


def _siblings():
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal


def _legacy(record):
    """The pre-stamp shape of ``record`` (as the previous release writes it)."""
    import _lib_journal as J
    out = {k: v for k, v in record.items() if k != "wpg"}
    if isinstance(out.get("id"), str) and out["id"].startswith("o:"):
        core = {k: v for k, v in out.items() if k not in ("v", "id")}
        out["id"] = J.content_id(core)
    return out


def _raw_append(records):
    """Write lines exactly as given, bypassing the stamping append APIs."""
    import _lib_journal as J
    jdir = _cctally_core.JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    with open(jdir / SEGMENT, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _journal_records():
    """Every decoded line of every segment, in canonical journal order (live
    emissions land in the wall-clock month's segment)."""
    import _cctally_journal as jr
    import _lib_journal as J
    out = []
    for name in jr.list_segments():
        path = _cctally_core.JOURNAL_DIR / name
        out.extend(J.decode_line(line)
                   for line in path.read_bytes().split(b"\n") if line)
    return out


def _usage_obs(J, pct):
    payload = {
        "kind": "weekly_usage_snapshot",
        "week_start_date": "2026-07-19",
        "week_end_date": "2026-07-26",
        "week_start_at": "2026-07-19T00:00:00+00:00",
        "week_end_at": "2026-07-26T00:00:00+00:00",
        "weekly_percent": pct,
        "source": "statusline",
        "payload_json": "{}",
    }
    return _legacy(J.make_obs(at="2026-07-22T12:00:00Z", src="statusline",
                              provider="claude", payload=payload))


def _snapshot_accept_hook(jr):
    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        p = rec["payload"]
        jr.emit_model_a(
            ctx, kind="snapshot_accept", evt_id="sa:" + rec["id"],
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": rec["at"],
                "week_start_date": p["week_start_date"],
                "week_end_date": p["week_end_date"],
                "week_start_at": p.get("week_start_at"),
                "week_end_at": p.get("week_end_at"),
                "weekly_percent": float(p["weekly_percent"]),
                "source": p.get("source", "statusline"),
                "payload_json": p.get("payload_json", "{}"),
            },
            at=ctx.as_of_for(rec))
    return hook


def _count(table):
    conn = _cctally_core.open_db()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_metadata_only_duplicate_is_classified_duplicate_and_not_appended(
        tmp_path, monkeypatch):
    ns = load_script()
    # Store one: the current pipeline over a legacy observation emits the
    # stamped `snapshot_accept` event this test needs as its reference.
    redirect_paths(ns, monkeypatch, tmp_path / "one")
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))
    obs = _usage_obs(J, 10.0)
    _raw_append([obs])
    jr.run_stats_ingest(mode="authoritative")
    stamped = [r for r in _journal_records() if r.get("t") == "evt"]
    assert len(stamped) == 1 and stamped[0]["wpg"] == "account-bound-v1"
    legacy_evt = _legacy(stamped[0])

    conn = _cctally_core.open_db()
    try:
        assert jr._classify_live_effective_event(conn, legacy_evt) == (
            jr.CLASSIFY_DUPLICATE)
    finally:
        conn.close()

    # Store two: the journal already retains the PRE-STAMP copy of that event
    # (written by the previous release). The current emission differs only by
    # `wpg`: a duplicate, so it is neither a conflict nor a second line.
    redirect_paths(ns, monkeypatch, tmp_path / "two")
    _raw_append([obs, legacy_evt])
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.conflicts_dropped == 0
    evts = [r for r in _journal_records() if r.get("t") == "evt"]
    assert evts == [legacy_evt]
    assert _count("weekly_usage_snapshots") == 1


def test_mixed_pre_stamp_and_stamped_copies_select_identically(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    stamped = J.make_evt(
        kind="budget", id="bm:codex:test", at="2026-07-22T12:00:00Z",
        payload={
            "vendor": "codex", "period_start_at": "2026-07-01T00:00:00Z",
            "period": "monthly", "threshold": 50, "budget_usd": 100.0,
            "spent_usd": 51.0, "consumption_pct": 51.0,
            "crossed_at_utc": "2026-07-22T12:00:00Z",
            "account_key": "*",
        })
    assert stamped["wpg"] == "account-bound-v1"
    legacy = _legacy(stamped)
    _raw_append([legacy, stamped])

    # rederive/rebuild selection (the shared kernel)
    selection = J.resolve_effective_events(_journal_records())
    assert selection.conflicts == ()
    assert [r["id"] for r in selection.active] == ["bm:codex:test"]

    # live ingest
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.conflicts_dropped == 0
    assert _count("budget_milestones") == 1
    live = _cctally_core.open_db()
    try:
        live_meta = live.execute(
            "SELECT event_id, rev, status, content_hash "
            "FROM journal_effective_events").fetchall()
        live_rows = live.execute(
            "SELECT vendor, period_start_at, threshold, journal_id "
            "FROM budget_milestones").fetchall()
    finally:
        live.close()
    assert [tuple(r) for r in live_meta] == [(
        "bm:codex:test", 0, "active", selection.by_id["bm:codex:test"].content_hash)]

    # rebuild
    target = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"),
                           target_path=str(target))
    rb = _cctally_core.open_db(_target_path=str(target))
    try:
        rb_meta = rb.execute(
            "SELECT event_id, rev, status, content_hash "
            "FROM journal_effective_events").fetchall()
        rb_rows = rb.execute(
            "SELECT vendor, period_start_at, threshold, journal_id "
            "FROM budget_milestones").fetchall()
    finally:
        rb.close()
    assert [tuple(r) for r in rb_meta] == [tuple(r) for r in live_meta]
    assert [tuple(r) for r in rb_rows] == [tuple(r) for r in live_rows]
