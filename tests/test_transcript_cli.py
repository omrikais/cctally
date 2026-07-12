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

import pytest
from conftest import load_script, redirect_paths
from test_conversation_endpoints import _boot, _get_ct


# ---- namespace builders ----------------------------------------------------

def _ns_export(session_id, *, scope="all", raw=False, output=None):
    return argparse.Namespace(
        transcript_action="export", session_id=session_id, scope=scope,
        raw=raw, output=output)


def _ns_search(query, **kw):
    base = dict(
        transcript_action="search", query=query, kind="all", limit=50, offset=0,
        project=None, model=None, date_from=None, date_to=None,
        cost_min=None, cost_max=None, rebuild_min=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


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
