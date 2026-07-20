"""Dashboard conversation-viewer handlers (#279 S5 F4 — line-budget seam).

Consumer-only sibling of ``bin/_cctally_dashboard.py`` — it re-imports every
name below, so ``bin/cctally``'s re-exports and the conversation pytest
files (``tests/test_conversation_endpoints.py``,
``tests/test_conversation_query.py``) keep resolving unchanged (spec §6).

What lives here (spec §6):
- the conversation constants/helpers ``_CONV_SEARCH_KINDS`` /
  ``_CONV_FIND_KINDS`` / ``_BadConversationFilter`` / ``_cached_file_sigs``
  (the ``/api/debug/backend`` helpers + ``_DEBUG_CACHE_TABLES`` +
  ``_handle_get_debug_backend`` are debug-coupled and STAY in the dashboard,
  gate P3-2);
- the query plumbing (``_conversation_query_impl`` / ``_parse_search_kind_impl``
  / ``_run_conversation_query_impl`` / ``_parse_conversation_filters_impl``);
- the eleven ``_handle_get_conversation*_impl(handler, …)`` handlers
  (including ``_handle_get_conversation_events_impl``, the live-tail SSE
  watch loop, with its ``_lib_conversation_watch`` usage). The dashboard
  keeps thin bound delegators on ``DashboardHTTPHandler``; the privacy gates
  (``_require_transcripts_allowed`` / ``_transcript_gate`` / …) STAY on the
  class and are reached via the ``handler`` parameter (spec §6).

Late-binding (spec §6): the two bare ``open_cache_db`` calls (in
``_run_conversation_query_impl`` + the events handler) reach
``sys.modules["_cctally_dashboard"].open_cache_db(...)`` — patched on the
dashboard module object at ``tests/test_conversation_endpoints.py:674``.

Cross-module reaches (spec §2.1 "fully-qualify cross-module refs"): the
cctally-forwarding shims the moved code called by bare name
(``_resolve_display_tz_obj`` / ``_apply_display_tz_override`` / ``load_config``)
are inlined to their ``sys.modules["cctally"].X`` call-time reach — behavior
+ ns["X"] patch surface preserved (none is dashboard-object-patched;
audited). The generic query-string helpers ``_qs_str`` / ``_qs_int`` stay in
the dashboard (used across every handler); they have 24 call sites here, so
they are reached via module-local forwarders (below) rather than 24 inline
edits — the forwarders hit ``sys.modules["_cctally_dashboard"]`` at call time
(no cycle: the dashboard is fully loaded before any handler runs).
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys

from _cctally_cache import sync_cache, sync_codex_cache, _codex_provider_roots

# Live-tail watch-loop tuning — used ONLY by _handle_get_conversation_events_impl
# below, so moved here with the events handler (spec §4.1 / §6).
_LIVE_TAIL_POLL_INTERVAL = 1.0      # seconds between stat polls of the open file(s)
_LIVE_TAIL_DEBOUNCE = 0.25          # settle window after first detected growth
_LIVE_TAIL_KEEPALIVE = 15.0         # idle keep-alive cadence (proxy guard)
_LIVE_TAIL_FILE_RESET_EVERY = 10    # re-resolve the session file set every N cycles


# Module-local forwarders for the two generic query-string helpers that STAY
# in the dashboard (24 call sites in the moved handlers; the dashboard is fully
# loaded before any handler runs, so the call-time reach is cycle-free).
def _qs_str(*args, **kwargs):
    return sys.modules["_cctally_dashboard"]._qs_str(*args, **kwargs)


def _qs_int(*args, **kwargs):
    return sys.modules["_cctally_dashboard"]._qs_int(*args, **kwargs)


# #177 S6 / #217 S2: valid kind facets for the conversation routes. Kept in
# lockstep with the kernel (``_lib_conversation_query._SEARCH_KINDS`` /
# ``_FIND_KINDS``; the kernel re-raises ValueError on an unknown kind, and the
# handlers reject with a 400 BEFORE the call — ``_run_conversation_query``
# collapses every kernel exception to a 500, so a per-route 4xx must be decided
# in the handler, not via try/except around the kernel).
#
# P1-1 (load-bearing kind-validation SPLIT): the cross-session search route
# accepts ``title`` and ``files``; the in-conversation ``/find`` route does NOT —
# its kernel (``find_in_conversation``) indexes ``_FIND_KIND_COLUMNS[kind]``,
# which has no ``title``/``files`` entry, so accepting them there would be a 500
# KeyError. Two distinct tuples keep ``/find?kind=title`` and ``/find?kind=files``
# a clean 400.
_CONV_SEARCH_KINDS = (
    "all", "prompts", "assistant", "tools", "thinking", "title", "files")
_CONV_FIND_KINDS = ("all", "prompts", "assistant", "tools", "thinking")


class _BadConversationFilter(Exception):
    """Internal sentinel: a browse-filter query param failed validation. The
    parse helper has ALREADY sent the 400 response when this is raised, so the
    caller just unwinds and returns (the conversation routes all 400 on bad
    input, consistent with the search ``kind`` facet). Module-private."""


def _cached_file_sigs(conn, paths):
    """{path: size_bytes} from session_files for the given paths — the cache's
    own view of how far each file is ingested. Size-only by design, matching the
    watch kernel's size-only signature (`file_sig`) and sync_cache's size-only
    delta signal: mtime is NOT consulted, because a size-unchanged ingest does
    not refresh session_files.mtime_ns and a stale mtime would re-detect
    'changed' every cycle forever. Used to baseline the live-tail watch so a file
    the cache hasn't caught up on reads as 'changed' on cycle 1 (spec §2.4).
    Paths with no row are simply absent → treated as changed."""
    out = {}
    if not paths:
        return out
    placeholders = ",".join("?" for _ in paths)
    try:
        rows = conn.execute(
            f"SELECT path, size_bytes FROM session_files "
            f"WHERE path IN ({placeholders})", list(paths)).fetchall()
    except sqlite3.OperationalError:
        return out
    for p, size in rows:
        out[p] = size
    return out


def _codex_cached_file_sigs(conn, paths):
    """{path: size_bytes} from ``codex_session_files`` for the given paths — the
    Codex analogue of ``_cached_file_sigs`` (spec §5.2). Size-only, matching the
    watch kernel's size-only signature and ``sync_codex_cache``'s size-only
    delta. Paths with no row are absent → treated as changed. Baselines the
    Codex live-tail watch against the cache's own committed cursor so growth
    during an ingest is re-detected next cycle (the per-path committed-cursor
    invariant — the S6 physical mutation sequence is at most an extra
    certificate, never this cursor's replacement)."""
    out = {}
    if not paths:
        return out
    placeholders = ",".join("?" for _ in paths)
    try:
        rows = conn.execute(
            f"SELECT path, size_bytes FROM codex_session_files "
            f"WHERE path IN ({placeholders})", list(paths)).fetchall()
    except sqlite3.OperationalError:
        return out
    for p, size in rows:
        out[p] = size
    return out


def _codex_all_committed_sizes(conn):
    """{path: size_bytes} for EVERY tracked Codex file — fed to the frontier as
    both the ``known_paths`` diff set and the ``committed_sizes`` growth
    baseline. A plain SELECT (no lock), so it never widens the SSE lock scope."""
    try:
        rows = conn.execute(
            "SELECT path, size_bytes FROM codex_session_files").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {p: size for (p, size) in rows}


def _codex_classified_paths(conn, paths):
    """Of ``paths``, those whose targeted ingest now carries a conversation key
    (``codex_session_files.last_conversation_key`` non-null) — i.e. classified
    (child or non-child). The driver reaps these from the frontier's pending set
    (§5.4): a child has already widened the file set; a non-child needs no
    further attention. Paths still unclassified (incomplete session_meta / a
    dirty first ingest) stay pending and are retried."""
    if not paths:
        return set()
    placeholders = ",".join("?" for _ in paths)
    try:
        rows = conn.execute(
            f"SELECT path FROM codex_session_files "
            f"WHERE path IN ({placeholders}) AND last_conversation_key IS NOT NULL",
            list(paths)).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


def _codex_walk_root_for_conversation(conn, conversation_key):
    """The configured ``walk_root`` of the conversation's OWN provider root (the
    frontier's scope — §5.4). Resolves the conversation's ``source_root_key`` via
    its thread facts, then matches it to a currently-configured provider root.
    ``None`` when the conversation has no thread row or its root is no longer
    configured (the frontier is then skipped — DB re-resolve alone still runs)."""
    cq = _conversation_query_impl_codex()
    thread = cq._thread_facts(conn, conversation_key)
    if thread is None:
        return None
    source_root_key = thread[3]
    for root in _codex_provider_roots():
        if root.source_root_key == source_root_key:
            return str(root.walk_root)
    return None


def _conversation_query_impl_codex():
    """Lazy-load the Codex conversation query kernel (source-path resolver +
    existence probe + thread facts for the live-tail file set)."""
    return sys.modules["cctally"]._load_sibling("_lib_codex_conversation_query")


def _conversation_dispatch_impl():
    """Lazy-load the provider-neutral dispatch kernel (the SSE preflight)."""
    return sys.modules["cctally"]._load_sibling("_lib_conversation_dispatch")


def _conversation_query_impl():
    """Lazy-load the pure conversation query kernel (Plan 2, §3)."""
    return sys.modules["cctally"]._load_sibling("_lib_conversation_query")


# ── #294 S7 — dual-form conversation routes (spec §2) ─────────────────────────
# Entity routes qualify lexically (an id beginning ``v1.`` opts into the neutral
# envelope contract); the three collection routes qualify by an explicit strict
# ``?source={claude,codex}``. Absence of qualification keeps today's Claude path
# byte-identical (the legacy code never touches the resolver). This is C1/C2.

# Every conversation param recognized across the three collection routes (§2.2).
# A qualified request rejects (400) any RECOGNIZED param not in that route's
# accepted whitelist; a GENUINELY unknown param (not in this set) is ignored,
# matching legacy leniency. Entity-only params (after/before/tail/scope/regex/…)
# are deliberately absent — they are meaningless on a collection route, so they
# fall to "genuinely unknown → ignored".
_RECOGNIZED_CONVERSATION_PARAMS = (
    "source", "project_key", "model", "limit", "cursor", "q", "kind",
    "sort", "offset", "date_from", "date_to", "projects",
    "cost_min", "cost_max", "rebuild_min", "models",
)
_QUALIFIED_BROWSE_ACCEPTED = ("source", "project_key", "model", "limit", "cursor")
_QUALIFIED_SEARCH_ACCEPTED = ("source", "q", "kind", "limit", "cursor")
_QUALIFIED_FACETS_ACCEPTED = ("source",)
# A raw browse cursor is a conversation key — printable + URL-safe by construction
# (§2.2). Syntactic-only validation: reject whitespace/control/empty; echo raw.
_BROWSE_CURSOR_RE = re.compile(r"\A[!-~]+\Z")


def _resolve_effective_speed():
    """§2.4: resolve ``auto`` → a concrete speed ONCE at the route I/O boundary,
    via the SAME chokepoint the Codex reporting commands use. Search never prices;
    detail/outline/export thread it down (Claude ignores it — cost is materialized)."""
    return sys.modules["cctally"]._resolve_codex_speed("auto")


def _conversation_dispatch():
    """Lazy-load the provider-neutral dispatch kernel (the S7 entity ops)."""
    return _conversation_dispatch_impl()


def _parse_source_param(handler, qs_raw):
    """Strict ``?source=`` parse (§2.2). Returns ``(qualified, source)``:

    - ``(False, None)`` — the param is absent → the caller runs the legacy path
      byte-identically;
    - ``(True, "claude"|"codex")`` — exactly one literal value;

    or ``None`` after having ALREADY sent a 400 (blank, duplicated, ``all``, or any
    other unknown value)."""
    import urllib.parse as _u
    vals = _u.parse_qs(qs_raw, keep_blank_values=True).get("source")
    if vals is None:
        return (False, None)
    if len(vals) != 1 or vals[0] not in ("claude", "codex"):
        handler._respond_json(400, {"error": f"invalid source: {vals}"})
        return None
    return (True, vals[0])


def _validate_qualified_params(handler, qs_raw, accepted):
    """§2.2 strict qualified-param validation. Rejects (400) any RECOGNIZED
    conversation param not in ``accepted``, and any accepted param that appears
    more than once. Genuinely-unknown params (outside
    ``_RECOGNIZED_CONVERSATION_PARAMS``) are ignored. Returns the
    ``keep_blank_values`` parse map on success, or ``None`` after a 400."""
    import urllib.parse as _u
    parsed = _u.parse_qs(qs_raw, keep_blank_values=True)
    accepted_set = set(accepted)
    for name, vals in parsed.items():
        if name in _RECOGNIZED_CONVERSATION_PARAMS and name not in accepted_set:
            handler._respond_json(400, {"error": f"unexpected param: {name}"})
            return None
        if name in accepted_set and len(vals) != 1:
            handler._respond_json(400, {"error": f"duplicate param: {name}"})
            return None
    return parsed


def _parse_qualified_limit(handler, parsed):
    """Strict qualified ``limit`` (§2.2): a base-10 integer 1..500. Absent → the
    kernel default (50); malformed or out of range → 400. Returns ``(ok, limit)``
    (``ok=False`` means a 400 was already sent)."""
    vals = parsed.get("limit")
    if vals is None:
        return (True, 50)
    raw = vals[0]
    if not (raw.isascii() and raw.isdigit()):   # strict base-10, no sign/space
        handler._respond_json(400, {"error": f"bad limit: {raw}"})
        return (False, None)
    n = int(raw)
    if not (1 <= n <= 500):
        handler._respond_json(400, {"error": f"limit out of range: {n}"})
        return (False, None)
    return (True, n)


def _parse_qualified_browse_cursor(handler, parsed):
    """The browse ``cursor`` is a raw conversation key — passed verbatim both
    directions (§2.2). Syntactic-only: blank → no cursor; a non-printable /
    whitespace value → 400. Returns ``(ok, cursor)``."""
    vals = parsed.get("cursor")
    if vals is None or vals[0] == "":
        return (True, None)
    cur = vals[0]
    if not _BROWSE_CURSOR_RE.match(cur):
        handler._respond_json(400, {"error": "malformed cursor"})
        return (False, None)
    return (True, cur)


def _respond_qualified_json(handler, env):
    """Map a neutral entity envelope's ``status`` → the §2.3 HTTP JSON transport:
    ``ok`` / ``normalization_pending`` → 200; ``gone`` → 410; ``validation_error``
    → 400; anything else (``not_found`` + unknown) → 404. Body is the envelope."""
    status = (env or {}).get("status")
    if status in ("ok", "normalization_pending"):
        handler._respond_json(200, env)
    elif status == "gone":
        handler._respond_json(410, env)
    elif status == "validation_error":
        handler._respond_json(400, env)
    else:
        handler._respond_json(404, env)


def _serve_qualified_entity(handler, dispatch_call, log_label):
    """Run a neutral entity dispatch through the shared 500-envelope scaffold and
    map its status to the §2.3 JSON transport. For the JSON entity legs (detail,
    outline, prompts, find, payload); export/anon-map/media serve non-JSON or a
    bespoke existence probe, so they do not route through here."""
    ok, env = handler._run_conversation_query(dispatch_call, log_label)
    if not ok:
        return
    _respond_qualified_json(handler, env)


def _handle_qualified_browse(handler, qs_raw, source):
    """``GET /api/conversations?source=…`` (§2.2). Strict param whitelist; the
    neutral browse envelope served verbatim (codex ``normalization_pending`` is a
    legitimate 200 empty state)."""
    parsed = _validate_qualified_params(handler, qs_raw, _QUALIFIED_BROWSE_ACCEPTED)
    if parsed is None:
        return
    ok, limit = _parse_qualified_limit(handler, parsed)
    if not ok:
        return
    ok, cursor = _parse_qualified_browse_cursor(handler, parsed)
    if not ok:
        return
    project_key = (parsed.get("project_key", [None])[0] or None)
    model = (parsed.get("model", [None])[0] or None)
    speed = _resolve_effective_speed()
    disp = _conversation_dispatch()
    ok, body = handler._run_conversation_query(
        lambda conn: disp.neutral_browse(
            conn, source=source, effective_speed=speed,
            project_key=project_key, model=model, limit=limit, cursor=cursor),
        "/api/conversations")
    if not ok:
        return
    handler._respond_json(200, body)


def _handle_qualified_facets(handler, qs_raw, source):
    """``GET /api/conversations/facets?source=…`` (§2.2). Accepts ``source`` only;
    serves the status-tagged facets-only envelope (pending → empty facet lists)."""
    parsed = _validate_qualified_params(handler, qs_raw, _QUALIFIED_FACETS_ACCEPTED)
    if parsed is None:
        return
    speed = _resolve_effective_speed()
    disp = _conversation_dispatch()
    ok, body = handler._run_conversation_query(
        lambda conn: disp.neutral_browse(
            conn, source=source, effective_speed=speed),
        "/api/conversations/facets")
    if not ok:
        return
    facets = (body or {}).get("facets") or {"projects": [], "models": []}
    handler._respond_json(
        200, {"status": (body or {}).get("status"), "facets": facets})


def _handle_qualified_search(handler, qs_raw, source):
    """``GET /api/conversation/search?source=…`` (§2.2). Strict whitelist; the
    search ``cursor`` must decode as base64url (else 400); the neutral search
    envelope (with the codec'd ``page.cursor``) is served verbatim."""
    parsed = _validate_qualified_params(handler, qs_raw, _QUALIFIED_SEARCH_ACCEPTED)
    if parsed is None:
        return
    query = (parsed.get("q", [""])[0] or "")
    kind = (parsed.get("kind", ["all"])[0] or "all")
    if kind not in _CONV_SEARCH_KINDS:
        handler._respond_json(400, {"error": f"unknown kind: {kind}"})
        return
    ok, limit = _parse_qualified_limit(handler, parsed)
    if not ok:
        return
    cursor = (parsed.get("cursor", [None])[0] or None)
    disp = _conversation_dispatch()
    if cursor is not None:
        try:
            disp.decode_search_cursor(cursor)
        except disp.InvalidSearchCursor:
            handler._respond_json(400, {"error": "invalid cursor"})
            return
    speed = _resolve_effective_speed()
    ok, body = handler._run_conversation_query(
        lambda conn: disp.neutral_search(
            conn, query, source=source, kind=kind,
            effective_speed=speed, limit=limit, cursor=cursor),
        "/api/conversation/search")
    if not ok:
        return
    handler._respond_json(200, body)


def _parse_search_kind_impl(handler, q, valid=_CONV_SEARCH_KINDS):
    """Read + validate the ``kind`` facet for a conversation route (#177 S6 /
    #217 S2). Returns the kind on success, or ``None`` after having ALREADY
    sent a 400 — callers just ``return`` on ``None``.

    ``valid`` is the per-route kind set (P1-1 split): the cross-session search
    route passes ``_CONV_SEARCH_KINDS`` (includes ``title``), the
    in-conversation ``/find`` route passes ``_CONV_FIND_KINDS`` (excludes
    ``title``/``files``), so ``/find?kind=title`` is a 400 here — never a 500
    KeyError downstream in ``find_in_conversation``. Kept in lockstep with the
    kernel's ``_SEARCH_KINDS`` / ``_FIND_KINDS`` (the kernel module is
    resolved lazily per-request, so the handler keeps literal tuples rather
    than reaching across that import edge for a nit)."""
    kind = _qs_str(q, "kind", "all")
    if kind not in valid:
        handler._respond_json(400, {"error": f"unknown kind: {kind}"})
        return None
    return kind

def _run_conversation_query_impl(handler, kernel_call, log_label):
    """Open cache.db, run ``kernel_call(conn)``, close — with the uniform
    500 envelopes the three conversation routes share (#151).

    Collapses the triplicated open-cache → try/except/finally → 500
    scaffold to one site. Returns ``(ok, body)``: ``ok=False`` means a 500
    has ALREADY been sent and the caller must just ``return``; ``ok=True``
    carries the kernel result (which may itself be ``None`` — the reader's
    404 sentinel — so the explicit flag, not ``body is None``, signals
    failure). An ``open_cache_db`` failure is a ``cache unavailable:`` 500;
    a kernel exception is logged as ``<log_label> failed: %r`` and returned
    as a ``{type}: {msg}`` 500 — byte-identical to the inlined handlers.
    """
    try:
        conn = sys.modules["_cctally_dashboard"].open_cache_db()  # late-binding: patched at test_conversation_endpoints.py:674
    except (sqlite3.DatabaseError, OSError) as exc:
        handler._respond_json(500, {"error": f"cache unavailable: {exc}"})
        return False, None
    try:
        body = kernel_call(conn)
    except Exception as exc:  # noqa: BLE001
        handler.log_error("%s failed: %r", log_label, exc)
        handler._respond_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return False, None
    finally:
        conn.close()
    return True, body

def _parse_conversation_filters_impl(handler, q):
    """Parse the browse-list filter params (spec §2) from a ``parse_qs``
    mapping. On any malformed value this sends a **400** and returns
    ``None`` — the caller just ``return``s (the conversation routes all 400
    on bad input). On success returns a dict of ``list_conversations``
    kwargs: ``date_from``/``date_to`` (UTC-ISO bounds), ``projects``
    (list[str] | None), ``cost_min``/``cost_max`` (float | None),
    ``rebuild_min`` (int | None), ``models`` (list[str] | None — the #278
    Theme C model-family axis). Empty/blank params drop to ``None``.

    Numeric axes validate strictly (a non-numeric cost / non-integer
    rebuild threshold is a hard 400). Date bounds route through the pure
    ``_lib_dashboard_dates.parse_filter_date_range`` helper, which resolves
    naive date-only bounds in ``display.tz`` and raises ``ValueError`` (→
    400) on a malformed date. Projects AND models accept BOTH repeated
    ``?projects=a&projects=b`` and a single comma-joined ``?projects=a,b``.
    """
    def _float(name):
        v = _qs_str(q, name, "")
        if v is None or v == "":
            return None
        try:
            return float(v)
        except ValueError:
            handler._respond_json(400, {"error": f"bad {name}: {v}"})
            raise _BadConversationFilter

    def _int(name):
        v = _qs_str(q, name, "")
        if v is None or v == "":
            return None
        try:
            return int(v)
        except ValueError:
            handler._respond_json(400, {"error": f"bad {name}: {v}"})
            raise _BadConversationFilter

    try:
        cost_min = _float("cost_min")
        cost_max = _float("cost_max")
        rebuild_min = _int("rebuild_min")
    except _BadConversationFilter:
        return None  # 400 already sent

    projects = [p for p in q.get("projects", []) if p] or None
    # Single comma-joined value -> split (the client may send either form).
    if projects and len(projects) == 1 and "," in projects[0]:
        projects = [s for s in projects[0].split(",") if s] or None

    # #278 Theme C: the model-family axis, mirroring projects. Accepts both
    # repeated ?models=opus&models=sonnet and a single comma-joined
    # ?models=opus,sonnet; blank/empty -> None. No numeric validation (enum-ish
    # strings); an unknown/typo'd family is a PRESENT axis that resolves to
    # zero ids in _model_clause -> zero results, never a silent unrestrict.
    models = [m for m in q.get("models", []) if m] or None
    if models and len(models) == 1 and "," in models[0]:
        models = [s for s in models[0].split(",") if s] or None

    date_from = _qs_str(q, "date_from", "") or None
    date_to = _qs_str(q, "date_to", "") or None
    if date_from or date_to:
        from importlib import import_module
        tz = sys.modules["cctally"]._resolve_display_tz_obj(
            sys.modules["cctally"]._apply_display_tz_override(
                sys.modules["cctally"].load_config(), type(handler).display_tz_pref_override
            )
        ).key
        try:
            df, dtt = import_module(
                "_lib_dashboard_dates"
            ).parse_filter_date_range(date_from, date_to, tz_name=tz)
        except ValueError as exc:
            handler._respond_json(400, {"error": str(exc)})
            return None
    else:
        df = dtt = None

    return {
        "date_from": df,
        "date_to": dtt,
        "projects": projects,
        "cost_min": cost_min,
        "cost_max": cost_max,
        "rebuild_min": rebuild_min,
        "models": models,
    }

def _handle_get_conversations_impl(handler) -> None:
    """``GET /api/conversations`` — the browse rail (spec §3.1).

    Gated first (loopback / Host allowlist). ``sort``/``limit``/``offset``
    are read from the query string; the kernel clamps bounds. The browse
    filters (date/project/cost/rebuild — spec §2) are parsed/validated here
    (malformed → 400) and threaded into the kernel. Cache-open failures are
    500s, never 5xx-with-stacktrace.
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    qs_raw = handler.path.partition("?")[2]
    parsed_src = _parse_source_param(handler, qs_raw)
    if parsed_src is None:
        return  # a 400 has already been sent
    qualified, source = parsed_src
    if qualified:
        return _handle_qualified_browse(handler, qs_raw, source)
    q = _u.parse_qs(qs_raw)
    sort = _qs_str(q, "sort", "recent")
    limit = _qs_int(q, "limit", 50)
    offset = _qs_int(q, "offset", 0)
    filters = handler._parse_conversation_filters(q)
    if filters is None:
        return  # a 400 has already been sent
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().list_conversations(
            conn, sort=sort, limit=limit, offset=offset, **filters),
        "/api/conversations")
    if not ok:
        return
    handler._respond_json(200, body)

def _handle_get_conversations_facets_impl(handler) -> None:
    """``GET /api/conversations/facets`` — distinct project labels + their
    conversation counts AND per-model-family session counts, for the browse
    filter's project + model multi-selects (spec §2 + #278 Theme C). Behind the
    SAME loopback/Host privacy gate as the list route. The projects half is a
    cheap GROUP BY over the ``conversation_sessions`` rollup; the model-family
    half is an index-only scan of ``conversation_messages(model, session_id)``
    via the partial covering index ``idx_conversation_messages_model_session``
    (#301 — a full heap-walk of the whole table before that index). The popover
    loads its options once from here (deriving from a paginated page would be
    incomplete).
    """
    if not handler._require_transcripts_allowed():
        return
    qs_raw = handler.path.partition("?")[2]
    parsed_src = _parse_source_param(handler, qs_raw)
    if parsed_src is None:
        return  # a 400 has already been sent
    qualified, source = parsed_src
    if qualified:
        return _handle_qualified_facets(handler, qs_raw, source)
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().list_conversation_facets(conn),
        "/api/conversations/facets")
    if not ok:
        return
    handler._respond_json(200, body)

def _handle_get_conversation_detail_impl(handler, path: str) -> None:
    """``GET /api/conversation/<session-id>`` — the reader (spec §3.2).

    The id is percent-decoded so clients that encode reserved chars
    round-trip. Unknown id → 404. ``after``/``before``/``tail``/``limit``
    page the items; ``after``/``before``/``tail`` are mutually exclusive
    (>1 supplied → 400). ``tail=1`` opens at the bottom; ``before=<id>``
    pages backward (#217 S2 / U4).
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    # ``path`` is already query-stripped by ``do_GET`` (``self.path.split("?")``),
    # so the cursor params (?after=/?before=/?tail=/?limit=) live ONLY on the
    # raw ``self.path``. Sibling handlers read ``self.path`` directly — the
    # detail route must too, or every request re-serves the head and
    # pagination is dead.
    query_str = handler.path.partition("?")[2]
    session_id = _u.unquote(path[len("/api/conversation/"):])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    q = _u.parse_qs(query_str)
    after = _qs_str(q, "after", None)
    before = _qs_str(q, "before", None)
    tail = _qs_str(q, "tail", None) in ("1", "true", "yes")
    limit = _qs_int(q, "limit", 500)
    # Mutual-exclusion 400 (#217 S2 / U4). The kernel ALSO raises ValueError
    # on >1 cursor as its own invariant, but ``_run_conversation_query``
    # collapses every kernel exception to a 500, so the 400 must be decided
    # HERE, before the kernel call — this explicit pre-call check is the
    # authoritative backstop for the handler path.
    if sum(1 for x in (after is not None, before is not None, tail) if x) > 1:
        handler.send_error(400, "after/before/tail are mutually exclusive")
        return
    if session_id.startswith("v1."):
        # Qualified (v1.) → neutral detail envelope (§2.1 / §2.3).
        speed = _resolve_effective_speed()
        disp = _conversation_dispatch()
        _serve_qualified_entity(
            handler,
            lambda conn: disp.neutral_detail(
                conn, session_id, effective_speed=speed,
                after=after, before=before, tail=tail, limit=limit),
            "/api/conversation")
        return
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().get_conversation(
            conn, session_id, after=after, before=before, tail=tail,
            limit=limit),
        "/api/conversation")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    handler._respond_json(200, body)

def _send_sse_headers(handler) -> None:
    """The exact SSE header set both the bare and qualified live-tail streams
    commit (kept in one place so bare stream bytes stay byte-identical)."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()


def _run_conversation_events_stream(
    handler, conn, *, passive, tail_data, resolve, ingest, cached_sigs,
    discovery_step=None,
) -> None:
    """Shared per-conversation live-tail SSE loop (spec §5.2), driving both the
    bare (``sessionId``) and qualified (``conversationKey``) streams from ONE
    body so their vocabulary — ``event: ready`` once, ``event: tail`` on
    committed growth, ``: keep-alive`` when idle — stays in lockstep and the bare
    stream bytes are byte-identical to today (only ``tail_data`` differs).

    ``resolve()`` → the watched file list; ``ingest(changed)`` → the targeted
    ingest (Claude ``sync_cache`` / Codex ``sync_codex_cache``) whose stats carry
    ``targeted_clean``; ``cached_sigs(paths)`` → ``{path: committed cursor}`` (the
    per-path committed-cursor baseline, so growth during an ingest is re-detected
    next cycle); ``discovery_step(files, seen) → (files, emitted)`` is the
    ~10-cycle Codex child-discovery frontier (``None`` for Claude — a bare or
    qualified-Claude stream never runs it). The cache lock is held ONLY inside
    ``ingest`` / ``discovery_step``, never across a sleep (the #297 WAL
    discipline)."""
    import time as _time
    watch = sys.modules["cctally"]._load_sibling("_lib_conversation_watch")
    tail_frame = (
        "event: tail\ndata: " + json.dumps(tail_data) + "\n\n").encode("utf-8")

    if passive:
        # Frozen-data contract: no ingest, no emit. Keep-alive only.
        while True:
            _time.sleep(_LIVE_TAIL_KEEPALIVE)
            handler.wfile.write(b": keep-alive\n\n")
            handler.wfile.flush()

    # #278 Theme B: signal that this connection is ACTIVELY live-tailing (not
    # degraded to keep-alive). The client sets `live` only on this 'ready'.
    handler.wfile.write(b"event: ready\ndata: {}\n\n")
    handler.wfile.flush()

    files = resolve()
    # Best-effort connect ingest for immediacy, then baseline `seen` from the
    # cache's own committed cursor so any pre-connect growth the connect-ingest
    # declined is still caught on cycle 1.
    try:
        if files:
            ingest(files)
    except sqlite3.DatabaseError:
        pass
    seen = cached_sigs(files)

    idle = 0.0
    cycles = 0
    while True:
        _time.sleep(_LIVE_TAIL_POLL_INTERVAL)
        cycles += 1
        changed = watch.changed_paths(files, seen)
        if changed:
            _time.sleep(_LIVE_TAIL_DEBOUNCE)
            new_seen, emitted = watch.watch_step(
                files, seen, ingest_fn=ingest,
                committed_sig_fn=lambda p: cached_sigs([p]).get(p))
            seen = new_seen
            if emitted:
                handler.wfile.write(tail_frame)
                handler.wfile.flush()
                idle = 0.0
                # A brand-new child file's FIRST content was just ingested, so
                # the conversation's source-path set may have grown. Re-resolve
                # now (vs waiting up to _LIVE_TAIL_FILE_RESET_EVERY cycles) so the
                # new thread live-tails promptly. A new path seeds seen=None (cur
                # lacks a row) → changed_paths flags it next cycle → it emits.
                new_files = resolve()
                if set(new_files) != set(files):
                    files = new_files
                    cur = cached_sigs(files)
                    for p in files:
                        seen.setdefault(p, cur.get(p))
                continue
        idle += _LIVE_TAIL_POLL_INTERVAL
        if idle >= _LIVE_TAIL_KEEPALIVE:
            handler.wfile.write(b": keep-alive\n\n")
            handler.wfile.flush()
            idle = 0.0
        if cycles % _LIVE_TAIL_FILE_RESET_EVERY == 0:
            # Layer 1 (both providers): DB re-resolve so a child ingested by ANY
            # other writer joins the watch immediately.
            files = resolve()
            seen = {p: s for p, s in seen.items() if p in set(files)}
            # Layer 2 (Codex only): bounded filesystem child discovery for
            # brand-new files no table yet knows (§5.4). A widened file set emits
            # a `tail` — the client refetches detail, whose children list
            # surfaces the new child (no new frame type).
            if discovery_step is not None:
                files, disc_emitted = discovery_step(files, seen)
                if disc_emitted:
                    handler.wfile.write(tail_frame)
                    handler.wfile.flush()
                    idle = 0.0


def _bare_conversation_events(handler, session_id: str) -> None:
    """Bare legacy Claude live-tail — today's no-preflight, ``sessionId``-framed
    behavior, byte-identical (spec §5.2 reserves this for bare streams)."""
    cq = handler._conversation_query()
    _send_sse_headers(handler)
    passive = bool(type(handler).no_sync)
    try:
        conn = sys.modules["_cctally_dashboard"].open_cache_db()  # late-binding: patched at test_conversation_endpoints.py:674
    except (sqlite3.DatabaseError, OSError):
        # Cache unavailable — degrade to keep-alive only; client backstop
        # tick still surfaces turns. (Headers already sent; can't 500.)
        passive = True
        conn = None

    def _resolve():
        return cq.session_source_paths(conn, session_id) if conn else []

    def _ingest(changed):
        return sync_cache(conn, only_paths=set(changed))

    try:
        _run_conversation_events_stream(
            handler, conn, passive=passive,
            tail_data={"sessionId": session_id},
            resolve=_resolve, ingest=_ingest,
            cached_sigs=lambda paths: _cached_file_sigs(conn, paths))
    except (BrokenPipeError, ConnectionResetError,
            ConnectionAbortedError, socket.timeout):
        # #279 S1 F3: a stalled send past the handler timeout raises
        # socket.timeout — treat as a client disconnect, not an error.
        pass
    except Exception as exc:  # noqa: BLE001
        # #279 S5 F6.2 (spec §8): headers are already committed, so route the
        # operator signal through the _lib_log chokepoint + a clean close.
        handler.log_error("api/conversation/events stream failed: %r", exc)
    finally:
        if conn is not None:
            conn.close()


def _make_codex_discovery_step(handler, conn, conversation_key, cq_codex):
    """Build the ~10-cycle Codex child-discovery frontier step (spec §5.4), or
    ``None`` when the conversation's own root is not currently configured (the
    frontier is then skipped — the DB re-resolve of layer 1 still runs). The
    frontier is constructed ONCE per SSE connection so its directory index,
    pending-candidate set, and rotation cursors accumulate across cycles."""
    walk_root = _codex_walk_root_for_conversation(conn, conversation_key)
    if walk_root is None:
        return None
    fw = sys.modules["cctally"]._load_sibling("_lib_codex_conversation_watch")
    frontier = fw.CodexChildFrontier(walk_root)

    def _discovery(files, seen):
        # Diff brand-new *.jsonl against every tracked Codex file; the same
        # {path: size} feeds both the frontier's known-set and its committed-size
        # growth baseline (a plain SELECT — no lock across the sleep).
        known = _codex_all_committed_sizes(conn)
        to_ingest = frontier.cycle(
            known_paths=set(known.keys()), committed_sizes=known)
        if not to_ingest:
            return files, False
        try:
            sync_codex_cache(conn, only_paths=set(to_ingest))
        except sqlite3.DatabaseError:
            return files, False
        # Reap the now-classified candidates (child → already widened; non-child
        # → done). Unclassified ones stay pending and are retried next cycle.
        frontier.reap(_codex_classified_paths(conn, to_ingest))
        new_files = cq_codex.codex_conversation_source_paths(conn, conversation_key)
        if set(new_files) == set(files):
            return files, False
        cur = _codex_cached_file_sigs(conn, new_files)
        for p in new_files:
            seen.setdefault(p, cur.get(p))
        return new_files, True

    return _discovery


def _qualified_conversation_events(handler, key: str) -> None:
    """Qualified (``v1.``) live-tail (spec §5.2): a neutral preflight — resolve →
    normalization authority (Codex) → existence — answered as plain JSON per
    §2.3 BEFORE any SSE bytes; only on ``ok`` are SSE headers committed and the
    ``conversationKey``-framed stream entered. A qualified Claude key reuses the
    existing Claude ingestion/watch mechanics internally; a Codex key uses
    targeted ingest + the directory-frontier child discovery."""
    disp = _conversation_dispatch_impl()
    try:
        conn = sys.modules["_cctally_dashboard"].open_cache_db()  # late-binding: patched at test_conversation_endpoints.py:674
    except (sqlite3.DatabaseError, OSError):
        conn = None

    if conn is None:
        # Cache unavailable — existence is unknowable, so we cannot preflight.
        # Degrade to a passive keep-alive stream (the client's backstop tick
        # still surfaces turns), mirroring the bare path's conn-failure fallback.
        _send_sse_headers(handler)
        try:
            _run_conversation_events_stream(
                handler, None, passive=True, tail_data={"conversationKey": key},
                resolve=lambda: [], ingest=lambda c: None,
                cached_sigs=lambda paths: {})
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, socket.timeout):
            pass
        except Exception as exc:  # noqa: BLE001
            handler.log_error("api/conversation/events stream failed: %r", exc)
        return

    try:
        preflight = disp.neutral_events_preflight(conn, key)
    except Exception as exc:  # noqa: BLE001
        # A preflight failure is a pre-headers error, so a JSON 500 is still
        # possible (unlike a mid-stream failure).
        handler.log_error("api/conversation/events preflight failed: %r", exc)
        handler._respond_json(500, {"error": "internal error"})
        conn.close()
        return
    status = preflight.get("status")
    if status == "normalization_pending":
        # A legitimate empty state (migration 025 not yet stamped) — 200 JSON
        # envelope, never a 200-SSE stream with nothing to say (§2.3).
        handler._respond_json(200, preflight)
        conn.close()
        return
    if status != "ok":
        # not_found (unresolvable ref or no rows) → 404 JSON (§2.3).
        handler._respond_json(404, preflight)
        conn.close()
        return
    source = preflight["source"]
    native = preflight["native_key"]

    # ok → commit SSE headers and enter the qualified watch loop.
    _send_sse_headers(handler)
    passive = bool(type(handler).no_sync)
    if source == "codex":
        cq_codex = _conversation_query_impl_codex()

        def _resolve():
            return cq_codex.codex_conversation_source_paths(conn, key)

        def _ingest(changed):
            return sync_codex_cache(conn, only_paths=set(changed))

        cached = lambda paths: _codex_cached_file_sigs(conn, paths)
        discovery = _make_codex_discovery_step(handler, conn, key, cq_codex)
    else:  # claude — reuse the Claude mechanics, speak qualified frames.
        cq = handler._conversation_query()

        def _resolve():
            return cq.session_source_paths(conn, native)

        def _ingest(changed):
            return sync_cache(conn, only_paths=set(changed))

        cached = lambda paths: _cached_file_sigs(conn, paths)
        discovery = None

    try:
        _run_conversation_events_stream(
            handler, conn, passive=passive,
            tail_data={"conversationKey": key},
            resolve=_resolve, ingest=_ingest, cached_sigs=cached,
            discovery_step=discovery)
    except (BrokenPipeError, ConnectionResetError,
            ConnectionAbortedError, socket.timeout):
        pass
    except Exception as exc:  # noqa: BLE001
        handler.log_error("api/conversation/events stream failed: %r", exc)
    finally:
        conn.close()


def _handle_get_conversation_events_impl(handler, path: str) -> None:
    """``GET /api/conversation/<id>/events`` — per-conversation live-tail SSE
    (spec §5.2). The transcript privacy gate is the first act, source-
    independent. A ``v1.`` key gets the neutral preflight + qualified
    (``conversationKey``) frames for BOTH providers; every other id keeps
    today's no-preflight, ``sessionId``-framed bare behavior byte-identical."""
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    key = _u.unquote(path[len("/api/conversation/"):-len("/events")])
    if not key:
        handler.send_error(404, "conversation not found")
        return
    if key.startswith("v1."):
        _qualified_conversation_events(handler, key)
    else:
        _bare_conversation_events(handler, key)


def _handle_get_conversation_search_impl(handler) -> None:
    """``GET /api/conversation/search?q=...&kind=...`` — cross-session
    FTS/LIKE search (spec §3.3). Matched BEFORE the ``<id>`` reader in
    ``do_GET``. ``kind`` (#177 S6) is validated to ``_CONV_SEARCH_KINDS``
    (else 400) before the kernel call.

    #217 S2 / Filtered-search: the browse filters (date/project/cost/rebuild)
    are parsed by the SAME ``_parse_conversation_filters`` the browse rail uses
    (malformed → 400 already sent) and threaded into the kernel, applied as a
    session-scope restriction across every kind. The 400s (bad kind, bad
    filter) are decided HERE, before the kernel call — ``_run_conversation_query``
    collapses kernel exceptions to a 500.
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    qs_raw = handler.path.partition("?")[2]
    parsed_src = _parse_source_param(handler, qs_raw)
    if parsed_src is None:
        return  # a 400 has already been sent
    qualified, source = parsed_src
    if qualified:
        return _handle_qualified_search(handler, qs_raw, source)
    q = _u.parse_qs(qs_raw)
    query = _qs_str(q, "q", "")
    limit = _qs_int(q, "limit", 50)
    offset = _qs_int(q, "offset", 0)
    kind = handler._parse_search_kind(q)
    if kind is None:
        return
    filters = handler._parse_conversation_filters(q)
    if filters is None:
        return  # a 400 has already been sent
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().search_conversations(
            conn, query, limit=limit, offset=offset, kind=kind, **filters),
        "/api/conversation/search")
    if not ok:
        return
    handler._respond_json(200, body)

def _handle_get_conversation_payload_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/payload?tool_use_id=<id>&which=<result|input>``
    — the #178 on-demand load-full route. Re-reads the source JSONL line so
    a clipped result/input can be expanded without enlarging the cache.

    Gated FIRST by the same loopback/Host transcript privacy predicate the
    three other conversation routes use (fail-closed 403). ``locate_tool_payload``
    runs against cache.db (via the shared 500-envelope scaffold); the actual
    full body is re-read from disk by ``read_full_payload`` (no cache conn).
    ``which`` is validated to ``result``/``input`` (else 400); a missing
    tool_use_id is 400; an unknown id is 404; a gone/unparseable source line
    is 410 (the documented consequence of storing only capped text).
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    session_id = _u.unquote(
        path[len("/api/conversation/"):-len("/payload")])
    q = _u.parse_qs(handler.path.partition("?")[2])
    if session_id.startswith("v1."):
        # Qualified payload readback (§3.4): Codex uses block_key + which={call,
        # output}; a v1.claude key keeps the tool_use_id + which={input,result}
        # selector. neutral_payload picks per the resolved source; gone → 410.
        which_q = _qs_str(q, "which", "")
        tool_use_id_q = _qs_str(q, "tool_use_id", "") or None
        block_key_q = _qs_str(q, "block_key", "") or None
        disp = _conversation_dispatch()
        _serve_qualified_entity(
            handler,
            lambda conn: disp.neutral_payload(
                conn, session_id, which=which_q,
                tool_use_id=tool_use_id_q, block_key=block_key_q),
            "/api/conversation/payload")
        return
    tool_use_id = _qs_str(q, "tool_use_id", "")
    which = _qs_str(q, "which", "result")
    if not session_id or which not in ("result", "input") or not tool_use_id:
        handler._respond_json(400, {"error": "bad request"})
        return
    cq = handler._conversation_query()
    ok, loc = handler._run_conversation_query(
        lambda conn: cq.locate_tool_payload(
            conn, session_id, tool_use_id, which),
        "/api/conversation/payload")
    if not ok:
        return
    if loc is None:
        handler._respond_json(404, {"error": "not found"})
        return
    payload = cq.read_full_payload(loc[0], loc[1], tool_use_id, which)
    if payload is None:
        handler._respond_json(410, {"error": "source no longer available"})
        return
    handler._respond_json(200, payload)

def _handle_get_conversation_outline_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/outline`` — full-session skeleton +
    session stats (#177 S5). Same fail-closed privacy gate; unknown id → 404.
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/outline")])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    if session_id.startswith("v1."):
        speed = _resolve_effective_speed()
        disp = _conversation_dispatch()
        _serve_qualified_entity(
            handler,
            lambda conn: disp.neutral_outline(
                conn, session_id, effective_speed=speed),
            "/api/conversation/outline")
        return
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().get_conversation_outline(conn, session_id),
        "/api/conversation/outline")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    handler._respond_json(200, body)

def _handle_get_conversation_prompts_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/prompts`` — ordered main-thread human
    prompts + full text (#217 S7 F10, the session-comparison spine). Same
    fail-closed transcript privacy gate as ``/outline`` —
    ``_require_transcripts_allowed()`` ONLY (no ``_check_origin_csrf``: the
    sibling transcript GETs gate on this predicate alone). Unknown id → 404.
    """
    if not handler._require_transcripts_allowed():
        return
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/prompts")])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    if session_id.startswith("v1."):
        disp = _conversation_dispatch()
        _serve_qualified_entity(
            handler,
            lambda conn: disp.neutral_prompts(conn, session_id),
            "/api/conversation/prompts")
        return
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().get_conversation_prompts(conn, session_id),
        "/api/conversation/prompts")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    handler._respond_json(200, body)

_CONV_EXPORT_SCOPES = ("all", "prompts", "chat", "recipe")

def _handle_get_conversation_export_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/export?scope=<all|prompts|chat|recipe>``
    — whole-session Markdown (issue #217 S5 F1/F5).

    Same fail-closed transcript privacy gate as ``/outline`` / ``/payload``
    / ``/find`` — ``_require_transcripts_allowed()`` ONLY. **No
    ``_check_origin_csrf``** (Codex P0-1): the sibling transcript GETs gate
    on this predicate alone; ``_check_origin_csrf`` rejects a missing
    ``Origin`` and would make export STRICTER than its sibling reader routes.

    ``scope`` is validated HERE, BEFORE the kernel (the
    ``_run_conversation_query``-collapses-kernel-exceptions-to-500 gotcha —
    an invalid scope is a clean 400, never a 500). Unknown session → 404.
    Emits ``text/markdown; charset=utf-8`` (the client builds the download
    Blob/filename, so no ``Content-Disposition`` is needed)."""
    if not handler._require_transcripts_allowed():
        return
    import os as _os
    import urllib.parse as _u
    qs = handler.path.partition("?")[2]
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/export")])
    q = _u.parse_qs(qs)
    scope = _qs_str(q, "scope", "all")
    if scope not in _CONV_EXPORT_SCOPES:
        handler._respond_json(400, {"error": f"unknown scope: {scope}"})
        return
    # #281 S4: `anonymize` gets its OWN strict parse (the default parse_qs /
    # _qs_str path silently drops blanks + first-picks duplicates). It must
    # appear at most once and be literal `0` or `1`; blank / duplicate / any
    # other spelling is a clean 400 HERE, BEFORE the kernel (the
    # _run_conversation_query-collapses-kernel-exceptions-to-500 gotcha). Absent
    # → raw (the R4 "raw export byte-unchanged" acceptance is structural).
    avals = _u.parse_qs(qs, keep_blank_values=True).get("anonymize")
    if avals is not None and (len(avals) != 1 or avals[0] not in ("0", "1")):
        handler._respond_json(400, {"error": f"invalid anonymize: {avals}"})
        return
    anonymize = avals is not None and avals[0] == "1"
    if not session_id:
        handler.send_error(404, "conversation not found")
        return

    if session_id.startswith("v1."):
        # Qualified export (§2.3 markdown leg / §3.6 provider-aware anon):
        # neutral_export serves the raw markdown member; ``anonymize=1`` scrubs
        # with the QUALIFIED provider-aware plan (byte-parity with the CLI export).
        speed = _resolve_effective_speed()
        disp = _conversation_dispatch()
        cref = disp.resolve_conversation_ref(session_id)

        def _q_kernel(conn):
            env = disp.neutral_export(
                conn, session_id, scope=scope, effective_speed=speed)
            if env.get("status") != "ok" or not anonymize:
                return env
            cq = handler._conversation_query()
            anon = sys.modules["cctally"]._load_sibling("_lib_conversation_anon")
            srcs = {cref.source} if cref else set()
            plan = cq.build_anon_plan_for_sources(
                conn, home_dir=_os.path.expanduser("~"), sources=srcs)
            return {**env, "markdown": anon.scrub_text(env["markdown"], plan)}

        ok, env = handler._run_conversation_query(
            _q_kernel, "/api/conversation/export")
        if not ok:
            return
        status = (env or {}).get("status")
        if status == "ok":
            data = env["markdown"].encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/markdown; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        if status == "validation_error":
            handler._respond_json(400, env)
            return
        if status == "normalization_pending":
            handler._respond_json(200, env)
            return
        handler._respond_json(404, env)  # not_found
        return

    def _kernel(conn):
        cq = handler._conversation_query()
        md = cq.get_conversation_export(conn, session_id, scope)
        if md is None or not anonymize:
            return md
        anon = sys.modules["cctally"]._load_sibling("_lib_conversation_anon")
        plan = cq.build_anon_plan_for_db(conn, home_dir=_os.path.expanduser("~"))
        return anon.scrub_text(md, plan)

    ok, body = handler._run_conversation_query(
        _kernel, "/api/conversation/export")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    data = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/markdown; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)

def _handle_get_conversation_anon_map_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/anon-map`` — the ``plan_to_wire`` JSON the
    client-side scrubber executes for per-card copy (#281 S4, spec §5).

    Same fail-closed transcript privacy gate as the sibling reader routes —
    ``_require_transcripts_allowed()`` ONLY (no ``_check_origin_csrf``). Although
    the plan is GLOBAL, the route probes existence (``conversation_exists``) and
    404s on an unknown ``<sid>``, matching sibling envelope discipline — never a
    token dump for a non-session. Contains only tokens the same gated client
    already sees raw, so no new information exposure. Matched BEFORE the bare
    ``<id>`` detail catch-all in ``do_GET`` (route-ordering test asserts the
    suffix does not fall through)."""
    if not handler._require_transcripts_allowed():
        return
    import os as _os
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/anon-map")])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    anon = sys.modules["cctally"]._load_sibling("_lib_conversation_anon")

    if session_id.startswith("v1."):
        # Qualified anon-map (§3.6): existence probe against the ref's OWN
        # provider tables, then the provider-aware plan (never the legacy builder).
        disp = _conversation_dispatch()
        cref = disp.resolve_conversation_ref(session_id)
        if cref is None:
            handler._respond_json(
                404, {"status": "not_found", "conversation_key": session_id})
            return

        def _q_kernel(conn):
            cq = handler._conversation_query()
            if cref.source == "codex":
                exists = _conversation_query_impl_codex().codex_conversation_exists(
                    conn, cref.conversation_key)
            else:
                exists = cq.conversation_exists(conn, cref.native_key)
            if not exists:
                return None
            plan = cq.build_anon_plan_for_sources(
                conn, home_dir=_os.path.expanduser("~"), sources={cref.source})
            return anon.plan_to_wire(plan)

        ok, body = handler._run_conversation_query(
            _q_kernel, "/api/conversation/anon-map")
        if not ok:
            return
        if body is None:
            handler._respond_json(
                404, {"status": "not_found", "conversation_key": session_id})
            return
        handler._respond_json(200, body)
        return

    def _kernel(conn):
        cq = handler._conversation_query()
        if not cq.conversation_exists(conn, session_id):
            return None
        plan = cq.build_anon_plan_for_db(conn, home_dir=_os.path.expanduser("~"))
        return anon.plan_to_wire(plan)

    ok, body = handler._run_conversation_query(
        _kernel, "/api/conversation/anon-map")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    handler._respond_json(200, body)

def _handle_get_conversation_find_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/find?q=...&kind=...`` — in-conversation
    find → document-ordered rendered-turn anchors (#177 S6). Same fail-closed
    privacy gate as the sibling routes; unknown id → 404; an invalid ``kind``
    → 400. Matched BEFORE the ``<id>`` reader catch-all in ``do_GET``.

    P1-1: validates against ``_CONV_FIND_KINDS`` (NOT the search set), so the
    cross-session-only ``kind=title``/``files`` return 400 here, never a 500.

    #217 S4 / I-1.2: ``regex``/``case`` are truthy params. An invalid regex
    is PRE-COMPILED here, BEFORE dispatching to the kernel — exactly as the
    detail route pre-validates ``after/before/tail`` — because
    ``_run_conversation_query`` collapses every kernel exception to a 500, so
    a ``re.error`` from the kernel's ``re.compile`` would otherwise leak as a
    500 instead of the actionable 400 the client maps to "invalid regex".
    """
    if not handler._require_transcripts_allowed():
        return
    import re as _re
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/find")])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    q = _u.parse_qs(handler.path.partition("?")[2])
    query = _qs_str(q, "q", "")
    kind = handler._parse_search_kind(q, valid=_CONV_FIND_KINDS)
    if kind is None:
        return
    regex = _qs_str(q, "regex", None) in ("1", "true", "yes")
    case = _qs_str(q, "case", None) in ("1", "true", "yes")
    # Pre-validate the regex HERE (Codex P1): the kernel compiles the same
    # pattern, but its ``re.error`` would be swallowed into the generic 500
    # envelope below. Compiling first turns a bad pattern into a clean 400.
    if regex:
        try:
            _re.compile(query, 0 if case else _re.IGNORECASE)
        except _re.error as e:
            handler._respond_json(400, {"error": f"invalid regex: {e}"})
            return
    if session_id.startswith("v1."):
        disp = _conversation_dispatch()
        _serve_qualified_entity(
            handler,
            lambda conn: disp.neutral_find(
                conn, session_id, query, kind=kind, regex=regex, case=case),
            "/api/conversation/find")
        return
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().find_in_conversation(
            conn, session_id, query, kind=kind, regex=regex, case=case),
        "/api/conversation/find")
    if not ok:
        return
    if body is None:
        handler.send_error(404, "conversation not found")
        return
    handler._respond_json(200, body)

_MEDIA_FETCH_SITE_ALLOWED = ("same-origin", "same-site", "none")

def _handle_get_conversation_media_impl(handler, path: str) -> None:
    """``GET /api/conversation/<sid>/media?tool_use_id=<id>&index=N`` or
    ``?uuid=<uuid>&index=N`` (#177 S4) — serves decoded image/PDF bytes by
    re-reading the source JSONL line (the #178 mechanism). Nothing is ever
    written to cache.db or disk; no outbound requests.

    Gated FIRST by the transcript privacy predicate (fail-closed 403),
    then by Fetch-Metadata: unlike the JSON routes, images embed
    cross-origin (an <img src> on any website the user visits passes the
    Host/loopback gate and leaks existence + dimensions via
    onload/naturalWidth), so a PRESENT Sec-Fetch-Site header must be
    same-origin/same-site/none; an absent header (curl, older browsers)
    is allowed — defense-in-depth, not the primary gate (Codex F1).
    Exactly one addressing key (tool_use_id XOR uuid) + a non-negative
    integer index, else 400. Content-Type is the kernel's allowlist
    constant; images get CSP default-src 'none'; PDFs get inline
    Content-Disposition instead (a CSP sandbox would break native PDF
    viewers)."""
    if not handler._require_transcripts_allowed():
        return
    sfs = (handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs and sfs not in _MEDIA_FETCH_SITE_ALLOWED:
        handler._respond_json(403, {"error": "cross-site media fetch not allowed"})
        return
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/media")])
    if session_id.startswith("v1."):
        # §3.5 / §2.3: Codex media is capability-gated (explicit 404, never a
        # silent zero-fill). A v1.claude key serves via its native Claude session
        # (the existing byte-serving mechanics); an unresolvable v1 → neutral 404.
        disp = _conversation_dispatch()
        cref = disp.resolve_conversation_ref(session_id)
        if cref is None:
            handler._respond_json(
                404, {"status": "not_found", "conversation_key": session_id})
            return
        if cref.source == "codex":
            handler._respond_json(
                404, {"status": "capability_unsupported", "source": "codex"})
            return
        session_id = cref.native_key
    q = _u.parse_qs(handler.path.partition("?")[2])
    tool_use_id = _qs_str(q, "tool_use_id", "")
    uuid = _qs_str(q, "uuid", "")
    index_raw = _qs_str(q, "index", "")
    if (not session_id or bool(tool_use_id) == bool(uuid)
            or not index_raw.isdigit()):
        handler._respond_json(400, {"error": "bad request"})
        return
    index = int(index_raw)
    key = ({"tool_use_id": tool_use_id} if tool_use_id else {"uuid": uuid})
    cq = handler._conversation_query()
    ok, loc = handler._run_conversation_query(
        lambda conn: cq.locate_media(conn, session_id, index=index, **key),
        "/api/conversation/media")
    if not ok:
        return
    if loc is None:
        handler._respond_json(404, {"error": "not found"})
        return
    # Defensive envelope parity with the sibling byte-serving handlers
    # (`_handle_get_doctor` / `_serve_static_file`): `locate_media` already
    # runs inside the `_run_conversation_query` 500-envelope, but the
    # `read_media_bytes` read + the byte emission did not. `read_media_bytes`
    # is internally defensive (OSError/ValueError → `gone`), so this guards
    # only an UNEXPECTED escape — but an unguarded one would kill the handler
    # thread with no logged 500. `response_started` tracks the commit point:
    # an exception BEFORE `send_response(200)` sends a clean logged 500; one
    # AFTER (mid-`wfile.write`, headers already out) can't re-send a status,
    # so it's logged only — never a silent thread death.
    response_started = False
    try:
        status, media_type, raw = cq.read_media_bytes(
            loc[0], loc[1], index=index, **key)
        if status == "unsupported":
            handler._respond_json(404, {"error": "not found"})
            return
        if status == "too_large":
            handler._respond_json(413, {"error": "media too large"})
            return
        if status != "ok":
            handler._respond_json(410, {"error": "source no longer available"})
            return
        handler.send_response(200)
        response_started = True
        handler.send_header("Content-Type", media_type)
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cache-Control", "private, max-age=86400")
        if media_type == "application/pdf":
            handler.send_header("Content-Disposition",
                             f'inline; filename="attachment-{index}.pdf"')
        else:
            handler.send_header("Content-Security-Policy", "default-src 'none'")
        handler.end_headers()
        handler.wfile.write(raw)
    except Exception as exc:  # noqa: BLE001
        handler.log_error("/api/conversation/media failed: %r", exc)
        if not response_started:
            handler._respond_json(
                500, {"error": f"{type(exc).__name__}: {exc}"})
