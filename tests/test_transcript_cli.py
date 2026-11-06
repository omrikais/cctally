"""`cctally transcript export|search` CLI (#281 S4 Task 3).

Direct-call precedent (``tests/test_cache_sync_cli.py``): ``ns["cmd_transcript"]``
with an ``argparse.Namespace`` under ``redirect_paths``. ``capsysbinary``
captures the byte-exact export emission. The CLI↔HTTP byte-parity test boots the
real endpoint server (reusing ``test_conversation_endpoints._boot``) over the
SAME fixture cache.db and compares CLI stdout bytes to the endpoint body bytes.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

import pytest
from conftest import load_script, redirect_paths
from test_conversation_endpoints import _boot, _get_ct

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CODEX_CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


# ---- namespace builders ----------------------------------------------------

def _ns_export(session_id, *, scope="all", raw=False, output=None, speed=None):
    return argparse.Namespace(
        transcript_action="export", session_id=session_id, scope=scope,
        raw=raw, output=output, speed=speed)


def _ns_search(query, **kw):
    base = dict(
        transcript_action="search", query=query, source="claude", kind="all",
        limit=50, offset=0, cursor=None,
        project=None, model=None, date_from=None, date_to=None,
        cost_min=None, cost_max=None, rebuild_min=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _seed_codex(ns, tmp_path, monkeypatch, *, scenario="modern-full",
                transform=None):
    """Ingest one Codex corpus scenario into the redirected cache and return
    ``(conversation_key, rollout_path)``. ``redirect_paths`` must have already run
    (or is run here when ``needs_redirect`` — see callers). ``transform`` maps
    the rollout text before it is written (a bracket-bearing variant)."""
    provider = tmp_path / "provider"
    rollout = provider / "sessions" / "2026" / "07" / "15" / f"{scenario}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    if transform is None:
        shutil.copyfile(CODEX_CORPUS / f"{scenario}.jsonl", rollout)
    else:
        rollout.write_text(
            transform((CODEX_CORPUS / f"{scenario}.jsonl").read_text("utf-8")),
            "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(provider))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        key = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE source_path LIKE ?", (f"%/{scenario}.jsonl",)).fetchone()[0]
    finally:
        conn.close()
    conversations = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conversations, rebuild=True)
    finally:
        conversations.close()
    return key, rollout


def _recompute_rollup(ns):
    """Populate the conversation_sessions rollup (which the project/date search
    filters resolve against) from the direct-inserted messages, then COMMIT so a
    fresh conn in cmd_transcript sees it."""
    conn = ns["open_cache_db"]()
    ns["_load_sibling"]("_cctally_cache")._recompute_conversation_sessions(conn)
    conn.commit()
    conn.close()


def _seed(ns, *, session_id="sx", cwd="/home/u/proj",
          text="edited /home/u/proj/secret.py here",
          ts="2026-06-01T00:00:00Z", entry_type="human"):
    conn = ns["open_cache_db"]()
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, session_id + "-u1", None, session_id + ".jsonl", 0, ts,
         entry_type, text, "[]", None, None, None, cwd, "main", 0))
    conn.commit()
    conn.close()


# ---- export ----------------------------------------------------------------

def test_export_default_anonymizes(tmp_path, monkeypatch, capsysbinary):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sx", text="edited /home/u/proj/secret.py here")
    rc = ns["cmd_transcript"](_ns_export("sx", scope="all"))
    assert rc == 0
    out = capsysbinary.readouterr().out
    assert b"/home/u/proj" not in out          # anonymized by default
    assert out.endswith(b"\n")                  # render ends in exactly one \n
    assert not out.endswith(b"\n\n")


def test_export_raw_matches_kernel_bytes(tmp_path, monkeypatch, capsysbinary):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sx", text="edited /home/u/proj/secret.py here")
    conn = ns["open_cache_db"]()
    cq = ns["_load_sibling"]("_lib_conversation_query")
    md = cq.get_conversation_export(conn, "sx", "all")
    conn.close()
    rc = ns["cmd_transcript"](_ns_export("sx", scope="all", raw=True))
    assert rc == 0
    out = capsysbinary.readouterr().out
    assert out == md.encode("utf-8")            # byte-exact, no added newline
    assert b"/home/u/proj" in out               # raw retains the identity token


def test_export_output_file_matches_stdout(tmp_path, monkeypatch, capsysbinary):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sx")
    ns["cmd_transcript"](_ns_export("sx", scope="all", raw=True))
    stdout_bytes = capsysbinary.readouterr().out
    outfile = tmp_path / "t.md"
    rc = ns["cmd_transcript"](_ns_export("sx", scope="all", raw=True,
                                         output=str(outfile)))
    assert rc == 0
    assert capsysbinary.readouterr().out == b""     # nothing on stdout
    assert outfile.read_bytes() == stdout_bytes


def test_export_unknown_session_exit_1(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rc = ns["cmd_transcript"](_ns_export("nope"))
    assert rc == 1
    assert "transcript:" in capsys.readouterr().err


def test_export_bad_scope_exits_2(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        ns["build_parser"]().parse_args(
            ["transcript", "export", "sx", "--scope", "bogus"])
    assert e.value.code == 2


def test_cli_http_byte_parity_all_scopes(tmp_path, monkeypatch, capsysbinary):
    """spec §8.3: CLI default bytes == ?anonymize=1 body; CLI --raw == no-param
    body — all four scopes, over the same fixture DB through the endpoint server."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)      # seeds s1/s2 + starts the server
    try:
        port = srv.server_address[1]
        for scope in ("all", "prompts", "chat", "recipe"):
            _, _, http_raw = _get_ct(
                port, f"/api/conversation/s1/export?scope={scope}")
            _, _, http_anon = _get_ct(
                port, f"/api/conversation/s1/export?scope={scope}&anonymize=1")
            capsysbinary.readouterr()
            assert ns["cmd_transcript"](_ns_export("s1", scope=scope, raw=True)) == 0
            assert capsysbinary.readouterr().out == http_raw, ("raw", scope)
            assert ns["cmd_transcript"](_ns_export("s1", scope=scope)) == 0
            assert capsysbinary.readouterr().out == http_anon, ("anon", scope)
    finally:
        srv.shutdown()


# ---- search ----------------------------------------------------------------

def test_search_table_lists_hit(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="shit", cwd="/home/u/proj",
          text="the special needle marker here", entry_type="assistant")
    rc = ns["cmd_transcript"](_ns_search("needle"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "shit" in out or "needle" in out.lower()


def test_search_json_envelope_camelcase(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sj", cwd="/home/u/proj",
          text="a rare needle token here", entry_type="assistant")
    rc = ns["cmd_transcript"](_ns_search("needle", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert list(payload)[0] == "schemaVersion" and payload["schemaVersion"] == 1
    assert "searchDepth" in payload and "hits" in payload and "total" in payload
    assert payload["hits"], "expected a hit"
    h = payload["hits"][0]
    assert h["sessionId"] == "sj"
    assert set(h) >= {"sessionId", "matchKinds", "projectLabel", "costUsd", "snippet"}


def test_search_zero_hits_exit_0(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rc = ns["cmd_transcript"](_ns_search("zzznomatchxyz", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"] == [] and payload["total"] == 0


def test_search_kinds_parse_and_bad_kind_exits_2(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    for k in ("all", "prompts", "assistant", "tools", "thinking", "title", "files"):
        ns["build_parser"]().parse_args(["transcript", "search", "q", "--kind", k])
    with pytest.raises(SystemExit) as e:
        ns["build_parser"]().parse_args(
            ["transcript", "search", "q", "--kind", "bogus"])
    assert e.value.code == 2


def test_search_all_filter_flags_parse(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    args = ns["build_parser"]().parse_args([
        "transcript", "search", "q",
        "--project", "a", "--project", "b", "--model", "opus",
        "--date-from", "2026-06-01", "--date-to", "2026-06-30",
        "--cost-min", "0.1", "--cost-max", "9", "--rebuild-min", "2",
        "--limit", "5", "--offset", "1", "--json"])
    assert args.project == ["a", "b"] and args.model == ["opus"]
    assert args.date_from == "2026-06-01" and args.rebuild_min == 2 and args.json


def test_search_date_filter_narrows(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sd1", text="june needle", ts="2026-06-10T00:00:00Z",
          entry_type="assistant")
    _seed(ns, session_id="sd2", text="july needle", ts="2026-07-10T00:00:00Z",
          entry_type="assistant")
    _recompute_rollup(ns)   # the date axis resolves against the rollup
    rc = ns["cmd_transcript"](_ns_search(
        "needle", json=True, date_from="2026-06-01", date_to="2026-06-30"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    sids = {h["sessionId"] for h in payload["hits"]}
    assert "sd1" in sids and "sd2" not in sids


def test_search_project_filter_narrows(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="pa", cwd="/home/u/alpha", text="shared needle one",
          entry_type="assistant")
    _seed(ns, session_id="pb", cwd="/home/u/beta", text="shared needle two",
          entry_type="assistant")
    _recompute_rollup(ns)   # project filter compares the stored rollup column
    rc = ns["cmd_transcript"](_ns_search("needle", json=True, project=["alpha"]))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    sids = {h["sessionId"] for h in payload["hits"]}
    assert "pa" in sids and "pb" not in sids


def test_search_pagination(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    for i in range(3):
        _seed(ns, session_id=f"pg{i}", text=f"paginate needle {i}",
              entry_type="assistant")
    rc = ns["cmd_transcript"](_ns_search("needle", json=True, limit=2, offset=0))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 3 and len(payload["hits"]) == 2


# ---- #1036 — `transcript search` output is byte-frozen (operator decision O2) --
#
# The viewer's search hits gained a structured `snippet_parts` list and the FTS
# extraction moved to collision-checked markers. The CLI must not change at all:
# a drifted legacy `snippet` (a bracket lost or doubled, an ellipsis moved) or a
# leaked `snippet_parts` key would silently alter what scripts parse. The
# expected bytes below were captured from the tree BEFORE the #1036 change
# (base cfca3d438) over excerpts carrying literal brackets, an adjacent match,
# a fragment cut at both edges, a bracketed title and a bracketed Codex message.

_BASELINE_LONG = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
    "omicron [pi] needle rho sigma tau upsilon phi chi psi omega one two three "
    "four five six seven eight nine ten")


def _seed_search_baseline(ns):
    """Claude messages + one AI title with bracket-bearing text for the frozen
    `transcript search` baseline."""
    _seed(ns, session_id="bk1", cwd="/home/u/proj",
          text="[WARN] needle arr[0] [x] needle needle done",
          ts="2026-06-01T00:00:00Z", entry_type="assistant")
    _seed(ns, session_id="bk2", cwd="/home/u/other", text=_BASELINE_LONG,
          ts="2026-06-02T00:00:00Z", entry_type="assistant")
    conn = ns["open_cache_db"]()
    try:
        conn.execute(ns["_load_sibling"]("_cctally_cache")._AI_TITLE_UPSERT_SQL,
                     ("bk1", "Review [P1] findings for the tail poll",
                      "bk1.jsonl", 0))
        conn.commit()
    finally:
        conn.close()


def _force_like_mode(ns):
    """Set the `fts5_unavailable` flag on every store the search connection can
    resolve, so the Claude CLI search takes its LIKE fallback."""
    for opener in ("open_cache_db", "open_conversations_db"):
        conn = ns[opener]()
        try:
            try:
                conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                             "VALUES('fts5_unavailable', '1')")
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()


def _bracket_codex_rollout(text):
    return text.replace("Synthetic assistant response",
                        "Synthetic [WARN] assistant arr[0] [x] response")


def _capture_search_outputs(ns, tmp_path, monkeypatch, capsys):
    """{case: stdout} for Claude FTS / title / LIKE and Codex search, table and
    --json. The Codex conversation and item keys and the scratch path are
    replaced by stable tokens (per-run values that #1036 does not touch)."""
    out = {}

    def run(name, query, **kw):
        assert ns["cmd_transcript"](_ns_search(query, **kw)) == 0
        out[name] = capsys.readouterr().out

    _seed_search_baseline(ns)
    capsys.readouterr()
    for kind, query in (("all", "needle"), ("title", "findings")):
        run(f"claude-{kind}-table", query, kind=kind)
        run(f"claude-{kind}-json", query, kind=kind, json=True)
    key, _rollout = _seed_codex(ns, tmp_path, monkeypatch,
                                transform=_bracket_codex_rollout)
    capsys.readouterr()
    run("codex-table", "assistant", source="codex", tz="utc")
    run("codex-json", "assistant", source="codex", json=True, tz="utc")
    _force_like_mode(ns)
    capsys.readouterr()
    run("claude-like-table", "needle")
    run("claude-like-json", "needle", json=True)
    tokens = {key: "<KEY>", str(tmp_path): "<TMP>"}
    for hit in json.loads(out["codex-json"])["hits"]:
        tokens[hit["itemKey"]] = "<ITEM>"
    for name, text in out.items():
        for value, token in tokens.items():
            text = text.replace(value, token)
        out[name] = text
    return out


_SEARCH_BASELINE: dict = {
    'claude-all-table': (
        '┌─────────┬──────────────────────┬─────────┬───────┬──────────────────────────────────────────────────────────────────┐\n'
        '│ Session │ When                 │ Project │ Kinds │ Snippet                                                          │\n'
        '├─────────┼──────────────────────┼─────────┼───────┼──────────────────────────────────────────────────────────────────┤\n'
        '│ bk1     │ 2026-06-01T00:00:00Z │ proj    │ -     │ [WARN] [needle] arr[0] [x] [needle] [needle] done                │\n'
        '├─────────┼──────────────────────┼─────────┼───────┼──────────────────────────────────────────────────────────────────┤\n'
        '│ bk2     │ 2026-06-02T00:00:00Z │ other   │ -     │ … mu nu xi omicron [pi] [needle] rho sigma tau upsilon phi chi … │\n'
        '└─────────┴──────────────────────┴─────────┴───────┴──────────────────────────────────────────────────────────────────┘\n'
        '\n'
        '2 of 2 match(es)\n'
    ),
    'claude-all-json': (
        '{\n'
        '  "schemaVersion": 1,\n'
        '  "query": "needle",\n'
        '  "mode": "fts",\n'
        '  "hits": [\n'
        '    {\n'
        '      "sessionId": "bk1",\n'
        '      "uuid": "bk1-u1",\n'
        '      "projectLabel": "proj",\n'
        '      "title": "Review [P1] findings for the tail poll",\n'
        '      "ts": "2026-06-01T00:00:00Z",\n'
        '      "snippet": "[WARN] [needle] arr[0] [x] [needle] [needle] done",\n'
        '      "matchKinds": [],\n'
        '      "costUsd": 0.0\n'
        '    },\n'
        '    {\n'
        '      "sessionId": "bk2",\n'
        '      "uuid": "bk2-u1",\n'
        '      "projectLabel": "other",\n'
        '      "title": "other",\n'
        '      "ts": "2026-06-02T00:00:00Z",\n'
        '      "snippet": " \\u2026 mu nu xi omicron [pi] [needle] rho sigma tau upsilon phi chi \\u2026 ",\n'
        '      "matchKinds": [],\n'
        '      "costUsd": 0.0\n'
        '    }\n'
        '  ],\n'
        '  "total": 2,\n'
        '  "kind": "all",\n'
        '  "searchDepth": "full"\n'
        '}\n'
    ),
    'claude-title-table': (
        '┌─────────┬──────────────────────┬─────────┬───────┬──────────────────────────────────────────┐\n'
        '│ Session │ When                 │ Project │ Kinds │ Snippet                                  │\n'
        '├─────────┼──────────────────────┼─────────┼───────┼──────────────────────────────────────────┤\n'
        '│ bk1     │ 2026-06-01T00:00:00Z │ proj    │ title │ Review [P1] [findings] for the tail poll │\n'
        '└─────────┴──────────────────────┴─────────┴───────┴──────────────────────────────────────────┘\n'
        '\n'
        '1 of 1 match(es)\n'
    ),
    'claude-title-json': (
        '{\n'
        '  "schemaVersion": 1,\n'
        '  "query": "findings",\n'
        '  "mode": "fts",\n'
        '  "hits": [\n'
        '    {\n'
        '      "sessionId": "bk1",\n'
        '      "uuid": "bk1-u1",\n'
        '      "projectLabel": "proj",\n'
        '      "title": "Review [P1] findings for the tail poll",\n'
        '      "ts": "2026-06-01T00:00:00Z",\n'
        '      "snippet": "Review [P1] [findings] for the tail poll",\n'
        '      "matchKinds": [\n'
        '        "title"\n'
        '      ],\n'
        '      "costUsd": 0.0\n'
        '    }\n'
        '  ],\n'
        '  "total": 1,\n'
        '  "kind": "title",\n'
        '  "searchDepth": "full"\n'
        '}\n'
    ),
    'codex-table': (
        '┌───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┬──────────────────────┬─────────────┬───────────┬────────────────────────────────────────────────┐\n'
        '│ Key                                                                                                                                                                                                                                                               │ When                 │ Project     │ Kinds     │ Snippet                                        │\n'
        '├───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────────────────┼─────────────┼───────────┼────────────────────────────────────────────────┤\n'
        '│ <KEY> │ 2026-07-14 12:13 UTC │ project-red │ assistant │ Synthetic [WARN] assistant arr[0] [x] response │\n'
        '└───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┴──────────────────────┴─────────────┴───────────┴────────────────────────────────────────────────┘\n'
        '\n'
        '1 of 1 match(es)\n'
    ),
    'codex-json': (
        '{\n'
        '  "schemaVersion": 1,\n'
        '  "source": "codex",\n'
        '  "query": "assistant",\n'
        '  "mode": "fts",\n'
        '  "total": 1,\n'
        '  "hits": [\n'
        '    {\n'
        '      "conversationKey": "<KEY>",\n'
        '      "itemKey": "<ITEM>",\n'
        '      "title": "Synthetic first meaningful user prompt",\n'
        '      "snippet": "Synthetic [WARN] assistant arr[0] [x] response",\n'
        '      "badges": [\n'
        '        "assistant"\n'
        '      ],\n'
        '      "lastActivityUtc": "2026-07-14T12:13:00Z",\n'
        '      "projectLabel": "project-red"\n'
        '    }\n'
        '  ],\n'
        '  "nextCursor": null\n'
        '}\n'
    ),
    'claude-like-table': (
        '┌─────────┬──────────────────────┬─────────┬───────┬────────────────────────────────────────────────────────────────────────────────────────────┐\n'
        '│ Session │ When                 │ Project │ Kinds │ Snippet                                                                                    │\n'
        '├─────────┼──────────────────────┼─────────┼───────┼────────────────────────────────────────────────────────────────────────────────────────────┤\n'
        '│ bk2     │ 2026-06-02T00:00:00Z │ other   │ -     │ … iota kappa lambda mu nu xi omicron [pi] needle rho sigma tau upsilon phi chi psi omega … │\n'
        '├─────────┼──────────────────────┼─────────┼───────┼────────────────────────────────────────────────────────────────────────────────────────────┤\n'
        '│ bk1     │ 2026-06-01T00:00:00Z │ proj    │ -     │ [WARN] needle arr[0] [x] needle needle done                                                │\n'
        '└─────────┴──────────────────────┴─────────┴───────┴────────────────────────────────────────────────────────────────────────────────────────────┘\n'
        '\n'
        '2 of 2 match(es)\n'
    ),
    'claude-like-json': (
        '{\n'
        '  "schemaVersion": 1,\n'
        '  "query": "needle",\n'
        '  "mode": "like",\n'
        '  "hits": [\n'
        '    {\n'
        '      "sessionId": "bk2",\n'
        '      "uuid": "bk2-u1",\n'
        '      "projectLabel": "other",\n'
        '      "title": "other",\n'
        '      "ts": "2026-06-02T00:00:00Z",\n'
        '      "snippet": "\\u2026 iota kappa lambda mu nu xi omicron [pi] needle rho sigma tau upsilon phi chi psi omega \\u2026",\n'
        '      "matchKinds": [],\n'
        '      "costUsd": 0.0\n'
        '    },\n'
        '    {\n'
        '      "sessionId": "bk1",\n'
        '      "uuid": "bk1-u1",\n'
        '      "projectLabel": "proj",\n'
        '      "title": "Review [P1] findings for the tail poll",\n'
        '      "ts": "2026-06-01T00:00:00Z",\n'
        '      "snippet": "[WARN] needle arr[0] [x] needle needle done",\n'
        '      "matchKinds": [],\n'
        '      "costUsd": 0.0\n'
        '    }\n'
        '  ],\n'
        '  "total": 2,\n'
        '  "kind": "all",\n'
        '  "searchDepth": "full"\n'
        '}\n'
    ),
}


def test_1036_search_output_bytes_frozen(tmp_path, monkeypatch, capsys):
    """Detects any byte change in `transcript search` (table and --json, Claude
    FTS / title / LIKE and Codex) from the #1036 snippet rework, including a
    `snippet_parts` key leaking into the CLI JSON."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    got = _capture_search_outputs(ns, tmp_path, monkeypatch, capsys)
    assert set(got) == set(_SEARCH_BASELINE)
    for name, expected in _SEARCH_BASELINE.items():
        assert got[name] == expected, name
    for name, text in got.items():
        assert "snippet_parts" not in text and "snippetParts" not in text, name


# ---- #294 S7 — dual-form export + --speed (spec §4.1) ----------------------

def test_export_speed_on_bare_id_exits_2(tmp_path, monkeypatch, capsys):
    """--speed is Codex pricing behavior; an explicit value on a bare (Claude)
    ref is a usage error, not a silent no-op (resolved-source rule)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed(ns, session_id="sx")
    rc = ns["cmd_transcript"](_ns_export("sx", speed="fast"))
    assert rc == 2
    assert "--speed" in capsys.readouterr().err


def test_export_speed_on_v1_claude_exits_2(tmp_path, monkeypatch, capsys):
    """Explicit --speed (even 'auto') on a v1.claude key is a usage error —
    resolved-source-based, not lexical."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    disp = ns["_load_sibling"]("_lib_conversation_dispatch")
    key = disp._mint_claude_conversation_key("s1")
    rc = ns["cmd_transcript"](_ns_export(key, speed="auto"))
    assert rc == 2
    assert "--speed" in capsys.readouterr().err


def test_export_v1_codex_default_runs_and_emits_markdown(tmp_path, monkeypatch,
                                                         capsysbinary):
    """Qualified default (anonymized) export runs and emits byte-exact Markdown
    (the anonymize/raw byte-parity vs HTTP is proven in the parity test below)."""
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _r = _seed_codex(ns, tmp_path, monkeypatch, scenario="root-a-collision")
    rc = ns["cmd_transcript"](_ns_export(key))
    assert rc == 0
    out = capsysbinary.readouterr().out
    assert out.startswith(b"#")
    assert out.endswith(b"\n") and not out.endswith(b"\n\n")


def test_export_v1_codex_speed_fast_ok(tmp_path, monkeypatch, capsysbinary):
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _r = _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_export(key, raw=True, speed="fast"))
    assert rc == 0
    assert capsysbinary.readouterr().out.startswith(b"#")


def test_export_v1_codex_scope_chat_exits_2(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _r = _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_export(key, scope="chat"))
    assert rc == 2
    assert "transcript:" in capsys.readouterr().err


def test_export_v1_unknown_key_exits_1(tmp_path, monkeypatch, capsys):
    """A malformed / unresolvable v1 key resolves to not_found → the dispatch
    export returns not_found → exit 1 (unknown conversation), never a 0-exit empty."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_export("v1.ghostkeythatdoesnotexist"))
    assert rc == 1
    assert "transcript:" in capsys.readouterr().err


def test_export_v1_codex_pending_exits_1(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _r = _seed_codex(ns, tmp_path, monkeypatch)
    cq = ns["_load_sibling"]("_lib_codex_conversation_query")
    monkeypatch.setattr(cq, "codex_normalization_authoritative", lambda conn: False)
    rc = ns["cmd_transcript"](_ns_export(key))
    assert rc == 1
    err = capsys.readouterr().err
    # A store deferred by a byte-zero replay marker reaches this same branch, so
    # the note must name the pending work generically — never a migration that
    # was applied long ago.
    assert "transcript: Codex conversation is not yet normalized — retry shortly" in err
    assert "025" not in err


def test_export_codex_cli_http_byte_parity(tmp_path, monkeypatch, capsysbinary):
    """§4.1: CLI export byte-matches GET /api/conversation/<v1key>/export in BOTH
    the anonymize (CLI default) and raw modes."""
    from test_codex_conversation_api import _boot as _boot_api, _get, _entity_path
    ns = load_script()
    srv, _root, keys, _r = _boot_api(ns, tmp_path, monkeypatch, claude_sids=())
    key = keys["modern-full"]
    try:
        port = srv.server_address[1]
        _s1, http_raw, _c1 = _get(port, _entity_path(key, "/export"))
        _s2, http_anon, _c2 = _get(port, _entity_path(key, "/export") + "?anonymize=1")
    finally:
        srv.shutdown()
    capsysbinary.readouterr()
    assert ns["cmd_transcript"](_ns_export(key, raw=True)) == 0
    assert capsysbinary.readouterr().out == http_raw
    assert ns["cmd_transcript"](_ns_export(key)) == 0
    assert capsysbinary.readouterr().out == http_anon


# ---- #294 S7 — search --source codex (spec §4.3) ---------------------------

def test_search_codex_table_columns(tmp_path, monkeypatch, capsys):
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_search("Synthetic", source="codex"))
    assert rc == 0
    out = capsys.readouterr().out
    for header in ("Key", "When", "Project", "Kinds", "Snippet"):
        assert header in out, header
    assert "v1." in out                          # full untruncated key


def test_search_codex_json_envelope_pinned(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_search("Synthetic", source="codex", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload)[:2] == ["schemaVersion", "source"]   # stamped-first
    assert payload["schemaVersion"] == 1 and payload["source"] == "codex"
    assert set(payload) == {"schemaVersion", "source", "query", "mode",
                            "total", "hits", "nextCursor"}
    if payload["hits"]:
        h = payload["hits"][0]
        assert set(h) == {"conversationKey", "itemKey", "title", "snippet",
                          "badges", "lastActivityUtc", "projectLabel"}


def test_search_codex_offset_exits_2(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_search("x", source="codex", offset=1))
    assert rc == 2
    assert "--offset" in capsys.readouterr().err


def test_search_cursor_with_claude_exits_2(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rc = ns["cmd_transcript"](_ns_search("x", source="claude", cursor="abc"))
    assert rc == 2
    assert "--cursor" in capsys.readouterr().err


def test_search_codex_filter_flag_exits_2(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_search("x", source="codex", project=["a"]))
    assert rc == 2
    assert "--project" in capsys.readouterr().err


def test_search_codex_bad_cursor_exits_2(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    rc = ns["cmd_transcript"](_ns_search("x", source="codex", cursor="!!!bad"))
    assert rc == 2
    assert "--cursor" in capsys.readouterr().err


def test_search_codex_pending_exit_0_with_note(tmp_path, monkeypatch, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    cq = ns["_load_sibling"]("_lib_codex_conversation_query")
    monkeypatch.setattr(cq, "codex_normalization_authoritative", lambda conn: False)
    rc = ns["cmd_transcript"](_ns_search("Synthetic", source="codex", json=True))
    assert rc == 0                               # navigation: "nothing yet" is truthful
    captured = capsys.readouterr()
    # Same generic wording as the export note: a replay-deferred store lands here
    # too, and telling the user to wait for migration 025 would be false.
    assert "the pending work runs on the next cache open" in captured.err
    assert "025" not in captured.err
    payload = json.loads(captured.out)
    assert payload["source"] == "codex" and payload["hits"] == []


def test_search_codex_cursor_roundtrip_via_subprocess(tmp_path, monkeypatch):
    """§4.3: the external cursor round-trips through a REAL subprocess argv."""
    import os
    import subprocess
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, tmp_path, monkeypatch)
    binp = str(pathlib.Path(ns["__file__"]).resolve())
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["CODEX_HOME"] = str(tmp_path / "provider")
    env["CCTALLY_DATA_DIR"] = str(tmp_path / ".local" / "share" / "cctally")
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["TZ"] = "Etc/UTC"

    def _run(extra):
        return subprocess.run(
            [binp, "transcript", "search", "--source", "codex", "Synthetic",
             "--limit", "1", "--json", *extra],
            capture_output=True, text=True, env=env, timeout=60)

    p1 = _run([])
    assert p1.returncode == 0, p1.stderr
    d1 = json.loads(p1.stdout)
    assert d1["source"] == "codex"
    cur = d1.get("nextCursor")
    # The modern-full slice carries several 'Synthetic…' items (distinct
    # item_keys), so 'Synthetic' at limit=1 MUST expose a second page. Fail
    # loudly (never silently pass) if the corpus ever thins below that.
    assert cur, "corpus must yield a second search page for the cursor round-trip"
    p2 = _run(["--cursor", cur])
    assert p2.returncode == 0, p2.stderr
    d2 = json.loads(p2.stdout)
    a = (d1["hits"][0]["conversationKey"], d1["hits"][0]["itemKey"])
    b = (d2["hits"][0]["conversationKey"], d2["hits"][0]["itemKey"])
    assert a != b


# ---- #850 §4.9 / A23: anonymized export fails closed -----------------------

_A23_BAD = b"/synthetic/\xffproject"
_A23_ACCOUNTS = ("a23aaaa" + "0" * 25, "a23bbbb" + "0" * 25)
_A23_SENTENCE = (
    "transcript: anonymized export is unavailable: 1 Codex project path(s) "
    "could not be read; run cctally cache-sync --source codex --rebuild"
)


def _a23_two_account_store(ns, tmp_path, monkeypatch):
    """A two-account store whose one Codex thread carries an undecodable cwd."""
    key, _rollout = _seed_codex(ns, tmp_path, monkeypatch)
    _seed(ns, session_id="a23-claude", text="edited /home/u/proj/secret.py")
    stats = ns["open_db"]()
    try:
        for index, account in enumerate(_A23_ACCOUNTS):
            stats.execute(
                "INSERT INTO accounts (account_key, provider, natural_id, "
                "email, label, plan_type, label_source, first_seen_utc, "
                "last_seen_utc) VALUES (?,'codex',?,?,?,NULL,'auto',?,?)",
                (account, account, f"a23-{index}@example.test",
                 f"a23-label-{index}", "2026-07-01T00:00:00Z",
                 "2026-07-01T00:00:00Z"),
            )
        stats.commit()
    finally:
        stats.close()
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
            "WHERE conversation_key = ?", (_A23_BAD, key))
        cache.commit()
    finally:
        cache.close()
    # The Codex rows belong to the first account, so `--account a23-label-0`
    # still resolves the conversation and the refusal is the reason the export
    # stops — not a conversation the scope filtered away.
    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key = ?",
            (_A23_ACCOUNTS[0],))
        conversations.commit()
    finally:
        conversations.close()
    return key


def test_850_a23_anonymized_codex_export_fails_closed_scoped_or_not(
    tmp_path, monkeypatch, capsysbinary,
):
    """A23. A path the store cannot supply as a scrub token can appear in any
    transcript, so the anonymized export refuses while any Codex thread `cwd`
    is undecodable — and it writes nothing, rather than emitting bytes that
    only look scrubbed. The scoped arm fails today for a second reason: the
    planner sees an EMPTY view, so it would have counted zero.
    """
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    key = _a23_two_account_store(ns, tmp_path, monkeypatch)

    for account in (None, "a23-label-0"):
        args = _ns_export(key)
        args.account = account
        rc = ns["cmd_transcript"](args)
        captured = capsysbinary.readouterr()
        assert rc == 3, account
        assert captured.out == b"", account
        assert _A23_SENTENCE in captured.err.decode("utf-8"), account

    raw = _ns_export(key, raw=True)
    raw.account = None
    assert ns["cmd_transcript"](raw) == 0
    assert capsysbinary.readouterr().out.startswith(b"#")

    claude = _ns_export("a23-claude")
    claude.account = None
    assert ns["cmd_transcript"](claude) == 0
    assert capsysbinary.readouterr().out.startswith(b"#")


# ---- #850 §4.9 / M5: a read failure never yields anonymized bytes ----------

def _break_the_fail_closed_count(monkeypatch):
    """Make the planner's fail-closed count read raise a real `sqlite3.Error`.

    The raw-table reference is resolved at call time, so pointing it at a table
    that is not there makes `conn.execute` raise `OperationalError: no such
    table` at exactly the statement under test. `_lib_codex_conversation_query`
    binds the resolver at import time, so the viewer's own key set is not
    affected and the planner leg is the only failing read.
    """
    import _lib_codex_metadata as metadata
    monkeypatch.setattr(
        metadata, "resolve_codex_threads_table",
        lambda conn: "main.codex_conversation_threads_absent")


def test_850_anonymized_export_writes_nothing_when_the_count_read_fails(
    tmp_path, monkeypatch, capsysbinary,
):
    """M5 is fail-closed, so a count the store could not perform must stop the
    export. Answering that read with zero let the CLI emit an anonymized
    transcript whose scrub vocabulary was never established.

    The command is driven through ``main`` rather than ``cmd_transcript``,
    because the deliberate outcome is the dispatcher's: a non-zero exit and a
    one-line message, not a traceback.
    """
    ns = load_script()
    # Transcripts are kept regardless of age: docs/fixtures-gotchas.md,
    # "Conversation retention is off in test environments (#965)".
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _rollout = _seed_codex(ns, tmp_path, monkeypatch)
    _break_the_fail_closed_count(monkeypatch)

    rc = ns["main"](["transcript", "export", key])
    captured = capsysbinary.readouterr()
    assert rc != 0, captured.err
    assert captured.out == b""
    assert b"Traceback" not in captured.err
    assert b"Error: no such table" in captured.err

    assert ns["main"](["transcript", "export", key, "--raw"]) == 0
    assert capsysbinary.readouterr().out.startswith(b"#")


# ---- #972 O6: a relabeled conversation's export scrubs both names -----------

_O6_ACCOUNT = ("acct-o6", "o6@example.test")


def _o6_store(ns, tmp_path, monkeypatch, conversations):
    """Codex conversations whose cwds are worktrees the reconcile relabels.

    ``conversations`` is ``[(repository, worktree_name, prompt_label)]`` under
    ``tmp_path/fs``. Each worktree is live for the first pass (its rollup is the
    worktree folder) and removed before the second (the reconcile moves it to
    the repository). Returns ``(keys, cwds, account_key)`` in input order.
    """
    from _codex_zstd_fixtures import conversation_records, write_rollout
    from _fixture_builders import make_git_repository_marker
    from test_codex_bounded_ingest import _auth_json, _expected_key

    redirect_paths(ns, monkeypatch, tmp_path / "data")
    # The provider root sorts after every cwd, so the first cwd is project-1.
    provider = tmp_path / "z-codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    (provider / "auth.json").write_text(_auth_json(*_O6_ACCOUNT))
    cwds = []
    for index, (repository, name, label) in enumerate(conversations):
        repo = tmp_path / "fs" / repository
        make_git_repository_marker(repo)
        worktree = repo / ".worktrees" / name
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/{name}\n")
        rollout = day / (f"rollout-2026-07-20T11-00-0{index}-"
                         f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff000{index}.jsonl")
        write_rollout(rollout, conversation_records(
            1, session_id=f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a0{index}",
            label=label, cwd=str(worktree)))
        cwds.append(worktree)
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
        keys = [cache.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE cwd = ?", (str(cwd),)).fetchone()[0] for cwd in cwds]
    finally:
        cache.close()

    def labels():
        conn = ns["open_conversations_db"]()
        try:
            ns["sync_codex_conversations"](conn)
            return [conn.execute(
                "SELECT project_label FROM main.codex_conversation_rollups "
                "WHERE conversation_key = ?", (key,)).fetchone()[0]
                for key in keys]
        finally:
            conn.close()

    assert labels() == [name for _r, name, _l in conversations]
    for cwd in cwds:
        shutil.rmtree(cwd)
    assert labels() == [repository for repository, _n, _l in conversations]
    account_key = _expected_key(*_O6_ACCOUNT)
    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT OR IGNORE INTO accounts (account_key, provider, natural_id, "
            "email, label, plan_type, label_source, first_seen_utc, "
            "last_seen_utc) VALUES (?,'codex',?,?,'o6',NULL,'auto',?,?)",
            (account_key, account_key, _O6_ACCOUNT[1],
             "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"))
        stats.commit()
    finally:
        stats.close()
    return keys, cwds, account_key


def _o6_export(ns, capsysbinary, key, *, account=None, raw=False):
    capsysbinary.readouterr()
    args = _ns_export(key, raw=raw)
    args.account = account
    rc = ns["cmd_transcript"](args)
    captured = capsysbinary.readouterr()
    return rc, captured.out.decode("utf-8"), captured.err.decode("utf-8")


def test_codex_export_scrubs_repository_and_folder_names(
        tmp_path, monkeypatch, capsysbinary):
    """#972 O6. A rollup relabeled from its worktree folder to its repository
    must leave NEITHER name in an anonymized export: the cwd's own basename and
    its rollup label both scrub to that root's project-N, unscoped and scoped.
    Detects a shared transcript leaking a private repository or branch name."""
    ns = load_script()
    keys, cwds, account_key = _o6_store(
        ns, tmp_path, monkeypatch,
        [("secret-repo", "private-feature", "secret-repo vs private-feature ")])
    rc, raw, _err = _o6_export(ns, capsysbinary, keys[0], raw=True)
    assert rc == 0 and "Prompt secret-repo vs private-feature" in raw
    for account in (None, account_key):
        rc, out, err = _o6_export(ns, capsysbinary, keys[0], account=account)
        assert rc == 0, (account, err)
        assert "secret-repo" not in out and "private-feature" not in out, account
        assert "Prompt project-1 vs project-1" in out, account
    conn = ns["open_conversations_db"]()
    try:
        cq = ns["_load_sibling"]("_lib_conversation_query")
        anon_mod = ns["_load_sibling"]("_lib_conversation_anon")
        plan = cq.build_anon_plan_for_sources(
            conn, home_dir=str(tmp_path / "data"), sources={"codex"}).plan
        assert anon_mod.scrub_text(str(cwds[0]), plan) == "project-1"
    finally:
        conn.close()


def test_scoped_plan_still_refuses_unproven_path(
        tmp_path, monkeypatch, capsysbinary):
    """#972 O6 keeps the scoped admission rule: when the cwd's session_meta is
    stamped by another account, the scoped export refuses (exit 3, no bytes)
    rather than taking the rollup label as proof of ownership."""
    ns = load_script()
    keys, _cwds, account_key = _o6_store(
        ns, tmp_path, monkeypatch,
        [("secret-repo", "private-feature", "secret-repo vs private-feature ")])
    conn = ns["open_conversations_db"]()
    try:
        conn.execute(
            "UPDATE codex_conversation_events SET account_key = 'acct-other' "
            "WHERE record_type = 'session_meta'")
        conn.commit()
    finally:
        conn.close()
    rc, out, err = _o6_export(ns, capsysbinary, keys[0], account=account_key)
    assert rc == 3 and out == ""
    assert "ambiguous account provenance" in err


def test_colliding_bare_word_moves_to_lowest_root(
        tmp_path, monkeypatch, capsysbinary):
    """#972 O6 / V-8. ``secret`` is the folder of the first root and the rollup
    label of the second, so it scrubs to the LOWER-numbered root (project-1),
    and the CLI and the dashboard export agree byte for byte."""
    from test_codex_conversation_api import _entity_path, _get, _wire_handler
    ns = load_script()
    keys, _cwds, _account = _o6_store(
        ns, tmp_path, monkeypatch,
        [("repo", "secret", "first "), ("secret", "other", "secret word ")])
    rc, cli, err = _o6_export(ns, capsysbinary, keys[1])
    assert rc == 0, err
    assert "Prompt project-1 word" in cli and "secret" not in cli
    srv = _wire_handler(ns, no_sync=True)
    try:
        status, http, _ctype = _get(
            srv.server_address[1],
            _entity_path(keys[1], "/export") + "?anonymize=1")
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200
    assert http == cli.encode("utf-8")
