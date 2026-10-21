"""#968 spec 5.2: one logical identity per rollout, whatever its container."""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import load_script, redirect_paths  # noqa: E402
from _codex_zstd_fixtures import (  # noqa: E402
    compress_in_place, token_records, write_rollout)

from test_codex_prune_drain import _no_detached_drains  # noqa: F401,E402

import _lib_codex_rollout_container as rc  # noqa: E402

UUID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
NAME = f"rollout-2026-07-20T10-00-00-{UUID}.jsonl"


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex-provider"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    return ns, provider, day


def _cc():
    """The cache module: these helpers are not re-exported on `cctally`, and
    `ns[...]` is a dict lookup that no module `__getattr__` reaches."""
    return sys.modules["_cctally_cache"]


def _entries(ns, path):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?",
            (str(path),)).fetchone()[0]
    finally:
        conn.close()


def test_compressed_only_rollout_is_discovered_under_its_logical_path(env):
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(3, session_id="s1"))
    compress_in_place(plain)
    found = _cc()._discover_codex_files_with_roots()
    # 92e06df85 globs only **/*.jsonl, so a compressed-only rollout is invisible.
    assert [str(d.source_path) for d in found] == [str(plain)]
    assert getattr(found[0], "container_kind", None) == rc.ZSTD


def test_identity_is_unchanged_by_compression(env):
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(3, session_id="s1"))
    before = [_cc().codex_file_identity(d)
              for d in _cc()._discover_codex_files_with_roots()]
    compress_in_place(plain)
    after = [_cc().codex_file_identity(d)
             for d in _cc()._discover_codex_files_with_roots()]
    assert len(before) == 1 and after == before


def test_both_forms_yield_one_entry_and_plain_wins(env):
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(3, session_id="s1"))
    twin = plain.with_name(plain.name + ".zst")
    twin.write_bytes(b"\x28\xb5\x2f\xfdnot-a-real-frame")
    found = _cc()._discover_codex_files_with_roots()
    assert [str(d.source_path) for d in found] == [str(plain)]
    assert getattr(found[0], "container_kind", "plain") == rc.PLAIN


def test_targeted_qualification_takes_either_spelling_and_refuses_escapes(
        env, tmp_path):
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(2, session_id="s1"))
    zst = compress_in_place(plain)
    outside = tmp_path / "elsewhere" / ("rollout-x-" + UUID + ".jsonl")
    write_rollout(outside, token_records(2, session_id="s2"))
    escaped = day / ("rollout-2026-07-20T11-00-00-" + UUID + ".jsonl.zst")
    os.symlink(compress_in_place(outside), escaped)
    qualified = _cc()._qualify_codex_targets({str(zst), str(escaped)})
    # 92e06df85 drops every non-`.jsonl` suffix, so the compressed target is lost.
    assert [str(d.source_path) for d in qualified] == [str(plain)]


def test_filename_facts_are_identical_for_both_spellings(env):
    ns, provider, day = env
    import _lib_jsonl
    import _lib_aggregators
    zname = day / (NAME + ".zst")
    match = _lib_jsonl._CODEX_FILENAME_UUID_RE.search(zname.name)
    assert match is not None and match.group(1) == UUID
    roots = [provider / "sessions"]
    assert (_lib_aggregators._session_path_parts_for_roots(roots, str(zname))
            == _lib_aggregators._session_path_parts_for_roots(
                roots, str(day / NAME)))


def test_recognition_reads_compressed_records_and_no_decoder_is_no_evidence(
        env, monkeypatch):
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(2, session_id="s1"))
    zst = compress_in_place(plain)
    # 92e06df85 parses the raw zstd frame as JSON and never recognizes it.
    assert _cc()._codex_file_is_recognizable(zst) is True
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    assert _cc()._codex_file_is_recognizable(zst) is False


def test_compressed_only_history_is_retained_by_a_full_walk(env):
    """I2: a rollout Codex compressed keeps every cached row."""
    ns, _provider, day = env
    plain = day / NAME
    write_rollout(plain, token_records(4, session_id="s1"))
    keeper = day / ("rollout-2026-07-20T12-00-00-" + UUID[:-1] + "2.jsonl")
    write_rollout(keeper, token_records(1, session_id="s2"))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    assert _entries(ns, plain) == 4
    compress_in_place(plain)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    # 92e06df85 no longer discovers the rollout and the ordinary prune deletes
    # its four rows. The plain keeper makes the prune scope eligible: it is the
    # newest file in discovery order, so the tail-first recognition probe
    # (`_codex_prune_scope`) reads it first.
    assert _entries(ns, plain) == 4


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("discovery", ["files", "fallback"])
def test_full_discovery_accepts_outside_file_links_and_ignores_archives(
        env, tmp_path, compressed, discovery):
    ns, provider, day = env
    outside = tmp_path / "outside.jsonl"
    write_rollout(outside, token_records(1, session_id="outside"))
    target = compress_in_place(outside) if compressed else outside
    walked = day / NAME
    link = walked.with_name(NAME + ".zst") if compressed else walked
    link.symlink_to(target)
    # Both walked aliases resolve to the same target; retain the first spelling.
    (day / ("z-alias.jsonl.zst" if compressed else "z-alias.jsonl")).symlink_to(target)
    archived = provider / "archived_sessions" / NAME
    write_rollout(archived, token_records(1, session_id="archive"))
    if discovery == "files":
        found = _cc()._discover_codex_files_with_roots()
        assert [f.source_path for f in found] == [walked]
        assert found[0].physical_path == outside.resolve()
        assert found[0].container_path == link
        assert found[0].container_kind == (rc.ZSTD if compressed else rc.PLAIN)
        from _lib_source_identity import codex_file_key
        assert _cc().codex_file_identity(found[0]) == codex_file_key(
            found[0].source_root_key, str(outside.resolve()))
    else:
        assert list(_cc()._iter_codex_jsonl_paths([provider / "sessions"])) == [walked]
    assert _cc()._qualify_codex_targets({str(link)}) == []


@pytest.mark.parametrize("discovery", ["files", "fallback"])
def test_full_discovery_keeps_identity_fallback_but_targeted_refuses_escape(
        env, tmp_path, monkeypatch, discovery):
    ns, provider, day = env
    outside = tmp_path / "outside.jsonl"
    write_rollout(outside, token_records(1, session_id="outside"))
    walked = day / NAME
    walked.symlink_to(outside)

    def fail_resolve(self, *args, **kwargs):
        raise OSError("synthetic identity resolver failure")

    monkeypatch.setattr(pathlib.Path, "resolve", fail_resolve)
    if discovery == "files":
        found = _cc()._discover_codex_files_with_roots()
        assert [f.source_path for f in found] == [walked]
        assert found[0].physical_path == walked.absolute()
    else:
        assert list(_cc()._iter_codex_jsonl_paths([provider / "sessions"])) == [walked]
    assert _cc()._qualify_codex_targets({str(walked)}) == []
