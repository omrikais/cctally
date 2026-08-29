"""I/O glue for `cctally quota` (#661 S1).

The arithmetic lives in the pure kernel `bin/_lib_quota_model.py`, which has
no clock, no file access and no database access. This module supplies
everything the kernel refuses to do for itself: it parses instants, reads both
stores coherently, resolves account scope, owns the coefficient-era catalogue,
persists the fitted calibration, renders text and JSON, and maps the result to
an exit code.

The split follows `_lib_pricing_check.py` / `_cctally_pricing_check.py`.

Spec: docs/superpowers/specs/2026-08-28-661-s1-quota-model-and-calibration.md
(each part is normative over the parts before it; Part V is the current one).
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
import sqlite3
import sys

import _cctally_core
import _lib_quota_model as qm
from _lib_quota_model import (  # re-exported for callers and tests
    BlockingReason, CalibrationEvidence, CalibrationStatus, Verdict,
)

UTC = dt.timezone.utc


def _cctally():
    """Resolve the current `cctally` module at call time."""
    return sys.modules["cctally"]


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


# ---------------------------------------------------------------------------
# The coefficient-era catalogue (spec section 6).
#
# The kernel's token-class coefficients were fitted on Opus-5-dominated
# traffic. `docs/quota-model.md` records the changeover and states that the
# tool "refuses to treat earlier data as calibrated": the Opus 4.8 era meters
# at a materially different units-per-point, which is a BUDGET difference the
# shared coefficients cannot absorb. Section 6 therefore says such eras are
# not fitted and persist as gaps carrying `unvalidated-coefficient-era`, a
# cause distinct from `unsupported-model-mix`.
#
# This catalogue is the glue's, not the kernel's, exactly as section 6 and
# Part III section 28 require: the kernel has no producer for that status and
# receives it through `analyse(extra_statuses=...)`.
# ---------------------------------------------------------------------------
#: First UTC date whose composition the shipped coefficients are validated
#: for. Every observation before it is excluded from the analysis window.
SUPPORTED_COMPOSITION_FROM: dt.date = dt.date(2026, 7, 25)

#: Provenance, so a later reader can re-test the boundary rather than inherit
#: it. Each entry states the era, its state and where the state came from.
COEFFICIENT_ERAS: tuple = (
    {
        "from": None,
        "until": SUPPORTED_COMPOSITION_FROM.isoformat(),
        "state": "unvalidated",
        "provenance": (
            "docs/quota-model.md: the Opus 4.8 era fits at about 1,989,101 "
            "units per point against 2.4M for Opus 5, a budget difference the "
            "shared token-class coefficients do not absorb. The shipped "
            "estimator gives 1,265,184 to 1,479,027 over the same weeks, and "
            "spec section 6 declines to adjudicate between the two."
        ),
    },
    {
        "from": SUPPORTED_COMPOSITION_FROM.isoformat(),
        "until": None,
        "state": "validated",
        "provenance": (
            "docs/quota-model.md: 2026-07-25 is the Opus 5 changeover, the "
            "epoch the token-class weights were fitted on."
        ),
    },
)

#: Snapshot sources that are synthetic by construction and are never read as
#: observations (spec section 4). A closed committed set: `record-credit`
#: writes a post-credit reading by construction, and the two repair sources
#: were observed once each on the production store with no tracked writer.
SYNTHETIC_SNAPSHOT_SOURCES: tuple = (
    "record-credit", "remediation", "manual-recovery",
)

#: Sources this repository knows how to produce. An unrecognized value is
#: RETAINED and counted in a diagnostic rather than silently dropped, because
#: dropping it would remove a genuine observation on the strength of a name.
KNOWN_SNAPSHOT_SOURCES: frozenset = frozenset(
    {"statusline", "tampermonkey", "api", "userscript"}
)


# ---------------------------------------------------------------------------
# Instants.
# ---------------------------------------------------------------------------
def parse_instant(value, label: str = "timestamp"):
    """Parse a stored ISO-8601 string to an aware UTC datetime.

    The kernel rejects naive datetimes, and text ordering mis-sorts: `+`
    (0x2B) sorts before `.` (0x2E) against fractional-second stamps, and
    `value[:10]` buckets by LOCAL date. So every stored string is parsed here
    and compared as an instant afterwards.

    A value carrying no offset is read as UTC, which is what every writer in
    this repository stores; it is not read as host-local.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date_argument(value, label: str):
    """Parse a `--since` / `--watch-from` argument to an aware UTC instant.

    A bare date means that date's first instant in UTC. Raises `ValueError`,
    which the command turns into exit 2 — argument errors only.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label}: empty value")
    try:
        if len(text) == 10:
            return dt.datetime.combine(
                dt.date.fromisoformat(text), dt.time(0, 0), tzinfo=UTC)
        return parse_instant(text, label)
    except ValueError as exc:
        raise ValueError(f"{label}: {text!r} is not an ISO-8601 date or "
                         f"timestamp ({exc})") from exc


# ---------------------------------------------------------------------------
# The two-store read (spec section 16).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class LoadResult:
    """One account's parsed population, or a typed reason there is none."""

    entries: tuple = ()
    snapshots: tuple = ()
    credits: tuple = ()
    newest_entry_at: "dt.datetime | None" = None
    analysis_start: "dt.datetime | None" = None
    diagnostics: dict = dataclasses.field(default_factory=dict)
    #: `None` on a usable read; a `CalibrationStatus` the glue contributes
    #: through `analyse(extra_statuses=...)` otherwise.
    status: "CalibrationStatus | None" = None
    cause: "str | None" = None


def _account_clause(column: str, account_key):
    """SQL fragment + params scoping one column to `account_key`.

    `None` is the merged view and adds no clause. The reserved
    `unattributed` sentinel matches BOTH the literal stamp and a NULL, which
    is this repository's read rule.
    """
    if account_key is None:
        return "", []
    import _lib_accounts
    if account_key == _lib_accounts.UNATTRIBUTED:
        return f" AND ({column} IS NULL OR {column} = ?)", [account_key]
    return f" AND {column} = ?", [account_key]


def _store_probe(conn, path) -> str:
    """A cheap fingerprint of one store's current state.

    `PRAGMA data_version` advances when ANOTHER connection commits, which is
    exactly the event that would make two reads describe two different states.
    The file size is folded in so a change the pragma cannot see on a fresh
    connection is still visible.
    """
    try:
        version = conn.execute("PRAGMA data_version").fetchone()[0]
    except (sqlite3.Error, IndexError, TypeError):
        version = "?"
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    return f"{version}:{size}"


def _probe_bundle(stats_conn, cache_conn) -> tuple:
    """Both stores' signatures, taken as ONE observation.

    `bin/_cctally_diagnosis_sources.py` probes each component around its own
    read. That protocol cannot see the hazard section 16 names, because a
    cache ingest landing BETWEEN the stats read and the cache read falls
    before the cache component's own opening probe. The pair is therefore
    taken around the whole read rather than around each half, which is
    strictly stronger and costs one extra pragma per attempt.
    """
    return (
        _store_probe(stats_conn, _cctally_core.DB_PATH),
        _store_probe(cache_conn, _cctally_core.CACHE_DB_PATH),
    )


def _read_stats_component(conn, account_key, start):
    """Meter readings and authoritative credit instants for one account.

    Returns `(snapshots, credits, diagnostics)`. Synthetic sources are
    excluded at the query as a closed committed set; an unrecognized source is
    retained and counted.
    """
    placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
    sql = (
        "SELECT id, captured_at_utc, week_start_at, week_start_date,"
        " weekly_percent, source FROM weekly_usage_snapshots"
        f" WHERE source NOT IN ({placeholders})"
    )
    params: list = list(SYNTHETIC_SNAPSHOT_SOURCES)
    if start is not None:
        sql += " AND captured_at_utc >= ?"
        params.append(start.isoformat())
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)

    snapshots = []
    unrecognised: dict = {}
    legacy_anchors = 0
    for rowid, captured, week_at, week_date, percent, source in conn.execute(
            sql, params):
        at = parse_instant(captured, "captured_at_utc")
        if at is None:
            continue
        anchor = parse_instant(week_at, "week_start_at")
        if anchor is None:
            # A pre-`week_start_at` row. Its date-only boundary is read as
            # that date's first UTC instant rather than dropped: dropping it
            # would remove a genuine observation, and the kernel canonicalizes
            # every anchor to the nearest hour anyway.
            anchor = parse_instant(f"{str(week_date)[:10]}T00:00:00+00:00",
                                   "week_start_date")
            if anchor is None:
                continue
            legacy_anchors += 1
        if source not in KNOWN_SNAPSHOT_SOURCES:
            unrecognised[source] = unrecognised.get(source, 0) + 1
        snapshots.append(qm.SnapshotRecord(
            at=at, week_start=anchor, percent=float(percent),
            source=str(source), rowid=int(rowid)))

    credits = []
    for table, column, kind in (
            ("week_reset_events", "effective_reset_at_utc", "reset"),
            ("weekly_credit_floors", "effective_at_utc", "floor")):
        csql = f"SELECT {column} FROM {table} WHERE 1=1"
        cparams: list = []
        if start is not None:
            csql += f" AND {column} >= ?"
            cparams.append(start.isoformat())
        cclause, cextra = _account_clause("account_key", account_key)
        csql += cclause
        cparams.extend(cextra)
        for (raw,) in conn.execute(csql, cparams):
            at = parse_instant(raw, column)
            if at is not None:
                credits.append(qm.CreditRecord(at=at, kind=kind))

    diagnostics = {
        "unrecognisedSnapshotSources": unrecognised,
        "legacyDateOnlyWeekAnchors": legacy_anchors,
    }
    return snapshots, credits, diagnostics


def _read_cache_component(conn, account_key, start):
    """Priced requests for one account, plus the store-wide ingest tail.

    `newest_entry_at` is deliberately NOT account-scoped. It answers "has the
    ingest finished covering this day", which is a property of the store; an
    account-scoped maximum would report every day of a dormant account as
    `no-local-history`.
    """
    sql = (
        "SELECT timestamp_utc, model, input_tokens, output_tokens,"
        " cache_create_tokens, cache_create_1h_tokens, cache_read_tokens"
        " FROM session_entries WHERE 1=1"
    )
    params: list = []
    if start is not None:
        sql += " AND timestamp_utc >= ?"
        params.append(start.isoformat())
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)
    sql += " ORDER BY timestamp_utc, id"

    entries = []
    for row in conn.execute(sql, params):
        at = parse_instant(row[0], "timestamp_utc")
        if at is None:
            continue
        entries.append(qm.EntryRecord(
            at=at, model=str(row[1] or ""), fresh=row[2] or 0,
            output=row[3] or 0, cache_create_total=row[4] or 0,
            cache_1h=row[5], cache_read=row[6] or 0))
    newest = parse_instant(
        conn.execute("SELECT MAX(timestamp_utc) FROM session_entries")
        .fetchone()[0], "timestamp_utc")
    return entries, newest


def _retained_snapshot_span(conn, account_key):
    """`(earliest, latest)` captured instants ignoring the era floor.

    The `unvalidated-coefficient-era` finding is a statement about retained
    rows, so it needs the span the floor hides. An empty store returns
    `(None, None)` and says nothing.
    """
    placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
    sql = ("SELECT MIN(captured_at_utc), MAX(captured_at_utc)"
           " FROM weekly_usage_snapshots"
           f" WHERE source NOT IN ({placeholders})")
    params: list = list(SYNTHETIC_SNAPSHOT_SOURCES)
    clause, extra = _account_clause("account_key", account_key)
    sql += clause
    params.extend(extra)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None, None
    return parse_instant(row[0]), parse_instant(row[1])


def era_floor_instant() -> "dt.datetime":
    """The first instant the shipped coefficients are validated for."""
    return dt.datetime.combine(
        SUPPORTED_COMPOSITION_FROM, dt.time(0, 0), tzinfo=UTC)


def resolve_analysis_start(since):
    """`max(--since, the era floor)`.

    A `--since` earlier than the floor does not widen the window: section 6
    says an era whose composition the coefficients are not validated for is
    not fitted, and a flag cannot licence fitting it.
    """
    floor = era_floor_instant()
    if since is None:
        return floor
    return max(since, floor)


def load_population(account_key, *, since, now) -> LoadResult:
    """Read both stores coherently and return one account's population.

    The stores are opened through the guarded helpers, so `CCTALLY_DATA_DIR`,
    the current schemas, migrations, corruption handling and the busy timeout
    all apply. Every failure is caught and reported as a typed `unavailable`
    result; an uncaught `OperationalError` from a store read is never allowed
    to reach the user.
    """
    qm.require_aware(now, "now")
    start = resolve_analysis_start(since)
    stats_conn = None
    cache_conn = None
    try:
        try:
            stats_conn = _cctally_core.open_db()
            cache_conn = _cctally().open_cache_db()
        except Exception:
            return LoadResult(analysis_start=start,
                              status=CalibrationStatus.UNAVAILABLE,
                              cause="store-unavailable")

        try:
            earliest, _latest = _retained_snapshot_span(
                stats_conn, account_key)
        except sqlite3.Error:
            return LoadResult(analysis_start=start,
                              status=CalibrationStatus.UNAVAILABLE,
                              cause="store-unavailable")

        payload = None
        for attempt in (0, 1):
            before = _probe_bundle(stats_conn, cache_conn)
            try:
                snapshots, credits, diagnostics = _read_stats_component(
                    stats_conn, account_key, start)
                entries, newest = _read_cache_component(
                    cache_conn, account_key, start)
            except sqlite3.Error:
                return LoadResult(analysis_start=start,
                                  status=CalibrationStatus.UNAVAILABLE,
                                  cause="store-unavailable")
            after = _probe_bundle(stats_conn, cache_conn)
            if before == after:
                payload = (snapshots, credits, diagnostics, entries, newest)
                break
        if payload is None:
            # A store that moves twice while we read it is under active
            # write, and publishing a fit over it would describe a state that
            # never existed as a whole — in the direction that manufactures a
            # rate change.
            return LoadResult(analysis_start=start,
                              status=CalibrationStatus.UNAVAILABLE,
                              cause="generation-incoherent")
    finally:
        for conn in (stats_conn, cache_conn):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    snapshots, credits, diagnostics, entries, newest = payload
    diagnostics = dict(diagnostics)
    diagnostics["analysisStart"] = start.isoformat()
    if not snapshots and earliest is not None and earliest < start:
        # Retained history exists, and all of it predates the era the shipped
        # coefficients are validated for. That is not thin evidence: it is
        # evidence the model has no validated coefficients for, and reporting
        # it as `insufficient-history` would tell the user to wait for days
        # that would never help.
        return LoadResult(
            analysis_start=start, newest_entry_at=newest,
            diagnostics=diagnostics,
            status=CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA,
            cause="history-predates-supported-composition")
    return LoadResult(
        entries=tuple(entries), snapshots=tuple(snapshots),
        credits=tuple(credits), newest_entry_at=newest,
        analysis_start=start, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# Account scoping (spec section 17).
# ---------------------------------------------------------------------------
def accounts_are_decorated() -> bool:
    """The #341 R8 gate: does the Claude provider render account decoration?

    True only above one REAL account. A lone `unattributed` bucket, or a
    single real account, decorates nothing and keeps today's output.
    """
    import _cctally_account
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return False
    try:
        return _cctally_account.provider_is_decorated(conn, "claude")
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _real_account_keys() -> list:
    import _cctally_account
    import _lib_accounts
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return []
    try:
        rows = _cctally_account.load_accounts(conn, "claude")
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [str(row["account_key"]) for row in rows
            if row["account_key"] not in (_lib_accounts.UNATTRIBUTED,
                                          _lib_accounts.VENDOR_WIDE)]


def _unattributed_bucket_has_rows() -> bool:
    import _lib_accounts
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return False
    try:
        placeholders = ",".join("?" for _ in SYNTHETIC_SNAPSHOT_SOURCES)
        row = conn.execute(
            "SELECT 1 FROM weekly_usage_snapshots WHERE source NOT IN "
            f"({placeholders}) AND (account_key IS NULL OR account_key = ?)"
            " LIMIT 1",
            list(SYNTHETIC_SNAPSHOT_SOURCES) + [_lib_accounts.UNATTRIBUTED],
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def resolve_accounts(args) -> tuple:
    """Which populations this invocation analyses, and an exit code or None.

    `--account` narrows to one, resolved case-insensitively through the shared
    ref resolver; an ambiguous or unknown ref is a native usage error at exit
    2 with the candidates on stderr.

    Without `--account`, the answer depends on the R8 gate rather than on a
    merge. At one real account, or a lone unattributed bucket, the single
    analysed population is the merged view `None` — which is byte-identical to
    today's behaviour and is not a merge of two meters. Above one real
    account, each account is analysed independently, because two accounts have
    independent weekly meters and one budget fitted across both is meaningless.
    """
    import _cctally_account
    import _lib_accounts
    ref = getattr(args, "account", None)
    if ref is not None:
        key, code = _cctally_account.resolve_account_filter(args, "claude")
        if code is not None:
            return [], code
        return [key], None
    if not accounts_are_decorated():
        return [None], None
    keys = _real_account_keys()
    if _unattributed_bucket_has_rows():
        keys.append(_lib_accounts.UNATTRIBUTED)
    return keys, None


def account_display_label(account_key) -> "str | None":
    """The label the account decoration renders, or None for the merged view."""
    if account_key is None:
        return None
    import _cctally_account
    try:
        conn = _cctally_core.open_db()
    except Exception:
        return str(account_key)[:8]
    try:
        return _cctally_account.display_account_label(conn, account_key)
    except sqlite3.Error:
        return str(account_key)[:8]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persistence (spec sections 7 and 20).
#
# The fitted calibration is machine-owned internal state and lives in neither
# database. `cache.db` is declared fully re-derivable and `cache-sync
# --rebuild` clears keys there; a plain `stats.db` table is re-materialized
# from the journal on rebuild; a journal-backed record with a stats fold is
# forbidden, because the thirteen-migration stats registry is frozen and a
# schema change is an epoch bump. Living in neither is what satisfies the
# durability requirement by construction.
# ---------------------------------------------------------------------------
#: Bumped when the shape below changes incompatibly. A file stamped ABOVE this
#: value is version-ahead and is preserved, never overwritten.
CALIBRATION_STATE_SCHEMA_VERSION: int = 1

CALIBRATION_FILENAME: str = "quota-calibrations.json"
CALIBRATION_LOCK_FILENAME: str = "quota-calibrations.lock"

#: The state key for the merged view — the single population a one-account or
#: lone-unattributed install analyses. `_lib_accounts.VENDOR_WIDE` already
#: means "all accounts", so it is reused rather than a fourth sentinel minted.
MERGED_STATE_KEY: str = "*"


def calibration_path():
    return _cctally_core.APP_DIR / CALIBRATION_FILENAME


def calibration_lock_path():
    return _cctally_core.APP_DIR / CALIBRATION_LOCK_FILENAME


def _state_key(account_key) -> str:
    return MERGED_STATE_KEY if account_key is None else str(account_key)


def empty_calibration_state() -> dict:
    return {"schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION, "accounts": {}}


@dataclasses.dataclass(frozen=True)
class LoadedCalibrations:
    """The stored state, plus what had to be done to read it.

    The plan's interface block names a bare `dict`. A bare dict cannot carry
    the quarantine path, and spec section 20 requires the command to report
    `unavailable` WITH that path — a rename the user is never told about
    would be indistinguishable from the silent overwrite the section forbids.
    """

    state: dict
    quarantined: "str | None" = None
    unreadable: bool = False


@contextlib.contextmanager
def calibration_lock():
    """Exclusive `flock` around the calibration file's read-modify-write.

    LOCK ORDER: this lock is a LEAF. No database connection, no other `flock`
    and no open SQLite transaction may be held while it is acquired, and it
    takes nothing else while held. The command therefore closes both stores
    before it persists anything.

    Blocking rather than non-blocking, following `config_writer_lock`: the
    write is millisecond-scale, so a brief wait is preferable to silently
    dropping a writer's update.
    """
    path = calibration_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _quarantine(path, now) -> str:
    """Rename a malformed or version-ahead file aside and return its new path.

    Silently rewriting it would destroy the only surviving record of budgets
    whose source rows have since been pruned, so the file is preserved under a
    timestamped suffix and a fresh one is started beside it.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.quarantined-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.quarantined-{stamp}.{counter}")
        counter += 1
    os.replace(str(path), str(candidate))
    return str(candidate)


def load_calibrations(*, now=None) -> LoadedCalibrations:
    """Read the stored state, quarantining a file this binary cannot own.

    `now` supplies the quarantine suffix and is required whenever a
    quarantine is possible; it defaults to the wall clock only so a read-only
    caller need not supply one.
    """
    path = calibration_path()
    if not path.exists():
        return LoadedCalibrations(empty_calibration_state())
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return LoadedCalibrations(empty_calibration_state(), unreadable=True)
    when = now or dt.datetime.now(UTC)
    try:
        state = json.loads(raw)
    except ValueError:
        return LoadedCalibrations(empty_calibration_state(),
                                  quarantined=_quarantine(path, when))
    version = state.get("schemaVersion") if isinstance(state, dict) else None
    if (not isinstance(state, dict) or not isinstance(version, int)
            or isinstance(version, bool)
            or version > CALIBRATION_STATE_SCHEMA_VERSION
            or not isinstance(state.get("accounts"), dict)):
        return LoadedCalibrations(empty_calibration_state(),
                                  quarantined=_quarantine(path, when))
    return LoadedCalibrations(state)


def save_calibrations(state: dict) -> None:
    """Write the state atomically, more strictly than `save_config` does.

    `_cctally_config.save_config` is a related precedent rather than the
    specification: it uses a PID-only temporary name at mode 0644 and does NOT
    fsync the parent directory, so a crash after the rename can lose the
    rename itself. This protocol creates the temporary file with `O_EXCL`
    under a name unique per process AND attempt, writes at 0600, fsyncs the
    contents, `os.replace`s into position, and then fsyncs the parent
    directory.

    The caller holds `calibration_lock()`.
    """
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temporaries(path)
    payload = (json.dumps(state, indent=2) + "\n").encode("utf-8")
    attempt = 0
    while True:
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{attempt}")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            attempt += 1
            if attempt > 64:
                raise
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    # The rename itself is only durable once the DIRECTORY entry is synced.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _sweep_stale_temporaries(path) -> None:
    """Remove temporary files a crashed writer left behind.

    Safe because the caller holds the exclusive lock, so no other writer owns
    one. Without this a crash at the wrong instant leaks a file per attempt.
    """
    prefix = f"{path.name}.tmp."
    try:
        names = list(path.parent.iterdir())
    except OSError:
        return
    for candidate in names:
        if candidate.name.startswith(prefix):
            try:
                candidate.unlink()
            except OSError:
                pass


@dataclasses.dataclass(frozen=True)
class PersistMode:
    """Everything the pure reducer needs that is not the analysis itself.

    `kind` is `"automatic"` or `"diagnostic"`. A diagnostic run — one passing
    `--watch-from`, or a `--since` narrower than the stored regime's span —
    reports its result and writes nothing, so the durable history stays a
    function of unmodified runs rather than of whatever the operator last
    asked to inspect.
    """

    kind: str
    account_key: "str | None"
    now: "dt.datetime"
    fingerprint: str
    regime_start: "dt.datetime"


def _interval_json(interval):
    if interval is None:
        return None
    return {"lo": interval.lo, "hi": interval.hi}


def _regime_from(evidence, *, effective_from, mode, status):
    """One durable regime record built from an AVAILABLE evidence value."""
    return {
        "effectiveFrom": effective_from.astimezone(UTC).isoformat(),
        "effectiveUntil": None,
        "fingerprint": mode.fingerprint,
        "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
        "unitsPerPoint": evidence.value,
        "interval": _interval_json(evidence.interval),
        "support": ({"days": evidence.support.days,
                     "segments": evidence.support.segments}
                    if evidence.support is not None else None),
        "status": status,
        "asOf": mode.now.astimezone(UTC).isoformat(),
        "qualifications": list(evidence.qualifications),
    }


def _decorate(regime, analysis):
    """Attach the composition the regime was fitted under."""
    regime["familyShares"] = dict(analysis.family_shares)
    regime["classShares"] = dict(analysis.class_shares)
    regime["familyRadius"] = analysis.family_radius
    regime["classRadius"] = analysis.class_radius
    return regime


def _open_regime(regimes):
    for regime in reversed(regimes):
        if regime.get("effectiveUntil") is None:
            return regime
    return None


def read_stored_state_readonly() -> "dict | None":
    """The persisted calibration state, read WITHOUT quarantining anything.

    `load_calibrations` renames a malformed or version-ahead file aside
    through `_quarantine`, so a read-only caller — `doctor` is the documented
    one — must not use it. This returns None rather than an empty state on
    any failure, so a caller can tell "nothing readable" from "nothing
    stored"; the two render differently.

    No lock is taken. `save_calibrations` writes through `os.replace` in the
    same directory, so a lock-free reader always sees a complete old or new
    inode, and a flock here would let a writer stall a read-only command.
    """
    try:
        raw = calibration_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(state, dict) or not isinstance(
            state.get("accounts"), dict):
        return None
    return state


def stored_regimes(state: dict, account_key) -> list:
    """The stored regimes for one account, oldest first. Never mutated."""
    accounts = state.get("accounts") or {}
    bucket = accounts.get(_state_key(account_key)) or {}
    regimes = bucket.get("regimes") or []
    return [dict(r) for r in regimes if isinstance(r, dict)]


def recorded_regime(state: dict, account_key) -> "dict | None":
    """The open regime a run would present as the durable prior, or None."""
    return _open_regime(stored_regimes(state, account_key))


def reduce_state(stored: dict, analysis, mode: PersistMode) -> dict:
    """The durable history as a pure function of `(stored, analysis, mode)`.

    Pure: no clock, no file access, no mutation of `stored`. That is what
    makes the history a function of its inputs rather than of call order, and
    it is why every transition below is testable without a filesystem.

    Transitions (spec section 20):

    * A diagnostic run writes nothing.
    * A confirmed rate change closes the open regime at the split's first
      instant and opens a successor from it. The first run that discovers a
      split persists BOTH sides, so the baseline is created if it is absent.
    * A trustworthy fit with no detected change updates the open regime in
      place, or opens the first one.
    * A stored regime under a different fingerprint is marked `stale` and a
      new regime is APPENDED; its own value is never rewritten, because that
      value was true under its own constants.
    * A non-trustworthy analysis writes nothing.
    """
    if mode.kind != "automatic":
        return stored
    key = _state_key(mode.account_key)
    regimes = stored_regimes(stored, mode.account_key)
    confirmed = (analysis.verdict is Verdict.RATE_CHANGE_DETECTED
                 and analysis.detector.split_date is not None)
    fitted_ok = (analysis.status is CalibrationStatus.OK
                 and analysis.fitted.state == "available")
    successor_ok = (confirmed and analysis.watch_fit.state == "available")
    if not fitted_ok and not successor_ok:
        return stored

    open_regime = _open_regime(regimes)
    # A stored regime fitted under different constants is marked stale and
    # left otherwise untouched. It is marked HERE, together with the append
    # below, because section 20 also says a run that writes nothing leaves the
    # stored state alone — and a run reaching this point is writing.
    if open_regime is not None and open_regime.get("fingerprint") != \
            mode.fingerprint:
        open_regime["status"] = CalibrationStatus.STALE.value
        open_regime["effectiveUntil"] = mode.now.astimezone(UTC).isoformat()
        open_regime = None

    if confirmed:
        boundary = dt.datetime.combine(
            analysis.detector.split_date, dt.time(0, 0), tzinfo=UTC)
        boundary_iso = boundary.isoformat()
        # An open regime that already STARTS at the boundary is the successor
        # this run is re-discovering, not a predecessor to close. Closing it
        # would set its `effectiveUntil` to its own `effectiveFrom` and the
        # append below would then add a third regime on every subsequent run.
        open_from = (parse_instant(open_regime.get("effectiveFrom"))
                     if open_regime is not None else None)
        predecessor = (open_regime if open_from is not None
                       and open_from < boundary else None)
        if predecessor is not None:
            predecessor["effectiveUntil"] = boundary_iso
            predecessor["status"] = CalibrationStatus.OK.value
        elif open_regime is None and analysis.baseline_fit.state == "available":
            # The first run that discovers a split persists both sides, so a
            # store with no prior regime still records what the rate WAS.
            baseline = _decorate(_regime_from(
                analysis.baseline_fit, effective_from=mode.regime_start,
                mode=mode, status=CalibrationStatus.OK.value), analysis)
            baseline["effectiveUntil"] = boundary_iso
            regimes.append(baseline)
        if successor_ok:
            successor = _decorate(_regime_from(
                analysis.watch_fit, effective_from=boundary, mode=mode,
                status=analysis.status.value), analysis)
            existing = next((r for r in regimes
                             if r.get("effectiveFrom") == boundary_iso
                             and r.get("effectiveUntil") is None), None)
            if existing is None:
                regimes.append(successor)
            else:
                existing.update(successor)
    elif fitted_ok:
        fresh = _decorate(_regime_from(
            analysis.fitted,
            effective_from=(
                parse_instant(open_regime["effectiveFrom"])
                if open_regime is not None
                and open_regime.get("effectiveFrom") else mode.regime_start),
            mode=mode, status=CalibrationStatus.OK.value), analysis)
        if open_regime is None:
            regimes.append(fresh)
        else:
            open_regime.update(fresh)
    else:
        return stored

    accounts = dict(stored.get("accounts") or {})
    accounts[key] = {"regimes": regimes}
    return {"schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION,
            "accounts": accounts}


def reset_calibration(account_key, *, now=None) -> bool:
    """Remove one account's regimes. True when something was stored.

    Idempotent: resetting an absent calibration succeeds and reports that
    nothing was stored, which is what makes it safe to put in a script.
    """
    with calibration_lock():
        loaded = load_calibrations(now=now)
        accounts = dict(loaded.state.get("accounts") or {})
        key = _state_key(account_key)
        if key not in accounts:
            return False
        accounts.pop(key)
        save_calibrations({
            "schemaVersion": CALIBRATION_STATE_SCHEMA_VERSION,
            "accounts": accounts})
        return True


def _alert_account_key(account_key) -> str:
    """The account identity §6.3's key carries.

    A merged read has no account, and `unattributed` is the estate's sentinel
    for exactly that, so it is used rather than an empty string — the column
    is NOT NULL and every other account-scoped alert family already spells the
    absence this way.
    """
    import _lib_accounts
    if account_key is None:
        return _lib_accounts.UNATTRIBUTED
    return str(account_key)


def persist_and_detect(analysis, mode: PersistMode) -> tuple:
    """`(quarantine path or None, transitions)` under one lock.

    The read, the reduction and the write all happen inside one exclusive
    lock, because the reducer is a read-modify-write and the atomic rename
    alone protects readers rather than writers.

    #661 S2 §6.5 step 1: the metering-rate transition is decided HERE, from
    the persisted state before and after this write. The trigger is the
    persistence transition itself rather than a second detector run, so two
    consumers cannot disagree about whether a change happened. The descriptor
    is RETURNED rather than acted on, because step 2 requires this leaf lock
    to be released before any stats lock is taken, and this function is the
    lock's whole extent.
    """
    with calibration_lock():
        loaded = load_calibrations(now=mode.now)
        if loaded.unreadable:
            # An EMPTY TUPLE, never `None`. `cmd_quota` iterates the second
            # element, so a `None` here raised `TypeError: 'NoneType' object
            # is not iterable` on a file this binary could not read — a
            # permissions problem, an I/O error, or a concurrent quarantine
            # rename removing the primary name between the existence check
            # and the read. No transition is detectable without a before
            # state, and "no transitions" is an empty sequence.
            return None, ()
        before = stored_regimes(loaded.state, mode.account_key)
        reduced = reduce_state(loaded.state, analysis, mode)
        transitions: tuple = ()
        if reduced is not loaded.state:
            save_calibrations(reduced)
            mrc = _cctally()._load_sibling("_lib_meter_rate_change")
            transitions = mrc.detect_transitions(
                before, stored_regimes(reduced, mode.account_key),
                provider="claude",
                account_key=_alert_account_key(mode.account_key),
                detected_at=mode.now.astimezone(UTC).isoformat())
        return loaded.quarantined, transitions


def rate_change_notifications_enabled() -> bool:
    """Whether a recorded transition also PUSHES (spec §6.2).

    Both switches must be on, exactly as the quota threshold axis requires:
    the global `alerts.enabled` and the family's own
    `alerts.rate_change_enabled`, each default-off. A config read that raises
    answers False rather than propagating — a malformed alerts block must not
    turn a recording path into an error path, because §6.2's whole point is
    that recording happens with no configuration at all.
    """
    c = _cctally()
    try:
        block = _cctally_core._get_alerts_config(c.load_config())
    except Exception:                                  # noqa: BLE001
        return False
    return bool(block.get("enabled")) and bool(block.get("rate_change_enabled"))


def record_rate_change_transition(transition, *, now) -> bool:
    """Steps 3 to 6 of spec §6.5. True when this call created the row.

    `run_stats_ingest` is the sole stats writer and it enforces journal-first,
    then commit, then notify — so the descriptor is passed THROUGH it rather
    than written here. `mode="authoritative"` because the caller must observe
    its own transition rather than leaving it to whichever process next holds
    the ingest lock: a user who just ran `cctally quota` and saw the change
    reported would otherwise find no event recorded.

    A stats index mid-epoch-rebuild, a busy lock or a corrupt store must not
    turn `cctally quota` into an error: the calibration is
    already persisted, and the transition is re-derivable from it on the next
    run because `detect_transitions` compares the state before and after each
    write — a run that wrote nothing produces no descriptor, so a missed
    recording is recovered by the next run that does write. That is a real
    gap, and it is the deliberate trade: the durable truth is the calibration
    file plus the journal, and neither is lost.

    The catch is `Exception` plus the two deferral signals, and NOT
    `BaseException` (#661 S2 Stage C review). `StatsRebuildDeferred` derives
    directly from `BaseException` — deliberately, so that no ordinary
    `except Exception` absorbs it — which made `except BaseException` the
    obvious way to cover it and swallowed `KeyboardInterrupt` and
    `SystemExit` with it. A Ctrl-C during the ingest then printed "could not
    record the metering-rate change" and the command carried on. Those are
    different kinds of event: one says this store is busy, the other says
    this process is ending, and only the first is this function's to absorb.
    Catching the shared parent rather than the epoch subclass keeps the
    "widened ONCE" property `StatsRebuildDeferred`'s own docstring states.
    """
    import _cctally_journal as jr
    from _cctally_db import StatsRebuildDeferred
    try:
        result = jr.run_stats_ingest(
            mode="authoritative",
            meter_rate_change={
                "transition": transition,
                "notify": rate_change_notifications_enabled(),
                "created_at": now.astimezone(UTC).isoformat(),
            },
        )
    except (Exception, StatsRebuildDeferred) as exc:   # noqa: BLE001
        eprint(f"quota: could not record the metering-rate change: {exc}")
        return False
    return bool(result.ran)


def persist(analysis, mode: PersistMode) -> "str | None":
    """`persist_and_detect`'s quarantine half, for callers with no stats leg."""
    quarantined, _transitions = persist_and_detect(analysis, mode)
    return quarantined


# ---------------------------------------------------------------------------
# The analysis (spec sections 21, 22, 23, 35).
# ---------------------------------------------------------------------------
#: Which outcome an invocation reports when it analysed several accounts.
#: A FINDING wins over degradation, which is `pricing-check`'s precedent and
#: the reason spec section 8 gives exit 1 to a confirmed change at all: a
#: degraded leg never masks a finding. Below that, an unhealthy account
#: outranks a merely thin one, because it names something to fix.
EXIT_SEVERITY: dict = {0: 0, 4: 1, 3: 2, 1: 3}


@dataclasses.dataclass(frozen=True)
class CurrentWeek:
    """The window the published consumption describes.

    `units_start` is where the unit total begins and is NOT always the week
    anchor: a credited week's continuing series uses post-credit captures, so
    counting units from the anchor would divide a whole week's tokens by a
    meter that was reset partway through. It matches `_floored_week_max`'s
    choice, made consistently here.
    """

    anchor: "dt.datetime"
    end: "dt.datetime"
    units_start: "dt.datetime"
    observed_percent: "int | None"


def current_week_window(segments, *, now) -> "CurrentWeek | None":
    """The subscription week the clock is in, or None when none is retained.

    None rather than a guess: the kernel then withholds the projection with
    the qualification `week-window-unknown`, which says the caller supplied no
    window rather than telling the user their history is missing.
    """
    if not segments:
        return None
    last = max(segments, key=lambda s: s.rows[-1].at)
    anchor = last.week_anchor
    end = anchor + dt.timedelta(days=7)
    if now >= end:
        return None
    in_week = [s for s in segments if s.week_anchor == anchor]
    # One segment on this anchor means the week ran uninterrupted, so the
    # units start where the week does. Several means a floor credit opened a
    # new slice under the same week identity, and the live slice is the last.
    # A reset credit re-anchors, so its successor is the only segment on its
    # own anchor and the anchor IS the credit instant.
    units_start = anchor if len(in_week) == 1 else in_week[-1].rows[0].at
    rows = sorted(last.rows, key=qm._snapshot_sort_key)
    observed = qm.integer_percent(rows[-1].percent) if rows else None
    return CurrentWeek(anchor, end, units_start, observed)


@dataclasses.dataclass(frozen=True)
class AccountResult:
    """Everything one account's renderers need, already computed."""

    account_key: "str | None"
    label: "str | None"
    analysis: object
    load: LoadResult
    week: "CurrentWeek | None"
    recorded: "dict | None"
    stale_prior: bool
    mode_kind: str
    #: The analysis run with `fingerprint_matches=True`. The reducer reads
    #: THIS one, so a stale prior cannot stop a fresh trustworthy fit from
    #: being accepted; `analysis` is what the user is shown.
    clean: object = None
    quarantined: "str | None" = None


def analyse_account(account_key, *, now, since, watch_from,
                    stored_state) -> AccountResult:
    """Read one account's population and run the kernel over it."""
    load = load_population(account_key, since=since, now=now)
    extra = [load.status] if load.status is not None else []
    segments = qm.build_segments(load.snapshots, load.credits)
    series = qm.build_daily_series(
        segments, load.entries, now=now, newest_entry_at=load.newest_entry_at)
    week = current_week_window(segments, now=now)
    if week is None:
        forecast_population: tuple = ()
        week_units = None
        observed = None
        week_start = week_end = None
    else:
        horizon = min(now, week.end)
        forecast_population = tuple(
            e for e in load.entries if week.units_start <= e.at < horizon)
        week_units = 0.0
        for entry in forecast_population:
            units = qm.weighted_units(entry)
            if units is None:
                continue
            if qm.family_participation(
                    qm.normalize_family(entry.model)) != "general":
                continue
            week_units += units
        observed = week.observed_percent
        week_start, week_end = week.units_start, week.end

    recorded = recorded_regime(stored_state, account_key)
    stale_prior = bool(
        recorded is not None
        and recorded.get("fingerprint")
        != qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT)
    mode_kind = "automatic"
    if watch_from is not None:
        mode_kind = "diagnostic"
    elif since is not None and recorded is not None:
        recorded_from = parse_instant(recorded.get("effectiveFrom"))
        if recorded_from is not None and since > recorded_from:
            mode_kind = "diagnostic"

    common = dict(
        now=now, newest_at=load.newest_entry_at,
        forecast_population=forecast_population,
        observed_percent=observed, current_week_units=week_units,
        current_week_start=week_start, current_week_end=week_end,
        override_split=watch_from.date() if watch_from is not None else None,
        extra_statuses=tuple(extra),
        unattributed=qm.unattributed_units(segments, load.entries, now=now),
        in_progress_excluded=qm.in_progress_dates(segments, now=now),
        diagnostics={"snapshots": len(load.snapshots),
                     "segments": len(segments),
                     "credits": len(load.credits)},
    )
    # `fingerprint_matches` describes the calibration the command would ACT
    # on, which is the fresh fit whenever one is trustworthy. Passing False
    # unconditionally under a stale prior would deadlock: `stale` outranks
    # everything below it, so the status could never reach `ok` and no new fit
    # could ever be accepted under the current constants. The fresh analysis
    # therefore runs first, and a stale prior only decides the REPORT when
    # that fresh analysis has no trustworthy fit of its own to publish.
    clean = qm.analyse(series, fingerprint_matches=True, **common)
    reported = clean
    if stale_prior and clean.status is not CalibrationStatus.OK:
        reported = qm.analyse(series, fingerprint_matches=False, **common)
    return AccountResult(
        account_key=account_key,
        label=account_display_label(account_key),
        analysis=reported, load=load, week=week, recorded=recorded,
        stale_prior=stale_prior, mode_kind=mode_kind, clean=clean)


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
def _iso(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def _evidence_json(evidence) -> dict:
    """One `CalibrationEvidence` on the wire.

    A withheld value carries a typed code and NO value, no interval and no
    support — it is absent, never zero — and no confidence field exists at
    all, because the design never defined what a confidence would mean here.
    """
    return {
        "state": evidence.state,
        "value": evidence.value,
        "interval": (None if evidence.interval is None
                     else {"lo": evidence.interval.lo,
                           "hi": evidence.interval.hi}),
        "support": (None if evidence.support is None
                    else {"days": evidence.support.days,
                          "segments": evidence.support.segments}),
        "population": dict(evidence.population),
        "code": evidence.code,
        "qualifications": list(evidence.qualifications),
    }


def _recorded_json(recorded, stale_prior) -> "dict | None":
    if recorded is None:
        return None
    out = dict(recorded)
    out["fingerprintMatchesCurrent"] = not stale_prior
    return out


def account_payload(result: AccountResult) -> dict:
    """One account's complete payload, camelCase throughout."""
    analysis = result.analysis
    load = result.load
    diagnostics = dict(analysis.diagnostics)
    # The detector block is derived from `analysis.detector` rather than read
    # out of `diagnostics`, which is the same source the text renderer uses.
    # Reading the dict key made the wire's disclosure depend on a diagnostic
    # any caller could omit, and section 24 requires it published.
    diagnostics.pop("detector", None)
    detector = qm.detector_diagnostics(analysis.detector)
    return {
        "status": analysis.status.value,
        "verdict": analysis.verdict.value,
        "exitCode": analysis.exit_code,
        "scope": {
            "accountKey": result.account_key,
            "accountLabel": result.label,
            "merged": result.account_key is None,
            "analysisStart": _iso(load.analysis_start),
            "invocation": result.mode_kind,
        },
        "calibration": {
            "recorded": _recorded_json(result.recorded, result.stale_prior),
            "fitted": _evidence_json(analysis.fitted),
        },
        "analysis": {
            "baseline": {"fit": _evidence_json(analysis.baseline_fit)},
            "watch": {"fit": _evidence_json(analysis.watch_fit)},
        },
        "currentWeek": {
            "start": _iso(result.week.units_start) if result.week else None,
            "end": _iso(result.week.end) if result.week else None,
            "observedPercent": analysis.observed_percent,
            "observedMinusModelled": diagnostics.get("observedMinusModelled"),
            "consumption": _evidence_json(analysis.consumption),
            "projection": _evidence_json(analysis.projection),
            "headroom": _evidence_json(analysis.headroom),
        },
        "composition": {
            "baseline": {"familyShares": dict(analysis.family_shares),
                         "classShares": dict(analysis.class_shares)},
            "current": {"familyShares": dict(analysis.current_family_shares),
                        "classShares": dict(analysis.current_class_shares)},
            "familyRadiusEffective": analysis.family_radius,
            "classRadiusEffective": analysis.class_radius,
            "familyRadiusEmpirical": diagnostics.get("familyRadiusEmpirical"),
            "classRadiusEmpirical": diagnostics.get("classRadiusEmpirical"),
            "radiusRule": "max(observed spread, materiality floor)",
        },
        "health": {
            "entriesThrough": _iso(load.newest_entry_at),
            "snapshots": diagnostics.get("snapshots"),
            "segments": diagnostics.get("segments"),
            "credits": diagnostics.get("credits"),
            "eligibleDays": diagnostics.get("eligibleDays"),
            "withheldDays": diagnostics.get("withheldDays"),
            "eligibilityFence": diagnostics.get("eligibilityFence"),
            "unattributedUnits": diagnostics.get("unattributedUnits"),
            "unattributedEntries": diagnostics.get("unattributedEntries"),
            "inProgressDayExcluded": diagnostics.get("inProgressDayExcluded"),
            "forecastPopulationEntries": diagnostics.get(
                "forecastPopulationEntries"),
            "rejectedNumericInputs": diagnostics.get("rejectedNumericInputs"),
            "unrecognisedSnapshotSources": load.diagnostics.get(
                "unrecognisedSnapshotSources"),
            "legacyDateOnlyWeekAnchors": load.diagnostics.get(
                "legacyDateOnlyWeekAnchors"),
            "storeCause": load.cause,
            "calibrationFileQuarantinedTo": result.quarantined,
        },
        "method": {
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "constantsFingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "verifiedAt": qm.QUOTA_MODEL_VERIFIED_AT,
            "supportedCompositionFrom": SUPPORTED_COMPOSITION_FROM.isoformat(),
            "coefficientEras": [dict(era) for era in COEFFICIENT_ERAS],
            "detector": detector,
            "dedicatedPoolScope": qm.DEDICATED_POOL_SCOPE_NOTE,
        },
        "blocking": [reason.value for reason in analysis.blocking],
    }


def build_payload(results, *, now, decorated) -> dict:
    """The whole invocation's JSON, stamped through the shared envelope.

    With one analysed population the account payload is flattened to the top
    level, which keeps spec section 8's stable key list at the top level and
    keeps a single-account install's output shaped as that section describes.
    Above one real account the accounts are published side by side, because
    section 17 says they are reported separately rather than merged.
    """
    worst = max(results, key=lambda r: EXIT_SEVERITY[r.analysis.exit_code])
    body = {
        "generatedAt": _iso(now),
        "status": worst.analysis.status.value,
        "verdict": worst.analysis.verdict.value,
        "exitCode": worst.analysis.exit_code,
    }
    if decorated and len(results) > 1:
        body["accounts"] = [account_payload(r) for r in results]
    else:
        body.update(account_payload(results[0]))
        body["status"] = results[0].analysis.status.value
        body["verdict"] = results[0].analysis.verdict.value
        body["exitCode"] = results[0].analysis.exit_code
    import _lib_json_envelope
    return _lib_json_envelope.stamp_schema_version(body)


def _fmt_units(value) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _fmt_pct(value) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _evidence_line(label, evidence, formatter) -> str:
    if evidence.state != "available":
        marks = (f"  ({', '.join(evidence.qualifications)})"
                 if evidence.qualifications else "")
        return f"  {label:26} withheld: {evidence.code}{marks}"
    interval = evidence.interval
    span = ""
    if interval is not None:
        span = (f"   [{formatter(interval.lo)} , "
                f"{formatter(interval.hi) if interval.hi is not None else '-'}]")
    marks = (f"  ({', '.join(evidence.qualifications)})"
             if evidence.qualifications else "")
    return f"  {label:26} {formatter(evidence.value)}{span}{marks}"


def render_text(results, *, now, decorated) -> list:
    """The human report, as a list of lines."""
    out: list = []
    for result in results:
        analysis = result.analysis
        payload_detector = qm.detector_diagnostics(analysis.detector)
        if decorated and len(results) > 1:
            out.append(f"=== {result.label or 'merged'} ===")
        out.append("cctally quota — weekly quota consumption and metering rate")
        out.append("")
        out.append("DATA")
        out.append(f"  entries through            "
                   f"{_iso(result.load.newest_entry_at) or '-'}")
        out.append(f"  analysis start             "
                   f"{_iso(result.load.analysis_start)}"
                   f"   (supported composition: "
                   f"{SUPPORTED_COMPOSITION_FROM.isoformat()} onward)")
        out.append(f"  eligible days              "
                   f"{analysis.diagnostics.get('eligibleDays', 0)}"
                   f"   ({analysis.diagnostics.get('withheldDays', 0)} withheld)")
        if result.load.cause:
            out.append(f"  ! store                    {result.load.cause}")
        if result.quarantined:
            out.append(f"  ! stored calibration       preserved at "
                       f"{result.quarantined}")
        out.append("")
        out.append("CALIBRATION   effective blended weighted units per weekly point")
        out.append(_evidence_line("fitted", analysis.fitted, _fmt_units))
        if result.recorded is not None:
            recorded_value = result.recorded.get("unitsPerPoint")
            stale = "  (stale — fitted under different constants)" \
                if result.stale_prior else ""
            out.append(f"  {'recorded prior':26} "
                       f"{_fmt_units(recorded_value)}{stale}")
        out.append("  The fitted value is not a provider budget. It is one "
                   "effective blended rate,")
        out.append("  valid for the observed model and token blend, and it is "
                   "the provider's budget")
        out.append("  minus an unmeasured, workload-dependent amount.")
        out.append("")
        out.append("CURRENT WEEK")
        if result.week is None:
            out.append("  no current subscription week is retained")
        else:
            out.append(f"  window                     "
                       f"{_iso(result.week.units_start)} .. "
                       f"{_iso(result.week.end)}")
            out.append(f"  observed meter             "
                       f"{analysis.observed_percent if analysis.observed_percent is not None else '-'}%")
        out.append(_evidence_line("modelled consumption", analysis.consumption,
                                  _fmt_pct))
        out.append(_evidence_line("headroom to 100%", analysis.headroom,
                                  _fmt_pct))
        out.append(_evidence_line("projection to week end",
                                  analysis.projection, _fmt_pct))
        difference = analysis.diagnostics.get("observedMinusModelled")
        if difference is not None:
            out.append(f"  {'observed minus modelled':26} "
                       f"{difference:+.2f} points")
        out.append("")
        out.append("COMPOSITION SUPPORT   radii are EFFECTIVE: "
                   "max(observed spread, materiality floor)")
        out.append(f"  family radius              "
                   f"{_fmt_radius(analysis.family_radius)}"
                   f"   (observed "
                   f"{_fmt_radius(analysis.diagnostics.get('familyRadiusEmpirical'))})")
        out.append(f"  token-class radius         "
                   f"{_fmt_radius(analysis.class_radius)}"
                   f"   (observed "
                   f"{_fmt_radius(analysis.diagnostics.get('classRadiusEmpirical'))})")
        out.append(f"  {qm.DEDICATED_POOL_SCOPE_NOTE}")
        out.append("")
        out.append("DETECTOR")
        out.append(f"  split                      "
                   f"{payload_detector['splitDate'] or 'none'}")
        out.append(f"  eligible days scanned      "
                   f"{payload_detector['scannedEligibleDays']}"
                   f"   (Holm family {payload_detector['holmFamilySize']})")
        if payload_detector["historyTruncated"]:
            out.append(
                f"  only the most recent {payload_detector['maxAutoScanDays']}"
                f" of {payload_detector['inputEligibleDays']} eligible days "
                f"were scanned;")
            out.append(
                f"  the scan starts at "
                f"{payload_detector['scanStartDate']} and the rank-sum "
                f"population is that retained set.")
        if payload_detector["rawP"] is not None:
            out.append(f"  p (raw / Holm)             "
                       f"{payload_detector['rawP']:.6f} / "
                       f"{payload_detector['holmP']:.6f}")
        out.append(_evidence_line("baseline fit", analysis.baseline_fit,
                                  _fmt_units))
        out.append(_evidence_line("successor fit", analysis.watch_fit,
                                  _fmt_units))
        out.append("")
        out.append(f"VERDICT   {analysis.verdict.value}"
                   f"   (status {analysis.status.value}, "
                   f"exit {analysis.exit_code})")
        for reason in analysis.blocking:
            out.append(f"  withheld because: {reason.value}")
        if analysis.verdict is Verdict.WITHHELD:
            # Spec section 9: a withheld verdict must name a rate change as
            # NOT RULED OUT rather than denied. "No rate change" is a finding;
            # this is the absence of one, and conflating them tells a user
            # their metering is unchanged when nothing was established.
            out.append("  This is not a finding of no rate change. The "
                       "evidence needed to decide is")
            out.append("  missing, so no verdict is reported either way.")
        if result.mode_kind == "diagnostic":
            out.append("  diagnostic run — nothing was persisted")
        out.append("")
    return out


def _fmt_radius(value) -> str:
    return "-" if value is None else f"{value:.4f}"


# ---------------------------------------------------------------------------
# The command (spec sections 8, 17, 23, 24).
# ---------------------------------------------------------------------------
def _reset(args, keys, *, now) -> int:
    """`--reset-calibration`, which is selected-account-only."""
    if getattr(args, "account", None) is None and accounts_are_decorated():
        eprint("quota: --reset-calibration needs --account on an install with "
               "more than one Claude account; refusing to clear every regime")
        for key in keys:
            eprint(f"  {key}  {account_display_label(key)}")
        return 2
    key = keys[0] if keys else None
    removed = reset_calibration(key, now=now)
    label = account_display_label(key) or "the merged view"
    if removed:
        print(f"quota: cleared the stored calibration for {label}")
    else:
        print(f"quota: nothing was stored for {label}")
    return 0


def cmd_quota(args) -> int:
    """`cctally quota` — weekly quota consumption and metering-rate change.

    Exit codes come from `QuotaAnalysis.exit_code`, never from `STATUS_EXIT`.
    Spec section 23 superseded section 19's rule that the status alone decides
    the verdict and the exit code: the status describes the current predictive
    calibration and the verdict describes the detector, so a confirmed change
    whose only shortfall is the successor regime's day count reports
    `insufficient-history` with `rate-change-detected` at exit 1 — which
    `STATUS_EXIT` maps to 4. Exit 2 is argument errors only.
    """
    now = _cctally_core._command_as_of()
    try:
        since = parse_date_argument(getattr(args, "since", None), "--since")
        watch_from = parse_date_argument(
            getattr(args, "watch_from", None), "--watch-from")
    except ValueError as exc:
        eprint(f"quota: {exc}")
        return 2

    keys, code = resolve_accounts(args)
    if code is not None:
        return code
    if getattr(args, "reset_calibration", False):
        return _reset(args, keys, now=now)

    decorated = accounts_are_decorated()
    loaded = load_calibrations(now=now)
    results = []
    for account_key in keys or [None]:
        result = analyse_account(
            account_key, now=now, since=since, watch_from=watch_from,
            stored_state=loaded.state)
        # Persistence runs AFTER the stores are closed, because the
        # calibration lock is a leaf in the lock order and takes nothing else.
        mode = PersistMode(
            kind=result.mode_kind, account_key=account_key, now=now,
            fingerprint=qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            regime_start=result.load.analysis_start)
        # §6.5 step 1-2: the descriptor is decided under the calibration
        # file's leaf lock, and that lock is released before any stats lock is
        # taken — `persist_and_detect` returns from its `with` block first.
        quarantined, transitions = persist_and_detect(result.clean, mode)
        quarantined = quarantined or loaded.quarantined
        results.append(dataclasses.replace(result, quarantined=quarantined))
        for transition in transitions:
            # §6.5 steps 3-6: through the sole stats writer, so the append,
            # the row and the cursor commit together and the notification
            # dispatches only after that commit. One write can create more
            # than one adjacent pair, and each is recorded: the row's UNIQUE
            # key dedups, so a pair a prior run already recorded folds to a
            # no-op rather than a second alert.
            record_rate_change_transition(transition, now=now)

    if getattr(args, "json", False):
        print(json.dumps(build_payload(results, now=now, decorated=decorated),
                         indent=2))
    else:
        for line in render_text(results, now=now, decorated=decorated):
            print(line)
    worst = max(results, key=lambda r: EXIT_SEVERITY[r.analysis.exit_code])
    return worst.analysis.exit_code
