"""What each pytest execution leg ACTUALLY collected (#648 D7).

Loaded with `-p tests._estate_leg_plugin` by bin/cctally-test-all on both legs.
Never enabled through pytest.ini or conftest.py: both would change behaviour for
anyone running pytest directly.

WHY THIS EXISTS AT ALL. #648 D7 builds each leg's argv from the artifact's one
`pytestExecution` declaration, which proves what pytest was ASKED to collect.
It does not prove what pytest DID collect, and spec M13 measured that gap on
the tree as it stood: `PYTEST_ADDOPTS=--ignore=tests/<file>.py` narrowed an
authoritative run while admission observed the complete estate, and the run
exited 0. The environment route is closed now — the aggregator removes that
variable from both legs — but a conftest hook, a plugin, or a future option can
narrow a collection just as quietly, so the observation is recorded rather than
inferred.

ONE FILE PER PROCESS, UNIONED BY THE SHELL. Under `pytest -n` the WORKERS
collect and the controller does not: its session never reaches this hook with
real items, so a controller-only writer would record an empty set on exactly the
configuration the authoritative gate runs. Each process therefore writes
`<dir>/<worker>.txt` and bin/cctally-test-all unions the directory. The union is
the right aggregation because every worker collects the same full set before
xdist distributes it, so a worker that died without writing costs nothing while
a run where NO file appears fails closed.

`trylast=True` puts this hook after pytest's own deselection, which is the whole
point: `--deselect` is applied in `pytest_collection_modifyitems`, and a hook
that ran first would record the pre-deselection list and call a narrowed leg
complete.

The underscore prefix keeps this file out of collection and out of the
`test_*.py` estate scan. There is deliberately no `tests/__init__.py`: both
pytest invocations run `python3 -m pytest` from the repository root, so `-m`
puts that root on `sys.path` and `tests` resolves as a namespace package.
"""
import os

import pytest

OBSERVED_DIR_ENV = "CCTALLY_ESTATE_OBSERVED_DIR"


def _observed_path():
    """The file this process writes, or None when the aggregator did not ask.

    Absent is not empty. A direct `python3 -m pytest` run sets nothing and must
    behave exactly as it did before this plugin existed, so the writer is
    disabled rather than writing a record nobody asked for into the tree.
    """
    directory = os.environ.get(OBSERVED_DIR_ENV)
    if not directory:
        return None
    worker = os.environ.get("PYTEST_XDIST_WORKER") or "controller"
    return os.path.join(directory, worker + ".txt")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    path = _observed_path()
    if path is None:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Written whole, including when `items` is empty: an empty file is the
    # record that this process collected nothing, which is a different fact
    # from no file at all and the shell treats the two differently.
    with open(path, "w", encoding="utf-8") as handle:
        for item in items:
            handle.write(item.nodeid + "\n")
