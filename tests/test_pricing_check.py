import importlib.util, pathlib, sys
import datetime as dt

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load(modname):
    spec = importlib.util.spec_from_file_location(modname, _BIN / f"{modname}.py")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclasses.dataclass can resolve cls.__module__
    # (matches the repo's other spec_from_file_location loaders).
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


pricing = _load("_lib_pricing")


def test_pricing_constants_present_and_wellformed():
    assert isinstance(pricing.PRICING_SNAPSHOT_DATE, str)
    # parses as ISO date
    dt.date.fromisoformat(pricing.PRICING_SNAPSHOT_DATE)
    assert isinstance(pricing.PRICING_STALENESS_DAYS, int) and pricing.PRICING_STALENESS_DAYS == 60
    assert pricing.LITELLM_PRICES_URL.startswith("https://")
    assert isinstance(pricing.PRICING_DRIFT_ALLOWLIST, list)
    for e in pricing.PRICING_DRIFT_ALLOWLIST:
        assert set(e) <= {"model", "field", "reason", "expires"}
        assert e["model"] and e["reason"]
        # Optional `expires` (#279 S7 W7) must parse as an ISO date.
        if "expires" in e:
            dt.date.fromisoformat(e["expires"])


pc = _load("_lib_pricing_check")

# Fakes mirroring the real predicates' contracts.
_CLAUDE_PRICED = {"claude-known"}


def _resolve_claude(m):  # returns dict|None
    return {"input_cost_per_token": 1e-6} if m in _CLAUDE_PRICED else None


_CODEX_PRICED = {"gpt-5", "gpt-5.3-codex"}


def _is_codex_fallback(m):  # True iff unknown
    return m not in _CODEX_PRICED


def test_classify_coverage_flags_unpriced_claude_and_codex_fallback():
    observed = [
        ("claude", "claude-known", 10, 1000),     # priced -> no gap
        ("claude", "claude-mystery", 3, 5000),     # unpriced -> gap
        ("codex", "gpt-5.3-codex", 4, 400),        # priced -> no gap
        ("codex", "gpt-7-brandnew", 2, 9000),      # fallback -> gap
    ]
    gaps = pc.classify_coverage(observed, _resolve_claude, _is_codex_fallback)
    by_model = {g.model: g for g in gaps}
    assert set(by_model) == {"claude-mystery", "gpt-7-brandnew"}
    assert by_model["claude-mystery"].kind == "unpriced"
    assert by_model["claude-mystery"].entry_count == 3
    assert by_model["claude-mystery"].token_total == 5000
    assert by_model["gpt-7-brandnew"].kind == "fallback"
    assert by_model["gpt-7-brandnew"].provider == "codex"


def test_classify_coverage_empty_when_all_priced():
    observed = [("claude", "claude-known", 1, 1), ("codex", "gpt-5", 1, 1)]
    assert pc.classify_coverage(observed, _resolve_claude, _is_codex_fallback) == []


def test_classify_coverage_ignores_codex_unattributed_model_sentinel():
    observed = [("codex", "unknown", 53, 2_485_412)]
    assert pc.classify_coverage(observed, _resolve_claude, _is_codex_fallback) == []


def test_scope_litellm_keeps_only_claude_and_codex_set():
    litellm = {
        "claude-3-5-haiku-latest": {"litellm_provider": "anthropic", "input_cost_per_token": 1e-6},
        "claude-opus-4-8":         {"litellm_provider": "anthropic", "input_cost_per_token": 5e-6},
        "gpt-5.4":                 {"litellm_provider": "openai",    "input_cost_per_token": 2.5e-6},
        "gpt-4o":                  {"litellm_provider": "openai",    "input_cost_per_token": 2.5e-6},  # decoy: not codex
        "gemini-2.0":              {"litellm_provider": "vertex_ai", "input_cost_per_token": 1e-6},     # decoy
        "sample_spec":             {"max_tokens": 1},  # decoy: no provider
    }
    scoped = pc.scope_litellm(litellm)
    assert "claude-3-5-haiku-latest" in scoped
    assert "claude-opus-4-8" in scoped
    assert "gpt-5.4" in scoped
    assert "gpt-4o" not in scoped       # openai but not a codex/gpt-5 model we track
    assert "gemini-2.0" not in scoped
    assert "sample_spec" not in scoped


_FIELDS = ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost")


def test_diff_pricing_categories_and_allowlist():
    claude = {
        "claude-a": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},  # matches
        "claude-b": {"input_cost_per_token": 2e-6},                                  # value drift
        "claude-lead": {"input_cost_per_token": 9e-6},                               # ahead of litellm
    }
    codex = {}
    litellm = {
        "claude-a": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},
        "claude-b": {"input_cost_per_token": 3e-6},        # theirs != ours -> drift
        "claude-new": {"input_cost_per_token": 7e-6},      # missing from us
    }
    res = pc.diff_pricing(claude, codex, litellm, allowlist=[])
    assert [r.model for r in res.value_drift] == ["claude-b"]
    drow = res.value_drift[0]
    assert drow.field == "input_cost_per_token" and drow.ours == 2e-6 and drow.theirs == 3e-6
    assert res.missing_from_us == ["claude-new"]
    assert res.ahead_of_litellm == ["claude-lead"]   # informational only


def test_diff_pricing_allowlist_suppresses_value_and_missing():
    claude = {"claude-b": {"input_cost_per_token": 2e-6}}
    litellm = {
        "claude-b": {"input_cost_per_token": 3e-6},
        "claude-new": {"input_cost_per_token": 7e-6},
    }
    allow = [
        {"model": "claude-b", "field": "input_cost_per_token", "reason": "deliberate"},
        {"model": "claude-new", "reason": "intentionally omitted"},
    ]
    res = pc.diff_pricing(claude, {}, litellm, allowlist=allow)
    assert res.value_drift == []
    assert res.missing_from_us == []


def test_stale_allowlist_entries_flags_resolved_divergence():
    claude = {"claude-b": {"input_cost_per_token": 3e-6}}  # now MATCHES litellm
    litellm = {"claude-b": {"input_cost_per_token": 3e-6}}
    allow = [{"model": "claude-b", "field": "input_cost_per_token", "reason": "was deliberate"}]
    stale = pc.stale_allowlist_entries(allow, claude, {}, litellm)
    assert len(stale) == 1 and stale[0]["model"] == "claude-b"


def test_expired_allowlist_entries_strict_cutover():
    # #279 S7 W7: an entry expiring ON 2026-08-31 is valid THROUGH that date and
    # only flagged the day AFTER; a no-`expires` entry never expires.
    allow = [
        {"model": "claude-sonnet-5", "field": "input_cost_per_token",
         "reason": "intro rate", "expires": "2026-08-31"},
        {"model": "claude-durable", "field": "output_cost_per_token",
         "reason": "no cutover"},
    ]
    assert pc.expired_allowlist_entries(allow, "2026-07-10") == []
    assert pc.expired_allowlist_entries(allow, "2026-08-31") == []  # valid THROUGH
    got = pc.expired_allowlist_entries(allow, "2026-09-01")
    assert len(got) == 1 and got[0]["model"] == "claude-sonnet-5"
    # Accepts a full ISO datetime / date object too (uses the date prefix).
    assert pc.expired_allowlist_entries(allow, "2026-09-01T12:00:00Z")[0]["model"] == "claude-sonnet-5"
    assert pc.expired_allowlist_entries([], "2026-09-01") == []


def test_stale_allowlist_entries_empty_when_divergence_real():
    claude = {"claude-b": {"input_cost_per_token": 2e-6}}  # still differs
    litellm = {"claude-b": {"input_cost_per_token": 3e-6}}
    allow = [{"model": "claude-b", "field": "input_cost_per_token", "reason": "deliberate"}]
    assert pc.stale_allowlist_entries(allow, claude, {}, litellm) == []


def test_check_table_shapes_provider_specific_and_sentinel_aware():
    claude_ok = {"c": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                       "cache_creation_input_token_cost": 1e-6, "cache_read_input_token_cost": 1e-7}}
    codex_ok = {"gpt-5": {"input_cost_per_token": 1.25e-6, "cache_read_input_token_cost": 1.25e-7,
                          "output_cost_per_token": 1e-5}}
    problems = pc.check_table_shapes(claude_ok, codex_ok, zero_sentinels=set())
    assert problems == []

    # Codex sentinel all-zero is allowed only when whitelisted
    codex_sentinel = {"gpt-x-spark": {"input_cost_per_token": 0.0,
                      "cache_read_input_token_cost": 0.0, "output_cost_per_token": 0.0}}
    assert pc.check_table_shapes({}, codex_sentinel, zero_sentinels={"gpt-x-spark"}) == []
    assert pc.check_table_shapes({}, codex_sentinel, zero_sentinels=set()) != []

    # Claude missing a required field is a problem
    claude_bad = {"c": {"input_cost_per_token": 1e-6}}
    assert pc.check_table_shapes(claude_bad, {}, zero_sentinels=set()) != []

    # Negative cost is always a problem
    codex_neg = {"gpt-5": {"input_cost_per_token": -1e-6,
                 "cache_read_input_token_cost": 1e-7, "output_cost_per_token": 1e-5}}
    assert pc.check_table_shapes({}, codex_neg, zero_sentinels=set()) != []


@pytest.mark.parametrize("drift,open_,expected", [
    (True, False, "create"),
    (True, True, "update"),
    (False, True, "close"),
    (False, False, "noop"),
])
def test_pricing_issue_action(drift, open_, expected):
    assert pc.pricing_issue_action(drift, open_) == expected


def test_allowlist_is_non_vacuous_against_committed_snapshot():
    import json
    snap = json.loads((pathlib.Path(__file__).resolve().parent
                       / "fixtures" / "pricing" / "litellm_scoped.json").read_text())
    scoped = pc.scope_litellm(snap)
    stale = pc.stale_allowlist_entries(
        pricing.PRICING_DRIFT_ALLOWLIST,
        pricing.CLAUDE_MODEL_PRICING, pricing.CODEX_MODEL_PRICING, scoped)
    assert stale == [], f"stale allowlist entries (divergence resolved upstream): {stale}"


def test_committed_snapshot_matches_live_tables():
    # The committed fixture must carry every model we price at its current
    # value, so a real PRICING_DRIFT_ALLOWLIST entry is required to diverge.
    import json
    snap = json.loads((pathlib.Path(__file__).resolve().parent
                       / "fixtures" / "pricing" / "litellm_scoped.json").read_text())
    scoped = pc.scope_litellm(snap)
    res = pc.diff_pricing(
        pricing.CLAUDE_MODEL_PRICING, pricing.CODEX_MODEL_PRICING,
        scoped, pricing.PRICING_DRIFT_ALLOWLIST)
    assert res.value_drift == [], res.value_drift
    assert res.missing_from_us == [], res.missing_from_us


def test_table_shapes_clean_on_live_tables():
    # gpt-5.3-codex-spark is the one documented all-zero sentinel.
    problems = pc.check_table_shapes(
        pricing.CLAUDE_MODEL_PRICING, pricing.CODEX_MODEL_PRICING,
        zero_sentinels={"gpt-5.3-codex-spark"})
    assert problems == [], problems


# ── Phase B: doctor pricing.coverage (read-only scan + check fn + wiring) ──

import os
import subprocess
import json as _json

_CCTALLY = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


def _run_cctally(args, home, extra_env=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TZ"] = "Etc/UTC"
    # Force prod data-dir layout under the fake HOME (never touch the dev
    # checkout's own data dir) — mirrors the harness's _lib-harness-env.sh.
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_CCTALLY), *args],
        capture_output=True, text=True, env=env,
    )


def _seed_cache_with_models(home, claude_models=(), codex_models=(), age_days=1):
    """Create a minimal cache.db under <home>/.local/share/cctally with the
    SAME schema + WAL the app uses, seeded with one entry per model at
    `age_days` before now. `claude_models` / `codex_models` are iterables of
    (model, input_tokens) tuples (token total drives the WARN detail)."""
    import sqlite3
    cache_path = home / ".local" / "share" / "cctally" / "cache.db"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.with_name("cache.db.maintenance.lock").touch()
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days))
    ts_iso = ts.isoformat().replace("+00:00", "Z")
    conn = sqlite3.connect(str(cache_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Columns mirror bin/_cctally_db.py's _apply_cache_schema exactly.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                timestamp_utc TEXT NOT NULL,
                model TEXT NOT NULL,
                msg_id TEXT, req_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                usage_extra_json TEXT, cost_usd_raw REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS codex_session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                timestamp_utc TEXT NOT NULL,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
        """)
        for i, (model, toks) in enumerate(claude_models):
            conn.execute(
                "INSERT INTO session_entries(source_path, line_offset, "
                "timestamp_utc, model, input_tokens) VALUES (?, ?, ?, ?, ?)",
                (f"/fake/{model}.jsonl", i, ts_iso, model, int(toks)),
            )
        for i, (model, toks) in enumerate(codex_models):
            conn.execute(
                "INSERT INTO codex_session_entries(source_path, line_offset, "
                "timestamp_utc, session_id, model, input_tokens, total_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"/fake/{model}.jsonl", i, ts_iso, "s1", model,
                 int(toks), int(toks)),
            )
        conn.commit()
    finally:
        conn.close()


def test_pricing_observed_models_no_mutation_on_fresh_home(tmp_path):
    # doctor --json drives doctor_gather_state -> _pricing_observed_models;
    # a virgin HOME must not create APP_DIR (read-only contract, spec §5.1).
    r = _run_cctally(["doctor", "--json"], home=tmp_path)
    assert r.returncode in (0, 2), r.stderr
    app_dir = tmp_path / ".local" / "share" / "cctally"
    assert not app_dir.exists(), (
        f"doctor mutated APP_DIR: {sorted(p.name for p in app_dir.rglob('*'))}"
    )


doctor = _load("_lib_doctor")


def _mk_state(**over):
    import dataclasses as dc
    # Minimal DoctorState: every field None (with the handful of
    # non-Optional bools defaulted False), then override.
    fields = {}
    for f in dc.fields(doctor.DoctorState):
        if f.default is not dc.MISSING or f.default_factory is not dc.MISSING:  # type: ignore[attr-defined]
            continue  # has a default — let the dataclass supply it
        fields[f.name] = None
    fields["claude_jsonl_present"] = False
    fields["codex_jsonl_present"] = False
    fields["dashboard_bind_stored"] = "loopback"
    fields["dev_mode"] = False
    fields["app_dir"] = "/tmp/cctally"
    fields["now_utc"] = dt.datetime(2026, 5, 29, tzinfo=dt.timezone.utc)
    fields["cctally_version"] = "test"
    fields.update(over)
    return doctor.DoctorState(**fields)


def test_check_pricing_coverage_warns_on_unpriced_claude():
    gaps = [pc.CoverageGap("claude", "claude-mystery", "unpriced", 3, 5000)]
    res = doctor._check_pricing_coverage(_mk_state(pricing_coverage=gaps))
    assert res.severity == "warn"
    assert res.id == "pricing.coverage"
    # details is a structured dict (sibling-check convention), not a string.
    assert isinstance(res.details, dict)
    assert "claude-mystery" in [g["model"] for g in res.details["unpriced"]]
    assert res.details["unpriced"][0]["entry_count"] == 3
    assert res.details["unpriced"][0]["token_total"] == 5000
    # The human-facing summary still names the gap kind.
    assert "unpriced" in res.summary


def test_check_pricing_coverage_warns_on_codex_fallback():
    gaps = [pc.CoverageGap("codex", "gpt-7-new", "fallback", 2, 900)]
    res = doctor._check_pricing_coverage(_mk_state(pricing_coverage=gaps))
    assert res.severity == "warn"
    assert "gpt-7-new" in [g["model"] for g in res.details["fallback"]]
    assert "fallback" in res.summary


def test_check_pricing_coverage_ok_when_empty():
    res = doctor._check_pricing_coverage(_mk_state(pricing_coverage=[]))
    assert res.severity == "ok"


def test_check_pricing_coverage_ok_when_none():  # cache absent / unreadable
    res = doctor._check_pricing_coverage(_mk_state(pricing_coverage=None))
    assert res.severity == "ok"


def test_pricing_category_registered():
    # Adding the category must surface "pricing" in every doctor report.
    cat_ids = {c[0] for c in doctor._CATEGORY_DEFINITIONS}
    assert "pricing" in cat_ids
    pricing_cat = next(c for c in doctor._CATEGORY_DEFINITIONS if c[0] == "pricing")
    check_ids = {cid for cid, _fn in pricing_cat[2]}
    assert "pricing.coverage" in check_ids


def _doctor_check(payload, check_id):
    for cat in payload["categories"]:
        for c in cat["checks"]:
            if c["id"] == check_id:
                return c
    raise AssertionError(f"check {check_id!r} not in doctor --json")


def test_doctor_warns_with_seeded_unpriced_model(tmp_path):
    # Seed a cache.db with an UNPRICED Claude model in-window, then doctor
    # --json must include pricing.coverage WARN naming that model.
    _seed_cache_with_models(
        tmp_path,
        claude_models=[("claude-totally-made-up-9000", 12345)],
        age_days=1,
    )
    r = _run_cctally(["doctor", "--json"], home=tmp_path)
    assert r.returncode in (0, 2), r.stderr
    payload = _json.loads(r.stdout)
    c = _doctor_check(payload, "pricing.coverage")
    assert c["severity"] == "warn", c
    models = [g["model"] for g in c["details"]["unpriced"]]
    assert "claude-totally-made-up-9000" in models
    assert c["details"]["unpriced"][0]["token_total"] == 12345


def test_doctor_coverage_scan_emits_no_cost_warning_on_stderr(tmp_path):
    # The read-only coverage scan must DETECT unpriced models without firing the
    # cost engine's `[cost] unknown model` stderr warning (warn=False path). If
    # this regresses, the diagnostic spams stderr just by doing its job and
    # poisons the dedup set. (Goldens discard stderr, so pin it here.)
    _seed_cache_with_models(
        tmp_path,
        claude_models=[("claude-totally-made-up-9000", 12345)],
        age_days=1,
    )
    r = _run_cctally(["doctor", "--json"], home=tmp_path)
    assert "[cost] unknown model" not in r.stderr, r.stderr
    # Sanity: the WARN still fired (so the test isn't vacuously passing because
    # detection silently broke).
    c = _doctor_check(_json.loads(r.stdout), "pricing.coverage")
    assert c["severity"] == "warn", c


def test_doctor_ok_with_seeded_priced_model(tmp_path):
    # A model cctally DOES price must not trip the WARN.
    priced = next(iter(pricing.CLAUDE_MODEL_PRICING))
    _seed_cache_with_models(tmp_path, claude_models=[(priced, 1000)], age_days=1)
    r = _run_cctally(["doctor", "--json"], home=tmp_path)
    payload = _json.loads(r.stdout)
    c = _doctor_check(payload, "pricing.coverage")
    assert c["severity"] == "ok", c


def test_doctor_pricing_ignores_out_of_window_models(tmp_path):
    # An unpriced model OLDER than the 30-day window must NOT WARN.
    _seed_cache_with_models(
        tmp_path,
        claude_models=[("claude-old-unpriced", 999)],
        age_days=45,
    )
    r = _run_cctally(["doctor", "--json"], home=tmp_path)
    payload = _json.loads(r.stdout)
    c = _doctor_check(payload, "pricing.coverage")
    assert c["severity"] == "ok", c


# ==========================================================================
# Phase C — `cctally pricing-check` subcommand (C1/C2 integration via
# subprocess; the golden harness `bin/cctally-pricing-check-test` covers the
# byte-stable JSON, this pins the load-bearing exit-code + degraded contracts).
# ==========================================================================


def test_pricing_check_offline_clean_exit0(tmp_path):
    # Fresh HOME, no cache: offline coverage is empty -> nothing actionable.
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 0, r.stderr
    doc = _json.loads(r.stdout)
    assert doc["schemaVersion"] == 1
    assert doc["coverage"] == []
    assert doc["status"] in ("ok", "degraded")
    # --offline must not even attempt the network legs.
    assert doc["existence"]["status"] == "skipped"


def test_pricing_check_offline_does_not_mutate_fresh_home(tmp_path):
    # Read-only contract (spec §5.1/§5.2): a virgin HOME must not create APP_DIR.
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 0, r.stderr
    app_dir = tmp_path / ".local" / "share" / "cctally"
    assert not app_dir.exists(), (
        f"pricing-check mutated APP_DIR: {sorted(p.name for p in app_dir.rglob('*'))}"
    )


def test_pricing_check_offline_finding_exit1(tmp_path):
    # Seed an UNPRICED Claude model -> offline coverage gap -> exit 1.
    _seed_cache_with_models(
        tmp_path,
        claude_models=[("claude-totally-made-up-9000", 12345)],
        age_days=1,
    )
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert any(g["kind"] == "unpriced" and g["model"] == "claude-totally-made-up-9000"
               for g in doc["coverage"]), doc


def test_pricing_check_offline_ignores_codex_unattributed_model_sentinel(tmp_path):
    _seed_cache_with_models(
        tmp_path,
        codex_models=[("unknown", 2_485_412)],
        age_days=1,
    )
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert _json.loads(r.stdout)["coverage"] == []


def test_pricing_check_offline_today_no_suppressions(tmp_path):
    # #279 S7 W7: today (pre-cutover) the sonnet-5 intro-rate suppression is NOT
    # expired, and staleSuppressions is SKIPPED offline (no network) — so both
    # additive keys are empty and exit 0.
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 0, r.stderr
    doc = _json.loads(r.stdout)
    assert doc["staleSuppressions"] == []
    assert doc["expiredSuppressions"] == []
    # staleSuppressions is network-derived → skipped offline, NOT a degradation.
    assert "litellm" not in doc["degraded_components"]


def test_pricing_check_offline_expired_suppression_exit1(tmp_path):
    # Past the sonnet-5 cutover (2026-09-01), the four intro-rate suppressions
    # are expired → expiredSuppressions non-empty → actionable → exit 1. Date-
    # derived, so it fires OFFLINE via the CCTALLY_AS_OF clock seam.
    r = _run_cctally(
        ["pricing-check", "--offline", "--json"], home=tmp_path,
        extra_env={"CCTALLY_AS_OF": "2026-09-01T00:00:00Z"},
    )
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert len(doc["expiredSuppressions"]) == 4, doc["expiredSuppressions"]
    assert all(e["model"] == "claude-sonnet-5" for e in doc["expiredSuppressions"])


def test_pricing_issue_findings_present_stale_expired():
    # The cron treats a stale OR expired suppression as issue-worthy (create/
    # update), independent of value_drift / missing_from_us.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pricing_issue",
        pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "pricing_issue.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    empty = {"drift": {"value_drift": [], "missing_from_us": []},
             "staleSuppressions": [], "expiredSuppressions": []}
    assert mod._findings_present(empty) is False
    assert mod._findings_present({**empty, "staleSuppressions": [{"model": "x"}]}) is True
    assert mod._findings_present({**empty, "expiredSuppressions": [{"model": "y"}]}) is True
    # And the pure kernel maps that to create/update.
    assert pc.pricing_issue_action(True, False) == "create"
    assert pc.pricing_issue_action(True, True) == "update"


def test_pricing_check_offline_all_history_ignores_window(tmp_path):
    # The subcommand's coverage is ALL-HISTORY (vs doctor's 30-day). An
    # unpriced model 45 days old (out of doctor's window) STILL trips it.
    _seed_cache_with_models(
        tmp_path,
        claude_models=[("claude-ancient-unpriced", 777)],
        age_days=45,
    )
    r = _run_cctally(["pricing-check", "--offline", "--json"], home=tmp_path)
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert any(g["model"] == "claude-ancient-unpriced" for g in doc["coverage"]), doc


def _write_litellm(tmp_path, body):
    f = tmp_path / "ll.json"
    f.write_text(_json.dumps(body))
    return f


def _models_file(tmp_path, ids, name="models.json"):
    f = tmp_path / name
    f.write_text(_json.dumps({"data": [{"id": i} for i in ids]}))
    return f


def test_pricing_check_drift_via_injected_litellm_exit1(tmp_path):
    # Inject a LiteLLM snapshot that diverges from our table on one value.
    snap = {"claude-3-5-haiku-20241022": {"litellm_provider": "anthropic",
            "input_cost_per_token": 9.99e-07}}  # embedded is 8e-07
    f = _write_litellm(tmp_path, snap)
    env = dict(os.environ, HOME=str(tmp_path), TZ="Etc/UTC",
               CCTALLY_DISABLE_DEV_AUTODETECT="1",
               CCTALLY_PRICING_LITELLM_FILE=str(f),
               # Inject an empty (but valid) /v1/models response so the
               # existence leg succeeds and only the drift triggers exit 1.
               CCTALLY_PRICING_MODELS_FILE=str(_models_file(tmp_path, [])))
    r = subprocess.run([sys.executable, str(_CCTALLY), "pricing-check", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert doc["status"] == "ok"  # both legs succeeded
    assert any(d["model"] == "claude-3-5-haiku-20241022"
               and d["field"] == "input_cost_per_token"
               for d in doc["drift"]["value_drift"]), doc


def test_pricing_check_degraded_clean_exit0(tmp_path):
    # LiteLLM unreachable (bad file) + no finding -> exit 0, status degraded.
    env = dict(os.environ, HOME=str(tmp_path), TZ="Etc/UTC",
               CCTALLY_DISABLE_DEV_AUTODETECT="1",
               CCTALLY_PRICING_LITELLM_FILE="/nonexistent/litellm.json",
               CCTALLY_PRICING_MODELS_FILE="/nonexistent/models.json")
    r = subprocess.run([sys.executable, str(_CCTALLY), "pricing-check", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert doc["status"] == "degraded"
    assert "litellm" in doc["degraded_components"]
    assert "models_api" in doc["degraded_components"]
    assert doc["drift"]["value_drift"] == []
    assert doc["coverage"] == []


def test_pricing_check_finding_while_degraded_exit1(tmp_path):
    # PRECEDENCE: a real drift on the LiteLLM leg + the /v1/models leg
    # degraded -> exit 1 (finding wins) but status stays degraded.
    snap = {"claude-3-5-haiku-20241022": {"litellm_provider": "anthropic",
            "input_cost_per_token": 9.99e-07}}
    f = _write_litellm(tmp_path, snap)
    env = dict(os.environ, HOME=str(tmp_path), TZ="Etc/UTC",
               CCTALLY_DISABLE_DEV_AUTODETECT="1",
               CCTALLY_PRICING_LITELLM_FILE=str(f),
               CCTALLY_PRICING_MODELS_FILE="/nonexistent")  # forces degraded
    r = subprocess.run([sys.executable, str(_CCTALLY), "pricing-check", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1, (r.returncode, r.stderr)  # finding wins
    doc = _json.loads(r.stdout)
    assert doc["status"] == "degraded"  # models_api leg degraded
    assert "models_api" in doc["degraded_components"]
    assert any(d["model"] == "claude-3-5-haiku-20241022"
               for d in doc["drift"]["value_drift"]), doc


def test_pricing_check_existence_gap_is_actionable_exit1(tmp_path):
    # The /v1/models leg surfaces a vendor model we don't price -> actionable.
    env = dict(os.environ, HOME=str(tmp_path), TZ="Etc/UTC",
               CCTALLY_DISABLE_DEV_AUTODETECT="1",
               # LiteLLM clean (empty scoped set -> no drift).
               CCTALLY_PRICING_LITELLM_FILE=str(_write_litellm(tmp_path, {})),
               CCTALLY_PRICING_MODELS_FILE=str(
                   _models_file(tmp_path, ["claude-brand-new-vendor-model"])))
    r = subprocess.run([sys.executable, str(_CCTALLY), "pricing-check", "--json"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1, (r.returncode, r.stderr)
    doc = _json.loads(r.stdout)
    assert doc["status"] == "ok"
    assert "claude-brand-new-vendor-model" in doc["existence"]["unpriced_vendor_models"]


def test_pricing_check_human_render_runs(tmp_path):
    # Non-JSON render must not crash and must mention pricing.
    r = _run_cctally(["pricing-check", "--offline"], home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "pricing" in r.stdout.lower()


def test_pricing_check_bad_arg_exit2(tmp_path):
    r = _run_cctally(["pricing-check", "--bogus-flag"], home=tmp_path)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


# --- Issue-manager script glue (.github/scripts/pricing_issue.py) ---------
# The load-bearing decision (pricing_issue_action) is unit-tested above; these
# cover the script's own glue: drift_present derivation and the --dry-run
# action mapping (which exercises the kernel-import path the cron uses).

_ISSUE_SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
                 / ".github" / "scripts" / "pricing_issue.py")


def _load_issue_script():
    spec = importlib.util.spec_from_file_location("pricing_issue", _ISSUE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_issue"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_issue_script_findings_present_predicate():
    pi = _load_issue_script()
    assert pi._findings_present({"drift": {"value_drift": [{"model": "x"}],
                                        "missing_from_us": []}}) is True
    assert pi._findings_present({"drift": {"value_drift": [],
                                        "missing_from_us": ["claude-new"]}}) is True
    # ahead_of_litellm is informational only — never drift (invariant #2).
    assert pi._findings_present({"drift": {"value_drift": [], "missing_from_us": [],
                                        "ahead_of_litellm": ["claude-lead"]}}) is False
    assert pi._findings_present({"drift": {"value_drift": [], "missing_from_us": []}}) is False
    assert pi._findings_present({}) is False


def test_issue_script_build_body_renders():
    pi = _load_issue_script()
    body = pi._build_body({
        "snapshotDate": "2026-05-04", "litellmSource": "ll", "status": "ok",
        "degraded_components": [],
        "drift": {"value_drift": [{"model": "claude-a", "field": "input_cost_per_token",
                                   "ours": 1e-6, "theirs": 2e-6}],
                  "missing_from_us": ["claude-new"], "ahead_of_litellm": []},
    })
    assert "claude-a" in body and "input_cost_per_token" in body
    assert "claude-new" in body
    assert "Remediation checklist" in body
    assert "PRICING_SNAPSHOT_DATE" in body


import subprocess as _sp  # noqa: E402


@pytest.mark.parametrize("payload,expected", [
    ({"drift": {"value_drift": [{"model": "x"}], "missing_from_us": []}}, "create"),
    ({"drift": {"value_drift": [], "missing_from_us": []}}, "noop"),
])
def test_issue_script_dry_run_action_mapping(tmp_path, payload, expected):
    import json as _j
    f = tmp_path / "p.json"
    f.write_text(_j.dumps(payload))
    r = _sp.run([sys.executable, str(_ISSUE_SCRIPT), "--dry-run", str(f)],
                capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"action={expected}" in r.stdout, r.stdout


def test_issue_script_bad_payload_exit2(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json")
    r = _sp.run([sys.executable, str(_ISSUE_SCRIPT), "--dry-run", str(f)],
                capture_output=True, text=True)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


# ---- _chip_for_model family bucketing (#244) -------------------------------

@pytest.mark.parametrize("model,chip", [
    ("claude-opus-4-8", "opus"),
    ("claude-sonnet-4-6", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("claude-fable-5", "fable"),          # #244 — dedicated family, not 'other'
    ("CLAUDE-FABLE-5", "fable"),          # case-insensitive
])
def test_chip_for_model_known_families(model, chip):
    assert pricing._chip_for_model(model) == chip


@pytest.mark.parametrize("model", ["gpt-5", "<synthetic>", "", None, "mystery-9000"])
def test_chip_for_model_unknown_is_other_not_sonnet(model):
    # The regression guard mirroring the frontend modelChipClass: an
    # unrecognized id must bucket to the neutral 'other', never 'sonnet'.
    assert pricing._chip_for_model(model) == "other"
