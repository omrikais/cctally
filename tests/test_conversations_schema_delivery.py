"""Conversations schema DDL must have an explicit delivery path (#580)."""
import pytest

from conftest import load_script
from schema_delivery_helpers import (
    assert_frozen_baseline,
    assert_migration_delivery,
    assert_registry_matches_schema,
    declared_schema_object_keys,
)


EXPECTED_BASELINE = frozenset(
    tuple(item.split(":", 1))
    for item in """
index:idx_codex_conv_messages_account_conversation
index:idx_codex_conv_msgs_conversation
index:idx_codex_conv_msgs_source
index:idx_codex_conv_rollups_recent
index:idx_codex_conv_touches_source
index:idx_codex_events_account_conversation
index:idx_codex_events_conversation
index:idx_codex_events_timestamp
index:idx_conv_messages_account_session
index:idx_conv_session_ts
index:idx_conv_session_uuid
index:idx_conv_sessions_recent
index:idx_conv_source
index:idx_conv_turnkey
index:idx_conversation_messages_cwd
index:idx_conversation_messages_model_session
index:idx_file_touches_path
table:cache_meta
table:codex_conversation_events
table:codex_conversation_file_touches
table:codex_conversation_fts
table:codex_conversation_messages
table:codex_conversation_rollups
table:codex_conversation_source_files
table:conversation_ai_titles
table:conversation_file_touches
table:conversation_fts
table:conversation_messages
table:conversation_sessions
table:conversation_source_files
table:conversation_title_fts
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


def test_every_declared_conversations_object_is_registered(db):
    assert_registry_matches_schema(
        db, db._apply_conversations_schema,
        db.CONVERSATIONS_REDERIVABLE_OBJECTS,
        store="conversations.db",
    )


def test_conversations_registry_is_stable_when_fts5_is_unavailable(
    db, monkeypatch,
):
    monkeypatch.setattr(db, "_fts5_available", lambda _conn: False)
    assert_registry_matches_schema(
        db, db._apply_conversations_schema,
        db.CONVERSATIONS_REDERIVABLE_OBJECTS,
        store="conversations.db",
    )


def test_conversations_scan_is_non_vacuous_for_every_supported_kind(db):
    declared = declared_schema_object_keys(db, db._apply_conversations_schema)
    assert ("table", "conversation_source_files") in declared
    assert ("index", "idx_conv_messages_account_session") in declared
    assert ("trigger", "codex_find_projection_message_ad") in declared
    assert {kind for kind, _name in declared} == {"index", "table", "trigger"}


def test_conversations_post_baseline_objects_have_real_migration_delivery(db):
    assert_migration_delivery(
        db, db.CONVERSATIONS_REDERIVABLE_OBJECTS,
        db._CONVERSATIONS_MIGRATIONS, store="conversations.db",
    )


def test_conversations_frozen_baseline_has_not_grown(db):
    assert_frozen_baseline(
        db.CONVERSATIONS_REDERIVABLE_OBJECTS, EXPECTED_BASELINE,
        store="conversations.db",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
