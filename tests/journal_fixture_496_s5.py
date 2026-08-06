"""#496 S5 — the deterministic equivalence fixture for the two prefix readers.

`db journal-repair` and `db rederive` are converted from a materialized
whole-prefix read to one streaming pass. Nothing about their output may move,
so both conversions are measured against values captured from the PRE-CHANGE
implementation over this fixture. The fixture is therefore built from bytes
alone — the same journal, byte for byte, on every machine and every run.

Two shapes:

`build_tainted` carries three orphan-commit batches and two
`journal_protocol_resolution` ops. Two of the three violations are
acknowledged, one is not, so both violation lists in a repair preview are
non-empty. The two resolution ops sit at deliberately different positions: the
first is mid-segment, and the second is the FIRST line of its segment, so its
evidence names the previous segment's end — the one prefix an accumulator can
only serve from a registered boundary.

`build_clean` carries no violation at all, so `plan_claude_usage` produces a
real plan rather than refusing a tainted journal. Its `planHash` is what pins
the rederive planner's output across the conversion.

The audit records' prefix digests are computed HERE, by an independent copy of
`journal_prefix_hash`'s framing rather than by calling it. A fixture that asked
the implementation under test for the digest it is then asserted against would
accept a changed framing silently; this one makes `resolve_effective_events`
raise "raw-prefix binding does not match" instead.
"""
from __future__ import annotations

import hashlib
import pathlib
import sqlite3

import _lib_journal as jl


AT = "2026-07-27T06:00:00Z"
LATER = "2026-07-27T07:00:00Z"

SEG_A = "observations-2026-07.jsonl"
SEG_B = "observations-2026-08.jsonl"
SEG_C = "observations-2026-09.jsonl"

SEGMENTS = (SEG_A, SEG_B, SEG_C)

CACHE_SESSION_PATH = "/tmp/claude/projects/repo/session.jsonl"


def _prefix_digest(journal_dir: pathlib.Path, high_water) -> str:
    """`journal_prefix_hash`'s framing, written out independently."""
    segment_name, offset = high_water
    digest = hashlib.sha256()
    for name in sorted(
        (path.name for path in journal_dir.glob("*.jsonl")),
        key=jl.segment_sort_key,
    ):
        path = journal_dir / name
        size = offset if name == segment_name else path.stat().st_size
        data = path.read_bytes()[:size]
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if name == segment_name:
            break
    return "sha256:" + digest.hexdigest()


def _claude_obs(at: str, weekly_percent: float) -> dict:
    return jl.make_obs(
        at=at,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload={
            "captured_at": at,
            "source": "statusline",
            "weekly_percent": weekly_percent,
            "resets_at": 1785196800,
        },
    )


def _snapshot_action(event_id: str, weekly_percent: float) -> dict:
    return {
        "action": "replace",
        "id": event_id,
        "rev": 1,
        "at": AT,
        "payload": {
            "kind": "snapshot_accept",
            "captured_at_utc": AT,
            "week_start_date": "2026-07-27",
            "week_start_at": "2026-07-27T00:00:00Z",
            "weekly_percent": weekly_percent,
            "source": "fixture",
            "account_key": "unattributed",
        },
    }


def _valid_batch(batch_id: str) -> list[dict]:
    return jl.make_correction_batch(
        batch_id=batch_id,
        family="claude-usage",
        at=AT,
        actions=[_snapshot_action(f"sa:{batch_id}", 42.0)],
    )


def _orphan_commit(batch_id: str) -> dict:
    """A commit marker with no begin — the `commit_without_begin` violation.

    Built through `make_correction_batch` so the manifest hash is genuine; only
    the commit half is written, which is the crash shape the selector taints.
    """
    return _valid_batch(batch_id)[-1]


def _append(journal_dir: pathlib.Path, name: str, records) -> None:
    journal_dir.mkdir(parents=True, exist_ok=True)
    with open(journal_dir / name, "ab") as handle:
        for record in records:
            handle.write(jl.encode_line(record))


def _violation_for(records, batch_id: str, evidence=()):
    """The live violation identity, resolved over the EXACT record list.

    `commit_without_begin` puts the selector's `enumerate` sequence inside the
    evidence the fingerprint hashes, so this cannot be computed over a filtered
    or partial list — it has to see every record written so far, and therefore
    the prefix evidence of every resolution op already written.
    """
    selection = jl.resolve_effective_events(
        records, protocol_prefix_evidence=tuple(evidence)
    )
    for violation in selection.protocol_violations:
        if violation.batch_id == batch_id:
            return violation
    raise AssertionError(f"fixture batch {batch_id} produced no violation")


def _seed_cache(app_dir: pathlib.Path) -> None:
    """Seed cache.db through a RAW connection, exactly as tests/test_cutover.py
    does: `open_cache_db()` on a fresh file runs the migration dispatcher, which
    arms a Codex re-ingest and drives a stats reconcile. The rederive planner
    needs two rows, not that."""
    import _cctally_db as db

    app_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(app_dir / "cache.db")
    try:
        db._apply_cache_schema(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        for migration in db._CACHE_MIGRATIONS:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?, 't')",
                (migration.name,),
            )
        conn.execute(f"PRAGMA user_version = {len(db._CACHE_MIGRATIONS)}")
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (CACHE_SESSION_PATH, 100, 1, 100, AT, "session-a", "/repo"),
        )
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                CACHE_SESSION_PATH,
                0,
                "2026-07-27T05:00:00+00:00",
                "claude-3-5-sonnet-20241022",
                0,
                0,
                100,
                0,
                40,
                "acct-a",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def build_tainted(app_dir, *, seed_cache: bool = True) -> dict:
    """Write the three-segment fixture with two acknowledged violations."""
    app_dir = pathlib.Path(app_dir)
    journal_dir = app_dir / "journal"

    stream: list[dict] = []
    evidence: list = []

    segment_a = [
        _claude_obs(AT, 10.0),
        *_valid_batch("batch:valid"),
        _orphan_commit("batch:ack-one"),
    ]
    _append(journal_dir, SEG_A, segment_a)
    stream.extend(segment_a)

    segment_b_head = [
        _claude_obs(LATER, 11.0),
        _orphan_commit("batch:ack-two"),
    ]
    _append(journal_dir, SEG_B, segment_b_head)
    stream.extend(segment_b_head)

    # Resolution #1 — mid-segment evidence, inside the segment being streamed.
    mid_b = (SEG_B, (journal_dir / SEG_B).stat().st_size)
    mid_b_digest = _prefix_digest(journal_dir, mid_b)
    audit_one = jl.make_protocol_resolution(
        at=LATER,
        violations=[_violation_for(stream, "batch:ack-one", evidence)],
        journal_high_water=mid_b,
        journal_prefix_hash=mid_b_digest,
    )
    _append(journal_dir, SEG_B, [audit_one])
    stream.append(audit_one)
    evidence.append((mid_b, mid_b_digest))

    # Resolution #2 — the FIRST line of its segment, so its evidence names the
    # previous segment's end. That prefix is gone by the time the op is decoded,
    # so it can only be served from the boundary registered at the transition.
    end_b = (SEG_B, (journal_dir / SEG_B).stat().st_size)
    end_b_digest = _prefix_digest(journal_dir, end_b)
    audit_two = jl.make_protocol_resolution(
        at=LATER,
        violations=[_violation_for(stream, "batch:ack-two", evidence)],
        journal_high_water=end_b,
        journal_prefix_hash=end_b_digest,
    )
    segment_c = [
        audit_two,
        _orphan_commit("batch:unack"),
        jl.make_evt(
            kind="snapshot_accept",
            id="sa:tail",
            at=LATER,
            payload={
                "captured_at_utc": LATER,
                "week_start_date": "2026-07-27",
                "week_end_date": "2026-08-03",
                "week_start_at": "2026-07-27T00:00:00+00:00",
                "week_end_at": "2026-08-03T00:00:00+00:00",
                "weekly_percent": 26.0,
                "source": "fixture",
                "payload_json": "{}",
                "account_key": "unattributed",
            },
        ),
    ]
    _append(journal_dir, SEG_C, segment_c)
    stream.extend(segment_c)

    if seed_cache:
        _seed_cache(app_dir)

    return {
        "segments": list(SEGMENTS),
        "high_water": (SEG_C, (journal_dir / SEG_C).stat().st_size),
        "audit_ids": [audit_one["id"], audit_two["id"]],
        "records": stream,
    }


def build_clean(app_dir, *, seed_cache: bool = True) -> dict:
    """Write a two-segment fixture with no structural violation at all."""
    app_dir = pathlib.Path(app_dir)
    journal_dir = app_dir / "journal"

    segment_a = [_claude_obs(AT, 10.0)]
    _append(journal_dir, SEG_A, segment_a)
    segment_b = [_claude_obs(LATER, 11.0)]
    _append(journal_dir, SEG_B, segment_b)

    if seed_cache:
        _seed_cache(app_dir)

    return {
        "segments": [SEG_A, SEG_B],
        "high_water": (SEG_B, (journal_dir / SEG_B).stat().st_size),
        "records": [*segment_a, *segment_b],
    }
