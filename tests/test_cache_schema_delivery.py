"""Cache schema DDL must have an explicit delivery path (#566, #580)."""
import pytest

from conftest import load_script
from schema_delivery_helpers import (
    _literal_object_keys,
    assert_frozen_baseline,
    assert_migration_delivery,
    assert_registry_matches_schema,
    declared_schema_object_keys,
)


EXPECTED_BASELINE = frozenset(
    tuple(item.split(":", 1))
    for item in """
index:idx_codex_conv_msgs_conversation
index:idx_codex_conv_msgs_source
index:idx_codex_conv_rollups_recent
index:idx_codex_conv_touches_source
index:idx_codex_entries_conversation
index:idx_codex_entries_session
index:idx_codex_entries_source
index:idx_codex_entries_source_root
index:idx_codex_entries_timestamp
index:idx_codex_entries_ts_root_conversation
index:idx_codex_events_conversation
index:idx_codex_events_timestamp
index:idx_codex_files_conversation
index:idx_codex_files_source_root
index:idx_codex_threads_source_path
index:idx_codex_threads_source_root
index:idx_conv_session_ts
index:idx_conv_session_uuid
index:idx_conv_sessions_recent
index:idx_conv_source
index:idx_conv_turnkey
index:idx_entries_dedup
index:idx_entries_mutation_seq
index:idx_entries_source
index:idx_entries_timestamp
index:idx_file_touches_path
index:idx_quota_window_captured_at
index:idx_quota_window_source_root
index:idx_session_files_session_id
table:cache_meta
table:codex_conversation_events
table:codex_conversation_file_touches
table:codex_conversation_fts
table:codex_conversation_messages
table:codex_conversation_rollups
table:codex_conversation_threads
table:codex_session_entries
table:codex_session_files
table:codex_source_roots
table:conversation_ai_titles
table:conversation_file_touches
table:conversation_fts
table:conversation_messages
table:conversation_sessions
table:conversation_title_fts
table:quota_window_snapshots
table:session_entries
table:session_files
trigger:codex_conv_fts_ad
trigger:codex_conv_fts_ai
trigger:codex_conv_fts_au
trigger:conv_fts_ad
trigger:conv_fts_ai
trigger:conv_fts_au
trigger:conv_title_fts_ad
trigger:conv_title_fts_ai
trigger:conv_title_fts_au
""".split()
)


@pytest.fixture
def db():
    load_script()
    import _cctally_db

    return _cctally_db


def test_every_declared_cache_object_is_registered(db):
    assert_registry_matches_schema(
        db, db._apply_cache_schema, db.CACHE_REDERIVABLE_OBJECTS,
        store="cache.db",
    )


def test_cache_registry_is_stable_when_fts5_is_unavailable(db, monkeypatch):
    monkeypatch.setattr(db, "_fts5_available", lambda _conn: False)
    assert_registry_matches_schema(
        db, db._apply_cache_schema, db.CACHE_REDERIVABLE_OBJECTS,
        store="cache.db",
    )


def test_cache_scan_is_non_vacuous_for_every_supported_kind(db):
    declared = declared_schema_object_keys(db, db._apply_cache_schema)
    assert ("table", "session_entries") in declared
    assert ("index", "idx_codex_entries_root_path") in declared
    assert ("trigger", "trg_codex_accounting_ins") in declared
    assert {kind for kind, _name in declared} == {"index", "table", "trigger"}


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        ("CREATE TABLE IF NOT EXISTS probe_table (id INTEGER)",
         ("table", "probe_table")),
        ("CREATE VIEW IF NOT EXISTS probe_view AS SELECT 1",
         ("view", "probe_view")),
        ("CREATE TRIGGER IF NOT EXISTS probe_trigger AFTER INSERT ON t BEGIN SELECT 1; END",
         ("trigger", "probe_trigger")),
        ("CREATE INDEX IF NOT EXISTS probe_index ON t(id)",
         ("index", "probe_index")),
    ],
)
def test_scanner_recognizes_every_supported_object_kind(ddl, expected):
    assert expected in _literal_object_keys(ddl)


def test_cache_post_baseline_objects_have_real_migration_delivery(db):
    assert_migration_delivery(
        db, db.CACHE_REDERIVABLE_OBJECTS, db._CACHE_MIGRATIONS,
        store="cache.db",
    )


def test_cache_frozen_baseline_has_not_grown(db):
    assert_frozen_baseline(
        db.CACHE_REDERIVABLE_OBJECTS, EXPECTED_BASELINE,
        store="cache.db",
    )


def test_same_kind_substitution_cannot_bypass_the_frozen_baseline(db):
    records = list(db.CACHE_REDERIVABLE_OBJECTS)
    index = next(
        i for i, record in enumerate(records)
        if (record.kind, record.name) == ("table", "session_entries")
    )
    records[index] = records[index]._replace(name="issue_580_substitution")
    with pytest.raises(AssertionError, match="unexpected baseline objects"):
        assert_frozen_baseline(records, EXPECTED_BASELINE, store="cache.db")


def test_the_566_index_keeps_its_delivery_path(db):
    record = next(
        record for record in db.CACHE_REDERIVABLE_OBJECTS
        if record.name == "idx_codex_entries_root_path"
    )
    assert record.kind == "index"
    assert record.introduced_by == "042_codex_entries_root_path_index"
    assert record.ensure_helper == "_apply_codex_entries_root_path_index"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
