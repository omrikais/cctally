"""#901 §4.6: the only doctor differences are the new disk-writes check and the
remapped reclaim check, asserted exactly and separately.

Each doctor scenario's current JSON golden is compared with its frozen copy at
Owner A's final commit (tests/fixtures/901/doctor-baseline/). Required:
- the categories before the new one are unchanged, check by check, except
  db.conversations_reclaimable, whose verdict is the remap's (`ok` on every
  fixture: below the start threshold) and whose summary/details may change;
- exactly one category is appended, holding exactly the new check;
- counts, overall severity and the identity slice (hence the envelope's
  fingerprint) are exactly what those two verdicts imply;
- the migration-registry views grow by exactly the cache migration revision 9
  adds after that commit (§5.3a), and by nothing else.
A whole-golden refresh cannot pass this: any other byte difference fails.
"""

import copy
import importlib
import json
import os
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")
sys.path.insert(0, BIN)
doctor = importlib.import_module("_lib_doctor")

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "fixtures" / "901" / "doctor-baseline"
CURRENT = ROOT / "tests" / "fixtures" / "doctor"
SCENARIOS = sorted(p.stem for p in BASELINE.glob("*.json"))

NEW_CHECK_ID = "performance.dashboard_disk_writes"
REMAPPED_ID = "db.conversations_reclaimable"
NEW_CATEGORY = {
    "id": "performance",
    "title": "Performance",
    "severity": "ok",
    "checks": [{
        "id": NEW_CHECK_ID,
        "title": "Dashboard disk writes",
        "severity": "ok",
        "summary": "No dashboard is running.",
        "details": {
            "policy_version": 1,
            "mode": "discovery",
            "instances": [],
            "maintenance": {
                "charged_bytes": 0,
                "largest_charge_bytes": 0,
                "window_start": "2026-05-12T14:00:00Z",
                "window_end": "2026-05-13T14:22:31Z",
                "window_minutes": 1462,
                "allowance_bytes": 6136266752,
                "verdict": "ok",
            },
        },
    }],
}


# Revision 9 (§5.3a, 901-PA-001 a) adds one cache migration after Owner A's
# final commit. The doctor's registry views must grow by exactly that entry.
BASE_CACHE_REGISTRY_SIZE = 47
ADDED_CACHE_MIGRATIONS = ("048_codex_quota_physical_group_order",)


def _with_registry_growth(check):
    """The baseline check as the grown registry must render it; any other
    check is returned unchanged."""
    grown = BASE_CACHE_REGISTRY_SIZE + len(ADDED_CACHE_MIGRATIONS)
    n = len(ADDED_CACHE_MIGRATIONS)
    chk = copy.deepcopy(check)
    details = chk.get("details") or {}
    if chk["id"] == "db.cache.file":
        if "registry_size" in details:
            assert details["registry_size"] == BASE_CACHE_REGISTRY_SIZE
            details["registry_size"] = grown
        chk["summary"] = chk["summary"].replace(
            f"/ {BASE_CACHE_REGISTRY_SIZE} known", f"/ {grown} known")
    elif chk["id"] == "db.version_ahead":
        cache = details.get("cache.db")
        if cache and "registry_size" in cache:
            assert cache["registry_size"] == BASE_CACHE_REGISTRY_SIZE
            cache["registry_size"] = grown
        chk["summary"] = chk["summary"].replace(
            f"known v{BASE_CACHE_REGISTRY_SIZE}", f"known v{grown}")
    elif chk["id"] == "db.migrations.applied":
        cache = details["by_db"]["cache.db"]
        assert not set(ADDED_CACHE_MIGRATIONS) & set(
            cache["applied"] + cache["failed"] + cache["pending"])
        cache["pending"] = cache["pending"] + list(ADDED_CACHE_MIGRATIONS)
        chk["summary"] = re.sub(
            r"(\d+)/(\d+) applied",
            lambda m: f"{m.group(1)}/{int(m.group(2)) + n} applied", chk["summary"])
    elif chk["id"] == "db.migrations.pending":
        pending = details["pending"]
        last_cache = max(i for i, (db, _) in enumerate(pending) if db == "cache.db")
        details["pending"] = (pending[:last_cache + 1]
                              + [["cache.db", name] for name in ADDED_CACHE_MIGRATIONS]
                              + pending[last_cache + 1:])
        chk["summary"] = re.sub(
            r"^(\d+) pending", lambda m: f"{int(m.group(1)) + n} pending", chk["summary"])
    return chk


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _report(doc):
    """A duck-typed DoctorReport over a JSON golden, for _lib_doctor.fingerprint."""
    return SimpleNamespace(
        schema_version=doc["schema_version"],
        overall_severity=doc["overall"]["severity"],
        counts=doc["overall"]["counts"],
        categories=[
            SimpleNamespace(checks=[SimpleNamespace(id=c["id"], severity=c["severity"])
                                    for c in cat["checks"]])
            for cat in doc["categories"]
        ],
    )


def test_baseline_covers_every_doctor_scenario():
    current = sorted(p.name for p in CURRENT.iterdir() if (p / "expected.json").is_file())
    assert SCENARIOS and SCENARIOS == current


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_only_the_authorized_doctor_differences(scenario):
    base = _load(BASELINE / f"{scenario}.json")
    cur = _load(CURRENT / scenario / "expected.json")

    assert {k: v for k, v in cur.items() if k not in ("categories", "overall")} == \
        {k: v for k, v in base.items() if k not in ("categories", "overall")}

    # Exactly one appended category, holding exactly the new check.
    assert len(cur["categories"]) == len(base["categories"]) + 1
    assert cur["categories"][-1] == NEW_CATEGORY
    assert all(c["id"] != NEW_CHECK_ID
               for cat in base["categories"] for c in cat["checks"])

    # Every earlier category and check is unchanged, except the remap's
    # summary and details; the remapped verdict on these fixtures is `ok`.
    remapped = 0
    for b_cat, c_cat in zip(base["categories"], cur["categories"]):
        assert {k: v for k, v in c_cat.items() if k != "checks"} == \
            {k: v for k, v in b_cat.items() if k != "checks"}
        assert [c["id"] for c in c_cat["checks"]] == [c["id"] for c in b_cat["checks"]]
        for b_chk, c_chk in zip(b_cat["checks"], c_cat["checks"]):
            if c_chk["id"] == REMAPPED_ID:
                remapped += 1
                assert c_chk["severity"] == "ok"
                assert (c_chk["id"], c_chk["title"]) == (b_chk["id"], b_chk["title"])
                assert c_chk.get("remediation") == b_chk.get("remediation")
            else:
                assert c_chk == _with_registry_growth(b_chk)
    assert remapped == 1

    # Counts, severity and the identity slice follow from those verdicts alone.
    expected_counts = dict(base["overall"]["counts"])
    expected_counts["ok"] = expected_counts.get("ok", 0) + 1
    assert cur["overall"]["counts"] == expected_counts
    assert cur["overall"]["severity"] == base["overall"]["severity"]

    expected = dict(base, overall={"counts": expected_counts,
                                   "severity": base["overall"]["severity"]},
                    categories=base["categories"] + [NEW_CATEGORY])
    assert doctor.fingerprint(_report(cur)) == doctor.fingerprint(_report(expected))
    assert doctor.fingerprint(_report(cur)) != doctor.fingerprint(_report(base))


def test_a_warning_new_check_alone_turns_the_envelope_severity_warn():
    """§4.6: severity becomes `warn` only when the new check is WARN and no
    other check is WARN or FAIL. Drives the product's own roll-up
    (`_max_severity`, as `run_checks` applies it per category and overall).

    No doctor fixture is all-OK (each carries WARN or FAIL checks), so the
    all-OK install is the 01-all-ok scenario's check layout with every
    verdict set to `ok`; its real verdicts then show that the new check
    changes nothing when another check already warns."""
    base = _load(BASELINE / "01-all-ok.json")
    real = [[c["severity"] for c in cat["checks"]] for cat in base["categories"]]
    assert doctor._max_severity([doctor._max_severity(v) for v in real]) == \
        base["overall"]["severity"] == "warn"
    all_ok = [["ok"] * len(v) for v in real]
    assert doctor._max_severity([doctor._max_severity(v) for v in all_ok]) == "ok"
    with_new_warn = all_ok + [["warn"]]
    assert doctor._max_severity([doctor._max_severity(v) for v in with_new_warn]) == "warn"
    with_new_ok = all_ok + [["ok"]]
    assert doctor._max_severity([doctor._max_severity(v) for v in with_new_ok]) == "ok"
    for new in ("ok", "warn"):
        assert doctor._max_severity(
            [doctor._max_severity(v) for v in real + [[new]]]) == "warn"
