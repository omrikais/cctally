"""The metering-rate notification-delivery ledger, as pure functions (#695).

A `meter_rate_change` row can be created by step-4a journal replay, which has
no `IngestContext` and is structurally unable to queue the notification
(invariant iv). The row then exists, the user is never told, and nothing
retries it. This ledger is the durable record of which identities have had a
dispatch opportunity claimed, so a later `cctally quota` can find the ones
that have not.

It lives OUTSIDE the journal and outside stats.db on purpose. A stats rebuild
replays the whole journal, so any delivery state derived from journal events
would come back empty and resurrect every historical notification. It also
lives outside `quota-calibrations.json`, because `reduce_state` and
`reset_calibration` each return a freshly constructed document and would
discard an additive key on the next ordinary write.

Everything here is total and side-effect free. The file, the lock, the atomic
write and the stats read belong to `bin/_cctally_quota_model.py`.
"""

#: Bumped only for a change to the document's own shape. `validate` rejects a
#: version ABOVE this one, so a newer binary's file is refused rather than
#: misread; a lower version is accepted, because every field this binary reads
#: has been present since version 1.
SCHEMA_VERSION: int = 1

FILENAME: str = "quota-rate-change-notification-decisions.json"
LOCK_FILENAME: str = "quota-rate-change-notification-decisions.lock"

#: The compare-and-set outcomes. The caller dispatches on CLAIM_WON and on
#: nothing else: `already_decided` means another process is dispatching,
#: `unusable` means the ledger cannot elect anyone, and `write_failed` leaves
#: the identity owed so a later run retries it.
CLAIM_WON: str = "won"
CLAIM_ALREADY_DECIDED: str = "already_decided"
CLAIM_UNUSABLE: str = "unusable"
CLAIM_WRITE_FAILED: str = "write_failed"


def empty_state() -> dict:
    """A fresh, unseeded ledger. NOT what an unusable file degrades to."""
    return {"schemaVersion": SCHEMA_VERSION, "seededFromStats": False,
            "decided": []}


def validate(document) -> "dict | None":
    """The document when every clause holds, otherwise None.

    None means UNUSABLE, and the caller must neither quarantine the file nor
    treat it as empty: quarantining discards a real record of dispatched
    notifications, and an empty reading makes every recorded identity look
    owed, which fires the whole history.

    `bool` is excluded wherever an `int` is required, following
    `load_calibrations`: `isinstance(True, int)` is true, so a `schemaVersion`
    of `true` would otherwise read as version 1.

    Duplicate entries are NOT a failure. A duplicate changes no answer this
    module gives — `decided_set` collapses it and `with_decided` writes the
    collapsed form back — so rejecting the file over one would disable the
    mechanism for a difference nobody can observe.
    """
    if not isinstance(document, dict):
        return None
    version = document.get("schemaVersion")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if version > SCHEMA_VERSION:
        return None
    if not isinstance(document.get("seededFromStats"), bool):
        return None
    decided = document.get("decided")
    if not isinstance(decided, list):
        return None
    for entry in decided:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            return None
        if not all(isinstance(part, str) for part in entry):
            return None
    return document


def decided_set(state) -> set:
    """The decided identities as `(provider, account_key, effective_from)`.

    The tuple is `RateChangeTransition.identity()` verbatim. It is NOT the
    calibration state's account key: `_state_key` maps a `None` account to
    `"*"` while `_alert_account_key` maps it to `unattributed`, and a ledger
    keyed on the first would miss every lookup made with the second.
    """
    return {tuple(entry) for entry in (state.get("decided") or ())}


def with_decided(state, identities, *, seeded=None) -> dict:
    """A NEW state carrying `identities` as decided, sorted and deduplicated.

    `seeded` is left at None to carry the existing flag forward; the seed
    passes True. The document is rebuilt rather than mutated so a caller
    cannot half-apply a write it then fails to persist.
    """
    merged = decided_set(state) | {tuple(i) for i in identities}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "seededFromStats": (bool(state.get("seededFromStats"))
                            if seeded is None else bool(seeded)),
        "decided": [list(i) for i in sorted(merged)],
    }


def owed(recorded, decided) -> tuple:
    """Recorded identities with no decision, in a stable order.

    `recorded` is read from `meter_rate_change_events` and NEVER from the
    calibration file's enumerated pairs. That file is discardable —
    `reset_calibration` removes an account's regimes and `load_calibrations`
    quarantines a malformed one — and an identity absent from both the
    candidates and the ledger would be owed forever.
    """
    return tuple(sorted(set(recorded) - set(decided)))
