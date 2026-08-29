"""A root-local estate-helper stub for the synthetic aggregator estates (#648 D10).

Four test families copy `bin/cctally-test-all` into a scratch tree and RUN it,
and since #648 the aggregator calls `bin/_lib_test_estate.py` at admission. Two
of those families select the kernels they copy as a CLASS — `BIN.glob(
"_lib_test_*.py")` at `tests/test_authoritative_test_contract.py` and
`tests/test_test_all_observability.py` — so they acquired the real checker
automatically while carrying neither the discovery kernel nor any committed
artifact, and every case in them failed on the fixture instead of on the
property it asserts. Measured after the admission call landed: 56 failures in
the contract family, 51 in the observability family and 1 in the scheduler
family.

WHAT IS STUBBED, AND WHAT IS NOT. This replaces the comparison against a
committed record, which a scratch tree has no way to satisfy. It does not
replace the aggregator's own handling of the result: the stub emits the same
tab-separated grammar the real checker does, so a case that wants drift, an
inability or an unauthorized transition writes that report and exercises the
real `contract_admit_estate` mapping.

The stub is also POSITIVE evidence that the check ran. It appends its argv to
`.estate-stub-invocations` in the tree root, so a mode assertion about a run
that must NOT check is an assertion about a file that does not exist rather
than about the absence of a diagnostic.
"""
from __future__ import annotations

import json
from pathlib import Path

INVOCATION_LOG = ".estate-stub-invocations"
REPORT_FIXTURE = "tests/estate-stub-report.txt"
PLAN_FIXTURE = "tests/estate-stub-plan.json"
EXIT_FIXTURE = "tests/estate-stub-exit"
PLAN_EXIT_FIXTURE = "tests/estate-stub-plan-exit"

_STUB = r"""#!/usr/bin/env python3
"@DOC@"
import json
import os
import sys

INVOCATION_LOG = "@LOG@"
REPORT_FIXTURE = "@REPORT@"
PLAN_FIXTURE = "@PLAN@"
EXIT_FIXTURE = "@EXIT@"
PLAN_EXIT_FIXTURE = "@PLANEXIT@"


def _value(name, default=None):
    if name in sys.argv:
        index = sys.argv.index(name)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


def main():
    repo = _value("--repo", os.getcwd())
    profile = _value("--profile", "public")
    with open(os.path.join(repo, INVOCATION_LOG), "a", encoding="utf-8") as log:
        log.write(" ".join(sys.argv[1:]) + "\n")

    plan_dir = _value("--plan-legs")
    if plan_dir is not None or "--validate-legs" in sys.argv:
        if plan_dir is not None:
            os.makedirs(plan_dir, exist_ok=True)
        path = os.path.join(repo, PLAN_FIXTURE)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                legs = json.load(handle)["legs"]
        else:
            # The DEFAULT declares a complement leg owning no node, plus a
            # benchmark leg IFF this tree carries the conventional benchmark
            # file. Probed at run time rather than at install time, because
            # several cases write that file after the estate is built.
            #
            # Probing the filesystem is exactly what #648 D7 removed from the
            # aggregator, and it is right HERE for the opposite reason: the
            # production path must refuse when a declared target vanished, and a
            # fixture that records no artifact has nothing to declare, so
            # reproducing the retired behaviour is what keeps every case about
            # the benchmark leg's REPORTING about that and not about a
            # declaration those cases never make.
            #
            # No declared nodes either way, so the collected-identifier
            # comparison stays vacuous: it asks whether every DECLARED node was
            # collected, and none is declared.
            legs = []
            benchmark = "tests/test_rebuild_benchmark.py"
            if os.path.exists(os.path.join(repo, benchmark)):
                legs.append({"name": "benchmark",
                             "selectors": [["file", benchmark]], "nodes": []})
            legs.append({"name": "pytest", "selectors": [], "nodes": []})
        for leg in legs:
            target = ""
            if plan_dir is not None:
                target = os.path.join(plan_dir, leg["name"] + ".txt")
                with open(target, "w", encoding="utf-8") as handle:
                    for node in leg.get("nodes") or []:
                        handle.write(node + "\n")
            print("leg\t%s\t%d\t%s"
                  % (leg["name"], len(leg.get("nodes") or []), target))
            for kind, selector in leg.get("selectors") or []:
                print("selector\t%s\t%s\t%s" % (leg["name"], kind, selector))
        print("end\tok")
        path = os.path.join(repo, PLAN_EXIT_FIXTURE)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                code = int(handle.read().strip() or "0")
            if code:
                return code

    if "--check" in sys.argv:
        path = os.path.join(repo, REPORT_FIXTURE)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                sys.stdout.write(handle.read())
        else:
            sys.stdout.write("version\t1\nprofile\t%s\nend\tok\n" % profile)

    path = os.path.join(repo, EXIT_FIXTURE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip() or "0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".replace("@DOC@", "Estate-helper stub (#648 D10). See tests/_estate_stub.py.") \
    .replace("@LOG@", INVOCATION_LOG).replace("@REPORT@", REPORT_FIXTURE) \
    .replace("@PLAN@", PLAN_FIXTURE).replace("@EXIT@", EXIT_FIXTURE) \
    .replace("@PLANEXIT@", PLAN_EXIT_FIXTURE)


def install(bindir, *, report=None, legs=None, exit_code=None,
            plan_exit_code=None):
    """Overwrite `bindir/_lib_test_estate.py` with the stub.

    Called AFTER the kernels are copied, because the class glob those families
    use has already put the real checker there.

    ``report`` is a verbatim report body; ``legs`` is the list of leg objects
    the ``--plan-legs`` mode should emit; ``exit_code`` makes the stub exit
    non-zero, which is how a case reaches the "produced no terminated report"
    branch of the contract's reader.
    """
    bindir = Path(bindir)
    repo = bindir.parent
    (bindir / "_lib_test_estate.py").write_text(_STUB, encoding="utf-8")
    if report is not None:
        _write(repo / REPORT_FIXTURE, report)
    if legs is not None:
        _write(repo / PLAN_FIXTURE, json.dumps({"legs": legs}, indent=2) + "\n")
    if exit_code is not None:
        _write(repo / EXIT_FIXTURE, "%d\n" % exit_code)
    if plan_exit_code is not None:
        _write(repo / PLAN_EXIT_FIXTURE, "%d\n" % plan_exit_code)


def invocations(repo):
    """Every argv the stub was called with, one per line; [] when never called."""
    path = Path(repo) / INVOCATION_LOG
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def clean_report(profile="public"):
    return "version\t1\nprofile\t%s\nend\tok\n" % profile


def drift_report(direction, axis, row, profile="public"):
    """A report naming one disagreement on one axis, in one direction."""
    return (
        "version\t1\nprofile\t%s\n"
        "finding\t%s\t%s\t1\t%s\n"
        "row\t%s\t%s\t%s\n"
        "end\tproblems\n" % (profile, direction, axis, row, direction, axis, row)
    )


def inability_report(tag, message, profile="public"):
    return (
        "version\t1\nprofile\t%s\ninability\t%s\t%s\nend\tproblems\n"
        % (profile, tag, message)
    )


def unauthorized_report(axis, token, kind="removed", profile="public"):
    return (
        "version\t1\nprofile\t%s\n"
        "unauthorized\t1\t%s\t%s\t%s\n"
        "uncovered\t%s\t%s\t%s\t%s\tunauthorized\n"
        "end\tproblems\n"
        % (profile, profile, axis, token, profile, axis, kind, token)
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
