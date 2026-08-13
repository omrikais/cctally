"""#496 S1 — incident identity and schema-text invariance.

Covers F18 (both `quota_projection_state` definitions must store byte-identical
SQL so `_stats_schema_fingerprint` is invariant to which path created the
table), F3 (`rebuild_stats_index` requires a structured `RebuildContext`), and
the schemaVersion 2 preservation manifest.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import load_script, redirect_paths

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
CCTALLY = BIN / "cctally"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ==========================================================================
# F18 — the two duplicated CREATE TABLE statements
# ==========================================================================

_FRESH_MARKER = "CREATE TABLE IF NOT EXISTS quota_projection_state ("
_BACKSTOP_PREFIX = "CREATE TABLE quota_projection_state"


def _extract_fresh_definition(source: str) -> str:
    """The multi-line definition inside `_apply_quota_projection_schema`'s
    `executescript` block, returned as one executable statement."""
    start = source.index(_FRESH_MARKER)
    terminator = "\n        );"
    end = source.index(terminator, start) + len(terminator) - 1
    return source[start:end]


def _extract_backstop_definition(source: str) -> str:
    """The pre-#341 widening backstop's `conn.execute(...)` statement.

    Located through the module's own AST rather than by text slicing, so the
    helper keeps finding it after the statement is reformatted.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "execute":
            continue
        try:
            value = ast.literal_eval(node.args[0])
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and value.lstrip().startswith(_BACKSTOP_PREFIX):
            return value
    raise AssertionError(
        "the pre-#341 quota_projection_state widening backstop was not found"
    )


def _stored_sql(tmp_path: pathlib.Path, name: str, statement: str) -> str:
    db = tmp_path / f"{name}.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(statement)
        row = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='quota_projection_state'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "statement did not create quota_projection_state"
    return row[0]


def _projection_columns(conn: sqlite3.Connection) -> set:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(quota_projection_state)")
    }


def test_both_quota_projection_state_definitions_store_identical_sql(tmp_path):
    """#496 S1 F18.

    `_stats_schema_fingerprint` hashes `sqlite_schema.sql` verbatim, so the
    fresh-path definition and the pre-#341 widening backstop must produce
    byte-identical stored text or an upgraded install can never qualify for
    cleanup-only interrupted-rebuild recovery.
    """
    import _cctally_core

    source = pathlib.Path(_cctally_core.__file__).read_text()
    fresh = _extract_fresh_definition(source)
    backstop = _extract_backstop_definition(source)

    assert _stored_sql(tmp_path, "fresh", fresh) == _stored_sql(
        tmp_path, "backstop", backstop
    )


def test_backstop_path_preserves_the_rebuild_schema_fingerprint(ns, tmp_path):
    """#496 S1 F18 reachability.

    An install upgrading from a legacy stats.db whose `quota_projection_state`
    predates #341 reaches the widening backstop. The resulting index must still
    fingerprint as the current epoch contract, or `stats_index_matches_journal_
    prefix` refuses cleanup-only recovery forever.
    """
    import _cctally_core
    import _cctally_journal

    target = tmp_path / "widened.db"
    conn = _cctally_core.open_db(_target_path=str(target))
    try:
        assert (
            _cctally_journal._stats_schema_fingerprint(conn)
            == _cctally_journal._REBUILD_SCHEMA_FINGERPRINT
        )
    finally:
        conn.close()

    raw = sqlite3.connect(str(target))
    try:
        # Regress the table to its pre-#341 shape, which is what makes the
        # widening backstop the branch that recreates it.
        raw.execute("DROP TABLE quota_projection_state")
        raw.execute(
            "CREATE TABLE quota_projection_state ("
            " source_root_key TEXT NOT NULL,"
            " generation TEXT NOT NULL,"
            " physical_signature TEXT NOT NULL,"
            " completed_at_utc TEXT NOT NULL,"
            " PRIMARY KEY(source_root_key))"
        )
        assert "account_key" not in _projection_columns(raw)

        _cctally_core._apply_quota_projection_schema(raw)

        # Assert the branch RAN before asserting the fingerprint, so this test
        # cannot pass vacuously by never reaching the backstop.
        assert "account_key" in _projection_columns(raw), (
            "the pre-#341 widening backstop did not run"
        )
        assert (
            _cctally_journal._stats_schema_fingerprint(raw)
            == _cctally_journal._REBUILD_SCHEMA_FINGERPRINT
        )
    finally:
        raw.close()


# ==========================================================================
# F3 — a required structured trigger context at every call site
# ==========================================================================

def _production_bin_sources() -> list:
    """Every shipped Python source under `bin/`.

    The extensionless entry point `bin/cctally` is added explicitly because a
    `*.py` glob silently misses it.
    """
    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    paths = [p for p in bin_dir.glob("*.py") if "test" not in p.name]
    entry = bin_dir / "cctally"
    if entry.exists():
        paths.append(entry)
    return sorted(paths)


def _rebuild_context_calls(path: pathlib.Path) -> list:
    calls = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else getattr(func, "id", None)
        )
        if name == "RebuildContext":
            calls.append(node)
    return calls


def test_rebuild_stats_index_requires_a_context():
    """#496 S1 F3. No default: a new call site cannot silently go unattributed."""
    import inspect
    import _cctally_journal

    sig = inspect.signature(_cctally_journal.rebuild_stats_index)
    param = sig.parameters["context"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_trigger_set_is_closed_and_complete():
    import _cctally_journal as j

    assert j.REBUILD_TRIGGERS == frozenset({
        "corruption-heal",
        "interrupted-rebuild-recovery",
        "db-rebuild",
        "journal-repair-acknowledge",
        "journal-repair-recovery",
        "rederive-apply",
        "rederive-recovery",
        "correction-recovery-in-band",
        "epoch-transition",
        "test-fixture",
    })


def test_unknown_trigger_is_rejected():
    import _cctally_journal as j

    with pytest.raises(ValueError):
        j.RebuildContext(trigger="not-a-real-trigger").validate()


def test_every_known_trigger_validates():
    import _cctally_journal as j

    for trigger in j.REBUILD_TRIGGERS:
        assert j.RebuildContext(trigger=trigger).validate().trigger == trigger


def test_a_caller_supplied_record_path_is_rejected():
    """`record_path` is resolved by the engine, never by a caller.

    `rebuild_stats_index` overwrites whatever a caller set, so a settable field
    that is silently discarded is a trap. Rejecting it makes the contract the
    docstring already states enforceable.
    """
    import _cctally_journal as j

    with pytest.raises(ValueError, match="record_path"):
        j.RebuildContext(
            trigger="db-rebuild", record_path="/tmp/caller-supplied.json"
        ).validate()


def test_no_production_call_site_uses_the_test_identity():
    """`test-fixture` is an escape hatch for harnesses, not for shipped code.

    Checked over the AST rather than the raw text, because the module that
    DEFINES the closed set legitimately contains the identifier as a literal.
    """
    offenders = []
    seen = 0
    for path in _production_bin_sources():
        for node in _rebuild_context_calls(path):
            seen += 1
            for keyword in node.keywords:
                if (
                    keyword.arg == "trigger"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "test-fixture"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
    # Non-vacuity: the spec enumerates nine production paths that reach the
    # rebuild engine, and each must build its own context.
    assert seen >= 9, f"expected >= 9 production contexts, found {seen}"
    assert offenders == []


# ==========================================================================
# The schemaVersion 2 preservation manifest
# ==========================================================================

_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


def _seed_live_index():
    """One journaled observation folded into a live stats.db."""
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
    return jr


def _page_size_of(path: pathlib.Path) -> int:
    raw = int.from_bytes(path.read_bytes()[16:18], "big")
    return 65536 if raw == 1 else raw


def _force_physical_publication(db_path) -> None:
    """Make the destination one SQLite refuses to open.

    #496 S3 made in-place transactional publication the mechanism, and an
    in-place publish NEVER preserves — preservation is a consequence of
    destroying a file. A test about the preservation manifest therefore has to
    reach the physical fallback, which only a structurally unopenable
    destination does. The magic string and the `user_version` at byte 60 are
    both left intact, because `_read_user_version_header` needs the first and
    the manifest records the second; the file-format version bytes at 18-19 are
    what SQLite rejects as NOTADB.
    """
    db = pathlib.Path(db_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    with db.open("r+b") as handle:
        handle.seek(18)
        handle.write(b"\xff\xff")


def test_preservation_manifest_is_schema_version_2_with_trigger_identity(ns):
    import _cctally_core

    jr = _seed_live_index()
    _cctally_core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    forensics = (
        _cctally_core.LOG_DIR
        / "stats.db-corruption-forensics-20260805T052635Z.json"
    )
    forensics.write_text("{}\n")
    _force_physical_publication(_cctally_core.DB_PATH)

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(
            trigger="corruption-heal",
            trigger_error="database disk image is malformed",
            forensics_path=str(forensics),
        )
    )
    incident = result.quarantine_dir
    assert incident is not None
    manifest = json.loads((incident / "manifest.json").read_text())

    # The six v1 fields keep their names and meanings, so a v1 reader is
    # unaffected by the bump.
    assert manifest["schemaVersion"] == 2
    assert manifest["quarantinedAtUtc"].endswith("Z")
    assert manifest["originalPath"] == str(_cctally_core.DB_PATH)
    assert manifest["movedFiles"][0] == "stats.db"
    assert manifest["complete"] is True
    assert manifest["cutoverProtocol"] == "cold-quarantine-then-replace-v2"

    assert manifest["trigger"] == "corruption-heal"
    assert manifest["triggerError"] == "database disk image is malformed"
    assert manifest["forensicsPath"] == str(forensics)
    assert manifest["binaryEpoch"] == _cctally_core.STATS_INDEX_EPOCH
    assert "binaryVersion" in manifest
    assert manifest["preservedUserVersion"] == _cctally_core.STATS_INDEX_EPOCH

    record_path = manifest["rebuildRecordPath"]
    assert isinstance(record_path, str)
    assert record_path.startswith(str(_cctally_core.LOG_DIR))
    assert record_path.endswith(".json")

    sizes = manifest["familySizes"]
    assert set(sizes) == set(manifest["movedFiles"])
    assert all(isinstance(v, int) and v >= 0 for v in sizes.values())
    assert sizes["stats.db"] == (incident / "stats.db").stat().st_size
    assert sizes["stats.db"] > 0


def test_preserved_user_version_is_read_from_a_file_sqlite_cannot_open(ns):
    """#496 S1. The value is worth recording precisely when SQLite refuses the
    file, so it is read from the raw header rather than by opening it."""
    import _cctally_core

    jr = _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    probe = sqlite3.connect(str(db))
    try:
        expected = int(probe.execute("PRAGMA user_version").fetchone()[0])
    finally:
        probe.close()
    assert expected == _cctally_core.STATS_INDEX_EPOCH

    # Destroy page 1's b-tree body but keep the 100-byte file header, where
    # `user_version` lives at offset 60.
    page_size = _page_size_of(db)
    with db.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00" * (page_size - 100))

    raised = None
    try:
        broken = sqlite3.connect(str(db))
        try:
            broken.execute("PRAGMA schema_version").fetchone()
            broken.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()
        finally:
            broken.close()
    except sqlite3.DatabaseError as exc:
        raised = exc
    assert raised is not None, "expected SQLite to refuse the damaged index"

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="corruption-heal")
    )
    manifest = json.loads((result.quarantine_dir / "manifest.json").read_text())
    assert manifest["preservedUserVersion"] == expected


# ==========================================================================
# F3 — end-to-end identity, driven through the REAL entry point
# ==========================================================================
#
# The AST sweep above proves every shipped `RebuildContext(...)` names a known
# trigger. It cannot prove the context a call site builds actually reaches the
# manifest, because it never runs the code. The tests below drive an entry
# point a user or the runtime reaches and read the identity back off disk.
#
# Five of the nine production triggers are covered that way. Two are here:
# `corruption-heal` and `db-rebuild`. Three are asserted in the test files that
# already own the fixtures needed to reach them, rather than duplicated:
# `rederive-apply` in `tests/test_rederive_command.py`,
# `journal-repair-acknowledge` in `tests/test_journal_repair_402.py`, and
# `interrupted-rebuild-recovery` in `tests/test_stats_rebuild_recovery_388.py`.
#
# The other four — `journal-repair-recovery`, `rederive-recovery`,
# `correction-recovery-in-band` and `epoch-transition` — are covered by the AST
# sweep's inspection of their wiring alone. Nothing drives those recovery paths
# to a manifest and reads the trigger back, so a call site passing the wrong
# but valid trigger would not be caught.


def _destroy_header_magic(db: pathlib.Path) -> None:
    """Make every open of the family fail with a CLASSIFIED corruption error."""
    with db.open("r+b") as handle:
        handle.write(b"not a database\x00")


def _cutover_manifests(app_dir: pathlib.Path) -> list:
    """Every current cold-quarantine manifest under `quarantine/`.

    Selected by protocol rather than by name so a synthesized legacy incident
    in the same directory is never mistaken for one this cutover wrote.
    """
    root = pathlib.Path(app_dir) / "quarantine"
    if not root.is_dir():
        return []
    out = []
    for incident in sorted(root.iterdir()):
        path = incident / "manifest.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if payload.get("cutoverProtocol") == "cold-quarantine-then-replace-v2":
            out.append(payload)
    return out


def _bundles(log_dir: pathlib.Path) -> list:
    if not pathlib.Path(log_dir).is_dir():
        return []
    return sorted(
        p for p in pathlib.Path(log_dir).iterdir()
        if p.is_file() and "corruption-forensics" in p.name and p.suffix == ".json"
    )


def test_corruption_heal_manifest_names_its_error_and_its_forensics_bundle(ns):
    """#496 S1 F3, driven through the real `open_db` auto-heal.

    This is the pairing nothing else pins: `triggerError` and `forensicsPath`
    must be populated FROM the forensics result the heal just produced, rather
    than left null. #496 S3 split the two halves across two processes — the
    hook writes the bundle, the detached worker writes the incident — so the
    pairing now has to survive travelling through the durable request.
    """
    import types
    import _cctally_core
    import _cctally_db
    import _cctally_store
    import _cctally_update

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    prior_spawn = _cctally_update._spawn_detached
    _cctally_update._spawn_detached = lambda _command: True
    try:
        with pytest.raises(_cctally_db.StatsHealDeferred):
            _cctally_core.open_db()
    finally:
        _cctally_update._spawn_detached = prior_spawn
    assert _cctally_store.cmd_stats_corruption_heal_internal(
        types.SimpleNamespace()
    ) == 0

    conn = _cctally_core.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [7.0], "the heal must republish a usable index"

    manifests = _cutover_manifests(_cctally_core.APP_DIR)
    assert len(manifests) == 1, f"expected one incident, got {manifests!r}"
    manifest = manifests[0]
    assert manifest["trigger"] == "corruption-heal"

    bundles = _bundles(_cctally_core.LOG_DIR)
    assert len(bundles) == 1, f"expected one forensics bundle, got {bundles!r}"
    assert manifest["forensicsPath"] == str(bundles[0])
    assert pathlib.Path(manifest["forensicsPath"]).exists()

    bundle = json.loads(bundles[0].read_text())
    # Written by THIS heal, not merely present: the bundle records the same
    # origin the manifest claims, and the classified error text is the same
    # string in both.
    assert bundle["trigger"]["origin"] == "corruption-heal"
    assert manifest["triggerError"]
    assert manifest["triggerError"] == bundle["trigger"]["message"]


def _isolated_env(tmp_path: pathlib.Path) -> dict:
    data = tmp_path / "data"
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    for path in (data, home, claude / "projects", codex):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "TZ": "Etc/UTC",
        }
    )
    return env


def _seed_cli(env: dict) -> pathlib.Path:
    now = int(time.time())
    result = subprocess.run(
        [
            sys.executable, str(CCTALLY), "record-usage",
            "--percent", "7",
            "--resets-at", str(now + 3 * 86400),
            "--five-hour-percent", "11",
            "--five-hour-resets-at", str(now + 3600),
        ],
        env=env, capture_output=True, text=True, timeout=110,
    )
    assert result.returncode == 0, result.stderr
    db = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "stats.db"
    assert db.exists()
    return db


def test_db_rebuild_cli_manifest_records_the_db_rebuild_trigger(tmp_path):
    """#496 S1 F3, driven through the real `cctally db rebuild --db stats`.

    The destination is made unopenable first, because #496 S3 publishes a
    readable one in place and an in-place publish never preserves.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _force_physical_publication(db)

    result = subprocess.run(
        [sys.executable, str(CCTALLY), "db", "rebuild", "--db", "stats"],
        env=env, capture_output=True, text=True, timeout=110,
    )
    assert result.returncode == 0, result.stderr

    data = pathlib.Path(env["CCTALLY_DATA_DIR"])
    manifests = _cutover_manifests(data)
    assert [m["trigger"] for m in manifests] == ["db-rebuild"]
    # An operator rebuild classifies no corruption, so there is no error text,
    # but it still writes and names a bundle.
    assert manifests[0]["triggerError"] is None
    assert pathlib.Path(manifests[0]["forensicsPath"]).exists()
