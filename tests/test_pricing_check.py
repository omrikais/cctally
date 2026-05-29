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
        assert set(e) <= {"model", "field", "reason"}
        assert e["model"] and e["reason"]


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


def _run_cctally(args, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TZ"] = "Etc/UTC"
    # Force prod data-dir layout under the fake HOME (never touch the dev
    # checkout's own data dir) — mirrors the harness's _lib-harness-env.sh.
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
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
