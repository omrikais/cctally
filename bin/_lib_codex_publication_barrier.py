"""Pure decisions of the #968 conversation publication barrier (spec 5.11).

No I/O and no SQLite. `_cctally_cache` owns the durable cache.db state (the
membership index, readiness targets, population, certificates and repair
queue) and the conversation pass owns the publication records; both call these
functions with plain values, so the readiness rule, the thread-metadata rule
and the certificate format are stated exactly once.
"""
from __future__ import annotations

import json
from typing import Iterable, NamedTuple, Optional

CERTIFICATE_VERSION = 1

READY = "ready"
REQUIRED = "required"
SUSPENDED = "suspended"

#: The pending hold (5.11 U6 table, first row): the candidate stays a
#: dependency of the next revision; pending work is never an exclusion.
PENDING_HOLD = "verification-pending"
#: Holds that suspend the contributor's current-generation readiness
#: obligation (U6 table, second row). Retained accepted membership is kept;
#: "excluded" never means deleting rows or forgetting membership.
#: `container-absent` is an accounted identity whose container is gone (an
#: orphan awaiting the off-hook prune, or one #485 refuses to prune).
SUSPENSION_HOLDS = frozenset({
    "decoder-unavailable", "decode-error", "verification-unverified",
    "container-absent",
})

#: The twelve source columns of a `codex_conversation_threads` row, in column
#: order. The two timestamp columns are bookkeeping, carried separately.
THREAD_FIELDS = (
    "conversation_key", "source_root_key", "native_thread_id",
    "root_thread_id", "parent_thread_id", "source_path", "cwd", "git_json",
    "source_kind", "thread_source_json", "model_provider", "context_window",
)

#: The generation token of a membership row that records a key a suspended
#: contributor of unknown membership (no watermark) is KNOWN to feed, from its
#: retained rows (review N2). Never equal to a real generation token, so it
#: never feeds a READY contributor; it is such an exclusion's effective
#: generation, and the row carries the key's retained thread as metadata when
#: that row names this contributor (5.11 U6 "using retained accepted facts
#: where they exist"). The rows are deleted when the membership is proven.
RETAINED_GENERATION = "retained"


class Target(NamedTuple):
    """One contributor's readiness target (a `codex_barrier_targets` row)."""
    identity: str
    source_path: str
    generation: str
    #: Offset after the last newline-terminated line of the captured target,
    #: set once accounting reached that target; None while it has not.
    complete_line_end: Optional[int]
    hold: Optional[str]
    #: Generation and decoded offset through which the production parser has
    #: recorded this identity's membership from offset 0.
    watermark_generation: Optional[str]
    watermark_offset: Optional[int]


class TargetVerdict(NamedTuple):
    state: str
    reason: Optional[str]


def classify_target(target: Target) -> TargetVerdict:
    """Readiness of one contributor (5.11 "The readiness target", U6).

    A watermark of another generation proves nothing, whatever its offset
    (968-SR-012: a G0 cursor never satisfies G1's target). A suspension is
    SUSPENDED whether or not its membership was ever proven (U6: "degraded
    contributors degrade; they do not freeze", review N2): one of unknown
    membership is an exclusion that blocks no key, and its keys keep the
    thread its retained facts supply (``decide_seal``).
    """
    if target.hold in SUSPENSION_HOLDS:
        return TargetVerdict(SUSPENDED, target.hold)
    if target.hold == PENDING_HOLD:
        return TargetVerdict(REQUIRED, PENDING_HOLD)
    if target.hold is not None:
        raise ValueError(f"unknown barrier hold {target.hold!r}")
    if (target.watermark_generation != target.generation
            or target.watermark_offset is None):
        return TargetVerdict(REQUIRED, "membership-unproven")
    if target.complete_line_end is None:
        return TargetVerdict(REQUIRED, "accounting-incomplete")
    if target.watermark_offset < target.complete_line_end:
        return TargetVerdict(REQUIRED, "membership-unproven")
    return TargetVerdict(READY, None)


class MemberFact(NamedTuple):
    """One `codex_barrier_membership` row."""
    identity: str
    generation: str
    source_path: str
    metadata_json: Optional[str]
    metadata_offset: Optional[int]


def metadata_from_thread(thread) -> str:
    """The generation-bound metadata one `session_meta` supplies (JSON).

    ``ensure_ascii`` escapes a lone surrogate (#845's undecodable cwd), and
    ``json.loads`` restores the identical string.
    """
    return json.dumps({name: getattr(thread, name) for name in THREAD_FIELDS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def metadata_from_row(row: dict) -> str:
    """``metadata_from_thread``'s JSON for a retained thread row (a dict)."""
    return json.dumps({name: row.get(name) for name in THREAD_FIELDS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def select_thread(candidates: Iterable[MemberFact], *,
                  retained: Optional[dict] = None) -> Optional[dict]:
    """The conversation's thread metadata.

    Review P2-2: when the retained thread row (the entry snapshot, else the
    current accounting row) equals the metadata some candidate supplies, it
    is kept, so certifying a legacy conversation changes nothing visible.
    Measured on the frozen 2026-10-14 baseline: of 243 multi-file keys the
    stored row equals the maximum-path choice for 236 and another
    contributor's metadata for 7. Otherwise (a new publication, or a retained
    row no contributor supplies) the rule is what a complete walk writes
    LAST: a full walk visits a root's rollouts in logical-path order and a
    file's records in offset order, so the last writer is the maximum of
    ``(source_path, metadata_offset)``, independent of the order in which
    healing reads the files.
    """
    facts = [f for f in candidates
             if f.metadata_json is not None and f.metadata_offset is not None]
    if retained is not None:
        want = {name: retained.get(name) for name in THREAD_FIELDS}
        for fact in facts:
            meta = json.loads(fact.metadata_json)
            if {name: meta.get(name) for name in THREAD_FIELDS} == want:
                return meta
    best = None
    for fact in facts:
        rank = (fact.source_path, int(fact.metadata_offset))
        if best is None or rank > best[0]:
            best = (rank, fact.metadata_json)
    return None if best is None else json.loads(best[1])


class SealDecision(NamedTuple):
    sealable: bool
    blocking: tuple
    certificate: Optional[dict]


def decide_seal(conversation_key: str, revision: int,
                targets: Iterable[Target], members: Iterable[MemberFact], *,
                timestamps: "tuple[str | None, str | None] | None" = None,
                retained_thread: Optional[dict] = None,
                ) -> SealDecision:
    """Seal a certificate for one conversation, or name what blocks it.

    ``targets`` holds every identity with any membership row for the key
    (``RETAINED_GENERATION`` rows included); ``members`` those rows. A
    REQUIRED contributor blocks (the transient pending hold included); a
    SUSPENDED one is an explicit exclusion whose retained accepted facts
    still supply metadata (U6): its watermark generation's rows when its
    membership was proven, else its ``RETAINED_GENERATION`` rows (review N2).
    ``retained_thread`` is the key's retained thread row: preferred when a
    candidate supplies it (P2-2), and kept when an exclusion leaves no
    metadata, so a seal never retracts a published thread to None because of
    an excluded contributor (P1-1, N2).
    """
    by_identity = {t.identity: t for t in targets}
    verdicts = {i: classify_target(t) for i, t in by_identity.items()}
    blocking = tuple(sorted((i, v.reason) for i, v in verdicts.items()
                            if v.state == REQUIRED))
    if blocking:
        return SealDecision(False, blocking, None)
    facts = list(members)
    contributors, exclusions, candidates = [], [], []
    for identity in sorted(by_identity):
        target, verdict = by_identity[identity], verdicts[identity]
        if verdict.state == READY:
            contributors.append({
                "identity": identity, "path": target.source_path,
                "generation": target.generation,
                "completeLineEnd": int(target.complete_line_end)})
            effective = target.generation
        else:
            exclusions.append({
                "identity": identity, "path": target.source_path,
                "reason": verdict.reason,
                "acceptedGeneration": target.watermark_generation})
            effective = (target.watermark_generation
                         if target.watermark_generation is not None
                         else RETAINED_GENERATION)
        candidates.extend(f for f in facts if f.identity == identity
                          and f.generation == effective)
    thread = select_thread(candidates, retained=retained_thread)
    if thread is None and retained_thread is not None and exclusions:
        # U6 / reviews P1-1, N2: an unreadable contributor never changes an
        # existing publication on its own, and it never freezes the key.
        # When no readable contributor and no retained fact supplies a
        # thread, the retained one is kept. Thread None is sealed only when
        # every contributor is readable and none supplies metadata, or when
        # there is no retained thread to retract.
        thread = {name: retained_thread.get(name) for name in THREAD_FIELDS}
    if thread is not None and timestamps is not None:
        thread = dict(thread, firstSeenUtc=timestamps[0],
                      lastSeenUtc=timestamps[1])
    return SealDecision(True, (), {
        "version": CERTIFICATE_VERSION,
        "conversationKey": conversation_key,
        "revision": int(revision),
        "contributors": contributors,
        "exclusions": exclusions,
        "thread": thread,
    })


def certificate_json(cert: dict) -> str:
    return json.dumps(cert, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def parse_certificate(text) -> Optional[dict]:
    try:
        cert = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(cert, dict) or cert.get("version") != CERTIFICATE_VERSION:
        return None
    return cert


def certificate_covers(cert: dict, source_path: str,
                       generation: str) -> Optional[int]:
    """The certified complete-line end for this file at this generation."""
    for contributor in cert.get("contributors", ()):
        if (contributor.get("path") == source_path
                and contributor.get("generation") == generation):
            return int(contributor["completeLineEnd"])
    return None


def certificate_excludes(cert: dict, source_path: str) -> bool:
    return any(e.get("path") == source_path for e in cert.get("exclusions", ()))


def publication_advances(published_revision, candidate_revision) -> bool:
    """Monotonic publication: only a strictly newer revision may publish."""
    return int(candidate_revision) > int(published_revision or 0)


#: cache.db ``cache_meta``: a fresh token written by every seal batch that
#: sealed anything (a token, not a counter: a deleted cache.db must never
#: repeat a value a conversations store has already acknowledged).
SEAL_SEQ_KEY = "codex_barrier_seal_seq"
#: conversations.db ``cache_meta``: the seal token whose certificates a
#: complete publication phase has considered.
PUBLISHED_SEQ_KEY = "codex_publication_seal_seq"
#: The TEMP view every conversations connection carries (5.11 "Reader
#: admission: last-good"); ``codex_conversation_threads`` shadows to it.
LAST_GOOD_THREADS_VIEW = "codex_last_good_threads"
#: Certificates newer than their publication, or never published. Run on a
#: conversations connection with cache.db attached as ``cache_db``.
BACKLOG_CERTIFICATES_SQL = (
    "SELECT c.conversation_key, c.revision, c.certificate_json "
    "FROM cache_db.codex_barrier_certificates c "
    "LEFT JOIN main.codex_conversation_publications p "
    "ON p.conversation_key = c.conversation_key "
    "WHERE p.conversation_key IS NULL OR c.revision > p.published_revision")
