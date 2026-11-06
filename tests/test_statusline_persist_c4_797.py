"""#797 T10: the persistence leg stays database- and journal-free (C4 of #825)
with provider-activity capture, and its cost is measured against the #825
baseline.

`test_persistence_leg_opens_no_journal_or_database` is the structural guard:
for usage-only, unchanged and scan-bound renders the real `_statusline_persist`
(with the detached reducer stubbed exactly where a render forks it) opens no
journal file and no SQLite database. `test_persistence_leg_median_t10` is the
measurement; it runs only when ``CCTALLY_T10_MEASURE=1`` (timings are not a
suite gate) and prints the per-class medians with and without capture.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
import sqlite3
import statistics
import sys
import time

import pytest

from conftest import load_script, redirect_paths


def _assistant(ts, request_id="req_01"):
    return json.dumps({
        "parentUuid": None, "isSidechain": False, "type": "assistant",
        "sessionId": "s1", "cwd": "/Users/u/proj", "requestId": request_id,
        "message": {"id": "msg_01", "model": "claude-opus-5-5", "role": "assistant",
                    "content": [{"type": "text", "text": "ok" * 400}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
        "uuid": "u-1", "timestamp": ts,
    })


def _progress(ts):
    return json.dumps({"type": "progress", "timestamp": ts,
                       "data": {"content": "p" * 900}})


def _append(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for line in lines:
            fh.write(line + "\n")


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    monkeypatch.setattr(app, "_nudge_dashboard_repaint", lambda: None)
    monkeypatch.setattr(app, "_statusline_oauth_tick", lambda *a, **k: None)
    return app


class _Clock:
    def __init__(self, monkeypatch):
        self.value = float(int(time.time()))
        monkeypatch.setattr(time, "time", lambda: self.value)


def _render_classes(tmp_path):
    """Three transcript layouts: usage-only (a session that grows by one
    response per render), unchanged (nothing moved since the last render) and
    scan-bound (the main tail holds no response inside the 1 MiB bound and
    twelve subagent files grow each render, past the eight-file budget)."""
    usage = tmp_path / "proj" / "usage.jsonl"
    _append(usage, [_assistant(f"2026-10-26T09:{i % 60:02d}:00Z") for i in range(200)])
    for k in range(3):
        _append(usage.with_suffix("") / "subagents" / f"agent-{k}.jsonl",
                [_assistant("2026-10-26T09:00:00Z")] * 20)
    unchanged = tmp_path / "proj" / "unchanged.jsonl"
    _append(unchanged, [_assistant(f"2026-10-26T09:{i % 60:02d}:00Z") for i in range(200)])
    bound = tmp_path / "proj" / "bound.jsonl"
    _append(bound, [_assistant("2026-10-26T08:00:00Z")]
            + [_progress("2026-10-26T08:00:01Z")] * 1200)
    for k in range(12):
        _append(bound.with_suffix("") / "subagents" / f"agent-{k:02d}.jsonl",
                [_assistant("2026-10-26T08:00:00Z")] * 20)

    def grow_usage(i):
        _append(usage, [_assistant(f"2026-10-26T10:{i % 60:02d}:{i % 60:02d}Z")])

    def grow_bound(i):
        _append(bound, [_progress("2026-10-26T10:00:00Z")])
        for k in range(12):
            _append(bound.with_suffix("") / "subagents" / f"agent-{k:02d}.jsonl",
                    [_progress("2026-10-26T10:00:00Z")])

    return {
        "usage-only": (usage, grow_usage, True),
        "unchanged": (unchanged, lambda i: None, False),
        "scan-bound": (bound, grow_bound, False),
    }


def _parsed(app, transcript, *, percent, session, now):
    payload = {"session_id": session, "transcript_path": str(transcript),
               "rate_limits": {"seven_day": {"used_percentage": percent,
                                             "resets_at": _iso(now + 3 * 86400)}}}
    return app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())


def test_persistence_leg_opens_no_journal_or_database(app, tmp_path, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as journal
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    clock = _Clock(monkeypatch)
    classes = _render_classes(tmp_path)
    # Prime the store through one real, synchronous reduction per class so
    # the measured renders are steady-state ones.
    for name, (transcript, _grow, _rising) in classes.items():
        sl._statusline_persist(_parsed(app, transcript, percent=20, session=name,
                                       now=int(clock.value)), sync_for_test=True)
    forks = []
    monkeypatch.setattr(sl, "_dispatch_render_worker", lambda reservation: (
        forks.append(reservation), sl._release_reservation(reservation)))
    forbidden = []

    def refuse(label):
        def _raise(*args, **kwargs):
            forbidden.append(label)
            raise AssertionError(label)
        return _raise

    monkeypatch.setattr(sqlite3, "connect", refuse("sqlite3.connect"))
    for name in ("ensure_account_bound_cutover", "find_account_bound_cutover",
                 "append_record", "append_records", "run_stats_ingest"):
        monkeypatch.setattr(journal, name, refuse(name))
    journal_dir = str(core.JOURNAL_DIR)
    databases = tuple(str(p) for p in (core.DB_PATH, core.CACHE_DB_PATH))
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def guarded(real):
        def _open(file, *args, **kwargs):
            text = os.fspath(file) if not isinstance(file, int) else ""
            if text.startswith(journal_dir) or text.startswith(databases):
                forbidden.append(text)
                raise AssertionError(f"render opened {text}")
            return real(file, *args, **kwargs)
        return _open

    monkeypatch.setattr(builtins, "open", guarded(real_open))
    monkeypatch.setattr(io, "open", guarded(real_io_open))
    monkeypatch.setattr(os, "open", guarded(real_os_open))
    captured = []
    real_capture = pa.capture_activity
    monkeypatch.setattr(pa, "capture_activity", lambda *a, **k: (
        lambda w: (captured.append(w), w)[1])(real_capture(*a, **k)))
    for i in range(3):
        clock.value += 1
        for name, (transcript, grow, rising) in classes.items():
            grow(i)
            sl._statusline_persist(_parsed(
                app, transcript, percent=21 + i if rising else 20, session=name,
                now=int(clock.value)))
    assert forbidden == []
    assert len(captured) == 9
    statuses = {w.status for w in captured}
    assert "known" in statuses and "unknown" in statuses


def _median_ms(samples):
    return statistics.median(samples) * 1000.0


@pytest.mark.skipif(os.environ.get("CCTALLY_T10_MEASURE") != "1",
                    reason="timing measurement; set CCTALLY_T10_MEASURE=1")
def test_persistence_leg_median_t10(app, tmp_path, monkeypatch):
    """The #825 method (commit 700ae6268): 200 renders per run, three runs per
    variant, the same session's renders with capture replaced by a constant
    witness as the control. Each render advances the clock one second, so
    every render appends a distinct evaluation."""
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(sl, "_dispatch_render_worker", lambda reservation: sl._release_reservation(
        reservation))
    real_capture = pa.capture_activity
    constant = real_capture(None, cache_path=tmp_path / "unused.json")
    results = {}
    for variant in ("control", "capture"):
        monkeypatch.setattr(pa, "capture_activity", real_capture if variant == "capture"
                            else (lambda *a, **k: constant))
        for run in range(3):
            root = tmp_path / f"{variant}-{run}"
            classes = _render_classes(root)
            for name, (transcript, grow, rising) in classes.items():
                session = f"{variant}-{run}-{name}"
                sl._statusline_persist(_parsed(app, transcript, percent=20, session=session,
                                               now=int(clock.value)), sync_for_test=True)
                samples = []
                for i in range(200):
                    clock.value += 1
                    grow(i)
                    parsed = _parsed(app, transcript, percent=20 + (i % 2 if rising else 0),
                                     session=session, now=int(clock.value))
                    start = time.perf_counter()
                    sl._statusline_persist(parsed)
                    samples.append(time.perf_counter() - start)
                results.setdefault((name, variant), []).append(_median_ms(samples))
    lines = []
    for name in ("usage-only", "unchanged", "scan-bound"):
        control = statistics.median(results[(name, "control")])
        capture = statistics.median(results[(name, "capture")])
        lines.append(f"T10 {name}: control {control:.3f} ms, capture {capture:.3f} ms, "
                     f"delta {capture - control:+.3f} ms; runs control "
                     f"{[round(x, 3) for x in results[(name, 'control')]]} capture "
                     f"{[round(x, 3) for x in results[(name, 'capture')]]}")
    # The cold case, informational: the first capture of a scan-bound session
    # (no cache yet: the 1 MiB main scan plus eight full subagent scans).
    bound = _render_classes(tmp_path / "cold")["scan-bound"][0]
    cold = []
    for i in range(20):
        start = time.perf_counter()
        real_capture(str(bound), cache_path=tmp_path / "cold-cache" / f"{i}.json")
        cold.append(time.perf_counter() - start)
    lines.append(f"T10 cold scan-bound capture: median {_median_ms(cold):.3f} ms")
    print("\n".join(lines))
    for name in ("usage-only", "unchanged", "scan-bound"):
        delta = (statistics.median(results[(name, "capture")])
                 - statistics.median(results[(name, "control")]))
        assert delta <= 2.0, lines


# --------------------------------------------------------------------------
# #1019/#1014 (spec 4.7 C4, 4.3): the v3 namespace and the frozen supports
# --------------------------------------------------------------------------


def _tree(root):
    return {p: p.read_bytes() for p in sorted(pathlib.Path(root).rglob("*"))
            if p.is_file()}


def _journal_kinds():
    import _cctally_core as core
    kinds = []
    for path in sorted(core.JOURNAL_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                kinds.append(((record.get("payload") or {}).get("kind"), record))
    return kinds


def test_v2_coordination_state_joins_legacy_inventory(app, tmp_path, monkeypatch):
    """C4: 1.116.1's v2 coordination state (candidates, pending drops and
    retained lows in its control, frozen plans, acknowledgements,
    tombstones, `transport.last`) is legacy spool input: never imported,
    never written, pruned or deleted; quiet files raise nothing and a
    post-boundary v2 write is a `legacy_spool_activity` trigger. Detects a
    v3 binary that reads or cleans up the previous release's spool."""
    import _cctally_core as core
    import _cctally_journal as journal
    sl = sys.modules["_cctally_statusline"]
    monkeypatch.setattr(sl, "_dispatch_render_worker", lambda reservation:
                        sl._release_reservation(reservation))
    assert core.STATUSLINE_ACCOUNT_BOUND_DIR.name == "statusline-account-bound-v3"
    assert core.STATUSLINE_ACCOUNT_BOUND_V2_DIR.name == (
        "statusline-account-bound-v2")
    assert core.LEGACY_SPOOL_BASELINE_PATH.parent == (
        core.STATUSLINE_ACCOUNT_BOUND_DIR)
    v2 = core.STATUSLINE_ACCOUNT_BOUND_V2_DIR
    account = v2 / "accounts" / ("k" * 32)
    for rel in ("candidates/c.json", "control.json", "frozen-plans/p.json",
                "acknowledgements/a.json", "tombstones/t.json"):
        (account / rel).parent.mkdir(parents=True, exist_ok=True)
        (account / rel).write_text('{"v2": true}')
    (v2 / "transport.last").write_text("1")
    for diagnostic in ("cutover-coordinate.json", "legacy-spool-baseline.json",
                       "legacy-writer-evidence.json",
                       "revision-coordinate.json"):
        (v2 / diagnostic).write_text("{}")
    paths = {str(p) for p in journal._legacy_spool_paths()}
    assert str(v2 / "accounts") in paths and str(v2 / "transport.last") in paths
    assert not any(name in p for p in paths for name in (
        "cutover-coordinate", "legacy-writer-evidence", "revision-coordinate"))
    assert str(v2 / "legacy-spool-baseline.json") not in paths

    journal.ensure_account_bound_cutover()
    before = _tree(v2)
    clock = _Clock(monkeypatch)
    sl._statusline_persist(_parsed(app, tmp_path / "none.jsonl", percent=20,
                                   session="current", now=int(clock.value)),
                           sync_for_test=True)
    assert _tree(v2) == before, "the v3 binary touched the v2 spool"
    current = core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts"
    assert current.exists()
    assert not any(data == b'{"v2": true}' for data in _tree(current).values())
    # Quiet pre-boundary v2 files raise nothing.
    assert not sl._rescan_legacy_spool()
    assert not [k for k, _r in _journal_kinds() if k == "legacy_spool_activity"]
    # A v2 diagnostic refresh is not spool activity; a v2 write is.
    (v2 / "revision-coordinate.json").write_text('{"refreshed": true}')
    assert not sl._rescan_legacy_spool()
    (account / "candidates" / "late.json").write_text('{"v2": "late"}')
    assert sl._rescan_legacy_spool()
    [activity] = [r for k, r in _journal_kinds() if k == "legacy_spool_activity"]
    assert activity["payload"]["changed"] == [
        f"statusline-account-bound-v2/accounts/{'k' * 32}/candidates/late.json"]


def _support(name, *, witness, received=900):
    import _lib_statusline_candidates as lib
    value = lib.AxisValue(30.0, 5_000_000, canonical_key=5_000_000)
    token = (name * 64)[:64]
    return lib.SupportingObservation(
        observation_id=lib.observation_identity(token, "sevenDay", value),
        token=token, percent=30.0, raw_resets_at=5_000_000,
        received_at=received, witness=witness)


def _witness(instant="2026-10-22T09:00:00Z"):
    import _lib_provider_activity as pa
    return pa.ActivityWitness(1, 1, "known", instant, None, 1, 1, "0" * 64)


def _pending_with(observations):
    import _lib_statusline_candidates as lib
    return lib.PendingDrop(
        canonical_key=5_000_000, reduced_percent=30.0, first_seen_at=700,
        kernel_stage="settling", attempts=0,
        contributors={item.token: lib.Contributor(700, False)
                      for item in observations},
        retry_signature=None, deadline_at=880,
        retained=lib.RetainedLow(30.0, 5_000_000, 5_000_000,
                                 tuple(observations)))


def test_retained_observation_witness_round_trips_under_v3_schema(app):
    """K3 groundwork (spec 4.2): each retained observation persists the
    witness its contributor's candidate carried when it was retained, as an
    exact-key member (`null` for none) of the v3 control schema; a v3
    document without it is refused, a v2 one still reads (witness None)."""
    import _lib_statusline_candidates as lib
    sl = sys.modules["_cctally_statusline"]
    assert lib.CONTROL_SCHEMA_VERSION == 3
    assert set(lib.CONTROL_SCHEMA_VERSIONS_READ) == {1, 2, 3}
    observations = sorted([_support("a", witness=_witness()),
                           _support("b", witness=None)],
                          key=lambda item: item.observation_id)
    pending = _pending_with(observations)
    projection = lib.DbProjection(None, None, db_files={
        "main": lib.FileFingerprint(device=1, inode=1, size=1, mtime_ns=1),
        "wal": None})
    control = lib.ControlState(projection, {"fiveHour": None,
                                            "sevenDay": pending})
    doc = sl._control_document(control)
    assert doc["schemaVersion"] == 3
    docs = doc["pendingDrops"]["sevenDay"]["retained"]["observations"]
    for item in docs:
        assert set(item) == {"observationId", "token", "percent",
                             "rawResetsAt", "receivedAt", "witness"}
    by_token = {item["token"]: item for item in docs}
    assert by_token[("b" * 64)]["witness"] is None
    assert by_token[("a" * 64)]["witness"]["instant"] == "2026-10-22T09:00:00Z"
    loaded = lib.validate_control_document(json.loads(json.dumps(doc)),
                                           now_epoch=1000)
    assert loaded.pending_drops["sevenDay"].retained.observations == tuple(
        observations)
    missing = json.loads(json.dumps(doc))
    del missing["pendingDrops"]["sevenDay"]["retained"]["observations"][0][
        "witness"]
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(missing, now_epoch=1000)
    v2 = json.loads(json.dumps(doc))
    v2["schemaVersion"] = 2
    for item in v2["pendingDrops"]["sevenDay"]["retained"]["observations"]:
        del item["witness"]
    old = lib.validate_control_document(v2, now_epoch=1000)
    assert all(item.witness is None for item in
               old.pending_drops["sevenDay"].retained.observations)


class _Binding:
    account_key = "a" * 32

    def document(self):
        return {"accountKey": self.account_key}


def _retained_low_plan(observations):
    import _lib_statusline_candidates as lib
    sl = sys.modules["_cctally_statusline"]
    now = 1000
    db = lib.DbProjection(
        None, lib.AxisProjection(90.0, 5_000_000, 5_000_000, now - 100, "api", 0),
        account_key=_Binding.account_key)
    control = lib.ControlState(db, {"fiveHour": None,
                                    "sevenDay": _pending_with(observations)})
    # No live contributor reports the retained 30, so the expired deadline
    # publishes it as `retained_low`.
    decision = lib.reduce_candidates(
        (), db=db, control=control, tombstones={}, now_epoch=now,
        account_key=_Binding.account_key)
    return sl._bound_publication_plan(
        decision, db, (), _Binding(), generation=1, now_epoch=now,
        intake_sequence=0)


def test_retained_low_provenance_freezes_sorted_supports_into_identity(app):
    """Spec 4.3: a `retained_low` axis freezes its supports (sorted by
    observation id, ties by the canonical JSON of the witness) into the
    plan body before the publication identity, so a support's witness is
    part of what the identity names (detects recovery that could not bind a
    frozen plan to the evidence it was published from)."""
    first = [_support("b", witness=_witness()), _support("a", witness=None)]
    plan = _retained_low_plan(first)
    assert plan is not None
    provenance = plan["provenance"]["sevenDay"]
    assert provenance["kind"] == "retained_low"
    supports = provenance["supports"]
    assert [item["observationId"] for item in supports] == sorted(
        item.observation_id for item in first)
    assert {item["token"]: item["witness"] for item in supports}[
        "a" * 64] is None
    second = [_support("b", witness=_witness("2026-10-22T09:05:00Z")),
              _support("a", witness=None)]
    changed = _retained_low_plan(second)
    assert changed["provenance"]["sevenDay"]["supports"] != supports
    assert changed["publicationId"] != plan["publicationId"]
    assert _retained_low_plan(list(reversed(first)))["publicationId"] == (
        plan["publicationId"])
