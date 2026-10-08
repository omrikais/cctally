"""#872: Codex derived caches advance only from the population their delta's
base names.

Spec: docs/superpowers/specs/2026-10-02-872-codex-project-cache-delta-base.md
(revision 4, Codex PROCEED at 5eb3ae551).

Every "equals cold" comparison is against a complete cold build taken in a
SEPARATE Python process over a backup copy of the same stores (spec §7,
review F9). Taking a reference therefore never repairs, primes or otherwise
disturbs the warm chain under test, and `_warm_fingerprint` proves that around
every reference.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import itertools
import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import _lib_snapshot_cache as sc
from _cctally_dashboard_sources import DashboardReadContext

import test_dashboard_source_read_model as trm
from test_codex_account_read_model import _observations, _split_corpus_accounts
from test_dashboard_accounts_wire import _ACCT_A, _ACCT_B, _seed_codex_accounts
from test_dashboard_source_read_model import NOW, START, _cache_root_key, _seeded_context

UTC = dt.timezone.utc
_ACCT_C = "c" * 32
#: The decorated scope builder always returns this bucket as a live key.
_UNATTRIBUTED = "unattributed"
TESTS = Path(__file__).resolve().parent
BIN = TESTS.parent / "bin"
_COLD_MARK = "COLD872 "
RED = "/synthetic/root-a/project-red"
BLUE = "/synthetic/root-a/project-blue"
RED_B = "/synthetic/root-b/project-red"


@dataclasses.dataclass
class _Env:
    ns: dict
    cache: sqlite3.Connection
    stats: sqlite3.Connection
    module: object
    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    scenario: dict | None = None
    counter: itertools.count = dataclasses.field(
        default_factory=lambda: itertools.count(1))

    @property
    def data_dir(self) -> Path:
        return self.tmp / "data"

    @property
    def codex_home(self) -> Path:
        return self.tmp / "provider"


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    module = sys.modules["_cctally_dashboard_sources"]
    module.reset_codex_source_caches()
    state = _Env(ns, cache, stats, module, tmp_path, monkeypatch)
    try:
        yield state
    finally:
        module.reset_codex_source_caches()
        cache.close()
        stats.close()


def _context(env, *, now=NOW, range_start=START, codex_budget=None):
    return DashboardReadContext(
        cache_conn=env.cache, stats_conn=env.stats, range_start=range_start,
        now_utc=now, display_tz_name="UTC", codex_budget=codex_budget)


def _build(env, version, **ctx):
    return env.module.build_codex_source_state(
        _context(env, **ctx), data_version=version)


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _published(state, *, version=True):
    """Every published, delta-derived surface of one Codex generation.

    `data` carries the parent domains, `accounts` (cards) and
    `account_scopes` (every child); `combined_accounting` and `account_scope`
    sit beside it. `clock_data` is excluded: it carries process-local values,
    the #857 token among them, that a separate process never shares.
    """
    out = {
        "availability": state.availability,
        "warnings": state.warnings,
        "data": state.data,
        "account_scope": state.account_scope,
        "combined_accounting": state.combined_accounting,
    }
    if version:
        out["data_version"] = state.data_version
    return json.loads(json.dumps(_jsonable(out), sort_keys=True))


def _record(env):
    slot = getattr(env.module, "_CODEX_DERIVED_COHERENCE", None)
    return None if slot is None else slot.get("state")


def _owner_contents(module):
    """Every checkpointed owner's entries, compared by value.

    Contents, not the byte ledger: a journal restore recharges each entry
    against the carrier as it then stands, so a borrowed-payload charge can
    move by a few bytes while every entry is exactly the one checkpointed.
    """
    return {owner.name: dict(owner.cache)
            for owner in module._CODEX_SOURCE_CACHE_OWNERS}


def _warm_fingerprint(module):
    """Process state a cold reference must never touch (review F9)."""
    slot = getattr(module, "_CODEX_DERIVED_COHERENCE", None)
    carrier = sc.checkpoint_codex_accounting_cache_state()
    return (
        module.codex_source_retained_bytes(),
        repr(module._quota_observation_cache_checkpoint()),
        module._CODEX_SOURCE_PUBLISH_EPOCH,
        sc.codex_accounting_consumed_provenance(),
        # `repr` of an `itertools.count` shows its next value without
        # consuming one (review F9: never draw from the allocator to read it).
        repr(sc._CODEX_ACCOUNTING_PROVENANCE_TOKENS),
        repr(getattr(module, "_CODEX_VISIBLE_ACCOUNT_VERSIONS", None)),
        repr(None if slot is None else slot.get("state")),
        repr({key: value for key, value in carrier.items() if key != "entries"}),
    )


def _install_scenario(monkeypatch, module, scenario):
    if not scenario:
        return
    observations = tuple(
        observation
        for spec in scenario["observations"]
        for observation in _observations(
            scenario["root"], spec["account"],
            weekly_reset=NOW + dt.timedelta(days=spec["days"]),
            used_weekly=spec["weekly"], used_5h=spec["five_hour"]))
    monkeypatch.setattr(
        module, "load_codex_quota_observations", lambda **_k: observations)


def _copy_store(source: Path, target: Path) -> None:
    """A consistent copy of every committed byte the build reads."""
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in (".db", ".sqlite", ".sqlite3"):
            reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            writer = sqlite3.connect(destination)
            try:
                reader.backup(writer)
            finally:
                writer.close()
                reader.close()
        else:
            shutil.copy2(path, destination)


_CHILD = (
    "import json, sys\n"
    "spec = json.loads(sys.argv[1])\n"
    "sys.path[:0] = spec['paths']\n"
    "import test_872_codex_derived_coherence as t\n"
    "print(t._COLD_MARK + json.dumps(t._cold_child(spec), sort_keys=True))\n"
)


def _cold(env, version, *, now=NOW, range_start=START, codex_budget=None):
    """A complete cold build of the same committed inputs, in another process."""
    before = _warm_fingerprint(env.module)
    target = env.tmp / f"cold-{next(env.counter)}"
    _copy_store(env.data_dir, target)
    spec = {
        "paths": [str(TESTS), str(BIN), str(TESTS.parent)],
        "data": str(target),
        "codex_home": str(env.codex_home),
        "version": version,
        "now": now.isoformat(),
        "range_start": range_start.isoformat(),
        "codex_budget": codex_budget,
        "scenario": env.scenario,
    }
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, json.dumps(spec)],
        capture_output=True, text=True, timeout=90, cwd=str(TESTS))
    assert done.returncode == 0, done.stderr[-4000:]
    lines = [line for line in done.stdout.splitlines()
             if line.startswith(_COLD_MARK)]
    assert lines, done.stdout[-2000:]
    assert _warm_fingerprint(env.module) == before, (
        "taking a cold reference disturbed the warm process")
    return json.loads(lines[-1][len(_COLD_MARK):])


def _cold_child(spec):
    from _pytest.monkeypatch import MonkeyPatch

    import conftest

    patch = MonkeyPatch()
    try:
        ns = conftest.load_script()
        conftest.redirect_paths(ns, patch, Path(spec["data"]))
        patch.setenv("CODEX_HOME", spec["codex_home"])
        module = sys.modules["_cctally_dashboard_sources"]
        module.reset_codex_source_caches()
        _install_scenario(patch, module, spec["scenario"])
        cache = ns["open_cache_db"]()
        stats = ns["open_db"]()
        try:
            state = module.build_codex_source_state(
                DashboardReadContext(
                    cache_conn=cache, stats_conn=stats,
                    range_start=dt.datetime.fromisoformat(spec["range_start"]),
                    now_utc=dt.datetime.fromisoformat(spec["now"]),
                    display_tz_name="UTC",
                    codex_budget=spec["codex_budget"]),
                data_version=spec["version"])
            return _published(state)
        finally:
            cache.close()
            stats.close()
    finally:
        patch.undo()


def _resync(env):
    env.ns["sync_codex_cache"](env.cache)
    conversations = env.ns["open_conversations_db"]()
    try:
        env.ns["sync_codex_conversations"](conversations)
    finally:
        conversations.close()


def _write_rollout(env, name, *, cwd, day, thread):
    text = ((trm.CORPUS / "modern-full.jsonl").read_text()
            .replace(RED, cwd)
            .replace("root-thread-a", thread)
            .replace("2026-07-14", day))
    path = env.codex_home / "sessions" / "2026" / "07" / day[-2:] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _resync(env)


def _decorate(env, keys):
    _split_corpus_accounts(env.cache, keys)
    _seed_codex_accounts(env.stats, [
        dict(account_key=key, email=f"{key[0]}@x.com", label=f"acct-{key[0]}",
             plan_type="pro")
        for key in keys])
    env.stats.commit()
    env.scenario = {
        "root": _cache_root_key(env.cache),
        "observations": [
            {"account": key, "days": 2 + index, "weekly": 10.0 * (index + 1),
             "five_hour": 5.0 * (index + 1)}
            for index, key in enumerate(keys)],
    }
    _install_scenario(env.monkeypatch, env.module, env.scenario)


def _bump_tokens(env, account_key, delta=1000):
    """An ID-stable cost update (same id, timestamp, account, membership).

    The NEWEST row is taken, so the update lands inside the account's
    current cycle window, which is what the card totals and the per-account
    periods aggregate. `account_key=None` takes it whatever its account."""
    row_id = env.cache.execute(
        "SELECT id FROM codex_session_entries "
        "WHERE ? IS NULL OR account_key=? "
        "ORDER BY timestamp_utc DESC, id DESC LIMIT 1",
        (account_key, account_key)).fetchone()[0]
    env.cache.execute(
        "UPDATE codex_session_entries SET input_tokens=input_tokens+?, "
        "total_tokens=total_tokens+? WHERE id=?", (delta, delta, row_id))
    env.cache.commit()
    return row_id


def _clone_row(env, account_key, index):
    """A new qualified row for `account_key` (reuses the corpus thread join)."""
    template = env.cache.execute(
        "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    cursor = env.cache.execute(
        "INSERT INTO codex_session_entries (source_path, line_offset, "
        "timestamp_utc, session_id, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, total_tokens, "
        "source_root_key, conversation_key, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"/cached/872-{account_key[0]}-{index}.jsonl", 872_000 + index,
         (NOW - dt.timedelta(hours=index + 2)).isoformat(),
         f"872-session-{index}", *template, account_key))
    env.cache.commit()
    return int(cursor.lastrowid)


def _spy(env, name):
    calls = []
    real = getattr(env.module, name)

    def spy(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    env.monkeypatch.setattr(env.module, name, spy)
    return calls


def _fail_once(env, name, exc):
    real = getattr(env.module, name)
    armed = {"left": 1}

    def failing(*args, **kwargs):
        if armed["left"]:
            armed["left"] -= 1
            raise exc
        return real(*args, **kwargs)

    env.monkeypatch.setattr(env.module, name, failing)


def _transient_metadata(env):
    env.monkeypatch.setattr(
        env.module, "_codex_conversation_metadata",
        lambda *a, **k: env.module._CodexConversationMetadataRead(
            {}, {}, sqlite3.OperationalError("872 transient")))


def _assert_account_keys(env, expected):
    """The record names a coherent population and every account it holds."""
    record = _record(env)
    assert record is not None and record.name is not None, record
    assert record.account_keys == frozenset(expected), record.account_keys


def _assert_incoherent(env, expected):
    """A build that did not advance every consumer records NO name, but keeps
    every account it has ever held, so the next build's G1 discards them all."""
    record = _record(env)
    assert record is not None and record.name is None, record
    assert record.account_keys == frozenset(expected), record.account_keys


def _assert_cold(env, state, version, **ctx):
    assert _published(state) == _cold(env, version, **ctx)


def test_the_cold_oracle_matches_a_fresh_warm_build_and_rejects_a_changed_store(env):
    """The oracle agrees with an untouched build and sees a committed change."""
    first = _build(env, "o1")
    assert _published(first) == _cold(env, "o1")
    _write_rollout(env, "rollout-b.jsonl", cwd=BLUE, day="2026-07-15",
                   thread="872-oracle")
    assert _published(first) != _cold(env, "o1"), (
        "non-vacuity: the oracle must see a committed store change")


def test_a1_a_foreign_label_state_with_a_pair_preserving_delta_equals_cold(env):
    """P1(b) through the public build: a label state that does not reflect
    the carrier's base, whose pair multiset a pair-preserving delta keeps
    equal, must not reach the reuse branch."""
    _write_rollout(env, "rollout-b.jsonl", cwd=BLUE, day="2026-07-15",
                   thread="872-a1")
    _build(env, "seed")
    rows = tuple(sorted(
        env.module._CODEX_VISIBLE_POPULATION_CACHE["rows_by_id"].values(),
        key=env.module._codex_population_order_key))
    red = tuple(r for r in rows if str(r.project_label) == "project-red")
    blue = tuple(r for r in rows if str(r.project_label) == "project-blue")
    assert red and blue, "precondition: two projects"
    env.module.reset_codex_source_caches()
    label = env.module._cached_project_labeled_entries
    label(red, ("parent",), population_signature=("872-foreign-0",))
    label(red, ("parent",), population_signature=("872-foreign-1",),
          changed_new=blue)
    state = env.module._CODEX_PROJECT_LABEL_CACHE[("parent",)]
    assert {pair[1] for pair in state["pairs"]} == {"project-red", "project-blue"}
    assert set(state["labels"]) == {str(r.project_key) for r in red}, (
        "precondition: the seeded state claims blue's pair without its label")
    _assert_cold(env, _build(env, "a1"), "a1")


def test_a2_the_refresh_path_survives_foreign_retained_populations(
        private_corpus, monkeypatch):
    """P4: tiny -> carrier-only reset -> small -> carrier-only reset -> tiny,
    through the real `_make_run_sync_now_locked` refresh. Cold references are
    taken BEFORE the chain, so they cannot repair it."""
    import test_tick_stats_integration as tsi

    bbf = tsi._load_build_bench()
    results: list[tuple[str, object]] = []

    def before(_cctally, tui):
        # Wrap the TUI's FOLD, not `build_codex_source_state`: replacing the
        # latter flips `codex_split_seams_unpatched` and sends the TUI down
        # its unsplit path, which is not the one production takes.
        real = getattr(tui.build_codex_source_state_from_capture,
                       "__real_872__",
                       tui.build_codex_source_state_from_capture)

        def recording(*args, **kwargs):
            try:
                state = real(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                results.append(("error", repr(exc)))
                raise
            results.append(("ok", _published(state, version=False)))
            return state

        recording.__real_872__ = real
        monkeypatch.setattr(
            tui, "build_codex_source_state_from_capture", recording)
        globals_ = real.__globals__
        state["sources"] = globals_
        state["carrier"] = globals_["_lib_snapshot_cache"]

    state: dict[str, object] = {}
    corpora = {"tiny": private_corpus("tiny"), "small": private_corpus("small")}

    def refresh(name):
        results.clear()
        tsi._run_refresh(corpora[name], bbf, skip_sync=True, before=before)
        assert results and results[-1][0] == "ok", results
        return results[-1][1]

    cold = {}
    for name in ("tiny", "small"):
        if state:
            state["sources"]["reset_codex_source_caches"]()
        cold[name] = refresh(name)
    state["sources"]["reset_codex_source_caches"]()
    for name in ("tiny", "small", "tiny"):
        state["carrier"].reset_codex_accounting_cache_state()
        assert refresh(name) == cold[name], name


def test_a3_a_transient_metadata_generation_then_new_rows_equals_cold(env):
    """P5: the transient generation skips both project legs while the carrier
    advances. The next build and its retry must equal cold."""
    _build(env, "g0")
    _write_rollout(env, "rollout-b.jsonl", cwd=BLUE, day="2026-07-15",
                   thread="872-a3-b")
    original = env.module._codex_conversation_metadata
    _transient_metadata(env)
    g1 = _build(env, "g1")
    assert g1.data["projects"]["rows"] == (), "precondition: transient skip"
    env.monkeypatch.setattr(env.module, "_codex_conversation_metadata", original)
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a3-a2")
    _assert_cold(env, _build(env, "g2"), "g2")
    _assert_cold(env, _build(env, "g2"), "g2")


def test_a4_a_widened_carrier_window_move_relabels_like_cold(env):
    """P6b: a monthly budget widens the carrier start to 07-01; moving the
    visible start past one of two same-basename projects must relabel."""
    budget = {"period": "monthly"}
    _write_rollout(env, "rollout-b.jsonl", cwd=RED_B, day="2026-07-16",
                   thread="872-a4")
    first = _build(env, "r1", codex_budget=budget)
    labels = sorted(r["label"] for r in first.data["projects"]["rows"])
    assert labels == ["project-red (1)", "project-red (2)"], labels
    later = START.replace(day=15)
    _assert_cold(env, _build(env, "r2", codex_budget=budget, range_start=later),
                 "r2", codex_budget=budget, range_start=later)


def test_a5_a_carrier_only_reset_then_a_revealed_row_equals_cold(env):
    """P7: the rebuilt carrier re-mints the old signature; the wire must not
    splice a from-empty delta into retained groups."""
    _write_rollout(env, "rollout-b.jsonl", cwd=BLUE, day="2026-07-21",
                   thread="872-a5")
    _build(env, "g1")
    sc.reset_codex_accounting_cache_state()
    later = NOW + dt.timedelta(days=2)
    _assert_cold(env, _build(env, "g2", now=later), "g2", now=later)


def test_a6_the_overflow_clear_on_an_unchanged_decorated_store_equals_cold(env):
    """P8 + F2: the production overflow clear empties the carrier while every
    derived cache survives; parent and both children must equal cold."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    _build(env, "g1")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    sc._clear_snapshot_accelerators()
    state = _build(env, "g2")
    assert {"accounts", "account_scopes"} <= set(state.data), "decorated"
    _assert_cold(env, state, "g2")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})


def test_a7_shrink_expand_retry_of_the_upper_bound_equals_cold(env):
    """P9 + F1, for parent and account output."""
    _write_rollout(env, "rollout-b.jsonl", cwd=RED_B, day="2026-07-16",
                   thread="872-a7")
    _decorate(env, (_ACCT_A, _ACCT_B))
    early = dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
    _assert_cold(env, _build(env, "g1"), "g1")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _assert_cold(env, _build(env, "g2", now=early), "g2", now=early)
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _assert_cold(env, _build(env, "g3"), "g3")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _assert_cold(env, _build(env, "g3"), "g3")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})


@pytest.mark.parametrize("second_mutation", [False, True])
def test_a8_a_transient_registry_failure_then_recovery_equals_cold(
        env, second_mutation):
    """F3: g1's registry read fails, so scopes are skipped while A's spend
    lands; recovery (with or without another A mutation) must equal cold."""
    import _cctally_account

    _decorate(env, (_ACCT_A, _ACCT_B))
    _build(env, "g0")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _bump_tokens(env, _ACCT_A)
    real = _cctally_account.real_account_count

    def failing(*_a, **_k):
        raise sqlite3.OperationalError("872 registry")

    env.monkeypatch.setattr(_cctally_account, "real_account_count", failing)
    g1 = _build(env, "g1")
    assert "account_scopes" not in g1.data, "precondition: scopes skipped"
    _assert_incoherent(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    env.monkeypatch.setattr(_cctally_account, "real_account_count", real)
    if second_mutation:
        # A DIFFERENT A row than g1's: replacing g1's row again would repair
        # the missed g1 change on its own and prove nothing.
        _clone_row(env, _ACCT_A, 7)
    _assert_cold(env, _build(env, "g2"), "g2")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})


def test_a10_an_unnamed_project_wire_never_serves_a_retained_value(env):
    """No name (no ledger) means no hit, even when the pair set is equal."""
    _build(env, "seed")
    rows = tuple(sorted(
        env.module._CODEX_VISIBLE_POPULATION_CACHE["rows_by_id"].values(),
        key=env.module._codex_population_order_key))
    richer = tuple(dataclasses.replace(
        row, input_tokens=row.input_tokens + 5000,
        total_tokens=row.total_tokens + 5000) for row in rows)
    env.module.reset_codex_source_caches()
    kwargs = dict(changed_old=(), changed_new=(), accounting_end=NOW,
                  cache_key=("parent",), semantic_signature="872",
                  population_signature=None)
    context = _context(env)
    first = env.module._cached_projects_wire(context, (), rows, **kwargs)
    second = env.module._cached_projects_wire(context, (), richer, **kwargs)
    assert second["total_tokens"] > first["total_tokens"], (
        "a retained value was served for a population the key cannot name")


_BASE_CASES = (
    "cold-from-empty", "extra-change", "start-change", "end-regression",
    "sequence-regression", "dirty-overflow", "overflow-clear",
    "warm-clean-same-head", "warm-clean-reminted", "warm-dirty",
    "restore-then-warm",
)


@pytest.mark.parametrize("case", _BASE_CASES)
def test_a11_the_carrier_names_each_deltas_base(env, case):
    """Contract: warm results name the prior state; cold results name none."""
    from test_population_signature import _accounting

    cache = env.cache
    sc.reset_codex_accounting_cache_state()
    end = NOW + dt.timedelta(microseconds=1)
    if case == "cold-from-empty":
        result = _accounting(cache, range_end=end)
        assert result.cold
        assert (result.base_population_signature,
                result.base_provenance_token) == (None, None)
        return
    prior = _accounting(cache, range_end=end)
    if case == "restore-then-warm":
        checkpoint = sc.checkpoint_codex_accounting_cache_state()
        _bump_tokens(env, None)
        _accounting(cache, range_end=end)
        sc.restore_codex_accounting_cache_state(checkpoint)
    expect_cold = case in {
        "extra-change", "start-change", "end-regression",
        "sequence-regression", "dirty-overflow", "overflow-clear"}
    kwargs = {"range_end": end}
    if case == "extra-change":
        kwargs["extra"] = ("speed", ("872-other-root",))
    elif case == "start-change":
        kwargs["range_start"] = START + dt.timedelta(days=1)
    elif case == "end-regression":
        kwargs["range_end"] = end - dt.timedelta(hours=1)
    elif case == "sequence-regression":
        cache.execute("UPDATE cache_meta SET value='0' "
                      "WHERE key='codex_accounting_mutation_seq'")
        cache.commit()
    elif case == "dirty-overflow":
        from test_snapshot_bounded_work import _widen_corpus
        _widen_corpus(cache, files=sc._CODEX_ACCOUNTING_MAX_DIRTY_PATHS + 1,
                      per_file=1, prefix="872-overflow", offset_base=900_000)
    elif case == "overflow-clear":
        sc._clear_snapshot_accelerators()
    elif case == "warm-clean-reminted":
        head = int(cache.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'").fetchone()[0])
        cache.execute(
            "INSERT INTO codex_accounting_change_log "
            "(mutation_seq, change_kind, source_root_key, source_path) "
            "VALUES (?, 'path', NULL, NULL)", (head + 1,))
        cache.execute("UPDATE cache_meta SET value=? "
                      "WHERE key='codex_accounting_mutation_seq'",
                      (str(head + 1),))
        cache.commit()
    elif case in ("warm-dirty", "restore-then-warm"):
        _bump_tokens(env, None, 7)
    result = _accounting(cache, **kwargs)
    assert result.cold is expect_cold, case
    if expect_cold:
        assert (result.base_population_signature,
                result.base_provenance_token) == (None, None), case
        return
    assert result.base_population_signature == prior.population_signature, case
    assert result.base_provenance_token == prior.provenance_token, case
    if case == "warm-clean-same-head":
        assert result.provenance_token == prior.provenance_token
        assert not result.changed_old and not result.changed_new
    if case == "warm-clean-reminted":
        assert result.provenance_token != prior.provenance_token
        assert not result.dirty_paths
    if case in ("warm-dirty", "restore-then-warm"):
        assert result.changed_old and result.changed_new


def test_a12_an_ordinary_warm_tick_never_discards_and_a_transient_one_discards_once(env):
    """I5: ordinary warm-dirty ticks keep the layer and update labels by delta;
    the build after a transient generation discards exactly once."""
    _build(env, "g0")
    first = _record(env)
    resets = _spy(env, "reset_codex_account_scope_cache")
    recounts = _spy(env, "_label_pair_counts")
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a12")
    _build(env, "g1")
    assert resets == [] and recounts == [], (resets, recounts)
    assert first is not None and _record(env) is not None
    assert _record(env).name is not None and _record(env).name != first.name
    original = env.module._codex_conversation_metadata
    _transient_metadata(env)
    _build(env, "g2")
    env.monkeypatch.setattr(env.module, "_codex_conversation_metadata", original)
    assert _record(env).name is None
    _build(env, "g3")
    assert resets == ["reset_codex_account_scope_cache"], resets
    _build(env, "g4")
    assert resets == ["reset_codex_account_scope_cache"], resets


def test_a16_a_vanished_account_among_three_rebuilds_on_return(env):
    """F4: three real accounts keep the registry decorating after A leaves,
    so only G2 condition 3 (a recorded key that is no longer live) can make
    the omission build incoherent."""
    _decorate(env, (_ACCT_A, _ACCT_B, _ACCT_C))
    _build(env, "g0")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _ACCT_C, _UNATTRIBUTED})
    env.cache.execute(
        "DELETE FROM codex_session_entries WHERE account_key=?", (_ACCT_A,))
    env.cache.commit()
    env.stats.execute("DELETE FROM accounts WHERE account_key=?", (_ACCT_A,))
    env.stats.commit()
    env.scenario["observations"] = [
        spec for spec in env.scenario["observations"]
        if spec["account"] != _ACCT_A]
    _install_scenario(env.monkeypatch, env.module, env.scenario)
    omitted = _build(env, "g1")
    assert "accounts" in omitted.data, "decoration must stay on"
    assert _ACCT_A not in omitted.data["account_scopes"]
    assert omitted.combined_accounting.get("status") != "unresolved", (
        "the degrade branch must not be what fires")
    assert omitted.metadata_health is None or omitted.metadata_health.get(
        "state") != "transient_read_failure"
    _assert_incoherent(env, {_ACCT_A, _ACCT_B, _ACCT_C, _UNATTRIBUTED})
    _clone_row(env, _ACCT_A, 1)
    _seed_codex_accounts(env.stats, [dict(
        account_key=_ACCT_A, email="a@x.com", label="acct-a",
        plan_type="pro")])
    env.stats.commit()
    env.scenario["observations"].insert(0, {
        "account": _ACCT_A, "days": 2, "weekly": 10.0, "five_hour": 5.0})
    _install_scenario(env.monkeypatch, env.module, env.scenario)
    _assert_cold(env, _build(env, "g2"), "g2")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _ACCT_C, _UNATTRIBUTED})


def test_a17_a_degraded_scope_build_after_a_partial_advance_equals_cold(env):
    """F4: B's child raises after A's scope (or B's period caches) advanced."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    _build(env, "g0")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _bump_tokens(env, _ACCT_A)
    _bump_tokens(env, _ACCT_B)
    real = env.module._codex_cache_report_wire

    def failing(*args, **kwargs):
        if kwargs.get("cache_key") == ("account", _ACCT_B):
            raise sqlite3.OperationalError("872 degrade")
        return real(*args, **kwargs)

    env.monkeypatch.setattr(env.module, "_codex_cache_report_wire", failing)
    degraded = _build(env, "g1")
    assert not degraded.data.get("account_scopes"), "precondition: degraded"
    assert (degraded.combined_accounting or {}).get("status") == "unresolved"
    _assert_incoherent(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    env.monkeypatch.setattr(env.module, "_codex_cache_report_wire", real)
    _assert_cold(env, _build(env, "g2"), "g2")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})


def test_a18_rollback_after_a_g1_discard_restores_and_the_retry_equals_cold(env):
    """F4/F8: a build that discards under G1 and then fails restores the
    owners and the record together; its retry equals cold."""
    _build(env, "g0")
    sc._clear_snapshot_accelerators()
    before = _owner_contents(env.module)
    if hasattr(env.module, "_CODEX_DERIVED_COHERENCE"):
        assert before["derived_coherence"], "precondition: a record exists"
    _fail_once(env, "_alerts_wire", RuntimeError("872 after projects"))
    with pytest.raises(RuntimeError, match="872 after projects"):
        _build(env, "g1")
    assert _owner_contents(env.module) == before, (
        "the failed build did not restore the owners and the record together")
    _assert_cold(env, _build(env, "g1"), "g1")


def test_a20_a_rebuilt_visible_fold_cannot_reissue_a_card_total_version(env):
    """F7: evict only the visible fold, then an ID-stable cost update."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    _build(env, "g0")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    env.module._CODEX_VISIBLE_POPULATION_CACHE.clear()
    _bump_tokens(env, _ACCT_A)
    _assert_cold(env, _build(env, "g1"), "g1")
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})


def test_a9_a_failure_after_project_writes_restores_and_the_retry_equals_cold(env):
    """Control: no G1 discard here; the record restores with the caches."""
    _build(env, "g0")
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a9")
    before = _owner_contents(env.module)
    _fail_once(env, "_alerts_wire", RuntimeError("872 a9"))
    with pytest.raises(RuntimeError, match="872 a9"):
        _build(env, "g1")
    assert _owner_contents(env.module) == before
    _assert_cold(env, _build(env, "g1"), "g1")


@pytest.mark.parametrize("evict", ["labels", "wire"])
def test_a13_one_project_cache_evicted_alone_still_equals_cold(env, evict):
    _build(env, "g0")
    target = (env.module._CODEX_PROJECT_LABEL_CACHE if evict == "labels"
              else env.module._CODEX_PROJECT_WIRE_CACHE)
    target.pop(("parent",), None)
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a13")
    _assert_cold(env, _build(env, "g1"), "g1")


@pytest.mark.parametrize("order", ["live-first", "share-first"])
def test_a19_interleaved_captures_and_folds_equal_cold(env, order):
    """Sequence test (RED or control, recorded at TDD): a live capture and a
    share capture with another start, folded in either order, then a failed
    fold and its retry."""
    import contextlib

    share_start = START.replace(day=10)
    module = env.module
    _build(env, "g0")
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a19")
    with contextlib.ExitStack() as stack:
        # Both path scopes stay open until their folds return, exactly as
        # `build_codex_source_state` holds its own scope across capture+fold.
        live_scope = stack.enter_context(module.codex_path_scope())
        live = module.capture_codex_source_state(
            _context(env, range_start=START), path_scope=live_scope)
        share_scope = stack.enter_context(module.codex_path_scope())
        share = module.capture_codex_source_state(
            _context(env, range_start=share_start), path_scope=share_scope)
        folds = [("live", live, live_scope, START),
                 ("share", share, share_scope, share_start)]
        if order == "share-first":
            folds.reverse()
        published = []
        for name, captured, scope, start in folds:
            state = module.build_codex_source_state_from_capture(
                captured, data_version=name, path_scope=scope)
            published.append((name, start, _published(state)))
    for name, start, value in published:
        assert value == _cold(env, name, range_start=start), name
    _fail_once(env, "_alerts_wire", RuntimeError("872 a19"))
    with pytest.raises(RuntimeError, match="872 a19"):
        _build(env, "retry")
    _assert_cold(env, _build(env, "retry"), "retry")


def test_a21_an_evicted_record_discards_exactly_once(env):
    _build(env, "g0")
    env.module._CODEX_DERIVED_COHERENCE.clear()
    resets = _spy(env, "reset_codex_account_scope_cache")
    _write_rollout(env, "rollout-a2.jsonl", cwd=RED, day="2026-07-16",
                   thread="872-a21")
    _assert_cold(env, _build(env, "g1"), "g1")
    assert resets == ["reset_codex_account_scope_cache"]


def test_a22_a_reminted_warm_clean_capture_never_discards(env):
    _build(env, "g0")
    first = _record(env).name
    head = int(env.cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'").fetchone()[0])
    env.cache.execute(
        "INSERT INTO codex_accounting_change_log "
        "(mutation_seq, change_kind, source_root_key, source_path) "
        "VALUES (?, 'path', NULL, NULL)", (head + 1,))
    env.cache.execute("UPDATE cache_meta SET value=? "
                      "WHERE key='codex_accounting_mutation_seq'",
                      (str(head + 1),))
    env.cache.commit()
    resets = _spy(env, "reset_codex_account_scope_cache")
    state = _build(env, "g1")
    assert resets == []
    assert _record(env).name is not None and _record(env).name != first
    _assert_cold(env, state, "g1")


def test_a23_a_foreign_label_state_without_a_delta_equals_cold(env):
    """P1(a) through the public build: the no-delta variant."""
    _write_rollout(env, "rollout-b.jsonl", cwd=BLUE, day="2026-07-15",
                   thread="872-a23")
    _build(env, "seed")
    rows = tuple(sorted(
        env.module._CODEX_VISIBLE_POPULATION_CACHE["rows_by_id"].values(),
        key=env.module._codex_population_order_key))
    red = tuple(r for r in rows if str(r.project_label) == "project-red")
    env.module.reset_codex_source_caches()
    env.module._cached_project_labeled_entries(
        red, ("parent",), population_signature=("872-foreign",))
    _assert_cold(env, _build(env, "a23"), "a23")


def test_a12b_a_decorated_warm_tick_never_discards(env):
    """I5, decorated: clean children are reused and the layer is kept."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    _build(env, "g0")
    first = _record(env).name
    resets = _spy(env, "reset_codex_account_scope_cache")
    _bump_tokens(env, _ACCT_A)
    state = _build(env, "g1")
    assert resets == [], resets
    assert _record(env).name not in (None, first)
    _assert_account_keys(env, {_ACCT_A, _ACCT_B, _UNATTRIBUTED})
    _assert_cold(env, state, "g1")


def test_a24_observation_and_block_only_accounts_stay_recorded_through_reuse(env):
    """G2 bookkeeping: every live key the scope builder returned is recorded —
    accounts with rows, an account live only through observations (C), one
    live only through a retained block (D) — and stays recorded while the
    clean ones are reused."""
    from test_codex_account_read_model import _seed_5h_block

    _decorate(env, (_ACCT_A, _ACCT_B))
    _seed_codex_accounts(env.stats, [dict(
        account_key=_ACCT_C, email="c@x.com", label="acct-c",
        plan_type="pro")])
    acct_d = "d" * 32
    _seed_5h_block(
        env.stats, root=env.scenario["root"], account_key=acct_d,
        limit_key="limit-d", start_at=NOW - dt.timedelta(hours=2),
        resets_at=NOW + dt.timedelta(hours=3))
    env.stats.commit()
    # E is live ONLY through observations: no registry row, no accounting
    # row and no retained block.
    acct_e = "e" * 32
    env.scenario["observations"].append(
        {"account": _ACCT_C, "days": 4, "weekly": 30.0, "five_hour": 15.0})
    env.scenario["observations"].append(
        {"account": acct_e, "days": 5, "weekly": 35.0, "five_hour": 17.0})
    _install_scenario(env.monkeypatch, env.module, env.scenario)
    g0 = _build(env, "g0")
    expected = {_ACCT_A, _ACCT_B, _ACCT_C, acct_d, acct_e}
    assert expected <= set(g0.data["account_scopes"]), sorted(
        g0.data["account_scopes"])
    _assert_account_keys(env, set(g0.data["account_scopes"]))
    resets = _spy(env, "reset_codex_account_scope_cache")
    _bump_tokens(env, _ACCT_A)
    g1 = _build(env, "g1")
    assert resets == [], resets
    _assert_account_keys(env, set(g0.data["account_scopes"]))
    _assert_cold(env, g1, "g1")

    # Disappearance: D's only block and E's only observations go.
    env.stats.execute(
        "DELETE FROM quota_window_blocks WHERE account_key=?", (acct_d,))
    env.stats.commit()
    observations = env.scenario["observations"]
    env.scenario["observations"] = [
        spec for spec in observations if spec["account"] != acct_e]
    _install_scenario(env.monkeypatch, env.module, env.scenario)
    g2 = _build(env, "g2")
    assert not {acct_d, acct_e} & set(g2.data["account_scopes"]), sorted(
        g2.data["account_scopes"])
    _assert_incoherent(env, set(g0.data["account_scopes"]))

    # Recovery: both come back; G1 discards once, then the record is whole.
    _seed_5h_block(
        env.stats, root=env.scenario["root"], account_key=acct_d,
        limit_key="limit-d", start_at=NOW - dt.timedelta(hours=2),
        resets_at=NOW + dt.timedelta(hours=3))
    env.stats.commit()
    env.scenario["observations"] = observations
    _install_scenario(env.monkeypatch, env.module, env.scenario)
    g3 = _build(env, "g3")
    _assert_account_keys(env, set(g0.data["account_scopes"]))
    _assert_cold(env, g3, "g3")


def test_a25_the_fold_names_its_label_population_with_token_and_visible_start(env):
    """The name the fold itself builds, not the test-facing helper's."""
    _build(env, "g0")
    name = env.module._CODEX_PROJECT_LABEL_CACHE[("parent",)][
        "population_signature"]
    assert name[-1] == env.module._CODEX_PROJECT_LABEL_ALGORITHM_VERSION
    assert any(isinstance(leg, tuple) and leg[:1] == ("provenance",)
               and isinstance(leg[1], int) for leg in name), name
    assert ("visible-start", START) in name, name
    assert name[:-1] == _record(env).name


def test_a26_a_g1_discard_keeps_the_cumulative_fallback_counters(env):
    """G1 discards with `keep_counters=True`: a production discard must not
    zero the counters `bin/cctally-snapshot-measure` reads."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    env.monkeypatch.setattr(
        env.module, "_CODEX_VISIBLE_POPULATION_MAX_BYTES", 1)
    env.monkeypatch.setattr(
        env.module, "_CODEX_ACCOUNT_CARD_TOTALS_MAX_BYTES", 1)
    _build(env, "g0")
    _build(env, "g0b")
    before = dict(env.module.codex_visible_population_cache_stats())
    assert before["fallbackCount"] >= 2, (
        "non-vacuity: every over-cap build counts a fallback", before)
    sc._clear_snapshot_accelerators()
    resets = _spy(env, "reset_codex_account_scope_cache")
    _build(env, "g1")
    assert resets, "precondition: the cold carrier made G1 discard"
    after = dict(env.module.codex_visible_population_cache_stats())
    assert after["fallbackCount"] > before["fallbackCount"], (before, after)


def test_a26_b_g1_discard_keeps_the_card_totals_fallback_counter(env):
    """The card-totals counter needs its own case: a card signature exists
    only while the visible population is admitted, so A26's over-cap
    population leaves this counter at zero. Here only the card cache's cap
    is exceeded, so every build counts card fallbacks."""
    _decorate(env, (_ACCT_A, _ACCT_B))
    env.monkeypatch.setattr(
        env.module, "_CODEX_ACCOUNT_CARD_TOTALS_MAX_BYTES", 1)
    _build(env, "g0")
    _build(env, "g0b")
    before = dict(env.module.codex_visible_population_cache_stats())
    assert before["fallbackCount"] == 0, (
        "precondition: the visible population is admitted", before)
    assert before["accountCardFallbackCount"] >= 2, (
        "non-vacuity: every card over the cap counts a fallback", before)
    sc._clear_snapshot_accelerators()
    resets = _spy(env, "reset_codex_account_scope_cache")
    _build(env, "g1")
    assert resets, "precondition: the cold carrier made G1 discard"
    after = dict(env.module.codex_visible_population_cache_stats())
    assert after["accountCardFallbackCount"] > (
        before["accountCardFallbackCount"]), (before, after)

