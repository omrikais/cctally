"""The aggregator-owner environment family (#966(c), spec 4.3, J7).

Owner-only variables belong to the bin/cctally-test-all invocation that set
them. They are removed at its shell-harness and pytest launch boundaries, at
the root conftest's import, and in every nested-aggregator environment builder;
every other CCTALLY_* name it reads is classified as deliberately inherited.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_OWNER_PREFIXES = ("CCTALLY_TEST_ALL_", "CCTALLY_TEST_EVIDENCE_")
_OWNER_NAMES = {"CCTALLY_TEST_RUN_ID", "CCTALLY_REMOTE_DIR", "_CCTALLY_HARNESS_SCRUB"}
#: #965's test retention switch (bin/_cctally_config.py
#: CONVERSATION_RETENTION_TEST_SWITCH; bin/_lib_test_isolation.py RETENTION_SWITCH).
#: Execution policy that every harness and pytest leg must keep inheriting.
_RETENTION_SWITCH = "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF"
_DUMP = (
    "import json, os\n"
    "def test_dump():\n"
    "    with open(os.environ['E1_CONFTEST_DUMP'], 'w') as fh:\n"
    "        json.dump(sorted(os.environ), fh)\n"
)


def test_the_root_conftest_scrubs_owner_only_variables_at_import(tmp_path):
    """A DIRECT pytest run (no aggregator boundary in front of it) that inherited
    the owner family still cannot hand it to anything it launches."""
    import _temp_hygiene as _hyg

    module = tmp_path / "test_dump_env.py"
    module.write_text(_DUMP, encoding="utf-8")
    dump = tmp_path / "env.json"
    sentinel = tmp_path / "parent-outcome.json"
    env = {
        "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", str(tmp_path)),
        "PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO / "bin")]),
        "E1_CONFTEST_DUMP": str(dump),
        "CCTALLY_TEST_ALL_OUTCOME_FILE": str(sentinel),
        "CCTALLY_TEST_RUN_ID": "e1-conftest",
        "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev"),
        "CCTALLY_TEST_JOBS": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-q", "-p", "no:cacheprovider",
         *_hyg.nested_basetemp_args(tmp_path), str(module)],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=110)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = json.loads(dump.read_text(encoding="utf-8"))
    leaked = sorted(n for n in names if n.startswith(_OWNER_PREFIXES) or n in _OWNER_NAMES)
    assert leaked == [], "owner-only variables survived the root conftest's import: %s" % leaked
    assert "CCTALLY_TEST_JOBS" in names
    assert not sentinel.exists()


def _referenced(paths):
    names = set()
    for path in paths:
        names |= set(re.findall(r"\b_?CCTALLY_[A-Z0-9_]+", path.read_text(encoding="utf-8")))
    return names


def test_every_aggregator_control_variable_is_classified():
    """Spec 4.3: every variable bin/cctally-test-all or its contract library
    reads is owner-only or deliberately inherited, never both and never neither.
    A new control variable fails here until it is classified."""
    import _lib_test_generation as G

    names = _referenced([REPO / "bin" / "cctally-test-all", REPO / "bin" / "_lib-test-contract.sh"])
    unclassified = sorted(n for n in names
                          if not G.is_owner_only(n) and n not in G.DELIBERATELY_INHERITED)
    assert unclassified == [], "unclassified aggregator variables: %s" % unclassified
    both = sorted(n for n in G.DELIBERATELY_INHERITED if G.is_owner_only(n))
    assert both == [], both
    assert set(G.OWNER_ONLY_NAMES) == _OWNER_NAMES
    assert tuple(G.OWNER_ONLY_PREFIXES) == _OWNER_PREFIXES
    # Never the whole namespace (spec 4.3).
    assert not G.is_owner_only("CCTALLY_TEST_JOBS") and not G.is_owner_only("CCTALLY_DATA_DIR")


def test_the_owner_family_is_named_in_full_and_scrubbed_in_place():
    import _lib_test_generation as G

    listed = G.owner_only_names({"CCTALLY_TEST_ALL_X": "1", "CCTALLY_TEST_JOBS": "2"})
    assert set(listed) == _OWNER_NAMES | {"CCTALLY_TEST_ALL_X"}, listed
    env = {"CCTALLY_TEST_ALL_OUTCOME_FILE": "/p", "CCTALLY_TEST_EVIDENCE_ROOT": "/e",
           "CCTALLY_REMOTE_DIR": "r", "CCTALLY_TEST_JOBS": "1", "PATH": "/usr/bin"}
    assert G.scrub_owner_env(env) is env
    assert env == {"CCTALLY_TEST_JOBS": "1", "PATH": "/usr/bin"}
    out = subprocess.run([sys.executable, "-B", str(REPO / "bin" / "_lib_test_generation.py"),
                          "owner-env-names"], capture_output=True, text=True, timeout=60,
                         env={"PATH": os.environ["PATH"], "CCTALLY_TEST_ALL_Y": "1"})
    assert out.returncode == 0 and set(out.stdout.split()) == _OWNER_NAMES | {"CCTALLY_TEST_ALL_Y"}


def test_every_deliberately_inherited_variable_survives_every_scrub(tmp_path):
    """Spec 4.3's inheritance regression: each deliberately inherited name,
    #965's retention switch among them, passes through the classification, the
    in-place scrub, the nested-aggregator builder and the aggregator's scrub
    list unchanged, while the owner family beside it is removed."""
    import _lib_test_generation as G
    import _temp_hygiene as _hyg

    assert _RETENTION_SWITCH in G.DELIBERATELY_INHERITED
    inherited = {name: "inherited-%d" % i for i, name in enumerate(sorted(G.DELIBERATELY_INHERITED))}
    owner = {"CCTALLY_TEST_ALL_OUTCOME_FILE": "/parent-outcome.json", "CCTALLY_TEST_RUN_ID": "r",
             "CCTALLY_TEST_EVIDENCE_ROOT": "/ev", "CCTALLY_REMOTE_DIR": "d"}
    assert [n for n in inherited if G.is_owner_only(n)] == []
    assert G.scrub_owner_env(dict(inherited, **owner)) == inherited
    child = _hyg.child_aggregator_env(dict(inherited, **owner), tmp_path)
    assert {n: child.get(n) for n in inherited} == inherited
    assert sorted(set(owner) & set(child)) == []
    out = subprocess.run([sys.executable, "-B", str(REPO / "bin" / "_lib_test_generation.py"),
                          "owner-env-names"], capture_output=True, text=True, timeout=60,
                         env=dict(inherited, PATH=os.environ["PATH"]))
    assert out.returncode == 0, out.stderr
    assert sorted(set(out.stdout.split()) & set(inherited)) == []


def test_the_aggregator_scrubs_at_both_launch_boundaries():
    """The two boundaries in bin/cctally-test-all consume the classification:
    run_one builds its `env -u` list from _CCTALLY_HARNESS_SCRUB, and both pytest
    legs run through pytest_env_sanitize, which carries OWNER_SCRUB."""
    text = (REPO / "bin" / "cctally-test-all").read_text(encoding="utf-8")
    run_one = text[text.index("run_one() {"):text.index("export -f run_one")]
    assert "_CCTALLY_HARNESS_SCRUB" in run_one and '"${scrub[@]}"' in run_one
    sanitize = re.search(r"pytest_env_sanitize=\(([^)]*)\)", text).group(1)
    assert "OWNER_SCRUB" in sanitize, sanitize
