"""``cctally account attribute`` end to end (#500 Task 3).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``
§4, §4.1, §7, §7.2, §8, §8.5, §9, §10, and acceptance criteria AC1-AC16.

Everything here drives the REAL CLI as a subprocess against a real data
directory, because the thing under test is a command: its parser, its exit
codes, its envelope, and the durability of what it writes across
``db rebuild --db stats`` and ``cache-sync --rebuild --source codex``.

The store is built from real Codex rollout JSONL under a fake ``CODEX_HOME``,
never by hand-inserting cache rows. That is not fastidiousness — a store seeded
by SQL cannot survive ``cache-sync --rebuild --source codex`` at all, so AC7
would be untestable against it, and ``docs/codex-gotchas.md`` records an
implementation that passed 1,870 tests while stamping zero rows in production
precisely because its fixture could not tell working from not working.

Density, per spec §10: one Codex root, 32 weekly window groups, three jittered
raw resets per group so the tolerance-connected component matching is genuinely
exercised, one group carrying a real conflicting account through a durable
per-file decision, one Spark model-scoped pool, one 5-hour window, spend rows
backed by the same retained rollout files, and unattributed neighbours
overlapping the selected group.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess

import pytest

UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin" / "cctally"

WEEK_MINUTES = 10_080
FIVE_HOUR_MINUTES = 300
WEEKS = 32

#: The reset anchor of group 0. Every other weekly group is a multiple of seven
#: days behind it, offset by a per-group hour of jitter so neighbouring nominal
#: weeks OVERLAP — which is what makes the spend-adoption pass discriminate
#: rather than stamp everything it scans.
BASE_RESET = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=UTC)

TARGET_GROUP = 5          # entirely unattributed; the spend rows live here
NATIVE_GROUP = 2          # a real conflicting account, via a durable file decision
DECOY_SPEND_GROUP = 20    # never selected; its spend must stay unattributed
SPLIT_GROUP = 12          # wide raw spellings; the only group that can SPLIT

#: The jitter offsets of one group's three provider spellings, in seconds.
#:
#: `SPLIT_GROUP` gets the wide set and every other group the narrow one. Both
#: form ONE tolerance-connected component while all three members exist, because
#: `_lib_quota.ResetAnchorComponents` unions transitively and each ADJACENT pair
#: of the wide set is inside `CODEX_RESET_ANCHOR_TOLERANCE_SECONDS = 600`. They
#: differ in what happens when the middle witness goes away: the wide set's two
#: survivors are 1,000s apart and re-derive as two components, while the narrow
#: set's survivors are 13s apart and stay one. A stored canonical anchor is what
#: the group key is built from, so two components is the only state that can
#: present an assertion with more than one candidate group.
_WIDE_OFFSETS = (0, 500, 1000)
_NARROW_OFFSETS = (0, 6, 13)

#: The groups the acceptance run selects. Contiguous, and bounded away from
#: their neighbours by nearly seven days, so a one-day pad on each side cannot
#: clip a neighbouring group and turn it into a partial-coverage refusal.
SELECTED = tuple(range(3, 9))

CODEX_ACCOUNT_ID = "acct-work"
CODEX_EMAIL = "work@example.com"
NATIVE_ACCOUNT_ID = "acct-native"
NATIVE_EMAIL = "native@example.com"


# ── the fixture ──────────────────────────────────────────────────────────────

def _b64(obj) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _auth_json(account_id: str, email: str) -> str:
    payload = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }
    token = f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(payload)}.sig"
    return json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": token, "access_token": "a", "refresh_token": "r"},
        "last_refresh": "2026-07-20T00:00:00Z",
    })


def _account_key(account_id: str, email: str) -> str:
    import _lib_accounts

    return _lib_accounts.account_key("codex", account_id + "\0" + email)


def _z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def reset_at(index: int) -> dt.datetime:
    """Group ``index``'s canonical reset. Higher index is further in the past."""
    return (BASE_RESET - dt.timedelta(days=7 * index)
            + dt.timedelta(hours=index % 5))


def raw_resets(index: int) -> tuple[dt.datetime, ...]:
    """Three jittered provider spellings of one physical reset."""
    base = reset_at(index)
    offsets = _WIDE_OFFSETS if index == SPLIT_GROUP else _NARROW_OFFSETS
    return tuple(base + dt.timedelta(seconds=offset) for offset in offsets)


def captures(index: int) -> tuple[dt.datetime, ...]:
    """The three capture instants of group ``index``, latest first.

    All three sit in a two-hour cluster six days before the reset, so the
    clusters of two adjacent groups are nearly seven days apart and a selector
    padded by a day cannot straddle one.
    """
    base = reset_at(index) - dt.timedelta(days=6)
    return tuple(base - dt.timedelta(hours=k) for k in (0, 1, 2))


def _session_meta(ts: dt.datetime, session_id: str, thread_id: str) -> dict:
    return {
        "timestamp": _z(ts), "type": "session_meta",
        "payload": {
            "id": thread_id, "session_id": session_id, "source": "codex",
            "thread_source": thread_id, "cwd": "/synthetic/project",
            "model": "gpt-5", "instructions": "synthetic",
        },
    }


def _turn_context(ts: dt.datetime, model: str) -> dict:
    return {
        "timestamp": _z(ts), "type": "turn_context",
        "payload": {"model": model, "turn_id": f"turn-{_z(ts)}"},
    }


def _quota_event(ts: dt.datetime, *, reset: dt.datetime, used_percent: float,
                 window_minutes: int = WEEK_MINUTES, limit_name=None) -> dict:
    """A ``token_count`` event carrying quota evidence and NO spend.

    Quota and spend are deliberately never emitted from the same record, so the
    observation count and the accounting-row count are independent numbers a
    test can assert exactly.
    """
    return {
        "timestamp": _z(ts), "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "rate_limits": {
                    "limit_id": "codex",
                    "limit_name": limit_name,
                    "plan_type": "pro",
                    "primary": {
                        "used_percent": float(used_percent),
                        "window_minutes": int(window_minutes),
                        "resets_at": int(reset.timestamp()),
                    },
                },
            },
        },
    }


def _spend_event(ts: dt.datetime, tokens: int) -> dict:
    """A ``token_count`` event carrying spend and NO quota evidence."""
    return {
        "timestamp": _z(ts), "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": tokens, "cached_input_tokens": tokens // 4,
                    "output_tokens": tokens // 8, "reasoning_output_tokens": 0,
                    "total_tokens": tokens + tokens // 8,
                },
                "total_token_usage": {"total_tokens": tokens},
            },
        },
    }


def _write_rollout(home: pathlib.Path, name: str, records: list[dict]) -> pathlib.Path:
    path = home / "sessions" / "2026" / "08" / "01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(rec, sort_keys=True) + "\n"
                for rec in sorted(records, key=lambda r: r["timestamp"])))
    return path


def _native_records() -> list[dict]:
    """The rollout ingested while ``auth.json`` names the native account."""
    records = [_session_meta(captures(NATIVE_GROUP)[-1] - dt.timedelta(minutes=5),
                             "22222222-2222-4222-8222-222222222222", "native")]
    # Earliest capture first, paired with the BASE raw spelling, so
    # "first sight wins" resolves the canonical anchor onto `reset_at(index)`
    # and the test can name a group without re-deriving the anchor rule.
    for slot, (capture, raw) in enumerate(
            zip(reversed(captures(NATIVE_GROUP)), raw_resets(NATIVE_GROUP))):
        records.append(_quota_event(
            capture, reset=raw, used_percent=10.0 + slot))
    # One accounting row inside that window, which the same durable per-file
    # decision stamps with the native account.
    records.append(_spend_event(
        reset_at(NATIVE_GROUP) - dt.timedelta(days=3), 40_000))
    return records


def _main_records(*, drop_split_bridge: bool = False) -> list[dict]:
    """Everything else, ingested with no ``auth.json`` — so unattributed.

    ``drop_split_bridge`` omits ``SPLIT_GROUP``'s middle raw spelling, which is
    the member whose presence unions that group's two outer spellings into one
    tolerance-connected component. Re-ingesting without it is how a test reaches
    the SPLIT resolution outcome through the real anchor derivation rather than
    by editing a stored anchor by hand.
    """
    records = [_session_meta(captures(WEEKS - 1)[-1] - dt.timedelta(minutes=5),
                             "11111111-1111-4111-8111-111111111111", "main")]
    for index in range(WEEKS):
        if index == NATIVE_GROUP:
            continue
        for slot, (capture, raw) in enumerate(
                zip(reversed(captures(index)), raw_resets(index))):
            if drop_split_bridge and index == SPLIT_GROUP and slot == 1:
                continue
            records.append(_quota_event(
                capture, reset=raw, used_percent=float(1 + slot + index % 7)))
    # A Spark model-scoped pool, ahead of the weekly series. The preceding
    # `turn_context` is what makes `_lib_codex_pools` classify it as a separate
    # pool rather than account weekly quota.
    spark_capture = BASE_RESET + dt.timedelta(days=13)
    records.append(_turn_context(spark_capture - dt.timedelta(minutes=1),
                                 "gpt-5.3-codex-spark"))
    records.append(_quota_event(
        spark_capture, reset=BASE_RESET + dt.timedelta(days=14),
        used_percent=33.0, limit_name="gpt-5.3-codex-spark"))
    records.append(_turn_context(spark_capture + dt.timedelta(minutes=1), "gpt-5"))
    # A 5-hour window, which is not account weekly quota either.
    records.append(_quota_event(
        BASE_RESET + dt.timedelta(hours=1),
        reset=BASE_RESET + dt.timedelta(hours=3), used_percent=8.0,
        window_minutes=FIVE_HOUR_MINUTES))
    # Six accounting rows in the INTERIOR of the target group's nominal week.
    target_start = reset_at(TARGET_GROUP) - dt.timedelta(minutes=WEEK_MINUTES)
    for offset in range(6):
        records.append(_spend_event(
            target_start + dt.timedelta(days=2, hours=offset),
            100_000 + offset * 1_000))
    # One decoy far outside any selected group.
    records.append(_spend_event(
        reset_at(DECOY_SPEND_GROUP) - dt.timedelta(days=3), 7_000))
    return records


def _cli(app_dir: pathlib.Path, codex_home: pathlib.Path, *args: str,
         extra_env=None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CODEX_HOME": str(codex_home),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "TZ": "Etc/UTC",
        **(extra_env or {}),
    }
    # Under the 120s pytest-timeout cap `bin/cctally-test-all` applies, so the
    # budget can actually fire and name the hung command instead of the test
    # dying first on a generic timeout.
    return subprocess.run(
        [str(BIN), *args], cwd=REPO_ROOT, env=env, text=True,
        capture_output=True, timeout=110, check=False,
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A built store plus the handles every test here needs."""
    from conftest import load_script, redirect_paths

    data = tmp_path / "data"
    ns = load_script()
    redirect_paths(ns, monkeypatch, data)
    app_dir = ns["_cctally_core"].APP_DIR

    home = tmp_path / "codex-provider"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))

    # Phase A: the native rollout, ingested while auth.json names its account.
    _write_rollout(home, "rollout-native.jsonl", _native_records())
    (home / "auth.json").write_text(_auth_json(NATIVE_ACCOUNT_ID, NATIVE_EMAIL))
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    # Phase B: everything else, with no auth.json, so it lands unattributed.
    # The native file's account is now a DURABLE per-file decision, so it keeps
    # its account through this sync and through both rebuilds.
    (home / "auth.json").unlink()
    _write_rollout(home, "rollout-main.jsonl", _main_records())
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    # The account the operator will assert has been observed at some point but
    # owns nothing yet — the ordinary shape for a second Codex account.
    import _cctally_journal as jr
    import _lib_journal as lj

    jr.append_record(lj.make_account_observe(
        at="2026-07-24T00:00:00Z", account_key=_account_key(
            CODEX_ACCOUNT_ID, CODEX_EMAIL),
        provider="codex", natural_id=CODEX_ACCOUNT_ID, email=CODEX_EMAIL,
        plan_type="pro", label="work", label_source="auto"))
    jr.append_record(lj.make_account_observe(
        at="2026-07-24T00:00:00Z", account_key="claude-only-account-key-0001",
        provider="claude", natural_id="uuid-claude", email="me@example.com",
        plan_type="max", label="mainclaude", label_source="auto"))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    return _Store(ns=ns, app_dir=app_dir, home=home,
                  account=_account_key(CODEX_ACCOUNT_ID, CODEX_EMAIL),
                  native=_account_key(NATIVE_ACCOUNT_ID, NATIVE_EMAIL))


class _Store:
    def __init__(self, *, ns, app_dir, home, account, native):
        self.ns = ns
        self.app_dir = app_dir
        self.home = home
        self.account = account
        self.native = native

    def cli(self, *args, extra_env=None):
        return _cli(self.app_dir, self.home, *args, extra_env=extra_env)

    def attribute(self, *args, ref="work", extra_env=None):
        return self.cli("account", "attribute", ref, *args, extra_env=extra_env)

    def json_attribute(self, *args, ref="work", extra_env=None):
        proc = self.attribute(*args, "--json", ref=ref, extra_env=extra_env)
        return proc, (json.loads(proc.stdout) if proc.stdout.strip() else None)

    # ── observation helpers ──────────────────────────────────────────────
    def _cache(self):
        return sqlite3.connect(str(self.app_dir / "cache.db"))

    def _stats(self):
        return sqlite3.connect(str(self.app_dir / "stats.db"))

    def entry_accounts(self):
        conn = self._cache()
        try:
            return [
                (str(row[0]), row[1]) for row in conn.execute(
                    "SELECT timestamp_utc, account_key FROM "
                    "codex_session_entries ORDER BY timestamp_utc, line_offset")
            ]
        finally:
            conn.close()

    def block_accounts(self):
        """Live block rows as ``{reset_iso: {account_key, ...}}``.

        ``orphaned_at IS NULL`` is the read model's own predicate: the sweep
        MARKS an obsolete block rather than deleting it, so a query without it
        would report a swept row as live.
        """
        conn = self._stats()
        try:
            out: dict = {}
            for reset, account in conn.execute(
                "SELECT resets_at_utc, account_key FROM quota_window_blocks "
                " WHERE source='codex' AND orphaned_at IS NULL"
            ):
                out.setdefault(str(reset), set()).add(account)
            return out
        finally:
            conn.close()

    def milestone_accounts(self):
        conn = self._stats()
        try:
            out: dict = {}
            for reset, account in conn.execute(
                "SELECT resets_at_utc, account_key FROM "
                "quota_percent_milestones "
                " WHERE source='codex' AND orphaned_at IS NULL"
            ):
                out.setdefault(str(reset), set()).add(account)
            return out
        finally:
            conn.close()

    def attribution_rows(self):
        conn = self._cache()
        try:
            return [
                (str(row[0]), str(row[1]), row[2]) for row in conn.execute(
                    "SELECT op_id, account_key, retracted_by_op_id "
                    "  FROM codex_window_attributions ORDER BY op_id")
            ]
        finally:
            conn.close()

    def journal_ops(self, kind: str | None = None):
        journal_dir = self.app_dir / "journal"
        out = []
        for segment in sorted(journal_dir.glob("*.jsonl")):
            for line in segment.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("t") != "op":
                    continue
                if kind is None or rec.get("payload", {}).get("kind") == kind:
                    out.append(rec)
        return out

    def journal_size(self):
        journal_dir = self.app_dir / "journal"
        return sum(p.stat().st_size for p in sorted(journal_dir.glob("*.jsonl")))

    def dump(self):
        """A logical dump of both axes, comparable across either rebuild."""
        return {
            "entries": self.entry_accounts(),
            "blocks": {k: sorted(v, key=lambda x: (x is None, x))
                       for k, v in self.block_accounts().items()},
            "milestones": {k: sorted(v, key=lambda x: (x is None, x))
                           for k, v in self.milestone_accounts().items()},
            "attributions": self.attribution_rows(),
        }


def _selector(indices=SELECTED) -> tuple[str, str]:
    """A half-open ``[since, until)`` that covers ``indices`` entirely."""
    earliest = min(min(captures(i)) for i in indices)
    latest = max(max(captures(i)) for i in indices)
    return (_z(earliest - dt.timedelta(days=1)),
            _z(latest + dt.timedelta(days=1)))


def _reset_iso(index: int) -> str:
    return reset_at(index).astimezone(UTC).isoformat()


# ── AC1 / AC2 / AC5 / AC7 / AC8: the acceptance test ─────────────────────────

def test_apply_retract_survives_stats_and_cache_rebuilds(store):
    since, until = _selector()

    # Preview writes nothing at all. Both witnesses are taken BEFORE the
    # preview runs: a dump captured afterwards cannot see a preview-induced
    # change, so it would assert nothing about the preview at all.
    journal_before = store.journal_size()
    before = store.dump()
    proc, payload = store.json_attribute("--since", since, "--until", until)
    assert proc.returncode == 0, proc.stderr
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "preview"
    assert payload["mode"] == "attribute"
    assert payload["source"] == "codex"
    assert payload["account"]["accountKey"] == store.account
    assert payload["summary"] == {
        "selectedGroups": len(SELECTED), "eligibleGroups": len(SELECTED),
        "noOpGroups": 0, "refusedGroups": 0, "blockingRefusedGroups": 0,
    }
    assert payload["actions"] == {
        "journalOpsAppended": 0, "quotaGroupsUpdated": 0, "spendRowsUpdated": 0,
    }
    assert {group["group"]["canonicalResetsAtUtc"] for group in payload["groups"]} == {
        _reset_iso(index) for index in SELECTED}
    for group in payload["groups"]:
        assert group["disposition"] == "eligible"
        assert len(group["group"]["rawResetsAtUtc"]) == 3, (
            "three jittered provider spellings, one physical component")
    assert store.journal_size() == journal_before
    assert store.dump() == before, "the preview writes no derived row either"

    # Apply.
    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "applied"
    assert payload["actions"]["journalOpsAppended"] == len(SELECTED)
    assert payload["actions"]["spendRowsUpdated"] == 6
    assert payload["actions"]["quotaGroupsUpdated"] == len(SELECTED)

    entries = store.entry_accounts()
    assert sum(1 for _ts, acct in entries if acct == store.account) == 6
    # The decoy outside every selected group stays unattributed, and the
    # natively-decided row keeps its own account.
    assert sum(1 for _ts, acct in entries if acct is None) == 1
    assert sum(1 for _ts, acct in entries if acct == store.native) == 1

    blocks = store.block_accounts()
    for index in SELECTED:
        assert blocks[_reset_iso(index)] == {store.account}
    assert blocks[_reset_iso(NATIVE_GROUP)] == {store.native}
    milestones = store.milestone_accounts()
    assert milestones[_reset_iso(TARGET_GROUP)] == {store.account}

    # Unrelated groups, the Spark pool and the 5-hour window are untouched.
    untouched = {
        reset: accounts for reset, accounts in blocks.items()
        if reset not in {_reset_iso(i) for i in SELECTED}
    }
    assert untouched, "the fixture must carry groups outside the selector"
    assert store.account not in {
        account for accounts in untouched.values() for account in accounts}

    applied = store.dump()
    assert applied != before

    # AC7, first leg.
    proc = store.cli("db", "rebuild", "--db", "stats")
    assert proc.returncode == 0, proc.stderr
    assert store.dump() == applied

    # AC7, second leg.
    proc = store.cli("cache-sync", "--rebuild", "--source", "codex")
    assert proc.returncode == 0, proc.stderr
    assert store.dump() == applied

    # AC8: retraction recomputes as if the assertion had never been recorded.
    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--retract", "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "applied"
    assert payload["mode"] == "retract"
    assert payload["actions"]["journalOpsAppended"] == len(SELECTED)

    retracted = store.dump()
    assert retracted["entries"] == before["entries"], (
        "the spend axis returns to its per-file baseline")
    assert retracted["blocks"] == before["blocks"]
    assert retracted["milestones"] == before["milestones"]
    assert all(row[2] for row in retracted["attributions"]), (
        "the assertion records survive, tombstoned")

    proc = store.cli("db", "rebuild", "--db", "stats")
    assert proc.returncode == 0, proc.stderr
    assert store.dump() == retracted
    proc = store.cli("cache-sync", "--rebuild", "--source", "codex")
    assert proc.returncode == 0, proc.stderr
    assert store.dump() == retracted


# ── AC9: idempotency ─────────────────────────────────────────────────────────

def test_a_second_identical_apply_appends_nothing_and_updates_nothing(store):
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    after_first = store.dump()
    size = store.journal_size()

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "noop"
    assert payload["summary"]["noOpGroups"] == len(SELECTED)
    assert payload["summary"]["eligibleGroups"] == 0
    assert payload["actions"] == {
        "journalOpsAppended": 0, "quotaGroupsUpdated": 0, "spendRowsUpdated": 0,
    }
    assert store.journal_size() == size, "the journal high-water is unchanged"
    assert store.dump() == after_first


# ── AC12: retraction reaches a dormant assertion ─────────────────────────────

def test_retracting_a_dormant_assertion_by_time_range(store):
    """An observation-based selector could never reach this, which is why §4.1
    selects over the durable assertion records instead."""
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0

    # Remove the underlying evidence for one group so its assertion goes
    # dormant: no current group carries its witnesses any more.
    conn = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        conn.execute(
            "DELETE FROM quota_window_snapshots "
            " WHERE source='codex' AND unixepoch(canonical_resets_at_utc) "
            "       = unixepoch(?)", (_z(reset_at(TARGET_GROUP)),))
        conn.commit()
    finally:
        conn.close()

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--retract")
    assert proc.returncode == 0, proc.stderr
    states = {group["disposition"] for group in payload["groups"]}
    assert "dormant" in states, payload["groups"]

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--retract", "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["actions"]["journalOpsAppended"] == len(SELECTED)
    assert all(row[2] for row in store.attribution_rows()), (
        "every assertion, dormant included, is tombstoned")


# ── AC3 / AC4 / AC11: the refusal matrix ─────────────────────────────────────

def test_a_group_with_a_different_real_account_is_refused(store):
    since, until = _selector((NATIVE_GROUP,))
    proc, payload = store.json_attribute("--since", since, "--until", until)
    assert proc.returncode == 2
    assert payload["summary"]["refusedGroups"] == 1
    codes = payload["groups"][0]["refusalCodes"]
    assert codes[0] == "native_account_conflict", codes
    # That group's own accounting row carries the same native account, so the
    # spend leg refuses independently. Both codes are reported; refusal is not
    # first-cause-only.
    assert codes == ["native_account_conflict", "spend_account_conflict"]
    assert payload["groups"][0]["nativeAccountKeys"] == [store.native]


def test_a_model_scoped_pool_is_refused_but_does_not_block(store):
    """AC4 in both halves.

    The Spark pool is REPORTED as refused at plan time, which is what keeps the
    exclusion visible. It does not BLOCK, because it can never be account weekly
    quota, so the operator never asked for it — a time range selected it.
    Measured read-only against the maintainer's store, one whole-era range
    selects 605 groups of which 539 are out of scope; letting those block would
    refuse every run an operator could make.
    """
    spark_capture = BASE_RESET + dt.timedelta(days=13)
    since = _z(spark_capture - dt.timedelta(hours=1))
    until = _z(spark_capture + dt.timedelta(hours=1))
    proc, payload = store.json_attribute("--since", since, "--until", until)
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "noop", "nothing to record, and nothing wrong"
    assert payload["summary"]["refusedGroups"] >= 1
    assert payload["summary"]["blockingRefusedGroups"] == 0
    assert any("model_scoped" in group["refusalCodes"]
               for group in payload["groups"])


def test_an_out_of_scope_window_never_blocks_an_otherwise_clean_apply(store):
    """The whole point of the split, asserted end to end: a range that reaches
    both attributable weekly groups and out-of-scope windows still applies."""
    # Groups 0 and 1 (attributable), the 5-hour window and the Spark pool —
    # and deliberately NOT the natively-identified group, whose refusal is a
    # genuine conflict and DOES block.
    since = _z(min(captures(1)) - dt.timedelta(days=1))
    until = _z(BASE_RESET + dt.timedelta(days=30))
    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "applied"
    assert payload["summary"]["refusedGroups"] >= 2, (
        "the Spark pool and the 5-hour window are both in this range")
    assert payload["summary"]["blockingRefusedGroups"] == 0
    assert payload["actions"]["journalOpsAppended"] == 2, (
        "groups 0 and 1, the only attributable ones in this range")


def test_a_partially_covered_group_is_refused_rather_than_split(store):
    """AC2. The group's whole extent is reported, so the operator can see the
    range they need rather than guessing at it."""
    target = captures(TARGET_GROUP)
    since = _z(min(target) + dt.timedelta(minutes=30))
    until = _z(max(target) + dt.timedelta(days=1))
    proc, payload = store.json_attribute("--since", since, "--until", until)
    assert proc.returncode == 2
    refused = [g for g in payload["groups"] if g["disposition"] == "refused"]
    assert refused, payload["groups"]
    assert any("partial_group" in g["refusalCodes"] for g in refused)


def test_an_empty_selection_exits_zero(store):
    far = BASE_RESET + dt.timedelta(days=400)
    proc, payload = store.json_attribute(
        "--since", _z(far), "--until", _z(far + dt.timedelta(days=1)))
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "empty"
    assert payload["summary"]["selectedGroups"] == 0
    assert payload["groups"] == []


def test_an_unknown_ref_exits_two(store):
    since, until = _selector()
    proc = store.attribute("--since", since, "--until", until, ref="nobody")
    assert proc.returncode == 2
    assert "ambiguous or unknown" in proc.stderr


def test_the_unattributed_sentinel_is_refused(store):
    since, until = _selector()
    proc = store.attribute("--since", since, "--until", until,
                           ref="unattributed")
    assert proc.returncode == 2
    assert "unattributed" in proc.stderr


def test_a_claude_ref_is_refused(store):
    since, until = _selector()
    proc = store.attribute("--since", since, "--until", until, ref="mainclaude")
    assert proc.returncode == 2
    assert "codex" in proc.stderr.lower()


def test_a_naive_instant_is_a_usage_error(store):
    proc = store.attribute("--since", "2026-01-01T00:00:00")
    assert proc.returncode == 2
    assert "timezone-aware" in proc.stderr


def test_until_before_since_is_a_usage_error(store):
    proc = store.attribute("--since", "2026-02-01T00:00:00Z",
                           "--until", "2026-01-01T00:00:00Z")
    assert proc.returncode == 2


# ── AC13: ownership change with no operator retraction ───────────────────────

def test_native_evidence_arriving_later_moves_both_axes(store):
    """Assert A, then let ordinary ingest supply native evidence naming another
    account for the same group, with NO operator retraction. Both axes must end
    on the native account (spec §7.1)."""
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    assert sum(1 for _ts, acct in store.entry_accounts()
               if acct == store.account) == 6

    # A new rollout carrying one more capture of the target group, ingested
    # while auth.json names the native account.
    (store.home / "auth.json").write_text(
        _auth_json(NATIVE_ACCOUNT_ID, NATIVE_EMAIL))
    _write_rollout(store.home, "rollout-late.jsonl", [
        _session_meta(captures(TARGET_GROUP)[0] + dt.timedelta(minutes=1),
                      "33333333-3333-4333-8333-333333333333", "late"),
        _quota_event(captures(TARGET_GROUP)[0] + dt.timedelta(minutes=2),
                     reset=raw_resets(TARGET_GROUP)[0], used_percent=42.0),
    ])
    proc = store.cli("cache-sync", "--source", "codex")
    assert proc.returncode == 0, proc.stderr

    blocks = store.block_accounts()
    assert blocks[_reset_iso(TARGET_GROUP)] == {store.native}, (
        "the percentage axis reverts by construction")
    assert sum(1 for _ts, acct in store.entry_accounts()
               if acct == store.account) == 0, (
        "and the spend axis must not be stranded on the asserted account")

    # Leaving them NULL would satisfy the assertion above and still break AC13,
    # which asks for both axes to end on the SAME account. Name it.
    window_start = reset_at(TARGET_GROUP) - dt.timedelta(minutes=WEEK_MINUTES)
    window_end = reset_at(TARGET_GROUP)
    inside = [
        account for timestamp, account in store.entry_accounts()
        if window_start <= dt.datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")) < window_end
    ]
    assert len(inside) == 6, inside
    assert set(inside) == {store.native}, (
        "both axes end on the account the provider named")


# ── AC10: the doctor leg ─────────────────────────────────────────────────────

def _doctor_check(payload: dict, check_id: str) -> dict:
    for category in payload["categories"]:
        for check in category["checks"]:
            if check["id"] == check_id:
                return check
    raise AssertionError(f"missing doctor check {check_id}")


def test_the_doctor_leg_is_ok_with_no_assertions(store):
    proc = store.cli("doctor", "--json")
    payload = json.loads(proc.stdout)
    check = _doctor_check(payload, "accounts.codex_window_attribution")
    assert check["severity"] == "ok"
    assert check["details"]["active"] == 0


def test_the_doctor_leg_warns_on_a_dormant_assertion(store):
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    conn = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        conn.execute(
            "DELETE FROM quota_window_snapshots "
            " WHERE source='codex' AND unixepoch(canonical_resets_at_utc) "
            "       = unixepoch(?)", (_z(reset_at(TARGET_GROUP)),))
        conn.commit()
    finally:
        conn.close()

    proc = store.cli("doctor", "--json")
    payload = json.loads(proc.stdout)
    check = _doctor_check(payload, "accounts.codex_window_attribution")
    assert check["severity"] == "warn"
    assert check["details"]["dormant"] == 1
    assert "retract" in check["remediation"]
    # WARN never contributes to the FAIL exit code. This fixture carries
    # unrelated FAILing legs (no install symlinks in a tmp data dir), so the
    # claim is asserted about the severity counts rather than about the exit
    # code, which those legs already own.
    assert check["severity"] not in ("fail",)


# ── §8.5: recovery from a crash after the append ─────────────────────────────

def test_a_crash_after_the_append_recovers_without_a_duplicate_op(store):
    """The journal holds the truth and the derived state does not.

    Simulated faithfully rather than mocked: the ops are appended exactly as
    the apply sequence appends them, and nothing else runs. A rerun must land
    them, finish the cache and stats steps, report ``recovered``, and append no
    duplicate — because an already-asserted group plans as a no-op.
    """
    import _cctally_journal as jr
    import _lib_journal as lj

    since, until = _selector()
    _proc, payload = store.json_attribute("--since", since, "--until", until)
    groups = [g["group"] for g in payload["groups"]]
    assert len(groups) == len(SELECTED)

    jr.append_records([
        lj.make_codex_window_attribution(
            at="2026-08-14T00:00:00Z", account_key=store.account,
            source_root_key=group["sourceRootKey"],
            logical_limit_key=group["logicalLimitKey"],
            observed_slot=group["observedSlot"],
            window_minutes=int(group["windowMinutes"]),
            raw_resets_at_utc=list(group["rawResetsAtUtc"]),
            canonical_resets_at_utc=group["canonicalResetsAtUtc"],
        )
        for group in groups
    ])
    assert store.attribution_rows() == [], (
        "the derived table has not seen the records yet, which is the state a "
        "crash after the append leaves behind")
    ops_before = len(store.journal_ops("codex_window_attribution"))

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] == "recovered"
    assert payload["actions"]["journalOpsAppended"] == 0
    assert len(store.journal_ops("codex_window_attribution")) == ops_before
    assert len(store.attribution_rows()) == len(SELECTED)
    assert sum(1 for _ts, acct in store.entry_accounts()
               if acct == store.account) == 6, (
        "the recovery completes the cache and stats steps it never reached")


# ── §8.2: strict adoption propagates ─────────────────────────────────────────

def test_a_failure_inside_the_bounded_adoption_pass_aborts_the_apply(store):
    """Reused unchanged, the best-effort adoption pass would swallow this and
    the command would report success with the spend axis untouched."""
    since, until = _selector()
    conn = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        conn.execute(
            "CREATE TRIGGER forced_adoption_failure "
            "BEFORE UPDATE ON codex_session_entries "
            "BEGIN SELECT RAISE(ABORT, 'forced adoption failure'); END")
        conn.commit()
    finally:
        conn.close()

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 3, proc.stdout
    assert payload["status"] == "recordedPending"
    assert payload["errors"][0]["code"] == "recordedPending"
    assert store.attribution_rows() == [], (
        "the cache transaction is rolled back, not partially committed")
    assert all(acct is None or acct == store.native
               for _ts, acct in store.entry_accounts()), (
        "and the spend axis is untouched rather than half-stamped")


# ── the cross-axis reconcile invariant ───────────────────────────────────────

def test_both_axes_name_the_same_account_for_every_attributed_window(store):
    """AX1: a live Codex block naming a real account and the accounting rows
    inside its nominal window must not name a DIFFERENT real account.

    This is a reconcile invariant rather than an assertion about one group, and
    it lives here rather than in `bin/cctally-reconcile-test` for the reason
    `tests/test_accounts_reconcile_invariants.py` already states about its own
    per-account invariants: that harness reconciles two SUBCOMMANDS' JSON over
    shared fixtures, and this one reconciles two derived tables. It is also the
    invariant a stranded spend stamp breaks, which is the §7.1 failure mode.
    """
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0

    stats = sqlite3.connect(str(store.app_dir / "stats.db"))
    try:
        blocks = [
            (str(row[0]), str(row[1]), int(row[2]))
            for row in stats.execute(
                "SELECT resets_at_utc, account_key, window_minutes "
                "  FROM quota_window_blocks "
                " WHERE source='codex' AND orphaned_at IS NULL")
            if row[1] and row[1] != "unattributed"
        ]
    finally:
        stats.close()
    assert blocks, "the invariant must have something to check"

    cache = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        entries = [
            (str(row[0]), row[1]) for row in cache.execute(
                "SELECT timestamp_utc, account_key FROM codex_session_entries")
        ]
    finally:
        cache.close()

    violations = []
    for reset_iso, account, window_minutes in blocks:
        reset = dt.datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
        start = reset - dt.timedelta(minutes=window_minutes)
        for timestamp, entry_account in entries:
            if not entry_account or entry_account == "unattributed":
                continue
            instant = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if start <= instant < reset and entry_account != account:
                violations.append((reset_iso, account, timestamp, entry_account))
    assert violations == [], violations


def test_the_doctor_leg_warns_on_an_assertion_native_evidence_now_contradicts(
        store):
    """The `conflicting` condition, reached the way it is actually reached in
    production: ordinary ingest supplies an account for a group the operator had
    attributed, so the assertion is suppressed and stops applying."""
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    (store.home / "auth.json").write_text(
        _auth_json(NATIVE_ACCOUNT_ID, NATIVE_EMAIL))
    _write_rollout(store.home, "rollout-late.jsonl", [
        _session_meta(captures(TARGET_GROUP)[0] + dt.timedelta(minutes=1),
                      "44444444-4444-4444-8444-444444444444", "late"),
        _quota_event(captures(TARGET_GROUP)[0] + dt.timedelta(minutes=2),
                     reset=raw_resets(TARGET_GROUP)[0], used_percent=42.0),
    ])
    assert store.cli("cache-sync", "--source", "codex").returncode == 0

    payload = json.loads(store.cli("doctor", "--json").stdout)
    check = _doctor_check(payload, "accounts.codex_window_attribution")
    assert check["severity"] == "warn"
    assert check["details"]["conflicting"] == 1
    assert check["details"]["dormant"] == 0


def test_the_doctor_leg_warns_when_the_derived_index_is_behind_the_journal(
        store):
    """The `cursor_behind` condition — attribution is being UNDER-applied, which
    is the state a crash between the append and the replay leaves behind."""
    import _cctally_journal as jr
    import _lib_journal as lj

    since, until = _selector()
    _proc, payload = store.json_attribute("--since", since, "--until", until)
    group = payload["groups"][0]["group"]
    jr.append_records([lj.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key=store.account,
        source_root_key=group["sourceRootKey"],
        logical_limit_key=group["logicalLimitKey"],
        observed_slot=group["observedSlot"],
        window_minutes=int(group["windowMinutes"]),
        raw_resets_at_utc=list(group["rawResetsAtUtc"]),
        canonical_resets_at_utc=group["canonicalResetsAtUtc"],
    )])

    payload = json.loads(store.cli("doctor", "--json").stdout)
    check = _doctor_check(payload, "accounts.codex_window_attribution")
    assert check["severity"] == "warn"
    assert check["details"]["cursor_behind"] is True
    assert check["details"]["active"] == 0, (
        "the record is in the journal and not yet in the derived index, which "
        "is exactly what the cursor is reporting")


def test_the_doctor_leg_warns_on_a_split_assertion(store):
    """AC10's fourth condition, reached through the real anchor derivation.

    ``SPLIT_GROUP`` carries three raw spellings at +0s, +500s and +1,000s. All
    three exist at assertion time, each adjacent pair is inside the 600s
    tolerance, and the union is transitive, so the assertion records one group
    and three witnesses. Re-ingesting the rollout WITHOUT the +500s bridge
    leaves two survivors 1,000s apart, which re-derive as two components and
    therefore two stored anchors — two current groups, each intersecting the
    assertion's witness set, which is the SPLIT the resolver reports.

    Deleting a snapshot row alone cannot produce this: the group key is built
    from the STORED ``canonical_resets_at_utc``, so the survivors of a delete
    keep the anchor they were written with and stay one group.
    """
    since, until = _selector((SPLIT_GROUP,))
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    rows = store.attribution_rows()
    assert len(rows) == 1, rows

    _write_rollout(store.home, "rollout-main.jsonl",
                   _main_records(drop_split_bridge=True))
    proc = store.cli("cache-sync", "--rebuild", "--source", "codex")
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(store.cli("doctor", "--json").stdout)
    check = _doctor_check(payload, "accounts.codex_window_attribution")
    assert check["details"]["split"] == 1, check["details"]
    assert check["severity"] == "warn"


# ── T3-2: the pre-commit verification must be real in retract mode ───────────

def test_a_retraction_that_leaves_an_assertion_active_aborts_the_transaction(
        store):
    """§8.2's "verify the applied row set against the plan it locked".

    A retraction never inserts a row under its own op id — it only stamps
    ``retracted_by_op_id`` on the assertions it names — so comparing the
    RETRACTION op ids against the still-active assertion ids is empty by
    construction and verifies nothing. The comparison has to be against the
    TARGETED assertion ids, and this is the state that tells the two apart: one
    named assertion survives the replay and the run must abort rather than
    report success.
    """
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    before = store.attribution_rows()
    assert before and all(row[2] is None for row in before), before

    conn = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        conn.execute(
            "CREATE TRIGGER skip_one_retraction "
            "BEFORE UPDATE OF retracted_by_op_id ON codex_window_attributions "
            "WHEN NEW.op_id = (SELECT MIN(op_id) FROM codex_window_attributions) "
            "BEGIN SELECT RAISE(IGNORE); END")
        conn.commit()
    finally:
        conn.close()

    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--retract", "--yes")
    assert proc.returncode == 3, proc.stdout
    assert payload["status"] == "recordedPending"
    assert all(row[2] is None for row in store.attribution_rows()), (
        "the transaction is rolled back whole rather than half-tombstoned")


# ── T3-6 / T3-3: the projection call and the rerun that finishes it ──────────

def _block_the_projection(store) -> None:
    """Make the quota projection's stats write fail, and nothing before it."""
    conn = sqlite3.connect(str(store.app_dir / "stats.db"))
    try:
        for verb in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER forced_projection_failure_{verb.lower()} "
                f"BEFORE {verb} ON quota_window_blocks "
                "BEGIN SELECT RAISE(ABORT, 'forced projection failure'); END")
        conn.commit()
    finally:
        conn.close()


def _unblock_the_projection(store) -> None:
    conn = sqlite3.connect(str(store.app_dir / "stats.db"))
    try:
        for verb in ("insert", "update"):
            conn.execute(f"DROP TRIGGER forced_projection_failure_{verb}")
        conn.commit()
    finally:
        conn.close()


def test_a_projection_failure_reports_recorded_pending_rather_than_raising(
        store):
    """``reconcile_codex_quota_projection`` runs after the write has landed and
    opens its own stats transaction over the whole history, so it can fail on
    its own. Uncaught, the operator gets a traceback instead of the
    ``recordedPending`` guidance and ``--json`` emits nothing at all."""
    since, until = _selector()
    _block_the_projection(store)
    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert payload is not None, "the envelope must still be emitted"
    assert payload["status"] == "recordedPending"
    assert "Traceback" not in proc.stderr


def test_a_rerun_after_a_failed_projection_completes_the_stats_step(store):
    """§8.5: "a rerun … completes the cache and stats steps, reports
    ``recovered``, and exits 0".

    The untested crash window is the one where the journal append and the cache
    commit both succeeded and the stats side did not. On the rerun the derived
    table already carries the assertions, so every group plans as a no-op — and
    a ``--yes`` short-circuit on that status exits 0 reporting ``noop`` while
    stats.db still lacks the attribution, which is the promise inverted.
    """
    since, until = _selector()
    _block_the_projection(store)
    proc = store.attribute("--since", since, "--until", until, "--yes")
    assert proc.returncode == 3, proc.stdout
    blocks = store.block_accounts()
    assert store.account not in {
        account for accounts in blocks.values() for account in accounts}, (
        "the precondition: the percentage axis never got the attribution")
    assert store.attribution_rows(), "but the cache transaction did commit"

    _unblock_the_projection(store)
    proc, payload = store.json_attribute(
        "--since", since, "--until", until, "--yes")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert payload["status"] == "recovered", payload["status"]
    assert payload["actions"]["journalOpsAppended"] == 0
    blocks = store.block_accounts()
    for index in SELECTED:
        assert blocks[_reset_iso(index)] == {store.account}, (
            "the rerun finishes the step the failed run never reached")


# ── T3-4: a five-hour window is not a model pool ─────────────────────────────

def test_a_five_hour_window_is_refused_as_not_weekly(store):
    """Reporting a 5-hour window under ``model_scoped`` tells an operator that
    the ``_lib_codex_pools`` classifier decided it, which it did not: the window
    is simply not account-level weekly quota. One disposition, two codes."""
    since = _z(BASE_RESET + dt.timedelta(minutes=30))
    until = _z(BASE_RESET + dt.timedelta(hours=2))
    proc, payload = store.json_attribute("--since", since, "--until", until)
    assert proc.returncode == 0, proc.stderr
    five_hour = [group for group in payload["groups"]
                 if group["group"]["windowMinutes"] == FIVE_HOUR_MINUTES]
    assert five_hour, payload["groups"]
    assert five_hour[0]["refusalCodes"] == ["not_weekly"]
    assert payload["summary"]["blockingRefusedGroups"] == 0


# ── T3-11: a nullable stored anchor must not journal the string "None" ───────

def test_a_retraction_over_a_null_anchor_journals_a_witness_not_none(store):
    """``codex_window_attributions.canonical_resets_at_utc`` is nullable per the
    DDL. The field is audit-only and never matched on, so a bad value is inert —
    but the journal is append-only, so ``str(None)`` writes the literal "None"
    permanently."""
    since, until = _selector()
    assert store.attribute("--since", since, "--until", until,
                           "--yes").returncode == 0
    victim = store.attribution_rows()[0][0]
    conn = sqlite3.connect(str(store.app_dir / "cache.db"))
    try:
        conn.execute("UPDATE codex_window_attributions "
                     "   SET canonical_resets_at_utc = NULL WHERE op_id = ?",
                     (victim,))
        conn.commit()
    finally:
        conn.close()

    # Wide enough to reach the null-anchor record through the assertion-time
    # tiebreak, which is the only selector that can reach it at all.
    wide_until = _z(dt.datetime.now(UTC) + dt.timedelta(days=1))
    proc = store.attribute("--since", since, "--until", wide_until,
                           "--retract", "--yes")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    anchors = {
        op["payload"]["canonical_resets_at_utc"]
        for op in store.journal_ops("codex_window_attribution_retract")
    }
    assert anchors, "the retraction must have journaled something"
    assert "None" not in anchors, anchors


# ── T3-13 / T3-14: what the two renders say about a run that wrote nothing ───

def test_a_preview_with_a_blocking_refusal_does_not_claim_an_apply(store):
    since, until = _selector((NATIVE_GROUP,))
    preview = store.attribute("--since", since, "--until", until)
    assert preview.returncode == 2
    assert "Nothing was written" not in preview.stdout, preview.stdout
    assert "--yes" in preview.stdout

    applied = store.attribute("--since", since, "--until", until, "--yes")
    assert applied.returncode == 2
    assert "Nothing was written" in applied.stdout, applied.stdout


def test_an_omitted_until_reports_the_resolved_instant(store):
    """A JSON consumer cannot reproduce the selection from ``until: null``,
    and the human render prints ``now`` for the same run — so the two disagree
    about the range the command actually used."""
    # An omitted `--until` runs to NOW, so the range has to start after the
    # natively-identified group or its blocking refusal owns the exit code.
    since = _z(min(captures(1)) - dt.timedelta(days=1))
    proc, payload = store.json_attribute("--since", since)
    assert proc.returncode == 0, proc.stderr
    selector = payload["selector"]
    assert selector["untilSpecified"] is False
    resolved = dt.datetime.fromisoformat(
        str(selector["until"]).replace("Z", "+00:00"))
    assert resolved.tzinfo is not None
    assert resolved > dt.datetime.fromisoformat(since.replace("Z", "+00:00"))

    human = store.attribute("--since", since)
    assert human.returncode == 0, human.stderr
    assert ", now)" not in human.stdout, human.stdout

    explicit = _z(BASE_RESET + dt.timedelta(days=30))
    proc, payload = store.json_attribute("--since", since, "--until", explicit)
    assert payload["selector"]["untilSpecified"] is True
    assert payload["selector"]["until"] == explicit


# ── T3-12: the human render is bounded and names the blocking count ──────────

def _synthetic_group(index: int, *, disposition: str, codes=()) -> dict:
    return {
        "group": {
            "sourceRootKey": "root", "logicalLimitKey": "limit",
            "observedSlot": "primary", "windowMinutes": 10_080,
            "canonicalResetsAtUtc": f"2026-01-{index % 28 + 1:02d}T00:00:00Z",
            "rawResetsAtUtc": [],
        },
        "disposition": disposition, "nativeAccountKeys": [],
        "assertionAccountKeys": [], "observationCount": 1,
        "spendCandidateCount": 0, "refusalCodes": list(codes),
        "assertionOpIds": [],
    }


def test_the_human_render_caps_refused_rows_and_names_the_blocking_count(capsys):
    """605 selected groups on the maintainer's real store, 539 of them out of
    scope. One row each buries the handful that need a decision, and a summary
    naming only ``refused`` cannot tell a blocking refusal from a skipped
    out-of-scope window."""
    import _cctally_account as account_mod

    groups = [_synthetic_group(i, disposition="refused", codes=("not_weekly",))
              for i in range(120)]
    groups.append(_synthetic_group(999, disposition="refused",
                                   codes=("partial_group",)))
    payload = {
        "status": "refused", "mode": "attribute", "source": "codex",
        "account": {"accountKey": "k" * 32, "accountLabel": "work"},
        "selector": {"since": "2026-01-01T00:00:00Z",
                     "until": "2026-08-01T00:00:00Z", "untilSpecified": True},
        "summary": {"selectedGroups": len(groups), "eligibleGroups": 0,
                    "noOpGroups": 0, "refusedGroups": len(groups),
                    "blockingRefusedGroups": 1},
        "groups": groups, "actions": dict(account_mod._ATTRIBUTE_NO_ACTIONS),
        "errors": [],
    }
    account_mod._attribute_render(payload, requested_apply=True)
    out = capsys.readouterr().out
    body = out.splitlines()
    reset_rows = [line for line in body if line.startswith("2026-01-")]
    assert len(reset_rows) == account_mod._ATTRIBUTE_REFUSED_ROW_CAP, len(reset_rows)
    assert f"{len(groups) - len(reset_rows)} more" in out, out
    assert "1 blocking" in out, out
