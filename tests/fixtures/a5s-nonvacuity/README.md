# A5s non-vacuity patches (#940 spec revision 7j, section 6.7)

The retention instrument is the direct write accounting in `tests/_sqlite_write_accounting.py` (a wrapping SQLite VFS that counts every byte SQLite writes, per file class, on each measured connection) and its four consumers: `tests/test_retention_spill_free.py`, `tests/test_retention_charge_coverage.py`, `tests/test_retention_reclaim_plan.py` and `tests/_retention_fixtures.py`. The instrument needs no calibrated root and no volume-specific counter. Each of the seven reversible patches below is a mutation the instrument must detect: applied to a clean tree, it must make every target node in the table fail with its designated outcome and failure text. The patch files are never committed in their applied state; the repository always carries the unpatched code, and a patch is applied only for the duration of one qualification run.

`tests/_a5s_mutation_runner.py` runs them. It first runs the unpatched target nodes, which must all pass. Then for each patch it applies the patch, runs that patch's target nodes with the lane's test command (pytest's `-rA -vv` short summary is appended), reverses the patch and requires a clean tree. A patch counts as detected only when pytest exits 1 and every target node reports its designated outcome carrying its designated failure text. A missing, passing, skipped or uncollected node, and any other exit (collection, usage, internal or transport failure), fails qualification. `tests/test_a5s_nonvacuity_patches.py` proves hermetically that each patch still applies to the current tree and reverts byte for byte; it does not run them. A child-process case is named by the parent node that runs the child.

Run all seven from the repository root on a clean tree, for example on Ubuntu:

    python3 tests/_a5s_mutation_runner.py -- python3 -m pytest -p no:cacheprovider

and on the Darwin runners through the remote wrapper:

    python3 tests/_a5s_mutation_runner.py -- bin/cctally-test-remote python3 -m pytest -p no:cacheprovider

## Patches

| Patch | Mutation |
|---|---|
| `01-product-temp-store-file.patch` | The product's deletion connection and its spill-free group lose `temp_store=MEMORY` (both set `FILE`). The pragma check that would catch this first is neutralised in the patched copy, so the run reaches the temp-byte assertion. |
| `02-fallback-charge-below-its-writes.patch` | The FILE fallback is charged 1 KiB per row instead of 50 KiB, so its writes exceed its charged allowance. |
| `03-default-control-does-not-spill.patch` | The default control keeps its statement journal in memory (`temp_store=MEMORY`), so it no longer spills. The validity guard that requires the default control to write a temp file must reject the run. |
| `04-blind-accounting.patch` | The accounting VFS counts no write at all, the failure mode of the retired logical counter on the bare external volume. The positive control of every measured interval must reject it. |
| `05-accounting-unavailable.patch` | The accounting VFS cannot be registered. Every consumer must fail at setup, and a subprocess writer must not publish evidence. |
| `06-measured-connection-bypasses.patch` | A measured connection is opened outside the accounting VFS: the spill-free deletion connection and the reclaim connection before activation, the crash child's connection before activation, and, in the `[reclaim-16]` coverage node, the foreign checkpointer through an explicit inner VFS (that node's pinned variant runs before its crash variant, so the other coverage nodes reach the crash child instead). Each attribution check must reject it. |
| `07-extent-only-accumulator.patch` | The accounting counts only the growth of each file's extent instead of every write, so rewrites of already-written pages are lost. The cumulative-accounting test must reject it. |

## Per-node targets

The mutation runner reads this table. Every cell is a code span; `Outcome` is `FAILED`, or `ERROR` for a designated setup failure, and `Failure text` must appear in the node's short-summary message.

| Patch | Node | Outcome | Failure text |
|---|---|---|---|
| `01-product-temp-store-file.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[claude]` | `FAILED` | `the product deletion wrote a temp file` |
| `01-product-temp-store-file.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[codex]` | `FAILED` | `the product deletion wrote a temp file` |
| `02-fallback-charge-below-its-writes.patch` | `tests/test_retention_spill_free.py::test_the_fallback_actually_spills_and_stays_within_its_bound` | `FAILED` | `the fallback's writes exceed its charge` |
| `02-fallback-charge-below-its-writes.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[delete-fallback-300]` | `FAILED` | `coverage: raise the failing reservation part` |
| `03-default-control-does-not-spill.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[claude]` | `FAILED` | `validity: the default control's statement journal reaches a temp file` |
| `03-default-control-does-not-spill.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[codex]` | `FAILED` | `validity: the default control's statement journal reaches a temp file` |
| `04-blind-accounting.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[claude]` | `FAILED` | `write accounting positive control failed` |
| `04-blind-accounting.patch` | `tests/test_retention_spill_free.py::test_the_fallback_actually_spills_and_stays_within_its_bound` | `FAILED` | `write accounting positive control failed` |
| `04-blind-accounting.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[delete-fallback-300]` | `FAILED` | `write accounting positive control failed` |
| `04-blind-accounting.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[reclaim-1]` | `FAILED` | `write accounting positive control failed` |
| `04-blind-accounting.patch` | `tests/test_retention_reclaim_plan.py::test_an_interior_relocation_chunk_is_covered_by_its_own_charge` | `FAILED` | `write accounting positive control failed` |
| `05-accounting-unavailable.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[claude]` | `ERROR` | `write accounting unavailable` |
| `05-accounting-unavailable.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[reclaim-1]` | `ERROR` | `write accounting unavailable` |
| `05-accounting-unavailable.patch` | `tests/test_retention_reclaim_plan.py::test_an_interior_relocation_chunk_is_covered_by_its_own_charge` | `ERROR` | `write accounting unavailable` |
| `05-accounting-unavailable.patch` | `tests/test_sqlite_write_accounting.py::test_a_subprocess_writer_publishes_validated_counters` | `FAILED` | `child writer: child write evidence missing` |
| `06-measured-connection-bypasses.patch` | `tests/test_retention_spill_free.py::test_spill_free_deletion_writes_each_dirty_page_once[claude]` | `FAILED` | `deletion connection: the connection bypasses the write accounting VFS` |
| `06-measured-connection-bypasses.patch` | `tests/test_retention_reclaim_plan.py::test_an_interior_relocation_chunk_is_covered_by_its_own_charge` | `FAILED` | `reclaim connection: the connection bypasses the write accounting VFS` |
| `06-measured-connection-bypasses.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[reclaim-1]` | `FAILED` | `crash child: the connection bypasses the write accounting VFS` |
| `06-measured-connection-bypasses.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[delete-50]` | `FAILED` | `crash child: the connection bypasses the write accounting VFS` |
| `06-measured-connection-bypasses.patch` | `tests/test_retention_charge_coverage.py::test_the_charge_covers_every_attributable_write[reclaim-16]` | `FAILED` | `foreign checkpoint: the connection bypasses the write accounting VFS` |
| `07-extent-only-accumulator.patch` | `tests/test_sqlite_write_accounting.py::test_cumulative_accounting_counts_every_write_not_the_file_extent` | `FAILED` | `cumulative accounting: TEMP must be the sum of every write` |

Where patches 03 and 04 are rejected. In revision 7j's feasibility probe (spec section 2.8), blind accounting (mutation 04) failed at the spill-free validity guards, because the probe had no positive control. The maintained instrument starts every measured interval with a positive control, a known delete-on-close TEMP_DB file that must be counted at exactly its size, so blind accounting is rejected there first: every patch-04 row names `write accounting positive control failed`. The validity guards rejecting a run is what patch 03's targets show: with the default control's statement journal held in memory, the guard that requires the default control to write a temp file fails, as their rows state.

The revision 7k Q8 nodes are no patch's targets. Their non-vacuity is in the module itself: the mismatch controls must be rejected by the `secure_delete` precondition, and the clustered negative fixture must fail the unchanged dominance guard (`tests/test_retention_spill_free.py`).

## Patch 03 under the retired logical counter (historical)

Before revision 7j the instrument read a logical write counter, and patch 03 was recorded as a sensitivity diagnostic rather than a mutation: with the default control's statement journal in memory there was no temp file, and the guard value (the default control's logical bytes minus its WAL frames and the unbounded control's own non-temp excess) was the difference of two non-temp excesses, which varied by several hundred KiB from run to run around the 64 KiB slack. The direct accounting measures the temp class itself, so the guard now compares the default control's TEMP bytes with the slack and the patched control writes none; patch 03 is a mutation the runner must detect on every lane.

The logical-counter guard values, in bytes, measured through the wrapper on the two Darwin runners:

| Run | `[claude]` unpatched | `[claude]` patch 03 | `[codex]` unpatched | `[codex]` patch 03 |
|---|---|---|---|---|
| runner B, when D3 landed | — | above 65,536 (passed) | — | −439,456 |
| runner B, D-2 fix round | 30,053,292 | −48,224 | 111,348,606 | 69,264 (passed) |
| runner A, D-2 fix round (two patched runs) | 30,118,828 | −27,744 / −31,840 | 109,951,876 | −774,504 / −602,472 |

No log of the first row's run survives. Its `[codex]` figure, −439,456, comes only from the D3 implementation report, which recorded that run; it was transcribed into this README by the D-2 review's documentation correction and has no other source, so treat the row as unverified. Unpatched, the default controls spilled about 30 MB (Claude, 6,000-row group) and about 110 MB (Codex, 24,000 rows) to a temp file, 460 and 1,700 times the slack.
