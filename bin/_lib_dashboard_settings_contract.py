"""Which settings leaves ``POST /api/settings`` may write.

Single source of truth for the dashboard settings contract (#513 S1). Kept
pure and dependency-free -- it imports nothing -- so the documentation test
can read the contract without loading the dashboard, and so there is no cycle
with ``_cctally_config``.

Three states, not two. A leaf the endpoint persists is ``WRITABLE``. A leaf
it deliberately accepts and does not persist is ``KNOWN_IGNORED``: there are
exactly four, pinned by #134 (the Codex partial-merge contract, where amounts
stay CLI-only) and #143 (``budget.period`` drives the forward-only reconcile
without being stored here). Anything absent from the map is unknown to this
endpoint and is rejected with the offending dotted path as ``field``.

The third state matters because two states would force ``budget.period`` to
be described as either writable or rejected, and it is neither.
"""

WRITABLE = "writable"
KNOWN_IGNORED = "known_ignored"

#: Fully-qualified dotted path -> disposition. Order is the endpoint's block
#: order and is not significant; membership is.
SETTINGS_LEAF_DISPOSITIONS = {
    "display.tz": WRITABLE,
    "alerts.enabled": WRITABLE,
    "alerts.projected_enabled": WRITABLE,
    "alerts.notifier": WRITABLE,
    "dashboard.cache_failure_markers": WRITABLE,
    "dashboard.live_tail": WRITABLE,
    "dashboard.lan_auth": WRITABLE,
    "update.check.enabled": WRITABLE,
    "update.check.ttl_hours": WRITABLE,
    "update.channel": WRITABLE,
    "cache_report.anomaly_threshold_pp": WRITABLE,
    "budget.weekly_usd": WRITABLE,
    "budget.alerts_enabled": WRITABLE,
    "budget.alert_thresholds": WRITABLE,
    "budget.projected_enabled": WRITABLE,
    "budget.project_alerts_enabled": WRITABLE,
    "budget.codex.alerts_enabled": WRITABLE,
    "budget.codex.projected_enabled": WRITABLE,
    # #143: answered 200 and drives the forward-only reconcile, never stored.
    "budget.period": KNOWN_IGNORED,
    # #134: CLI-only amounts, preserved from the persisted block on a merge.
    "budget.codex.amount_usd": KNOWN_IGNORED,
    "budget.codex.period": KNOWN_IGNORED,
    "budget.codex.alert_thresholds": KNOWN_IGNORED,
}


def _ancestors(path):
    """Yield every proper dotted prefix of ``path``, outermost first."""
    parts = path.split(".")
    for i in range(1, len(parts)):
        yield ".".join(parts[:i])


#: Every interior path a request may descend through, derived from the leaf
#: paths so a new leaf can never forget to register its parent.
SETTINGS_OBJECT_PATHS = frozenset(
    parent
    for path in SETTINGS_LEAF_DISPOSITIONS
    for parent in _ancestors(path)
)

#: The six blocks the endpoint accepts at the top level.
SETTINGS_TOP_LEVEL_BLOCKS = frozenset(
    path.split(".")[0] for path in SETTINGS_LEAF_DISPOSITIONS
)

#: Blocks that are NOT a valid no-op when sent empty. A named block carrying
#: no leaves is an ordinary partial-PUT no-op everywhere else -- notably
#: ``{"cache_report": {}}``, which is what a combined save sends when the user
#: never opened that tab, and which must keep answering 200.
SETTINGS_REQUIRED_LEAVES = {"display": frozenset({"display.tz"})}


def disposition_for(path):
    """Return ``WRITABLE``, ``KNOWN_IGNORED``, or ``None`` for an unknown path."""
    return SETTINGS_LEAF_DISPOSITIONS.get(path)


def is_object_path(path):
    """True when ``path`` is an interior node a request may descend through."""
    return path in SETTINGS_OBJECT_PATHS
