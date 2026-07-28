#!/usr/bin/env python3
"""Generate a journal segment carrying all three same-revision conflict classes.

Issue #374. Production journals written by pre-quarantine binaries can contain
two or more evt lines sharing an ``(id, rev)`` with DIFFERENT content — a state
the append-only journal can never un-write. Before #374 that made
``rebuild_stats_index`` abort, so a `stats.db` at the previous index epoch could
never rebuild and the dashboard refused to start.

This builder writes a deterministic segment reproducing the fourteen groups
observed in production, at their observed SHAPES:

  * Class A — ``account_key: null`` widened to ``"unattributed"`` by the #341
    multi-account epic. Seven ``sa:`` groups. Variant 1 carries no
    ``account_key``; variant 2 carries ``"unattributed"``. The rebuild's own
    ``_normalize_legacy_account_stamp`` WIDENS this rather than closing it —
    variant 1 is stamped with the cutover op's account while variant 2 is left
    alone — so the fixture also carries the cutover op that drives that mapping.
  * Class B — a ``wcs:`` group re-derived across attempts. Five groups: one with
    NINE variants whose cost climbs monotonically as the range end advances, and
    four whose later variant carries ``cost_usd: 0.0`` (the degraded shape a
    cache-corruption window produced).
  * Class C — an ``fhbc:`` block re-emitted under a stable natural key, two
    groups, differing only in volatile fields (``last_updated_at_utc`` — which is
    also the event's top-level ``at`` — plus, across a restart,
    ``created_at_utc`` / ``first_observed_at_utc`` /
    ``seven_day_pct_at_block_start``).

EVERY identifier here is SYNTHETIC: synthetic account keys, synthetic project
paths, synthetic obs digests. Nothing is derived from a production journal — the
artifact is committed to the repo AND published to the public mirror, and a
prod-derived digest would leak (the #373 lesson).

    python3 bin/build-journal-conflict-fixture.py [output-dir]

Default output dir is ``tests/fixtures/journal-conflicts/``; the segment is
``observations-2026-07.jsonl``. Deterministic — re-running produces byte-identical
output, so a regenerate is a no-op diff.
"""
from __future__ import annotations

import pathlib
import sys


def _load_kernel():
    bin_dir = str(pathlib.Path(__file__).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import _lib_journal

    return _lib_journal


SEGMENT_NAME = "observations-2026-07.jsonl"

# Synthetic cutover account — a fixed 32-hex token that is NOT any real account
# digest. The rebuild stamps Class A's unstamped variant with this value.
SYNTHETIC_CUTOVER_ACCOUNT = "ac00000000000000000000000000cafe"
UNATTRIBUTED = "unattributed"
CUTOVER_OP_ID = "accounts-cutover-v1"

WEEK_START_DATE = "2026-07-13"
WEEK_END_DATE = "2026-07-20"
WEEK_START_AT = "2026-07-13T00:00:00+00:00"
WEEK_END_AT = "2026-07-20T00:00:00+00:00"


def _synthetic_obs_id(n: int) -> str:
    """A synthetic ``o:<16 hex>`` obs id. Fixed prefix + zero-padded counter, so
    it is visibly synthetic and can never collide with a real content digest."""
    return f"o:5eed{n:012d}"


def _snapshot_payload(percent: float, *, account_key=None) -> dict:
    payload = {
        "captured_at_utc": "2026-07-18T09:00:00Z",
        "week_start_date": WEEK_START_DATE,
        "week_end_date": WEEK_END_DATE,
        "week_start_at": WEEK_START_AT,
        "week_end_at": WEEK_END_AT,
        "weekly_percent": percent,
        "source": "statusline",
        "payload_json": "{}",
    }
    if account_key is not None:
        payload["account_key"] = account_key
    return payload


def _cost_payload(cost: float, range_end: str, *, account_key) -> dict:
    return {
        "captured_at_utc": "2026-07-18T09:00:00Z",
        "week_start_date": WEEK_START_DATE,
        "week_end_date": WEEK_END_DATE,
        "week_start_at": WEEK_START_AT,
        "week_end_at": WEEK_END_AT,
        "range_start_iso": "2026-07-13T00:00:00+00:00",
        "range_end_iso": range_end,
        "cost_usd": cost,
        "mode": "auto",
        "project": None,
        "account_key": account_key,
    }


def _block_payload(window_key: int, *, cost: float, last_updated: str,
                   created: str, first_observed: str, seven_day_pct: float,
                   account_key: str) -> dict:
    return {
        "five_hour_window_key": window_key,
        "five_hour_resets_at": "2026-07-18T10:00:00Z",
        "block_start_at": "2026-07-18T05:00:00Z",
        "first_observed_at_utc": first_observed,
        "last_observed_at_utc": "2026-07-18T09:58:00Z",
        "final_five_hour_percent": 61.0,
        "seven_day_pct_at_block_start": seven_day_pct,
        "created_at_utc": created,
        "last_updated_at_utc": last_updated,
        "is_closed": 1,
        "total_cost_usd": cost,
        "account_key": account_key,
        "_models": [
            {
                "five_hour_window_key": window_key,
                "model": "claude-opus-4",
                "cost_usd": cost,
                "entry_count": 4,
                "account_key": account_key,
            }
        ],
        "_projects": [
            {
                "five_hour_window_key": window_key,
                "project_path": "/synthetic/projects/alpha",
                "cost_usd": cost,
                "entry_count": 4,
                "account_key": account_key,
            }
        ],
    }


def build_records(J) -> tuple[list, list]:
    """Return ``(records, conflicting_event_ids)`` in canonical append order."""
    records: list = []
    conflicting: list = []

    # The cutover op: it is what makes Class A's normalisation WIDEN rather than
    # close the divergence, so the fixture must carry one.
    cutover = J.make_op(
        at="2026-07-17T12:00:00Z",
        src="accounts-cutover",
        payload={"kind": "accounts_cutover",
                 "claude_legacy_account": SYNTHETIC_CUTOVER_ACCOUNT},
    )
    cutover["id"] = CUTOVER_OP_ID
    records.append(cutover)

    # ── Class A: seven `sa:` groups, null vs "unattributed" ───────────────
    for n in range(7):
        event_id = f"sa:{_synthetic_obs_id(n)}"
        percent = 10.0 + n
        records.append(J.make_evt(
            kind="snapshot_accept", id=event_id, at="2026-07-18T09:00:00Z",
            payload=_snapshot_payload(percent)))
        records.append(J.make_evt(
            kind="snapshot_accept", id=event_id, at="2026-07-18T09:00:00Z",
            payload=_snapshot_payload(percent, account_key=UNATTRIBUTED)))
        conflicting.append(event_id)

    # ── Class B(i): one `wcs:` group with NINE monotonic-cost variants ────
    nine_id = f"wcs:{_synthetic_obs_id(100)}:{WEEK_START_DATE}"
    for step in range(9):
        records.append(J.make_evt(
            kind="weekly_cost_snapshot", id=nine_id,
            at="2026-07-18T09:00:00Z",
            payload=_cost_payload(
                1430.30 + step * 18.28,
                f"2026-07-24T0{4 + step // 2}:{(step % 2) * 30:02d}:00+00:00",
                account_key=SYNTHETIC_CUTOVER_ACCOUNT)))
    conflicting.append(nine_id)

    # ── Class B(ii): four `wcs:` groups degraded to cost 0.0 ─────────────
    for n in range(4):
        event_id = f"wcs:{_synthetic_obs_id(200 + n)}:{WEEK_START_DATE}"
        records.append(J.make_evt(
            kind="weekly_cost_snapshot", id=event_id,
            at="2026-07-18T09:00:00Z",
            payload=_cost_payload(410.5 + n, "2026-07-24T04:40:45+00:00",
                                  account_key=SYNTHETIC_CUTOVER_ACCOUNT)))
        records.append(J.make_evt(
            kind="weekly_cost_snapshot", id=event_id,
            at="2026-07-18T09:00:00Z",
            payload=_cost_payload(0.0, "2026-07-24T19:03:11+00:00",
                                  account_key=SYNTHETIC_CUTOVER_ACCOUNT)))
        conflicting.append(event_id)

    # ── Class C: two `fhbc:` groups differing only in volatile fields ────
    for n in range(2):
        window_key = 4000000 + n
        event_id = f"fhbc:{UNATTRIBUTED}:{window_key}"
        records.append(J.make_evt(
            kind="five_hour_block_close", id=event_id,
            at="2026-07-18T09:58:00Z",
            payload=_block_payload(
                window_key, cost=12.5 + n,
                last_updated="2026-07-18T09:58:00Z",
                created="2026-07-18T05:02:00Z",
                first_observed="2026-07-18T05:02:00Z",
                seven_day_pct=44.0,
                account_key=UNATTRIBUTED)))
        # The retry: a moved clock, and (across a restart) moved
        # created/first-observed/seven-day fields.
        records.append(J.make_evt(
            kind="five_hour_block_close", id=event_id,
            at="2026-07-18T09:59:30Z",
            payload=_block_payload(
                window_key, cost=12.5 + n,
                last_updated="2026-07-18T09:59:30Z",
                created="2026-07-18T05:03:10Z",
                first_observed="2026-07-18T05:03:10Z",
                seven_day_pct=44.5,
                account_key=UNATTRIBUTED)))
        conflicting.append(event_id)

    return records, conflicting


def main(argv: list) -> int:
    J = _load_kernel()
    out_dir = pathlib.Path(
        argv[1] if len(argv) > 1
        else pathlib.Path(__file__).resolve().parent.parent
        / "tests" / "fixtures" / "journal-conflicts"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    records, conflicting = build_records(J)
    payload = b"".join(J.encode_line(record) for record in records)
    (out_dir / SEGMENT_NAME).write_bytes(payload)
    print(f"wrote {out_dir / SEGMENT_NAME} — {len(records)} lines, "
          f"{len(conflicting)} conflicting group(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
