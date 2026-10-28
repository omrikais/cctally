"""Provider-sample projection glue (#797/#823/#840 spec 4.3, D8 and D9).

`claude_oauth_samples` (stats epoch 1019) holds one row per ADMITTED Claude
api observation carrying a valid `oauth_request_started_at`, materialized
from the raw journal record by :func:`project_sample` — the one projection
implementation every derivation path shares:

* live ingest and the scratch replays (plain `db rederive`, the derivation-
  completion step) through `_cctally_record._pipeline_claude_usage`, which
  projects every admitted reading before reset/credit detection and the
  snapshot fold decision, so equal readings that the fold deduplicates still
  each produce a sample, and neither the five-hour MAX adjustment nor credit
  suppression or retirement touches a sample;
* ordinary and correction rebuilds (`_rebuild_stats_index_locked`), which are
  apply-only and never run the pipeline, through :class:`RebuildProjector`
  offered every admitted record of the rebuild's single read pass.

Reviewed weekly decisions (`claude_weekly_observation_decision`, its `_v2`
successor) are projected beside it into `claude_weekly_observation_decisions`
by the op fold applier :func:`apply_weekly_observation_decision_op`, latest
decision per observation winning in journal order, so the weekly eligibility
of a sample is resolved at READ time and a later decision rewrites no sample
row. A `hold` makes only the WEEKLY sample ineligible; a later effective
`accept` restores it; five-hour samples are never excluded by a weekly
decision. (The schema-3 `retract` projects each retraction as an ordinary
`hold` in `weekly_axis_decisions`, so it needs nothing further here.)

Rows are retained: nothing in this work prunes the table. The read is exact-
window (spec D9) and an unavailable projection raises
:class:`ProjectionUnavailable` rather than answering "no sample".
"""
from __future__ import annotations

import sqlite3
from typing import Literal

import _lib_oauth_samples

SAMPLES_TABLE = "claude_oauth_samples"
DECISIONS_TABLE = "claude_weekly_observation_decisions"
#: The reviewed weekly decision kinds and the payload key holding each schema
#: version's decision list.
_DECISION_LISTS = {1: "decisions", 2: "weekly_axis_decisions",
                   3: "weekly_axis_decisions"}
_DISPOSITIONS = frozenset(("hold", "accept"))

_INSERT = (
    "INSERT OR IGNORE INTO claude_oauth_samples (account_key, observation_id,"
    " requested_at_utc, captured_at_utc, weekly_percent, weekly_resets_at_utc,"
    " five_hour_percent, five_hour_resets_at_utc, five_hour_window_key)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_COLUMNS = (
    "s.account_key, s.observation_id, s.requested_at_utc, s.captured_at_utc,"
    " s.weekly_percent, s.weekly_resets_at_utc, s.five_hour_percent,"
    " s.five_hour_resets_at_utc, s.five_hour_window_key"
)
_READ_WEEKLY = (
    f"SELECT {_COLUMNS} FROM claude_oauth_samples AS s"
    " WHERE s.account_key = ? AND s.weekly_resets_at_utc = ?"
    "   AND s.requested_at_utc > ? AND s.weekly_percent IS NOT NULL"
    "   AND unixepoch(s.captured_at_utc) < unixepoch(s.weekly_resets_at_utc)"
    "   AND NOT EXISTS (SELECT 1 FROM claude_weekly_observation_decisions AS d"
    "                    WHERE d.observation_id = s.observation_id"
    "                      AND d.disposition = 'hold')"
    " ORDER BY s.requested_at_utc, s.observation_id"
)
_READ_FIVE_HOUR = (
    f"SELECT {_COLUMNS} FROM claude_oauth_samples AS s"
    " WHERE s.account_key = ? AND s.five_hour_window_key = ?"
    "   AND s.requested_at_utc > ? AND s.five_hour_percent IS NOT NULL"
    "   AND unixepoch(s.captured_at_utc)"
    "       < MIN(unixepoch(s.five_hour_resets_at_utc), s.five_hour_window_key)"
    " ORDER BY s.requested_at_utc, s.observation_id"
)
_UPSERT_DECISION = (
    "INSERT INTO claude_weekly_observation_decisions (observation_id,"
    " disposition, decision_op_id) VALUES (?, ?, ?)"
    " ON CONFLICT(observation_id) DO UPDATE SET"
    " disposition = excluded.disposition,"
    " decision_op_id = excluded.decision_op_id"
)


class ProjectionUnavailable(RuntimeError):
    """The samples projection cannot be read on this connection (absent,
    older index shape): a classification that needs it is unavailable,
    never "no proof"."""


def sample_from_record(rec) -> "_lib_oauth_samples.OAuthSample | None":
    """The sample of one journal record, or None. Only a current-revision
    (`wpr: 2`) Claude usage observation from a recording source can carry a
    sample — the same records `_pipeline_claude_usage` derives — so no key is
    ever honoured on a historical or previous-release record."""
    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return None
    payload = rec.get("payload")
    if (not isinstance(payload, dict)
            or _lib_oauth_samples.REQUEST_STARTED_KEY not in payload
            or rec.get("provider") != "claude"):
        return None
    if "resets_at" not in payload or "weekly_percent" not in payload:
        return None
    import _cctally_record
    import _lib_account_protocol
    import _lib_accounts
    if rec.get("src") not in _cctally_record._CLAUDE_OBS_SRCS:
        return None
    if not _lib_account_protocol.has_current_revision(rec):
        return None
    return _lib_oauth_samples.sample_from_observation(
        payload,
        account_key=rec.get("account") or _lib_accounts.UNATTRIBUTED,
        observation_id=rec.get("id"),
        captured_at_utc=payload.get("captured_at"),
    )


def project_sample(conn, sample: "_lib_oauth_samples.OAuthSample") -> None:
    """Materialize ``sample`` on ``conn`` (the caller's transaction).
    Idempotent: an observation id is a content id, so a replay of the same
    record inserts nothing."""
    conn.execute(_INSERT, sample.row())


def project_record(conn, rec) -> None:
    """Project ``rec``'s sample when it carries one. The caller has already
    admitted ``rec``."""
    sample = sample_from_record(rec)
    if sample is not None:
        project_sample(conn, sample)


class RebuildProjector:
    """The apply-only rebuild's leg of the projection: its single read pass
    offers every admitted record, and samples are projected into the scratch
    index in bounded batches (one transaction each), so the pass never holds
    the whole sample population in memory."""

    BATCH = 2048

    def __init__(self, conn) -> None:
        self._conn = conn
        self._pending: list = []
        self.projected = 0

    def offer(self, rec) -> None:
        sample = sample_from_record(rec)
        if sample is None:
            return
        self._pending.append(sample)
        if len(self._pending) >= self.BATCH:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sample in self._pending:
                project_sample(conn, sample)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self.projected += len(self._pending)
        self._pending = []


def apply_weekly_observation_decision_op(conn, record) -> None:
    """Fold applier for a reviewed weekly decision op: each well-formed
    ``{observationId, disposition}`` entry becomes the observation's
    effective decision (latest op wins, in journal order). Malformed entries
    are skipped here; `db rederive` validates every retained decision op and
    refuses a malformed one."""
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        return
    version = payload.get("schema_version")
    key = _DECISION_LISTS.get(version) if type(version) is int else None
    decisions = payload.get(key) if key is not None else None
    if not isinstance(decisions, list):
        return
    op_id = record.get("id")
    if not isinstance(op_id, str):
        return
    for item in decisions:
        if not isinstance(item, dict):
            continue
        observation_id = item.get("observationId")
        disposition = item.get("disposition")
        if (isinstance(observation_id, str) and observation_id
                and disposition in _DISPOSITIONS):
            conn.execute(_UPSERT_DECISION,
                         (observation_id, disposition, op_id))


def read_samples(conn, *, account_key: str,
                 axis: Literal["weekly", "five_hour"],
                 window: "str | int",
                 requested_after_utc: str,
                 ) -> "tuple[_lib_oauth_samples.OAuthSample, ...]":
    """The account's samples of exactly one authoritative window whose `q` is
    strictly after ``requested_after_utc`` and whose response was captured
    before that window's reset, ordered by `q`.

    ``window`` is the weekly reset (an epoch or a tz-aware ISO instant, raw or
    normalized: the nearest-hour normalization is the identity) for
    ``"weekly"``, and the canonical five-hour window key for
    ``"five_hour"``. The weekly read excludes an observation whose effective
    reviewed weekly decision is `hold`; the five-hour read never does. The
    capture bound uses the sample's own window (for five hours, the earlier
    of its raw reset and its window key); the caller still compares against
    its member's advertised reset. Raises :class:`ProjectionUnavailable`
    when the projection cannot be read."""
    if not isinstance(account_key, str) or not account_key:
        raise ValueError("read_samples requires an exact account")
    after = _lib_oauth_samples.canonical_instant(requested_after_utc)
    if axis == "weekly":
        sql = _READ_WEEKLY
        key = _lib_oauth_samples.weekly_window(window)
    elif axis == "five_hour":
        if type(window) is not int:
            raise ValueError("a five-hour window is its canonical int key")
        sql = _READ_FIVE_HOUR
        key = window
    else:
        raise ValueError(f"unknown sample axis: {axis!r}")
    try:
        rows = conn.execute(sql, (account_key, key, after)).fetchall()
    except sqlite3.OperationalError as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    return tuple(_lib_oauth_samples.OAuthSample(*row) for row in rows)


_READ_ONE = (
    f"SELECT {_COLUMNS} FROM claude_oauth_samples AS s"
    " WHERE s.account_key = ? AND s.observation_id = ?"
)


def sample_for_observation(conn, *, account_key: str, observation_id
                           ) -> "_lib_oauth_samples.OAuthSample | None":
    """The account's admitted sample of exactly one observation, or None when
    that observation is not an admitted sample (or the projection cannot be
    read). #797 spec 4.4: a five-hour credit's causal facts name the
    observations it was armed and baselined from; their request instants
    and RAW values are read here, never from the clamped snapshot column."""
    if not isinstance(account_key, str) or not account_key or observation_id is None:
        return None
    try:
        row = conn.execute(_READ_ONE, (account_key, str(observation_id))).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _lib_oauth_samples.OAuthSample(*row)


def projection_covered(conn, *, account_key: str) -> bool:
    """Whether ``conn`` holds a complete samples projection for
    ``account_key``: the index is at this binary's epoch with both
    projection tables. The projection commits in the same transaction as
    the journal cursor on every path, so a current index covers every
    admitted observation up to its cursor for every account; anything else
    (an older or newer index shape, a missing table, an unreadable
    connection) is uncovered."""
    if not isinstance(account_key, str) or not account_key:
        return False
    import _cctally_core
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        if epoch != _cctally_core.STATS_INDEX_EPOCH:
            return False
        names = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
            " AND name IN (?, ?)", (SAMPLES_TABLE, DECISIONS_TABLE))}
    except sqlite3.Error:
        return False
    return names == {SAMPLES_TABLE, DECISIONS_TABLE}
