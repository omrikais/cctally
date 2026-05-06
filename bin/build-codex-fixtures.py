#!/usr/bin/env python3
"""Build seeded SQLite fixtures for the four Codex subcommand harnesses.

Shared builder module serving bin/cctally-codex-{daily,monthly,weekly,session}-test.
Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/codex-{daily,monthly,weekly,session}/<scenario>/.local/share/cctally/.

Most fixtures seed codex_session_entries directly via _fixture_builders.py
helpers (Phase 3 precedent). The two `token-count-dedup` fixtures (one under
codex-daily/, one under codex-session/) additionally write plaintext JSONL
under <fixture>/.codex/sessions/<project>/<file>.jsonl so sync_codex_cache()
ingests them at harness runtime — this is the single fixture shape that
exercises _iter_codex_jsonl_entries_with_offsets's dedup code path
(bin/cctally:1011-1023).

All schema/seeding goes through bin/_fixture_builders.py — do not duplicate
schema here. Idempotent: each builder overwrites existing DBs and JSONL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    FIXED_LAST_INGESTED_AT,
    create_cache_db,
    create_stats_db,
    seed_codex_session_entry,
    seed_codex_session_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests/fixtures"
CODEX_DAILY_DIR = FIXTURES_ROOT / "codex-daily"
CODEX_MONTHLY_DIR = FIXTURES_ROOT / "codex-monthly"
CODEX_WEEKLY_DIR = FIXTURES_ROOT / "codex-weekly"
CODEX_SESSION_DIR = FIXTURES_ROOT / "codex-session"


def _iso(ts: dt.datetime) -> str:
    """UTC-ISO 'Z'-suffixed timestamp, seconds precision."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms(ts: dt.datetime) -> str:
    """UTC-ISO with milliseconds (Codex JSONL timestamps use this shape)."""
    utc = ts.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond // 1000:03d}Z"


def _scenario_path(command_dir: Path, scenario: str) -> tuple[Path, Path]:
    """Return (scenario_dir, db_dir). Creates db_dir.

    Also wipes any prior `.codex/sessions` tree under the scenario so that
    DB-only scenarios stay deterministic against stale JSONL (sync_codex_cache
    ingests any *.jsonl it finds under HOME/.codex/sessions at runtime).
    JSONL-seeded scenarios (token-count-dedup) re-create the tree after
    _scenario_path returns, so this wipe is safe for both shapes.
    """
    scenario_dir = command_dir / scenario
    jsonl_root = scenario_dir / ".codex" / "sessions"
    if jsonl_root.exists():
        shutil.rmtree(jsonl_root)
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)
    return scenario_dir, db_dir


def _codex_source_path(scenario_dir: Path, project: str, file_stem: str) -> str:
    """Build a host-portable, sessions-relative source_path for seeded rows.

    Returns a bare-relative POSIX string like
    `.codex/sessions/<project>/<file_stem>.jsonl` — NO absolute prefix and NO
    leading slash. This keeps committed fixture cache.db files free of
    maintainer absolute paths (e.g. /Volumes/.../cctally-dev/...) so they're
    safe to publish in the public mirror. The `scenario_dir` argument is
    accepted for backward-compatibility with callers but is intentionally
    unused; nothing in the seeded source_path depends on the host build path.

    At harness runtime, _session_path_parts (bin/cctally) recognizes this
    bare-relative form via a `.codex/sessions/...` prefix check and yields
    session_id_path = '<project>/<file_stem>' — same value the older
    absolute-prefix form produced via Path.relative_to(CODEX_SESSIONS_DIR).
    Goldens are byte-stable across the change.
    """
    del scenario_dir  # accepted for API compat; no longer needed
    return f".codex/sessions/{project}/{file_stem}.jsonl"


def _seed_multi_week_rollup_entries(
    conn: sqlite3.Connection,
    *,
    scenario_dir: Path,
    project: str = "proj-rollup",
    file_stem: str = "rollout-001",
    session_id: str = "mwr-session-uuid",
) -> str:
    """Shared seeder for the three multi-week-rollup fixtures.

    Consumers: codex-daily, codex-monthly, codex-weekly — all three
    variants seed IDENTICAL row data so one source_path + six entries
    drive all three aggregators. Returns the source_path used; callers
    may choose to record it in a companion session_files row or not —
    _aggregate_codex_* only joins on source_path for session_id
    resolution which is already present on every codex_session_entries
    row.

    Seeds 6 entries across 3 Monday-starting subscription weeks spanning
    ~2 calendar months (late March → mid April 2026). Two models
    (gpt-5-codex, gpt-5.2-codex); three entries have cached>0 so the
    LiteLLM table/JSON divergence on the Input column is visible
    (Phase 4 gotcha #5).

    Uses _codex_source_path so the seeded source_path is
    CODEX_SESSIONS_DIR-relative-stable at harness runtime → JSON
    sessionId becomes '<project>/<file_stem>' (byte-stable across
    checkouts).
    """
    source_path = _codex_source_path(scenario_dir, project, file_stem)

    # 6 entries, 2 per week, across three Monday-starting weeks:
    #   Week A (Mar 30 – Apr  5): 2026-03-31 (Tue), 2026-04-02 (Thu)
    #   Week B (Apr  6 – Apr 12): 2026-04-07 (Tue), 2026-04-10 (Fri)
    #   Week C (Apr 13 – Apr 19): 2026-04-13 (Mon), 2026-04-15 (Wed)
    # Per LiteLLM convention (gotcha #4): input_tokens INCLUDES cached;
    # output_tokens INCLUDES reasoning.
    entries = [
        # ts,                                   model,          input, cached, output, reason,  total
        (dt.datetime(2026, 3, 31, 14, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5-codex",   12_000,      0,  4_000,      0, 16_000),
        (dt.datetime(2026, 4,  2, 10, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5.2-codex",  8_000,  2_000,  3_000,    500, 11_000),
        (dt.datetime(2026, 4,  7,  9, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5-codex",   20_000,  5_000,  6_000,      0, 26_000),
        (dt.datetime(2026, 4, 10, 17, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5-codex",   15_000,      0,  5_000,    800, 20_000),
        (dt.datetime(2026, 4, 13, 11, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5.2-codex", 10_000,  3_000,  4_000,    600, 14_000),
        (dt.datetime(2026, 4, 15,  8, 0, 0,
                     tzinfo=dt.timezone.utc), "gpt-5.2-codex",  5_000,      0,  2_000,      0,  7_000),
    ]
    for line_offset, (ts, model, input_t, cached, output, reason, total) in enumerate(entries):
        seed_codex_session_entry(
            conn,
            source_path=source_path,
            line_offset=line_offset,
            timestamp_utc=_iso(ts),
            session_id=session_id,
            model=model,
            input_tokens=input_t,
            cached_input_tokens=cached,
            output_tokens=output,
            reasoning_output_tokens=reason,
            total_tokens=total,
        )
    return source_path


# ---------------------------------------------------------------------------
# token-count-dedup JSONL helper
# ---------------------------------------------------------------------------
# Scenario-local per Q2 (approved by user): JSONL emission lives in this
# builder, NOT in bin/_fixture_builders.py. The only in-repo consumers are
# the two token-count-dedup fixtures (codex-daily and codex-session).
#
# PHASE-4-CRITICAL FIXTURE — DO NOT "FIX" TO UPSTREAM PARITY.
# Our `_iter_codex_jsonl_entries_with_offsets` (bin/cctally:906)
# intentionally dedups re-emitted token_count events by tracking
# info.total_token_usage.total_tokens and skipping any event whose
# cumulative is not strictly greater than the previous. Upstream ccusage-
# codex sums every emission, which is why the same JSONL rollout produces
# ~2x cost numbers there. See CLAUDE.md's Gotcha section ("Intentional
# divergence from upstream on duplicate `token_count` events"). If a future
# change makes this fixture pass under naive-sum semantics, something has
# broken the dedup path — treat that as a production regression, not as a
# fixture to re-capture.
# ---------------------------------------------------------------------------


def _write_token_count_dedup_jsonl(
    jsonl_path: Path,
    *,
    session_id: str,
    model: str,
    anchor: dt.datetime,
) -> list[int]:
    """Write a Codex JSONL file with mixed yielding + dedup-skipped token_count events.

    Returns the list of cumulative total_tokens values that the dedup
    pass SHOULD yield (i.e., the strictly-increasing prefix visible to
    the aggregator).

    JSONL shape (one JSON object per line):
      * session_meta: payload.id → sets iterator's session_id state.
      * turn_context: payload.model → sets iterator's model state.
      * event_msg with payload.type=token_count, payload.info containing:
          - last_token_usage: per-turn token counts (what we want to count).
          - total_token_usage.total_tokens: cumulative tracker used for
            the dedup guard. Strictly-increasing → yielded; flat/decreasing
            → skipped.

    Layout (7 event_msg records; 3 yielded; 4 dedup-skipped):

      Event 1: cumulative=1000   (yielded — first event always advances from 0)
      Event 2: cumulative=1000   (skipped — same cumulative, UI re-emit)
      Event 3: cumulative=3500   (yielded — advances)
      Event 4: cumulative=3500   (skipped)
      Event 5: cumulative=3500   (skipped)
      Event 6: cumulative=7000   (yielded — advances)
      Event 7: cumulative=7000   (skipped)

    Per-event last_token_usage values are chosen so the yielded sum
    (Events 1, 3, 6) is strongly distinct from the naive-sum total
    (all 7). Naive sum would be ~2.3x the dedup'd sum.

    jsonl_path's parent dir is created. File is overwritten if present.
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict] = [
        # session_meta (sets session_id state).
        {
            "timestamp": _iso_ms(anchor),
            "type": "session_meta",
            "payload": {"id": session_id},
        },
        # turn_context (sets model state).
        {
            "timestamp": _iso_ms(anchor + dt.timedelta(seconds=1)),
            "type": "turn_context",
            "payload": {"model": model},
        },
        # Event 1: yielded. cumulative=1000; last_token_usage: 700 input, 300 output.
        _tcd_event(anchor + dt.timedelta(minutes=1), inp=700, cached=100, out=300, reason=0, cum=1000),
        # Event 2: skipped (same cumulative as Event 1).
        _tcd_event(anchor + dt.timedelta(minutes=1, seconds=15), inp=700, cached=100, out=300, reason=0, cum=1000),
        # Event 3: yielded. cumulative=3500; last_token_usage: 1800 input, 700 output.
        _tcd_event(anchor + dt.timedelta(minutes=3), inp=1800, cached=400, out=700, reason=100, cum=3500),
        # Event 4: skipped.
        _tcd_event(anchor + dt.timedelta(minutes=3, seconds=10), inp=1800, cached=400, out=700, reason=100, cum=3500),
        # Event 5: skipped.
        _tcd_event(anchor + dt.timedelta(minutes=3, seconds=20), inp=1800, cached=400, out=700, reason=100, cum=3500),
        # Event 6: yielded. cumulative=7000; last_token_usage: 2500 input, 1000 output.
        _tcd_event(anchor + dt.timedelta(minutes=6), inp=2500, cached=500, out=1000, reason=200, cum=7000),
        # Event 7: skipped.
        _tcd_event(anchor + dt.timedelta(minutes=6, seconds=8), inp=2500, cached=500, out=1000, reason=200, cum=7000),
    ]

    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    # Return the cumulative values that SHOULD have been yielded (for
    # sanity checks at capture time).
    return [1000, 3500, 7000]


def _tcd_event(ts: dt.datetime, *, inp: int, cached: int, out: int, reason: int, cum: int) -> dict:
    """Build one Codex event_msg/token_count record with the schema the ingest iterator expects.

    Helper for _write_token_count_dedup_jsonl.
    """
    return {
        "timestamp": _iso_ms(ts),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": inp,
                    "cached_input_tokens": cached,
                    "output_tokens": out,
                    "reasoning_output_tokens": reason,
                    "total_tokens": inp + out,
                },
                "total_token_usage": {"total_tokens": cum},
            },
        },
    }


def build_codex_daily_empty_range():
    """Scenario: zero Codex entries → _emit_codex_no_data sentinel path.

    Verifies:
      * Terminal: "No Codex usage data found." (no filters applied).
      * JSON: {"daily":[],"totals":null} (compact, no indent).
    cmd_codex_daily does NOT consult _command_as_of() — AS_OF is set only
    to satisfy the harness lib's AS_OF SKIP guard (_lib-fixture-harness.sh).
    """
    scenario_dir, db_dir = _scenario_path(CODEX_DAILY_DIR, "empty-range")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    # No codex_session_entries rows: aggregation returns [] and the empty
    # sentinel path fires. No codex_session_files rows either (ingest
    # silently no-ops because <fixture>/.codex/sessions/ is absent).

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_daily_tz_local_overrides_timezone():
    """F2 regression: ``--tz local`` with concurrent ``--timezone UTC`` must
    NOT silently fall through to ``--timezone``.

    The renderer's title banner ``(Timezone: <name>)`` is the primary
    observable: pre-F2 tz_name was sourced from `args.timezone` because
    `resolve_display_tz` returned None for canonical "local"; banner
    showed "(Timezone: UTC)" even though the user explicitly asked for
    local. Post-F2: explicit "--tz local" wins; tz_name is None;
    banner falls back to host-local via `_local_tz_name()` (under
    TZ=Etc/UTC the harness sets, that's "Etc/UTC"). One day of data
    keeps `_emit_codex_no_data` from short-circuiting before the banner.
    """
    scenario_dir, db_dir = _scenario_path(
        CODEX_DAILY_DIR, "tz-local-with-timezone"
    )
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    source_path = _codex_source_path(
        scenario_dir, "proj-tz-local", "rollout-tz-001"
    )
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_codex_session_entry(
            conn,
            source_path=source_path,
            line_offset=0,
            timestamp_utc=_iso(as_of - dt.timedelta(hours=2)),
            session_id="tz-local-session",
            model="gpt-5-codex",
            input_tokens=5_000,
            cached_input_tokens=1_000,
            output_tokens=1_500,
            reasoning_output_tokens=0,
            total_tokens=6_500,
        )
        conn.commit()

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
        'FLAGS="--tz local --timezone UTC"\n'
    )


def build_codex_monthly_empty_range():
    """Scenario: zero Codex entries → _emit_codex_no_data with list_key='monthly'.

    Same shape as codex-daily/empty-range with the JSON list_key differing.
    cmd_codex_monthly does NOT consult _command_as_of() — AS_OF is set only
    to satisfy the harness lib's AS_OF SKIP guard (_lib-fixture-harness.sh).
    """
    scenario_dir, db_dir = _scenario_path(CODEX_MONTHLY_DIR, "empty-range")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_weekly_empty_range():
    """Scenario: zero Codex entries → _emit_codex_no_data(args, "weekly").

    cmd_codex_weekly DOES consult _command_as_of() (unlike daily/monthly/
    session), so AS_OF is load-bearing for the window resolution even
    though the window produces zero entries. Verifies no wall-clock bytes
    leak into the empty sentinel output path.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_WEEKLY_DIR, "empty-range")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_session_empty_range():
    """Scenario: zero Codex entries → _emit_codex_no_data(args, "sessions").

    list_key is PLURAL matching upstream ccusage-codex 18.0.8
    (vs. codex-daily/monthly/weekly which use singular list_keys).
    cmd_codex_session does NOT consult _command_as_of() — AS_OF is set
    only to satisfy the harness lib's AS_OF SKIP guard (_lib-fixture-harness.sh).
    """
    scenario_dir, db_dir = _scenario_path(CODEX_SESSION_DIR, "empty-range")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_daily_multi_week_rollup():
    """Scenario: three subscription weeks of Codex usage across two models.

    Daily grouping → 6 distinct daily rows. Locks:
      * Date-bucket order: ascending by date (default args.order == 'asc').
      * JSON date key format: 'MMM DD, YYYY' per
        _codex_daily_bucket_display.
      * LiteLLM table/JSON Input-column divergence: on the cached>0
        entries (2026-04-02, 2026-04-07, 2026-04-13), JSON inputTokens
        INCLUDES cached; terminal Input column shows input - cached
        (Phase 4 gotcha #5). Lock both conventions in the respective
        goldens.
      * models dict per daily row has 1 or 2 entries depending on which
        models contributed that day; isFallback=false on both (both
        models are direct hits in CODEX_MODEL_PRICING).

    Shared seeder _seed_multi_week_rollup_entries is used — same seed
    data consumed by codex-monthly/multi-week-rollup (Task 7) and
    codex-weekly/multi-week-rollup (Task 8). Changing the seed data
    affects all three fixtures; re-capture goldens for all three on
    any deliberate change.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_DAILY_DIR, "multi-week-rollup")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        _seed_multi_week_rollup_entries(conn, scenario_dir=scenario_dir)
        conn.commit()

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_monthly_multi_week_rollup():
    """Scenario: same 6 entries as codex-daily/multi-week-rollup, bucketed by calendar month.

    Produces 2 rows (Mar 2026, Apr 2026). Locks:
      * JSON month key format 'MMM YYYY' per _codex_monthly_bucket_display
        (no day component — distinguishes from daily's 'MMM DD, YYYY').
      * Monthly rollup math: March bucket sums only the 2026-03-31
        entry (16k totalTokens); April bucket sums the remaining 5
        (78k totalTokens).
      * models dict per row: March has 1 model (gpt-5-codex only);
        April has both models (gpt-5-codex + gpt-5.2-codex).

    Shares seed data with codex-daily/multi-week-rollup and
    codex-weekly/multi-week-rollup via _seed_multi_week_rollup_entries.
    A change to the seeder regenerates all three fixtures' goldens.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_MONTHLY_DIR, "multi-week-rollup")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        _seed_multi_week_rollup_entries(conn, scenario_dir=scenario_dir)
        conn.commit()

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_weekly_multi_week_rollup():
    """Scenario: same 6 entries as codex-daily/multi-week-rollup, bucketed by Monday-starting subscription week.

    Produces 3 rows with week anchors 2026-03-30, 2026-04-06, 2026-04-13
    under AS_OF=2026-04-15T12:00:00Z.

    Locks:
      * AS_OF-pinned window resolution via _command_as_of() in
        cmd_codex_weekly (threads now_utc into _parse_cli_date_range).
      * Monday week-anchor math: 2026-03-31 (Tue) + 2026-04-02 (Thu)
        both land in the week starting 2026-03-30 (Mon). With no
        config.json under HOME=<fixture>, get_week_start_name falls back
        to "monday".
      * JSON week key format "MMM DD, YYYY" — identical to daily's
        format, byte-distinct from monthly's "MMM YYYY".
      * Reconciliation invariant: weekly totals (27k + 46k + 21k = 94k)
        equal the daily totals sum of the same 6 entries. Future
        reconcile-test work can cite this fixture.

    Shares seed data with codex-daily/multi-week-rollup and
    codex-monthly/multi-week-rollup via _seed_multi_week_rollup_entries.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_WEEKLY_DIR, "multi-week-rollup")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        _seed_multi_week_rollup_entries(conn, scenario_dir=scenario_dir)
        conn.commit()

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_daily_unknown_model_fallback():
    """Scenario: two entries with model='gpt-hypothetical-99' (NOT in CODEX_MODEL_PRICING).

    Exercises the LEGACY_FALLBACK_MODEL path end-to-end:
      * _resolve_codex_pricing returns (gpt-5-entry, True).
      * _calculate_codex_entry_cost bills using gpt-5 pricing.
      * JSON models dict entry for 'gpt-hypothetical-99' has
        isFallback=true (via _is_codex_fallback).
      * _warn_unknown_codex_model emits EXACTLY ONE stderr line:
        "[codex] unknown model, using gpt-5 fallback pricing
         (isFallback=true): gpt-hypothetical-99"
        — captured into golden via 2>&1.

    Two entries with the SAME unknown model are seeded to prove the
    warning is one-shot per model name per process (dedup via
    _unknown_codex_model_warnings: set[str]). If two warnings appear in
    the golden, the dedup set isn't firing.

    Minimal 1-day fixture (both entries on 2026-04-15) — codex-daily is
    data-bounded, AS_OF is cosmetic for the harness lib's SKIP guard.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_DAILY_DIR, "unknown-model-fallback")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    source_path = _codex_source_path(scenario_dir, "proj-fallback", "rollout-fb-001")

    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Two entries with the SAME unknown model — proves the warning
        # is one-shot per model name.
        seed_codex_session_entry(
            conn,
            source_path=source_path,
            line_offset=0,
            timestamp_utc=_iso(as_of - dt.timedelta(hours=6)),
            session_id="umf-session-uuid",
            model="gpt-hypothetical-99",
            input_tokens=10_000,
            cached_input_tokens=0,
            output_tokens=3_000,
            reasoning_output_tokens=0,
            total_tokens=13_000,
        )
        seed_codex_session_entry(
            conn,
            source_path=source_path,
            line_offset=1,
            timestamp_utc=_iso(as_of - dt.timedelta(hours=3)),
            session_id="umf-session-uuid",
            model="gpt-hypothetical-99",
            input_tokens=5_000,
            cached_input_tokens=1_000,
            output_tokens=2_000,
            reasoning_output_tokens=0,
            total_tokens=7_000,
        )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_daily_breakdown_per_model():
    """Scenario: one day, three models. FLAGS='--breakdown' triggers per-model child rows.

    Verifies:
      * Terminal: 1 parent day row + 3 child model rows (indented /
        sub-rendered by _render_codex_bucket_table's breakdown path).
      * JSON: daily[0].models has 3 keys, all isFallback=false.

    --json output shape is independent of --breakdown (the models dict
    is always present in JSON); --breakdown only affects the terminal
    renderer. We capture BOTH golden modes with --breakdown layered on
    because the harness lib merges input.env FLAGS with the mode flag.

    First Codex fixture to use input.env FLAGS — mirrors Phase 1's
    weekly/breakdown-per-model shape.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_DAILY_DIR, "breakdown-per-model")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    source_path = _codex_source_path(scenario_dir, "proj-breakdown", "rollout-br-001")

    with sqlite3.connect(db_dir / "cache.db") as conn:
        entries = [
            # (hours_back, model,              input, cached, output, reason, total)
            ( 10, "gpt-5-codex",          10_000,  2_000,  3_000,      0, 13_000),
            (  8, "gpt-5.1-codex-max",     8_000,      0,  2_500,    500, 10_500),
            (  6, "gpt-5.2-codex",         6_000,  1_000,  2_000,      0,  8_000),
        ]
        for i, (hours_back, model, inp, cached, out, reason, total) in enumerate(entries):
            seed_codex_session_entry(
                conn,
                source_path=source_path,
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                session_id="bpm-session-uuid",
                model=model,
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_output_tokens=reason,
                total_tokens=total,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(as_of)}"\n'
        'FLAGS="--breakdown"\n'
    )


def build_codex_session_cross_day_session():
    """Scenario: two sessions with distinguishable last_activity for ordering observability.

      * Session X (short, single-day): 1 entry on 2026-04-13 18:00Z.
        last_activity = 2026-04-13T18:00:00.000Z.
      * Session Y (cross-day): 3 entries on 2026-04-14 22:00Z,
        2026-04-14 23:45Z, and 2026-04-15 02:30Z — spans UTC midnight.
        last_activity = 2026-04-15T02:30:00.000Z.

    Locks:
      * _aggregate_codex_sessions sums Session Y's cross-day entries
        into one row; totalTokens = 16k+11k+8.5k = 35.5k.
      * Default --order asc renders X first (earlier), Y second.
      * --order desc renders Y first, X second (aggregator's natural
        order; asc reverses it per cmd_codex_session).
      * JSON lastActivity uses millisecond precision + 'Z' suffix per
        _codex_last_activity_iso.
      * JSON sessionId is the CODEX_SESSIONS_DIR-relative path without
        the .jsonl suffix (e.g., 'proj-short/rollout-short-x').

    Two distinct source_paths (one per session) so _session_path_parts
    produces two distinct session_id_path values.

    First Codex fixture with golden-desc.txt — opts the fixture into
    the codex-session-test harness's --order desc loop on subsequent
    runs.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_SESSION_DIR, "cross-day-session")

    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    source_x = _codex_source_path(scenario_dir, "proj-short", "rollout-short-x")
    source_y = _codex_source_path(scenario_dir, "proj-cross", "rollout-cross-y")

    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Session X — single entry, 2026-04-13 18:00 UTC
        seed_codex_session_entry(
            conn,
            source_path=source_x,
            line_offset=0,
            timestamp_utc=_iso(dt.datetime(2026, 4, 13, 18, 0, 0, tzinfo=dt.timezone.utc)),
            session_id="session-x-uuid",
            model="gpt-5-codex",
            input_tokens=5_000, cached_input_tokens=0,
            output_tokens=2_000, reasoning_output_tokens=0,
            total_tokens=7_000,
        )
        # Session Y — three entries crossing midnight UTC.
        session_y_entries = [
            (dt.datetime(2026, 4, 14, 22,  0, 0, tzinfo=dt.timezone.utc),
             12_000, 2_000, 4_000, 500, 16_000),
            (dt.datetime(2026, 4, 14, 23, 45, 0, tzinfo=dt.timezone.utc),
              8_000,     0, 3_000,   0, 11_000),
            (dt.datetime(2026, 4, 15,  2, 30, 0, tzinfo=dt.timezone.utc),
              6_000, 1_500, 2_500, 400,  8_500),
        ]
        for i, (ts, inp, cached, out, reason, total) in enumerate(session_y_entries):
            seed_codex_session_entry(
                conn,
                source_path=source_y,
                line_offset=i,  # per-source line offsets restart at 0
                timestamp_utc=_iso(ts),
                session_id="session-y-uuid",
                model="gpt-5-codex",
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_output_tokens=reason,
                total_tokens=total,
            )
        conn.commit()

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_daily_token_count_dedup():
    """Scenario: JSONL-seeded — exercises the full ingest → dedup → cost → aggregation pipeline.

    Critical fixture per spec Risk #2; MUST fail if dedup is ever
    reverted to upstream parity.

    codex_session_entries is NOT pre-seeded. Instead a JSONL file under
    <fixture>/.codex/sessions/proj-dedup/rollout-dd-001.jsonl contains 7
    token_count events; 3 yielded + 4 dedup-skipped.
    sync_codex_cache() walks and ingests this at harness runtime. Dedup
    is enforced at yield time (strict-greater guard on
    info.total_token_usage.total_tokens).

    Totals in the golden equal the sum of the 3 YIELDED events only.
    Upstream parity would emit ~2.3x larger totals.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_DAILY_DIR, "token-count-dedup")

    # Fresh stats.db / cache.db. cache.db has empty codex_session_entries
    # — sync_codex_cache populates at harness runtime.
    # _scenario_path wipes any prior .codex/sessions tree for idempotency.
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    anchor = dt.datetime(2026, 4, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
    jsonl_path = scenario_dir / ".codex" / "sessions" / "proj-dedup" / "rollout-dd-001.jsonl"
    _write_token_count_dedup_jsonl(
        jsonl_path,
        session_id="dedup-session-uuid",
        model="gpt-5-codex",
        anchor=anchor,
    )

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_codex_session_token_count_dedup():
    """Scenario: JSONL-seeded — session-view of the dedup pipeline.

    Same JSONL shape as codex-daily/token-count-dedup (reuses
    _write_token_count_dedup_jsonl) but rendered through cmd_codex_session
    aggregation.

    Produces 1 session row with the 3-yielded sum in its totals columns:
    inputTokens=5000, outputTokens=2000, cachedInputTokens=1000,
    reasoningOutputTokens=300, totalTokens=7000.

    Locks:
      * sessionId format: 'proj-dedup-s/rollout-dd-s-001' (relative to
        CODEX_SESSIONS_DIR, no .jsonl suffix).
      * Both daily and session aggregators produce IDENTICAL dedup'd
        totals — a regression that re-expands dedup'd rows in the
        session view would diff here without diffing in the daily
        fixture.

    If dedup is ever reverted to upstream parity, totals diverge and
    this test fails — the regression safety net visible from the
    session view too.
    """
    scenario_dir, db_dir = _scenario_path(CODEX_SESSION_DIR, "token-count-dedup")

    # _scenario_path wipes any prior .codex/sessions tree for idempotency.
    create_stats_db(db_dir / "stats.db")
    create_cache_db(db_dir / "cache.db")

    anchor = dt.datetime(2026, 4, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
    jsonl_path = scenario_dir / ".codex" / "sessions" / "proj-dedup-s" / "rollout-dd-s-001.jsonl"
    _write_token_count_dedup_jsonl(
        jsonl_path,
        session_id="session-dedup-uuid",
        model="gpt-5-codex",
        anchor=anchor,
    )

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/. Used by cctally-codex-{daily,"
            "monthly,weekly,session}-test to write into a per-run scratch "
            "dir so the in-tree fixtures stay byte-stable across harness "
            "runs. The shared builder writes four sub-trees "
            "(codex-daily/, codex-monthly/, codex-weekly/, codex-session/) "
            "directly under this directory."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_ROOT = args.out
        CODEX_DAILY_DIR = FIXTURES_ROOT / "codex-daily"
        CODEX_MONTHLY_DIR = FIXTURES_ROOT / "codex-monthly"
        CODEX_WEEKLY_DIR = FIXTURES_ROOT / "codex-weekly"
        CODEX_SESSION_DIR = FIXTURES_ROOT / "codex-session"
    for d in (CODEX_DAILY_DIR, CODEX_MONTHLY_DIR, CODEX_WEEKLY_DIR, CODEX_SESSION_DIR):
        d.mkdir(parents=True, exist_ok=True)
    build_codex_daily_empty_range()
    build_codex_daily_tz_local_overrides_timezone()
    build_codex_monthly_empty_range()
    build_codex_weekly_empty_range()
    build_codex_session_empty_range()
    build_codex_daily_multi_week_rollup()
    build_codex_monthly_multi_week_rollup()
    build_codex_weekly_multi_week_rollup()
    build_codex_daily_unknown_model_fallback()
    build_codex_daily_breakdown_per_model()
    build_codex_session_cross_day_session()
    build_codex_daily_token_count_dedup()
    build_codex_session_token_count_dedup()
    print(f"Built Codex fixtures under {FIXTURES_ROOT}")
