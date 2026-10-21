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
  adds after that commit (§5.3a), and by nothing else;
- #968 adds exactly the Compressed Codex rollouts check and one OK count.
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
#: #825/#832 (spec section 6, 8) authorizes two further differences, each
#: asserted exactly: the stats epoch moves 1017 -> 1018 (the `db.version_ahead`
#: details and summary), and the journal category gains `journal.legacy_writer`
#: right after `journal.protocol`. On every fixture that leg is OK and reports
#: either an unavailable summary or no cutover yet.
# #968 adds cache migration 049; the registry views grow by that entry too.
BASE_STATS_EPOCH = 1017
ACCOUNT_BOUND_STATS_EPOCH = 1018
LEGACY_WRITER_ID = "journal.legacy_writer"
LEGACY_WRITER_AFTER = "journal.protocol"
LEGACY_WRITER_SUMMARIES = (
    "unavailable (no current admission summary)",
    "no account-bound cutover yet",
)


def _with_account_bound_epoch(check):
    """The baseline check as epoch 1018 renders it; others unchanged."""
    if check["id"] != "db.version_ahead":
        return check
    chk = copy.deepcopy(check)
    stats = (chk.get("details") or {}).get("stats.db")
    if stats and stats.get("epoch") == BASE_STATS_EPOCH:
        stats["epoch"] = ACCOUNT_BOUND_STATS_EPOCH
    chk["summary"] = chk["summary"].replace(
        f"epoch v{BASE_STATS_EPOCH}", f"epoch v{ACCOUNT_BOUND_STATS_EPOCH}")
    return chk


#: #968 (spec 5.6, 5.13): the `data` category gains exactly
#: `data.codex_compressed_rollouts`, right after `data.codex_prune_safety`.
#: No doctor fixture holds a compressed rollout, so it is OK on every one.
COMPRESSED_ID = "data.codex_compressed_rollouts"
COMPRESSED_AFTER = "data.codex_prune_safety"
COMPRESSED_CHECK = {
    "id": COMPRESSED_ID,
    "title": "Compressed Codex rollouts",
    "severity": "ok",
    "summary": "All compressed Codex rollouts are readable and verified.",
    "details": {"compressedOnly": 0, "retainedComplete": 0,
                "retainedIncomplete": 0, "neverIngested": 0,
                "unverified": 0, "verificationDebt": 0, "readDebt": 0,
                "pruneDebt": 0},
}


def _split_compressed_rollouts(categories):
    """``categories`` without the #968 check, which must equal COMPRESSED_CHECK."""
    out, found = [], []
    for cat in categories:
        checks = []
        for chk in cat["checks"]:
            if chk["id"] == COMPRESSED_ID:
                assert checks and checks[-1]["id"] == COMPRESSED_AFTER
                found.append(chk)
            else:
                checks.append(chk)
        out.append(dict(cat, checks=checks))
    assert found == [COMPRESSED_CHECK]
    return out

def _split_legacy_writer(categories):
    """``categories`` without the #825/#832 leg, and that leg."""
    found = []
    out = []
    for cat in categories:
        checks = []
        for chk in cat["checks"]:
            if chk["id"] == LEGACY_WRITER_ID:
                assert checks and checks[-1]["id"] == LEGACY_WRITER_AFTER
                found.append(chk)
            else:
                checks.append(chk)
        out.append(dict(cat, checks=checks))
    assert len(found) == 1
    return out, found[0]


#: #825/#832 (spec section 8, F16) also authorizes the `accounting` category
#: right after `accounts`, holding exactly `accounting.outcome_quarantine`.
#: Every doctor fixture lacks a validated current index, so the leg reports
#: unavailable — WARN, never OK — and adds exactly one WARN to the counts.
QUARANTINE_CATEGORY_ID = "accounting"
QUARANTINE_AFTER = "accounts"
QUARANTINE_CATEGORY = {
    "id": "accounting",
    "title": "Accounting",
    "severity": "warn",
    "checks": [{
        "id": "accounting.outcome_quarantine",
        "title": "Outcome quarantine",
        "severity": "warn",
        "summary": "unavailable (no validated stats index)",
        "remediation": ("Run any cctally command to bring the stats index up "
                        "to date, then run `cctally doctor` again."),
        "details": {"available": False, "accounts": []},
    }],
}


def _split_quarantine_category(categories):
    """``categories`` without the #825/#832 accounting category, which must
    sit right after `accounts` and equal `QUARANTINE_CATEGORY` exactly."""
    ids = [cat["id"] for cat in categories]
    index = ids.index(QUARANTINE_CATEGORY_ID)
    assert ids[index - 1] == QUARANTINE_AFTER
    assert categories[index] == QUARANTINE_CATEGORY
    return categories[:index] + categories[index + 1:]


BASE_CACHE_REGISTRY_SIZE = 47
ADDED_CACHE_MIGRATIONS = (
    "048_codex_quota_physical_group_order",
    "049_codex_rollout_containers",
)


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


#: Doctor scenarios added after the #901 baseline was frozen. Each is pinned by
#: its own goldens and has no baseline to differ from; naming them here keeps
#: an undeclared new scenario failing below.
#: #825/#832 (spec section 8, F9/F16): the two scenarios with a current
#: account-bound store, the only fixtures where `journal.legacy_writer` and
#: `accounting.outcome_quarantine` report real states rather than unavailable.
POST_BASELINE_SCENARIOS = ("40-account-bound-clear", "41-account-bound-legacy-write")


def test_baseline_covers_every_doctor_scenario():
    current = sorted(p.name for p in CURRENT.iterdir() if (p / "expected.json").is_file())
    assert set(POST_BASELINE_SCENARIOS) <= set(current)
    assert SCENARIOS and SCENARIOS == [
        name for name in current if name not in POST_BASELINE_SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_only_the_authorized_doctor_differences(scenario):
    base = _load(BASELINE / f"{scenario}.json")
    cur_full = _load(CURRENT / scenario / "expected.json")
    cur = dict(cur_full)
    cur["categories"], legacy_writer = _split_legacy_writer(
        _split_quarantine_category(_split_compressed_rollouts(cur_full["categories"])))
    assert legacy_writer["severity"] == "ok"
    assert legacy_writer["title"] == "Legacy writer"
    assert legacy_writer["summary"] in LEGACY_WRITER_SUMMARIES

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
                assert c_chk == _with_account_bound_epoch(
                    _with_registry_growth(b_chk))
    assert remapped == 1

    # Counts, severity and the identity slice follow from those verdicts alone.
    expected_counts = dict(base["overall"]["counts"])
    expected_counts["ok"] = expected_counts.get("ok", 0) + 3
    expected_counts["warn"] = expected_counts.get("warn", 0) + 1
    assert cur["overall"]["counts"] == expected_counts
    # Every fixture already carries a WARN or FAIL, so the roll-up holds.
    assert cur["overall"]["severity"] == base["overall"]["severity"]

    expected_categories = []
    for cat in base["categories"] + [NEW_CATEGORY]:
        checks = []
        for chk in cat["checks"]:
            checks.append(chk)
            if chk["id"] == LEGACY_WRITER_AFTER:
                checks.append(legacy_writer)
            if chk["id"] == COMPRESSED_AFTER:
                checks.append(COMPRESSED_CHECK)
        expected_categories.append(dict(cat, checks=checks))
        if cat["id"] == QUARANTINE_AFTER:
            expected_categories.append(QUARANTINE_CATEGORY)
    expected = dict(base, overall={"counts": expected_counts,
                                   "severity": base["overall"]["severity"]},
                    categories=expected_categories)
    assert doctor.fingerprint(_report(cur_full)) == doctor.fingerprint(
        _report(expected))
    assert doctor.fingerprint(_report(cur_full)) != doctor.fingerprint(
        _report(base))


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
