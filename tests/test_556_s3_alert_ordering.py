"""#556 S3 — the canonical firing instant and the ordered provider union."""
from __future__ import annotations

import pytest

import _lib_dashboard_sources as sources


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-10T13:58:00Z", "2026-08-10T13:58:00Z"),
    ("2026-08-10T13:58:00+00:00", "2026-08-10T13:58:00Z"),
    ("2026-08-10T15:58:00+02:00", "2026-08-10T13:58:00Z"),
    ("2026-08-10T08:58:00-05:00", "2026-08-10T13:58:00Z"),
])
def test_canonical_alerted_at_normalizes_every_spelling_to_one(raw, expected):
    assert sources.canonical_alerted_at(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "2026-08-10T13:58:00", "not-a-time", 17])
def test_canonical_alerted_at_rejects_rather_than_degrading(bad):
    # A bad value must raise. Degrading to "" is what let E1 sort silently.
    with pytest.raises(ValueError):
        sources.canonical_alerted_at(bad)


def _state(source, rows):
    return sources.SourceDashboardState(
        source=source,
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="v",
        last_success_at=None,
        capabilities={},
        data={"alerts": {"rows": tuple(rows)}},
    )


def test_union_interleaves_providers_by_firing_instant():
    # Claude rows carry alerted_at and NO created_at — the production shape.
    claude = [
        {"source": "claude", "id": "c1", "alerted_at": "2026-08-10T13:59:00Z"},
        {"source": "claude", "id": "c2", "alerted_at": "2026-08-10T13:57:00Z"},
    ]
    # Codex rows carry `created_at` too, as the equal-valued compatibility
    # alias the wire publishes. Keeping it here means a regression back to
    # sorting on `created_at` fails this test rather than passing it.
    # One Codex row is offset-spelled: 15:58+02:00 IS 13:58Z, and sorts FIRST
    # under a lexicographic compare while belonging second.
    codex = [
        {"source": "codex", "key": "x1", "alerted_at": "2026-08-10T15:58:00+02:00",
         "created_at": "2026-08-10T15:58:00+02:00"},
        {"source": "codex", "key": "x2", "alerted_at": "2026-08-10T13:56:00Z",
         "created_at": "2026-08-10T13:56:00Z"},
    ]
    rows = sources._combined_alert_rows(_state("claude", claude), _state("codex", codex))
    assert [r.get("id") or r.get("key") for r in rows] == ["c1", "x1", "c2", "x2"]


def test_union_ties_break_claude_before_codex_preserving_native_order():
    same = "2026-08-10T13:00:00Z"
    claude = [{"source": "claude", "id": "c1", "alerted_at": same},
              {"source": "claude", "id": "c2", "alerted_at": same}]
    codex = [{"source": "codex", "key": "x1", "alerted_at": "2026-08-10T15:00:00+02:00"}]
    rows = sources._combined_alert_rows(_state("claude", claude), _state("codex", codex))
    assert [r.get("id") or r.get("key") for r in rows] == ["c1", "c2", "x1"]


def test_union_raises_on_a_row_with_no_firing_instant():
    claude = [{"source": "claude", "id": "c1"}]
    with pytest.raises(ValueError):
        sources._combined_alert_rows(_state("claude", claude), _state("codex", []))


# ── Task 2: instant-aware ordering before every truncation (§2.3) ──────────
#
# `load_script` is imported here rather than at module scope because the tests
# above are pure-kernel and must not pay for a namespace load.
from conftest import load_script, redirect_paths  # noqa: E402


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _seed_budget_alert(
    conn, *, vendor, threshold, period_start_at, crossed_at, alerted_at,
):
    conn.execute(
        "INSERT INTO budget_milestones "
        "(vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
        " consumption_pct, crossed_at_utc, alerted_at, account_key) "
        "VALUES (?, ?, 'subscription-week', ?, 100.0, 50.0, 50.0, ?, ?, '*')",
        (vendor, period_start_at, threshold, crossed_at, alerted_at),
    )


def test_per_axis_truncation_keeps_the_newest_rows_across_spellings(ns):
    """A LIMIT decides MEMBERSHIP, so it must compare instants, not spellings.

    The offset-spelled row is the OLDEST of the three, yet its text sorts above
    both `Z`-spelled rows. Under a textual `ORDER BY … LIMIT 2` it displaces the
    genuinely-second row, which no later projection or composition can recover.
    """
    conn = ns["open_db"]()
    try:
        _seed_budget_alert(
            conn, vendor="claude", threshold=50,
            period_start_at="2026-08-03T00:00:00Z",
            crossed_at="2026-08-10T13:40:00Z",
            alerted_at="2026-08-10T13:59:00Z",
        )
        _seed_budget_alert(
            conn, vendor="claude", threshold=75,
            period_start_at="2026-08-04T00:00:00Z",
            crossed_at="2026-08-10T13:45:00Z",
            # 15:56+02:00 IS 13:56Z — the oldest row, spelled so it sorts first.
            alerted_at="2026-08-10T15:56:00+02:00",
        )
        _seed_budget_alert(
            conn, vendor="claude", threshold=90,
            period_start_at="2026-08-05T00:00:00Z",
            crossed_at="2026-08-10T13:50:00Z",
            alerted_at="2026-08-10T13:57:00Z",
        )
        conn.commit()
        rows = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn, limit=2)
    finally:
        conn.close()
    assert [row["threshold"] for row in rows] == [50, 90]


def test_the_cross_axis_union_resort_is_instant_aware_too(ns):
    """The union re-sort before `out[:limit]` is a truncation as well."""
    conn = ns["open_db"]()
    try:
        _seed_budget_alert(
            conn, vendor="claude", threshold=50,
            period_start_at="2026-08-03T00:00:00Z",
            crossed_at="2026-08-10T13:40:00Z",
            alerted_at="2026-08-10T15:56:00+02:00",  # 13:56Z — the older row
        )
        conn.execute(
            "INSERT INTO projected_milestones "
            "(week_start_at, period, metric, threshold, projected_value, "
            " denominator, crossed_at_utc, alerted_at, account_key) "
            "VALUES ('2026-08-03T00:00:00Z', 'subscription-week', 'weekly_pct', "
            "        100, 104.0, 100.0, ?, ?, '*')",
            ("2026-08-10T13:50:00Z", "2026-08-10T13:58:00Z"),
        )
        conn.commit()
        rows = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()
    assert [row["axis"] for row in rows] == ["projected", "budget"]


def test_codex_alert_wire_publishes_the_firing_instant_not_the_crossing_one(ns):
    """§2.4. The wire filtered on `alerted_at` and published `crossed_at_utc`."""
    import _cctally_dashboard_sources as ds

    conn = ns["open_db"]()
    try:
        _seed_budget_alert(
            conn, vendor="codex", threshold=50,
            period_start_at="2026-08-01T00:00:00Z",
            crossed_at="2026-08-10T13:40:00Z",
            alerted_at="2026-08-10T13:58:00Z",
        )
        conn.commit()
        [row] = ds._alerts_wire(conn)
    finally:
        conn.close()
    assert row["alerted_at"] == "2026-08-10T13:58:00Z"
    assert row["created_at"] == "2026-08-10T13:58:00Z"


def test_codex_alert_wire_truncates_by_firing_instant_not_crossing_instant(ns):
    """The wire's own LIMIT decided membership by the CROSSING instant.

    A row that fired most recently but crossed longest ago was dropped at the
    boundary, so the newest alert was missing from the Codex projection.
    """
    import _cctally_dashboard_sources as ds

    monkey_limit = 3
    conn = ns["open_db"]()
    try:
        for index in range(monkey_limit):
            _seed_budget_alert(
                conn, vendor="codex", threshold=10 + index,
                period_start_at=f"2026-08-0{index + 1}T00:00:00Z",
                crossed_at=f"2026-08-10T13:5{index}:00Z",
                alerted_at=f"2026-08-09T10:0{index}:00Z",
            )
        _seed_budget_alert(
            conn, vendor="codex", threshold=99,
            period_start_at="2026-08-06T00:00:00Z",
            crossed_at="2026-07-01T00:00:00Z",      # the OLDEST crossing
            alerted_at="2026-08-11T12:00:00Z",      # the NEWEST firing
        )
        conn.commit()
        original = ds.SOURCE_HISTORY_LIMIT
        ds.SOURCE_HISTORY_LIMIT = monkey_limit
        try:
            rows = ds._alerts_wire(conn)
        finally:
            ds.SOURCE_HISTORY_LIMIT = original
    finally:
        conn.close()
    assert [row["threshold"] for row in rows][0] == 99
    assert 99 in [row["threshold"] for row in rows]


def test_the_sql_ordering_expression_agrees_with_the_python_helper():
    """§2.3's assumption, pinned over the committed estate.

    SQL decides every truncation and `canonical_alerted_at` decides every
    composition. A spelling the two disagree on would truncate one set of rows
    and order another, silently. This is where a future writer emitting a
    spelling only one of them understands surfaces.
    """
    import glob
    import json
    import pathlib
    import sqlite3

    values: set[str] = set()

    def _collect(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "alerted_at" and isinstance(item, str) and item:
                    values.add(item)
                _collect(item)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    root = pathlib.Path(__file__).resolve().parent
    for path in sorted(glob.glob(str(root / "fixtures/dashboard/*/golden-data.json"))):
        with open(path, encoding="utf-8") as handle:
            _collect(json.load(handle))
    assert values, "no alerted_at values in the dashboard goldens to check"

    # The goldens hold eight `Z` spellings and one `+02:00`. They do NOT hold a
    # `+00:00`, which is exactly what `_utc_iso` emits, so a scan of the estate
    # alone leaves one of the two real production spellings unchecked. These are
    # named rather than left to a future fixture to cover by accident.
    values.update({
        "2026-08-10T13:58:00+00:00",
        "2026-08-10T13:58:00Z",
        "2026-08-10T15:58:00+02:00",
        "2026-08-10T08:13:00+05:45",
    })

    conn = sqlite3.connect(":memory:")
    try:
        for value in sorted(values):
            (sql_canonical,) = conn.execute(
                f"SELECT {sources.canonical_alerted_at_sql('?')}", (value,),
            ).fetchone()
            assert sql_canonical == sources.canonical_alerted_at(value), value
    finally:
        conn.close()


# ── Task 4: schema version, state identity, Claude alert mutation ─────────

CLAUDE_ROWS = (
    {"source": "claude", "id": "c1", "alerted_at": "2026-08-10T13:59:00Z"},
    {"source": "claude", "id": "c2", "alerted_at": "2026-08-10T13:57:00Z"},
)
CLAUDE_ROWS_NEWER = (
    {"source": "claude", "id": "c1", "alerted_at": "2026-08-10T13:59:00Z"},
    {"source": "claude", "id": "c2", "alerted_at": "2026-08-10T13:55:00Z"},
)
CODEX_ROWS = (
    {"source": "codex", "key": "x1", "alerted_at": "2026-08-10T13:58:00Z",
     "created_at": "2026-08-10T13:58:00Z"},
    {"source": "codex", "key": "x2", "alerted_at": "2026-08-10T13:56:00Z",
     "created_at": "2026-08-10T13:56:00Z"},
)


def _all_state(claude_rows, codex_rows):
    return sources.compose_all_state(
        _state("claude", claude_rows), _state("codex", codex_rows),
    )


def test_reordering_the_alert_union_moves_the_all_data_version():
    """§2.9. The version material omitted alerts entirely, so two materially
    different unions shared one `data_version`."""
    a = _all_state(CLAUDE_ROWS, CODEX_ROWS)
    b = _all_state(CLAUDE_ROWS_NEWER, CODEX_ROWS)
    assert a.data_version != b.data_version


def test_shifting_every_instant_moves_the_version_though_the_order_does_not():
    """The case above changes the union's ORDER as well as its instants, so a
    version material hashing only `(source, id)` in list order would still pass
    it. Here every row moves by the same hour, the order is identical, and only
    the instants differ — which is what pins the instant into the material."""
    def shift(rows):
        return tuple(
            dict(row, alerted_at=row["alerted_at"].replace("T13:", "T14:"))
            for row in rows
        )

    a = _all_state(CLAUDE_ROWS, CODEX_ROWS)
    b = _all_state(shift(CLAUDE_ROWS), shift(CODEX_ROWS))

    def order(state):
        return [row.get("id") or row.get("key") for row in state.data["alerts"]["rows"]]

    assert order(a) == order(b) == ["c1", "x1", "c2", "x2"]
    assert a.data_version != b.data_version


def test_the_all_state_publishes_the_union_it_hashed():
    a = _all_state(CLAUDE_ROWS, CODEX_ROWS)
    assert [row.get("id") or row.get("key") for row in a.data["alerts"]["rows"]] == [
        "c1", "x1", "c2", "x2",
    ]


def test_a_claude_alert_mutation_advances_the_dispatch_signature(
    ns, monkeypatch, tmp_path,
):
    """§2.9. `codex_stats_digest` covers the Codex alert tables; nothing
    covered Claude's, so a fired Claude alert could leave the idle path
    short-circuiting on a retained prior bundle."""
    import _lib_snapshot_cache as sc

    stats = ns["open_db"]()
    cache = ns["open_cache_db"]()
    try:
        before = sc.compute_signature(
            cache, stats, generation=0,
            claude_stats_digest=sources.claude_stats_digest(stats),
        )
        _seed_budget_alert(
            stats, vendor="claude", threshold=90,
            period_start_at="2026-08-03T00:00:00Z",
            crossed_at="2026-08-10T13:40:00Z",
            alerted_at="2026-08-10T13:59:00Z",
        )
        stats.commit()
        after = sc.compute_signature(
            cache, stats, generation=0,
            claude_stats_digest=sources.claude_stats_digest(stats),
        )
    finally:
        cache.close()
        stats.close()
    assert before != after


def test_arming_an_existing_claude_alert_advances_the_signature_too(ns):
    """Arming is an in-place UPDATE that adds no row anywhere."""
    import _lib_snapshot_cache as sc

    stats = ns["open_db"]()
    cache = ns["open_cache_db"]()
    try:
        _seed_budget_alert(
            stats, vendor="claude", threshold=90,
            period_start_at="2026-08-03T00:00:00Z",
            crossed_at="2026-08-10T13:40:00Z",
            alerted_at=None,
        )
        stats.commit()
        before = sc.compute_signature(
            cache, stats, generation=0,
            claude_stats_digest=sources.claude_stats_digest(stats),
        )
        stats.execute(
            "UPDATE budget_milestones SET alerted_at = '2026-08-10T13:59:00Z' "
            "WHERE vendor = 'claude'"
        )
        stats.commit()
        after = sc.compute_signature(
            cache, stats, generation=0,
            claude_stats_digest=sources.claude_stats_digest(stats),
        )
    finally:
        cache.close()
        stats.close()
    assert before != after


# ── Task 5: the both-provider alert fixture ───────────────────────────────

def test_all_combined_retains_the_offset_spelling_in_sqlite(tmp_path):
    """Not the rendered order — the stored bytes.

    `_seed_budget_milestone` writes both timestamps through `_iso`, which
    canonicalizes every aware datetime to UTC `Z`. Had the override taken a
    datetime, `15:58+02:00` would be stored as `13:58Z`, the fixture would
    still render in the right order, and it would prove nothing (§5.3).
    """
    import pathlib
    import sqlite3
    import subprocess

    repo = pathlib.Path(__file__).resolve().parent.parent
    subprocess.run(
        ["python3", str(repo / "bin" / "build-dashboard-fixtures.py"),
         "--out", str(tmp_path)],
        check=True, capture_output=True,
    )
    db = tmp_path / "all-combined" / ".local" / "share" / "cctally" / "stats.db"
    conn = sqlite3.connect(db)
    try:
        stored = [
            row[0] for row in conn.execute(
                "SELECT alerted_at FROM budget_milestones WHERE vendor = 'codex' "
                "ORDER BY threshold"
            )
        ]
    finally:
        conn.close()
    assert "2026-04-16T15:58:00+02:00" in stored, stored


def test_the_committed_golden_lists_both_providers_alerts_in_firing_order():
    """§5.3. The interleave means any source-grouped ordering fails here."""
    import json
    import pathlib

    golden = json.loads(
        (pathlib.Path(__file__).resolve().parent / "fixtures" / "dashboard"
         / "all-combined" / "golden-data.json").read_text()
    )
    rows = golden["sources"]["all"]["data"]["alerts"]["rows"]
    assert [(row["source"], row["axis"], row["threshold"]) for row in rows] == [
        ("claude", "budget", 50),        # 13:59Z
        ("codex", "codex_budget", 60),   # 15:58+02:00 IS 13:58Z
        ("claude", "budget", 75),        # 13:57Z
        ("codex", "codex_budget", 80),   # 13:56Z
    ]
    # The published instants are canonical, and they are the FIRING instants —
    # the crossings are a different permutation, so a regression to sorting on
    # `crossed_at` would reorder these.
    assert [row["alerted_at"] for row in rows] == [
        "2026-04-16T13:59:00Z", "2026-04-16T13:58:00Z",
        "2026-04-16T13:57:00Z", "2026-04-16T13:56:00Z",
    ]
