"""cctally transcript — anonymized-by-default export + raw cross-session search
over the conversation cache (#281 S4 R4).

Thin CLI wrappers over the SAME kernels the dashboard HTTP routes use
(``get_conversation_export`` / ``search_conversations`` / ``build_anon_plan_for_db``
/ ``scrub_text``), so ``transcript export`` byte-matches ``GET …/export`` and the
viewer becomes scriptable.

- ``export``: anonymized Markdown by default; ``--raw`` disables the whole scrub
  (identity + secrets) and is byte-identical to the dashboard's raw export.
  Byte-exact emission: ``sys.stdout.buffer.write`` of the exact UTF-8 bytes (no
  ``print``, no added trailing newline — the render already ends in exactly one),
  or ``--output PATH`` writes the same exact bytes; nothing else on stdout.
  Unknown session → ``transcript: …`` on stderr, exit 1.
- ``search``: RAW output (a navigation surface, not a sharing artifact). Mirrors
  the full HTTP filter surface; date-only values parse through the SAME
  display-tz-aware helper the HTTP handler uses. Human table by default; ``--json``
  emits a ``schemaVersion``-stamped, explicitly camelCased envelope.

Accessor discipline: no ``_cctally_*`` sibling is imported directly; every helper
is reached via the call-time ``_cctally()`` accessor / ``_load_sibling`` (matching
the other command modules), except the pure ``_lib_fmt._boxed_table`` renderer.
"""
from __future__ import annotations

import os
import pathlib
import sys

from _cctally_core import eprint
from _lib_fmt import _boxed_table


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    return sys.modules["cctally"]


def cmd_transcript(args) -> int:
    action = getattr(args, "transcript_action", None)
    if action == "export":
        return _cmd_transcript_export(args)
    if action == "search":
        return _cmd_transcript_search(args)
    eprint("transcript: expected a subcommand (`export` or `search`)")
    return 2


# ---- export ----------------------------------------------------------------

_SPEED_ONLY_CODEX_MSG = "transcript: --speed applies only to Codex conversations"
_PENDING_EXPORT_MSG = (
    "transcript: Codex conversation is not yet normalized "
    "(migration 025 runs on the next cache open) — retry shortly")


def _emit_export(md: str, output) -> None:
    """Byte-exact emission shared by the bare and qualified export paths: the exact
    UTF-8 bytes to ``--output`` or to stdout (no ``print``, no added trailing
    newline — the render already ends in exactly one), so the CLI byte-matches
    ``GET /api/conversation/<id>/export``."""
    data = md.encode("utf-8")
    if output:
        pathlib.Path(output).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _cmd_transcript_export(args) -> int:
    c = _cctally()
    session_id = args.session_id
    scope = getattr(args, "scope", "all")
    raw = bool(getattr(args, "raw", False))
    output = getattr(args, "output", None)
    speed_arg = getattr(args, "speed", None)   # None sentinel = flag omitted

    if session_id.startswith("v1."):
        return _cmd_transcript_export_qualified(
            c, session_id, scope, raw, output, speed_arg)

    # Legacy bare Claude path — byte-untouched. --speed is Codex pricing behavior,
    # so an explicit value on any non-Codex ref is a usage error (resolved-source
    # rule, §4.1) — never a silent no-op.
    if speed_arg is not None:
        eprint(_SPEED_ONLY_CODEX_MSG)
        return 2

    conn = c.open_cache_db()
    try:
        cq = c._load_sibling("_lib_conversation_query")
        md = cq.get_conversation_export(conn, session_id, scope)
        if md is None:
            eprint(f"transcript: no conversation found for session {session_id!r}")
            return 1
        if not raw:
            anon = c._load_sibling("_lib_conversation_anon")
            plan = cq.build_anon_plan_for_db(conn, home_dir=os.path.expanduser("~"))
            md = anon.scrub_text(md, plan)
    finally:
        conn.close()

    _emit_export(md, output)
    return 0


def _cmd_transcript_export_qualified(
        c, session_id, scope, raw, output, speed_arg) -> int:
    """Qualified (``v1.``) export via the neutral dispatch layer (§4.1). Anonymized
    by default with the QUALIFIED provider-aware plan (§3.6); ``--raw`` escapes.
    Byte-matches ``GET /api/conversation/<v1key>/export`` in both modes."""
    disp = c._load_sibling("_lib_conversation_dispatch")
    cref = disp.resolve_conversation_ref(session_id)
    # Resolved-source --speed rejection (§4.1): explicit --speed (any value,
    # including auto) is a usage error unless the ref resolves to source == codex.
    if speed_arg is not None and (cref is None or cref.source != "codex"):
        eprint(_SPEED_ONLY_CODEX_MSG)
        return 2
    speed = c._resolve_codex_speed(speed_arg or "auto")

    conn = c.open_cache_db()
    try:
        env = disp.neutral_export(
            conn, session_id, scope=scope, effective_speed=speed)
        status = env.get("status")
        if status == "normalization_pending":
            eprint(_PENDING_EXPORT_MSG)
            return 1
        if status == "validation_error":
            eprint(f"transcript: scope {scope!r} is not supported for a Codex "
                   f"conversation (only the whole-conversation export)")
            return 2
        if status != "ok":
            eprint(f"transcript: no conversation found for key {session_id!r}")
            return 1
        md = env["markdown"]
        if not raw:
            cq = c._load_sibling("_lib_conversation_query")
            anon = c._load_sibling("_lib_conversation_anon")
            plan = cq.build_anon_plan_for_sources(
                conn, home_dir=os.path.expanduser("~"),
                sources={cref.source})
            md = anon.scrub_text(md, plan)
    finally:
        conn.close()

    _emit_export(md, output)
    return 0


# ---- search ----------------------------------------------------------------

def _cmd_transcript_search(args) -> int:
    if getattr(args, "source", "claude") == "codex":
        return _cmd_transcript_search_codex(args)
    # Legacy Claude path (byte-frozen). --cursor is Codex-only pagination — using
    # it with --source claude is a usage error, never a silent no-op (§4.3).
    if getattr(args, "cursor", None) is not None:
        eprint("transcript: --cursor requires --source codex")
        return 2
    return _cmd_transcript_search_claude(args)


def _cmd_transcript_search_claude(args) -> int:
    c = _cctally()
    query = args.query
    kind = getattr(args, "kind", "all")
    limit = getattr(args, "limit", 50)
    offset = getattr(args, "offset", 0)
    projects = getattr(args, "project", None) or None
    models = getattr(args, "model", None) or None
    cost_min = getattr(args, "cost_min", None)
    cost_max = getattr(args, "cost_max", None)
    rebuild_min = getattr(args, "rebuild_min", None)

    # Date-only bounds parse through the SAME display-tz-aware helper the HTTP
    # filter handler uses (no second parser) — reuse, don't reimplement.
    date_from_in = getattr(args, "date_from", None)
    date_to_in = getattr(args, "date_to", None)
    df = dtt = None
    if date_from_in or date_to_in:
        tz = c.resolve_display_tz(args, c.load_config())
        tz_name = tz.key if tz is not None else None
        dates = c._load_sibling("_lib_dashboard_dates")
        try:
            df, dtt = dates.parse_filter_date_range(
                date_from_in, date_to_in, tz_name=tz_name)
        except ValueError as exc:
            eprint(f"transcript: {exc}")
            return 2

    conn = c.open_cache_db()
    try:
        cq = c._load_sibling("_lib_conversation_query")
        try:
            result = cq.search_conversations(
                conn, query, limit=limit, offset=offset, kind=kind,
                date_from=df, date_to=dtt, projects=projects, models=models,
                cost_min=cost_min, cost_max=cost_max, rebuild_min=rebuild_min)
        except ValueError as exc:            # unknown kind (belt-and-suspenders)
            eprint(f"transcript: {exc}")
            return 2
    finally:
        conn.close()

    if getattr(args, "json", False):
        payload = c.stamp_schema_version(_search_to_camel(result))
        print(_json_dumps(payload))
        return 0
    _render_search_table(result)
    return 0


# ---- search (Codex) --------------------------------------------------------

_CODEX_SEARCH_PENDING_MSG = (
    "transcript: Codex conversations are not yet normalized "
    "(migration 025 runs on the next cache open); no results yet")


def _cmd_transcript_search_codex(args) -> int:
    """``transcript search --source codex`` (§4.3). The Codex search kernel has no
    filter axes and paginates by opaque cursor, so ``--offset`` and the Claude-only
    filter flags are usage errors, and ``--cursor`` carries the external cursor."""
    c = _cctally()
    query = args.query
    kind = getattr(args, "kind", "all")
    limit = getattr(args, "limit", 50)
    cursor = getattr(args, "cursor", None)
    as_json = bool(getattr(args, "json", False))

    # Pagination + filter axes the Codex kernel does not have → exit 2 (silently
    # ignoring a filter would fabricate results).
    if getattr(args, "offset", 0):
        eprint("transcript: --offset is not supported with --source codex "
               "(use --cursor)")
        return 2
    rejected = []
    if getattr(args, "project", None):
        rejected.append("--project")
    if getattr(args, "model", None):
        rejected.append("--model")
    if getattr(args, "date_from", None):
        rejected.append("--date-from")
    if getattr(args, "date_to", None):
        rejected.append("--date-to")
    if getattr(args, "cost_min", None) is not None:
        rejected.append("--cost-min")
    if getattr(args, "cost_max", None) is not None:
        rejected.append("--cost-max")
    if getattr(args, "rebuild_min", None) is not None:
        rejected.append("--rebuild-min")
    if rejected:
        eprint(f"transcript: {', '.join(rejected)} not supported with "
               f"--source codex")
        return 2

    disp = c._load_sibling("_lib_conversation_dispatch")
    # Validate the external cursor up front → exit 2 on a bad token.
    if cursor is not None:
        try:
            disp.decode_search_cursor(cursor)
        except disp.InvalidSearchCursor:
            eprint("transcript: invalid --cursor")
            return 2

    conn = c.open_cache_db()
    try:
        result = disp.neutral_search(
            conn, query, source="codex", kind=kind,
            effective_speed=c._resolve_codex_speed("auto"),
            limit=limit, cursor=cursor)
    finally:
        conn.close()

    # normalization_pending: an empty, exit-0 answer with one stderr note — search
    # is navigation, and "nothing yet" is truthful (§4.3).
    if result.get("status") == "normalization_pending":
        eprint(_CODEX_SEARCH_PENDING_MSG)

    if as_json:
        payload = c.stamp_schema_version(_codex_search_to_camel(result, query))
        print(_json_dumps(payload))
        return 0
    _render_codex_search_table(args, result)
    return 0


def _codex_search_to_camel(result: dict, query: str) -> dict:
    """The pinned, stamped-first camelCase Codex search envelope (§4.3):
    ``{schemaVersion, source, query, mode, total, hits[...], nextCursor}``. The
    leading ``schemaVersion`` is inserted by ``stamp_schema_version``."""
    hits = [
        {
            "conversationKey": h.get("conversation_key"),
            "itemKey": h.get("item_key"),
            "title": h.get("title"),
            "snippet": h.get("snippet"),
            "badges": h.get("badges", []),
            "lastActivityUtc": h.get("last_activity_utc"),
            "projectLabel": h.get("project_label"),
        }
        for h in result.get("hits", [])
    ]
    return {
        "source": "codex",
        "query": result.get("query", query),
        "mode": result.get("mode"),
        "total": result.get("total", 0),
        "hits": hits,
        "nextCursor": (result.get("page") or {}).get("cursor"),
    }


def _render_codex_search_table(args, result: dict) -> None:
    """Codex human table: Key / When / Project / Kinds / Snippet (§4.3). ``Key`` is
    the full untruncated ``v1.`` conversation key (so search → export pipes);
    ``When`` renders ``last_activity_utc`` through the display-tz chokepoint;
    ``Project`` is ``—`` when null; ``Kinds`` are the hit badges."""
    c = _cctally()
    hits = result.get("hits", [])
    if not hits:
        print("No matching transcripts.")
        return
    tz = c.resolve_display_tz(args, c.load_config())
    rows = []
    for h in hits:
        last = h.get("last_activity_utc")
        when = c.format_display_dt(
            last, tz, fmt="%Y-%m-%d %H:%M", suffix=True) if last else ""
        kinds = ",".join(h.get("badges") or []) or "-"
        rows.append([
            h.get("conversation_key") or "",     # full, untruncated key
            when,
            h.get("project_label") or "—",
            kinds,
            (h.get("snippet") or "").strip(),
        ])
    table = _boxed_table(
        ["Key", "When", "Project", "Kinds", "Snippet"], rows,
        ["left", "left", "left", "left", "left"])
    print(table)
    total = result.get("total", len(hits))
    print(f"\n{len(hits)} of {total} match(es)")
    next_cursor = (result.get("page") or {}).get("cursor")
    if next_cursor:
        print(f"next: --cursor {next_cursor}")


def _json_dumps(payload) -> str:
    import json
    return json.dumps(payload, indent=2)


def _search_to_camel(result: dict) -> dict:
    """Explicit recursive camelCase mapping of the ``search_conversations`` result
    (this command's own serializer — ``stamp_schema_version`` only inserts the
    leading key). Top level then hit level, per spec §7."""
    out = {
        "query": result.get("query", ""),
        "mode": result.get("mode"),
        "hits": [_hit_to_camel(h) for h in result.get("hits", [])],
        "total": result.get("total", 0),
        "kind": result.get("kind"),
        "searchDepth": result.get("search_depth"),
    }
    if result.get("filter_degraded"):
        out["filterDegraded"] = True
    return out


def _hit_to_camel(h: dict) -> dict:
    return {
        "sessionId": h.get("session_id"),
        "uuid": h.get("uuid"),
        "projectLabel": h.get("project_label"),
        "title": h.get("title"),
        "ts": h.get("ts"),
        "snippet": h.get("snippet"),
        "matchKinds": h.get("match_kinds", []),
        "costUsd": h.get("cost_usd", 0.0),
    }


def _render_search_table(result: dict) -> None:
    hits = result.get("hits", [])
    if not hits:
        print("No matching transcripts.")
        return
    rows = []
    for h in hits:
        kinds = ",".join(h.get("match_kinds") or []) or "-"
        rows.append([
            h.get("session_id") or "",
            h.get("ts") or "",
            h.get("project_label") or "",
            kinds,
            (h.get("snippet") or "").strip(),
        ])
    table = _boxed_table(
        ["Session", "When", "Project", "Kinds", "Snippet"], rows,
        ["left", "left", "left", "left", "left"])
    print(table)
    total = result.get("total", len(hits))
    depth = result.get("search_depth")
    suffix = f"  (search depth: {depth})" if depth and depth != "full" else ""
    print(f"\n{len(hits)} of {total} match(es){suffix}")
