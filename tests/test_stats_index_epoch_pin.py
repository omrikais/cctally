"""The one reviewed pin of `STATS_INDEX_EPOCH` (#1057).

Failure detected (install/upgrade): an epoch change that was not made
deliberately, for example a merge resolution that picks another branch's
epoch. Every epoch change costs every installed user one full stats index
rebuild on upgrade (bounded by #997, about an hour on the maintainer's store),
so it must be visible and reviewed. This module is listed in `CORE_PYTEST`
(`bin/_lib_quick_gate.py`), so every change to `bin/_cctally_core.py` runs it,
and a bump that forgets it fails at the quick gate.

This is the only test that pins the current epoch by value. Elsewhere a test
that observes a store or envelope the current binary produced compares it
with `_cctally_core.STATS_INDEX_EPOCH`, and a test of a feature that arrived at
a given epoch asserts a fixed lower bound (`>= 10NN`). Do not add another
`== <current epoch>` literal in a test module: an owned or heavy module
re-pinned at every bump drags its whole row into the bump's quick gate.

Bump checklist
--------------
An epoch-only bump to N edits exactly these partners; each has a tripwire the
bump's own quick-gate selection runs:

- constant: `STATS_INDEX_EPOCH = N` in `bin/_cctally_core.py`, plus its
  history entry in the comment above it. No regenerate step.
- pin: `REVIEWED_STATS_INDEX_EPOCH = N` below. Fails when missing:
  this module (core row).
- doctor fallback: `epoch = N` in `bin/_lib_doctor.py` (the value a
  hand-built `DoctorState` without an injected epoch reports). Fails when
  missing:
  `tests/test_selector_state_496_s5b.py::test_the_doctor_fallback_epoch_matches_the_constant`
  (site-inventories).
- doctor goldens: every rendered epoch under `tests/fixtures/doctor/*/` —
  `"epoch": N` in each `expected.json`, `'epoch': N` in each
  `expected.verbose.txt`, `epoch vN` in the `20-stats-version-ahead` and
  `28-stats-mismatch-with-journal` summaries across all four modes
  (`expected.json` summary, `expected.txt`, `expected.quiet.txt`,
  `expected.verbose.txt`; a whole-word search for the number misses these),
  and the `40-account-bound-clear` `user_version: N` and `version N / 13
  known` lines. Regenerate on the runner, setting the variable inside the
  payload (the runner client does not forward it):
  `bin/cctally-test-remote --export checkout/tests/fixtures/doctor=<dest> --
  bash -c 'CCTALLY_DOCTOR_REGENERATE=1 bin/cctally-doctor-test'`, then copy
  the `expected.*` files back (the export drops each `setup.sh`'s executable
  bit, so keep those unchanged). Fails when missing: `bin/cctally-doctor-test`
  (doctor row, selected through the bump's own `bin/_lib_doctor.py` edit; a
  bump that forgets only the goldens changes no fixture path, so the fixture
  convention does not fire).
- dashboard stores: the 20 committed
  `tests/fixtures/dashboard/*/.local/share/cctally/stats.db` at
  `user_version` N. Regenerate on the runner with
  `bin/cctally-test-remote --export out/dash=<dest> -- bash -c 'python3
  bin/build-dashboard-fixtures.py --out ../out/dash'` (the export source is
  relative to the run directory, which holds `checkout/` and `out/`), then
  copy back each `stats.db` whose committed `user_version` is the old epoch.
  Fails when missing:
  `tests/test_fixture_builder_contract.py` `[dashboard]` (whole-tree).
- registry shapes: the `sql_shape` of each detector-access site in
  `bin/_lib_detector_access_registry.py` whose only changed dependency is the
  binding `_cctally_core.STATS_INDEX_EPOCH`, with a reviewed re-pin note.
  Regenerate with `bin/cctally-test-remote python3
  tests/_detector_access_callgraph.py --base <sha>` and explain each changed
  dependency (the registry's module docstring). Fails when missing:
  `tests/test_detector_access_registry.py` (site-inventories).

Re-read obligations
-------------------
The per-module epoch tripwires this pin replaced made each owner look at every
bump; these obligations now live here:

- A stats schema change is an epoch bump, never a stats migration: the stats
  registry is frozen and `LEGACY_STATS_HEAD` stays 13, because an
  epoch-current open returns before any schema work.
- `DERIVATION_FRONTIER_EPOCH` moves independently of this constant, and only
  with a derivation-fence change (`tests/test_account_bound_revision2_797.py`).
- A bump that changes `quota_window_blocks` grouping re-reads the #416 epoch
  analysis kept at `tests/test_codex_window_canonicalization.py` ("Task 10
  Step 6"). #416 Slice 2 changed no stats schema (its only schema change was
  cache migration 032) and therefore did not bump.
- A schema change also moves `_REBUILD_SCHEMA_FINGERPRINT`
  (`bin/_cctally_journal.py`); an epoch-only bump does not.

Epoch history
-------------
The full per-epoch record is the comment above `STATS_INDEX_EPOCH` in
`bin/_cctally_core.py`. The #416 tripwire this pin absorbed recorded the
chain since then: public #5's incremental projector (the reverse map and
composable group digest on `quota_window_blocks`, the
`quota_projection_ledger_state` row) moved 1004 -> 1005 -> 1006, #460 to 1007,
#496 S3 to 1008 (`stats_publication_stamp`), #496 S5b to 1009 (durable
selector state), #538 to 1010 (the rollback-journal transition, no schema
change), #661 S2 to 1011 (`meter_rate_change_events`), #750 S2 to 1012 (its
rate-change disclosure columns), #750 S3 to 1013 (reset-event origin identity
and the transactional debounce state), #769 S2 to 1014 (source-local
five-hour credit confirmation state), #769 S11 to 1015
(`weekly_usage_snapshots.weekly_observation_held`), #869 to 1016
(`five_hour_blocks.pricing_provenance_json`), #901 to 1017 (read indexes),
#825/#832 to 1018 (the account-bound journal fence), #797/#823/#840 to 1019
(its revision 2), #1019/#1014 to 1020 (its revision 3), #943 to 1021, #1015 to
1022, #948 to 1023 and the #1018 twin to 1024.
"""
from __future__ import annotations

import _cctally_core

REVIEWED_STATS_INDEX_EPOCH = 1024


def test_the_stats_index_epoch_is_the_reviewed_value():
    assert _cctally_core.STATS_INDEX_EPOCH == REVIEWED_STATS_INDEX_EPOCH
