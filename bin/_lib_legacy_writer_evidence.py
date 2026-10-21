"""Pure legacy-writer evidence (#825/#832, spec section 8, case F9).

After the account-bound cutover, records an older cctally process appends are
excluded from current accounting. `doctor`'s `journal.legacy_writer` leg warns
while that is still happening, and clears after seven quiet days (operator
Q4). The seven-day clock needs evidence of WHEN the excluded population was
first observed, and neither the disposable index (rebuilt) nor a record's `at`
(event time, which a backdated legacy event fakes) provides it. So one small
durable sidecar records:

* the cutover identity;
* the excluded population: its count and an ORDER-INDEPENDENT digest over the
  physical coordinates of every excluded complete record;
* `firstObservedAt`: the wall-clock time a current scan first observed THAT
  population.

The trigger is the population of physical coordinates, never a record's `at`
or the maximum coordinate alone, so a record added to an earlier segment, a
new bootstrap segment, or a backdated event all count as new evidence. A scan
that finds the same population leaves the evidence untouched, so a rebuild
over unchanged evidence does not restart the age. A missing sidecar is
recreated with the current time, which can only lengthen a warning. The
sidecar is diagnostic only: it never decides admission.

Pure: no I/O, no clock (callers pass `now`).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re

SCHEMA_VERSION = 1
QUIET_DAYS = 7
REMEDY = ("an older cctally process (for example a dashboard started before "
          "the upgrade) is still writing; restart it")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclasses.dataclass(frozen=True)
class Evidence:
    cutover_id: str
    excluded_count: int
    coordinate_digest: str
    first_observed_at: str  # ISO-8601 UTC


def _parse_instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def to_document(evidence: Evidence) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "cutoverId": evidence.cutover_id,
        "excludedCount": evidence.excluded_count,
        "coordinateDigest": evidence.coordinate_digest,
        "firstObservedAt": evidence.first_observed_at,
    }


def from_document(doc) -> Evidence | None:
    """The evidence a sidecar document holds, or None when it is not a valid
    document (treated exactly like a missing sidecar)."""
    if not isinstance(doc, dict) or doc.get("schemaVersion") != SCHEMA_VERSION:
        return None
    cutover_id = doc.get("cutoverId")
    count = doc.get("excludedCount")
    digest = doc.get("coordinateDigest")
    first = doc.get("firstObservedAt")
    if (not isinstance(cutover_id, str) or not cutover_id
            or type(count) is not int or count < 0
            or not isinstance(digest, str) or not _DIGEST_RE.match(digest)
            or _parse_instant(first) is None):
        return None
    return Evidence(cutover_id, count, digest, first)


def observe(existing: Evidence | None, *, cutover_id: str | None,
            excluded_count: int, coordinate_digest: str | None,
            now: dt.datetime) -> Evidence | None:
    """The update rule. Returns the evidence a scan should leave behind:

    * no cutover, or no excluded record at all: ``existing`` unchanged (None
      when there was none) — nothing to warn about;
    * the same cutover, count and digest: ``existing`` unchanged, so the age
      keeps running;
    * anything else (a new population, or no valid sidecar): new evidence
      first observed ``now``.
    """
    if cutover_id is None or excluded_count <= 0 or coordinate_digest is None:
        return existing
    if (existing is not None
            and existing.cutover_id == cutover_id
            and existing.excluded_count == excluded_count
            and existing.coordinate_digest == coordinate_digest):
        return existing
    return Evidence(cutover_id, int(excluded_count), coordinate_digest,
                    _iso(now))


@dataclasses.dataclass(frozen=True)
class Verdict:
    severity: str          # "ok" | "warn"
    state: str             # "none" | "recent" | "quiet"
    excluded_count: int
    last_seen_at: str | None
    age_seconds: int | None


def classify(evidence: Evidence | None, *, excluded_count: int,
             now: dt.datetime) -> Verdict:
    """The leg's verdict: WARN while the latest excluded population was first
    observed under `QUIET_DAYS` ago, OK with a historical note afterwards,
    OK with nothing to report when nothing was ever excluded."""
    if excluded_count <= 0 or evidence is None:
        return Verdict("ok", "none", max(0, excluded_count), None, None)
    first = _parse_instant(evidence.first_observed_at)
    age = max(0, int((now.astimezone(dt.timezone.utc) - first).total_seconds()))
    recent = age < QUIET_DAYS * 86400
    return Verdict("warn" if recent else "ok",
                   "recent" if recent else "quiet",
                   excluded_count, evidence.first_observed_at, age)
