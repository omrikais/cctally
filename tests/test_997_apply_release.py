"""#997 T-D1 — the rederive apply releases its preview records before its
rebuild (spec D18, tests T18).

A `RederivePreview` carries every retained journal record of its prefix
(`records`) and one commit coordinate per record (`record_ends`). Nothing
after the apply's planning reads either, yet before D18 the apply ran
`rebuild_stats_index` with every preview it built still bound: R2a measured
1.11 GB held when the rebuild started and a 1.55 GB peak against the 1.43 GB
target. D18 rebinds each preview to a copy without those two fields
immediately before each of the three rebuild sites.

The probe cannot use tuple subclasses (CPython refuses `weakref.ref` on them)
or `gc.get_objects` identity (CPython untracks atomic-only tuples and dicts).
It wraps `_preview_from_snapshot` instead, so every preview the invocation
builds carries its `records` and `record_ends` as weak-referenceable sequence
wrappers and each record as a weak-referenceable `dict` copy, and it keeps
only weak references to them. The patched rebuild collects garbage and reads
which of them are still alive.
"""
from __future__ import annotations

import collections.abc
import dataclasses
import datetime as dt
import gc
import json
import weakref

import pytest

from tests.test_rederive_command import (
    _isolated,
    _raw_obs,
    _seed_cache,
    _seed_wrong_journal,
)

FAMILY = "claude-usage"
#: `_iso_now` pinned, so the two stores of a comparison plan identically.
FIXED_NOW = "2026-07-25T13:30:00+00:00"


@pytest.fixture(autouse=True)
def _without_account_bound_cutover_chokepoint(monkeypatch):
    """These scenarios pin their own journal prefixes (see the same opt-out
    in tests/test_rederive_command.py)."""
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")


# --- the probe --------------------------------------------------------------


class _ProbeSequence(collections.abc.Sequence):
    """A weak-referenceable stand-in for a preview's `records` or
    `record_ends` tuple. The mixin supplies iteration, `.index()`, `in` and
    `reversed`; indexing and slicing delegate to the wrapped tuple."""

    def __init__(self, items):
        self._items = tuple(items)

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self):
        return len(self._items)


class _ProbeRecord(dict):
    """A weak-referenceable copy of one journal record: container death alone
    would miss a copied collection that still held the records."""


class _Registry:
    """Weak references only: the registry must not keep anything alive."""

    def __init__(self):
        self.refs: list = []
        self.previews = 0
        self.observations: list = []

    def track(self, label, obj):
        self.refs.append((label, weakref.ref(obj)))

    def live(self):
        return [label for label, ref in self.refs if ref() is not None]


def _site(kwargs) -> str:
    if kwargs["context"].trigger == "rederive-apply":
        return "correction"
    # Only the completed-batch recovery passes a swap hook under the
    # `rederive-recovery` trigger; the reviewed materialization does not.
    return "recovery" if "before_swap" in kwargs else "materialization"


def _install_probe(monkeypatch, registry):
    import _cctally_journal as runtime
    import _cctally_rederive as rederive

    real_preview = rederive._preview_from_snapshot
    real_rebuild = runtime.rebuild_stats_index

    def wrapped_preview(*args, **kwargs):
        built = real_preview(*args, **kwargs)
        number = registry.previews
        registry.previews += 1
        records = _ProbeSequence(_ProbeRecord(record) for record in built.records)
        ends = _ProbeSequence(built.record_ends)
        registry.track(f"preview{number}.records", records)
        registry.track(f"preview{number}.record_ends", ends)
        for index in range(len(records)):
            registry.track(f"preview{number}.records[{index}]", records[index])
        return dataclasses.replace(built, records=records, record_ends=ends)

    def probe_rebuild(**kwargs):
        gc.collect()
        registry.observations.append((_site(kwargs), registry.live()))
        return real_rebuild(**kwargs)

    monkeypatch.setattr(rederive, "_preview_from_snapshot", wrapped_preview)
    monkeypatch.setattr(runtime, "rebuild_stats_index", probe_rebuild)


def _assert_released(registry, site):
    assert [observed for observed, _live in registry.observations] == [site]
    live = registry.observations[0][1]
    assert not live, (
        "preview records still reachable when the rebuild started: "
        f"{len(live)} live, first {live[:6]}"
    )


def _disable_release(monkeypatch):
    import _cctally_rederive as rederive

    # raising=False: before D18 the seam does not exist, and the control
    # must still run (and fail at the release assertion).
    monkeypatch.setattr(
        rederive, "_compact_preview", lambda preview: preview, raising=False,
    )


# --- scenarios ----------------------------------------------------------------


def _crash_after(stage):
    def hook(name):
        if name == stage:
            raise RuntimeError(f"simulated crash at {stage}")
    return hook


def _reviewed(mod, tmp_path, observation_ids, disposition):
    """Write a pinned schema-2 manifest at the current journal prefix and
    return (manifest path, op, expected hashes, manifest high-water)."""
    import _cctally_journal as runtime
    import _cctally_rederive as rederive

    high_water = runtime.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {
            "segment": high_water[0], "offset": high_water[1],
        },
        "journalPrefixHash": runtime.journal_prefix_hash(high_water),
        "reviewedAt": "2026-07-25T13:00:00Z",
        "reason": "Reviewed capture for the D18 release probe",
        "weeklyAxisDecisions": [
            {"observationId": observation_id, "disposition": disposition}
            for observation_id in observation_ids
        ],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "reviewed-decisions.json"
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    pre = rederive.preview_db_rederive(FAMILY, reviewed_op=op)
    hashes = (pre.baseline_plan.plan_hash, pre.plan.plan_hash)
    manifest.update({
        "expectedBaselinePlanHash": hashes[0],
        "expectedDecisionPlanHash": hashes[1],
    })
    path.write_text(json.dumps(manifest))
    op = rederive._reviewed_weekly_op_from_manifest(path)
    return path, op, hashes, manifest["journalHighWater"]


def _converged(mod):
    """A store whose plain plan has nothing left to correct."""
    import _cctally_rederive as rederive

    _seed_cache(mod)
    first, _wrong = _seed_wrong_journal(mod)
    assert rederive.apply_db_rederive(FAMILY).status == "applied"
    return first


def _replica_flap(mod):
    """Captures alternating between two reset instants (a stale replica);
    holding all but the first pair is a reviewed decision with correction
    actions (the shape of test_reviewed_weekly_decision_command_is_durable_
    and_idempotent)."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    _seed_cache(mod)
    records = []
    for minute, (weekly, end_day) in enumerate(
        ((60, 27), (10, 28), (60, 27), (10, 28), (60, 27), (10, 28))
    ):
        captured = f"2026-07-25T12:{minute:02d}:00Z"
        payload = dict(_raw_obs(journal)["payload"])
        payload.update({
            "captured_at": captured,
            "weekly_percent": weekly,
            "resets_at": int(dt.datetime(
                2026, 7, end_day, tzinfo=dt.timezone.utc).timestamp()),
            "five_hour_percent": 20 + minute,
            "five_hour_resets_at": "2026-07-25T16:00:00Z",
        })
        records.append(journal.make_obs(
            at=captured, src="record-usage", provider="claude",
            account="acct-a", payload=payload,
        ))
    runtime.append_records(
        records, now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc),
    )
    return [record["id"] for record in records[2:]]


def _append_op_then_crash(op, hashes, monkeypatch):
    import _cctally_rederive as rederive

    monkeypatch.setattr(
        rederive, "_REDERIVE_CRASH_HOOK",
        _crash_after("after-reviewed-decision-op"),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        rederive.apply_db_rederive(
            FAMILY, reviewed_op=op, expected_plan_hashes=hashes,
        )
    monkeypatch.setattr(rederive, "_REDERIVE_CRASH_HOOK", None)


def _setup(scenario, tmp_path, monkeypatch):
    """Build one scenario's store. Returns (module, invocation) where the
    invocation names the reviewed manifest (or None for a plain apply), the
    rebuild site the measured apply reaches, the status it reports, and the
    preview role an error at that site carries."""
    mod = _isolated(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _cctally_rederive as rederive

    monkeypatch.setattr(rederive, "_iso_now", lambda: FIXED_NOW)
    inv = {"path": None, "op": None, "hashes": None, "manifest_hw": None}
    if scenario == "correction":
        _seed_cache(mod)
        _seed_wrong_journal(mod)
        inv.update(site="correction", status="applied", role="execution")
    elif scenario == "recovery":
        _seed_cache(mod)
        _seed_wrong_journal(mod)
        real = runtime.rebuild_stats_index

        def crash(**_kwargs):
            raise OSError("injected rebuild crash")

        monkeypatch.setattr(runtime, "rebuild_stats_index", crash)
        with pytest.raises(rederive.RederiveApplyError):
            rederive.apply_db_rederive(FAMILY)
        monkeypatch.setattr(runtime, "rebuild_stats_index", real)
        inv.update(site="recovery", status="recovered", role="execution")
    elif scenario == "no-op":
        _converged(mod)
        inv.update(site=None, status="no-op", role=None)
    elif scenario in {"reviewed-fresh", "reviewed-retry"}:
        first = _converged(mod)
        path, op, hashes, manifest_hw = _reviewed(
            mod, tmp_path, [first["id"]], "accept")
        if scenario == "reviewed-retry":
            _append_op_then_crash(op, hashes, monkeypatch)
        # D4: the decision's plan is empty, so the apply completes it with an
        # empty correction batch and rebuilds at the correction site.
        inv.update(
            path=path, op=op, hashes=hashes, manifest_hw=manifest_hw,
            site="correction",
            status="applied" if scenario == "reviewed-fresh" else "recovered",
            role="execution",
        )
    elif scenario in {"reviewed-fresh-correction", "reviewed-retry-correction"}:
        held = _replica_flap(mod)
        path, op, hashes, manifest_hw = _reviewed(
            mod, tmp_path, held, "hold")
        if scenario == "reviewed-retry-correction":
            _append_op_then_crash(op, hashes, monkeypatch)
        inv.update(
            path=path, op=op, hashes=hashes, manifest_hw=manifest_hw,
            site="correction", status="applied", role="execution",
        )
    else:  # pragma: no cover - parametrization typo
        raise AssertionError(scenario)
    inv["pre_run_hw"] = runtime.journal_high_water()
    return mod, inv


PROBE_SCENARIOS = (
    "correction",
    "recovery",
    "reviewed-fresh",
    "reviewed-retry",
    "reviewed-fresh-correction",
    "reviewed-retry-correction",
)


#: One scenario per rebuild site (correction, recovery, materialization) runs
#: the control: the probe's ability to see a retained preview is a property
#: of the site, not of the path that reached it.
CONTROL_SCENARIOS = ("correction", "recovery", "reviewed-fresh")


@pytest.mark.parametrize("scenario, release", [
    *((scenario, "on") for scenario in PROBE_SCENARIOS),
    *((scenario, "disabled") for scenario in CONTROL_SCENARIOS),
])
def test_apply_releases_records_before_rebuild(
    scenario, release, tmp_path, monkeypatch,
):
    """T18: at entry to every rebuild site, none of the `records` or
    `record_ends` containers any preview of this invocation carried, nor any
    record they held, is still reachable. The control (release disabled)
    must fail at that same assertion at each site, which proves the probe can
    see a retained preview there."""
    _mod, inv = _setup(scenario, tmp_path, monkeypatch)
    import _cctally_rederive as rederive

    registry = _Registry()
    _install_probe(monkeypatch, registry)
    if release == "disabled":
        _disable_release(monkeypatch)
    result = rederive.apply_db_rederive(
        FAMILY, reviewed_op=inv["op"], expected_plan_hashes=inv["hashes"],
    )
    assert result.status == inv["status"]
    # Non-vacuity: the invocation built previews holding records, and on an
    # exact retry it built the original too.
    assert registry.previews >= (2 if scenario.startswith("reviewed") else 1)
    assert any(".records[" in label for label, _ref in registry.refs)
    if release == "disabled":
        with pytest.raises(AssertionError, match="still reachable"):
            _assert_released(registry, inv["site"])
        return
    _assert_released(registry, inv["site"])
    assert result.preview.records == ()
    assert result.preview.record_ends == ()


# --- output equivalence -------------------------------------------------------


def _command_args(mod, inv, *, as_json):
    argv = ["db", "rederive", "--family", FAMILY]
    if inv["path"] is not None:
        argv += ["--reviewed-weekly-decisions", str(inv["path"])]
    argv.append("--yes")
    if as_json:
        argv.append("--json")
    return mod.build_parser().parse_args(argv)


def _stable_rebuild(monkeypatch, failure):
    """The real rebuild with its wall-clock duration zeroed, or a rebuild
    that fails the way `failure` names."""
    import _cctally_completion
    import _cctally_journal as runtime

    real = runtime.rebuild_stats_index

    def rebuild(**kwargs):
        if failure == "raises":
            raise OSError("injected rebuild failure")
        if failure == "obligation":
            raise _cctally_completion.ResidualObligationNotDurable(
                "injected obligation loss")
        return dataclasses.replace(real(**kwargs), duration_s=0.0)

    monkeypatch.setattr(runtime, "rebuild_stats_index", rebuild)


def _run_command(scenario, tmp_path, monkeypatch, capsys, *,
                 release, as_json, failure=None):
    mod, inv = _setup(scenario, tmp_path, monkeypatch)
    capsys.readouterr()
    # The measured run's patches are scoped to it, so the next store's setup
    # rebuilds through the real `rebuild_stats_index`.
    with pytest.MonkeyPatch.context() as measured:
        _stable_rebuild(measured, failure)
        if release == "disabled":
            _disable_release(measured)
        status = mod.cmd_db_rederive(_command_args(mod, inv, as_json=as_json))
    out, err = capsys.readouterr()
    return inv, status, out, err


def _both(scenario, tmp_path, monkeypatch, capsys, *, failure=None):
    """Run the command in four fresh stores: JSON and text, each with the
    release on and with `_compact_preview` patched to the identity."""
    runs = {}
    for release in ("on", "disabled"):
        for as_json in (True, False):
            runs[release, as_json] = _run_command(
                scenario, tmp_path / f"{release}-{'json' if as_json else 'text'}",
                monkeypatch, capsys,
                release=release, as_json=as_json, failure=failure,
            )
    for as_json in (True, False):
        _inv, on_status, on_out, on_err = runs["on", as_json]
        _inv, off_status, off_out, off_err = runs["disabled", as_json]
        assert (on_status, on_out, on_err) == (off_status, off_out, off_err)
    return runs


EQUIVALENCE_SCENARIOS = (
    "correction",
    "recovery",
    "reviewed-fresh",
    "reviewed-retry",
    "reviewed-fresh-correction",
    "reviewed-retry-correction",
    "no-op",
)


@pytest.mark.parametrize("scenario", EQUIVALENCE_SCENARIOS)
def test_apply_output_unchanged_by_release(
    scenario, tmp_path, monkeypatch, capsys,
):
    """T18: the applied, recovered, reviewed-applied, exact-retry and no-op
    paths print the same JSON and text and exit the same with the release on
    as with it disabled."""
    runs = _both(scenario, tmp_path, monkeypatch, capsys)
    inv, status, out, _err = runs["on", True]
    assert status == 0
    payload = json.loads(out)
    assert payload["status"] == inv["status"]
    assert payload["errors"] == []


ERROR_SCENARIOS = (
    "correction",
    "recovery",
    "reviewed-fresh",
    "reviewed-retry",
    "reviewed-fresh-correction",
    "reviewed-retry-correction",
)


#: A rebuild that raises runs at every scenario; the residual obligation that
#: is not durable runs once per rebuild site, whose handler is the same for
#: every scenario that reaches it.
@pytest.mark.parametrize("scenario, failure", [
    *((scenario, "raises") for scenario in ERROR_SCENARIOS),
    *((scenario, "obligation") for scenario in CONTROL_SCENARIOS),
])
def test_apply_error_output_unchanged_by_release(
    scenario, failure, tmp_path, monkeypatch, capsys,
):
    """T18: at each rebuild site, a rebuild that raises (`RederiveApplyError`)
    and a residual obligation that is not durable (`_obligation_lost`) print
    the same JSON and text and exit the same with the release on as with it
    disabled, and the error keeps its preview role: the execution preview for
    a correction or a recovery, the reporting preview for a reviewed
    decision's materialization."""
    runs = _both(scenario, tmp_path, monkeypatch, capsys, failure=failure)
    inv, status, out, err = runs["on", True]
    assert status == 3
    payload = json.loads(out)
    assert payload["status"] == "failed"
    if failure == "obligation":
        assert payload["indexPublished"] is True
        assert "injected obligation loss" in payload["errors"][0]
    else:
        assert "injected rebuild failure" in payload["errors"][0]
    hw = payload["journalHighWater"]
    if inv["role"] == "report":
        assert hw == inv["manifest_hw"]
    elif inv["manifest_hw"] is not None:
        assert hw != inv["manifest_hw"]
    else:
        assert (hw["segment"], hw["offset"]) == tuple(inv["pre_run_hw"])
    _inv, _status, text_out, text_err = runs["on", False]
    assert text_out == ""
    assert text_err
