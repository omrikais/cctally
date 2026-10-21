"""#968 T3: no decoder (and a corrupt stream) fails closed everywhere."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, snapshot_tables, token_records, write_rollout, zstd_bytes)
from test_codex_hook_lifecycle import _hook_args  # noqa: E402

import _lib_codex_rollout_container as rc  # noqa: E402

WARNING = ("Some compressed Codex rollouts could not be read. Cached history "
           "was kept. Use Python 3.14+ or install `backports.zstd` in the "
           "Python environment running cctally (`pip install backports.zstd`).")
REFUSAL = ("Codex rebuild refused: compressed rollouts could not be decoded. "
           "Existing Codex data was kept.")
DIRECT = ("Codex totals are unavailable because compressed history could not "
          "be decoded.")


def _name(i):
    return f"rollout-2026-07-20T1{i}-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff02{i:02d}.jsonl"


@pytest.fixture(autouse=True)
def _no_detached_drains(monkeypatch):
    """Patch each fresh sibling, including loads through imported aliases."""
    import conftest
    import subprocess
    import sys

    real_load = conftest.load_script_module
    real_popen = subprocess.Popen
    escaped = []

    def patch_current():
        update = sys.modules.get("_cctally_update")
        if update is not None:
            monkeypatch.setattr(update, "_spawn_detached", lambda command: True)

    def load_without_detaching(*args, **kwargs):
        module = real_load(*args, **kwargs)
        patch_current()
        return module

    def guard_detached(*popenargs, **kwargs):
        argv = popenargs[0] if popenargs else kwargs.get("args")
        if (kwargs.get("start_new_session") and isinstance(argv, (list, tuple))
                and argv and str(argv[-1]).startswith("_codex-")):
            escaped.append(tuple(argv))
            raise AssertionError("a Codex detached worker escaped test isolation")
        return real_popen(*popenargs, **kwargs)

    # Every conftest.load_script alias resolves this primitive at call time;
    # wrapping only this module's load_script name would miss helper-owned loads.
    monkeypatch.setattr(conftest, "load_script_module", load_without_detaching)
    monkeypatch.setattr(subprocess, "Popen", guard_detached)
    patch_current()
    yield
    assert not escaped, f"detached Codex worker attempts: {escaped!r}"


@pytest.fixture(params=["no-decoder", "corrupt-stream"])
def estate(request, tmp_path, monkeypatch):
    """Recognized mixed root: a plain keeper, a retained compressed rollout,
    and a never-ingested compressed rollout."""
    ns = load_script()
    redirect_paths(
        ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setitem(ns, "_hook_tick_read_stdin_event", lambda: {
        "event": "Stop", "session_id": "t", "transcript_path": "", "cwd": ""})
    keeper, retained, fresh = day / _name(9), day / _name(1), day / _name(2)
    write_rollout(keeper, token_records(1, session_id="keep"))
    records = conversation_records(3, session_id="ret")
    records.append({"timestamp": "2026-07-20T10:50:00.000Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"rate_limits": {
                        "limit_id": "codex", "limit_name": "codex",
                        "primary": {"used_percent": 42, "window_minutes": 10080,
                                    "resets_at": 1785150000}}}}})
    write_rollout(retained, records)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()
    write_rollout(fresh, token_records(2, session_id="new"))
    for path in (retained, fresh):
        data = path.read_bytes()
        blob = zstd_bytes(data)
        if request.param == "corrupt-stream":
            blob = blob[: len(blob) // 2]
        path.with_name(path.name + ".zst").write_bytes(blob)
        path.unlink()
    if request.param == "no-decoder":
        monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    # Every family below must be nonempty before preservation is meaningful.
    state = _families(ns)
    assert all(state[0].values()), state[0]
    assert all(state[1].values()), state[1]
    ns["_task06_retained_families"] = state
    return ns, retained, fresh, request.param


CACHE_FAMILIES = (
    "codex_session_entries", "quota_window_snapshots", "codex_file_accounts",
    "codex_file_incarnations", "codex_conversation_threads", "codex_session_files",
)
CONVERSATION_FAMILIES = (
    "codex_conversation_events", "codex_conversation_messages",
    "codex_conversation_rollups", "codex_conversation_source_files",
)


def _families(ns):
    out = []
    for opener, tables in (("open_cache_db", CACHE_FAMILIES),
                           ("open_conversations_db", CONVERSATION_FAMILIES)):
        conn = ns[opener]()
        try:
            out.append(snapshot_tables(conn, tables))
        finally:
            conn.close()
    return tuple(out)


def _entries(ns, path=None):
    conn = ns["open_cache_db"]()
    try:
        if path is None:
            return conn.execute(
                "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?",
            (str(path),)).fetchone()[0]
    finally:
        conn.close()


def _events(ns):
    conv = ns["open_conversations_db"]()
    try:
        return conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0]
    finally:
        conv.close()


def _meta(ns, key, store="cache"):
    opener = ns["open_cache_db"] if store == "cache" else ns["open_conversations_db"]
    conn = opener()
    try:
        row = conn.execute("SELECT value FROM cache_meta WHERE key=?",
                           (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def test_retained_rows_survive_and_never_ingested_files_are_counted(estate, capsys):
    ns, retained, fresh, mode = estate
    before = _entries(ns, retained)
    assert before == 3
    assert ns["main"](["codex", "daily", "--since", "20260701"]) in (0, 1)
    # 92e06df85: no compressed discovery, so the eligible prune deletes `retained`.
    assert _entries(ns, retained) == before
    assert _entries(ns, fresh) == 0
    err = capsys.readouterr().err
    if mode == "no-decoder":
        assert err.count(WARNING) == 1
    status = json.loads(_meta(ns, "codex_container_status") or "{}")
    reason = "decoder-unavailable" if mode == "no-decoder" else "decode-error"
    assert status.get("skipped", {}).get(reason, 0) >= 1


def test_the_hook_stays_silent_and_records_its_condition(estate, capsys):
    ns, retained, _fresh, _mode = estate
    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert capsys.readouterr().err == ""
    assert _entries(ns, retained) == 3
    assert _meta(ns, "codex_container_status") is not None


def test_a_compressed_rollout_path_never_reaches_the_hook_log(estate):
    """Regression guard: a container error quotes the physical `.jsonl.zst`
    path, and the hook log's existing path and UUID rules must keep both the
    username and the conversation id out of the durable log."""
    import _cctally_record
    name = ("rollout-2026-07-31T09-00-00-"
            "0199aa11-bb22-cc33-dd44-ee55ff667788.jsonl.zst")
    detail = _cctally_record._hook_log_error_detail(PermissionError(
        13, "Permission denied",
        "/Users/someone/.codex/sessions/2026/07/31/" + name))
    relative = _cctally_record._hook_log_safe_free_text(
        "decode-error in sessions/2026/07/31/" + name)
    for text in (detail, relative):
        assert "someone" not in text
        assert "/Users/" not in text
        assert "0199aa11" not in text
    assert "PermissionError" in detail


def test_doctor_warns_without_a_decoder(estate):
    ns, _retained, _fresh, mode = estate
    if mode != "no-decoder":
        return
    import _cctally_doctor
    import _lib_doctor
    report = _lib_doctor.run_checks(_cctally_doctor.doctor_gather_state())
    checks = {c.id: c for cat in report.categories for c in cat.checks}
    check = checks["data.codex_compressed_rollouts"]
    assert check.severity == "warn"
    assert check.summary == (
        "2 compressed Codex rollouts can't be read on this Python (1 never "
        "ingested). Cached history was kept. Install `backports.zstd` or use "
        "Python 3.14+.")


def test_a_contended_read_exits_3_instead_of_plain_only_totals(estate, capsys):
    ns, *_ = estate
    lock = open(sys.modules["_cctally_core"].CACHE_LOCK_PATH, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        code = ns["main"](["codex", "daily", "--since", "20260701"])
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert code == 3
    assert DIRECT in capsys.readouterr().err


def _assert_everything_kept(ns, retained, before_events, marker_key=None,
                            store="cache"):
    assert _entries(ns, retained) == 3
    assert before_events > 0
    assert _events(ns) == before_events
    assert _families(ns) == ns["_task06_retained_families"]
    if marker_key is not None:
        assert _meta(ns, marker_key, store) is not None


def test_explicit_rebuild_refuses_with_exit_3(estate, capsys):
    ns, retained, _fresh, _mode = estate
    events = _events(ns)
    conn = ns["open_cache_db"]()
    try:
        key = sys.modules["_cctally_cache"].CODEX_REPLAY_FROM_ZERO_KEY
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)", (key, "1"))
        conn.commit()
    finally:
        conn.close()
    assert ns["main"](["cache-sync", "--source", "codex", "--rebuild"]) == 3
    assert REFUSAL in capsys.readouterr().err
    _assert_everything_kept(ns, retained, events, key)


def test_an_implicit_sync_finding_the_replay_marker_refuses(estate):
    ns, retained, _fresh, _mode = estate
    key = sys.modules["_cctally_cache"].CODEX_REPLAY_FROM_ZERO_KEY
    before_events = _events(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (key, "1"))
        conn.commit()
        stats = ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    assert stats.rebuild_refused in ("decoder-unavailable", "decode-error")
    _assert_everything_kept(ns, retained, before_events, key)


def test_the_replay_drain_worker_refuses(estate):
    ns, retained, _fresh, _mode = estate
    cache_mod = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (cache_mod.CODEX_REPLAY_FROM_ZERO_KEY, "1"))
        conn.commit()
    finally:
        conn.close()
    events = _events(ns)
    assert cache_mod.cmd_codex_replay_drain_internal(None) == 0
    _assert_everything_kept(ns, retained, events,
                            cache_mod.CODEX_REPLAY_FROM_ZERO_KEY)


@pytest.mark.parametrize("route", [
    "conversation_rebuild_codex_pending", "contract", "codex_replay_pending"])
def test_every_conversations_rebuild_route_refuses(estate, route):
    ns, retained, _fresh, _mode = estate
    cache_mod = sys.modules["_cctally_cache"]
    conv = ns["open_conversations_db"]()
    try:
        if route == "contract":
            conv.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES("
                         "'codex_conversation_contract_version','0-stale')")
            marker = None
        elif route == "codex_replay_pending":
            marker = cache_mod.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY
            conv.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                         (marker, "1"))
        else:
            marker = route
            conv.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                         (marker, "1"))
        conv.commit()
        events = conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0]
        stats = ns["sync_codex_conversations"](conv)
    finally:
        conv.close()
    assert getattr(stats, "rebuild_refused", None) in (
        "decoder-unavailable", "decode-error")
    _assert_everything_kept(ns, retained, events, marker, store="conversations")


# -- Doctor states: operator clarifications D2, D3, D6 (review P2-3) ---------

NO_DECODER = ("3 compressed Codex rollouts can't be read on this Python (1 "
              "never ingested). Cached history was kept. Install "
              "`backports.zstd` or use Python 3.14+.")
UNVERIFIED = ("2 compressed Codex rollouts kept their cached history but "
              "couldn't be verified against it.")
CONTINUING = "Codex history maintenance is continuing in the background."
ALL_OK = "All compressed Codex rollouts are readable and verified."
DECODER = {"codex_decoder_capability": "backports.zstd"}


def _debt_sentence(v, r, p):
    return ("Codex history maintenance has been pending for more than 24 "
            f"hours ({v} verifications, {r} reads, {p} deletions).")


def _doctor_state(**extra):
    import _lib_doctor
    fields = {f.name: (f.default if f.default is not dataclasses.MISSING
                       else None)
              for f in dataclasses.fields(_lib_doctor.DoctorState)}
    state = _lib_doctor.DoctorState(**fields)
    for name, value in extra.items():
        # Set after construction, so a tree without these fields still
        # reaches the behavioral assertion rather than a TypeError.
        setattr(state, name, value)
    return state


def _compressed_check(**extra):
    import _lib_doctor
    report = _lib_doctor.run_checks(_doctor_state(**extra))
    found = [c for cat in report.categories for c in cat.checks
             if c.id == "data.codex_compressed_rollouts"]
    assert found, "the doctor has no data.codex_compressed_rollouts check"
    return found[0]


def _census(compressed=0, complete=0, incomplete=0, never=0, unverified=0):
    return {"compressedOnly": compressed, "retainedComplete": complete,
            "retainedIncomplete": incomplete, "neverIngested": never,
            "unverified": unverified}


def _debt(v=0, p=0, old=False):
    return {"verifications": v, "deletions": p, "olderThan24h": old}


def test_doctor_is_ok_only_when_nothing_is_unread_pending_or_unverified():
    check = _compressed_check(codex_compressed_census=_census(2, complete=2),
                              codex_maintenance_debt=_debt(), **DECODER)
    assert (check.severity, check.summary) == ("ok", ALL_OK)


@pytest.mark.parametrize(("census", "debt"), [
    (_census(2, complete=2), _debt(v=1)),
    (_census(2, complete=1, never=1), _debt()),
    (_census(2, complete=1, incomplete=1), _debt()),
    (_census(), _debt(p=4)),
], ids=["verification", "never-ingested-read", "incomplete-read", "deletion"])
def test_doctor_is_ok_while_recent_maintenance_continues(census, debt):
    """D2 and D6: pending verification, reading or deletion work younger than
    24 hours is `ok` "continuing", never the all-clear sentence."""
    check = _compressed_check(codex_compressed_census=census,
                              codex_maintenance_debt=debt, **DECODER)
    assert (check.severity, check.summary) == ("ok", CONTINUING)


def test_doctor_warns_when_maintenance_is_older_than_24_hours():
    check = _compressed_check(
        codex_compressed_census=_census(4, complete=1, never=1, incomplete=1,
                                        unverified=0),
        codex_maintenance_debt=_debt(v=1, p=5, old=True), **DECODER)
    assert (check.severity, check.summary) == ("warn", _debt_sentence(1, 2, 5))
    assert check.details["readDebt"] == 2


def test_an_unverified_container_warns_at_once_and_is_never_debt():
    """D3: terminal `unverified` is a warning now, not maintenance debt."""
    check = _compressed_check(codex_compressed_census=_census(2, unverified=2),
                              codex_maintenance_debt=_debt(), **DECODER)
    assert (check.severity, check.summary) == ("warn", UNVERIFIED)
    assert check.details["unverified"] == 2
    assert check.details["verificationDebt"] == 0


def test_doctor_without_a_decoder_names_the_remedy_and_counts_no_reads():
    check = _compressed_check(
        codex_compressed_census=_census(3, complete=2, never=1),
        codex_maintenance_debt=_debt(),
        codex_decoder_capability="unavailable")
    assert (check.severity, check.summary) == ("warn", NO_DECODER)
    assert check.details["readDebt"] == 0
    assert check.details["decoderAvailable"] is False


@pytest.mark.parametrize(("extra", "expected"), [
    ({"codex_decoder_capability": "unavailable",
      "codex_compressed_census": _census(3, never=1, unverified=2),
      "codex_maintenance_debt": _debt(v=1, p=2, old=True)},
     " ".join([NO_DECODER, UNVERIFIED, _debt_sentence(1, 0, 2)])),
    ({**DECODER,
      "codex_compressed_census": _census(3, never=1, unverified=2),
      "codex_maintenance_debt": _debt(old=True)},
     " ".join([UNVERIFIED, _debt_sentence(0, 1, 0)])),
], ids=["no-decoder-unverified-debt", "unverified-debt"])
def test_combined_warnings_state_each_sentence_once_in_order(extra, expected):
    """D3: one `warn`, each true sentence once, joined by a single space, in
    the order no decoder, unverified, debt."""
    check = _compressed_check(**extra)
    assert (check.severity, check.summary) == ("warn", expected)


def test_partial_status_cannot_clear_estate_failures_or_read_age(estate):
    ns, retained, _fresh, _mode = estate
    cache = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        old = {"version": 1, "census": "complete", "skipped": {
            "decoder-unavailable": 7, "decode-error": 3},
            "readsPending": 4, "readsPendingSince": "2026-01-01T00:00:00Z",
            "rebuildRefused": "decode-error", "barrier": {"pending": 2}}
        cache._set_cache_meta(conn, "codex_container_status", json.dumps(old))
        conn.commit()
        cache._record_codex_container_status(
            conn, cache.CodexIngestStats(), census_complete=False)
        status = json.loads(conn.execute("SELECT value FROM cache_meta WHERE key=?",
                                        ("codex_container_status",)).fetchone()[0])
        assert status["census"] == "partial"
        assert status["skipped"]["decoder-unavailable"] == 7
        assert status["skipped"]["decode-error"] == 3
        assert status["readsPending"] == 4
        assert status["readsPendingSince"] == "2026-01-01T00:00:00Z"
        assert status["rebuildRefused"] == "decode-error"
        # Task 10 now derives this bounded object from all cache-owned
        # barrier rows even for a partial walk; the old placeholder has no
        # standing when the complete persisted targets are available.
        assert status["barrier"] == {
            "population": 0, "repairsPending": 0, "certificationPending": 0,
            "repairPending": 0, "suspended": 0, "membershipUnproven": 0,
            "unresolved": {"verification-pending": 0, "decoder-unavailable": 0,
                           "decode-error": 0, "verification-unverified": 0,
                           "container-absent": 0, "membership-unproven": 0,
                           "accounting-incomplete": 0},
            "membershipKnown": 0, "membershipUnknown": 0,
            "withRetainedFallback": 0, "withoutRetainedFallback": 0,
            "keysMembershipUnknown": 0, "walkCensus": True,
        }
        assert str(retained) not in json.dumps(status)
    finally:
        conn.close()


def test_direct_fallback_decodes_old_mtime_and_deduplicates(tmp_path, monkeypatch):
    import datetime as dt
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    path = day / _name(1)
    records = token_records(2, session_id="ret")
    records.insert(-1, records[-2])  # Same cumulative token event is not another turn.
    write_rollout(path, records)
    blob = zstd_bytes(path.read_bytes())
    compressed = path.with_name(path.name + ".zst")
    compressed.write_bytes(blob)
    path.unlink()
    os.utime(compressed, (1, 1))
    entries = ns["_collect_codex_entries_direct"](
        dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc))
    assert len(entries) == 2
    assert [entry.total_tokens for entry in entries] == [100, 100]
    assert {entry.source_path for entry in entries} == {str(path)}


def test_doctor_census_is_live_read_only_and_records_pending_age(estate):
    import datetime as dt
    ns, retained, fresh, _mode = estate
    import _cctally_doctor
    cache = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_files SET container_state='pending' WHERE path=?",
                     (str(retained),))
        cache._set_cache_meta(conn, "codex_container_status", json.dumps({
            "version": 1, "census": "partial", "verificationPendingSince": "2026-07-20T00:00:00Z",
            "readsPendingSince": "2026-07-20T00:00:00Z"}))
        conn.commit()
    finally:
        conn.close()
    seeded = _families(ns)
    state = _cctally_doctor.doctor_gather_state(
        now_utc=dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc))
    assert state.codex_jsonl_present is True
    assert state.codex_compressed_census == _census(2, never=1)
    assert state.codex_maintenance_debt["verifications"] == 1
    assert state.codex_maintenance_debt["olderThan24h"] is True
    assert state.codex_container_status["census"] == "partial"
    assert _families(ns) == seeded
    # A status record is an observation, never a replacement for live presence.
    fresh.with_name(fresh.name + ".zst").unlink()
    again = _cctally_doctor.doctor_gather_state()
    assert again.codex_compressed_census["compressedOnly"] == 1


def test_status_counts_pending_verification_separately_from_unverified(estate):
    ns, retained, _fresh, _mode = estate
    cache = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_files SET container_state='unverified' WHERE path=?",
                     (str(retained),))
        cache._record_codex_container_status(conn, cache.CodexIngestStats(),
                                             census_complete=True)
        status = json.loads(conn.execute("SELECT value FROM cache_meta WHERE key=?",
                                        ("codex_container_status",)).fetchone()[0])
        assert status["verificationDebt"] == 0
        assert status["verificationPendingSince"] is None
        assert status["skipped"]["verification-unverified"] == 1
    finally:
        conn.close()


def test_doctor_does_not_turn_an_unknown_census_into_all_clear():
    check = _compressed_check(codex_compressed_census=None,
                              codex_decoder_capability=None,
                              codex_maintenance_debt=None)
    assert check.severity == "warn"
    assert "unavailable" in check.summary
    assert "compressedOnly" not in check.details


def test_doctor_reports_a_partial_observation_as_partial():
    check = _compressed_check(codex_compressed_census=_census(2, complete=1, never=1),
                              codex_maintenance_debt=_debt(),
                              codex_container_status={"version": 1, "census": "partial",
                                                      "observedAt": "2026-07-20T00:00:00Z"},
                              **DECODER)
    assert check.summary == CONTINUING
    assert check.details["census"] == "partial"
    assert check.details["observedAt"] == "2026-07-20T00:00:00Z"


def test_explicit_warning_and_maintenance_note_are_once_per_process(estate, capsys):
    ns, _retained, _fresh, mode = estate
    ns["main"](["codex", "daily", "--since", "20260701"])
    ns["main"](["codex", "daily", "--since", "20260701"])
    err = capsys.readouterr().err
    if mode == "no-decoder":
        assert err.count(WARNING) == 1
    assert err.count(CONTINUING) <= 1


def test_never_ingested_unread_history_cannot_certify_a_clean_walk(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    keeper, fresh = day / _name(9), day / _name(2)
    write_rollout(keeper, token_records(1, session_id="keeper"))
    write_rollout(fresh, token_records(1, session_id="new"))
    fresh.with_name(fresh.name + ".zst").write_bytes(zstd_bytes(fresh.read_bytes()))
    fresh.unlink()
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn)
        assert stats.full_walk_complete is False
        stats = ns["sync_codex_cache"](conn, only_paths=[str(fresh)])
        assert stats.targeted_clean is False
    finally:
        conn.close()



def test_refused_replay_preserves_retained_coverage_marker(estate):
    ns, _retained, _fresh, _mode = estate
    cache = sys.modules["_cctally_cache"]
    frontier = sys.modules["_lib_ingest_frontier"]
    conn = ns["open_cache_db"]()
    try:
        cache._set_cache_meta(conn, frontier.CODEX_FULL_WALK_COMPLETE_KEY, "1")
        cache._set_cache_meta(conn, cache.CODEX_REPLAY_FROM_ZERO_KEY, "1")
        conn.commit()
        stats = ns["sync_codex_cache"](conn)
        assert stats.rebuild_refused
        assert conn.execute("SELECT value FROM cache_meta WHERE key=?",
                            (frontier.CODEX_FULL_WALK_COMPLETE_KEY,)).fetchone() == ("1",)
    finally:
        conn.close()


def test_a_root_refusal_is_a_partial_status_observation(estate, monkeypatch):
    ns, _retained, _fresh, _mode = estate
    cache = sys.modules["_cctally_cache"]
    conn = ns["open_cache_db"]()
    try:
        cache._set_cache_meta(conn, "codex_container_status", json.dumps({
            "version": 1, "skipped": {"decode-error": 9}, "decoder": "backports.zstd"}))
        conn.commit()
        monkeypatch.setattr(cache, "_discover_codex_files_with_roots", lambda: [])
        stats = ns["sync_codex_cache"](conn)
        assert stats.prune_refused
        record = json.loads(conn.execute("SELECT value FROM cache_meta WHERE key=?",
                                         ("codex_container_status",)).fetchone()[0])
        assert record["census"] == "partial"
        assert record["skipped"]["decode-error"] == 9
    finally:
        conn.close()


@pytest.mark.parametrize("route", ["conversation_rebuild_codex_pending", "contract"])
def test_task06_unchanged_runtime_retention_diagnostic(estate, route):
    """Authorized sr4 diagnostic, not a corrected-candidate verification gate.

    Capture every table and every column, including every cache_meta marker,
    before/after the refused sync and after the original production openers.
    """
    ns, retained, _fresh, mode = estate
    cache = sys.modules["_cctally_cache"]
    core = sys.modules["_cctally_core"]

    def value(v):
        return {"bytesHex": v.hex()} if isinstance(v, bytes) else v

    def snapshot(conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        result = {}
        for table in sorted(tables):
            quoted = '"' + table.replace('"', '""') + '"'
            cur = conn.execute("SELECT * FROM " + quoted)
            columns = [item[0] for item in cur.description]
            rows = [dict(zip(columns, [value(v) for v in row])) for row in cur]
            result[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return result

    def differences(a, b):
        result = {}
        for store in sorted(set(a) | set(b)):
            for table in sorted(set(a[store]) | set(b[store])):
                left, right = a[store].get(table, []), b[store].get(table, [])
                if left == right:
                    continue
                removed = [row for row in left if row not in right]
                added = [row for row in right if row not in left]
                result[store + "." + table] = {"removedNamedRows": removed,
                                                  "addedNamedRows": added}
        return result

    conv = ns["open_conversations_db"]()
    accounting = ns["open_cache_db"]()
    try:
        if route == "contract":
            cache._set_cache_meta(conv, "codex_conversation_contract_version", "0-stale")
        else:
            cache._set_cache_meta(conv, route, "1")
        conv.commit()
        before = {"cache": snapshot(accounting), "conversations": snapshot(conv)}
        stats = ns["sync_codex_conversations"](conv)
        immediate = {"cache": snapshot(accounting), "conversations": snapshot(conv)}
    finally:
        accounting.close()
        conv.close()
    # Reproduce the existing preservation helper's production opener sequence.
    _entries(ns, retained)
    _events(ns)
    _families(ns)
    if route != "contract":
        _meta(ns, route, "conversations")
    accounting = sqlite3.connect(f"file:{core.CACHE_DB_PATH}?mode=ro", uri=True)
    conv = sqlite3.connect(f"file:{core.CONVERSATIONS_DB_PATH}?mode=ro", uri=True)
    try:
        reopened = {"cache": snapshot(accounting), "conversations": snapshot(conv)}
    finally:
        conv.close()
        accounting.close()
    evidence = {"label": "UNCHANGED-RUNTIME DIAGNOSTIC sr4", "mode": mode, "route": route,
                "refusal": stats.rebuild_refused,
                "beforeSync": before, "afterSyncSameConnection": immediate,
                "afterOriginalProductionOpeners": reopened,
                "sameConnectionNamedDifferences": differences(before, immediate),
                "productionOpenerNamedDifferences": differences(immediate, reopened)}
    print("TASK06_DIAGNOSTIC_BEGIN " + mode + " " + route)
    print(json.dumps(evidence, sort_keys=True, indent=2))
    print("TASK06_DIAGNOSTIC_END " + mode + " " + route)
    assert stats.rebuild_refused in ("decoder-unavailable", "decode-error")
    immediate_changes = differences(before, immediate)
    assert set(immediate_changes) == {"conversations.cache_meta"}
    status_change = immediate_changes["conversations.cache_meta"]
    assert status_change["removedNamedRows"] == [
        row for row in before["conversations"]["cache_meta"]
        if row["key"] == "codex_container_status"]
    assert len(status_change["addedNamedRows"]) == 1
    assert status_change["addedNamedRows"][0]["key"] == "codex_container_status"
    observed = json.loads(status_change["addedNamedRows"][0]["value"])
    assert observed["version"] == 1
    assert observed["census"] == "partial"
    assert observed["rebuildRefused"] == stats.rebuild_refused
    assert reopened == immediate



def _retained_contract_estate(tmp_path, monkeypatch, *, compressed):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    path = day / _name(1)
    write_rollout(path, conversation_records(2, session_id="healthy"))
    if compressed:
        path.with_name(path.name + ".zst").write_bytes(zstd_bytes(path.read_bytes()))
        path.unlink()
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        if compressed:
            accepted = conv.execute(
                "SELECT container_kind,container_state,content_anchor_version,"
                "content_anchor,content_anchor_offset,last_byte_offset "
                "FROM codex_conversation_source_files WHERE path=?", (str(path),)).fetchone()
            assert accepted[0:3] == ("zstd", "accepted", 1)
            assert accepted[3] is not None
            assert accepted[4] == accepted[5]
    finally:
        conv.close()
    return ns, path


def _arm_retained_contract(ns):
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     ("codex_conversation_contract_version", "0-stale"))
        conv.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     ("conversation_rebuild_codex_pending", "1"))
        conv.commit()
    finally:
        conv.close()


def _count_real_contract_replays(monkeypatch):
    cache = sys.modules["_cctally_cache"]
    original = cache._replay_codex_normalization
    calls = []

    def replay(conn):
        calls.append(True)
        return original(conn)

    monkeypatch.setattr(cache, "_replay_codex_normalization", replay)
    return calls


def _forbid_opener_decode(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("normal opener tried to decode or validate a rollout")
    monkeypatch.setattr(rc, "open_decoded", forbidden)
    monkeypatch.setattr(rc, "validate_full_stream", forbidden)


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "accepted-zstd"])
def test_retained_contract_replay_really_runs_without_opening_containers(
        tmp_path, monkeypatch, compressed):
    ns, _path = _retained_contract_estate(tmp_path, monkeypatch, compressed=compressed)
    calls = _count_real_contract_replays(monkeypatch)
    _arm_retained_contract(ns)
    _forbid_opener_decode(monkeypatch)
    import _sql_plan_guard as guard
    with guard.capture_sql_plans() as recorder:
        conv = ns["open_conversations_db"]()
    if compressed:
        decisions = [statement for statement in recorder.statements
                     if "_cctally_cache._codex_retained_contract_replay_ready" in statement.call_sites]
        assert any("FROM codex_conversation_source_files" in statement.sql
                   for statement in decisions)
        unexplained, _stale = guard.classify(decisions, allowlist=(), pending=())
        assert not unexplained, guard.format_findings(unexplained)
    try:
        assert calls == [True]
        assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                            ("codex_conversation_contract_version",)).fetchone() == ("5",)
        assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                            ("conversation_rebuild_codex_pending",)).fetchone() is None
        assert conv.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] == 4
    finally:
        conv.close()


def test_retained_contract_migrated_accepted_default_is_not_current_evidence(tmp_path, monkeypatch):
    ns, path = _retained_contract_estate(tmp_path, monkeypatch, compressed=True)
    calls = _count_real_contract_replays(monkeypatch)
    _arm_retained_contract(ns)
    core = sys.modules["_cctally_core"]
    conv = sqlite3.connect(core.CONVERSATIONS_DB_PATH)
    try:
        conv.execute("UPDATE codex_conversation_source_files SET "
                     "content_anchor=NULL,content_anchor_version=NULL,content_anchor_offset=NULL "
                     "WHERE path=?", (str(path),))
        conv.commit()
    finally:
        conv.close()
    _forbid_opener_decode(monkeypatch)
    conv = ns["open_conversations_db"]()
    try:
        assert calls == []
        assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                            ("codex_conversation_contract_version",)).fetchone() == ("0-stale",)
        assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                            ("conversation_rebuild_codex_pending",)).fetchone() == ("1",)
    finally:
        conv.close()


def test_retained_contract_refusal_clears_only_after_complete_guarded_sync(tmp_path, monkeypatch):
    ns, _path = _retained_contract_estate(tmp_path, monkeypatch, compressed=True)
    cache = sys.modules["_cctally_cache"]
    calls = _count_real_contract_replays(monkeypatch)
    _arm_retained_contract(ns)
    core = sys.modules["_cctally_core"]
    conv = sqlite3.connect(core.CONVERSATIONS_DB_PATH)
    try:
        cache._set_cache_meta(conv, "codex_container_status", json.dumps({
            "version": 1, "census": "partial", "rebuildRefused": "decode-error",
            "skipped": {"decode-error": 1}, "decoder": "backports.zstd"}))
        conv.commit()
    finally:
        conv.close()
    with monkeypatch.context() as no_decode:
        _forbid_opener_decode(no_decode)
        conv = ns["open_conversations_db"]()
        try:
            assert calls == []
            assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                                ("codex_conversation_contract_version",)).fetchone() == ("0-stale",)
            assert conv.execute("SELECT value FROM cache_meta WHERE key=?",
                                ("conversation_rebuild_codex_pending",)).fetchone() == ("1",)
        finally:
            conv.close()
    conv = ns["open_conversations_db"]()
    try:
        stats = ns["sync_codex_conversations"](conv)
        assert stats.rebuild_refused is None
        assert stats.barrier_keys_published == 1
        assert stats.barrier_files_published == 1
        status = json.loads(conv.execute("SELECT value FROM cache_meta WHERE key=?",
                                         ("codex_container_status",)).fetchone()[0])
        assert status["census"] == "partial"
        assert status["rebuildRefused"] == "decode-error"
        assert conv.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] == 4
        # Publication completed this pass; the next ordinary guarded sync can
        # now prove the complete census and clear the retained refusal.
        stats = ns["sync_codex_conversations"](conv)
        assert stats.rebuild_refused is None
        assert stats.files_processed == 0
        assert stats.files_skipped_unchanged == 1
        status = json.loads(conv.execute("SELECT value FROM cache_meta WHERE key=?",
                                         ("codex_container_status",)).fetchone()[0])
        assert status["census"] == "complete"
        assert status["rebuildRefused"] is None
        assert conv.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] == 4
    finally:
        conv.close()
    assert calls == []  # successful full sync ingests; it does not fake a retained replay
    _arm_retained_contract(ns)
    with monkeypatch.context() as no_decode:
        _forbid_opener_decode(no_decode)
        conv = ns["open_conversations_db"]()
        conv.close()
    assert calls == [True]


# Task 6 review regressions: typed command failures, warning adapters, and
# incomplete maintenance probes must preserve the existing fail-closed contract.
@pytest.mark.parametrize("source", ["codex", "all"])
@pytest.mark.parametrize("route", ["range-cost", "diff", "diff-projects", "cache-report", "report", "report-sync"])
def test_review_t6_typed_fallback_reaches_command_boundary(
        estate, monkeypatch, capsys, source, route):
    import datetime as dt
    from _lib_quota import QuotaObservation, QuotaWindowIdentity

    ns, *_ = estate
    adapter = ns["_cctally_source_analytics"]
    utc = dt.timezone.utc
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="review-root", logical_limit_key="primary",
        observed_slot="primary", window_minutes=7 * 24 * 60)
    observation = QuotaObservation(
        identity=identity, captured_at=dt.datetime(2026, 7, 20, 11, tzinfo=utc),
        used_percent=10.0, resets_at=dt.datetime(2026, 7, 25, tzinfo=utc),
        source_path="/synthetic/quota.jsonl", line_offset=1)
    monkeypatch.setattr(adapter, "load_codex_quota_observations", lambda: (observation,))
    # Isolate the unrelated Claude leg from the global lock held for Codex.
    monkeypatch.setattr(adapter, "_run_claude_json_adapter",
                        lambda args, command: adapter.SourceResult("claude", "empty", {}))
    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", lambda **kwargs: None)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-21T12:00:00Z")
    commands = {
        "range-cost": ["range-cost", "--start", "2026-07-20T00:00:00Z",
                       "--end", "2026-07-21T00:00:00Z"],
        "diff": ["diff", "--a", "last-7d", "--b", "prev-7d", "--only", "overall,models", "--sync"],
        "diff-projects": ["diff", "--a", "last-7d", "--b", "prev-7d", "--sync"],
        "cache-report": ["cache-report", "--since", "2026-07-20", "--until", "2026-07-21"],
        "report": ["report"],
        "report-sync": ["report", "--sync-current"],
    }
    lock = open(sys.modules["_cctally_core"].CACHE_LOCK_PATH, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        code = ns["main"]([*commands[route], "--source", source, "--json"])
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.splitlines().count(DIRECT) == 1
    assert captured.out == ""


@pytest.fixture
def retained_only(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    day = tmp_path / "codex" / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(day.parents[3]))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-21T12:00:00Z")
    path = day / _name(1)
    write_rollout(path, conversation_records(3, session_id="retained-only"))
    # Recognition remains grounded in an ordinary readable keeper. The only
    # compressed rollout is retained, with no never-ingested file masking it.
    write_rollout(day / _name(9), token_records(1, session_id="keeper"))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(path.read_bytes()))
    path.unlink()
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    return ns, path


@pytest.mark.parametrize("previous_walk", [False, True], ids=["first-transition", "already-pending"])
def test_review_t6_retained_only_warning_is_once_and_shared_sync_is_silent(
        retained_only, previous_walk, capsys):
    ns, path = retained_only
    if previous_walk:
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn)
        finally:
            conn.close()
        assert stats.container_pending == 1
    assert capsys.readouterr().err == ""
    for iteration in range(2):
        assert ns["main"](["codex", "daily", "--since", "20260701"]) == 0
        assert capsys.readouterr().err.splitlines().count(WARNING) == (iteration == 0)
        assert _entries(ns, path) == 3


@pytest.mark.parametrize("route", ["project", "cache-report", "range-cost", "diff", "report-sync"])
def test_review_t6_explicit_qualified_sync_warns_once(estate, monkeypatch, capsys, route):
    ns, *_rest, mode = estate
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-21T12:00:00Z")
    commands = {
        "project": ["project", "--since", "2026-07-20", "--until", "2026-07-21"],
        "cache-report": ["cache-report", "--since", "2026-07-20", "--until", "2026-07-21"],
        "range-cost": ["range-cost", "--start", "2026-07-20T00:00:00Z", "--end", "2026-07-21T00:00:00Z", "--project", "missing"],
        "diff": ["diff", "--a", "last-7d", "--b", "prev-7d", "--sync"],
        "report-sync": ["report", "--sync-current"],
    }
    # The loader is also a dashboard/shared adapter: its default must be silent.
    import datetime as dt
    adapter = ns["_cctally_source_analytics"]
    adapter.load_qualified_codex_entries(
        dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc), speed="standard")
    assert capsys.readouterr().err == ""
    for iteration in range(2):
        ns["main"]([*commands[route], "--source", "codex", "--json"])
        err = capsys.readouterr().err
        expected = int(mode == "no-decoder" and iteration == 0)
        assert err.splitlines().count(WARNING) == expected


@pytest.mark.parametrize("kind", ["verification", "deletion", "both", "recent", "unknown"])
def test_review_t6_unavailable_conversations_preserves_known_cache_debt(
        retained_only, monkeypatch, kind):
    import datetime as dt
    from contextlib import contextmanager
    import _cctally_doctor
    import _lib_doctor

    ns, path = retained_only
    # Presence and capability are independent; this case has a decoder so the
    # maintenance sentence, not the no-decoder sentence, is decisive.
    monkeypatch.setattr(rc, "decoder_capability", lambda: "backports.zstd")
    conv = ns["open_conversations_db"]()
    conv.close()
    stamp = "2026-07-21T11:00:00Z" if kind == "recent" else "2026-07-19T00:00:00Z"
    v = int(kind in ("verification", "both", "recent"))
    p = int(kind in ("deletion", "both"))
    conn = ns["open_cache_db"]()
    try:
        if v:
            conn.execute("UPDATE codex_session_files SET container_state='pending' WHERE path=?", (str(path),))
        else:
            # Make the live compressed generation genuinely accepted, so an
            # unknown conversations probe is the only unresolved observation.
            selected = rc.select_container(path)
            conn.execute("UPDATE codex_session_files SET container_kind='zstd', container_state='accepted', container_size_bytes=?, decoded_size_bytes=size_bytes, mtime_ns=?, device_id=?, inode=? WHERE path=?",
                         (selected.fingerprint.size, selected.fingerprint.mtime_ns,
                          selected.fingerprint.device, selected.fingerprint.inode, str(path)))
        if p:
            conn.execute("INSERT INTO codex_prune_debt VALUES(?,?,?)", ("/missing.jsonl", "review-root", stamp))
        conn.execute("INSERT OR REPLACE INTO cache_meta VALUES(?,?)", (
            "codex_container_status", json.dumps({"version": 1, "census": "complete",
                                                 "verificationPendingSince": stamp})))
        conn.commit()
    finally:
        conn.close()

    @contextmanager
    def unavailable(**kwargs):
        yield None

    monkeypatch.setattr(_cctally_doctor, "_conversation_ro_guarded", unavailable)
    state = _cctally_doctor.doctor_gather_state(
        now_utc=dt.datetime(2026, 7, 21, 12, tzinfo=dt.timezone.utc))
    check = next(c for cat in _lib_doctor.run_checks(state).categories for c in cat.checks
                 if c.id == "data.codex_compressed_rollouts")
    assert state.codex_maintenance_debt is not None
    assert state.codex_maintenance_debt["verifications"] == v
    assert state.codex_maintenance_debt["deletions"] == p
    assert check.details["maintenanceCoverage"] == "partial"
    assert check.summary != ALL_OK
    if kind == "recent":
        assert (check.severity, check.summary) == ("ok", CONTINUING)
    elif v or p:
        assert (check.severity, check.summary) == ("warn", _debt_sentence(v, 0, p))
        assert state.codex_maintenance_debt["olderThan24h"] is True
    else:
        assert check.severity == "warn"
        assert "unavailable" in check.summary


def test_review_t6_unknown_maintenance_coverage_cannot_certify_all_clear():
    check = _compressed_check(codex_compressed_census=_census(1, complete=1),
                              codex_maintenance_debt=None, **DECODER)
    assert check.severity == "warn"
    assert check.summary != ALL_OK
    assert check.details["maintenanceCoverage"] == "partial"
    assert check.details["verificationDebt"] is None
    assert check.details["pruneDebt"] is None


def test_review_t6_terminal_unverified_retained_history_warns_once(
        retained_only, monkeypatch, capsys):
    ns, path = retained_only
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_files SET container_state='unverified' WHERE path=?",
                     (str(path),))
        conn.commit()
        before = snapshot_tables(conn, CACHE_FAMILIES)
    finally:
        conn.close()
    calls = []

    def unavailable():
        calls.append("resolve")
        return rc.UNAVAILABLE

    monkeypatch.setattr(rc, "resolve_zstd_backend", unavailable)
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn)
        assert snapshot_tables(conn, CACHE_FAMILIES) == before
    finally:
        conn.close()
    assert stats.container_pending == 0
    assert stats.container_skipped == {"verification-unverified": 1}
    assert stats.container_decoder == "unavailable"
    assert calls == ["resolve"]  # the status writer's existing observation
    assert capsys.readouterr().err == ""  # shared sync remains silent
    for iteration in range(2):
        previous_calls = len(calls)
        assert ns["main"](["codex", "daily", "--since", "20260701"]) == 0
        assert len(calls) == previous_calls + 1  # no extra warning resolver call
        assert capsys.readouterr().err.splitlines().count(WARNING) == (iteration == 0)
        assert _entries(ns, path) == 3
        conn = ns["open_cache_db"]()
        try:
            assert snapshot_tables(conn, CACHE_FAMILIES) == before
        finally:
            conn.close()
