#!/usr/bin/env python3
"""Build deterministic, adapter-produced provider-aware share artifacts."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import pathlib
import shutil
import sqlite3
import sys
import types
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BIN_DIR = pathlib.Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_source_analytics as analytics  # noqa: E402


UTC = timezone.utc
START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 2, 12, tzinfo=UTC)
FORMATS = ("md", "html", "svg", "terminal", "json")
SOURCE_COMMANDS = ("project", "diff", "range-cost", "cache-report", "report")
SOURCE_HANDLERS = {
    "project": "cmd_source_project",
    "diff": "cmd_source_diff",
    "range-cost": "cmd_source_range_cost",
    "cache-report": "cmd_source_cache_report",
    "report": "cmd_source_report",
}
LEGACY_HANDLER_TARGETS = {
    "project": ("_cmd_project_claude", "_cctally_project", "cmd_project"),
    "diff": ("_cmd_diff_claude", "_cctally_diff", "cmd_diff"),
    "range-cost": ("_cmd_range_cost_claude", "_cctally_reporting", "cmd_range_cost"),
    "cache-report": ("_cmd_cache_report_claude", "_cctally_cache_report", "cmd_cache_report"),
    "report": ("_cmd_report_claude", "_cctally_forecast", "cmd_report"),
}
SOURCE_STATES = (
    "codex-populated", "codex-empty", "all-claude-empty",
    "all-codex-empty", "all-claude-unavailable", "all-codex-unavailable",
)
CODEX_REPORTS = ("codex-daily", "codex-monthly", "codex-weekly", "codex-session")
CODEX_REPORT_HANDLERS = {
    "codex-daily": "cmd_codex_daily",
    "codex-monthly": "cmd_codex_monthly",
    "codex-weekly": "cmd_codex_weekly",
    "codex-session": "cmd_codex_session",
}
CODEX_REPORT_STATES = ("populated", "empty")
CANARIES = (
    "/raw-path-canary", "source-root-canary", "conversation-canary",
    "quota-canary", "repository-canary", "project-canary",
)


@dataclass(frozen=True)
class MatrixCase:
    family: str
    command: str
    state: str

    @property
    def slug(self) -> str:
        return f"{self.family}-{self.command}-{self.state}"


CANONICAL_MATRIX = tuple(
    MatrixCase("source", command, state)
    for command in SOURCE_COMMANDS for state in SOURCE_STATES
) + tuple(
    MatrixCase("codex-report", command, state)
    for command in CODEX_REPORTS for state in CODEX_REPORT_STATES
)


def expected_artifact_paths() -> set[pathlib.Path]:
    return {
        pathlib.Path(case.slug) / f"output.{fmt}.golden"
        for case in CANONICAL_MATRIX for fmt in FORMATS
    }


def _load_cctally():
    """Load the real CLI namespace so adapter call-time lookups stay real."""
    path = BIN_DIR / "cctally"
    module = types.ModuleType("cctally")
    module.__file__ = str(path)
    sys.modules["cctally"] = module
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def _entry() -> analytics.QualifiedCodexEntry:
    return analytics.QualifiedCodexEntry(
        timestamp=datetime(2026, 7, 1, 8, tzinfo=UTC),
        source_root_key="/raw-path-canary/source-root-canary",
        conversation_key="conversation-canary",
        project_key=analytics.opaque_project_key(
            "codex", "/raw-path-canary/source-root-canary", "repository-canary/project-canary",
        ),
        project_label="project",
        model="gpt-5",
        input_tokens=900,
        cached_input_tokens=400,
        output_tokens=334,
        reasoning_output_tokens=34,
        total_tokens=1234,
        cost_usd=4.25,
    )


def _diff_entries() -> tuple[analytics.QualifiedCodexEntry, ...]:
    """Exercise changed and new native diff rows through real command routes."""
    root = "/raw-path-canary/source-root-canary"
    project_key = analytics.opaque_project_key(
        "codex", root, "repository-canary/project-canary",
    )
    other_project_key = analytics.opaque_project_key(
        "codex", root, "repository-canary/other-project-canary",
    )
    common = {"source_root_key": root, "conversation_key": "conversation-canary"}
    return (
        _entry(),
        analytics.QualifiedCodexEntry(
            timestamp=datetime(2026, 7, 1, 16, tzinfo=UTC),
            project_key=project_key, project_label="project", model="gpt-5",
            input_tokens=1100, cached_input_tokens=600, output_tokens=400,
            reasoning_output_tokens=40, total_tokens=1500, cost_usd=5.5,
            **common,
        ),
        analytics.QualifiedCodexEntry(
            timestamp=datetime(2026, 7, 1, 18, tzinfo=UTC),
            project_key=other_project_key, project_label="other-project", model="gpt-5.5",
            input_tokens=500, cached_input_tokens=100, output_tokens=200,
            reasoning_output_tokens=20, total_tokens=800, cost_usd=2.0,
            **common,
        ),
    )


def _windows() -> tuple[analytics.AnalyticsWindow, analytics.AnalyticsWindow]:
    return (
        analytics.AnalyticsWindow("A", "calendar", START, datetime(2026, 7, 1, 12, tzinfo=UTC)),
        analytics.AnalyticsWindow("B", "calendar", datetime(2026, 7, 1, 12, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC)),
    )


def _report_blocks(*, populated: bool):
    """Build one real native quota block for the public report handler."""
    if not populated:
        return ()
    from _lib_quota import QuotaObservation, QuotaWindowIdentity, build_blocks
    identity = QuotaWindowIdentity(
        "codex", "/raw-path-canary/source-root-canary", "quota-canary", "five-hour", 300,
    )
    observation = QuotaObservation(
        identity, END - timedelta(hours=1), 37.0, END,
        "/raw-path-canary/source-root-canary/quota-canary.jsonl", 0,
    )
    return build_blocks((observation,))


CLAUDE_WEEK_START = datetime(2026, 6, 29, tzinfo=UTC)
CLAUDE_WEEK_END = CLAUDE_WEEK_START + timedelta(days=7)


class _FixtureArgs(types.SimpleNamespace):
    """Fixture-only namespace that can prove an unobserved sink is unavailable."""

    def __copy__(self):
        return type(self)(**self.__dict__)

    def __setattr__(self, name, value):
        if name == "_source_result_sink" and getattr(self, "discard_legacy_sink", False):
            value = lambda _payload: None
        super().__setattr__(name, value)


def _claude_joined_entries(module, *, populated: bool):
    if not populated:
        return ()
    return (
        module._JoinedClaudeEntry(
            timestamp=START + timedelta(hours=8), model="claude-sonnet-4-20250514",
            input_tokens=100, output_tokens=20, cache_creation_tokens=30,
            cache_read_tokens=40, source_path="/safe/claude/first.jsonl",
            session_id="fixture-session-a", project_path="/safe/claude-project",
            cost_usd=1.25,
        ),
        module._JoinedClaudeEntry(
            timestamp=START + timedelta(hours=13), model="claude-sonnet-4-20250514",
            input_tokens=200, output_tokens=40, cache_creation_tokens=60,
            cache_read_tokens=80, source_path="/safe/claude/second.jsonl",
            session_id="fixture-session-b", project_path="/safe/claude-project",
            cost_usd=2.50,
        ),
    )


def _claude_usage_entries(module, joined):
    return tuple(module.UsageEntry(
        timestamp=entry.timestamp,
        model=entry.model,
        usage={
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens": entry.cache_read_tokens,
        },
        cost_usd=entry.cost_usd,
        source_path=entry.source_path,
    ) for entry in joined)


def _fixture_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots (id INTEGER PRIMARY KEY, week_start_date TEXT, week_end_date TEXT, "
        "week_start_at TEXT, week_end_at TEXT, captured_at_utc TEXT)"
    )
    conn.execute("CREATE TABLE weekly_cost_snapshots (week_start_date TEXT, week_end_date TEXT)")
    conn.execute(
        "INSERT INTO weekly_usage_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        (1, "2026-06-29", "2026-07-05", "2026-06-29T00:00:00Z",
         "2026-07-06T00:00:00Z", "2026-07-02T12:00:00Z"),
    )
    conn.execute(
        "INSERT INTO weekly_cost_snapshots VALUES (?, ?)", ("2026-06-29", "2026-07-05")
    )
    return conn


@contextlib.contextmanager
def _legacy_fixture_dependencies(module, command: str, *, populated: bool):
    """Feed deterministic records into real Claude handlers without replacing them."""
    joined = _claude_joined_entries(module, populated=populated)
    usage = _claude_usage_entries(module, joined)
    restores = []

    def replace(target, name, value):
        restores.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def joined_entries(start, end, **_kwargs):
        return [entry for entry in joined if start <= entry.timestamp <= end]

    def usage_entries(start, end, **_kwargs):
        return [entry for entry in usage if start <= entry.timestamp <= end]

    replace(module, "get_claude_session_entries", joined_entries)
    replace(module, "get_entries", usage_entries)
    replace(module, "_load_claude_config_for_args", lambda _args: {"display": {"tz": "utc"}})
    replace(module, "open_db", _fixture_db)

    if command == "project":
        subweek = module.SubWeek(
            CLAUDE_WEEK_START.isoformat(), CLAUDE_WEEK_END.isoformat(),
            date(2026, 6, 29), date(2026, 7, 5), "fixture", date(2026, 6, 29),
        )
        replace(module, "_compute_subscription_weeks", lambda *_args, **_kwargs: (subweek,))
        replace(module._cctally_project, "open_db", _fixture_db)
        replace(module._cctally_project, "_load_week_snapshots", lambda *_args: {})
    elif command == "report":
        week_ref = module.make_week_ref(
            week_start_date="2026-06-29", week_end_date="2026-07-05",
            week_start_at="2026-06-29T00:00:00Z", week_end_at="2026-07-06T00:00:00Z",
        )
        trend_rows = () if not populated else (module.TuiTrendRow(
            week_label="Jun 29", week_start_at=CLAUDE_WEEK_START,
            used_pct=37.0, dollars_per_percent=1.25 / 37.0, delta_dpp=None,
            spark_height=1, is_current=True, week_start_date=date(2026, 6, 29),
            week_end_date=date(2026, 7, 5), week_end_at=CLAUDE_WEEK_END,
            weekly_cost_usd=1.25, usage_captured_at="2026-07-02T12:00:00Z",
            cost_captured_at="2026-07-02T12:00:00Z", as_of="2026-07-02T12:00:00Z",
            range_start_iso="2026-06-29T00:00:00Z", range_end_iso="2026-07-06T00:00:00Z",
        ),)
        replace(module, "load_config", lambda: {"display": {"tz": "utc"}})
        replace(module, "_get_canonical_boundary_for_date", lambda *_args: (
            "2026-06-29T00:00:00Z", "2026-07-06T00:00:00Z",
        ))
        replace(module, "_apply_reset_events_to_weekrefs", lambda _conn, refs: refs)
        replace(module, "get_recent_weeks", lambda *_args: [week_ref] if populated else [])
        replace(module, "build_trend_view", lambda *_args, **_kwargs: module.TrendView(rows=trend_rows))
        replace(module._cctally_forecast, "open_db", _fixture_db)
    try:
        yield
    finally:
        for target, name, original in reversed(restores):
            setattr(target, name, original)


def _assert_real_legacy_handler(module, command: str) -> None:
    """Fail closed if the source adapter would invoke anything but its command."""
    handler_name, component_name, component_handler_name = LEGACY_HANDLER_TARGETS[command]
    handler = getattr(module, handler_name)
    actual = getattr(getattr(module, component_name), component_handler_name)
    if handler is not actual:
        raise RuntimeError(f"fixture legacy handler bypassed: {command}")


def _source_flags(command: str, state: str) -> tuple[str, bool, bool]:
    if state == "codex-populated":
        return "codex", True, False
    if state == "codex-empty":
        return "codex", False, False
    if state == "all-claude-empty":
        return "all", True, False
    if state == "all-codex-empty":
        return "all", False, False
    if state == "all-claude-unavailable":
        return "all", True, False
    if state == "all-codex-unavailable":
        return "all", True, True
    raise ValueError(state)


def _source_args(
    command: str, source: str, *, populated: bool, unavailable: bool,
    fmt: str | None, json_mode: bool, discard_legacy_sink: bool,
) -> _FixtureArgs:
    windows = _windows()
    legacy_since = None if command == "project" else "2026-07-01"
    legacy_until = None if command == "project" else "2026-07-02"
    return _FixtureArgs(
        command=command, source=source, format=fmt, json=json_mode,
        theme="light", no_branding=False, reveal_projects=False, output="-" if fmt else None,
        copy=False, open_after_write=False,
        source_entries=(
            _diff_entries() if command == "diff" else (_entry(),)
        ) if populated else (),
        range_start=START, range_end=END, start=START, end=END, as_of=END,
        window_a=windows[0], window_b=windows[1], a=None, b=None,
        project=None, model=None, group="git-root", blocks=_report_blocks(
            populated=populated or unavailable,
        ) if command == "report" else (),
        speed="auto", order="desc", sort="cost", breakdown=False, only=None,
        with_extra="", allow_mismatch=False, show_all=False, min_delta_usd=None,
        min_delta_pct=None, top=None, sync=False, total_only=False,
        by_session=False, group_by="date", detail=False, sync_current=False,
        weeks=1, unavailable_fixture=unavailable, discard_legacy_sink=discard_legacy_sink,
        config=None, tz="utc", timezone=None, compact=False, color=False, no_color=False,
        width=None, debug=False, debug_now=False, emit_json=False, since=legacy_since,
        until=legacy_until, days=None, anomaly_threshold_pp=15, anomaly_window_days=14,
        no_anomaly=False, mode="auto", explain=False, offline=True, week_start_name=None,
    )


def _capture_source(module, args, *, command: str, state: str) -> str:
    source = module._cctally_source_analytics
    _assert_real_legacy_handler(module, command)
    emitted: list[str] = []
    original_emit = module._emit
    original_share_emit = module._cctally_share._emit
    original_source_entries = source._source_entries

    def fixture_entries(entry_args, *entry_args_rest, **kwargs):
        if getattr(entry_args, "unavailable_fixture", False):
            if kwargs.get("qualified", False):
                raise source.QualifiedMetadataUnavailable("fixture unavailable")
            raise RuntimeError("fixture unavailable")
        return original_source_entries(entry_args, *entry_args_rest, **kwargs)

    module._emit = lambda content, *, kind, value: emitted.append(content)
    module._cctally_share._emit = lambda content, *, kind, value: emitted.append(content)
    source._source_entries = fixture_entries
    try:
        with _legacy_fixture_dependencies(
            module, command, populated=state != "all-claude-empty",
        ):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = getattr(source, SOURCE_HANDLERS[command])(args)
                if result != 0:
                    raise RuntimeError(f"fixture source handler failed: {command} ({result})")
    finally:
        module._emit = original_emit
        module._cctally_share._emit = original_share_emit
        source._source_entries = original_source_entries
    return emitted[0] if args.format else stdout.getvalue()


def _source_artifacts(module, command: str, state: str) -> dict[str, str]:
    source_name, populated, unavailable = _source_flags(command, state)
    artifacts = {
        fmt: _capture_source(
            module, _source_args(
                command, source_name, populated=populated, unavailable=unavailable,
                fmt=fmt, json_mode=False, discard_legacy_sink=state == "all-claude-unavailable",
            ), command=command, state=state,
        )
        for fmt in ("md", "html", "svg")
    }
    artifacts["terminal"] = _capture_source(
        module, _source_args(
            command, source_name, populated=populated, unavailable=unavailable,
            fmt=None, json_mode=False, discard_legacy_sink=state == "all-claude-unavailable",
        ), command=command, state=state,
    )
    artifacts["json"] = _capture_source(
        module, _source_args(
            command, source_name, populated=populated, unavailable=unavailable,
            fmt=None, json_mode=True, discard_legacy_sink=state == "all-claude-unavailable",
        ), command=command, state=state,
    )
    return artifacts


def _bucket(command: str):
    from _lib_aggregators import CodexBucketUsage
    bucket = {"codex-daily": "2026-07-01", "codex-monthly": "2026-07", "codex-weekly": "2026-06-29"}[command]
    return CodexBucketUsage(bucket, 900, 400, 334, 34, 1234, 4.25, ["gpt-5"], [])


def _session():
    from _lib_aggregators import CodexSessionUsage
    return CodexSessionUsage(
        "conversation-canary-0000", "safe-session", "safe-session", "safe", 900, 400,
        334, 34, 1234, 4.25, ["gpt-5"], [], START, "/safe/codex",
    )


def _codex_view(command: str, populated: bool):
    rows = [_session()] if populated and command == "codex-session" else ([_bucket(command)] if populated else [])
    return types.SimpleNamespace(
        rows=tuple(rows), total_cost_usd=4.25 if populated else 0.0,
        total_tokens=1234 if populated else 0, total_sessions=len(rows),
        period_start=START if populated else END, period_end=END, display_tz_label="UTC",
    ), rows


def _codex_report_args(*, fmt: str | None, json_mode: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        config=None, tz=None, timezone=None, compact=False, speed="auto",
        order="asc", breakdown=False, json=json_mode, format=fmt, theme="light",
        no_branding=False, reveal_projects=False, output="-" if fmt else None,
        copy=False, open_after_write=False, since=None, until=None, debug=False,
    )


def _capture_codex_report(module, command: str, *, populated: bool, args) -> str:
    """Run each established Codex command through its public handler surface."""
    emitted: list[str] = []
    view, _rows = _codex_view(command, populated)
    view_builder_name = "build_" + command.replace("codex-", "codex_") + "_view"
    originals = {
        name: getattr(module, name)
        for name in ("load_config", "resolve_display_tz", "_parse_cli_date_range",
                     "get_codex_entries", view_builder_name, "_emit")
    }
    original_share_emit = module._cctally_share._emit
    module.load_config = lambda *_args, **_kwargs: {"display": {"tz": "utc"}}
    module.resolve_display_tz = lambda *_args, **_kwargs: ZoneInfo("Etc/UTC")
    module._parse_cli_date_range = lambda *_args, **_kwargs: (START, END)
    module.get_codex_entries = lambda *_args, **_kwargs: ()
    setattr(module, view_builder_name, lambda *_args, **_kwargs: view)
    module._emit = lambda content, *, kind, value: emitted.append(content)
    module._cctally_share._emit = lambda content, *, kind, value: emitted.append(content)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = getattr(module._cctally_codex, CODEX_REPORT_HANDLERS[command])(args)
            if result != 0:
                raise RuntimeError(f"fixture Codex handler failed: {command} ({result})")
    finally:
        for name, original in originals.items():
            setattr(module, name, original)
        module._cctally_share._emit = original_share_emit
    return emitted[0] if args.format else stdout.getvalue()


def _codex_artifacts(module, command: str, state: str) -> dict[str, str]:
    populated = state == "populated"
    artifacts = {
        fmt: _capture_codex_report(
            module, command, populated=populated,
            args=_codex_report_args(fmt=fmt, json_mode=False),
        )
        for fmt in ("md", "html", "svg")
    }
    artifacts["terminal"] = _capture_codex_report(
        module, command, populated=populated,
        args=_codex_report_args(fmt=None, json_mode=False),
    )
    artifacts["json"] = _capture_codex_report(
        module, command, populated=populated,
        args=_codex_report_args(fmt=None, json_mode=True),
    )
    return artifacts


def build(out: pathlib.Path) -> None:
    previous_as_of = os.environ.get("CCTALLY_AS_OF")
    os.environ["CCTALLY_AS_OF"] = "2026-07-02T12:00:00Z"
    # Force plain (no-ANSI) output so the terminal goldens are byte-stable
    # regardless of the ambient environment. The Codex-report handlers resolve
    # color from the environment via _supports_color_stdout(), which enables
    # color when $CI is set (as GitHub Actions does) or a dev shell exports
    # $FORCE_COLOR — the source-family args pass color=False explicitly, but the
    # Codex-report handlers take no color arg. Pin NO_COLOR (checked before CI)
    # and drop FORCE_COLOR (checked before NO_COLOR) so generation matches the
    # committed plain goldens everywhere. Mirrors build-codex-quota-fixtures.py.
    previous_no_color = os.environ.get("NO_COLOR")
    previous_force_color = os.environ.get("FORCE_COLOR")
    os.environ["NO_COLOR"] = "1"
    os.environ.pop("FORCE_COLOR", None)
    try:
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        module = _load_cctally()
        for case in CANONICAL_MATRIX:
            artifacts = (
                _source_artifacts(module, case.command, case.state)
                if case.family == "source" else _codex_artifacts(module, case.command, case.state)
            )
            for fmt in FORMATS:
                (out / case.slug).mkdir(parents=True, exist_ok=True)
                (out / case.slug / f"output.{fmt}.golden").write_text(artifacts[fmt], encoding="utf-8")
    finally:
        if previous_as_of is None:
            os.environ.pop("CCTALLY_AS_OF", None)
        else:
            os.environ["CCTALLY_AS_OF"] = previous_as_of
        if previous_no_color is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = previous_no_color
        if previous_force_color is not None:
            os.environ["FORCE_COLOR"] = previous_force_color


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--manifest", action="store_true")
    parser.add_argument("--canaries", action="store_true")
    args = parser.parse_args()
    if args.manifest:
        print("\n".join(str(path) for path in sorted(expected_artifact_paths())))
        return 0
    if args.canaries:
        print("\n".join(CANARIES))
        return 0
    if args.out is None:
        parser.error("--out is required unless --manifest is used")
    build(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
