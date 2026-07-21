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


def _seed_codex(ns, tmp_path, monkeypatch, *, scenario="modern-full"):
    """Ingest one Codex corpus scenario into the redirected cache and return
    ``(conversation_key, rollout_path)``. ``redirect_paths`` must have already run
    (or is run here when ``needs_redirect`` — see callers)."""
    provider = tmp_path / "provider"
    rollout = provider / "sessions" / "2026" / "07" / "15" / f"{scenario}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CODEX_CORPUS / f"{scenario}.jsonl", rollout)
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
    redirect_paths(ns, monkeypatch, tmp_path)
    key, _r = _seed_codex(ns, tmp_path, monkeypatch, scenario="root-a-collision")
    rc = ns["cmd_transcript"](_ns_export(key))
    assert rc == 0
    out = capsysbinary.readouterr().out
    assert out.startswith(b"#")
    assert out.endswith(b"\n") and not out.endswith(b"\n\n")


def test_export_v1_codex_speed_fast_ok(tmp_path, monkeypatch, capsysbinary):
    ns = load_script()
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
    assert "025" in capsys.readouterr().err     # cites migration 025


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
    assert "025" in captured.err                 # one stderr note citing migration 025
    payload = json.loads(captured.out)
    assert payload["source"] == "codex" and payload["hits"] == []


def test_search_codex_cursor_roundtrip_via_subprocess(tmp_path, monkeypatch):
    """§4.3: the external cursor round-trips through a REAL subprocess argv."""
    import os
    import subprocess
    ns = load_script()
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
