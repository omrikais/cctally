"""The aggregator-owner environment family (#966(c), spec 4.3, J7).

bin/cctally-test-all classifies every control variable it or its contract
library reads as owner-only or deliberately inherited. Owner-only variables
describe ONE invocation and are removed at every boundary that could hand them
to another run: the aggregator's harness and pytest launch boundaries
(``owner-env-names`` prints the list it scrubs), the root conftest's import
and every nested-aggregator environment builder (``scrub_owner_env``).

The suite generation this module used to manage (a marked temp root with
recorded process groups, a descendant token, a sweep and a 24-hour retention)
is gone (test regime reset, session 4): the runner supervisor's per-run
directory bounds a run's temp, and the aggregator's own suite directory
lives and dies inside it.

Stdlib only and Python 3.9-parseable.
"""
from __future__ import annotations

import argparse
import os
import sys


# --- the aggregator-owner environment family (#966(c), spec 4.3, J7) --------
#: Owner-only: they describe ONE bin/cctally-test-all invocation (its outcome
#: record, its run id, its evidence store and flags) and are removed at every
#: boundary that could hand them to another run.
OWNER_ONLY_PREFIXES = ("CCTALLY_TEST_ALL_", "CCTALLY_TEST_EVIDENCE_")
OWNER_ONLY_NAMES = frozenset({
    "CCTALLY_TEST_RUN_ID", "CCTALLY_REMOTE_DIR",
    "_CCTALLY_HARNESS_SCRUB",          # the scrub list itself
})
#: Deliberately inherited, by spec 4.3's preserved classes; the readers of each
#: are in the plan's population (c). A new control variable is added to one of
#: the two sets, and tests/test_owner_environment.py fails until it is.
DELIBERATELY_INHERITED = frozenset({
    # execution policy
    "CCTALLY_AGENTMEM_TEST_POLICY", "CCTALLY_AUTHORITATIVE_RUN", "CCTALLY_DOCTOR_REGENERATE",
    "CCTALLY_PROGRESS_INTERVAL", "CCTALLY_PYTEST_DURATIONS",
    "CCTALLY_PYTEST_TIMEOUT", "CCTALLY_REMOTE_EXEC",
    # golden regeneration switches (CONTRACT_REGENERATION_VARIABLES in
    # bin/_lib-test-contract.sh): admission must SEE one to refuse it, and a
    # local run that sets one deliberately must reach the harness that reads it.
    "CCTALLY_MIGRATIONS_REGENERATE", "CCTALLY_PRICING_CHECK_REGENERATE",
    "CCTALLY_REGEN_GOLDENS", "CCTALLY_SETUP_REGENERATE", "CCTALLY_UPDATE_TEST_REGEN",
    # #965's test retention switch (bin/_cctally_config.py, bin/_lib_test_isolation.py):
    # the aggregator and every harness export it, and the isolation layer
    # refuses a covered launch that lacks it, so every child must inherit it.
    "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF",
    # parallelism
    "CCTALLY_TEST_JOBS", "CCTALLY_RELEASE_TEST_JOBS", "CCTALLY_OUTER_JOBS",
    "CCTALLY_INNER_JOBS", "CCTALLY_PYTEST_JOBS",
    # supervision: the runner supervisor's run directory, which a private-tree
    # Darwin run requires to contain TMPDIR (test regime reset, session 4)
    "CCTALLY_RUNNER_RUN_DIR",
    # isolation
    "CCTALLY_DISABLE_DEV_AUTODETECT",
    # leg-owned output channels
    "CCTALLY_DURATIONS_PATH", "CCTALLY_DURATIONS_LEG",
})


def is_owner_only(name):
    return name in OWNER_ONLY_NAMES or name.startswith(OWNER_ONLY_PREFIXES)


def owner_only_names(environ=None):
    """Every fixed owner-only name, plus every prefixed one present, sorted."""
    environ = os.environ if environ is None else environ
    return sorted(OWNER_ONLY_NAMES | set(n for n in environ if is_owner_only(n)))


def scrub_owner_env(env):
    """Remove every owner-only variable from ``env`` in place (``os.environ``
    included) and return it. Apply BEFORE a caller's explicit overrides."""
    for name in [n for n in env if is_owner_only(n)]:
        del env[name]
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(prog="_lib_test_generation.py")
    sub = parser.add_subparsers(dest="verb")
    sub.add_parser("owner-env-names")
    args = parser.parse_args(argv)          # a usage error exits 2 here
    if args.verb is None:
        parser.print_usage(sys.stderr)
        return 2
    print(" ".join(owner_only_names()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
