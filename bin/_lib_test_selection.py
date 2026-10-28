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

# Two entries name mirror-private files; the mirror's privacy guard classifies
# paths, not contents, and a tree without a file skips its entry.
SERIAL: tuple[str, ...] = (
    'tests/test_rebuild_benchmark.py',
    'tests/test_bench_corpus_discriminators.py::test_the_session_fingerprint_backstop_fails_a_run_it_should_fail',
    'tests/test_stats_writer_storm_386.py::test_h1_multiwriter_baseline_stays_intact',
    'tests/test_stats_writer_storm_386.py::test_dashboard_source_reader_releases_rollback_snapshot',
    'tests/test_public_test_dep_closure.py::test_the_projected_public_tree_plans_and_collects',
    'tests/test_public_test_dep_closure.py::test_the_public_subset_runs_the_preflight_harness',
    'tests/test_quota_mutation_coverage.py',
    # A wall-clock scaling ratio (test regime reset, Task 5.5): under -n 10 it
    # measured scheduler contention, not the planner.
    'tests/test_retention_planner_scaling.py',
)

COMMON: tuple[str, ...] = ('-q', '-rfE', '-p', 'no:cacheprovider')


def _contains(target: str, node: str) -> bool:
    """True when pytest `target` (a file or node) collects the narrower `node`."""
    return node.startswith(target + '::') or node.startswith(target + '[')


def _xdist(jobs: int) -> list[str]:
    return ['-n', str(jobs), '--dist', 'loadgroup'] if jobs else []


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
