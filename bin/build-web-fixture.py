#!/usr/bin/env python3
"""Generate a deterministic, scrubbed envelope.json fixture for dashboard/web tests.

Run this from the repo root to rebuild dashboard/web/__tests__/fixtures/envelope.json.
The output matches the Envelope shape emitted by snapshot_to_envelope() but
with placeholder project names and predictable session IDs — no real user data.
"""

import argparse
import json
from pathlib import Path

DEFAULT_FIXTURE_PATH = Path("dashboard/web/__tests__/fixtures/envelope.json")


def build_envelope() -> dict:
    # Header
    header = {
        "week_label": "Apr 21–28",
        "used_pct": 17.4,
        "five_hour_pct": 42.0,
        "dollar_per_pct": 1.23,
        "forecast_pct": 68.5,
        "forecast_verdict": "ok",
        "vs_last_week_delta": -0.05,
    }

    # Current week — 17 milestones (17% used)
    milestones = [
        {
            "percent": i + 1,
            "crossed_at_utc": f"2026-04-21T{10 + i // 4:02d}:{(i * 15) % 60:02d}:00Z",
            "cumulative_usd": round(1.2 * (i + 1), 4),
            "marginal_usd": None if i == 0 else round(1.2, 4),
            "five_hour_pct_at_cross": (i * 5.0) % 100,
        }
        for i in range(17)
    ]
    current_week = {
        "used_pct": 17.4,
        "five_hour_pct": 42.0,
        "five_hour_resets_in_sec": 7200,
        "spent_usd": 20.95,
        "dollar_per_pct": 1.23,
        "reset_at_utc": "2026-04-28T00:00:00Z",
        "reset_in_sec": 3 * 86400 + 5 * 3600,
        "last_snapshot_age_sec": 5,
        "milestones": milestones,
    }

    # Forecast
    forecast = {
        "verdict": "ok",
        "week_avg_projection_pct": 68.5,
        "recent_24h_projection_pct": 72.0,
        "budget_100_per_day_usd": 24.50,
        "budget_90_per_day_usd": 21.00,
        "confidence": "high",
        "confidence_score": 6,
        "explain": None,
    }

    # Trend — 8 rows for spark panel, 12 rows for modal
    def mk_row(i: int, is_current: bool) -> dict:
        return {
            "label": f"W{i:02d}",
            "used_pct": 15.0 + i * 2.3,
            "dollar_per_pct": 1.10 + i * 0.04,
            "delta": None if i == 0 else round((i - 4) * 0.02, 4),
            "is_current": is_current,
        }

    weeks = [mk_row(i, i == 7) for i in range(8)]
    history = [mk_row(i, i == 11) for i in range(12)]
    trend = {
        "weeks": weeks,
        "spark_heights": [int(w["used_pct"] * 2) for w in weeks],
        "history": history,
    }

    # Sessions — 20 synthetic rows with project-00..project-19 and session-0000..
    session_rows = []
    models = ["claude-opus-4-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    projects = [f"project-{i:02d}" for i in range(20)]
    for i in range(20):
        session_rows.append({
            "session_id": f"session-{i:04d}-0000-0000-0000-000000000000",
            "started_utc": f"2026-04-{22 + i // 10:02d}T{(10 + i) % 24:02d}:00:00Z",
            "duration_min": 15 + (i * 7) % 120,
            "model": models[i % 3],
            "project": projects[i % len(projects)],
            "cost_usd": round(1.5 + (i * 0.7) % 8.0, 2),
        })
    sessions = {
        "total": len(session_rows),
        "sort_key": "started_desc",
        "rows": session_rows,
    }

    return {
        "envelope_version": 2,
        "generated_at": "2026-04-24T13:07:00Z",
        "last_sync_at": "2026-04-24T13:06:55Z",
        "sync_age_s": 5,
        "last_sync_error": None,
        "header": header,
        "current_week": current_week,
        "forecast": forecast,
        "trend": trend,
        "sessions": sessions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the dashboard/web envelope.json test fixture."
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_FIXTURE_PATH),
        help=(
            "Output path for the fixture JSON "
            f"(default: {DEFAULT_FIXTURE_PATH})."
        ),
    )
    args = parser.parse_args()
    output_path = Path(args.out)
    env = build_envelope()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(env, indent=2) + "\n")
    print(f"Wrote {output_path} ({output_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
