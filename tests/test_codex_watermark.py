"""#279 S3 F1 — the Codex delta-resume watermark must be the iterator's REAL
dedup watermark (the cumulative `total_token_usage.total_tokens` the guard
compares against), stamped onto `_CodexIterState.total_tokens` and persisted by
`sync_codex_cache`.

Before this fix, `_CodexIterState.total_tokens` was declared but never written,
and the caller reconstructed the watermark as `initial + Σ(per-turn
last_token_usage.total_tokens)`. Those two quantities are equal only while
Codex's per-turn and cumulative accounting stay mutually consistent — a
divergence (a turn whose cumulative jumps by more than its per-turn delta) makes
the reconstructed sum too low, seeding a too-low watermark on the next resume so
events in the gap re-yield and double-count.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import _lib_jsonl as lj  # noqa: E402
import _lib_accounts as accounts  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402

_CodexIterState = lj._CodexIterState
_iter = lj._iter_codex_jsonl_entries_with_offsets


def _token_count_line(ts, last_total, cumulative, *, include_ttu=True):
    info = {
        "last_token_usage": {
            "input_tokens": last_total, "output_tokens": 0,
            "cached_input_tokens": 0, "reasoning_output_tokens": 0,
            "total_tokens": last_total,
        },
    }
    if include_ttu:
        info["total_token_usage"] = {"total_tokens": cumulative}
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    })


def _session_meta_line(ts, sid):
    return json.dumps({"timestamp": ts, "type": "session_meta",
                       "payload": {"id": sid}})


def _usage_line(ts, *, input_tokens, cached_input_tokens, output_tokens,
                cumulative):
    total = input_tokens + output_tokens
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": 0,
                "total_tokens": total,
            },
            "total_token_usage": {"total_tokens": cumulative},
        }},
    })


def _auth_json(account_id, email):
    def _b64(value):
        return base64.urlsafe_b64encode(
            json.dumps(value).encode()).decode().rstrip("=")

    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    token = f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"
    return json.dumps({
        "tokens": {
            "id_token": token,
            "access_token": "access",
            "refresh_token": "refresh",
        },
    })


def _account_key(account_id, email):
    return accounts.account_key("codex", account_id + "\0" + email)


def _stored_rows(conn):
    return list(conn.execute(
        "SELECT timestamp_utc, input_tokens, cached_input_tokens, "
        "output_tokens, total_tokens, account_key FROM codex_session_entries "
        "ORDER BY timestamp_utc, line_offset"))


def test_state_total_tokens_carries_real_watermark(tmp_path):
    """Cumulative advances MORE than the per-turn delta: the state must carry
    the cumulative (200), not initial + sum-of-deltas (110)."""
    p = tmp_path / "rollout-a.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-1"),
        _token_count_line("2026-07-01T10:00:01Z", 100, 100),
        _token_count_line("2026-07-01T10:00:02Z", 10, 200),  # cumulative +100, delta 10
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState()
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert len(rows) == 2
    assert state.total_tokens == 200  # the REAL watermark, not 110

    # A follow-up event whose cumulative (150) lies BELOW the real watermark
    # (200) but ABOVE the old reconstructed sum (110) must NOT re-yield on
    # resume — the old code seeded 110 and would have double-counted it.
    state2 = _CodexIterState(total_tokens=state.total_tokens)
    p2 = tmp_path / "cont.jsonl"
    p2.write_text(_token_count_line("2026-07-01T10:00:03Z", 5, 150) + "\n")
    with open(p2) as fh:
        rows2 = list(_iter(fh, str(p2), initial_session_id="sess-1", state=state2))
    assert rows2 == []


def test_self_originating_lower_cumulative_starts_new_generation(tmp_path):
    """A continued producer can restart its cumulative counter at zero.

    The first record of the new generation is self-originating: its cumulative
    total equals its complete provider-native ``last_token_usage`` total.  It
    and the following arithmetic continuation must be retained even though
    both totals remain below the prior generation's terminal watermark.
    """
    p = tmp_path / "continued-generation.jsonl"
    prefix = _session_meta_line("2026-07-01T10:00:00Z", "sess-continued") + "\n"
    p.write_text(prefix + "\n".join([
        json.dumps({
            "timestamp": "2026-07-01T18:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5", "turn_id": "turn-restarted"},
        }),
        _token_count_line("2026-07-01T18:00:01Z", 1_500, 1_500),
        _token_count_line("2026-07-01T18:00:02Z", 1_600, 3_100),
    ]) + "\n")
    state = _CodexIterState(
        total_tokens=20_000,
        last_accounting_timestamp=dt.datetime(
            2026, 7, 1, 10, 0, 2, tzinfo=dt.timezone.utc),
    )
    with open(p) as fh:
        fh.seek(len(prefix.encode()))
        rows = list(_iter(
            fh, str(p), initial_session_id="sess-continued", state=state))

    assert [entry.total_tokens for _offset, entry in rows] == [1_500, 1_600]
    assert state.total_tokens == 3_100


def test_replayed_self_originating_generation_is_not_accounting(tmp_path):
    """A copied earlier generation can have the same restart-shaped totals.

    Even fresh re-emission timestamps cannot authorize a counter restart:
    without a distinct producer lifecycle record, appending the copied totals
    at new byte offsets must not lower the watermark or make the continuation
    count twice.
    """
    p = tmp_path / "replayed-generation.jsonl"
    prefix = _session_meta_line("2026-07-01T10:00:00Z", "sess-replay") + "\n"
    p.write_text(prefix + "\n".join([
        _token_count_line("2026-07-01T11:00:01Z", 1_000, 1_000),
        _token_count_line("2026-07-01T11:00:02Z", 2_500, 3_500),
        _token_count_line("2026-07-01T11:00:03Z", 3_500, 7_000),
    ]) + "\n")
    state = _CodexIterState(
        total_tokens=7_000,
        last_accounting_timestamp=dt.datetime(
            2026, 7, 1, 10, 0, 3, tzinfo=dt.timezone.utc),
    )
    with open(p) as fh:
        fh.seek(len(prefix.encode()))
        rows = list(_iter(
            fh, str(p), initial_session_id="sess-replay", state=state))

    assert rows == []
    assert state.total_tokens == 7_000
    assert state.last_accounting_timestamp == dt.datetime(
        2026, 7, 1, 10, 0, 3, tzinfo=dt.timezone.utc)


def test_accepted_out_of_order_timestamp_cannot_weaken_restart_guard(tmp_path):
    """The chronological watermark is a maximum, not the latest assignment."""
    p = tmp_path / "out-of-order-timestamp.jsonl"
    p.write_text("\n".join([
        json.dumps({
            "timestamp": "2026-07-01T08:59:59Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5", "turn_id": "turn-old"},
        }),
        _token_count_line("2026-07-01T09:00:00Z", 100, 200),
        json.dumps({
            "timestamp": "2026-07-01T09:29:59Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5", "turn_id": "turn-noise"},
        }),
        _token_count_line("2026-07-01T09:30:00Z", 50, 50),
    ]) + "\n")
    retained_max = dt.datetime(
        2026, 7, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
    state = _CodexIterState(
        session_id="sess-max-timestamp",
        total_tokens=100,
        last_accounting_timestamp=retained_max,
    )
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))

    assert [entry.total_tokens for _offset, entry in rows] == [100]
    assert state.total_tokens == 200
    assert state.last_accounting_timestamp == retained_max


def test_legacy_no_ttu_file_does_not_inflate_watermark(tmp_path):
    """No total_token_usage dict (older Codex builds): yields happen
    unconditionally, but the watermark must stay at the seed (0), NOT inflate by
    the per-turn sums — otherwise a later mixed-format tail would skip genuinely
    new events."""
    p = tmp_path / "rollout-legacy.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-legacy"),
        _token_count_line("2026-07-01T10:00:01Z", 100, 0, include_ttu=False),
        _token_count_line("2026-07-01T10:00:02Z", 50, 0, include_ttu=False),
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState()
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert len(rows) == 2
    assert state.total_tokens == 0


def test_state_seed_precedence_state_wins(tmp_path):
    """Caller-supplied NON-ZERO state.total_tokens beats the kwarg (mirrors the
    session_id/model seeding precedence). Guard uses 500 (state), not 100
    (kwarg), so a cumulative-400 event is deduped away."""
    p = tmp_path / "rollout-prec.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-prec"),
        _token_count_line("2026-07-01T10:00:01Z", 400, 400),
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState(total_tokens=500)
    with open(p) as fh:
        rows = list(_iter(fh, str(p), initial_total_tokens=100, state=state))
    assert rows == []
    assert state.total_tokens == 500


def test_metadata_only_tail_preserves_prior_watermark(tmp_path):
    """A delta window whose only new record is a session_meta (no yielded
    token_count) leaves the seed watermark untouched — the caller then persists
    the prior value unchanged."""
    p = tmp_path / "rollout-tail.jsonl"
    p.write_text(_session_meta_line("2026-07-01T10:00:05Z", "sess-tail") + "\n")
    state = _CodexIterState(total_tokens=321)
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert rows == []
    assert state.total_tokens == 321


def test_sync_persists_iterator_watermark(tmp_path, monkeypatch):
    """Integration: sync_codex_cache persists the cumulative (200), not the
    reconstructed initial+Σ(per-turn) sum (110). A second sync over an appended
    stale-cumulative (150) event ingests 0 new rows (idempotent resume)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    rollout = sessions / "rollout-2026-07-01T10-00-00-aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa.jsonl"
    turn_ctx = json.dumps({"timestamp": "2026-07-01T10:00:00Z", "type": "turn_context",
                           "payload": {"model": "gpt-5"}})
    rollout.write_text("\n".join([
        _session_meta_line("2026-07-01T10:00:00Z", "sess-div"),
        turn_ctx,
        _token_count_line("2026-07-01T10:00:01Z", 100, 100),
        _token_count_line("2026-07-01T10:00:02Z", 10, 200),  # cumulative +100, delta 10
    ]) + "\n")

    sync_codex_cache = ns["sync_codex_cache"]
    open_cache_db = ns["open_cache_db"]

    conn = open_cache_db()
    try:
        sync_codex_cache(conn)
        row = conn.execute(
            "SELECT last_total_tokens FROM codex_session_files"
        ).fetchone()
        assert row is not None
        assert row[0] == 200, f"expected the cumulative watermark 200, got {row[0]}"
        n_before = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        assert n_before == 2

        # Append a stale-cumulative (150) event: below the real watermark (200)
        # but above the old reconstructed sum (110). A correct resume ingests
        # ZERO new rows.
        with rollout.open("a") as fh:
            fh.write(_token_count_line("2026-07-01T10:00:03Z", 5, 150) + "\n")
        sync_codex_cache(conn)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        assert n_after == 2, f"stale-cumulative event double-counted: {n_after}"
    finally:
        conn.close()


def test_sync_and_rebuild_ignore_replayed_restart_shaped_history(
        tmp_path, monkeypatch):
    """Fresh byte offsets do not make a copied cumulative series new usage."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    sessions = provider_root / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    rollout = sessions / (
        "rollout-2026-07-01T10-00-00-"
        "bbbbbbbb-0000-0000-0000-bbbbbbbbbbbb.jsonl")
    accounting = [
        _token_count_line("2026-07-01T10:00:01Z", 1_000, 1_000),
        _token_count_line("2026-07-01T10:00:02Z", 2_500, 3_500),
        _token_count_line("2026-07-01T10:00:03Z", 3_500, 7_000),
    ]
    rollout.write_text("\n".join([
        _session_meta_line("2026-07-01T10:00:00Z", "sess-replay-sync"),
        json.dumps({
            "timestamp": "2026-07-01T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5"},
        }),
        *accounting,
    ]) + "\n")

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        original = _stored_rows(cache)
    finally:
        cache.close()
    assert len(original) == 3

    replayed_accounting = [
        _token_count_line("2026-07-01T11:00:01Z", 1_000, 1_000),
        _token_count_line("2026-07-01T11:00:02Z", 2_500, 3_500),
        _token_count_line("2026-07-01T11:00:03Z", 3_500, 7_000),
    ]
    with rollout.open("a") as fh:
        fh.write("\n".join(replayed_accounting) + "\n")
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        incremental = _stored_rows(cache)
        watermark = cache.execute(
            "SELECT last_total_tokens FROM codex_session_files"
        ).fetchone()[0]
        ns["sync_codex_cache"](cache, rebuild=True)
        rebuilt = _stored_rows(cache)
    finally:
        cache.close()

    assert incremental == original
    assert watermark == 7_000
    assert rebuilt == original


@pytest.mark.parametrize("switch_account", [False, True])
def test_sync_recovers_a_continued_lower_cumulative_generation(
        tmp_path, monkeypatch, switch_account):
    """Incremental ingest, a reopened cache, and byte-zero rebuild agree.

    The first new-generation record is a divergent cold context read and the
    second is predominantly cached.  The values make an account mix-up or a
    reconstruction from the old cumulative watermark directly observable.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    sessions = provider_root / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    rollout = sessions / "rollout-generation.jsonl"

    a_id, a_email = "acct-a", "a@example.com"
    b_id, b_email = (
        ("acct-b", "b@example.com") if switch_account else (a_id, a_email))
    key_a = _account_key(a_id, a_email)
    key_b = _account_key(b_id, b_email)
    (provider_root / "auth.json").write_text(_auth_json(a_id, a_email))
    turn = json.dumps({
        "timestamp": "2026-07-01T10:00:00Z",
        "type": "turn_context",
        "payload": {"model": "gpt-5"},
    })
    head = "\n".join([
        _session_meta_line("2026-07-01T10:00:00Z", "sess-generation"),
        turn,
        _usage_line(
            "2026-07-01T10:00:01Z", input_tokens=10_000,
            cached_input_tokens=9_000, output_tokens=1_000,
            cumulative=11_000),
        _usage_line(
            "2026-07-01T10:00:02Z", input_tokens=10_000,
            cached_input_tokens=9_500, output_tokens=1_000,
            cumulative=22_000),
    ]) + "\n"
    rollout.write_text(head)

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        before_a = list(cache.execute(
            "SELECT input_tokens, cached_input_tokens, output_tokens, "
            "total_tokens FROM codex_session_entries WHERE account_key=? "
            "ORDER BY timestamp_utc", (key_a,)))
    finally:
        cache.close()
    assert len(before_a) == 2

    (provider_root / "auth.json").write_text(_auth_json(b_id, b_email))
    transition = json.dumps({
        "timestamp": "2026-07-01T18:00:00Z",
        "type": "turn_context",
        "payload": {"model": "gpt-5", "turn_id": "turn-restarted"},
    })
    tail = "\n".join([
        transition,
        _usage_line(
            "2026-07-01T18:00:01Z", input_tokens=1_500,
            cached_input_tokens=100, output_tokens=100,
            cumulative=1_600),
        _usage_line(
            "2026-07-01T18:00:02Z", input_tokens=1_600,
            cached_input_tokens=1_500, output_tokens=100,
            cumulative=3_300),
        # A lower value that is not self-originating remains noise.
        _usage_line(
            "2026-07-01T18:00:03Z", input_tokens=10,
            cached_input_tokens=0, output_tokens=10,
            cumulative=3_200),
    ]) + "\n"

    # Commit a metadata-only delta first. The following process must recover
    # the lifecycle transition from retained physical bytes between the last
    # accounting row and this durable cursor; no in-memory flag may be needed.
    rollout.write_text(head + transition + "\n")
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        assert len(_stored_rows(cache)) == 2
    finally:
        cache.close()

    rollout.write_text(head + tail)

    # Reopen the cache to prove no in-memory parser state is required.
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        incremental = _stored_rows(cache)
        after_a = list(cache.execute(
            "SELECT input_tokens, cached_input_tokens, output_tokens, "
            "total_tokens FROM codex_session_entries WHERE account_key=? "
            "AND timestamp_utc < '2026-07-01T18:00:00' ORDER BY timestamp_utc",
            (key_a,)))
        post = list(cache.execute(
            "SELECT input_tokens, cached_input_tokens, output_tokens, "
            "total_tokens, account_key FROM codex_session_entries "
            "WHERE timestamp_utc >= '2026-07-01T18:00:00' "
            "ORDER BY timestamp_utc"))
        watermark = cache.execute(
            "SELECT last_total_tokens FROM codex_session_files"
        ).fetchone()[0]
    finally:
        cache.close()

    assert after_a == before_a
    assert post == [
        (1_500, 100, 100, 1_600, key_b),
        (1_600, 1_500, 100, 1_700, key_b),
    ]
    assert watermark == 3_300

    # The repaired rows are the exact inputs to the account-scoped milestone
    # ladder, not merely rows that exist somewhere in the cache.
    import _cctally_quota as quota
    from _lib_quota import QuotaWindowIdentity

    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        root_key = cache.execute(
            "SELECT source_root_key FROM codex_session_files"
        ).fetchone()[0]
        milestone_offset = cache.execute(
            "SELECT line_offset FROM codex_session_entries "
            "WHERE timestamp_utc >= '2026-07-01T18:00:02' "
            "ORDER BY timestamp_utc LIMIT 1"
        ).fetchone()[0]
        reset = dt.datetime(2026, 7, 8, 18, tzinfo=dt.timezone.utc)
        cache.execute(
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            "source_path, line_offset, captured_at_utc, observed_slot, "
            "logical_limit_key, limit_id, limit_name, window_minutes, "
            "used_percent, resets_at_utc, account_key) "
            "VALUES ('codex',?,?,0,?,'primary','limit','codex',"
            "'7-day limit',10080,1.0,?,?)",
            (root_key, str(rollout), "2026-07-01T17:59:59+00:00",
             reset.isoformat(), key_b),
        )
        cache.commit()
        stats.execute(
            "INSERT INTO quota_percent_milestones (source, source_root_key, "
            "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
            "percent_threshold, captured_at_utc, source_path, line_offset, "
            "high_water_percent, generation, account_key) "
            "VALUES ('codex',?,'limit','primary',10080,?,50,?,?,?,50,"
            "'g1',?)",
            (root_key, reset.isoformat(), "2026-07-01T18:00:02+00:00",
             str(rollout), milestone_offset, key_b),
        )
        stats.commit()
        identity = QuotaWindowIdentity(
            source="codex", source_root_key=root_key,
            logical_limit_key="limit", observed_slot="primary",
            window_minutes=10_080, account_key=key_b,
        )
        ladder = quota.codex_quota_breakdown(
            identity, reset, speed="standard", cache_conn=cache,
            stats_conn=stats, account_key=key_b)
    finally:
        stats.close()
        cache.close()
    assert len(ladder) == 1
    assert ladder[0].input_tokens == 3_100
    assert ladder[0].cached_input_tokens == 1_600
    assert ladder[0].output_tokens == 200
    assert ladder[0].total_tokens == 3_300
    expected_cost = sum([
        ns["_calculate_codex_entry_cost"](
            "gpt-5", 1_500, 100, 100, 0, speed="standard"),
        ns["_calculate_codex_entry_cost"](
            "gpt-5", 1_600, 1_500, 100, 0, speed="standard"),
    ])
    assert ladder[0].cost_usd == pytest.approx(expected_cost, abs=1e-12)

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, rebuild=True)
        rebuilt = _stored_rows(cache)
    finally:
        cache.close()
    assert rebuilt == incremental


def test_budgeted_resume_keeps_the_new_generation_watermark(
        tmp_path, monkeypatch):
    """A hook budget may stop between the first and second restarted turns.

    The first turn and its lower generation watermark must commit together, so
    the following resume counts the second turn once instead of comparing it to
    the old generation or replaying the first.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    sessions = provider_root / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    rollout = sessions / "rollout-budgeted-generation.jsonl"
    (provider_root / "auth.json").write_text(
        _auth_json("acct-a", "a@example.com"))
    head = "\n".join([
        _session_meta_line("2026-07-01T10:00:00Z", "sess-budgeted"),
        json.dumps({
            "timestamp": "2026-07-01T10:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5"},
        }),
        _usage_line(
            "2026-07-01T10:00:01Z", input_tokens=19_000,
            cached_input_tokens=18_000, output_tokens=1_000,
            cumulative=20_000),
    ]) + "\n"
    rollout.write_text(head)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    (provider_root / "auth.json").write_text(
        _auth_json("acct-b", "b@example.com"))
    rollout.write_text(head + "\n".join([
        json.dumps({
            "timestamp": "2026-07-01T18:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5"},
        }),
        _usage_line(
            "2026-07-01T18:00:01Z", input_tokens=1_500,
            cached_input_tokens=100, output_tokens=100,
            cumulative=1_600),
        _usage_line(
            "2026-07-01T18:00:02Z", input_tokens=1_600,
            cached_input_tokens=1_500, output_tokens=100,
            cumulative=3_300),
    ]) + "\n")

    import _cctally_cache as cache_module
    real_clock = cache_module._walk_clock
    calls = {"count": 0}

    def _clock():
        calls["count"] += 1
        return 0.0 if calls["count"] <= 4 else 2.0

    cache_module._walk_clock = _clock
    try:
        cache = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](cache, budget_seconds=1.0)
        finally:
            cache.close()
    finally:
        cache_module._walk_clock = real_clock

    assert stats.budget_exhausted is True
    cache = ns["open_cache_db"]()
    try:
        partial = list(cache.execute(
            "SELECT total_tokens, account_key FROM codex_session_entries "
            "WHERE timestamp_utc >= '2026-07-01T18:00:00'"))
        file_state = cache.execute(
            "SELECT ingest_complete, last_total_tokens FROM codex_session_files"
        ).fetchone()
    finally:
        cache.close()
    assert partial == [(1_600, _account_key("acct-b", "b@example.com"))]
    assert file_state == (0, 1_600)

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        final = list(cache.execute(
            "SELECT total_tokens, account_key FROM codex_session_entries "
            "WHERE timestamp_utc >= '2026-07-01T18:00:00' "
            "ORDER BY timestamp_utc"))
    finally:
        cache.close()
    assert final == [
        (1_600, _account_key("acct-b", "b@example.com")),
        (1_700, _account_key("acct-b", "b@example.com")),
    ]
