#!/usr/bin/env bash
# Shared harness environment preamble (dev-instance isolation, 2026-05-26).
#
# Sourced by EVERY bin/cctally-*-test harness — directly (standalone
# harnesses) or transitively (bin/_lib-fixture-harness.sh sources it).
# Exports the suppressor that forces _cctally_core's dev-checkout
# auto-detect OFF, so a harness running bin/cctally from this git
# checkout resolves the PROD data-dir layout under its fake HOME
# (…/cctally), not the dev layout (…/cctally-dev). Without it, every
# harness would relocate its data dir and its golden diff would fail.
#
# Coverage is enforced by tests/test_harness_dev_autodetect_coverage.py.
export CCTALLY_DISABLE_DEV_AUTODETECT=1

# Fixture commands run against short-lived fake HOME directories. Suppress
# post-command update and telemetry workers centrally: both are detached, so a
# harness can otherwise remove its scratch tree while a worker is still
# creating marker files or SQLite sidecars inside it. That race presents as
# intermittent `disk I/O error` / `Directory not empty` failures under CI
# parallelism. The flag gates the post-command hook only; update/telemetry
# command handlers and doctor state resolution remain testable directly.
export CCTALLY_DISABLE_UPDATE_CHECK=1

# #496 S6: the artifact-retention sweep is the THIRD detached post-command
# worker, and the paragraph above applies to it verbatim — it writes an
# admission marker, a daily stamp and a log into the data directory after the
# command it followed has already exited. `bin/cctally-rederive-test`
# fingerprints the WHOLE data directory before and after a no-op apply and
# requires byte equality, so an asynchronous writer makes that assertion
# unmeetable; it failed on every run once ordinary mutating commands began
# scheduling sweeps. The flag gates the post-command admission hook only, so
# `cctally db prune`, the worker entry point and the admission predicate all
# stay directly testable — and they are, in tests/test_retention_worker.py.
export CCTALLY_DISABLE_RETENTION_SWEEP=1

# Issue #108: cctally now honors $CODEX_HOME. Neutralize a dev's exported
# value so it can't leak into codex-* goldens (the codex harnesses pin HOME
# to a fake tree whose .codex/sessions the fixtures populate).
unset CODEX_HOME

# Anonymous install-count telemetry (spec 2026-07-07): the `doctor` report
# now carries a telemetry-state line whose resolved reason reads these env
# opt-outs. Neutralize a dev's exported values so a maintainer with
# DO_NOT_TRACK / CCTALLY_DISABLE_TELEMETRY set in their shell regenerates the
# same goldens CI produces (both resolve to "enabled" under the suppressed
# dev-checkout, since the harness also forces CCTALLY_DISABLE_DEV_AUTODETECT).
unset DO_NOT_TRACK
unset CCTALLY_DISABLE_TELEMETRY
