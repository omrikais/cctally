"""Build-scoped Codex path-identity memo (#566 §5.1 item 1).

``_session_path_parts`` resolved ``_codex_session_roots()`` once per ENTRY, not
once per file: on the maintainer's store that was 191,304 calls resolving about
2,324 distinct paths, and every one of them ran an ``is_dir()`` syscall per
configured root. The memo collapses that to one root resolution and one parse
per distinct path per source build.

The key-correctness cases come first, because they are the ones that would
silently corrupt output rather than merely cost time. A memo that outlived a
root change would keep returning an identity derived from roots that no longer
exist, and nothing downstream would notice.
"""
import pathlib

import pytest
from conftest import load_script  # type: ignore


@pytest.fixture()
def agg(monkeypatch, tmp_path):
    load_script()
    import _lib_aggregators
    return _lib_aggregators


def _make_roots(tmp_path, *names):
    roots = []
    for name in names:
        root = tmp_path / name / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        roots.append(root)
    return roots


def test_a_new_scope_sees_new_roots(agg, tmp_path, monkeypatch):
    """The memo is keyed on the roots, so a root change is never served stale."""
    root_a, root_b = _make_roots(tmp_path, "homeA", "homeB")
    source_path = str(root_a / "2026" / "08" / "rollout-x.jsonl")

    with agg.codex_path_scope(roots=[root_a]):
        under_a = agg._session_path_parts(source_path)
    with agg.codex_path_scope(roots=[root_b]):
        under_b = agg._session_path_parts(source_path)

    # Under root A the path is relative to it; under root B it matches no root
    # and falls back to the basename. Same input, different answer -- which is
    # exactly what a memo keyed on the path alone would get wrong.
    assert under_a == ("2026/08/rollout-x", "rollout-x", "2026/08")
    assert under_b == ("rollout-x", "rollout-x", ".")
    assert under_a != under_b


def test_the_scope_pins_roots_for_its_own_lifetime(agg, tmp_path):
    """Roots are captured once per build, so one build is internally coherent."""
    root_a, root_b = _make_roots(tmp_path, "homeA", "homeB")
    source_path = str(root_a / "2026" / "08" / "rollout-x.jsonl")
    with agg.codex_path_scope(roots=[root_a]) as scope:
        first = agg._session_path_parts(source_path)
        second = agg._session_path_parts(source_path)
        assert first == second
        assert scope.misses == 1


def test_roots_resolve_once_per_scope_not_once_per_entry(agg, tmp_path, monkeypatch):
    """The call-count collapse this change exists for."""
    root_a, = _make_roots(tmp_path, "homeA")
    calls = {"n": 0}
    import cctally

    real = cctally._codex_session_roots

    def counting():
        calls["n"] += 1
        return [root_a]

    monkeypatch.setattr(cctally, "_codex_session_roots", counting)
    try:
        paths = [str(root_a / f"rollout-{i}.jsonl") for i in range(50)]
        with agg.codex_path_scope():
            for path in paths * 4:
                agg._session_path_parts(path)
        assert calls["n"] == 1
    finally:
        monkeypatch.setattr(cctally, "_codex_session_roots", real)


def test_misses_are_bounded_by_distinct_paths(agg, tmp_path):
    root_a, = _make_roots(tmp_path, "homeA")
    paths = [str(root_a / f"rollout-{i}.jsonl") for i in range(37)]
    with agg.codex_path_scope(roots=[root_a]) as scope:
        for path in paths * 11:
            agg._session_path_parts(path)
        assert scope.misses == 37


def test_no_scope_still_resolves_live_roots(agg, tmp_path, monkeypatch):
    """Outside a build the function keeps its historic per-call behaviour.

    The path is nested under root_a so that the two roots produce DIFFERENT
    answers for the same argument. With a flat path both roots return
    ("rollout-x", "rollout-x", "."), and a leaked module-level memo serving
    root_a's answer to the second call would satisfy the assertion it is
    supposed to fail.
    """
    root_a, root_b = _make_roots(tmp_path, "homeA", "homeB")
    source_path = str(root_a / "deep" / "rollout-x.jsonl")
    import cctally

    monkeypatch.setattr(cctally, "_codex_session_roots", lambda: [root_a])
    assert agg._session_path_parts(source_path) == (
        "deep/rollout-x", "rollout-x", "deep",
    )
    monkeypatch.setattr(cctally, "_codex_session_roots", lambda: [root_b])
    assert agg._session_path_parts(source_path) == ("rollout-x", "rollout-x", ".")


def test_scopes_nest_and_restore(agg, tmp_path):
    root_a, root_b = _make_roots(tmp_path, "homeA", "homeB")
    source_path = str(root_a / "deep" / "rollout-x.jsonl")
    with agg.codex_path_scope(roots=[root_a]):
        outer = agg._session_path_parts(source_path)
        with agg.codex_path_scope(roots=[root_b]):
            inner = agg._session_path_parts(source_path)
        restored = agg._session_path_parts(source_path)
    assert outer == restored == ("deep/rollout-x", "rollout-x", "deep")
    assert inner == ("rollout-x", "rollout-x", ".")
    assert agg.active_codex_path_scope() is None


def test_aggregate_sessions_is_byte_identical_under_a_scope(agg, tmp_path, monkeypatch):
    """The memo must not move the aggregate it feeds."""
    import datetime as dt
    import cctally

    root_a, = _make_roots(tmp_path, "homeA")
    monkeypatch.setattr(cctally, "_codex_session_roots", lambda: [root_a])
    entries = [
        agg.CodexEntry(
            timestamp=dt.datetime(2026, 8, 1, 12, i, tzinfo=dt.timezone.utc),
            session_id=f"s{i % 3}",
            model="gpt-5",
            input_tokens=100 + i,
            cached_input_tokens=10 + i,
            output_tokens=20 + i,
            reasoning_output_tokens=i,
            total_tokens=120 + 2 * i,
            source_path=str(root_a / "2026" / "08" / f"rollout-{i % 3}.jsonl"),
        )
        for i in range(12)
    ]
    without = agg._aggregate_codex_sessions(list(entries))
    with agg.codex_path_scope():
        within = agg._aggregate_codex_sessions(list(entries))
    assert without == within


def test_the_memo_is_independent_of_the_speed_tier(agg, tmp_path, monkeypatch):
    """Speed feeds cost, not identity, so it is deliberately not a memo key."""
    import datetime as dt
    import cctally

    root_a, = _make_roots(tmp_path, "homeA")
    monkeypatch.setattr(cctally, "_codex_session_roots", lambda: [root_a])
    entries = [
        agg.CodexEntry(
            timestamp=dt.datetime(2026, 8, 1, 12, i, tzinfo=dt.timezone.utc),
            session_id=f"s{i % 2}", model="gpt-5",
            input_tokens=100 + i, cached_input_tokens=10 + i,
            output_tokens=20 + i, reasoning_output_tokens=i,
            total_tokens=120 + 2 * i,
            source_path=str(root_a / "2026" / "08" / f"rollout-{i % 2}.jsonl"),
        )
        for i in range(8)
    ]
    for speed in ("standard", "fast"):
        without = agg._aggregate_codex_sessions(list(entries), speed=speed)
        with agg.codex_path_scope():
            within = agg._aggregate_codex_sessions(list(entries), speed=speed)
        assert without == within
    # Non-vacuity: the two tiers really do produce different costs, so the
    # equality above is not comparing two identical universes.
    standard = agg._aggregate_codex_sessions(list(entries), speed="standard")
    fast = agg._aggregate_codex_sessions(list(entries), speed="fast")
    assert [row.cost_usd for row in standard] != [row.cost_usd for row in fast]


def test_an_unused_scope_never_touches_the_filesystem(agg, tmp_path, monkeypatch):
    """Root resolution is deferred to the first path the scope is asked about.

    The rooted fallback session view derives identity lexically and must never
    discover anything, so a build that reaches it opens a scope and asks it
    nothing. Resolving roots at scope entry made that build run an `is_dir()`
    per configured root anyway, which broke the contract
    `test_rooted_fallback_sessions_never_discover_filesystem` pins.
    """
    import cctally

    monkeypatch.setattr(
        cctally, "_codex_session_roots",
        lambda: (_ for _ in ()).throw(AssertionError("resolved eagerly")),
    )
    with agg.codex_path_scope() as scope:
        assert scope.misses == 0
