"""`POST /api/settings` classifies every submitted path (#513 S1, spec §1.3-§1.7).

One rule: each submitted terminal path is looked up in
``SETTINGS_LEAF_DISPOSITIONS``. Writable leaves are validated and merged,
known-ignored leaves are accepted and disclosed via ``ignored_fields``, and
anything else is a 400 carrying that dotted path as ``field``.

The emptiness rule operates on BLOCKS, not leaves: a request must name at
least one known block, but a named block carrying no leaves is an ordinary
partial-PUT no-op. ``{"cache_report": {}}`` is what a combined save sends
when the user never opened that tab, so turning it into a 400 would break a
real client path. Every expected value below was recorded against the
pre-change tree in ``docs/superpowers/plans/513-s1-baseline.md``.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire_handlers(ns):
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].no_sync = False
    ns["DashboardHTTPHandler"].display_tz_pref_override = None


def _post_json(host, port, body):
    c = http.client.HTTPConnection(host, port, timeout=5)
    raw = json.dumps(body).encode()
    host_header = f"{host}:{port}"
    c.putrequest("POST", "/api/settings", skip_host=True,
                 skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", host_header)
    c.putheader("Origin", f"http://{host_header}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    payload = r.read().decode("utf-8", errors="replace")
    c.close()
    try:
        return r.status, json.loads(payload)
    except json.JSONDecodeError:
        return r.status, None


#: A stored Codex budget. Without it the fail-closed guard answers 400 before
#: execution reaches the merge, which is what would make an F5 assertion
#: vacuous -- see the Task 1 baseline record.
CODEX_SEED = {
    "budget": {
        "weekly_usd": 100,
        "codex": {"amount_usd": 50, "period": "calendar-month"},
    }
}


@pytest.fixture
def post(monkeypatch, tmp_path):
    """Return a `post(body, seed=None) -> (status, body, config)` helper."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)

    def _do(body, seed=None):
        if seed is not None:
            ns["CONFIG_PATH"].write_text(json.dumps(seed))
        srv, _t, port = _serve(ns)
        try:
            status, out = _post_json("127.0.0.1", port, body)
        finally:
            srv.shutdown()
        try:
            cfg = json.loads(ns["CONFIG_PATH"].read_text())
        except (OSError, json.JSONDecodeError):
            cfg = None
        return status, out, cfg

    return _do


# --- Unknown leaves are rejected, and name themselves --------------------

UNKNOWN_LEAF_CASES = [
    ("alerts.quota", {"alerts": {"quota": {"enabled": True}}}, None),
    ("alerts.bogus", {"alerts": {"bogus": 1}}, None),
    ("budget.projects", {"budget": {"projects": {"/x": 5}}}, None),
    ("budget.accounts", {"budget": {"accounts": {"k": 5}}}, None),
    (
        "budget.codex.accounts",
        {"budget": {"codex": {"accounts": {"k": 5}}}},
        CODEX_SEED,
    ),
    ("budget.bogus", {"budget": {"bogus": 1}}, None),
    ("display.bogus", {"display": {"tz": "utc", "bogus": 1}}, None),
    ("cache_report.anomaly_window_days",
     {"cache_report": {"anomaly_window_days": 14}}, None),
    ("update.check.banana", {"update": {"check": {"banana": 1}}}, None),
    ("update.banner", {"update": {"banner": {"enabled": True}}}, None),
    ("dashboard.banana", {"dashboard": {"banana": 1}}, None),
]


@pytest.mark.parametrize(
    "field,body,seed", UNKNOWN_LEAF_CASES, ids=[c[0] for c in UNKNOWN_LEAF_CASES]
)
def test_unknown_leaf_is_rejected_and_names_itself(post, field, body, seed):
    status, out, cfg = post(body, seed=seed)
    assert status == 400, out
    assert out["field"] == field
    # No partial write: a rejected request persists nothing new.
    assert cfg == seed


def test_quota_and_projects_no_longer_report_success(post):
    """The F6 defect: a write the endpoint discards answered 200.

    Baseline recorded 200-and-discard for both of these.
    """
    for body in ({"alerts": {"quota": {}}}, {"budget": {"projects": {}}}):
        status, out, _cfg = post(body)
        assert status == 400, (body, out)


# --- Purpose-written rejections keep their message, gain a pointer -------

def test_command_template_keeps_its_purpose_written_message(post):
    status, out, _cfg = post({"alerts": {"command_template": ["x"]}})
    assert status == 400
    assert out["error"] == (
        "alerts.command_template is CLI/config-only "
        "(not settable via the dashboard)"
    )
    assert out["field"] == "alerts.command_template"


@pytest.mark.parametrize("leaf", ["bind", "expose_transcripts"])
def test_dashboard_bind_time_leaves_keep_their_message(post, leaf):
    status, out, _cfg = post({"dashboard": {leaf: "lan"}})
    assert status == 400
    assert "not settable via the dashboard" in out["error"]
    assert out["field"] == f"dashboard.{leaf}"


# --- F5: one boolean rule ------------------------------------------------

def test_codex_alerts_enabled_string_is_rejected_like_its_sibling(post):
    """Baseline answered 200 here and persisted true, because the merge
    coerced with bool(). The sibling budget.alerts_enabled already answered
    400. They must now answer identically."""
    status, out, cfg = post(
        {"budget": {"codex": {"alerts_enabled": "yes"}}}, seed=CODEX_SEED
    )
    assert status == 400, out
    assert out["field"] == "budget.codex.alerts_enabled"
    # The persisted config is semantically unchanged -- no coerced true.
    assert cfg == CODEX_SEED


def test_codex_and_claude_boolean_rejections_agree(post):
    codex_status, codex_out, _ = post(
        {"budget": {"codex": {"projected_enabled": 1}}}, seed=CODEX_SEED
    )
    claude_status, claude_out, _ = post({"budget": {"projected_enabled": 1}})
    assert codex_status == claude_status == 400
    assert codex_out["field"] == "budget.codex.projected_enabled"
    assert claude_out["field"] == "budget.projected_enabled"


def test_codex_booleans_still_round_trip_when_actually_boolean(post):
    status, out, cfg = post(
        {"budget": {"codex": {"alerts_enabled": True}}}, seed=CODEX_SEED
    )
    assert status == 200, out
    assert cfg["budget"]["codex"]["alerts_enabled"] is True


# --- Ordering: classify before the fail-closed prerequisite --------------

def test_unknown_codex_leaf_reports_the_leaf_not_the_prerequisite(post):
    status, out, _cfg = post({"budget": {"codex": {"bogus": 1}}})
    assert status == 400
    assert out["field"] == "budget.codex.bogus"
    assert "no Codex budget configured" not in out["error"]


def test_fail_closed_still_fires_when_every_leaf_is_known(post):
    status, out, _cfg = post({"budget": {"codex": {"alerts_enabled": True}}})
    assert status == 400
    assert "no Codex budget configured" in out["error"]
    assert out["field"] == "budget.codex"


# --- ignored_fields ------------------------------------------------------

def test_ignored_fields_is_sorted_exact_and_persists_nothing(post):
    """One leaf could not prove sorting and would never reach the Codex
    ignored leaves, so this sends all four at once."""
    status, out, cfg = post(
        {"budget": {
            "period": "calendar-week",
            "codex": {
                "period": "calendar-week",
                "amount_usd": 999,
                "alert_thresholds": [10],
            },
        }},
        seed=CODEX_SEED,
    )
    assert status == 200, out
    assert out["ignored_fields"] == [
        "budget.codex.alert_thresholds",
        "budget.codex.amount_usd",
        "budget.codex.period",
        "budget.period",
    ]
    # Every corresponding persisted value is untouched.
    assert cfg["budget"]["codex"]["amount_usd"] == 50
    assert cfg["budget"]["codex"]["period"] == "calendar-month"
    assert "alert_thresholds" not in cfg["budget"]["codex"]
    assert "period" not in cfg["budget"]


def test_budget_period_alone_is_disclosed_and_still_answers_200(post):
    status, out, cfg = post(
        {"budget": {"period": "calendar-month"}},
        seed={"budget": {"weekly_usd": 100}},
    )
    assert status == 200, out
    assert out["ignored_fields"] == ["budget.period"]
    assert "period" not in cfg["budget"]


def test_ordinary_write_carries_no_ignored_fields_key(post):
    """Absence is what keeps the field additive for existing clients."""
    status, out, _cfg = post({"display": {"tz": "utc"}})
    assert status == 200, out
    assert "ignored_fields" not in out


def test_mixed_write_discloses_only_the_ignored_leaf(post):
    status, out, cfg = post(
        {"budget": {"period": "calendar-week", "weekly_usd": 42}},
    )
    assert status == 200, out
    assert out["ignored_fields"] == ["budget.period"]
    assert cfg["budget"]["weekly_usd"] == 42


# --- The emptiness rule operates on blocks ------------------------------

def test_no_known_block_is_still_rejected(post):
    status, out, _cfg = post({})
    assert status == 400
    assert out["error"] == (
        "body must contain at least one of: "
        "display, alerts, update, cache_report, budget, dashboard"
    )
    assert out["field"] == "$"


def test_display_is_the_one_block_that_requires_a_leaf(post):
    status, out, _cfg = post({"display": {}})
    assert status == 400
    assert out["error"] == "missing display.tz"
    assert out["field"] == "display.tz"


EMPTY_BLOCK_ECHOES = [
    ("alerts", {"enabled", "weekly_thresholds", "five_hour_thresholds",
                "projected_enabled", "notifier", "command_configured"}),
    ("budget", {"weekly_usd", "alerts_enabled", "alert_thresholds",
                "projected_enabled", "period", "projects",
                "project_alerts_enabled", "accounts", "codex"}),
    ("dashboard", {"cache_failure_markers", "live_tail", "lan_auth"}),
    ("update", {"check"}),
    ("cache_report", {"anomaly_threshold_pp"}),
]


@pytest.mark.parametrize(
    "block,echo_keys", EMPTY_BLOCK_ECHOES, ids=[c[0] for c in EMPTY_BLOCK_ECHOES]
)
def test_named_but_empty_block_is_a_200_no_op(post, block, echo_keys):
    """Recorded against the pre-change tree: every one of these answers 200
    WITH its full cooked echo. The echo is driven by block presence, so
    "no-op" means "writes no leaf", not "returns nothing"."""
    status, out, _cfg = post({block: {}})
    assert status == 200, out
    assert set(out[block]) == echo_keys
    assert "ignored_fields" not in out


def test_empty_cache_report_block_preserves_a_persisted_threshold(post):
    """The pinned combined-save no-op: the tab was never opened, so the
    request carries the block with no leaves and must not clobber."""
    status, out, cfg = post(
        {"cache_report": {}}, seed={"cache_report": {"anomaly_threshold_pp": 42}}
    )
    assert status == 200, out
    assert out["cache_report"] == {"anomaly_threshold_pp": 42}
    assert cfg["cache_report"]["anomaly_threshold_pp"] == 42


# --- Every 400 carries a field pointer ----------------------------------

FIELD_POINTER_CASES = [
    ("foo", {"foo": {"bar": 1}}, None),
    ("alerts", {"alerts": "bad"}, None),
    ("budget", {"budget": "bad"}, None),
    ("update", {"update": "bad"}, None),
    ("update.check", {"update": {"check": "bad"}}, None),
    ("cache_report", {"cache_report": "bad"}, None),
    ("dashboard", {"dashboard": "bad"}, None),
    ("budget.codex", {"budget": {"codex": "bad"}}, CODEX_SEED),
    ("alerts.notifier", {"alerts": {"notifier": "nope"}}, None),
    ("alerts.enabled", {"alerts": {"enabled": "yes"}}, None),
    ("budget.alerts_enabled", {"budget": {"alerts_enabled": "yes"}}, None),
    ("display.tz", {"display": {"tz": "Mars/Olympus"}}, None),
    ("update.check.ttl_hours", {"update": {"check": {"ttl_hours": 0}}}, None),
    ("update.channel", {"update": {"channel": "nightly"}}, None),
    (
        "cache_report.anomaly_threshold_pp",
        {"cache_report": {"anomaly_threshold_pp": -1}},
        None,
    ),
    (
        "dashboard.cache_failure_markers",
        {"dashboard": {"cache_failure_markers": "yes"}},
        None,
    ),
]


@pytest.mark.parametrize(
    "field,body,seed", FIELD_POINTER_CASES,
    ids=[c[0] + "-" + str(i) for i, c in enumerate(FIELD_POINTER_CASES)],
)
def test_every_rejection_names_a_field(post, field, body, seed):
    status, out, _cfg = post(body, seed=seed)
    assert status == 400, out
    assert out["field"] == field


# --- The endpoint reads the contract, it does not restate it ------------

def test_block_order_names_exactly_the_contract_blocks():
    """The 'at least one of' message is built from an ordered tuple because
    a frozenset has no order. That tuple must not drift from the contract."""
    dash = sys.modules.get("_cctally_dashboard") or (
        load_script() and sys.modules["_cctally_dashboard"]
    )
    assert set(dash._SETTINGS_BLOCK_ORDER) == dash.SETTINGS_TOP_LEVEL_BLOCKS


def test_cache_report_allowed_keys_are_derived_from_the_contract():
    """Two assertions, because either one alone certifies too little.

    Recomputing the implementation's own comprehension only proves the two
    expressions read the same, so it fails on textual divergence and passes on
    a wrong value they happen to share. The literal is what pins the value; the
    recomputation is what pins the derivation.
    """
    load_script()
    crm = sys.modules["_cctally_dashboard_cache_report"]
    contract = sys.modules["_lib_dashboard_settings_contract"]
    assert crm._CACHE_REPORT_ALLOWED_KEYS == frozenset({"anomaly_threshold_pp"})
    assert crm._CACHE_REPORT_ALLOWED_KEYS == frozenset(
        path.split(".", 1)[1]
        for path, disposition in contract.SETTINGS_LEAF_DISPOSITIONS.items()
        if path.startswith("cache_report.")
        and disposition == contract.WRITABLE
    )


def test_malformed_stored_block_rejection_also_names_its_field(post):
    status, out, _cfg = post({"alerts": {"enabled": True}}, seed={"alerts": "bad"})
    assert status == 400, out
    assert out["error"] == "alerts must be an object"
    assert out["field"] == "alerts"
