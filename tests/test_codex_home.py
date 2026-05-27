"""Issue #108 — $CODEX_HOME multi-root resolution for Codex commands.

Covers the two resolvers, multi-root config detection, session-id derivation
under multiple roots, and end-to-end ingestion union (totals + session id).
"""
from __future__ import annotations

import importlib.util
import json as _json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


def _load_cctally_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cc():
    return _load_cctally_module()


# ── _codex_home_roots() ───────────────────────────────────────────────────
def test_home_roots_unset_defaults(cc, tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_empty_string_defaults(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_single(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "a"))
    assert cc._codex_home_roots() == [tmp_path / "a"]


def test_home_roots_comma_list_and_blanks(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/a, ,{tmp_path}/b,")
    assert cc._codex_home_roots() == [tmp_path / "a", tmp_path / "b"]


def test_home_roots_all_blank_falls_back(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", " , ,")
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_expands_tilde(cc, tmp_path, monkeypatch):
    # NOTE: Path.expanduser() resolves "~" via os.path.expanduser, which reads
    # $HOME (not the Path.home() classmethod), so we set $HOME rather than
    # monkeypatching cc.pathlib.Path.home here.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "~/codexdir")
    assert cc._codex_home_roots() == [tmp_path / "codexdir"]


# ── _codex_session_roots() ────────────────────────────────────────────────
def test_session_roots_home_with_sessions(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    assert cc._codex_session_roots() == [tmp_path / "h" / "sessions"]


def test_session_roots_direct_jsonl_dir(cc, tmp_path, monkeypatch):
    # No sessions/ subdir → the entry itself is walked directly.
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "logs"))
    assert cc._codex_session_roots() == [tmp_path / "logs"]


def test_session_roots_nonexistent_skipped(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/missing,{tmp_path}/h")
    assert cc._codex_session_roots() == [tmp_path / "h" / "sessions"]


def test_session_roots_mixed_ordered_and_deduped(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    # h listed twice → deduped, order preserved.
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/h,{tmp_path}/logs,{tmp_path}/h")
    assert cc._codex_session_roots() == [
        tmp_path / "h" / "sessions",
        tmp_path / "logs",
    ]


# ── _detect_codex_fast_service_tier() any-root ────────────────────────────
def _write_cfg(root: Path, tier: str | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if tier is not None:
        (root / "config.toml").write_text(f'service_tier = "{tier}"\n')


def test_detect_fast_any_root_true(cc, tmp_path, monkeypatch):
    _write_cfg(tmp_path / "a", "standard")
    _write_cfg(tmp_path / "b", "fast")
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/a,{tmp_path}/b")
    assert cc._detect_codex_fast_service_tier() is True


def test_detect_fast_all_clean_false(cc, tmp_path, monkeypatch):
    _write_cfg(tmp_path / "a", "standard")
    _write_cfg(tmp_path / "b", None)  # no config.toml at all
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/a,{tmp_path}/b")
    assert cc._detect_codex_fast_service_tier() is False


def test_detect_fast_priority_in_direct_dir(cc, tmp_path, monkeypatch):
    # A direct-JSONL entry (no sessions/) that nonetheless carries a fast
    # config.toml MUST count — config is read from every entry.
    _write_cfg(tmp_path / "logs", "priority")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "logs"))
    assert cc._detect_codex_fast_service_tier() is True


def test_detect_fast_missing_root_skipped(cc, tmp_path, monkeypatch):
    _write_cfg(tmp_path / "b", "fast")
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/missing,{tmp_path}/b")
    assert cc._detect_codex_fast_service_tier() is True


# ── multi-root ingestion (real JSONL walked by sync_codex_cache) ──────────
def _iso_ms(y, mo, d, h, mi, s):
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}.000Z"


def _write_rollout(jsonl_path: Path, session_id: str, model: str,
                   inp: int, cached: int, out: int) -> None:
    """Write a minimal real Codex rollout JSONL (schema the ingest iterator
    expects: session_meta → turn_context → one yielded token_count event)."""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": _iso_ms(2026, 4, 17, 10, 0, 0), "type": "session_meta",
         "payload": {"id": session_id}},
        {"timestamp": _iso_ms(2026, 4, 17, 10, 0, 1), "type": "turn_context",
         "payload": {"model": model}},
        {"timestamp": _iso_ms(2026, 4, 17, 10, 1, 0), "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {
                 "input_tokens": inp, "cached_input_tokens": cached,
                 "output_tokens": out, "reasoning_output_tokens": 0,
                 "total_tokens": inp + out},
             "total_token_usage": {"total_tokens": inp + out}}}},
    ]
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(_json.dumps(rec, separators=(",", ":")) + "\n")


def _run_codex(args, *, home, data_dir, codex_home):
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    env.update({
        "HOME": str(home), "TZ": "Etc/UTC", "NO_COLOR": "1",
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DATA_DIR": str(data_dir),
        "CCTALLY_AS_OF": "2026-04-20T00:00:00Z",
    })
    if codex_home is not None:
        env["CODEX_HOME"] = codex_home
    return subprocess.run([sys.executable, str(CCTALLY), *args],
                          capture_output=True, text=True, env=env, check=True).stdout


def test_multiroot_ingestion_union_totals(cc, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    data = tmp_path / "data"
    a = tmp_path / "rootA"; b = tmp_path / "rootB"
    _write_rollout(a / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-aaaa.jsonl",
                   "aaaa", "gpt-5", 1000, 0, 500)
    _write_rollout(b / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-bbbb.jsonl",
                   "bbbb", "gpt-5", 2000, 0, 700)

    def total(codex_home):
        # fresh cache per run
        import shutil
        if data.exists():
            shutil.rmtree(data)
        out = _run_codex(["codex-daily", "--json"], home=home, data_dir=data,
                         codex_home=codex_home)
        return _json.loads(out)["totals"]["costUSD"]

    only_a = total(str(a / ".codex"))
    only_b = total(str(b / ".codex"))
    both = total(f"{a / '.codex'},{b / '.codex'}")
    assert only_a > 0 and only_b > 0
    assert both == pytest.approx(only_a + only_b)


# ── _session_path_parts() multi-root ──────────────────────────────────────
def test_path_parts_under_second_root(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/h,{tmp_path}/logs")
    agg = cc._load_sibling("_lib_aggregators")
    src = str(tmp_path / "logs" / "2026" / "04" / "x.jsonl")
    id_path, fname, directory = agg._session_path_parts(src)
    assert id_path == "2026/04/x"
    assert fname == "x"
    assert directory == "2026/04"


def test_path_parts_home_sessions_relative(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    agg = cc._load_sibling("_lib_aggregators")
    src = str(tmp_path / "h" / "sessions" / "2026" / "04" / "y.jsonl")
    id_path, _, _ = agg._session_path_parts(src)
    assert id_path == "2026/04/y"


def test_path_parts_bare_relative_fixture_form_unchanged(cc, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    agg = cc._load_sibling("_lib_aggregators")
    id_path, fname, directory = agg._session_path_parts(
        ".codex/sessions/proj/rollout-z.jsonl")
    assert id_path == "proj/rollout-z"
    assert directory == "proj"


def test_multiroot_session_ids_end_to_end(cc, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    data = tmp_path / "data"
    a = tmp_path / "rootA"; b = tmp_path / "rootB"
    _write_rollout(a / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-aaaa.jsonl",
                   "aaaa", "gpt-5", 1000, 0, 500)
    # direct-JSONL root: no sessions/ subdir, jsonl sits directly under <entry>.
    _write_rollout(b / "2026" / "04" / "17" / "rollout-bbbb.jsonl",
                   "bbbb", "gpt-5", 2000, 0, 700)
    out = _run_codex(["codex-session", "--json"], home=home, data_dir=data,
                     codex_home=f"{a / '.codex'},{b}")
    sessions = _json.loads(out)["sessions"]
    ids = {s["sessionId"] for s in sessions}
    # root A is a Codex home → id relative to <A>/.codex/sessions.
    # root B is a direct-JSONL dir → id relative to <B> itself.
    assert "2026/04/17/rollout-aaaa" in ids
    assert "2026/04/17/rollout-bbbb" in ids
