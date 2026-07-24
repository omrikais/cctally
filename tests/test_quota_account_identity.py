"""Account-scoped Codex quota identity, continuity fold, and per-account hero cycles (#341 Task 2).

Covers:
  * ``QuotaWindowIdentity.account_key`` participating in eq/hash — two accounts
    sharing one physical window key never merge (Step 3, spec §2 never-combine).
  * The window-account continuity fold ``adopt_unidentified_observations`` —
    identified observations authoritative/never-reassigned; unidentified adopted
    iff exactly one identified account; zero/ambiguous stay unattributed (Step 4,
    spec §2 rev 4 + acceptance 4).
  * ``(source_root_key, account_key)`` projection state / arming / qaa-id
    rebuild-verbatim determinism (Step 3).
  * ``_resolve_codex_weekly_cycle`` per-account list — three simultaneous
    account cycles no longer degrade to ``CodexCycleUnavailable("conflicting")``;
    single-account byte-stable (Step 6, spec §4).
"""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_accounts as accts  # noqa: E402
import _lib_quota as quota  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
RESET = dt.datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
ACCOUNT_A = accts.account_key("codex", "acct-a\0a@example.com")
ACCOUNT_B = accts.account_key("codex", "acct-b\0b@example.com")


# ---------------------------------------------------------------------------
# pure-kernel identity + continuity fold (Steps 3 & 4)
# ---------------------------------------------------------------------------

def _identity(*, account_key=accts.UNATTRIBUTED, root="root-a", limit="limit-primary",
              slot="primary", window_minutes=300):
    return quota.QuotaWindowIdentity(
        source="codex", source_root_key=root, account_key=account_key,
        logical_limit_key=limit, observed_slot=slot, window_minutes=window_minutes,
        limit_id="native-primary", limit_name="Primary",
    )


def _obs(identity, *, used=10.0, captured=None, resets=RESET, path="/codex/root-a/r.jsonl",
         offset=0):
    return quota.QuotaObservation(
        identity=identity,
        captured_at=captured or (NOW - dt.timedelta(minutes=30)),
        used_percent=used, resets_at=resets, source_path=path, line_offset=offset,
    )


def test_identity_account_key_participates_in_eq_and_hash():
    a = _identity(account_key=ACCOUNT_A)
    b = _identity(account_key=ACCOUNT_B)
    assert a != b
    assert hash(a) != hash(b) or a != b  # distinct identities never collapse
    assert len({a, b}) == 2


def test_identity_rejects_empty_account_key():
    with pytest.raises(ValueError):
        _identity(account_key="")


def test_adopt_leaves_two_identified_accounts_separate():
    # never-combine: two identified accounts share one physical window key.
    a = _obs(_identity(account_key=ACCOUNT_A), path="/codex/root-a/a.jsonl")
    b = _obs(_identity(account_key=ACCOUNT_B), path="/codex/root-a/b.jsonl")
    folded = quota.adopt_unidentified_observations([a, b])
    keys = sorted({o.identity.account_key for o in folded})
    assert keys == sorted([ACCOUNT_A, ACCOUNT_B])


def test_adopt_unidentified_when_exactly_one_identified():
    identified = _obs(_identity(account_key=ACCOUNT_A), path="/codex/root-a/live.jsonl",
                      captured=NOW - dt.timedelta(minutes=10))
    legacy = _obs(_identity(account_key=accts.UNATTRIBUTED), path="/codex/root-a/legacy.jsonl",
                  captured=NOW - dt.timedelta(minutes=50))
    folded = quota.adopt_unidentified_observations([identified, legacy])
    assert all(o.identity.account_key == ACCOUNT_A for o in folded)


def test_adopt_zero_identified_stays_unattributed():
    x = _obs(_identity(account_key=accts.UNATTRIBUTED), path="/codex/root-a/x.jsonl")
    y = _obs(_identity(account_key=accts.UNATTRIBUTED), path="/codex/root-a/y.jsonl", offset=1)
    folded = quota.adopt_unidentified_observations([x, y])
    assert all(o.identity.account_key == accts.UNATTRIBUTED for o in folded)


def test_adopt_ambiguous_two_identified_leaves_unattributed():
    a = _obs(_identity(account_key=ACCOUNT_A), path="/codex/root-a/a.jsonl")
    b = _obs(_identity(account_key=ACCOUNT_B), path="/codex/root-a/b.jsonl")
    legacy = _obs(_identity(account_key=accts.UNATTRIBUTED), path="/codex/root-a/legacy.jsonl")
    folded = quota.adopt_unidentified_observations([a, b, legacy])
    legacy_after = [o for o in folded if o.source_path.endswith("legacy.jsonl")]
    assert legacy_after and all(o.identity.account_key == accts.UNATTRIBUTED for o in legacy_after)
    # the identified accounts are untouched.
    assert {o.identity.account_key for o in folded} == {ACCOUNT_A, ACCOUNT_B, accts.UNATTRIBUTED}


def test_adopt_only_within_same_physical_window():
    # a distinct resets_at is a distinct physical window -> no cross adoption.
    identified = _obs(_identity(account_key=ACCOUNT_A), resets=RESET,
                      path="/codex/root-a/a.jsonl")
    other_window = _obs(_identity(account_key=accts.UNATTRIBUTED),
                        resets=RESET + dt.timedelta(days=7), path="/codex/root-a/o.jsonl")
    folded = quota.adopt_unidentified_observations([identified, other_window])
    other_after = [o for o in folded if o.source_path.endswith("o.jsonl")]
    assert other_after[0].identity.account_key == accts.UNATTRIBUTED


# ---------------------------------------------------------------------------
# DB integration: reconcile projection is account-scoped (Steps 3 & 4)
# ---------------------------------------------------------------------------

def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota_mod = importlib.import_module("_cctally_quota")
    return ns, quota_mod


def _seed_root(ns, root_key):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_root_key) DO UPDATE SET last_seen_utc=excluded.last_seen_utc""",
            (root_key, f"/codex/{root_key}", NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def _seed_quota_obs(ns, *, root_key, source_path, account_key, observations,
                    logical_limit_key="limit-primary", observed_slot="primary",
                    window_minutes=300, resets_at=RESET):
    conn = ns["open_cache_db"]()
    try:
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset, captured_at_utc,
                observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,
                used_percent, resets_at_utc, plan_type, individual_limit_json,
                reached_type, observed_model, account_key)
               VALUES ('codex', ?, ?, ?, ?, ?, ?, 'native-primary', 'Primary', ?, ?, ?,
                       'pro', NULL, NULL, NULL, ?)""",
            [
                (root_key, source_path, offset, captured, observed_slot,
                 logical_limit_key, window_minutes, used, resets_at.isoformat(),
                 account_key)
                for captured, offset, used in observations
            ],
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def _iso(hour, minute=0):
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


def test_two_accounts_one_root_produce_separate_blocks(tmp_path, monkeypatch):
    ns, quota_mod = _load(tmp_path, monkeypatch)
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/a.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(9), 0, 20.0), (_iso(11), 1, 62.0)])
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/b.jsonl",
                    account_key=ACCOUNT_B, observations=[(_iso(9), 0, 10.0), (_iso(11), 1, 55.0)])
    quota_mod.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_db"]()
    try:
        blocks = conn.execute(
            "SELECT account_key FROM quota_window_blocks ORDER BY account_key"
        ).fetchall()
    finally:
        conn.close()
    accounts = sorted({str(r[0]) for r in blocks})
    assert accounts == sorted([ACCOUNT_A, ACCOUNT_B]), (
        f"two accounts on one physical window must not merge: got {accounts}"
    )


def test_two_accounts_distinct_percent_milestones(tmp_path, monkeypatch):
    ns, quota_mod = _load(tmp_path, monkeypatch)
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/a.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(9), 0, 40.0), (_iso(11), 1, 61.0)])
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/b.jsonl",
                    account_key=ACCOUNT_B, observations=[(_iso(9), 0, 40.0), (_iso(11), 1, 61.0)])
    quota_mod.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT account_key, percent_threshold FROM quota_percent_milestones "
            "WHERE percent_threshold=61 ORDER BY account_key"
        ).fetchall()
    finally:
        conn.close()
    accounts = sorted({str(r[0]) for r in rows})
    assert accounts == sorted([ACCOUNT_A, ACCOUNT_B])


def test_projection_state_keyed_per_account(tmp_path, monkeypatch):
    ns, quota_mod = _load(tmp_path, monkeypatch)
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/a.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(11), 1, 62.0)])
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/b.jsonl",
                    account_key=ACCOUNT_B, observations=[(_iso(11), 1, 55.0)])
    quota_mod.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT source_root_key, account_key FROM quota_projection_state "
            "ORDER BY account_key"
        ).fetchall()
    finally:
        conn.close()
    pairs = sorted({(str(r[0]), str(r[1])) for r in rows})
    assert pairs == sorted([("root-a", ACCOUNT_A), ("root-a", ACCOUNT_B)])


def test_cutover_straddling_window_adopts_live_account(tmp_path, monkeypatch):
    # A window still open at cutover: pre-cutover unidentified obs + post-cutover
    # identified obs share one resets_at -> all fold under the live account.
    ns, quota_mod = _load(tmp_path, monkeypatch)
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/legacy.jsonl",
                    account_key=None, observations=[(_iso(9), 0, 20.0)])
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/live.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(11), 1, 62.0)])
    quota_mod.reconcile_codex_quota_projection(now=NOW)
    conn = ns["open_db"]()
    try:
        blocks = conn.execute(
            "SELECT DISTINCT account_key FROM quota_window_blocks"
        ).fetchall()
    finally:
        conn.close()
    accounts = sorted({str(r[0]) for r in blocks})
    assert accounts == [ACCOUNT_A], f"straddling window must adopt the live account: {accounts}"


# ---------------------------------------------------------------------------
# qaa arming id + rebuild-verbatim determinism (Step 3)
# ---------------------------------------------------------------------------

def _write_quota_config(ns, *, actual):
    import json as _json
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    core.CONFIG_PATH.write_text(_json.dumps({"alerts": {
        "enabled": True,
        "quota": {
            "enabled": True,
            "actual_thresholds": list(actual),
            "projected_thresholds": [],
            "rules": [],
        },
    }}) + "\n")


def test_qaa_arming_id_and_row_are_account_qualified(tmp_path, monkeypatch):
    ns, quota_mod = _load(tmp_path, monkeypatch)
    import _cctally_journal as jr
    import _lib_journal as jl
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/a.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(11), 1, 95.0)])
    _write_quota_config(ns, actual=(90,))
    quota_mod.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=NOW)
    # (a) the journaled qaa id carries the account after the root.
    lines = _journal_lines(jr, jl)
    arming = [l for l in lines if l.get("t") == "evt"
              and (l.get("payload") or {}).get("kind") == "quota_alert_arming"]
    assert len(arming) == 1
    assert arming[0]["id"] == f"qaa:codex:root-a:{ACCOUNT_A}:limit-primary:primary:300"
    assert arming[0]["payload"]["account_key"] == ACCOUNT_A
    # (b) the arming row is stamped with the account.
    conn = ns["open_db"]()
    try:
        live = conn.execute(
            "SELECT account_key, rule_fingerprint, activated_at_utc FROM quota_alert_arming"
        ).fetchall()
    finally:
        conn.close()
    assert len(live) == 1 and str(live[0][0]) == ACCOUNT_A


def test_qaa_arming_survives_rebuild_verbatim(tmp_path, monkeypatch):
    ns, quota_mod = _load(tmp_path, monkeypatch)
    import _cctally_journal as jr
    _seed_root(ns, "root-a")
    _seed_quota_obs(ns, root_key="root-a", source_path="/codex/root-a/a.jsonl",
                    account_key=ACCOUNT_A, observations=[(_iso(11), 1, 95.0)])
    _write_quota_config(ns, actual=(90,))
    quota_mod.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=NOW)
    conn = ns["open_db"]()
    try:
        live = conn.execute(
            "SELECT source, source_root_key, account_key, logical_limit_key, observed_slot, "
            "window_minutes, rule_fingerprint, activated_at_utc FROM quota_alert_arming"
        ).fetchall()
    finally:
        conn.close()
    # Rebuild the stats index from the journal and compare the arming row verbatim.
    jr.rebuild_stats_index()
    conn = ns["open_db"]()
    try:
        rebuilt = conn.execute(
            "SELECT source, source_root_key, account_key, logical_limit_key, observed_slot, "
            "window_minutes, rule_fingerprint, activated_at_utc FROM quota_alert_arming"
        ).fetchall()
    finally:
        conn.close()
    assert rebuilt == live, "rebuild must reproduce the account-qualified arming row verbatim"
    assert str(live[0][2]) == ACCOUNT_A


def _journal_lines(jr, jl):
    core = importlib.import_module("_cctally_core")
    out = []
    for seg in jr.list_segments():
        for raw in (core.JOURNAL_DIR / seg).read_bytes().splitlines():
            rec = jl.decode_line(raw)
            if rec is not None:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# per-account hero cycles (Step 6)
# ---------------------------------------------------------------------------

def _weekly_obs(account_key, *, root, resets_at, used=30.0):
    identity = quota.QuotaWindowIdentity(
        source="codex", source_root_key=root, account_key=account_key,
        logical_limit_key="weekly", observed_slot="primary", window_minutes=10_080,
        limit_id="native-weekly", limit_name="Weekly",
    )
    return quota.QuotaObservation(
        identity=identity, captured_at=NOW - dt.timedelta(minutes=30), used_percent=used,
        resets_at=resets_at, source_path=f"/codex/{root}/{account_key}.jsonl", line_offset=0,
    )


def test_resolve_weekly_cycle_returns_one_per_account():
    ds = importlib.import_module("_cctally_dashboard_sources")
    observations = [
        _weekly_obs(ACCOUNT_A, root="root-a", resets_at=NOW + dt.timedelta(days=2)),
        _weekly_obs(ACCOUNT_B, root="root-a", resets_at=NOW + dt.timedelta(days=4)),
    ]
    cycles = ds._resolve_codex_weekly_cycle(observations, NOW)
    assert isinstance(cycles, list)
    accounts = sorted({c.quota_identity.account_key for c in cycles})
    assert accounts == sorted([ACCOUNT_A, ACCOUNT_B])


def test_resolve_weekly_cycle_single_account_one_element():
    ds = importlib.import_module("_cctally_dashboard_sources")
    observations = [_weekly_obs(accts.UNATTRIBUTED, root="root-a",
                                resets_at=NOW + dt.timedelta(days=2))]
    cycles = ds._resolve_codex_weekly_cycle(observations, NOW)
    assert isinstance(cycles, list) and len(cycles) == 1
    assert cycles[0].quota_identity.account_key == accts.UNATTRIBUTED
