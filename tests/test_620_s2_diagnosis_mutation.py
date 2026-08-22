"""#620 S2 — mutation and failure-injection checks for the diagnosis.

A golden proves the output has not changed. These prove the output DEPENDS on
its inputs, which is the property a golden cannot observe: a diagnosis that
had stopped reading a signal would keep every golden green forever.

Each mutation changes exactly one raw input, proves that input's own
generation component digest moved, and only then requires the reported winner
or value to change. The digest assertion is what stops the check passing
vacuously against an input nothing actually reads.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _cctally_core
import _lib_diagnosis as kernel
from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_sources import (
    WINDOW_END, WINDOW_START, _scope, _seed_claude, _seed_claude_blocks,
    _sources,
)


UTC = dt.timezone.utc
_REPO = Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"


@pytest.fixture
def seeded_store(tmp_path, monkeypatch):
    """Four deliberately dominant signals over one measurable window.

    Entries are spread across FOUR five-hour blocks rather than piled into
    one. With every entry inside a single block the burst class has one
    distinct subject and withholds for want of support, so a mutation that
    moved the blocks would change nothing observable and the check would pass
    while proving nothing.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-opus-4-20250514", 40),
                             ("claude-haiku-4-5", 20)),
                 projects=("/repo/alpha", "/repo/beta"),
                 sessions=("sess-a", "sess-b"),
                 interval_minutes=15)
    _seed_claude_blocks(ns)
    return ns


def _cache_conn(ns):
    return ns["open_cache_db"]()


def _stats_conn(ns):
    return ns["open_db"]()


def _generation(scope=None):
    resolved = scope or _scope()
    return _sources().establish_generation(
        resolved, kernel.resolve_policy_plan(resolved.source,
                                            transcripts_visible=True))


def _diagnose(scope=None):
    return _sources().build_diagnosis(scope or _scope(),
                                      measured_at=WINDOW_END,
                                      transcripts_visible=True)


def _class_result(report, contributor_class):
    for result in report.results:
        for class_result in result.classes:
            if class_result.contributor_class == contributor_class:
                return class_result
    raise AssertionError(f"{contributor_class} is absent from the report")


def _winner(report, contributor_class):
    rows = _class_result(report, contributor_class).rows
    return rows[0].subject_key if rows else None


def _value(report, contributor_class):
    rows = _class_result(report, contributor_class).rows
    return rows[0].observed_usd.value if rows else None


# --- the mutations ------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Mutation:
    name: str
    component: str
    contributor_class: str
    apply: object
    delete: object

    def __str__(self) -> str:
        return self.name


def _remodel(ns):
    """Rename the dominant model, which moves the model_mix winner."""
    conn = _cache_conn(ns)
    try:
        conn.execute(
            "UPDATE session_entries SET model = 'claude-sonnet-4-20250514' "
            "WHERE model = 'claude-opus-4-20250514'"
        )
        conn.commit()
    finally:
        conn.close()


def _reproject(ns):
    conn = _cache_conn(ns)
    try:
        conn.execute(
            "UPDATE session_files SET project_path = '/repo/gamma' "
            "WHERE project_path = '/repo/alpha'"
        )
        conn.commit()
    finally:
        conn.close()


def _resession(ns):
    conn = _cache_conn(ns)
    try:
        conn.execute(
            "UPDATE session_files SET session_id = 'sess-c' "
            "WHERE session_id = 'sess-a'"
        )
        conn.commit()
    finally:
        conn.close()


def _reblock(ns):
    """Move every block off the entries, which empties the burst class."""
    conn = _stats_conn(ns)
    try:
        conn.execute(
            "UPDATE five_hour_blocks SET block_start_at = '2020-01-01T00:00:00+00:00', "
            "five_hour_resets_at = '2020-01-01T05:00:00+00:00'"
        )
        conn.commit()
    finally:
        conn.close()


def _delete_entries(ns):
    conn = _cache_conn(ns)
    try:
        conn.execute("DELETE FROM session_entries")
        conn.commit()
    finally:
        conn.close()


def _delete_files(ns):
    conn = _cache_conn(ns)
    try:
        conn.execute("DELETE FROM session_files")
        conn.commit()
    finally:
        conn.close()


def _delete_blocks(ns):
    conn = _stats_conn(ns)
    try:
        conn.execute("DELETE FROM five_hour_blocks")
        conn.commit()
    finally:
        conn.close()


MUTATIONS = [
    _Mutation("model", "cache", "model_mix", _remodel, _delete_entries),
    _Mutation("project", "cache", "project_concentration", _reproject,
              _delete_files),
    # Deleting `session_files` does NOT delete the session evidence: the
    # adapter falls back to the source path, which is the production
    # behaviour the spec asks for ("unresolvable identities reduce
    # identityCoverage rather than being dropped"). Deleting the accounting
    # rows is what removes this class's evidence.
    _Mutation("session", "cache", "session_concentration", _resession,
              _delete_entries),
    _Mutation("block", "stats", "five_hour_bursts", _reblock, _delete_blocks),
]


@pytest.mark.parametrize("mutation", MUTATIONS, ids=str)
def test_mutating_one_signal_changes_the_diagnosis(mutation, seeded_store):
    before_gen = _generation()
    before = _diagnose()
    mutation.apply(seeded_store)
    after_gen = _generation()
    after = _diagnose()
    # The digest assertion is what stops this passing vacuously against an
    # input nothing actually reads.
    assert getattr(after_gen, mutation.component) != getattr(
        before_gen, mutation.component
    ), f"the {mutation.component} component digest did not move"
    changed = (
        _winner(after, mutation.contributor_class)
        != _winner(before, mutation.contributor_class)
        or _value(after, mutation.contributor_class)
        != _value(before, mutation.contributor_class)
        or _class_result(after, mutation.contributor_class).verdict
        != _class_result(before, mutation.contributor_class).verdict
    )
    assert changed, (
        f"{mutation.contributor_class} reported the same answer after its own "
        f"evidence changed"
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=str)
def test_deleting_the_evidence_never_yields_a_healthy_result(mutation,
                                                             seeded_store):
    mutation.delete(seeded_store)
    result = _class_result(_diagnose(), mutation.contributor_class)
    assert result.verdict in {
        kernel.VerdictState.WITHHELD.value,
        kernel.VerdictState.NOT_APPLICABLE.value,
    }, result
    assert result.verdict != kernel.VerdictState.NO_CONTRIBUTOR.value


def test_an_unresolvable_session_identity_reduces_coverage_rather_than_dropping(
    seeded_store,
):
    """Spec §3: a session whose identity does not resolve stays in the
    population with a lower identityCoverage. It is not silently discarded,
    and it is not a withheld class on its own."""
    _delete_files(seeded_store)
    result = _class_result(_diagnose(), "session_concentration")
    assert result.verdict in {"contributor", "no_contributor"}
    assert result.population.identity_coverage == 0.0
    assert result.population.support_units == 60


def test_a_mutation_that_touches_nothing_leaves_the_answer_alone(seeded_store):
    """The pair with the mutation tests above. Without it they would pass on a
    diagnosis that simply produced a different answer every run."""
    first = _diagnose()
    second = _diagnose()
    assert _winner(first, "model_mix") == _winner(second, "model_mix")
    assert _value(first, "model_mix") == _value(second, "model_mix")


def test_the_configuration_component_moves_on_its_own_axis(seeded_store):
    """Configuration is a generation component that no S2 verdict reads, so
    it is asserted on the digest alone rather than on the answer."""
    before = _generation()
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"display": {"tz": "utc"}, "marker": "moved"}) + "\n"
    )
    after = _generation()
    assert after.configuration != before.configuration
    assert after.cache == before.cache
    assert after.stats == before.stats


def test_the_generation_id_moves_with_any_component(seeded_store):
    scope = _scope()
    # The plan is part of the generation identity since #620 S3, so a caller
    # that hashes a vector states which plan produced it. Holding it constant
    # here is what keeps this test about the COMPONENT.
    plan = kernel.resolve_policy_plan(scope.source, transcripts_visible=True)
    before = _generation(scope).generation_id(scope, plan)
    _remodel(seeded_store)
    after = _generation(scope).generation_id(scope, plan)
    assert before != after


# --- failure injection --------------------------------------------------

def _raises(*_args, **_kwargs):
    raise RuntimeError("injected loader failure")


def test_a_loader_exception_is_never_rendered_as_healthy(seeded_store,
                                                         monkeypatch):
    """A caught-and-logged degrade passes every gate that checks only inputs."""
    monkeypatch.setattr(_sources(), "_load_class_facts_inner", _raises)
    report = _diagnose()
    result = _class_result(report, "model_mix")
    assert result.verdict == kernel.VerdictState.WITHHELD.value
    assert result.code == kernel.WithheldCause.CALCULATION_FAILED.value
    assert report.overall_verdict != "no_contributor_detected"


def test_an_exception_in_one_class_does_not_erase_the_others(seeded_store,
                                                             monkeypatch):
    real = _sources()._load_class_facts_inner

    def _selective(bundle, scope, spec):
        if spec.kind == "model_mix":
            raise RuntimeError("injected loader failure")
        return real(bundle, scope, spec)

    monkeypatch.setattr(_sources(), "_load_class_facts_inner", _selective)
    report = _diagnose()
    assert _class_result(report, "model_mix").code == "calculation_failed"
    assert _class_result(report, "project_concentration").verdict in {
        "contributor", "no_contributor",
    }


def test_a_store_that_cannot_be_opened_is_never_rendered_as_healthy(
        tmp_path, monkeypatch):
    """A store that cannot be opened withholds its provider and stays a
    failure: the report names `provider_unavailable` on every class and on the
    denominator, and `unreadable_store_is_terminal` is what the CLI turns into
    exit 3. What it must never do is read as an answer."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    report = _diagnose()
    assert report.overall_verdict == "withheld"
    assert report.overall_code == "provider_unavailable"
    assert kernel.unreadable_store_is_terminal(report) is True
    assert _class_result(report, "model_mix").code == "provider_unavailable"


# --- the rendered result ------------------------------------------------

def _run_cli(argv, *, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_AS_OF"] = "2026-08-17T00:00:00Z"
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env["TZ"] = "Etc/UTC"
    env.pop("CLAUDE_CONFIG_DIR", None)
    return subprocess.run([sys.executable, str(_BIN), *argv],
                          capture_output=True, text=True, env=env)


def _count_rendered_rows(out: str) -> int:
    return sum(1 for line in out.splitlines()
               if line.strip()[:2].rstrip(".").isdigit()
               and "of the denominator" in line)


def test_a_rendered_report_has_a_non_zero_row_count(seeded_store, tmp_path):
    """Assert the rendered result and a non-zero row count, not the stored
    inputs. A caught-and-logged degrade renders an empty report and passes
    every gate that checks only what went in."""
    run = _run_cli(["explain", "--window",
                    "2026-08-10..2026-08-16"], home=tmp_path)
    assert run.returncode == 0, run.stderr
    assert _count_rendered_rows(run.stdout) > 0, run.stdout


def test_the_rendered_report_falls_to_zero_rows_when_the_evidence_is_deleted(
    seeded_store, tmp_path,
):
    """The pair with the test above: it only means something if the counter
    can reach zero."""
    _delete_entries(seeded_store)
    run = _run_cli(["explain", "--window",
                    "2026-08-10..2026-08-16"], home=tmp_path)
    assert run.returncode == 0, run.stderr
    assert _count_rendered_rows(run.stdout) == 0, run.stdout
