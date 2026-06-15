"""Regression guards for the per-migration golden builders (issues #194, #197).

Two distinct drift classes both surface ONLY on a builder regen, because the
per-migration golden tests read the COMMITTED fixtures and never exercise the
builders:

#194 — marker presence. Issue #140 moved the ``schema_migrations`` marker stamp
OUT of the handlers and into the dispatcher's central ``_stamp_applied``. A
builder that only calls ``handler(conn)`` no longer produces the marker; a full
regen wrote markerless ``post.sqlite`` goldens and silently broke
``test_migration_002`` / ``test_migration_008``. The ``test_builder_*_carries_marker``
tests below exercise the builder itself to catch that.

#197 — byte idempotency. The conversation cache goldens build ``pre.sqlite`` via
``_cctally_db._apply_cache_schema``, which always emits the CURRENT full cache
schema — so as later migrations add tables / reshape the FTS5 index, the
committed goldens silently fell behind, and a full regen rewrote them with a
different on-disk schema. Combined with cache 001's wall-clock self-stamp (the
#140 carve-out) and builder schema evolution (e.g. the #181 ``speed`` column),
a full regen dirtied ~24 committed goldens that the maintainer then had to
hand-revert. The ``test_*_byte_idempotent`` tests below rebuild every
builder-produced golden into a throwaway dir and assert it matches the
committed fixture, in two tiers. On the SQLite version that produced the
goldens (the maintainer's machine, where they are regenerated) it is a STRICT
byte compare — writer-version header bytes 96-99 normalized — the full #197
guarantee that a regen will not dirty the committed file. On any OTHER SQLite
version the on-disk PAGE layout legitimately differs (page allocation /
freelist / B-tree balancing are not portable across library versions), so the
byte tier falls back to a version-independent SEMANTIC compare: the canonical
SQL dump plus the header pragmas iterdump omits. That keeps the guard green on
the public Linux CI matrix — which runs a different libsqlite3 than the macOS
self-hosted gate — while still catching real builder/data drift (issue #199).
They fail LOUDLY at the commit that introduces drift, instead of letting it
accumulate.

Coverage note: 005/006 per-migration goldens have NO builder script (frozen
artifacts hand-built once; their tests only READ them) and are not in the
``_apply_cache_schema`` drift class (tiny explicit stats schemas), so they are
intentionally out of the byte-idempotency guard — there is nothing to rebuild
them against.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest


BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
BUILDER_PATH = BIN_DIR / "build-migrations-fixtures.py"
BUILDER_009_010_PATH = BIN_DIR / "build-migration-009-010-fixtures.py"
PER_MIGRATION_ROOT = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
)

# Bytes 96-99 of the SQLite header carry SQLITE_VERSION_NUMBER (the
# library write-version). _fixture_builders' atexit hook zeros them in the
# COMMITTED fixtures; a freshly-built file inside a running test has NOT yet
# been through that hook, so we zero the field in-memory on both sides before
# comparing (mirrors _fixture_builders.normalize_sqlite_writer_version without
# mutating the in-tree committed fixture).
_WRITER_VERSION_OFFSET = 96


def _load_module(name: str, path: Path):
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = ilu.spec_from_file_location(name, path)
    mod = ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def builder_module():
    """Import ``bin/build-migrations-fixtures.py`` so its per-migration build
    functions can be called against a tmp scenario dir."""
    return _load_module("build_migrations_fixtures", BUILDER_PATH)


@pytest.fixture(scope="module")
def builder_009_010_module():
    """Import ``bin/build-migration-009-010-fixtures.py`` (the second
    per-migration builder, for the 009/010 recompute stats goldens)."""
    return _load_module("build_migration_009_010_fixtures", BUILDER_009_010_PATH)


def _has_marker(db_path: Path, name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ).fetchone() is not None
    finally:
        conn.close()


def _markers(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()


def _normalized_bytes(path: Path) -> bytes:
    """Read *path* and zero the SQLite writer-version header field (bytes
    96-99) in-memory, so two DBs built by different SQLite library versions
    compare equal on everything BUT that version stamp."""
    raw = bytearray(path.read_bytes())
    if len(raw) >= _WRITER_VERSION_OFFSET + 4:
        raw[_WRITER_VERSION_OFFSET:_WRITER_VERSION_OFFSET + 4] = b"\x00\x00\x00\x00"
    return bytes(raw)


def _semantic_content(path: Path):
    """Version-portable content fingerprint of a SQLite DB.

    SQLite's physical encoding is not stable across library versions, in TWO
    ways, neither of which is logical content:

    * page layout — page allocation / freelist ordering / B-tree balancing
      differ, so the raw bytes differ;
    * FTS5 stores its inverted index as version-dependent binary segments in
      shadow tables (``<fts>_data`` / ``_idx`` / ``_docsize`` / ``_content`` /
      ``_config``), so even ``iterdump`` — which serializes those BLOBs verbatim
      — differs across versions for identical indexed text.

    So the fingerprint is built from a throwaway in-memory COPY (the committed
    golden is NEVER mutated) with every FTS5 virtual table dropped — which
    cascades its shadow tables away — leaving:

      * ``user_version`` / ``application_id`` — header pragmas iterdump omits
        but that matter for a migration fixture (the migration version);
      * the FTS5 ``CREATE VIRTUAL TABLE`` statements (schema — a tokenizer /
        column / ``content=`` change still shows here);
      * the canonical SQL dump of every remaining real table (schema + rows in
        B-tree key order). The conversation FTS5 is external-content
        (``content='conversation_messages'``), so the indexed text lives in a
        real table that STAYS in this dump — a change to what is indexed is
        still caught.

    Both sides are fingerprinted by the SAME interpreter, so any version quirk
    in iterdump's own formatting cancels out of the comparison."""
    src = sqlite3.connect(path)
    try:
        user_version = src.execute("PRAGMA user_version").fetchone()[0]
        application_id = src.execute("PRAGMA application_id").fetchone()[0]
        fts5_vtabs = [
            r[0] for r in src.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND sql LIKE '%USING fts5%'"
            )
        ]
        fts5_schema = tuple(sorted(
            r[0] for r in src.execute(
                "SELECT sql FROM sqlite_master WHERE sql LIKE '%USING fts5%'"
            )
        ))
        mem = sqlite3.connect(":memory:")
        try:
            src.backup(mem)
            for vtab in fts5_vtabs:
                mem.execute(f'DROP TABLE "{vtab}"')
            dump = "\n".join(mem.iterdump())
        finally:
            mem.close()
    finally:
        src.close()
    return (user_version, application_id, fts5_schema, dump)


def _discover_per_migration_builders(mod) -> dict:
    """Map ``<scenario-dir-name> -> build fn`` for every
    ``build_per_migration_*`` callable in *mod*. Auto-discovery means a future
    builder is byte-guarded the moment it lands (no test edit), and the
    scenario name is derived from the function name (the two are kept identical
    by convention)."""
    out = {}
    prefix = "build_per_migration_"
    for attr in dir(mod):
        if attr.startswith(prefix) and callable(getattr(mod, attr)):
            out[attr[len(prefix):]] = getattr(mod, attr)
    return out


def _assert_dir_byte_idempotent(committed_dir: Path, rebuilt_dir: Path) -> None:
    """Every committed ``*.sqlite`` in *committed_dir* must rebuild to its
    committed counterpart in *rebuilt_dir*, in two tiers (issues #197, #199):

    * STRICT byte idempotency (writer-version-normalized) when the running
      SQLite library is the version that produced the committed goldens — the
      maintainer's machine, where the goldens are regenerated. The full #197
      guarantee: a regen will not dirty the committed file.
    * SEMANTIC idempotency when the SQLite version differs (e.g. the public
      Linux CI matrix, #199). The raw page layout legitimately differs across
      library versions, so the byte tier cannot hold; fall back to the
      version-independent content fingerprint (SQL dump + header pragmas). This
      still catches real builder/data drift, tolerating only layout churn."""
    committed = sorted(committed_dir.glob("*.sqlite"))
    assert committed, f"no committed *.sqlite in {committed_dir}"
    for golden in committed:
        rebuilt = rebuilt_dir / golden.name
        assert rebuilt.exists(), (
            f"builder did not produce {golden.name} for "
            f"{committed_dir.name} (committed golden has no rebuilt counterpart)"
        )
        if _normalized_bytes(rebuilt) == _normalized_bytes(golden):
            continue  # strict byte-idempotent (goldens' origin SQLite version)
        # Raw bytes differ: a real builder/data change OR merely a different
        # SQLite version's page layout. The content fingerprint discriminates —
        # equal ⇒ pure layout churn (tolerated); unequal ⇒ genuine drift.
        assert _semantic_content(rebuilt) == _semantic_content(golden), (
            f"{committed_dir.name}/{golden.name} is NOT idempotent — a full "
            f"builder regen would change the committed golden's CONTENT (SQL "
            f"dump or header pragmas differ, not merely SQLite page layout). "
            f"Run the builder and commit the refreshed fixture (issue #197)."
        )


# Scenario names are derived once at import via auto-discovery against the
# real builder module, so parametrize IDs stay in lockstep with the builders.
_BUILDERS = _discover_per_migration_builders(
    _load_module("build_migrations_fixtures", BUILDER_PATH)
)


@pytest.mark.parametrize("scenario", sorted(_BUILDERS))
def test_per_migration_golden_byte_idempotent(
    builder_module, scenario, tmp_path
):
    """Each ``build-migrations-fixtures.py`` per-migration builder must rebuild
    its committed goldens byte-for-byte (issue #197)."""
    build_fn = getattr(builder_module, f"build_per_migration_{scenario}")
    out_dir = tmp_path / scenario
    build_fn(out_dir)
    _assert_dir_byte_idempotent(PER_MIGRATION_ROOT / scenario, out_dir)


@pytest.mark.parametrize(
    "scenario,build_fn_name",
    [
        ("009_recompute_five_hour_blocks_dedup_fix", "build_009"),
        ("010_recompute_percent_milestones_dedup_fix", "build_010"),
    ],
)
def test_009_010_recompute_golden_byte_idempotent(
    builder_009_010_module, scenario, build_fn_name, tmp_path, monkeypatch
):
    """The second builder (``build-migration-009-010-fixtures.py``) must also
    rebuild its committed goldens byte-for-byte. It writes to a module-global
    ``FIX_BASE``, so redirect that at a tmp dir and compare (issue #197)."""
    monkeypatch.setattr(builder_009_010_module, "FIX_BASE", tmp_path)
    getattr(builder_009_010_module, build_fn_name)()
    _assert_dir_byte_idempotent(PER_MIGRATION_ROOT / scenario, tmp_path / scenario)


def test_builder_002_post_carries_marker(builder_module, tmp_path):
    """The 002 builder must stamp the 002 marker into post.sqlite — pre-fix the
    UPDATE matched zero rows post-#140, leaving it markerless (issue #194)."""
    scenario = tmp_path / "002_conversation_messages_backfill"
    builder_module.build_per_migration_002_conversation_messages_backfill(
        scenario
    )
    post = scenario / "post.sqlite"
    assert post.exists(), "builder did not write 002 post.sqlite"
    assert _has_marker(post, "002_conversation_messages_backfill"), (
        "002 post.sqlite must carry the 002 marker — the builder must apply "
        "the dispatcher's central _stamp_applied after the handler (issue #194)"
    )


def test_builder_008_post_carries_marker(builder_module, tmp_path):
    """The 008 builder must stamp the 008 marker into post.sqlite — pre-fix it
    stamped nothing post-#140, leaving it markerless (issue #194)."""
    scenario = tmp_path / "008_recompute_weekly_cost_snapshots_dedup_fix"
    builder_module.build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        scenario
    )
    post = scenario / "post.sqlite"
    assert post.exists(), "builder did not write 008 post.sqlite"
    assert _has_marker(
        post, "008_recompute_weekly_cost_snapshots_dedup_fix"
    ), (
        "008 post.sqlite must carry the 008 marker — the builder must apply "
        "the dispatcher's central _stamp_applied after the handler (issue #194)"
    )


def test_builder_008_pre_cache_stays_clean(builder_module, tmp_path):
    """The 008 builder runs the handler's eager cache-migration step against a
    throwaway COPY, so the committed pre-cache sidecar stays a clean pre-008
    state — only the 001 marker, no downstream cache markers, and no stray
    work-cache file left behind (issue #194)."""
    scenario = tmp_path / "008_recompute_weekly_cost_snapshots_dedup_fix"
    builder_module.build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        scenario
    )
    pre_cache = scenario / "pre-cache.sqlite"
    assert pre_cache.exists(), "builder did not write 008 pre-cache.sqlite"
    assert _markers(pre_cache) == ["001_dedup_highest_wins"], (
        "pre-cache.sqlite must carry ONLY the 001 marker (clean pre-008 cache "
        "state); any downstream cache marker means the builder ran the eager "
        "cache-migration step against the in-tree fixture instead of a copy "
        "(issue #194)"
    )
    # The throwaway cache copy must be cleaned up — the committed fixture dir
    # holds only pre/pre-cache/post .sqlite.
    assert not (scenario / "_work_cache.db").exists(), (
        "builder left its throwaway _work_cache.db behind"
    )
