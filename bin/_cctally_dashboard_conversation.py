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
import socket
import sqlite3
import sys

from _cctally_cache import sync_cache

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


def _conversation_query_impl():
    """Lazy-load the pure conversation query kernel (Plan 2, §3)."""
    return sys.modules["cctally"]._load_sibling("_lib_conversation_query")

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
    q = _u.parse_qs(handler.path.partition("?")[2])
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
    conversation counts, for the browse filter's project multi-select (spec
    §2). Behind the SAME loopback/Host privacy gate as the list route; a
    cheap indexed GROUP BY over the rollup. The popover loads its options
    once from here (deriving from a paginated page would be incomplete).
    """
    if not handler._require_transcripts_allowed():
        return
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

def _handle_get_conversation_events_impl(handler, path: str) -> None:
    """``GET /api/conversation/<id>/events`` — per-conversation live-tail
    SSE (spec §2). Fail-closed behind the same transcript privacy gate as
    the other conversation routes. Watches only this session's file(s);
    emits ``event: tail`` on growth, ``: keep-alive`` when idle. Passive
    (no ingest, no emit) under ``--no-sync``."""
    if not handler._require_transcripts_allowed():
        return
    import time as _time
    import urllib.parse as _u
    watch = sys.modules["cctally"]._load_sibling("_lib_conversation_watch")
    cq = handler._conversation_query()
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/events")])
    if not session_id:
        handler.send_error(404, "conversation not found")
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

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
        if passive:
            # Frozen-data contract: no ingest, no emit. Keep-alive only.
            while True:
                _time.sleep(_LIVE_TAIL_KEEPALIVE)
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()

        # #278 Theme B: signal that this connection is ACTIVELY live-tailing
        # (not degraded to keep-alive). The client sets `live` only on this, so
        # passive/cache-open-failed streams fall back to the memo-backed global
        # tick instead of stranding updates (Codex F4). Passive branch above
        # emits only ': keep-alive', so 'ready' is unambiguous.
        handler.wfile.write(b"event: ready\ndata: {}\n\n")
        handler.wfile.flush()

        files = _resolve()
        # Best-effort connect ingest for immediacy, then baseline `seen`
        # from the cache's own offsets (session_files) so any pre-connect
        # growth the connect-ingest declined is still caught on cycle 1.
        try:
            if files:
                sync_cache(conn, only_paths=set(files))
        except sqlite3.DatabaseError:
            pass
        seen = _cached_file_sigs(conn, files)

        idle = 0.0
        cycles = 0
        while True:
            _time.sleep(_LIVE_TAIL_POLL_INTERVAL)
            cycles += 1
            changed = watch.changed_paths(files, seen)
            if changed:
                _time.sleep(_LIVE_TAIL_DEBOUNCE)
                new_seen, emitted = watch.watch_step(
                    files, seen, ingest_fn=_ingest,
                    committed_sig_fn=lambda p: _cached_file_sigs(conn, [p]).get(p))
                seen = new_seen
                if emitted:
                    handler.wfile.write(
                        ("event: tail\ndata: "
                         + json.dumps({"sessionId": session_id})
                         + "\n\n").encode("utf-8"))
                    handler.wfile.flush()
                    idle = 0.0
                    # §6 P2-H — a brand-new subagent file's FIRST content was
                    # just ingested by this emitting cycle, so the session's
                    # source-path set may have grown. Re-resolve it now (vs
                    # waiting up to _LIVE_TAIL_FILE_RESET_EVERY cycles) so the
                    # new thread (incl. a skill invoked inside it) live-tails
                    # promptly. A new path seeds seen=None (cur lacks a row),
                    # so changed_paths flags it next cycle → it ingests + emits.
                    # setdefault never disturbs an existing cursor.
                    new_files = _resolve()
                    if set(new_files) != set(files):
                        files = new_files
                        cur = _cached_file_sigs(conn, files)
                        for p in files:
                            seen.setdefault(p, cur.get(p))
                    continue
            idle += _LIVE_TAIL_POLL_INTERVAL
            if idle >= _LIVE_TAIL_KEEPALIVE:
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                idle = 0.0
            if cycles % _LIVE_TAIL_FILE_RESET_EVERY == 0:
                files = _resolve()
                seen = {p: s for p, s in seen.items() if p in set(files)}
    except (BrokenPipeError, ConnectionResetError,
            ConnectionAbortedError, socket.timeout):
        # #279 S1 F3: a stalled send past the handler timeout raises
        # socket.timeout inside the SSE loop — treat it as a client
        # disconnect (same as the other peer-gone classes), not an error.
        pass            # client disconnect is normal
    except Exception as exc:  # noqa: BLE001
        # #279 S5 F6.2 (spec §8): a genuine bug mid-stream used to kill the
        # live-tail SSE silently via handle_error. Headers are already
        # committed — route the operator signal through the _lib_log chokepoint
        # (handler.log_error) + a deliberate clean close via the finally below.
        handler.log_error("api/conversation/events stream failed: %r", exc)
    finally:
        if conn is not None:
            conn.close()

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
    q = _u.parse_qs(handler.path.partition("?")[2])
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
    import urllib.parse as _u
    session_id = _u.unquote(path[len("/api/conversation/"):-len("/export")])
    q = _u.parse_qs(handler.path.partition("?")[2])
    scope = _qs_str(q, "scope", "all")
    if scope not in _CONV_EXPORT_SCOPES:
        handler._respond_json(400, {"error": f"unknown scope: {scope}"})
        return
    if not session_id:
        handler.send_error(404, "conversation not found")
        return
    ok, body = handler._run_conversation_query(
        lambda conn: handler._conversation_query().get_conversation_export(
            conn, session_id, scope),
        "/api/conversation/export")
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
