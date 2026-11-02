"""Shared end-to-end driver for the #797 stale-contributor scenarios (spec
section 6 T1, T4, T15).

Every reading goes through the real paths: a status-line render is the real
`_statusline_persist(parsed, sync_for_test=True)` over a real synthetic
transcript (main and subagent files, the line template of
`tests/test_milestone_rebuild_after_five_hour_credit.py`), so the provider-
activity witness is captured by the real scanner and the detached reducer
runs synchronously; an OAuth reading is the real forced refresh with only
the network fetch stubbed, so its observation carries
`oauth_request_started_at` and lands in `claude_oauth_samples`; a manual
credit is the real `record-credit`. Maintenance is the real `db rebuild` and
`db rederive` (`tests/_outcome_quarantine_trace.py`).

Outcomes are compared with `collections.Counter` over complete payloads:
live, rebuilt and rederived stores must agree, and every scenario also pins
literal expected counts, so equally empty or equally wrong stores never pass.

Task 5 builds its five-hour scenarios from the same `Trace`: a render or
an OAuth reading carries a five-hour value when ``five`` is given, and the
mutations below cover both axes and compose (`mutate(mp, a, b)`).
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import pathlib
import sys
import time

import _lib_oauth_samples as samples_lib
import _outcome_quarantine_trace as T

UTC = dt.timezone.utc

#: The scenarios' weekly window: resets 2026-10-24T05:00:00Z (on the hour).
WEEK_END = "2026-10-24T05:00:00Z"
#: The five-hour scenarios' window: resets 2026-10-22T12:00:00Z (its
#: canonical key is the same instant).
FIVE_END = "2026-10-22T12:00:00Z"


def epoch(text: str) -> int:
    return T.epoch(text)



def iso(value: int) -> str:
    return T.iso(value)


def _assistant(ts: str, *, session: str, n: int) -> str:
    """One assistant response line (the template of
    `tests/test_milestone_rebuild_after_five_hour_credit.py:88`)."""
    return json.dumps({
        "type": "assistant", "sessionId": session, "timestamp": ts,
        "requestId": f"req_{session}_{n}", "cwd": f"/work/{session}",
        "message": {"id": f"msg_{session}_{n}", "model": "claude-opus-5-5",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    })


class Session:
    """One Claude Code session: a transcript and its subagent directory."""

    def __init__(self, root: pathlib.Path, name: str):
        self.name = name
        self.path = root / "projects" / name / f"{name}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self._n = 0

    def respond(self, at: str, *, subagent: "str | None" = None) -> None:
        """Append one assistant response completed at ``at`` (to a subagent
        transcript when ``subagent`` names one)."""
        self._n += 1
        target = self.path
        if subagent is not None:
            target = self.path.with_suffix("") / "subagents" / f"agent-{subagent}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as fh:
            fh.write(_assistant(at, session=self.name, n=self._n) + "\n")


class Trace:
    """One scenario's store, clock and inspectors.

    ``week_resets_at`` (#1019) places the scenarios' weekly window: the
    epoch every render and OAuth reading advertises unless a step names its
    own ``week_end``."""

    def __init__(self, monkeypatch, tmp_path, *, account="A",
                 week_resets_at: int = epoch(WEEK_END)):
        self.mp = monkeypatch
        self.root = tmp_path
        self.app = T.make_app(monkeypatch, tmp_path)
        self.sl = sys.modules["_cctally_statusline"]
        monkeypatch.setattr(self.app, "_bust_statusline_cache", lambda: "skipped")
        monkeypatch.setattr(self.app, "_resolve_oauth_token", lambda: "credential")
        # A render triggers no OAuth tick of its own: OAuth readings are the
        # scenario's explicit steps.
        monkeypatch.setattr(self.sl, "_statusline_oauth_tick", lambda *a, **k: None)
        self.account_name = account
        self.week_end = iso(int(week_resets_at))
        for name in dict.fromkeys(("A", "B", "unattributed", account)):
            T.retain_session_input(self.app, name)
        self.account = T.identity(account)
        self.log: list = []
        # The unpatched request-start clock, restored for a reading without
        # an exact ``requested_at``.
        self._request_started = sys.modules["_cctally_refresh"]._oauth_request_started_now
        #: Each published below-baseline reduction's route (`track_routes`).
        self.routes: list = []

    # --- steps ---------------------------------------------------------------

    def clock(self, at: str) -> None:
        T.set_clock(self.mp, at)

    def as_account(self, name: str) -> str:
        """Make ``name`` the selected Claude account of later steps."""
        self.account_name = name
        self.account = T.identity(name)
        return self.account

    def session(self, name: str) -> Session:
        return Session(self.root, name)

    def render(self, session: Session, percent: float, *, at: str,
               week_end: "str | None" = None, five: "float | None" = None,
               five_end: str = FIVE_END) -> None:
        """One real status-line render of ``session`` reporting ``percent``
        (and the five-hour ``five`` when given)."""
        week_end = self.week_end if week_end is None else week_end
        self.clock(at)
        T.identity(self.account_name)
        limits = {"seven_day": {"used_percentage": percent, "resets_at": week_end}}
        if five is not None:
            limits["five_hour"] = {"used_percentage": five, "resets_at": five_end}
        payload = {
            "session_id": session.name, "transcript_path": str(session.path),
            "rate_limits": limits,
        }
        parsed = self.app._lib_statusline.parse_statusline_stdin(
            json.dumps(payload).encode())
        self.sl._statusline_persist(parsed, sync_for_test=True)
        self.log.append(("render", session.name, float(percent),
                         None if five is None else float(five), at))

    def oauth(self, weekly: float, *, at: str, lead: int = 2,
              week_end: "str | None" = None, five: "float | None" = None,
              five_end: str = FIVE_END, requested_at: "str | None" = None) -> str:
        """One real forced OAuth refresh whose request starts ``lead`` seconds
        before its response is captured at ``at``; returns its ``q``. The
        response carries a five-hour window only when ``five`` is given.

        ``requested_at`` (#1019) is an exact request instant, fractional
        digits kept, in place of ``lead``: ``q`` is then that instant, and
        the request's clock is its whole second."""
        week_end = self.week_end if week_end is None else week_end
        T.identity(self.account_name)
        captured = epoch(at)
        refresh = sys.modules["_cctally_refresh"]
        if requested_at is not None:
            exact = samples_lib.canonical_instant(requested_at)
            assert exact is not None, requested_at
            whole = int(dt.datetime.fromisoformat(
                exact.replace("Z", "+00:00")).timestamp())
            assert whole <= captured, (requested_at, at)
            lead = captured - whole
            self.mp.setattr(refresh, "_oauth_request_started_now", lambda _c: exact)
        else:
            self.mp.setattr(refresh, "_oauth_request_started_now",
                            self._request_started)
        T.set_clock(self.mp, iso(captured - lead))
        response = {"seven_day": {"utilization": weekly, "resets_at": week_end}}
        if five is not None:
            response["five_hour"] = {"utilization": five, "resets_at": five_end}

        def fetch(**_kwargs):
            T.set_clock(self.mp, iso(captured))
            return response

        self.mp.setattr(self.app, "_fetch_oauth_usage", fetch)
        result = self.app._refresh_usage_inproc()
        assert result.status == "ok", result
        self.log.append(("oauth", float(weekly), five, at))
        if requested_at is not None:
            return exact
        return dt.datetime.fromtimestamp(captured - lead, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    def manual_credit(self, to: float, *, at: str, effective: "str | None" = None) -> None:
        """A real `record-credit --to <to> --yes` at ``at``."""
        self.clock(at)
        T.identity(self.account_name)
        argv = ["record-credit", "--to", str(to), "--yes"]
        if effective is not None:
            argv += ["--at", effective]
        args = self.app.build_parser().parse_args(argv)
        assert args.func(args) == 0
        self.log.append(("manual", float(to), at))

    def track_routes(self) -> None:
        """Record the route of every published drop (#1019): each frozen
        publication plan whose weekly or five-hour value lies below the
        same window's baseline is named ``unanimous`` (every pending
        contributor satisfied), ``deadline`` (the expired retained low
        despite unsatisfied contributors) or ``hold`` (`_hold_pending`, no
        live candidate on the axis), as ``(instant, axis, percent, route)``
        in `routes`."""
        lib = sys.modules["_lib_statusline_candidates"]
        real_axis, real_hold, real_plan = (
            lib._reduce_axis, lib._hold_pending, self.sl._bound_publication_plan)
        held: list = []
        decided: dict = {}

        def hold(*args, **kwargs):
            decision = real_hold(*args, **kwargs)
            held.append(decision)
            return decision

        def axis(name, candidates, db_axis, pending, tombstone, now_epoch, **kwargs):
            held.clear()
            decision = real_axis(name, candidates, db_axis, pending, tombstone,
                                 now_epoch, **kwargs)
            value = decision.candidate
            if (decision.action == "PUBLISH_DB" and value is not None
                    and db_axis is not None
                    and value.canonical_key == db_axis.canonical_key
                    and value.percent < db_axis.percent):
                if any(h is decision for h in held):
                    route = "hold"
                elif (decision.pending is not None and decision.pending.contributors
                      and all(c.satisfied
                              for c in decision.pending.contributors.values())):
                    route = "unanimous"
                else:
                    route = "deadline"
                decided.setdefault((int(now_epoch), name, float(value.percent)), route)
            return decision

        def plan(decision, projection, candidates, binding, **kwargs):
            frozen = real_plan(decision, projection, candidates, binding, **kwargs)
            if frozen is not None:
                at_ = int(kwargs["now_epoch"])
                for name in ("sevenDay", "fiveHour"):
                    value = frozen.get(name)
                    route = None if value is None else decided.get(
                        (at_, name, float(value["percent"])))
                    if route is not None:
                        self.routes.append((iso(at_), name, float(value["percent"]),
                                            route))
            return frozen

        self.mp.setattr(lib, "_hold_pending", hold)
        self.mp.setattr(lib, "_reduce_axis", axis)
        self.mp.setattr(self.sl, "_bound_publication_plan", plan)

    # --- inspectors ------------------------------------------------------------

    def _rows(self, sql, params=()):
        conn = self.app.open_db()
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def stored(self) -> "float | None":
        """The account's current weekly projection (what the status line
        accounts against)."""
        projection = self.sl._read_db_projection_once(account_key=self.account)
        return None if projection.seven_day is None else projection.seven_day.percent

    def published(self, source: str = "statusline", *, account=None) -> list:
        """Every weekly value a ``source`` observation recorded, in order."""
        return [r[0] for r in self._rows(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE account_key = ? AND source = ? ORDER BY id",
            (account or self.account, source))]

    def samples(self) -> list:
        return self._rows(
            "SELECT weekly_percent, requested_at_utc FROM claude_oauth_samples "
            "WHERE account_key = ? ORDER BY requested_at_utc", (self.account,))

    def transition(self, account=None):
        return self.sl._read_transition(account or self.account,
                                        now_epoch=int(time.time()))

    def receipts(self, account=None) -> list:
        t = self.transition(account)
        return [] if t is None else list(getattr(t, "receipts", ()))

    def receipt_for(self, session: Session, percent: float, *, axis: str = "sevenDay"):
        """The receipt held for the session's ``axis`` sample at ``percent``,
        or None."""
        token = self.token(session)
        for r in self.receipts():
            if (r.sigma.token == token and r.sigma.axis == axis
                    and r.sigma.percent == float(percent)):
                return r
        return None

    def five_published(self, source: str = "statusline", *, account=None) -> list:
        """Every five-hour value a ``source`` observation recorded, in order."""
        return [r[0] for r in self._rows(
            "SELECT five_hour_percent FROM weekly_usage_snapshots "
            "WHERE account_key = ? AND source = ? AND five_hour_percent IS NOT NULL "
            "ORDER BY id", (account or self.account, source))]

    def five_credits(self, account=None) -> list:
        """Every five-hour credit of the account: (prior, post, effective)."""
        return self._rows(
            "SELECT prior_percent, post_percent, effective_reset_at_utc "
            "FROM five_hour_reset_events WHERE account_key = ? "
            "ORDER BY effective_reset_at_utc, detected_at_utc",
            (account or self.account,))

    def five_state(self, source: str = "statusline", *, account=None):
        """The ``source``'s five-hour confirmation state: (baseline, pending)."""
        rows = self._rows(
            "SELECT baseline_pct, pending_low_pct FROM five_hour_credit_confirmation_state "
            "WHERE account_key = ? AND source = ?", (account or self.account, source))
        return rows[0] if rows else None

    def observations(self, source: str = "statusline") -> list:
        """Every ``source`` observation's raw policy stamps, in journal order:
        (five-hour percent, fold policy, detector policy)."""
        out = []
        for rec in T.journal_records():
            payload = rec.get("payload") or {}
            if rec.get("t") == "obs" and payload.get("source") == source:
                out.append((payload.get("five_hour_percent"),
                            payload.get("fold_policy"), payload.get("detector_policy")))
        return out

    def token(self, session: Session) -> str:
        """The session's contributor token (`_candidate_identity_token`)."""
        import hashlib
        return hashlib.sha256(f"session_id\0{session.name}".encode()).hexdigest()

    def milestones(self, account=None) -> list:
        """The account's weekly percent milestones: (week start, threshold)."""
        return self._rows(
            "SELECT week_start_date, percent_threshold FROM percent_milestones "
            "WHERE account_key = ? ORDER BY id", (account or self.account,))

    def milestone_rows(self, account=None) -> list:
        """The account's weekly milestones with their segment and cost:
        (threshold, reset event id, cumulative cost), in order."""
        return self._rows(
            "SELECT percent_threshold, reset_event_id, cumulative_cost_usd "
            "FROM percent_milestones WHERE account_key = ? ORDER BY id",
            (account or self.account,))

    def resets(self, account=None) -> list:
        """Every weekly credit of the account: (old end, new end, pre-credit %)."""
        return self._rows(
            "SELECT old_week_end_at, new_week_end_at, observed_pre_credit_pct "
            "FROM week_reset_events WHERE account_key = ? ORDER BY id",
            (account or self.account,))

    def floors(self) -> list:
        return self._rows(
            "SELECT effective_at_utc, observed_pre_credit_pct FROM weekly_credit_floors "
            "WHERE account_key = ? ORDER BY id", (self.account,))

    def quarantined(self) -> bool:
        return T.account_quarantined(self.app, self.account_name)

    def outcomes(self) -> dict:
        result = T.outcomes(self.app)
        result["milestones"] = sorted(json.dumps(list(r)) for r in self._rows(
            "SELECT week_start_date, percent_threshold, account_key "
            "FROM percent_milestones"))
        return result

    def maintenance(self) -> dict:
        """Live, rebuilt and rederived outcomes (`db rebuild`, then `db
        rederive`), each as a multiset."""
        live = self.outcomes()
        T.rebuild(self.app)
        rebuilt = self.outcomes()
        T.rederive(self.app)
        rederived = self.outcomes()
        return {"live": live, "rebuilt": rebuilt, "rederived": rederived}


def counter(outcomes: dict) -> collections.Counter:
    """One store's outcomes as a multiset of (category, payload)."""
    return collections.Counter(
        (category, item) for category, items in outcomes.items() for item in items)


def assert_same_outcomes(live: dict, rebuilt: dict, rederived: dict) -> None:
    """Live, rebuilt and rederived stores hold the same outcome multiset."""
    a, b, c = counter(live), counter(rebuilt), counter(rederived)
    assert a == b, ("live != rebuilt", a - b, b - a)
    assert a == c, ("live != rederived", a - c, c - a)


def at(base: str, seconds: int) -> str:
    """``base`` moved by ``seconds``."""
    return iso(epoch(base) + seconds)


# --- mutation controls (spec section 6: each must make a preservation
# control fail) --------------------------------------------------------------


def mutate(monkeypatch, *names: str) -> None:
    """Install the named mutations of the refusal rules (several compose:
    a pair weakens two conjuncts at once). Each covers both axes."""
    for name in names:
        _mutate(monkeypatch, name)


def _mutate(monkeypatch, name: str) -> None:
    import _cctally_statusline as sl
    import _lib_oauth_samples as samples_lib
    import _lib_stale_proof as sp
    import dataclasses

    if name == "domination-alone":
        # Refusal on proof alone, without a receipt (the drop facts, #1019,
        # are kept as the real glue computes them).
        real_facts = sl._stale_member_facts

        def facts(stale, receipts, candidates):
            if stale is None:
                return None
            out = {axis: dict(f) for axis, f in
                   (real_facts(stale, receipts, candidates) or {}).items()}
            for axis, window, samples, _credits, _supports in sl._stale_axes(stale):
                if window is None:
                    continue
                members = [m for m in sl._axis_members(candidates, axis)
                           if m.sigma.window == window]
                for m in members:
                    p = sp.proven_stale(m.sigma, samples,
                                        after_utc=m.sigma.activity_at_utc)
                    if p is not None:
                        merged = out.setdefault(axis, {})
                        merged[m.sigma.token] = dataclasses.replace(
                            merged.get(m.sigma.token, sp.ALL_ELIGIBLE), refused=True,
                            proof_observation_id=p.observation_id, receipt_valid=False)
            return {a: f for a, f in out.items() if f} or None
        monkeypatch.setattr(sl, "_stale_member_facts", facts)
    elif name == "numerical-domination":
        # A receipt earned because another value dominates the member.
        real = sp.earn_receipts

        def earn(members, accounted, samples, **kwargs):
            earned = list(real(members, accounted, samples, **kwargs))
            have = {r.sigma.key() for r in earned}
            if accounted.percent is not None:
                lifted = dataclasses.replace(accounted)
                for m in members:
                    if m.sigma.key() not in have and m.sigma.percent < accounted.percent:
                        earned.extend(real([m], dataclasses.replace(
                            lifted, percent=m.sigma.percent), (), **kwargs))
            return tuple(earned)
        monkeypatch.setattr(sp, "earn_receipts", earn)
    elif name in ("capture-instant", "statusline-sample"):
        real = sl._read_stale_inputs

        def read(conn, account_key, **kwargs):
            stale = real(conn, account_key, **kwargs)
            if stale.window is None and stale.five_window is None:
                return stale
            if name == "capture-instant":
                def q_of(captured):
                    return samples_lib.canonical_instant(captured)
                rows = conn.execute(
                    "SELECT observation_id, captured_at_utc, requested_at_utc "
                    "FROM claude_oauth_samples WHERE account_key = ?",
                    (account_key,)).fetchall()
                captured = {row[0]: q_of(row[1]) for row in rows}
                by_q = {row[2]: q_of(row[1]) for row in rows}
                samples = tuple(dataclasses.replace(
                    s, requested_at_utc=q_of(s.captured_at_utc)) for s in stale.samples)
                five_samples = tuple(dataclasses.replace(
                    s, requested_at_utc=q_of(s.captured_at_utc))
                    for s in stale.five_samples)
                credits = tuple(
                    dataclasses.replace(c, requested_at_utc=captured.get(
                        c.event_id.split(":origin:", 1)[-1], c.requested_at_utc))
                    if c.causal_kind == "provider" else c for c in stale.credits)
                five_credits = tuple(
                    dataclasses.replace(
                        c, requested_at_utc=by_q.get(c.requested_at_utc, c.requested_at_utc),
                        baseline_requested_at_utc=by_q.get(
                            c.baseline_requested_at_utc, c.baseline_requested_at_utc),
                        arming_requested_at_utc=by_q.get(
                            c.arming_requested_at_utc, c.arming_requested_at_utc))
                    if c.causal_kind == "provider" else c for c in stale.five_credits)
                accounted = (None if stale.accounted is None
                             else dataclasses.replace(stale.accounted, credits=credits))
                five_accounted = (None if stale.five_accounted is None
                                  else dataclasses.replace(stale.five_accounted,
                                                           credits=five_credits))
                return dataclasses.replace(
                    stale, samples=samples, credits=credits, accounted=accounted,
                    five_samples=five_samples, five_credits=five_credits,
                    five_accounted=five_accounted)
            rows = conn.execute(
                "SELECT id, weekly_percent, captured_at_utc, week_end_at, "
                "five_hour_percent, five_hour_resets_at, five_hour_window_key "
                "FROM weekly_usage_snapshots WHERE account_key = ? "
                "AND source = 'statusline'", (account_key,)).fetchall()
            extra = tuple(samples_lib.OAuthSample(
                account_key, f"row:{r[0]}", samples_lib.canonical_instant(r[2]),
                r[2], float(r[1]), r[3], None, None, None) for r in rows)
            five_extra = tuple(samples_lib.OAuthSample(
                account_key, f"row:{r[0]}", samples_lib.canonical_instant(r[2]),
                r[2], float(r[1]), r[3], float(r[4]), r[5],
                None if r[6] is None else int(r[6]))
                for r in rows if r[4] is not None and r[5] is not None)
            return dataclasses.replace(stale, samples=stale.samples + extra,
                                       five_samples=stale.five_samples + five_extra)
        monkeypatch.setattr(sl, "_read_stale_inputs", read)
    elif name in ("drop-bound-ignored", "drop-no-tolerance"):
        # #1019: a higher sample proves a low regardless of request order
        # (`drop-bound-ignored`), or any higher sample does, without the
        # 1 pp tolerance (`drop-no-tolerance`).
        def low(bound_utc, samples, *, axis, window, percent,
                advertised_reset_epoch=None):
            if axis not in sp.AXES or sp._interval(bound_utc) is None:
                return None
            reset = int(window) if advertised_reset_epoch is None else int(
                advertised_reset_epoch)
            margin = 0.0 if name == "drop-no-tolerance" else sp.TOLERANCE_PP
            return sp._latest([
                s for s in sp._axis_window(samples, axis, window)
                if sp._axis_value(s, axis) > float(percent) + margin
                and (name == "drop-bound-ignored"
                     or sp.after(s.requested_at_utc, bound_utc))
                and sp._before_epoch(s.captured_at_utc, reset)])
        monkeypatch.setattr(sp, "proven_stale_low", low)
        if name == "drop-bound-ignored":
            # ...and the partition reads every sample of the window, not
            # only those requested after its earliest bound.
            real_scope = sl._stale_scope

            def scope(account_key, candidates, transition):
                _after, receipts_held, work = real_scope(account_key, candidates,
                                                         transition)
                return sl._EVERY_SAMPLE, receipts_held, work
            monkeypatch.setattr(sl, "_stale_scope", scope)
    elif name == "drop-received-at-as-sigma":
        # #1019: the received_at fallback treated as a known t_m, so a
        # member without supported activity enters the sigma-based (rise
        # refusal and receipt) paths.
        real_members = sl._axis_members

        def members(candidates, axis):
            out = list(real_members(candidates, axis))
            have = {m.sigma.token for m in out}
            revision = sorted(sp.SUPPORTED_REVISIONS)[0]
            for candidate in candidates:
                value = sl._candidates._candidate_axis(candidate, axis)
                if value is None or candidate.token in have:
                    continue
                window = sl._member_window(value, axis)
                bound = sp.drop_bound(candidate.token, None, int(candidate.received_at),
                                      axis=axis, percent=value.percent, window=window)
                out.append(sp.Member(sp.SampleIdentity(
                    candidate.token, bound, axis, float(value.percent), window,
                    *revision), int(value.raw_resets_at)))
            return out
        monkeypatch.setattr(sl, "_axis_members", members)
    elif name == "samples-cross-account":
        # #1019 (A3): the provider-sample read ignores the account, so
        # another account's samples of the same window decide this
        # account's members (both axes, both refusals).
        import _cctally_oauth_samples as projection
        import sqlite3
        real_read = projection.read_samples

        def read_any(conn, *, account_key, **kwargs):
            try:
                keys = [row[0] for row in conn.execute(
                    "SELECT DISTINCT account_key FROM claude_oauth_samples")]
            except sqlite3.Error:
                keys = []
            merged = {}
            for key in dict.fromkeys([account_key, *keys]):
                for s in real_read(conn, account_key=key, **kwargs):
                    merged.setdefault(s.observation_id,
                                      dataclasses.replace(s, account_key=account_key))
            return tuple(sorted(merged.values(),
                                key=lambda s: (s.requested_at_utc, s.observation_id)))
        monkeypatch.setattr(projection, "read_samples", read_any)
    elif name == "drop-refused-may-rise":
        # #1019 (P-R2): a drop-refused member stays rise-eligible.
        lib = sys.modules["_lib_statusline_candidates"]
        monkeypatch.setattr(lib, "rise_ineligible_tokens", lambda member_facts: frozenset(
            token for token, facts in (member_facts or {}).items() if facts.refused))
    elif name == "provider-floor-before-receipt":
        # A receipt kept across a provider credit requested before e_sigma.
        real = sp.receipt_valid

        def valid(receipt, transitions, **kwargs):
            return real(receipt, [t for t in transitions
                                  if t.causal_kind != "provider"
                                  or t.event_id in dict(receipt.credits)], **kwargs)
        monkeypatch.setattr(sp, "receipt_valid", valid)
    else:  # pragma: no cover - a test typo
        raise ValueError(name)


#: The receipt-based rise refusal's mutations (#797).
RECEIPT_MUTATIONS = ("domination-alone", "numerical-domination", "capture-instant",
                     "statusline-sample", "provider-floor-before-receipt")
#: The drop refusal's mutations (#1019 spec 6 L12).
DROP_MUTATIONS = ("drop-bound-ignored", "drop-no-tolerance",
                  "drop-received-at-as-sigma", "drop-refused-may-rise",
                  "samples-cross-account")
MUTATIONS = RECEIPT_MUTATIONS + DROP_MUTATIONS
