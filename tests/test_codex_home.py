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


def test_home_roots_malformed_tilde_user_skipped(cc, tmp_path, monkeypatch):
    # A `~baduser` entry (the user does not exist) makes expanduser() raise
    # RuntimeError; the bad entry must be dropped, not abort the whole list —
    # the valid sibling root beside it survives. No exception escapes.
    monkeypatch.setenv("CODEX_HOME", f"~cctally_nonexistent_user_zz/x,{tmp_path}/good")
    roots = cc._codex_home_roots()
    assert tmp_path / "good" in roots
    # The malformed entry left no residue: only the valid root remains.
    assert roots == [tmp_path / "good"]


def test_home_roots_all_invalid_tilde_no_fallback(cc, tmp_path, monkeypatch):
    # P3 (issue #108): $CODEX_HOME set to a SINGLE all-invalid `~user` entry.
    # The variable IS explicitly set, so we must respect the override and
    # yield [] — NOT silently fall back to the default ~/.codex account.
    # (Contrast test_home_roots_all_blank_falls_back, where no non-blank entry
    # was given at all, so the default still applies.)
    monkeypatch.setenv("CODEX_HOME", "~cctally_nonexistent_user_zz")
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == []


def test_home_roots_relative_made_absolute(cc, tmp_path, monkeypatch):
    # P2 (issue #108): a relative $CODEX_HOME must canonicalize to absolute so
    # real ingested source_paths are absolute — otherwise the cache-prune's
    # isabs() fixture carve-out cannot distinguish a real relative-root row
    # from a synthetic baked-fixture row, and a relative-root switch leaks
    # stale totals. .absolute() resolves against cwd.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", "codexA")
    roots = cc._codex_home_roots()
    assert roots == [tmp_path / "codexA"]
    assert roots[0].is_absolute()


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


def _run_codex(args, *, home, data_dir, codex_home, cwd=None, columns=None):
    env = dict(os.environ)
    env.pop("CODEX_HOME", None)
    env.update({
        "HOME": str(home), "TZ": "Etc/UTC", "NO_COLOR": "1",
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DATA_DIR": str(data_dir),
        "CCTALLY_AS_OF": "2026-04-20T00:00:00Z",
        # Suppress the detached background update-check (main()'s post-command
        # hook). It mkdir's APP_DIR (= CCTALLY_DATA_DIR) to write
        # update-state.json / update.log asynchronously after codex-daily
        # returns; test_multiroot_ingestion_union_totals does
        # `shutil.rmtree(data)` between sub-runs, which races that detached
        # writer under full-suite IO load and flaked ~15% on Linux CI.
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
    })
    if codex_home is not None:
        env["CODEX_HOME"] = codex_home
    if columns is not None:
        # Widen the table so the cross-root disambiguator suffix renders in
        # full rather than ellipsizing under the no-TTY narrow fallback.
        env["COLUMNS"] = str(columns)
    return subprocess.run([sys.executable, str(CCTALLY), *args],
                          capture_output=True, text=True, env=env,
                          cwd=cwd, check=True).stdout


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


# ── P2: alias/symlink dedup in the file walker ────────────────────────────
def test_walker_dedups_symlinked_root_alias(cc, tmp_path):
    # P2 (issue #108): the same physical rollout reachable via two root
    # spellings (canonical + symlink) must ingest ONCE. The walker dedups on
    # the RESOLVED path; UNIQUE(source_path, line_offset) keys on the string,
    # so a raw-spelling dedup would double-count tokens/cost on a fresh walk.
    cache = cc._load_sibling("_cctally_cache")
    real = tmp_path / "real" / "sessions"
    real.mkdir(parents=True)
    _write_rollout(real / "2026" / "04" / "17" / "rollout-x.jsonl",
                   "xxxx", "gpt-5", 1000, 0, 500)
    link = tmp_path / "linked"
    os.symlink(tmp_path / "real", link)
    # List both spellings (canonical + via the symlink) as if comma-listed.
    roots = [real, link / "sessions"]
    found = list(cache._iter_codex_jsonl_paths(roots))
    assert len(found) == 1, f"alias double-glob not deduped: {found}"


def test_root_aware_discovery_uses_first_canonical_provider_root(cc, tmp_path, monkeypatch):
    """One physical rollout has one resolved path and the first configured
    provider root wins, even when later entries overlap through aliases."""
    cache = cc._load_sibling("_cctally_cache")
    provider = tmp_path / "provider"
    rollout = provider / "sessions" / "2026" / "04" / "17" / "rollout-x.jsonl"
    _write_rollout(rollout, "xxxx", "gpt-5", 1000, 0, 500)
    alias_parent = tmp_path / "alias-parent"
    os.symlink(provider, alias_parent)
    # The first entry is a Codex home (provider root != sessions walk root).
    # The next two entries reach the same file directly and via a symlink.
    monkeypatch.setenv(
        "CODEX_HOME",
        f"{provider},{provider / 'sessions'},{alias_parent / 'sessions'}",
    )

    discovered = cache._discover_codex_files_with_roots()

    assert len(discovered) == 1
    item = discovered[0]
    assert item.physical_path == rollout.resolve()
    assert item.provider_root == provider.resolve()
    assert item.walk_root == (provider / "sessions").resolve()
    assert item.source_root_key == __import__("hashlib").sha256(
        b"cctally-source-root-v1\0" + str(provider.resolve()).encode("utf-8")
    ).hexdigest()[:32]


def test_root_aware_discovery_keeps_configured_path_spelling_for_rows(
    cc, tmp_path, monkeypatch,
):
    """Physical de-dup must not rewrite the path reporting uses for roots."""
    cache = cc._load_sibling("_cctally_cache")
    real = tmp_path / "real-provider"
    rollout = real / "sessions" / "proj" / "rollout-x.jsonl"
    _write_rollout(rollout, "xxxx", "gpt-5", 1000, 0, 500)
    alias = tmp_path / "configured-provider"
    os.symlink(real, alias)
    monkeypatch.setenv("CODEX_HOME", str(alias))

    [item] = cache._discover_codex_files_with_roots()

    assert item.physical_path == rollout.resolve()
    assert item.source_path == alias / "sessions" / "proj" / "rollout-x.jsonl"


# ── P1: cache scoped to current $CODEX_HOME (prior-root purge) ────────────
def test_codex_home_switch_purges_prior_root(cc, tmp_path):
    # P1 (issue #108): reusing one cache.db across `CODEX_HOME=/A` then
    # `CODEX_HOME=/B` must return B's totals ONLY, not A+B. iter_codex_entries
    # has no root predicate, so sync_codex_cache must purge prior-root rows.
    home = tmp_path / "home"; home.mkdir()
    data = tmp_path / "data"           # SHARED across both runs (no rmtree)
    a = tmp_path / "rootA"; b = tmp_path / "rootB"
    _write_rollout(a / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-aaaa.jsonl",
                   "aaaa", "gpt-5", 1000, 0, 500)
    _write_rollout(b / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-bbbb.jsonl",
                   "bbbb", "gpt-5", 2000, 0, 700)

    def cost(out):
        return _json.loads(out)["totals"]["costUSD"]

    # Baseline B-only total from a pristine cache dir.
    only_b = cost(_run_codex(["codex-daily", "--json"], home=home,
                             data_dir=tmp_path / "data_b_only",
                             codex_home=str(b / ".codex")))
    assert only_b > 0

    # Run A first (populates the SHARED cache), then switch to B against the
    # same cache. The switched run must equal only_b, never only_a + only_b.
    only_a = cost(_run_codex(["codex-daily", "--json"], home=home,
                             data_dir=data, codex_home=str(a / ".codex")))
    assert only_a > 0
    switched = cost(_run_codex(["codex-daily", "--json"], home=home,
                               data_dir=data, codex_home=str(b / ".codex")))
    assert switched == pytest.approx(only_b)
    assert switched != pytest.approx(only_a + only_b)


def test_relative_codex_home_switch_purges_prior_root(cc, tmp_path):
    # P2 (issue #108): the prior-root purge must also work for a RELATIVE
    # $CODEX_HOME (e.g. `codexA`). _codex_home_roots canonicalizes to absolute
    # so the row is stored absolute and the prune's isabs guard prunes it.
    # Run with cwd=tmp_path so the relative roots resolve there.
    home = tmp_path / "home"; home.mkdir()
    data = tmp_path / "data"           # SHARED across both runs (no rmtree)
    _write_rollout(tmp_path / "codexA" / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-aaaa.jsonl",
                   "aaaa", "gpt-5", 1000, 0, 500)
    _write_rollout(tmp_path / "codexB" / ".codex" / "sessions" / "2026" / "04" / "17" / "rollout-bbbb.jsonl",
                   "bbbb", "gpt-5", 2000, 0, 700)

    def cost(out):
        return _json.loads(out)["totals"]["costUSD"]

    only_b = cost(_run_codex(["codex-daily", "--json"], home=home,
                             data_dir=tmp_path / "data_b_only",
                             codex_home="codexB/.codex", cwd=tmp_path))
    assert only_b > 0
    only_a = cost(_run_codex(["codex-daily", "--json"], home=home,
                             data_dir=data, codex_home="codexA/.codex", cwd=tmp_path))
    assert only_a > 0
    switched = cost(_run_codex(["codex-daily", "--json"], home=home,
                               data_dir=data, codex_home="codexB/.codex", cwd=tmp_path))
    assert switched == pytest.approx(only_b)
    assert switched != pytest.approx(only_a + only_b)


def test_codex_session_no_merge_across_roots_same_relpath(cc, tmp_path):
    # P3 (issue #108): two DISTINCT files sharing a relative path under
    # different $CODEX_HOME roots must NOT collapse into one codex-session row.
    # _session_path_parts strips the matched root, so both yield id_path
    # "2026/04/17/rollout-same"; the aggregator must disambiguate by root.
    home = tmp_path / "home"; home.mkdir()
    data = tmp_path / "data"
    a = tmp_path / "rootA"; b = tmp_path / "rootB"
    rel = ("sessions", "2026", "04", "17", "rollout-same.jsonl")
    _write_rollout(a.joinpath(".codex", *rel), "aaaa", "gpt-5", 1000, 0, 500)
    _write_rollout(b.joinpath(".codex", *rel), "bbbb", "gpt-5", 2000, 0, 700)

    codex_home = f"{a / '.codex'},{b / '.codex'}"
    out = _run_codex(["codex-session", "--json"], home=home, data_dir=data,
                     codex_home=codex_home)
    payload = _json.loads(out)
    sessions = payload["sessions"]
    # Two distinct sessions, NOT one merged row.
    assert len(sessions) == 2, f"cross-root sessions collapsed: {sessions}"
    assert sorted(s["totalTokens"] for s in sessions) == [1500, 2700]
    # `sessionId` keeps its upstream-compatible relative-PATH value (unchanged),
    # so both rows still share it...
    assert {s["sessionId"] for s in sessions} == {"2026/04/17/rollout-same"}
    # ...but issue #110 adds an additive `codexRoot` discriminator on the
    # (only) colliding rows, present on BOTH and DISTINCT, mapping each row to
    # its matched $CODEX_HOME root.
    assert all("codexRoot" in s for s in sessions), sessions
    roots = {s["codexRoot"] for s in sessions}
    assert roots == {str(a / ".codex"), str(b / ".codex")}, roots
    by_root = {s["codexRoot"]: s["totalTokens"] for s in sessions}
    assert by_root[str(a / ".codex")] == 1500
    assert by_root[str(b / ".codex")] == 2700
    # Both sessions are counted in the report totals (A + B), not just one.
    assert payload["totals"]["totalTokens"] == 1500 + 2700

    # Table render (wide, so the suffix isn't ellipsized): the Session column
    # carries a short root discriminator — distinct per row.
    table = _run_codex(["codex-session"], home=home, data_dir=data,
                       codex_home=codex_home, columns=300)
    assert "(rootA)" in table, table
    assert "(rootB)" in table, table


def test_codex_root_short_labels_disambiguator():
    """Unit-test the cross-root label helper (issue #110): first differing
    segment after the longest common ancestor, with full-path fallback."""
    _load_cctally_module()  # side effect: loads _lib_render into sys.modules
    render = sys.modules["_lib_render"]
    # Canonical ~/.codex layout under distinct parents → parent segment wins.
    assert render._codex_root_short_labels(
        ["/tmp/x/rootA/.codex", "/tmp/x/rootB/.codex"]) == ["rootA", "rootB"]
    # Roots differing only at the leaf → leaf segment wins.
    assert render._codex_root_short_labels(
        ["/home/.codex", "/home/.codexbackup"]) == [".codex", ".codexbackup"]
    # No common ancestor → first segments already distinct.
    assert render._codex_root_short_labels(
        ["/aaa/.codex", "/bbb/.codex"]) == ["aaa", "bbb"]
