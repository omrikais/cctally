"""Shared trace harness for the #825/#832 outcome-quarantine cases (spec 5.6;
cases F12, F14, F15, F16).

A trace is a list of steps. A CURRENT step goes through the real bound
recording kernel (`record_usage_bound`), which appends a current-stamped
observation and runs the real ingest pipeline. A LEGACY step appends the
previous release's exact bytes (an UNSTAMPED post-boundary Claude
observation) and pauses before its ingest: no legacy ingest ever runs, so the
record is consumed — and excluded — by the next current ingest.

Outcomes are compared by logical identity, payload and multiplicity: the
`week_reset_events` and `five_hour_reset_events` rows with their row ids and
journal ids dropped and EVERY other column kept (fix round O, review
CD4-O02: `detected_at_utc` is accounting payload).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc


def epoch(text: str) -> int:
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def iso(value: int) -> str:
    return dt.datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


def set_clock(monkeypatch, at: str) -> None:
    moment = epoch(at)
    monkeypatch.setattr(time, "time", lambda: float(moment))
    monkeypatch.setenv("CCTALLY_AS_OF", iso(moment))
    pin_journal_clock(monkeypatch, moment)


def pin_journal_clock(monkeypatch, moment: int) -> None:
    """#948: stamp the `at` of journal ops the writers append from the wall
    clock (the cutover, `legacy_spool_activity`) at the pinned instant too.
    The recovery barrier's cohort H is the greatest admitted `at`, ops
    included, so a wall-clock op among pinned readings would move H (and
    every expiry bound) to the real date the test runs on."""
    import _cctally_journal
    monkeypatch.setattr(_cctally_journal, "_now_iso", lambda: iso(moment))


def identity(name: str) -> str:
    import _cctally_core as core
    import _lib_accounts
    if name == "unattributed":
        core.CLAUDE_JSON_PATH.write_text(json.dumps({}))
        core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
        return _lib_accounts.UNATTRIBUTED
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": name, "emailAddress": f"{name}@example.test"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _lib_accounts.account_key("claude", name)


def current(app, monkeypatch, *, at, weekly, week_end, five=None,
            five_end=None, account="A", source="api", planned_key=None,
            credential=None):
    """One bound current reading through `record_usage_bound`. A
    ``credential`` binds it to that selected OAuth credential's evidence, as
    the authoritative OAuth path captures it."""
    import _cctally_statusline as sl
    set_clock(monkeypatch, at)
    identity(account)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(epoch(week_end)),
        five_hour_percent=five,
        five_hour_resets_at=None if five_end is None else str(epoch(five_end)),
        source=source)
    if planned_key is not None:
        args.five_hour_window_key = planned_key
    rc = app.record_usage_bound(
        args, binding=sl._capture_binding(credential=credential),
        nudge_dashboard=False)
    assert rc == 0, rc


def legacy_record(*, at, weekly, week_end, five=None, five_end=None,
                  account=None, captured=None, source="api"):
    """The previous release's unstamped usage observation."""
    import _lib_journal as J
    payload = {
        "weekly_percent": float(weekly),
        "resets_at": epoch(week_end),
        "source": source,
        "captured_at": captured or at,
    }
    if five is not None:
        payload["five_hour_percent"] = float(five)
        payload["five_hour_resets_at"] = dt.datetime.fromtimestamp(
            epoch(five_end), UTC).isoformat(timespec="seconds")
    return J.unstamped_record(J.make_obs(
        at=at, src="record-usage", provider="claude", payload=payload,
        account=account))


def raw_append(records) -> None:
    """Append these exact bytes to the newest segment (a legacy writer
    bypasses the stamp and the cutover chokepoint)."""
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    segments = jr.list_segments()
    segment = segments[-1] if segments else J.segment_name(jr._utc_now())
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.JOURNAL_DIR / segment, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def legacy(app, monkeypatch, *, at, weekly, week_end, five=None, five_end=None,
           account=None, captured=None, source="api"):
    """A frozen-release reading: appended after its real capture, paused
    before its ingest."""
    acct = None
    if account is not None:
        import _lib_accounts
        acct = (_lib_accounts.UNATTRIBUTED if account == "unattributed"
                else _lib_accounts.account_key("claude", account))
    raw_append([legacy_record(at=at, weekly=weekly, week_end=week_end,
                              five=five, five_end=five_end, account=acct,
                              captured=captured, source=source)])


def oauth(app, monkeypatch, *, at, weekly, week_end, five=None, five_end=None,
          account="A", credential=None, axes=None, expect="ok"):
    """One authoritative OAuth publication through
    `_authoritative_record_usage` (#948 spec 6.4: plausibility before fences,
    then the reading, its fences, the selected-state reconciliation and —
    when it newly covers a paused axis — one `outcome_authority_completion`).
    Returns the result; asserts its status is ``expect``."""
    import _cctally_statusline as sl
    set_clock(monkeypatch, at)
    identity(account)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(epoch(week_end)),
        five_hour_percent=five,
        five_hour_resets_at=None if five_end is None else str(epoch(five_end)),
        source="api")
    observed = tuple(axes) if axes is not None else (
        ("sevenDay", "fiveHour") if five is not None else ("sevenDay",))
    result = sl._authoritative_record_usage(
        args, observed,
        binding=sl._capture_binding(credential=credential or f"token-{account}"),
        nudge_dashboard=False)
    assert result.status == expect, result
    return result


def ops_of_kind(kind: str) -> list:
    """Every journal op of payload ``kind``, in journal order (#948)."""
    return [rec for rec in journal_records()
            if rec.get("t") == "op"
            and (rec.get("payload") or {}).get("kind") == kind]


def ingest(app) -> None:
    import _cctally_journal as jr
    jr.run_stats_ingest(mode="authoritative")


def run_steps(app, monkeypatch, steps) -> None:
    for kind, values in steps:
        if kind == "current":
            current(app, monkeypatch, **values)
        elif kind == "legacy":
            legacy(app, monkeypatch, **values)
        elif kind == "oauth":
            oauth(app, monkeypatch, **values)
        else:  # pragma: no cover - a trace typo
            raise ValueError(kind)


def _rows(app, sql):
    conn = app.open_db()
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


#: A credit row's PHYSICAL identity: its rowid, and its journal id (a natural
#: key over columns the row already carries, or a backfill rowid). Every
#: other column is the credit's accounting payload — `detected_at_utc`
#: included, which `_resolve_active_five_hour_reset_event` orders and
#: generates by — so a projection never names columns, it drops these
#: (fix round O, review CD4-O02; spec 5.6 and section 9 M20/F17).
CREDIT_PHYSICAL_COLUMNS = frozenset({"id", "journal_id"})


def credit_payload(row: dict) -> dict:
    """One credit row as its complete accounting payload."""
    return {k: v for k, v in row.items() if k not in CREDIT_PHYSICAL_COLUMNS}


def outcomes_from(conn) -> dict:
    """`outcomes` over an open connection (a retained or snapshot read)."""
    def rows(table):
        cursor = conn.execute(f"SELECT * FROM {table}")
        columns = [d[0] for d in cursor.description]
        return [credit_payload(dict(zip(columns, row)))
                for row in cursor.fetchall()]

    weekly = rows("week_reset_events")
    five = rows("five_hour_reset_events")
    return {
        "in_place": sorted(json.dumps(r, sort_keys=True) for r in weekly
                           if r["observed_pre_credit_pct"] is not None),
        "transitions": sorted(json.dumps(
            [r["account_key"], r["old_week_end_at"], r["new_week_end_at"]])
            for r in weekly if r["observed_pre_credit_pct"] is None),
        "five_hour": sorted(json.dumps(r, sort_keys=True) for r in five),
    }


def outcomes(app) -> dict:
    """The C5 outcome multiset: in-place weekly credits and five-hour credits
    by their complete accounting payload and multiplicity, and moved
    week-end transitions by the approved logical tuple (account, old end,
    new end), whatever observation detected them."""
    conn = app.open_db()
    try:
        return outcomes_from(conn)
    finally:
        conn.close()


def outcome_count(result: dict) -> int:
    return sum(len(v) for v in result.values())


def quarantine_state(app) -> dict:
    """The persisted quarantine tables, row ids included (they are the
    trigger sequence). #948: the per-axis recovery barrier's episodes,
    bounds and axis state replaced the envelope and certificates."""
    return {
        "triggers": _rows(app, "SELECT seq, segment, byte_offset, kind, reason, "
                               "at FROM outcome_quarantine_triggers ORDER BY seq"),
        "episodes": _rows(app, "SELECT * FROM outcome_quarantine_episodes "
                               "ORDER BY opener_segment, opener_offset"),
        "bounds": _rows(app, "SELECT scope, axis, bounds_json FROM "
                             "outcome_quarantine_bounds ORDER BY scope, axis"),
        "axis_state": _rows(app, "SELECT * FROM outcome_quarantine_axis_state "
                                 "ORDER BY generation, account_key, axis"),
    }


def account_key_of(name: str) -> str:
    import _lib_accounts
    return (_lib_accounts.UNATTRIBUTED if name == "unattributed"
            else _lib_accounts.account_key("claude", name))


def account_status(app, name: str) -> dict:
    """`_cctally_quarantine.account_status` for ``name`` (#948: per axis)."""
    import _cctally_quarantine
    conn = app.open_db()
    try:
        return _cctally_quarantine.account_status(conn, account_key_of(name))
    finally:
        conn.close()


def account_status_key(app, account_key: str) -> dict:
    """`account_status` for an exact account key (the oracle's mixed-world
    classification of each new outcome)."""
    import _cctally_quarantine
    conn = app.open_db()
    try:
        return _cctally_quarantine.account_status(conn, account_key)
    finally:
        conn.close()


def axis_allowed(app, name: str) -> dict:
    """``{axis: allowed}`` for ``name``'s drop/outcome gate (#948)."""
    return {axis: doc["allowed"]
            for axis, doc in account_status(app, name)["axes"].items()}


def account_quarantined(app, name: str) -> bool:
    """Whether any axis of ``name`` is paused (#948: per axis)."""
    return not all(axis_allowed(app, name).values())


def quarantine_report(app) -> dict:
    """`_cctally_quarantine.report` over the index (#948)."""
    import _cctally_quarantine
    conn = app.open_db()
    try:
        return _cctally_quarantine.report(conn)
    finally:
        conn.close()


def retain_session_input(app, name: str) -> None:
    """`db rederive` re-derives from retained session input; give the
    account one priced entry."""
    import _lib_accounts
    key = (_lib_accounts.UNATTRIBUTED if name == "unattributed"
           else _lib_accounts.account_key("claude", name))
    cache = app.open_cache_db()
    try:
        source = f"/retained/{name}/session.jsonl"
        cache.execute(
            "INSERT OR IGNORE INTO session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (source, 100, 1, 100, "2026-10-01T00:00:00Z", name,
             f"/retained/{name}"))
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
            " model, input_tokens, output_tokens, cache_create_tokens,"
            " cache_read_tokens, cache_create_1h_tokens, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source, 0, "2026-10-01T00:00:00Z", "claude-3-5-sonnet-20241022",
             100, 10, 0, 0, 0, key))
        cache.commit()
    finally:
        cache.close()


def journal_records() -> list:
    """Every decodable record of the journal, in segment order."""
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    records = []
    for seg in jr.list_segments():
        for line in (core.JOURNAL_DIR / seg).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec is not None:
                records.append(rec)
    return records


def _shifted(text: str, seconds: int) -> str:
    """``text`` moved by ``seconds``, in the same ISO spelling."""
    moved = (dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
             + dt.timedelta(seconds=seconds)).isoformat()
    return moved.replace("+00:00", "Z") if text.endswith("Z") else moved


def shift_credit_detection(app, mutated, category, *, seconds=60) -> bool:
    """A MIXED-ONLY credit mutation for the C5 reference oracles (fix round
    O, review CD4-O02): move the ``detected_at_utc`` of every effective
    credit of ``category`` (``"five_hour"`` or ``"in_place"``) by
    ``seconds``, preserving its identity, amounts, effective instant and
    multiplicity, as a completed correction, then rebuild.

    Only a store whose journal holds an unstamped (excluded) observation is
    mutated, so a reference world sharing the same process is never touched.
    ``mutated`` maps each event id to the instant it was moved to; a later
    call re-applies exactly that instant when a rederive restored the
    original, so ingest, rebuild and rederive all hold the same mutation.
    Returns whether a correction was appended."""
    import _cctally_journal as jr
    import _lib_account_protocol as P
    import _lib_journal as J
    records = journal_records()
    if not any(rec.get("t") == "obs" and not P.is_stamped(rec)
               for rec in records):
        return False
    kind = {"five_hour": "five_hour_credit", "in_place": "week_reset"}[category]
    selection = J.resolve_effective_events(records)
    actions = []
    for event_id, selected in sorted(selection.by_id.items()):
        record = selected.record
        if selected.status != "active" or record is None:
            continue
        payload = record.get("payload") or {}
        if payload.get("kind") != kind or (
                kind == "week_reset"
                and payload.get("observed_pre_credit_pct") is None):
            continue
        target = mutated.setdefault(event_id, _shifted(
            payload["detected_at_utc"], seconds))
        if payload.get("detected_at_utc") == target:
            continue
        changed = dict(payload)
        changed["detected_at_utc"] = target
        actions.append({"action": "replace", "id": event_id,
                        "rev": selected.rev + 1, "at": target,
                        "payload": changed})
    if not actions:
        return False
    jr.append_records(J.make_correction_batch(
        batch_id=f"shift-detection:{actions[0]['id']}:{actions[0]['rev']}",
        family="shift-detection", at=actions[0]["at"], actions=actions))
    rebuild(app)
    return True


REBUILD_ARGV = ("db", "rebuild", "--db", "stats", "--json")
REDERIVE_ARGV = ("db", "rederive", "--family", "claude-usage", "--yes",
                 "--json")


def rebuild(app) -> None:
    args = app.build_parser().parse_args(list(REBUILD_ARGV))
    assert args.func(args) == 0


def rederive(app) -> None:
    args = app.build_parser().parse_args(list(REDERIVE_ARGV))
    assert args.func(args) == 0


def continue_through_maintenance(app, monkeypatch, *, further=()):
    """After live ingest: a real `db rebuild --db stats` plus ``further[0]``
    (one more reading), then a real `db rederive` plus ``further[1]``."""
    result = {"live": outcomes(app), "live_state": quarantine_state(app)}
    rebuild(app)
    result["rebuilt_state"] = quarantine_state(app)
    result["rebuilt"] = outcomes(app)
    if len(further) > 0:
        run_steps(app, monkeypatch, [further[0]])
    result["after_rebuild"] = outcomes(app)
    result["after_rebuild_state"] = quarantine_state(app)
    rederive(app)
    result["rederived_state"] = quarantine_state(app)
    if len(further) > 1:
        run_steps(app, monkeypatch, [further[1]])
    result["after_rederive"] = outcomes(app)
    return result


def run_trace(app, monkeypatch, steps, *, further=()):
    """Run ``steps``, then `continue_through_maintenance`."""
    for name in ("A", "B", "unattributed"):
        retain_session_input(app, name)
    run_steps(app, monkeypatch, steps)
    return continue_through_maintenance(app, monkeypatch, further=further)
