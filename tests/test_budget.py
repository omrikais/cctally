"""Unit tests for the pure budget kernel (bin/_lib_budget.py) + F1 structural
invariant that forecast and budget share project_linear.

Also covers the Task 2 per-project budget surface (#19/#121, spec §7):
``budget set/unset --project`` config writes, the per-project display
section, the project-only render path, and the additive ``--json``
``projects[]`` array.
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

_BIN = REPO / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


def _load(name, path):
    # SourceFileLoader handles both `.py` siblings and the extensionless
    # `bin/cctally` main script (spec_from_file_location can't infer a
    # loader for the latter). Mirrors the repo's canonical loaders
    # (tests/test_config_path_override.py, tests/test_pricing_check.py).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass's sys.modules[cls.__module__]
    # lookup resolves (Python 3.14).
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


_budget = _load("_lib_budget", REPO / "bin" / "_lib_budget.py")
BudgetInputs = _budget.BudgetInputs
compute_budget_status = _budget.compute_budget_status
project_linear = _budget.project_linear


UTC = dt.timezone.utc
WS = dt.datetime(2026, 5, 26, 0, 0, tzinfo=UTC)
WE = WS + dt.timedelta(days=7)


def _mk(spent, recent_24h, *, target=300.0, now_days=3.5, thresholds=(90, 100)):
    return BudgetInputs(
        target_usd=target,
        spent_usd=spent,
        recent_24h_usd=recent_24h,
        week_start_at=WS,
        week_end_at=WE,
        now=WS + dt.timedelta(days=now_days),
        alert_thresholds=thresholds,
    )


def test_project_linear_is_pure_unsorted():
    assert project_linear(10.0, 2.0, 1.0, 3.0) == (12.0, 16.0)
    # Does NOT sort — caller's responsibility.
    assert project_linear(0.0, 1.0, 5.0, 1.0) == (5.0, 1.0)


def test_consumption_pct_and_remaining():
    s = compute_budget_status(_mk(spent=182.40, recent_24h=36.0))
    assert abs(s.consumption_pct - 60.8) < 1e-9
    assert abs(s.remaining_usd - 117.60) < 1e-9


def test_verdict_ok_warn_over():
    # ok: tiny spend, tiny recent rate, far from target.
    assert compute_budget_status(_mk(spent=10.0, recent_24h=1.0)).verdict == "ok"
    # over: already past target.
    assert compute_budget_status(_mk(spent=310.0, recent_24h=5.0)).verdict == "over"
    # over by projection: modest spend but a recent rate that projects past target.
    hot = compute_budget_status(_mk(spent=150.0, recent_24h=120.0, now_days=3.5))
    assert hot.verdict == "over"


def test_crossed_thresholds_snap_up():
    # 89.9999999% must count as 90 via the +1e-9 snap-up.
    s = compute_budget_status(_mk(spent=269.9999999999, recent_24h=0.0, target=300.0))
    assert 90 in s.crossed_thresholds


def test_low_confidence_early_week():
    early = compute_budget_status(_mk(spent=5.0, recent_24h=5.0, now_days=0.5))
    assert early.low_confidence is True
    midweek = compute_budget_status(_mk(spent=150.0, recent_24h=40.0, now_days=3.5))
    assert midweek.low_confidence is False


def test_zero_target_is_safe():
    s = compute_budget_status(_mk(spent=50.0, recent_24h=10.0, target=0.0))
    assert s.consumption_pct == 0.0  # no divide-by-zero


def test_empty_thresholds_render_verdict():
    # alerts silenced (empty thresholds) but verdict still computes via fallback.
    s = compute_budget_status(_mk(spent=10.0, recent_24h=1.0, thresholds=()))
    assert s.verdict in {"ok", "warn", "over"}
    assert s.crossed_thresholds == ()


def test_f1_structural_budget_uses_project_linear():
    """compute_budget_status must route projection through project_linear."""
    import inspect
    src = inspect.getsource(compute_budget_status)
    assert "project_linear(" in src
    # And must NOT re-implement the primitive's `current + rate*remaining`
    # math inline — that body lives only in project_linear.
    assert "rate_low * remaining" not in src


def test_budget_status_exposes_week_average_projection():
    # now_days=3.5 over a 168h week => elapsed 84h, remaining 84h.
    # rate_avg = spent/84 ($/h); week-average projection over full window =
    # spent + rate_avg*84 = 2*spent. With spent=100 => 200.
    st = compute_budget_status(_mk(spent=100.0, recent_24h=48.0, now_days=3.5))
    assert abs(st.week_avg_projection_usd - 200.0) < 1e-9
    # Must equal spent + rate_avg*remaining, NOT max(low, high). With a recent
    # rate distinct from the week-average, the high-end band differs from the
    # week-average projection — guards against binding to the wrong field.
    assert (
        st.week_avg_projection_usd != st.projected_eow_high_usd
        or st.projected_eow_low_usd == st.projected_eow_high_usd
    )


def test_week_avg_projection_zero_elapsed_collapses_to_spent():
    # now == week_start => elapsed 0h, rate_avg 0 => projection == spent.
    st = compute_budget_status(_mk(spent=42.0, recent_24h=0.0, now_days=0.0))
    assert abs(st.week_avg_projection_usd - 42.0) < 1e-9


def test_get_budget_config_ignores_unknown_keys():
    """Unknown budget sub-keys are warn-and-ignored, not fatal (forward compat)."""
    cctally = _load("cctally", REPO / "bin" / "cctally")
    out = cctally._get_budget_config(
        {"budget": {"weekly_usd": 300.0, "alerts_enabled": False, "bogus": 1}}
    )
    assert out["weekly_usd"] == 300.0
    assert out["alerts_enabled"] is False
    assert "bogus" not in out


def test_f1_structural_forecast_uses_project_linear():
    """_compute_forecast must route projection through project_linear too."""
    import inspect
    cctally = _load("cctally", REPO / "bin" / "cctally")
    src = inspect.getsource(cctally._compute_forecast)
    assert "project_linear(" in src


# ──────────────────────────────────────────────────────────────────────────
# Task 2: per-project budgets — CLI set/unset + display section (spec §7)
# ──────────────────────────────────────────────────────────────────────────
#
# Driven through load_script() + redirect_paths() so the kernel's path
# constants point at the per-test tmp dir, NOT the developer's real
# ~/.local/share/cctally ([HOME-only test loader reads prod DB] gotcha).

from conftest import load_script, redirect_paths  # noqa: E402
from _fixture_builders import (  # noqa: E402
    seed_session_entry,
    seed_session_file,
)

UTC = dt.timezone.utc
PJ_WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC)
PJ_WEEK_END = PJ_WEEK_START + dt.timedelta(days=7)
PJ_AS_OF = PJ_WEEK_START + dt.timedelta(hours=96)
ENTRY_USD = 1.80  # 100k in + 100k out on claude-sonnet-4-6


def _pj_iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def pjns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _pj_iso(PJ_AS_OF))
    return ns


def _pj_budget_args(**overrides):
    """Build a budget Namespace with every field cmd_budget reads."""
    base = dict(
        action=None, amount=None, project=None,
        config=None, reveal_projects=False, tz=None,
        json=False, format=None, theme="light", no_branding=False,
        output=None, copy=False, open_after_write=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _pj_seed_window(ns):
    """Seed one boundary-aware weekly_usage_snapshots row so the budget window
    resolves to [PJ_WEEK_START, PJ_WEEK_END)."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " page_url, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _pj_iso(PJ_WEEK_START + dt.timedelta(hours=1)),
                PJ_WEEK_START.date().isoformat(),
                (PJ_WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
                _pj_iso(PJ_WEEK_START),
                _pj_iso(PJ_WEEK_END),
                40.0, None, "fixture", json.dumps({"fixture": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _pj_seed_entries(ns, root_to_count):
    """Seed entries: {project_root: n_entries}. Each entry == $1.80."""
    conn = ns["open_cache_db"]()
    try:
        for i, (root, n) in enumerate(root_to_count.items()):
            src = f"/fx/pj-{i}.jsonl"
            seed_session_file(
                conn, path=src, session_id=f"s-{i}", project_path=root,
            )
            for j in range(n):
                seed_session_entry(
                    conn, source_path=src, line_offset=j,
                    timestamp_utc=_pj_iso(PJ_WEEK_START + dt.timedelta(hours=3)),
                    model="claude-sonnet-4-6",
                    input_tokens=100_000, output_tokens=100_000,
                )
        conn.commit()
    finally:
        conn.close()


def _pj_write_config(ns, budget_block):
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"display": {"tz": "utc"}, "budget": budget_block}) + "\n"
    )


def _pj_read_projects(ns):
    import _cctally_core
    if not _cctally_core.CONFIG_PATH.exists():
        return {}
    cfg = json.loads(_cctally_core.CONFIG_PATH.read_text())
    return cfg.get("budget", {}).get("projects", {})


# ── set/unset --project CLI ─────────────────────────────────────────────────


def test_budget_set_project_cwd_resolves_git_root(pjns, monkeypatch, tmp_path):
    """`budget set 25 --project` (bare) inside a git repo writes
    budget.projects[<repo_root>] == 25.0."""
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount="25", project="__CWD__")
    )
    assert rc == 0
    projects = _pj_read_projects(pjns)
    expected_key = os.path.realpath(str(repo))
    assert projects.get(expected_key) == 25.0


def test_budget_set_project_outside_repo_exit_2(pjns, monkeypatch, tmp_path):
    """`budget set 25 --project` (bare) outside any git repo → exit 2."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount="25", project="__CWD__")
    )
    assert rc == 2
    assert _pj_read_projects(pjns) == {}


def test_budget_set_project_explicit_path(pjns):
    """`budget set 10 --project /tmp/some/root` writes that explicit key
    (realpath-normalized)."""
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount="10", project="/tmp/some/root")
    )
    assert rc == 0
    projects = _pj_read_projects(pjns)
    key = os.path.realpath(os.path.expanduser("/tmp/some/root"))
    assert projects.get(key) == 10.0


def test_budget_set_project_explicit_subdir_resolves_to_git_root(pjns, tmp_path):
    """IMPORTANT-2 regression: `budget set 25 --project <monorepo>/packages/foo`
    (a SUB-DIRECTORY of a git-root) stores the GIT-ROOT key, not the sub-dir —
    so `_sum_cost_by_project` (which buckets entries under the git-root) can
    ever match it. Without git-root resolution on the explicit-path branch the
    stored sub-dir key never matches → permanent $0."""
    monorepo = tmp_path / "monorepo"
    (monorepo / ".git").mkdir(parents=True)
    subdir = monorepo / "packages" / "foo"
    subdir.mkdir(parents=True)
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount="25", project=str(subdir))
    )
    assert rc == 0
    projects = _pj_read_projects(pjns)
    git_root = os.path.realpath(str(monorepo))
    sub_key = os.path.realpath(str(subdir))
    # Stored under the git-root, NOT the sub-dir path.
    assert projects.get(git_root) == 25.0
    assert sub_key not in projects


def test_set_project_numeric_value_emits_hint(pjns, capsys, monkeypatch, tmp_path):
    # `budget set --project 25` → argparse binds 25 to --project, amount=None.
    # chdir to a dir with no `./25` so the numeric value is NOT a real path and
    # the misplaced-amount hint fires (deterministic vs the host cwd).
    monkeypatch.chdir(tmp_path)
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount=None, project="25")
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "looks like an amount" in err
    assert "budget set 25 --project" in err


def test_set_project_numeric_named_dir_keeps_requires_amount(
    pjns, capsys, monkeypatch, tmp_path
):
    # A real directory whose name is a bare number (e.g. a repo `./2025`): the
    # user has the PATH right and just omitted the amount — the misplaced-amount
    # hint must NOT fire (it would misdirect). `os.path.isdir` guard excludes it.
    (tmp_path / "2025").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount=None, project="2025")
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires an amount" in err
    assert "looks like an amount" not in err


def test_set_project_real_path_keeps_requires_amount(pjns, capsys):
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount=None, project="/abs/path")
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires an amount" in err
    assert "looks like an amount" not in err  # hint must NOT steal real paths


def test_set_project_bare_flag_keeps_requires_amount(pjns, capsys):
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="set", amount=None, project="__CWD__")
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires an amount" in err
    assert "looks like an amount" not in err


def test_budget_unset_project_removes_key(pjns, capsys):
    """`budget unset --project /tmp/x` removes the configured key; idempotent.

    The FIRST unset reports ``status:"unset"``; a SECOND unset of the
    already-absent key is an idempotent no-op success reporting
    ``status:"noop"`` (still exit 0)."""
    pjns["cmd_budget"](
        _pj_budget_args(action="set", amount="10", project="/tmp/x")
    )
    key = os.path.realpath(os.path.expanduser("/tmp/x"))
    assert key in _pj_read_projects(pjns)
    capsys.readouterr()  # drain the `set` stdout so the unset JSON reads clean
    rc = pjns["cmd_budget"](
        _pj_budget_args(action="unset", project="/tmp/x", json=True)
    )
    assert rc == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {"schemaVersion": 1, "status": "unset", "project_key": key}
    assert key not in _pj_read_projects(pjns)
    # idempotent: unsetting again is a no-op success with status "noop".
    rc2 = pjns["cmd_budget"](
        _pj_budget_args(action="unset", project="/tmp/x", json=True)
    )
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out)
    assert second == {"schemaVersion": 1, "status": "noop", "project_key": key}


# ── display section (terminal + project-only + --json) ──────────────────────


def test_budget_terminal_renders_project_section(pjns, capsys):
    """With budget.projects populated, bare `cctally budget` renders the
    per-project section (basename + budget + spent + used% + verdict)."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    root_b = os.path.realpath("/fake/repos/beta")
    _pj_seed_entries(pjns, {root_a: 10, root_b: 5})  # $18.00, $9.00
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0, root_b: 20.0},
    })
    rc = pjns["cmd_budget"](_pj_budget_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out
    # alpha is over budget ($18 / $15 = 120%), beta is under ($9 / $20 = 45%).
    # Sorted by used% desc → alpha appears before beta.
    assert out.index("alpha") < out.index("beta")


def test_budget_project_only_renders_without_global(pjns, capsys):
    """budget.weekly_usd unset but budget.projects populated → bare
    `cctally budget` STILL renders the per-project section."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    _pj_seed_entries(pjns, {root_a: 10})  # $18.00
    _pj_write_config(pjns, {
        "alerts_enabled": True, "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0},
    })
    rc = pjns["cmd_budget"](_pj_budget_args())
    assert rc == 0
    out = capsys.readouterr().out
    # The global unset message still prints…
    assert "No weekly budget set" in out
    # …AND the per-project section renders.
    assert "alpha" in out


def test_budget_project_only_json(pjns, capsys):
    """Project-only --json emits status:"unset" AND a non-empty projects[]."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    _pj_seed_entries(pjns, {root_a: 10})  # $18.00
    _pj_write_config(pjns, {
        "alerts_enabled": True, "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0},
    })
    rc = pjns["cmd_budget"](_pj_budget_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unset"
    assert payload["weekly_usd"] is None
    assert len(payload["projects"]) == 1
    p = payload["projects"][0]
    assert set(p) >= {
        "project", "project_key", "budget_usd", "spent_usd",
        "consumption_pct", "verdict", "low_confidence",
    }
    assert p["project_key"] == root_a
    assert p["budget_usd"] == 15.0
    assert p["spent_usd"] == pytest.approx(18.0, abs=1e-9)


def test_budget_json_projects_keys_and_sort(pjns, capsys):
    """Full-status --json carries projects[] sorted by consumption_pct desc
    with the documented key set."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    root_b = os.path.realpath("/fake/repos/beta")
    _pj_seed_entries(pjns, {root_a: 10, root_b: 5})  # $18.00, $9.00
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0, root_b: 20.0},
    })
    rc = pjns["cmd_budget"](_pj_budget_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    pcts = [p["consumption_pct"] for p in payload["projects"]]
    assert pcts == sorted(pcts, reverse=True)  # desc
    # alpha (120%) before beta (45%).
    assert payload["projects"][0]["project_key"] == root_a


def test_budget_json_deleted_project_zero_row(pjns, capsys):
    """A configured project_key with NO matching entry renders $0 / 0% / ok,
    never an error (deleted/moved/never-matched repo, spec §7.2)."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    _pj_seed_entries(pjns, {root_a: 10})  # only alpha has spend
    ghost = os.path.realpath("/fake/repos/ghost")  # configured but no entries
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0, ghost: 50.0},
    })
    rc = pjns["cmd_budget"](_pj_budget_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    by_key = {p["project_key"]: p for p in payload["projects"]}
    g = by_key[ghost]
    assert g["spent_usd"] == 0.0
    assert g["consumption_pct"] == 0.0
    assert g["verdict"] == "ok"


def test_budget_empty_projects_baseline_unchanged(pjns, capsys):
    """Empty budget.projects → NO per-project section appended; the unset
    baseline string is byte-identical."""
    _pj_write_config(pjns, {"alerts_enabled": True, "alert_thresholds": [90, 100]})
    rc = pjns["cmd_budget"](_pj_budget_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "No weekly budget set. Set one with: cctally budget set <amount>.\n"


def test_budget_empty_projects_json_baseline_unchanged(pjns, capsys):
    """Empty budget.projects, no global budget → --json baseline carries NO
    projects[] key (and no codex key). `period` is now ALWAYS present (spec
    §5/§10.8, code-review #1) — additive, defaults to subscription-week."""
    _pj_write_config(pjns, {"alerts_enabled": True, "alert_thresholds": [90, 100]})
    rc = pjns["cmd_budget"](_pj_budget_args(json=True))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == (
        '{"schemaVersion": 1, "status": "unset", "weekly_usd": null, '
        '"period": "subscription-week"}'
    )
    payload = json.loads(out)
    assert "projects" not in payload
    assert "codex" not in payload


# ── share-output anonymization (spec §7.5) ──────────────────────────────────


def test_budget_share_anonymizes_project_names(pjns, capsys):
    """Default share output anonymizes project basenames (project-1, …) via
    the _lib_share._scrub chokepoint; the real names never appear."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    _pj_seed_entries(pjns, {root_a: 10})  # $18.00
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0},
    })
    rc = pjns["cmd_budget"](
        _pj_budget_args(format="md", output="-", reveal_projects=False)
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" not in out  # anonymized — real basename absent
    assert "project-1" in out  # the scrubbed label


def test_budget_share_reveal_projects_shows_real_names(pjns, capsys):
    """`--reveal-projects` opts back into real basenames in share output."""
    _pj_seed_window(pjns)
    root_a = os.path.realpath("/fake/repos/alpha")
    _pj_seed_entries(pjns, {root_a: 10})  # $18.00
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_a: 15.0},
    })
    rc = pjns["cmd_budget"](
        _pj_budget_args(format="md", output="-", reveal_projects=True)
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out


# ── same-basename collision (IMPORTANT-1 regression) ────────────────────────
#
# Two DISTINCT git-roots sharing a basename (`app`) must NOT render as two
# bare `app` rows (terminal/JSON), and must NOT collapse to a single
# `project-1` in anonymized share. Routing labels through
# `_project_disambiguate_labels` suffixes the parent-dir segment
# ("app (work)" / "app (personal)"). The pre-existing per-project fixtures use
# three DISTINCT basenames (alpha/beta/gamma) so this gap was untested.


def _pj_collision_setup(pjns):
    """Seed two same-basename git-roots: /fake/work/app ($16.20) and
    /fake/personal/app ($7.20), both budgeted. Returns (root_work, root_home)."""
    _pj_seed_window(pjns)
    root_work = os.path.realpath("/fake/work/app")
    root_home = os.path.realpath("/fake/personal/app")
    # 9 entries → $16.20 (work), 4 entries → $7.20 (personal).
    _pj_seed_entries(pjns, {root_work: 9, root_home: 4})
    _pj_write_config(pjns, {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "alert_thresholds": [90, 100],
        "projects": {root_work: 15.0, root_home: 20.0},
    })
    return root_work, root_home


def test_budget_same_basename_json_rows_distinguishable(pjns, capsys):
    """Two same-basename git-roots get DISTINCT `project` labels in --json
    (not two bare `app`), each carrying its own project_key + spend."""
    root_work, root_home = _pj_collision_setup(pjns)
    rc = pjns["cmd_budget"](_pj_budget_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    labels = [p["project"] for p in payload["projects"]]
    # Both rows are present, and their display labels are NOT both bare `app`.
    assert len(payload["projects"]) == 2
    assert len(set(labels)) == 2, f"labels collided: {labels!r}"
    assert "app" not in labels, f"bare `app` leaked (no disambiguation): {labels!r}"
    by_key = {p["project_key"]: p["project"] for p in payload["projects"]}
    # Disambiguated by parent-dir segment.
    assert by_key[root_work] == "app (work)"
    assert by_key[root_home] == "app (personal)"


def test_budget_same_basename_terminal_distinguishable(pjns, capsys):
    """The terminal per-project table shows BOTH disambiguated labels."""
    _pj_collision_setup(pjns)
    rc = pjns["cmd_budget"](_pj_budget_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "app (work)" in out
    assert "app (personal)" in out


def test_budget_same_basename_share_anon_not_collapsed(pjns, capsys):
    """Anonymized share gives the two same-basename projects DISTINCT anon
    labels (project-1/project-2), spend-RANKED (work $16.20 > personal $7.20
    → work=project-1) — NOT a single collapsed project-1. Proves both the
    disambiguation (distinct ProjectCell labels survive into _collect) AND the
    MoneyCell spend-ranking (MINOR-4) together."""
    _pj_collision_setup(pjns)
    rc = pjns["cmd_budget"](
        _pj_budget_args(format="md", output="-", reveal_projects=False)
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Two distinct anon labels — never collapsed to one.
    assert "project-1" in out
    assert "project-2" in out
    # The real basename never leaks under default (anonymized) output.
    assert "app (work)" not in out
    assert "app (personal)" not in out
    # Spend-ranked: the higher-spend project ($16.20) is project-1, so its
    # spend line ($16.20) sits on the project-1 row and the lower ($7.20) on
    # project-2. A lexical (non-spend) fallback would still number them, but
    # the MoneyCell makes the RANK deterministic by spend.
    p1_line = next(ln for ln in out.splitlines() if "project-1" in ln)
    p2_line = next(ln for ln in out.splitlines() if "project-2" in ln)
    assert "$16.20" in p1_line, f"project-1 not the high-spend row: {p1_line!r}"
    assert "$7.20" in p2_line, f"project-2 not the low-spend row: {p2_line!r}"


# ──────────────────────────────────────────────────────────────────────────
# Task 1: calendar-period + per-vendor (Codex) budget config schema
# (spec §2). Two coverage blocks:
#   A. ``_get_budget_config`` validation of the two new leaves
#      (``budget.period`` enum, ``budget.codex`` nested block) + the new
#      defaults, exercised through the isolated kernel loader so a cached
#      ``_cctally_core`` never reads the real prod DB.
#   B. ``config get/set/unset`` round-trips for the new keys via the CLI
#      (a real subprocess against a scratch ``CCTALLY_DATA_DIR`` — mirrors
#      ``tests/test_project_budget_config.py::_run_cli``). ``budget.period``
#      is a plain string leaf; ``budget.codex`` is a JSON object (like
#      ``budget.projects``).
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cpns(monkeypatch, tmp_path):
    """Isolated kernel namespace for the calendar-period config tests."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── Block A: _get_budget_config validation of the new leaves ─────────────────


def test_period_default_is_subscription_week(cpns):
    """An absent budget block surfaces the new defaults: period
    ``subscription-week`` (zero-migration back-compat) and codex None."""
    cfg = cpns["_get_budget_config"]({})
    assert cfg["period"] == "subscription-week"
    assert cfg["codex"] is None


def test_budget_periods_constants_exposed(cpns):
    """The per-vendor period enums are module constants for the parser/config
    layer to reuse — Codex may NOT use subscription-week."""
    import _cctally_core

    assert _cctally_core.BUDGET_PERIODS == (
        "subscription-week", "calendar-week", "calendar-month",
    )
    assert _cctally_core.CODEX_BUDGET_PERIODS == (
        "calendar-week", "calendar-month",
    )
    assert "subscription-week" not in _cctally_core.CODEX_BUDGET_PERIODS


def test_period_valid_values_accepted(cpns):
    for p in ("subscription-week", "calendar-week", "calendar-month"):
        cfg = cpns["_get_budget_config"]({"budget": {"period": p}})
        assert cfg["period"] == p


def test_period_invalid_value_rejected(cpns):
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"]({"budget": {"period": "foo"}})


def test_period_non_string_rejected(cpns):
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"]({"budget": {"period": 7}})


def test_codex_valid_block_round_trips(cpns):
    cfg = cpns["_get_budget_config"](
        {"budget": {"codex": {"amount_usd": 200, "period": "calendar-month"}}}
    )
    codex = cfg["codex"]
    assert codex["amount_usd"] == 200.0
    assert isinstance(codex["amount_usd"], float)
    assert codex["period"] == "calendar-month"
    # Defaults filled for the unspecified leaves.
    assert codex["alerts_enabled"] is False
    assert codex["alert_thresholds"] == [90, 100]
    assert codex["projected_enabled"] is False


def test_codex_defaults_period_is_calendar_month(cpns):
    """A codex block without an explicit period defaults to calendar-month."""
    cfg = cpns["_get_budget_config"]({"budget": {"codex": {"amount_usd": 50}}})
    assert cfg["codex"]["period"] == "calendar-month"


def test_codex_rejects_subscription_week(cpns):
    """Codex has no Anthropic subscription week — that period is rejected."""
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": 200,
                                  "period": "subscription-week"}}}
        )


def test_codex_must_be_object(cpns):
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"]({"budget": {"codex": [1, 2]}})


def test_codex_amount_must_be_positive_finite(cpns):
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": -5, "period": "calendar-month"}}}
        )
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": 0, "period": "calendar-month"}}}
        )


def test_codex_amount_bool_rejected(cpns):
    """A bool (int subclass) is not a valid amount — mirrors weekly_usd."""
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": True,
                                  "period": "calendar-month"}}}
        )


def test_codex_amount_required(cpns):
    """A codex block with no amount_usd is invalid (it must define a budget)."""
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"period": "calendar-month"}}}
        )


def test_codex_alerts_enabled_must_be_bool(cpns):
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": 200, "period": "calendar-month",
                                  "alerts_enabled": "yes"}}}
        )


def test_codex_alert_thresholds_validated(cpns):
    """The Codex block's thresholds reuse the budget thresholds rule: ints in
    [1,100], sorted/deduped; out-of-range rejected."""
    cfg = cpns["_get_budget_config"](
        {"budget": {"codex": {"amount_usd": 200, "period": "calendar-month",
                              "alert_thresholds": [100, 90, 90]}}}
    )
    assert cfg["codex"]["alert_thresholds"] == [90, 100]
    with pytest.raises(cpns["_BudgetConfigError"]):
        cpns["_get_budget_config"](
            {"budget": {"codex": {"amount_usd": 200, "period": "calendar-month",
                                  "alert_thresholds": [0, 101]}}}
        )


def test_codex_none_is_no_budget(cpns):
    """An explicit null codex value is the no-Codex-budget sentinel."""
    cfg = cpns["_get_budget_config"]({"budget": {"codex": None}})
    assert cfg["codex"] is None


# ── Block B: config get/set/unset round-trip via the CLI ─────────────────────


def _cp_run_cli(data_dir, *args):
    import subprocess

    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "cctally"), *args],
        capture_output=True, text=True, env=env,
    )


def test_config_period_string_round_trip(tmp_path):
    """`config set budget.period calendar-month` persists and round-trips."""
    set_res = _cp_run_cli(tmp_path, "config", "set", "budget.period",
                          "calendar-month")
    assert set_res.returncode == 0, set_res.stderr
    get_res = _cp_run_cli(tmp_path, "config", "get", "budget.period")
    assert get_res.returncode == 0, get_res.stderr
    assert get_res.stdout.strip().endswith("=calendar-month")


def test_config_period_invalid_exit_2(tmp_path):
    """An out-of-enum period is rejected with exit 2, no write."""
    res = _cp_run_cli(tmp_path, "config", "set", "budget.period", "foo")
    assert res.returncode == 2, res.stdout + res.stderr


def test_config_codex_json_round_trip(tmp_path):
    """`config set budget.codex '<json-object>'` persists and `config get`
    emits JSON that parses back to the defaults-filled block."""
    set_res = _cp_run_cli(
        tmp_path, "config", "set", "budget.codex",
        '{"amount_usd": 200, "period": "calendar-month"}',
    )
    assert set_res.returncode == 0, set_res.stderr
    get_res = _cp_run_cli(tmp_path, "config", "get", "budget.codex")
    assert get_res.returncode == 0, get_res.stderr
    rhs = get_res.stdout.strip().split("=", 1)[1]
    parsed = json.loads(rhs)
    assert parsed["amount_usd"] == 200.0
    assert parsed["period"] == "calendar-month"


def test_config_codex_subscription_week_exit_2(tmp_path):
    """A Codex block with period subscription-week is rejected with exit 2."""
    res = _cp_run_cli(
        tmp_path, "config", "set", "budget.codex",
        '{"amount_usd": 200, "period": "subscription-week"}',
    )
    assert res.returncode == 2, res.stdout + res.stderr


def test_config_codex_non_object_exit_2(tmp_path):
    """A JSON array (non-object) for budget.codex is rejected with exit 2."""
    res = _cp_run_cli(tmp_path, "config", "set", "budget.codex", "[1,2]")
    assert res.returncode == 2, res.stdout + res.stderr


def test_config_unset_codex_clears_leaf(tmp_path):
    """`config unset budget.codex` drops the leaf, restoring the None default."""
    _cp_run_cli(
        tmp_path, "config", "set", "budget.codex",
        '{"amount_usd": 200, "period": "calendar-month"}',
    )
    unset_res = _cp_run_cli(tmp_path, "config", "unset", "budget.codex")
    assert unset_res.returncode == 0, unset_res.stderr
    get_res = _cp_run_cli(tmp_path, "config", "get", "budget.codex")
    assert get_res.returncode == 0, get_res.stderr
    rhs = get_res.stdout.strip().split("=", 1)[1]
    # null is the no-Codex-budget sentinel.
    assert json.loads(rhs) is None


# ──────────────────────────────────────────────────────────────────────────
# Task 2: Codex spend helper + per-(vendor,period) status decoupling
# (spec §4/§5). `_sum_codex_cost_for_range` reads the CACHE DB via
# `get_codex_entries` (NOT a stats conn), filters to [start, end), and sums
# `_calculate_codex_entry_cost` per entry — so a Codex budget reconciles to
# `codex-*` to the cent. The calendar/Codex status path must render `$0`/`0%`
# when entries are empty and MUST NOT short-circuit to "no usage data yet
# this week" just because a Claude weekly snapshot is absent (review #5).
# ──────────────────────────────────────────────────────────────────────────

from _fixture_builders import seed_codex_session_entry  # noqa: E402

CX_MONTH_START = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
CX_AS_OF = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cxns(monkeypatch, tmp_path):
    """Isolated kernel namespace pinned to a fixed June clock for the Codex
    spend + calendar-month decoupling tests."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _pj_iso(CX_AS_OF))
    return ns


def _cx_seed_codex_entries(ns, rows):
    """Seed codex_session_entries. `rows` is a list of (timestamp, model,
    input, cached, output) tuples. Returns the cache conn closed."""
    conn = ns["open_cache_db"]()
    try:
        for i, (ts, model, inp, cached, out) in enumerate(rows):
            seed_codex_session_entry(
                conn,
                source_path=f"/fx/codex-{i}.jsonl",
                line_offset=i,
                timestamp_utc=_pj_iso(ts),
                session_id=f"cx-s{i}",
                model=model,
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_output_tokens=0,
                total_tokens=inp + out,
            )
        conn.commit()
    finally:
        conn.close()


def test_sum_codex_cost_for_range_sums_in_range(cxns):
    """`_sum_codex_cost_for_range` sums `_calculate_codex_entry_cost` over the
    in-range entries and excludes out-of-range ones."""
    ns = cxns
    in1 = CX_MONTH_START + dt.timedelta(days=2)
    in2 = CX_MONTH_START + dt.timedelta(days=5)
    before = CX_MONTH_START - dt.timedelta(days=1)   # excluded (< start)
    after = CX_MONTH_START + dt.timedelta(days=40)    # excluded (>= end)
    _cx_seed_codex_entries(ns, [
        (in1, "gpt-5", 100_000, 0, 50_000),
        (in2, "gpt-5", 200_000, 0, 80_000),
        (before, "gpt-5", 999_999, 0, 999_999),
        (after, "gpt-5", 999_999, 0, 999_999),
    ])
    start = CX_MONTH_START
    end = CX_MONTH_START + dt.timedelta(days=30)  # 2026-07-01
    got = ns["_sum_codex_cost_for_range"](start, end, speed="standard")
    # Independent expected: the cost primitive over only the two in-range rows.
    calc = ns["_calculate_codex_entry_cost"]
    expected = (
        calc("gpt-5", 100_000, 0, 50_000, 0, speed="standard")
        + calc("gpt-5", 200_000, 0, 80_000, 0, speed="standard")
    )
    assert abs(got - expected) < 1e-9, f"got={got} expected={expected}"


def test_sum_codex_cost_for_range_empty_is_zero(cxns):
    """No Codex entries → $0.00, never an error."""
    ns = cxns
    start = CX_MONTH_START
    end = CX_MONTH_START + dt.timedelta(days=30)
    assert ns["_sum_codex_cost_for_range"](start, end) == 0.0


def _cx_budget_args(**overrides):
    base = dict(
        action=None, amount=None, project=None, vendor="claude", period=None,
        config=None, reveal_projects=False, tz=None,
        json=False, format=None, theme="light", no_branding=False,
        output=None, copy=False, open_after_write=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_codex_budget_renders_without_weekly_snapshot(cxns, capsys):
    """A configured Codex calendar-month budget renders a `codex` JSON block
    with spend from the entries even when NO weekly_usage_snapshots row exists,
    and never emits the "no usage data yet this week" note (review #5)."""
    ns = cxns
    in1 = CX_MONTH_START + dt.timedelta(days=3)
    _cx_seed_codex_entries(ns, [(in1, "gpt-5", 100_000, 0, 50_000)])
    _pj_write_config(ns, {
        "codex": {"amount_usd": 200.0, "period": "calendar-month"},
    })
    rc = ns["cmd_budget"](_cx_budget_args(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no usage data" not in out.lower()
    payload = json.loads(out)
    assert "codex" in payload
    codex = payload["codex"]
    assert codex["amount_usd"] == 200.0
    assert codex["period"] == "calendar-month"
    calc = ns["_calculate_codex_entry_cost"]
    expected = calc("gpt-5", 100_000, 0, 50_000, 0, speed="standard")
    # speed=auto resolves to standard with no config.toml fast tier present.
    assert abs(codex["spent_usd"] - expected) < 1e-9


def test_codex_budget_empty_renders_zero_without_snapshot(cxns, capsys):
    """A Codex budget with NO entries and NO weekly snapshot renders $0/0%,
    not a no-data short-circuit."""
    ns = cxns
    _pj_write_config(ns, {
        "codex": {"amount_usd": 200.0, "period": "calendar-month"},
    })
    rc = ns["cmd_budget"](_cx_budget_args(json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no usage data" not in out.lower()
    payload = json.loads(out)
    assert payload["codex"]["spent_usd"] == 0.0
    assert payload["codex"]["consumption_pct"] == 0.0


def test_budget_json_period_key_always_present(cxns, capsys):
    """The Claude top-level `--json` carries an additive `period` key always,
    and the `codex` sibling is absent when no Codex budget is configured."""
    ns = cxns
    # A Claude calendar-month budget with no weekly snapshot still renders.
    in1 = CX_MONTH_START + dt.timedelta(days=3)
    conn = ns["open_cache_db"]()
    try:
        seed_session_file(conn, path="/fx/c.jsonl", session_id="s", project_path="/r")
        seed_session_entry(
            conn, source_path="/fx/c.jsonl", line_offset=0,
            timestamp_utc=_pj_iso(in1), model="claude-sonnet-4-6",
            input_tokens=100_000, output_tokens=100_000,
        )
        conn.commit()
    finally:
        conn.close()
    _pj_write_config(ns, {
        "weekly_usd": 300.0, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [90, 100],
    })
    rc = ns["cmd_budget"](_cx_budget_args(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["period"] == "calendar-month"
    assert "codex" not in payload  # gated like projects


def test_vendor_codex_period_subscription_week_exit_2(cxns, capsys):
    """`budget set 200 --vendor codex --period subscription-week` exits 2 with a
    clear stderr message, no config write."""
    ns = cxns
    rc = ns["cmd_budget"](_cx_budget_args(
        action="set", amount="200", vendor="codex", period="subscription-week",
    ))
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "subscription-week" in err or "subscription week" in err


def test_set_codex_budget_writes_codex_block(cxns, capsys):
    """`budget set 200 --vendor codex --period month` writes budget.codex and
    confirms vendor + period + amount."""
    ns = cxns
    rc = ns["cmd_budget"](_cx_budget_args(
        action="set", amount="200", vendor="codex", period="month",
    ))
    assert rc == 0
    import _cctally_core
    cfg = json.loads(_cctally_core.CONFIG_PATH.read_text())
    codex = cfg["budget"]["codex"]
    assert codex["amount_usd"] == 200.0
    assert codex["period"] == "calendar-month"
    out = capsys.readouterr().out.lower()
    assert "codex" in out
    assert "200" in out


def test_set_codex_budget_preserves_period_on_reset(cxns, capsys):
    """`budget set` without `--period` preserves a previously-chosen period."""
    ns = cxns
    ns["cmd_budget"](_cx_budget_args(
        action="set", amount="200", vendor="codex", period="calendar-week",
    ))
    capsys.readouterr()
    rc = ns["cmd_budget"](_cx_budget_args(
        action="set", amount="250", vendor="codex", period=None,
    ))
    assert rc == 0
    import _cctally_core
    cfg = json.loads(_cctally_core.CONFIG_PATH.read_text())
    codex = cfg["budget"]["codex"]
    assert codex["amount_usd"] == 250.0
    assert codex["period"] == "calendar-week"  # preserved, not reset to default


def test_unset_codex_budget_removes_block(cxns, capsys):
    """`budget unset --vendor codex` removes budget.codex."""
    ns = cxns
    ns["cmd_budget"](_cx_budget_args(
        action="set", amount="200", vendor="codex", period="month",
    ))
    capsys.readouterr()
    rc = ns["cmd_budget"](_cx_budget_args(action="unset", vendor="codex"))
    assert rc == 0
    import _cctally_core
    cfg = json.loads(_cctally_core.CONFIG_PATH.read_text())
    assert cfg["budget"].get("codex") is None


def test_set_claude_period_month_terminal_header(cxns, capsys):
    """`budget set 300 --period month` then bare budget renders a
    `(calendar month YYYY-MM)` header from the display-tz civil boundary."""
    ns = cxns
    in1 = CX_MONTH_START + dt.timedelta(days=3)
    conn = ns["open_cache_db"]()
    try:
        seed_session_file(conn, path="/fx/c.jsonl", session_id="s", project_path="/r")
        seed_session_entry(
            conn, source_path="/fx/c.jsonl", line_offset=0,
            timestamp_utc=_pj_iso(in1), model="claude-sonnet-4-6",
            input_tokens=100_000, output_tokens=100_000,
        )
        conn.commit()
    finally:
        conn.close()
    ns["cmd_budget"](_cx_budget_args(
        action="set", amount="300", vendor="claude", period="month",
    ))
    capsys.readouterr()
    rc = ns["cmd_budget"](_cx_budget_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "calendar month 2026-06" in out
