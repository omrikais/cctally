"""Per-test durations for an authoritative run (#630 S1, F3).

Loaded with `-p tests._pytest_durations_plugin` by bin/cctally-test-all on
authoritative evidence runs only. Never enabled through pytest.ini or
conftest.py: both would change behaviour for anyone running pytest directly,
and conftest.py belongs to another session.

xdist propagates `-p` to workers, so the writer is disabled in any process
that carries `workerinput`. The controller receives every worker's serialized
setup/call/teardown report, which is the same population pytest's own
durations summary aggregates.

The underscore prefix keeps this file out of collection and out of the
`test_*.py` estate scan. There is deliberately no `tests/__init__.py`: both
pytest invocations run `python3 -m pytest` from the repository root, so `-m`
puts that root on `sys.path` and `tests` resolves as a namespace package.
"""
import gzip
import json
import math
import os
import time

_OUT_ENV = "CCTALLY_DURATIONS_PATH"
_LEG_ENV = "CCTALLY_DURATIONS_LEG"

# Level 6 rather than gzip's default 9: the artifact enters the evidence
# retention budget, and 9 buys a few percent for a large multiple of the CPU
# on a file written while the suite is still running.
_COMPRESS_LEVEL = 6


def _writer_is_disabled(config):
    return hasattr(config, "workerinput") or not os.environ.get(_OUT_ENV)


def _exit_status_int(exitstatus):
    """pytest hands over an `ExitCode`, an int, or (rarely) a string.

    Recorded as a plain int so the artifact stays readable by a consumer that
    knows nothing about pytest's enum; an unrecognisable value is recorded as
    None, which the merge treats as a footer that cannot vouch for its own
    population.
    """
    try:
        return int(exitstatus)
    except (TypeError, ValueError):
        return None


def _report_timestamp(report, attribute):
    """Keep a report's epoch seconds, or explicitly retain the unknown value."""
    value = getattr(report, attribute, None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value):
            return value
    return None


class _DurationsWriter:
    """Holds the open handle.

    A module-level `pytest_runtest_logreport(report)` receives only the report
    and cannot reach the config that owns the file, so the writer is an
    instance registered at configure time. `_writer_is_disabled` stays
    module-level so the worker guard can be called directly by a test.
    """

    def __init__(self, path, leg, distributed=False):
        self._distributed = distributed
        self._lifecycle = {
            "configuredAt": time.time(),
            "sessionStartedAt": None,
            "collectionStartedAt": None,
            "collectionFinishedAt": None,
            "sessionFinishedAt": None,
        }
        self._handle = gzip.open(
            path, "wt", encoding="utf-8", compresslevel=_COMPRESS_LEVEL
        )
        self._leg = leg
        self._count = 0

    def pytest_sessionstart(self, session):
        self._lifecycle["sessionStartedAt"] = time.time()

    def pytest_collection(self, session):
        # xdist's controller skips local collection; its collection hook is a
        # dispatch boundary, not evidence of when workers collected tests.
        if not self._distributed:
            self._lifecycle["collectionStartedAt"] = time.time()

    def pytest_collection_finish(self, session):
        if not self._distributed:
            self._lifecycle["collectionFinishedAt"] = time.time()

    def pytest_runtest_logreport(self, report):
        worker_id = getattr(report, "worker_id", None)
        self._handle.write(json.dumps({
            "nodeId": report.nodeid,
            "phase": report.when,
            "durationSeconds": round(report.duration, 6),
            "outcome": report.outcome,
            "leg": self._leg,
            "workerId": worker_id if isinstance(worker_id, str) and worker_id else None,
            "startedAt": _report_timestamp(report, "start"),
            "finishedAt": _report_timestamp(report, "stop"),
        }) + "\n")
        self._count += 1

    def pytest_sessionfinish(self, session, exitstatus):
        self._lifecycle["sessionFinishedAt"] = time.time()
        # The footer is the completeness signal the merge reads, and it needs
        # BOTH facts. A leg whose process was killed outright — a worker
        # crash, the aggregator's own TERM-then-KILL of the pytest process
        # group — never reaches this hook, so its file ends without a footer.
        # But reaching the hook is not the same as having run the collected
        # tests: `_pytest.main.wrap_session` calls `pytest_sessionfinish` from
        # its `finally` block on every path where `initstate >= 2`, including
        # after the `except BaseException` arm that sets INTERNAL_ERROR and
        # after a KeyboardInterrupt. So the real exit status is recorded here
        # and the merge decides completeness from it. Writing an unconditional
        # `sessionFinished: True` and discarding `exitstatus` published
        # `complete: true` over a truncated population.
        self._handle.write(json.dumps({
            "footer": True,
            "leg": self._leg,
            "records": self._count,
            "sessionFinished": True,
            "exitStatus": _exit_status_int(exitstatus),
            "lifecycle": self._lifecycle,
        }) + "\n")
        self._handle.close()


def pytest_configure(config):
    if _writer_is_disabled(config):
        return
    config.pluginmanager.register(
        _DurationsWriter(os.environ[_OUT_ENV],
                         os.environ.get(_LEG_ENV, "pytest"),
                         distributed=bool(getattr(getattr(config, "option", None),
                                                  "numprocesses", None))),
        name="cctally-durations-writer",
    )
