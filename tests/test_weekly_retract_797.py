"""#797 T7 — the reviewed `retract` disposition (spec section 4.7, Q5, D4;
section 6 T7; acceptance A7).

A fake-reset storm is journalled through the real bound recording kernel
(`record_usage_bound`, the direct recipe the discriminator does not cover,
R2): a genuine redeem L0 after a 74, then an idle session replaying its
cached 74 (A1, A2, A3) before each fresh low (L1, L2, L3), so each low mints
a `week_reset` whose pre-credit value is the stale 74 and each replay seeds a
milestone at 74 in the epoch it lands in. A schema-3 reviewed manifest then
retracts the replays through the real `db rederive` command (preview, then
apply with the previewed plan hashes).

`retract` is counterfactual weekly exclusion: weekly detection is suppressed
for a retracted observation during the reviewed replay exactly as for
`hold`, but it is not in hold's dependency-preservation set, so outcomes are
retained, superseded or tombstoned by the corrected replay. `hold` keeps its
#858 semantics, and on the same storm still keeps a fake reset alive through
the milestone that cites the held snapshot — the control that tells the two
dispositions apart.

On the parent every schema-3 manifest is refused ("requires schemaVersion 1
or 2"), which is the RED reason of the retraction cases.
"""
from __future__ import annotations

import collections
import json

import pytest

import _outcome_quarantine_trace as T

WEEK_END = "2026-10-24T05:00:00Z"
FIVE_END = "2026-10-22T12:00:00Z"
DAY = "2026-10-22T"

#: The storm (source, capture, weekly, five-hour) and each step's name.
STORM = (
    ("P70", "api", "08:00:00", 70, 10),
    ("P74", "api", "09:00:00", 74, 12),
    ("L0", "api", "09:31:00", 0, 13),          # the genuine redeem
    ("A1", "statusline", "09:32:00", 74, 13),  # stale replay
    ("L1", "api", "09:33:00", 0, 14),          # fake reset R1
    ("A2", "statusline", "09:34:00", 74, 15),  # stale replay, five-hour rise
    ("L2", "api", "09:35:00", 1, 15),          # fake reset R2
    ("A3", "statusline", "09:36:00", 74, 15),  # stale replay
    ("L3", "api", "09:37:00", 1, 16),          # fake reset R3
    ("G", "api", "09:40:00", 3, 17),           # genuine growth
)


# --- the store -----------------------------------------------------------------


def _app(monkeypatch, tmp_path):
    app = T.make_app(monkeypatch, tmp_path)
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    return app


def _record(app, monkeypatch, steps) -> dict:
    """Record ``steps`` through the real bound kernel; their observation ids
    by step name."""
    ids = {}
    for name, source, at, weekly, five in steps:
        before = {r["id"] for r in T.journal_records() if r.get("t") == "obs"}
        T.current(app, monkeypatch, at=DAY + at + "Z", weekly=weekly,
                  week_end=WEEK_END, five=five, five_end=FIVE_END,
                  source=source)
        [new] = [r["id"] for r in T.journal_records()
                 if r.get("t") == "obs" and r["id"] not in before]
        ids[name] = new
    return ids


def _storm(app, monkeypatch) -> dict:
    return _record(app, monkeypatch, STORM)


def _active() -> dict:
    """Every effective active event: id -> payload."""
    import _lib_journal as J
    selection = J.resolve_effective_events(T.journal_records())
    return {event_id: dict(selected.record.get("payload") or {})
            for event_id, selected in selection.by_id.items()
            if selected.status == "active" and selected.record is not None}


def _of(kind, active=None) -> dict:
    active = _active() if active is None else active
    return {k: v for k, v in active.items() if v.get("kind") == kind}


def _resets(active=None) -> list:
    return sorted(p["origin_observation_id"]
                  for p in _of("week_reset", active).values())


def _wr(ids, name) -> str:
    return f"wr:{T.identity('A')}:origin:{ids[name]}"


def _milestones(active=None) -> dict:
    """Weekly milestones: id -> (threshold, reset ref, cited snapshot)."""
    return {k: (p["percent_threshold"], p["reset_event_ref"],
                p["usage_snapshot_ref"])
            for k, p in _of("percent_milestone", active).items()}


def _five_hour_milestones(active=None) -> dict:
    return {k: (p["percent_threshold"], p["usage_snapshot_ref"])
            for k, p in _of("five_hour_milestone", active).items()}


def _stats(app, sql, params=()):
    conn = app.open_db()
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _logical(app) -> dict:
    """The stats index's outcome state as multisets (Counter), for live =
    rebuild = rederive comparisons."""
    return {
        "resets": collections.Counter(_stats(
            app, "SELECT account_key, origin_observation_id, "
                 "observed_pre_credit_pct, effective_reset_at_utc "
                 "FROM week_reset_events")),
        "milestones": collections.Counter(_stats(
            app, "SELECT account_key, week_start_date, percent_threshold, "
                 "captured_at_utc FROM percent_milestones")),
        "five_hour_milestones": collections.Counter(_stats(
            app, "SELECT account_key, five_hour_window_key, percent_threshold, "
                 "captured_at_utc FROM five_hour_milestones")),
        "snapshots": collections.Counter(_stats(
            app, "SELECT captured_at_utc, weekly_percent, "
                 "weekly_observation_held, five_hour_percent "
                 "FROM weekly_usage_snapshots")),
    }


# --- the command -----------------------------------------------------------------


def _manifest(tmp_path, decisions, *, version=3, name="review.json",
              identity=(), reason="Reviewed stale status-line replays"):
    import _cctally_journal as jr
    hw = jr.journal_high_water()
    body = {
        "schemaVersion": version,
        "journalHighWater": {"segment": hw[0], "offset": hw[1]},
        "journalPrefixHash": jr.journal_prefix_hash(hw),
        "reviewedAt": "2026-10-23T12:00:00Z",
        "reason": reason,
        "weeklyAxisDecisions": [
            {"observationId": obs, "disposition": disposition}
            for obs, disposition in decisions],
        "snapshotIdentityDecisions": list(identity),
    }
    path = tmp_path / name
    path.write_text(json.dumps(body))
    return path


def _rederive(app, capsys, path=None, *, yes=False):
    argv = ["db", "rederive", "--family", "claude-usage", "--json"]
    if path is not None:
        argv += ["--reviewed-weekly-decisions", str(path)]
    if yes:
        argv.append("--yes")
    capsys.readouterr()
    args = app.build_parser().parse_args(argv)
    rc = args.func(args)
    out = capsys.readouterr().out.strip().splitlines()
    return rc, json.loads(out[-1])


def _apply(app, capsys, path):
    """Preview, pin the two plan hashes, apply; the preview and the applied
    payload."""
    rc, preview = _rederive(app, capsys, path)
    assert rc == 0 and preview["status"] == "preview", preview
    body = json.loads(path.read_text())
    body.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(body))
    rc, applied = _rederive(app, capsys, path, yes=True)
    assert rc == 0 and applied["status"] == "applied", applied
    return preview, applied


def _no_actions(payload) -> bool:
    return all(payload["actionCounts"][k] == 0
               for k in ("supersede", "tombstone", "add"))


# --- manifest schema 3 and payload schema 3 --------------------------------------


def test_manifest_v3_decodes_retract_into_the_hold_projection(
        monkeypatch, tmp_path):
    """One strict decoder: a retraction persists as an ordinary `hold` in
    `weekly_axis_decisions` plus the canonical `weekly_axis_retractions`
    list, under the existing op kind at payload schema 3."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids["A3"], "retract"), (ids["A1"], "retract"),
                                (ids["G"], "accept"), (ids["P70"], "hold")])
    op = R._reviewed_weekly_op_from_manifest(path)
    payload = op["payload"]
    assert payload["kind"] == "claude_weekly_observation_decision_v2"
    assert payload["schema_version"] == 3
    assert payload["weekly_axis_decisions"] == sorted([
        {"observationId": ids["A1"], "disposition": "hold"},
        {"observationId": ids["A3"], "disposition": "hold"},
        {"observationId": ids["G"], "disposition": "accept"},
        {"observationId": ids["P70"], "disposition": "hold"},
    ], key=lambda item: item["observationId"])
    assert payload["weekly_axis_retractions"] == sorted([ids["A1"], ids["A3"]])


@pytest.mark.parametrize("mutation, message", [
    ("v2-retract", "malformed"),
    ("unknown-key", "schemaVersion"),
    ("duplicate", "duplicate"),
    ("bad-disposition", "malformed"),
])
def test_manifest_v3_strict_decode_refuses(monkeypatch, tmp_path, mutation,
                                           message):
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R
    import _lib_rederive as L
    ids = _record(app, monkeypatch, STORM[:4])
    decisions = [(ids["A1"], "retract")]
    version = 3
    if mutation == "v2-retract":
        version = 2
    elif mutation == "duplicate":
        decisions = [(ids["A1"], "retract"), (ids["A1"], "hold")]
    elif mutation == "bad-disposition":
        decisions = [(ids["A1"], "retracted")]
    path = _manifest(tmp_path, decisions, version=version)
    if mutation == "unknown-key":
        body = json.loads(path.read_text())
        body["weeklyAxisRetractions"] = [ids["A1"]]
        path.write_text(json.dumps(body))
    with pytest.raises(L.RederiveConflict, match=message):
        R._reviewed_weekly_op_from_manifest(path)


def _v3_payload(ids, **over):
    payload = {
        "kind": "claude_weekly_observation_decision_v2",
        "schema_version": 3,
        "journal_high_water": {"segment": "observations-2026-10.jsonl",
                               "offset": 1},
        "journal_prefix_hash": "sha256:" + "0" * 64,
        "reason": "r",
        "weekly_axis_decisions": [
            {"observationId": ids[0], "disposition": "hold"},
            {"observationId": ids[1], "disposition": "hold"}],
        "weekly_axis_retractions": [ids[0]],
        "snapshot_identity_decisions": [],
    }
    payload.update(over)
    return payload


@pytest.mark.parametrize("over", [
    # a retraction that references no same-op projected hold
    {"weekly_axis_retractions": ["o:00000000000000ff"]},
    # a retraction whose projection is an accept
    {"weekly_axis_decisions": [
        {"observationId": "o:0000000000000001", "disposition": "accept"}],
     "weekly_axis_retractions": ["o:0000000000000001"]},
    # non-canonical lists
    {"weekly_axis_retractions": ["o:0000000000000002", "o:0000000000000001"]},
    {"weekly_axis_retractions": ["o:0000000000000001", "o:0000000000000001"]},
    # a literal `retract` never appears in the projection
    {"weekly_axis_decisions": [
        {"observationId": "o:0000000000000001", "disposition": "retract"}],
     "weekly_axis_retractions": []},
    # schema 3 requires the list
    {"weekly_axis_retractions": None},
])
def test_payload_v3_strict_decode_refuses(over):
    import _lib_rederive as L
    ids = ("o:0000000000000001", "o:0000000000000002")
    payload = _v3_payload(ids, **over)
    if payload["weekly_axis_retractions"] is None:
        del payload["weekly_axis_retractions"]
    with pytest.raises(L.RederiveConflict):
        L.decode_reviewed_weekly_payload(payload)


def test_payload_decode_and_latest_decision_wins():
    import _lib_rederive as L
    a, b = "o:0000000000000001", "o:0000000000000002"
    decoded = L.decode_reviewed_weekly_payload(_v3_payload((a, b)))
    assert decoded.dispositions == {a: "retract", b: "hold"}
    v2 = dict(_v3_payload((a, b)), schema_version=2)
    del v2["weekly_axis_retractions"]
    assert L.decode_reviewed_weekly_payload(v2).dispositions == {
        a: "hold", b: "hold"}
    # hold then retract -> retract; retract then accept -> accept; retract
    # then hold -> hold (the latest logical decision per observation wins).
    later = _v3_payload((b, a), weekly_axis_retractions=[b])
    accept = dict(v2, weekly_axis_decisions=[
        {"observationId": a, "disposition": "accept"}])
    assert L.effective_reviewed_weekly_dispositions(
        [_v3_payload((a, b)), later]) == {a: "hold", b: "retract"}
    assert L.effective_reviewed_weekly_dispositions(
        [_v3_payload((a, b)), accept]) == {a: "accept", b: "hold"}
    assert L.effective_reviewed_weekly_dispositions(
        [v2, _v3_payload((a, b))]) == {a: "retract", b: "hold"}


def test_retract_enters_the_op_identity_and_the_plan_fingerprints(
        monkeypatch, tmp_path, capsys):
    """The same observations held (schema 2) and retracted (schema 3): the
    ops differ in identity and the plans in hash; the preview counts the
    retractions separately."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    stale = [ids["A1"], ids["A2"], ids["A3"]]
    hold = _manifest(tmp_path, [(o, "hold") for o in stale], version=2,
                     name="hold.json")
    retract = _manifest(tmp_path, [(o, "retract") for o in stale],
                        name="retract.json")
    mixed = _manifest(tmp_path, [(ids["A1"], "retract"), (ids["A2"], "hold"),
                                 (ids["G"], "accept")], name="mixed.json")
    held_op = R._reviewed_weekly_op_from_manifest(hold)
    retract_op = R._reviewed_weekly_op_from_manifest(retract)
    assert held_op["id"] != retract_op["id"]
    _rc, held = _rederive(app, capsys, hold)
    _rc, retracted = _rederive(app, capsys, retract)
    _rc, both = _rederive(app, capsys, mixed)
    assert held["decisionPlanHash"] != retracted["decisionPlanHash"]
    assert held["reviewedWeeklyDecisionCounts"] == {
        "hold": 3, "accept": 0, "retract": 0}
    assert retracted["reviewedWeeklyDecisionCounts"] == {
        "hold": 0, "accept": 0, "retract": 3}
    assert both["reviewedWeeklyDecisionCounts"] == {
        "hold": 1, "accept": 1, "retract": 1}
    # Retract and hold suppress the same weekly detection, so the plans
    # differ only through what hold preserves: the fingerprint carries the
    # retractions even where the actions agree.
    import _cctally_journal as jr
    records = [*R.read_rederive_journal_prefix()[0]]
    with R._open_cache_read_view() as cache:
        held_plan = R.plan_claude_usage(
            [*records, held_op], cache_conn=cache,
            journal_high_water=jr.journal_high_water(), enforce_guard=False)
        retract_plan = R.plan_claude_usage(
            [*records, retract_op], cache_conn=cache,
            journal_high_water=jr.journal_high_water(), enforce_guard=False)
    assert held_plan.config_fingerprint != retract_plan.config_fingerprint


# --- the storm: hold versus retract ----------------------------------------------


def test_hold_keeps_its_858_semantics_on_the_storm(monkeypatch, tmp_path, capsys):
    """Preservation control (passes on the parent): an ordinary `hold` of the
    replays keeps the milestones citing the held snapshots together with
    their `reset_event_ref`, so the fake reset R1 those milestones name stays
    effective; only the resets no held milestone names are tombstoned."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    assert _resets() == sorted([ids["L0"], ids["L1"], ids["L2"], ids["L3"]])
    path = _manifest(tmp_path, [(ids[n], "hold") for n in ("A1", "A2", "A3")],
                     version=2)
    _apply(app, capsys, path)
    milestones = _milestones()
    # Each replay's milestone survives with its reset.
    assert {(t, ref, snap) for t, ref, snap in milestones.values()
            if snap in {f"sa:{ids[n]}" for n in ("A1", "A2", "A3")}} == {
        (74, _wr(ids, "L0"), f"sa:{ids['A1']}"),
        (74, _wr(ids, "L1"), f"sa:{ids['A2']}"),
        (74, _wr(ids, "L2"), f"sa:{ids['A3']}"),
    }
    assert _resets() == sorted([ids["L0"], ids["L1"], ids["L2"]])


def _expected_after_retract(ids) -> dict:
    sa = {n: f"sa:{ids[n]}" for n in ids}
    return {
        "resets": [ids["L0"]],
        # The genuine week's crossings stay; the replays' milestones go, and
        # the corrected replay's own crossings of the genuine epoch replace
        # them.
        "milestone_cites": sorted({snap for _t, _r, snap in
                                   _milestones_expected(ids).values()}),
        "held": {sa["A1"]: 1, sa["A2"]: 1, sa["A3"]: 1},
    }


def _milestones_expected(ids) -> dict:
    account = T.identity("A")
    week = "2026-10-17"
    genuine = {
        f"pm:{account}:{week}:0:{p}": (
            p, "0", f"sa:{ids['P70' if p == 70 else 'P74']}")
        for p in (70, 71, 72, 73, 74)}
    epoch = _wr(ids, "L0")
    genuine.update({
        f"pm:{account}:{week}:{epoch}:1": (1, epoch, f"sa:{ids['L2']}"),
        f"pm:{account}:{week}:{epoch}:2": (2, epoch, f"sa:{ids['G']}"),
        f"pm:{account}:{week}:{epoch}:3": (3, epoch, f"sa:{ids['G']}"),
    })
    return genuine


def test_retract_removes_exactly_the_unsupported_graph(
        monkeypatch, tmp_path, capsys):
    """RED on the parent (schema 3 refused). Retracting the three replays
    tombstones the three fake resets and every milestone of the replays and
    of the fake epochs, keeps the genuine reset with its identity and
    payload, and keeps every five-hour crossing (including A2's own). A2's
    snapshot stays as a weekly-held carrier of its own five-hour rise; A1 and
    A3 carried no five-hour evidence of their own (their readings equal the
    preceding low's), so the corrected replay stores nothing for them."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    before = _active()
    genuine = _of("week_reset", before)[_wr(ids, "L0")]
    five_before = _five_hour_milestones(before)
    assert any(snap == f"sa:{ids['A2']}" for _t, snap in five_before.values())
    path = _manifest(tmp_path, [(ids[n], "retract") for n in ("A1", "A2", "A3")])
    preview, applied = _apply(app, capsys, path)
    assert preview["reviewedWeeklyDecisionCounts"]["retract"] == 3
    after = _active()
    assert _resets(after) == [ids["L0"]]
    assert _of("week_reset", after)[_wr(ids, "L0")] == genuine
    assert _milestones(after) == _milestones_expected(ids)
    assert set(_five_hour_milestones(after)) == set(five_before)
    snapshots = _of("snapshot_accept", after)
    assert snapshots[f"sa:{ids['A2']}"]["weekly_observation_held"] == 1
    assert snapshots[f"sa:{ids['A2']}"]["five_hour_percent"] == 15.0
    assert f"sa:{ids['A1']}" not in snapshots
    assert f"sa:{ids['A3']}" not in snapshots
    rows = _stats(app, "SELECT origin_observation_id FROM week_reset_events")
    assert rows == [(ids["L0"],)]
    held = _stats(app, "SELECT captured_at_utc, weekly_observation_held, "
                       "five_hour_percent FROM weekly_usage_snapshots "
                       "WHERE source = 'statusline' ORDER BY captured_at_utc")
    assert held == [("2026-10-22T09:34:00Z", 1, 15.0)]


def test_retract_closes_a_dependent_milestone_without_cancelling_the_tombstone(
        monkeypatch, tmp_path, capsys):
    """RED on the parent. Retract A1 and hold A2 and A3 in one manifest. R1's
    predecessor is the retracted A1, so the corrected replay no longer
    supports R1 — yet the milestone at 74 citing the HELD A2 names R1, and
    hold's preservation would keep R1 alive to keep that milestone. The
    retraction wins: R1 is tombstoned and that milestone with it. R2, whose
    predecessor is the held A2, keeps hold's #858 semantics: the held A3's
    milestone still keeps it."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids["A1"], "retract"), (ids["A2"], "hold"),
                                (ids["A3"], "hold")])
    _apply(app, capsys, path)
    assert _resets() == sorted([ids["L0"], ids["L2"]])
    cited = {snap: (t, ref) for t, ref, snap in _milestones().values()}
    assert f"sa:{ids['A1']}" not in cited
    assert f"sa:{ids['A2']}" not in cited
    assert cited[f"sa:{ids['A3']}"] == (74, _wr(ids, "L2"))
    references = {ref for _t, ref, _s in _milestones().values()}
    assert _wr(ids, "L1") not in references and _wr(ids, "L3") not in references


def test_retract_durably_converges_without_the_manifest(
        monkeypatch, tmp_path, capsys):
    """apply -> rebuild -> plain rederive -> rebuild -> plain rederive: zero
    remaining correction actions, no fake identity resurrected, live =
    rebuilt = rederived as multisets, without resupplying the manifest."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    fakes = {_wr(ids, n) for n in ("L1", "L2", "L3")}
    path = _manifest(tmp_path, [(ids[n], "retract") for n in ("A1", "A2", "A3")])
    _apply(app, capsys, path)
    live = _logical(app)
    assert sum(live["resets"].values()) == 1
    states = []
    for _round in range(2):
        T.rebuild(app)
        states.append(_logical(app))
        rc, plain = _rederive(app, capsys, yes=True)
        assert rc == 0 and plain["status"] == "no-op", plain
        assert _no_actions(plain), plain["actionCounts"]
        states.append(_logical(app))
        assert not fakes & set(_active())
    for state in states:
        assert state == live
    assert _milestones() == _milestones_expected(ids)


def test_interrupted_retract_application_recovers_to_the_same_state(
        monkeypatch, tmp_path, capsys):
    """A crash after the durable decision op and before its batch: a plain
    rederive (no manifest) completes the decision's batch — the previewed
    batch id — and reaches the uninterrupted final state."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids[n], "retract") for n in ("A1", "A2", "A3")])
    rc, preview = _rederive(app, capsys, path)
    assert rc == 0
    body = json.loads(path.read_text())
    body.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(body))

    def crash(stage):
        if stage == "after-reviewed-decision-op":
            raise RuntimeError("simulated crash after durable op")

    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", crash)
    op = R._reviewed_weekly_op_from_manifest(path)
    with pytest.raises(RuntimeError, match="durable op"):
        R.apply_db_rederive(
            "claude-usage", reviewed_op=op,
            expected_plan_hashes=(preview["baselinePlanHash"],
                                  preview["decisionPlanHash"]))
    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", None)
    assert not any(r.get("t") == "correction_batch" for r in T.journal_records()
                   if r.get("family") == "claude-usage")
    rc, recovered = _rederive(app, capsys, yes=True)
    # The durable op is the decision; the plain rederive appends its batch.
    assert rc == 0 and recovered["status"] == "applied", (
        recovered["status"], recovered["batchId"], preview["batchId"],
        recovered["actionCounts"])
    assert recovered["batchId"] == preview["batchId"]
    assert _resets() == [ids["L0"]]
    assert _milestones() == _milestones_expected(ids)
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain


def test_alternate_support_keeps_the_reset_identity_and_genuine_milestones(
        monkeypatch, tmp_path, capsys):
    """Review F4. Baseline 0, stale status-line A74, an independently
    retained fresh API B74 that stores no new snapshot, the genuine redeem
    L0; retract A. The corrected replay stores B74, so L0's reset keeps its
    identity (it follows the originating low) and its pre-credit 74, and the
    genuine crossings survive under their ids, now citing B."""
    app = _app(monkeypatch, tmp_path)
    ids = _record(app, monkeypatch, (
        ("Z", "api", "08:00:00", 0, 10),
        ("A", "statusline", "08:10:00", 74, 11),
        ("B", "api", "08:20:00", 74, 11),
        ("L", "api", "08:30:00", 0, 12),
    ))
    before = _active()
    snapshots = _of("snapshot_accept", before)
    assert f"sa:{ids['B']}" not in snapshots     # B stored no snapshot
    reset = _wr(ids, "L")
    assert _resets(before) == [ids["L"]]
    pre = _of("week_reset", before)[reset]
    assert pre["observed_pre_credit_pct"] == 74.0
    genuine = {k: t for k, (t, _r, snap) in _milestones(before).items()
               if snap == f"sa:{ids['A']}"}
    assert genuine and 74 in genuine.values()
    path = _manifest(tmp_path, [(ids["A"], "retract")])
    _apply(app, capsys, path)
    after = _active()
    assert _resets(after) == [ids["L"]]
    assert _of("week_reset", after)[reset]["observed_pre_credit_pct"] == 74.0
    milestones = _milestones(after)
    for key, threshold in genuine.items():
        assert milestones[key][0] == threshold
        assert milestones[key][2] == f"sa:{ids['B']}", key
    assert _of("snapshot_accept", after)[f"sa:{ids['A']}"][
        "weekly_observation_held"] == 1
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain


# --- the burst guard --------------------------------------------------------------


def test_retract_tombstones_never_count_as_reset_additions(
        monkeypatch, tmp_path, capsys):
    """Three tombstoned resets of one (account, week end, pre-credit) key —
    the storm's — pass the unchanged guard: tombstones are not additions."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids[n], "retract") for n in ("A1", "A2", "A3")])
    preview, _applied = _apply(app, capsys, path)
    assert preview["planGuard"] is None
    assert preview["actionCountsByEventKind"]["week_reset"]["tombstone"] == 3
    assert preview["actionCountsByEventKind"]["week_reset"]["add"] == 0


def test_burst_guard_still_refuses_an_unrelated_unsafe_addition(
        monkeypatch, tmp_path, capsys):
    """A schema-3 manifest that retracts one observation and accepts three
    lows whose resets share one key still refuses (exit 2,
    `week-reset-add-burst`) before anything is appended."""
    import _cctally_journal as jr
    import _lib_journal as J
    app = _app(monkeypatch, tmp_path)
    ids = _record(app, monkeypatch, (("P", "api", "06:00:00", 4, 8),
                                     ("Z", "api", "07:00:00", 5, 9)))
    account = T.identity("A")
    raw = []
    for minute, weekly in enumerate((60.0, 10.0, 60.0, 10.0, 60.0, 10.0)):
        captured = f"{DAY}08:{minute:02d}:00Z"
        raw.append(J.make_obs(
            at=captured, src="record-usage", provider="claude",
            account=account, payload={
                "captured_at": captured, "source": "api",
                "weekly_percent": weekly, "resets_at": T.epoch(WEEK_END)}))
    jr.append_records(raw)
    lows = [r["id"] for r in raw if r["payload"]["weekly_percent"] == 10.0]
    path = _manifest(tmp_path, [(ids["Z"], "retract"),
                                *[(o, "accept") for o in lows]])
    before = [r for r in T.journal_records()]
    rc, payload = _rederive(app, capsys, path)
    assert rc == 2, payload
    assert payload["planGuard"] is not None, payload["conflicts"]
    assert payload["planGuard"]["code"] == "week-reset-add-burst"
    assert payload["planGuard"]["violations"][0]["addCount"] == 3
    assert T.journal_records() == before


# --- mutation controls ----------------------------------------------------------


def test_mutation_retract_preserved_like_hold_keeps_the_fake_resets(
        monkeypatch, tmp_path, capsys):
    """Non-vacuity: with retractions fed back into hold's preservation (the
    two dispositions collapsed), the same retract manifest keeps the fake
    resets R1 and R2 — what the retraction test above refuses."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R
    monkeypatch.setattr(R, "_reviewed_weekly_retractions",
                        lambda records: frozenset())
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids[n], "retract") for n in ("A1", "A2", "A3")])
    _apply(app, capsys, path)
    assert _resets() == sorted([ids["L0"], ids["L1"], ids["L2"]])


def test_mutation_without_the_unsupported_reset_rule_keeps_r1(
        monkeypatch, tmp_path, capsys):
    """Non-vacuity: without the rule that a held milestone never restores a
    reset a retraction left unsupported, the mixed manifest keeps R1."""
    app = _app(monkeypatch, tmp_path)
    import _lib_rederive as L
    monkeypatch.setattr(L, "_retraction_unsupported_resets",
                        lambda current, desired, retracted, **_kw: frozenset())
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids["A1"], "retract"), (ids["A2"], "hold"),
                                (ids["A3"], "hold")])
    _apply(app, capsys, path)
    assert ids["L1"] in _resets()


# --- the operator's correction flow: an earlier hold, then the retraction ------

#: The storm with two more stale replays, each repeating the replay before
#: it: neither is accepted as a snapshot event of its own, so a corrected
#: replay that drops the stored replay could accept one in its place.
HELD_STORM = (
    STORM[:4]
    + (("A1x", "statusline", "09:32:30", 74, 13),)
    + STORM[4:8]
    + (("A3x", "statusline", "09:36:30", 74, 15),)
    + STORM[8:]
)


def test_hold_then_retract_converges_on_the_genuine_reset(
        monkeypatch, tmp_path, capsys):
    """Review RV-6: the operator's week already carries a reviewed `hold`
    of the stored replays, which keeps a fake reset alive (#858). A later
    schema-3 manifest retracts them, together with the stale replays that
    were never accepted as rows. Every fake reset is tombstoned and the
    genuine one survives with its identity; the plain rederive afterwards
    is a no-op and a rebuild changes nothing. (Every replay is retracted, so
    hold's preservation no longer applies; the next test is the one that
    needs the retraction rule.)"""
    app = _app(monkeypatch, tmp_path)
    ids = _record(app, monkeypatch, HELD_STORM)
    snapshots = _of("snapshot_accept")
    assert f"sa:{ids['A1x']}" not in snapshots
    assert f"sa:{ids['A3x']}" not in snapshots
    genuine = _of("week_reset")[_wr(ids, "L0")]
    stored = ("A1", "A2", "A3")
    hold = _manifest(tmp_path, [(ids[n], "hold") for n in stored], version=2,
                     name="hold.json")
    _apply(app, capsys, hold)
    survivors = set(_resets()) - {ids["L0"]}
    assert survivors, "the hold must keep a fake reset alive (#858)"
    retract = _manifest(tmp_path, [(ids[n], "retract")
                                   for n in ("A1", "A1x", "A2", "A3", "A3x")],
                        name="retract.json")
    preview, _applied = _apply(app, capsys, retract)
    assert preview["reviewedWeeklyDecisionCounts"]["retract"] == 5
    after = _active()
    assert _resets(after) == [ids["L0"]]
    assert _of("week_reset", after)[_wr(ids, "L0")] == genuine
    fakes = {_wr(ids, n) for n in ("L1", "L2", "L3")}
    assert not fakes & set(after)
    assert not {ref for _t, ref, _s in _milestones(after).values()} & fakes
    live = _logical(app)
    assert sum(live["resets"].values()) == 1
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    assert _no_actions(plain), plain["actionCounts"]
    assert _logical(app) == live
    T.rebuild(app)
    assert _logical(app) == live
    assert _resets() == [ids["L0"]]


def test_hold_then_mixed_retract_attributes_through_an_absent_predecessor(
        monkeypatch, tmp_path, capsys):
    """Review RV-6, the predecessor-ordering case of
    `_retraction_unsupported_resets`. After the earlier hold, a manifest
    retracts A1 and A1x and keeps A2 and A3 held. R1's detection-time
    predecessor is A1x, which has no snapshot event in the current graph,
    and the held A2's milestone names R1, so hold's preservation would keep
    it. The rule reads the predecessor from the raw observation stream, so
    the retracted A1x is R1's predecessor although it has no snapshot: R1 is
    attributed to the retraction and tombstoned with that milestone; R2
    keeps hold's #858 semantics."""
    app = _app(monkeypatch, tmp_path)
    ids = _record(app, monkeypatch, HELD_STORM)
    assert f"sa:{ids['A1x']}" not in _of("snapshot_accept")
    hold = _manifest(tmp_path, [(ids[n], "hold") for n in ("A1", "A2", "A3")],
                     version=2, name="hold.json")
    _apply(app, capsys, hold)
    assert ids["L1"] in _resets()
    mixed = _manifest(tmp_path, [(ids["A1"], "retract"), (ids["A1x"], "retract"),
                                 (ids["A2"], "hold"), (ids["A3"], "hold")],
                      name="mixed.json")
    _apply(app, capsys, mixed)
    resets = _resets()
    assert ids["L0"] in resets and ids["L2"] in resets
    assert ids["L1"] not in resets
    references = {ref for _t, ref, _s in _milestones().values()}
    assert _wr(ids, "L1") not in references
    live = _logical(app)
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    T.rebuild(app)
    assert _logical(app) == live


# --- the operator's store shape: fakes an earlier hold left in place ------------

#: Every stale replay of HELD_STORM, stored or not.
REPLAYS = ("A1", "A1x", "A2", "A3", "A3x")


def _earlier_binary_hold(current, desired, *_args, **_kwargs):
    """The 2026-10-22 store's history (op o:415b): an earlier binary applied
    the reviewed `hold` of the stale replays without #858's dependency
    preservation, so the replays' snapshots became held (or were not
    accepted at all) while the fake resets stayed in place. The current
    binary's baseline replay no longer supports those resets, so they sit in
    the store as baseline drift."""
    for event_id, record in current.items():
        if (record.get("payload") or {}).get("kind") == "week_reset":
            desired.setdefault(event_id, record)


def _store_after_an_earlier_hold(app, monkeypatch, tmp_path, capsys) -> dict:
    """HELD_STORM, every replay held by an earlier binary's reviewed `hold`:
    each fake reset's raw predecessor (A1x, A2, A3x) is a held replay that is
    a held snapshot or no snapshot event at all in the current graph, so the
    latest UNHELD snapshot before each fake's origin is a genuine,
    unretracted reading (L0, L1, L2). The plain baseline tombstones the three
    fakes (drift)."""
    import _lib_rederive as L
    ids = _record(app, monkeypatch, HELD_STORM)
    hold = _manifest(tmp_path, [(ids[n], "hold") for n in REPLAYS],
                     version=2, name="hold.json")
    with monkeypatch.context() as m:
        m.setattr(L, "_preserve_reviewed_hold_dependencies",
                  _earlier_binary_hold)
        _apply(app, capsys, hold)
    assert _resets() == sorted(ids[n] for n in ("L0", "L1", "L2", "L3"))
    snapshots = _of("snapshot_accept")
    for name in ("A1x", "A3x"):
        assert f"sa:{ids[name]}" not in snapshots, name
    assert snapshots[f"sa:{ids['A2']}"]["weekly_observation_held"] == 1
    for name in ("L0", "L1", "L2"):
        assert snapshots[f"sa:{ids[name]}"]["weekly_observation_held"] == 0
    rc, plain = _rederive(app, capsys)
    assert rc == 0, plain
    assert plain["actionCountsByEventKind"]["week_reset"]["tombstone"] == 3
    return ids


def test_retract_tombstones_fakes_whose_raw_predecessor_is_a_held_replay(
        monkeypatch, tmp_path, capsys):
    """Detects fake resets surviving a reviewed retraction on the operator's
    store shape (review of 79847ac5a, D1): the reset's predecessor is the
    latest raw weekly observation of its account before the origin — here a
    held replay that is a held snapshot (A2) or has no snapshot event (A1x,
    A3x) — not the latest unheld current snapshot, which is a genuine
    reading. Every fake is in retraction scope, so its tombstone survives
    the baseline/reviewed intersection although the baseline plan carries
    the identical tombstone; the genuine reset survives with its identity,
    no milestone cites a fake, and a plain rederive and a rebuild change
    nothing."""
    app = _app(monkeypatch, tmp_path)
    ids = _store_after_an_earlier_hold(app, monkeypatch, tmp_path, capsys)
    genuine = _of("week_reset")[_wr(ids, "L0")]
    retract = _manifest(tmp_path, [(ids[n], "retract") for n in REPLAYS],
                        name="retract.json")
    rc, preview = _rederive(app, capsys, retract)
    assert rc == 0, preview
    assert preview["actionCountsByEventKind"]["week_reset"]["tombstone"] == 3, (
        "fake resets excluded from the reviewed batch as baseline drift")
    _preview, applied = _apply(app, capsys, retract)
    assert applied["actionCountsByEventKind"]["week_reset"]["tombstone"] == 3
    after = _active()
    assert _resets(after) == [ids["L0"]]
    assert _of("week_reset", after)[_wr(ids, "L0")] == genuine
    fakes = {_wr(ids, n) for n in ("L1", "L2", "L3")}
    assert not fakes & set(after)
    assert not {ref for _t, ref, _s in _milestones(after).values()} & fakes
    live = _logical(app)
    assert sum(live["resets"].values()) == 1
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    assert _no_actions(plain), plain["actionCounts"]
    assert _logical(app) == live
    T.rebuild(app)
    assert _logical(app) == live
    assert _resets() == [ids["L0"]]


def test_a_fake_whose_raw_predecessor_stays_held_is_not_retraction_scope(
        monkeypatch, tmp_path, capsys):
    """Control, detects an over-wide retraction scope: on the same store a
    manifest retracts A1, A1x and A2 while A3 and A3x keep the earlier
    `hold`. R1 and R2 (raw predecessors A1x and A2, retracted) are
    tombstoned; R3's raw predecessor A3x is held but not retracted, so its
    tombstone — identical in the baseline and reviewed plans — stays
    excluded drift and R3 survives the reviewed apply."""
    app = _app(monkeypatch, tmp_path)
    ids = _store_after_an_earlier_hold(app, monkeypatch, tmp_path, capsys)
    r3 = _of("week_reset")[_wr(ids, "L3")]
    retract = _manifest(tmp_path, [(ids[n], "retract")
                                   for n in ("A1", "A1x", "A2")],
                        name="retract.json")
    preview, _applied = _apply(app, capsys, retract)
    assert preview["actionCountsByEventKind"]["week_reset"]["tombstone"] == 2
    assert _resets() == sorted([ids["L0"], ids["L3"]])
    assert _of("week_reset")[_wr(ids, "L3")] == r3


# --- D4: a reviewed decision whose plan is empty ---------------------------------
#
# The documented split procedure (docs/commands/db.md) applies a later manifest
# whose retractions an earlier decision already holds, so its decision plan is
# empty. Its apply appends an empty completed correction batch right after the
# op, so the plain path reads the decision as finished; without it every later
# plain rederive refuses once any other record follows the op, and drift is
# never applied again.

#: A reading after the storm: an ordinary non-correction journal record.
LATER = (("H", "api", "09:45:00", 4, 18),)

_UNFINISHED = "unfinished reviewed weekly decision has new journal records"


def _after_op(op_id) -> list:
    records = T.journal_records()
    [index] = [i for i, r in enumerate(records)
               if r.get("t") == "op" and r.get("id") == op_id]
    return records[index + 1:]


def _assert_empty_batch(suffix) -> None:
    assert [(r.get("t"), r.get("phase")) for r in suffix] == [
        ("correction_batch", "begin"), ("correction_batch", "commit")], suffix
    assert len({r["id"] for r in suffix}) == 1, suffix
    for record in suffix:
        assert record["family"] == "claude-usage", record
        assert record["action_count"] == 0, record


def test_empty_reviewed_decision_leaves_later_drift_appliable(
        monkeypatch, tmp_path, capsys):
    """D4, detects a store whose plain rederive refuses forever: a second
    manifest re-submits the earlier hold of the operator's store shape, so
    its decision plan is empty; after one ordinary reading the plain
    rederive must still preview and apply the store's baseline drift (the
    three fake resets) instead of refusing the unfinished decision."""
    app = _app(monkeypatch, tmp_path)
    ids = _store_after_an_earlier_hold(app, monkeypatch, tmp_path, capsys)
    again = _manifest(tmp_path, [(ids[n], "hold") for n in REPLAYS],
                      version=2, name="again.json")
    preview, _applied = _apply(app, capsys, again)
    assert _no_actions(preview), preview["actionCounts"]
    _record(app, monkeypatch, LATER)
    rc, plain = _rederive(app, capsys)
    assert rc == 0, plain["conflicts"]
    assert plain["actionCountsByEventKind"]["week_reset"]["tombstone"] == 3
    rc, applied = _rederive(app, capsys, yes=True)
    assert rc == 0 and applied["status"] == "applied", applied
    assert _resets() == [ids["L0"]]
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain


def test_empty_reviewed_decision_is_a_completed_empty_batch(
        monkeypatch, tmp_path, capsys):
    """D4, detects an empty decision left unfinished or a recovery loop over
    its batch: re-submitting an applied retraction appends exactly one empty
    completed batch after its op; a rebuild changes nothing, two plain
    rederives are no-ops that write nothing, and after an ordinary reading
    the plain rederive still runs."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    decisions = [(ids[n], "retract") for n in ("A1", "A2", "A3")]
    _apply(app, capsys, _manifest(tmp_path, decisions))
    preview, applied = _apply(app, capsys,
                              _manifest(tmp_path, decisions, name="again.json"))
    assert _no_actions(preview), preview["actionCounts"]
    _assert_empty_batch(_after_op(applied["reviewedWeeklyDecisionId"]))
    live = _logical(app)
    T.rebuild(app)
    assert _logical(app) == live
    for _round in range(2):
        before = T.journal_records()
        rc, plain = _rederive(app, capsys, yes=True)
        assert rc == 0 and plain["status"] == "no-op", plain
        assert T.journal_records() == before
    assert _logical(app) == live
    _record(app, monkeypatch, LATER)
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain


def _empty_decision_without_batch(app, monkeypatch, tmp_path, capsys):
    """The storm retracted, then the same retraction re-applied the way the
    previous binary applied an empty decision: its op and no batch."""
    import dataclasses
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    decisions = [(ids[n], "retract") for n in ("A1", "A2", "A3")]
    _apply(app, capsys, _manifest(tmp_path, decisions))
    original = R._preview_from_snapshot

    def earlier_binary(*args, **kwargs):
        return dataclasses.replace(original(*args, **kwargs),
                                   empty_decision_batch_id=None)

    with monkeypatch.context() as m:
        m.setattr(R, "_preview_from_snapshot", earlier_binary)
        _p, stuck = _apply(app, capsys,
                           _manifest(tmp_path, decisions, name="stuck.json"))
    assert _after_op(stuck["reviewedWeeklyDecisionId"]) == []
    return decisions, stuck["reviewedWeeklyDecisionId"]


def test_plain_rederive_completes_an_unfinished_empty_decision(
        monkeypatch, tmp_path, capsys):
    """D4 resume path, detects an unfinished empty decision staying
    unfinished until a later record makes it refuse: with only its op in
    the journal, the plain preview reports the pending write and the plain
    apply appends the empty batch."""
    app = _app(monkeypatch, tmp_path)
    _decisions, op_id = _empty_decision_without_batch(
        app, monkeypatch, tmp_path, capsys)
    before = T.journal_records()
    rc, plain = _rederive(app, capsys)
    assert rc == 0 and plain["status"] == "preview", plain
    assert _no_actions(plain), plain["actionCounts"]
    assert T.journal_records() == before
    rc, recovered = _rederive(app, capsys, yes=True)
    assert rc == 0 and recovered["status"] == "recovered", recovered
    _assert_empty_batch(_after_op(op_id))
    _record(app, monkeypatch, LATER)
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain


def test_a_store_stuck_on_an_empty_decision_recovers_by_a_fresh_manifest(
        monkeypatch, tmp_path, capsys):
    """D4, detects a store an earlier binary left stuck staying stuck: the
    empty decision was applied without its batch and a reading followed, so
    the plain rederive refuses; a fresh reviewed manifest (the conflict's own
    remedy) then writes its empty batch, and the plain rederive works.

    #1015 spec 7 (A10, R1-P1-2), detects a recovered store whose forecast
    never calibrates again: a fit made while the store was stuck stays
    clean across the recovery's empty batch, and through a qualifying refit
    and a rebuild the forecast is calibrated from the new fit. Every sweep
    is the production `sweep_account` over a real `load_population`; only
    the analyses are constructed."""
    import test_derived_consumers_retract_797 as D
    import _cctally_quota_model as qmg
    qm = qmg.qm
    app = _app(monkeypatch, tmp_path)
    account = T.identity("A")
    D._seed_entries(app, account)
    decisions, _op_id = _empty_decision_without_batch(
        app, monkeypatch, tmp_path, capsys)
    _record(app, monkeypatch, LATER)
    rc, refused = _rederive(app, capsys)
    assert rc == 2 and _UNFINISHED in refused["conflicts"][0], refused
    D._sweep(account, D._analysis(fitted=D._evidence(qm, D.X_OLD)))
    assert D._forecast(account) == D._expected_forecast(D.X_OLD)
    _p, fresh = _apply(app, capsys,
                       _manifest(tmp_path, decisions, name="fresh.json"))
    _assert_empty_batch(_after_op(fresh["reviewedWeeklyDecisionId"]))
    # #1015: the fresh manifest's empty batch is completed and touches no
    # account, so it can invalidate no fit; its op is ordered by coordinate.
    fresh_op = fresh["reviewedWeeklyDecisionId"]
    [empty] = _batches_after(fresh_op)
    facts = _lineage(app)
    assert facts["batches"][empty] == (_commit_end(empty), frozenset())
    assert facts["ops"][fresh_op] == _line_end(lambda r: r.get("id") == fresh_op)
    assert _materializing(facts, fresh_op, T.identity("A")) == set()
    live = _logical(app)
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    _record(app, monkeypatch, (("I", "api", "09:50:00", 5, 19),))
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    T.rebuild(app)
    assert _logical(app)["resets"] == live["resets"]
    _agree_across_rebuilds(app)
    # The empty batch touches no account: the stuck-era fit still steers.
    assert D._forecast(account) == D._expected_forecast(D.X_OLD)
    # A qualifying refit after the recovery, then a rebuild: the forecast is
    # calibrated from the new fit, whose basis is every completed batch.
    D._sweep(account, D._analysis(fitted=D._evidence(qm, D.X_NEW)))
    T.rebuild(app)
    assert D._open_regime(account)["fitLineage"]["basis"] == sorted(
        D._basis(app))
    assert D._assessments(app, account) == ("clean",)
    assert D._forecast(account) == D._expected_forecast(D.X_NEW)


def test_plain_rederive_with_an_empty_plan_writes_no_batch(
        monkeypatch, tmp_path, capsys):
    """D4 control, detects the empty batch leaking onto the plain path: with
    no reviewed decision and nothing to correct, an applied plain rederive
    appends nothing."""
    app = _app(monkeypatch, tmp_path)
    _storm(app, monkeypatch)
    before = T.journal_records()
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    assert T.journal_records() == before


# --- #1015 T0: the R8 probe (spec section 7, R8) ---------------------------------
#
# An interrupted NONEMPTY reviewed hold (its durable op, no batch), then an
# ordinary reading (so the plain path refuses the unfinished decision), then a
# fresh manifest repeating the hold on the unchanged source. The fresh
# manifest's baseline used to replay the earlier, never-completed op, so the
# delta dropped every hold action and the fresh batch materialized nothing:
# the held observations' inputs stayed uncorrected until a later plain
# rederive.

_HELD = ("A1", "A2", "A3")


def _interrupted_hold(app, monkeypatch, tmp_path, capsys) -> dict:
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    path = _manifest(tmp_path, [(ids[n], "hold") for n in _HELD], version=2,
                     name="interrupted.json")
    rc, preview = _rederive(app, capsys, path)
    assert rc == 0 and not _no_actions(preview), preview["actionCounts"]
    body = json.loads(path.read_text())
    body.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(body))

    def crash(stage):
        if stage == "after-reviewed-decision-op":
            raise RuntimeError("simulated crash after durable op")

    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", crash)
    op = R._reviewed_weekly_op_from_manifest(path)
    with pytest.raises(RuntimeError, match="durable op"):
        R.apply_db_rederive(
            "claude-usage", reviewed_op=op,
            expected_plan_hashes=(preview["baselinePlanHash"],
                                  preview["decisionPlanHash"]))
    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", None)
    assert not any(r.get("t") == "correction_batch" for r in T.journal_records()
                   if r.get("family") == "claude-usage")
    _record(app, monkeypatch, LATER)
    rc, refused = _rederive(app, capsys)
    assert rc == 2 and _UNFINISHED in refused["conflicts"][0], refused
    return ids


def _reference_after_hold(monkeypatch, tmp_path, capsys) -> dict:
    """The corrected inputs: an uninterrupted reviewed hold of the replays
    over the same observations, in its own store."""
    root = tmp_path / "reference"
    root.mkdir()
    app = _app(monkeypatch, root)
    ids = _storm(app, monkeypatch)
    _record(app, monkeypatch, LATER)
    _apply(app, capsys, _manifest(root, [(ids[n], "hold") for n in _HELD],
                                  version=2))
    return _logical(app)


def test_r8_fresh_manifest_after_an_interrupted_hold_corrects_the_inputs(
        monkeypatch, tmp_path, capsys):
    """#1015 R8, detects a reviewed hold whose fresh-manifest recovery leaves
    the stats inputs uncorrected (the index, and every fit over it, stays
    poisoned until an unrelated plain rederive): right after the fresh
    manifest the store equals an uninterrupted hold's (snapshots, resets,
    milestones), the fake reset the hold retires is gone, and a plain
    rederive afterwards has no remaining correction."""
    reference = _reference_after_hold(monkeypatch, tmp_path, capsys)
    root = tmp_path / "probe"
    root.mkdir()
    app = _app(monkeypatch, root)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _interrupted_hold(app, monkeypatch, root, capsys)
    fresh = _manifest(root, [(ids[n], "hold") for n in _HELD], version=2,
                      name="fresh.json")
    # The reviewed plan hashes that move: the baseline plan (the abandoned op
    # left the baseline) and with it the decision (delta) plan, which now
    # carries the hold's actions. A manifest previewed before the fix must be
    # re-previewed.
    with monkeypatch.context() as m:
        m.setattr(R, "_fresh_manifest_baseline",
                  lambda records, _completed, _family, exclude_index=None: [
                      r for i, r in enumerate(records) if i != exclude_index])
        rc, before_fix = _rederive(app, capsys, fresh)
    assert rc == 0 and _no_actions(before_fix), before_fix["actionCounts"]
    rc, fixed = _rederive(app, capsys, fresh)
    assert rc == 0 and not _no_actions(fixed), fixed["actionCounts"]
    assert fixed["baselinePlanHash"] != before_fix["baselinePlanHash"]
    assert fixed["decisionPlanHash"] != before_fix["decisionPlanHash"]
    preview, applied = _apply(app, capsys, fresh)
    assert _resets() == sorted([ids["L0"], ids["L1"], ids["L2"]]), (
        "fresh manifest left the hold's corrections unapplied",
        preview["actionCounts"], applied["actionCounts"])
    assert _logical(app) == reference
    rc, plain = _rederive(app, capsys)
    assert rc == 0 and _no_actions(plain), plain["actionCounts"]


# --- #1015 T1: correction ordering is materialization (spec 5.1, section 7) -----
#
# A reviewed decision changes the population only when a completed correction
# batch materializes it, whatever produced the batch. The published lineage
# facts — each reviewed op's journal coordinate, each completed batch's commit
# coordinate and footprint, each decision's decoded disposition — must be
# identical on live ingest, an ordinary rebuild, a correction rebuild and a
# completing rebuild, or a fit's assessment would depend on the path that
# published its inputs.


def _lineage(app) -> dict:
    """The published lineage facts."""
    ops = {op: (seg, off) for op, seg, off in _stats(
        app, "SELECT decision_op_id, op_segment, op_offset "
             "FROM claude_weekly_correction_ops")}
    footprints = collections.defaultdict(set)
    for batch_id, provider, account in _stats(
            app, "SELECT batch_id, provider, account_key "
                 "FROM correction_batch_footprint"):
        footprints[batch_id].add((provider, account))
    batches = {batch_id: ((seg, off), frozenset(footprints.get(batch_id, ())))
               for batch_id, seg, off in _stats(
                   app, "SELECT batch_id, commit_segment, commit_offset "
                        "FROM correction_batch_lineage")}
    assert set(footprints) <= set(batches), "a footprint without its batch"
    history = collections.Counter(_stats(
        app, "SELECT decision_op_id, observation_id, disposition "
             "FROM claude_weekly_correction_history"))
    return {"ops": ops, "batches": batches, "history": history}


def _materializing(facts, op_id, account) -> set:
    """The batches that may materialize a correction of ``op_id`` for an
    observation of ``account`` (spec 5.1): committed after the op, and
    touching that account, every Claude account, or an unknowable owner."""
    import _cctally_journal as jr
    op_at = jr.journal_coordinate_key(facts["ops"][op_id])
    owners = {("claude", account), ("claude", "*"), ("*", "*")}
    return {batch_id for batch_id, (commit, footprint)
            in facts["batches"].items()
            if jr.journal_coordinate_key(commit) > op_at and footprint & owners}


def _line_end(match) -> tuple:
    """The END coordinate of the first journal line ``match`` accepts."""
    import _cctally_journal as jr
    import _lib_journal as J
    for segment, offset, raw in jr.iter_range(None, jr.journal_high_water()):
        record = J.decode_line(raw)
        if record is not None and match(record):
            return (segment, offset + len(raw) + 1)
    raise AssertionError("no such journal line")


def _commit_end(batch_id) -> tuple:
    return _line_end(lambda r: r.get("t") == "correction_batch"
                     and r.get("phase") == "commit" and r.get("id") == batch_id)


def _batches_after(op_id) -> list:
    return [r["id"] for r in _after_op(op_id)
            if r.get("t") == "correction_batch" and r.get("phase") == "commit"]


def _agree_across_rebuilds(app) -> dict:
    """The facts as published now, which an ordinary rebuild and a completing
    rebuild (no predecessor index) must reproduce exactly."""
    import _cctally_core as core
    facts = _lineage(app)
    T.rebuild(app)
    assert _lineage(app) == facts, "ordinary rebuild"
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = core.DB_PATH.with_name(core.DB_PATH.name + suffix)
        if path.exists():
            path.unlink()
    T.rebuild(app)
    assert _lineage(app) == facts, "completing rebuild"
    return facts


def test_lineage_an_op_and_its_completed_batch(monkeypatch, tmp_path, capsys):
    """#1015, detects a reviewed hold whose correction is not recorded
    against its account (a fit over the held inputs would stay steering):
    the op's coordinate is the end of its line, its nonempty batch commits
    after it and touches account A, the history carries the decoded
    dispositions, and live ingest and every rebuild publish the same facts."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    _p, applied = _apply(app, capsys, _manifest(
        tmp_path, [(ids["A1"], "hold"), (ids["A2"], "retract")]))
    op = applied["reviewedWeeklyDecisionId"]
    [batch] = _batches_after(op)
    facts = _lineage(app)
    assert facts["ops"] == {op: _line_end(lambda r: r.get("id") == op)}
    assert facts["batches"][batch][0] == _commit_end(batch)
    assert ("claude", T.identity("A")) in facts["batches"][batch][1]
    assert _materializing(facts, op, T.identity("A")) == {batch}
    assert facts["history"] == collections.Counter(
        {(op, ids["A1"], "hold"): 1, (op, ids["A2"], "retract"): 1})
    _assert_reader_sees(facts, T.identity("A"))
    _record(app, monkeypatch, LATER)
    assert _lineage(app) == facts, "a later live ingest"
    _agree_across_rebuilds(app)
    _assert_reader_sees(facts, T.identity("A"))


def _assert_reader_sees(facts, account) -> None:
    """The fail-fast reader returns exactly the published facts: the scoped
    rows carry their index account and op coordinate, another account's
    scope sees none of them, and every batch arrives with its footprint."""
    import _cctally_quota_calibration as qcg
    read = qcg.read_correction_facts(account_scope=account)
    assert read is not None, "the published index must be readable"
    assert collections.Counter(
        (r.op_id, r.observation_id, r.disposition) for r in read.corrections
    ) == facts["history"]
    assert {r.account_key for r in read.corrections} == {account}
    assert all(r.op_coord == facts["ops"][r.op_id] and r.captured_at
               for r in read.corrections)
    assert {b.batch_id: (b.commit_coord, b.footprint)
            for b in read.batches} == facts["batches"]
    assert read.basis == frozenset(facts["batches"])
    other = qcg.read_correction_facts(account_scope=T.identity("B"))
    assert other is not None and other.corrections == ()


def test_lineage_an_empty_batch_completed_by_live_ingest_touches_nothing(
        monkeypatch, tmp_path, capsys):
    """#1015, detects an empty decision batch that invalidates fits (or is
    missing) when only live ingest completed it: batch commit, crash before
    the rebuild, live ingest, then a no-op plain rederive. The batch is
    completed with no footprint, and the facts survive every rebuild."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _storm(app, monkeypatch)
    decisions = [(ids[n], "retract") for n in _HELD]
    _apply(app, capsys, _manifest(tmp_path, decisions))
    path = _manifest(tmp_path, decisions, name="again.json")
    rc, preview = _rederive(app, capsys, path)
    assert rc == 0 and _no_actions(preview), preview["actionCounts"]
    body = json.loads(path.read_text())
    body.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(body))

    def crash(stage):
        if stage == "after-batch-commit":
            raise RuntimeError("simulated crash after the batch commit")

    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", crash)
    with pytest.raises(RuntimeError, match="batch commit"):
        R.apply_db_rederive(
            "claude-usage",
            reviewed_op=R._reviewed_weekly_op_from_manifest(path),
            expected_plan_hashes=(preview["baselinePlanHash"],
                                  preview["decisionPlanHash"]))
    monkeypatch.setattr(R, "_REDERIVE_CRASH_HOOK", None)
    [op] = [r["id"] for r in T.journal_records() if r.get("t") == "op"
            and (r.get("payload") or {}).get("kind", "").startswith(
                "claude_weekly_observation_decision")][-1:]
    [batch] = _batches_after(op)
    assert batch not in _lineage(app)["batches"], "nothing ingested it yet"
    T.ingest(app)
    facts = _lineage(app)
    assert facts["batches"][batch] == (_commit_end(batch), frozenset())
    assert facts["ops"][op] == _line_end(lambda r: r.get("id") == op)
    assert _materializing(facts, op, T.identity("A")) == set()
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "no-op", plain
    assert _lineage(app) == facts
    _agree_across_rebuilds(app)


def test_lineage_an_op_whose_batch_never_completes_has_nothing_to_order(
        monkeypatch, tmp_path, capsys):
    """#1015, detects an interrupted decision that taints fits although it
    changed nothing: its op is recorded (live ingest read it) and no batch
    follows it."""
    app = _app(monkeypatch, tmp_path)
    _interrupted_hold(app, monkeypatch, tmp_path, capsys)
    [op] = [r["id"] for r in T.journal_records() if r.get("t") == "op"
            and (r.get("payload") or {}).get("kind", "").startswith(
                "claude_weekly_observation_decision")]
    facts = _lineage(app)
    assert op in facts["ops"]
    assert _materializing(facts, op, T.identity("A")) == set()
    assert _agree_across_rebuilds(app) == facts


def test_lineage_a_historical_r8_journal_orders_by_the_plain_batch(
        monkeypatch, tmp_path, capsys):
    """#1015 R8 history (R4-P3-1), detects a journal an affected release
    already wrote staying wrong after the fix: an interrupted hold, a reading,
    a fresh manifest applied as the unchanged binary did (an empty batch),
    then a reading and a plain rederive batch that corrects the held inputs.
    The empty batch touches nothing; the plain batch materializes both ops'
    corrections for account A."""
    app = _app(monkeypatch, tmp_path)
    import _cctally_rederive as R    # the module the loaded app uses
    ids = _interrupted_hold(app, monkeypatch, tmp_path, capsys)
    fresh = _manifest(tmp_path, [(ids[n], "hold") for n in _HELD], version=2,
                      name="fresh.json")
    with monkeypatch.context() as m:
        m.setattr(R, "_fresh_manifest_baseline",
                  lambda records, _completed, _family, exclude_index=None: [
                      r for i, r in enumerate(records) if i != exclude_index])
        preview, applied = _apply(app, capsys, fresh)
    assert _no_actions(preview), preview["actionCounts"]
    fresh_op = applied["reviewedWeeklyDecisionId"]
    [empty] = _batches_after(fresh_op)
    _record(app, monkeypatch, (("I", "api", "09:50:00", 5, 19),))
    rc, plain = _rederive(app, capsys, yes=True)
    assert rc == 0 and plain["status"] == "applied", plain
    [first_op] = [r["id"] for r in T.journal_records() if r.get("t") == "op"
                  and r["id"] != fresh_op
                  and (r.get("payload") or {}).get("kind", "").startswith(
                      "claude_weekly_observation_decision")]
    plain_batches = [b for b in _batches_after(fresh_op) if b != empty]
    assert len(plain_batches) == 1, plain_batches
    facts = _lineage(app)
    assert facts["batches"][empty][1] == frozenset()
    account = T.identity("A")
    assert _materializing(facts, first_op, account) == set(plain_batches)
    assert _materializing(facts, fresh_op, account) == set(plain_batches)
    _agree_across_rebuilds(app)


def _effective_rev(event_id) -> int:
    import _lib_journal as J
    return J.resolve_effective_events(T.journal_records()).by_id[event_id].rev


def _append_batch(name, actions) -> str:
    """Append one completed correction batch the way every producer does,
    then ingest it: a batch that changes a winner is healed by live ingest's
    correction rebuild (one per cycle)."""
    import _cctally_journal as jr
    import _lib_journal as J
    batch_id = f"test-1015:{name}"
    jr.append_records(J.make_correction_batch(
        batch_id=batch_id, family="test-1015", at="2026-10-23T13:00:00Z",
        actions=actions))
    T.ingest(None)
    return batch_id


def test_lineage_footprints_follow_the_owner_the_fold_stores(
        monkeypatch, tmp_path, capsys):
    """#1015 (R5-P1-2, R5-P2-1), detects a correction attributed to the wrong
    account — a missed taint, or one on an unrelated account: a replacement
    moving an event from A to B touches both; a tombstone touches its removed
    owner; a payload without an account is owned by the fold's default; a
    later move touching only B, and a Codex-only batch, never materialize a
    correction for A; and no batch committed before an op counts for it."""
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    _p, applied = _apply(app, capsys, _manifest(
        tmp_path, [(ids[n], "hold") for n in _HELD], version=2))
    op = applied["reviewedWeeklyDecisionId"]
    [reviewed_batch] = _batches_after(op)
    account_a, account_b = T.identity("A"), T.identity("B")
    T.identity("A")
    active = _active()
    m1, m2, m3 = sorted(_milestones(active))[:3]

    def replace(event_id, payload, bump=1):
        return {"action": "replace", "id": event_id,
                "rev": _effective_rev(event_id) + bump,
                "at": "2026-10-23T13:00:00Z", "payload": payload}

    moved = _append_batch("move", [replace(
        m1, dict(active[m1], account_key=account_b))])
    removed = _append_batch("tomb", [{
        "action": "tombstone", "id": m2, "rev": _effective_rev(m2) + 1,
        "at": "2026-10-23T13:00:00Z", "payload": None}])
    defaulted = _append_batch("default", [replace(
        m3, {k: v for k, v in active[m3].items() if k != "account_key"})])
    only_b = _append_batch("only-b", [replace(
        m1, dict(active[m1], account_key=account_b))])
    codex = _append_batch("codex", [{
        "action": "replace", "id": "qaa:test-1015", "rev": 1,
        "at": "2026-10-23T13:00:00Z",
        "payload": {"kind": "quota_alert_arming", "state": "disarmed",
                    "source": "codex", "source_root_key": "root",
                    "logical_limit_key": "weekly", "observed_slot": "slot",
                    "window_minutes": 10080,
                    "account_key": "unattributed"}}])
    facts = _lineage(app)
    footprint = {b: facts["batches"][b][1] for b in
                 (moved, removed, defaulted, only_b, codex)}
    assert footprint == {
        moved: {("claude", account_a), ("claude", account_b)},
        removed: {("claude", account_a)},
        defaulted: {("claude", account_a), ("claude", "unattributed")},
        only_b: {("claude", account_b)},
        codex: {("codex", "unattributed")},
    }
    for batch_id in footprint:
        assert facts["batches"][batch_id][0] == _commit_end(batch_id)
    assert _materializing(facts, op, account_a) == {
        reviewed_batch, moved, removed, defaulted}
    assert {moved, only_b} <= _materializing(facts, op, account_b)
    # A later reviewed decision: every batch above committed before its op.
    _p, later = _apply(app, capsys, _manifest(
        tmp_path, [(ids["A1"], "accept")], version=2, name="later.json"))
    later_op = later["reviewedWeeklyDecisionId"]
    facts = _lineage(app)
    assert _materializing(facts, later_op, account_a) == set(
        _batches_after(later_op))
    assert not _materializing(facts, later_op, account_a) & set(footprint)
    _agree_across_rebuilds(app)


def test_lineage_a_derivation_completion_over_a_held_snapshot_taints_by_window(
        monkeypatch, tmp_path, capsys):
    """#1015 spec 7 (A10, F3), detects a derivation-completion batch that
    replaces a held observation's retained snapshot without invalidating the
    fits over it: the batch touches the observation's account, so a stamped
    fit whose window holds the capture — clean before the batch — is tainted
    by it, and a fit whose window does not hold it stays clean."""
    import _cctally_journal as jr
    import _cctally_quota_calibration as qcg
    import _lib_journal as J
    import _lib_quota_lineage as L
    app = _app(monkeypatch, tmp_path)
    ids = _storm(app, monkeypatch)
    _p, applied = _apply(app, capsys, _manifest(
        tmp_path, [(ids[n], "hold") for n in _HELD], version=2))
    op = applied["reviewedWeeklyDecisionId"]
    account = T.identity("A")
    basis = sorted(_lineage(app)["batches"])

    def fit(start, until):
        return L.fit_lineage(
            scope=account, basis=basis,
            windows=(L.window(start, until, until_inclusive=True),),
            estimator=None, regime={"asOf": until})

    overlapping = fit("2026-10-10T00:00:00+00:00", "2026-10-23T00:00:00+00:00")
    elsewhere = fit("2026-09-22T00:00:00+00:00", "2026-10-10T00:00:00+00:00")

    def assessed(lineage):
        return L.assess(lineage, qcg.read_correction_facts(
            account_scope=account), coord_key=qcg._coordinate_order).state

    assert assessed(overlapping) == "clean", "the reviewed batch is in basis"
    event_id = "sa:" + ids["A1"]
    assert event_id in _active(), "the held observation keeps its snapshot"
    batch = f"{jr.DERIVATION_COMPLETION_FAMILY}:test-1015-held"
    jr.append_records(J.make_correction_batch(
        batch_id=batch, family=jr.DERIVATION_COMPLETION_FAMILY,
        at="2026-10-23T13:00:00Z", actions=[{
            "action": "replace", "id": event_id,
            "rev": _effective_rev(event_id) + 1,
            "at": "2026-10-23T13:00:00Z",
            "payload": dict(_active()[event_id])}]))
    T.ingest(None)
    facts = _lineage(app)
    assert facts["batches"][batch][0] == _commit_end(batch)
    assert ("claude", account) in facts["batches"][batch][1]
    assert batch in _materializing(facts, op, account)
    assert assessed(overlapping) == "tainted"
    assert assessed(elsewhere) == "clean"
    _agree_across_rebuilds(app)
    assert assessed(overlapping) == "tainted"


@pytest.mark.parametrize("actions, predecessors, expected", [
    # replacement A -> B, then a later action on the same id sees B
    ([("e", 1, "replace", ("claude", "B")), ("e", 2, "replace", ("claude", "C"))],
     {"e": ("active", ("claude", "A"))},
     {("claude", "A"), ("claude", "B"), ("claude", "C")}),
    # tombstone of a tombstoned id removes nothing; its re-creation adds only
    # its own payload
    ([("e", 2, "tombstone", None), ("e", 3, "replace", ("claude", "B"))],
     {"e": ("tombstone", None)}, {("claude", "B")}),
    # a tombstone over nothing touches nothing
    ([("e", 0, "tombstone", None)], {}, set()),
    # a new event (explicit, or a replacement with no predecessor)
    ([("e", 0, "new", ("codex", "unattributed")),
      ("f", 1, "replace", ("claude", "*"))],
     {}, {("codex", "unattributed"), ("claude", "*")}),
    # an unresolvable owner is genuinely unknowable
    ([("e", 1, "replace", None)], {"e": ("active", None)}, {("*", "*")}),
    # a completed empty batch
    ([], {}, set()),
])
def test_batch_footprint_kernel(actions, predecessors, expected):
    """#1015 spec 5.1, detects a footprint computed from the wrong revision
    (a later final winner instead of the one effective at the action)."""
    import _lib_quota_lineage as L
    seen = []

    def predecessor_of(event_id, rev):
        seen.append((event_id, rev))
        found = predecessors.get(event_id)
        return None if found is None else L.Revision(*found)

    got = L.batch_footprint([L.Action(*a) for a in actions], predecessor_of)
    assert got == frozenset(expected)
    assert len(seen) == len({a[0] for a in actions if a[2] != "new"}), (
        "only the first action on an id consults the caller")
