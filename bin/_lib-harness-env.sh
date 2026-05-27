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

# Issue #108: cctally now honors $CODEX_HOME. Neutralize a dev's exported
# value so it can't leak into codex-* goldens (the codex harnesses pin HOME
# to a fake tree whose .codex/sessions the fixtures populate).
unset CODEX_HOME
