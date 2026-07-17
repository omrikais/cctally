"""Codex adapter for durable provider-neutral quota interpretation.

``quota_window_snapshots`` remains cache.db's physical, re-derivable evidence.
This module reads that committed cache after the S1 ingest lock releases and
reconciles an interpreted index in stats.db.  The two databases are not and do
not pretend to be one atomic transaction: a retry always derives a complete new
stats generation from the physical cache.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

import _cctally_core
from _cctally_core import _command_as_of, eprint
from _lib_quota import (
    QuotaBlock,
    QuotaForecast,
    QuotaFreshness,
    QuotaHistory,
    QuotaObservation,
    QuotaPercentMilestone,
    QuotaRule,
    QuotaWindowIdentity,
    build_blocks,
    build_history,
    forecast_quota,
    quota_freshness,
    quota_rule_fingerprint,
    quota_threshold_decisions,
    percent_milestones,
    resolve_quota_rule,
    select_baseline,
    source_path_key,
)
from _lib_json_envelope import stamp_schema_version


UTC = dt.timezone.utc
_DASHBOARD_PROJECTION_CERTIFICATE_KEY = "codex_quota_projection_certificate"


@dataclass(frozen=True)
class QuotaProjectionResult:
    """Counts from one completed reconciliation transaction."""

    generation: str | None
    blocks_upserted: int
    milestones_upserted: int
    blocks_orphaned: int
    milestones_orphaned: int
    roots_stamped: int
    alerts_dispatched: int


@dataclass(frozen=True)
class CodexQuotaBreakdownRow:
    """One milestone correlated with root-qualified Codex accounting."""

    percent: int
    captured_at: dt.datetime
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float
    marginal_cost_usd: float


def _parse_utc(value: str, label: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _physical_tuple(observation: QuotaObservation) -> tuple[dt.datetime, str, int]:
    return (observation.captured_at, observation.source_path, observation.line_offset)


def _cache_connection() -> sqlite3.Connection:
    """Open a read-only cache connection without invoking or re-running ingest."""
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cache_root_keys(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0]) for row in conn.execute(
                "SELECT source_root_key FROM codex_source_roots"
            )
        }
    except sqlite3.OperationalError:
        return set()


def codex_physical_mutation_seq(conn: sqlite3.Connection) -> int:
    """Return the cache-local Codex physical sequence without scanning history."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
        return 0 if row is None else int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def load_codex_quota_projection_certificate(
    conn: sqlite3.Connection,
) -> tuple[int, dict[str, str]] | None:
    """Read the post-reconciliation physical-signature certificate in O(1)."""
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            (_DASHBOARD_PROJECTION_CERTIFICATE_KEY,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        sequence = int(payload["sequence"])
        signatures = {
            str(root_key): str(signature)
            for root_key, signature in dict(payload["signatures"]).items()
        }
    except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if sequence < 0 or any(len(signature) != 64 for signature in signatures.values()):
        return None
    return sequence, signatures


def _store_codex_quota_projection_certificate(
    *,
    sequence: int,
    signatures: Mapping[str, str],
) -> None:
    """Stamp exact validated signatures only if cache physical state is unchanged.

    The certificate is written after the independent stats transaction commits.
    A later cache mutation necessarily advances ``sequence``, so a dashboard
    reader fails coherence rather than combining new physical cache data with
    the prior projection certificate.
    """
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if codex_physical_mutation_seq(conn) != sequence:
                conn.rollback()
                return
            payload = json.dumps({
                "sequence": sequence,
                "signatures": dict(sorted(signatures.items())),
            }, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_DASHBOARD_PROJECTION_CERTIFICATE_KEY, payload),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _stats_projection_signatures_match(
    stats_conn: sqlite3.Connection,
    active_roots: set[str],
    cert_sigs: Mapping[str, str],
) -> bool:
    """True iff stats.db's projection signature matches the certificate for every root.

    The cache certificate alone does not prove stats.db still holds the
    projection: stats.db can be independently wiped/recovered while cache.db
    persists (F1).  Require an exact ``quota_projection_state.physical_signature``
    match for every active root before the reconcile is allowed to short-circuit.
    A missing row, a mismatch, or any ``sqlite3.Error`` degrades to False, which
    forces the full reconcile (fail-safe).
    """
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, physical_signature FROM quota_projection_state"
        ).fetchall()
    except sqlite3.Error:
        return False
    projection = {str(row[0]): str(row[1]) for row in rows}
    return all(
        projection.get(root) == cert_sigs.get(root) for root in active_roots
    )


def load_codex_quota_observations(
    *,
    source_root_keys: Iterable[str] | None = None,
    cache_conn: sqlite3.Connection | None = None,
    captured_at_or_after: dt.datetime | None = None,
    active_at: dt.datetime | None = None,
    max_rows: int | None = None,
    physical_signatures: dict[str, str] | None = None,
) -> tuple[QuotaObservation, ...]:
    """Load only valid root-qualified S1 physical quota rows.

    Invalid/legacy partial rows remain cache evidence but are not safe enough to
    become a quota identity, so they are skipped window-by-window.  This is a
    projection reader only; it never parses rollout JSONL or mutates cache.db.

    ``cache_conn`` is caller-owned and lets a coordinated dashboard rebuild
    read quota evidence from its exact accounting generation.  Omitting it
    preserves the established independent read-only connection behavior.

    The optional time/cardinality bounds are dashboard read-model controls;
    their defaults preserve the CLI's complete-history behavior.  ``active_at``
    retains reset windows that are still active even when their last capture is
    older than ``captured_at_or_after``.  When ``physical_signatures`` is
    supplied, exact S2 signatures are accumulated from the same cursor before
    presentation bounds are applied, so coherence validation does not require
    a second unbounded observation load.
    """
    for name, value in (
        ("captured_at_or_after", captured_at_or_after), ("active_at", active_at),
    ):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            if name == "captured_at_or_after":
                captured_at_or_after = value.astimezone(UTC)
            else:
                active_at = value.astimezone(UTC)
    if max_rows is not None:
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
            raise ValueError("max_rows must be a positive integer or None")
    requested = None if source_root_keys is None else {str(key) for key in source_root_keys}
    owns_conn = cache_conn is None
    if owns_conn:
        try:
            conn = _cache_connection()
        except (FileNotFoundError, sqlite3.Error):
            return ()
    else:
        conn = cache_conn
    previous_row_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT source, source_root_key, source_path, line_offset,
                   captured_at_utc, observed_slot, logical_limit_key, limit_id,
                   limit_name, window_minutes, used_percent, resets_at_utc,
                   plan_type, individual_limit_json, reached_type
              FROM quota_window_snapshots
             WHERE source='codex' AND source_root_key IS NOT NULL
        """
        params: list[object] = []
        if requested is not None:
            if not requested:
                return ()
            sql += " AND source_root_key IN (" + ",".join("?" for _ in requested) + ")"
            params.extend(sorted(requested))
        # When exact signatures are requested this first cursor must cover the
        # complete root history.  Otherwise apply dashboard presentation bounds
        # in SQL so only the capped evidence crosses the SQLite/Python boundary.
        sql_bounded = physical_signatures is None
        if sql_bounded and captured_at_or_after is not None:
            if active_at is not None:
                sql += (
                    " AND (unixepoch(captured_at_utc) >= unixepoch(?) "
                    "OR unixepoch(resets_at_utc) > unixepoch(?))"
                )
                params.extend((_utc_iso(captured_at_or_after), _utc_iso(active_at)))
            else:
                sql += " AND unixepoch(captured_at_utc) >= unixepoch(?)"
                params.append(_utc_iso(captured_at_or_after))
        if sql_bounded and max_rows is not None:
            if active_at is not None:
                sql += (
                    " ORDER BY (unixepoch(resets_at_utc) > unixepoch(?)) DESC, "
                    "unixepoch(captured_at_utc) DESC, unixepoch(resets_at_utc) DESC, "
                    "source_path DESC, line_offset DESC"
                )
                params.append(_utc_iso(active_at))
            else:
                sql += (
                    " ORDER BY unixepoch(captured_at_utc) DESC, "
                    "unixepoch(resets_at_utc) DESC, source_path DESC, line_offset DESC"
                )
            sql += " LIMIT ?"
            params.append(max_rows)
        else:
            sql += " ORDER BY source_root_key, captured_at_utc, resets_at_utc, source_path, line_offset"
        result: list[QuotaObservation] = []
        signature_tuples: dict[str, list[tuple[object, ...]]] = {}
        for row in conn.execute(sql, tuple(params)):
            required_text = (
                "source", "source_root_key", "source_path", "captured_at_utc",
                "observed_slot", "logical_limit_key", "resets_at_utc",
            )
            if any(row[name] is None or not str(row[name]).strip() for name in required_text):
                continue
            try:
                identity = QuotaWindowIdentity(
                    source=str(row["source"]),
                    source_root_key=str(row["source_root_key"]),
                    logical_limit_key=str(row["logical_limit_key"]),
                    observed_slot=str(row["observed_slot"]),
                    window_minutes=int(row["window_minutes"]),
                    limit_id=row["limit_id"],
                    limit_name=row["limit_name"],
                )
                observation = QuotaObservation(
                    identity=identity,
                    captured_at=_parse_utc(str(row["captured_at_utc"]), "captured_at_utc"),
                    used_percent=float(row["used_percent"]),
                    resets_at=_parse_utc(str(row["resets_at_utc"]), "resets_at_utc"),
                    source_path=str(row["source_path"]),
                    line_offset=int(row["line_offset"]),
                    plan_type=row["plan_type"],
                    individual_limit_json=row["individual_limit_json"],
                    reached_type=row["reached_type"],
                )
            except (TypeError, ValueError, OverflowError):
                # Physical retention is intentionally more permissive than the
                # provider-neutral identity contract.  One malformed window
                # must not suppress unrelated valid windows or accounting.
                continue
            if physical_signatures is not None:
                signature_tuples.setdefault(identity.source_root_key, []).append((
                    identity.source_root_key,
                    identity.logical_limit_key,
                    _utc_iso(observation.captured_at),
                    observation.source_path,
                    observation.line_offset,
                    observation.used_percent,
                    _utc_iso(observation.resets_at),
                ))
            if (
                captured_at_or_after is not None
                and observation.captured_at < captured_at_or_after
                and (active_at is None or observation.resets_at <= active_at)
            ):
                continue
            result.append(observation)
        if physical_signatures is not None:
            physical_signatures.clear()
            roots = requested if requested is not None else set(signature_tuples)
            for root_key in roots:
                encoded = json.dumps(
                    sorted(signature_tuples.get(root_key, ())),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                physical_signatures[root_key] = hashlib.sha256(encoded).hexdigest()
            if captured_at_or_after is not None or max_rows is not None:
                return load_codex_quota_observations(
                    source_root_keys=requested,
                    cache_conn=conn,
                    captured_at_or_after=captured_at_or_after,
                    active_at=active_at,
                    max_rows=max_rows,
                )
        if max_rows is not None and len(result) > max_rows:
            result = sorted(
                result,
                key=lambda observation: (
                    1 if active_at is not None and observation.resets_at > active_at else 0,
                    observation.captured_at,
                    observation.resets_at,
                    observation.source_path,
                    observation.line_offset,
                ),
                reverse=True,
            )[:max_rows]
            result.sort(key=lambda observation: (
                observation.identity.source_root_key,
                observation.captured_at,
                observation.resets_at,
                observation.source_path,
                observation.line_offset,
            ))
        return tuple(result)
    finally:
        if owns_conn:
            conn.close()
        else:
            conn.row_factory = previous_row_factory


def _historic_root_keys(conn: sqlite3.Connection) -> set[str]:
    roots: set[str] = set()
    for table in (
        "quota_window_blocks", "quota_percent_milestones",
        "quota_threshold_events", "quota_projection_state",
    ):
        try:
            roots.update(str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT source_root_key FROM {table}"
            ) if row[0] is not None)
        except sqlite3.OperationalError:
            continue
    return roots


def _signature(observations: Iterable[QuotaObservation], source_root_key: str) -> str:
    tuples = [
        (
            observation.identity.source_root_key,
            observation.identity.logical_limit_key,
            _utc_iso(observation.captured_at),
            observation.source_path,
            observation.line_offset,
            observation.used_percent,
            _utc_iso(observation.resets_at),
        )
        for observation in observations
        if observation.identity.source_root_key == source_root_key
    ]
    encoded = json.dumps(
        sorted(tuples), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _block_params(block: QuotaBlock, generation: str) -> tuple[object, ...]:
    latest = block.observations[-1]
    identity = block.identity
    return (
        identity.source, identity.source_root_key, identity.logical_limit_key,
        identity.observed_slot, identity.window_minutes, identity.limit_id,
        identity.limit_name, _utc_iso(block.resets_at), _utc_iso(block.nominal_start_at),
        _utc_iso(block.first_observed_at), _utc_iso(block.last_observed_at),
        block.first_percent, block.current_percent, latest.source_path,
        latest.line_offset, generation,
    )


_BLOCK_UPSERT = """
    INSERT INTO quota_window_blocks
       (source, source_root_key, logical_limit_key, observed_slot,
        window_minutes, limit_id, limit_name, resets_at_utc, nominal_start_at_utc,
        first_observed_at_utc, last_observed_at_utc, first_percent, current_percent,
        last_source_path, last_line_offset, generation, orphaned_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    ON CONFLICT(source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc) DO UPDATE SET
      limit_id=excluded.limit_id, limit_name=excluded.limit_name,
      nominal_start_at_utc=excluded.nominal_start_at_utc,
      first_observed_at_utc=excluded.first_observed_at_utc,
      last_observed_at_utc=excluded.last_observed_at_utc,
      first_percent=excluded.first_percent, current_percent=excluded.current_percent,
      last_source_path=excluded.last_source_path, last_line_offset=excluded.last_line_offset,
      generation=excluded.generation, orphaned_at=NULL
"""


_MILESTONE_UPSERT = """
    INSERT INTO quota_percent_milestones
       (source, source_root_key, logical_limit_key, observed_slot, window_minutes,
        resets_at_utc, percent_threshold, captured_at_utc, source_path,
        line_offset, high_water_percent, generation, orphaned_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)
    ON CONFLICT(source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, percent_threshold) DO UPDATE SET
      captured_at_utc=excluded.captured_at_utc, source_path=excluded.source_path,
      line_offset=excluded.line_offset, high_water_percent=excluded.high_water_percent,
      generation=excluded.generation, orphaned_at=NULL
"""


def _milestone_params(
    block: QuotaBlock, milestone: QuotaPercentMilestone, generation: str,
) -> tuple[object, ...]:
    identity = block.identity
    return (
        identity.source, identity.source_root_key, identity.logical_limit_key,
        identity.observed_slot, identity.window_minutes, _utc_iso(block.resets_at),
        milestone.percent, _utc_iso(milestone.captured_at), milestone.observation.source_path,
        milestone.observation.line_offset, milestone.percent, generation,
    )


def _orphan_unseen(conn: sqlite3.Connection, roots: set[str], generation: str, now_iso: str) -> tuple[int, int]:
    if not roots:
        return (0, 0)
    placeholders = ",".join("?" for _ in roots)
    args = (now_iso, *sorted(roots), generation)
    blocks = conn.execute(
        "UPDATE quota_window_blocks SET orphaned_at=COALESCE(orphaned_at, ?) "
        "WHERE source='codex' AND source_root_key IN (" + placeholders + ") "
        "AND generation<>?", args,
    ).rowcount
    milestones = conn.execute(
        "UPDATE quota_percent_milestones SET orphaned_at=COALESCE(orphaned_at, ?) "
        "WHERE source='codex' AND source_root_key IN (" + placeholders + ") "
        "AND generation<>?", args,
    ).rowcount
    # Threshold events are terminal evidence and are never recreated here.
    # Their orphan marker tracks whether the stable source block is present in
    # this completed generation, so a cache rebuild that restores the exact
    # block clears a transient prune marker without creating a new terminal
    # claim.
    event_sql = f"""UPDATE quota_threshold_events AS events
              SET orphaned_at=CASE WHEN EXISTS (
                  SELECT 1 FROM quota_window_blocks AS blocks
                   WHERE blocks.source=events.source
                     AND blocks.source_root_key=events.source_root_key
                     AND blocks.logical_limit_key=events.logical_limit_key
                     AND blocks.observed_slot=events.observed_slot
                     AND blocks.window_minutes=events.window_minutes
                     AND blocks.resets_at_utc=events.resets_at_utc
                     AND blocks.generation=?
              ) THEN NULL ELSE COALESCE(events.orphaned_at, ?) END
            WHERE events.source='codex'
              AND events.source_root_key IN ({placeholders})
        """
    conn.execute(
        event_sql,
        (generation, now_iso, *sorted(roots)),
    )
    return (int(blocks), int(milestones))


def _quota_alert_config() -> tuple[bool, bool, tuple[QuotaRule, ...], dict]:
    """Resolve global + quota gates and exact JSON-shaped overrides once."""
    c = _cctally()
    config = c.load_config()
    alerts = _cctally_core._get_alerts_config(config)
    quota = c._get_quota_alerts_config(config)
    rules = tuple(
        QuotaRule(
            source=rule["source"],
            source_root_key=rule["source_root_key"],
            logical_limit_key=rule["logical_limit_key"],
            actual_thresholds=tuple(rule["actual_thresholds"]),
            projected_thresholds=tuple(rule["projected_thresholds"]),
        )
        for rule in quota["rules"]
    )
    return bool(alerts["enabled"]), bool(quota["enabled"]), rules, quota


def _arming_row(conn: sqlite3.Connection, identity: QuotaWindowIdentity) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT rule_fingerprint, activated_at_utc FROM quota_alert_arming
             WHERE source=? AND source_root_key=? AND logical_limit_key=?
               AND observed_slot=? AND window_minutes=?""",
        (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes,
        ),
    ).fetchone()


def _activate_quota_rule(
    conn: sqlite3.Connection, identity: QuotaWindowIdentity, fingerprint: str, now_iso: str,
) -> tuple[bool, dt.datetime]:
    """Persist one identity's resolved rule boundary, returning (changed, at)."""
    row = _arming_row(conn, identity)
    if row is not None and row["rule_fingerprint"] == fingerprint:
        return False, _parse_utc(str(row["activated_at_utc"]), "activated_at_utc")
    conn.execute(
        """INSERT INTO quota_alert_arming
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, rule_fingerprint, activated_at_utc)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(source, source_root_key, logical_limit_key,
                           observed_slot, window_minutes) DO UPDATE SET
                 rule_fingerprint=excluded.rule_fingerprint,
                 activated_at_utc=excluded.activated_at_utc""",
        (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes, fingerprint, now_iso,
        ),
    )
    return True, _parse_utc(now_iso, "activated_at_utc")


def _insert_quota_terminal_event(
    conn: sqlite3.Connection,
    *, identity: QuotaWindowIdentity, resets_at: dt.datetime,
    threshold: int, kind: str, qualifying_percent: float | None,
    projected_percent: float | None, disposition: str, now_iso: str,
) -> bool:
    """Claim one durable threshold lifecycle row; unique-key races converge."""
    alerted_at = now_iso if disposition == "alerted" else None
    suppressed_at = now_iso if disposition == "suppressed_backfill" else None
    cur = conn.execute(
        """INSERT OR IGNORE INTO quota_threshold_events
               (source, source_root_key, logical_limit_key, observed_slot,
                window_minutes, resets_at_utc, threshold, qualifying_kind,
                qualifying_percent, projected_percent, severity, created_at_utc,
                disposition, alerted_at, suppressed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identity.source, identity.source_root_key, identity.logical_limit_key,
            identity.observed_slot, identity.window_minutes, _utc_iso(resets_at),
            threshold, kind, qualifying_percent, projected_percent,
            _cctally().severity_for(threshold), now_iso, disposition,
            alerted_at, suppressed_at,
        ),
    )
    return cur.rowcount == 1


def _block_observations_at_or_before(
    block: QuotaBlock, at: dt.datetime,
) -> tuple[QuotaObservation, ...]:
    return tuple(point for point in block.observations if point.captured_at <= at)


def _quota_projection_for_block(
    history: QuotaHistory, block: QuotaBlock, now: dt.datetime,
) -> float | None:
    """Return a fresh projected percent only for the current native block."""
    forecast = forecast_quota(history.physical_observations, now)
    if forecast.status != "ok" or forecast.resets_at != block.resets_at:
        return None
    return forecast.projected_percent


def _quota_alert_payload(
    *, identity: QuotaWindowIdentity, resets_at: dt.datetime, threshold: int,
    kind: str, now_iso: str, qualifying_percent: float | None,
    projected_percent: float | None,
) -> dict:
    return _cctally()._build_alert_payload_quota(
        source=identity.source, source_root_key=identity.source_root_key,
        logical_limit_key=identity.logical_limit_key,
        observed_slot=identity.observed_slot, window_minutes=identity.window_minutes,
        resets_at_utc=_utc_iso(resets_at), threshold=threshold, kind=kind,
        crossed_at_utc=now_iso, qualifying_percent=qualifying_percent,
        projected_percent=projected_percent,
    )


def _evaluate_quota_alerts(
    conn: sqlite3.Connection,
    *, observations: tuple[QuotaObservation, ...], alert_eligible_roots: set[str],
    now: dt.datetime, now_iso: str,
) -> list[dict]:
    """Arm or claim quota alerts within the caller's stats transaction.

    A fresh fingerprint writes only terminal backfill suppressions. Later
    eligible observations can claim an alerted row. No non-terminal state is
    stored: the arming boundary plus unique terminal event key is sufficient.
    """
    if not alert_eligible_roots:
        return []
    global_enabled, quota_enabled, rules, config = _quota_alert_config()
    # Disabled delivery is entirely inert: it must not leave an arming
    # boundary that could turn disabled-period evidence into a later alert.
    if not (global_enabled and quota_enabled):
        placeholders = ", ".join("?" for _ in alert_eligible_roots)
        conn.execute(
            f"""DELETE FROM quota_alert_arming
                  WHERE source='codex' AND source_root_key IN ({placeholders})""",
            tuple(sorted(alert_eligible_roots)),
        )
        return []
    histories = build_history(observations)
    queued: list[dict] = []
    for history in histories:
        identity = history.identity
        if identity.source_root_key not in alert_eligible_roots:
            continue
        resolved = resolve_quota_rule(
            identity,
            default_actual_thresholds=config["actual_thresholds"],
            default_projected_thresholds=config["projected_thresholds"],
            rules=rules,
        )
        fingerprint = quota_rule_fingerprint(
            identity, resolved, global_enabled=global_enabled,
            quota_enabled=quota_enabled,
        )
        changed, activated_at = _activate_quota_rule(conn, identity, fingerprint, now_iso)

        # Future evidence is never a threshold qualifier (including a first
        # activation backfill). A later well-clocked observation creates the
        # appropriate normal activation/claim path.
        freshness = quota_freshness(history.physical_observations, now)
        if freshness.state == "future":
            continue
        blocks = tuple(
            block for block in build_blocks(history.observations)
            if block.identity == identity
        )
        for block in blocks:
            present = _block_observations_at_or_before(block, now)
            if not present:
                continue
            if changed:
                actual_percent = max(point.used_percent for point in present)
                projected_percent = _quota_projection_for_block(history, block, now)
                for decision in quota_threshold_decisions(
                    current_percent=actual_percent,
                    projected_percent=projected_percent,
                    actual_thresholds=resolved.actual_thresholds,
                    projected_thresholds=resolved.projected_thresholds,
                ):
                    _insert_quota_terminal_event(
                        conn, identity=identity, resets_at=block.resets_at,
                        threshold=decision.threshold, kind=decision.kind,
                        qualifying_percent=(actual_percent if decision.kind == "actual" else None),
                        projected_percent=(
                            projected_percent if decision.kind == "projected" else None
                        ),
                        disposition="suppressed_backfill", now_iso=now_iso,
                    )
                continue
            later = tuple(point for point in present if point.captured_at > activated_at)
            if not later:
                continue
            actual_percent = max(point.used_percent for point in later)
            projected_percent = None
            baseline = select_baseline(history.observations, now)
            if (
                freshness.state != "stale" and baseline is not None
                and baseline.resets_at == block.resets_at
                and baseline.captured_at > activated_at
            ):
                projected_percent = _quota_projection_for_block(history, block, now)
            for decision in quota_threshold_decisions(
                current_percent=actual_percent,
                projected_percent=projected_percent,
                actual_thresholds=resolved.actual_thresholds,
                projected_thresholds=resolved.projected_thresholds,
            ):
                qualifying = actual_percent if decision.kind == "actual" else None
                projected = projected_percent if decision.kind == "projected" else None
                if _insert_quota_terminal_event(
                    conn, identity=identity, resets_at=block.resets_at,
                    threshold=decision.threshold, kind=decision.kind,
                    qualifying_percent=qualifying, projected_percent=projected,
                    disposition="alerted", now_iso=now_iso,
                ):
                    queued.append(_quota_alert_payload(
                        identity=identity, resets_at=block.resets_at,
                        threshold=decision.threshold, kind=decision.kind,
                        now_iso=now_iso, qualifying_percent=qualifying,
                        projected_percent=projected,
                    ))
    return queued


def reconcile_codex_quota_projection(
    *,
    source_root_keys: Iterable[str] | None = None,
    alert_eligible_root_keys: Iterable[str] = (),
    now: dt.datetime | None = None,
    _before_stats_commit: Callable[[], None] | None = None,
    _after_stats_commit: Callable[[], None] | None = None,
) -> QuotaProjectionResult:
    """Reconcile every active Codex root into one stats transaction.

    Reporting reconciles every configured root. Threshold evaluation is limited
    to explicitly lifecycle-eligible roots, so read-only quota commands pass an
    empty set and never create an alert claim or activation boundary.
    """
    if now is None:
        now = dt.datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_iso = _utc_iso(now)

    alert_eligible_roots = {str(key) for key in alert_eligible_root_keys}

    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)
    try:
        # F2: read active_roots, the physical sequence, and the certificate
        # inside ONE WAL read snapshot so a concurrent commit cannot interleave
        # a stale sequence with a fresh certificate.
        cache.execute("BEGIN")
        try:
            active_roots = (
                _cache_root_keys(cache)
                if source_root_keys is None else {str(key) for key in source_root_keys}
            )
            physical_sequence = codex_physical_mutation_seq(cache)
            certificate = load_codex_quota_projection_certificate(cache)
        finally:
            cache.commit()
        # Short-circuit: when nothing is alert-eligible and the certificate
        # proves the cache physical state is current AND the stats-side
        # projection still matches it (F1), the ~2.9 s observation load and the
        # whole reconcile are provably a no-op. Any missed concurrent write
        # leaves cur_seq != cert_seq (or a stats-signature mismatch) on the next
        # call, so the scheme is self-healing.
        if not alert_eligible_roots and certificate is not None:
            cert_seq, cert_sigs = certificate
            if physical_sequence == cert_seq and active_roots <= set(cert_sigs):
                stats_conn = _cctally_core.open_db()
                try:
                    if _stats_projection_signatures_match(
                        stats_conn, active_roots, cert_sigs
                    ):
                        return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)
                finally:
                    stats_conn.close()
        observations = load_codex_quota_observations(
            source_root_keys=active_roots, cache_conn=cache,
        )
    finally:
        cache.close()

    # No configured roots and no existing interpreted history means there is no
    # stats work.  This preserves the existing empty-Codex sync fast path.
    stats = _cctally_core.open_db()
    try:
        historic_roots = _historic_root_keys(stats)
        roots_to_reconcile = active_roots | historic_roots
        if not roots_to_reconcile:
            return QuotaProjectionResult(None, 0, 0, 0, 0, 0, 0)

        generation = secrets.token_hex(16)
        blocks = build_blocks(observations)
        queued_alerts: list[dict] = []
        stats.execute("BEGIN IMMEDIATE")
        try:
            for block in blocks:
                stats.execute(_BLOCK_UPSERT, _block_params(block, generation))
                for milestone in percent_milestones(block):
                    stats.execute(
                        _MILESTONE_UPSERT,
                        _milestone_params(block, milestone, generation),
                    )
            blocks_orphaned, milestones_orphaned = _orphan_unseen(
                stats, roots_to_reconcile, generation, now_iso,
            )
            queued_alerts = _evaluate_quota_alerts(
                stats, observations=observations,
                alert_eligible_roots=alert_eligible_roots & active_roots,
                now=now, now_iso=now_iso,
            )
            # The completion stamp is intentionally the final DML in the stats
            # transaction.  A pre-commit failure rolls all projection updates
            # back; a retry sees the prior complete generation or rederives it.
            for root_key in sorted(active_roots):
                stats.execute(
                    """INSERT INTO quota_projection_state
                       (source_root_key, generation, physical_signature, completed_at_utc)
                       VALUES (?,?,?,?)
                       ON CONFLICT(source_root_key) DO UPDATE SET
                         generation=excluded.generation,
                         physical_signature=excluded.physical_signature,
                         completed_at_utc=excluded.completed_at_utc""",
                    (root_key, generation, _signature(observations, root_key), now_iso),
                )
            if _before_stats_commit is not None:
                _before_stats_commit()
            stats.commit()
        except Exception:
            stats.rollback()
            raise
    finally:
        stats.close()

    if _after_stats_commit is not None:
        _after_stats_commit()
    _store_codex_quota_projection_certificate(
        sequence=physical_sequence,
        signatures={root_key: _signature(observations, root_key) for root_key in active_roots},
    )
    # Set-then-dispatch: all alert claims committed before this best-effort I/O.
    # The dispatch helper never raises on notifier/FS failures; retain the
    # defensive guard so an injected test double cannot reopen a refire path.
    for payload in queued_alerts:
        try:
            _cctally()._dispatch_alert_notification(payload, mode="real")
        except Exception:
            pass
    return QuotaProjectionResult(
        generation=generation,
        blocks_upserted=len(blocks),
        milestones_upserted=sum(len(percent_milestones(block)) for block in blocks),
        blocks_orphaned=blocks_orphaned,
        milestones_orphaned=milestones_orphaned,
        roots_stamped=len(active_roots),
        alerts_dispatched=len(queued_alerts),
    )


def _load_active_milestones(
    identity: QuotaWindowIdentity, resets_at: dt.datetime,
) -> list[sqlite3.Row]:
    stats = _cctally_core.open_db()
    try:
        return list(stats.execute(
            """SELECT percent_threshold, captured_at_utc, source_path, line_offset
                 FROM quota_percent_milestones
                WHERE source=? AND source_root_key=? AND logical_limit_key=?
                  AND observed_slot=? AND window_minutes=? AND resets_at_utc=?
                  AND orphaned_at IS NULL
                ORDER BY percent_threshold""",
            (
                identity.source, identity.source_root_key, identity.logical_limit_key,
                identity.observed_slot, identity.window_minutes, _utc_iso(resets_at),
            ),
        ))
    finally:
        stats.close()


def _matching_block_observations(
    identity: QuotaWindowIdentity, resets_at: dt.datetime,
) -> tuple[QuotaObservation, ...]:
    return tuple(
        observation for observation in load_codex_quota_observations(
            source_root_keys={identity.source_root_key},
        )
        if observation.identity == identity and observation.resets_at == resets_at
    )


def codex_quota_breakdown(
    identity: QuotaWindowIdentity,
    resets_at: str | dt.datetime,
    *, speed: str = "auto",
) -> tuple[CodexQuotaBreakdownRow, ...]:
    """Correlate durable milestone boundaries with live-priced cache accounting.

    Each comparison is the full physical tuple ``(timestamp, path, offset)`` so
    same-timestamp records stay deterministic.  Pricing is deliberately read
    now rather than materialized in stats.db, keeping a pricing refresh
    immediately effective for historical quota breakdowns.
    """
    reset = _parse_utc(resets_at, "resets_at") if isinstance(resets_at, str) else resets_at
    if reset.tzinfo is None or reset.utcoffset() is None:
        raise ValueError("resets_at must be timezone-aware")
    reset = reset.astimezone(UTC)
    points = sorted(_matching_block_observations(identity, reset), key=_physical_tuple)
    if not points:
        return ()
    milestones = _load_active_milestones(identity, reset)
    if not milestones:
        return ()
    try:
        cache = _cache_connection()
    except (FileNotFoundError, sqlite3.Error):
        return ()
    try:
        entries = []
        for row in cache.execute(
            """SELECT timestamp_utc, source_path, line_offset, model,
                      input_tokens, cached_input_tokens, output_tokens,
                      reasoning_output_tokens, total_tokens
                 FROM codex_session_entries
                WHERE source_root_key=?""",
            (identity.source_root_key,),
        ):
            try:
                physical = (_parse_utc(str(row["timestamp_utc"]), "timestamp_utc"),
                            str(row["source_path"]), int(row["line_offset"]))
            except (TypeError, ValueError):
                continue
            entries.append((physical, row))
    finally:
        cache.close()
    entries.sort(key=lambda pair: pair[0])
    resolved_speed = sys.modules["cctally"]._resolve_codex_speed(speed)
    calculate_cost = sys.modules["cctally"]._calculate_codex_entry_cost
    start = _physical_tuple(points[0])
    prior_cumulative = 0.0
    cumulative_input = 0
    cumulative_cached = 0
    cumulative_output = 0
    cumulative_reasoning = 0
    cumulative_total = 0
    result: list[CodexQuotaBreakdownRow] = []
    for milestone in milestones:
        end = (
            _parse_utc(str(milestone["captured_at_utc"]), "captured_at_utc"),
            str(milestone["source_path"]), int(milestone["line_offset"]),
        )
        selected = [row for physical, row in entries if start < physical <= end]
        input_tokens = sum(int(row["input_tokens"]) for row in selected)
        cached = sum(int(row["cached_input_tokens"]) for row in selected)
        output = sum(int(row["output_tokens"]) for row in selected)
        reasoning = sum(int(row["reasoning_output_tokens"]) for row in selected)
        total = sum(int(row["total_tokens"]) for row in selected)
        marginal = sum(
            calculate_cost(
                str(row["model"]), int(row["input_tokens"]),
                int(row["cached_input_tokens"]), int(row["output_tokens"]),
                int(row["reasoning_output_tokens"]), speed=resolved_speed,
            )
            for row in selected
        )
        cumulative = prior_cumulative + marginal
        cumulative_input += input_tokens
        cumulative_cached += cached
        cumulative_output += output
        cumulative_reasoning += reasoning
        cumulative_total += total
        result.append(CodexQuotaBreakdownRow(
            percent=int(milestone["percent_threshold"]), captured_at=end[0],
            input_tokens=cumulative_input, cached_input_tokens=cumulative_cached,
            output_tokens=cumulative_output, reasoning_output_tokens=cumulative_reasoning,
            total_tokens=cumulative_total, cost_usd=cumulative,
            marginal_cost_usd=marginal,
        ))
        prior_cumulative = cumulative
        start = end
    return tuple(result)


# === Canonical nested `cctally codex quota` CLI ===========================


class QuotaCLIError(ValueError):
    """A cctally-native quota CLI validation failure (exit 2)."""


def _cctally():
    """Resolve the current main module at call time for test isolation."""
    return sys.modules["cctally"]


def _iso_z(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity_wire(identity: QuotaWindowIdentity) -> dict[str, object]:
    return {
        "source": identity.source,
        "sourceRootKey": identity.source_root_key,
        "logicalLimitKey": identity.logical_limit_key,
        "observedSlot": identity.observed_slot,
        "windowMinutes": identity.window_minutes,
        "limitId": identity.limit_id,
        "limitName": identity.limit_name,
    }


def _freshness_wire(freshness: QuotaFreshness) -> dict[str, object]:
    return {
        "state": freshness.state,
        "source": "local-rollout",
        "capturedAt": _iso_z(freshness.captured_at),
        "ageSeconds": freshness.age_seconds,
        "staleAfterSeconds": freshness.stale_after_seconds,
    }


def _observation_wire(observation: QuotaObservation) -> dict[str, object]:
    return {
        "capturedAt": _iso_z(observation.captured_at),
        "usedPercent": observation.used_percent,
        "resetsAt": _iso_z(observation.resets_at),
        "sourcePathKey": source_path_key(observation.source_path),
        "lineOffset": observation.line_offset,
    }


def _duration_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _identity_label(identity: QuotaWindowIdentity) -> str:
    return (
        f"{identity.observed_slot} · {_duration_label(identity.window_minutes)}"
        f" · root={identity.source_root_key} · limit={identity.logical_limit_key}"
    )


def _parse_as_of(value: str | None) -> dt.datetime:
    if value is None:
        return _command_as_of().astimezone(UTC)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid --as-of timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_reset_at(value: str) -> dt.datetime:
    if "T" not in value and "t" not in value:
        raise QuotaCLIError("--reset-at rejects date-only input; include HH:MM")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid --reset-at timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_range_bound(value: str | None, *, display_tz, option: str) -> dt.datetime | None:
    if value is None:
        return None
    try:
        if "T" not in value and "t" not in value:
            date = dt.date.fromisoformat(value)
            return dt.datetime.combine(date, dt.time.min, tzinfo=display_tz).astimezone(UTC)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuotaCLIError(f"invalid {option} timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QuotaCLIError(f"{option} datetime must include an offset (or use a date-only value)")
    return parsed.astimezone(UTC)


def _history_in_range(
    history: QuotaHistory, *, since: dt.datetime | None, until: dt.datetime | None,
) -> tuple[QuotaObservation, ...]:
    return tuple(
        observation for observation in history.physical_observations
        if (since is None or observation.captured_at >= since)
        and (until is None or observation.captured_at < until)
    )


def _candidate_text(histories: tuple[QuotaHistory, ...]) -> str:
    if not histories:
        return "  (no active Codex quota identities)"
    return "\n".join(
        "  root-key={root} limit-key={limit}".format(
            root=history.identity.source_root_key,
            limit=history.identity.logical_limit_key,
        )
        for history in histories
    )


def _select_histories(
    histories: tuple[QuotaHistory, ...], *, root_key: str | None, limit_key: str | None,
) -> tuple[QuotaHistory, ...]:
    selected = tuple(
        history for history in histories
        if (root_key is None or history.identity.source_root_key == root_key)
        and (limit_key is None or history.identity.logical_limit_key == limit_key)
    )
    if (root_key is not None or limit_key is not None) and not selected:
        raise QuotaCLIError(
            "no quota identity matches the exact selectors; candidates:\n"
            + _candidate_text(histories)
        )
    return selected


def _sync_and_load(args, as_of: dt.datetime) -> tuple[QuotaHistory, ...]:
    c = _cctally()
    if not getattr(args, "no_sync", False):
        cache = c.open_cache_db()
        try:
            c.sync_codex_cache(cache)
        finally:
            cache.close()
    # All five CLI leaves use the single durable projection reconciler.  It
    # gives breakdown its milestone index and heals a cache/stats interruption
    # without ever reinterpreting or mutating physical cache evidence here.
    reconcile_codex_quota_projection(now=as_of)
    observations = load_codex_quota_observations()
    return build_history(observations)


def _command_context(args, *, range_args: bool = False):
    c = _cctally()
    config = c._load_claude_config_for_args(args)
    display_tz = c._resolve_display_tz_obj(config)
    as_of = _parse_as_of(getattr(args, "as_of", None))
    since = until = None
    if range_args:
        since = _parse_range_bound(getattr(args, "since", None), display_tz=display_tz, option="--since")
        until = _parse_range_bound(getattr(args, "until", None), display_tz=display_tz, option="--until")
        if since is not None and until is not None and until <= since:
            raise QuotaCLIError("--until must be after --since")
    histories = _sync_and_load(args, as_of)
    selected = _select_histories(
        histories,
        root_key=getattr(args, "root_key", None),
        limit_key=getattr(args, "limit_key", None),
    )
    return as_of, since, until, selected


def _emit(args, payload: dict[str, object], text: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(stamp_schema_version(payload), ensure_ascii=False))
    else:
        print(text)
    return 0


def _command_error(exc: QuotaCLIError) -> int:
    eprint(f"cctally codex quota: {exc}")
    return 2


def cmd_codex_quota_history(args) -> int:
    """Render root-qualified physical local-rollout quota history."""
    try:
        as_of, since, until, histories = _command_context(args, range_args=True)
    except QuotaCLIError as exc:
        return _command_error(exc)
    windows = []
    text_rows = ["Codex quota history · local-rollout"]
    for history in histories:
        shown = _history_in_range(history, since=since, until=until)
        if not shown:
            continue
        freshness = quota_freshness(history.physical_observations, as_of)
        windows.append({
            "identity": _identity_wire(history.identity),
            "freshness": _freshness_wire(freshness),
            "orphaned": False,
            "observations": [_observation_wire(observation) for observation in shown],
        })
        text_rows.append(_identity_label(history.identity))
        text_rows.extend(
            "  {at}  {percent:.1f}%  reset {reset}  path {path}".format(
                at=_iso_z(observation.captured_at), percent=observation.used_percent,
                reset=_iso_z(observation.resets_at), path=source_path_key(observation.source_path),
            )
            for observation in shown
        )
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "windows": windows,
    }
    if len(text_rows) == 1:
        text_rows.append("No Codex quota history.")
    return _emit(args, payload, "\n".join(text_rows))


def _statusline_status(history: QuotaHistory, as_of: dt.datetime) -> tuple[str, QuotaObservation | None, QuotaFreshness]:
    freshness = quota_freshness(history.physical_observations, as_of)
    current = select_baseline(history.observations, as_of)
    if freshness.state == "future":
        return "future", current, freshness
    if current is None:
        return "unavailable", None, freshness
    if freshness.state == "stale":
        return "stale", current, freshness
    return "ok", current, freshness


def cmd_codex_quota_statusline(args) -> int:
    """Render one truthful native status segment for every selected identity."""
    try:
        as_of, _since, _until, histories = _command_context(args)
    except QuotaCLIError as exc:
        return _command_error(exc)
    windows = []
    text_rows = []
    for history in histories:
        status, current, freshness = _statusline_status(history, as_of)
        label = _identity_label(history.identity)
        windows.append({
            "identity": _identity_wire(history.identity),
            "freshness": _freshness_wire(freshness),
            "status": status,
            "current": None if current is None else {
                "usedPercent": current.used_percent, "resetsAt": _iso_z(current.resets_at),
            },
            "label": label,
        })
        if current is None:
            row = f"{label} · unavailable"
        else:
            row = f"{label} · {current.used_percent:.1f}% · resets {_iso_z(current.resets_at)}"
        if status == "future":
            row += " · FUTURE DATA"
        elif status == "stale":
            row += " · STALE"
        text_rows.append(row)
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "windows": windows,
    }
    return _emit(args, payload, "\n".join(text_rows) if text_rows else "Codex quota unavailable.")


def _forecast_wire(history: QuotaHistory, as_of: dt.datetime) -> dict[str, object]:
    forecast: QuotaForecast = forecast_quota(history.physical_observations, as_of)
    freshness = quota_freshness(history.physical_observations, as_of)
    return {
        "identity": _identity_wire(history.identity),
        "freshness": _freshness_wire(freshness), "status": forecast.status,
        "currentPercent": forecast.current_percent,
        "ratePercentPerHour": forecast.rate_percent_per_hour,
        "projectedPercent": forecast.projected_percent,
        "resetsAt": _iso_z(forecast.resets_at),
        "remainingSeconds": forecast.remaining_seconds,
        "sampleCount": forecast.sample_count,
        "sampleSpanSeconds": forecast.sample_span_seconds,
        "confidence": forecast.confidence,
    }


def cmd_codex_quota_forecast(args) -> int:
    """Render independent native-window forecasts without quota blending."""
    try:
        as_of, _since, _until, histories = _command_context(args)
    except QuotaCLIError as exc:
        return _command_error(exc)
    forecasts = [_forecast_wire(history, as_of) for history in histories]
    text_rows = ["Codex quota forecast · local-rollout"]
    for history, forecast in zip(histories, forecasts):
        label = _identity_label(history.identity)
        current = forecast["currentPercent"]
        projected = forecast["projectedPercent"]
        row = f"{label} · {forecast['status']}"
        if current is not None:
            row += f" · current {float(current):.1f}%"
        if projected is not None:
            row += f" · projected {float(projected):.1f}%"
        text_rows.append(row)
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "forecasts": forecasts,
    }
    return _emit(args, payload, "\n".join(text_rows))


def cmd_codex_quota_blocks(args) -> int:
    """Render reset-native quota blocks from the provider-neutral kernel."""
    try:
        as_of, since, until, histories = _command_context(args, range_args=True)
    except QuotaCLIError as exc:
        return _command_error(exc)
    blocks = []
    text_rows = ["Codex quota blocks · local-rollout"]
    for block in build_blocks(
        observation for history in histories for observation in history.physical_observations
    ):
        if since is not None and block.last_observed_at < since:
            continue
        if until is not None and block.first_observed_at >= until:
            continue
        blocks.append({
            "identity": _identity_wire(block.identity), "resetAt": _iso_z(block.resets_at),
            "nominalStartAt": _iso_z(block.nominal_start_at),
            "firstObservedAt": _iso_z(block.first_observed_at),
            "lastObservedAt": _iso_z(block.last_observed_at),
            "firstPercent": block.first_percent, "currentPercent": block.current_percent,
            "orphaned": False,
        })
        text_rows.append(
            f"{_identity_label(block.identity)} · {block.first_percent:.1f}% → "
            f"{block.current_percent:.1f}% · reset {_iso_z(block.resets_at)}"
        )
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "blocks": blocks,
    }
    if len(text_rows) == 1:
        text_rows.append("No Codex quota blocks.")
    return _emit(args, payload, "\n".join(text_rows))


def cmd_codex_quota_breakdown(args) -> int:
    """Render root-qualified, live-priced milestone deltas for one block."""
    try:
        as_of, _since, _until, histories = _command_context(args)
        if len(histories) != 1:
            raise QuotaCLIError(
                "breakdown requires selectors resolving to exactly one quota identity; candidates:\n"
                + _candidate_text(histories)
            )
        reset_at = _parse_reset_at(args.reset_at)
        identity = histories[0].identity
        matching = [
            block for block in build_blocks(histories[0].physical_observations)
            if block.resets_at == reset_at
        ]
        if len(matching) != 1:
            raise QuotaCLIError(
                "--reset-at matches no unique native quota block; candidates:\n"
                + _candidate_text(histories)
            )
    except QuotaCLIError as exc:
        return _command_error(exc)
    c = _cctally()
    speed = c._resolve_codex_speed(args.speed)
    rows = codex_quota_breakdown(identity, reset_at, speed=speed)
    block = matching[0]
    milestones = [
        {
            "percent": row.percent, "capturedAt": _iso_z(row.captured_at),
            "inputTokens": row.input_tokens, "cachedInputTokens": row.cached_input_tokens,
            "outputTokens": row.output_tokens, "reasoningOutputTokens": row.reasoning_output_tokens,
            "totalTokens": row.total_tokens, "costUSD": row.cost_usd,
            "marginalCostUSD": row.marginal_cost_usd,
        }
        for row in rows
    ]
    payload = {
        "source": "codex", "generatedAt": _iso_z(as_of),
        "freshnessSource": "local-rollout", "identity": _identity_wire(identity),
        "block": {"resetAt": _iso_z(block.resets_at), "nominalStartAt": _iso_z(block.nominal_start_at)},
        "speed": speed, "milestones": milestones,
    }
    text_rows = [
        f"Codex quota breakdown · {_identity_label(identity)}",
        f"reset {_iso_z(block.resets_at)} · speed {speed}",
    ]
    text_rows.extend(
        f"{row.percent:>3}%  {row.total_tokens:>8} tokens  ${row.cost_usd:.6f}  Δ${row.marginal_cost_usd:.6f}"
        for row in rows
    )
    if not rows:
        text_rows.append("No percent milestones.")
    return _emit(args, payload, "\n".join(text_rows))
