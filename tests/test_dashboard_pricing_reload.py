"""#714 — in-process pricing reload.

Only a long-lived process can hold stale pricing; every CLI invocation
re-imports. The target is the dashboard, which keeps serving after an
installer replaces `bin/_lib_pricing.py` underneath it.

The dangerous outcome this module exists to prevent is a PARTIAL swap.
`_cctally_cache.PRICING_SNAPSHOT_DATE` is the value that gates the
materialized-cost write refusal, and swapping only that would clear the
refusal and resume writing cost computed from the OLD tables — worse than
refusing, because a refusal costs a stale rollup and this costs a wrong one.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
from http.client import HTTPConnection

import pytest

from tests.conftest import load_script, redirect_paths


@pytest.fixture
def pricing():
    ns = load_script()
    return sys.modules["_lib_pricing"]


@pytest.fixture(autouse=True)
def _restore_the_live_pricing_snapshot():
    """The published snapshot is PROCESS state, shared with every other test in
    this xdist worker.

    A case that adopts a synthetic revision and does not put the real one back
    leaves `_lib_pricing`'s tables holding one fake model, and the failure is
    then reported against whichever pricing test happens to run next —
    `test_pricing_check.py` reporting `claude-fable-5 is unpriced` is what this
    module actually caused, in an order-dependent way that passed when this
    file ran last. Per-test `finally` blocks are not enough, because the watch
    cases adopt through `poll_once` rather than through an explicit call.
    """
    load_script()
    pricing_mod = sys.modules["_lib_pricing"]
    before = pricing_mod.current_pricing_snapshot()
    try:
        yield
    finally:
        pricing_mod.adopt_pricing_candidate(before, force=True)


def _snapshot_source(*, date="2099-01-01", claude_rate=0.000123,
                     codex_rate=0.000456, drop=None):
    """A minimal but COMPLETE `_lib_pricing.py` candidate.

    Only the assignments the reader consumes are present, because the reader
    must take exactly those and must not depend on the rest of the module
    being importable — the file it reads is one an installer just wrote and
    this process never executes it.
    """
    parts = {
        "PRICING_SNAPSHOT_DATE": f'PRICING_SNAPSHOT_DATE = "{date}"',
        "TIERED_THRESHOLD": "TIERED_THRESHOLD = 200_000",
        "CODEX_TIERED_THRESHOLD": "CODEX_TIERED_THRESHOLD = 272_000",
        "CLAUDE_MODEL_PRICING": (
            "CLAUDE_MODEL_PRICING = {\n"
            '    "claude-opus-4-8": {\n'
            f'        "input_cost_per_token": {claude_rate},\n'
            f'        "output_cost_per_token": {claude_rate * 5},\n'
            f'        "cache_creation_input_token_cost": {claude_rate * 1.25},\n'
            f'        "cache_read_input_token_cost": {claude_rate * 0.1},\n'
            "    },\n"
            "}"
        ),
        "CLAUDE_FAST_MULTIPLIER_OVERRIDES": (
            'CLAUDE_FAST_MULTIPLIER_OVERRIDES = {"claude-opus-4-8": 1.0}'),
        "CODEX_MODEL_PRICING": (
            "CODEX_MODEL_PRICING = {\n"
            '    "gpt-5": {\n'
            f'        "input_cost_per_token": {codex_rate},\n'
            f'        "output_cost_per_token": {codex_rate * 5},\n'
            f'        "cache_read_input_token_cost": {codex_rate * 0.1},\n'
            "    },\n"
            "}"
        ),
        "CODEX_MODEL_ALIASES": 'CODEX_MODEL_ALIASES = {"gpt-5-latest": "gpt-5"}',
        "CODEX_LEGACY_FALLBACK_MODEL": 'CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"',
        "CACHE_WRITE_1H_MULTIPLIER": "CACHE_WRITE_1H_MULTIPLIER = 2.0",
        "CODEX_FAST_MULTIPLIER_OVERRIDES": (
            'CODEX_FAST_MULTIPLIER_OVERRIDES = {"gpt-5": 2.0}'),
        "CODEX_FAST_MULTIPLIER_FALLBACK": "CODEX_FAST_MULTIPLIER_FALLBACK = 2.0",
    }
    if drop:
        parts.pop(drop)
    return "\n".join(parts.values()) + "\n"


def _cache_meta(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _write_candidate(tmp_path, source, name="_lib_pricing.py"):
    path = tmp_path / name
    path.write_text(source)
    return path


# --- Task 4.1: the snapshot and the literal reader -------------------------


def test_a_complete_candidate_is_read_into_a_snapshot(pricing, tmp_path):
    path = _write_candidate(tmp_path, _snapshot_source())
    snap = pricing.read_pricing_snapshot_from_source(path)
    assert snap.snapshot_date == "2099-01-01"
    assert snap.claude_pricing["claude-opus-4-8"][
        "input_cost_per_token"] == 0.000123
    assert snap.codex_pricing["gpt-5"]["input_cost_per_token"] == 0.000456
    assert snap.aliases == {"gpt-5-latest": "gpt-5"}
    assert snap.tier_thresholds == {"claude": 200_000, "codex": 272_000}
    assert snap.fallback_model == "gpt-5"
    assert snap.cache_write_1h_multiplier == 2.0
    assert snap.fast_multipliers["claude"] == {"claude-opus-4-8": 1.0}
    assert snap.fast_multipliers["codex"] == {"gpt-5": 2.0}
    assert snap.fast_multipliers["codex_fallback"] == 2.0


@pytest.mark.parametrize("missing", [
    "PRICING_SNAPSHOT_DATE",
    "CLAUDE_MODEL_PRICING",
    "CODEX_MODEL_PRICING",
    "CODEX_MODEL_ALIASES",
    "TIERED_THRESHOLD",
    "CODEX_TIERED_THRESHOLD",
    "CODEX_LEGACY_FALLBACK_MODEL",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CLAUDE_FAST_MULTIPLIER_OVERRIDES",
    "CODEX_FAST_MULTIPLIER_OVERRIDES",
    "CODEX_FAST_MULTIPLIER_FALLBACK",
])
def test_a_candidate_missing_any_field_is_rejected(pricing, tmp_path, missing):
    """A PARTIAL candidate is the whole hazard: adopting one clears the write
    refusal and then prices from whatever the missing half left behind."""
    path = _write_candidate(tmp_path, _snapshot_source(drop=missing))
    with pytest.raises(pricing.PricingCandidateRejected) as excinfo:
        pricing.read_pricing_snapshot_from_source(path)
    assert missing in str(excinfo.value)


def test_a_non_literal_assignment_is_rejected_loudly(pricing, tmp_path):
    """The rot guard. A future refactor of a pricing table into a
    comprehension or a call would silently disable the reload if the reader
    skipped what it could not evaluate."""
    source = _snapshot_source().replace(
        'CODEX_MODEL_ALIASES = {"gpt-5-latest": "gpt-5"}',
        "CODEX_MODEL_ALIASES = dict(zip(['gpt-5-latest'], ['gpt-5']))")
    path = _write_candidate(tmp_path, source)
    with pytest.raises(pricing.PricingCandidateRejected) as excinfo:
        pricing.read_pricing_snapshot_from_source(path)
    assert "CODEX_MODEL_ALIASES" in str(excinfo.value)


def test_the_shipped_pricing_module_is_still_literal_evaluable(pricing):
    """The same rot guard, against the file that actually ships. This is the
    case that fails when someone rewrites a table as a comprehension."""
    snap = pricing.read_pricing_snapshot_from_source(
        pathlib.Path(pricing.__file__))
    assert snap.snapshot_date == pricing.PRICING_SNAPSHOT_DATE
    assert snap.claude_pricing == pricing.CLAUDE_MODEL_PRICING
    assert snap.codex_pricing == pricing.CODEX_MODEL_PRICING


@pytest.mark.parametrize("date,why", [
    ("", "absent"),
    ("not-a-date", "unparseable"),
])
def test_a_candidate_whose_date_is_unusable_is_rejected(pricing, tmp_path,
                                                        date, why):
    path = _write_candidate(tmp_path, _snapshot_source(date=date))
    with pytest.raises(pricing.PricingCandidateRejected):
        pricing.read_pricing_snapshot_from_source(path)


def test_a_downgrade_candidate_is_refused_and_the_live_snapshot_retained(
        pricing, tmp_path):
    """`_lib_pricing` documents that a pricing revision always ADVANCES the
    snapshot date, so a candidate that does not is either a rollback or a
    corrupt read."""
    live = pricing.current_pricing_snapshot()
    older = (dt.date.fromisoformat(live.snapshot_date)
             - dt.timedelta(days=1)).isoformat()
    for date in (older, live.snapshot_date):
        path = _write_candidate(tmp_path, _snapshot_source(date=date))
        candidate = pricing.read_pricing_snapshot_from_source(path)
        assert pricing.adopt_pricing_candidate(candidate) is False
        assert pricing.current_pricing_snapshot() is live


def test_a_file_whose_signature_changes_mid_parse_is_rejected(pricing,
                                                              tmp_path):
    """An installer replacing the file while it is being read yields a
    candidate assembled from two different files."""
    path = _write_candidate(tmp_path, _snapshot_source())
    real_read = pathlib.Path.read_bytes

    def rewriting(self):
        data = real_read(self)
        if self == path:
            path.write_text(_snapshot_source(date="2099-02-02",
                                             claude_rate=0.9))
        return data

    original = pathlib.Path.read_bytes
    pathlib.Path.read_bytes = rewriting
    try:
        with pytest.raises(pricing.PricingCandidateRejected) as excinfo:
            pricing.read_pricing_snapshot_from_source(path)
    finally:
        pathlib.Path.read_bytes = original
    assert "signature" in str(excinfo.value).lower()


# --- Task 4.2: every consumer reads the snapshot ---------------------------


def test_the_inventory_is_complete_no_module_keeps_an_unreplaceable_copy(
        pricing):
    """The trap, asserted rather than claimed.

    `bin/cctally` copies the tables at import and both dashboard source
    modules read those copies, so a swap that misses one prices from a stale
    table while the refusal latch is already cleared. This walks EVERY loaded
    module and fails on any attribute still bound to an object from the
    superseded snapshot.
    """
    load_script()
    before = pricing.current_pricing_snapshot()
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-03-03",
        claude_pricing={"claude-opus-4-8": {"input_cost_per_token": 1.0,
                                            "output_cost_per_token": 1.0,
                                            "cache_creation_input_token_cost": 1.0,
                                            "cache_read_input_token_cost": 1.0}},
        codex_pricing={"gpt-5": {"input_cost_per_token": 2.0,
                                 "output_cost_per_token": 2.0,
                                 "cache_read_input_token_cost": 2.0}},
        aliases={},
        tier_thresholds={"claude": 1, "codex": 2},
        fallback_model="gpt-5",
        cache_write_1h_multiplier=9.0,
        fast_multipliers={"claude": {}, "codex": {}, "codex_fallback": 1.0},
    )
    stale = {
        id(before.claude_pricing): "claude_pricing",
        id(before.codex_pricing): "codex_pricing",
        id(before.aliases): "aliases",
        id(before.fast_multipliers["claude"]): "fast_multipliers[claude]",
        id(before.fast_multipliers["codex"]): "fast_multipliers[codex]",
    }
    try:
        assert pricing.adopt_pricing_candidate(candidate) is True
        leaked = []
        for mod_name, module in list(sys.modules.items()):
            if module is None:
                continue
            for attr in list(vars(module) or {}):
                try:
                    value = getattr(module, attr)
                except Exception:  # noqa: BLE001
                    continue
                if id(value) in stale:
                    leaked.append(f"{mod_name}.{attr} -> {stale[id(value)]}")
        assert leaked == [], (
            "these modules still hold the superseded pricing objects, so a "
            f"reload cannot replace what they price from: {sorted(leaked)}")
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


_MISSING = object()

#: Every module-global NAME that carries a pricing value, mapped to the
#: `PricingSnapshot` field that supplies it. Independent of
#: `PRICING_EXPORT_BINDINGS` on purpose: the guard below sweeps by NAME across
#: every loaded `bin/` module, so DELETING an inventory entry still fails —
#: which an assertion driven off the inventory alone cannot do.
_PRICING_VALUE_NAMES = {
    "PRICING_SNAPSHOT_DATE": lambda s: s.snapshot_date,
    "CLAUDE_MODEL_PRICING": lambda s: s.claude_pricing,
    "CODEX_MODEL_PRICING": lambda s: s.codex_pricing,
    "CODEX_MODEL_ALIASES": lambda s: s.aliases,
    "TIERED_THRESHOLD": lambda s: s.tier_thresholds["claude"],
    "DEFAULT_TIERED_THRESHOLD": lambda s: s.tier_thresholds["claude"],
    "CODEX_TIERED_THRESHOLD": lambda s: s.tier_thresholds["codex"],
    "CODEX_LEGACY_FALLBACK_MODEL": lambda s: s.fallback_model,
    "CACHE_WRITE_1H_MULTIPLIER": lambda s: s.cache_write_1h_multiplier,
    "CLAUDE_FAST_MULTIPLIER_OVERRIDES": lambda s: s.fast_multipliers["claude"],
    "CODEX_FAST_MULTIPLIER_OVERRIDES": lambda s: s.fast_multipliers["codex"],
    "CODEX_FAST_MULTIPLIER_FALLBACK":
        lambda s: s.fast_multipliers["codex_fallback"],
}


def _production_modules():
    """Every loaded `bin/` module registered under its CANONICAL name.

    The canonical-name restriction is load-bearing rather than tidy.
    `PRICING_EXPORT_BINDINGS` addresses a module by the `sys.modules` key it
    would ordinarily have, so that is the only identity a swap can reach. Test
    modules elsewhere in the estate execute a `bin/` file into a second module
    object under a private alias — `tests/test_claude_fast_pricing.py`
    registers `bin/_lib_cache_report.py` as `_issue_413_cache_report` — and
    that copy holds the same pricing-named globals at whatever revision it was
    loaded with. Reporting it would be reporting a module the inventory cannot
    address and should not try to, which is a different fact from the one this
    guard exists to catch.
    """
    bin_dir = pathlib.Path(
        sys.modules["_lib_pricing"].__file__).resolve().parent
    found = {}
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            resolved = pathlib.Path(path).resolve()
        except OSError:
            continue
        if resolved.parent != bin_dir:
            continue
        if name != resolved.stem:
            continue
        found[name] = module
    return found


def test_every_recorded_binding_is_refreshed_by_a_swap(pricing):
    """The inventory guard, non-vacuous for the SCALAR bindings too.

    The previous form held `id()` of five CONTAINER objects, so the six
    scalar-valued binding kinds — the snapshot date, both tier thresholds, the
    Codex fallback model, the cache-write multiplier and the Codex fast
    fallback — were invisible to it: deleting any of their
    `PRICING_EXPORT_BINDINGS` entries failed no assertion. This drives the
    assertion off the inventory itself, so it is complete by construction for
    every entry rather than for the five the author happened to list.
    """
    load_script()
    before = pricing.current_pricing_snapshot()
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-11-11",
        claude_pricing={"claude-opus-4-8": {"input_cost_per_token": 1.5}},
        codex_pricing={"gpt-5": {"input_cost_per_token": 2.5}},
        aliases={"gpt-5-x": "gpt-5"},
        tier_thresholds={"claude": 111, "codex": 222},
        fallback_model="gpt-5-sentinel",
        cache_write_1h_multiplier=3.5,
        fast_multipliers={"claude": {"a": 1.0}, "codex": {"b": 2.0},
                          "codex_fallback": 4.5},
    )
    assert pricing.PRICING_EXPORT_BINDINGS, "the inventory is empty"
    try:
        assert pricing.adopt_pricing_candidate(candidate) is True
        wrong = []
        for module_name, attr, accessor in pricing.PRICING_EXPORT_BINDINGS:
            module = sys.modules.get(module_name)
            assert module is not None, (
                f"{module_name} is not loaded, so its binding is untested")
            actual = getattr(module, attr, _MISSING)
            expected = accessor(candidate)
            if actual is _MISSING or actual != expected:
                wrong.append(f"{module_name}.{attr}={actual!r} "
                             f"(expected {expected!r})")
        assert wrong == [], (
            "a swap left these recorded bindings holding the superseded "
            f"revision: {wrong}")
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_no_pricing_named_global_escapes_the_inventory(pricing):
    """The other half, and the one a DELETED inventory entry cannot hide from.

    An assertion driven off `PRICING_EXPORT_BINDINGS` checks exactly what the
    inventory already lists, so removing an entry removes the check with it.
    This sweeps every loaded `bin/` module by NAME instead, so a
    pricing-carrying global that no inventory entry refreshes is reported
    wherever it lives.
    """
    load_script()
    importlib.import_module("_lib_cache_report")
    before = pricing.current_pricing_snapshot()
    # `_PRICING_VALUE_NAMES` is a second inventory, so pin it against the
    # first: every recorded attribute name must appear in it, resolving to the
    # same snapshot field.
    for _module_name, attr, accessor in pricing.PRICING_EXPORT_BINDINGS:
        assert attr in _PRICING_VALUE_NAMES, (
            f"{attr} is a recorded binding this sweep does not know about")
        assert _PRICING_VALUE_NAMES[attr](before) == accessor(before), (
            f"{attr} resolves to a different snapshot field in each inventory")
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-12-12",
        claude_pricing={"claude-opus-4-8": {"input_cost_per_token": 1.25}},
        codex_pricing={"gpt-5": {"input_cost_per_token": 2.25}},
        aliases={"gpt-5-y": "gpt-5"},
        tier_thresholds={"claude": 1111, "codex": 2222},
        fallback_model="gpt-5-sentinel-2",
        cache_write_1h_multiplier=6.5,
        fast_multipliers={"claude": {"c": 1.0}, "codex": {"d": 2.0},
                          "codex_fallback": 8.5},
    )
    try:
        assert pricing.adopt_pricing_candidate(candidate) is True
        stale = []
        for module_name, module in sorted(_production_modules().items()):
            for attr, accessor in _PRICING_VALUE_NAMES.items():
                if attr not in vars(module):
                    continue
                actual = getattr(module, attr)
                expected = accessor(candidate)
                if actual != expected:
                    stale.append(f"{module_name}.{attr}={actual!r} "
                                 f"(expected {expected!r})")
        assert stale == [], (
            "these production globals carry a pricing value that no "
            "`PRICING_EXPORT_BINDINGS` entry refreshes, so a reload leaves "
            f"them pricing from the superseded revision: {stale}")
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_every_cost_kernel_prices_from_the_published_snapshot(pricing):
    """Not just the tables: the thresholds, the fallback and the cache-write
    multiplier are pricing-affecting too, and a swap that misses one of them
    prices part of an entry from the old revision."""
    load_script()
    before = pricing.current_pricing_snapshot()
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-04-04",
        claude_pricing={"claude-opus-4-8": {
            "input_cost_per_token": 1e-5,
            "output_cost_per_token": 2e-5,
            "cache_creation_input_token_cost": 3e-5,
            "cache_read_input_token_cost": 4e-5}},
        codex_pricing={"gpt-5": {"input_cost_per_token": 5e-5,
                                 "output_cost_per_token": 6e-5,
                                 "cache_read_input_token_cost": 7e-5}},
        aliases={"gpt-5-latest": "gpt-5"},
        tier_thresholds={"claude": 10, "codex": 20},
        fallback_model="gpt-5",
        cache_write_1h_multiplier=7.0,
        fast_multipliers={"claude": {"claude-opus-4-8": 3.0},
                          "codex": {"gpt-5": 4.0}, "codex_fallback": 5.0},
    )
    try:
        assert pricing.adopt_pricing_candidate(candidate) is True
        resolved = pricing._resolve_model_pricing("claude-opus-4-8")
        assert resolved["input_cost_per_token"] == 1e-5
        codex, is_fallback = pricing._resolve_codex_pricing("gpt-5-latest")
        assert codex["input_cost_per_token"] == 5e-5 and is_fallback is False
        assert pricing.CACHE_WRITE_1H_MULTIPLIER == 7.0
        assert pricing.TIERED_THRESHOLD == 10
        assert pricing.CODEX_TIERED_THRESHOLD == 20
        assert pricing._claude_fast_multiplier("claude-opus-4-8") == 3.0
        assert pricing._codex_fast_multiplier("gpt-5") == 4.0
        assert pricing._codex_fast_multiplier("gpt-unknown") == 5.0
        assert pricing.PRICING_SNAPSHOT_DATE == "2099-04-04"
        cctally = sys.modules["cctally"]
        assert cctally.CLAUDE_MODEL_PRICING["claude-opus-4-8"][
            "input_cost_per_token"] == 1e-5
        assert cctally.CACHE_WRITE_1H_MULTIPLIER == 7.0
        assert sys.modules["_cctally_cache"].PRICING_SNAPSHOT_DATE == \
            "2099-04-04"
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_a_dashboard_cost_calculation_uses_the_new_rate(pricing):
    """The dashboard paths named in the spec: the cache-report kernel and the
    Codex source calculation both read `cctally`'s import-time copies."""
    ns = load_script()
    before = pricing.current_pricing_snapshot()
    doubled_claude = {
        model: {k: (v * 2 if isinstance(v, (int, float)) else v)
                for k, v in rates.items()}
        for model, rates in before.claude_pricing.items()
    }
    doubled_codex = {
        model: {k: (v * 2 if isinstance(v, (int, float)) else v)
                for k, v in rates.items()}
        for model, rates in before.codex_pricing.items()
    }
    usage = ns["claude_usage_dict"](
        cache_1h_tokens=0, speed="standard", input_tokens=1000,
        output_tokens=1000, cache_creation_tokens=0, cache_read_tokens=0)
    baseline = ns["_calculate_entry_cost"]("claude-opus-4-8", usage)
    assert baseline > 0
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-05-05",
        claude_pricing=doubled_claude,
        codex_pricing=doubled_codex,
        aliases=before.aliases,
        tier_thresholds=before.tier_thresholds,
        fallback_model=before.fallback_model,
        cache_write_1h_multiplier=before.cache_write_1h_multiplier,
        fast_multipliers=before.fast_multipliers,
    )
    try:
        assert pricing.adopt_pricing_candidate(candidate) is True
        after = ns["_calculate_entry_cost"]("claude-opus-4-8", usage)
        assert after == pytest.approx(baseline * 2, rel=1e-9)
        # The dashboard's own copies, which are what a partial swap misses.
        assert sys.modules["cctally"].CLAUDE_MODEL_PRICING is \
            pricing.current_pricing_snapshot().claude_pricing
        assert sys.modules["cctally"].CODEX_MODEL_PRICING is \
            pricing.current_pricing_snapshot().codex_pricing
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_work_in_flight_keeps_the_snapshot_it_captured(pricing):
    """A request, a snapshot build or a sync pass that started before the swap
    keeps pricing from the revision it began with, so one response is never
    assembled from two."""
    load_script()
    before = pricing.current_pricing_snapshot()
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-06-06",
        claude_pricing={"claude-opus-4-8": {"input_cost_per_token": 99.0,
                                            "output_cost_per_token": 99.0,
                                            "cache_creation_input_token_cost": 99.0,
                                            "cache_read_input_token_cost": 99.0}},
        codex_pricing=before.codex_pricing,
        aliases=before.aliases,
        tier_thresholds=before.tier_thresholds,
        fallback_model=before.fallback_model,
        cache_write_1h_multiplier=before.cache_write_1h_multiplier,
        fast_multipliers=before.fast_multipliers,
    )
    try:
        with pricing.pricing_snapshot_context() as captured:
            assert captured is before
            assert pricing.adopt_pricing_candidate(candidate) is True
            assert pricing.current_pricing_snapshot() is before, (
                "work already inside a pricing context must not observe the "
                "swap it did not start with")
            assert pricing._resolve_model_pricing(
                "claude-opus-4-8")["input_cost_per_token"] != 99.0
        assert pricing.current_pricing_snapshot() is candidate
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_concurrent_readers_never_observe_a_half_swapped_snapshot(pricing):
    """The pointer swap is one assignment of one immutable object, so a reader
    sees the old snapshot whole or the new one whole."""
    load_script()
    before = pricing.current_pricing_snapshot()
    candidate = pricing.PricingSnapshot(
        snapshot_date="2099-07-07",
        claude_pricing={"m": {"input_cost_per_token": 1.0}},
        codex_pricing={"gpt-5": {"input_cost_per_token": 1.0}},
        aliases={},
        tier_thresholds={"claude": 7, "codex": 7},
        fallback_model="gpt-5",
        cache_write_1h_multiplier=7.0,
        fast_multipliers={"claude": {}, "codex": {}, "codex_fallback": 7.0},
    )
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            snap = pricing.current_pricing_snapshot()
            seen.append((snap.snapshot_date, snap.cache_write_1h_multiplier,
                         snap.tier_thresholds["claude"]))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for _ in range(50):
            pricing.adopt_pricing_candidate(candidate, force=True)
            pricing.adopt_pricing_candidate(before, force=True)
        stop.set()
        for t in threads:
            t.join(30)
        expected = {
            (before.snapshot_date, before.cache_write_1h_multiplier,
             before.tier_thresholds["claude"]),
            (candidate.snapshot_date, candidate.cache_write_1h_multiplier,
             candidate.tier_thresholds["claude"]),
        }
        assert seen, "the readers observed nothing"
        assert set(seen) <= expected, set(seen) - expected
    finally:
        stop.set()
        for t in threads:
            t.join(30)
        pricing.adopt_pricing_candidate(before, force=True)


def _boot_dashboard(ns, tmp_path, monkeypatch):
    """The real handler class on a real socket, the way `cmd_dashboard` wires it.

    Deliberately a booted server rather than a hand-called handler method: the
    finding this covers is that nothing in production ENTERS
    `pricing_snapshot_context`, and only the real dispatch path can show that
    it now does.
    """
    from tests._support_http import serve_dashboard, shorten_sse_keepalive

    redirect_paths(ns, monkeypatch, tmp_path)
    shorten_sse_keepalive(ns, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    handler_cls = ns["DashboardHTTPHandler"]
    snapshot = dash._empty_dashboard_snapshot()
    handler_cls.snapshot_ref = ns["_SnapshotRef"](snapshot)
    handler_cls.hub = ns["SSEHub"]()
    handler_cls.hub.publish(handler_cls.snapshot_ref.get())
    handler_cls.sync_lock = threading.Lock()
    handler_cls.run_sync_now = staticmethod(lambda *a, **k: None)
    handler_cls.cctally_host = "127.0.0.1"
    handler_cls.cctally_api_token = None
    return serve_dashboard(
        ns, configure=lambda srv: setattr(
            srv, "handle_error", lambda request, addr: None))


def _synthetic_candidate(pricing, before, date):
    return pricing.PricingSnapshot(
        snapshot_date=date,
        claude_pricing={"claude-opus-4-8": {"input_cost_per_token": 99.0,
                                            "output_cost_per_token": 99.0,
                                            "cache_creation_input_token_cost": 99.0,
                                            "cache_read_input_token_cost": 99.0}},
        codex_pricing=before.codex_pricing,
        aliases=before.aliases,
        tier_thresholds=before.tier_thresholds,
        fallback_model=before.fallback_model,
        cache_write_1h_multiplier=before.cache_write_1h_multiplier,
        fast_multipliers=before.fast_multipliers,
    )


def test_a_real_request_observes_one_pricing_revision_across_a_live_swap(
        tmp_path, monkeypatch):
    """The wiring, not the mechanism.

    `pricing_snapshot_context()` was defined, documented and tested in
    isolation while NOTHING in `bin/` entered it, so a snapshot build or a
    request straddling a swap priced its first half from one revision and its
    second from another — the outcome §5 exists to prevent. This drives a real
    HTTP request through the real dispatch table, swaps the live pointer
    half-way through the handler, and asserts the request kept the revision it
    entered with.
    """
    from tests._support_http import stop

    ns = load_script()
    srv, thread, port = _boot_dashboard(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    candidate = _synthetic_candidate(pricing, before, "2099-10-10")
    observed = []

    def probing_handler(self):
        observed.append(pricing.current_pricing_snapshot())
        assert pricing.adopt_pricing_candidate(candidate) is True
        observed.append(pricing.current_pricing_snapshot())
        observed.append(
            pricing._resolve_model_pricing("claude-opus-4-8")[
                "input_cost_per_token"])
        self._respond_json(200, {"ok": True})

    monkeypatch.setattr(
        dash.DashboardHTTPHandler, "_handle_get_doctor", probing_handler)
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/api/doctor")
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        conn.close()
        assert len(observed) == 3, observed
        assert observed[0] is before
        assert observed[1] is before, (
            "the request must keep the revision it entered with; the swap it "
            "did not start with reached it half-way through")
        assert observed[2] != 99.0, (
            "the second half of the response priced from the new revision")
        # The context is scoped to the request, not leaked past it.
        assert pricing.current_pricing_snapshot() is candidate
    finally:
        stop(srv, thread)
        pricing.adopt_pricing_candidate(before, force=True)


def test_the_snapshot_build_iteration_pins_one_pricing_revision(tmp_path,
                                                                monkeypatch):
    """`run_iteration` is the dashboard's whole snapshot build. A swap landing
    part-way through it would assemble one published snapshot from two
    revisions."""
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    candidate = _synthetic_candidate(pricing, before, "2099-10-11")
    observed = []

    def probing_sync(**kwargs):
        observed.append(pricing.current_pricing_snapshot())
        assert pricing.adopt_pricing_candidate(candidate) is True
        observed.append(pricing.current_pricing_snapshot())

    try:
        run_iteration = dash._make_dashboard_run_iteration(
            sync_lock=threading.Lock(),
            run_sync_now=probing_sync,
            run_sync_now_locked=probing_sync,
            skip_sync=True,
        )
        run_iteration()
        assert observed == [before, before], observed
        assert pricing.current_pricing_snapshot() is candidate
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


def test_the_conversation_sync_pass_pins_one_pricing_revision(tmp_path,
                                                              monkeypatch):
    """The transcript ingest pass writes materialized cost, so it is the sync
    pass §5 names alongside the request and the build."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    candidate = _synthetic_candidate(pricing, before, "2099-10-12")
    observed = []

    def probing_claude(conn, **kwargs):
        observed.append(pricing.current_pricing_snapshot())
        assert pricing.adopt_pricing_candidate(candidate) is True
        observed.append(pricing.current_pricing_snapshot())
        return None

    monkeypatch.setattr(dash, "_conversation_frontier_plans", lambda conn: None)
    monkeypatch.setattr(dash, "sync_claude_conversations", probing_claude)
    monkeypatch.setattr(dash, "sync_codex_conversations",
                        lambda conn, **kwargs: None)
    monkeypatch.setattr(dash, "_dashboard_maybe_prune_retention", lambda: None)
    try:
        assert str(dash._conversation_sync_pass()) == "ok"
        assert observed == [before, before], observed
        assert pricing.current_pricing_snapshot() is candidate
    finally:
        pricing.adopt_pricing_candidate(before, force=True)


# --- Task 4.3: the detector has an owner in both modes ---------------------


def test_the_pricing_watch_thread_starts_under_no_sync(tmp_path, monkeypatch):
    """`_DashboardSyncThread` and `_make_conversation_sync_thread` are BOTH
    disabled under `--no-sync`, so a detector placed in either has no owner
    there — which is the mode a long-lived read-only dashboard runs in."""
    import ast

    load_script()
    dash_path = pathlib.Path(sys.modules["_cctally_dashboard"].__file__)
    tree = ast.parse(dash_path.read_text())
    cmd = next(node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and node.name == "cmd_dashboard")
    starts = [node for node in ast.walk(cmd)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "_start_pricing_watch"]
    assert len(starts) == 1, "cmd_dashboard must start the watch exactly once"
    guarded = [
        node for node in ast.walk(cmd)
        if isinstance(node, ast.If)
        and any(isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_start_pricing_watch"
                for inner in ast.walk(node))
    ]
    assert guarded == [], (
        "the pricing watch must not sit behind an `if` — `--no-sync` is "
        "exactly the mode with no other owner")
    # ORDERING. Counting the call and refusing an `if` around it still admits a
    # call placed AFTER `serve_forever`, where it would never run at all
    # because `serve_forever` blocks. Pin it ahead of the serving thread.
    serving = [node.lineno for node in ast.walk(cmd)
               if isinstance(node, ast.Attribute)
               and node.attr == "serve_forever"]
    assert serving, "cmd_dashboard no longer starts the HTTP server here"
    assert starts[0].lineno < min(serving), (
        "the pricing watch must start BEFORE the server begins serving: "
        f"{starts[0].lineno} vs {min(serving)}")


def test_the_watch_actually_adopts_a_replaced_pricing_file(tmp_path,
                                                           monkeypatch):
    """The behavioural half. The AST case above pins where the call sits; this
    one pins that the watch does the job — a running watch, a replaced file,
    and the process pricing from the new revision without a restart."""
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    newer = (dt.date.fromisoformat(before.snapshot_date)
             + dt.timedelta(days=1)).isoformat()
    path = _write_candidate(
        tmp_path, _snapshot_source(date=before.snapshot_date))
    watch = dash._PricingWatch(source_path=path, interval_s=0.01)
    thread = threading.Thread(target=watch.run, daemon=True)
    try:
        assert watch.poll_once() is False, (
            "the baseline poll must not adopt a same-dated revision")
        assert pricing.current_pricing_snapshot() is before
        thread.start()
        path.write_text(_snapshot_source(date=newer, claude_rate=0.000777))
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if pricing.current_pricing_snapshot().snapshot_date == newer:
                break
            time.sleep(0.02)
        adopted = pricing.current_pricing_snapshot()
        assert adopted.snapshot_date == newer, (
            "the running watch never adopted the replaced file")
        assert adopted.claude_pricing["claude-opus-4-8"][
            "input_cost_per_token"] == 0.000777
        assert sys.modules["cctally"].CLAUDE_MODEL_PRICING is \
            adopted.claude_pricing, (
                "the import-time copies must move with the adoption")
    finally:
        watch.stop()
        thread.join(20)
        pricing.adopt_pricing_candidate(before, force=True)


def test_the_watch_parses_only_after_the_signature_changes(tmp_path,
                                                           monkeypatch):
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    path = _write_candidate(tmp_path, _snapshot_source(date="2099-08-08"))
    parses = []
    real = pricing.read_pricing_snapshot_from_source

    def counting(p):
        parses.append(str(p))
        return real(p)

    monkeypatch.setattr(pricing, "read_pricing_snapshot_from_source", counting)
    watch = dash._PricingWatch(source_path=path, interval_s=0.0)
    watch.poll_once()
    assert len(parses) == 1, "the first poll establishes the signature"
    watch.poll_once()
    watch.poll_once()
    assert len(parses) == 1, (
        "an unchanged inode/size/mtime signature must not be re-parsed")
    path.write_text(_snapshot_source(date="2099-09-09"))
    watch.poll_once()
    assert len(parses) == 2


def test_the_watch_never_raises_out_of_its_thread(tmp_path, monkeypatch):
    """A pricing file an installer is mid-write through is the ordinary case,
    not an exceptional one, and a raising watch would take the thread down and
    leave the process permanently stale.

    The thread is the whole claim, so the thread is what this runs. The prior
    form called `poll_once` twice on the calling thread and asserted nothing,
    which pinned neither that the watch survives nor that nothing escapes it.
    """
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    path = tmp_path / "_lib_pricing.py"
    watch = dash._PricingWatch(source_path=path, interval_s=0.01)
    polls = []
    real_poll = watch.poll_once

    def counting():
        polls.append(real_poll())

    watch.poll_once = counting
    escaped = []

    def runner():
        try:
            watch.run()
        except BaseException as exc:  # noqa: BLE001 — that is the assertion
            escaped.append(exc)

    def wait_for_more_polls(*, attempts=400):
        mark = len(polls)
        for _ in range(attempts):
            if len(polls) > mark:
                return True
            time.sleep(0.02)
        return False

    thread = threading.Thread(target=runner, daemon=True)
    try:
        thread.start()
        # 1. the file does not exist at all
        assert wait_for_more_polls()
        # 2. a file an installer is part-way through writing
        path.write_text("PRICING_SNAPSHOT_DATE = (")
        assert wait_for_more_polls()
        # 3. complete, parseable, but not a literal
        path.write_text('PRICING_SNAPSHOT_DATE = str("2099-01-01")\n')
        assert wait_for_more_polls()
        # 4. complete and literal, but a rollback
        path.write_text(_snapshot_source(date="1999-01-01"))
        assert wait_for_more_polls()
        assert thread.is_alive(), (
            "the watch thread died on a file an installer was mid-write "
            "through, which leaves the process permanently stale")
        assert escaped == [], escaped
        assert polls and not any(polls), (
            "no candidate above is adoptable, so every poll must report False")
        assert pricing.current_pricing_snapshot() is before
    finally:
        watch.stop()
        thread.join(20)
        pricing.adopt_pricing_candidate(before, force=True)


# --- Task 4.4: recovery, and the documented boundary -----------------------


def test_a_reload_clears_the_refusal_on_the_next_authorized_sync(
        tmp_path, monkeypatch):
    """End to end: a store recorded under a NEWER pricing revision than the
    process refuses the materialized-cost write; the pricing file is replaced;
    the next authorized recompute converges and marks the episode inactive."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["_cctally_cache"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    conn = ns["open_conversations_db"]()
    try:
        store_date = (dt.date.fromisoformat(before.snapshot_date)
                      + dt.timedelta(days=30)).isoformat()
        cache._set_cache_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_FP_KEY, store_date)
        conn.commit()
        assert cache._recompute_conversation_sessions(conn) is False
        # `_recompute_conversation_sessions` never commits — its production
        # callers do — so the refusal record and the flag it armed have to be
        # committed here, or closing this connection discards both and the
        # recovery sync below has nothing to converge.
        conn.commit()
        record = json.loads(_cache_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY))
        assert record["active"] is True

        newer = pricing.PricingSnapshot(
            snapshot_date=store_date,
            claude_pricing=before.claude_pricing,
            codex_pricing=before.codex_pricing,
            aliases=before.aliases,
            tier_thresholds=before.tier_thresholds,
            fallback_model=before.fallback_model,
            cache_write_1h_multiplier=before.cache_write_1h_multiplier,
            fast_multipliers=before.fast_multipliers,
        )
        assert pricing.adopt_pricing_candidate(newer) is True
        assert cache.PRICING_SNAPSHOT_DATE == store_date
        assert _cache_meta(
            conn, "conversation_sessions_backfill_pending") == "1", (
                "the refusal arms the flag the recovery sync consumes")
    finally:
        conn.close()

    # The documented recovery sequence, driven for real. The previous form
    # called the two internals by hand and then issued the
    # `DELETE FROM cache_meta` that the ingest path owns, so it simulated the
    # middle of the sequence it claimed to drive. `sync_claude_conversations`
    # is that ingest path: it arms the backfill, recomputes, consumes the flag
    # and settles the episode in one authorized pass. Nothing here restarts the
    # process.
    conn = ns["open_conversations_db"]()
    try:
        stats = ns["sync_claude_conversations"](conn)
        assert stats.deferred_reason is None, stats.deferred_reason
        assert _cache_meta(
            conn, "conversation_sessions_backfill_pending") is None, (
                "the authorized sync must consume the flag it converged")
        record = json.loads(_cache_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY))
        assert record["active"] is False, (
            "the converged recompute must mark the episode inactive, which is "
            "what returns doctor to OK with no restart")
        assert record["first_refused_at_utc"], (
            "the tombstone keeps when the episode opened")
    finally:
        conn.close()

    # And the operator-facing half the plan asks for: doctor returns OK with no
    # restart. Driven through the real `doctor --json` over the same store.
    report = subprocess.run(
        [sys.executable, str(pathlib.Path(ns["__file__"]).resolve()),
         "doctor", "--json"],
        env={**os.environ, "HOME": str(tmp_path), "TZ": "Etc/UTC"},
        capture_output=True, text=True,
    )
    payload = json.loads(report.stdout)
    check = next(
        c for category in payload["categories"] for c in category["checks"]
        if c["id"] == "pricing.conversation_rollup_writer"
    )
    assert check["severity"] == "ok", check
    pricing.adopt_pricing_candidate(before, force=True)


def test_a_reload_drops_no_connected_sse_client(tmp_path, monkeypatch):
    """`--no-sync` or not, the HTTP server and the SSE hub are untouched by an
    adoption. A restart is what this feature exists to avoid, so a client that
    was streaming before the swap must still be streaming after it."""
    from tests._support_http import PRESENCE_BACKSTOP_SECONDS, stop

    ns = load_script()
    srv, thread, port = _boot_dashboard(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    pricing = sys.modules["_lib_pricing"]
    before = pricing.current_pricing_snapshot()
    handler_cls = ns["DashboardHTTPHandler"]
    live = []

    def read_frame(response):
        buf = b""
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = response.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace")

    try:
        client = HTTPConnection("127.0.0.1", port,
                                timeout=PRESENCE_BACKSTOP_SECONDS)
        client.request("GET", "/api/events")
        response = client.getresponse()
        live.extend((client, response))
        assert response.status == 200
        assert "event: update" in read_frame(response)

        path = _write_candidate(
            tmp_path, _snapshot_source(date="2099-02-02", claude_rate=0.000999))
        watch = dash._PricingWatch(source_path=path, interval_s=0.0)
        assert watch.poll_once() is True, "the replaced file was not adopted"
        assert pricing.current_pricing_snapshot().snapshot_date == "2099-02-02"

        handler_cls.hub.publish(handler_cls.snapshot_ref.get())
        assert "event: update" in read_frame(response), (
            "the streaming client was dropped by the pricing adoption")
    finally:
        stop(srv, thread, connections=live)
        pricing.adopt_pricing_candidate(before, force=True)


def test_the_documented_boundary_is_recorded(pricing):
    """The failure mode of this feature is someone assuming it does more than
    pricing, so the boundary is written down and this pins that it stays."""
    doc = pathlib.Path(
        pathlib.Path(pricing.__file__).parent.parent
        / "docs" / "updates-gotchas.md").read_text()
    assert "pricing reload" in doc.lower()
    for word in ("algorithms", "endpoints", "migrations", "UI assets"):
        assert word.lower() in doc.lower(), word
