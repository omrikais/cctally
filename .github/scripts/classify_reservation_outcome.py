#!/usr/bin/env python3
"""Validate scheduled-CI reservation refusal; never manufacture a suite pass.

Only the reservation helper's pre-workload 300-second wait-exhaustion branch
emits this sanitized proof. The run/attempt ID and fresh invocation nonce must
match the workflow, and no suite outcome may exist. An absent, stale, malformed
or unsanitized document fails closed. The workflow remains failed; a successful
marker represents unavailable full verification, never a receipt or a pass.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import stat


FIELDS = frozenset(("schemaVersion", "runId", "invocationId", "cause", "reservationState",
                    "workloadStarted", "waitSeconds", "waitedSeconds", "observedAt"))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def classify(doc, *, event_name, run_id, invocation_id):
    """None iff this exact scheduled invocation proved no-workload wait exhaustion."""
    if event_name != "schedule":
        return "the event is not schedule"
    if not re.fullmatch(r"macos-[1-9][0-9]*-[1-9][0-9]*", run_id):
        return "the expected run/attempt identity is invalid"
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        return "the expected invocation identity is invalid"
    if not isinstance(doc, dict) or set(doc) != FIELDS:
        return "the record does not have exactly the sanitized proof fields"
    if type(doc["schemaVersion"]) is not int or doc["schemaVersion"] != 1:
        return "the proof version is not 1"
    if doc["runId"] != run_id or doc["invocationId"] != invocation_id:
        return "the proof belongs to another run, attempt or invocation"
    if doc["cause"] != "reservation-wait-exhausted":
        return "the refusal cause is not confirmed wait exhaustion"
    if doc["reservationState"] not in ("busy", "unknown") or doc["workloadStarted"] is not False:
        return "the proof does not establish refusal before workload execution"
    if not _number(doc["waitSeconds"]) or doc["waitSeconds"] != 300:
        return "the proof does not state the CI 300-second admission budget"
    if not _number(doc["waitedSeconds"]) or doc["waitedSeconds"] < 300:
        return "the admission budget was not exhausted"
    try:
        moment = datetime.fromisoformat(doc["observedAt"])
        if moment.utcoffset() != timedelta(0):
            return "the observation does not carry a UTC timestamp"
    except (TypeError, ValueError):
        return "the observation timestamp is malformed"
    return None


def _object(pairs):
    doc = {}
    for key, value in pairs:
        if key in doc:
            raise ValueError("duplicate JSON key")
        doc[key] = value
    return doc


def _constant(value):
    raise ValueError("non-finite JSON constant")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--suite-outcome", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.path.lexists(args.suite_outcome):
        print("not missing verification: a suite outcome exists")
        return 1
    try:
        fd = os.open(args.record, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("not a bounded regular proof file")
            doc = json.load(stream, object_pairs_hook=_object, parse_constant=_constant)
        reason = classify(doc, event_name=args.event_name, run_id=args.run_id,
                          invocation_id=args.invocation_id)
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        reason = "the proof is absent, unreadable or malformed"
    if reason is not None:
        print("not missing verification: " + reason)
        return 1
    print("full verification unavailable: reservation wait exhausted before workload execution")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
