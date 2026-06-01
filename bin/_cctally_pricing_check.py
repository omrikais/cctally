"""`cctally pricing-check` subcommand entry point.

I/O sibling: holds the network/existence fetchers + `cmd_pricing_check`
+ the text renderer + `_pricing_observed_models` (the offline cache-scan
coverage leg, #125 Batch E C7), plus the two `_ENV_PRICING_*`
test-injection env-var name constants (module-private here). The pure
decision kernel lives in `_lib_pricing_check` (imported qualified,
module-top).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). The qualified
`_lib_pricing_check` import is the eagerly-preloaded library kernel
(bin/cctally:287). Every other sibling-homed symbol the command calls is
reached via the call-time `_cctally()` accessor so test monkeypatches
through `cctally`'s namespace are preserved — see spec §3.2.

bin/cctally re-exports `cmd_pricing_check` (eager) so the parser's
`set_defaults(func=c.cmd_pricing_check)` resolves unchanged.

Spec: docs/superpowers/specs/2026-05-30-extract-diagnostics-cmd-design.md
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import urllib.request

import _cctally_core
import _lib_pricing_check
from _cctally_core import _command_as_of, eprint


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.2)."""
    return sys.modules["cctally"]


# ── pricing-check network legs (spec §5.2) ──────────────────────────────
#
# Hidden dev hooks (like `project`'s CCTALLY_AS_OF): read at call time, NOT
# import time, so a test/harness can set them in the child process env. They
# are deliberately absent from `--help` — they only exist to make the
# network legs deterministic in tests (invariant #4: no test hits the
# network). When set to a path, the corresponding fetcher reads that local
# JSON instead of issuing an HTTP request.
_ENV_PRICING_LITELLM_FILE = "CCTALLY_PRICING_LITELLM_FILE"
_ENV_PRICING_MODELS_FILE = "CCTALLY_PRICING_MODELS_FILE"


def _fetch_litellm_prices() -> "tuple[dict, bool]":
    """Fetch the LiteLLM model_prices map. Returns ``(data, ok)``.

    NEVER raises to the caller: on any failure (bad inject file, network
    error, non-JSON, non-dict body) returns ``({}, False)`` so the drift
    leg degrades gracefully (spec invariant #1). Honors the hidden
    ``CCTALLY_PRICING_LITELLM_FILE`` env hook for deterministic tests.
    """
    c = _cctally()
    inject = os.environ.get(_ENV_PRICING_LITELLM_FILE, "").strip()
    if inject:
        try:
            data = json.loads(pathlib.Path(inject).read_text())
            return (data, True) if isinstance(data, dict) else ({}, False)
        except Exception:
            return {}, False
    try:
        # UA can be plain here — LiteLLM is a raw GitHub JSON blob, not the
        # Anthropic rate-limited surface that requires `claude-code/*`.
        req = urllib.request.Request(
            c.LITELLM_PRICES_URL, headers={"User-Agent": "cctally"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data, True) if isinstance(data, dict) else ({}, False)
    except Exception as exc:
        eprint(f"[pricing-check] LiteLLM fetch failed: {exc}")
        return {}, False


def _fetch_anthropic_models_or_none() -> "dict | None":
    """GET https://api.anthropic.com/v1/models with the Claude OAuth bearer.

    Returns the parsed JSON object on success, or ``None`` on ANY failure
    (no token, 401/403, network error, non-JSON, non-dict body) so the
    existence leg degrades to ``status: degraded``. NEVER raises to the
    caller — wrapped in a broad try/except.

    C0 DE-RISK SPIKE STATUS — UNVERIFIED LIVE REACHABILITY: this code path
    was authored WITHOUT a live `/v1/models` call (the sandbox has no real
    OAuth token). It is UNKNOWN whether the Claude Code OAuth bearer
    authorizes `GET /v1/models`; if the endpoint 401/403s, this function
    returns None and the existence leg reports `status: degraded` (the
    feature still stands on LiteLLM drift + the local coverage guard). The
    maintainer must run `cctally pricing-check` on a machine with a real
    OAuth token to confirm the leg actually reaches `status: ok`. The whole
    leg is fully exercisable offline via `CCTALLY_PRICING_MODELS_FILE`.
    """
    c = _cctally()
    try:
        token = c._resolve_oauth_token()
        if not token:
            return None
        # Mirror the OAuth-usage UA discipline: Anthropic rate-limits
        # per-UA, so use `claude-code/<version>` (NOT Python-urllib).
        # RAW config read (NOT load_config / _load_config_unlocked — both
        # call ensure_dirs() and would mutate a fresh HOME, violating the
        # read-only contract). Honors an `oauth_usage.user_agent` override
        # when config.json exists; otherwise the default `claude-code/<v>`.
        raw_cfg = {}
        try:
            if _cctally_core.CONFIG_PATH.exists():
                parsed = json.loads(
                    _cctally_core.CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw_cfg = parsed
        except (json.JSONDecodeError, OSError):
            raw_cfg = {}
        cfg = c._get_oauth_usage_config(raw_cfg)
        user_agent = c._resolve_oauth_usage_user_agent(cfg)
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "User-Agent": user_agent,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        eprint(f"[pricing-check] /v1/models fetch failed: {exc}")
        return None


def _pricing_existence_check() -> dict:
    """Anthropic-only vendor `/v1/models` coverage gap.

    Returns ``{"status": "ok"|"degraded"|"skipped", "unpriced_vendor_models":
    [...]}``. ``ok`` = the vendor list was obtained; the gap is the IDs the
    vendor offers that ``_resolve_model_pricing`` cannot price. ``degraded``
    = the fetch failed (no token / 401 / network / non-JSON). Honors the
    hidden ``CCTALLY_PRICING_MODELS_FILE`` env hook.

    Codex existence is intentionally out of scope (no OpenAI credentials —
    spec §4 non-goals); the payload's existence block is Anthropic-only.
    """
    c = _cctally()
    inject = os.environ.get(_ENV_PRICING_MODELS_FILE, "").strip()
    if inject:
        try:
            raw = json.loads(pathlib.Path(inject).read_text())
        except Exception:
            return {"status": "degraded", "unpriced_vendor_models": []}
    else:
        raw = _fetch_anthropic_models_or_none()
        if raw is None:
            return {"status": "degraded", "unpriced_vendor_models": []}
    if not isinstance(raw, dict):
        return {"status": "degraded", "unpriced_vendor_models": []}
    ids = [m.get("id") for m in raw.get("data", []) if isinstance(m, dict) and m.get("id")]
    # Detection-only: warn=False so a vendor model we don't price doesn't
    # fire the cost-engine's one-shot stderr warning.
    gap = sorted(i for i in ids if c._resolve_model_pricing(i, warn=False) is None)
    return {"status": "ok", "unpriced_vendor_models": gap}


# Private sentinel so `_pricing_observed_models` can tell "default 30-day
# window" apart from an explicit `since=None` all-history scan.
_PRICING_SCAN_DEFAULT_WINDOW = object()


def _pricing_observed_models(now_utc, *, since=_PRICING_SCAN_DEFAULT_WINDOW):
    """Read-only scan of the session-entry cache for observed models.

    Returns a list of ``(provider, model, entry_count, token_total)`` tuples,
    one per DISTINCT model seen in ``cache.db`` (Claude ``session_entries`` +
    Codex ``codex_session_entries``). By default it scans the trailing 30-day
    window relative to ``now_utc`` (the `doctor` coverage signal — recent =
    actionable). Pass ``since=<datetime>`` to widen/narrow the window, or
    ``since=None`` explicitly for an all-history scan (used by `pricing-check`).

    Read-only / no-mutation contract (spec §5.1): mirrors the freshness read
    in this same function — guard on ``CACHE_DB_PATH.exists()``, raw
    ``sqlite3.connect`` (NEVER ``open_cache_db()`` / ``sync_cache()`` /
    ``load_config()`` / ``ensure_dirs()``), and treat a missing table/column as
    "no observed models" rather than crashing. ``doctor --json`` on a virgin
    HOME must not create ``APP_DIR`` — regression
    ``test_pricing_observed_models_no_mutation_on_fresh_home``.
    """
    out: list = []
    if not _cctally_core.CACHE_DB_PATH.exists():
        return out
    # Sentinel: the 30-day window is the default; `since=False` is not a
    # supported value, so distinguish "caller wants all-history" (None) from
    # "caller did not pass since" via a private marker.
    if since is _PRICING_SCAN_DEFAULT_WINDOW:
        cutoff_iso = (now_utc - dt.timedelta(days=30)).isoformat()
    elif since is None:
        cutoff_iso = None  # all-history
    else:
        cutoff_iso = since.isoformat()
    try:
        conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
    except sqlite3.Error:
        return out
    try:
        # Token-sum expressions use the ACTUAL cache column names from
        # bin/_cctally_db.py::_apply_cache_schema (verified — Claude uses
        # cache_create_tokens, NOT cache_creation_tokens; Codex carries a
        # materialized total_tokens covering input/cache/output/reasoning).
        for provider, table, tok_expr in (
            ("claude", "session_entries",
             "COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+"
             "COALESCE(cache_create_tokens,0)+COALESCE(cache_read_tokens,0)"),
            ("codex", "codex_session_entries",
             "COALESCE(total_tokens,0)"),
        ):
            where = "model IS NOT NULL"
            params: tuple = ()
            if cutoff_iso is not None:
                where = "timestamp_utc >= ? AND " + where
                params = (cutoff_iso,)
            try:
                rows = conn.execute(
                    f"SELECT model, COUNT(*), SUM({tok_expr}) FROM {table} "
                    f"WHERE {where} GROUP BY model",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []  # table/column missing — treat as none
            for model, cnt, toks in rows:
                out.append((provider, model, int(cnt or 0), int(toks or 0)))
    finally:
        conn.close()
    return out


def cmd_pricing_check(args: argparse.Namespace) -> int:
    """`cctally pricing-check` — detect stale/missing embedded pricing.

    Three independently-degrading legs (spec §5.2):
      1. coverage (offline, ALL-HISTORY) — models in cache.db we can't price.
      2. drift (network, LiteLLM) — embedded value vs LiteLLM (direction-aware
         + allowlist-suppressed).
      3. existence (network, Anthropic `/v1/models`) — vendor models absent
         from our table.

    Exit-code precedence (spec invariant #1, §5.2):
      1 — ANY actionable finding (coverage gap OR value_drift OR
          missing_from_us OR an existence gap), EVEN IF a network leg
          degraded. Findings always win over degradation.
      0 — NO actionable findings (fully clean OR partially/fully degraded
          but nothing actionable). JSON still carries status=degraded.
      2 — argument/usage error (argparse handles before we run).

    ``status`` (ok|degraded) reports check COMPLETENESS; the exit code
    reports whether the operator must ACT. They are orthogonal: a degraded
    leg never masks a finding and never fabricates one.
    """
    c = _cctally()
    now_utc = _command_as_of()
    status = "ok"
    degraded: list[str] = []

    # 1. Coverage — offline, all-history (since=None). Read-only scan; any
    #    failure degrades to [] (the scan itself swallows DB errors).
    try:
        observed = _pricing_observed_models(now_utc, since=None)
        coverage = _lib_pricing_check.classify_coverage(
            observed,
            lambda m: c._resolve_model_pricing(m, warn=False),
            c._is_codex_fallback,
        )
    except Exception:
        coverage = []

    drift = {"value_drift": [], "missing_from_us": [], "ahead_of_litellm": []}
    existence = {"status": "skipped", "unpriced_vendor_models": []}

    if not args.offline:
        litellm, ok = _fetch_litellm_prices()
        if ok:
            scoped = _lib_pricing_check.scope_litellm(litellm)
            res = _lib_pricing_check.diff_pricing(
                c.CLAUDE_MODEL_PRICING, c.CODEX_MODEL_PRICING,
                scoped, c.PRICING_DRIFT_ALLOWLIST,
            )
            drift = {
                "value_drift": [dataclasses.asdict(r) for r in res.value_drift],
                "missing_from_us": list(res.missing_from_us),
                "ahead_of_litellm": list(res.ahead_of_litellm),
            }
        else:
            status = "degraded"
            degraded.append("litellm")
        existence = _pricing_existence_check()
        if existence["status"] == "degraded":
            status = "degraded"
            degraded.append("models_api")

    # Actionable = any finding on a leg that ran. `ahead_of_litellm` is
    # NEVER actionable (invariant #2). A degraded leg contributes no finding.
    actionable = (
        bool(coverage)
        or bool(drift["value_drift"])
        or bool(drift["missing_from_us"])
        or bool(existence["unpriced_vendor_models"])
    )

    payload = {
        "schemaVersion": 1,
        "status": status,
        "degraded_components": degraded,
        "snapshotDate": c.PRICING_SNAPSHOT_DATE,
        "coverage": [dataclasses.asdict(g) for g in coverage],
        "drift": drift,
        "existence": existence,
        "litellmSource": c.LITELLM_PRICES_URL,
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _render_pricing_check_text(payload, offline=args.offline, actionable=actionable)

    return 1 if actionable else 0


def _render_pricing_check_text(payload: dict, *, offline: bool, actionable: bool) -> None:
    """Human-readable render of the pricing-check payload. JSON is the
    machine contract; this is a readable summary for interactive use."""
    out = sys.stdout.write
    status = payload["status"]
    out(f"pricing-check  (snapshot {payload['snapshotDate']})\n")
    if status == "degraded":
        out(f"  status: degraded — incomplete check "
            f"({', '.join(payload['degraded_components'])} unavailable)\n")
    else:
        out("  status: ok\n")

    cov = payload["coverage"]
    if cov:
        out(f"\n  Coverage gaps ({len(cov)} model(s) we cannot price exactly):\n")
        for g in cov:
            kind = ("unpriced ($0)" if g["kind"] == "unpriced"
                    else "approximated via gpt-5")
            entries = g["entry_count"]
            noun = "entry" if entries == 1 else "entries"
            out(f"    • {g['model']} ({g['provider']}): {entries} "
                f"{noun} / {g['token_total']} tokens — {kind}\n")
    else:
        out("\n  Coverage: all observed models priced.\n")

    if offline:
        out("\n  (offline — network drift + existence legs skipped)\n")
    else:
        vd = payload["drift"]["value_drift"]
        mu = payload["drift"]["missing_from_us"]
        if vd:
            out(f"\n  Value drift vs LiteLLM ({len(vd)} field(s)):\n")
            for d in vd:
                out(f"    • {d['model']}.{d['field']}: ours={d['ours']} "
                    f"litellm={d['theirs']}\n")
        if mu:
            out(f"\n  Models LiteLLM prices but we don't ({len(mu)}):\n")
            for m in mu:
                out(f"    • {m}\n")
        if not vd and not mu and "litellm" not in payload["degraded_components"]:
            out("\n  Drift: embedded pricing matches LiteLLM.\n")
        ex = payload["existence"]
        if ex["status"] == "ok":
            gap = ex["unpriced_vendor_models"]
            if gap:
                out(f"\n  Vendor models not in our table ({len(gap)}):\n")
                for m in gap:
                    out(f"    • {m}\n")
            else:
                out("\n  Existence: all vendor models priced.\n")
        elif ex["status"] == "degraded":
            out("\n  Existence: /v1/models unavailable (skipped).\n")

    # Single-sourced from cmd_pricing_check's exit-code predicate (don't
    # recompute the four-clause boolean here — it would drift).
    if actionable:
        out("\n  Action: review CLAUDE_MODEL_PRICING / CODEX_MODEL_PRICING; "
            "bump PRICING_SNAPSHOT_DATE on sync.\n")
