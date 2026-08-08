"""#496 S6 F21 — every surface that describes stats.db durability (spec §8).

Thirteen surfaces told users that `stats.db` holds history which cannot be
rebuilt. Since the DB journal redesign that is true only of a **pre-cutover**
installation with no retained journal data. On every installation that has
retained journal data, `stats.db` is a disposable index derived from it:
cctally rebuilds or heals it automatically and deleting it loses nothing.

The approved wording, stated once in spec §8.1 and reused with the length
adapted but never the substance:

    When cctally has retained journal data for this installation, `stats.db`
    is a disposable index derived from it: cctally rebuilds it or heals it
    automatically, and deleting it loses nothing. On a pre-cutover
    installation with no retained journal data, `stats.db` may be the only
    copy of your recorded history — cctally preserves it, refuses to replace
    it with an empty rebuild, and points you at guarded repair instead.

**Absence of the old phrase is not sufficient**, so each surface is asserted to
state BOTH cases positively. Every literal below is hardcoded here and never
imported from the source it checks: a tripwire that imports the constant it
tests passes by construction.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Both concepts, per surface, as positive statements.
#:
#: `journal_backed` and `journal_named` together are what make the
#: journal-backed case concrete: a surface has to say the index is disposable
#: or rebuilt AND name the journal it is rebuilt from.
#:
#: `pre_cutover` is deliberately NARROWER than the plan's draft, which accepted
#: the word "preserve". Several of these surfaces already contained "preserves
#: the corrupt original before replacing anything" for an unrelated reason, so
#: accepting that word would let a surface pass this test while still saying
#: nothing at all about a pre-cutover installation.
REQUIRED = {
    "journal_backed": ("disposable", "rebuild"),
    "journal_named": ("journal",),
    "pre_cutover": ("only copy",),
}

#: The blanket claim, in every spelling it shipped in.
RETIRED_CLAIMS = ("not re-derivable from jsonl", "non-re-derivable")

#: `(path, anchor, lines_before, lines_after)`.
#:
#: The anchor locates the surface by content rather than by a line number,
#: because every line number in the spec has since drifted. The window is
#: stated per surface and is small enough that no two surfaces overlap — the
#: three `docs/runtime-data.md` entries are a single line each for exactly that
#: reason, so one of them cannot pass by borrowing another's wording.
SURFACES = [
    (
        "docs/runtime-data.md",
        "All persistent state lives under",
        0, 0,
    ),
    (
        "docs/runtime-data.md",
        "| `stats.db` | ",
        0, 0,
    ),
    (
        "docs/runtime-data.md",
        "| `stats.db.bak-*` |",
        0, 0,
    ),
    (
        "docs/commands/doctor.md",
        "- `db.integrity` —",
        0, 0,
    ),
    (
        "docs/commands/db.md",
        "no `--force` race bypass",
        4, 6,
    ),
    (
        "bin/_cctally_parser.py",
        'db_recover.add_argument(\n        "--yes"',
        0, 11,
    ),
    (
        "bin/_cctally_core.py",
        "stats.db appears corrupt or unreadable",
        1, 12,
    ),
    (
        "bin/_cctally_db.py",
        "cctally: repairing stats.db",
        1, 10,
    ),
    (
        "bin/_lib_doctor.py",
        "reports corruption. ",
        1, 8,
    ),
    (
        "bin/_cctally_db.py",
        "class StatsDbCorruptError",
        0, 22,
    ),
    (
        "bin/_cctally_core.py",
        "#279 S1 F4: probe connect",
        0, 10,
    ),
    (
        "bin/cctally",
        "except StatsDbCorruptError as exc:",
        0, 8,
    ),
    (
        "bin/_cctally_db.py",
        "def cmd_db_recover",
        0, 16,
    ),
]


def _read(path):
    return (ROOT / path).read_text()


def _normalize(text):
    """Read a wrapped surface the way a person reads it.

    A source comment or a message assembled from adjacent string literals wraps
    mid-phrase — `may be the only\n# copy of your recorded history` — so a raw
    substring test would demand the wording be laid out to suit the test rather
    than to read well. Comment markers and literal joins are removed and runs of
    whitespace collapsed; nothing else about the text is changed.
    """
    text = re.sub(r'"\s*\n\s*"', "", text)
    text = re.sub(r"(?m)^\s*#\s?", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _read_surface(path, anchor, before, after):
    text = _read(path)
    assert anchor in text, f"{path}: anchor not found: {anchor!r}"
    assert text.count(anchor) == 1, (
        f"{path}: anchor is not unique ({text.count(anchor)} matches): {anchor!r}"
    )
    # The line index comes from the anchor's CHARACTER offset, not from a
    # search for its first line: three `db_recover.add_argument(` calls sit in
    # a row, and matching on the first line alone silently windowed the wrong
    # one while the uniqueness check above still passed.
    index = text[:text.index(anchor)].count("\n")
    lines = text.splitlines()
    start = max(0, index - before)
    return "\n".join(lines[start:index + after + 1])


def test_there_are_exactly_thirteen_surfaces():
    """C8: F21 spans thirteen surfaces, not the five the issue body named."""
    assert len(SURFACES) == 13
    assert len(set(SURFACES)) == 13


def test_the_scan_covers_the_extensionless_entry_point():
    """A structural scan that globs `*.py` misses `bin/cctally` entirely."""
    assert any(path.endswith("bin/cctally") for path, _, _, _ in SURFACES)


@pytest.mark.parametrize(
    "surface", SURFACES, ids=[f"{p}:{a[:32]}" for p, a, _, _ in SURFACES]
)
def test_every_durability_surface_states_both_cases(surface):
    path, anchor, before, after = surface
    text = _normalize(_read_surface(path, anchor, before, after))
    for concept, words in REQUIRED.items():
        assert any(word in text for word in words), (
            f"{path} ({anchor!r}) does not state the {concept} case; "
            f"expected one of {words} in:\n{text}"
        )


@pytest.mark.parametrize(
    "path", sorted({p for p, _, _, _ in SURFACES})
)
def test_no_surface_file_still_makes_the_blanket_claim(path):
    text = _read(path).lower()
    for claim in RETIRED_CLAIMS:
        assert claim not in text, (
            f"{path} still describes stats.db as {claim!r}"
        )


def test_the_retired_claim_literals_are_not_imported_from_the_source():
    """Non-vacuity for the scan above: it must be able to fire.

    Both literals are spelled out in this file. Feeding one of them through the
    same reader the scan uses proves the check is a real substring test rather
    than a comparison that can never match.
    """
    for claim in RETIRED_CLAIMS:
        assert claim in (
            "stats.db is the non-re-derivable DB; not re-derivable from JSONL"
        ).lower()


# --------------------------------------------------------------------------
# §8.4 — `docs/runtime-data.md` gains the rows it never had
# --------------------------------------------------------------------------

#: Every artifact the corruption-recovery and retention machinery leaves on
#: disk. Before #496 S6 the file map documented the cache incident pair and
#: nothing else, so a user who found 18 GiB under `quarantine/` had no
#: documented answer for what it was or whether deleting it was safe.
#:
#: The key is a UNIQUE anchor into the file map's table, so `_read_surface`
#: windows exactly one row and every assertion below is made INSIDE that row.
#: A whole-file substring test cannot do this job, and the previous version of
#: this file made exactly that mistake: it asserted four literals over the
#: whole document, so deleting the retention sentence from any single row left
#: it green because some other row still carried each literal.
#:
#: The concepts are stated PER ROW rather than uniformly, because the rows do
#: not all make the same claim. `bounded_by` is what limits how much of this
#: artifact accumulates; `reclaimed_by` is how it goes away, which is the "is
#: it safe to delete" question that produced F13; `never_swept` and
#: `unclassified` appear only on the rows that are about evidence the
#: classifier may fail to decide about. Requiring them everywhere would assert
#: a claim broader than the file makes.
INCIDENT_ARTIFACT_ROWS = {
    "| `quarantine/cache.db-*`, `logs/cache.db-corruption-forensics-*` |": {
        "bounded_by": ("storage.artifact_retention",),
        "reclaimed_by": ("cctally db prune",),
        "never_swept": ("never deletes",),
        "unclassified": ("cause cctally could not determine",),
    },
    "| `quarantine/stats.db-*` |": {
        "bounded_by": ("storage.artifact_retention",),
        "reclaimed_by": ("cctally db prune",),
        "never_swept": ("never deleted automatically",),
        "unclassified": ("cause cctally could not determine",),
    },
    "| `logs/stats.db-corruption-forensics-*.json` and": {
        "bounded_by": ("bounded by the same policy",),
        "reclaimed_by": ("reclaimed together with it",),
    },
    "| `logs/stats-rebuild-*.json` |": {
        "bounded_by": ("bounded by the same policy",),
        "reclaimed_by": ("cctally db prune",),
    },
    "| `logs/stats-heal-events.json` |": {
        # A fixed-size ring is bounded by its own writer, not by the policy,
        # and the row has to say so rather than leave a reader looking for a
        # `db prune` that will never touch it.
        "bounded_by": ("capped at 50 entries",),
        "reclaimed_by": ("needs no reclamation",),
    },
    "| `quarantine/stats.db-*/classification.json`,": {
        "bounded_by": ("rewritten whenever the cause is re-determined",),
        "reclaimed_by": ("retained until you classify or remove it by hand",),
        "never_swept": ("never deleted automatically",),
        "unclassified": ("unclassified evidence",),
    },
    "| `stats.db.bak-*` |": {
        "bounded_by": ("retention sweeps",),
        "reclaimed_by": ("cctally db prune --include-backups",),
        "never_swept": ("never deleted automatically",),
    },
}


@pytest.mark.parametrize("anchor", sorted(INCIDENT_ARTIFACT_ROWS))
def test_runtime_data_documents_every_incident_artifact(anchor):
    """The row is present and appears exactly once.

    Uniqueness is the assertion, not decoration: it is what makes the
    single-line window below one row rather than whichever row happened to
    match first.
    """
    _read_surface("docs/runtime-data.md", anchor, 0, 0)


@pytest.mark.parametrize(
    "anchor,concepts",
    sorted(INCIDENT_ARTIFACT_ROWS.items()),
    ids=[anchor[:44] for anchor in sorted(INCIDENT_ARTIFACT_ROWS)],
)
def test_every_incident_artifact_row_states_its_retention(anchor, concepts):
    """Each row states, IN ITSELF, what bounds it and how it is reclaimed.

    A row that only named the file would answer "what is this" and leave "is it
    safe to delete" — the question that produced F13 — unanswered. Asserting it
    over the whole document instead of over the row is the same over-claim:
    it reports the corpus as documented while any one row says nothing.
    """
    row = _normalize(_read_surface("docs/runtime-data.md", anchor, 0, 0))
    for concept, words in concepts.items():
        assert any(word in row for word in words), (
            f"the {anchor!r} row does not state the {concept} case; "
            f"expected one of {words} in:\n{row}"
        )


# --------------------------------------------------------------------------
# §8.6 — the behaviour the corrected wording describes
# --------------------------------------------------------------------------
#
# The documentation change above is only honest if the product does the two
# things it now claims. These certify both, so a later change that made the
# no-journal guard rebuild instead of preserving would fail here rather than
# leaving thirteen surfaces describing behaviour that had moved.

import sys  # noqa: E402

_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

_W1 = 1767830400  # 2026-01-08T00:00:00Z


@pytest.fixture
def store(monkeypatch, tmp_path):
    loaded = load_script()
    redirect_paths(loaded, monkeypatch, tmp_path)
    return loaded


@pytest.fixture
def no_spawn(monkeypatch):
    """Record the detached heal spawn instead of launching a real process."""
    import _cctally_update

    spawned = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    return spawned


def _journal_one_observation():
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    jr.run_stats_ingest(mode="authoritative")


def _mangle(path):
    with open(path, "r+b") as handle:
        handle.write(b"not a database " * 200)


def test_a_journal_backed_index_is_rebuilt_and_loses_nothing(store, no_spawn):
    import _cctally_core as core
    import _cctally_db
    import _cctally_store as st
    import types

    _journal_one_observation()
    opened = core.open_db()
    try:
        before = [
            tuple(row) for row in opened.execute(
                "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
                "ORDER BY id"
            )
        ]
    finally:
        opened.close()
    assert before, "the fixture journaled nothing, so nothing can be rebuilt"

    _mangle(core.DB_PATH)
    with pytest.raises(_cctally_db.StatsHealDeferred):
        core.open_db()
    assert st.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0

    healed = core.open_db()
    try:
        after = [
            tuple(row) for row in healed.execute(
                "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
                "ORDER BY id"
            )
        ]
    finally:
        healed.close()
    assert after == before


def test_a_no_journal_index_is_preserved_and_the_error_names_db_repair(store):
    """The pre-cutover case the corrected wording is about.

    With no retained journal data there is nothing to rebuild FROM, so a
    rebuild would publish an EMPTY index over the only copy of the history.
    The heal declines and `open_db` raises its guided error instead.
    """
    import _cctally_core as core
    import _cctally_db
    import _cctally_journal as jr

    opened = core.open_db()
    opened.close()
    high_water = jr.journal_high_water()
    assert high_water is None or high_water[1] == 0, (
        "this fixture is only the no-journal case while the journal is empty; "
        f"it reported {high_water!r}"
    )

    before = pathlib.Path(core.DB_PATH).read_bytes()
    _mangle(core.DB_PATH)
    damaged = pathlib.Path(core.DB_PATH).read_bytes()

    with pytest.raises(_cctally_db.StatsDbCorruptError) as raised:
        core.open_db()

    message = str(raised.value)
    assert "db repair --db stats --yes" in message
    assert "only copy" in message
    assert "never auto-recreated" in message
    assert pathlib.Path(core.DB_PATH).read_bytes() == damaged, (
        "the damaged index was replaced; the no-journal guard must preserve it"
    )
    assert damaged != before, "the fixture did not actually damage anything"
    quarantine = core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == [], (
        "nothing may be quarantined when the heal declines"
    )
