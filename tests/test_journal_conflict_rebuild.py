"""Issue #374 — end-to-end rebuild over a journal carrying all three conflict
classes (acceptance criterion 1).

The committed fixture is produced by `bin/build-journal-conflict-fixture.py` and
reproduces the fourteen production groups at their observed shapes with wholly
SYNTHETIC identifiers. Before #374 this journal made `rebuild_stats_index` abort
with `same revision conflict for … rev 0`, so a stats.db at the previous index
epoch could never rebuild and the dashboard refused to start.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

import _lib_journal as J
from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "journal-conflicts"
SEGMENT = "observations-2026-07.jsonl"

SYNTHETIC_CUTOVER_ACCOUNT = "ac00000000000000000000000000cafe"

_EXPECTED_IDS = frozenset(
    [f"sa:o:5eed{n:012d}" for n in range(7)]
    + [f"wcs:o:5eed{100:012d}:2026-07-13"]
    + [f"wcs:o:5eed{200 + n:012d}:2026-07-13" for n in range(4)]
    + [f"fhbc:unattributed:{4000000 + n}" for n in range(2)]
)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _install_conflict_fixture():
    import _cctally_core

    journal_dir = _cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_DIR / SEGMENT, journal_dir / SEGMENT)


def test_builder_output_is_deterministic(tmp_path):
    """A regenerate must be a byte no-op, or the committed fixture drifts."""
    out = tmp_path / "regen"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "build-journal-conflict-fixture.py"),
         str(out)],
        check=True, capture_output=True,
    )
    assert (out / SEGMENT).read_bytes() == (FIXTURE_DIR / SEGMENT).read_bytes()


def test_fixture_carries_no_production_derived_identifier():
    """#373's lesson: this fixture is published to the public mirror, so every
    identifier must be visibly synthetic."""
    text = (FIXTURE_DIR / SEGMENT).read_text()
    assert "/Users/" not in text
    assert "/home/" not in text
    assert "@" not in text
    for line in text.splitlines():
        record = J.decode_line(line.encode("utf-8"))
        assert record is not None
        event_id = record["id"]
        assert event_id == "accounts-cutover-v1" or "5eed" in event_id or (
            event_id.startswith("fhbc:unattributed:4000"))


def test_rebuild_over_conflicted_journal_completes_and_reports(ns):
    import _cctally_journal as jr

    _install_conflict_fixture()

    result = jr.rebuild_stats_index()

    assert {c.event_id for c in result.conflicts} == _EXPECTED_IDS
    assert len(result.conflicts) == 14
    assert all(c.rev == 0 for c in result.conflicts)
    # The nine-variant group reports every distinct variant, not just two.
    nine = next(c for c in result.conflicts if c.event_id.endswith(
        f"wcs:o:5eed{100:012d}:2026-07-13"))
    assert len(nine.content_hashes) == 9
    assert nine.selected_hash in nine.content_hashes
    # Conflicts are reported in a deterministic order.
    assert [c.event_id for c in result.conflicts] == sorted(
        c.event_id for c in result.conflicts)


def test_rebuild_materializes_the_lowest_sequence_provisional_winner(ns):
    import _cctally_core
    import _cctally_journal as jr

    _install_conflict_fixture()
    jr.rebuild_stats_index()

    conn = _cctally_core.open_db()
    try:
        # Class A: the FIRST-written variant wins, and the rebuild's legacy
        # normalisation stamps it with the cutover op's account.
        assert conn.execute(
            "SELECT account_key FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:o:5eed000000000000'"
        ).fetchone()[0] == SYNTHETIC_CUTOVER_ACCOUNT
        # Class B: the first (lowest) cost, not the highest and not the degraded
        # zero — the selector never guesses "best", only "first".
        assert conn.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots "
            f"WHERE journal_id = 'wcs:o:5eed{100:012d}:2026-07-13'"
        ).fetchone()[0] == pytest.approx(1430.30)
        assert conn.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots "
            f"WHERE journal_id = 'wcs:o:5eed{200:012d}:2026-07-13'"
        ).fetchone()[0] == pytest.approx(410.5)
        # Class C: the first block close, with its children attached.
        block = conn.execute(
            "SELECT id, last_updated_at_utc, created_at_utc FROM five_hour_blocks "
            "WHERE journal_id = 'fhbc:unattributed:4000000'"
        ).fetchone()
        assert block[1] == "2026-07-18T09:58:00Z"
        assert block[2] == "2026-07-18T05:02:00Z"
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_block_models WHERE block_id = ?",
            (block[0],)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_block_projects WHERE block_id = ?",
            (block[0],)).fetchone()[0] == 1
    finally:
        conn.close()


def test_rebuild_over_conflicted_journal_is_deterministic(ns, tmp_path):
    """Two independent rebuilds of the same conflicted journal must agree — the
    provisional winner is a pure function of the journal, never a race."""
    import _cctally_journal as jr

    _install_conflict_fixture()
    first = jr.rebuild_stats_index(target_path=str(tmp_path / "a.db"))
    second = jr.rebuild_stats_index(target_path=str(tmp_path / "b.db"))

    assert [c.to_dict() for c in first.conflicts] == [
        c.to_dict() for c in second.conflicts]
    assert first.rows_by_table == second.rows_by_table


def test_db_rebuild_command_exits_zero_over_the_conflicted_journal(ns, capsys):
    """The epoch-1002 wedge itself: `cctally db rebuild --db stats` exited 3
    with `same revision conflict …`. It must now complete and report."""
    import argparse

    _install_conflict_fixture()
    capsys.readouterr()

    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False)) == 0

    out = capsys.readouterr().out
    assert "14 quarantined same-revision group(s)" in out
    assert "cctally db rederive --family claude-usage" in out
