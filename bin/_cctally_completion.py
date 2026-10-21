"""The shared, durable derivation-completion step (#825/#832 fix round A,
Codex fix-round-1 Q1/Q2).

A stats rebuild is apply-only: it replays journaled decisions and never
derives a raw observation. Whenever an admitted current input reached the
journal without a current binary deriving it, an apply-only rebuild consumes
it without materializing it — permanently, because no decision exists for any
later rebuild to replay. Three ways in:

* the epoch transition: the tick whose ingest found another epoch appended its
  reading and deferred (spec C3/F1);
* a previous-release process whose ingest, at its own epoch, derived current
  readings UNSTAMPED: admission excludes those decisions (review P1-1);
* the corrupt-index heal and any interrupted recovery, whose rebuild consumed
  a reading a deferred ingest never reached.

`complete_and_rebuild` runs before every production rebuild of the live index
(the caller holds stats maintenance EXCLUSIVE and `journal.ingest.lock`, so no
current decision can be appended concurrently). It

1. starts from a PROVEN complete predecessor state — the derivation frontier
   of a trusted current index (`journal_derivation_frontier`) — or, absent that
   proof, from the ORIGIN of the retained journal, bootstrap facts included
   (fix round E, spec 4.7 revision 1, Codex fix-round-2 Q1): the cutover is no
   proof, because an admitted decision for a pre-cutover input lands after
   it. The replaced index's applied cursor
   is never proof: an old process advances it past inputs it derived
   unstamped, and a rebuild advances it past inputs it never derived. The
   frontier counts consumed inputs; the cycle that consumed them journaled
   its decisions beyond it, through the proof's output end, so the
   predecessor is the rebuild through the frontier PLUS those owed decisions
   (fix round C, P1-1) — without them a window input is derived against a
   state uninterrupted ingest never had;
2. builds a scratch index at that point and replays the admitted inputs of the
   window in journal order through the ONE shared scratch derivation
   (`_cctally_rederive.replay_scratch_records`: the live pipeline hooks,
   harvest included), evaluating the outcome quarantine at each input's
   original position, while a `_cctally_journal.ScratchRetention` keeps the
   projection on the retained decisions (historical facts, corrections,
   reviewed holds and tombstones), never on fresh candidates in their place;
3. finds the GAP inputs (an input with a derived decision no admitted
   decision answers; an input a previous release decided is history, never a
   gap), replays the window without them as the reference, and once more
   with them, holding a fresh candidate only where it differs from the
   reference, so that the difference between the two is exactly the gap's
   causal effect and its descendants — never an unrelated repricing of
   decisions already made;
4. journals that difference by the binding append policy: no physical event
   under the id → an ordinary current-stamped revision-0 `evt`; an admitted
   identical decision → nothing; only an excluded legacy decision, or an
   admitted decision the gap changes → one completed correction batch whose
   action is above every retained revision of the id; an admitted decision the
   gap removes → a tombstone in that batch;
5. rebuilds the live index over the result. The consumed-input frontier stays
   distinct from output positions: inputs another process appended between
   the window and the appended outputs are completed in a further round, and
   an index published with inputs still unproven records the frontier where
   proof stops, so the next completion picks them up. The published proof's
   output end is the journal end, sound only because nothing between the two
   derives from a later input (`_certify`); when that cannot be shown, the
   proof completion started from stands.

6. owes ONE post-unlock ingest when the published prefix stops before an
   admitted derivable input (fix round J4, Codex cycle 5 P1-1). The
   recorder that appended such an input waited a bounded time for the
   locks this step holds and may have exited, and a statusline wake finds
   no intake for it, so nothing else is bound to derive it. A durable
   obligation file of its own (`residual_dir`, named by a nonce) records
   the obligation before the locks are released; every publisher's
   completion wake then dispatches the statusline continuation worker,
   which reads every obligation and runs exactly one bounded authoritative
   ingest (`run_owed_residual_derivation`) holding no accounting or
   selected-state lock. Each obligation names the journal end it was
   written at, and the attempt discharges it only when the cursor its
   ingest left applied covers that residual — or nothing derivable remains
   before it (fix round K5, Codex cycle 6 P1-1) — by removing exactly that
   file, so a completion that records its obligation meanwhile (another
   file) is never discharged by an attempt that predates it (fix round K6,
   Codex cycle 6 verification P1-1). A returned lock-contention stop (a
   busy maintenance, ingest or cache writer lock) keeps the obligation and
   hands the lock that refused it to the statusline's one-shot release
   waiter, whose wake runs the next attempt; any other failure keeps it
   too, recording the failure and a backoff in it, so the continuation
   never retries because of its own failure and a persistently failing
   ingest costs one attempt per backoff interval. A worker killed inside
   the ingest leaves the obligation for the next activating event (review
   cycle 6).

Maintenance never replays a notification: the scratch replay has no alert
configuration and the live index is only ever folded, never derived. A retry
recomputes the same plan from journal truth, reuses an interrupted batch's
`at`, and so re-emits byte-identical lines; a completed batch makes the gap
disappear, so a further run appends nothing.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import uuid

import _cctally_core
import _cctally_journal as _journal
import _lib_account_protocol
import _lib_accounts
import _lib_journal
import _lib_journal_router
import _lib_rederive

#: The correction-batch family this step (and the live collision check)
#: journals under.
FAMILY = _journal.DERIVATION_COMPLETION_FAMILY

#: Completion rounds per rebuild. A round only repeats when another process
#: appended a derivable input while the previous round was appending its own
#: outputs; past this bound the index is published with the frontier where
#: proof stops and the next completion continues from there.
MAX_ROUNDS = 3

#: Op kinds the pipeline derives decisions from (manual credits, cost syncs
#: and reviewed weekly decisions). Account-registry ops only fold, which an
#: apply-only rebuild already does, so they alone never open a window.
_DERIVING_OP_KINDS = frozenset({
    "weekly_credit_floor",
    "sync_week",
    "claude_weekly_observation_decision",
    _lib_account_protocol.WEEKLY_OBSERVATION_DECISION_V2_KIND,
})


def _order(coordinate) -> tuple:
    return _journal.journal_coordinate_key(coordinate)


def _iso_now() -> str:
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def is_deriving_input(record) -> bool:
    """Whether the live pipeline derives decisions from ``record``."""
    if not isinstance(record, dict):
        return False
    if record.get("t") == "obs":
        import _cctally_rederive
        return bool(_cctally_rederive._rederivable_raw_records([record]))
    if record.get("t") == "op":
        return (record.get("payload") or {}).get("kind") in _DERIVING_OP_KINDS
    return False


def window_has_derivable_inputs(lo, hi, boundary) -> bool:
    """Whether the journal holds an admitted deriving input after ``lo`` and
    at or before ``hi`` (both line-boundary coordinates)."""
    if lo is None or hi is None or _order(hi) <= _order(lo):
        return False
    for segment, offset, raw in _journal.iter_range(lo, hi):
        record = _lib_journal.decode_line(raw)
        if (record is not None and is_deriving_input(record)
                and _journal._is_admitted(record, segment, offset, boundary)):
            return True
    return False


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def complete_and_rebuild(*, context, high_water=None, update_quota_cache=True,
                         before_swap=None):
    """Complete derivation through the rebuild's prefix, then rebuild the
    live index over it (see the module docstring). Returns the final
    rebuild's `RebuildResult`."""
    import _cctally_retention

    dest = pathlib.Path(_cctally_core.DB_PATH)
    boundary = _journal._whole_prefix_admission_boundary()
    # #946 (spec 4.7 revision 2): the SAFE endpoint of the prefix, so the
    # proof this step publishes never names a torn suffix as consumed.
    target = (_journal._safe_pinned_high_water(high_water)
              if high_water is not None
              else _journal.journal_safe_high_water())
    if boundary is None or target is None:
        # No account-bound cutover yet: nothing is fenced and there is no
        # baseline to replay from, so the rebuild stays apply-only and
        # carries whatever frontier the replaced index proves.
        with _cctally_retention.retention_shared(label="stats rebuild"):
            return _journal._rebuild_stats_index_locked(
                context=context, high_water=high_water,
                update_quota_cache=update_quota_cache,
                before_swap=before_swap)
    trusted = _journal.read_trusted_derivation_state(dest)
    baseline = (boundary.segment, boundary.end_offset)
    if trusted is not None and _order(trusted.frontier) < _order(baseline):
        trusted = None
    frontier, target = _complete(context, trusted, target, boundary)
    with _cctally_retention.retention_shared(label="stats rebuild"):
        result = _journal._rebuild_stats_index_locked(
            context=context, high_water=target,
            update_quota_cache=update_quota_cache, before_swap=before_swap,
            derivation_frontier=frontier,
            whole_prefix=high_water is None)
    # Step 6 (fix round J4): read the journal end AFTER the publication, so
    # an input appended while the final rebuild ran is owed too. Still under
    # the caller's locks, so no ingest can consume it in between.
    end = _journal.journal_safe_high_water()
    if window_has_derivable_inputs(target, end, boundary):
        try:
            owe_residual_derivation(target, residual_through=end)
        except ResidualObligationNotDurable as exc:
            # Fix round K11 (K9K10-R7): the index IS published. The caller
            # still posts the publication wake once its locks are released,
            # and reports the obligation failure rather than a failed
            # rebuild; ``result`` is the published index.
            exc.result = result
            raise
    return result


def post_publication_wake() -> None:
    """#825/#832 spec 4.3 revision 2: the completion wake a live index
    publication posts once its locks are released. Fix round K11
    (K9K10-R7): a completion whose residual obligation could not be made
    durable has still published, so it posts this wake before reporting
    that failure. Never raises, never waits for the work. File-only unless
    every retained head waits: then the event compares their dependency
    tokens without waiting (fix round K13, 825-832-CD7-V02), which a
    publishing process may read.

    Fix round K12 (K11R-11): the one helper every live publication uses.
    It also swallows `StatsRebuildDeferred`, a `BaseException` a wake's own
    reservation can meet, which no publication may surface."""
    import _cctally_db
    try:
        import _cctally_statusline
        _cctally_statusline.post_wake("index-publication")
    except (Exception, _cctally_db.StatsRebuildDeferred):  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# the residual-derivation continuation (fix round J4, Codex cycle 5 P1-1)
# --------------------------------------------------------------------------

#: The backoff after a failed residual attempt that no lock caused (fix
#: round K6, Codex cycle 6 verification P1-1): the first retry waits this
#: long, each further failure doubles it, up to `_RESIDUAL_RETRY_MAX_S`.
_RESIDUAL_RETRY_BASE_S = 60
_RESIDUAL_RETRY_MAX_S = 3600

#: A marker document larger than this is not one this module wrote.
_RESIDUAL_MARKER_MAX_BYTES = 64 * 1024


def _now() -> float:
    """The wall clock the residual backoff is judged against (a test seam)."""
    import time
    return time.time()


def residual_dir() -> pathlib.Path:
    """The durable obligations: one file per completion that owes ONE
    post-unlock ingest, named by its nonce (fix round K6). A completion only
    ever creates its own file, and a discharge only ever removes a file it
    read and proved covered, so no replacement can race a removal."""
    return pathlib.Path(_cctally_core.APP_DIR) / "derivation-residual"


def _obligation_paths() -> "list[pathlib.Path]":
    """One listing; a missing directory is no obligation. A dot-file is an
    unpublished temporary and never an obligation.

    Fix round K11 (K9K10-R8): only a MISSING directory is empty. Any other
    listing failure raises `OSError`, because a directory that cannot be
    listed can hold owed work: the callers count it as owed
    (`residual_derivation_owed`, `residual_derivation_due`) and answer it
    with a bounded attempt (`run_owed_residual_derivation`)."""
    try:
        with os.scandir(residual_dir()) as entries:
            names = [entry.name for entry in entries]
    except FileNotFoundError:
        return []
    return sorted(residual_dir() / name for name in names
                  if name.endswith(".json") and not name.startswith("."))


@dataclasses.dataclass(frozen=True)
class _Obligation:
    path: pathlib.Path
    document: dict


def _read_obligation(path: pathlib.Path) -> "_Obligation | None":
    """The obligation recorded at ``path``, or None once it is gone. A file
    that cannot be parsed still owes its ingest: it reads as an obligation
    whose residual end is unknown (only a clean cycle covers it) and that
    has never failed."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_RESIDUAL_MARKER_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raw = b""
    try:
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            document = {}
    except (ValueError, UnicodeDecodeError):
        document = {}
    return _Obligation(path, document)


def _read_obligations() -> "list[_Obligation]":
    found = []
    for path in _obligation_paths():
        obligation = _read_obligation(path)
        if obligation is not None:
            found.append(obligation)
    return found


def residual_obligations() -> "list[dict]":
    """Every recorded obligation's document, oldest file name first
    (diagnostics and tests)."""
    return [obligation.document for obligation in _read_obligations()]


def residual_derivation_owed() -> bool:
    """File-only and lock-free (one listing): does any completion owe a
    residual ingest, whatever its backoff? An unreadable obligation
    directory is owed work, never none (fix round K11, K9K10-R8)."""
    try:
        return bool(_obligation_paths())
    except OSError:
        return True


def _unreadable_backoff_path() -> pathlib.Path:
    """Fix round K11 (K9K10-R8): the backoff of the attempts made while the
    obligation directory cannot be listed. It lives beside that directory,
    never in it, so it stays writable when only the directory is not."""
    return (pathlib.Path(_cctally_core.APP_DIR)
            / "residual-obligations-unlistable.json")


def _read_unreadable_backoff() -> dict:
    try:
        with open(_unreadable_backoff_path(), "rb") as handle:
            document = json.loads(
                handle.read(_RESIDUAL_MARKER_MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _unreadable_due(now: float) -> bool:
    """Whether an attempt over an unlistable obligation directory is owed
    now: never attempted, or its non-sliding backoff ended (spec 4.3
    *Waiting heads*: 60 s doubling to 3,600 s)."""
    return _due(_Obligation(_unreadable_backoff_path(),
                            _read_unreadable_backoff()), now)


def _clear_unreadable_backoff() -> None:
    """End the unlistable directory's backoff once it lists again. A stat
    in the steady state: there is nothing to remove."""
    path = _unreadable_backoff_path()
    if not os.path.lexists(path):
        return
    try:
        path.unlink()
    except OSError:
        pass


def _retry_at(document: dict):
    value = document.get("retryAt")
    return value if isinstance(value, (int, float)) and not isinstance(
        value, bool) else None


def _due(obligation: _Obligation, now: float) -> bool:
    """Whether an attempt is owed now. A retry instant further away than the
    longest backoff was recorded before the clock stepped back, and is due
    rather than stalling the obligation for as long as the step."""
    retry_at = _retry_at(obligation.document)
    return (retry_at is None or retry_at <= now
            or retry_at - now > _RESIDUAL_RETRY_MAX_S)


def residual_derivation_due(now: "float | None" = None) -> bool:
    """File-only and lock-free: is an attempt owed NOW — an obligation exists
    that is not waiting out the backoff of its own failed attempt (fix round
    K6)? The statusline counts only this as pending work, so an obligation
    in backoff costs no dispatch until an activating event after it ends.
    One listing when nothing is owed."""
    now = _now() if now is None else now
    try:
        paths = _obligation_paths()
    except OSError:
        # Fix round K11 (K9K10-R8): unknown obligations are owed, bounded by
        # the unreadable directory's own backoff.
        return _unreadable_due(now)
    if not paths:
        return False
    for path in paths:
        obligation = _read_obligation(path)
        if obligation is not None and _due(obligation, now):
            return True
    return False


def _point(coordinate):
    return (None if coordinate is None
            else [str(coordinate[0]), int(coordinate[1])])


class ResidualObligationNotDurable(OSError):
    """A completion published its capped index but could not durably record
    the residual-derivation obligation it owes (review cycle 7 P1-1, spec
    4.7). The completion must not report success: nothing would resume its
    residual automatically. The residual input itself stays in the journal,
    past the published cursor, for the next ingest."""


def _fsync_path(path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_obligation(path: pathlib.Path, document: dict) -> None:
    """Durably publish ``document`` at ``path`` (temporary, fsync, atomic
    rename, directory fsync), or raise `OSError`. Only a completion creating
    its own nonce file, and the one reservation-holding attempt recording its
    failure in a file it read, ever write one, so the rename never replaces
    another writer's obligation.

    Review cycle 7 P1-1: EVERY writer fsyncs the residual directory's parent
    after ensuring the directory exists, not only the writer that created
    it. A creator that died between its `mkdir` and that barrier left a
    directory a host crash can still lose — with every obligation a later
    writer put in it."""
    directory = path.parent
    temp = directory / f".{path.name}.{os.getpid()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _fsync_path(directory.parent)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_path(directory)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def owe_residual_derivation(published, *, residual_through=None) -> None:
    """Record, durably, that the index this completion published stops at
    ``published`` before an admitted derivable input, and that the inputs
    owed end at ``residual_through`` (the journal end the completion read;
    read here when not given). Every completion records its OWN obligation
    file, named by a fresh nonce: one attempt answers all of them (one
    ingest consumes every input past the cursor) and removes only the files
    it read and proved covered.

    Fail-closed (review cycle 7 P1-1, spec 4.7): when the obligation cannot
    be made durable this raises `ResidualObligationNotDurable`, so the
    completion — which has already published its capped index — reports
    failure instead of a success whose wake finds nothing to resume. The
    caller's own failure handling (an exit 3, a retained heal marker, a
    rederive apply error) then stands, and the residual input stays in the
    journal for the next ingest."""
    if residual_through is None:
        try:
            residual_through = _journal.journal_safe_high_water()
        except Exception:  # noqa: BLE001 — best effort, like the write
            residual_through = None
    nonce = uuid.uuid4().hex
    path = residual_dir() / f"{nonce}.json"
    try:
        _write_obligation(path, {
            "schemaVersion": 3,
            "publishedThrough": _point(published),
            "residualThrough": _point(residual_through),
            "recordedAt": _iso_now(), "nonce": nonce})
    except OSError as exc:
        raise ResidualObligationNotDurable(
            getattr(exc, "errno", None) or errno.EIO,
            "the derivation completion published a capped index but could "
            f"not durably record its residual obligation ({exc})",
            str(path)) from exc
    # Fix round K7 (Codex round 14 P1-1): the new obligation advances
    # the statusline's pending generation, as a render capture does, so
    # a continuation pass that scanned the disk before this file existed
    # cannot clear pending past it and lose the wake that follows.
    # Fix round K13 (review cycle 7 P1-1): a failed registration is not
    # absorbed — `register_durable_work` falls back to the durable dirty
    # notification, and an older pass keeps pending for the publication
    # wake this completion posts after it releases its locks.
    try:
        import _cctally_statusline
        _cctally_statusline.register_durable_work()
    except Exception:  # noqa: BLE001 — the obligation itself is durable
        pass


@dataclasses.dataclass(frozen=True)
class ResidualAttempt:
    """What one `run_owed_residual_derivation` call did (fix rounds K5, K6).

    ``disposition`` is ``none`` (nothing was owed), ``discharged`` (the
    applied cursor covers every obligation it read), ``blocked`` (a
    lock-contention stop: the uncovered obligations stay and ``blocked_by``
    names the lock whose release resumes them), ``failed`` (any other
    failure: the uncovered obligations stay, each recording the failure and
    its backoff, and ``reason`` says why) or ``backoff`` (every obligation is
    still waiting out a failed attempt's backoff: no ingest ran)."""

    owed: bool
    disposition: str
    blocked_by: object = None
    reason: str = ""


def _residual_through(document: dict):
    """The journal end an obligation's residual inputs reach, or None when
    it does not say (unreadable, or written without one)."""
    through = document.get("residualThrough")
    if (isinstance(through, list) and len(through) == 2
            and isinstance(through[0], str) and type(through[1]) is int
            and through[1] >= 0):
        return (through[0], through[1])
    return None


def _residual_covered(result, through) -> bool:
    """Whether a returned ingest consumed the residual inputs a marker owes:
    its applied cursor reaches ``through``, or no admitted derivable input
    lies between the two. A marker that names no end is covered only by a
    clean cycle, which consumed through a high water it read after the
    marker was written."""
    if not getattr(result, "ran", False) or getattr(result, "error", None):
        return False
    applied = getattr(result, "applied_through", None)
    if through is None:
        return getattr(result, "blocked_by", None) is None
    if applied is not None and _order(applied) >= _order(through):
        return True
    start = applied if applied is not None else journal_origin()
    return not window_has_derivable_inputs(
        start, through, _journal._whole_prefix_admission_boundary())


def _discharge(obligation: _Obligation) -> None:
    """Remove exactly the obligation file this attempt read and proved
    covered. No other writer replaces a file under its name, so a newer
    completion's obligation — another file — can never be removed here."""
    try:
        obligation.path.unlink()
    except OSError:
        pass


def _record_failure(obligation: _Obligation, reason: str, now: float) -> float:
    """Record one failed attempt in the obligation it was made for: the
    count, when and why, and the backoff before the next attempt. Returns
    the retry instant."""
    document = dict(obligation.document)
    failures = document.get("failures")
    failures = (failures if type(failures) is int and failures >= 0 else 0) + 1
    interval = min(_RESIDUAL_RETRY_MAX_S,
                   _RESIDUAL_RETRY_BASE_S * 2 ** min(failures - 1, 16))
    document.update({
        "failures": failures,
        "lastFailure": reason,
        "lastFailureAt": _iso_now(),
        "retryAt": now + interval,
    })
    try:
        _write_obligation(obligation.path, document)
    except OSError:
        # The obligation itself is already durable and unchanged; only its
        # backoff is lost, so the next activating event retries sooner.
        pass
    return now + interval


def run_owed_residual_derivation() -> ResidualAttempt:
    """Answer the recorded obligations with exactly ONE bounded
    authoritative ingest attempt. Called by the statusline continuation
    worker, which holds no accounting or selected-state lock, so the
    ingest's bounded waits respect the lock-order law. The attempt reads
    every obligation file first; one written after that (a completion that
    published another capped prefix meanwhile) is not answered by this
    attempt and stays owed. Once the ingest RETURNS (fix round K5, Codex
    cycle 6 P1-1; fix round K6, its verification P1-1):

    * each obligation its applied cursor covers is discharged — its own
      file removed, nothing else;
    * it lost a lock another holder held (a contended ``blocked_by``: the
      maintenance or ingest lock refused it, or the cache leg prefix-stopped
      on a cache writer flock) -> the uncovered obligations stay, and the
      caller registers that lock with the one-shot release waiter, whose
      wake runs the next attempt; nothing here retries;
    * anything else (a raised error, an I/O failure on a lock, a cycle that
      returned short of the residual with no lock to wait for) -> the
      uncovered obligations stay, durable, each recording the failure and a
      backoff. The attempt never re-arms itself: until the backoff ends no
      attempt runs an ingest and the statusline counts none of them as
      pending work, so a persistently failing ingest costs at most one
      attempt per backoff interval, run by an activating event after it.

    When every obligation is still in its backoff, nothing runs. A worker
    killed inside the ingest leaves every obligation for the next
    activating event (review cycle 6)."""
    try:
        obligations = _read_obligations()
    except OSError as exc:
        return _attempt_unreadable_obligations(exc)
    _clear_unreadable_backoff()
    if not obligations:
        return ResidualAttempt(False, "none")
    now = _now()
    if not any(_due(obligation, now) for obligation in obligations):
        return ResidualAttempt(True, "backoff",
                               reason="every obligation is in its backoff")
    import _cctally_db
    result = None
    failure = None
    try:
        result = _journal.run_stats_ingest(mode="authoritative")
    except (Exception, _cctally_db.StatsRebuildDeferred) as exc:
        failure = exc
    block = getattr(result, "blocked_by", None)
    uncovered = []
    for obligation in obligations:
        if failure is None and _residual_covered(
                result, _residual_through(obligation.document)):
            _discharge(obligation)
        else:
            uncovered.append(obligation)
    if not uncovered:
        return ResidualAttempt(True, "discharged")
    if failure is None and block is not None and getattr(
            block, "contended", False):
        return ResidualAttempt(
            True, "blocked", blocked_by=block,
            reason=f"{block.lock} lock busy ({block.mode})")
    if failure is not None:
        reason = f"ingest raised {type(failure).__name__}: {failure}"
    elif result is None or not isinstance(result, _journal.IngestResult):
        reason = "ingest returned no result"
    elif getattr(result, "error", None) is not None:
        error = result.error
        reason = f"ingest error {type(error).__name__}: {error}"
    elif block is not None:
        reason = f"{block.lock} lock failed with an I/O error"
    else:
        reason = "the cycle returned short of the residual inputs"
    retry_at = max(_record_failure(obligation, reason, now)
                   for obligation in uncovered)
    retry = (dt.datetime.fromtimestamp(retry_at, tz=dt.timezone.utc)
             .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    _cctally_core.eprint(
        f"[stats] residual derivation attempt failed: {reason}; "
        f"{len(uncovered)} obligation(s) kept in {residual_dir()}, "
        f"retried by the first activating event after {retry}")
    return ResidualAttempt(True, "failed", reason=reason)


def _attempt_unreadable_obligations(listing_error) -> ResidualAttempt:
    """Fix round K11 (K9K10-R8): the obligation directory cannot be listed,
    so whatever it holds is owed and unknown. When the directory's own
    backoff allows, run the ONE ingest every obligation is answered by —
    nothing can be discharged, because nothing was read. The attempt is
    recorded (with the next, doubled, backoff) BEFORE the ingest runs, so an
    attempt the ingest's own wake starts meanwhile waits it out instead of
    ingesting again."""
    now = _now()
    reason = f"the obligation directory cannot be listed ({listing_error})"
    if not _unreadable_due(now):
        return ResidualAttempt(True, "backoff", reason=reason)
    retry_at = _record_failure(
        _Obligation(_unreadable_backoff_path(), _read_unreadable_backoff()),
        reason, now)
    import _cctally_db
    result = None
    try:
        result = _journal.run_stats_ingest(mode="authoritative")
    except (Exception, _cctally_db.StatsRebuildDeferred):
        result = None
    block = getattr(result, "blocked_by", None)
    if block is not None and getattr(block, "contended", False):
        return ResidualAttempt(
            True, "blocked", blocked_by=block,
            reason=f"{block.lock} lock busy ({block.mode})")
    retry = (dt.datetime.fromtimestamp(retry_at, tz=dt.timezone.utc)
             .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    _cctally_core.eprint(
        f"[stats] residual derivation obligations unknown: {reason}; "
        f"retried by the first activating event after {retry}")
    return ResidualAttempt(True, "failed", reason=reason)


def _certify(proven, entry, boundary, fallback):
    """The derivation proof to publish once every admitted derivable input
    through ``proven`` has its decisions journaled (fix round C, P1-1).

    The output end is the journal end now. That is sound only when no
    admitted decision in between derives from an input after ``proven``: the
    caller holds `journal.ingest.lock`, so nothing appended since ``entry``
    (the journal end when completion started) is a live decision — only this
    step's own outputs, for inputs at or before ``proven``, and recorders'
    inputs. Before ``entry`` a live cycle may have derived a later input, so
    that stretch must hold no derivable input — or no admitted decision at
    all: a decision derived from an input is journaled after it, so a stretch
    holding inputs but no decision (a recorder appended after the caller's
    pinned prefix, its ingest waiting on the lock this step holds) holds
    nothing derived from a later input either. Withholding the proof there
    left it behind the published cursor, where live ingest can never extend
    it, so drop classification (which requires the proof at the cursor,
    review cycle 3 P1-1) stayed unknown until some later rebuild. Otherwise
    ``fallback`` (the proof completion started from, still a journal fact, or
    None) stands."""
    if proven is None:
        return fallback
    if (entry is None or _order(proven) >= _order(entry)
            or not window_has_derivable_inputs(proven, entry, boundary)
            or not _window_has_admitted_decisions(proven, entry, boundary)):
        # The output end is the verified complete-line endpoint (revision
        # 2), never the raw size: a torn suffix past it holds no output.
        return _journal.DerivationFrontier(
            proven, _journal.journal_safe_high_water())
    return fallback


def _window_has_admitted_decisions(lo, hi, boundary) -> bool:
    """Whether the journal holds an admitted decision record (an event, a
    correction or a correction-batch marker) after ``lo`` and at or before
    ``hi``. An excluded record is never part of the admitted state a proof
    describes."""
    if lo is None or hi is None or _order(hi) <= _order(lo):
        return False
    for segment, offset, raw in _journal.iter_range(lo, hi):
        record = _lib_journal.decode_line(raw)
        if (isinstance(record, dict)
                and record.get("t") in ("evt", "correction", "correction_batch")
                and _journal._is_admitted(record, segment, offset, boundary)):
            return True
    return False


def journal_origin():
    """The coordinate before the first retained journal line: where
    completion reconstructs from without a trusted frontier (fix round E)."""
    segments = _journal.list_segments()
    return (segments[0], 0) if segments else None


def _complete(context, trusted, target, boundary):
    """Returns ``(proof, target)``: the derivation proof to publish (a
    `DerivationFrontier` or None), and the prefix the live index must cover
    (at least the original target, plus any outputs this step appended).

    Without a trusted frontier the predecessor is the empty index at the
    journal's origin and the window is the whole retained journal, bootstrap
    facts included (fix round E, Codex fix-round-2 Q1): the cutover is no
    proof that the decisions it precedes are all a later input needs — an
    admitted decision for a pre-cutover input lands after it."""
    entry = _journal.journal_safe_high_water()
    start = trusted.frontier if trusted is not None else journal_origin()
    if start is None:
        return _certify(target, entry, boundary, None), target
    if _order(start) >= _order(target):
        # The proof already reaches past this prefix: a journal fact, kept.
        return (trusted if trusted is not None
                else _certify(target, entry, boundary, None)), target
    if not window_has_derivable_inputs(start, target, boundary):
        return _certify(target, entry, boundary, trusted), target
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="cctally-completion-", dir=str(_cctally_core.APP_DIR)) as tmp:
        base = pathlib.Path(tmp) / "predecessor.db"
        _journal.rebuild_stats_index(
            context=_journal.RebuildContext(trigger=context.trigger),
            target_path=str(base), high_water=start,
            update_quota_cache=False)
        owed = frozenset()
        if trusted is not None and trusted.output_end is not None:
            owed = _apply_owed_outputs(
                base, start, trusted.output_end, boundary)
        for round_index in range(MAX_ROUNDS):
            appended = _complete_window(
                base, pathlib.Path(tmp), start, target, boundary, round_index,
                owed=owed)
            if not appended:
                return _certify(target, entry, boundary, trusted), target
            after = _journal.journal_safe_high_water()
            if not window_has_derivable_inputs(target, after, boundary):
                return _certify(after, entry, boundary, trusted), after
            if round_index + 1 < MAX_ROUNDS:
                target = after
        # Review cycle 3 P1-1 (Codex fix-round-4 Q1): the rounds ran out
        # while another process kept appending derivable inputs. Publish
        # ONLY through the proven input endpoint: the residual inputs past it
        # stay outside both applied-cursor fields and the admission and
        # selector coverage, so ordinary ingest resumes there. The proof's
        # output end (the journal end now) is kept separate, so the decisions
        # this last round journaled beyond the residual inputs are owed, not
        # discarded: ingest applies them before it derives those inputs, and
        # advances the frontier with the cursor in one transaction. Advancing
        # the published prefix to the journal end instead consumed the
        # residual inputs without deriving them, permanently.
        return _certify(target, entry, boundary, trusted), target


def _apply_owed_outputs(base, start, output_end, boundary) -> frozenset:
    """Complete the predecessor at the consumed-input frontier ``start``
    (fix round C, P1-1): apply the admitted decisions journaled after it, up
    to its output end, which were derived from inputs at or before it.

    A rebuild through the frontier alone lacks them, because the cycle that
    consumed those inputs journaled its decisions beyond its captured high
    water, and the window replay derives only inputs — so a later window
    input would be derived against a state uninterrupted ingest never had.
    The proof guarantees nothing in that stretch derives from a later input,
    so these are exactly the outputs owed. Each is applied as its effective
    revision through that output end, in the rebuild's fold order (structural
    folds, the open 5h block projection, then milestones). Returns the ids
    applied, which the predecessor then holds."""
    prefix = _read_prefix(start, output_end, boundary)
    if prefix.cutover_claude is _journal._CUTOVER_UNSEEN:
        prefix.cutover_claude = _journal._resolve_cutover_for_rebuild(
            _journal._CUTOVER_UNSEEN, output_end, _journal.list_segments(),
            {"lines": 0, "bytes": 0, "decodes": 0}, boundary=boundary)
    elif prefix.cutover_claude is None:
        prefix.cutover_claude = _lib_accounts.UNATTRIBUTED
    for record in prefix.slots:
        if record is not None:
            _journal._normalize_legacy_account_stamp(
                record, prefix.cutover_claude)
    selection = _lib_journal.resolve_effective_events(
        prefix.slots, protocol_prefix_evidence=tuple(prefix.evidence))
    owed: dict = {}
    for position, record in enumerate(prefix.window):
        if record.get("t") not in ("evt", "correction"):
            continue
        selected = selection.by_id.get(record.get("id"))
        if selected is not None and selected.status == "active":
            owed.setdefault(record["id"], (position, selected.record))
    if not owed:
        return frozenset()
    stream = sorted(owed.values(), key=lambda item: (
        _journal._fold_order(item[1]), item[0]))
    conn = _cctally_core.open_db(_target_path=str(base))
    try:
        for phase in ("structural", "tail"):
            if phase == "tail":
                import sys
                backfill = getattr(sys.modules.get("cctally"),
                                   "_backfill_five_hour_blocks", None)
                if backfill is not None:
                    # Fix round J5: the first writer is read over the WHOLE
                    # prefix in journal order, never over the owed decisions
                    # alone, which may start after a window's first decision.
                    backfill(conn, only_missing=True,
                             journaled_first=(
                                 _journal.journaled_block_first_observations(
                                     prefix.slots, selection.by_id)))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for _position, record in stream:
                    structural = (_journal._fold_order(record)
                                  < _journal._REBUILD_MILESTONE_ORDER)
                    if structural == (phase == "structural"):
                        _journal._apply_evt(conn, record)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    finally:
        conn.close()
    return frozenset(owed)


# --------------------------------------------------------------------------
# one round
# --------------------------------------------------------------------------

class _Prefix:
    """One pass over the journal through the round's target."""

    def __init__(self):
        self.slots: list = []          # selector slots, one per decoded record
        self.evidence: list = []       # protocol-resolution prefix evidence
        self.window: list = []         # window records (placeholders kept)
        self.coordinates: list = []    # parallel to `window`
        self.excluded_revisions: dict = {}
        self.inert_revisions: dict = {}
        self.begin_at: dict = {}
        self.batch_family: dict = {}
        self.cutover_claude = _journal._CUTOVER_UNSEEN
        # Admitted decision ids before the window (the predecessor holds
        # them), and ids whose admitted revision-0 event a previous release
        # wrote (fix round E).
        self.before_ids: set = set()
        self.legacy_origin_ids: set = set()


def _bump(table: dict, record) -> None:
    event_id = record.get("id")
    rev = record.get("rev", 0)
    if isinstance(event_id, str) and type(rev) is int and rev >= 0:
        table[event_id] = max(rev, table.get(event_id, rev))


def _read_prefix(start, target, boundary) -> _Prefix:
    out = _Prefix()
    segments = _journal.list_segments()
    hasher = _lib_journal_router.PrefixHashAccumulator()
    prior = {"hw": None}
    placeholder = _lib_account_protocol.excluded_placeholder()
    start_key = _order(start)
    for segment, offset, raw in _journal._iter_range_with_segments(
        None, target, segments,
        on_segment=lambda name: hasher.begin_segment(name, prior["hw"]),
        on_bytes=hasher.extend,
    ):
        end = (segment, offset + len(raw) + 1)
        record = _lib_journal.decode_line(raw)
        if record is None:
            prior["hw"] = end
            continue
        in_window = _order((segment, offset)) >= start_key
        if not _journal._is_admitted(record, segment, offset, boundary):
            out.slots.append(None)
            if record.get("t") in ("evt", "correction"):
                _bump(out.excluded_revisions, record)
            if in_window:
                out.window.append({**placeholder, "quarantine": {
                    "record": record, "segment": segment, "offset": offset}})
                out.coordinates.append((segment, offset))
            prior["hw"] = end
            continue
        t = record.get("t")
        if t == "op":
            _journal._capture_protocol_prefix_evidence(
                record, prior["hw"], out.evidence, hasher=hasher)
        if (out.cutover_claude is _journal._CUTOVER_UNSEEN
                and record.get("id") == _journal.CUTOVER_OP_ID):
            out.cutover_claude = _journal._cutover_value_of(record)
        if t in ("evt", "correction") and isinstance(record.get("id"), str):
            if not in_window:
                out.before_ids.add(record["id"])
            if (t == "evt" and record.get("rev", 0) == 0
                    and not _lib_account_protocol.is_stamped(record)):
                out.legacy_origin_ids.add(record["id"])
        if t == "correction_batch" and record.get("phase") == "begin":
            out.begin_at.setdefault(record.get("id"), record.get("at"))
            out.batch_family.setdefault(record.get("id"), record.get("family"))
        out.slots.append(
            record if t in _lib_journal_router.RETAINED_RECORD_TYPES else None)
        if in_window:
            out.window.append(record)
            out.coordinates.append((segment, offset))
        prior["hw"] = end
    return out


def _held_observation_ids(slots) -> frozenset:
    """The observations a retained reviewed weekly decision holds (latest
    decision per observation wins, as the rederive planner reads them)."""
    import _cctally_rederive
    effective: dict = {}
    for record in slots:
        if (not isinstance(record, dict) or record.get("t") != "op"
                or (record.get("payload") or {}).get("kind")
                not in _cctally_rederive._REVIEWED_WEEKLY_KINDS):
            continue
        payload = record["payload"]
        decisions = (payload.get("decisions")
                     if payload.get("schema_version") == 1
                     else payload.get("weekly_axis_decisions")) or []
        for item in decisions:
            if isinstance(item, dict) and isinstance(
                    item.get("observationId"), str):
                effective[item["observationId"]] = item.get("disposition")
    return frozenset(obs for obs, disposition in effective.items()
                     if disposition == "hold")


@contextlib.contextmanager
def _cache_read_view():
    """A stable read-only view of cache.db for the replay's cost reads, or
    None when there is no readable cache. Takes no cache flock (the caller
    holds the stats locks); a read transaction pins one snapshot for both
    replays, so the causal difference never reflects a cache change."""
    conn = None
    path = _cctally_core.CACHE_DB_PATH
    if pathlib.Path(path).exists():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                   timeout=5.0)
            conn.execute("BEGIN")
            conn.execute("SELECT 1 FROM session_entries LIMIT 1").fetchall()
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            conn = None
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


@contextlib.contextmanager
def _no_cache_read_adapters():
    """The replay's readers with no cache: no retained usage entries and no
    alert configuration — the same for both replays, so the causal
    difference is unaffected; only cost facts go unpriced."""
    import sys
    cctally = sys.modules["cctally"]
    replacements = {
        "get_entries": lambda *args, **kwargs: [],
        "get_claude_session_entries": lambda *args, **kwargs: [],
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


def _replay(base, tmp, name, prefix, held_ids, cache_conn, *, retention,
            skip=frozenset()):
    """One scratch replay of the window from the predecessor state, the
    projection kept on the retained decisions by ``retention``. Returns
    ``(events, by_input)``: every derived candidate, and per raw input index
    its own candidates."""
    import _cctally_quarantine
    import _cctally_rederive
    copy_path = tmp / name
    shutil.copyfile(base, copy_path)
    records = copy.deepcopy(prefix.window)
    for record in records:
        if record.get("t") != _lib_account_protocol.EXCLUDED_PLACEHOLDER_TYPE:
            _journal._normalize_legacy_account_stamp(
                record, prefix.cutover_claude)
    by_input: dict = {}
    conn = _cctally_core.open_db(_target_path=str(copy_path))
    try:
        fold = _cctally_quarantine.QuarantineFold.load(conn, held_ids=held_ids)
        events = _cctally_rederive.replay_scratch_records(
            conn, records, cache_conn, fold=fold, held_ids=held_ids,
            coordinates=prefix.coordinates, skip_indexes=skip,
            on_input=lambda index, derived: by_input.__setitem__(
                index, list(derived)),
            read_adapters=(None if cache_conn is not None
                           else _no_cache_read_adapters()),
            retention=retention)
    finally:
        conn.close()
    return events, by_input


def _owned_by_id(events) -> dict:
    """First candidate per owned id, in derivation order (a later divergent
    re-derivation of one id is what live ingest withholds as a conflict)."""
    out: dict = {}
    for event in events:
        if _lib_rederive._is_owned_event(event):
            out.setdefault(event["id"], event)
    return out


def _effective_records(selection) -> dict:
    """Every selected id's effective record (None for a tombstone)."""
    return {event_id: (item.record if item.status == "active" else None)
            for event_id, item in selection.by_id.items()
            if item.status in ("active", "tombstone")}


def _retention(effective, prefix, owed, *, legacy, reference=None):
    retention = _journal.ScratchRetention(
        effective, reference=reference, retained_only=legacy)
    retention.seed(prefix.before_ids)
    retention.seed(owed)
    return retention


#: The snapshot decision id the live pipeline mints for a Claude usage
#: observation (`_cctally_record._pipeline_claude_usage`).
_SNAPSHOT_ID_PREFIX = "sa:"


def _is_legacy_input(record) -> bool:
    """Whether a window input was itself written by a previous release:
    unstamped, or stamped by a writer generation other than this one."""
    return (isinstance(record, dict) and record.get("t") == "obs"
            and not _lib_account_protocol.is_stamped(record))


def _legacy_decided_inputs(prefix, by_input=None) -> frozenset:
    """The window inputs a previous release decided (fix round E): a LEGACY
    input (`_is_legacy_input`) one of whose decisions is an admitted
    UNSTAMPED revision-0 event. Its history is exactly what that release
    journaled, which the current pipeline may spell or extend differently,
    so it is never a gap and its extra fresh candidates are never
    materialized. Before a replay only the snapshot linkage is known;
    ``by_input`` adds every derived id.

    Both conditions are required (revision 2, Codex design-fix-round-3 Q6,
    P1-1). Legacy provenance alone never exempts a missing decision, and a
    CURRENT-stamped input never becomes history because one of its derived
    candidates collides with a retained legacy event identity: a same-second
    current reading re-emits the retained closure of a five-hour block a
    previous-release reading closed at that second, and treating that retry
    as the reading's own history left the reading's decision unjournaled."""
    legacy = prefix.legacy_origin_ids
    out = {index for index, record in enumerate(prefix.window)
           if _is_legacy_input(record)
           and _SNAPSHOT_ID_PREFIX + str(record.get("id")) in legacy}
    for index, derived in (by_input or {}).items():
        if not (0 <= index < len(prefix.window)
                and _is_legacy_input(prefix.window[index])):
            continue
        if any(event["id"] in legacy for event in derived
               if _lib_rederive._is_owned_event(event)):
            out.add(index)
    return frozenset(out)


def _gap_inputs(by_input, selection, legacy) -> frozenset:
    """The inputs with a derived decision no admitted decision answers,
    previous-release inputs (``legacy``) excepted. Only the with-gap /
    without-gap comparison, never an absent id alone, says what a gap
    owes."""
    return frozenset(
        index for index, derived in by_input.items()
        if index not in legacy
        and any(_lib_rederive._is_owned_event(event)
                and event["id"] not in selection.by_id
                for event in derived))


def _complete_window(base, tmp, start, target, boundary, round_index, *,
                     owed=frozenset()) -> bool:
    """Plan and journal one round. Returns whether anything was appended.

    Three replays from the predecessor, each keeping the projection on the
    retained decisions (`_journal.ScratchRetention`): one over every input
    finds the gap; one without the gap is the reference; one with the gap
    holds the fresh candidate only where it differs from the reference, so a
    gap's effect reaches its descendants while every unaffected decision
    stays the retained one. Their difference is the causal suffix."""
    prefix = _read_prefix(start, target, boundary)
    if prefix.cutover_claude is _journal._CUTOVER_UNSEEN:
        prefix.cutover_claude = _journal._resolve_cutover_for_rebuild(
            _journal._CUTOVER_UNSEEN, target, _journal.list_segments(),
            {"lines": 0, "bytes": 0, "decodes": 0}, boundary=boundary)
    elif prefix.cutover_claude is None:
        prefix.cutover_claude = _lib_accounts.UNATTRIBUTED
    for record in prefix.slots:
        if record is not None:
            _journal._normalize_legacy_account_stamp(
                record, prefix.cutover_claude)
    selection = _lib_journal.resolve_effective_events(
        prefix.slots, protocol_prefix_evidence=tuple(prefix.evidence))
    # Inert (incomplete or tainted) batch actions of OTHER families still
    # occupy their revision: a later retry of that batch must not collide.
    for record in prefix.slots:
        if (isinstance(record, dict) and record.get("t") == "correction"
                and record.get("batch") not in selection.completed_batches
                and prefix.batch_family.get(record.get("batch")) != FAMILY):
            _bump(prefix.inert_revisions, record)
    held_ids = _held_observation_ids(prefix.slots)
    effective = _effective_records(selection)

    legacy = _legacy_decided_inputs(prefix)

    with _cache_read_view() as cache_conn:
        _events, by_input = _replay(
            base, tmp, f"all-{round_index}.db", prefix, held_ids, cache_conn,
            retention=_retention(effective, prefix, owed, legacy=legacy))
        legacy = _legacy_decided_inputs(prefix, by_input)
        gap = _gap_inputs(by_input, selection, legacy)
        if not gap:
            return False
        events_a, _ = _replay(
            base, tmp, f"without-{round_index}.db", prefix, held_ids,
            cache_conn, skip=gap,
            retention=_retention(effective, prefix, owed, legacy=legacy))
        reference: dict = {}
        for event in events_a:
            reference.setdefault(
                event["id"], _lib_journal.event_content_hash(event))
        events_b, _ = _replay(
            base, tmp, f"with-{round_index}.db", prefix, held_ids, cache_conn,
            retention=_retention(effective, prefix, owed, legacy=legacy,
                                 reference=reference))
    with_gap = _owned_by_id(events_b)
    without_gap = _owned_by_id(events_a)

    plain: list = []
    actions: list = []
    for event_id in sorted(set(with_gap) | set(without_gap)):
        desired = with_gap.get(event_id)
        reference = without_gap.get(event_id)
        if (desired is not None and reference is not None
                and _lib_rederive._semantic_event(desired)
                == _lib_rederive._semantic_event(reference)):
            # Not caused by the gap: an unrelated difference against the
            # journal (a repricing, a previous release's input the current
            # pipeline extends) is never acted on.
            continue
        selected = selection.by_id.get(event_id)
        current = (selected.record if selected is not None
                   and selected.status == "active" else None)
        physical = [rev for rev in (
            prefix.excluded_revisions.get(event_id),
            prefix.inert_revisions.get(event_id),
            None if selected is None else selected.rev,
        ) if rev is not None]
        if desired is None:
            if current is not None:
                actions.append({"action": "tombstone", "id": event_id,
                                "rev": max(physical) + 1,
                                "at": str(current["at"]), "payload": None})
            continue
        if current is not None:
            desired = _lib_rederive._preserve_non_derivable_state(
                current, desired)
            if (_lib_rederive._semantic_event(current)
                    == _lib_rederive._semantic_event(desired)):
                continue  # an admitted identical decision: nothing
        if not physical:
            plain.append(desired)
            continue
        actions.append({"action": "replace", "id": event_id,
                        "rev": max(physical) + 1,
                        "at": str(desired["at"]),
                        "payload": dict(desired.get("payload") or {})})

    records: list = []
    if actions:
        digest = hashlib.sha256(json.dumps(
            actions, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        batch_id = f"{FAMILY}:{digest}"
        if batch_id not in selection.completed_batches:
            records.extend(_lib_journal.make_correction_batch(
                batch_id=batch_id, family=FAMILY,
                # An interrupted earlier attempt's `at`, so the retry's lines
                # are byte-identical and the selector keeps each once.
                at=str(prefix.begin_at.get(batch_id) or _iso_now()),
                actions=actions))
    order = {id(event): index for index, event in enumerate(events_b)}
    plain.sort(key=lambda event: order.get(id(event), 0))
    records.extend(plain)
    if not records:
        return False
    _journal.append_records(records)
    return True
