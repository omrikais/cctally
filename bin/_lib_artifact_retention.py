"""Pure kernel for retained-artifact retention (#496 S6, spec §3 / §4 / §6.5).

This module takes **no filesystem, no locks, no clock and no config**. Every
decision it makes is a function of the values it is handed. All I/O — the
metadata walk, JSON parsing, `disk_usage`, config reading, locking, tombstone
renames, deletion — lives in `bin/_cctally_retention.py` and the producer glue.

`resolve_retention_policy` (§6.5) is the **strict** resolver over the raw
`storage.artifact_retention` block. It never falls back to defaults on
malformed input, because a policy the user never wrote must not arm deletion;
`load_config()` cannot be used for this reason (spec C14).
"""
from __future__ import annotations

import dataclasses
import re

# Public surface: shipped in the npm tarball + brew formula + public mirror.

_SECONDS_PER_DAY = 86400
_BYTES_PER_MIB = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    """A resolved, validated retention policy in canonical units.

    `None` on any of the first four fields means that rule is disabled.
    `max_shape_examples` is never `None` — the shape floor (§3.4) is binding
    by maintainer decision Q5.
    """

    max_age_seconds: "int | None"
    max_count_per_family: "int | None"
    max_total_bytes: "int | None"
    min_free_bytes: "int | None"
    max_shape_examples: int


@dataclasses.dataclass(frozen=True)
class PolicyResolution:
    """The outcome of reading the persisted policy.

    `status` is one of ``missing`` (no block written — use `DEFAULT_POLICY`),
    ``valid`` (a policy the user wrote) or ``malformed`` (refuse to act; §6.5
    requires doctor to FAIL and both `db prune` modes to exit 2).
    """

    status: str
    policy: "RetentionPolicy | None"
    reason: "str | None"


#: Maintainer decision Q8: 30 days / 20 per family / 4096 MiB total /
#: 10240 MiB free floor, keeping 8 damage-shape examples. The free floor is
#: measured rather than chosen — `db vacuum` needs roughly
#: ``2 * db_bytes + wal_bytes`` free and `conversations.db` is 5.15 GB.
DEFAULT_POLICY = RetentionPolicy(
    max_age_seconds=30 * _SECONDS_PER_DAY,
    max_count_per_family=20,
    max_total_bytes=4096 * _BYTES_PER_MIB,
    min_free_bytes=10240 * _BYTES_PER_MIB,
    max_shape_examples=8,
)

#: field name -> (floor, nullable, unit multiplier, policy attribute)
_POLICY_FIELDS: "dict[str, tuple[int, bool, int, str]]" = {
    "max_age_days": (1, True, _SECONDS_PER_DAY, "max_age_seconds"),
    "max_count_per_family": (1, True, 1, "max_count_per_family"),
    "max_total_mib": (1, True, _BYTES_PER_MIB, "max_total_bytes"),
    "min_free_mib": (0, True, _BYTES_PER_MIB, "min_free_bytes"),
    "max_shape_examples": (1, False, 1, "max_shape_examples"),
}

#: At least one of these must stay enabled, or nothing bounds growth.
#: `min_free_mib` is deliberately not one of them: it is a floor that reacts
#: to disk pressure, not a bound on what this subsystem retains.
_SIZE_RULE_FIELDS = ("max_age_days", "max_count_per_family", "max_total_mib")


def default_policy_block() -> "dict[str, int]":
    """The default policy rendered in the config file's own units.

    Used by `config get` so an operator sees the effective policy in the same
    shape `config set` accepts.
    """
    return {
        "max_age_days": DEFAULT_POLICY.max_age_seconds // _SECONDS_PER_DAY,
        "max_count_per_family": DEFAULT_POLICY.max_count_per_family,
        "max_total_mib": DEFAULT_POLICY.max_total_bytes // _BYTES_PER_MIB,
        "min_free_mib": DEFAULT_POLICY.min_free_bytes // _BYTES_PER_MIB,
        "max_shape_examples": DEFAULT_POLICY.max_shape_examples,
    }


def policy_to_block(policy: RetentionPolicy) -> "dict[str, int | None]":
    """Render a resolved policy back into the config file's units."""
    return {
        "max_age_days": (
            None if policy.max_age_seconds is None
            else policy.max_age_seconds // _SECONDS_PER_DAY
        ),
        "max_count_per_family": policy.max_count_per_family,
        "max_total_mib": (
            None if policy.max_total_bytes is None
            else policy.max_total_bytes // _BYTES_PER_MIB
        ),
        "min_free_mib": (
            None if policy.min_free_bytes is None
            else policy.min_free_bytes // _BYTES_PER_MIB
        ),
        "max_shape_examples": policy.max_shape_examples,
    }


def resolve_retention_policy(raw: object) -> PolicyResolution:
    """Resolve the raw `storage.artifact_retention` block, strictly (§6.5).

    `raw` is whatever a raw read of `config.json` produced for that key —
    `None` when the block is absent. This function never repairs input: an
    unknown field, a boolean, a non-integer, a value below its floor, a null
    on the non-nullable `max_shape_examples`, or disabling every size rule all
    return ``malformed`` with a reason. Only ``missing`` and ``valid`` carry a
    policy a caller may act on.
    """
    if raw is None:
        return PolicyResolution("missing", DEFAULT_POLICY, None)
    if isinstance(raw, dict) and not raw:
        return PolicyResolution("missing", DEFAULT_POLICY, None)
    if not isinstance(raw, dict):
        return PolicyResolution(
            "malformed",
            None,
            "storage.artifact_retention must be a JSON object, got "
            f"{type(raw).__name__}",
        )

    unknown = sorted(set(raw) - set(_POLICY_FIELDS))
    if unknown:
        return PolicyResolution(
            "malformed",
            None,
            "storage.artifact_retention has unknown field(s): "
            + ", ".join(unknown),
        )

    resolved: "dict[str, int | None]" = {}
    for field, (floor, nullable, unit, attribute) in _POLICY_FIELDS.items():
        if field not in raw:
            resolved[attribute] = getattr(DEFAULT_POLICY, attribute)
            continue
        value = raw[field]
        if value is None:
            if not nullable:
                return PolicyResolution(
                    "malformed",
                    None,
                    f"storage.artifact_retention.{field} may not be null "
                    "(the damage-shape floor is always in force)",
                )
            resolved[attribute] = None
            continue
        # isinstance(True, int) is True in Python: check bool FIRST, or
        # `max_count_per_family: true` would silently resolve to 1.
        if isinstance(value, bool) or not isinstance(value, int):
            return PolicyResolution(
                "malformed",
                None,
                f"storage.artifact_retention.{field} must be an integer "
                f"or null, got {value!r}",
            )
        if value < floor:
            return PolicyResolution(
                "malformed",
                None,
                f"storage.artifact_retention.{field} must be >= {floor}, "
                f"got {value}",
            )
        resolved[attribute] = value * unit

    if all(
        resolved[_POLICY_FIELDS[field][3]] is None for field in _SIZE_RULE_FIELDS
    ):
        return PolicyResolution(
            "malformed",
            None,
            "storage.artifact_retention must keep at least one of "
            + ", ".join(_SIZE_RULE_FIELDS)
            + " enabled",
        )

    return PolicyResolution("valid", RetentionPolicy(**resolved), None)


# --------------------------------------------------------------------------
# §3.1 / §3.2 — the reference graph and the absolute protection gate
# --------------------------------------------------------------------------

#: The artifact kinds this subsystem recognizes. Anything else is protected
#: rather than swept: §3.2 names "outside the recognized artifact set" as a
#: protection condition, and a kind nobody wrote a validator for is exactly
#: that.
RETENTION_KINDS = (
    "incident", "bundle", "wal_evidence", "rebuild_record", "backup",
    "backup_member",
)

#: Kinds that are a root whatever references them. An incident is the unit a
#: policy counts and ages, and a backup stem has no referrer at all. The
#: remaining kinds are a root only when nothing references them (§3.1: "a
#: bundle owned by a rebuild record is a member of that record").
#:
#: This is a KIND rule and not an in-degree rule, and the difference is load
#: bearing: a stats incident manifest names its rebuild record and that record
#: names the incident back (`bin/_cctally_journal.py:7122` and `:7770`), so a
#: pure "referenced by nobody" test would leave both nodes of that real cycle
#: unrooted and the whole component permanently invisible to the planner.
_ROOT_WHATEVER_REFERENCES_IT = frozenset({"incident", "backup"})

#: The confidence values a decided verdict never carries. §3.3 states the rule
#: as "other than `unknown`" rather than as a list of accepted values, so a
#: confidence this subsystem has not seen before classifies rather than
#: silently protecting — the classifier and the gate must not disagree about
#: what "classified" means.
UNDECIDED_CONFIDENCES = frozenset({"unknown", ""})


def is_classified(confidence) -> bool:
    """Whether a recorded confidence counts as a decision (§3.3).

    The field is `confidence`. Reading a `verdict` key instead reports every
    incident as unclassified, which happened once during design.
    """
    return isinstance(confidence, str) and confidence not in UNDECIDED_CONFIDENCES

#: Every recognized kind must be classified before it can be deleted. The map
#: is explicit rather than a constant `True` so that adding a kind forces a
#: decision about it instead of inheriting deletability by default.
_KIND_REQUIRES_CLASSIFICATION = {
    "incident": True,
    "bundle": True,
    "wal_evidence": True,
    "rebuild_record": True,
    "backup": True,
    # A `-wal`, `-shm` or `.classification.json` sidecar of a backup stem.
    # §3.7 keys a backup family by its STEM, so the sidecars must be MEMBERS
    # of that stem rather than roots of their own — which is why they cannot
    # carry kind `backup`, whose entry in `_ROOT_WHATEVER_REFERENCES_IT` would
    # root each of them separately and let the count bound see one family as
    # three. Their own classification is never read, because §3.2 takes the
    # classification condition from the root.
    "backup_member": True,
}


@dataclasses.dataclass(frozen=True)
class RetentionMember:
    """One retained artifact, as the metadata walk observed it.

    Every field is supplied by the glue. The kernel never re-derives one from
    the filesystem, so a test states a condition by setting a field rather than
    by building a directory.
    """

    id: str
    kind: str
    family: str
    created_at_epoch: float
    disk_bytes: int
    logical_bytes: int
    references: "tuple[str, ...]"
    is_symlink: bool
    in_root: bool
    exists: bool
    valid: bool
    classification: "str | None"
    shape_token: "str | None"
    finalized: bool
    active: bool


@dataclasses.dataclass(frozen=True)
class RetentionRoot:
    """A thing a policy counts and ages (§3.1).

    `reachable_ids` includes the root itself, because the deletion closure
    deletes the root along with the members exclusive to it. There is
    deliberately **no `exclusive_closure_ids` field**: for roots A and B that
    both reference target T, T is exclusive to neither at construction time, so
    exclusivity is a property of a SELECTION and is computed by
    `deletion_closure` against the currently selected set.
    """

    id: str
    kind: str
    family: str
    created_at_epoch: float
    reachable_ids: "frozenset[str]"
    own_member_ids: "frozenset[str]"
    requires_classification: bool
    classification: "str | None"
    shape_token: "str | None"
    protected_reasons: "tuple[str, ...]"


@dataclasses.dataclass(frozen=True)
class RetentionGraph:
    """The whole retained corpus: roots, members, and the inverse index."""

    roots: "tuple[RetentionRoot, ...]"
    members: "dict[str, RetentionMember]"
    inbound_roots: "dict[str, frozenset[str]]"
    roots_by_id: "dict[str, RetentionRoot]"


def _member_protection_reasons(member, known_ids) -> "list[str]":
    """Why this member protects every root that can reach it (§3.2).

    Protection propagates BACKWARD through the whole reference graph, not
    through a per-root exclusive closure. An invalid SHARED member belongs to
    no root's exclusive closure, so a closure-only gate would let the planner
    delete it once every inbound root happened to be selected.
    """
    reasons: "list[str]" = []
    if member.kind not in _KIND_REQUIRES_CLASSIFICATION:
        reasons.append("unrecognized-kind")
    if not member.exists:
        reasons.append("missing")
    if not member.valid:
        reasons.append("invalid")
    if not member.finalized:
        reasons.append("unfinished")
    if member.active:
        reasons.append("active")
    if member.is_symlink:
        reasons.append("symlink")
    if not member.in_root:
        reasons.append("outside-root")
    if any(ref not in known_ids for ref in member.references):
        reasons.append("dangling-reference")
    return reasons


def _reachable_from(by_id, known_ids, root_ids, member_id) -> "set[str]":
    """Everything `member_id` reaches, **stopping at every other root**.

    Reachability carries a visited set: the corpus contains a real cycle — an
    incident manifest naming its rebuild record and that record naming the
    incident back — so a walk without one does not terminate on the
    maintainer's own store.

    Stopping at another root is what makes the root set an ANTICHAIN, and that
    is a correctness requirement rather than an optimization. `deletion_closure`
    admits a member only when every inbound root is selected, so a root reached
    by a second root is excluded from its own singleton closure: it would be
    credited against the count bound, absent from `delete_ids`, `keep_ids` and
    `protected_ids` alike, and the bound reported satisfied over a corpus still
    over budget. With this walk `inbound_roots[root] == {root}` for every root,
    so a selected root is always inside its own closure.
    """
    reachable = {member_id}
    queue = [ref for ref in by_id[member_id].references if ref in known_ids]
    while queue:
        current = queue.pop()
        if current in reachable or current in root_ids:
            continue
        reachable.add(current)
        queue.extend(ref for ref in by_id[current].references if ref in known_ids)
    return reachable


def _resolve_root_ids(by_id, known_ids) -> "set[str]":
    """The root set: kind-and-orphanhood, then completed so nothing is stranded.

    The base rule is §3.1's: incidents and backup stems are roots whatever
    references them, an unrecognized kind is a root because nobody wrote a
    validator for it, and a bundle, WAL-evidence directory or rebuild record is
    a root only when nothing references it.

    That rule alone can strand a member. Two bundles that reference each other
    and nothing else are both "referenced", so neither is a root, and no root
    reaches either — they are invisible to the planner and can never be
    reclaimed. The completion loop below promotes such a member to a root, so
    the graph carries the second invariant this subsystem needs: **every member
    is reachable from at least one root**. Preferring an orphan that no other
    orphan references keeps a satellite a satellite; the lexicographic fallback
    is what breaks a pure cycle deterministically.
    """
    referenced: "set[str]" = set()
    for member in by_id.values():
        referenced.update(ref for ref in member.references if ref in known_ids)

    root_ids = {
        member_id
        for member_id, member in by_id.items()
        if member.kind in _ROOT_WHATEVER_REFERENCES_IT
        or member.kind not in _KIND_REQUIRES_CLASSIFICATION
        or member_id not in referenced
    }

    while True:
        covered: "set[str]" = set()
        for root_id in root_ids:
            covered |= _reachable_from(by_id, known_ids, root_ids, root_id)
        stranded = known_ids - covered
        if not stranded:
            return root_ids
        inner = {
            ref
            for member_id in stranded
            for ref in by_id[member_id].references
            if ref in stranded
        }
        promote = sorted(stranded - inner) or [min(stranded)]
        root_ids.update(promote)


def build_graph(members) -> RetentionGraph:
    """Build the reference graph, identify roots and index inbound roots (§3.1).

    Two invariants hold over the result, and the planner and the doctor leg
    both rest on them:

    - **no root is reachable from another root**, so every root appears in the
      deletion closure of any selection containing it;
    - **every member is reachable from some root**, so nothing in the corpus is
      invisible to the planner.
    """
    by_id: "dict[str, RetentionMember]" = {}
    for member in members:
        if member.id in by_id:
            raise ValueError(f"duplicate retained-artifact id: {member.id}")
        by_id[member.id] = member
    known_ids = set(by_id)

    root_ids = _resolve_root_ids(by_id, known_ids)

    own_reasons = {
        member_id: _member_protection_reasons(member, known_ids)
        for member_id, member in by_id.items()
    }
    for member_id in root_ids:
        member = by_id[member_id]
        if member.kind == "wal_evidence":
            # §3.3: WAL evidence is valid when a valid bundle or incident
            # references it. Nothing does, so it cannot be validated and must
            # not be swept on its own.
            own_reasons[member_id].append("unreferenced-evidence")

    inbound: "dict[str, set[str]]" = {member_id: set() for member_id in by_id}
    roots: "list[RetentionRoot]" = []
    for member_id in sorted(root_ids):
        member = by_id[member_id]
        reachable = _reachable_from(by_id, known_ids, root_ids, member_id)
        for reached in reachable:
            inbound[reached].add(member_id)

        reasons: "set[str]" = set()
        for reached in reachable:
            reasons.update(own_reasons[reached])
        requires_classification = _KIND_REQUIRES_CLASSIFICATION.get(
            member.kind, True
        )
        if requires_classification and not is_classified(member.classification):
            reasons.add("unclassified")

        roots.append(RetentionRoot(
            id=member_id,
            kind=member.kind,
            family=member.family,
            created_at_epoch=member.created_at_epoch,
            reachable_ids=frozenset(reachable),
            own_member_ids=frozenset(reachable - root_ids),
            requires_classification=requires_classification,
            classification=member.classification,
            shape_token=member.shape_token,
            protected_reasons=tuple(sorted(reasons)),
        ))

    roots.sort(key=lambda root: (root.created_at_epoch, root.id))
    return RetentionGraph(
        roots=tuple(roots),
        members=by_id,
        inbound_roots={
            member_id: frozenset(found) for member_id, found in inbound.items()
        },
        roots_by_id={root.id: root for root in roots},
    )


# --------------------------------------------------------------------------
# §5.2 — one central admission predicate
# --------------------------------------------------------------------------
#
#     new_plan = eligible_hook_tick_branch OR eligible_mutating_command_branch
#     recovery = eligible_invocation AND a pending plan exists
#
# Revision 3 stated `hook-tick` as a mandatory condition and then said ordinary
# commands enter through the same predicate, which read literally means no
# ordinary command can ever qualify. It is a DISJUNCTION.

#: The commands whose success may schedule a sweep. A central ANNOTATION, not
#: a computed property: reports legitimately write to the entry cache while
#: remaining user-facing reads, so "does it mutate the filesystem" would
#: misclassify `daily` and `report` as mutating. `db` children are named by
#: their qualified `db <action>` form.
RETENTION_MUTATING_COMMANDS = frozenset({
    "sync-week",
    "record-usage",
    "record-credit",
    "cache-sync",
    "db rebuild",
    "db rederive",
    "db journal-repair",
    "db vacuum",
    "db checkpoint",
    "db backup",
})

#: `cctally db prune` never admits, in EITHER path and in EVERY mode. A
#: successful `--yes` has already applied the plan, so a redundant automatic
#: one immediately afterwards is pure waste; and a preview that triggered a
#: real deletion would contradict its own output.
RETENTION_NEVER_ADMITS = frozenset({"db prune"})

#: On the mutating allowlist because their APPLY mutates, but preview-by-
#: default — so their PREVIEW must not admit. `db journal-repair`'s preview
#: carries a literal no-mutation contract that `bin/cctally` already honours by
#: skipping the update hooks for it, and a preview that filed an admission
#: marker broke exactly that contract. It is also correct on the merits: a
#: preview created no new evidence, so there is nothing new to reclaim.
#: `record-credit` is here for the same reason and is NOT a `db` child: it is
#: preview-and-confirm by default, so its preview created no new evidence and
#: must not schedule a sweep.
RETENTION_PREVIEW_BY_DEFAULT = frozenset({
    "db rederive", "db journal-repair", "db prune", "record-credit",
})


def qualified_command(command, action=None) -> str:
    """`db rebuild` for a db child, the bare command otherwise."""
    name = str(command or "")
    child = str(action or "")
    return f"{name} {child}" if name == "db" and child else name


def retention_admission_possible(
    *,
    command,
    action=None,
    exit_code: int = 0,
    hook_forked: "bool | None" = None,
    hook_explain: bool = False,
    hook_foreground: bool = False,
    prune_mode: "str | None" = None,
    applied: "bool | None" = None,
) -> bool:
    """Whether this invocation could admit at all, from the invocation alone.

    Split out of `retention_admission` so the glue can reject before it
    measures anything. The daily rate limit costs a `stat` and the pending-plan
    probe costs a `glob` of the data directory, and both were evaluated as
    ARGUMENTS — so `cctally statusline`, which can never admit, paid a readdir
    on every render to reach a decision this function makes for free.
    """
    name = qualified_command(command, action)
    if prune_mode is not None or name in RETENTION_NEVER_ADMITS:
        return False
    if name in RETENTION_PREVIEW_BY_DEFAULT and applied is not True:
        return False
    if int(exit_code) != 0:
        # A failed command never admits, in either path: whatever went wrong
        # may be the very corruption whose evidence a sweep would reclaim.
        return False
    hook_branch = (
        name == "hook-tick"
        and not hook_explain
        and not hook_foreground
        and hook_forked is True
    )
    # Every hidden worker, `statusline`, `doctor`, share previews and every
    # read-only command fail both branches, because the allowlist is a
    # whitelist.
    return bool(hook_branch or name in RETENTION_MUTATING_COMMANDS)


def retention_admission(
    *,
    command,
    action=None,
    exit_code: int = 0,
    hook_forked: "bool | None" = None,
    hook_explain: bool = False,
    hook_foreground: bool = False,
    prune_mode: "str | None" = None,
    applied: "bool | None" = None,
    rate_limited: bool = False,
    pending_plan_present: bool = False,
) -> str:
    """What this invocation may schedule: `new-plan`, `recovery`, or nothing.

    Every input is explicit. The daily rate limit and the presence of a pending
    plan are booleans the glue measures, because this function reads no clock
    and no filesystem.

    `applied` is the `--yes` state of a preview-by-default command, and `None`
    on a command that has no preview mode at all. Only a command in
    `RETENTION_PREVIEW_BY_DEFAULT` is gated on it.
    """
    if not retention_admission_possible(
        command=command, action=action, exit_code=exit_code,
        hook_forked=hook_forked, hook_explain=hook_explain,
        hook_foreground=hook_foreground, prune_mode=prune_mode,
        applied=applied,
    ):
        return ""
    if not rate_limited:
        return "new-plan"
    if pending_plan_present:
        # Unconditional and NOT rate-limited: a crashed deletion must not wait
        # 24 hours to finish. A new plan already resumes first (§5.4), so this
        # branch only matters once the daily limit has closed the other one.
        return "recovery"
    return ""


# --------------------------------------------------------------------------
# §5.5 — the resume decision table
# --------------------------------------------------------------------------

#: The two durable phases a reclaim entry passes through. No unlink happens
#: until `marked` is fsynced, which is what makes the table below decidable.
RECLAIM_PHASE_MARKING = "marking"
RECLAIM_PHASE_MARKED = "marked"


#: How long a reclaim entry may carry an error before the condition counts as
#: stuck rather than transient. Most errors are retried and clear themselves:
#: `_resume_marking_pass` re-decides every entry on every pass, so a rename that
#: failed on a busy file succeeds later. One case cannot clear itself — phase
#: `marking` with neither the source nor the tombstone present, which means
#: something outside this subsystem moved the member — and the record holding it
#: would otherwise sit in the data directory forever with nothing reporting it.
RECLAIM_STUCK_AFTER_SECONDS = 86400


def format_disk_bytes(value, *, digits: int = 1) -> str:
    """Bytes in the largest unit that still carries a significant figure.

    ONE home for the rule, because a fixed GiB rendering prints `0.00 GiB` in
    every column of a corpus below about 50 MiB — which is most of them on a
    healthy install and all of them in a fixture, and is exactly the figure
    that tells an operator nothing. `digits` differs by surface: `db prune`'s
    table renders two decimals per §6.4, doctor's one-line summaries one.
    """
    value = int(value or 0)
    for threshold, unit in (
        (1024 ** 3, "GiB"), (1024 ** 2, "MiB"), (1024, "KiB"),
    ):
        if value >= threshold:
            return f"{value / threshold:.{digits}f} {unit}"
    return f"{value} B"


def reclaim_entry_is_stuck(
    *, error, first_failed_at_epoch, now_epoch,
    threshold_seconds: int = RECLAIM_STUCK_AFTER_SECONDS,
) -> bool:
    """Whether one reclaim entry's error has persisted long enough to report.

    An entry with no error is never stuck, and an entry whose first failure has
    no recorded time is treated as fresh rather than as stuck — a record written
    by an earlier binary carries no stamp, and reporting it immediately would
    raise an alarm about a condition nobody has observed to persist.
    """
    if not error:
        return False
    if first_failed_at_epoch is None:
        return False
    return (now_epoch - first_failed_at_epoch) >= threshold_seconds


def resume_action(phase: str, source_present: bool, tombstone_present: bool) -> str:
    """What a resuming worker must do with one reclaim entry (§5.5).

    The durable phase is what makes "neither exists" decidable. There is an
    unavoidable crash window after a tombstone is unlinked and before its entry
    is cleared from the pending record, so at `marked` that state is a
    completed entry rather than an error — reporting it as a permanent failure
    would make a SUCCESSFUL deletion non-resumable.

    Two rows §5.5's table does not enumerate:

    - `marking` with neither present. The rename never completed and no unlink
      can have happened yet, so the member was moved by something outside this
      subsystem. Fail closed.
    - `marked` with the source present and the tombstone gone. The deletion
      completed and something re-created the original path; the new inode is
      not ours to remove, and there is nothing left to delete.
    """
    if source_present and tombstone_present:
        return "fail-closed"
    if phase == RECLAIM_PHASE_MARKING:
        if source_present:
            return "resume-rename"
        if tombstone_present:
            return "advance-to-marked"
        return "fail-closed"
    if tombstone_present:
        return "continue-deletion"
    return "entry-complete"


# --------------------------------------------------------------------------
# §3.1 / §3.4 / §3.5 / §3.6 — the planner
# --------------------------------------------------------------------------

#: The damage shape that is not a shape (§3.4). An incident whose preserved
#: damage token is the literal `none` earns no floor; 11 of the 29 tokens on
#: the maintainer's store are this value.
NON_SHAPE_TOKEN = "none"

#: The bounds, in the order §3.5 applies them. The names are the policy's own
#: attribute names, so `unsatisfied_rules` says which knob to change.
BOUND_ORDER = (
    "max_age_seconds", "max_count_per_family", "max_total_bytes", "min_free_bytes",
)


@dataclasses.dataclass(frozen=True)
class RetentionState:
    """The graph plus the two values the kernel refuses to read for itself.

    `now_epoch` and `free_disk_bytes` are handed in because the kernel takes
    no clock and no filesystem. `free_disk_bytes` is None when the glue could
    not measure it, and the free-disk floor is then skipped rather than
    guessed at.
    """

    graph: RetentionGraph
    now_epoch: float
    free_disk_bytes: "int | None"


@dataclasses.dataclass(frozen=True)
class RetentionPlan:
    """What a sweep would delete, keep and refuse to touch."""

    delete_ids: "tuple[str, ...]"
    #: `(root_id, ordered member ids)` per selected root, in deletion order.
    #: The marking engine decides per ROOT (§5.4), so it needs the grouping
    #: explicitly rather than having to re-derive it from `reasons`. Within a
    #: group every referrer precedes its referent.
    delete_groups: "tuple[tuple[str, tuple[str, ...]], ...]"
    keep_ids: "tuple[str, ...]"
    protected_ids: "tuple[str, ...]"
    reasons: "dict[str, str]"
    before_bytes: int
    projected_bytes: int
    reclaimable_bytes: int
    reference_pinned_bytes: int
    unsatisfied_rules: "tuple[str, ...]"
    #: Roots a bound would have taken and the shape floor kept (§3.6), with
    #: what deleting them on top of the plan would have freed. Reported
    #: SEPARATELY from `unsatisfied_rules` because the operator asked for this
    #: retention through `max_shape_examples` and has nothing to act on.
    floor_retained_ids: "tuple[str, ...]" = ()
    floor_retained_bytes: int = 0


def deletion_closure(graph: RetentionGraph, selected) -> "frozenset[str]":
    """The members deletable given exactly this selected set of roots (§3.1).

        deletion_closure(S) = { m : m reachable from some root in S
                                  and m reachable from no root outside S }

    Exclusivity is a property of the SELECTION, never of a root. For roots A
    and B that both reference target T, T is in neither singleton closure and
    enters only once both are selected — which is why a statically computed
    `exclusive_closure_ids` field either never reclaims T or subtracts its
    bytes too early.
    """
    selected = frozenset(selected)
    if not selected:
        return frozenset()
    reached: "set[str]" = set()
    for root_id in selected:
        root = graph.roots_by_id.get(root_id)
        if root is not None:
            reached.update(root.reachable_ids)
    return frozenset(
        member_id
        for member_id in reached
        if graph.inbound_roots.get(member_id, frozenset()) <= selected
    )


def _closure_order(graph: RetentionGraph, root_id: str, closure) -> "list[str]":
    """`root_id` first, then the members it reaches, **every** referrer first.

    §5.4 renames a reference-bearing root before the bundle it references, so a
    crash cannot leave a surviving manifest pointing at a tombstone. A pre-order
    walk alone does NOT give that: it emits a member the first time it is
    reached, which can precede a referrer reached only by a longer path. The
    production shape is exactly that diamond — 28 of the maintainer's 142
    incidents name both a forensics bundle and a rebuild record, and that record
    names the same bundle — so under one reference ordering the bundle would be
    renamed before the record that points at it.

    The pre-order below therefore only fixes a deterministic candidate order.
    The emission loop is a topological pass over it: a member is emitted once
    every in-closure referrer has been emitted, and when a cycle leaves nothing
    ready the earliest pre-order candidate wins — which is the entry root on the
    production incident-to-rebuild-record cycle.
    """
    candidates: "list[str]" = []
    seen: "set[str]" = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in closure:
            continue
        seen.add(current)
        candidates.append(current)
        member = graph.members.get(current)
        if member is None:
            continue
        stack.extend(reversed([
            ref for ref in member.references if ref in graph.members
        ]))

    referrers: "dict[str, set[str]]" = {member_id: set() for member_id in candidates}
    for member_id in candidates:
        member = graph.members.get(member_id)
        if member is None:
            continue
        for ref in member.references:
            if ref in referrers and ref != member_id:
                referrers[ref].add(member_id)

    ordered: "list[str]" = []
    emitted: "set[str]" = set()
    pending = list(candidates)
    while pending:
        ready = next(
            (member_id for member_id in pending if referrers[member_id] <= emitted),
            None,
        )
        chosen = pending[0] if ready is None else ready
        ordered.append(chosen)
        emitted.add(chosen)
        pending.remove(chosen)
    return ordered


def _shape_floor_ids(graph: RetentionGraph, eligible, max_shape_examples: int):
    """The roots §3.4 keeps because they are the last example of a shape.

    One example per distinct shape, capped at `max_shape_examples` shapes. The
    kept example is the NEWEST, which under oldest-first selection is exactly
    "never delete the last one". Shapes compete for the cap by the recency of
    their newest example, so a cap smaller than the number of shapes is still
    deterministic.
    """
    if max_shape_examples <= 0:
        return frozenset()
    newest: "dict[str, RetentionRoot]" = {}
    for root in graph.roots:
        token = root.shape_token
        if not isinstance(token, str) or not token or token == NON_SHAPE_TOKEN:
            continue
        if root.id not in eligible:
            continue
        current = newest.get(token)
        if current is None or (
            root.created_at_epoch, root.id
        ) > (current.created_at_epoch, current.id):
            newest[token] = root
    ranked = sorted(
        newest.values(),
        key=lambda root: (-root.created_at_epoch, root.id),
    )
    return frozenset(root.id for root in ranked[:max_shape_examples])


class _Selection:
    """Roots chosen so far, with the closure updated after every addition.

    Recomputing once per phase is not enough: the byte phase alone selects
    several roots, and each one changes what the next one would reclaim
    (§3.1).

    **The update is incremental, and that is a cost decision, not a semantic
    one.** `deletion_closure` remains the authoritative statement of §3.1. The
    equality is enforced by
    `tests/test_artifact_retention_kernel.py::test_the_incremental_selection_equals_the_authoritative_closure`,
    which compares this class against `deletion_closure` after every addition
    over 400 generated corpora, and NOT by an assertion in `add`: re-deriving
    the whole closure once per candidate is precisely the cost the incremental
    form removes.

    **That differential covers exactly three of this class's outputs** —
    `closure`, `bytes_reclaimed` and `surviving_root_counts`. It does not
    compare `order` or `groups`, and cannot: §5.4's topological deletion order
    is produced by `_closure_order`, so a differential would have to
    re-implement it. That ordering is covered instead by
    `test_delete_ids_puts_a_referrer_before_its_referent`,
    `test_a_diamond_emits_every_referrer_before_the_shared_referent`,
    `test_the_production_cycle_still_puts_the_incident_first`,
    `test_delete_groups_lead_with_their_root_and_carry_its_closure`,
    `test_delete_groups_flatten_back_to_delete_ids` and
    `test_a_shared_member_joins_the_group_of_the_root_that_completed_it`.

    What changed is how the same answer is reached: recomputing the whole
    closure and the whole surviving-root tally per candidate made
    `plan_artifact_retention` quadratic
    in the root count — measured at 8.0 ms over 142 roots, 32.1 over 284, 125.8
    over 568 and 535.7 over 1136, a clean 4x per doubling. The walk's
    5000-entry cap admits roughly 1600 roots, and this runs on the periodic
    doctor gather, which is the shape that has pegged this repository's CPU
    before.

    The rewrite rests on the definition itself. A member joins the closure
    exactly when the last of its inbound roots is selected, so one countdown
    per member — decremented when a root that reaches it is added — decides
    membership in O(1) per inbound edge over the whole selection. A member with
    NO inbound root can never join, because it is reachable from no selected
    root; its countdown starts at zero and is excluded explicitly rather than
    by arithmetic.
    """

    def __init__(self, graph: RetentionGraph):
        self._graph = graph
        self.root_ids: "list[str]" = []
        self.order: "list[str]" = []
        self.groups: "list[tuple[str, tuple[str, ...]]]" = []
        self.reasons: "dict[str, str]" = {}

        #: member -> how many of its inbound roots are still unselected.
        self._pending: "dict[str, int]" = {}
        #: root -> the members that root reaches, i.e. the inverse of
        #: `inbound_roots`, so one addition touches only its own edges.
        self._reached_by: "dict[str, list[str]]" = {}
        for member_id, inbound in graph.inbound_roots.items():
            if not inbound:
                continue
            self._pending[member_id] = len(inbound)
            for root_id in inbound:
                self._reached_by.setdefault(root_id, []).append(member_id)

        self._closed: "set[str]" = set()
        self._frozen: "frozenset[str] | None" = frozenset()
        self._bytes = 0
        self._counts: "dict[str, int]" = {}
        for root in graph.roots:
            self._counts[root.family] = self._counts.get(root.family, 0) + 1

    @property
    def closure(self) -> "frozenset[str]":
        """The deletion closure of the roots selected so far.

        Materialized on demand and cached until the next addition. Freezing the
        set inside `add` instead copied the whole closure once per selected
        root, which is a second roots-times-members term and was most of what
        remained after the countdown replaced the recomputation.
        """
        if self._frozen is None:
            self._frozen = frozenset(self._closed)
        return self._frozen

    @property
    def bytes_reclaimed(self) -> int:
        return self._bytes

    def add(self, root_id: str, reason: str) -> None:
        self.root_ids.append(root_id)
        self.reasons[root_id] = reason
        newly: "set[str]" = set()
        for member_id in self._reached_by.get(root_id, ()):
            remaining = self._pending[member_id] - 1
            self._pending[member_id] = remaining
            if remaining:
                continue
            newly.add(member_id)
            self._closed.add(member_id)
            member = self._graph.members.get(member_id)
            if member is not None:
                self._bytes += member.disk_bytes
            root = self._graph.roots_by_id.get(member_id)
            if root is not None and self._counts.get(root.family):
                self._counts[root.family] -= 1
        self._frozen = None
        added: "list[str]" = []
        # Every member that newly closed is reachable from the root just added
        # — that is what made its countdown reach zero — so ordering the walk
        # from this root reaches all of them, in §5.4's topological order.
        for member_id in _closure_order(self._graph, root_id, self._closed):
            if member_id in newly:
                self.order.append(member_id)
                added.append(member_id)
                self.reasons.setdefault(member_id, "closure")
        if added:
            self.groups.append((root_id, tuple(added)))

    def surviving_root_counts(self) -> "dict[str, int]":
        """Roots per family that this selection would leave on disk.

        A root survives when it is not in the deletion closure — NOT merely
        when it has not been selected. The distinction is what §3.5 means by
        "projected state is recomputed after every root added": the age phase
        removes its selections from the candidate list without touching any
        per-family tally, so a count phase that measured the original
        population would keep taking until the candidates ran out. Measured on
        the maintainer's corpus shape with classification complete, a 20-per-
        family bound left 40 survivors on its own and 8 once a firing age bound
        preceded it.

        The tally is maintained by `add` rather than rebuilt here, which is
        what makes the count phase linear; a family whose survivors reach zero
        is dropped so the mapping keeps agreeing with a rebuilt one key for
        key.
        """
        return {
            family: count for family, count in self._counts.items() if count
        }


def _run_selection(
    state: RetentionState, policy: RetentionPolicy, floored,
) -> "tuple[_Selection, int]":
    """Select roots oldest-first against each bound in turn (§3.5).

    Factored out so §3.6's "is this bound blocked by PROTECTION?" question can
    be answered by running the very same selection with the shape floor
    disabled, rather than by four per-bound subtractions that the byte and
    free-disk bounds cannot express (a floored root's share of a shared member
    is not a per-root quantity).
    """
    graph = state.graph
    # Already oldest-first from `build_graph`. Each phase consumes this list in
    # order and hands its leftovers to the next, rather than removing from one
    # shared list: `list.remove` compares whole frozen dataclasses, so the
    # removals alone cost roots-squared field comparisons at the walk's cap.
    remaining = [
        root for root in graph.roots
        if not root.protected_reasons and root.id not in floored
    ]

    selection = _Selection(graph)

    if policy.max_age_seconds is not None:
        kept = []
        for root in remaining:
            if state.now_epoch - root.created_at_epoch > policy.max_age_seconds:
                selection.add(root.id, "max_age_seconds")
            else:
                kept.append(root)
        remaining = kept

    if policy.max_count_per_family is not None:
        # Re-read the surviving population before EVERY candidate. Maintaining
        # a private tally seeded from `graph.roots` is what let the age phase's
        # selections be counted twice; a protected or shape-floored root does
        # survive and must keep counting, which is why the tally is derived
        # from the closure rather than from `remaining`.
        kept = []
        for root in remaining:
            counts = selection.surviving_root_counts()
            if counts.get(root.family, 0) > policy.max_count_per_family:
                selection.add(root.id, "max_count_per_family")
            else:
                kept.append(root)
        remaining = kept

    before_bytes = sum(member.disk_bytes for member in graph.members.values())
    cursor = 0

    if policy.max_total_bytes is not None:
        while (
            cursor < len(remaining)
            and before_bytes - selection.bytes_reclaimed > policy.max_total_bytes
        ):
            selection.add(remaining[cursor].id, "max_total_bytes")
            cursor += 1

    if state.free_disk_bytes is not None and policy.min_free_bytes is not None:
        while (
            cursor < len(remaining)
            and state.free_disk_bytes + selection.bytes_reclaimed
            < policy.min_free_bytes
        ):
            selection.add(remaining[cursor].id, "min_free_bytes")
            cursor += 1

    return selection, before_bytes


def _unsatisfied_rules(
    state: RetentionState, policy: RetentionPolicy, selection: "_Selection",
    before_bytes: int,
) -> "list[str]":
    """The bounds this selection leaves unmet."""
    graph = state.graph
    deleted_ids = selection.closure
    reclaimable = selection.bytes_reclaimed
    unsatisfied: "list[str]" = []
    if policy.max_age_seconds is not None and any(
        state.now_epoch - root.created_at_epoch > policy.max_age_seconds
        for root in graph.roots
        if root.id not in deleted_ids
    ):
        unsatisfied.append("max_age_seconds")
    if policy.max_count_per_family is not None:
        surviving = selection.surviving_root_counts()
        if any(count > policy.max_count_per_family for count in surviving.values()):
            unsatisfied.append("max_count_per_family")
    if (
        policy.max_total_bytes is not None
        and before_bytes - reclaimable > policy.max_total_bytes
    ):
        unsatisfied.append("max_total_bytes")
    if (
        state.free_disk_bytes is not None
        and policy.min_free_bytes is not None
        and state.free_disk_bytes + reclaimable < policy.min_free_bytes
    ):
        unsatisfied.append("min_free_bytes")
    return unsatisfied


def plan_artifact_retention(
    state: RetentionState, policy: RetentionPolicy,
) -> RetentionPlan:
    """Select roots oldest-first against each bound in turn (§3.5).

    The protection gate runs before every bound and is absolute (§3.2). When
    protection alone exceeds a bound, everything eligible is still deleted and
    the bound is reported `unsatisfied` rather than silently unmet (§3.6).

    **The shape floor is excused from `unsatisfied_rules`; protection is not**
    (§3.6). Both leave a root on disk that a bound would otherwise have taken,
    but only one of them is something the operator can act on. A floored root
    is retained by the policy the operator themselves set through
    `max_shape_examples`, and reporting that as an unsatisfied bound states
    that they must act while the system does exactly what they asked. Two of
    the four damage shapes on the maintainer's corpus appear exactly once, so
    without this the age bound becomes permanently unsatisfiable on a healthy
    install and `db.retained_artifacts` FAILs forever — a FAIL no action
    clears, which trains the operator to ignore doctor.

    The question is therefore answered by re-running the identical selection
    with the floor disabled. Whatever that run still leaves unmet is what
    protection blocks.
    """
    graph = state.graph
    protected = [root for root in graph.roots if root.protected_reasons]
    eligible_ids = {
        root.id for root in graph.roots if not root.protected_reasons
    }
    floored = _shape_floor_ids(graph, eligible_ids, policy.max_shape_examples)

    selection, before_bytes = _run_selection(state, policy, floored)

    reclaimable = selection.bytes_reclaimed
    projected = before_bytes - reclaimable
    selected_ids = set(selection.root_ids)
    # What survives is what the closure does NOT delete. `build_graph`
    # guarantees every root is inside its own closure, so for a graph it built
    # these two sets agree on roots; keeping the distinction here means a graph
    # assembled by any other route still reports every root in exactly one of
    # `delete_ids`, `keep_ids` and `protected_ids` instead of dropping it.
    deleted_ids = selection.closure

    pinned = 0
    for member_id, inbound in graph.inbound_roots.items():
        if member_id in deleted_ids or not inbound:
            continue
        if inbound & selected_ids:
            pinned += graph.members[member_id].disk_bytes

    if floored:
        floor_free, _ = _run_selection(state, policy, frozenset())
        unsatisfied = _unsatisfied_rules(state, policy, floor_free, before_bytes)
        # What the floor kept is what a bound WOULD have taken (§6.4's "that
        # age and count would otherwise have removed"), never every root that
        # merely holds a floor.
        floor_retained = tuple(
            root.id for root in graph.roots
            if root.id in floored and root.id in floor_free.closure
        )
        floor_retained_bytes = sum(
            graph.members[member_id].disk_bytes
            for member_id in deletion_closure(
                graph, selected_ids | set(floor_retained)
            )
            - deleted_ids
        )
    else:
        unsatisfied = _unsatisfied_rules(state, policy, selection, before_bytes)
        floor_retained = ()
        floor_retained_bytes = 0

    reasons = dict(selection.reasons)
    for root in protected:
        reasons[root.id] = ",".join(root.protected_reasons)
    keep_ids = []
    for root in graph.roots:
        if root.id in deleted_ids or root.protected_reasons:
            continue
        keep_ids.append(root.id)
        if root.id in selected_ids:
            # Selected, and still on disk: another root that survives can reach
            # it. Overwriting the selection reason is deliberate — reporting it
            # as deleted under `max_count_per_family` would describe work the
            # plan is not going to do.
            reasons[root.id] = "reference-pinned"
            continue
        reasons.setdefault(
            root.id, "shape-floor" if root.id in floored else "retained",
        )

    return RetentionPlan(
        delete_ids=tuple(selection.order),
        delete_groups=tuple(selection.groups),
        keep_ids=tuple(keep_ids),
        protected_ids=tuple(root.id for root in protected),
        reasons=reasons,
        before_bytes=before_bytes,
        projected_bytes=projected,
        reclaimable_bytes=reclaimable,
        reference_pinned_bytes=pinned,
        unsatisfied_rules=tuple(
            rule for rule in BOUND_ORDER if rule in unsatisfied
        ),
        floor_retained_ids=floor_retained,
        floor_retained_bytes=floor_retained_bytes,
    )


# --------------------------------------------------------------------------
# §3.3 — validation and classification, per member kind
# --------------------------------------------------------------------------
#
# A universal "no manifest means protected" rule would permanently protect
# every standalone bundle, rebuild record and ordinary backup family, none of
# which has an incident manifest. Each predicate below is therefore about one
# kind, and each returns a plain bool the metadata walk stores on the member.

#: Files an incident directory acquires AFTER its members were moved, so they
#: can never appear in `movedFiles`. Excluding them is what keeps a classified
#: incident valid (§3.3).
INCIDENT_CONTROL_METADATA = frozenset({"manifest.json", "classification.json"})

#: The statuses a rebuild record may terminally carry. A missing or
#: unrecognized status is INVALID rather than terminal, because a record whose
#: rebuild never reported an outcome may still describe live work.
TERMINAL_REBUILD_STATUSES = frozenset({"ok", "failed"})

_MACHINE_BACKUP_RE = re.compile(r"\.bak-corrupt-malformed-\d{8}T\d{6}Z$")
_USER_BACKUP_RE = re.compile(r"\.bak-\d{8}T\d{6}Z$")


def validate_incident(*, manifest, observed) -> bool:
    """An incident is valid when its manifest agrees with what is on disk.

    `observed` is every entry name directly inside the incident directory.
    Control metadata is excluded from the comparison because it is written
    after the move — an incident that has since been classified would
    otherwise become invalid, and therefore protected, precisely because it
    was classified.
    """
    if not isinstance(manifest, dict):
        return False
    moved = manifest.get("movedFiles")
    if not isinstance(moved, list) or any(
        not isinstance(name, str) for name in moved
    ):
        return False
    return set(moved) == set(observed) - INCIDENT_CONTROL_METADATA


def incident_is_finalized(*, manifest, pending_marker_present) -> bool:
    """Whether the quarantine that produced this incident ran to completion.

    §3.2 words the condition as "a `.quarantine-pending.json` marker is
    present, or `complete` is not `true`". The second half of that reading is
    wrong on this tree, and the first half is what actually carries the
    invariant.

    Both quarantine paths write `manifest.json` only AFTER every member has
    been renamed into the incident directory (`bin/_cctally_db.py:1409` and
    `:1487`), and the strict path unlinks its pending record only after that.
    So a readable manifest already proves the move finished, and a crash
    mid-move leaves an incident with no manifest at all — which §3.3 rejects
    as invalid, and which is therefore protected anyway.

    Reading an ABSENT `complete` key as unfinished would protect 22 of the 142
    incidents on the maintainer's store, holding 8.83 of its 18.2 GiB,
    including every cache incident §4.3's correlation was measured to unlock —
    which would make acceptance criterion 2 unreachable. Only an explicit
    `complete: false`, or a live pending marker naming this incident, means
    unfinished.
    """
    if pending_marker_present:
        return False
    if isinstance(manifest, dict) and manifest.get("complete") is False:
        return False
    return True


def validate_bundle(payload) -> bool:
    """A forensics bundle is valid when it parses and carries a schema version.

    Deliberately weaker than the incident rule: a successful synchronous
    `db rebuild` writes a bundle with no `trigger` at all
    (`bin/_cctally_db.py:1006` against the `:1426` call site), so demanding
    one would invalidate every bundle that command has ever written.
    """
    return isinstance(payload, dict) and payload.get("schemaVersion") is not None


def validate_rebuild_record(payload) -> bool:
    """A rebuild record is valid only at an ENUMERATED terminal status."""
    if not isinstance(payload, dict):
        return False
    return payload.get("status") in TERMINAL_REBUILD_STATUSES


def classification_applies(*, verdict, incident_name) -> bool:
    """Whether a `classification.json` classifies THIS incident (§3.3).

    Binding the verdict to the directory it names prevents a classification
    file from authorizing deletion of a directory it was not written for.
    """
    if not isinstance(verdict, dict):
        return False
    if verdict.get("incident") != incident_name:
        return False
    return is_classified(verdict.get("confidence"))


def incident_classification(*, manifest, verdict, incident_name):
    """The confidence that classifies an incident, or None (§3.3).

    A `schemaVersion: 2` manifest with a truthy `trigger` classifies itself
    `exact`. A manifest claiming version 2 with no trigger is semantically
    invalid: it does NOT fall through to the sidecar, because a producer that
    stamped the version and omitted the trigger left a defect rather than a
    weaker verdict.
    """
    if isinstance(manifest, dict) and manifest.get("schemaVersion") == 2:
        trigger = manifest.get("trigger")
        if isinstance(trigger, str) and trigger:
            return "exact"
        return None
    if classification_applies(verdict=verdict, incident_name=incident_name):
        return verdict.get("confidence")
    return None


def backup_origin(name: str) -> str:
    """Which of §3.7's three naming shapes a backup stem has.

    ``machine`` is `db repair`'s own copy and is the only sweepable origin.
    ``user`` is `cctally db backup`, excluded by maintainer decision Q4.
    ``unknown`` is a hand-made copy, and this install carries several — which
    is why an unrecognized name is never deleted automatically.
    """
    if _MACHINE_BACKUP_RE.search(name):
        return "machine"
    if _USER_BACKUP_RE.search(name):
        return "user"
    return "unknown"


def _backup_identities(entries):
    """`{name: (device, inode, size)}` for a backup family, or None if unusable."""
    if not isinstance(entries, list):
        return None
    identities = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or name in identities:
            return None
        identities[name] = (
            entry.get("device"), entry.get("inode"), entry.get("size"),
        )
    return identities


def backup_sidecar_applies(*, sidecar, observed) -> bool:
    """Whether a backup classification sidecar still describes this family.

    Device and inode are what §3.3 names, and they are what stops a
    REPLACEMENT family written at the same machine-shaped stem from inheriting
    an earlier `exact` verdict and becoming deletable without ever having been
    classified. Size is compared as well, so an in-place rewrite of the same
    inode is caught too.

    `mtime` is recorded by the writer but deliberately NOT compared, and the
    reason is availability rather than safety. Comparing it WOULD add safety: a
    filesystem may reuse an inode number once the recorded file is gone, and a
    device, inode and size match on a reused inode authorizes deleting a family
    this sidecar never described. And omitting it is the safe direction on the
    other side too — a copy or restore tool that perturbs mtime would make the
    family unclassified, which PROTECTS it.

    The decision stands because that protection has no way back: nothing rewrites
    a backup sidecar, so a family protected by a perturbed mtime stays protected
    forever with no operator-facing remedy. The residual inode-reuse risk is
    bounded by the device and size comparison and by §3.7's rule that only a
    machine-shaped stem is ever swept.
    """
    if not isinstance(sidecar, dict):
        return False
    if not is_classified(sidecar.get("confidence")):
        return False
    recorded = _backup_identities(sidecar.get("members"))
    present = _backup_identities(observed)
    if recorded is None or present is None or not recorded:
        return False
    return recorded == present


def backup_classification(*, sidecar, observed):
    """The confidence a backup family carries, or None (§3.3)."""
    if backup_sidecar_applies(sidecar=sidecar, observed=observed):
        return sidecar.get("confidence")
    return None


# --------------------------------------------------------------------------
# §4.3 — family-parameterized incident classification
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Verdict:
    """One classification decision about one incident directory.

    `incident` names the directory this verdict describes, so a
    `classification.json` can never authorize deletion of a directory it was
    not written for (§3.3).
    """

    schema_version: int
    method: str
    confidence: str
    incident: str
    trigger: "str | None"
    candidates: "tuple[str, ...]"
    forensics_path: "str | None"
    evidence: dict


#: The trigger origins a *correlated but trigger-less* bundle leaves open, per
#: family. A v1 bundle carries no `trigger` object at all, so correlation can
#: only narrow the cause to the producers that write one for that family.
FAMILY_CANDIDATE_TRIGGERS: "dict[str, tuple[str, ...]]" = {
    "stats.db": ("corruption-heal", "db-rebuild"),
    "cache.db": ("cache.open", "cache.sync"),
    "conversations.db": ("conversations.open", "conversations.recovery"),
}

#: A rebuild measured at 71 to 77 seconds, so the window is bounded well above
#: that while staying far too small to reach an unrelated incident. The value
#: `bin/cctally-classify-incidents` has always used.
DEFAULT_CORRELATION_WINDOW_SECONDS = 600

#: The classification record's own schema version, matching the one the
#: shipped correlator writes.
CLASSIFICATION_SCHEMA_VERSION = 1


def nearest_preceding_bundle(bundles, when):
    """The latest bundle at or before `when`, or None.

    Ported unchanged from `bin/cctally-classify-incidents:130`; only the tuple
    shape widens, from `(stamp, path)` to `(stamp, path, payload)`. `bundles`
    is ascending by time and already filtered to one family. The window is NOT
    applied here, exactly as in the original — the caller applies it.
    """
    best = None
    for entry in bundles:
        if entry[0] <= when:
            best = entry
        else:
            break
    return best


def classify_incident(
    *,
    family: str,
    incident_name: str,
    manifest: "dict | None",
    bundles,
    incident_time,
    window_seconds: int = DEFAULT_CORRELATION_WINDOW_SECONDS,
) -> Verdict:
    """Render a verdict for one incident of one family (§4.3).

    Precedence: a self-classifying v2 manifest wins outright; otherwise the
    nearest preceding bundle inside the window decides — `exact` when it names
    its own `trigger.origin`, `candidate` when it does not — and otherwise the
    incident stays `unknown` and therefore protected.

    A `schemaVersion: 2` manifest with no truthy trigger is semantically
    invalid: it claims to classify itself and does not. It is reported
    `unknown` rather than falling through to correlation, because correlating
    an incident whose own manifest contradicts itself would dress a defect up
    as evidence.
    """
    manifest = manifest if isinstance(manifest, dict) else {}
    candidates = FAMILY_CANDIDATE_TRIGGERS.get(family, ())
    manifest_version = manifest.get("schemaVersion")

    if manifest_version == 2:
        trigger = manifest.get("trigger")
        if isinstance(trigger, str) and trigger:
            forensics = manifest.get("forensicsPath")
            return Verdict(
                schema_version=CLASSIFICATION_SCHEMA_VERSION,
                method="manifest-v2",
                confidence="exact",
                incident=incident_name,
                trigger=trigger,
                candidates=(),
                forensics_path=forensics if isinstance(forensics, str) else None,
                evidence={
                    "manifestSchemaVersion": 2,
                    "triggerError": manifest.get("triggerError"),
                    "binaryVersion": manifest.get("binaryVersion"),
                },
            )
        return Verdict(
            schema_version=CLASSIFICATION_SCHEMA_VERSION,
            method="header-only",
            confidence="unknown",
            incident=incident_name,
            trigger=None,
            candidates=candidates,
            forensics_path=None,
            evidence={
                "manifestSchemaVersion": manifest_version,
                "windowSeconds": window_seconds,
                "reason": (
                    "a schemaVersion 2 manifest without a trigger is "
                    "semantically invalid"
                ),
            },
        )

    match = (
        None
        if incident_time is None
        else nearest_preceding_bundle(bundles, incident_time)
    )
    if match is not None:
        when, path, bundle = match
        gap = int((incident_time - when).total_seconds())
        if gap <= window_seconds:
            trigger_block = bundle.get("trigger") if isinstance(bundle, dict) else None
            origin = (
                trigger_block.get("origin")
                if isinstance(trigger_block, dict)
                else None
            )
            evidence = {
                "manifestSchemaVersion": manifest_version,
                "gapSeconds": gap,
                "windowSeconds": window_seconds,
            }
            if isinstance(origin, str) and origin:
                # The bundle names its own trigger. Calling that `candidate`
                # would understate the evidence: cache and conversations
                # producers pass a trigger into `write_corruption_forensics`,
                # and `tests/test_cache_corruption_recovery.py:994` pins it.
                return Verdict(
                    schema_version=CLASSIFICATION_SCHEMA_VERSION,
                    method="forensics-trigger",
                    confidence="exact",
                    incident=incident_name,
                    trigger=origin,
                    candidates=(),
                    forensics_path=path,
                    evidence=evidence,
                )
            return Verdict(
                schema_version=CLASSIFICATION_SCHEMA_VERSION,
                method="forensics-correlation",
                confidence="candidate",
                incident=incident_name,
                trigger=None,
                candidates=candidates,
                forensics_path=path,
                evidence={
                    **evidence,
                    "reason": (
                        "a bundle carrying no trigger cannot distinguish the "
                        "producers of this family"
                    ),
                },
            )

    return Verdict(
        schema_version=CLASSIFICATION_SCHEMA_VERSION,
        method="header-only",
        confidence="unknown",
        incident=incident_name,
        trigger=None,
        candidates=candidates,
        forensics_path=None,
        evidence={
            "manifestSchemaVersion": manifest_version,
            "windowSeconds": window_seconds,
            "reason": (
                "no forensics bundle within the window preceding this incident"
            ),
        },
    )


def verdict_to_record(verdict: Verdict) -> "dict[str, object]":
    """The persisted `classification.json` body, byte-comparable across runs.

    Keeps the key names `bin/cctally-classify-incidents` already writes so a
    reader does not have to know which producer wrote the file.
    """
    record: "dict[str, object]" = {
        "schemaVersion": verdict.schema_version,
        "incident": verdict.incident,
        "method": verdict.method,
        "confidence": verdict.confidence,
        "evidence": verdict.evidence,
    }
    if verdict.trigger is not None:
        record["trigger"] = verdict.trigger
    if verdict.candidates:
        record["candidates"] = list(verdict.candidates)
    if verdict.forensics_path is not None:
        record["forensicsPath"] = verdict.forensics_path
    return record
