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

#1015 (spec 5.1) adds the lineage facts beside them, on the same paths:

* `claude_weekly_observation_index`, one row per admitted Claude usage
  observation carrying a weekly reading (:func:`project_observation` live and
  scratch; :class:`RebuildProjector` in the rebuild, which stages a record the
  legacy normalization would stamp in `rebuild_weekly_observation_unstamped`
  until :func:`attribute_staged_observations` knows the cutover account);
* `claude_weekly_correction_history`, every decision of every reviewed op with
  its decoded disposition, written by the same op applier;
* `claude_weekly_correction_ops`, the op's journal coordinate, written by
  :func:`record_correction_op` on the published paths only (live ingest and
  the rebuild). A scratch replay never writes it.

#1018 twin (spec 5.1, stats epoch 1024) enriches each observation-index row
with the record's raw weekly value, its normalized provider window, whether
it is a genuine weekly reading for the account the row receives (the
detector mask's weekly element, :func:`weekly_reading_genuine`) and whether it
may confirm a displayed weekly value (`weekly_freshness_eligible`: genuine,
both values present, captured strictly before the reset, and not held by the
effective reviewed decision). Every path computes them through
:func:`weekly_observation_facts`; the decision applier keeps eligibility
current, and an insert consults the decisions already folded, so the fold
order cannot change the final row. :func:`latest_weekly_reading_at` (spec
5.2) is the one read: the newest eligible confirmation of one displayed
value.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
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
    " ORDER BY s.requested_at_utc"
)
_READ_FIVE_HOUR = (
    f"SELECT {_COLUMNS} FROM claude_oauth_samples AS s"
    " WHERE s.account_key = ? AND s.five_hour_window_key = ?"
    "   AND s.requested_at_utc > ? AND s.five_hour_percent IS NOT NULL"
    "   AND unixepoch(s.captured_at_utc)"
    "       < MIN(unixepoch(s.five_hour_resets_at_utc), s.five_hour_window_key)"
    " ORDER BY s.requested_at_utc"
)
#: #1018 twin spec 5.1: the eligibility bound on insert is the caller's
#: account-specific value AND no effective `hold` already folded, so a
#: decision folded before its observation still applies.
_INSERT_OBSERVATION = (
    "INSERT OR IGNORE INTO claude_weekly_observation_index (observation_id,"
    " account_key, captured_at_utc, weekly_percent, weekly_resets_at_utc,"
    " weekly_reading_genuine, weekly_freshness_eligible)"
    " VALUES (?, ?, ?, ?, ?, ?, ? AND NOT EXISTS (SELECT 1"
    " FROM claude_weekly_observation_decisions AS d"
    " WHERE d.observation_id = ? AND d.disposition = 'hold'))"
)
#: The apply-only rebuild's insert. Its read pass flushes every queued row
#: before the Phase 1 fold applies any reviewed decision op, and the applier
#: then maintains eligibility on the indexed row, so the rebuild binds the
#: account-specific value directly: no statement of the pass reads a projected
#: table before the final flush (#1003 D7), and the batch size cannot change
#: a published row.
_INSERT_OBSERVATION_REBUILD = (
    "INSERT OR IGNORE INTO claude_weekly_observation_index (observation_id,"
    " account_key, captured_at_utc, weekly_percent, weekly_resets_at_utc,"
    " weekly_reading_genuine, weekly_freshness_eligible)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
#: The rebuild's staging table for observations whose account is the cutover
#: op's (known only after the read pass). Scratch-index only; dropped by
#: :func:`attribute_staged_observations` before the inventory check. #1018
#: twin: it carries the account-independent facts, so genuineness and
#: eligibility are computed against the cutover account when the row moves.
#: `pre_reset` is 1 exactly when both values are present and parseable and
#: the capture is strictly before the reset (parsed instants).
STAGING_TABLE = "rebuild_weekly_observation_unstamped"
_CREATE_STAGING = (
    "CREATE TABLE IF NOT EXISTS rebuild_weekly_observation_unstamped ("
    " observation_id TEXT PRIMARY KEY, captured_at_utc TEXT NOT NULL,"
    " weekly_percent REAL, weekly_resets_at_utc TEXT,"
    " base_genuine INTEGER NOT NULL, required_account TEXT,"
    " pre_reset INTEGER NOT NULL)"
)
_INSERT_STAGED = (
    "INSERT OR IGNORE INTO rebuild_weekly_observation_unstamped"
    " (observation_id, captured_at_utc, weekly_percent, weekly_resets_at_utc,"
    " base_genuine, required_account, pre_reset) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_ATTRIBUTE_STAGED = (
    "INSERT OR IGNORE INTO claude_weekly_observation_index"
    " (observation_id, account_key, captured_at_utc, weekly_percent,"
    " weekly_resets_at_utc, weekly_reading_genuine, weekly_freshness_eligible)"
    " SELECT s.observation_id, ?, s.captured_at_utc, s.weekly_percent,"
    " s.weekly_resets_at_utc,"
    " s.base_genuine AND (s.required_account IS NULL OR s.required_account = ?),"
    " s.base_genuine AND (s.required_account IS NULL OR s.required_account = ?)"
    " AND s.pre_reset AND NOT EXISTS (SELECT 1"
    " FROM claude_weekly_observation_decisions AS d"
    " WHERE d.observation_id = s.observation_id AND d.disposition = 'hold')"
    " FROM rebuild_weekly_observation_unstamped AS s"
)
#: #1018 twin spec 5.1: the decision applier's eligibility maintenance.
_HOLD_ELIGIBILITY = (
    "UPDATE claude_weekly_observation_index SET weekly_freshness_eligible = 0"
    " WHERE observation_id = ?"
)
_READ_ELIGIBILITY_FACTS = (
    "SELECT weekly_reading_genuine, weekly_percent, weekly_resets_at_utc,"
    " captured_at_utc FROM claude_weekly_observation_index"
    " WHERE observation_id = ?"
)
_SET_ELIGIBILITY = (
    "UPDATE claude_weekly_observation_index SET weekly_freshness_eligible = ?"
    " WHERE observation_id = ?"
)
_INSERT_HISTORY = (
    "INSERT OR IGNORE INTO claude_weekly_correction_history (decision_op_id,"
    " observation_id, disposition) VALUES (?, ?, ?)"
)
_INSERT_OP = (
    "INSERT OR IGNORE INTO claude_weekly_correction_ops (decision_op_id,"
    " op_segment, op_offset) VALUES (?, ?, ?)"
)
#: The reviewed weekly decision op kinds whose journal coordinate lineage
#: records.
DECISION_OP_KINDS = frozenset((
    "claude_weekly_observation_decision",
    "claude_weekly_observation_decision_v2",
))
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


def _weekly_observation_key(rec) -> "tuple[str, str] | None":
    """``(observation_id, captured_at_utc)`` of one Claude usage observation
    carrying a weekly reading — exactly the records
    `_cctally_record._pipeline_claude_usage` derives — or None. The capture
    instant is the recording path's fallback (`payload.captured_at`, else the
    record's `at`) in canonical full-precision UTC; a record whose instants
    are both unparsable projects nothing."""
    if not isinstance(rec, dict) or rec.get("t") != "obs":
        return None
    if rec.get("provider") != "claude":
        return None
    payload = rec.get("payload")
    if (not isinstance(payload, dict)
            or "resets_at" not in payload or "weekly_percent" not in payload):
        return None
    import _cctally_record
    if rec.get("src") not in _cctally_record._CLAUDE_OBS_SRCS:
        return None
    observation_id = rec.get("id")
    if not isinstance(observation_id, str) or not observation_id:
        return None
    for value in (payload.get("captured_at"), rec.get("at")):
        if not value:
            continue
        try:
            return observation_id, _lib_oauth_samples.canonical_instant(value)
        except (TypeError, ValueError):
            continue
    return None


@dataclasses.dataclass(frozen=True)
class WeeklyObservationFacts:
    """The account-independent weekly facts of one indexed observation
    (#1018 twin spec 5.1). ``required_account`` is the one account a v2
    publication's provenance names (None for a historical-policy or direct
    record); ``base_genuine`` is False for a withheld or unclassifiable
    detector step and for a `projection` (store-filled) weekly axis."""

    weekly_percent: "float | None"
    weekly_resets_at_utc: "str | None"
    base_genuine: bool
    required_account: "str | None"


#: The publication provenance kinds `_detector_observation_mask` classifies.
_PROVENANCE_KINDS = ("candidate", "projection", "retained_low")


def _weekly_genuineness(payload) -> "tuple[bool, str | None]":
    """``(base_genuine, required_account)`` of one raw payload: the weekly
    element of `_cctally_record._detector_observation_mask` with its account
    comparison factored out, so a staged row can apply it once its account is
    known. Reads only the payload."""
    import _cctally_journal
    policy = _cctally_journal.detector_policy_of(payload)
    if policy == "unsupported":
        return False, None
    if policy == "historical" or "publication_id" not in payload:
        return True, None
    provenance = payload.get("publication")
    if not isinstance(provenance, dict):
        return False, None
    accounts = set()
    for axis, present in (("sevenDay", True),
                          ("fiveHour",
                           payload.get("five_hour_percent") is not None)):
        if not present:
            continue
        source = provenance.get(axis)
        if (not isinstance(source, dict)
                or source.get("kind") not in _PROVENANCE_KINDS):
            return False, None
        account = source.get("account")
        if not isinstance(account, str) or not account:
            return False, None
        accounts.add(account)
    if len(accounts) != 1:
        # The axes name different accounts: no row account matches both.
        return False, None
    return provenance["sevenDay"]["kind"] != "projection", accounts.pop()


def weekly_observation_facts(rec) -> "WeeklyObservationFacts | None":
    """The enriched facts of one record, or None exactly when
    :func:`_weekly_observation_key` projects no index row for it."""
    if _weekly_observation_key(rec) is None:
        return None
    payload = rec["payload"]
    base_genuine, required_account = _weekly_genuineness(payload)
    return WeeklyObservationFacts(
        weekly_percent=_lib_oauth_samples.finite_percent(
            payload.get("weekly_percent")),
        weekly_resets_at_utc=_lib_oauth_samples.weekly_reset_identity(
            payload.get("resets_at")),
        base_genuine=base_genuine,
        required_account=required_account,
    )


def weekly_reading_genuine(facts: WeeklyObservationFacts,
                           account_key: str) -> bool:
    """Whether the observation is a genuine weekly reading for the row's
    ``account_key``: `_detector_observation_mask`'s weekly element for that
    account (spec 5.1)."""
    return bool(facts.base_genuine and (
        facts.required_account is None
        or facts.required_account == account_key))


def _before_reset(captured_at_utc, weekly_resets_at_utc) -> bool:
    """Both instants parse and the capture is strictly before the reset,
    compared as parsed instants: the two columns are spelled at different
    precisions, so a text comparison would admit a capture at the reset."""
    if weekly_resets_at_utc is None:
        return False
    try:
        captured = _lib_oauth_samples.canonical_instant(captured_at_utc)
        reset = _lib_oauth_samples.canonical_instant(weekly_resets_at_utc)
    except (TypeError, ValueError):
        return False
    # Both are now the same full-precision spelling, so text order is
    # chronological.
    return captured < reset


def _pre_reset(facts: WeeklyObservationFacts, captured_at_utc) -> bool:
    """Eligibility's account- and decision-independent half."""
    return (facts.weekly_percent is not None
            and _before_reset(captured_at_utc, facts.weekly_resets_at_utc))


def _observation_params(observation_id, account, captured_at_utc,
                        facts: WeeklyObservationFacts) -> tuple:
    genuine = weekly_reading_genuine(facts, account)
    return (observation_id, account, captured_at_utc, facts.weekly_percent,
            facts.weekly_resets_at_utc, int(genuine),
            int(genuine and _pre_reset(facts, captured_at_utc)),
            observation_id)


def project_observation(conn, rec, *, cutover_account=None) -> None:
    """#1015 spec 5.1: project ``rec`` into `claude_weekly_observation_index`
    on live ingest and the scratch replays, under the one attribution rule:
    the stamp, else `unattributed` — except a record the legacy normalization
    would stamp, which takes the cutover op's Claude account.

    The scratch replays normalize before their hooks, so they never reach
    that branch. Live ingest does NOT normalize an observation before the
    pipeline, so it passes ``cutover_account`` (a zero-argument callable,
    resolved at most once per cycle) and the index still agrees with the
    rebuild's staged attribution. Idempotent under the primary key."""
    key = _weekly_observation_key(rec)
    if key is None:
        return
    import _cctally_journal
    import _lib_accounts
    account = rec.get("account") or _lib_accounts.UNATTRIBUTED
    if (cutover_account is not None
            and _cctally_journal.needs_cutover_attribution(rec)):
        account = cutover_account()
    conn.execute(_INSERT_OBSERVATION, _observation_params(
        key[0], account, key[1], weekly_observation_facts(rec)))


def attribute_staged_observations(conn, claude_account: str) -> int:
    """Move the rebuild's staged observations into the index under the
    cutover op's Claude account (``claude_account``, `unattributed` when the
    journal has none), then drop the staging table. One transaction; a no-op
    when nothing was staged. Returns the number of rows moved."""
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (STAGING_TABLE,)).fetchone()
    if present is None:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        moved = conn.execute(
            _ATTRIBUTE_STAGED,
            (claude_account, claude_account, claude_account)).rowcount
        conn.execute("DROP TABLE rebuild_weekly_observation_unstamped")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return moved


class RebuildProjector:
    """The apply-only rebuild's leg of the projection: its single read pass
    offers every admitted record, and samples are projected into the scratch
    index in bounded batches (one transaction each), so the pass never holds
    the whole sample population in memory."""

    #: #1003 D7: queued items (samples plus observation-index rows) per flush.
    #: Each flush is a `BEGIN IMMEDIATE … COMMIT` on the rebuild scratch, which
    #: keeps the store policy (a synced rollback journal per transaction), so
    #: 2,048 cost ≈ 1.8 GB of physical writes per rebuild. Every reader of the
    #: projected tables runs after the pass's final flush, so the batch moves
    #: only when rows are written, never what is read.
    BATCH = 65536

    def __init__(self, conn) -> None:
        self._conn = conn
        self._pending: list = []
        # #1015: observation-index rows, ``(statement, params)``, flushed in
        # the same bounded batches.
        self._observations: list = []
        self._staging_created = False
        self.projected = 0

    def offer(self, rec) -> None:
        sample = sample_from_record(rec)
        if sample is not None:
            self._pending.append(sample)
        key = _weekly_observation_key(rec)
        if key is not None:
            # The record is not yet normalized: a record the legacy
            # normalization would stamp takes the cutover account, which only
            # the end of the pass knows, so it is staged; every other record
            # keeps its stamp or `unattributed` (spec 5.1's three cases).
            import _cctally_journal
            facts = weekly_observation_facts(rec)
            if _cctally_journal.needs_cutover_attribution(rec):
                self._observations.append((_INSERT_STAGED, (
                    key[0], key[1], facts.weekly_percent,
                    facts.weekly_resets_at_utc, int(facts.base_genuine),
                    facts.required_account,
                    int(_pre_reset(facts, key[1])))))
            else:
                import _lib_accounts
                self._observations.append(
                    (_INSERT_OBSERVATION_REBUILD, _observation_params(
                        key[0],
                        rec.get("account") or _lib_accounts.UNATTRIBUTED,
                        key[1], facts)[:-1]))
        if len(self._pending) + len(self._observations) >= self.BATCH:
            self.flush()

    def flush(self) -> None:
        if not self._pending and not self._observations:
            return
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sample in self._pending:
                project_sample(conn, sample)
            for statement, params in self._observations:
                if statement is _INSERT_STAGED and not self._staging_created:
                    conn.execute(_CREATE_STAGING)
                    self._staging_created = True
                conn.execute(statement, params)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self.projected += len(self._pending)
        self._pending = []
        self._observations = []


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
    # #1015 spec 5.1: the history carries the DECODED disposition, so a
    # schema-3 retraction (projected as an ordinary `hold`) is `retract`.
    retractions = payload.get("weekly_axis_retractions") if version == 3 else None
    retracted = (frozenset(r for r in retractions if isinstance(r, str))
                 if isinstance(retractions, list) else frozenset())
    for item in decisions:
        if not isinstance(item, dict):
            continue
        observation_id = item.get("observationId")
        disposition = item.get("disposition")
        if (isinstance(observation_id, str) and observation_id
                and disposition in _DISPOSITIONS):
            conn.execute(_UPSERT_DECISION,
                         (observation_id, disposition, op_id))
            _maintain_freshness_eligibility(conn, observation_id, disposition)
            conn.execute(_INSERT_HISTORY, (
                op_id, observation_id,
                "retract" if (disposition == "hold"
                              and observation_id in retracted)
                else disposition))


def _maintain_freshness_eligibility(conn, observation_id: str,
                                    disposition: str) -> None:
    """#1018 twin spec 5.1: the decided observation's freshness eligibility,
    in the applier's transaction. A `hold` (a schema-3 retraction is one)
    clears it; an `accept` recomputes it from the stored genuine flag and the
    parsed pre-reset condition. An observation not yet indexed (a decision
    folded before it, or still staged in a rebuild) is covered by the insert,
    which consults the decisions table."""
    if disposition == "hold":
        conn.execute(_HOLD_ELIGIBILITY, (observation_id,))
        return
    row = conn.execute(_READ_ELIGIBILITY_FACTS, (observation_id,)).fetchone()
    if row is None:
        return
    genuine, weekly_percent, weekly_resets_at_utc, captured_at_utc = row
    eligible = bool(genuine) and weekly_percent is not None and _before_reset(
        captured_at_utc, weekly_resets_at_utc)
    conn.execute(_SET_ELIGIBILITY, (int(eligible), observation_id))


def record_correction_op(conn, record, coordinate) -> None:
    """#1015 spec 5.1: record where a reviewed weekly decision op sits in the
    journal — ``coordinate`` is the END ``(segment, offset)`` of its line, as
    the published path read it. Live ingest and the rebuild call this; a
    scratch replay has no coordinate and never does. Idempotent."""
    if coordinate is None or not isinstance(record, dict):
        return
    payload = record.get("payload")
    if (not isinstance(payload, dict)
            or payload.get("kind") not in DECISION_OP_KINDS):
        return
    op_id = record.get("id")
    if not isinstance(op_id, str) or not op_id:
        return
    conn.execute(_INSERT_OP, (op_id, str(coordinate[0]), int(coordinate[1])))


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
    # #1019: the window's index serves the `q` order; equal-`q` ties are
    # ordered here, because an `observation_id` term in the SQL ORDER BY needs
    # a temporary B-tree, which the render path's plan policy refuses (#902).
    rows.sort(key=lambda row: (row[2], row[1]))
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


@dataclasses.dataclass(frozen=True)
class WeeklyReadingScope:
    """The displayed weekly value a confirming reading must repeat (#1018
    twin spec 5.2). Built by the current-week card from its displayed row's
    originating observation; server-private, never serialized."""

    account_key: str
    #: The window identity, in the samples' second-precision spelling, read
    #: from the displayed row's originating observation (never the stored
    #: `week_end_at`, which may be a first-established boundary).
    weekly_resets_at_utc: str
    #: The displayed `used_pct` as stored: matched by exact float equality.
    weekly_percent: float
    #: Canonical full-precision text, inclusive: the card's segment start.
    lower_bound_utc: str
    #: Canonical full-precision text, exclusive: the earlier of the provider
    #: reset and the displayed window's end.
    upper_bound_utc: str


_LATEST_WEEKLY_READING = (
    "SELECT captured_at_utc FROM claude_weekly_observation_index"
    " WHERE account_key = ? AND weekly_resets_at_utc = ? AND weekly_percent = ?"
    " AND weekly_freshness_eligible = 1"
    " AND captured_at_utc >= ? AND captured_at_utc < ?"
    " ORDER BY captured_at_utc DESC LIMIT 1"
)


def latest_weekly_reading_at(conn, scope: WeeklyReadingScope,
                             now_utc: dt.datetime) -> "dt.datetime | None":
    """The capture instant of the newest eligible observation-index row that
    confirms ``scope``'s displayed value (same account, window and exact
    value; captured in ``[lower, upper)`` and at or before ``now_utc``), or
    None when none does. One statement on the freshness partial index, so it
    needs no transaction. Raises ``sqlite3.Error`` to the caller, which keeps
    its prior value rather than publishing a guess.

    The two upper limits (``< upper`` and ``<= now``) are bound as ONE
    exclusive bound, the earlier of ``upper`` and ``now`` + 1 µs: captures
    are canonical microsecond text, so ``<= now`` is exactly ``< now + 1 µs``.
    With both written as separate terms SQLite seeks on one and filters the
    other, so a suffix of readings captured after now (a clock step, a
    replayed future capture) would be walked on every tick; one bound keeps
    the lookup a single index seek."""
    upper = _lib_oauth_samples.canonical_instant(scope.upper_bound_utc)
    not_after_now = _lib_oauth_samples.canonical_instant(
        now_utc + dt.timedelta(microseconds=1))
    row = conn.execute(_LATEST_WEEKLY_READING, (
        scope.account_key, scope.weekly_resets_at_utc,
        float(scope.weekly_percent),
        _lib_oauth_samples.canonical_instant(scope.lower_bound_utc),
        min(upper, not_after_now))).fetchone()
    if row is None:
        return None
    text = str(row[0])
    return dt.datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text
    ).astimezone(dt.timezone.utc)
