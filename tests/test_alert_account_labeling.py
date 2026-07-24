"""Account dimension on alerts (#341 Task 3, spec §6): the UNCONDITIONAL 8th
`alerts.log` tab field (`account_key`; `*` for vendor-wide rows) proven with
RAW-BYTE reads, and the R8 `[<label>]` notification-title prefix that appears
ONLY when the vendor has more than one real account.

The log is runtime state (exempt from R8), so the 8th field is unconditional;
the title prefix IS gated by R8. Isolation via load_isolated_cctally_module so
LOG_DIR + DB_PATH + CLAUDE_JSON_PATH point at the per-test tmp dir.
"""
from __future__ import annotations

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _acc(provider, natural):
    import _lib_accounts
    return _lib_accounts.account_key(provider, natural)


def _seed_claude(observes):
    import _cctally_journal as jr
    import _lib_journal as lj
    for kw in observes:
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index()


def _dispatch(cc, payload, sink):
    import _cctally_alerts
    return _cctally_alerts._dispatch_alert_notification(
        payload,
        popen_factory=(lambda args, **k: sink.append(list(args))),
        mode="real", platform="linux",
        which_on_path=lambda n: n == "notify-send",
    )


def _log_bytes():
    return (_cctally_core.LOG_DIR / "alerts.log").read_bytes()


# --------------------------------------------------------------------------
# raw-byte 8th field
# --------------------------------------------------------------------------

def test_eighth_field_is_account_key_raw_bytes(cc):
    cc.open_db().close()  # materialize the stats schema (accounts table)
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key="deadbeefdeadbeef",
    )
    sink = []
    assert _dispatch(cc, payload, sink) == "queued"
    raw = _log_bytes()
    assert raw.endswith(b"\n")
    fields = raw.decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8                       # 7 -> 8 field evolution
    assert fields[1] == "weekly"                  # axis
    assert fields[6] == "info"                    # severity (7th)
    assert fields[7] == "deadbeefdeadbeef"        # 8th = account_key


def test_eighth_field_star_for_vendor_wide_budget(cc):
    cc.open_db().close()
    payload = cc._build_alert_payload_budget(
        threshold=90, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_at="2026-07-01T00:00:00Z", budget_usd=100.0,
        spent_usd=90.0, consumption_pct=90.0,
    )
    sink = []
    _dispatch(cc, payload, sink)
    fields = _log_bytes().decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8
    assert fields[7] == "*"                        # vendor-wide sentinel


def test_eighth_field_present_for_quota(cc):
    cc.open_db().close()
    payload = cc._build_alert_payload_quota(
        source="codex", source_root_key="root-a", logical_limit_key="primary",
        observed_slot="primary", window_minutes=300,
        resets_at_utc="2026-07-15T15:00:00+00:00", threshold=95, kind="actual",
        crossed_at_utc="2026-07-01T00:00:00Z", qualifying_percent=95.0,
        projected_percent=None, account_key="codexkey1234",
    )
    sink = []
    _dispatch(cc, payload, sink)
    fields = _log_bytes().decode("utf-8").rstrip("\n").split("\t")
    assert len(fields) == 8
    assert fields[7] == "codexkey1234"


# --------------------------------------------------------------------------
# R8 [label] title prefix
# --------------------------------------------------------------------------

def test_label_prefix_when_multi_real_account(cc):
    ka = _acc("claude", "uuid-a")
    kb = _acc("claude", "uuid-b")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ])
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key=ka,
    )
    sink = []
    _dispatch(cc, payload, sink)
    joined = " ".join(sink[0])
    assert "[alice]" in joined            # title carries the label prefix


def test_no_prefix_when_single_real_account_R8(cc):
    ka = _acc("claude", "uuid-solo")
    _seed_claude([
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="solo@x.com", label="solo", label_source="auto"),
        # a legacy unattributed bucket must NOT trigger decoration (R8)
        dict(at="2026-07-01T00:00:00Z", account_key="unattributed",
             provider="claude", label_source="auto"),
    ])
    payload = cc._build_alert_payload_weekly(
        threshold=60, crossed_at_utc="2026-07-01T00:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=1.0,
        dollars_per_percent=0.01, account_key=ka,
    )
    sink = []
    _dispatch(cc, payload, sink)
    joined = " ".join(sink[0])
    assert "[solo]" not in joined
    # byte-identical title to the pre-#341 render (no bracket prefix at all)
    assert "[" not in joined.split("cctally")[0]
