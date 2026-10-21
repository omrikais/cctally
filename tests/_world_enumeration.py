"""The bounded world-enumeration oracle for #825/#832 (spec section 9, case
F17; section 5.6 and constraint C5).

A **trace** is a list of steps through the real pipeline: a ``current`` step is
a bound reading through `record_usage_bound` (real append, real ingest); a
``legacy`` step appends the previous release's unstamped observation and pauses
before its ingest, so the next current ingest excludes it. ``rebuild`` and
``rederive`` steps run the real `db rebuild --db stats` / `db rederive`.

C5 judges the mixed trace against every **reference interpretation** of the
uniform-current trace over the same timeline: each excluded reading is absent
(a torn binding) or present as a bound current reading under ``A``, ``B`` or
``unattributed``. Two excluded readings therefore give sixteen worlds.

The check (`check`): at every processed prefix, the mixed trace's outcomes are
a sub-multiset of the intersection of every world's outcomes at the same
prefix. Outcomes minted before the first excluded reading are compared
separately — every world must hold exactly the mixed trace's — and must still
be present at the end of the mixed trace, so withholding cannot hide a lost
earlier outcome. Comparison keys are those of `_outcome_quarantine_trace.
outcomes`: credits by their complete accounting payload (every column but
the physical row and journal ids — `detected_at_utc` included, fix round O)
and multiplicity, transitions by (account, old end, new end).

Every world runs the mixed trace's maintenance steps — the same real `db
rebuild --db stats` and real `db rederive`, at the same positions (fix round
O4, review CD5-O01). C5's reference is the uniform-current trace over the
SAME timeline, maintenance included. An earlier revision ran the worlds
without maintenance, on the argument that a rebuild can only lose detector
state; that direction is the wrong one for a subset check: a larger reference
outcome set weakens "mixed is inside every world". A five-hour credit armed
before a rebuild and confirmed after it is absent from the maintained world
(the admitted cold-start gap, `docs/five-hour-gotchas.md`) but present in a
maintenance-free one, so a mixed rebuild that wrongly restored the disposable
confirmation state passed (pinned by `test_f17_rejects_restored_confirmation_
state_across_a_rebuild`). Uniform-current replay equivalence itself is case
M19's.

Execution shares prefixes: every trace in a batch is inserted into a trie of
steps, and the trie is walked depth-first with the data directory snapshotted
at each branch point and restored before every later sibling. A fresh store
per trace gives the same results (pinned by `test_f17_trie_execution_matches_
fresh_stores`) at a fraction of the cost. The mixed traces run first, and a
reference world stops at its first maintenance step when the mixed trace
holds no outcome from there on (`reference_horizon`): an empty mixed
multiset constrains no world, so the check is the full check (pinned by
`test_f17_lazy_reference_horizon_is_exact`), and the worlds' real rebuilds
and rederives run wherever a mixed outcome is compared with them.
"""
from __future__ import annotations

import collections
import dataclasses
import itertools
import json
import shutil
from typing import Iterator

import _outcome_quarantine_trace as T

#: The account each world may give an excluded reading. None = absent.
INTERPRETATIONS = (None, "A", "B", "unattributed")
CATEGORIES = ("in_place", "transitions", "five_hour")
EMPTY_OUTCOMES = {category: [] for category in CATEGORIES}


class OracleViolation(AssertionError):
    """A mixed outcome some reference world does not hold."""


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def _freeze(values: dict) -> tuple:
    return tuple(sorted(values.items()))


def step_key(step) -> tuple:
    kind, values = step
    return (kind, _freeze(values or {}))


def world_steps(steps, interpretation) -> list:
    """The uniform-current trace one world assigns: the i-th legacy step is
    ``interpretation[i]`` — dropped (a ``skip`` position) or a bound current
    reading under that account, with its own capture instant as its clock.
    Every other step — a current reading, and a real rebuild or rederive —
    is kept at its position, so a world runs the mixed trace's own
    maintenance schedule."""
    out, index = [], 0
    for kind, values in steps:
        if kind == "legacy":
            account = interpretation[index]
            index += 1
            if account is None:
                out.append(("skip", {}))
                continue
            values = dict(values)
            captured = values.pop("captured", None)
            values["account"] = account
            if captured is not None:
                values["at"] = captured
            out.append(("current_x", values))
        else:
            out.append((kind, values))
    return out


def worlds(steps) -> Iterator[tuple]:
    count = sum(1 for kind, _ in steps if kind == "legacy")
    return itertools.product(INTERPRETATIONS, repeat=count)


def trigger_index(steps) -> int:
    for index, (kind, _values) in enumerate(steps):
        if kind == "legacy":
            return index
    return len(steps)


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _current_allowing_rejection(app, monkeypatch, *, at, weekly, week_end,
                                five=None, five_end=None, account="A",
                                source="api"):
    """A world's reading of an excluded observation, through the same bound
    kernel as `T.current`. Unlike a current role reading it may sit outside the
    ingress bands (the 8-day or 30-day weekly edge), and the current binary
    then rejects it with exit 2 — in that world it is simply never recorded,
    the same as "absent"."""
    import argparse
    import _cctally_statusline as sl
    T.set_clock(monkeypatch, at)
    T.identity(account)
    args = argparse.Namespace(
        percent=weekly, resets_at=str(T.epoch(week_end)),
        five_hour_percent=five,
        five_hour_resets_at=None if five_end is None else str(T.epoch(five_end)),
        source=source)
    rc = app.record_usage_bound(args, binding=sl._capture_binding(),
                                nudge_dashboard=False)
    assert rc in (0, 2), rc


class Store:
    """One process-local scratch store, reset by restoring a directory
    snapshot. Built on `_outcome_quarantine_trace.make_app`, with priced
    session input retained for A, B and `unattributed` so a real rederive has
    its source."""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.app = T.make_app(monkeypatch, tmp_path / "store")
        # Imported AFTER `make_app`: `load_script` drops every cached
        # `_cctally_*` sibling, so a module object imported before it is not
        # the one the app calls (fix round O4 measured the stub below
        # silently inert that way).
        import _cctally_core as core
        import _cctally_db
        # `db rebuild` first writes a corruption-forensics bundle (`lsof`, a
        # process scan): diagnostics only, outcome-neutral, and most of a
        # rebuild's cost on a store this small.
        monkeypatch.setattr(_cctally_db, "write_corruption_forensics",
                            lambda *args, **kwargs: None)
        # One parser per store for the maintenance commands: building the
        # whole CLI parser cost more than the rebuild itself.
        self._parser = self.app.build_parser()
        for name in ("A", "B", "unattributed"):
            T.retain_session_input(self.app, name)
        self.app_dir = core.APP_DIR
        self.snapshots = tmp_path / "snapshots"
        self.snapshots.mkdir()
        self._serial = 0
        self.template = self.save()
        self.executed = 0

    def save(self):
        self._serial += 1
        target = self.snapshots / str(self._serial)
        shutil.copytree(self.app_dir, target, copy_function=shutil.copyfile)
        return target

    def restore(self, snapshot) -> None:
        shutil.rmtree(self.app_dir)
        # Fresh inodes and mtimes, so no in-process memo keyed on a file's
        # identity can mistake the restored store for the one it replaced.
        shutil.copytree(snapshot, self.app_dir, copy_function=shutil.copyfile)

    def apply(self, step) -> None:
        kind, values = step
        self.executed += 1
        if kind == "current":
            T.current(self.app, self.monkeypatch, **values)
        elif kind == "current_x":
            _current_allowing_rejection(self.app, self.monkeypatch, **values)
        elif kind == "legacy":
            T.legacy(self.app, self.monkeypatch, **values)
        elif kind == "rebuild":
            self._command(T.REBUILD_ARGV)
        elif kind == "rederive":
            self._command(T.REDERIVE_ARGV)
        elif kind == "skip":
            pass
        else:  # pragma: no cover - a grammar typo
            raise ValueError(kind)

    def _command(self, argv) -> None:
        """The real `T.rebuild` / `T.rederive` command, parsed by the
        store's parser."""
        args = self._parser.parse_args(list(argv))
        assert args.func(args) == 0, argv

    def outcomes(self) -> dict:
        """`T.outcomes` over one connection (it is read after every step):
        the same shared projection, never a column list of its own (fix
        round O, review CD4-O02)."""
        conn = self.app.open_db()
        try:
            return T.outcomes_from(conn)
        finally:
            conn.close()

    def run_all(self, sequences) -> list:
        """Run every step sequence from the empty store; return, per
        sequence, its outcomes after every step. Shared prefixes run once."""
        root: dict = {"children": {}, "ends": []}
        for index, seq in enumerate(sequences):
            node = root
            for step in seq:
                node = node["children"].setdefault(
                    step_key(step), {"step": step, "children": {}, "ends": []})
            node["ends"].append(index)
        results: list = [None] * len(sequences)
        self.restore(self.template)
        self._walk(root, [], results)
        return results

    def _walk(self, node, trail, results) -> None:
        for index in node["ends"]:
            results[index] = list(trail)
        children = list(node["children"].values())
        snapshot = self.save() if len(children) > 1 else None
        for position, child in enumerate(children):
            if position:
                self.restore(snapshot)
            self.apply(child["step"])
            # A legacy append runs no ingest and a skip position runs nothing,
            # so neither can change an outcome: reuse the previous snapshot.
            if child["step"][0] in ("legacy", "skip") and trail:
                snap = trail[-1]
            elif child["step"][0] in ("legacy", "skip"):
                snap = EMPTY_OUTCOMES
            else:
                snap = self.outcomes()
            self._walk(child, trail + [snap], results)
        if snapshot is not None:
            shutil.rmtree(snapshot)


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def _counter(snapshot, category):
    return collections.Counter(snapshot[category])


def check(steps, mixed, world_results, *, label="") -> None:
    """``mixed`` and every entry of ``world_results`` are per-step outcome
    lists aligned with ``steps``. Raises `OracleViolation`.

    A world entry is ``None`` at a position `run_and_check` did not run
    (`reference_horizon`); that is legal only where the mixed trace holds no
    outcome at all, and anything else is a harness error."""
    first = trigger_index(steps)
    for position, snapshot in enumerate(mixed):
        unrun = [world for world, result in world_results.items()
                 if result[position] is None]
        if unrun:
            if position < first or outcome_total(snapshot):
                raise AssertionError(
                    f"{label}: harness error: world {unrun[0]} was not run at "
                    f"step {position}, where the mixed trace constrains it")
            continue
        for category in CATEGORIES:
            have = _counter(snapshot, category)
            if position < first:
                for world, result in world_results.items():
                    if _counter(result[position], category) != have:
                        raise OracleViolation(
                            f"{label}: pre-trigger {category} differ at step "
                            f"{position} in world {world}: "
                            f"{snapshot[category]} vs {result[position][category]}")
                continue
            for world, result in world_results.items():
                missing = have - _counter(result[position], category)
                if missing:
                    raise OracleViolation(
                        f"{label}: step {position} ({steps[position][0]}) mints "
                        f"{category} {sorted(missing)} absent from world {world}")
    if 0 < first <= len(mixed):
        for category in CATEGORIES:
            lost = _counter(mixed[first - 1], category) - _counter(
                mixed[-1], category)
            if lost:
                raise OracleViolation(
                    f"{label}: pre-trigger {category} {sorted(lost)} lost by the "
                    "end of the trace")


def outcome_total(snapshot) -> int:
    return sum(len(snapshot[category]) for category in CATEGORIES)


MAINTENANCE = ("rebuild", "rederive")


def reference_horizon(steps, mixed) -> int:
    """How many steps of each reference world the check needs (fix round
    O4, review CD5-O01): every step, unless the mixed trace holds no outcome
    at all from its first maintenance step after the trigger to its end. A
    position where the mixed outcome multiset is empty constrains no world
    (the empty multiset is inside every one), so the worlds' real rebuilds
    and rederives — most of an F17 trace's cost — run exactly where some
    mixed outcome is compared with them. The worlds' readings before that
    point always run."""
    first = trigger_index(steps)
    for position in range(first, len(steps)):
        if steps[position][0] in MAINTENANCE:
            if all(outcome_total(snapshot) == 0
                   for snapshot in mixed[position:]):
                return position
            break
    return len(steps)


def run_and_check(store, traces) -> dict:
    """Run every mixed trace, then all of their worlds in one trie walk,
    check each, and return ``{label: (mixed, {world: result})}``. ``traces``
    maps a label to its step list. A world shared by several traces runs to
    the longest `reference_horizon` they need; past it its result is
    ``None``."""
    labels = list(traces)
    mixed_results = dict(zip(labels, store.run_all(
        [traces[label] for label in labels])))
    unique: dict = {}
    refs = {}
    for label, steps in traces.items():
        horizon = reference_horizon(steps, mixed_results[label])
        for world in worlds(steps):
            seq = world_steps(steps, world)
            key = tuple(step_key(step) for step in seq)
            entry = unique.setdefault(key, [seq, 0])
            entry[1] = max(entry[1], horizon)
            refs[(label, world)] = key
    keys = list(unique)
    ran = store.run_all([unique[key][0][:unique[key][1]] for key in keys])
    by_key = {key: result + [None] * (len(unique[key][0]) - len(result))
              for key, result in zip(keys, ran)}
    out = {}
    for label, steps in traces.items():
        mixed = mixed_results[label]
        per_world = {world: by_key[refs[(label, world)]]
                     for world in worlds(steps)}
        check(steps, mixed, per_world, label=label)
        out[label] = (mixed, per_world)
    return out


def dump(result) -> str:  # pragma: no cover - failure text only
    return json.dumps(result, indent=1, default=str)


# --------------------------------------------------------------------------
# skeleton grammar
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Skeleton:
    """A fixed-role trace of at most eight readings with two excluded slots.

    ``steps`` are ``(role, values)`` pairs: role ``P``/``Q`` is a current
    reading by the primary/secondary account, role ``X`` an excluded reading
    whose values come from ``values`` (four representative overrides per
    slot). ``further`` are the readings after the rebuild and the rederive.
    ``genuine`` is how many outcomes the no-exclusion variant (both slots read
    as the primary account's current readings) must mint. The
    certified-recovery variant appends the shared `RECOVERY` suffix."""

    name: str
    roles: tuple
    steps: tuple
    values: tuple
    further: tuple
    genuine: int


@dataclasses.dataclass(frozen=True)
class Variant:
    order: int      # 0: P=A, Q=B; 1: P=B, Q=A
    source: str     # the current readings' default source
    offset: int     # seconds added to every raw five-hour reset


#: The certified-recovery suffix shared by every family: an idle gap that
#: spans every family's weekly reset and five-hour window end (and the 8-day
#: ingress edge), a two-axis witness in a fresh week and window, then a
#: genuine same-source five-hour descent and its confirmation — one credit.
RECOVERY = (
    ("P", dict(at="2026-10-20T13:00:00Z", weekly=20, week_end="2026-10-27T12:00:00Z",
               five=70, five_end="2026-10-20T18:00:00Z")),
    ("P", dict(at="2026-10-20T13:10:00Z", weekly=20, week_end="2026-10-27T12:00:00Z",
               five=20, five_end="2026-10-20T18:00:00Z")),
    ("P", dict(at="2026-10-20T13:20:00Z", weekly=20, week_end="2026-10-27T12:00:00Z",
               five=20, five_end="2026-10-20T18:00:00Z")),
)
RECOVERY_GENUINE = 1
RECOVERY_FURTHER = (
    ("P", dict(at="2026-10-20T13:30:00Z", weekly=21, week_end="2026-10-27T12:00:00Z",
               five=21, five_end="2026-10-20T18:00:00Z")),
    ("P", dict(at="2026-10-20T13:40:00Z", weekly=22, week_end="2026-10-27T12:00:00Z",
               five=22, five_end="2026-10-20T18:00:00Z")),
)

VARIANTS = tuple(Variant(order, source, offset)
                 for order in (0, 1) for source in ("api", "statusline")
                 for offset in (0, -1))


def _shift(iso_text, seconds):
    if not seconds or iso_text is None:
        return iso_text
    return T.iso(T.epoch(iso_text) + seconds)


def _concrete(role, values, variant, override=None):
    names = ("A", "B") if variant.order == 0 else ("B", "A")
    values = dict(values)
    if override:
        values.update(override)
    if "five_end" in values:
        values["five_end"] = _shift(values["five_end"], variant.offset)
    if role == "X":
        values.setdefault("account", names[1])
        account = values.pop("account")
        acct = None if account is None else (
            names[0] if account == "P" else names[1] if account == "Q"
            else account)
        values.setdefault("source", variant.source)
        values["account"] = acct
        return ("legacy", values)
    values.setdefault("source", variant.source)
    values["account"] = names[0] if role == "P" else names[1]
    return ("current", values)


#: The value pairs the gate runs per (skeleton, variant). Each of the four
#: representative values appears once in each excluded slot, every slot-1
#: value meets a different slot-2 value, and the no-exclusion pair (0, 0) is
#: included: 4 mixed traces and 61 distinct reference worlds per variant.
#: Spec section 9 sizes the full product at every pair (16 mixed traces, 169
#: worlds); that costs about an hour of worker time per suite run (measured
#: 3,583 s over the 13 families on the runner), so the full product runs on
#: demand with `CCTALLY_F17_FULL=1` and its measured run is the acceptance
#: evidence (`.local/reports/825-832-account-bound/acceptance/`).
GATE_PAIRS = ((0, 0), (1, 2), (2, 3), (3, 1))


def value_pairs(count: int = 4) -> tuple:
    import os
    if os.environ.get("CCTALLY_F17_FULL") == "1":
        return tuple(itertools.product(range(count), repeat=2))
    return GATE_PAIRS


def mixed_traces(skel: Skeleton, variant: Variant, *, recovery=False,
                 maintenance=True, pairs=None) -> dict:
    """The skeleton's mixed traces for one variant, one per value pair in the
    two excluded slots (`value_pairs`)."""
    out = {}
    for first, second in (pairs or value_pairs(len(skel.values))):
        picks = iter((skel.values[first], skel.values[second]))
        steps = []
        for role, values in skel.steps:
            steps.append(_concrete(role, values, variant,
                                   next(picks) if role == "X" else None))
        if recovery:
            steps += [_concrete(role, values, variant)
                      for role, values in RECOVERY]
        further = RECOVERY_FURTHER if recovery else skel.further
        tail = [_concrete(role, values, variant) for role, values in further]
        if maintenance:
            steps += [("rebuild", {}), tail[0], ("rederive", {}), tail[1]]
        else:
            steps += tail
        out[f"{skel.name}/{variant}/{first}{second}{'/rec' if recovery else ''}"] = steps
    return out
