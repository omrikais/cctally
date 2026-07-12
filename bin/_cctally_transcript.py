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

def _cmd_transcript_export(args) -> int:
    c = _cctally()
    session_id = args.session_id
    scope = getattr(args, "scope", "all")
    raw = bool(getattr(args, "raw", False))
    output = getattr(args, "output", None)

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

    data = md.encode("utf-8")
    if output:
        pathlib.Path(output).write_bytes(data)
    else:
        # Byte-exact: raw bytes to stdout, no `print`, no added trailing newline.
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    return 0


# ---- search ----------------------------------------------------------------

def _cmd_transcript_search(args) -> int:
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
