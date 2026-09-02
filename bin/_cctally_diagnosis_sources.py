"""The only store-opening component of the diagnosis (#620 S2).

Both `cctally explain` and `GET /api/diagnosis` call `build_diagnosis`, which
is what makes the two surfaces incapable of drifting. Everything here is I/O
and shaping; every classification rule lives in the pure
`bin/_lib_diagnosis.py`.

Three things in this file are load-bearing and easy to lose.

The open path is genuinely read-only. The ordinary opener performs schema
work, migration, legacy import, contract repair and replay, any of which
would mutate a store the diagnosis is only reading and would defeat
component-local consistency. `open_read_only` uses the raw `mode=ro` URI
connect, the same class of path `db checkpoint` established with its
`mode=rw` connect that skips schema, migrations and purge.

Generation is a version vector with component-local consistency, not a claim
of an instantaneous cross-file cut: SQLite provides no such thing across
separate files, and a content digest describes bytes read rather than
proving that independently opened snapshots coexisted. Each component is
probed before and after its read; a component whose probe differs is re-read
once, and a second divergence yields `generation_incoherent`.

Codex 5-hour entries and windows are BOTH classified through
`bin/_lib_codex_pools.py`, and an entry joins only to a window of a
compatible logical pool. Existing block assembly at
`bin/_cctally_dashboard_sources.py:2366-2414` assigns by account and time
without a pool restriction, so an unrestricted join would let an overlapping
Spark window and a standard window both claim the same spend. An entry
matching no compatible window is a coverage gap and is never duplicated
across pools.

Spec: docs/superpowers/specs/2026-08-19-620-s2-on-demand-diagnosis.md §3, §5
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pickle
import re
import shlex
import sqlite3
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Any, Collection, Iterable, Mapping, NamedTuple, Sequence

import _cctally_core
import _lib_accounts
import _lib_codex_pools
import _lib_diagnosis as kernel
import _lib_pricing
from _lib_diagnosis import (
    ClassResult, ContributorSpec, Denominator, DiagnosisWindow,
    EstablishmentError, EstablishmentFailure, PopulationCoverage,
    SubjectFacts, WithheldCause,
)
from _lib_fmt import stable_sum
from _lib_pricing import (
    _calculate_entry_cost, _resolve_codex_pricing, claude_usage_dict,
)

UTC = dt.timezone.utc

# Read-only connections still contend with a live writer's WAL, so they get a
# bounded wait rather than an immediate `database is locked`.
READ_BUSY_TIMEOUT_MS = 4000

_STORE_PATHS = {
    "cache": "CACHE_DB_PATH",
    "stats": "DB_PATH",
    "conversations": "CONVERSATIONS_DB_PATH",
}

# The generation components. `conversations` is CONDITIONAL: a plan that
# reads no conversation bytes publishes no component for it at all, and
# `GenerationVector.as_dict` appends it only when it is non-null.
# The three every plan reads, in the order they are established.
_UNCONDITIONAL_COMPONENTS: tuple[str, ...] = ("stats", "cache", "configuration")
# Every component this module can publish. DERIVED from the unconditional
# three, so it cannot drift from them: it was a hand-written tuple that nothing
# in the repository read, which is a constant that can only ever be wrong.
GENERATION_COMPONENTS: tuple[str, ...] = _UNCONDITIONAL_COMPONENTS + (
    "conversations",)


def _cctally():
    return sys.modules["cctally"]


# --- the scope ----------------------------------------------------------

@dataclass(frozen=True)
class DiagnosisScope:
    """Provider, immutable account key and a half-open `[start, end)` window.

    The plan declared `window_start` and `window_end` as ISO strings. They
    are timezone-aware datetimes here, because every consumer in this file
    compares them against parsed row timestamps and a string window would
    mean parsing the same two values at a dozen call sites. `window_start_iso`
    and `window_end_iso` render the wire form.
    """

    source: str
    account_key: str | None
    window_start: dt.datetime
    window_end: dt.datetime
    effective_speed: str | None = None
    display_tz: str = "UTC"
    label: str = ""

    def __post_init__(self) -> None:
        for value in (self.window_start, self.window_end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise EstablishmentFailure(
                    EstablishmentError.RANGE_UNRESOLVED.value,
                    "diagnosis window bounds must be timezone-aware",
                )
        if self.window_end <= self.window_start:
            raise EstablishmentFailure(
                EstablishmentError.RANGE_UNRESOLVED.value,
                "diagnosis window end must be after its start",
            )

    @property
    def window_start_iso(self) -> str:
        return _iso_z(self.window_start)

    @property
    def window_end_iso(self) -> str:
        return _iso_z(self.window_end)

    def preceding(self) -> "DiagnosisScope":
        """The immediately preceding equal-duration half-open window.

        Same provider, account, timezone interpretation and effective speed,
        which is what makes the baseline a comparison rather than a
        coincidence.
        """
        span = self.window_end - self.window_start
        return DiagnosisScope(
            source=self.source,
            account_key=self.account_key,
            window_start=self.window_start - span,
            window_end=self.window_start,
            effective_speed=self.effective_speed,
            display_tz=self.display_tz,
            label="baseline",
        )

    def window(self) -> DiagnosisWindow:
        return DiagnosisWindow(
            start_at=self.window_start_iso,
            end_at=self.window_end_iso,
            tz=self.display_tz,
            label=self.label,
        )


def _iso_z(value: dt.datetime) -> str:
    """The wire form: an instant rendered with a trailing `Z`."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _iso_sql(value: dt.datetime) -> str:
    """The SQL bound form, which is deliberately NOT the wire form.

    The two stores disagree about how they spell an instant:
    `session_entries.timestamp_utc` is written with a `+00:00` offset, while
    `codex_session_entries.timestamp_utc` goes through
    `_lib_jsonl._format_codex_timestamp` and ends in `Z`. These comparisons
    are lexical over an indexed TEXT column, and `+` (0x2B) sorts before `Z`
    (0x5A), so binding the `+00:00` form makes `>= start` admit both
    spellings at the lower bound and `< end` exclude both at the upper one.
    Binding the `Z` form instead silently drops every Claude row that lands
    exactly on the window start.
    """
    return value.astimezone(UTC).isoformat()


def _parse_ts(raw: object) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if raw.endswith("Z"):
            # Codex's canonical store spelling. Avoid allocating a replaced
            # string and normalizing an offset that is already known to be
            # UTC on every accounting row in the hot scan.
            return dt.datetime.fromisoformat(raw[:-1]).replace(tzinfo=UTC)
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    if parsed.tzinfo is UTC:
        return parsed
    return parsed.astimezone(UTC)


# --- the read-only open path -------------------------------------------

def _store_path(kind: str):
    try:
        attribute = _STORE_PATHS[kind]
    except KeyError as exc:
        raise EstablishmentFailure(
            EstablishmentError.STORE_UNAVAILABLE.value, f"unknown store {kind}"
        ) from exc
    return getattr(_cctally_core, attribute)


def open_read_only(kind: str) -> sqlite3.Connection:
    """Open one store read-only, performing no schema work at all.

    `mode=ro` is what makes the claim structural rather than a promise: the
    connection cannot write, so migration, legacy import, contract repair and
    replay cannot run behind our back and change the very bytes whose digest
    we are about to publish.
    """
    path = _store_path(kind)
    if not path.exists():
        raise EstablishmentFailure(
            EstablishmentError.STORE_UNAVAILABLE.value,
            f"{kind} store is not present",
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute(f"PRAGMA busy_timeout={READ_BUSY_TIMEOUT_MS}")
    except sqlite3.Error as exc:
        raise EstablishmentFailure(
            EstablishmentError.STORE_UNAVAILABLE.value,
            f"{kind} store could not be opened read-only: {exc}",
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _execute(conn: sqlite3.Connection, sql: str,
             params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """The single query chokepoint.

    Every read in this module goes through it, so an account-scoping audit
    can observe the whole SQL surface from one place rather than trusting a
    grep over call sites.
    """
    return list(conn.execute(sql, tuple(params)))


# --- generation ---------------------------------------------------------

@dataclass(frozen=True)
class GenerationVector:
    stats: str
    cache: str
    configuration: str
    # Optional because a plan that reads no conversation bytes publishes no
    # conversations component at all. A `None` here is ABSENCE, not the value
    # zero and not the string "None".
    conversations: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"stats": self.stats, "cache": self.cache,
               "configuration": self.configuration}
        if self.conversations is not None:
            out["conversations"] = self.conversations
        return out

    def generation_id(self, scope: DiagnosisScope,
                      plan: "kernel.ExecutionPlan") -> str:
        """SHA-256 over the contract version, the normalized scope, the whole
        plan and the component vector.

        Two surfaces reading the same facts under the same plan produce the
        same id, which is what makes the equivalence check meaningful. The
        plan is part of the identity because a seven-class report with three
        withheld results is a different report from a four-class one and must
        not share an identifier with it. Every plan tokenizes; there is no
        legacy exemption.
        """
        digest = hashlib.sha256()
        digest.update(str(kernel.DIAGNOSIS_CONTRACT_VERSION).encode())
        for part in (
            scope.source, scope.account_key or _lib_accounts.UNATTRIBUTED,
            scope.window_start_iso, scope.window_end_iso,
            scope.effective_speed or "", scope.display_tz,
        ):
            digest.update(b"\x1f")
            digest.update(str(part).encode())
        digest.update(b"\x1d")
        digest.update(plan.plan_token().encode())
        # Iterate `as_dict()`, NOT `GENERATION_COMPONENTS` with `getattr`, or a
        # null component would hash the literal string "None" and an absent
        # component would be indistinguishable from a present one whose digest
        # happened to be that text.
        for component, value in self.as_dict().items():
            digest.update(b"\x1e")
            digest.update(component.encode())
            digest.update(b"=")
            digest.update(value.encode())
        return digest.hexdigest()


def _digest_component_pair(current_rows: Iterable[Sequence[Any]],
                           baseline_rows: Iterable[Sequence[Any]] | None
                           ) -> str:
    """One component's published digest, binding BOTH window subdigests.

    The preceding-window bundle is established separately while only the
    current bundle's vector is published, so a baseline value could change
    while `generationId` stayed constant — a pre-existing S2 defect that three
    more baselines would compound. Each component digest therefore covers its
    current-window and its baseline-window row streams, and the two are
    domain-separated so a row moving between them is visible.
    """
    return _digest_component_pair_from_digests(
        _digest_rows(current_rows), _digest_rows(baseline_rows or ()))


def _digest_component_pair_from_digests(
        current_digest: str, baseline_digest: str | None) -> str:
    """Bind two already-established component digests without rereading.

    `_establish` computes each digest while its component is protected by the
    before/after probe pair. Re-serializing the full accounting population at
    publication time added a second 150K-row hash pass without observing any
    new fact. Domain-separating those same subdigests is byte-identical to the
    original row-stream helper above.
    """
    digest = hashlib.sha256()
    digest.update(b"current=")
    digest.update(current_digest.encode())
    digest.update(b"\x1dbaseline=")
    digest.update((baseline_digest or _digest_rows(())).encode())
    return digest.hexdigest()


def _probe_component(component: str, bundle: "StoreBundle") -> str:
    """A cheap fingerprint of one component's current state.

    `PRAGMA data_version` advances when another connection commits to the
    database, which is exactly the event that would make a digest describe
    two different states. The file size is folded in so a change the pragma
    cannot see on a fresh connection is still visible.
    """
    if component == "configuration":
        path = _cctally_core.CONFIG_PATH
        try:
            stat = path.stat()
        except OSError:
            return "absent"
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    if component == "conversations":
        # This component's read runs the three conversation-derived
        # evaluators, and prompt-cache churn reads `session_entries` off the
        # CACHE connection to seed its walk — after the cache component's own
        # probe pair has closed. A writer committing between the two moves
        # `flaggedTurnCount`, which this digest binds, so a probe covering
        # only `conversations.db` cannot fire `generation_incoherent` for a
        # mutation that genuinely moved a published figure. The cost is that a
        # cache commit during this read now costs a re-read, and a second one
        # `generation_incoherent`; a component exempting itself from the
        # coherence contract is worse than an occasional retry.
        #
        # The fold applies only where the read actually happens. On the
        # schema-gap branch the component runs no evaluator, reads no cache
        # bytes and digests one constant naming one of our own tables, so a
        # `cache.db` commit cannot move its digest — and folding the cache
        # probe there would let a hook tick force a re-read and a second one a
        # 503 for a component nothing about `cache.db` can change.
        #
        # The gate itself can raise, and this probe runs OUTSIDE the component
        # read, so it cannot be moved inside that read's backstop. It gets the
        # equivalent protection instead: a `sqlite3` error here decides no
        # fold, and the read below asks the same question inside its own
        # backstop and classifies the failure there. A probe that raised would
        # end the whole report — all seven classes — before the read ever
        # reached that classification, which is the failure this catch exists
        # to prevent.
        #
        # `sqlite3.Error` and not `Exception`: an unrecognised source raises
        # `EstablishmentFailure`, which must still end the report rather than
        # be absorbed into a probe string.
        #
        # The fallback probes the WIDER pair, which is the shape a readable
        # store also probes. A lock that clears between the two probes would
        # otherwise read as a divergence and cost a re-read for a store that
        # is fine.
        #
        # The cost, stated so the next reader does not have to derive it: the
        # wide pair binds `cache.db` even though the read that follows may
        # fail before touching a cache byte, so a hook tick committing between
        # the probes reads as a divergence for a component that read nothing
        # there, and two of them raise `generation_incoherent`. The real-store
        # pass recorded exactly that against a live store with a running
        # dashboard. The wide pair is still right, because the probe cannot
        # know in advance whether the read will fail, and the narrow
        # alternative pays a shape flip and a retry in the common case.
        try:
            gap = bundle.conversations_schema_gap()
        except sqlite3.Error:
            return (_store_probe("conversations", bundle) + "|"
                    + _store_probe("cache", bundle))
        if gap is not None:
            return _store_probe("conversations", bundle)
        return (_store_probe("conversations", bundle) + "|"
                + _store_probe("cache", bundle))
    return _store_probe(component, bundle)


def _store_probe(component: str, bundle: "StoreBundle") -> str:
    conn = bundle.connection(component)
    try:
        version = _execute(conn, "PRAGMA data_version")[0][0]
    except (sqlite3.Error, IndexError):
        version = "?"
    try:
        size = _store_path(component).stat().st_size
    except OSError:
        size = -1
    return f"{version}:{size}"


def _read_component(component: str, scope: DiagnosisScope,
                    bundle: "StoreBundle") -> tuple[list, Any]:
    """Read one component's facts and return the rows that describe them.

    The caller digests the returned stream in deterministic key order, so the
    digest describes the facts the report is built from rather than the file's
    bytes.

    **Every component needs its OWN branch.** This dispatched `configuration`,
    then `cache`, and fell through to `_read_stats_component` for anything
    else, so a `conversations` component added without a branch would be
    silently digested as stats: a plausible digest describing entirely the
    wrong facts, with nothing failing. The fall-through is now an explicit
    refusal instead.
    """
    if component == "configuration":
        return _digest_configuration(scope)
    if component == "cache":
        return _read_cache_component(scope, bundle)
    if component == "conversations":
        return _read_conversations_component(scope, bundle)
    if component == "stats":
        return _read_stats_component(scope, bundle)
    raise EstablishmentFailure(
        EstablishmentError.STORE_UNAVAILABLE.value,
        f"unknown generation component {component}",
    )


def _digest_configuration(scope: DiagnosisScope) -> tuple[list, Any]:
    try:
        raw = _cctally_core.CONFIG_PATH.read_text()
        payload = json.loads(raw)
    except (OSError, ValueError):
        payload = {}
    relevant = {
        "display.tz": scope.display_tz,
        "source": scope.source,
        "config": payload,
    }
    body = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return [("configuration", body)], payload


def _digest_rows(rows: Iterable[Sequence[Any]]) -> str:
    """Digest a component's already-materialized primitive row stream.

    Protocol 5 is deterministic for these tuples of SQLite/JSON primitives
    and frames the values without delimiter collisions. Feeding the pickler
    directly into SHA avoids both the per-cell Python string loop and a second
    serialized copy proportional to the accounting population.
    """
    digest = hashlib.sha256()

    class _DigestWriter:
        def write(self, data: bytes) -> int:
            digest.update(data)
            return len(data)

    payload = rows if isinstance(rows, (list, tuple)) else tuple(rows)
    pickle.Pickler(_DigestWriter(), protocol=5).dump(payload)
    return digest.hexdigest()


# --- raw accounting facts ----------------------------------------------

class AccountingEntry(NamedTuple):
    """One priced accounting row, provider-neutral for the fold."""

    timestamp: dt.datetime
    model: str
    project_key: str
    project_label: str
    session_key: str
    session_label: str
    root_key: str
    pool: str | None
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_create_tokens: int = 0
    cache_read_tokens: int = 0
    is_fallback_pricing: bool = False
    # Session identity and project identity are separate facts and are asked
    # separately. One flag for both made the project class withhold as
    # `unattributed_evidence` whenever `session_files` was still inside its
    # documented lazy-backfill window, even though every project path had
    # resolved.
    project_identity_resolved: bool = True
    session_identity_resolved: bool = True
    # Whether this entry's model has a pricing row of its own. A Claude model
    # the embedded table does not know contributes zero cost, which is what
    # `pricing_unavailable` is about; a Codex model priced through the legacy
    # fallback IS priced, and is a `is_fallback_pricing` qualification only.
    pricing_resolved: bool = True
    # --- the S3 join keys (#620 S3) ---------------------------------------
    #
    # Populated only under the S3-expanded projection. Three of them default to
    # `None` there, which is absence; `source_path` defaults to the EMPTY
    # STRING, because it is a path and a bucket derived from one, and no
    # caller distinguishes "no path" from "a path we did not read". They are
    # physical join keys and never reach a row field, an evidence value or
    # the wire; the Claude pair joins the canonical turn, the Codex offset is
    # what the event inference attributes a turn from, and `source_path` is
    # what the Claude subagent bucket is derived from inside the reader.
    source_path: str = ""
    msg_id: str | None = None
    req_id: str | None = None
    line_offset: int | None = None


@dataclass(frozen=True)
class NativeBlock:
    """One provider-native 5-hour window.

    `key` carries the root and the logical pool, not just the start instant.
    Two windows on one root can begin in the same minute and belong to
    different pools — that is exactly the Spark-beside-standard case the
    pool-compatible join exists for — so a start-only key would collapse
    them into one subject and undo the separation.
    """

    key: str
    label: str
    start_at: dt.datetime
    end_at: dt.datetime
    root_key: str
    pool: str | None
    next_step: str = ""


def _native_block_key(root_key: str, pool: str | None,
                      start_at: dt.datetime) -> str:
    """The subject key both block builders publish, in one place.

    A five-hour subject key is a COMPOSITE — root, logical pool and start
    instant — and every test that hand-wrote a bare instant instead was
    checking a shape production never produces. Deriving it from here is what
    keeps such a test honest.
    """
    return f"{root_key}|{pool or 'standard'}|{_iso_z(start_at)}"


@dataclass(frozen=True)
class _PopulationIndex:
    total_usd: float
    observed_range: tuple[dt.datetime | None, dt.datetime | None]
    coverage_counts: Mapping[str, int]
    grouped_entries: Mapping[
        str, tuple[dict[str, list[AccountingEntry]], dict[str, str]]]
    population_digest: str


@dataclass(frozen=True)
class RawFacts:
    entries: tuple[AccountingEntry, ...] = ()
    blocks: tuple[NativeBlock, ...] = ()
    retained_start: dt.datetime | None = None
    retained_end: dt.datetime | None = None
    # The earliest instant this install was observing this provider at all,
    # independent of the accounting rows. It is what separates a store pruned
    # past the window start from an install that simply did not exist then.
    store_horizon: dt.datetime | None = None
    unavailable_cause: str | None = None
    population_digest_override: str | None = field(
        default=None, repr=False, compare=False)

    @cached_property
    def _index(self) -> _PopulationIndex:
        """Derive every provider-wide fold in one population traversal."""
        grouped = {kind: defaultdict(list)
                   for kind in ("model", "project", "session")}
        labels = {kind: {} for kind in grouped}
        model_groups = grouped["model"]
        project_groups = grouped["project"]
        session_groups = grouped["session"]
        model_labels = labels["model"]
        project_labels = labels["project"]
        session_labels = labels["session"]
        costs: list[float] = []
        digest = (None if self.population_digest_override is not None
                  else hashlib.sha256())
        low: dt.datetime | None = None
        high: dt.datetime | None = None
        model_count = project_count = session_count = priced_count = 0
        for entry in self.entries:
            costs.append(entry.cost_usd)
            if low is None or entry.timestamp < low:
                low = entry.timestamp
            if high is None or entry.timestamp > high:
                high = entry.timestamp
            model_count += bool(entry.model)
            project_count += bool(entry.project_identity_resolved)
            session_count += bool(entry.session_identity_resolved)
            priced_count += not entry.is_fallback_pricing
            if digest is not None:
                digest.update((f"{_iso_z(entry.timestamp)}|{entry.model}|"
                               f"{entry.session_key}|"
                               f"{entry.cost_usd!r}").encode())
            model_key = entry.model or "(unknown)"
            if model_key not in model_groups:
                model_labels[model_key] = model_key
            model_groups[model_key].append(entry)
            if entry.project_key not in project_groups:
                project_labels[entry.project_key] = entry.project_label
            project_groups[entry.project_key].append(entry)
            if entry.session_key not in session_groups:
                session_labels[entry.session_key] = entry.session_label
            session_groups[entry.session_key].append(entry)
        return _PopulationIndex(
            total_usd=stable_sum(costs),
            observed_range=(low, high),
            coverage_counts=MappingProxyType({
                "model": model_count,
                "project": project_count,
                "session": session_count,
                "block": len(costs),
                "priced": priced_count,
            }),
            grouped_entries=MappingProxyType({
                kind: (grouped[kind], labels[kind]) for kind in grouped
            }),
            population_digest=(self.population_digest_override
                               if self.population_digest_override is not None
                               else digest.hexdigest()[:32]),
        )

    @property
    def total_usd(self) -> float:
        return self._index.total_usd

    @property
    def observed_range(self) -> tuple[dt.datetime | None, dt.datetime | None]:
        return self._index.observed_range

    @property
    def coverage_counts(self) -> Mapping[str, int]:
        return self._index.coverage_counts

    @property
    def grouped_entries(self) -> Mapping[
            str, tuple[dict[str, list[AccountingEntry]], dict[str, str]]]:
        return self._index.grouped_entries

    @property
    def population_digest(self) -> str:
        return self._index.population_digest


# The "not yet asked" sentinel for the memoized schema-gap answer, because
# `None` is a real answer there and would make every probe re-ask.
_UNPROBED = object()


class StoreBundle:
    """The open read-only connections plus the facts read through them."""

    def __init__(self, scope: DiagnosisScope,
                 plan: "kernel.ExecutionPlan") -> None:
        self.scope = scope
        self._connections: dict[str, sqlite3.Connection] = {}
        self.facts = RawFacts()
        self.baseline: RawFacts | None = None
        self.vector: GenerationVector | None = None
        # The POLICY plan this bundle reads under. A class denied in stage 1
        # is settled, so a denied plan never opens, probes or digests
        # `conversations.db`.
        #
        # REQUIRED, for the reason R9 made it required on `build_diagnosis`
        # and `build_provider_diagnosis`: a default of "everything is visible"
        # is the permissive answer, and a caller that forgot to thread the
        # route's transcript gate would silently read a store the request was
        # not authorized to read. Every production caller passes one; only
        # tests and the benchmark ever reached the default.
        self.plan = plan
        # Whether an AUTHORIZED conversations open succeeded. `False` when the
        # plan asked for the store and it was absent or unopenable; also
        # `False`, and never consulted, when the plan asked for nothing.
        self.conversations_available = False
        # The digest row streams, per component, kept so the published digest
        # can bind the baseline window's streams alongside these.
        self.component_rows: dict[str, list] = {}
        # The three conversation-derived evaluations, computed ONCE. Populated
        # during establishment when the plan opened the conversations store,
        # so the digest and the rows describe one evaluation rather than two.
        self.s3_evaluations: dict | None = None
        self.claude_window_sessions: tuple[str, ...] | None = None
        # Each S3 class's aggregate qualifying-cost share over the immediately
        # preceding equal-duration window. Absent means the baseline could not
        # be established, which is `baseline_insufficient`.
        self.s3_baseline_shares: dict[str, float] = {}
        # The Codex thread rows the fan-out predicate resolves over, read and
        # digested by the `cache` component that owns them.
        self.codex_threads: tuple = ()
        # Memoized schema-gap answer. `_UNPROBED` rather than `None`, because
        # `None` is the answer "this store can serve every statement".
        self._conversations_gap: Any = _UNPROBED

    def connection(self, kind: str) -> sqlite3.Connection:
        conn = self._connections.get(kind)
        if conn is None:
            conn = open_read_only(kind)
            self._connections[kind] = conn
        return conn

    def conversations_schema_gap(self) -> str | None:
        """Which of our own tables this store cannot serve, memoized.

        Memoized because the probe pair and the read all ask, and because the
        answer cannot change under them: a schema change needs a WRITABLE
        reopen and this connection is `mode=ro` for the bundle's whole life.
        """
        if self._conversations_gap is _UNPROBED:
            self._conversations_gap = _conversations_schema_gap(
                self.connection("conversations"), self.scope.source)
        return self._conversations_gap

    def close(self) -> None:
        for conn in self._connections.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._connections.clear()

    def __enter__(self) -> "StoreBundle":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --- Claude accounting --------------------------------------------------

_CLAUDE_ENTRIES_SQL = """
    SELECT se.timestamp_utc, se.model,
           se.input_tokens, se.output_tokens,
           se.cache_create_tokens, se.cache_read_tokens,
           se.cache_create_1h_tokens, se.cost_usd_raw, se.speed,
           se.source_path, sf.session_id, sf.project_path,
           se.account_key
      FROM session_entries se
      LEFT JOIN session_files sf ON sf.path = se.source_path
     WHERE se.timestamp_utc >= ? AND se.timestamp_utc < ?
"""

# The S3-expanded Claude projection. A SECOND FIXED STRING, never the frozen
# one with columns concatenated onto it: a projection built by concatenation
# cannot be byte-frozen, because every edit to the S3 half rewrites the S2
# half too. It adds the canonical turn key `(msg_id, req_id)`, which is what
# lets `maxContextWindowFraction` be derived from the in-window accounting
# population the adapter has already loaded rather than by walking retained
# conversation history.
_CLAUDE_ENTRIES_S3_SQL = """
    SELECT se.timestamp_utc, se.model,
           se.input_tokens, se.output_tokens,
           se.cache_create_tokens, se.cache_read_tokens,
           se.cache_create_1h_tokens, se.cost_usd_raw, se.speed,
           se.source_path, sf.session_id, sf.project_path,
           se.account_key, se.msg_id, se.req_id
      FROM session_entries se
      LEFT JOIN session_files sf ON sf.path = se.source_path
     WHERE se.timestamp_utc >= ? AND se.timestamp_utc < ?
"""

_CLAUDE_RETENTION_SQL = """
    SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM session_entries
     WHERE 1=1
"""


def _cache_projection_sql(plan: "kernel.ExecutionPlan") -> str:
    """Which of the four fixed projections this plan runs.

    Which projection runs is a property of the PLAN, so two reports over the
    same window under different plans read different facts and publish
    different `cache` digests. The plan consulted here is the POLICY plan,
    decided before any store opens: the established plan can withhold a class
    after the read, and choosing the projection from it would publish a digest
    describing a read the plan no longer claims to have made.
    """
    if plan.source == "codex":
        return (_CODEX_ENTRIES_S3_SQL if plan.requires_s3_projection()
                else _CODEX_ENTRIES_SQL)
    return (_CLAUDE_ENTRIES_S3_SQL if plan.requires_s3_projection()
            else _CLAUDE_ENTRIES_SQL)


def _account_predicate(column: str, account_key: str | None,
                       params: list[Any]) -> str:
    """Scope a read to one account.

    `None` keeps the account-blind merged read. The reserved `unattributed`
    sentinel matches BOTH the literal stamp and NULL, which is the cache
    read-path rule (`NULL` is `unattributed`).
    """
    if account_key is None:
        return ""
    if account_key == _lib_accounts.UNATTRIBUTED:
        params.append(_lib_accounts.UNATTRIBUTED)
        return f" AND ({column} IS NULL OR {column} = ?)"
    params.append(account_key)
    return f" AND {column} = ?"


def _read_claude_entries(scope: DiagnosisScope, conn: sqlite3.Connection,
                         plan: "kernel.ExecutionPlan"
                         ) -> tuple[list[AccountingEntry], list[tuple]]:
    params: list[Any] = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    expanded = plan.requires_s3_projection()
    sql = _cache_projection_sql(plan) + _account_predicate(
        "se.account_key", scope.account_key, params
    ) + " ORDER BY se.timestamp_utc ASC, se.id ASC"
    rows = _execute(conn, sql, params)

    c = _cctally()
    resolver_cache: dict[str, Any] = {}
    entries: list[AccountingEntry] = []
    digest_rows: list[tuple] = []
    for row in rows:
        timestamp = _parse_ts(row["timestamp_utc"])
        if timestamp is None:
            continue
        usage = claude_usage_dict(   # #195 chokepoint
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_creation_tokens=int(row["cache_create_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_1h_tokens=row["cache_create_1h_tokens"],
            speed=row["speed"],
        )
        model = str(row["model"] or "")
        # `mode="calculate"`, and no `cost_usd`: the diagnosis REPRICES every
        # entry from `CLAUDE_MODEL_PRICING` (spec §3). `mode="auto"` beside a
        # non-null stored cost returns that column verbatim and never consults
        # the table, which would publish figures a pricing edit cannot correct
        # and — worse — would report `isFallbackPricing` and `pricingResolved`
        # computed from the embedded table beside dollars that did not come
        # from it. A cost written before the #195 cache-write-TTL fix silently
        # under-prices, and the diagnosis would inherit that silently too.
        # `cost_usd_raw` is still read, for the population digest: the digest
        # covers the facts read, not the facts used.
        cost = _calculate_entry_cost(model, usage, mode="calculate")
        project_path = row["project_path"]
        project = c._resolve_project_key(project_path, "git-root", resolver_cache)
        session_id = row["session_id"]
        source_path = str(row["source_path"] or "")
        entries.append(AccountingEntry(
            timestamp=timestamp,
            model=model,
            project_key=project.bucket_path,
            project_label=project.display_key,
            session_key=str(session_id or source_path or "(unknown)"),
            session_label=str(session_id or "(unknown)"),
            root_key="claude",
            pool=None,
            cost_usd=cost,
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_create_tokens=int(row["cache_create_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            is_fallback_pricing=_claude_pricing_is_fallback(model),
            project_identity_resolved=not project.is_unknown,
            session_identity_resolved=bool(session_id),
            pricing_resolved=not _claude_pricing_is_fallback(model),
            source_path=source_path,
            msg_id=(row["msg_id"] if expanded else None),
            req_id=(row["req_id"] if expanded else None),
        ))
        digest_row = (
            row["timestamp_utc"], model, row["input_tokens"],
            row["output_tokens"], row["cache_create_tokens"],
            row["cache_read_tokens"], row["cache_create_1h_tokens"],
            row["cost_usd_raw"], row["speed"], source_path,
            session_id, project_path, row["account_key"],
        )
        if expanded:
            # The digest covers the facts READ, so the expanded projection's
            # extra columns belong in it. Under the frozen projection the row
            # is byte-identical to S2's.
            digest_row = digest_row + (row["msg_id"], row["req_id"])
        digest_rows.append(digest_row)
    return entries, digest_rows


def _claude_pricing_is_fallback(model: str) -> bool:
    """Whether this model priced through a fallback rather than its own row.

    `_calculate_entry_cost` returns only a float, so the qualification is
    otherwise lost between pricing resolution and the row that reports it.
    """
    c = _cctally()
    pricing = getattr(c, "CLAUDE_MODEL_PRICING", None)
    if not isinstance(pricing, Mapping):
        return False
    return model not in pricing


_CLAUDE_BLOCKS_SQL = """
    SELECT five_hour_window_key, block_start_at, five_hour_resets_at,
           account_key
      FROM five_hour_blocks
     WHERE five_hour_resets_at > ? AND block_start_at < ?
"""

# The install's own observation horizon for this provider, deliberately NOT
# bounded by the requested window: it is the evidence that separates a pruned
# store from a young one.
_CLAUDE_HORIZON_SQL = """
    SELECT MIN(block_start_at) FROM five_hour_blocks
     WHERE 1=1
"""

_CODEX_HORIZON_SQL = """
    SELECT MIN(nominal_start_at_utc) FROM quota_window_blocks
     WHERE source='codex' AND window_minutes=300
"""


def _read_claude_blocks(scope: DiagnosisScope,
                        conn: sqlite3.Connection) -> tuple[list[NativeBlock], list[tuple]]:
    params: list[Any] = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    sql = _CLAUDE_BLOCKS_SQL + _account_predicate(
        "account_key", scope.account_key, params
    ) + " ORDER BY block_start_at ASC, five_hour_window_key ASC"
    try:
        rows = _execute(conn, sql, params)
    except sqlite3.Error:
        return [], []
    blocks: list[NativeBlock] = []
    digest_rows: list[tuple] = []
    for row in rows:
        start_at = _parse_ts(row["block_start_at"])
        end_at = _parse_ts(row["five_hour_resets_at"])
        if start_at is None or end_at is None or end_at <= start_at:
            continue
        blocks.append(NativeBlock(
            key=_native_block_key("claude", None, start_at),
            label=_iso_z(start_at),
            start_at=start_at,
            end_at=end_at,
            root_key="claude",
            pool=None,
            next_step=(
                f"cctally five-hour-breakdown --block-start {_iso_z(start_at)}"
            ),
        ))
        digest_rows.append((
            row["five_hour_window_key"], row["block_start_at"],
            row["five_hour_resets_at"], row["account_key"],
        ))
    return blocks, digest_rows


# --- Codex accounting ---------------------------------------------------

_CODEX_ENTRIES_SQL = """
    SELECT entries.timestamp_utc, entries.source_path,
           entries.source_root_key, entries.conversation_key, entries.model,
           entries.account_key, entries.input_tokens,
           entries.cached_input_tokens, entries.output_tokens,
           entries.reasoning_output_tokens
      FROM codex_session_entries AS entries
     WHERE entries.timestamp_utc >= ? AND entries.timestamp_utc < ?
"""

# The S3-expanded Codex projection, the second fixed string on this side. It
# adds `line_offset`, the physical key an accounting row is attributed by:
# `codex_session_entries` retains no `turn_id`, so the owning turn is
# recovered by mapping accounting offsets through the event inference, and
# without the offset the loaded rows recover neither the turn nor its
# per-turn capacity. `source_root_key` and `conversation_key` — the fan-out
# join keys — are already in the frozen projection.
_CODEX_ENTRIES_S3_SQL = """
    SELECT entries.timestamp_utc, entries.source_path,
           entries.source_root_key, entries.conversation_key, entries.model,
           entries.account_key, entries.input_tokens,
           entries.cached_input_tokens, entries.output_tokens,
           entries.reasoning_output_tokens, entries.line_offset
      FROM codex_session_entries AS entries
     WHERE entries.timestamp_utc >= ? AND entries.timestamp_utc < ?
"""

_CODEX_RETENTION_SQL = """
    SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM codex_session_entries
     WHERE 1=1
"""


def _compiled_codex_pricer(model: str, speed: str):
    """Resolve one model's canonical pricing once for the hot range scan.

    The returned function preserves `_calculate_codex_entry_cost`'s exact
    arithmetic and warning contract. It only removes repeated model aliases,
    dictionary lookups, rate extraction and fast-tier resolution from every
    token event in the same diagnosis window.
    """
    pricing, is_fallback = _resolve_codex_pricing(model)
    if pricing is None:
        _lib_pricing._warn_unknown_codex_model(model)
        return (lambda *_tokens: 0.0), is_fallback
    if is_fallback:
        _lib_pricing._warn_unknown_codex_model(model)

    threshold = _lib_pricing.CODEX_TIERED_THRESHOLD
    input_rate = pricing.get("input_cost_per_token", 0.0)
    input_tier = pricing.get("input_cost_per_token_above_272k_tokens")
    cache_rate = pricing.get("cache_read_input_token_cost", 0.0)
    cache_tier = pricing.get(
        "cache_read_input_token_cost_above_272k_tokens")
    output_rate = pricing.get("output_cost_per_token", 0.0)
    output_tier = pricing.get("output_cost_per_token_above_272k_tokens")
    multiplier = (_lib_pricing._codex_fast_multiplier(model)
                  if speed == "fast" else 1.0)

    def _price(input_tokens: int, cached_input_tokens: int,
               output_tokens: int,
               reasoning_output_tokens: int) -> float:
        del reasoning_output_tokens
        non_cached_input = max(0, input_tokens - cached_input_tokens)
        if non_cached_input <= 0 or not input_rate:
            input_cost = 0.0
        elif non_cached_input > threshold and input_tier is not None:
            input_cost = (threshold * input_rate
                          + (non_cached_input - threshold) * input_tier)
        else:
            input_cost = non_cached_input * input_rate
        if cached_input_tokens <= 0 or not cache_rate:
            cached_input_cost = 0.0
        elif cached_input_tokens > threshold and cache_tier is not None:
            cached_input_cost = (
                threshold * cache_rate
                + (cached_input_tokens - threshold) * cache_tier)
        else:
            cached_input_cost = cached_input_tokens * cache_rate
        if output_tokens <= 0 or not output_rate:
            output_cost = 0.0
        elif output_tokens > threshold and output_tier is not None:
            output_cost = (threshold * output_rate
                           + (output_tokens - threshold) * output_tier)
        else:
            output_cost = output_tokens * output_rate
        base = input_cost + cached_input_cost + output_cost
        if speed == "fast":
            base *= multiplier
        return base

    return _price, is_fallback


def _read_codex_entries(scope: DiagnosisScope, conn: sqlite3.Connection,
                        plan: "kernel.ExecutionPlan"
                        ) -> tuple[
                            list[AccountingEntry], list[tuple], list]:
    params: list[Any] = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    expanded = plan.requires_s3_projection()
    # Every Codex query carries `account_key` (#341 / #373). A Codex read that
    # forgets it merges two accounts' spend into one denominator.
    sql = _cache_projection_sql(plan) + _account_predicate(
        "entries.account_key", scope.account_key, params
    ) + (" ORDER BY entries.timestamp_utc ASC, entries.source_root_key ASC,"
         " entries.conversation_key ASC, entries.id ASC")
    rows = _execute(conn, sql, params)

    # Thread metadata is conversation-shaped, not entry-shaped. Joining it
    # onto the hot accounting range repeats the same cwd/git/topology strings
    # once per token event (150,000 times in the acceptance corpus) before
    # Python immediately deduplicates them again. Read the scoped thread set
    # once under this component's same probe pair and reuse it for project
    # attribution, the per-entry digest, and the fan-out evaluator.
    scoped = sorted({str(row["conversation_key"]) for row in rows
                     if row["conversation_key"]})
    thread_rows = _read_codex_threads(conn, scoped)
    threads = {str(row["conversation_key"]): row for row in thread_rows}

    c = _cctally()
    speed = scope.effective_speed or "standard"
    pricers: dict[str, tuple[Any, bool]] = {}
    resolver_cache: dict[str, Any] = {}
    projects: dict[str, tuple[str, str, bool]] = {}
    for conversation_key, thread in threads.items():
        cwd = thread["cwd"]
        if isinstance(cwd, str) and cwd:
            project = c._resolve_project_key(cwd, "git-root", resolver_cache)
            projects[conversation_key] = (
                project.bucket_path, project.display_key,
                not project.is_unknown,
            )
        else:
            projects[conversation_key] = ("(unassigned)", "(unassigned)",
                                          False)
    pools: dict[str, str | None] = {}
    entries: list[AccountingEntry] = []
    digest_rows: list[tuple] = []
    for row in rows:
        timestamp = _parse_ts(row["timestamp_utc"])
        if timestamp is None:
            continue
        model = row["model"] or ""
        input_tokens = int(row["input_tokens"] or 0)
        cached_input_tokens = int(row["cached_input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        reasoning_output_tokens = int(row["reasoning_output_tokens"] or 0)
        compiled = pricers.get(model)
        if compiled is None:
            compiled = _compiled_codex_pricer(model, speed)
            pricers[model] = compiled
        pricer, is_fallback = compiled
        cost = pricer(
            input_tokens, cached_input_tokens, output_tokens,
            reasoning_output_tokens,
        )
        conversation_key = row["conversation_key"] or ""
        source_path = row["source_path"] or ""
        root_key = row["source_root_key"] or ""
        thread = threads.get(conversation_key)
        cwd = thread["cwd"] if thread is not None else None
        # Project identity is conversation metadata, so resolve it once per
        # thread rather than once per token event. Absent metadata still
        # reduces identityCoverage and never basename-merges git roots.
        project_key, project_label, project_resolved = projects.get(
            conversation_key, ("(unassigned)", "(unassigned)", False))
        if model not in pools:
            pools[model] = _lib_codex_pools.codex_model_scoped_quota_pool(model)
        entries.append(AccountingEntry(
            timestamp=timestamp,
            model=model,
            project_key=project_key,
            project_label=project_label,
            session_key=conversation_key or source_path or "(unknown)",
            session_label=conversation_key or "(unknown)",
            root_key=root_key,
            pool=pools[model],
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_input_tokens,
            is_fallback_pricing=bool(is_fallback),
            project_identity_resolved=project_resolved,
            session_identity_resolved=bool(conversation_key),
            # The Codex legacy fallback prices the entry, so the model is
            # qualified rather than unpriceable.
            pricing_resolved=True,
            source_path=source_path,
            line_offset=(row["line_offset"] if expanded else None),
        ))
        digest_row = (
            row["timestamp_utc"], row["source_root_key"],
            row["conversation_key"], model, row["account_key"],
            row["input_tokens"], row["cached_input_tokens"],
            row["output_tokens"], row["reasoning_output_tokens"],
        )
        if expanded:
            digest_row = digest_row + (row["source_path"], row["line_offset"])
        digest_rows.append(digest_row)
    return entries, digest_rows, thread_rows


_CODEX_BLOCKS_SQL = """
    SELECT source_root_key, logical_limit_key, observed_slot, limit_name,
           resets_at_utc, nominal_start_at_utc, orphaned_at, account_key
      FROM quota_window_blocks
     WHERE source='codex' AND window_minutes=300
       AND resets_at_utc > ? AND nominal_start_at_utc < ?
"""


def _read_codex_blocks(scope: DiagnosisScope,
                       conn: sqlite3.Connection) -> tuple[list[NativeBlock], list[tuple]]:
    # The Codex quota projection gate, before any fallback-catching SQL. This
    # read is a CONSUMER of the published projection, so reading it while the
    # projection is incomplete would report a partial set of native blocks as
    # if it were the whole set — and the `except sqlite3.Error` below would
    # render a refusal as an empty block list. The gate fails closed, and a
    # store that cannot answer coherently is a report-establishment failure.
    quota = _cctally()._load_sibling("_cctally_quota")
    try:
        quota.assert_projection_readable(conn)
    except quota.QuotaProjectionIncomplete as exc:
        raise EstablishmentFailure(
            EstablishmentError.STORE_UNAVAILABLE.value,
            f"the Codex quota projection is incomplete: {exc}",
        ) from exc

    params: list[Any] = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    sql = _CODEX_BLOCKS_SQL + _account_predicate(
        "account_key", scope.account_key, params
    ) + (" ORDER BY nominal_start_at_utc ASC, source_root_key ASC,"
         " logical_limit_key ASC, observed_slot ASC")
    try:
        rows = _execute(conn, sql, params)
    except sqlite3.Error:
        return [], []
    blocks: list[NativeBlock] = []
    digest_rows: list[tuple] = []
    for row in rows:
        if row["orphaned_at"] is not None:
            continue
        start_at = _parse_ts(row["nominal_start_at_utc"])
        end_at = _parse_ts(row["resets_at_utc"])
        if start_at is None or end_at is None or end_at <= start_at:
            continue
        pool = _window_pool(row["logical_limit_key"], row["limit_name"])
        root = str(row["source_root_key"] or "")
        blocks.append(NativeBlock(
            key=_native_block_key(root, pool, start_at),
            label=(_iso_z(start_at) if pool is None
                   else f"{_iso_z(start_at)} ({pool})"),
            start_at=start_at,
            end_at=end_at,
            root_key=root,
            pool=pool,
            next_step="cctally codex quota blocks",
        ))
        digest_rows.append((
            row["source_root_key"], row["logical_limit_key"],
            row["observed_slot"], row["limit_name"], row["resets_at_utc"],
            row["nominal_start_at_utc"], row["account_key"],
        ))
    return blocks, digest_rows


def _window_pool(logical_limit_key: object, limit_name: object) -> str | None:
    """Classify a quota window's logical pool.

    Pool classification has exactly one home. The two independent axes are
    `modelPool` in the interpreted key and a Spark `limit_name`; `limit_id`
    is deliberately not one.
    """
    pool = _lib_codex_pools.codex_key_model_pool(logical_limit_key)
    if pool is not None:
        return pool
    return _lib_codex_pools.codex_model_scoped_quota_pool(limit_name)


# --- component reads ----------------------------------------------------

def _read_cache_component(scope: DiagnosisScope,
                          bundle: StoreBundle) -> tuple[list, Any]:
    conn = bundle.connection("cache")
    thread_rows: list = []
    try:
        if scope.source == "codex":
            entries, digest_rows, thread_rows = (
                _read_codex_entries(scope, conn, bundle.plan)
            )
            retention_sql = _CODEX_RETENTION_SQL
            # Thread metadata drives project attribution on every plan and
            # fan-out on the expanded plan. Digest each conversation-shaped
            # fact once here instead of repeating cwd/git strings for every
            # token event in that conversation.
            digest_rows = list(digest_rows) + _codex_thread_digest_rows(
                thread_rows)
        else:
            entries, digest_rows = _read_claude_entries(scope, conn, bundle.plan)
            retention_sql = _CLAUDE_RETENTION_SQL
    except sqlite3.Error as exc:
        # One provider's accounting tables can be absent or unreadable while
        # the other provider's are fine — an older cache.db carries no Codex
        # tables at all. That withholds this provider as `provider_unavailable`
        # rather than ending a two-provider report.
        # The digest describes the facts read, and none were. It names the
        # failure by exception TYPE rather than by message, because a SQLite
        # message is not stable across versions and this digest is published.
        return [("provider_unavailable", type(exc).__name__)], {
            "entries": (),
            "retained_start": None,
            "retained_end": None,
            "unavailable_cause": WithheldCause.PROVIDER_UNAVAILABLE.value,
            "codex_threads": (),
        }
    retention_params: list[Any] = []
    retention_sql += _account_predicate(
        "account_key", scope.account_key, retention_params
    )
    try:
        low, high = _execute(conn, retention_sql, retention_params)[0]
    except (sqlite3.Error, IndexError):
        low = high = None
    return list(digest_rows), {
        "entries": tuple(entries),
        "retained_start": _parse_ts(low),
        "retained_end": _parse_ts(high),
        "codex_threads": tuple(thread_rows),
    }


def _read_codex_threads(conn: sqlite3.Connection,
                        scoped: Sequence[str]) -> list:
    """The thread rows the Codex fan-out predicate resolves over.

    Seeded from the scoped in-window accounting keys, never by walking the
    retained thread graph, and widened once to the candidate PARENTS the
    scoped children point at — because a parent with no in-window spend is
    still the thing a child has to resolve against.

    DEDUPED by `conversation_key`, the table's primary key. The two reads
    overlap whenever a scoped conversation is also somebody's parent, and a
    row handed in twice presents itself as two matches under the complete
    identity, which is exactly the condition that makes a child unallocated.
    """
    if not scoped:
        return []
    rows: dict[str, Any] = {}
    for row in _codex_threads_for(conn, _CODEX_THREADS_BY_KEY_SQL, scoped):
        rows.setdefault(str(row["conversation_key"]), row)
    parents = sorted({str(row["parent_thread_id"]) for row in rows.values()
                      if row["parent_thread_id"]})
    if parents:
        for row in _codex_threads_for(conn, _CODEX_THREADS_BY_NATIVE_SQL,
                                      parents):
            rows.setdefault(str(row["conversation_key"]), row)
    return [rows[key] for key in sorted(rows)]


def _codex_thread_digest_rows(thread_rows: Sequence[Any]) -> list[tuple]:
    """The facts read, in deterministic key order.

    `conversation_key`, `native_thread_id` and `parent_thread_id` are opaque
    provider keys and never reach a row field, an evidence value or the wire;
    they are here because this digest describes the facts READ, which is a
    different contract from the conversations component's.
    """
    return [
        (str(row["conversation_key"]), str(row["source_root_key"] or ""),
         str(row["native_thread_id"] or ""),
         str(row["root_thread_id"] or ""),
         str(row["parent_thread_id"] or ""), row["cwd"], row["git_json"],
         row["context_window"])
        for row in thread_rows
    ]


def _read_store_horizon(scope: DiagnosisScope,
                        conn: sqlite3.Connection) -> tuple[dt.datetime | None,
                                                           object]:
    """The earliest provider-native block this install ever recorded.

    Unbounded by the requested window on purpose. `_retention_coverage` uses
    it as the low bound of what the store could ever have answered for.

    The Codex form reads `quota_window_blocks` behind its own `except`, so it
    gates the quota projection itself rather than relying on `_read_codex_blocks`
    having run first. Call order is not a property the enumeration guard can
    check, and both functions execute module-level SQL that the guard sees as
    one `<module>` site.
    """
    if scope.source == "codex":
        quota = _cctally()._load_sibling("_cctally_quota")
        try:
            quota.assert_projection_readable(conn)
        except quota.QuotaProjectionIncomplete as exc:
            raise EstablishmentFailure(
                EstablishmentError.STORE_UNAVAILABLE.value,
                f"the Codex quota projection is incomplete: {exc}",
            ) from exc
    sql = (_CODEX_HORIZON_SQL if scope.source == "codex"
           else _CLAUDE_HORIZON_SQL)
    params: list[Any] = []
    sql += _account_predicate("account_key", scope.account_key, params)
    try:
        raw = _execute(conn, sql, params)[0][0]
    except (sqlite3.Error, IndexError):
        return None, None
    return _parse_ts(raw), raw


def _read_stats_component(scope: DiagnosisScope,
                          bundle: StoreBundle) -> tuple[list, Any]:
    conn = bundle.connection("stats")
    if scope.source == "codex":
        blocks, digest_rows = _read_codex_blocks(scope, conn)
    else:
        blocks, digest_rows = _read_claude_blocks(scope, conn)
    horizon, raw_horizon = _read_store_horizon(scope, conn)
    # The horizon is part of the facts the report rests on, so it belongs in
    # the digest: a mutation that moved it must move this component.
    digest_rows = list(digest_rows) + [("horizon", raw_horizon)]
    return digest_rows, {"blocks": tuple(blocks), "store_horizon": horizon}


# --- Codex fan-out resolution (#620 S3 X4) ------------------------------
#
# New code rather than an extraction: nothing existing resolves a Codex parent
# set-wise over prefetched rows, and the closest thing that does exist is the
# per-child expansion this deliberately does not reuse.

# The ONLY two Codex origin categories this tree can attribute a meaning to.
CODEX_ORIGIN_MAIN: str = "user"
CODEX_ORIGIN_SUBAGENT: str = "subagent"
_CODEX_ORIGIN_CATEGORIES = frozenset({CODEX_ORIGIN_MAIN, CODEX_ORIGIN_SUBAGENT})


def codex_origin_category(value: object) -> str | None:
    """The origin category of a `root_thread_id`, or `None` when ambiguous.

    `_inferred_codex_thread_source` returns an explicit `thread_source`
    VERBATIM, whatever string it is, and no resolver, vocabulary or authority
    in this tree maps an arbitrary string onto an origin — its own docstring
    records `source: "vscode"` co-occurring with `thread_source: "user"`. Only
    the two exact literals are recognised; every other value belongs to
    neither population, lowers `evaluabilityCoverage` and carries the
    identifiable-subset qualification.
    """
    if isinstance(value, str) and value in _CODEX_ORIGIN_CATEGORIES:
        return value
    return None


@dataclass(frozen=True)
class CodexFanout:
    """Resolved parents, and the children that could not be resolved."""

    # conversation_key -> (source_root_key, parent native_thread_id)
    parents: Mapping[str, tuple[str, str]]
    # conversation_keys that failed evaluation and lower evaluability coverage
    unallocated: tuple[str, ...]
    # conversation_keys whose origin category is neither of the two literals
    # this tree can attribute a meaning to. They belong to NEITHER population
    # and the predicate could not be decided for them, so they lower
    # `evaluabilityCoverage` exactly as an unresolvable parent does. Separate
    # from `unallocated`, because "we could not read the category" and "we
    # read it and could not resolve the parent" are different statements.
    ambiguous: tuple[str, ...] = ()


def _thread_field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return row[name]


def resolve_codex_fanout(thread_rows: Sequence[Any],
                         entries: Sequence[str]) -> CodexFanout:
    """Resolve each scoped child conversation to exactly one parent thread.

    **"Resolvable parent" means exactly ONE non-self match under the complete
    identity.** The table's uniqueness is
    `(source_root_key, root_thread_id, native_thread_id)`, not the pair, and
    the existing per-child resolution queries the weaker pair and takes an
    arbitrary `fetchone()` — which this must not copy. Zero or several matches
    make the child unallocated and lower `evaluabilityCoverage`.

    Set-wise over prefetched rows and seeded from the scoped in-window
    accounting keys in `entries`, never by walking the retained thread graph.
    """
    scoped = [key for key in dict.fromkeys(entries) if key]
    by_native: dict[tuple[str, str], list[Any]] = {}
    by_key: dict[str, Any] = {}
    for row in thread_rows:
        root = str(_thread_field(row, "source_root_key") or "")
        native = _thread_field(row, "native_thread_id")
        key = _thread_field(row, "conversation_key")
        if native is not None:
            by_native.setdefault((root, str(native)), []).append(row)
        if key is not None:
            by_key.setdefault(str(key), row)

    parents: dict[str, tuple[str, str]] = {}
    unallocated: list[str] = []
    ambiguous: list[str] = []
    for key in scoped:
        row = by_key.get(key)
        if row is None:
            ambiguous.append(key)
            continue
        category = codex_origin_category(_thread_field(row, "root_thread_id"))
        if category is None:
            # Neither of the two literals this tree can attribute a meaning
            # to, so the predicate could not be decided for this child.
            ambiguous.append(key)
            continue
        if category != CODEX_ORIGIN_SUBAGENT:
            # Decided, and not a fan-out member: a main-thread conversation.
            continue
        root = str(_thread_field(row, "source_root_key") or "")
        parent = _thread_field(row, "parent_thread_id")
        native = _thread_field(row, "native_thread_id")
        if parent is None:
            unallocated.append(key)
            continue
        candidates = [
            candidate for candidate in by_native.get((root, str(parent)), ())
            if str(_thread_field(candidate, "native_thread_id")) != str(native)
        ]
        if len(candidates) != 1:
            unallocated.append(key)
            continue
        parents[key] = (root, str(parent))
    return CodexFanout(parents=parents, unallocated=tuple(unallocated),
                       ambiguous=tuple(ambiguous))


# --- conversations: the semantic projection (#620 S3 §4.4, §4.5) --------

def allocate_scan_budget(keys: Sequence[Any], budget: int) -> dict[Any, int]:
    """Split one scan budget into equal per-subject shares, in key order.

    A single long subject must not consume the whole budget and starve every
    subject after it, which an unallocated global budget allows and which
    would make the report depend on read order. The remainder is distributed
    in the same key order, so the allocation is a function of the key set
    alone.
    """
    ordered = list(keys)
    if not ordered:
        return {}
    share, remainder = divmod(max(0, int(budget)), len(ordered))
    return {key: share + (1 if index < remainder else 0)
            for index, key in enumerate(ordered)}


# --- the turn predicate (#620 S3 §2.1) ----------------------------------
#
# The canonical normalization is REUSED rather than reinterpreted:
# `conversation_sessions.msg_count` groups `conversation_messages` with no
# `is_sidechain` predicate while subagent files carry the parent's
# `session_id`, so it is a sidechain-inclusive physical count and cannot
# answer "how many human turns does this conversation have".


def _conversation_query():
    return _cctally()._load_sibling("_lib_conversation_query")


def _is_compaction_row(text: object, blocks_json: object) -> bool:
    """Sentinel inference over the reconstructed body, the read-time authority.

    Delegates to `_lib_conversation_query.is_compaction_row`, which is the ONE
    place this rule is written. A third restatement had already diverged from
    the canonical one on whether a malformed block element raises.
    """
    return _conversation_query().is_compaction_row(text, blocks_json)


def _row_field(row: Any, name: str, default: Any = None) -> Any:
    """One named column, whether the row is a `sqlite3.Row` or a mapping.

    The predicates are pure and unit-tested over plain dicts, while the
    evaluators feed them `sqlite3.Row` objects — which index by name but have
    no `get`.
    """
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _row_blocks(blocks_json: object) -> list:
    """The block objects of a row, dropping any element that is not one."""
    return [b for b in _conversation_query().parse_blocks_json(blocks_json)
            if isinstance(b, Mapping)]


# The 18-column physical stream `fold_claude_canonical` folds, in its order.
# Named here so a caller reading a narrower projection states which columns it
# is supplying and which it is defaulting.
_CLAUDE_FOLD_COLUMNS = (
    "id", "uuid", "timestamp_utc", "entry_type", "text", "blocks_json",
    "model", "msg_id", "req_id", "is_sidechain", "cwd", "git_branch",
    "source_path", "parent_uuid", "source_tool_use_id", "stop_reason",
    "attribution_skill", "attribution_plugin",
)


def _claude_fold_row(row: Any) -> tuple:
    return tuple(_row_field(row, name) for name in _CLAUDE_FOLD_COLUMNS)


def _claude_canonical_items(rows: Sequence[Any], *,
                            allow_human_fallback: bool = False) -> list:
    """The canonical items of ONE session, through the extracted fold.

    The normalization is REUSED rather than restated. `fold_claude_canonical`
    deduplicates `(session_id, uuid)`, groups assistant fragments
    non-adjacently by `(msg_id, req_id)`, folds tool results and skill bodies,
    promotes a slash-command invocation carrying real args to a human turn and
    applies the meta classification — which is the whole of spec 2.1's Claude
    predicate. Restating any part of it here is how the deduplication went
    missing in the first place.

    `rows` must be one session's rows in `(timestamp_utc, id)` order. The token
    and cost maps are empty because this path publishes no per-turn cost: cost
    comes from the accounting population the adapter has already read.
    """
    query = _conversation_query()
    physical = [_claude_fold_row(row) for row in rows]
    folded = query.fold_claude_canonical(
        physical, {}, {}, allow_human_fallback=allow_human_fallback)
    return folded["items"]


def _claude_item_is_human_turn(item: Mapping[str, Any]) -> bool:
    """A non-empty final `kind='human'` item on the main thread.

    `is_sidechain` and a non-null `subagent_key` both disqualify: subagent
    files carry the PARENT's `session_id`, which is why
    `conversation_sessions.msg_count` is a sidechain-inclusive physical count
    and cannot answer how many human turns a conversation has.
    """
    if item.get("kind") != "human":
        return False
    if item.get("is_sidechain"):
        return False
    if item.get("subagent_key") is not None:
        return False
    return bool((item.get("text") or "").strip())


def _claude_human_turn_items(items: Sequence[Mapping[str, Any]]) -> list:
    """The main-thread human turns among already-folded canonical items."""
    return [item for item in items if _claude_item_is_human_turn(item)]


@dataclass
class _ClaudeAssociation:
    """One qualifying human turn and the assistant turns that answered it."""

    human: Mapping[str, Any]
    replies: list


def _claude_associate_items(items: Sequence[Mapping[str, Any]]) -> list:
    """Associate each main-thread assistant turn with its nearest preceding
    qualifying human, over ONE session's already-folded items.

    Several assistant turns may belong to one human, and an assistant turn
    preceding any qualifying human is ORPHANED: it contributes no synthetic
    human turn and belongs to no association. Both sides come from the same
    canonical fold, so an assistant turn is one item per `(msg_id, req_id)`
    however many physical fragments it arrived in.
    """
    associations: list = []
    current: int | None = None
    for item in items:
        if _claude_item_is_human_turn(item):
            associations.append(_ClaudeAssociation(human=item, replies=[]))
            current = len(associations) - 1
            continue
        if item.get("kind") != "assistant":
            continue
        if item.get("is_sidechain") or item.get("subagent_key") is not None:
            continue
        if current is None:
            continue                        # orphaned: no qualifying human yet
        associations[current].replies.append(item)
    return associations


def _codex_origin(root_thread_id: object) -> str:
    """`main`, `delegated`, or `ambiguous`.

    `_inferred_codex_thread_source` returns any explicit `thread_source`
    VERBATIM, so there is no vocabulary to resolve an arbitrary string
    against. Only these two literals carry a meaning this tree can defend;
    every other value belongs to neither population, lowers
    `evaluabilityCoverage` and carries the identifiable-subset qualification.
    """
    category = codex_origin_category(root_thread_id)
    if category == CODEX_ORIGIN_MAIN:
        return "main"
    if category == CODEX_ORIGIN_SUBAGENT:
        return "delegated"
    return "ambiguous"


# The `codex_conversation_messages` columns a canonical row needs. `text` is
# read because "a human turn is a NON-EMPTY canonical prompt" and emptiness is
# a property of the body; nothing else about the body is used, and no part of
# it reaches a row field, an evidence value or the digest.
_CODEX_ROW_COLUMNS = (
    "conversation_key", "source_root_key", "source_path", "line_offset",
    "timestamp_utc", "turn_id", "call_id", "kind", "event_type",
    "record_family", "model", "text", "content_digest", "content_len",
    "detail_json", "search_tool", "search_thinking",
)


def _codex_normalized_rows(rows: Sequence[Any]) -> list:
    """Physical `codex_conversation_messages` rows as canonical row objects."""
    codex_kernel = _cctally()._load_sibling("_lib_codex_conversation")
    built = []
    for row in rows:
        values = {name: _row_field(row, name) for name in _CODEX_ROW_COLUMNS}
        built.append(codex_kernel.CodexNormalizedRow(
            conversation_key=str(values["conversation_key"] or ""),
            source_root_key=str(values["source_root_key"] or ""),
            source_path=str(values["source_path"] or ""),
            line_offset=int(values["line_offset"] or 0),
            timestamp_utc=values["timestamp_utc"],
            turn_id=values["turn_id"],
            call_id=values["call_id"],
            kind=str(values["kind"] or ""),
            event_type=values["event_type"],
            record_family=str(values["record_family"] or ""),
            model=values["model"],
            text=str(values["text"] or ""),
            content_digest=str(values["content_digest"] or ""),
            content_len=int(values["content_len"] or 0),
            detail_json=values["detail_json"],
            search_tool=str(values["search_tool"] or ""),
            search_thinking=str(values["search_thinking"] or ""),
        ))
    return built


def _codex_human_turns(rows: Sequence[Any]) -> int:
    """How many canonical human turns ONE Codex conversation holds.

    A physical `COUNT(*)` over `kind='user'` over-counts on three independent
    axes, and each one silently drops a short conversation by calling it long:
    a turn-less user row canonicalizes to `unturned` rather than `prompt`, the
    `event_msg` member of a digest-exact mirror pair is a duplicate of its
    `response_item` partner, and an empty body is not a turn at all.

    So the count goes through the same normalization
    `bin/_lib_codex_conversation.py` performs — `pair_mirrors` then
    `canonical_items` — restricted to `kind='user'` rows. That restriction is
    exact rather than approximate: mirror pairing groups by
    `(turn_id, kind, digest, len)` and tracks its unturned adjacency per kind,
    and the reasoning-containment pass touches `kind='reasoning'` only, so no
    pairing decision about a user row depends on a row of another kind.
    """
    codex_kernel = _cctally()._load_sibling("_lib_codex_conversation")
    kept, _suppressed = codex_kernel.pair_mirrors(_codex_normalized_rows(rows))
    items = codex_kernel.canonical_items(kept)
    return sum(1 for item in items
               if item["klass"] == "prompt"
               and (item["anchor_row"].text or "").strip())


def _median_turns(values: Sequence[int]) -> int | None:
    """The LOWER median, so an even count publishes a real member value."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[(len(ordered) - 1) // 2]


def _claude_context_window(model: object) -> int | None:
    """This request's own model context window, or `None` when unknown.

    Capacities come from the statusline's table, not from the pricing layer:
    a model can be priced and still have no published window, and the two
    tables answer different questions.
    """
    statusline = _cctally()._load_sibling("_cctally_statusline")
    return statusline._resolve_context_window(model, lambda *_a, **_k: None)


# --- the reads the evaluators run (#620 S3 §4.2) ------------------------
#
# Every statement below is SET-WISE and chunked. A per-session or per-file
# statement is the N+1 spec §4.2 exists to forbid, and it is multiplied by two
# bundles and up to two probe attempts.

# SQLite's compiled-parameter ceiling is far higher, but a bounded chunk keeps
# one very wide window from building a statement no plan can reuse.
_IN_CHUNK = 400

_CHURN_ROW_COLUMNS = ("id", "uuid", "entry_type", "text", "blocks_json",
                      "model", "msg_id", "req_id", "source_path")

_CLAUDE_WINDOW_SESSIONS_SQL = """
    SELECT DISTINCT session_id FROM conversation_messages
     WHERE timestamp_utc >= ? AND timestamp_utc < ? AND session_id IS NOT NULL
"""

# The seed prefix: the LAST `share` rows before the window, ranked descending
# so SQLite applies the budget rather than the reader fetching everything and
# declining to parse some of it. Ordered back to document order by `rank DESC`.
_CLAUDE_SEED_PREFIX_SQL = """
    SELECT session_id, id, uuid, entry_type, text, blocks_json, model,
           msg_id, req_id, source_path, rank FROM (
        SELECT session_id, id, uuid, entry_type, text, blocks_json, model,
               msg_id, req_id, source_path,
               ROW_NUMBER() OVER (PARTITION BY session_id
                                  ORDER BY timestamp_utc DESC, id DESC) AS rank
          FROM conversation_messages
         WHERE session_id IN ({placeholders}) AND timestamp_utc < ?
    ) WHERE rank <= ?
     ORDER BY session_id ASC, rank DESC
"""

_CLAUDE_WINDOW_ROWS_SQL = """
    SELECT session_id, id, uuid, entry_type, text, blocks_json, model,
           msg_id, req_id, source_path
      FROM conversation_messages
     WHERE session_id IN ({placeholders})
       AND timestamp_utc >= ? AND timestamp_utc < ?
     ORDER BY session_id ASC, timestamp_utc ASC, id ASC
"""

# The human-candidate population spans the WHOLE retained conversation,
# because "short" is a property of the conversation and a window-clipped count
# would call a long conversation short whenever the window caught only its
# tail. `entry_type` is not decisive — command normalization can promote a
# `meta` row to human — so each candidate costs a normalization, which is what
# `DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS` bounds.
# The 18 fold columns plus `session_id` and the budget rank. The whole stream
# is fed to `fold_claude_canonical`, which is the ONE statement of the Claude
# turn normalization, so the projection carries what that fold reads rather
# than a narrower set a second normalization would have needed.
_CLAUDE_TURN_CANDIDATE_SQL = """
    SELECT session_id, id, uuid, timestamp_utc, entry_type, text, blocks_json,
           model, msg_id, req_id, is_sidechain, cwd, git_branch, source_path,
           parent_uuid, source_tool_use_id, stop_reason, attribution_skill,
           attribution_plugin, rank FROM (
        SELECT session_id, id, uuid, timestamp_utc, entry_type, text,
               blocks_json, model, msg_id, req_id, is_sidechain, cwd,
               git_branch, source_path, parent_uuid, source_tool_use_id,
               stop_reason, attribution_skill, attribution_plugin,
               ROW_NUMBER() OVER (PARTITION BY session_id
                                  ORDER BY timestamp_utc ASC, id ASC) AS rank
          FROM conversation_messages
         WHERE session_id IN ({placeholders})
           AND entry_type IN ('meta', 'human')
           AND (is_sidechain IS NULL OR is_sidechain = 0)
    ) WHERE rank <= ?
     ORDER BY session_id ASC, rank ASC
"""

# In-window assistant turn keys. The Claude association is a STORED key on
# both sides, so the context-window fraction is derived from the accounting
# population the adapter has already loaded, at a cost bounded by the window's
# accounting size and independent of conversation length.
_CLAUDE_WINDOW_ASSISTANT_SQL = """
    SELECT session_id, id, uuid, timestamp_utc, entry_type, text, blocks_json,
           model, msg_id, req_id, is_sidechain, cwd, git_branch, source_path,
           parent_uuid, source_tool_use_id, stop_reason, attribution_skill,
           attribution_plugin
      FROM conversation_messages
     WHERE session_id IN ({placeholders}) AND entry_type = 'assistant'
       AND msg_id IS NOT NULL
       AND timestamp_utc >= ? AND timestamp_utc < ?
     ORDER BY session_id ASC, timestamp_utc ASC, id ASC
"""

# The fan-out path's own projection. It derives a bucket from the source path
# and joins on the turn key, so it needs five columns and no body at all —
# where the statement above carries the nineteen that feed the canonical fold.
# `blocks_json` is the largest column in the table on a real store, and
# loading every in-window assistant turn's body to read five columns is a cost
# the fan-out class never asked for. The ORDER BY is the same, because the
# deduplication keeps the FIRST occurrence per uuid and therefore depends on
# it; `timestamp_utc` and `id` order the result without being selected.
_CLAUDE_WINDOW_SUBAGENT_SQL = """
    SELECT session_id, uuid, source_path, msg_id, req_id
      FROM conversation_messages
     WHERE session_id IN ({placeholders}) AND entry_type = 'assistant'
       AND msg_id IS NOT NULL
       AND timestamp_utc >= ? AND timestamp_utc < ?
     ORDER BY session_id ASC, timestamp_utc ASC, id ASC
"""

# The canonical prompt candidates. A physical `COUNT(*)` cannot answer how
# many human turns a Codex conversation holds — spec 2.1 requires canonical
# `klass='prompt'` items formed over mirror-paired rows — so the rows
# themselves are read and normalized, under the same per-conversation
# normalize budget the Claude predicate carries, because canonicalization
# costs more than counting.
_CODEX_PROMPT_CANDIDATE_SQL = """
    SELECT conversation_key, source_root_key, source_path, line_offset,
           timestamp_utc, turn_id, call_id, kind, event_type, record_family,
           model, text, content_digest, content_len, detail_json, search_tool,
           search_thinking, rank FROM (
        SELECT conversation_key, source_root_key, source_path, line_offset,
               timestamp_utc, turn_id, call_id, kind, event_type,
               record_family, model, text, content_digest, content_len,
               detail_json, search_tool, search_thinking,
               ROW_NUMBER() OVER (PARTITION BY conversation_key
                                  ORDER BY timestamp_utc ASC,
                                           source_path ASC,
                                           line_offset ASC) AS rank
          FROM codex_conversation_messages
         WHERE conversation_key IN ({placeholders}) AND kind = 'user'
    ) WHERE rank <= ?
     ORDER BY conversation_key ASC, rank ASC
"""

_CODEX_EVENT_COLUMNS = (
    "source_path", "line_offset", "source_root_key", "conversation_key",
    "native_thread_id", "root_thread_id", "parent_thread_id", "timestamp_utc",
    "record_type", "event_type", "turn_id", "call_id", "payload_json",
)

# `infer_codex_event_turns` materializes a file's WHOLE sequence, so an
# arbitrarily long pre-window history is scanned to attribute one in-window
# entry. The budget is allocated per source file so no file starves another,
# and a file that exhausts its share yields NO turn map at all — a truncated
# read would produce a wrong map rather than a missing one.
_CODEX_EVENT_FILTER = """
           AND (record_type IN ('session_meta', 'turn_context')
                OR turn_id IS NOT NULL
                OR event_type = 'token_count')
"""

# Read at most ONE row beyond each file's allocated share. A grouped COUNT
# scans every token event even though the only question is whether the file
# exceeds its cap. Repeating this term under one `UNION ALL` keeps the probe
# set-wise while letting the source-path/offset index stop each arm at its
# first conclusive row. The decision remains exact: `share + 1` rows means the
# whole file is starved, while at most `share` means every relevant row fit.
_CODEX_EVENT_BUDGET_TERM_SQL = """
    SELECT ? AS source_path, COUNT(*) AS rows_read
      FROM (
        SELECT 1
          FROM codex_conversation_events
         WHERE source_path = ?
""" + _CODEX_EVENT_FILTER + """
         ORDER BY line_offset ASC
         LIMIT ?
      )
"""

# Three bindings per compound arm stay below SQLite's historical 999-variable
# ceiling, and below its 500-term compound-select ceiling too.
_CODEX_EVENT_BUDGET_CHUNK = 300

# Only lifecycle anchors can change the inferred turn. Accounting offsets are
# merged against these rows in Python, so token_count payloads never cross the
# SQLite boundary merely to inherit the current turn.
_CODEX_EVENT_ANCHORS_SQL = """
    SELECT source_path, line_offset, source_root_key, conversation_key,
           native_thread_id, root_thread_id, parent_thread_id, timestamp_utc,
           record_type, event_type, turn_id, call_id, payload_json
      FROM codex_conversation_events
     WHERE source_path IN ({placeholders})
       AND (record_type IN ('session_meta', 'turn_context')
            OR turn_id IS NOT NULL)
     ORDER BY source_path ASC, line_offset ASC
"""

_CODEX_THREADS_BY_KEY_SQL = """
    SELECT conversation_key, source_root_key, native_thread_id,
           root_thread_id, parent_thread_id, cwd, git_json, context_window
      FROM codex_conversation_threads
     WHERE conversation_key IN ({placeholders})
"""

_CODEX_THREADS_BY_NATIVE_SQL = """
    SELECT conversation_key, source_root_key, native_thread_id,
           root_thread_id, parent_thread_id, cwd, git_json, context_window
      FROM codex_conversation_threads
     WHERE native_thread_id IN ({placeholders})
"""

# `cache_create_1h_tokens` is fetched even though the churn predicate reads
# only the raw creation and read counts: the #195 rule is that every SELECT
# reading `cache_create_tokens` off `session_entries` carries the split
# column, because a missed site does not raise and does not move a golden — it
# silently under-prices — and this map is one keystroke away from feeding a
# cost fold.
_CLAUDE_TURN_TOKENS_SQL = (
    "SELECT msg_id, req_id, cache_create_tokens, cache_read_tokens, "
    "       cache_create_1h_tokens, speed "
    "FROM session_entries WHERE "
)


# The statements each provider runs against the CONVERSATIONS connection.
#
# This is the schema gate's whole input, and it is the statements themselves
# rather than a hand-written list of the tables and columns they name. The
# previous gate restated the requirement, nothing referenced it, and a
# projection that grew a column would have left the gate passing, the
# statement raising, and every affected user told we have a defect on every
# report — which is precisely the outcome the gate exists to prevent. A
# statement cannot disagree with itself.
#
# `test_every_claude_conversations_statement_is_declared` and its Codex twin
# assert that every statement the evaluators actually run appears here, so a
# statement added without being declared fails rather than going un-gated.
_CONVERSATIONS_STATEMENTS: dict[str, tuple[str, ...]] = {
    "claude": (
        _CLAUDE_WINDOW_SESSIONS_SQL,
        _CLAUDE_SEED_PREFIX_SQL,
        _CLAUDE_WINDOW_ROWS_SQL,
        _CLAUDE_TURN_CANDIDATE_SQL,
        _CLAUDE_WINDOW_ASSISTANT_SQL,
        _CLAUDE_WINDOW_SUBAGENT_SQL,
    ),
    "codex": (
        _CODEX_PROMPT_CANDIDATE_SQL,
        _CODEX_EVENT_BUDGET_TERM_SQL,
        _CODEX_EVENT_ANCHORS_SQL,
    ),
}

# The table a statement reads, for the digest row alone. The regex skips the
# `FROM (` of a windowed subquery because `(` is not an identifier character,
# so it names the physical table in every statement above. A wrong label here
# cannot change a verdict — only the word printed beside `component_schema_
# incomplete`.
_FROM_TABLE_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)")


def _statement_table(sql: str) -> str:
    match = _FROM_TABLE_RE.search(sql)
    return match.group(1) if match else "conversations"


# `no such table`, `no such column`, `has no column named` and `ambiguous
# column name` all describe an object the statement NAMED. Before the schema
# gate below one of these means the STORE cannot serve our reads; past it the
# store demonstrably carries what our statements name, so one of these is a
# defect in our own SQL and reporting it as an absent signal makes it
# indistinguishable from a store that genuinely holds nothing.
_MISSING_SCHEMA_OBJECT_RE = re.compile(
    r"no such (?:table|column|index|view|trigger)\b"
    r"|has no column named\b"
    r"|ambiguous column name\b"
)


def _conversations_schema_gap(conn: sqlite3.Connection,
                              source: str) -> str | None:
    """The first table this provider's reads need and the store cannot serve.

    Answered by asking the STORE — every statement is COMPILED against it
    with `EXPLAIN`, which prepares the statement and runs none of it. That is
    what makes `_is_store_failure` below able to treat a missing schema object
    past this point as OUR defect: past this gate the store has been observed
    to carry every object the statements name, because it compiled them all.

    A statement that fails to compile for any reason OTHER than a missing
    schema object is not a store shortfall — a window function unsupported by
    an older SQLite is our own environment, and `database is locked` is the
    store refusing to answer right now — so that exception propagates rather
    than being silently reported as an absent signal.

    WHERE it propagates to is the point, and it was nowhere until R13. Both
    callers now handle it: `_read_conversations_component` calls this INSIDE
    its own `except Exception` backstop, where `_is_store_failure` splits a
    store-shaped cause from a defect in our own code and only the three
    conversation-derived classes are withheld; and `_probe_component`, which
    runs outside that read and so cannot be moved inside it, catches
    `sqlite3.Error` and falls back to the wider probe pair. Neither swallows
    the cause: the probe declines to decide the fold, and the read that
    follows it asks the same question again and classifies the answer.

    An unrecognised source RAISES `EstablishmentFailure`, which is not a
    `sqlite3.Error` and therefore passes through the probe's catch and ends
    the report. Failing open would give a third provider exactly the behaviour
    this gate declined to allow: a statement raising past a gate that never
    checked it.
    """
    try:
        statements = _CONVERSATIONS_STATEMENTS[source]
    except KeyError as exc:
        raise EstablishmentFailure(
            EstablishmentError.STORE_UNAVAILABLE.value,
            f"no conversations statements are declared for source {source}",
        ) from exc
    for sql in statements:
        # One placeholder is enough to compile: `IN (?)` and `IN (?,?)` are the
        # same statement as far as the schema is concerned.
        compiled = sql.format(placeholders="?")
        try:
            _execute(conn, "EXPLAIN " + compiled,
                     (None,) * compiled.count("?"))
        except sqlite3.OperationalError as exc:
            if _MISSING_SCHEMA_OBJECT_RE.search(str(exc)):
                return _statement_table(sql)
            raise
    return None


# Messages a store failure never produces, because each one describes our own
# SQL text or the SQLite build we are running on rather than the bytes on
# disk. `ROW_NUMBER() OVER (...)` is a syntax error on SQLite before 3.25, and
# reporting that as an absent signal tells the user to fix a store that is
# perfectly healthy.
_OUR_OWN_SQL_RE = re.compile(
    r"syntax error"
    r"|no such function\b"
    r"|no such collation sequence\b"
    r"|wrong number of arguments\b"
    r"|misuse of\b"
)


def _is_store_failure(exc: BaseException) -> bool:
    """Whether a raised exception describes the STORE rather than our code.

    Mapping EVERY `sqlite3.Error` to a store-shaped cause moved the F12
    conflation rather than removing it: a `ProgrammingError` about the number
    of bindings, or an `OperationalError` naming a schema object, is a defect
    in this module and must say so. What remains store-shaped is what a store
    actually produces — a malformed image, a locked or busy file, a disk I/O
    error, a read-only or corrupt page — none of which our code can cause.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)):
        return False
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc)
        if (_MISSING_SCHEMA_OBJECT_RE.search(message)
                or _OUR_OWN_SQL_RE.search(message)):
            return False
    return True


def _chunks(values: Sequence[Any], size: int = _IN_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _claude_turn_token_map(conn: sqlite3.Connection,
                           keys: Sequence[tuple], account_key: str | None
                           ) -> dict[tuple, dict]:
    """`{(msg_id, req_id): {"cache_creation", "cache_read", "speed"}}`.

    The cache-churn walk begins BEFORE the window, and those turns are state:
    their tokens move the running maximum and they contribute no support,
    coverage, observed USD or denominator. They are not in the loaded
    accounting population, so they are read here — chunked, never per turn.
    """
    usage: dict[tuple, dict] = {}
    pairs = [(m, r) for (m, r) in dict.fromkeys(keys)
             if m is not None and r is not None]
    for chunk in _chunks(pairs):
        params: list[Any] = [value for pair in chunk for value in pair]
        condition = " OR ".join("(msg_id=? AND req_id=?)" for _ in chunk)
        # The account clause is appended AFTER the pair clause, so its
        # parameter follows the pair parameters in the same order.
        sql = (_CLAUDE_TURN_TOKENS_SQL + "(" + condition + ")"
               + _account_predicate("account_key", account_key, params))
        for row in _execute(conn, sql, params):
            usage[(row[0], row[1])] = {
                "cache_creation": int(row[2] or 0),
                "cache_read": int(row[3] or 0),
                "cache_1h": row[4],
                "speed": row[5],
            }
    return usage


# --- the evaluation result (#620 S3 §1.2, §1.6) -------------------------

@dataclass(frozen=True)
class _EvidenceValue:
    """One evidence figure, available with its value or withheld with a code."""

    value: Any = None
    code: str | None = None
    qualifications: tuple[str, ...] = ()


@dataclass(frozen=True)
class _S3Evaluation:
    """One conversation-derived class's decided facts over one window.

    `established` False means the signal could not be established at all,
    which is `withheld / signal_unavailable` and never `provider_unavailable`.
    The three populations of §1.6 are distinct: CANDIDATE entries were
    eligible for an attempted evaluation, EVALUATED entries are those whose
    predicate could be decided, and QUALIFYING entries are the evaluated ones
    the predicate matched.
    """

    established: bool = False
    # Set when the evaluator RAISED. A caught-and-logged degrade that renders
    # as "the signal could not be established" is indistinguishable from a
    # store that genuinely holds nothing, so the two are separated here and
    # the loader publishes `calculation_failed` for this one.
    failure: str | None = None
    qualifying: tuple = ()
    evaluated: tuple = ()
    candidate_count: int = 0
    gap_codes: tuple[str, ...] = ()
    evidence: Mapping[str, _EvidenceValue] = field(default_factory=dict)
    qualifications: tuple[str, ...] = ()

    @property
    def qualifying_usd(self) -> float:
        return stable_sum(entry.cost_usd for entry in self.qualifying)

    def digest_rows(self, kind: str) -> list[tuple]:
        """Exactly the derived facts the report publishes or aggregates.

        Per-row topology, opaque provider keys, raw text, blocks payloads,
        content digests, filesystem paths and identities reach neither this
        digest nor the wire, so it is an equality oracle only for facts the
        response body already discloses. A compaction change still moves it,
        because a compaction change moves the flagged-turn count and the
        estimated wasted USD.
        """
        rows: list[tuple] = [
            (f"{kind}.established", int(self.established)),
            (f"{kind}.failure", self.failure or ""),
            (f"{kind}.qualifying_usd", repr(self.qualifying_usd)),
            (f"{kind}.qualifying_entries", len(self.qualifying)),
            (f"{kind}.evaluated", len(self.evaluated)),
            (f"{kind}.candidates", self.candidate_count),
            (f"{kind}.gaps", ",".join(sorted(self.gap_codes))),
            (f"{kind}.qualifications", ",".join(sorted(self.qualifications))),
        ]
        for name in sorted(self.evidence):
            field_value = self.evidence[name]
            rows.append((
                f"{kind}.evidence.{name}",
                field_value.code if field_value.code is not None
                else repr(field_value.value),
            ))
        return rows


# Each S3 class contributes ONE aggregate subject whose key, kind and label
# are constants of the class rather than anything derived from a member.
# `_display_key` aliases only project and session subjects and emits every
# other key verbatim, so a member-derived key would reach the wire and the
# screen unaliased.
_S3_SUBJECT = {
    "cache_churn": (kernel.SUBJECT_CACHE_CHURN, "Turns that rebuilt their cache"),
    "short_high_context": (kernel.SUBJECT_SHORT_HIGH_CONTEXT,
                           "Short conversations carrying large context"),
    "subagent_fanout": (kernel.SUBJECT_SUBAGENT_FANOUT,
                        "Delegated subagent work"),
}


def _unestablished(gap_codes: Sequence[str] = ()) -> _S3Evaluation:
    """A signal that could not be established, carrying WHY where it is known.

    A class withheld because every spending session exhausted its seed share
    reported `signal_unavailable` with nothing beside it, so the one fact that
    explains the withholding was dropped exactly where the reader needs it.
    """
    return _S3Evaluation(established=False, gap_codes=tuple(gap_codes))


def _nothing_evaluable(candidates: Sequence[Any],
                       evaluated: Sequence[Any]) -> bool:
    """Whether a class had candidates and could decide none of them.

    Spec 2.2, 2.3 and 2.4 state the same rule for all three conversation
    classes: a signal that could not be established at all is
    `withheld / signal_unavailable`, not a class answered over an empty
    evaluated population. Reporting the latter publishes
    `insufficient_population (support 0 units)` over a fully populated
    window, which is a false sentence about the store. A class with NO
    candidates is a different statement and keeps its support shortfall.
    """
    return bool(candidates) and not evaluated


def _seed_gap(exhausted: Sequence[Any]) -> tuple[str, ...]:
    """The gap code a seed-budget exhaustion publishes, or nothing."""
    return (kernel.GAP_SCAN_BUDGET_EXHAUSTED,) if exhausted else ()


def _fanout_gaps(unallocated: Sequence[Any],
                 ambiguous: Sequence[Any]) -> tuple[str, ...]:
    """The fan-out gap codes, stated once for both the established and the
    unestablished return."""
    codes = []
    if unallocated:
        codes.append(kernel.GAP_UNRESOLVED_SUBAGENT_ATTRIBUTION)
    if ambiguous:
        codes.append(kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY)
    return tuple(sorted(codes))


# --- 2.2 prompt-cache churn, Claude only --------------------------------

def _claude_window_session_ids(scope: DiagnosisScope, bundle: StoreBundle,
                               conversations: sqlite3.Connection
                               ) -> tuple[str, ...]:
    """The shared conversation population for this provider/window bundle."""
    if bundle.claude_window_sessions is None:
        # Seed and normalization budgets are divided over the COMPLETE
        # transcript population, not only sessions carrying priced entries.
        # A transcript-only session can therefore change another session's
        # share even though it can never contribute a ranked dollar. Replacing
        # this set with the accounting population changes typed withholding,
        # so the complete range scan is part of the evidence contract.
        bounds = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
        bundle.claude_window_sessions = tuple(sorted(
            str(row[0]) for row in _execute(
                conversations, _CLAUDE_WINDOW_SESSIONS_SQL, bounds)
        ))
    return bundle.claude_window_sessions


def _evaluate_cache_churn(scope: DiagnosisScope, bundle: StoreBundle,
                          conversations: sqlite3.Connection) -> _S3Evaluation:
    """Flag every in-window turn that re-created the bulk of its cached prefix.

    `_iter_cache_failures` is reused unchanged and gains no seed parameter:
    seeding is achieved by WHAT IT IS FED. For every session with potentially
    evaluable events in the window the stream begins at the last normalized
    compaction strictly before `window_start`, inclusive; where none exists it
    begins at the session's earliest retained event with an empty running
    maximum. Only flags whose assistant event falls inside the half-open
    window are published — the prefix is state.
    """
    query = _conversation_query()
    facts = bundle.facts
    bounds = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    all_sessions = _claude_window_session_ids(scope, bundle, conversations)
    if not all_sessions:
        return _unestablished()

    allocation = allocate_scan_budget(all_sessions,
                                      kernel.DIAGNOSIS_SEED_SCAN_BUDGET_ROWS)
    # Preserve the complete-population allocation above, then avoid loading
    # seed/window bodies for transcript-only sessions when accounting already
    # resolved the exact sessions that can contribute a ranked dollar. The
    # empty-accounting fallback keeps the prior typed outcome for older or
    # partially populated stores.
    spending_sessions = {
        entry.session_key for entry in facts.entries
        if entry.session_identity_resolved and entry.session_key
    }
    sessions = tuple(
        session for session in all_sessions if session in spending_sessions
    ) if spending_sessions else all_sessions
    largest = max((allocation[session] for session in sessions), default=0)
    prefix: dict[str, list] = {session: [] for session in sessions}
    overflowed: set[str] = set()
    for chunk in _chunks(sessions):
        sql = _CLAUDE_SEED_PREFIX_SQL.format(
            placeholders=_placeholders(len(chunk)))
        # `largest + 1` fetches ONE row past the widest share, so a session
        # holding more history than its own share is visible as exhausted
        # without a second query per session.
        for row in _execute(conversations, sql,
                            list(chunk) + [bounds[0], largest + 1]):
            session = str(row["session_id"])
            if int(row["rank"]) > allocation.get(session, 0):
                overflowed.add(session)
                continue
            prefix[session].append(row)

    window_rows: dict[str, list] = {session: [] for session in sessions}
    for chunk in _chunks(sessions):
        sql = _CLAUDE_WINDOW_ROWS_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk) + bounds):
            window_rows[str(row["session_id"])].append(row)

    is_compaction = query.is_compaction_row
    streams: dict[str, list] = {}
    exhausted: set[str] = set()
    for session in sessions:
        rows = prefix.get(session, [])
        seed_index = None
        for index, row in enumerate(rows):
            if (row["entry_type"] in ("meta", "human")
                    and is_compaction(row["text"], row["blocks_json"])):
                seed_index = index
        if seed_index is not None:
            head = rows[seed_index:]
        elif session in overflowed:
            # The share was consumed without reaching a compaction and more
            # history exists, so the seed cannot be established for this
            # session. It leaves the evaluated population rather than being
            # walked from an arbitrary point with a fabricated running maximum.
            exhausted.add(session)
            continue
        else:
            head = rows
        streams[session] = head + window_rows.get(session, [])
    if not streams:
        return _unestablished(_seed_gap(exhausted))

    turn_keys = [
        (row["msg_id"], row["req_id"])
        for rows in streams.values() for row in rows
        if row["entry_type"] == "assistant" and row["msg_id"] is not None
    ]
    usage = _claude_turn_token_map(bundle.connection("cache"), turn_keys,
                                   scope.account_key)

    flagged_keys: set[tuple] = set()
    flagged_sessions: set[str] = set()
    wasted: list[float] = []
    for session, rows in streams.items():
        in_window = {
            (row["msg_id"], row["req_id"])
            for row in window_rows.get(session, ())
            if row["entry_type"] == "assistant" and row["msg_id"] is not None
        }
        physical = [tuple(row[column] for column in _CHURN_ROW_COLUMNS)
                    for row in rows]
        events, event_keys = query.fold_claude_cache_failure_events(
            physical, usage, with_sources=True)
        for index, _prev, lost, model, speed in query._iter_cache_failures(
                events):
            key = event_keys[index]
            if key is None or key not in in_window:
                continue
            flagged_keys.add(key)
            flagged_sessions.add(session)
            wasted.append(query._cache_failure_wasted_usd(
                model, lost, speed=speed))

    known = set(all_sessions)
    seeded = set(streams)
    candidates = [entry for entry in facts.entries
                  if entry.session_key in known]
    evaluated = [entry for entry in candidates if entry.session_key in seeded]
    if _nothing_evaluable(candidates, evaluated):
        # `streams` is a statement about SESSIONS and `evaluated` is built
        # from ENTRIES, so a window where one session supplies the transcript
        # rows that seed and a different session supplies the spend passes the
        # session-level guard with nothing evaluable behind it. The same rule
        # the other two classes apply is what stops it publishing
        # `insufficient_population (support 0 units)` over a populated window.
        return _unestablished(_seed_gap(exhausted))
    qualifying = [entry for entry in evaluated
                  if (entry.msg_id, entry.req_id) in flagged_keys]
    return _S3Evaluation(
        established=True,
        qualifying=tuple(qualifying),
        evaluated=tuple(evaluated),
        candidate_count=len(candidates),
        gap_codes=_seed_gap(exhausted),
        evidence={
            "flaggedTurnCount": _EvidenceValue(value=len(flagged_keys)),
            "affectedConversationCount": _EvidenceValue(
                value=len(flagged_sessions)),
            # The counterfactual, and never the sort key: this class ranks on
            # the RETAINED cost of the turns it flags, like every other class.
            "estWastedUsd": _EvidenceValue(value=stable_sum(wasted)),
        },
    )


# --- 2.3 short conversations carrying large context ---------------------

def _large_enough(fraction: float) -> bool:
    """`>= 0.80`, with the repository's coverage slack.

    A fraction is a ratio of a summed token count to a capacity, so a request
    genuinely at four fifths of its window can compute one bit below it. The
    slack is the same internal float guard the coverage thresholds use; the
    PUBLISHED rule is the threshold itself.
    """
    return fraction >= (kernel.DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION
                        - kernel.COVERAGE_EPSILON)


def _short_context_evaluation(facts: RawFacts, *, known: Sequence[str],
                              unevaluable: Mapping[str, str],
                              qualifying: Sequence[str],
                              turn_counts: Mapping[str, int],
                              fractions: Mapping[str, float],
                              qualifications: Sequence[str] = (),
                              session_level: Mapping[str, bool] | None = None
                              ) -> _S3Evaluation:
    """Shape one provider's decided conversations into the class result."""
    known_set = set(known)
    qualifying_set = set(qualifying)
    by_session, _labels = facts.grouped_entries["session"]
    candidate_count = sum(len(by_session.get(key, ())) for key in known_set)
    evaluated = tuple(
        entry
        for key in known_set if key not in unevaluable
        for entry in by_session.get(key, ())
    )
    if candidate_count and not evaluated:
        return _unestablished(sorted(set(unevaluable.values())))
    matched = tuple(
        entry
        for key in qualifying_set if key not in unevaluable
        for entry in by_session.get(key, ())
    )
    member_turns = [turn_counts[key] for key in qualifying_set
                    if key in turn_counts]
    members = sorted((fractions[key], key) for key in qualifying_set
                     if key in fractions)
    thin = kernel.WithheldCause.INSUFFICIENT_POPULATION.value
    median = _median_turns(member_turns)
    fraction_field = _EvidenceValue(code=thin)
    if members:
        best_fraction, best_key = members[-1]
        # The qualification describes the conversation that produced the
        # PUBLISHED maximum, so a per-turn winner is never marked
        # session-level because some other conversation fell back.
        marks = ((kernel.QUALIFICATION_SESSION_LEVEL_CAPACITY,)
                 if (session_level or {}).get(best_key) else ())
        fraction_field = _EvidenceValue(value=best_fraction,
                                        qualifications=marks)
    return _S3Evaluation(
        established=True,
        qualifying=matched,
        evaluated=evaluated,
        candidate_count=candidate_count,
        gap_codes=tuple(sorted(set(unevaluable.values()))),
        evidence={
            "conversationCount": _EvidenceValue(value=len(qualifying_set)),
            "medianHumanTurns": (_EvidenceValue(value=median)
                                 if median is not None
                                 else _EvidenceValue(code=thin)),
            "maxContextWindowFraction": fraction_field,
        },
        qualifications=tuple(qualifications),
    )


def _evaluate_short_high_context(scope: DiagnosisScope, bundle: StoreBundle,
                                 conversations: sqlite3.Connection
                                 ) -> _S3Evaluation:
    if scope.source == "codex":
        return _evaluate_codex_short_high_context(scope, bundle, conversations)
    return _evaluate_claude_short_high_context(scope, bundle, conversations)


def _evaluate_claude_short_high_context(scope: DiagnosisScope,
                                        bundle: StoreBundle,
                                        conversations: sqlite3.Connection
                                        ) -> _S3Evaluation:
    """Conversations of one to three human turns holding a very large request.

    Turn counts span the WHOLE retained conversation while the COST stays
    window-clipped, because "short" is a property of the conversation and a
    window-clipped count would call a long conversation short whenever the
    window caught only its tail.

    `maxContextWindowFraction` reads no retained history at all on Claude: the
    association is a STORED key on both sides, so the fraction is derived from
    the in-window accounting population the adapter has already loaded, joined
    on `(msg_id, req_id)`.
    """
    facts = bundle.facts
    bounds = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    sessions = _claude_window_session_ids(scope, bundle, conversations)
    if not sessions:
        return _unestablished()

    budget = kernel.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS
    candidate_rows: dict[str, list] = {session: [] for session in sessions}
    overflowed: set[str] = set()
    for chunk in _chunks(sessions):
        sql = _CLAUDE_TURN_CANDIDATE_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk) + [budget + 1]):
            session = str(row["session_id"])
            if int(row["rank"]) > budget:
                overflowed.add(session)
                continue
            candidate_rows[session].append(row)

    assistants: dict[str, list] = {session: [] for session in sessions}
    for chunk in _chunks(sessions):
        sql = _CLAUDE_WINDOW_ASSISTANT_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk) + bounds):
            assistants[str(row["session_id"])].append(row)

    by_turn: dict[tuple, list] = {}
    for entry in facts.entries:
        if entry.msg_id is not None:
            by_turn.setdefault((entry.msg_id, entry.req_id), []).append(entry)

    maximum = kernel.DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS
    unevaluable: dict[str, str] = {}
    qualifying: list[str] = []
    turn_counts: dict[str, int] = {}
    fractions: dict[str, float] = {}
    for session in sessions:
        rows = candidate_rows.get(session, [])
        replies = assistants.get(session, [])
        # ONE fold per session over the whole stream. The human-turn count
        # spans the retained candidates and the associations need the
        # in-window assistants, and an assistant row creates no human item, so
        # folding the union answers both — where folding twice would parse
        # every `blocks_json` body twice.
        items = _claude_canonical_items(sorted(
            list(rows) + list(replies),
            key=lambda r: (str(_row_field(r, "timestamp_utc") or ""),
                           int(_row_field(r, "id") or 0))))
        count = len(_claude_human_turn_items(items))
        if count > maximum:
            turn_counts[session] = count
            continue                        # decided: the conversation is long
        if session in overflowed:
            # Never short, never long: the budget ran out before the predicate
            # could be decided, so the conversation leaves the evaluated
            # population rather than being classified by a truncated count.
            unevaluable[session] = kernel.GAP_SCAN_BUDGET_EXHAUSTED
            continue
        turn_counts[session] = count
        if count < 1:
            continue                        # decided: no human turn at all
        associations = _claude_associate_items(items)
        # A canonical turn item is one item per `(msg_id, req_id)` however
        # many physical fragments it arrived in, and the fold strips the
        # internal turn key before returning. The item's anchor uuid names the
        # physical row it was seeded from, and every fragment of a turn shares
        # that turn's key, so the anchor recovers it exactly.
        key_of_uuid = {str(_row_field(row, "uuid")):
                       (_row_field(row, "msg_id"), _row_field(row, "req_id"))
                       for row in replies}
        keys = [key_of_uuid[uuid] for association in associations
                for reply in association.replies
                for uuid in [str(reply["anchor"]["uuid"])]
                if key_of_uuid.get(uuid, (None,))[0] is not None]
        best: float | None = None
        saw_request = False
        for key in dict.fromkeys(keys):
            for entry in by_turn.get(key, ()):
                saw_request = True
                capacity = _claude_context_window(entry.model)
                if not capacity:
                    continue
                # The Claude numerator, matching the statusline's own context
                # segment: input plus BOTH cache legs.
                used = (entry.input_tokens + entry.cache_read_tokens
                        + entry.cache_create_tokens)
                fraction = used / capacity
                best = fraction if best is None else max(best, fraction)
        if saw_request and best is None:
            unevaluable[session] = kernel.GAP_UNKNOWN_CONTEXT_WINDOW
            continue
        # With several replies the LARGEST single request fraction is used,
        # never their sum: two replies at 0.45 do not make a full window.
        if best is not None and _large_enough(best):
            qualifying.append(session)
            fractions[session] = best
    return _short_context_evaluation(
        facts, known=sessions, unevaluable=unevaluable, qualifying=qualifying,
        turn_counts=turn_counts, fractions=fractions,
    )


def _codex_turn_capacities(events: Sequence[Any]) -> dict[str, int]:
    """`{turn_id: model_context_window}` from the retained lifecycle records.

    Codex capacity is PER TURN, not per session:
    `codex_conversation_threads.context_window` is populated only from
    `session_meta` and cannot describe a request whose model changed
    mid-thread, while `turn_context.model_context_window` is retained per
    turn.
    """
    capacities: dict[str, int] = {}
    for event in events:
        # Spec 2.3 names `turn_context.model_context_window`, and the record
        # type is what identifies one: `infer_codex_event_turns` and the
        # normalizer both branch on it. Accepting the key from any payload
        # takes a capacity from a record that never described a turn.
        if getattr(event, "record_type", None) != "turn_context":
            continue
        try:
            payload = json.loads(getattr(event, "payload_json", "") or "{}")
        except (ValueError, TypeError):
            continue
        body = payload.get("payload")
        if not isinstance(body, Mapping):
            continue
        window = body.get("model_context_window")
        turn_id = getattr(event, "turn_id", None) or body.get("turn_id")
        if not isinstance(window, int) or window <= 0 or not turn_id:
            continue
        capacities[str(turn_id)] = window
    return capacities


def _fold_codex_target_turns(events: Sequence[Any],
                             target_offsets: Sequence[int]) -> dict[int, str | None]:
    """Infer turns only at selected physical offsets.

    Rows without a native turn anchor never change the lifecycle state. The
    old diagnosis nevertheless loaded every token-count payload, constructed
    a ``CodexPhysicalEvent`` for it, parsed its JSON, and then threw every map
    entry away except the accounting offsets. This merge walks the retained
    lifecycle anchors and the already-known accounting offsets directly.

    The pending list preserves the canonical late-anchor rule: a later native
    proof backfills only the unanchored prefix since the latest
    ``session_meta``. A ``task_started`` anchor establishes the turn forward
    but does not backfill that prefix.
    """
    ordered_events = sorted(events, key=lambda event: int(event.line_offset))
    targets = sorted({int(offset) for offset in target_offsets})
    result: dict[int, str | None] = {}
    pending: list[int] = []
    current: str | None = None
    target_index = 0

    def _record_until(limit: int, *, inclusive: bool = False) -> None:
        nonlocal target_index
        while target_index < len(targets):
            offset = targets[target_index]
            if offset > limit or (offset == limit and not inclusive):
                break
            if current is None:
                pending.append(offset)
            else:
                result[offset] = current
            target_index += 1

    for event in ordered_events:
        offset = int(event.line_offset)
        _record_until(offset)
        record_type = getattr(event, "record_type", None)
        event_type = getattr(event, "event_type", None)
        explicit = getattr(event, "turn_id", None)
        if explicit is None and record_type == "turn_context":
            try:
                obj = json.loads(getattr(event, "payload_json", "") or "{}")
            except (ValueError, TypeError):
                obj = {}
            payload = obj.get("payload") if isinstance(obj, Mapping) else None
            candidate = payload.get("turn_id") if isinstance(payload, Mapping) else None
            explicit = candidate if isinstance(candidate, str) and candidate else None
        if record_type == "session_meta":
            # A later anchor cannot cross this segment boundary. Pending
            # targets from the preceding segment stay unresolved.
            for target in pending:
                result[target] = None
            pending.clear()
            current = None
        elif explicit is not None:
            is_late = (record_type != "turn_context"
                       and event_type != "task_started")
            if current is None and is_late:
                for target in pending:
                    result[target] = str(explicit)
            else:
                for target in pending:
                    result[target] = None
            pending.clear()
            current = str(explicit)
        if (record_type == "session_meta" and target_index < len(targets)
                and targets[target_index] == offset):
            result[offset] = None
            target_index += 1
        else:
            _record_until(offset, inclusive=True)

    _record_until(targets[-1] if targets else -1, inclusive=True)
    for target in pending:
        result[target] = None
    return {offset: result.get(offset) for offset in targets}


def _codex_threads_for(cache: sqlite3.Connection, sql: str,
                       values: Sequence[str]) -> list:
    rows: list = []
    for chunk in _chunks(list(values)):
        rows.extend(_execute(cache, sql.format(
            placeholders=_placeholders(len(chunk))), list(chunk)))
    return rows


def _evaluate_codex_short_high_context(scope: DiagnosisScope,
                                       bundle: StoreBundle,
                                       conversations: sqlite3.Connection
                                       ) -> _S3Evaluation:
    """The Codex half, over normalized prompts and per-turn capacity.

    The population is the MAIN-THREAD conversations, and only the exact
    literals `user` and `subagent` are recognisable origins — so an ambiguous
    origin excludes a thread from the main-thread population just as it
    excludes it from the delegated one. The consequence is stated rather than
    hidden: this class is an identifiable subset of unknown completeness on
    Codex, not only the fan-out class.
    """
    facts = bundle.facts
    by_conversation, _labels = facts.grouped_entries["session"]
    scoped = sorted(key for key in by_conversation if key)
    if not scoped:
        return _unestablished()
    threads = {str(row["conversation_key"]): row
               for row in bundle.codex_threads}
    budget = kernel.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS
    candidate_rows: dict[str, list] = {key: [] for key in scoped}
    overflowed: set[str] = set()
    for chunk in _chunks(scoped):
        sql = _CODEX_PROMPT_CANDIDATE_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk) + [budget + 1]):
            key = str(row["conversation_key"])
            if int(row["rank"]) > budget:
                overflowed.add(key)
                continue
            candidate_rows[key].append(row)
    prompt_counts: dict[str, int] = {}

    def _prompt_count(key: str) -> int:
        """Canonicalize ONE conversation's prompts, once, on first demand.

        Counting every candidate up front normalized conversations the loop
        below then discarded — a delegated thread, an ambiguous origin and an
        overflowed budget all decide the conversation without ever reading the
        count. The work is bounded by the per-conversation normalize budget,
        so it was waste rather than a defect, but it is waste proportional to
        the window's whole conversation set.
        """
        if key not in prompt_counts:
            prompt_counts[key] = _codex_human_turns(
                candidate_rows.get(key, []))
        return prompt_counts[key]

    targets_by_path: dict[str, list[int]] = {}
    for entry in facts.entries:
        if entry.source_path and entry.line_offset is not None:
            targets_by_path.setdefault(entry.source_path, []).append(
                int(entry.line_offset))
    turn_maps, capacities, starved = _codex_turn_attribution(
        conversations, targets_by_path)

    maximum = kernel.DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS
    unevaluable: dict[str, str] = {}
    qualifying: list[str] = []
    turn_counts: dict[str, int] = {}
    fractions: dict[str, float] = {}
    # Per subject, not per provider: one boolean for the whole provider stamps
    # the session-level qualification on `maxContextWindowFraction` even when
    # the published maximum came from a per-turn capacity.
    session_level: dict[str, bool] = {}
    for key in scoped:
        thread = threads.get(key)
        origin = _codex_origin(
            thread["root_thread_id"] if thread is not None else None)
        if origin == "ambiguous":
            # Neither population. The value is not a category this tree can
            # attribute a meaning to, so the predicate could not be decided.
            # The ORIGIN is what could not be read, which is why this is not
            # `unknown_context_window`.
            unevaluable[key] = kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY
            continue
        if origin != "main":
            continue                        # decided: a delegated thread
        if key in overflowed:
            # Never short, never long: canonicalizing the prompts ran out of
            # its per-conversation share before the predicate could be decided.
            unevaluable[key] = kernel.GAP_SCAN_BUDGET_EXHAUSTED
            continue
        count = _prompt_count(key)
        turn_counts[key] = count
        if count > maximum or count < 1:
            continue
        entries = by_conversation.get(key, [])
        if any(entry.source_path in starved for entry in entries):
            unevaluable[key] = kernel.GAP_SCAN_BUDGET_EXHAUSTED
            continue
        best: float | None = None
        best_is_session_level = False
        saw_request = False
        for entry in entries:
            turn = turn_maps.get(entry.source_path, {}).get(entry.line_offset)
            if turn is None:
                continue                    # orphaned: no owning turn
            saw_request = True
            from_session = False
            capacity = capacities.get(str(turn))
            if not capacity and thread is not None:
                capacity = thread["context_window"]
                from_session = bool(capacity)
            if not capacity:
                continue
            # Codex `input_tokens` is already cache-inclusive.
            fraction = entry.input_tokens / capacity
            if best is None or fraction > best:
                best = fraction
                best_is_session_level = from_session
        if saw_request and best is None:
            unevaluable[key] = kernel.GAP_UNKNOWN_CONTEXT_WINDOW
            continue
        if best is not None and _large_enough(best):
            qualifying.append(key)
            fractions[key] = best
            session_level[key] = best_is_session_level
    return _short_context_evaluation(
        facts, known=scoped, unevaluable=unevaluable, qualifying=qualifying,
        turn_counts=turn_counts, fractions=fractions,
        qualifications=(kernel.QUALIFICATION_IDENTIFIABLE_SUBSET,),
        session_level=session_level,
    )


def _codex_turn_attribution(conversations: sqlite3.Connection,
                            targets_by_path: Mapping[str, Sequence[int]]):
    """`(turn_maps, capacities, starved)` over the budgeted event inference.

    A file that exhausts its share yields NO turn map: a truncated read would
    produce a WRONG map rather than a missing one, because the inference
    replays a file's whole lifecycle from its start.
    """
    turn_maps: dict[str, dict] = {}
    capacities: dict[str, int] = {}
    starved: set[str] = set()
    paths = sorted(targets_by_path)
    if not paths:
        return turn_maps, capacities, starved
    physical = _cctally()._load_sibling("_lib_jsonl").CodexPhysicalEvent
    allocation = allocate_scan_budget(
        list(paths), kernel.DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS)
    per_file = kernel.DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS
    allocation = {path: min(share, per_file)
                  for path, share in allocation.items()}
    for chunk in _chunks(paths, _CODEX_EVENT_BUDGET_CHUNK):
        sql = "\nUNION ALL\n".join(
            _CODEX_EVENT_BUDGET_TERM_SQL for _path in chunk)
        params: list[Any] = []
        for path in chunk:
            params.extend((path, path, allocation.get(path, 0) + 1))
        for row in _execute(conversations, sql, params):
            path = str(row["source_path"])
            if int(row["rows_read"]) > allocation.get(path, 0):
                starved.add(path)
    grouped: dict[str, list] = {path: [] for path in paths if path not in starved}
    eligible = sorted(grouped)
    for chunk in _chunks(eligible):
        sql = _CODEX_EVENT_ANCHORS_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk)):
            grouped.setdefault(str(row["source_path"]), []).append(row)
    for path, rows in grouped.items():
        events = [physical(*[row[column] for column in _CODEX_EVENT_COLUMNS])
                  for row in rows]
        turn_maps[path] = _fold_codex_target_turns(
            events, targets_by_path.get(path, ()))
        capacities.update(_codex_turn_capacities(events))
    return turn_maps, capacities, starved


# --- 2.4 subagent fan-out ----------------------------------------------

def _fanout_evidence(facts: RawFacts, *, candidates: Sequence[Any],
                     unallocated: Sequence[Any], allocated: Sequence[Any],
                     bucket_usd: Mapping[Any, float], identified: int,
                     subset: bool,
                     ambiguous: Sequence[Any] = ()) -> _S3Evaluation:
    """Shape one provider's resolved fan-out into the class result.

    `observedUsd` carries ALLOCATED qualifying cost only. Unallocated cost is
    published as its own figure and never folded in or discarded, and it
    qualifies the row as partial attribution.

    **An entry whose origin category could not be read is NOT unallocated.**
    An unallocated entry is known subagent spend that could not be joined
    exactly once to a bucket. An ambiguous one is spend this tree can say
    nothing about — a thread carrying an unrecognised origin, or no thread row
    at all, which is the shape a Codex rollout landing mid-`thread_source`
    rollout produces. Filing it as unallocated overstates `unallocatedUsd`
    and stamps partial attribution on conversations that may be main-thread.

    Both kinds are candidates that FAILED evaluation, so both lower
    `evaluabilityCoverage` and both are excluded from `support_units`; only
    the unallocated ones contribute dollars to a published figure.
    """
    unallocated_usd = stable_sum(entry.cost_usd for entry in unallocated)
    undecided = {id(entry) for entry in unallocated}
    undecided.update(id(entry) for entry in ambiguous)
    evaluated = [entry for entry in candidates if id(entry) not in undecided]
    if _nothing_evaluable(candidates, evaluated):
        return _unestablished(_fanout_gaps(unallocated, ambiguous))
    qualifying_usd = stable_sum(entry.cost_usd for entry in allocated)
    largest = max(bucket_usd.values(), default=0.0)
    subset_marks = ((kernel.QUALIFICATION_IDENTIFIABLE_SUBSET,) if subset
                    else ())
    qualifications = list(subset_marks)
    if unallocated_usd > 0.0:
        qualifications.append(kernel.QUALIFICATION_PARTIAL_ATTRIBUTION)
    thin = kernel.WithheldCause.INSUFFICIENT_POPULATION.value
    return _S3Evaluation(
        established=True,
        qualifying=tuple(allocated),
        evaluated=tuple(evaluated),
        candidate_count=len(candidates),
        gap_codes=_fanout_gaps(unallocated, ambiguous),
        evidence={
            "identifiedSubagentCount": _EvidenceValue(
                value=identified, qualifications=subset_marks),
            # Divided by the CLASS's own qualifying USD, never by the report
            # denominator: this states how concentrated the fan-out is, not
            # how large it is.
            "largestSubagentShare": (
                _EvidenceValue(value=largest / qualifying_usd)
                if qualifying_usd > 0 else _EvidenceValue(code=thin)),
            "unallocatedUsd": _EvidenceValue(value=unallocated_usd),
        },
        qualifications=tuple(qualifications),
    )


def _evaluate_subagent_fanout(scope: DiagnosisScope, bundle: StoreBundle,
                              conversations: sqlite3.Connection | None
                              ) -> _S3Evaluation:
    if scope.source == "codex":
        # Codex derives this from `codex_conversation_threads` and
        # `codex_session_entries`, both of which live in `cache.db`, so it
        # needs no transcript authorization and none is claimed.
        return _evaluate_codex_fanout(scope, bundle)
    if conversations is None:
        return _unestablished()
    return _evaluate_claude_fanout(scope, bundle, conversations)


def _evaluate_claude_fanout(scope: DiagnosisScope, bundle: StoreBundle,
                            conversations: sqlite3.Connection
                            ) -> _S3Evaluation:
    """Cost bucketed by `(session_id, subagent_key)`, grouped by parent.

    The raw `source_path` never leaves the reader: `_subagent_key` strips the
    `agent-` prefix and the `.jsonl` suffix and returns the hash alone. Cost
    comes from the canonical accounting population the adapter has already
    read at read-time pricing; the outline's `subagent_costs` map is
    display-only and is reused for its GROUPING only, never as the cost
    authority.
    """
    query = _conversation_query()
    facts = bundle.facts
    bounds = [_iso_sql(scope.window_start), _iso_sql(scope.window_end)]
    sessions = _claude_window_session_ids(scope, bundle, conversations)
    if not sessions:
        return _unestablished()
    assistant_rows: dict[str, list] = {}
    for chunk in _chunks(sessions):
        sql = _CLAUDE_WINDOW_SUBAGENT_SQL.format(
            placeholders=_placeholders(len(chunk)))
        for row in _execute(conversations, sql, list(chunk) + bounds):
            assistant_rows.setdefault(str(row["session_id"]), []).append(row)
    bucket_of: dict[tuple, set] = {}
    for session, rows in assistant_rows.items():
        # The same `(session_id, uuid)` deduplication the canonical fold
        # applies. Without it a row retained under two source paths puts two
        # buckets in one turn key, the key resolves to neither, and a
        # correctly attributable entry is published as unallocated.
        #
        # The uuid is read RAW, the way the fold reads it. `uuid` is nullable,
        # so coercing through `str` would collapse a null and a literal
        # `"None"` body onto one key here and onto two inside the fold — one
        # rule with two statements again.
        for row in query.dedupe_claude_uuid_rows(
                rows, uuid_of=lambda r: _row_field(r, "uuid")):
            subagent = query._subagent_key(row["source_path"])
            if subagent is None:
                continue
            bucket_of.setdefault((row["msg_id"], row["req_id"]), set()).add(
                (session, subagent))
    buckets_by_parent: dict[str, set] = {}
    for buckets in bucket_of.values():
        if len(buckets) != 1:
            continue                        # not resolvable to one bucket
        session, subagent = next(iter(buckets))
        buckets_by_parent.setdefault(session, set()).add(subagent)
    qualifying_parents = {
        session for session, buckets in buckets_by_parent.items()
        if len(buckets) >= kernel.DIAGNOSIS_MIN_SUBAGENT_BUCKETS
    }

    known = set(sessions)
    candidates = [entry for entry in facts.entries
                  if entry.session_key in known]
    allocated: list = []
    unallocated: list = []
    bucket_rows: dict[tuple, list] = {}
    for entry in candidates:
        buckets = bucket_of.get((entry.msg_id, entry.req_id))
        if buckets is not None and len(buckets) == 1:
            session, subagent = next(iter(buckets))
            if session in qualifying_parents:
                allocated.append(entry)
                bucket_rows.setdefault((session, subagent), []).append(entry)
            continue
        if query._subagent_key(entry.source_path) is not None:
            # A subagent-shaped accounting row that could not join exactly
            # once to a normalized bucket. Its dollars are neither folded in
            # nor discarded — they are published as `unallocatedUsd`.
            unallocated.append(entry)
    identified = sum(len(buckets_by_parent[session])
                     for session in qualifying_parents)
    bucket_usd = {key: stable_sum(entry.cost_usd for entry in rows)
                  for key, rows in bucket_rows.items()}
    return _fanout_evidence(
        facts, candidates=candidates, unallocated=unallocated,
        allocated=allocated, bucket_usd=bucket_usd, identified=identified,
        subset=False,
    )


def _evaluate_codex_fanout(scope: DiagnosisScope,
                           bundle: StoreBundle) -> _S3Evaluation:
    """Delegated Codex work, grouped by the resolved parent thread.

    The predicate is the delegation ORIGIN CATEGORY, matched from
    `root_thread_id`; `_is_fork` compares `parent_thread_id` against
    `native_thread_id` and is a separate concept. A parent resolves only on
    exactly one non-self match under the COMPLETE identity, because the
    table's uniqueness is the triple and the existing resolution queries the
    weaker pair.
    """
    facts = bundle.facts
    by_conversation, _labels = facts.grouped_entries["session"]
    scoped = sorted(key for key in by_conversation if key)
    if not scoped:
        return _unestablished()
    # The thread rows come from the `cache` component that read and DIGESTED
    # them, so a changed origin category or parent pointer moves the published
    # identifier as well as the verdict.
    fanout = resolve_codex_fanout(list(bundle.codex_threads), scoped)
    groups: dict[tuple, set] = {}
    for key, group in fanout.parents.items():
        groups.setdefault(group, set()).add(key)
    qualifying_groups = {
        group for group, children in groups.items()
        if len(children) >= kernel.DIAGNOSIS_MIN_SUBAGENT_BUCKETS
    }
    qualifying_children = {
        key for key, group in fanout.parents.items()
        if group in qualifying_groups
    }
    unresolvable = set(fanout.unallocated)
    unreadable = set(fanout.ambiguous)

    candidates = facts.entries
    allocated: list = []
    unallocated: list = []
    ambiguous: list = []
    bucket_rows: dict[str, list] = {}
    for key, session_entries in by_conversation.items():
        if key in unreadable:
            # The origin category could not be read at all, so this is not
            # known subagent spend and its dollars are published nowhere.
            ambiguous.extend(session_entries)
            continue
        if key in unresolvable:
            unallocated.extend(session_entries)
            continue
        group = fanout.parents.get(key)
        if group is None or group not in qualifying_groups:
            continue
        matching = [entry for entry in session_entries
                    if entry.root_key == group[0]]
        if len(matching) != len(session_entries):
            # The join is `(source_root_key, conversation_key)`, and a key
            # that reached a different root is not this child's spend.
            unallocated.extend(entry for entry in session_entries
                               if entry.root_key != group[0])
        if matching:
            allocated.extend(matching)
            bucket_rows[key] = matching
    bucket_usd = {key: stable_sum(entry.cost_usd for entry in rows)
                  for key, rows in bucket_rows.items()}
    return _fanout_evidence(
        facts, candidates=candidates, unallocated=unallocated,
        allocated=allocated, bucket_usd=bucket_usd,
        identified=len(qualifying_children), subset=True,
        ambiguous=ambiguous,
    )


# --- dispatch and memoization -------------------------------------------

_S3_EVALUATORS = {
    "cache_churn": _evaluate_cache_churn,
    "short_high_context": _evaluate_short_high_context,
    "subagent_fanout": _evaluate_subagent_fanout,
}

# The classes whose evaluation READS the conversations store. Everything else
# an S3 class needs lives in `cache.db`, which every plan already opens.
_S3_NEEDS_CONVERSATIONS = {
    "cache_churn": ("claude",),
    "short_high_context": ("claude", "codex"),
    "subagent_fanout": ("claude",),
}


def _evaluate_s3_classes(scope: DiagnosisScope, bundle: StoreBundle, *,
                         conversations: sqlite3.Connection | None,
                         only: "Collection[str] | None" = None
                         ) -> dict[str, _S3Evaluation]:
    """Evaluate every S3 class the POLICY plan permits, once.

    A class the plan withholds or marks `not_applicable` is not evaluated at
    all — a denied plan never reads the store it was denied. A permitted class
    whose store is absent is `established=False`, which the loader renders as
    `withheld / signal_unavailable`.

    `only` narrows the set further, so the lazy fallback in `_s3_evaluations`
    re-runs nothing a memo already decided.
    """
    results: dict[str, _S3Evaluation] = {}
    for kind, evaluator in _S3_EVALUATORS.items():
        if only is not None and kind not in only:
            continue
        decision = bundle.plan.mode_for(kind)
        if decision is None or decision.mode != kernel.ClassMode.MEASURE.value:
            continue
        needs = scope.source in _S3_NEEDS_CONVERSATIONS.get(kind, ())
        if needs and conversations is None:
            results[kind] = _unestablished()
            continue
        try:
            results[kind] = evaluator(scope, bundle, conversations)
        except Exception as exc:
            # An evaluator that raises must not render as healthy, and must
            # not take down the classes that never read a transcript.
            #
            # The cause splits by KIND rather than by layer, so this handler
            # and the component-level one state one principle through
            # `_is_store_failure`: a failure a STORE actually produces keeps
            # its store-shaped cause, and anything else — including a
            # `sqlite3` error naming a schema object the store was already
            # observed to carry — is a defect in our own code. Reporting a
            # defect as an absent signal makes it indistinguishable from a
            # store that genuinely holds nothing, and only one of those is
            # worth fixing.
            results[kind] = _S3Evaluation(
                established=False,
                failure=(None if _is_store_failure(exc)
                         else type(exc).__name__))
    return results


def _s3_evaluations(bundle: StoreBundle,
                    scope: DiagnosisScope) -> dict[str, _S3Evaluation]:
    """The memo the loaders read.

    Populated during establishment when the plan opened the conversations
    store, so the digest and the rows describe ONE evaluation rather than two.
    A plan that opened no conversations connection — a denied Codex route, for
    instance — still evaluates its `cache.db`-only classes, lazily, here.

    The fallback keys on WHICH CLASSES the memo covers rather than on whether
    a memo exists. A conversations read that raises leaves the memo empty
    rather than absent, and an empty mapping is still not `None`: a
    `None` test would skip the fallback and withhold Codex `subagent_fanout`,
    which reads only `cache.db` and which the Codex table of Section 3
    requires to measure whether that store is absent or unreadable. (C21
    governs the accounting-store overlay and denial precedence, which is a
    different rule.) A class the memo already covers is never re-evaluated,
    so the digest and the rows still describe one evaluation of everything
    the conversations read decided.
    """
    memo = dict(bundle.s3_evaluations or {})
    expected = set()
    for kind in _S3_EVALUATORS:
        decision = bundle.plan.mode_for(kind)
        if (decision is not None
                and decision.mode == kernel.ClassMode.MEASURE.value):
            expected.add(kind)
    missing = expected - set(memo)
    if missing:
        # Guarded by MEMBERSHIP rather than by `setdefault`, so the sentence
        # above is literally true: a class the memo already decided is not
        # evaluated a second time and its result discarded.
        memo.update(_evaluate_s3_classes(scope, bundle, conversations=None,
                                         only=missing))
    bundle.s3_evaluations = memo
    return memo


def _read_conversations_component(scope: DiagnosisScope,
                                  bundle: StoreBundle) -> tuple[list, Any]:
    """Run the conversation-derived evaluators once, and digest what they
    publish — nothing else.

    `generationId` is on the wire, so a digest computed over raw bodies,
    `blocks_json`, `content_digest` or payload JSON would let any caller test
    transcript-content equality, while locating a compaction requires parsing
    exactly those blocks. The governing rule is therefore narrower and
    attainable: the component is digested over exactly the derived facts the
    report itself publishes or aggregates. Under that rule the digest is an
    equality oracle only for facts the response body already discloses, so it
    adds no exposure — and a change to semantically INERT text moves neither
    the digest nor the body.

    The evaluation is performed HERE rather than in the loaders, so the digest
    and the rows describe one evaluation rather than two.
    """
    conn = bundle.connection("conversations")
    try:
        # `EstablishmentFailure` from an unrecognised source cannot reach
        # here: `_read_with_probe` always probes first, and the probe's
        # narrower `except sqlite3.Error` lets that refusal through to end the
        # report. Stated because this site's `except Exception` would
        # otherwise absorb it, contradicting the gate's own docstring.
        #
        # INSIDE the backstop, because the gate itself raises: it re-raises
        # every `OperationalError` that does not name a missing schema object,
        # and `database is locked` — the `SQLITE_BUSY_SNAPSHOT` shape this
        # store produces while `_conversation_sync_pass` commits — is exactly
        # that. Outside the handler that raise reached no classifier anywhere
        # in the adapter, in the route or in `cmd_explain`, and took down all
        # seven classes.
        incomplete = bundle.conversations_schema_gap()
        if incomplete is not None:
            # A store that predates a column these statements select is a
            # store this read cannot answer from, which is the Section 3
            # `absent or unreadable` cell. Deciding it HERE, by asking the
            # store, is what lets a schema-object failure past this point be
            # reported as our own defect rather than guessed at from an
            # exception message. The digest row names one of our own table
            # names and no store content.
            return [("component_schema_incomplete", incomplete)], {
                "store_readable": False, "evaluations": {}, "failure": None,
            }
        evaluations = _evaluate_s3_classes(scope, bundle, conversations=conn)
        # INSIDE the backstop. The extension is one loop over the evaluations
        # the call above produced, and it cannot raise today, but a raise here
        # would take down all seven classes — including the four accounting
        # ones that never read a transcript — where the whole point of this
        # handler is that they keep answering.
        rows: list[tuple] = []
        for kind in sorted(evaluations):
            if scope.source not in _S3_NEEDS_CONVERSATIONS.get(kind, ()):
                # This class read no conversation bytes, so its facts belong
                # to the `cache` component rather than to this one.
                continue
            rows.extend(evaluations[kind].digest_rows(kind))
    except Exception as exc:
        # `except Exception`, not `except sqlite3.Error`. An older
        # conversations.db carries neither table, and a `blocks_json` array
        # holding a bare string raises `AttributeError` out of body
        # reconstruction — which is the NORMAL path for a compaction row,
        # because compaction blanks `text`.
        #
        # The cause splits by KIND, exactly as the per-evaluator handler
        # does and through the same `_is_store_failure`: a failure a store
        # actually produces reports `signal_unavailable`, and anything else
        # is a defect in our own code and reports `calculation_failed`.
        # Neither is `provider_unavailable` — the accounting store is fine
        # and the four accounting classes still answer.
        store_readable = not _is_store_failure(exc)
        return [("component_failed", type(exc).__name__)], {
            "store_readable": store_readable, "evaluations": {},
            "failure": (None if _is_store_failure(exc)
                        else type(exc).__name__),
        }
    return rows, {"store_readable": True, "evaluations": evaluations,
                  "failure": None}


def establish_generation(scope: DiagnosisScope,
                         plan: "kernel.ExecutionPlan") -> GenerationVector:
    """Establish the version vector alone, closing the stores afterwards.

    The vector this returns binds the CURRENT window only. The published
    vector binds both windows and is assembled in `build_provider_diagnosis`,
    which is the only place that holds the baseline bundle.

    `plan` is REQUIRED, and for the same reason `StoreBundle` requires it: the
    vector describes exactly the components the plan opened, so a default plan
    would publish a digest over stores this request never authorized.
    """
    with StoreBundle(scope, plan) as bundle:
        _establish(scope, bundle)
        assert bundle.vector is not None
        return bundle.vector


def _establish(scope: DiagnosisScope, bundle: StoreBundle) -> StoreBundle:
    """Probe, read, re-probe; re-read once on a divergence, then refuse.

    A second divergence is `generation_incoherent` rather than a third
    attempt, because a component that moves twice while we read it is a
    component under active write, and publishing a digest over it would
    describe a state that never existed as a whole.
    """
    rows: dict[str, list] = {}
    payloads: dict[str, Any] = {}

    def _read_with_probe(component: str) -> None:
        for attempt in (0, 1):
            before = _probe_component(component, bundle)
            component_rows, payload = _read_component(component, scope, bundle)
            after = _probe_component(component, bundle)
            if before == after:
                rows[component] = list(component_rows)
                payloads[component] = payload
                return
            if attempt == 1:
                raise EstablishmentFailure(
                    EstablishmentError.GENERATION_INCOHERENT.value,
                    f"the {component} component changed twice while it was read",
                )

    for component in _UNCONDITIONAL_COMPONENTS:
        _read_with_probe(component)
    cache_payload = payloads.get("cache") or {}
    stats_payload = payloads.get("stats") or {}
    # The cache component already hashes every accounting row read for this
    # exact provider/window under its probe pair. Re-hashing a second,
    # lossy projection of the same 150K-entry population for the denominator
    # identifier added CPU and no independent coherence. Domain-separate the
    # established component digest so the public population identifier stays
    # opaque and remains distinct from the generation component value.
    cache_digest = _digest_rows(rows["cache"])
    population_digest = hashlib.sha256(
        b"diagnosis-population\x00" + cache_digest.encode()).hexdigest()[:32]
    # The accounting facts are assembled BEFORE the conversations component,
    # because that component digests the facts the three conversation-derived
    # evaluators publish and every one of them divides transcript structure
    # against this accounting population.
    bundle.codex_threads = tuple(cache_payload.get("codex_threads") or ())
    bundle.facts = RawFacts(
        entries=cache_payload.get("entries", ()),
        blocks=stats_payload.get("blocks", ()),
        retained_start=cache_payload.get("retained_start"),
        retained_end=cache_payload.get("retained_end"),
        store_horizon=stats_payload.get("store_horizon"),
        unavailable_cause=cache_payload.get("unavailable_cause"),
        population_digest_override=population_digest,
    )
    # A class denied in plan stage 1 is SETTLED, so the store it would have
    # needed is never opened, probed or digested. The conversations component
    # is therefore established only when the plan asks for it.
    if bundle.plan.requires_conversations():
        try:
            bundle.connection("conversations")
        except EstablishmentFailure as exc:
            if exc.code != EstablishmentError.STORE_UNAVAILABLE.value:
                raise
            # An AUTHORIZED open that failed. Only the classes that needed the
            # store are withheld, as `signal_unavailable`, so this never
            # reaches `unreadable_store_is_terminal` and never turns an
            # answered report into exit 3.
            bundle.conversations_available = False
        else:
            _read_with_probe("conversations")
            payload = payloads.get("conversations") or {}
            # `store_readable` is a statement about the STORE, which is what
            # the plan's `signal_unavailable` cell describes. A `sqlite3.Error`
            # out of the read clears it; a defect in our own code does not,
            # because the store was readable and the failure is ours.
            bundle.conversations_available = bool(payload.get("store_readable"))
            # The evaluation the component just digested IS the one the
            # loaders publish. Re-running it there would read the store twice
            # and could describe a different state from the digest.
            bundle.s3_evaluations = payload.get("evaluations")
            if payload.get("failure") is not None:
                # Carry the component's own cause forward per class, so a
                # defect reports `calculation_failed` rather than being
                # flattened into "the signal could not be established".
                bundle.s3_evaluations = {
                    kind: _S3Evaluation(established=False,
                                        failure=payload["failure"])
                    for kind in _S3_EVALUATORS
                }
    bundle.component_rows = rows
    bundle.vector = GenerationVector(
        stats=_digest_rows(rows["stats"]),
        cache=cache_digest,
        configuration=_digest_rows(rows["configuration"]),
        conversations=(_digest_rows(rows["conversations"])
                       if "conversations" in rows else None),
    )
    return bundle


# --- coverage -----------------------------------------------------------

_IDENTITY_FLAGS = {
    "project": "project_identity_resolved",
    "session": "session_identity_resolved",
}


def _identity_resolved(entry: AccountingEntry, identity_kind: str) -> bool:
    """Whether THIS class's subject identity resolved for this entry.

    `model_mix` names its subject by the model string and `five_hour_bursts`
    by a provider-native block an entry only reaches by matching it, so both
    resolve by construction over their own attributed subset.
    """
    flag = _IDENTITY_FLAGS.get(identity_kind)
    if flag is None:
        return bool(entry.model) if identity_kind == "model" else True
    return bool(getattr(entry, flag))


def _coverage_for(scope: DiagnosisScope, facts: RawFacts,
                  *, attributed: Sequence[AccountingEntry],
                  identity_kind: str = "model",
                  gap_codes: Sequence[str] = ()) -> PopulationCoverage:
    """Coverage for one class, measured over that class's own population.

    `countCoverage` and `usdCoverage` are by definition ratios of the class's
    attributed subset to the whole provider population, and stay so.
    `identityCoverage` and `pricingCoverage` are properties OF the attributed
    subset and are computed over it — publishing the provider figure on every
    class made `session_concentration` report an identity figure derived from
    project resolution while its own subjects had resolved perfectly.
    `retentionCoverage` is genuinely provider-scoped: it compares the store's
    retained range with the requested window and has no per-class meaning.
    """
    total_usd = facts.total_usd
    total_count = len(facts.entries)
    observed_start, observed_end = facts.observed_range
    attributed_count = len(attributed)
    if attributed is facts.entries:
        attributed_usd = total_usd
        identity_resolved = facts.coverage_counts.get(identity_kind,
                                                       attributed_count)
        priced_without_fallback = facts.coverage_counts["priced"]
    else:
        attributed_usd = stable_sum(e.cost_usd for e in attributed)
        identity_resolved = sum(1 for e in attributed
                                if _identity_resolved(e, identity_kind))
        priced_without_fallback = sum(
            1 for e in attributed if not e.is_fallback_pricing
        )
    return PopulationCoverage(
        requested_start=scope.window_start_iso,
        requested_end=scope.window_end_iso,
        observed_start=_iso_z(observed_start) if observed_start else None,
        observed_end=_iso_z(observed_end) if observed_end else None,
        count_coverage=(attributed_count / total_count) if total_count else None,
        usd_coverage=(attributed_usd / total_usd) if total_usd > 0 else None,
        identity_coverage=((identity_resolved / attributed_count)
                           if attributed_count else None),
        retention_coverage=_retention_coverage(scope, facts),
        pricing_coverage=((priced_without_fallback / attributed_count)
                          if attributed_count else None),
        support_units=attributed_count,
        gap_codes=tuple(gap_codes),
    )


# Handed out BY REFERENCE to every S3 coverage object, so it is wrapped rather
# than copied: a plain dict would let one caller mutate the map every other
# coverage object is publishing.
_S3_DIMENSIONS: Mapping[str, str] = kernel.FrozenDict({
    "countCoverage": "evaluated entries / all in-window provider entries",
    "usdCoverage": "evaluated USD / all in-window provider USD",
    "identityCoverage": "evaluated entries whose subject identity resolved "
                        "/ evaluated entries",
    "pricingCoverage": "evaluated entries priced from their own row "
                       "/ evaluated entries",
    "retentionCoverage": "the fraction of the requested window the store "
                         "could answer for",
    "evaluabilityCoverage": "entries whose predicate could be decided "
                            "/ entries eligible for an attempted evaluation",
    "supportUnits": "evaluated entries",
})


def s3_coverage(scope: DiagnosisScope, facts: RawFacts, *,
                evaluated: Sequence[AccountingEntry],
                candidate_count: int,
                identity_kind: str = "model",
                gap_codes: Sequence[str] = ()) -> PopulationCoverage:
    """Coverage for one conversation-derived class, over three populations.

    S2's `_coverage_for` is NOT modified and keeps serving the four accounting
    classes, so every S2 coverage block stays byte-identical.

    The three populations are distinct and each field names its own. The
    CANDIDATE population is every in-window priced entry eligible for an
    attempted evaluation, INCLUDING the ones that could not be decided —
    defining a candidate as a row the predicate "was applied to" excludes by
    construction the very rows the dimension exists to count. The EVALUATED
    population is the candidates whose predicate could be decided. The
    QUALIFYING population is the evaluated entries the predicate matched.

    `support_units` counts the evaluated population and never the qualifying
    set, because counting qualifiers would make confidence rise precisely as
    the problem worsens.
    """
    total_usd = facts.total_usd
    total_count = len(facts.entries)
    observed_start, observed_end = facts.observed_range
    evaluated_usd = stable_sum(e.cost_usd for e in evaluated)
    evaluated_count = len(evaluated)
    identity_resolved = sum(1 for e in evaluated
                            if _identity_resolved(e, identity_kind))
    priced_without_fallback = sum(
        1 for e in evaluated if not e.is_fallback_pricing
    )
    return PopulationCoverage(
        requested_start=scope.window_start_iso,
        requested_end=scope.window_end_iso,
        observed_start=_iso_z(observed_start) if observed_start else None,
        observed_end=_iso_z(observed_end) if observed_end else None,
        count_coverage=(evaluated_count / total_count) if total_count else None,
        usd_coverage=(evaluated_usd / total_usd) if total_usd > 0 else None,
        identity_coverage=((identity_resolved / evaluated_count)
                           if evaluated_count else None),
        retention_coverage=_retention_coverage(scope, facts),
        pricing_coverage=((priced_without_fallback / evaluated_count)
                          if evaluated_count else None),
        evaluability_coverage=((evaluated_count / candidate_count)
                               if candidate_count else None),
        dimensions=_S3_DIMENSIONS,
        support_units=evaluated_count,
        gap_codes=tuple(gap_codes),
    )


def _preempted_coverage(scope: DiagnosisScope, facts: RawFacts,
                        *, cause: str,
                        gap_codes: Sequence[str] = ()) -> PopulationCoverage:
    """The coverage of a class that was never evaluated.

    A provider-wide cause is decided before any class shapes its subjects, so
    the class attributed nothing — and `0` is a measurement over an attributed
    population, not the absence of one. Publishing `supportUnits: 0` and
    `usdCoverage: 0.0` for a 60-entry window states that none of its dollars
    are covered, which is false; the truth is that this class never measured
    them. The unmeasurable dimensions are therefore absent, and `gap_codes`
    names the cause that preempted them.

    `retentionCoverage` stays, because it is provider-scoped: it compares the
    store's retained range with the requested window and is measured before
    any class exists. The observed bounds stay for the same reason.

    Both mechanisms that produce `provider_unavailable` build their coverage
    here, which is what makes them publish the same shape rather than two
    shapes that happen to agree on most keys.
    """
    observed = [e.timestamp for e in facts.entries]
    return PopulationCoverage(
        requested_start=scope.window_start_iso,
        requested_end=scope.window_end_iso,
        observed_start=_iso_z(min(observed)) if observed else None,
        observed_end=_iso_z(max(observed)) if observed else None,
        retention_coverage=_retention_coverage(scope, facts),
        support_units=None,
        # The cause first, then whatever the class knows about WHY it could
        # not be established. A class withheld because every spending session
        # exhausted its seed share must still say `scan_budget_exhausted`, or
        # the reader is left with a cause and no reason.
        gap_codes=(cause,) + tuple(
            code for code in gap_codes if code != cause),
    )


def _retention_coverage(scope: DiagnosisScope,
                        facts: RawFacts) -> float | None:
    """The fraction of the requested window the store could answer for.

    Retention is a statement about the LOW side of the window, and the
    earliest retained accounting row alone cannot make it: a fresh install two
    days into the subscription week has exactly the same shape as a store
    pruned five days back, and reading that shape as pruning withheld every
    class of a brand-new user's very first `cctally explain`.

    The discriminator is the install's own observation horizon, which stats.db
    records independently of the accounting rows. An install that was already
    observing before the window and holds no accounting rows for the earlier
    part has been pruned. An install whose horizon begins inside the window
    never covered the earlier part at all, so that interval is excluded from
    the denominator rather than counted against the store.

    A quiet tail is not a retention failure — no rows after the last piece of
    work is the ordinary state of every store — so the high side only decides
    whether the retained range overlaps the window at all.

    **Without the horizon there is no discriminator, so there is no figure.**
    Neither table that carries it is written by the accounting path:
    `five_hour_blocks` is written by `record-usage` from the status-line hook,
    and `quota_window_blocks` needs the optional Codex hooks or a rollout
    ingest. An install that never wired either has no horizon at all, and
    falling back to the requested window start restores exactly the rule the
    horizon replaced — a young store reported as pruned, with a definite
    figure. An unmeasurable dimension is absent rather than derived from the
    very bound it replaced. The consequence is stated and accepted: a store
    with no provider-native blocks never reports `stale_evidence`, because
    pruning and youth genuinely cannot be told apart without that signal.
    """
    if facts.retained_start is None or facts.retained_end is None:
        return None
    horizon = facts.store_horizon
    if horizon is None:
        return None
    answerable_start = scope.window_start
    if horizon > scope.window_start:
        answerable_start = min(horizon, scope.window_end)
    span = (scope.window_end - answerable_start).total_seconds()
    if span <= 0:
        # The install began observing at or after the window end, so there is
        # no interval it could have answered for and no claim to make.
        return None
    low = max(answerable_start, facts.retained_start)
    high = min(scope.window_end, facts.retained_end)
    if high <= low:
        return 0.0
    return min(1.0, (scope.window_end - low).total_seconds() / span)


def _provider_withheld_cause(scope: DiagnosisScope,
                             facts: RawFacts) -> str | None:
    """The provider-wide withheld cause, in the published precedence order.

    These are conditions of the whole population rather than of one class, so
    they are decided once and applied to every class. Deciding them per class
    would let a class with thin support report `insufficient_population` while
    the real reason was that the window predates what the store retains.
    """
    if facts.unavailable_cause is not None:
        return facts.unavailable_cause
    retention = _retention_coverage(scope, facts)
    if not facts.entries:
        if retention is not None and retention <= 0.0:
            return WithheldCause.RETAINED_RANGE_MISMATCH.value
        return None
    if facts.total_usd <= 0.0:
        # A total of zero has two causes and they are not the same statement.
        # Nothing in the population could be priced is `pricing_unavailable`.
        # A priceable population that genuinely cost nothing is not a withheld
        # measurement at all: the answer is zero, and every class then withholds
        # on the ordinary "no dollars to divide by" rule.
        if not any(e.pricing_resolved for e in facts.entries):
            return WithheldCause.PRICING_UNAVAILABLE.value
        return None
    # The same slack `support_shortfall` applies to USD coverage against the
    # same constant, and the kernel's constant rather than a second copy of it.
    # Retention is a ratio of two independently computed spans, so a genuinely
    # half-retained window computes as 0.49999999999999994 and a bare `<`
    # withheld the WHOLE provider as `stale_evidence`.
    if (retention is not None
            and retention < kernel.WITHHOLD_MIN_COVERAGE
            - kernel.COVERAGE_EPSILON):
        return WithheldCause.STALE_EVIDENCE.value
    return None


# --- per-class fact loading --------------------------------------------

@dataclass(frozen=True)
class ClassFacts:
    contributor_class: str
    subjects: tuple[SubjectFacts, ...]
    population: PopulationCoverage
    total_priced_usd: float
    not_applicable: bool = False
    preempting_cause: str | None = None
    # True when this class's predicate was actually evaluated over an
    # adequately covered population. With no subjects that is the healthy
    # inverse — `no_contributor` — rather than a support shortfall.
    predicate_evaluated: bool = False
    # Class-specific evidence, keyed by camelCase wire name. Attached to every
    # row `classify_class` builds for this class.
    evidence: Mapping[str, Any] | None = None


def _subject_from_group(key: str, label: str,
                        entries: Sequence[AccountingEntry],
                        baseline_share: float | None,
                        baseline_code: str | None,
                        next_step: str | None = None,
                        qualifications: Sequence[str] = ()) -> SubjectFacts:
    return SubjectFacts(
        subject_key=key,
        subject_label=label,
        observed_usd=stable_sum(e.cost_usd for e in entries),
        priced_entry_count=len(entries),
        is_fallback_pricing=any(e.is_fallback_pricing for e in entries),
        baseline_share=baseline_share,
        baseline_code=baseline_code,
        next_step=next_step,
        qualifications=tuple(qualifications),
    )


def _group_entries(entries: Sequence[AccountingEntry], kind: str):
    grouped: dict[str, list[AccountingEntry]] = {}
    labels: dict[str, str] = {}
    for entry in entries:
        if kind == "model":
            key, label = entry.model or "(unknown)", entry.model or "(unknown)"
        elif kind == "project":
            key, label = entry.project_key, entry.project_label
        else:
            key, label = entry.session_key, entry.session_label
        grouped.setdefault(key, []).append(entry)
        labels.setdefault(key, label)
    return grouped, labels


def _assign_entries_to_blocks(entries: Sequence[AccountingEntry],
                              blocks: Sequence[NativeBlock]):
    """Join accounting entries to provider-native blocks within a pool.

    An entry joins only to a window of a compatible logical pool, and to at
    most one window, so no entry is ever counted in two pools. An entry
    matching no compatible window is a coverage gap, reported through
    `unmatched`, and appears in no block.
    """
    assigned: dict[str, list[AccountingEntry]] = {}
    unmatched: list[AccountingEntry] = []
    for entry in entries:
        match = None
        for block in blocks:
            if block.root_key != entry.root_key:
                continue
            if block.pool != entry.pool:
                continue
            if block.start_at <= entry.timestamp < block.end_at:
                match = block
                break
        if match is None:
            unmatched.append(entry)
            continue
        assigned.setdefault(match.key, []).append(entry)
    return assigned, unmatched


def _baseline_lookup(bundle: StoreBundle, scope: DiagnosisScope,
                     spec: ContributorSpec):
    """The class-specific comparator over the immediately preceding window.

    Returns `(lookup, code)`. `lookup(subject_key, pool) -> float` answers for
    one of this window's subjects; `code` is set only when the baseline could
    not be established at all, in which case `lookup` is `None`.

    The comparators are the ones the spec names, and two of them are
    deliberately not per-key lookups. A 5-hour block's key embeds its start
    instant and a session key is minted per session, so neither can appear in
    both windows and a per-key lookup would miss every single time. The
    comparator for `five_hour_bursts` is therefore the maximum
    provider-native block share within the subject's own Codex pool, and for
    `session_concentration` it is the maximum single-session share.

    A subject the baseline does not contain, in a baseline that WAS
    established, compares to zero rather than being withheld: a model,
    project or session that appears this week and did not exist last week is
    the most informative case the baseline has.
    """
    baseline = bundle.baseline
    if baseline is None or not baseline.entries:
        return None, kernel.BaselineOutcome.BASELINE_INSUFFICIENT.value
    total = baseline.total_usd
    if total <= 0:
        return None, kernel.BaselineOutcome.BASELINE_INSUFFICIENT.value

    if spec.kind == "five_hour_bursts":
        assigned, _unmatched = _assign_entries_to_blocks(
            baseline.entries, baseline.blocks
        )
        pool_of = {b.key: (b.pool or "standard") for b in baseline.blocks}
        best_by_pool: dict[str, float] = {}
        for key, rows in assigned.items():
            pool = pool_of.get(key, "standard")
            share = stable_sum(e.cost_usd for e in rows) / total
            if share > best_by_pool.get(pool, 0.0):
                best_by_pool[pool] = share

        def _block_lookup(_key: str, pool: str | None) -> float:
            return best_by_pool.get(pool or "standard", 0.0)

        return _block_lookup, None

    if spec.kind == "session_concentration":
        grouped, _labels = baseline.grouped_entries["session"]
        top = max(
            (stable_sum(e.cost_usd for e in rows) / total
             for rows in grouped.values()),
            default=0.0,
        )

        def _session_lookup(_key: str, _pool: str | None) -> float:
            return top

        return _session_lookup, None

    kind = "model" if spec.kind == "model_mix" else "project"
    grouped, _labels = baseline.grouped_entries[kind]
    shares = {key: stable_sum(e.cost_usd for e in rows) / total
              for key, rows in grouped.items()}

    def _keyed_lookup(key: str, _pool: str | None) -> float:
        return shares.get(key, 0.0)

    return _keyed_lookup, None


def load_class_facts(bundle: StoreBundle, scope: DiagnosisScope,
                     spec: ContributorSpec) -> ClassFacts:
    """Shape one class's subjects and its coverage out of the loaded facts.

    A loader exception is not allowed to render as healthy: it is caught here
    and reported as `calculation_failed`, which the overall verdict then
    withholds on.
    """
    facts = bundle.facts
    provider_cause = _provider_withheld_cause(scope, facts)
    plan = kernel.establish_plan(
        bundle.plan,
        conversations_available=bundle.conversations_available,
        provider_cause=provider_cause,
    )
    decision = plan.mode_for(spec.kind)
    if decision is not None and decision.mode == kernel.ClassMode.NOT_APPLICABLE.value:
        # A capability statement rather than an availability one: it stays
        # true whatever the store did, so the provider overlay never reaches
        # it and `count_applicable` excludes it.
        return ClassFacts(
            spec.kind, (),
            _preempted_coverage(scope, facts,
                                cause=kernel.VerdictState.NOT_APPLICABLE.value),
            facts.total_usd, not_applicable=True,
        )
    if decision is not None and decision.mode == kernel.ClassMode.WITHHOLD.value:
        return ClassFacts(
            spec.kind, (),
            _preempted_coverage(scope, facts, cause=decision.cause),
            facts.total_usd, preempting_cause=decision.cause,
        )
    if provider_cause is not None:
        return ClassFacts(
            spec.kind, (),
            _preempted_coverage(scope, facts, cause=provider_cause),
            facts.total_usd, preempting_cause=provider_cause,
        )
    try:
        if spec.kind in _S3_CLASS_KINDS:
            return _load_s3_class_facts(bundle, scope, spec)
        return _load_class_facts_inner(bundle, scope, spec)
    except Exception:
        return ClassFacts(
            spec.kind, (),
            _preempted_coverage(scope, facts,
                                cause=WithheldCause.CALCULATION_FAILED.value),
            0.0, preempting_cause=WithheldCause.CALCULATION_FAILED.value,
        )


def _s3_next_step(scope: DiagnosisScope) -> str:
    """`cctally explain` over the same scope, with EXACT bounds.

    Transcript-free and valid in every configuration, and it points at exactly
    the population the row measured — which date grammar cannot express,
    because a window can begin and end at any instant.
    """
    parts = ["cctally", "explain", "--source", scope.source,
             "--start-at", scope.window_start_iso,
             "--end-at", scope.window_end_iso, "--json"]
    return " ".join(shlex.quote(part) for part in parts)


def _load_s3_class_facts(bundle: StoreBundle, scope: DiagnosisScope,
                         spec: ContributorSpec) -> ClassFacts:
    """One conversation-derived class's subjects, coverage and evidence.

    Under D-B the class contributes exactly ONE aggregate subject — the
    qualifying set — whose observed USD is that set's in-window retained cost.
    Members appear only as evidence fields, and the subject's key, kind and
    label are constants of the class rather than anything derived from a
    member.
    """
    facts = bundle.facts
    evaluation = _s3_evaluations(bundle, scope).get(spec.kind)
    if evaluation is None or not evaluation.established:
        cause = (WithheldCause.CALCULATION_FAILED.value
                 if evaluation is not None and evaluation.failure is not None
                 else WithheldCause.SIGNAL_UNAVAILABLE.value)
        return ClassFacts(
            spec.kind, (),
            _preempted_coverage(
                scope, facts, cause=cause,
                gap_codes=(evaluation.gap_codes if evaluation is not None
                           else ())),
            facts.total_usd, preempting_cause=cause,
        )
    population = s3_coverage(
        scope, facts, evaluated=evaluation.evaluated,
        candidate_count=evaluation.candidate_count,
        identity_kind=_identity_kind_for(spec),
        gap_codes=evaluation.gap_codes,
    )
    evidence = {
        name: (kernel.available(value.value, population, value.qualifications)
               if value.code is None
               else kernel.withheld(value.code, population,
                                    value.qualifications))
        for name, value in evaluation.evidence.items()
    }
    subjects: tuple[SubjectFacts, ...] = ()
    if evaluation.qualifying:
        subject_key, subject_label = _S3_SUBJECT[spec.kind]
        baseline_share = bundle.s3_baseline_shares.get(spec.kind)
        subjects = (SubjectFacts(
            subject_key=subject_key,
            subject_label=subject_label,
            observed_usd=evaluation.qualifying_usd,
            priced_entry_count=len(evaluation.qualifying),
            is_fallback_pricing=any(entry.is_fallback_pricing
                                    for entry in evaluation.qualifying),
            baseline_share=baseline_share,
            baseline_code=(None if baseline_share is not None else
                           kernel.BaselineOutcome.BASELINE_INSUFFICIENT.value),
            next_step=_s3_next_step(scope),
            qualifications=evaluation.qualifications,
        ),)
    # `predicate_evaluated` is what makes an evaluated predicate that matched
    # NOTHING report `no_contributor` rather than a support shortfall: it has
    # no subjects by RESULT, not by absence of data.
    return ClassFacts(spec.kind, subjects, population, facts.total_usd,
                      predicate_evaluated=True, evidence=evidence)


def _s3_baseline_shares(bundle: StoreBundle,
                        scope: DiagnosisScope) -> dict[str, float]:
    """Each S3 class's aggregate qualifying-cost share over ONE window.

    Called against the preceding-window bundle, which is why the two windows
    receive SEPARATE scan budgets: a heavy current read cannot starve the
    baseline and silently turn every baseline into `baseline_insufficient`.
    """
    total = bundle.facts.total_usd
    if total <= 0:
        return {}
    shares: dict[str, float] = {}
    for kind, evaluation in _s3_evaluations(bundle, scope).items():
        if not evaluation.established:
            continue
        share = evaluation.qualifying_usd / total
        if share <= 0 and len(evaluation.evaluated) < kernel.spec_for(
                kind).min_priced_entries:
            # Spec 2.4 permits a published zero only after an ADEQUATELY
            # evaluated zero-match. A baseline window holding three evaluated
            # entries and no match is not evidence that the class was absent
            # there, and publishing 0.0 for it states a comparison the store
            # cannot support. It is `baseline_insufficient` instead.
            continue
        shares[kind] = share
    return shares


_CLASS_IDENTITY_KIND = {
    "model_mix": "model",
    "project_concentration": "project",
    "session_concentration": "session",
    "five_hour_bursts": "block",
}

# The three conversation-derived classes. Named here rather than derived from
# `spec.subject_kind`, so a future accounting class that happened to publish
# an aggregate subject would not silently join them.
_S3_CLASS_KINDS = frozenset({"cache_churn", "short_high_context",
                             "subagent_fanout"})


def _identity_kind_for(spec: ContributorSpec) -> str:
    return _CLASS_IDENTITY_KIND.get(spec.kind, "model")


def _block_qualifications(block: NativeBlock,
                          scope: DiagnosisScope) -> tuple[str, ...]:
    """State an overlap rather than implying the block sits inside the window.

    A native block is loaded when it OVERLAPS the requested window, so a
    reported burst subject can carry a start instant before the window the
    report claims to explain. Its dollars are still window-scoped — only
    entries inside the window are ever assigned — so the honest fix is to say
    the block extends past the window rather than to clip a provider-native
    boundary the provider did not clip.
    """
    codes: list[str] = []
    if block.start_at < scope.window_start:
        codes.append("block_precedes_window")
    if block.end_at > scope.window_end:
        codes.append("block_exceeds_window")
    return tuple(codes)


def _load_class_facts_inner(bundle: StoreBundle, scope: DiagnosisScope,
                            spec: ContributorSpec) -> ClassFacts:
    facts = bundle.facts
    baseline_lookup, baseline_code = _baseline_lookup(bundle, scope, spec)

    def _baseline_for(key: str, pool: str | None) -> float | None:
        return None if baseline_lookup is None else baseline_lookup(key, pool)

    if spec.kind == "five_hour_bursts":
        assigned, unmatched = _assign_entries_to_blocks(
            facts.entries, facts.blocks
        )
        blocks_by_key = {b.key: b for b in facts.blocks}
        subjects = tuple(
            _subject_from_group(
                key,
                blocks_by_key[key].label if key in blocks_by_key else key,
                rows,
                _baseline_for(
                    key,
                    blocks_by_key[key].pool if key in blocks_by_key else None,
                ),
                baseline_code,
                (blocks_by_key[key].next_step or None
                 if key in blocks_by_key else None),
                (_block_qualifications(blocks_by_key[key], scope)
                 if key in blocks_by_key else ()),
            )
            for key, rows in assigned.items()
        )
        gap_codes = ("unmatched_pool_window",) if unmatched else ()
        attributed = [e for rows in assigned.values() for e in rows]
        population = _coverage_for(
            scope, facts, attributed=attributed, identity_kind="block",
            gap_codes=gap_codes,
        )
        return ClassFacts(spec.kind, subjects, population, facts.total_usd)

    kind = _identity_kind_for(spec)
    grouped, labels = facts.grouped_entries[kind]
    subjects = tuple(
        _subject_from_group(key, labels[key], rows,
                            _baseline_for(key, None), baseline_code)
        for key, rows in grouped.items()
    )
    attributed = facts.entries

    gap_codes: tuple[str, ...] = ()
    preempting: str | None = None
    if kind == "project" and facts.entries:
        resolved = sum(1 for e in facts.entries
                       if e.project_identity_resolved)
        if resolved == 0:
            # A wholly unattributed population is withheld rather than
            # reported under one synthetic bucket.
            preempting = WithheldCause.UNATTRIBUTED_EVIDENCE.value
            gap_codes = ("unresolved_project_identity",)
        elif resolved < len(facts.entries):
            gap_codes = ("unresolved_project_identity",)

    population = _coverage_for(
        scope, facts, attributed=attributed, identity_kind=kind,
        gap_codes=gap_codes,
    )
    return ClassFacts(spec.kind, subjects, population, facts.total_usd,
                      preempting_cause=preempting)


# --- the assembled diagnosis -------------------------------------------

def _population_digest(facts: RawFacts) -> str:
    return facts.population_digest


def _next_step_context(scope: DiagnosisScope) -> dict[str, str]:
    return {
        "source": scope.source,
        # Whether the provider's target subcommands can be told to reproduce
        # this diagnosis's own accounting. The rule lives in the kernel beside
        # the templates that consume it.
        "mode_flag": kernel.next_step_mode_flag(scope.source),
        "window_start_date": scope.window_start.astimezone(UTC).date().isoformat(),
        "window_end_date": scope.window_end.astimezone(UTC).date().isoformat(),
    }


def build_provider_diagnosis(scope: DiagnosisScope, *,
                             transcripts_visible: bool
                             ) -> kernel.ProviderResult:
    """One provider's whole answer, over its own read-only stores.

    `transcripts_visible` is plan stage 1's authorization input. It is always
    true on the CLI; the route passes its own gate value, and a plan denied
    here never opens, probes or digests `conversations.db`.

    It carries NO DEFAULT, deliberately. A `True` default is the shape of the
    R9 defect: the route called this without the argument, the default applied,
    and every request — including one the transcript gate would have denied —
    opened, probed and digested `conversations.db` and published an identifier
    bound to transcript-derived facts. Nothing failed, because a default cannot
    fail. An unthreaded call site now raises instead.
    """
    policy = kernel.resolve_policy_plan(scope.source,
                                        transcripts_visible=transcripts_visible)
    with StoreBundle(scope, policy) as bundle:
        _establish(scope, bundle)
        baseline_vector: GenerationVector | None = None
        preceding = scope.preceding()
        try:
            with StoreBundle(preceding, policy) as baseline_bundle:
                _establish(preceding, baseline_bundle)
                bundle.baseline = baseline_bundle.facts
                baseline_vector = baseline_bundle.vector
                # The S3 comparator is the preceding window's aggregate
                # qualifying-cost share, so it is computed while that bundle
                # is still open — its stores close with the block.
                bundle.s3_baseline_shares = _s3_baseline_shares(
                    baseline_bundle, preceding)
        except EstablishmentFailure:
            # A baseline that cannot be established withholds only the
            # baseline field. The observed-cost result still renders.
            bundle.baseline = None

        # The provider coverage and the provider-wide cause are established
        # BEFORE the denominator, because the denominator is itself an
        # `EvidenceField` withheld under that same ladder. A definite
        # `$0.00 of locally retained cost` printed above four classes withheld
        # as `pricing_unavailable` states a figure nothing in the population
        # could support.
        provider_cause = _provider_withheld_cause(scope, bundle.facts)
        if bundle.facts.unavailable_cause is not None:
            # The store could not be read, so there is no population to
            # measure. This is the SAME builder the unopenable-store path
            # uses, so the two mechanisms that produce `provider_unavailable`
            # cannot publish different coverage.
            coverage = _preempted_coverage(
                scope, bundle.facts, cause=bundle.facts.unavailable_cause
            )
        else:
            coverage = _coverage_for(
                scope, bundle.facts, attributed=bundle.facts.entries,
                identity_kind="model",
            )
        denominator_usd = (
            kernel.withheld(provider_cause, coverage)
            if provider_cause is not None
            else kernel.available(bundle.facts.total_usd, coverage)
        )
        denominator = Denominator(
            identity="totalExplainedRetainedCost",
            source=scope.source,
            account_key=scope.account_key,
            window_start=scope.window_start_iso,
            window_end=scope.window_end_iso,
            population_digest=_population_digest(bundle.facts),
            usd=denominator_usd,
        )
        context = _next_step_context(scope)
        classes: list[ClassResult] = []
        for spec in kernel.CONTRIBUTOR_REGISTRY:
            class_facts = load_class_facts(bundle, scope, spec)
            classes.append(kernel.classify_class(
                spec,
                subjects=class_facts.subjects,
                denominator=denominator,
                population=class_facts.population,
                preempting_cause=class_facts.preempting_cause,
                not_applicable=class_facts.not_applicable,
                next_step_context=context,
                predicate_evaluated=class_facts.predicate_evaluated,
                evidence=class_facts.evidence,
            ))
        assert bundle.vector is not None
        # Stage 2: the plan that is HASHED carries the availability outcomes,
        # not the intentions stage 1 resolved.
        established = kernel.establish_plan(
            policy,
            conversations_available=bundle.conversations_available,
            provider_cause=provider_cause,
        )
        # Each component digest binds BOTH windows, so a baseline that moved
        # while the current window stood still moves the identifier too.
        published = GenerationVector(
            stats=_digest_component_pair_from_digests(
                bundle.vector.stats,
                baseline_vector.stats if baseline_vector else None),
            cache=_digest_component_pair_from_digests(
                bundle.vector.cache,
                baseline_vector.cache if baseline_vector else None),
            configuration=_digest_component_pair_from_digests(
                bundle.vector.configuration,
                baseline_vector.configuration if baseline_vector else None),
            conversations=(
                _digest_component_pair_from_digests(
                    bundle.vector.conversations,
                    (baseline_vector.conversations
                     if baseline_vector else None))
                if bundle.vector.conversations is not None else None),
        )
        return kernel.build_provider_result(
            source=scope.source,
            account_key=scope.account_key,
            effective_speed=scope.effective_speed,
            denominator=denominator,
            classes=classes,
            generation=published,
            coverage=coverage,
            plan=established,
        )


def _build_provider_task(args) -> kernel.ProviderResult:
    """Pickle-safe provider build for the independent `all` branches."""
    scope, transcripts_visible = args
    try:
        result = build_provider_diagnosis(
            scope, transcripts_visible=transcripts_visible)
    except EstablishmentFailure as exc:
        if exc.code != EstablishmentError.STORE_UNAVAILABLE.value:
            raise
        result = _unavailable_provider_result(
            scope, detail=exc.message,
            transcripts_visible=transcripts_visible)
    return result


def _start_forked_provider(args) -> tuple[int, int]:
    """Fork before any provider store opens; return `(pid, read_fd)`."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            payload = (True, _build_provider_task(args))
        except EstablishmentFailure as exc:
            payload = (False, "establishment", exc.code, exc.message)
        except BaseException as exc:
            payload = (False, "unexpected", type(exc).__name__, str(exc))
        try:
            with os.fdopen(write_fd, "wb") as stream:
                pickle.dump(payload, stream, protocol=5)
        finally:
            os._exit(0)
    os.close(write_fd)
    return pid, read_fd


def _finish_forked_provider(worker: tuple[int, int]):
    pid, read_fd = worker
    try:
        with os.fdopen(read_fd, "rb") as stream:
            payload = pickle.load(stream)
    finally:
        _waited_pid, status = os.waitpid(pid, 0)
    if not payload[0]:
        if payload[1] == "establishment":
            raise EstablishmentFailure(payload[2], payload[3])
        raise RuntimeError(f"{payload[2]}: {payload[3]}")
    if status != 0:
        raise RuntimeError(f"diagnosis provider worker exited {status}")
    return payload[1]


def _spawn_worker_init(cctally_path: str,
                       store_paths: Mapping[str, str]) -> None:
    """Initialize a safe spawned/forkserver dashboard worker."""
    import importlib.machinery
    import importlib.util
    import pathlib

    # The canonical entry point is deliberately extensionless (`bin/cctally`),
    # so `spec_from_file_location` cannot infer a loader from its suffix.
    # Name the source loader explicitly; this executes the same file the
    # parent loaded instead of copying another import surface into the worker.
    loader = importlib.machinery.SourceFileLoader("cctally", cctally_path)
    spec = importlib.util.spec_from_loader("cctally", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load cctally worker from {cctally_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    spec.loader.exec_module(module)
    for name, raw in store_paths.items():
        setattr(_cctally_core, name, pathlib.Path(raw))


def _build_isolated_provider(args):
    """Run one provider in a portable, one-shot safe process worker."""
    import concurrent.futures
    import multiprocessing

    c = _cctally()
    paths = {
        name: str(getattr(_cctally_core, name))
        for name in ("CACHE_DB_PATH", "DB_PATH",
                     "CONVERSATIONS_DB_PATH", "CONFIG_PATH")
    }
    methods = multiprocessing.get_all_start_methods()
    method = "forkserver" if "forkserver" in methods else "spawn"
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context(method),
            initializer=_spawn_worker_init,
            initargs=(str(c.__file__), paths)) as executor:
        return executor.submit(_build_provider_task, args).result()


def build_diagnosis(scope: DiagnosisScope,
                    *, measured_at: dt.datetime | None = None,
                    transcripts_visible: bool) -> kernel.DiagnosisReport:
    """The one entry point both the CLI and the dashboard route call.

    Under `--source all` each provider gets its own scope, its own
    denominator and its own verdict. Nothing is ranked across providers and
    no denominator spans them.

    `transcripts_visible` is keyword-required for the reason stated on
    `build_provider_diagnosis`: a default that means "visible" cannot fail,
    and the one call site that forgot it was the dashboard route.
    """
    now = measured_at or dt.datetime.now(UTC)
    sources = (("claude", "codex") if scope.source == "all"
               else (scope.source,))
    def _build_one(source: str) -> kernel.ProviderResult:
        provider_scope = DiagnosisScope(
            source=source,
            account_key=scope.account_key,
            window_start=scope.window_start,
            window_end=scope.window_end,
            effective_speed=scope.effective_speed if source == "codex" else None,
            display_tz=scope.display_tz,
            label=scope.label,
        )
        try:
            return build_provider_diagnosis(
                provider_scope, transcripts_visible=transcripts_visible)
        except EstablishmentFailure as exc:
            # A store that cannot be OPENED withholds its provider rather than
            # ending the request, whether or not a second provider was
            # requested. The failure is not lost: `unreadable_store_is_terminal`
            # reads it back off the finished report, and the CLI exits 3 while
            # still printing the typed cause. Ending the request here instead
            # gave a single-provider user an exit code and no report, and a
            # two-provider user a report and exit 0, from one condition.
            if exc.code != EstablishmentError.STORE_UNAVAILABLE.value:
                raise
            return _unavailable_provider_result(
                provider_scope, detail=exc.message,
                transcripts_visible=transcripts_visible)

    if len(sources) == 1:
        results = [_build_one(sources[0])]
    else:
        # Provider stores, denominators and generation vectors are deliberately
        # independent under `all`. Reading them in series made the combined
        # latency the SUM of the Claude and Codex paths while adding no
        # consistency guarantee. `map` preserves the canonical Claude/Codex
        # result order even when Codex finishes first.
        tasks = [(
            DiagnosisScope(
                source=source,
                account_key=scope.account_key,
                window_start=scope.window_start,
                window_end=scope.window_end,
                effective_speed=(scope.effective_speed
                                 if source == "codex" else None),
                display_tz=scope.display_tz,
                label=scope.label,
            ), transcripts_visible,
        ) for source in sources]
        # Keep Claude in the already-loaded parent and isolate only the much
        # larger Codex fold. This retains true CPU overlap without paying to
        # fork and pickle both branches; canonical order remains Claude then
        # Codex regardless of which finishes first.
        # Fork before opening either provider's stores. The child owns Codex;
        # the parent builds Claude while it runs, then reads one compact result.
        if (threading.current_thread() is threading.main_thread()
                and hasattr(os, "fork")):
            codex_worker = _start_forked_provider(tasks[1])
            try:
                claude_result = _build_one("claude")
            except BaseException:
                # Drain the pipe and reap the already-running child before
                # preserving the parent failure. Without this path an early
                # Claude establishment error leaves either a zombie or a
                # child blocked while writing its result into a full pipe.
                try:
                    _finish_forked_provider(codex_worker)
                except BaseException:
                    pass
                raise
            codex_result = _finish_forked_provider(codex_worker)
        else:
            # `os.fork()` from ThreadingHTTPServer's request thread is not a
            # safe operation. A one-shot forkserver worker gives the route the
            # same CPU isolation without retaining a second full diagnosis
            # heap between requests or adding any periodic work.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=1) as launcher:
                codex_future = launcher.submit(
                    _build_isolated_provider, tasks[1])
                claude_result = _build_one("claude")
                codex_result = codex_future.result()
        results = [claude_result, codex_result]
    return kernel.build_report(_iso_z(now), scope.window(), results)


def _unavailable_provider_result(scope: DiagnosisScope, *,
                                 detail: str = "",
                                 transcripts_visible: bool
                                 ) -> kernel.ProviderResult:
    """One provider withheld as `provider_unavailable`, every class included.

    Withheld is a field-level statement, so the class rows still exist and
    still name their cause; only the measurement is absent.

    `detail` carries the failure's own message onto the withheld denominator's
    qualifications. It exists because one of the failures reaching here is
    `QuotaProjectionIncomplete`, which is a RETRY signal that names its
    remedy — `run \\`cctally cache-sync\\` to reconcile it` — and discarding
    the message left the user reading `withheld (provider_unavailable)` for a
    store that is readable and one command away from answering.
    """
    coverage = _preempted_coverage(
        scope, RawFacts(), cause=WithheldCause.PROVIDER_UNAVAILABLE.value
    )
    denominator = Denominator(
        identity="totalExplainedRetainedCost",
        source=scope.source,
        account_key=scope.account_key,
        window_start=scope.window_start_iso,
        window_end=scope.window_end_iso,
        population_digest="",
        usd=kernel.withheld(WithheldCause.PROVIDER_UNAVAILABLE.value, coverage,
                            (detail,) if detail else ()),
    )
    # The SAME two-stage plan the readable path resolves, with the accounting
    # overlay applied. It is what keeps a capability statement true here too:
    # Codex prompt-cache churn stays `not_applicable` rather than becoming a
    # seventh `provider_unavailable` row, because whether the store could be
    # read has no bearing on whether the provider retains a loss predicate.
    plan = kernel.establish_plan(
        kernel.resolve_policy_plan(scope.source,
                                   transcripts_visible=transcripts_visible),
        conversations_available=False,
        provider_cause=WithheldCause.PROVIDER_UNAVAILABLE.value,
    )
    # A `not_applicable` class names its own reason, exactly as the readable
    # path does, so a client cannot tell the two `provider_unavailable`
    # mechanisms apart by their coverage.
    not_applicable_coverage = _preempted_coverage(
        scope, RawFacts(), cause=kernel.VerdictState.NOT_APPLICABLE.value
    )
    classes = [
        ClassResult(
            decision.kind,
            kernel.VerdictState.NOT_APPLICABLE.value, None, (),
            not_applicable_coverage,
        )
        if decision.mode == kernel.ClassMode.NOT_APPLICABLE.value
        else ClassResult(
            decision.kind, kernel.VerdictState.WITHHELD.value,
            decision.cause, (), coverage,
        )
        for decision in plan.classes
    ]
    return kernel.build_provider_result(
        source=scope.source,
        account_key=scope.account_key,
        effective_speed=scope.effective_speed,
        denominator=denominator,
        classes=classes,
        generation=None,
        coverage=coverage,
        plan=plan,
    )
