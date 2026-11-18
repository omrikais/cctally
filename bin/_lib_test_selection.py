#!/usr/bin/env python3
"""The retained pytest selection (test regime reset, spec 4.7 steps 2-3).

Stdlib only. One list, SERIAL, names the tests that never run under xdist:
wall-clock bounds and tests that launch their own pytest trees, which under a
parallel leg measure scheduler contention instead of the code under test.
bin/cctally-test-all and the quick gate (bin/_lib_quick_gate.py,
bin/cctally-quick-gate) both read it from here.

`pytest_legs` builds the arguments of the two pytest legs, without the
interpreter prefix:

* the bulk leg runs `tests/` under xdist, minus every serial file (`--ignore`)
  and serial node (`--deselect`) the tree has, minus every quarantined id;
* the serial leg runs the serial entries the tree has, without xdist, minus
  every quarantined id; an entry wholly covered by a quarantined id is dropped.

An empty list means the selection emptied that leg, and the caller omits it.
Called with explicit `targets` (the quick gate), the bulk leg runs those
targets and deselects each serial target inside one of them, and the serial
leg runs `serial`.

CLI (exit 0; 1 for an unreadable quarantine list; 2 for bad usage):
    python3 bin/_lib_test_selection.py legs --repo DIR --jobs N --out DIR \\
        [--quarantine FILE]
writes `bulk` and `serial` under --out, each a NUL-separated argument list.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib_quarantine  # noqa: E402

# Entries may name mirror-private tests; a tree without a file skips its entry.
# The public-dependency guard reads a whole repo-relative path literal as a
# run-time reference, so the one whole-file private entry is named through its
# own small waived assignment, keeping the waiver off every other entry of the
# tuple (#1116).
_QUOTA_MUTATION_COVERAGE = 'tests/test_quota_mutation_coverage.py'  # mirror-private-ok: a mirror-private test; pytest_legs keeps only entries whose file exists
SERIAL: tuple[str, ...] = (
    'tests/test_rebuild_benchmark.py',
    'tests/test_bench_corpus_discriminators.py::test_the_session_fingerprint_backstop_fails_a_run_it_should_fail',
    'tests/test_stats_writer_storm_386.py::test_h1_multiwriter_baseline_stays_intact',
    'tests/test_stats_writer_storm_386.py::test_dashboard_source_reader_releases_rollback_snapshot',
    'tests/test_public_test_dep_closure.py::test_the_projected_public_tree_plans_and_collects',
    'tests/test_public_test_dep_closure.py::test_the_public_subset_runs_the_preflight_harness',
    _QUOTA_MUTATION_COVERAGE,
    # A wall-clock scaling ratio (test regime reset, Task 5.5): under -n 10 it
    # measured scheduler contention, not the planner.
    'tests/test_retention_planner_scaling.py',
    # A loaded bulk leg (21 workers, run 5afb86dffbdd8b11) took T1's cases past
    # their 360 s budget and killed three workers; away from that load (re-runs
    # at the leg's eased tail, an idle profile) a case took 78-109 s (#996).
    'tests/test_world_enumeration_oracle_f17.py::test_t1_finite_continuous_recovery',
    # At 21 workers on lan its [codex] case took 104-114 s of the 120 s cap, and
    # it is the likeliest trigger of three runs whose bulk leg lost one worker
    # and then stalled to its deadline (#996).
    'tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once',
    # It starts nested xdist sessions and kills their workers (#1086).
    'tests/test_xdist_recovery.py::test_worker_death_subprocess',
    # A nested pytest under a 100 s launch wait: 19 of 19 lan@21 bulk legs
    # killed it at that wait; it takes 39.7 s idle (#1082).
    'tests/test_quick_gate.py::test_every_source_a_rule_guard_scans_selects_it[tests/test_public_test_dep_closure.py]',
    # bin/cctally-update-test under an 85 s wait took 76-86 s at lan@21, where
    # 13 of 19 bulk legs failed it; it takes 26.6 s idle (#1082).
    'tests/test_test_temp_kernel.py::test_an_update_test_cleanup_failure_fails_the_harness_and_spares_hard_links',
)

COMMON: tuple[str, ...] = ('-q', '-rfE', '-p', 'no:cacheprovider')


def _contains(target: str, node: str) -> bool:
    """True when pytest `target` (a file or node) collects the narrower `node`."""
    return node.startswith(target + '::') or node.startswith(target + '[')


def _xdist(jobs: int) -> list[str]:
    # tests/_xdist_recovery.py corrects loadgroup's worker-death handling and
    # makes an unrecoverable death a classified failure (#1086).
    return ['-n', str(jobs), '--dist', 'loadgroup', '-p', 'tests._xdist_recovery'] if jobs else []


def pytest_legs(repo: Path, quarantine_ids: Sequence[str], *, jobs: int,
                targets: Sequence[str] | None = None,
                serial: Sequence[str] | None = None) -> tuple[list[str], list[str]]:
    """(bulk_argv, serial_argv) for `python3 -m pytest`; [] omits a leg.

    ``jobs`` is the bulk leg's xdist worker count, 0 for none (pytest-xdist
    absent). Without ``targets`` this is the full run over ``repo``'s tests/.
    """
    quarantined = list(quarantine_ids)
    deselect = [f'--deselect={q}' for q in quarantined]
    if targets is None:
        repo = Path(repo)
        present = [s for s in SERIAL if (repo / s.split('::')[0]).is_file()]
        excluded = [f'--ignore={s}' if '::' not in s else f'--deselect={s}' for s in present]
        bulk = [*COMMON, *_xdist(jobs), *excluded, *deselect, 'tests/']
        serial_targets = present
    else:
        serial_targets = list(serial or ())
        inside = [s for s in serial_targets if any(_contains(t, s) for t in targets)]
        bulk = ([*COMMON, *_xdist(jobs), *(f'--deselect={s}' for s in inside), *deselect,
                 *targets] if targets else [])
    kept = [s for s in serial_targets if not any(s.startswith(q) for q in quarantined)]
    serial_argv = [*COMMON, *deselect, *kept] if kept else []
    return bulk, serial_argv


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='_lib_test_selection.py')
    sub = parser.add_subparsers(dest='command', required=True)
    legs = sub.add_parser('legs')
    legs.add_argument('--repo', required=True)
    legs.add_argument('--jobs', type=int, required=True)
    legs.add_argument('--out', required=True)
    legs.add_argument('--quarantine')
    args = parser.parse_args(argv)
    ids: list[str] = []
    if args.quarantine:
        try:
            ids = _lib_quarantine.pytest_ids(_lib_quarantine.load_path(Path(args.quarantine)))
        except _lib_quarantine.QuarantineError as exc:
            print(f'quarantine: {exc}', file=sys.stderr)
            return 1
    bulk, serial = pytest_legs(Path(args.repo), ids, jobs=max(args.jobs, 0))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, leg in (('bulk', bulk), ('serial', serial)):
        (out / name).write_bytes(b''.join(a.encode() + b'\0' for a in leg))
    print(f'quarantined={len(ids)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
