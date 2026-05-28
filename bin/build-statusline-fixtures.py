#!/usr/bin/env python3
"""Build seeded fixture state for `cctally-statusline-test`.

Per spec §5.1 most scenarios are pure stdin-driven and don't need seeded
state — they ship as hand-authored ``input.json`` + ``golden.txt`` under
``tests/fixtures/statusline/<name>/``. This builder writes the small
subset that needs:

  * ``cache.db`` rows (session-cost segments — ``cost-source-cctally`` /
    ``cost-source-both`` / ``resumed-session`` / ``tz-display-utc``).
  * ``stats.db`` rows (5h/7d HWM + DB-latest-row fallback —
    ``extensions-hwm-clamp`` /
    ``extensions-no-stdin-rate-limits-db-fallback``).
  * Transcript JSONL files (``message.usage.input_tokens`` for context %
    bands — ``context-green`` / ``context-yellow`` / ``context-red`` /
    ``context-1m-window``).
  * ``config.json`` (config persistence + override —
    ``config-persistence`` / ``cli-overrides-config`` /
    ``config-path-override``).

Default ``--out`` is the in-tree fixture directory at
``tests/fixtures/statusline``, which writes ``<scenario>/seeds/...``
directly into the repo tree. The harness overrides ``--out`` with a
per-run scratch path so in-tree fixtures stay byte-stable; invoking
this builder manually with no args ALSO regenerates the in-tree seeds,
which is intentional for the ad-hoc "rebuild the committed fixtures
in place" workflow. Byte-stability is guaranteed via
``_fixture_builders.register_fixture_db()`` (zeros the SQLite
writer-version header bytes at process exit).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

# Make `_fixture_builders` importable when run directly.
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _fixture_builders as fb  # noqa: E402


# Pinned `now` for every fixture (matches CCTALLY_AS_OF in input.env).
# Mid-block, mid-week — gives non-trivial countdowns on both 5h and 7d.
AS_OF = "2026-05-28T12:00:00Z"

# Reset moments downstream fixtures pin against (chosen so AS_OF lands
# inside the active 5h block and inside the 7d window with the time
# remainders called out in the spec line shape).
#   AS_OF                = 2026-05-28T12:00:00Z (epoch 1779969600)
#   5h resets_at         = 2026-05-28T15:22:00Z (epoch 1779981720)
#                          delta = +12120s = 3h 22m left
#   7d resets_at         = 2026-06-04T02:00:00Z (epoch 1780538400)
#                          delta = +568800s = 6d 14h left
FIVE_H_RESETS_EPOCH = 1779981720   # 2026-05-28T15:22:00Z
SEVEN_D_RESETS_EPOCH = 1780538400  # 2026-06-04T02:00:00Z

# Sonnet 4.5 200K context window — used in the context-* transcripts.
SONNET_45_CONTEXT_WINDOW = 200_000


# ----- DB seed helpers ----------------------------------------------------


def _open_cache_db(path: pathlib.Path) -> sqlite3.Connection:
    """Open a fresh cache.db with the full session_entries / session_files
    schema (matches production via _fixture_builders.create_cache_db).
    """
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.create_cache_db(path)
    conn = sqlite3.connect(path)
    return conn


def _open_stats_db(path: pathlib.Path) -> sqlite3.Connection:
    """Open a fresh stats.db with the full schema."""
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.create_stats_db(path)
    conn = sqlite3.connect(path)
    return conn


def _seed_session_cost(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    source_path: str,
    timestamp_utc: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_create: int = 0,
    cache_read: int = 0,
    cost_usd_raw: "float | None" = None,
    line_offset: int = 1,
) -> None:
    """Convenience wrapper: upsert session_files + insert session_entries."""
    # IGNORE because tests may share session_files rows across multiple
    # seeded entries.
    conn.execute(
        """INSERT OR IGNORE INTO session_files
           (path, size_bytes, mtime_ns, last_byte_offset,
            last_ingested_at, session_id, project_path)
           VALUES (?, 0, 0, 0, ?, ?, NULL)""",
        (source_path, fb.FIXED_LAST_INGESTED_AT, session_id),
    )
    fb.seed_session_entry(
        conn,
        source_path=source_path,
        line_offset=line_offset,
        timestamp_utc=timestamp_utc,
        model="claude-sonnet-4-5",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_create=cache_create,
        cache_read=cache_read,
        msg_id=f"msg-{session_id}-{line_offset}",
        req_id=f"req-{session_id}-{line_offset}",
        cost_usd_raw=cost_usd_raw,
    )


# ----- Scenario builders --------------------------------------------------


def build_cost_source_cctally(out: pathlib.Path) -> None:
    """Seeds cache.db with one session-entry row attributed to session
    `cs-cctally`. Cost is computed at query time from CLAUDE_MODEL_PRICING:
        input_tokens=200_000 → 200_000 * 3e-06 = $0.60
        output_tokens=20_000 → 20_000 * 1.5e-05 = $0.30
        total $0.90
    """
    fix = out / "cost-source-cctally" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "cache.db"
    conn = _open_cache_db(db)
    _seed_session_cost(
        conn,
        session_id="cs-cctally",
        source_path="/cache/cs-cctally.jsonl",
        timestamp_utc="2026-05-28T11:30:00Z",
        input_tokens=200_000,
        output_tokens=20_000,
    )
    conn.commit()
    conn.close()


def build_cost_source_both(out: pathlib.Path) -> None:
    """Same seed shape as cost-source-cctally but used with --cost-source
    both. Stdin provides a `cost.total_cost_usd` of $0.45 (the "cc"
    value); cache.db value is $0.90 (the "cctally" value).
    """
    fix = out / "cost-source-both" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "cache.db"
    conn = _open_cache_db(db)
    _seed_session_cost(
        conn,
        session_id="cs-both",
        source_path="/cache/cs-both.jsonl",
        timestamp_utc="2026-05-28T11:30:00Z",
        input_tokens=200_000,
        output_tokens=20_000,
    )
    conn.commit()
    conn.close()


def build_resumed_session(out: pathlib.Path) -> None:
    """Two source_path entries sharing the same session_id (resumed
    across files). Both files map to session `resumed-1` via session_files.
    Cost:
        a.jsonl: input=100_000 → $0.30
        b.jsonl: input=100_000 → $0.30
        total $0.60
    """
    fix = out / "resumed-session" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "cache.db"
    conn = _open_cache_db(db)
    _seed_session_cost(
        conn,
        session_id="resumed-1",
        source_path="/cache/resumed-a.jsonl",
        timestamp_utc="2026-05-28T11:00:00Z",
        input_tokens=100_000,
    )
    _seed_session_cost(
        conn,
        session_id="resumed-1",
        source_path="/cache/resumed-b.jsonl",
        timestamp_utc="2026-05-28T11:05:00Z",
        input_tokens=100_000,
        line_offset=2,
    )
    conn.commit()
    conn.close()


def build_tz_display_utc(out: pathlib.Path) -> None:
    """`today` cost depends on the display tz. We seed two entries:
        - 2026-05-28T11:30:00Z   (clearly on AS_OF's UTC date 2026-05-28)
                                  → counts toward today
                                  → input=100_000 → $0.30
        - 2026-05-27T23:00:00Z   (previous UTC date)
                                  → does NOT count under -z UTC
                                  → input=50_000  → $0.15

    Under -z UTC + AS_OF=2026-05-28T12:00:00Z, today's bucket = $0.30.
    """
    fix = out / "tz-display-utc" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "cache.db"
    conn = _open_cache_db(db)
    _seed_session_cost(
        conn,
        session_id="tz-utc",
        source_path="/cache/tz-utc-a.jsonl",
        timestamp_utc="2026-05-28T11:30:00Z",
        input_tokens=100_000,
    )
    _seed_session_cost(
        conn,
        session_id="tz-utc-prev",
        source_path="/cache/tz-utc-b.jsonl",
        timestamp_utc="2026-05-27T23:00:00Z",
        input_tokens=50_000,
        line_offset=2,
    )
    conn.commit()
    conn.close()


def build_extensions_hwm_clamp(out: pathlib.Path) -> None:
    """Seed stats.db with a snapshot whose five_hour_percent=35.0 (HWM)
    for window_key matching FIVE_H_RESETS_EPOCH; weekly_percent=45.0 for
    the week ending at SEVEN_D_RESETS_EPOCH. Stdin will carry lower
    percentages (30/40) so the clamp pulls them up to (35/45).
    """
    fix = out / "extensions-hwm-clamp" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "stats.db"
    conn = _open_stats_db(db)
    # _canonical_5h_window_key floors to 10-minute boundaries; for an
    # epoch already on a clean :22:00 boundary the floor lands on the
    # same minute (1748445720 // 600 * 600 = 1748445600 = 15:20:00Z).
    canonical_5h = (FIVE_H_RESETS_EPOCH // 600) * 600
    week_start_date = "2026-05-28"  # AS_OF date — stub for HWM lookup
    week_end_date = "2026-06-04"
    fb.seed_weekly_usage_snapshot(
        conn,
        captured_at_utc="2026-05-28T11:50:00Z",
        week_start_date=week_start_date,
        week_end_date=week_end_date,
        weekly_percent=45.0,
        five_hour_percent=35.0,
        five_hour_resets_at="2026-05-28T15:22:00Z",
        five_hour_window_key=canonical_5h,
        source="statusline-fixture",
    )
    conn.commit()
    conn.close()


def build_extensions_hwm_7d_post_reset(out: pathlib.Path) -> None:
    """Reset-aware 7d HWM clamp (regression for the post-reset stale-clamp).

    Anthropic mid-week reset / in-place credit leaves the pre-reset peak
    snapshots in the SAME `week_start_date` bucket (the boundary the
    snapshots carry does not change). The 7d HWM clamp must NOT pull the
    post-reset percent up to that stale peak — it has to floor the MAX to
    snapshots captured at/after the latest reset effective within the
    window (mirroring the CLI/dashboard `_apply_reset_events_to_subweeks`
    segmentation). 5h is immune (a 5h reset mints a new window key).

    Seed two snapshots in week_start_date=2026-05-28 (week ends at
    SEVEN_D_RESETS_EPOCH = 2026-06-04T02:00:00Z):
      - pre-reset peak  weekly_percent=41.0  captured 09:00Z (before reset)
      - post-reset      weekly_percent= 2.0  captured 11:00Z (after  reset)
    plus a week_reset_events row with effective_reset_at_utc=10:00Z (inside
    the window). Stdin carries seven_day 2.0 / five_hour 8.0. A naive
    bucket-wide MAX would clamp 7d up to 41%; the reset-aware clamp keeps
    it at 2%. Five-hour clamp resolves to 8% (single window key).
    """
    fix = out / "extensions-hwm-7d-post-reset" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "stats.db"
    conn = _open_stats_db(db)
    canonical_5h = (FIVE_H_RESETS_EPOCH // 600) * 600
    common = dict(
        week_start_date="2026-05-28",
        week_end_date="2026-06-04",
        week_start_at="2026-05-28T02:00:00Z",
        week_end_at="2026-06-04T02:00:00Z",
        five_hour_percent=8.0,
        five_hour_resets_at="2026-05-28T15:22:00Z",
        five_hour_window_key=canonical_5h,
        source="statusline-fixture",
    )
    # Pre-reset peak — captured BEFORE the effective reset moment.
    fb.seed_weekly_usage_snapshot(
        conn,
        captured_at_utc="2026-05-28T09:00:00Z",
        weekly_percent=41.0,
        **common,
    )
    # Post-reset value — captured AFTER the effective reset moment.
    fb.seed_weekly_usage_snapshot(
        conn,
        captured_at_utc="2026-05-28T11:00:00Z",
        weekly_percent=2.0,
        **common,
    )
    # Reset event: effective moment sits inside the current window
    # [2026-05-28T02:00:00Z, 2026-06-04T02:00:00Z). Mixed offset spelling
    # (+00:00) vs the snapshots' `Z` — the clamp normalizes via unixepoch().
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
        (
            "2026-05-28T10:05:00Z",
            "2026-05-30T02:00:00+00:00",
            "2026-06-04T02:00:00+00:00",
            "2026-05-28T10:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def build_extensions_no_stdin_db_fallback(out: pathlib.Path) -> None:
    """Stdin lacks rate_limits entirely; statusline must read the latest
    weekly_usage_snapshots row from stats.db. Seed:
        weekly_percent=42.0
        five_hour_percent=34.0
        week_end_at=2026-06-04T02:00:00Z
        five_hour_window_key=floor(FIVE_H_RESETS_EPOCH / 600) * 600
    The DB-latest path reads `week_end_at` (ISO timestamp) preferentially
    over `week_end_date` (date-only) — Implementor A's round-2 M2 fix.
    """
    fix = out / "extensions-no-stdin-rate-limits-db-fallback" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    db = fix / "stats.db"
    conn = _open_stats_db(db)
    canonical_5h = (FIVE_H_RESETS_EPOCH // 600) * 600
    # week_start_date + week_end_date are date-only; week_start_at +
    # week_end_at are ISO timestamps.
    fb.seed_weekly_usage_snapshot(
        conn,
        captured_at_utc="2026-05-28T11:50:00Z",
        week_start_date="2026-05-28",
        week_end_date="2026-06-04",
        week_start_at="2026-05-28T02:00:00Z",
        week_end_at="2026-06-04T02:00:00Z",
        weekly_percent=42.0,
        five_hour_percent=34.0,
        five_hour_resets_at="2026-05-28T15:22:00Z",
        five_hour_window_key=canonical_5h,
        source="statusline-fixture",
    )
    conn.commit()
    conn.close()


def build_context_band_transcripts(out: pathlib.Path) -> None:
    """Three transcript JSONL files for green/yellow/red context bands.

    Sonnet 4.5 context window = 200_000. We pick input_tokens so the
    last-assistant-turn % lands in each band:
        green   35% → input=70_000  (70_000 / 200_000 = 0.35)
        yellow  65% → input=130_000
        red     95% → input=190_000
    """
    bands = [
        ("green", 70_000, 35),
        ("yellow", 130_000, 65),
        ("red", 190_000, 95),
    ]
    for band, tokens, pct in bands:
        scenario = out / f"context-{band}" / "seeds" / "transcripts"
        scenario.mkdir(parents=True, exist_ok=True)
        path = scenario / f"sess-{band}.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "..."}}),
            json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": "...",
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }}),
        ]
        path.write_text("\n".join(lines) + "\n")


def build_context_1m_window(out: pathlib.Path) -> None:
    """Opus 4.7 1M context-window variant. input_tokens=350_000 →
    350_000 / 1_000_000 = 35% (green under default 50/80 thresholds).
    Same percentage as `context-green` but the model_id forces the 1M
    table lookup — sanity-checks that the 200K default doesn't leak.
    """
    scenario = out / "context-1m-window" / "seeds" / "transcripts"
    scenario.mkdir(parents=True, exist_ok=True)
    path = scenario / "sess-1m.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "..."}}),
        json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": "...",
            "usage": {
                "input_tokens": 350_000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }}),
    ]
    path.write_text("\n".join(lines) + "\n")


def build_config_persistence(out: pathlib.Path) -> None:
    """Default config.json under <scratch>/.local/share/cctally/config.json
    sets statusline.visual_burn_rate=emoji + statusline.cctally_extensions=false.
    With no CLI flags, the rendered line should reflect those config values.
    """
    fix = out / "config-persistence" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    cfg = {
        "display": {"tz": "UTC"},
        "statusline": {
            "visual_burn_rate": "emoji",
            "cost_source": "cc",
            "cctally_extensions": False,
        },
    }
    (fix / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def build_cli_overrides_config(out: pathlib.Path) -> None:
    """Same baseline config.json as config-persistence; CLI flags should
    override. With `-B off --cctally-extensions` the rendered line drops
    the burn-rate emoji and re-includes segment 5.
    """
    fix = out / "cli-overrides-config" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    cfg = {
        "display": {"tz": "UTC"},
        "statusline": {
            "visual_burn_rate": "emoji",
            "cost_source": "cc",
            "cctally_extensions": False,
        },
    }
    (fix / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def build_config_path_override(out: pathlib.Path) -> None:
    """`--config PATH` reads from PATH. Default config.json sets
    visual_burn_rate=off; the alternate (passed via --config) sets it to
    `emoji-text`. The rendered line must reflect the override path's
    values, not the default path's.
    """
    fix = out / "config-path-override" / "seeds"
    fix.mkdir(parents=True, exist_ok=True)
    # Default path config (under .local/share/cctally/config.json)
    cfg_default = {
        "display": {"tz": "UTC"},
        "statusline": {
            "visual_burn_rate": "off",
            "cost_source": "cc",
            "cctally_extensions": True,
        },
    }
    (fix / "config.json").write_text(json.dumps(cfg_default, indent=2) + "\n")
    # Override path config (referenced via --config in flags.txt)
    cfg_alt = {
        "statusline": {
            "visual_burn_rate": "emoji-text",
            "cost_source": "cc",
            "cctally_extensions": True,
        },
    }
    (fix / "custom-config.json").write_text(json.dumps(cfg_alt, indent=2) + "\n")


# ----- Top-level dispatch -------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_out = HERE.parent / "tests" / "fixtures" / "statusline"
    ap.add_argument(
        "--out",
        default=str(default_out),
        help="Output root (default: tests/fixtures/statusline). The harness "
             "lays this on as an overlay onto the in-tree fixture dirs.",
    )
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    build_cost_source_cctally(out)
    build_cost_source_both(out)
    build_resumed_session(out)
    build_tz_display_utc(out)
    build_extensions_hwm_clamp(out)
    build_extensions_hwm_7d_post_reset(out)
    build_extensions_no_stdin_db_fallback(out)
    build_context_band_transcripts(out)
    build_context_1m_window(out)
    build_config_persistence(out)
    build_cli_overrides_config(out)
    build_config_path_override(out)

    print(f"Wrote fixtures under {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
