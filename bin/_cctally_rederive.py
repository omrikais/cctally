"""Scratch rederivation adapter for #372 Task B.

This eager sibling replays retained Claude observations and relevant operator
records through the current ingest hooks on a disposable stats index. Derived
events are captured in memory; the durable journal, source cache, config,
projection files, provider state, and alert dispatcher are never mutated.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import _cctally_core
import _cctally_journal as _journal
import _cctally_record as _record
import _lib_account_protocol
import _lib_clone_copy
import _lib_journal
import _lib_json_envelope
import _lib_rebuild_progress
import _lib_rederive
import _cctally_store
from _cctally_store import StatsIndexNotCurrent


class RederiveBusy(RuntimeError):
    """The stable-view/apply lock set could not be acquired in time."""


class RederiveApplyError(RuntimeError):
    """An operational apply stage failed after a concrete preview existed."""

    def __init__(self, stage, preview, batch_id, cause):
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.preview = preview
        self.batch_id = batch_id
        self.cause = cause


class RederiveObligationNotDurable(RederiveApplyError):
    """Fix round K12 (K11R-2): the corrected index IS published, but its
    residual-derivation obligation could not be made durable. Raised only
    after the apply's lock context has exited normally, so the locks are
    released and the holder wake is posted; the command reports it with
    message N2 and exit 3, never as a failed apply."""


@dataclass(frozen=True)
class RederivePreview:
    plan: _lib_rederive.RederivePlan
    records: tuple[dict, ...]
    journal_high_water: "tuple[str, int] | None"
    record_ends: tuple[tuple[str, int], ...]
    generated_at: str
    batch_id: "str | None"
    incomplete_batch: bool
    latest_completed_batch: "str | None"
    latest_completed_high_water: "tuple[str, int] | None"
    recovery_required: bool
    # #374: the quarantined same-revision groups this plan will resolve by
    # forcing a revision advance. Additive; empty on a clean journal.
    journal_conflicts: tuple = ()
    reviewed_decision_id: "str | None" = None
    baseline_plan: "_lib_rederive.RederivePlan | None" = None
    baseline_plan_guard: "dict | None" = None
    # D4: the empty correction batch an unfinished reviewed decision whose
    # plan has no actions completes with. Never reported as `batch_id`, so a
    # preview's output is unchanged; None when there is no such decision.
    empty_decision_batch_id: "str | None" = None


@dataclass(frozen=True)
class RederiveCommandResult:
    preview: RederivePreview
    status: str
    batch_id: "str | None"
    rebuild: "object | None"


_REDERIVE_LOCK_TIMEOUT_SECONDS = 5.0
_REDERIVE_CRASH_HOOK = None


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _fingerprint(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


#: #874: the only tables the cache contract names. Inspecting any other table
#: is not the planner's business, and on a virtual table whose module this
#: SQLite build lacks (a legacy FTS5 index on a build without FTS5) even
#: `PRAGMA table_info` raises before the contract could type the gap.
_CONTRACT_TABLES = ("session_entries", "session_files", "codex_session_entries")


def _cache_contract(cache_conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = {
        row[0] for row in cache_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        name: {
            row[1] for row in cache_conn.execute(f"PRAGMA table_info({name})")
        }
        for name in _CONTRACT_TABLES
        if name in names
    }


def _cache_fingerprint(cache_conn: sqlite3.Connection, *, include_codex=False) -> str:
    """Hash every cost-bearing Claude cache row and its project metadata.

    The caller must have validated the cache contract first, including
    ``validate_codex_cache_contract`` when ``include_codex`` is set (#874):
    a missing column here is a raw sqlite error, not a typed data gap.

    #997 D16: the digest is `_fingerprint` of
    ``{"codexEntries"?, "sessionEntries", "sessionFiles"}`` (each a list of
    row lists), but the canonical JSON is fed to SHA-256 row by row as the
    cursor yields it, in sorted key order with `_canonical_bytes`' encoder
    settings, instead of materializing every row and one document of them
    (+0.7 GB transient on the maintainer's cache). The bytes hashed, and so
    the digest, are identical, so a preview's plan guard and an apply's
    recheck agree across the change.
    """
    entry_columns = (
        "source_path", "line_offset", "timestamp_utc", "model", "input_tokens",
        "output_tokens", "cache_create_tokens", "cache_read_tokens",
        "cache_create_1h_tokens", "cost_usd_raw", "speed", "account_key",
    )
    file_columns = ("path", "session_id", "project_path")
    sections = {
        "sessionEntries": (
            "SELECT " + ",".join(entry_columns)
            + " FROM session_entries ORDER BY source_path, line_offset"),
        "sessionFiles": (
            "SELECT " + ",".join(file_columns)
            + " FROM session_files ORDER BY path"),
    }
    if include_codex:
        sections["codexEntries"] = (
            "SELECT source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,account_key "
            "FROM codex_session_entries ORDER BY source_path,line_offset"
        )
    encode = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode
    digest = hashlib.sha256()
    digest.update(b"{")
    for index, key in enumerate(sorted(sections)):
        if index:
            digest.update(b",")
        digest.update(encode(key).encode("utf-8") + b":[")
        first = True
        for row in cache_conn.execute(sections[key]):
            if not first:
                digest.update(b",")
            first = False
            digest.update(encode(list(row)).encode("utf-8"))
        digest.update(b"]")
    digest.update(b"}")
    return "sha256:" + digest.hexdigest()


def _split_matches(parts, total) -> bool:
    """A recorded cost split agrees with its total up to summation order.

    The at-fire writer sums one interleaved entry stream into the total and
    into per-model buckets, and the two association orders round differently
    as spend grows. `fsum` removes the split's own rounding; the relative bound
    absorbs the total's, while a material mismatch still fails (#869 F8).
    """
    return math.isclose(
        math.fsum(parts), total, rel_tol=1e-10, abs_tol=1e-9,
    )


def _desired_codex_budget_costs(selection, cache_conn, *, uncertain=None):
    """Correct only journaled fallback facts with an unchanged source set.

    ``uncertain`` collects the ids of crossings kept unchanged because their
    retained source population no longer matches the recorded one.
    """
    import _cctally_cache
    import _lib_cost_provenance
    import _lib_pricing
    out = []
    for selected in selection.by_id.values():
        record = selected.record
        if selected.status != "active" or record is None:
            continue
        payload = record.get("payload") or {}
        if payload.get("kind") != "budget" or payload.get("vendor") != "codex":
            continue
        old = payload.get("_pricing") or {}
        if old.get("version") != 1:
            continue  # legacy evidence is insufficient for automatic repricing
        missing = old.get("fallbackModels") or []
        if not missing:
            out.append(record)  # a genuinely priced at-fire fact is immutable
            continue
        old_model_costs = old.get("modelCostsUsd")
        spent_value = payload.get("spent_usd")
        if (
            not isinstance(missing, list)
            or not missing
            or any(not isinstance(model, str) or not model for model in missing)
            or len(set(missing)) != len(missing)
            or not isinstance(old_model_costs, dict)
            or any(
                not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                for value in old_model_costs.values()
            )
            or any(model not in old_model_costs for model in missing)
            or not isinstance(old.get("directCostUsd"), (int, float))
            or not math.isfinite(old["directCostUsd"])
            or old["directCostUsd"] < 0
            or old.get("speed") not in ("standard", "fast")
            or not isinstance(spent_value, (int, float))
            or not math.isfinite(spent_value)
            or not _split_matches(old_model_costs.values(), spent_value)
            or not _split_matches(
                [old["directCostUsd"],
                 *(old_model_costs[model] for model in missing)],
                spent_value,
            )
        ):
            raise _lib_rederive.RederiveDataGap(
                f"Codex budget {record['id']} has incomplete at-fire "
                "pricing provenance"
            )
        if any(_lib_pricing._is_codex_fallback(model) for model in missing):
            # #869 F1: a direct card has not shipped for every fallback model,
            # so nothing about this crossing can change yet. Decided before
            # the population comparison: drift in a fact this run cannot
            # correct must not refuse every other correction in the family.
            out.append(record)
            continue
        try:
            start = dt.datetime.fromisoformat(
                payload["period_start_at"].replace("Z", "+00:00")
            )
            end = dt.datetime.fromisoformat(
                payload["crossed_at_utc"].replace("Z", "+00:00")
            )
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("invalid budget cost bounds")
            speed = old["speed"]
            threshold = float(payload["threshold"])
            target = float(payload["budget_usd"])
            scope = payload.get("account_key")
            if scope == "*":
                scope = None
            source = _cctally_cache.iter_codex_entries(
                cache_conn, start, end, account_key=scope,
            )
            cost, newer = _lib_cost_provenance.codex_cost_with_provenance(
                source, end=end, speed=speed,
            )
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as exc:
            raise _lib_rederive.RederiveDataGap(
                f"Codex budget {record['id']} lacks retained cost inputs: {exc}"
            ) from exc
        if (
            newer["sourceHash"] != old.get("sourceHash")
            or newer["entryCount"] != old.get("entryCount")
        ):
            # A changed population cannot license a new amount, but it does
            # not invalidate the recorded crossing either: keep it exactly as
            # fired and disclose it (#869 F1/Q2).
            if uncertain is not None:
                uncertain.add(record["id"])
            out.append(record)
            continue
        if any(model not in newer["modelCostsUsd"] for model in missing):
            raise _lib_rederive.RederiveDataGap(
                f"Codex budget {record['id']} lacks a complete at-fire "
                "priced-cost split"
            )
        if any(
            model in newer["fallbackModels"]
            for model in old_model_costs if model not in missing
        ):
            # #869 V5: a model priced by a direct card at fire has lost that
            # card, so this run cannot price the crossing truthfully. That is
            # this crossing's gap alone: keep it exactly as fired and disclose
            # it, rather than refusing every other correction in the family.
            if uncertain is not None:
                uncertain.add(record["id"])
            out.append(record)
            continue
        # Only the fallback-priced models change. The direct-card contribution
        # is a real at-fire fact even when those cards are revised later.
        cost = math.fsum([
            float(old["directCostUsd"]),
            *(newer["modelCostsUsd"][model] for model in missing),
        ])
        consumption_pct = cost / target * 100.0 if target > 0 else 0.0
        if consumption_pct + 1e-9 < threshold:
            # #869 F6: the fallback overpriced this crossing and the real card
            # puts it below its threshold. Omitting it from the desired set
            # tombstones it in the correction batch, so the natural key is
            # free for a genuine later crossing (which re-fires under a new
            # incarnation id, `bm2:`).
            continue
        revised = copy.deepcopy(record)
        revised_payload = revised["payload"]
        revised_payload["spent_usd"] = cost
        revised_payload["consumption_pct"] = consumption_pct
        revised_pricing = copy.deepcopy(newer)
        revised_pricing["modelCostsUsd"] = {
            model: (
                newer["modelCostsUsd"][model] if model in missing else value
            )
            for model, value in old_model_costs.items()
        }
        revised_pricing["directCostUsd"] = cost
        revised_payload["_pricing"] = revised_pricing
        out.append(revised)
    return out


def _historical_close_candidates(selection) -> set:
    """(account_key, window key) of each unmarked close with a $0 model."""
    import _lib_accounts

    out = set()
    for selected in selection.by_id.values():
        record = selected.record
        if selected.status != "active" or record is None:
            continue
        payload = record.get("payload") or {}
        if (
            payload.get("kind") != "five_hour_block_close"
            or payload.get("_pricing")
            or payload.get("pricing_provenance_json") is not None
            or not _lib_rederive.close_cost_is_uncertain(payload)
        ):
            continue
        try:
            out.add((
                payload.get("account_key") or _lib_accounts.UNATTRIBUTED,
                int(payload["five_hour_window_key"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _historical_close_markers(selection, desired_events, block_sources):
    """event id -> correction marker for each provable historical close (#869).

    An unmarked close predates computation-time pricing evidence. It is
    provable only from the retained entries the scratch replay assigned to the
    same block, with the same account, no provider cost, and a positive current
    card rate for every token class those entries used. The planner still
    compares the replayed child populations before adopting the correction.
    """
    import _lib_accounts
    import _lib_cost_provenance

    desired_closes = {
        event.get("id"): event for event in desired_events
        if (event.get("payload") or {}).get("kind") == "five_hour_block_close"
    }
    markers = {}
    for event_id, selected in selection.by_id.items():
        record = selected.record
        if (
            selected.status != "active"
            or record is None
            or event_id not in desired_closes
        ):
            continue
        payload = record.get("payload") or {}
        if payload.get("kind") != "five_hour_block_close":
            continue
        desired = desired_closes[event_id].get("payload") or {}
        key = (
            desired.get("account_key") or _lib_accounts.UNATTRIBUTED,
            desired.get("five_hour_window_key"),
        )
        try:
            key = (key[0], int(key[1]))
        except (TypeError, ValueError):
            continue
        marker = _lib_cost_provenance.historical_close_marker(
            payload, block_sources.get(key),
            unattributed=_lib_accounts.UNATTRIBUTED,
        )
        if marker is not None:
            markers[event_id] = marker
    return markers


def _uncertain_cost_facts(selection) -> set:
    """Ids of retained Codex monetary events lacking proof of their inputs.

    Closed Claude blocks are judged by the plan itself, because whether one
    stays uncertain depends on the correction this plan makes to it.
    """
    out = set()
    for event_id, selected in selection.by_id.items():
        if selected.status != "active" or selected.record is None:
            continue
        payload = selected.record.get("payload") or {}
        kind = payload.get("kind")
        if (kind == "budget" and payload.get("vendor") == "codex"
                and "_pricing" not in payload):
            out.add(event_id)
        elif (kind == "projected"
              and payload.get("metric") == "codex_budget_usd"):
            # Historical projection inputs/config are not retained, so this
            # at-fire alert value cannot be recomputed from a new rate card.
            out.add(event_id)
    return out


def _validate_cache_rows(cache_conn: sqlite3.Connection,
                         raw_records: list[dict]) -> None:
    missing_metadata = cache_conn.execute(
        "SELECT COUNT(*) FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE sf.path IS NULL"
    ).fetchone()[0]
    if missing_metadata:
        raise _lib_rederive.RederiveDataGap(
            "cache.db session_files metadata missing for "
            f"{missing_metadata} session_entries row(s)"
        )
    unknown_split = cache_conn.execute(
        "SELECT COUNT(*) FROM session_entries "
        "WHERE cache_create_tokens > 0 AND cache_create_1h_tokens IS NULL"
    ).fetchone()[0]
    if unknown_split:
        raise _lib_rederive.RederiveDataGap(
            "cache.db cache_create_1h_tokens missing for "
            f"{unknown_split} cache-write row(s)"
        )
    import _lib_accounts
    positive_accounts = {
        record.get("account") or _lib_accounts.UNATTRIBUTED
        for record in raw_records
        if record.get("t") == "obs"
        and record.get("provider") == "claude"
        and float((record.get("payload") or {}).get("weekly_percent") or 0) > 0
    }
    for account_key in sorted(positive_accounts):
        if account_key == _lib_accounts.UNATTRIBUTED:
            row = cache_conn.execute(
                "SELECT 1 FROM session_entries "
                "WHERE account_key IS NULL OR account_key = ? LIMIT 1",
                (_lib_accounts.UNATTRIBUTED,),
            ).fetchone()
        else:
            row = cache_conn.execute(
                "SELECT 1 FROM session_entries WHERE account_key = ? LIMIT 1",
                (account_key,),
            ).fetchone()
        if row is None:
            raise _lib_rederive.RederiveDataGap(
                "cache.db has no Claude session_entries for positive usage "
                f"account {account_key}"
            )


def _joined_entries(cache_conn, range_start, range_end, *,
                    project=None, account_key=None):
    cache = sys.modules["_cctally_cache"]
    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()
    sql = (
        "SELECT se.timestamp_utc, se.model, se.input_tokens, se.output_tokens, "
        "se.cache_create_tokens, se.cache_read_tokens, se.source_path, "
        "sf.session_id, sf.project_path, se.cost_usd_raw, se.speed, "
        "se.cache_create_1h_tokens, se.line_offset, se.account_key "
        "FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE se.timestamp_utc >= ? AND se.timestamp_utc <= ?"
    )
    params = [start_iso, end_iso]
    if project is not None:
        escaped = (
            project.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        )
        sql += r" AND se.source_path LIKE ? ESCAPE '\'"
        params.append(f"%/projects/{escaped}/%")
    if account_key is not None:
        import _lib_accounts
        if account_key == _lib_accounts.UNATTRIBUTED:
            sql += " AND (se.account_key IS NULL OR se.account_key = ?)"
            params.append(_lib_accounts.UNATTRIBUTED)
        else:
            sql += " AND se.account_key = ?"
            params.append(account_key)
    sql += " ORDER BY se.timestamp_utc ASC, se.id ASC"
    out = []
    for row in cache_conn.execute(sql, params):
        entry = cache._JoinedClaudeEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            model=row[1],
            input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            cache_creation_tokens=int(row[4] or 0),
            cache_read_tokens=int(row[5] or 0),
            source_path=row[6],
            session_id=row[7],
            project_path=row[8],
            cost_usd=row[9],
            usage_extra=({"speed": row[10]} if row[10] else None),
            cache_1h_tokens=(None if row[11] is None else int(row[11])),
        )
        # #869: the retained source identity and account of the entry. The
        # cost path never reads these; the historical-close proof fingerprints
        # the exact population and requires every entry to be the close's.
        entry.source_line_offset = row[12]
        entry.source_account_key = row[13]
        out.append(entry)
    return out


@contextlib.contextmanager
def _scratch_read_adapters(cache_conn: sqlite3.Connection):
    """Temporarily route current cost/config readers to stable supplied inputs."""
    cctally = sys.modules["cctally"]
    cache = sys.modules["_cctally_cache"]
    replacements = {
        "get_entries": lambda start, end, *, project=None, skip_sync=False,
            account_key=None: cache.iter_entries(
                cache_conn, start, end, project=project, account_key=account_key),
        "get_claude_session_entries": (
            lambda start, end, *, project=None, skip_sync=False, account_key=None:
            _joined_entries(
                cache_conn, start, end, project=project, account_key=account_key)
        ),
        # No historical alert/budget config exists in the journal. The family
        # registry classifies those latches as re-materialized projections.
        "load_config": lambda *args, **kwargs: {},
    }
    prior = {name: getattr(cctally, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(cctally, name, value)
        yield
    finally:
        for name, value in prior.items():
            setattr(cctally, name, value)


#: Op kinds the scratch replay runs through the pipeline hooks.
_REDERIVABLE_OP_KINDS = frozenset({
    "weekly_credit_floor", "account_observe", "account_label",
    "accounts_cutover", "sync_week",
})


def _is_rederivable_raw(record: dict) -> bool:
    """Whether the scratch replay runs ``record`` through the pipeline hooks:
    a Claude reading from a recording source, or a deriving/registry op
    (#997 spec D5: a per-record predicate, so the replay can test a record it
    prepared one at a time)."""
    t = record.get("t")
    if t == "obs":
        return (record.get("provider") == "claude"
                and record.get("src") in _record._CLAUDE_OBS_SRCS)
    if t != "op":
        return False
    kind = (record.get("payload") or {}).get("kind")
    return kind in _REDERIVABLE_OP_KINDS or kind in _REVIEWED_WEEKLY_KINDS


def _rederivable_raw_records(records: list[dict]) -> list[dict]:
    return [record for record in records if _is_rederivable_raw(record)]


#: #825/#832 spec 5.1: the previous release validates a reviewed weekly
#: decision by exact equality with its own `make_op`, which a stamped record can
#: never satisfy, so the current binary emits the versioned kind (an unknown op
#: to the previous release) and reads both.
_REVIEWED_WEEKLY_KIND = _lib_account_protocol.WEEKLY_OBSERVATION_DECISION_V2_KIND
_REVIEWED_WEEKLY_LEGACY_KIND = "claude_weekly_observation_decision"
_REVIEWED_WEEKLY_KINDS = frozenset((
    _REVIEWED_WEEKLY_LEGACY_KIND, _REVIEWED_WEEKLY_KIND))
_REVIEWED_MANIFEST_KEYS_V1 = frozenset({
    "schemaVersion", "journalHighWater", "journalPrefixHash",
    "reviewedAt", "reason", "decisions",
})
_REVIEWED_MANIFEST_KEYS_V2 = frozenset({
    "schemaVersion", "journalHighWater", "journalPrefixHash",
    "reviewedAt", "reason", "weeklyAxisDecisions",
    "snapshotIdentityDecisions",
})
_REVIEWED_MANIFEST_EXPECTATION_KEYS = frozenset({
    "expectedBaselinePlanHash", "expectedDecisionPlanHash",
})
_REVIEWED_OP_EXPECTATION_KEYS = frozenset({
    "expected_baseline_plan_hash", "expected_decision_plan_hash",
})
_REVIEWED_OP_KEYS_V1 = frozenset({
    "kind", "schema_version", "journal_high_water",
    "journal_prefix_hash", "reason", "decisions",
}) | _REVIEWED_OP_EXPECTATION_KEYS
_REVIEWED_OP_KEYS_V2 = frozenset({
    "kind", "schema_version", "journal_high_water",
    "journal_prefix_hash", "reason", "weekly_axis_decisions",
    "snapshot_identity_decisions",
}) | _REVIEWED_OP_EXPECTATION_KEYS
#: #797/#823/#840 spec 4.7 (Q5): payload schema 3 adds the canonical
#: `weekly_axis_retractions` list beside the hold projection. Manifest schema 3
#: keeps schema 2's keys and admits the `retract` disposition.
_REVIEWED_OP_KEYS_V3 = _REVIEWED_OP_KEYS_V2 | frozenset({
    "weekly_axis_retractions",
})
_REVIEWED_MANIFEST_DISPOSITIONS = {
    1: ("hold", "accept"), 2: ("hold", "accept"),
    3: ("hold", "accept", "retract"),
}


def _reviewed_weekly_op_from_manifest(path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise _lib_rederive.RederiveConflict(
            f"invalid reviewed weekly decision manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has unexpected fields"
        )
    version = manifest.get("schemaVersion")
    expected_keys = (
        _REVIEWED_MANIFEST_KEYS_V1 if version == 1
        else _REVIEWED_MANIFEST_KEYS_V2 if version in (2, 3)
        else None
    )
    if type(version) is not int:
        expected_keys = None
    actual_keys = set(manifest)
    if (expected_keys is None
            or actual_keys not in (
                expected_keys,
                expected_keys | _REVIEWED_MANIFEST_EXPECTATION_KEYS,
            )):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest requires schemaVersion 1, 2 "
            "or 3 with exactly its fields"
        )
    for key in _REVIEWED_MANIFEST_EXPECTATION_KEYS & actual_keys:
        value = manifest[key]
        if (not isinstance(value, str) or len(value) != 71
                or not value.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in value[7:])):
            raise _lib_rederive.RederiveConflict(
                f"reviewed weekly decision manifest has invalid {key}"
            )
    hw = manifest["journalHighWater"]
    if (not isinstance(hw, dict) or set(hw) != {"segment", "offset"}
            or not isinstance(hw["segment"], str) or not hw["segment"]
            or type(hw["offset"]) is not int or hw["offset"] < 0):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has invalid journalHighWater"
        )
    digest = manifest["journalPrefixHash"]
    if (not isinstance(digest, str) or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has invalid journalPrefixHash"
        )
    reviewed_at = manifest["reviewedAt"]
    if not isinstance(reviewed_at, str):
        raise _lib_rederive.RederiveConflict("reviewedAt must be UTC ISO-Z")
    try:
        parsed = dt.datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _lib_rederive.RederiveConflict(
            "reviewedAt must be UTC ISO-Z"
        ) from exc
    if (parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0)
            or parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
            != reviewed_at):
        raise _lib_rederive.RederiveConflict("reviewedAt must be UTC ISO-Z")
    reason = manifest["reason"]
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision reason must be nonempty"
        )
    decisions = (
        manifest["decisions"] if version == 1
        else manifest["weeklyAxisDecisions"]
    )
    identity_decisions = (
        [] if version == 1 else manifest["snapshotIdentityDecisions"]
    )
    if (not isinstance(decisions, list)
            or not isinstance(identity_decisions, list)
            or not decisions and not identity_decisions):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decisions must select at least one decision"
        )
    seen = set()
    normalized = []
    for item in decisions:
        if (not isinstance(item, dict)
                or set(item) != {"observationId", "disposition"}
                or not isinstance(item["observationId"], str)
                or not item["observationId"]
                or item["disposition"]
                not in _REVIEWED_MANIFEST_DISPOSITIONS[version]):
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision entry is malformed"
            )
        obs_id = item["observationId"]
        if obs_id in seen:
            raise _lib_rederive.RederiveConflict(
                f"duplicate reviewed weekly observation: {obs_id}"
            )
        seen.add(obs_id)
        normalized.append({
            "observationId": obs_id,
            "disposition": item["disposition"],
        })
    identity_ids = set()
    normalized_identity = []
    for item in identity_decisions:
        if (not isinstance(item, dict)
                or set(item) != {
                    "acceptedObservationId", "replayObservationId",
                    "disposition",
                }
                or not all(
                    isinstance(item[key], str) and item[key]
                    for key in (
                        "acceptedObservationId", "replayObservationId",
                    )
                )
                or item["disposition"] not in ("preserve", "rederive")
                or item["acceptedObservationId"] == item["replayObservationId"]
                or item["acceptedObservationId"] in identity_ids
                or item["replayObservationId"] in identity_ids):
            raise _lib_rederive.RederiveConflict(
                "snapshot identity decision is malformed, repeated, or ambiguous"
            )
        identity_ids.update((
            item["acceptedObservationId"], item["replayObservationId"],
        ))
        normalized_identity.append(dict(item))
    base = {
        "kind": _REVIEWED_WEEKLY_KIND,
        "schema_version": version,
        "journal_high_water": dict(hw),
        "journal_prefix_hash": digest,
        "reason": reason,
    }
    # #797/#823/#840 spec 4.7: a retraction persists as an ordinary `hold` in
    # the decision list plus the canonical `weekly_axis_retractions` list.
    payload = _lib_rederive.reviewed_weekly_payload(
        version=version, decisions=normalized, base=base)
    if version != 1:
        payload["snapshot_identity_decisions"] = sorted(
            normalized_identity,
            key=lambda item: (
                item["acceptedObservationId"], item["replayObservationId"],
            ),
        )
    if _REVIEWED_MANIFEST_EXPECTATION_KEYS <= actual_keys:
        payload.update({
            "expected_baseline_plan_hash": (
                manifest["expectedBaselinePlanHash"]
            ),
            "expected_decision_plan_hash": (
                manifest["expectedDecisionPlanHash"]
            ),
        })
    op = _lib_journal.make_op(
        at=reviewed_at,
        src="rederive",
        payload=payload,
    )
    if len(_lib_journal.encode_line(op)) > _journal._MAX_LINE_BYTES:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision exceeds journal line limit"
        )
    return op


def _reviewed_weekly_expected_hashes(path, *, required: bool):
    """Read plan pins that a successful apply also writes into its op."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise _lib_rederive.RederiveConflict(
            f"invalid reviewed weekly decision manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has unexpected fields"
        )
    version = manifest.get("schemaVersion")
    if version not in (1, 2, 3) or type(version) is not int:
        return None
    present = _REVIEWED_MANIFEST_EXPECTATION_KEYS & set(manifest)
    if not present:
        if required:
            raise _lib_rederive.RederiveConflict(
                f"schemaVersion {version} apply requires "
                "expectedBaselinePlanHash and expectedDecisionPlanHash "
                "from a reviewed preview"
            )
        return None
    if present != _REVIEWED_MANIFEST_EXPECTATION_KEYS:
        raise _lib_rederive.RederiveConflict(
            f"schemaVersion {version} apply requires both reviewed plan hashes"
        )
    for key in present:
        value = manifest[key]
        if (not isinstance(value, str) or len(value) != 71
                or not value.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in value[7:])):
            raise _lib_rederive.RederiveConflict(
                f"reviewed weekly decision manifest has invalid {key}"
            )
    return (
        manifest["expectedBaselinePlanHash"],
        manifest["expectedDecisionPlanHash"],
    )


def _reviewed_op_expected_hashes(record, *, required: bool):
    payload = record.get("payload") or {}
    present = _REVIEWED_OP_EXPECTATION_KEYS & set(payload)
    if not present:
        if required:
            raise _lib_rederive.RederiveConflict(
                "retained reviewed weekly decision lacks durable plan hashes"
            )
        return None
    if present != _REVIEWED_OP_EXPECTATION_KEYS:
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision has incomplete plan hashes"
        )
    values = (
        payload["expected_baseline_plan_hash"],
        payload["expected_decision_plan_hash"],
    )
    if any(
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in value[7:])
        for value in values
    ):
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision has invalid plan hashes"
        )
    return values


def _reviewed_weekly_decisions(records):
    """``(effective, identity_pairs, ops)`` of the retained reviewed weekly
    decisions of ``records``: the effective disposition per observation
    (``hold``, ``accept`` or ``retract``; the latest logical decision wins),
    the preserved snapshot identity pairs, and the ops in journal order.
    Every payload goes through the one strict decoder
    (`_lib_rederive.decode_reviewed_weekly_payload`)."""
    raw_ids = {
        record.get("id") for record in records
        if record.get("t") == "obs"
        and record.get("provider") == "claude"
        and record.get("src") in _record._CLAUDE_OBS_SRCS
        and "weekly_percent" in (record.get("payload") or {})
        and "resets_at" in (record.get("payload") or {})
    }
    effective = {}
    identity_effective = {}
    ops = []
    for record in records:
        if (record.get("t") != "op" or
                (record.get("payload") or {}).get("kind")
                not in _REVIEWED_WEEKLY_KINDS):
            continue
        decoded = _lib_rederive.decode_reviewed_weekly_payload(
            record["payload"])
        for obs_id, disposition in decoded.dispositions.items():
            if obs_id not in raw_ids:
                raise _lib_rederive.RederiveConflict(
                    f"unknown or duplicate reviewed weekly observation: {obs_id}"
                )
            effective[obs_id] = disposition
        for item in decoded.identity_decisions:
            identity_effective[
                (item["acceptedObservationId"], item["replayObservationId"])
            ] = item["disposition"]
        ops.append(record)
    return effective, tuple(
        pair for pair, disposition in identity_effective.items()
        if disposition == "preserve"
    ), ops


def _reviewed_weekly_state(records):
    """``(held, accepted, identity_pairs, ops)``. ``held`` is every
    observation whose weekly detection the reviewed replay suppresses: an
    effective `hold` or `retract` (spec 4.7: a retraction is suppressed
    exactly as a hold; only hold's dependency preservation tells them
    apart, `_reviewed_weekly_retractions`)."""
    effective, identity_pairs, ops = _reviewed_weekly_decisions(records)
    return (frozenset(
        obs_id for obs_id, disposition in effective.items()
        if disposition in ("hold", "retract")
    ), frozenset(
        obs_id for obs_id, disposition in effective.items()
        if disposition == "accept"
    ), identity_pairs, ops)


def _reviewed_weekly_retractions(records) -> frozenset:
    """The observations whose effective reviewed weekly decision is
    `retract`."""
    effective, _pairs, _ops = _reviewed_weekly_decisions(records)
    return frozenset(obs_id for obs_id, disposition in effective.items()
                     if disposition == "retract")


class _PinnedPrefixDigest:
    """The one prefix digest a deferred reviewed-decision validation needs,
    captured inline by the reading pass."""

    def __init__(self, high_water, digest):
        self._high_water = high_water
        self._digest = digest

    def digest_at(self, high_water):
        return self._digest if high_water == self._high_water else None


def _validate_retained_reviewed_op(record, prior_high_water, hasher) -> None:
    """Verify an operator decision against the bytes before its journal line."""
    payload = record.get("payload")
    if (not isinstance(payload, dict)
            or payload.get("kind") not in _REVIEWED_WEEKLY_KINDS):
        return
    bound = payload.get("journal_high_water")
    version = payload.get("schema_version")
    expected_keys = (
        _REVIEWED_OP_KEYS_V1 if version == 1
        else _REVIEWED_OP_KEYS_V2 if version == 2
        else _REVIEWED_OP_KEYS_V3 if version == 3
        else None
    )
    if (expected_keys is None or set(payload) != expected_keys
            or not isinstance(bound, dict)
            or set(bound) != {"segment", "offset"}
            or type(bound.get("segment")) is not str
            or type(bound.get("offset")) is not int
            or not isinstance(payload.get("journal_prefix_hash"), str)
            or not isinstance(payload.get("reason"), str)
            or not payload["reason"].strip()
            or record != _lib_journal.make_op(
                at=record.get("at"), src=record.get("src"), payload=payload,
                writer_generation=record.get("wpg"),
                # #797/#823/#840 (spec 4.6 B1): the protocol revision is part
                # of a current decision's content-id core.
                writer_revision=record.get("wpr"),
            )):
        raise _lib_rederive.RederiveConflict(
            "malformed retained reviewed weekly decision op"
        )
    # #797/#823/#840 spec 4.7: the one strict decoder (schema 3's
    # retractions must each name a same-op projected hold).
    _lib_rederive.decode_reviewed_weekly_payload(payload)
    _reviewed_op_expected_hashes(record, required=True)
    expected = (bound["segment"], bound["offset"])
    if prior_high_water != expected or hasher.digest_at(expected) != (
        payload["journal_prefix_hash"]
    ):
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision prefix does not match its "
            "preceding journal bytes"
        )


def _planner_cutover_claude(records) -> str:
    """The journal-wide Claude legacy account the planner normalizes under:
    every `accounts_cutover` value validated first (missing or conflicting
    values refuse), `unattributed` when the prefix has no cutover."""
    import _lib_accounts

    cutover_values = {
        (record.get("payload") or {}).get("claude_legacy_account")
        for record in records
        if record.get("t") == "op"
        and (record.get("payload") or {}).get("kind") == "accounts_cutover"
    }
    if None in cutover_values:
        raise _lib_rederive.RederiveConflict(
            "accounts_cutover is missing claude_legacy_account"
        )
    if len(cutover_values) > 1:
        raise _lib_rederive.RederiveConflict(
            "conflicting accounts_cutover Claude account values"
        )
    return (
        next(iter(cutover_values))
        if cutover_values
        else _lib_accounts.UNATTRIBUTED
    )


def _normalize_legacy_accounts(records: list[dict]) -> None:
    """Normalize every record of ``records`` IN PLACE (the pre-#997 planner
    input; kept for callers that own a private copy)."""
    cutover_claude = _planner_cutover_claude(records)
    for record in records:
        _journal._normalize_legacy_account_stamp(record, cutover_claude)


def _normalized_planning_view(records, cutover_claude=None) -> list:
    """#997 spec D5: the planner's normalized records, copy-on-write.

    The same global pass as `_normalize_legacy_accounts` (every cutover value
    validated first), but a record normalization leaves unchanged is SHARED
    with the caller, and a changed one is a normalized deep copy. Changed
    copies are memoized by source-object identity, so one shared object at
    many positions (a D4 stub) yields one shared normalized object. Order,
    multiplicity and positions are preserved and the caller's records, which
    `apply_db_rederive` reads afterwards, are never mutated. Peak memory loses
    the up-front deep copy of every record normalization leaves alone."""
    if cutover_claude is None:
        cutover_claude = _planner_cutover_claude(records)
    changed: dict = {}
    view = []
    for record in records:
        if not isinstance(record, dict):
            view.append(record)
            continue
        hit = changed.get(id(record))
        if hit is not None:
            view.append(hit)
            continue
        probe = dict(record)
        if isinstance(record.get("payload"), dict):
            probe["payload"] = dict(record["payload"])
        _journal._normalize_legacy_account_stamp(probe, cutover_claude)
        if probe == record:
            view.append(record)
            continue
        normalized = copy.deepcopy(record)
        _journal._normalize_legacy_account_stamp(normalized, cutover_claude)
        changed[id(record)] = normalized
        view.append(normalized)
    return view


def _derive_desired_events(records: list[dict], cache_conn: sqlite3.Connection,
                           scratch_dir: Path, *,
                           held_weekly_observation_ids=frozenset(),
                           block_sources: "dict | None" = None) -> list[dict]:
    # #386: this replays the whole ingest pipeline into a PRIVATE scratch index
    # and is reached from `db rederive`'s PREVIEW, which takes no locks by
    # design (its contract is zero persistent writes to the live family). The
    # connection is authorizer-armed like every other `open_db` handle, so the
    # scratch replay has to declare itself sanctioned — otherwise a write-free
    # preview is denied for writing to its own temp file. The scope is entered
    # around the replay only; nothing here touches DB_PATH.
    import _cctally_store

    scratch_path = scratch_dir / "stats.rederive.db"
    with _cctally_store.stats_write_scope("rederive-derive"):
        return _derive_desired_events_into(
            records, cache_conn, str(scratch_path),
            held_weekly_observation_ids=held_weekly_observation_ids,
            block_sources=block_sources)


def _derive_desired_events_into(
    records, cache_conn, scratch_path, *,
    held_weekly_observation_ids=frozenset(),
    block_sources: "dict | None" = None,
) -> list[dict]:
    raw_records = _rederivable_raw_records(records)
    held_ids = frozenset(held_weekly_observation_ids)
    valid_ids = {
        record.get("id") for record in raw_records
        if record.get("t") == "obs"
        and "weekly_percent" in (record.get("payload") or {})
        and "resets_at" in (record.get("payload") or {})
    }
    unknown_ids = held_ids - valid_ids
    if unknown_ids:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly hold names no Claude raw observation: "
            + ", ".join(sorted(unknown_ids))
        )
    conn = _cctally_core.open_db(_target_path=str(scratch_path))
    # #997 (spec D1): a disposable scratch, never published or trusted.
    import _cctally_store
    _cctally_store.apply_disposable_scratch_policy(conn)
    # #825/#832 spec 5.6 item 8: the scratch replay re-evaluates the same
    # triggers and certificates over the corrected history in journal order,
    # so it can never plan an outcome ordinary processing withholds.
    import _cctally_quarantine
    fold = _cctally_quarantine.QuarantineFold.empty(held_ids=held_ids)
    try:
        # #997 spec D5: the planner's view shares unchanged records with the
        # caller, so the replay prepares a private copy of each record.
        return replay_scratch_records(
            conn, records, cache_conn, fold=fold, held_ids=held_ids,
            block_sources=block_sources, prepare=copy.deepcopy)
    finally:
        conn.close()


#: #997 (spec D2): modifying inputs per scratch replay transaction — raw inputs
#: plus retained `evt`/`correction` offers. One input per commit made every
#: input pay a rollback-journal create/fsync/unlink and a database fsync.
_SCRATCH_REPLAY_COMMIT_EVERY = 4096


def replay_scratch_records(
    conn, records, cache_conn, *, fold, held_ids=frozenset(),
    block_sources: "dict | None" = None, coordinates=None,
    skip_indexes=frozenset(), on_input=None, read_adapters=None,
    retention=None, prepare: "Callable[[dict], dict] | None" = None,
) -> list[dict]:
    """THE scratch derivation: run every rederivable raw record of
    ``records`` through the live pipeline hooks and harvest, in journal
    order, on the private scratch connection ``conn``, capturing every derived
    candidate instead of journaling it (`db rederive` and the #825/#832
    derivation-completion step share it, so there is one detector
    implementation).

    ``fold`` is the outcome-quarantine fold, advanced over every slot —
    excluded placeholders included — so each input's gate is evaluated at its
    original position. ``coordinates`` (parallel to ``records``, each
    `(segment, offset)` or None) feeds the fold the physical positions a live
    ingest gives it. An index in ``skip_indexes`` still advances the fold but
    its hooks do not run. ``on_input(index, events)`` receives each raw input's
    own derived candidates. ``read_adapters`` replaces the stable cost/config
    read adapters over ``cache_conn`` (a context manager), for a caller with no
    cache to read. ``retention`` (derivation completion only, a
    `_cctally_journal.ScratchRetention`) keeps the scratch projection on the
    retained effective decisions: the emitters consult it, and every
    ``evt``/``correction`` line of ``records`` is offered to it at its own
    position. ``prepare`` (#997 spec D5) maps each record to the one the
    fold, the retention and the hooks see, immediately before it is offered
    (a per-record copy instead of a whole-list one); the raw-input test runs
    on the prepared record. Returns every candidate in derivation order."""
    events: list[dict] = []
    reviewed_weekly_basis_by_account = {}
    hooks = (
        _journal._pipeline_op_fold,
        _record._pipeline_claude_usage,
        _record._pipeline_record_credit,
        _record._pipeline_sync_week,
    )
    import _lib_account_protocol as _protocol
    adapters = (_scratch_read_adapters(cache_conn) if read_adapters is None
                else read_adapters)
    # #997 (spec D2): one transaction holds up to
    # `_SCRATCH_REPLAY_COMMIT_EVERY` modifying inputs. Only the commit boundary
    # moves: each raw input keeps its own `IngestContext` and `_harvest`, in
    # journal order, and every reader shares this connection, which sees its
    # own uncommitted writes. On any exception the open batch rolls back and
    # the caller discards the scratch.
    bound = max(1, int(_SCRATCH_REPLAY_COMMIT_EVERY))
    pending = 0

    def _open_batch():
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")

    inputs = 0
    commits = 0

    def _count_input():
        nonlocal pending, inputs, commits
        pending += 1
        inputs += 1
        if pending >= bound:
            conn.commit()
            pending = 0
            commits += 1
            # #997 D9: every commit batch (throttled to one write per ten
            # seconds; a no-op unless a worker installed a progress sink).
            _lib_rebuild_progress.tick(inputs=inputs, commits=commits)
        elif not inputs & 1023:
            _lib_rebuild_progress.tick(inputs=inputs, commits=commits)

    try:
        with adapters:
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    continue
                if prepare is not None:
                    record = prepare(record)
                if record.get("t") == _protocol.EXCLUDED_PLACEHOLDER_TYPE:
                    carried = record.get("quarantine") or {}
                    fold.on_excluded(
                        carried.get("record") or {},
                        segment=carried.get("segment"),
                        offset=carried.get("offset"))
                    continue
                if (retention is not None
                        and record.get("t") in ("evt", "correction")):
                    _open_batch()
                    retention.on_journaled(conn, record)
                    _count_input()
                coordinate = (None if coordinates is None
                              else coordinates[index])
                if coordinate is None:
                    gate = fold.on_record(record)
                else:
                    gate = fold.on_record(
                        record, segment=coordinate[0], offset=coordinate[1])
                if not _is_rederivable_raw(record) or index in skip_indexes:
                    continue
                before = len(events)
                ctx = _journal.IngestContext(
                    conn=conn,
                    batch=[record],
                    config={},
                    event_sink=events,
                    projection_writes=False,
                    held_weekly_observation_ids=held_ids,
                    reviewed_weekly_basis_by_account=(
                        reviewed_weekly_basis_by_account),
                    block_source_evidence=block_sources,
                    quarantine=fold,
                    outcome_gate=gate,
                    scratch_retention=retention,
                )
                if retention is not None:
                    retention.current_input = index
                _open_batch()
                try:
                    for hook in hooks:
                        hook(ctx, record)
                    _journal._harvest(ctx)
                finally:
                    if retention is not None:
                        retention.current_input = None
                _count_input()
                if on_input is not None:
                    on_input(index, events[before:])
        if conn.in_transaction:
            conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return events


def _planner_op_kinds() -> "set[str]":
    """Every `op` kind the planner validates against `_OP_CLASSIFICATIONS`.

    One definition, so a test can assert what the planner ACTUALLY validates
    rather than re-deriving the same union beside it (review round 2, finding
    R2-6). A test that rebuilds the expression locally is a membership check
    against a set it constructed itself: it passes whatever this module does.

    `sync_week` is named literally because it is journaled as an `op` with no
    fold applier and no accounts machinery entry, so neither registry carries
    it, and an unclassified op kind raises `RederiveConflict`.
    """
    return (
        set(_journal.FOLD_APPLIERS)
        | set(_journal._ACCOUNTS_MACHINERY_KINDS)
        | {"sync_week"}
    )


def plan_claude_usage(
    records,
    *,
    cache_conn: sqlite3.Connection,
    journal_high_water: "tuple[str, int] | None",
    protocol_prefix_evidence=(),
    enforce_guard=True,
):
    """Produce a deterministic Task-A-compatible plan without durable writes.

    ``records`` must be the canonical journal prefix ending at
    ``journal_high_water``. ``cache_conn`` must be a caller-held stable SQLite
    read view. Task C owns lock/snapshot orchestration; Task B owns the pure
    replay and comparison contract.
    """
    # #997 spec D5: a copy-on-write normalized view, not a whole-list deep
    # copy. Every planner consumer below reads this view.
    records = _normalized_planning_view(list(records))
    report = _lib_rederive.validate_family_registry(
        evt_kinds=set(_journal._EVT_SPECS),
        op_kinds=_planner_op_kinds(),
    )
    if report.unclassified_evt_kinds or report.unclassified_op_kinds:
        raise _lib_rederive.RederiveConflict(
            "unclassified journal kind(s): evt="
            + ",".join(report.unclassified_evt_kinds)
            + " op=" + ",".join(report.unclassified_op_kinds)
        )
    tables = _cache_contract(cache_conn)
    _lib_rederive.validate_claude_cache_contract(tables)
    codex_pricing_records = any(
        (record.get("payload") or {}).get("vendor") == "codex"
        and (record.get("payload") or {}).get("kind") == "budget"
        and (record.get("payload") or {}).get("_pricing")
        for record in records
    )
    if codex_pricing_records:
        # #874: the Codex fingerprint and re-pricing read the Codex cache only
        # here, so its contract is required only here, and before any reader.
        _lib_rederive.validate_codex_cache_contract(tables)
    raw_records = _rederivable_raw_records(records)
    _validate_cache_rows(cache_conn, raw_records)
    cache_fingerprint = _cache_fingerprint(
        cache_conn, include_codex=codex_pricing_records,
    )
    held_ids, _accepted_ids, identity_pairs, reviewed_ops = (
        _reviewed_weekly_state(records)
    )
    # #797/#823/#840 spec 4.7 (D4): a retraction is suppressed in the replay
    # like a hold (it is in `held_ids`) but is not in hold's
    # dependency-preservation set.
    retracted_ids = _reviewed_weekly_retractions(records)
    config_basis = {
        "historicalConfig": "not-retained",
        "projectionPolicy": "retire-and-rematerialize",
    }
    if reviewed_ops:
        config_basis["reviewedWeeklyDecisionOps"] = []
        for op in reviewed_ops:
            # Durable plan pins authenticate this derived plan. Feeding them
            # back into its own config fingerprint would make the plan hash
            # self-referential, so planning uses the decision's pin-free
            # logical identity while the complete journal record remains
            # content-addressed and validates the pins on recovery.
            payload = {
                key: value for key, value in op["payload"].items()
                if key not in _REVIEWED_OP_EXPECTATION_KEYS
            }
            # #797/#823/#840 spec 4.6 B1: the logical identity stays the
            # revision-1 one, so a plan an earlier writer pinned keeps its
            # config fingerprint across the revision-2 stamp.
            logical = _lib_journal.make_op(
                at=op["at"], src=op["src"], payload=payload,
                writer_revision=None,
            )
            config_basis["reviewedWeeklyDecisionOps"].append({
                "id": logical["id"], "at": logical["at"],
                "payload": logical["payload"],
            })
    config_fingerprint = _fingerprint(config_basis)
    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=protocol_prefix_evidence,
    )
    tainted = [
        *selection.protocol_violations,
        *selection.acknowledged_protocol_violations,
    ]
    if tainted:
        summary = ", ".join(
            f"{violation.batch_id}:{violation.kind}"
            for violation in tainted[:10]
        )
        raise _lib_rederive.RederiveConflict(
            "journal contains tainted correction batch(es): " + summary
        )
    # Only an unmarked close with a zero-cost model can need the historical
    # proof, so the scratch replay keeps retained entries for those blocks
    # alone rather than for every block in the journal.
    block_sources = dict.fromkeys(_historical_close_candidates(selection))
    with tempfile.TemporaryDirectory(prefix="cctally-rederive-") as tmp:
        desired = _derive_desired_events(
            records, cache_conn, Path(tmp),
            held_weekly_observation_ids=held_ids,
            block_sources=block_sources,
        )
    historical_markers = _historical_close_markers(
        selection, desired, block_sources)
    codex_uncertain: set = set()
    desired.extend(_desired_codex_budget_costs(
        selection, cache_conn, uncertain=codex_uncertain))
    # #426: the scratch replay only sees RETAINED sources, so it can never
    # reproduce the pre-cutover rows the journal exported as `b:<table>:<rowid>`
    # evt lines. Hold them out of the diff instead of retiring them. Evidence is
    # counted in OBSERVATIONS alone — an operator record is replay input but
    # derives nothing on its own, and the cutover re-emits some of them
    # (`weekly_credit_floor`) carrying their original historical timestamp.
    preserved = _lib_rederive.preserved_history(
        records,
        evidence_retained=any(
            record.get("t") == "obs" for record in raw_records
        ),
    )
    return _lib_rederive.build_claude_usage_plan(
        selection=selection,
        physical_revisions=_excluded_physical_revisions(records),
        desired_events=desired,
        journal_high_water=journal_high_water,
        cache_fingerprint=cache_fingerprint,
        config_fingerprint=config_fingerprint,
        preserved_events=preserved.values(),
        conflicted_event_ids=owned_conflicted_event_ids(selection),
        raw_observations=raw_records,
        snapshot_identity_decisions=identity_pairs,
        reviewed_weekly_hold_ids=held_ids - retracted_ids,
        reviewed_weekly_retraction_ids=retracted_ids,
        uncertain_cost_fact_ids=(
            _uncertain_cost_facts(selection) | codex_uncertain),
        historical_close_markers=historical_markers,
        enforce_guard=enforce_guard,
    )


def _excluded_physical_revisions(records) -> dict:
    """The highest revision per id among the excluded events and correction
    actions the placeholders of ``records`` carry (#825/#832 fix round A,
    Codex fix-round-1 Q2)."""
    import _lib_account_protocol as _protocol
    out: dict = {}
    for record in records:
        if (not isinstance(record, dict)
                or record.get("t") != _protocol.EXCLUDED_PLACEHOLDER_TYPE):
            continue
        carried = (record.get("quarantine") or {}).get("record") or {}
        event_id = carried.get("id")
        rev = carried.get("rev", 0)
        if (carried.get("t") in ("evt", "correction")
                and isinstance(event_id, str) and type(rev) is int
                and rev >= 0):
            out[event_id] = max(rev, out.get(event_id, rev))
    return out


def owned_conflicted_event_ids(selection) -> frozenset:
    """The quarantined same-revision groups (#374) this family owns.

    Scoped two ways: the selector already filters `conflicts` to the WINNING
    revision (a group a completed rev-1 batch superseded is resolved, not
    outstanding), and this filters to events `claude-usage` re-derives — a
    conflict in a retained family (`quota_alert_arming`) or an unknown kind must
    force no action, because a correction this family cannot re-derive would be
    a fabrication."""
    owned = set()
    for conflict in getattr(selection, "conflicts", ()):
        selected = selection.by_id.get(conflict.event_id)
        if selected is None or selected.record is None:
            continue
        if _lib_rederive._is_owned_event(selected.record):
            owned.add(conflict.event_id)
    return frozenset(owned)


def _is_inert_codex_obs(record) -> bool:
    """An admitted Codex quota observation: always admitted (its family is
    Codex-only), never a rederivable raw input, never a Claude reading."""
    return (record.get("t") == "obs"
            and record.get("provider") == "codex"
            and (record.get("payload") or {}).get("kind")
            == "quota_window_snapshot")


def _inert_codex_stub(record: dict, table: "dict | None" = None) -> dict:
    """#997 spec D4: the read-only stand-in for an inert Codex quota
    observation on the rederive path — `t`, `provider`, `src`, `account` and
    a `payload` of `kind` plus `account_key`/`vendor` when present, which is
    everything a consumer on this path reads (admission, positional protocol
    selection, `_is_rederivable_raw`, `is_claude_reading`, legacy account
    classification and normalization, the Codex pricing scan, preservation
    and the reviewed-decision suffix checks). Equal key tuples
    `(src, account, payload.account_key, payload.vendor)` share one object
    from ``table`` (the reader's per-read intern table), so 2.19 M positions
    cost a pointer each. A stub is never mutated: the planner's normalized
    view copies it once (`_normalized_planning_view`)."""
    payload = record.get("payload") or {}
    key = (record.get("src"),
           "account" in record, record.get("account"),
           "account_key" in payload, payload.get("account_key"),
           "vendor" in payload, payload.get("vendor"))
    try:
        stub = None if table is None else table.get(key)
    except TypeError:  # an unhashable stamp: keep the record as it is
        return record
    if stub is not None:
        return stub
    stub_payload = {"kind": payload.get("kind")}
    for name in ("account_key", "vendor"):
        if name in payload:
            stub_payload[name] = payload[name]
    stub = {"t": "obs", "provider": "codex", "src": record.get("src")}
    if "account" in record:
        stub["account"] = record["account"]
    stub["payload"] = stub_payload
    if table is not None:
        table[key] = stub
    return stub


def read_rederive_journal_prefix(
    high_water: "tuple[str, int] | None" = None,
):
    """Stream and strictly decode one canonical journal prefix.

    Returns `(records, high_water, record_ends, protocol_prefix_evidence)`.
    The evidence digests come from a `PrefixHashAccumulator` fed by the bytes
    this pass is already reading. Before this, the prefix was materialized as
    raw lines, materialized again as decoded records while the first form was
    still referenced, walked a third time to produce the evidence, and re-read
    from byte zero by `journal_prefix_hash` once per
    `journal_protocol_resolution` op (#496 S5 §4).

    Retention is DELIBERATELY unfiltered, unlike the rebuild's. The rebuild
    keeps only the decision records and substitutes `None` placeholders for
    everything else; the planner here reads every observation for cache
    validation, desired-event derivation and preservation decisions, and walks
    `records` in parallel with `record_ends`. Both lists stay complete and
    aligned, so the win in this file is the removed double materialization and
    the removed hash traversals, not reduced retention.

    #997 spec D4: positions are kept, but an admitted Codex quota observation
    is retained as an interned read-only stub (`_inert_codex_stub`) rather
    than decoded in full; no consumer on this path reads its quota payload,
    `id` or `at`.
    """
    if high_water is None:
        high_water = _journal.journal_high_water()
    if high_water is None:
        return [], None, [], ()
    segments = _journal.list_segments()
    if high_water[0] not in segments:
        raise OSError(
            f"journal high-water segment is unavailable: {high_water[0]}"
        )
    records: list[dict] = []
    record_ends: list[tuple[str, int]] = []
    evidence: list = []
    evidence_owners: list = []
    malformed = 0
    prior_high_water = None
    hasher = _journal._new_prefix_hasher()
    # #825/#832 (spec 5.3): admission before interpretation, resolved within
    # this one pass. A reviewed decision's validation needs the prefix digest
    # at its own position, so the digest is taken inline and the validation
    # runs after the pass, for admitted decisions only.
    pass_boundary = _journal._PassBoundary()
    coordinates: list = []
    pending_reviewed: list = []
    inert_stubs: dict = {}
    lines = 0
    for segment, offset, raw in _journal._iter_range_with_segments(
        None,
        high_water,
        segments,
        on_segment=lambda name, size: hasher.begin_segment(
            name, prior_high_water, size=size),
        on_bytes=hasher.extend,
    ):
        lines += 1
        if not lines & 4095:
            # #997 D9 (throttled; a no-op without a progress sink).
            _lib_rebuild_progress.tick(lines=lines)
        record = _lib_journal.decode_line(raw)
        record_end = (segment, offset + len(raw) + 1)
        if record is None:
            malformed += 1
            prior_high_water = record_end
            continue
        inert = _is_inert_codex_obs(record)
        if not inert:
            # #997 D15: every record retained in full is compacted before
            # anything holds a reference to it, so the identity checks below
            # (`records[index] is record`) see only the compacted object.
            record = _lib_journal.compact_retained(record)
        pass_boundary.observe(record)
        if record.get("t") == "op":
            if (record.get("payload") or {}).get("kind") in _REVIEWED_WEEKLY_KINDS:
                pending_reviewed.append((
                    len(records), record, prior_high_water,
                    None if prior_high_water is None
                    else hasher.digest_at(prior_high_water),
                ))
            _journal._capture_protocol_prefix_evidence(
                record,
                prior_high_water,
                evidence,
                hasher=hasher,
            )
            if len(evidence) > len(evidence_owners):
                evidence_owners.append(len(records))
        elif inert:
            record = _inert_codex_stub(record, inert_stubs)
        records.append(record)
        record_ends.append(record_end)
        coordinates.append((segment, offset))
        prior_high_water = record_end
    # An inert placeholder keeps every position and sequence number; an
    # excluded correction, reviewed decision or Claude record is never planning
    # input (case F7).
    originals = list(records)
    placeholder = _lib_account_protocol.excluded_placeholder()
    _journal._apply_pass_admission(
        records, coordinates, pass_boundary.resolve(high_water),
        placeholder=placeholder)
    # #825/#832 fix round C (CD2-005): an excluded acknowledgement takes its
    # raw-prefix evidence with it, so selection reads it as absent.
    evidence = _journal._admitted_prefix_evidence(
        evidence, evidence_owners, records)
    # #825/#832 spec 5.6: each excluded slot keeps its original record and
    # coordinate for the outcome-quarantine fold of the scratch replay (a
    # trigger whose readings widen the every-account envelope). Nothing else
    # reads a placeholder's fields.
    for index, slot in enumerate(records):
        if slot is placeholder:
            records[index] = {
                **placeholder,
                "quarantine": {
                    "record": originals[index],
                    "segment": coordinates[index][0],
                    "offset": coordinates[index][1],
                },
            }
    for index, record, prior, digest in pending_reviewed:
        if records[index] is record:
            _validate_retained_reviewed_op(
                record, prior, _PinnedPrefixDigest(prior, digest),
            )
    # Released before the raise below, so a malformed prefix does not pin the
    # accumulator (its hash states, registered boundaries and segment opener;
    # since #997 D12 it streams and retains no segment bytes) on the traceback
    # while the exception unwinds. On the success path the frame
    # dies two statements later, so this mirrors the repair reader, where the
    # release genuinely precedes a loop over every decoded record.
    hasher = None
    if malformed:
        raise _lib_rederive.RederiveConflict(
            f"journal prefix contains {malformed} malformed line(s)"
        )
    return records, high_water, record_ends, tuple(evidence)


def _read_only_journal_high_water() -> "tuple[str, int] | None":
    """Capture an append-only prefix without creating a coordination file."""
    segments = _journal.list_segments()
    if not segments:
        return None
    latest = segments[-1]
    return (latest, os.path.getsize(_cctally_core.JOURNAL_DIR / latest))


def _batch_id_for_plan(plan: _lib_rederive.RederivePlan) -> "str | None":
    if not plan.actions:
        return None
    body = {
        "family": plan.family,
        "actions": [
            action.to_correction_action() for action in plan.actions
        ],
    }
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return f"rederive:{plan.family}:{digest}"


def _empty_decision_batch_id(family: str, decision_id: str) -> str:
    """D4: the id of the empty batch that completes reviewed decision
    ``decision_id`` when its plan has no actions.

    The plain path reads a reviewed decision as finished only when its own
    completed correction batch follows it, so a decision that changes
    nothing (a later split manifest whose observations an earlier decision
    already holds) still gets a batch: begin and commit, ``action_count``
    0. Keyed by the decision, because every empty action set is the same
    and each decision needs its own completion.
    """
    body = {"family": family, "actions": [], "reviewedDecision": decision_id}
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return f"rederive:{family}:{digest}"


def _iso_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


#: How each read-only snapshot's main file was taken, ``"clone"`` or
#: ``"copy"`` (#997 D13). Per process; tests clear and read it.
SNAPSHOT_COUNTS: dict = {}


def _device_of(path) -> int:
    """`st_dev` of ``path`` (a seam: tests report another device)."""
    return os.stat(path).st_dev


def _snapshot_clone_parent(path: Path, tmpdir: str) -> "str | None":
    """Where a clonable snapshot's directory goes, or None off Darwin.

    A clone shares blocks only within one volume, so the directory is under
    `TMPDIR` when `TMPDIR` is on the source's device and otherwise in the
    source's own directory (a data folder on an external volume with an
    internal `TMPDIR`)."""
    if _lib_clone_copy._PLATFORM != "darwin":
        return None
    try:
        same = _device_of(tmpdir) == _device_of(path.parent)
    except OSError:
        return tmpdir
    return tmpdir if same else str(path.parent)


def _take_sqlite_snapshot(path: Path, snapshot: Path, *, clone: bool):
    """Write a consistent snapshot of ``path`` (and its WAL prefix) to
    ``snapshot``; return ``"clone"`` or ``"copy"``, or None when ``clone``
    was asked for and the filesystem refused it."""
    wal = path.with_name(path.name + "-wal")
    snapshot_wal = snapshot.with_name(snapshot.name + "-wal")
    # Copy the append-only WAL prefix before the main file. If a checkpoint
    # races this read, a main-file identity change makes us retry rather
    # than combining generations. SQLite validates the copied WAL frames.
    for attempt in range(3):
        before = path.stat()
        snapshot_wal.unlink(missing_ok=True)
        try:
            wal_size = wal.stat().st_size
            with wal.open("rb") as source, snapshot_wal.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.truncate(wal_size)
        except FileNotFoundError:
            snapshot_wal.unlink(missing_ok=True)
        if clone:
            # A clone needs a new path, so a retry's previous snapshot goes
            # first (#997 D13).
            snapshot.unlink(missing_ok=True)
            if not _lib_clone_copy.try_clone(str(path), str(snapshot)):
                return None
            method = "clone"
        else:
            shutil.copyfile(path, snapshot)
            method = "copy"
        after = path.stat()
        if (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return method
        if attempt == 2:
            raise RederiveBusy(
                "cache.db changed during the read-only preview snapshot; "
                "retry shortly"
            )
    raise AssertionError("unreachable")


@contextlib.contextmanager
def _open_sqlite_snapshot(path: Path, *, prefix: str):
    """Yield a query-only SQLite family copy without touching the source.

    #997 D13: on Darwin the main file is a clone sharing the source's blocks
    (the cache snapshot was a 2.54 GB copy per preview), its directory placed
    by `_snapshot_clone_parent`. A refused clone removes that directory and
    takes the real copy under `TMPDIR`, so a volume that cannot clone never
    receives a copy beside the data. Elsewhere the snapshot is the real copy
    under `TMPDIR`. The WAL prefix is always a byte copy truncated to the
    size read. The directory is removed on exit; a crash leaves it behind.
    """
    tmpdir = tempfile.gettempdir()
    parent = _snapshot_clone_parent(path, tmpdir)
    holder = None
    try:
        method = None
        if parent is not None:
            holder = tempfile.TemporaryDirectory(prefix=prefix, dir=parent)
            snapshot = Path(holder.name) / path.name
            method = _take_sqlite_snapshot(path, snapshot, clone=True)
            if method is None and parent != tmpdir:
                holder.cleanup()
                holder = None
        if method is None:
            if holder is None:
                holder = tempfile.TemporaryDirectory(prefix=prefix, dir=tmpdir)
            snapshot = Path(holder.name) / path.name
            snapshot.unlink(missing_ok=True)
            method = _take_sqlite_snapshot(path, snapshot, clone=False)
        SNAPSHOT_COUNTS[method] = SNAPSHOT_COUNTS.get(method, 0) + 1
        conn = sqlite3.connect(snapshot)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()
    finally:
        if holder is not None:
            holder.cleanup()


@contextlib.contextmanager
def _open_cache_read_view():
    """Yield a stable cache snapshot without touching source WAL sidecars."""
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        raise _lib_rederive.RederiveDataGap(
            f"missing cache.db at {path}"
        )
    with _open_sqlite_snapshot(
        path,
        prefix="cctally-rederive-cache-",
    ) as conn:
        yield conn


def _latest_completed_family_batch(
    records: list[dict],
    record_ends: list[tuple[str, int]],
    completed: frozenset[str],
    family: str,
) -> tuple["str | None", "tuple[str, int] | None"]:
    latest_id = None
    latest_high_water = None
    for record, record_end in zip(records, record_ends):
        if (
            record.get("t") == "correction_batch"
            and record.get("phase") == "commit"
            and record.get("family") == family
            and record.get("id") in completed
        ):
            latest_id = record["id"]
            latest_high_water = record_end
    return latest_id, latest_high_water


def _preview_from_snapshot(
    family: str,
    *,
    journal_high_water: "tuple[str, int] | None" = None,
    reviewed_op: "dict | None" = None,
) -> RederivePreview:
    if family != _lib_rederive.FAMILY:
        raise _lib_rederive.RederiveConflict(
            f"unsupported rederive family: {family}"
        )
    if journal_high_water is None:
        journal_high_water = _read_only_journal_high_water()
    if journal_high_water is None:
        records, high_water, record_ends, protocol_evidence = [], None, [], ()
    else:
        records, high_water, record_ends, protocol_evidence = (
            read_rederive_journal_prefix(journal_high_water)
        )
    planning_records = records
    baseline_records = None
    decision_op = reviewed_op
    decision_from_journal = False
    decision_batch_completed = False
    retained_decision = None
    retained_decision_index = None
    if reviewed_op is not None:
        bound = reviewed_op["payload"]["journal_high_water"]
        expected = (bound["segment"], bound["offset"])
        if high_water != expected:
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision journalHighWater drifted"
            )
        if _journal.journal_prefix_hash(high_water) != (
            reviewed_op["payload"]["journal_prefix_hash"]
        ):
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision journalPrefixHash drifted"
            )
        planning_records = [*records, reviewed_op]
        baseline_records = records
    else:
        decision_indexes = [
            index for index, record in enumerate(records)
            if record.get("t") == "op"
            and (record.get("payload") or {}).get("kind")
            in _REVIEWED_WEEKLY_KINDS
        ]
        if decision_indexes:
            index = decision_indexes[-1]
            retained_decision = records[index]
            retained_decision_index = index
    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=protocol_evidence,
    )
    if retained_decision is not None:
        suffix = records[retained_decision_index + 1:]
        first = suffix[0] if suffix else None
        completed_decision_batch = (
            first is not None
            and first.get("t") == "correction_batch"
            and first.get("phase") == "begin"
            and first.get("family") == family
            and first.get("id") in selection.completed_batches
        )
        correction_only_suffix = all(
            record.get("t") in {"correction", "correction_batch"}
            for record in suffix
        )
        if correction_only_suffix:
            decision_op = retained_decision
            decision_from_journal = not completed_decision_batch
            decision_batch_completed = completed_decision_batch
            baseline_records = [
                *records[:retained_decision_index],
                *records[retained_decision_index + 1:],
            ]
        elif not completed_decision_batch:
            raise _lib_rederive.RederiveConflict(
                "unfinished reviewed weekly decision has new journal records "
                "after it; review a fresh manifest"
            )
    current_events = {
        event_id: selected.record
        for event_id, selected in selection.by_id.items()
        if selected.status == "active" and selected.record is not None
    }
    plan_high_water = high_water
    if decision_op is not None and not decision_batch_completed:
        bound = decision_op["payload"]["journal_high_water"]
        plan_high_water = (bound["segment"], bound["offset"])
    baseline_plan = None
    baseline_plan_guard = None
    with _open_cache_read_view() as cache:
        if decision_op is None:
            plan = plan_claude_usage(
                planning_records,
                cache_conn=cache,
                journal_high_water=high_water,
                protocol_prefix_evidence=protocol_evidence,
            )
        else:
            baseline_plan = plan_claude_usage(
                baseline_records,
                cache_conn=cache,
                journal_high_water=plan_high_water,
                protocol_prefix_evidence=protocol_evidence,
                enforce_guard=False,
            )
            baseline_plan_guard = _lib_rederive.week_reset_add_burst_guard(
                baseline_plan)
            reviewed_plan = plan_claude_usage(
                planning_records,
                cache_conn=cache,
                journal_high_water=plan_high_water,
                protocol_prefix_evidence=protocol_evidence,
                enforce_guard=False,
            )
            _held_ids, accepted_ids, _identity_pairs, _reviewed_ops = (
                _reviewed_weekly_state(planning_records)
            )
            plan = _lib_rederive.causal_delta_plan(
                baseline_plan, reviewed_plan,
                current_events=current_events,
                authorized_week_reset_origins=accepted_ids,
            )
            _lib_rederive.enforce_week_reset_add_burst_guard(plan)

    batch_id = _batch_id_for_plan(plan)
    generated_at = _iso_now()
    incomplete = False

    def _begins_of(identity):
        return [
            record for record in records
            if record.get("t") == "correction_batch"
            and record.get("phase") == "begin"
            and record.get("id") == identity
        ]

    empty_decision_batch_id = None
    if (batch_id is None and decision_op is not None
            and not decision_batch_completed):
        # D4: an unfinished reviewed decision whose plan is empty completes
        # with an empty batch, so the plain path reads it as finished. A
        # retry after an interrupted append reuses the begin's `at`, so its
        # lines are byte-identical.
        empty_id = _empty_decision_batch_id(family, decision_op["id"])
        if empty_id not in selection.completed_batches:
            empty_id = _lib_journal.revision_batch_id(
                empty_id, _begins_of(empty_id))
        if empty_id not in selection.completed_batches:
            empty_decision_batch_id = empty_id
            begins = _begins_of(empty_id)
            if begins:
                generated_at = str(begins[0]["at"])
                incomplete = True
    if batch_id is not None:
        # #797/#823/#840 spec 4.6 B1: an incomplete batch an earlier writer
        # began without the revision-2 stamp is retried under a derived id,
        # never restamped.
        if batch_id not in selection.completed_batches:
            batch_id = _lib_journal.revision_batch_id(
                batch_id, _begins_of(batch_id))
        begins = _begins_of(batch_id)
        if begins:
            generated_at = str(begins[0]["at"])
            incomplete = batch_id not in selection.completed_batches
    latest_id, latest_high_water = _latest_completed_family_batch(
        records,
        record_ends,
        selection.completed_batches,
        family,
    )
    recovery_required = (
        latest_id is not None and not _stats_has_batch(latest_id)
    )
    owned_conflicts = owned_conflicted_event_ids(selection)
    journal_conflicts = tuple(
        conflict for conflict in selection.conflicts
        if conflict.event_id in owned_conflicts
    )
    preview = RederivePreview(
        journal_conflicts=journal_conflicts,
        plan=plan,
        records=tuple(records),
        journal_high_water=high_water,
        record_ends=tuple(record_ends),
        generated_at=generated_at,
        batch_id=batch_id,
        incomplete_batch=incomplete,
        latest_completed_batch=latest_id,
        latest_completed_high_water=latest_high_water,
        recovery_required=recovery_required,
        reviewed_decision_id=(
            None if decision_op is None else decision_op["id"]),
        baseline_plan=baseline_plan,
        baseline_plan_guard=baseline_plan_guard,
        empty_decision_batch_id=empty_decision_batch_id,
    )
    if decision_op is not None and not decision_batch_completed:
        expected_hashes = _reviewed_op_expected_hashes(
            decision_op, required=decision_from_journal,
        )
        if expected_hashes is not None:
            _assert_reviewed_plan_hashes(preview, expected_hashes)
    return preview


@contextlib.contextmanager
def _rederive_locks(*, apply: bool, timeout: float):
    """Acquire the stable-view locks in the repository's total order."""
    from _lib_cache_writer_lock import (
        acquire_ordered_flocks,
        release_cache_writer_flocks,
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    locks = [
        (
            _cctally_core.STATS_LOCK_MAINTENANCE_PATH,
            fcntl.LOCK_EX if apply else fcntl.LOCK_SH,
        ),
        (_cctally_core.CACHE_LOCK_MAINTENANCE_PATH, fcntl.LOCK_SH),
    ]
    if apply:
        locks.append((_cctally_core.JOURNAL_INGEST_LOCK_PATH, fcntl.LOCK_EX))
    locks.append((_cctally_core.CACHE_LOCK_PATH, fcntl.LOCK_EX))
    held = acquire_ordered_flocks(locks, timeout=timeout)
    if held is None:
        raise RederiveBusy(
            "another database sync or maintenance operation holds the "
            "rederive lock set; retry shortly"
        )
    # #386: record the stats maintenance hold so a nested live `open_db()` does
    # not request SHARED on a second fd of this same file and self-deadlock, and
    # (when applying) declare the sanctioned write regime + the ingest hold so a
    # heal reached from in here recognises itself as the serialized writer.
    import _cctally_store

    _cctally_core.note_stats_maintenance_acquired()
    try:
        with _cctally_store.stats_write_scope("rederive", ingest_lock=apply):
            yield
    finally:
        _cctally_core.note_stats_maintenance_released()
        release_cache_writer_flocks(held)
    # #825/#832 spec 4.3 revision 2: a current holder posts a wake once its
    # locks are released (the fast path; the dependency waiter covers every
    # other exit) — after a rederive's live index publication or none.
    _post_statusline_wake("holder:rederive")


def _post_statusline_wake(reason: str) -> None:
    try:
        import _cctally_statusline
        _cctally_statusline.post_wake(reason)
    except Exception:
        pass


# --------------------------------------------------------------------------
# #500 §8/§8.1 — the operator-attribution apply lock set
# --------------------------------------------------------------------------
#
# `_rederive_locks` above yields no handles and releases its whole set together,
# so there is no way to drop only the two cache flocks at the end of the cache
# transaction while retaining stats maintenance and the ingest lock for the
# stats transaction that follows. `account attribute` needs exactly that: its
# cache work must be committed and unlocked before the stats transaction begins,
# which is the repository law that all cache work precedes the stats transaction
# (`docs/journal-gotchas.md`), while the ingest lock has to be held ACROSS both
# so nothing can consume the appended prefix in between.
#
# ORDERED PARTIAL RELEASE is the whole mechanism: the owner below releases a
# SUFFIX of the acquired set in reverse acquisition order and nothing else. A
# release that skipped an inner lock, or that took them in acquisition order,
# would be the lock-order violation this exists to avoid rather than a
# convenience on top of it.


class _OrderedApplyLockOwner:
    """A held ordered flock set that can release a suffix of itself."""

    __slots__ = ("_paths", "_held", "_cache_flock_count", "_cache_flocks_noted")

    def __init__(self, paths, held, *, cache_flock_count: int) -> None:
        self._paths = tuple(paths)
        self._held = list(held)
        self._cache_flock_count = int(cache_flock_count)
        self._cache_flocks_noted = True
        _cctally_core.note_attribution_apply_cache_flocks_acquired()

    @property
    def held_paths(self) -> tuple:
        return tuple(self._paths[:len(self._held)])

    def _note_cache_flocks_released(self) -> None:
        if self._cache_flocks_noted:
            self._cache_flocks_noted = False
            _cctally_core.note_attribution_apply_cache_flocks_released()

    def release_cache_flocks(self) -> None:
        """Drop the global cache writer flock and the Codex provider flock.

        Idempotent, because the apply sequence releases them at the end of its
        cache transaction and the context manager's ``finally`` releases
        whatever is left — the second call must not close a descriptor twice.

        The release is RECORDED as well as performed, and that is what turns the
        repository lock-order law into something the code enforces:
        `_run_stats_ingest_once(locks_held=True)` refuses while this context
        still holds them, so a caller that forgets this call gets a loud refusal
        instead of a stats transaction opened underneath live cache flocks.
        """
        from _lib_cache_writer_lock import release_cache_writer_flocks

        if len(self._held) <= len(self._paths) - self._cache_flock_count:
            self._note_cache_flocks_released()
            return
        keep = len(self._paths) - self._cache_flock_count
        # `release_cache_writer_flocks` releases the list it is given in REVERSE
        # order, which is what makes this a suffix release rather than an
        # arbitrary one.
        release_cache_writer_flocks(self._held[keep:])
        del self._held[keep:]
        self._note_cache_flocks_released()

    def release_all(self) -> None:
        from _lib_cache_writer_lock import release_cache_writer_flocks

        release_cache_writer_flocks(self._held)
        self._held.clear()
        self._note_cache_flocks_released()


@contextlib.contextmanager
def codex_attribution_apply_locks(*, timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS):
    """Acquire the #500 §8 apply order and yield an ordered-partial-release owner.

    The order is ``_rederive_locks``' applying order extended by the Codex
    provider flock, exactly as the spec states:

    1. stats maintenance, exclusive
    2. cache maintenance, shared
    3. ``journal.ingest.lock``, exclusive
    4. global ``cache.db.lock``, exclusive
    5. the Codex provider flock, exclusive

    ``owner.release_cache_flocks()`` drops 5 then 4 and retains 1-3, so the
    stats transaction runs with the ingest lock still held while every cache
    writer is free again.

    Do not ``os.fork()`` and do not start a thread inside this block. The
    lock-order guard behind ``release_cache_flocks()`` is a ContextVar counter,
    so a child context starts from a COPY of this one and never observes the
    later release — a forked or spawned worker would either refuse a stats
    ingest that is legitimately unblocked, or, if it were created before the
    acquisition, run one while the cache flocks are still held. The flocks
    themselves are process-wide and unaffected; the guard is what is advisory
    across a context boundary.
    """
    from _lib_cache_writer_lock import acquire_ordered_flocks

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    locks = [
        (_cctally_core.STATS_LOCK_MAINTENANCE_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_MAINTENANCE_PATH, fcntl.LOCK_SH),
        (_cctally_core.JOURNAL_INGEST_LOCK_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_CODEX_PATH, fcntl.LOCK_EX),
    ]
    held = acquire_ordered_flocks(locks, timeout=timeout)
    if held is None:
        raise RederiveBusy(
            "another database sync or maintenance operation holds the "
            "attribution apply lock set; retry shortly"
        )
    owner = _OrderedApplyLockOwner(
        [path for path, _mode in locks], held, cache_flock_count=2)
    # #386, the same declaration `_rederive_locks` makes: record the stats
    # maintenance hold so a nested live `open_db()` does not request SHARED on a
    # second fd of this same file and self-deadlock, and declare the sanctioned
    # write regime plus the ingest hold so a heal reached from in here
    # recognises itself as the serialized writer.
    import _cctally_store

    _cctally_core.note_stats_maintenance_acquired()
    try:
        with _cctally_store.stats_write_scope(
            "codex-window-attribution", ingest_lock=True,
        ):
            yield owner
    finally:
        _cctally_core.note_stats_maintenance_released()
        owner.release_all()
    # #825/#832 spec 4.3 revision 2: the outer lock owner of a
    # `run_stats_ingest(locks_held=True)` cycle posts its wake once the whole
    # set is released.
    _post_statusline_wake("holder:attribution")


def preview_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
    reviewed_op: "dict | None" = None,
) -> RederivePreview:
    # Preview has a literal zero-persistent-write contract. A fixed append-only
    # journal prefix and a read-only SQLite transaction are stable inputs
    # without creating any coordination files.
    del lock_timeout
    return _preview_from_snapshot(family, reviewed_op=reviewed_op)


def _stats_has_batch(batch_id: str) -> bool:
    path = _cctally_core.DB_PATH
    if not path.exists():
        return False
    try:
        with _open_sqlite_snapshot(
            path,
            prefix="cctally-rederive-stats-",
        ) as conn, _cctally_store.current_stats_snapshot(conn):
            # #825/#832 spec 6: convergence evidence only from a validated
            # current index; anything else fails closed (rebuild).
            row = conn.execute(
                "SELECT 1 FROM journal_effective_events "
                "WHERE batch_id = ? LIMIT 1",
                (batch_id,),
            ).fetchone()
            if row is None:
                # D4: an empty batch (a reviewed decision whose plan had no
                # actions) leaves no effective event; the index has folded
                # it when its selector row is completed.
                row = conn.execute(
                    "SELECT 1 FROM journal_selector_batches "
                    "WHERE batch_id = ? AND status = 'completed' "
                    "AND action_count = 0",
                    (batch_id,),
                ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error, StatsIndexNotCurrent):
        return False


def _call_crash_hook(stage: str) -> None:
    if _REDERIVE_CRASH_HOOK is not None:
        _REDERIVE_CRASH_HOOK(stage)
    if (
        os.environ.get("CCTALLY_REDERIVE_TEST_MODE") == "1"
        and os.environ.get("CCTALLY_REDERIVE_TEST_CRASH_STAGE") == stage
    ):
        os.kill(os.getpid(), signal.SIGKILL)


def _reviewed_op_is_exact_retry(preview: RederivePreview, op: dict) -> bool:
    bound = op["payload"]["journal_high_water"]
    expected = (bound["segment"], bound["offset"])
    if _journal.journal_prefix_hash(expected) != (
        op["payload"]["journal_prefix_hash"]
    ):
        return False
    matches = [
        index for index, record in enumerate(preview.records)
        if record.get("t") == "op" and record.get("id") == op["id"]
    ]
    if len(matches) != 1:
        return False
    index = matches[0]
    return preview.records[index] == op and (
        preview.record_ends[index - 1] if index else None
    ) == expected


def _reviewed_op_needs_materialization(preview: RederivePreview) -> bool:
    last_index = None
    for index, record in enumerate(preview.records):
        if (record.get("t") == "op" and
                (record.get("payload") or {}).get("kind")
                in _REVIEWED_WEEKLY_KINDS):
            last_index = index
    if last_index is None:
        return False
    try:
        with _open_sqlite_snapshot(
            _cctally_core.DB_PATH, prefix="cctally-rederive-stats-",
        ) as conn:
            row = conn.execute(
                "SELECT segment, offset FROM journal_cursor WHERE id=1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        row = None
    if row is None:
        return True
    cursor = (str(row[0]), int(row[1]))
    if cursor == preview.journal_high_water:
        return False
    try:
        return preview.record_ends.index(cursor) < last_index
    except ValueError:
        return True


def _refuse_earlier_baseline_actions(
    family: str, preview: RederivePreview, reviewed_op: dict,
) -> None:
    """Keep pre-existing corrections outside the exact decision's history.

    A reviewed weekly decision can remove a reset-burst guard without causing
    older rederive drift. Such drift must be resolved separately before the
    reviewed operation is appended; it is not part of the operator's selected
    observations. The baseline plan may itself trip the burst guard, but that
    exception still carries its complete proposed action set.
    """
    selected_ids = {
        item["observationId"]
        for item in reviewed_op["payload"]["decisions"]
    }
    selected_at = [
        dt.datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
        for record in preview.records
        if record.get("t") == "obs" and record.get("id") in selected_ids
    ]
    if len(selected_at) != len(selected_ids):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision has unknown selected observations"
        )
    first_selected_at = min(selected_at)
    try:
        baseline = _preview_from_snapshot(family)
        baseline_plan = baseline.plan
    except _lib_rederive.RederivePlanGuardConflict as exc:
        baseline_plan = exc.plan
    baseline_actions = {
        action.event_id: action.payload_hash
        for action in baseline_plan.actions
    }
    unchanged_earlier = [
        action for action in preview.plan.actions
        if action.disposition != "add"
        and baseline_actions.get(action.event_id) == action.payload_hash
        and dt.datetime.fromisoformat(action.at.replace("Z", "+00:00"))
        < first_selected_at
    ]
    if unchanged_earlier:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly correction contains pre-existing actions "
            "earlier than its selected observations; resolve baseline "
            "rederive drift first"
        )


def _assert_reviewed_plan_hashes(preview, expected_plan_hashes) -> None:
    if expected_plan_hashes is None:
        return
    if preview.baseline_plan is None:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision has no baseline plan to verify"
        )
    expected_baseline, expected_decision = expected_plan_hashes
    if preview.baseline_plan.plan_hash != expected_baseline:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly baseline plan hash drifted: expected "
            f"{expected_baseline}, got {preview.baseline_plan.plan_hash}"
        )
    if preview.plan.plan_hash != expected_decision:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision plan hash drifted: expected "
            f"{expected_decision}, got {preview.plan.plan_hash}"
        )


def _compact_preview(preview: RederivePreview) -> RederivePreview:
    """#997 D18: the preview without its journal records.

    `records` (every retained record of the prefix) and `record_ends` (one
    commit coordinate per record) are read only by planning: the exact-retry
    and materialization checks run before any rebuild starts. Every other
    field (both plans and their hashes, `batch_id`, `incomplete_batch`, the
    high-water marks) is kept, so the status choice, the result, the
    renderers and every error read what they read before. The one release
    seam: T18 disables the release by patching it to the identity.
    """
    return dataclasses.replace(preview, records=(), record_ends=())


def _release_preview_records(*previews):
    """#997 D18: compact copies of `previews`, in order, for the apply to
    rebind immediately before a rebuild, so no full preview stays reachable
    through any name. A preview passed under two names comes back as ONE
    copy, so an alias stays an alias; `None` (no such preview) stays
    `None`."""
    copies: dict = {}
    released = []
    for preview in previews:
        if preview is not None and id(preview) not in copies:
            copies[id(preview)] = _compact_preview(preview)
        released.append(None if preview is None else copies[id(preview)])
    return tuple(released)


def apply_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
    reviewed_op: "dict | None" = None,
    expected_plan_hashes: "tuple[str, str] | None" = None,
) -> RederiveCommandResult:
    import _cctally_completion

    lost: list = []
    with _rederive_locks(apply=True, timeout=lock_timeout), \
            _held_past_unlock(lost):
        appended_reviewed_op = False
        # A reviewed exact retry's preview at the decision's bound; D18
        # releases it with the others.
        original = None
        if reviewed_op is not None:
            durable_hashes = _reviewed_op_expected_hashes(
                reviewed_op, required=True,
            )
            if expected_plan_hashes != durable_hashes:
                raise _lib_rederive.RederiveConflict(
                    "reviewed weekly apply hashes do not match its durable op"
                )
        if reviewed_op is None:
            preview = _preview_from_snapshot(family)
            report_preview = preview
        else:
            try:
                report_preview = _preview_from_snapshot(
                    family, reviewed_op=reviewed_op)
            except _lib_rederive.RederiveConflict as exc:
                if "journalHighWater drifted" not in str(exc):
                    raise
                preview = _preview_from_snapshot(family)
                if not _reviewed_op_is_exact_retry(preview, reviewed_op):
                    raise exc
                bound = reviewed_op["payload"]["journal_high_water"]
                original = _preview_from_snapshot(
                    family,
                    journal_high_water=(bound["segment"], bound["offset"]),
                    reviewed_op=reviewed_op,
                )
                op_index = next(
                    index for index, record in enumerate(preview.records)
                    if record.get("t") == "op"
                    and record.get("id") == reviewed_op["id"]
                )
                # D4: an empty decision's own batch is its empty one.
                original_batch_id = (
                    original.batch_id or original.empty_decision_batch_id)
                if any(
                    not (
                        (record.get("t") == "correction_batch"
                         and record.get("id") == original_batch_id)
                        or (record.get("t") == "correction"
                            and record.get("batch") == original_batch_id)
                    )
                    for record in preview.records[op_index + 1:]
                ):
                    raise _lib_rederive.RederiveConflict(
                        "reviewed weekly decision retry has new journal records "
                        "after its decision; review a fresh manifest"
                    )
                original_actions = original.plan.to_correction_actions()
                current_actions = preview.plan.to_correction_actions()
                if current_actions != original_actions and not (
                    not current_actions
                    and original.batch_id is not None
                    and preview.latest_completed_batch == original.batch_id
                ):
                    raise _lib_rederive.RederiveConflict(
                        "reviewed weekly decision retry changed correction "
                        "actions"
                    )
                report_preview = original
                _assert_reviewed_plan_hashes(
                    report_preview, expected_plan_hashes)
            else:
                _assert_reviewed_plan_hashes(
                    report_preview, expected_plan_hashes)
                try:
                    op_high_water = _journal.append_records(
                        [reviewed_op],
                        expected_high_water=report_preview.journal_high_water,
                    )
                except Exception as exc:
                    raise RederiveApplyError(
                        "reviewed decision append", report_preview,
                        report_preview.batch_id, exc,
                    ) from exc
                appended_reviewed_op = True
                _call_crash_hook("after-reviewed-decision-op")
                preview = _preview_from_snapshot(
                    family, journal_high_water=op_high_water)
                if (preview.plan.to_correction_actions()
                        != report_preview.plan.to_correction_actions()):
                    raise RederiveApplyError(
                        "reviewed decision revalidation", report_preview,
                        report_preview.batch_id,
                        "scratch actions changed after decision append",
                    )
        plan = preview.plan
        recovering_prior = (
            preview.latest_completed_batch is not None
            and not _stats_has_batch(preview.latest_completed_batch)
        )
        # D4: an unfinished reviewed decision whose plan is empty appends its
        # empty completed batch here, exactly like a batch with actions, so
        # the plain path reads the decision as finished. It reports what the
        # decision reported before (the latest earlier batch), so the output
        # is unchanged.
        batch_id = preview.batch_id
        reported_batch_id = preview.batch_id
        if not plan.actions and preview.empty_decision_batch_id is not None:
            batch_id = preview.empty_decision_batch_id
            reported_batch_id = preview.latest_completed_batch
        if batch_id is not None:
            batch = _lib_journal.make_correction_batch(
                batch_id=batch_id,
                family=family,
                at=preview.generated_at,
                actions=plan.to_correction_actions() if plan.actions else [],
            )
            try:
                batch_high_water = _journal.append_records(
                    batch,
                    expected_high_water=preview.journal_high_water,
                    line_hook=lambda index: _call_crash_hook(
                        f"after-batch-line-{index}"
                    ),
                )
            except Exception as exc:
                raise RederiveApplyError(
                    "correction append", preview, batch_id, exc
                ) from exc
            # #997 D18: nothing past planning reads the records.
            preview, report_preview, original = _release_preview_records(
                preview, report_preview, original)
            try:
                _call_crash_hook("after-batch-commit")
                result = _journal.rebuild_stats_index(
                    context=_journal.RebuildContext(trigger="rederive-apply"),
                    high_water=batch_high_water,
                    update_quota_cache=False,
                    complete_derivation=True,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
                _call_crash_hook("after-rebuild")
            except _cctally_completion.ResidualObligationNotDurable as exc:
                raise _obligation_lost(
                    "stats rebuild", preview, batch_id, exc
                ) from exc
            except Exception as exc:
                raise RederiveApplyError(
                    "stats rebuild", preview, batch_id, exc
                ) from exc
            status = (
                "recovered"
                if preview.incomplete_batch or recovering_prior
                # D4: as before, completing an empty decision this
                # invocation did not append (an interrupted apply resumed by
                # an exact retry or a plain rederive) is a recovery.
                or (not plan.actions and not appended_reviewed_op)
                else "applied"
            )
            return RederiveCommandResult(
                preview=report_preview,
                status=status,
                batch_id=reported_batch_id,
                rebuild=result,
            )

        latest = preview.latest_completed_batch
        if latest is not None and not _stats_has_batch(latest):
            if preview.latest_completed_high_water is None:
                raise RederiveBusy(
                    f"completed correction batch {latest} has no commit high-water"
                )
            # #997 D18: nothing past planning reads the records.
            preview, report_preview, original = _release_preview_records(
                preview, report_preview, original)
            try:
                result = _journal.rebuild_stats_index(
                    context=_journal.RebuildContext(
                        trigger="rederive-recovery"
                    ),
                    high_water=preview.latest_completed_high_water,
                    update_quota_cache=False,
                    complete_derivation=True,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
                _call_crash_hook("after-rebuild")
            except _cctally_completion.ResidualObligationNotDurable as exc:
                raise _obligation_lost(
                    "stats recovery", preview, latest, exc
                ) from exc
            except Exception as exc:
                raise RederiveApplyError(
                    "stats recovery", preview, latest, exc
                ) from exc
            return RederiveCommandResult(
                preview=report_preview,
                status="recovered",
                batch_id=latest,
                rebuild=result,
            )
        if appended_reviewed_op or _reviewed_op_needs_materialization(preview):
            # #997 D18: released only after the check above has read them.
            preview, report_preview, original = _release_preview_records(
                preview, report_preview, original)
            try:
                result = _journal.rebuild_stats_index(
                    context=_journal.RebuildContext(trigger="rederive-recovery"),
                    high_water=preview.journal_high_water,
                    update_quota_cache=False,
                    complete_derivation=True,
                )
            except _cctally_completion.ResidualObligationNotDurable as exc:
                raise _obligation_lost(
                    "reviewed decision materialization", report_preview,
                    latest, exc,
                ) from exc
            except Exception as exc:
                raise RederiveApplyError(
                    "reviewed decision materialization", report_preview,
                    latest, exc,
                ) from exc
            return RederiveCommandResult(
                preview=report_preview,
                status="applied" if appended_reviewed_op else "recovered",
                batch_id=latest,
                rebuild=result,
            )
        return RederiveCommandResult(
            preview=report_preview,
            status="no-op",
            batch_id=latest,
            rebuild=None,
        )
    raise lost[0]


@contextlib.contextmanager
def _held_past_unlock(lost: list):
    """Fix round K12 (K11R-2): hold a lost-obligation outcome until the
    enclosing lock context has exited normally (locks released, holder wake
    posted); the caller re-raises it from ``lost`` afterwards."""
    try:
        yield
    except RederiveObligationNotDurable as exc:
        lost.append(exc)


def _obligation_lost(stage, preview, batch_id, exc):
    """The rebuild published; only its residual obligation was lost."""
    return RederiveObligationNotDurable(stage, preview, batch_id, exc)


def _high_water_dict(high_water):
    if high_water is None:
        return None
    return {"segment": high_water[0], "offset": high_water[1]}


def _rebuild_dict(result):
    if result is None:
        return None
    return {
        "segmentsRead": result.segments_read,
        "linesFolded": result.lines_folded,
        "malformed": result.malformed,
        "durationSeconds": round(result.duration_s, 3),
        "rowsByTable": result.rows_by_table,
    }


def _command_payload(
    *,
    status: str,
    preview: "RederivePreview | None" = None,
    batch_id: "str | None" = None,
    rebuild=None,
    conflicts=(),
    data_gaps=(),
    errors=(),
    family: str = _lib_rederive.FAMILY,
    journal_high_water=None,
    guarded_plan: "_lib_rederive.RederivePlan | None" = None,
    plan_guard=None,
    reviewed_decision_id=None,
    reviewed_decision_counts=None,
):
    plan = guarded_plan if preview is None else preview.plan
    counts = (
        {"retain": 0, "supersede": 0, "tombstone": 0, "add": 0}
        if plan is None else dict(plan.counts)
    )
    body = {
        "status": status,
        "family": family,
        "journalHighWater": (
            _high_water_dict(journal_high_water)
            if preview is None
            else _high_water_dict(preview.journal_high_water)
        ),
        "batchId": batch_id,
        "planHash": None if plan is None else plan.plan_hash,
        "actionCounts": counts,
        "actionCountsByEventKind": (
            {
                kind: {name: 0 for name in counts}
                for kind in sorted(_lib_rederive._EVT_CLASSIFICATIONS)
            }
            if plan is None else plan.action_counts_by_event_kind
        ),
        "planGuard": plan_guard,
        # #426: how many owned events the plan held OUT of the diff because no
        # retained source can re-derive them (pre-cutover exported history).
        "preservedEventCount": 0 if plan is None else plan.preserved_event_count,
        # `conflicts` is the LEGACY key and keeps its meaning: command-validation
        # failure messages (unsupported family, prod guard, structural journal
        # protocol errors). #374's quarantined same-revision GROUPS ride the new
        # `journalConflicts` key — never overload the old one.
        "conflicts": list(conflicts),
        "journalConflicts": [
            conflict.to_dict()
            for conflict in (() if preview is None else preview.journal_conflicts)
        ],
        "dataGaps": list(data_gaps),
        "errors": list(errors),
        "rebuild": _rebuild_dict(rebuild),
        "noOp": status == "no-op",
    }
    if reviewed_decision_id is not None:
        body["reviewedWeeklyDecisionId"] = reviewed_decision_id
    if reviewed_decision_counts is not None:
        # #797/#823/#840 spec 4.7: the manifest's decisions by disposition,
        # retractions counted on their own (they persist as holds).
        body["reviewedWeeklyDecisionCounts"] = reviewed_decision_counts
    if plan is not None and plan.uncertain_cost_fact_count:
        body["uncertainCostFactCount"] = plan.uncertain_cost_fact_count
    if preview is not None and preview.baseline_plan is not None:
        body["baselinePlanHash"] = preview.baseline_plan.plan_hash
        body["baselineActionCounts"] = dict(preview.baseline_plan.counts)
        body["baselinePlanGuard"] = preview.baseline_plan_guard
        body["decisionPlanHash"] = preview.plan.plan_hash
        body["decisionActionCounts"] = dict(preview.plan.counts)
    return _lib_json_envelope.stamp_schema_version(body, version=1)


def _emit_command_payload(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, separators=(",", ":")))
        return
    status = payload["status"]
    if status == "preview":
        actions = sum(
            payload["actionCounts"][name]
            for name in ("supersede", "tombstone", "add")
        )
        if actions == 0 and payload["batchId"] is not None:
            print(
                f"cctally: rederive preview for {payload['family']} — "
                f"completed batch {payload['batchId']} needs stats.db recovery; "
                "no changes written."
            )
        else:
            print(
                f"cctally: rederive preview for {payload['family']} — "
                f"{actions} correction action(s); no changes written."
            )
    elif status == "applied":
        if payload["batchId"] is None:
            print(
                f"cctally: applied {payload['family']} reviewed decision "
                "and rebuilt stats.db."
            )
        else:
            print(
                f"cctally: applied {payload['family']} correction batch "
                f"{payload['batchId']} and rebuilt stats.db."
            )
    elif status == "recovered":
        print(
            f"cctally: recovered {payload['family']} correction batch "
            f"{payload['batchId']} and rebuilt stats.db."
        )
    elif status == "no-op":
        print(
            f"cctally: {payload['family']} is already current; "
            "no correction batch was appended."
        )
    if "baselinePlanHash" in payload:
        def _counts_text(counts):
            return ", ".join(
                f"{name}={counts[name]}"
                for name in ("retain", "supersede", "tombstone", "add")
            )
        print(f"baseline plan {payload['baselinePlanHash']}")
        print(
            "baseline action counts: "
            + _counts_text(payload["baselineActionCounts"])
        )
        if payload.get("baselinePlanGuard") is not None:
            print(
                "baseline plan guard: "
                + json.dumps(
                    payload["baselinePlanGuard"],
                    sort_keys=True, separators=(",", ":"),
                )
            )
        print(f"decision plan {payload['decisionPlanHash']}")
        print(
            "decision action counts: "
            + _counts_text(payload["decisionActionCounts"])
        )
    if "reviewedWeeklyDecisionCounts" in payload:
        counts = payload["reviewedWeeklyDecisionCounts"]
        print("reviewed decisions: " + ", ".join(
            f"{name}={counts[name]}" for name in ("hold", "accept", "retract")))


def _reviewed_decision_counts(reviewed_op) -> "dict | None":
    """A reviewed manifest's decisions per disposition (`hold`, `accept`,
    `retract`), or None without a manifest."""
    if reviewed_op is None:
        return None
    decoded = _lib_rederive.decode_reviewed_weekly_payload(
        reviewed_op["payload"])
    counts = dict.fromkeys(_lib_rederive.REVIEWED_WEEKLY_DISPOSITIONS, 0)
    for disposition in decoded.dispositions.values():
        counts[disposition] += 1
    return counts


def cmd_db_rederive(args) -> int:
    """Preview or apply one audited Claude-usage correction plan."""
    family = str(getattr(args, "family", ""))
    as_json = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "yes", False))
    try:
        journal_high_water = _read_only_journal_high_water()
    except OSError as exc:
        payload = _command_payload(
            status="failed",
            family=family,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    if family != _lib_rederive.FAMILY:
        message = f"unsupported rederive family: {family}"
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=journal_high_water,
            conflicts=(message,),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive: {message}", file=sys.stderr)
        return 2
    if apply:
        import _cctally_db

        if _cctally_db._would_block_prod_stats(_cctally_core.DB_PATH):
            message = (
                "refusing to rederive the prod stats.db "
                "(~/.local/share/cctally) from a dev checkout; run the "
                "installed binary or set CCTALLY_ALLOW_PROD_MIGRATION=1"
            )
            payload = _command_payload(
                status="conflict",
                family=family,
                journal_high_water=journal_high_water,
                conflicts=(message,),
            )
            if as_json:
                _emit_command_payload(payload, as_json=True)
            else:
                print(f"cctally: db rederive: {message}", file=sys.stderr)
            return 2
    if apply and _journal.account_bound_cutover_enforced():
        # #825/#832 spec 5.5: a prefix-bound administrative operation
        # establishes the account-bound cutover before mutable planning, so
        # the plan's prefix already contains the boundary. A reviewed manifest
        # cut from a prefix without it then fails its existing drift check.
        try:
            _journal.ensure_account_bound_cutover()
        except _journal.AccountBoundCutoverDeferred:
            pass
    try:
        reviewed_path = getattr(args, "reviewed_weekly_decisions", None)
        reviewed_op = (
            None if reviewed_path is None
            else _reviewed_weekly_op_from_manifest(reviewed_path)
        )
        expected_plan_hashes = (
            None if reviewed_path is None
            else _reviewed_weekly_expected_hashes(
                reviewed_path, required=apply,
            )
        )
        if apply:
            result = apply_db_rederive(
                family,
                reviewed_op=reviewed_op,
                expected_plan_hashes=expected_plan_hashes,
            )
            payload = _command_payload(
                status=result.status,
                preview=result.preview,
                batch_id=result.batch_id,
                rebuild=result.rebuild,
                reviewed_decision_id=(
                    None if reviewed_op is None else reviewed_op["id"]),
                reviewed_decision_counts=_reviewed_decision_counts(reviewed_op),
            )
        else:
            preview = preview_db_rederive(family, reviewed_op=reviewed_op)
            status = (
                "preview"
                if reviewed_op is not None or preview.plan.actions
                or preview.recovery_required
                # D4: the apply completes an unfinished empty decision.
                or preview.empty_decision_batch_id is not None
                else "no-op"
            )
            payload = _command_payload(
                status=status,
                preview=preview,
                batch_id=(
                    preview.latest_completed_batch
                    if status == "no-op" or preview.recovery_required
                    else preview.batch_id
                ),
                reviewed_decision_id=preview.reviewed_decision_id,
                reviewed_decision_counts=_reviewed_decision_counts(reviewed_op),
            )
    except _lib_rederive.RederiveDataGap as exc:
        payload = _command_payload(
            status="missing-source",
            family=family,
            journal_high_water=journal_high_water,
            data_gaps=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive missing source: {exc}", file=sys.stderr)
        return 2
    except _lib_rederive.RederivePlanGuardConflict as exc:
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=exc.plan.journal_high_water,
            batch_id=_batch_id_for_plan(exc.plan),
            guarded_plan=exc.plan,
            plan_guard=exc.plan_guard,
            conflicts=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive conflict: {exc}", file=sys.stderr)
        return 2
    except (_lib_rederive.RederiveConflict,
            _lib_journal.JournalProtocolError) as exc:
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=journal_high_water,
            conflicts=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive conflict: {exc}", file=sys.stderr)
        return 2
    except RederiveObligationNotDurable as exc:
        # Fix round K12 (K11R-2): the corrected index IS published (and its
        # holder wake posted); only the residual obligation was lost.
        payload = _command_payload(
            status="failed",
            preview=exc.preview,
            batch_id=exc.batch_id,
            errors=(str(exc),),
        )
        payload["indexPublished"] = True
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(
                "cctally: the corrected stats.db was published, but the "
                "record that the inputs written during the rebuild still need "
                f"deriving could not be saved ({exc.cause}). Those inputs stay "
                "in the journal and the next ingest derives them; check that "
                "the data directory is writable.",
                file=sys.stderr,
            )
        return 3
    except RederiveApplyError as exc:
        payload = _command_payload(
            status="failed",
            preview=exc.preview,
            batch_id=exc.batch_id,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    except (RederiveBusy, _journal.JournalError, sqlite3.Error, OSError) as exc:
        payload = _command_payload(
            status="failed",
            family=family,
            journal_high_water=journal_high_water,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    _emit_command_payload(payload, as_json=as_json)
    return 0
