"""The launch ledger of the detached stats rebuild workers (#997, spec D8).

On 2026-10-22 an epoch-rebuild worker ran for hours and nothing recorded
whether one worker ran throughout or one died and another replaced it: the log
held only returned outcomes. Each worker (the epoch rebuild and the corruption
heal) now keeps two private records in ``APP_DIR``: a run state, written only
by the holder of the worker flock, and this launch ledger, written only under
the worker's admission flock. This module is the pure kernel that decides what
became of each launch; spawners, workers, ``doctor`` and ``db status`` all
classify through it, and only spawners and workers persist the result.

The central rule: **a death is settled by evidence, never by elapsed time.**
An unsettled entry is settled when

(a) the run state shows its ``launchId`` finished, which transfers that
    outcome as ``finished:<outcome>``, or records its in-run death; or
(b) its process is confirmed gone by identity — absent, a zombie, or a live
    pid whose ``birth`` differs — which settles ``died-pre-start`` without a
    start acknowledgement and ``died-in-run`` with one. The caller probes the
    identities BEFORE it reads the files it passes here, so a re-read after the
    probe is what this function sees.

A process that is alive, or whose liveness is unknown (``ps`` failing, a live
pid with a null ``birth``), stays unresolved however old the entry is: a slow
child is never declared dead.

Ledger shape (``schemaVersion`` 1)::

    {"schemaVersion": 1,
     "entries": [{"launchId", "at", "pid", "birth",
                  "spawn": "reserved"|"ok"|"failed",
                  "ack": None | {"kind": "started", "runId", "at"}
                              | {"kind": "busy", "at"},
                  "outcome": None | <terminal>, "settledAt"}],
     "counts": {<terminal>: int},     # cumulative, survives pruning
     "lastDeathAt": str | None}

``pid`` and ``birth`` are null while ``spawn`` is ``reserved``; ``birth`` may
stay null after it (``ps`` could not see the child). Unresolved entries are
never dropped: the spawner admits no launch while ``LAUNCH_CAPACITY`` are
unresolved (``has_launch_capacity``), which is what bounds them.

``<terminal>`` is ``finished:<worker outcome>``, ``busy``, ``spawn-failed``,
``died-pre-start`` or ``died-in-run``. Every function returns a new ledger and
never mutates its input. Stdlib only, and importable on its own.
"""
from __future__ import annotations

import copy

SCHEMA_VERSION = 1
#: Unresolved entries at which the spawner withholds further launches.
LAUNCH_CAPACITY = 100
#: Settled entries kept (the most recent); ``counts`` keeps the totals.
SETTLED_KEEP = 10
DEATHS = ("died-pre-start", "died-in-run")


def empty_ledger() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "entries": [],
        "counts": {},
        "lastDeathAt": None,
    }


def normalize(ledger) -> dict:
    """A validated deep copy of ``ledger``; ``ValueError`` when malformed."""
    if not isinstance(ledger, dict):
        raise ValueError("launch ledger is not an object")
    if ledger.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("unsupported launch ledger schemaVersion")
    entries = ledger.get("entries", [])
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("launchId"), str)
            for e in entries):
        raise ValueError("launch ledger entries are malformed")
    counts = ledger.get("counts") or {}
    if not isinstance(counts, dict) or not all(
            isinstance(k, str) and isinstance(v, int)
            for k, v in counts.items()):
        raise ValueError("launch ledger counts are malformed")
    last_death = ledger.get("lastDeathAt")
    if last_death is not None and not isinstance(last_death, str):
        raise ValueError("launch ledger lastDeathAt is malformed")
    out = empty_ledger()
    out["entries"] = [_entry(**{k: e.get(k) for k in _ENTRY_KEYS})
                      for e in copy.deepcopy(entries)]
    out["counts"] = dict(counts)
    out["lastDeathAt"] = last_death
    return out


_ENTRY_KEYS = ("launchId", "at", "pid", "birth", "spawn", "ack", "outcome",
               "settledAt")


def _entry(launchId, at, pid, birth, spawn="ok", ack=None, outcome=None,
           settledAt=None) -> dict:
    return {
        "launchId": launchId, "at": at, "pid": pid, "birth": birth,
        "spawn": spawn or "ok", "ack": ack, "outcome": outcome,
        "settledAt": settledAt,
    }


def find(ledger: dict, launch_id: str) -> "dict | None":
    for entry in ledger.get("entries", ()):
        if entry.get("launchId") == launch_id:
            return entry
    return None


def unresolved(ledger: dict) -> list:
    return [e for e in ledger.get("entries", ()) if not e.get("outcome")]


def newest(ledger: dict) -> "dict | None":
    """The newest launch in ledger (launch) order."""
    entries = ledger.get("entries") or []
    return entries[-1] if entries else None


def _settle_in_place(ledger: dict, entry: dict, terminal: str, at) -> bool:
    if entry.get("outcome"):
        return False
    entry["outcome"] = terminal
    entry["settledAt"] = at
    counts = ledger.setdefault("counts", {})
    counts[terminal] = int(counts.get(terminal, 0)) + 1
    if terminal in DEATHS:
        ledger["lastDeathAt"] = at or entry.get("at")
    return True


def append_entry(ledger: dict, *, launch_id: str, at, pid, birth,
                 spawn: str = "ok") -> dict:
    """Record a launch. A launch id already present is left as it is (the
    worker's own acknowledgement may have recorded it first). A failed spawn
    is settled at once as ``spawn-failed``."""
    out = copy.deepcopy(ledger)
    if find(out, launch_id) is not None:
        return out
    entry = _entry(launch_id, at, pid, birth, spawn=spawn)
    out.setdefault("entries", []).append(entry)
    if spawn == "failed":
        _settle_in_place(out, entry, "spawn-failed", at)
    return out


def reserve(ledger: dict, *, launch_id: str, at) -> dict:
    """The spawner's reservation, persisted BEFORE the spawn (revision 6): a
    spawner killed after it leaves an entry with no pid, which stays
    unresolved with unknown liveness until the child acknowledges."""
    out = copy.deepcopy(ledger)
    if find(out, launch_id) is None:
        out.setdefault("entries", []).append(
            _entry(launch_id, at, None, None, spawn="reserved"))
    return out


def fill_spawn(ledger: dict, launch_id: str, *, pid, birth, at,
               spawn: str = "ok") -> dict:
    """Complete a reservation with the spawn's result. A failed spawn is
    settled at once as ``spawn-failed``. An identity the worker already
    recorded at acknowledgement is never overwritten."""
    out = copy.deepcopy(ledger)
    entry = find(out, launch_id)
    if entry is None:
        out.setdefault("entries", []).append(
            _entry(launch_id, at, None, None, spawn="reserved"))
        entry = out["entries"][-1]
    if spawn == "failed":
        if entry.get("spawn") == "reserved":
            entry["spawn"] = "failed"
        _settle_in_place(out, entry, "spawn-failed", at)
        return out
    if entry.get("pid") is None:
        entry["pid"] = pid
        entry["birth"] = birth
    if entry.get("spawn") == "reserved":
        entry["spawn"] = "ok"
    return out


def acknowledge(ledger: dict, launch_id: str, kind: str, run_id=None, *,
                at, pid=None, birth=None) -> dict:
    """A worker's acknowledgement: ``started`` (with its ``runId``) or
    ``busy`` (which is also the entry's terminal). Fills the worker's own
    ``pid`` and ``birth`` into a reservation the spawner never completed, and
    appends the entry when there is none (a ledger lost to the unreadable
    archive path)."""
    out = copy.deepcopy(ledger)
    entry = find(out, launch_id)
    if entry is None:
        entry = _entry(launch_id, at, pid, birth)
        out.setdefault("entries", []).append(entry)
    elif entry.get("pid") is None and pid is not None:
        entry["pid"] = pid
        entry["birth"] = birth
        if entry.get("spawn") == "reserved":
            entry["spawn"] = "ok"
    if kind == "started":
        entry["ack"] = {"kind": "started", "runId": run_id, "at": at}
    elif kind == "busy":
        entry["ack"] = {"kind": "busy", "at": at}
        _settle_in_place(out, entry, "busy", at)
    else:
        raise ValueError(f"unknown acknowledgement kind {kind!r}")
    return out


def settle(ledger: dict, launch_id: str, terminal: str, *, at) -> dict:
    """Give an entry its terminal outcome; the first settlement wins."""
    out = copy.deepcopy(ledger)
    entry = find(out, launch_id)
    if entry is not None:
        _settle_in_place(out, entry, terminal, at)
    return out


def liveness(entry: dict, identities: dict) -> str:
    """``alive`` | ``gone`` | ``unknown`` for a ledger entry or a run (both
    carry ``pid`` and ``birth``), from ``process_identity`` results."""
    pid = entry.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    identity = identities.get(pid)
    if not isinstance(identity, dict):
        return "unknown"
    status = identity.get("status")
    if status == "gone":
        return "gone"
    if status != "alive":
        return "unknown"
    entry_birth, live_birth = entry.get("birth"), identity.get("birth")
    if entry_birth and live_birth and entry_birth != live_birth:
        return "gone"  # the pid now names a different process
    if not entry_birth or not live_birth:
        return "unknown"
    return "alive"


def classify(ledger: dict, state, identities: dict, *, now=None) -> dict:
    """Settle what the evidence settles. Returns ``{"ledger", "settled"}``,
    ``settled`` being the newly settled entries (copies). Never settles by
    age; see the module docstring for rules (a) and (b)."""
    out = copy.deepcopy(ledger)
    run = state.get("run") if isinstance(state, dict) else None
    run = run if isinstance(run, dict) else None
    deaths = state.get("deaths") if isinstance(state, dict) else None
    # A death whose ``launchId`` is not a string (a corrupt record in a
    # state that still validates) settles nothing.
    died_in_run = {d.get("launchId") for d in (deaths or ())
                   if isinstance(d, dict) and isinstance(d.get("launchId"), str)
                   and d.get("launchId")}
    settled = []
    for entry in out.get("entries", []):
        if entry.get("outcome"):
            continue
        launch_id = entry.get("launchId")
        terminal = None
        if (run is not None and run.get("launchId") == launch_id
                and run.get("status") == "finished"):
            terminal = f"finished:{run.get('outcome')}"
        elif launch_id in died_in_run:
            terminal = "died-in-run"
        elif liveness(entry, identities) == "gone":
            ack = entry.get("ack")
            started = isinstance(ack, dict) and ack.get("kind") == "started"
            terminal = "died-in-run" if started else "died-pre-start"
        if terminal is not None and _settle_in_place(out, entry, terminal,
                                                     now):
            settled.append(copy.deepcopy(entry))
    return {"ledger": out, "settled": settled}


def _protected_ids(protected_launch_ids) -> frozenset:
    if isinstance(protected_launch_ids, str):
        return frozenset({protected_launch_ids})
    if not isinstance(protected_launch_ids, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(i for i in protected_launch_ids if isinstance(i, str))


def prune(ledger: dict, protected_launch_ids=None) -> dict:
    """Bound the settled entries to the newest ``SETTLED_KEEP``, in ledger
    order. Unresolved entries are never dropped (``has_launch_capacity``
    bounds them); ``counts`` and ``lastDeathAt`` are kept.

    ``protected_launch_ids`` (one id, or an iterable of them; a non-string
    member protects nothing) names the entries a run state references or is
    about to: the current ``run.launchId`` and, for a worker whose own state
    write has not landed yet, its own launch as well. Each is exempt from the
    bound, because the slot's entry is the record that makes the next
    worker's death accounting exactly-once, and the settled bound then counts
    them plus ``SETTLED_KEEP`` minus their number of others."""
    out = copy.deepcopy(ledger)
    entries = out.get("entries", [])
    wanted = _protected_ids(protected_launch_ids)
    protected = {id(e) for e in entries if e.get("launchId") in wanted}
    done_ids = [id(e) for e in entries
                if e.get("outcome") and id(e) not in protected]
    settled_room = max(0, SETTLED_KEEP - sum(
        1 for e in entries if id(e) in protected and e.get("outcome")))
    dropped = set(done_ids[:max(0, len(done_ids) - settled_room)])
    out["entries"] = [e for e in entries if id(e) not in dropped]
    return out


def has_launch_capacity(ledger: dict) -> bool:
    """Whether the spawner may admit another launch: fewer than
    ``LAUNCH_CAPACITY`` entries are unresolved."""
    return len(unresolved(ledger)) < LAUNCH_CAPACITY
