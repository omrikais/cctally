"""cctally CLI argument-parser construction (eager sibling).

Holds the full argparse tree: build_parser() + per-command _build_*_parser
helpers, the _add_*_args helpers, _share_validate_args, the _nonneg_int
type validator, and CLIHelpFormatter. Loaded eagerly by bin/cctally; every
symbol is re-exported into the cctally namespace. cmd_* handlers and other
bin/cctally-staying globals are reached via the call-time _cctally() accessor.

Spec: docs/superpowers/specs/2026-05-30-parser-share-extraction-design.md
"""
from __future__ import annotations

import argparse
import sys
import textwrap

from _cctally_core import WEEKDAY_MAP
from _lib_display_tz import _argparse_tz


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _nonneg_int(raw: str) -> int:
    """argparse `type=` validator for non-negative integer flags (issue #89).

    Used by ``--debug-samples`` so a negative N is rejected at parse time
    rather than silently coerced inside the helper. Raises
    ``argparse.ArgumentTypeError`` so argparse surfaces the message under
    the standard ``argument <flag>:`` prefix.
    """
    try:
        n = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be a non-negative integer, got '{raw}'"
        )
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


class CLIHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Human-friendly formatter for multi-line help and default values."""

    def __init__(self, prog: str, **kwargs: object) -> None:
        kwargs.setdefault("max_help_position", 30)
        super().__init__(prog, **kwargs)  # type: ignore[arg-type]


def _argparse_has_arg(parser, option_string: str) -> bool:
    """Return True if ``parser`` already registered ``option_string``."""
    for action in parser._actions:
        if option_string in (action.option_strings or ()):
            return True
    return False


def _add_mode_arg(parser, *, noop: bool = False) -> None:
    """Add ccusage's -m/--mode {auto,calculate,display} cost-source flag.

    Standalone (not folded into _add_ccusage_alias_args) so it lands only
    on the six Session-C reporting commands and never collides with
    range-cost, which defines its own -m/--mode.

    noop=True (five-hour-blocks only): the flag is accepted for surface
    parity with `blocks` but does not alter numbers — that command's cost
    is the authoritative materialized five_hour_blocks.total_cost_usd
    computed at record-time (always auto semantics).
    """
    help_real = (
        "Cost source: auto (recorded costUSD when present, else computed), "
        "calculate (always compute from embedded pricing), display "
        "(recorded costUSD only; $0 when absent). Default: auto."
    )
    help_noop = (
        "Accepted for ccusage drop-in compat; no-op here — five-hour-blocks "
        "cost is the authoritative materialized per-block value computed at "
        "record-time. Default: auto."
    )
    parser.add_argument(
        "-m", "--mode",
        default="auto",
        choices=["auto", "calculate", "display"],
        help=help_noop if noop else help_real,
    )


def _add_ccusage_alias_args(parser, *, ansi_emit: bool) -> None:
    """Attach the Session A ccusage alias surface to a Claude-cmd subparser.

    Sibling to ``_add_codex_shared_args`` (declared inside ``build_parser``)
    but tailored for Claude commands. Every flag is guarded with
    ``_argparse_has_arg`` so existing per-parser declarations
    (cache-report's ``--offline``, project / five-hour-blocks / diff's
    ``--no-color``) do NOT cause ``argparse.ArgumentError`` — the helper
    just skips the duplicate. This makes future collisions self-healing
    when a contributor adds a Session A-managed flag directly on a
    subparser.

    Args:
      parser:    the subparser to mutate.
      ansi_emit: ``True`` for project + diff (the 2 real ANSI emitters).
                 ``False`` for the other 8 in-scope cmds. Controls only
                 the ``--color`` help text and whether ``--no-color`` is
                 attempted as a fresh add (when ``ansi_emit=True`` we
                 skip ``--no-color`` entirely — those parsers already
                 declared it themselves).

    Spec §7.1.2 / issue #86 Session A.
    """

    def _maybe_add(opt: str, *args, **kwargs):
        if _argparse_has_arg(parser, opt):
            return
        parser.add_argument(opt, *args, **kwargs)

    def _maybe_add2(opt1: str, opt2: str, *args, **kwargs):
        # Two-form add (short + long) — skip if EITHER is present.
        if _argparse_has_arg(parser, opt1) or _argparse_has_arg(parser, opt2):
            return
        parser.add_argument(opt1, opt2, *args, **kwargs)

    _maybe_add2(
        "-z", "--timezone", default=None, metavar="TZ",
        help="Alias for --tz (drop-in compat with ccusage). When both "
             "are supplied, --tz wins.",
    )
    _maybe_add2(
        "-O", "--offline",
        action=argparse.BooleanOptionalAction, default=False,
        help="Accepted for ccusage drop-in compat; cctally is always offline.",
    )
    _maybe_add(
        "--compact", action="store_true",
        help="Force compact table layout regardless of terminal width.",
    )
    _maybe_add(
        "--config", default=None, metavar="PATH",
        help="Read config from PATH for this invocation only (no "
             "mutation of the default config at "
             "~/.local/share/cctally/config.json). Missing or invalid "
             "PATH errors out with a clear message.",
    )
    _maybe_add2(
        "-d", "--debug", action="store_true",
        help="Emit a stderr 'Pricing Mismatch Debug Report' "
             "(totals + per-model stats + sample discrepancies, "
             "matching ccusage's --debug shape).",
    )
    _maybe_add(
        "--debug-samples", type=_nonneg_int, default=5, metavar="N",
        help="Cap on sample-discrepancy rows in the --debug report "
             "(default 5; N=0 suppresses the sample block; "
             "negatives rejected at parse time).",
    )
    _maybe_add(
        "--single-thread", action="store_true",
        help="Accepted for ccusage drop-in compat; cctally ingestion "
             "is already single-threaded via the session-entry cache.",
    )
    if ansi_emit:
        _maybe_add(
            "--color", action="store_true", default=False,
            help="Force ANSI color output (overrides NO_COLOR env). When "
                 "neither --color nor --no-color is set, color is auto-"
                 "detected from isatty() and NO_COLOR/FORCE_COLOR env.",
        )
        # --no-color already declared on these parsers; do nothing here.
    else:
        # No-op-for-compat surface (spec §7.3): these flags parse but do
        # NOT flow through the color resolver on this command. Color (where
        # the renderer emits any) follows the auto-detect — isatty() plus
        # NO_COLOR / FORCE_COLOR env — so the help must NOT claim "no ANSI
        # is emitted" (daily/monthly/weekly/blocks/session/cache-report DO
        # emit auto-detected ANSI on a TTY; only the no-color env vars
        # suppress it). Force/suppress color on the 2 real ANSI commands
        # (project, diff) instead, or use NO_COLOR=1 / FORCE_COLOR=1.
        _maybe_add(
            "--color", action="store_true", default=False,
            help="Accepted for ccusage drop-in compat; does not control "
                 "this command's color. Color auto-detects from isatty() "
                 "and honors NO_COLOR / FORCE_COLOR env.",
        )
        _maybe_add(
            "--no-color", action="store_true", default=False,
            help="Accepted for ccusage drop-in compat; does not suppress "
                 "this command's color. Use NO_COLOR=1 env (or pipe stdout) "
                 "to disable auto-detected ANSI.",
        )


def _add_codex_shared_args(parser: argparse.ArgumentParser) -> None:
    """Register upstream `ccusage-codex sharedArgs` on a codex subparser.

    Upstream sharedArgs (node_modules/@ccusage/codex/dist/index.js):
      --timezone/-z, --locale/-l, --compact, --color, --noColor,
      --offline/--no-offline.

    Honored here: --timezone (dates + aggregation buckets) and
    --compact (table layout). Accepted-but-no-op (stored on the
    namespace for drop-in parity with upstream scripts): --locale
    (we don't locale-format dates), --color / --noColor (we don't
    emit ANSI codes today). --offline is accepted as a no-op too
    (we are always offline); it uses BooleanOptionalAction so
    `--no-offline` also parses cleanly. `-O` is kept as the short
    form for offline for backward compat with earlier builds.
    """
    parser.add_argument(
        "-z", "--timezone", default=None, metavar="TZ",
        help="IANA timezone for date bucketing and Date/Last Activity cells.",
    )
    parser.add_argument(
        "-l", "--locale", default=None, metavar="LOCALE",
        help="Accepted for drop-in compat; no-op (dates are not locale-formatted).",
    )
    parser.add_argument(
        "--compact", action="store_true",
        help="Force compact table layout regardless of terminal width.",
    )
    parser.add_argument(
        "--color", action="store_true",
        help="Accepted for drop-in compat; no-op today (no ANSI escapes are emitted).",
    )
    parser.add_argument(
        "--noColor", action="store_true", dest="no_color",
        help="Accepted for drop-in compat; no-op today (no ANSI escapes are emitted).",
    )
    parser.add_argument(
        "-O", "--offline", action=argparse.BooleanOptionalAction, default=False,
        help="Accepted for drop-in compat with ccusage-codex; we are always offline.",
    )
    parser.add_argument(
        "--speed", choices=("auto", "standard", "fast"), default="auto",
        help="Codex pricing tier. auto (default) reads service_tier from "
             "~/.codex/config.toml (fast|priority -> fast pricing); fast "
             "forces the fast-tier multiplier; standard forces base pricing.",
    )
    parser.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. Overrides "
             "config display.tz for this call. Takes precedence over "
             "upstream's --timezone for drop-in parity.",
    )
    # Issue #92: codex parity for the #89 --debug surface. Codex JSONL
    # has no recorded costUSD to diff against, so the report is the
    # codex variant ("Codex Pricing Debug Report": totals + top-N
    # highest computed-cost entries), wired via
    # _emit_codex_debug_samples_if_set in each cmd_codex_* body.
    parser.add_argument(
        "-d", "--debug", action="store_true",
        help="Emit a stderr 'Codex Pricing Debug Report' (totals + "
             "the N highest computed-cost sample entries).",
    )
    parser.add_argument(
        "--debug-samples", type=_nonneg_int, default=5, metavar="N",
        help="Cap on top-entry sample rows in the --debug report "
             "(default 5; N=0 suppresses the sample block; "
             "negatives rejected at parse time).",
    )


def _add_share_args(parser, *, has_status_line: bool = False) -> None:
    """Attach shareable-reports flags + format/json mutex to a subparser.

    Idempotent — call exactly once per subparser. Caller MUST remove any
    pre-existing ``--json`` (and ``--status-line`` for forecast) from the
    subparser before invoking this helper, so the mutex group owns those
    flags. Raises ``RuntimeError`` on contract violation — surfaces at
    parser-build time (i.e., on every CLI invocation, including ``--help``)
    instead of at the user invocation that hits the unguarded
    ``--format --json`` combo. The prior shape silently skipped re-adding,
    leaving the mutex unenforced for any future 9th share-enabled subparser
    whose existing ``--json`` was accidentally left in place.
    """
    if _argparse_has_arg(parser, "--json"):
        raise RuntimeError(
            f"_add_share_args: parser {parser.prog!r} already has --json; "
            "remove it before calling _add_share_args so mutex applies"
        )
    if has_status_line and _argparse_has_arg(parser, "--status-line"):
        raise RuntimeError(
            f"_add_share_args: parser {parser.prog!r} already has --status-line; "
            "remove it before calling _add_share_args(has_status_line=True)"
        )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--format", choices=("md", "html", "svg"),
        help="Render output as shareable markdown, self-contained HTML, or SVG. "
             "Default destination: md->stdout, html/svg->~/Downloads file.")
    output_group.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON; suppresses terminal render.")
    if has_status_line:
        output_group.add_argument(
            "--status-line", action="store_true", dest="status_line",
            help="Emit one-line compact string for status-line injection.")

    parser.add_argument(
        "--theme", choices=("light", "dark"), default="light",
        help="Color theme for HTML/SVG (default: light). No-op for markdown.")
    parser.add_argument(
        "--no-branding", action="store_true", dest="no_branding",
        help="Strip the 'Generated by cctally' footer from --format output.")
    parser.add_argument(
        "--output", metavar="PATH",
        help="Write --format output to PATH instead of the default destination "
             "(stdout for md; ~/Downloads/cctally-<cmd>-<utcdate>.<ext> for html/svg). "
             "Use '-' for stdout.")
    parser.add_argument(
        "--copy", action="store_true",
        help="Pipe --format md output to clipboard (pbcopy/xclip/clip). "
             "Rejected for html/svg.")
    parser.add_argument(
        "--open", action="store_true", dest="open_after_write",
        help="After writing --format html/svg to a file, open it in the default app. "
             "Rejected for md.")


def _share_validate_args(args) -> None:
    """Reject share flag combinations BEFORE any DB / sync / render work.

    Two layers of validation:

    1. Share-only flags (``--output``, ``--copy``, ``--open``) require
       ``--format``. Silent dropping trains users to assume the file
       was written.

    2. Destination-shape combinations (``--copy`` + ``--output``,
       ``--copy`` + non-md, ``--open`` + md, ``--open`` + ``--output -``).
       These were previously caught only inside ``_resolve_destination``
       / ``_share_render_and_emit`` — i.e. AFTER ``--sync-current`` had
       already mutated the DB and the snapshot had been built. Surfacing
       them at validation time means an exit-2 flag-shape error never
       triggers side effects.

    Exit 2 with a stderr message naming the offending combo so the
    failure is loud and scriptable. Idempotent; safe to call from every
    share-enabled subcommand before the ``--format`` gate. Existing
    late checks in ``_resolve_destination`` / ``_share_render_and_emit``
    are kept as defense-in-depth for any future caller that bypasses
    this helper.
    """
    if not getattr(args, "format", None):
        offenders = []
        if getattr(args, "output", None):
            offenders.append("--output")
        if getattr(args, "copy", False):
            offenders.append("--copy")
        if getattr(args, "open_after_write", False):
            offenders.append("--open")
        if not offenders:
            return
        verb = "requires" if len(offenders) == 1 else "require"
        sys.stderr.write(
            f"cctally: {', '.join(offenders)} {verb} --format\n"
        )
        sys.exit(2)

    # --format is set — validate destination-shape combos.
    fmt = args.format
    copy = getattr(args, "copy", False)
    output = getattr(args, "output", None)
    open_after_write = getattr(args, "open_after_write", False)

    if copy and output is not None:
        # Mutex: a clipboard destination by definition has no path.
        sys.stderr.write(
            "cctally: --copy is mutually exclusive with --output\n"
        )
        sys.exit(2)
    if copy and fmt != "md":
        sys.stderr.write(
            "cctally: --copy is only valid with --format md\n"
        )
        sys.exit(2)
    if open_after_write and fmt == "md":
        sys.stderr.write(
            "cctally: --open is only valid with --format html or --format svg\n"
        )
        sys.exit(2)
    if open_after_write and output == "-":
        # Open-after-write to stdout has no file to launch — was a silent
        # no-op pre-fix; now an explicit exit 2 so users notice.
        sys.stderr.write(
            "cctally: --open is incompatible with --output - (no file to open)\n"
        )
        sys.exit(2)


def _build_daily_parser(subparsers, name, *, help_text, xref):
    """Build the `daily` leaf parser (issue #86 Session B; routes to cmd_daily).

    Build-once, register-twice: this body is the verbatim former inline `daily`
    construction, parameterized only by `name`, the parent-list `help_text`, and
    the `xref` appended to `description` (renders on `cctally <name> --help`).
    """
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show usage grouped by date, matching upstream ccusage daily output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally daily --since 20260414
              cctally daily --since 20260410 --until 20260416
              cctally daily --since 20260414 --breakdown
              cctally daily --since 20260414 --json
              cctally daily --order desc
              cctally daily --instances
              cctally daily -i --project-aliases repos=Repos
        """),
    )
    p.add_argument(
        "-s", "--since",
        default=None,
        metavar="YYYYMMDD",
        help="Filter from date (inclusive).",
    )
    p.add_argument(
        "-u", "--until",
        default=None,
        metavar="YYYYMMDD",
        help="Filter until date (inclusive).",
    )
    p.add_argument(
        "-b", "--breakdown",
        action="store_true",
        help="Show per-model cost breakdown sub-rows.",
    )
    p.add_argument(
        "-o", "--order",
        choices=("asc", "desc"),
        default="asc",
        help="Sort direction by date (default: asc).",
    )
    p.add_argument(
        "--reveal-projects",
        action="store_true",
        dest="reveal_projects",
        help="In --format output, show real project basenames instead of "
             "the default project-1, project-2, ... anonymization.",
    )
    p.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    p.add_argument(
        "-i", "--instances",
        action="store_true",
        default=False,
        help="Group the report by project (git-root).",
    )
    p.add_argument(
        "-p", "--project",
        action="append",
        default=None,
        metavar="PATTERN",
        help="Filter to projects matching PATTERN (substring of the project "
             "label or path; repeatable, OR semantics).",
    )
    p.add_argument(
        "--project-aliases",
        dest="project_aliases",
        default=None,
        metavar="PAIRS",
        help="Comma-separated key=Label pairs overriding project display "
             "labels (e.g. cctally-dev=Tracker). Display-only.",
    )
    _add_ccusage_alias_args(p, ansi_emit=False)
    _add_mode_arg(p)
    _add_share_args(p)
    p.set_defaults(func=c.cmd_daily)
    return p


def _build_monthly_parser(subparsers, name, *, help_text, xref):
    """Build the `monthly` leaf parser (issue #86 Session B; routes to cmd_monthly)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show usage grouped by calendar month, matching upstream ccusage monthly output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally monthly --since 20260101
              cctally monthly --since 20260101 --until 20260331
              cctally monthly --since 20260101 --breakdown
              cctally monthly --since 20260101 --json
              cctally monthly --order desc
        """),
    )
    p.add_argument(
        "-s", "--since",
        default=None,
        metavar="YYYYMMDD",
        help="Filter from date (inclusive).",
    )
    p.add_argument(
        "-u", "--until",
        default=None,
        metavar="YYYYMMDD",
        help="Filter until date (inclusive).",
    )
    p.add_argument(
        "-b", "--breakdown",
        action="store_true",
        help="Show per-model cost breakdown sub-rows.",
    )
    p.add_argument(
        "-o", "--order",
        choices=("asc", "desc"),
        default="asc",
        help="Sort direction by month (default: asc).",
    )
    p.add_argument(
        "--reveal-projects",
        action="store_true",
        dest="reveal_projects",
        help="In --format output, show real project basenames instead of "
             "the default project-1, project-2, ... anonymization.",
    )
    p.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    _add_ccusage_alias_args(p, ansi_emit=False)
    _add_mode_arg(p)
    _add_share_args(p)
    p.set_defaults(func=c.cmd_monthly)
    return p


def _build_weekly_parser(subparsers, name, *, help_text, xref):
    """Build the `weekly` leaf parser (issue #86 Session B; routes to cmd_weekly)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Claude usage grouped by subscription week. Boundaries are anchored "
                    "to weekly_usage_snapshots.week_start_at with 7-day-cadence extrapolation "
                    "for pre-snapshot history. Columns extend daily/monthly's set with Used % "
                    "and $/1%."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally weekly
              cctally weekly --since 20260101
              cctally weekly --breakdown
              cctally weekly --json
              cctally weekly --order desc
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYYMMDD",
                   help="Filter from date (inclusive).")
    p.add_argument("-u", "--until", default=None, metavar="YYYYMMDD",
                   help="Filter until date (inclusive).")
    p.add_argument("-b", "--breakdown", action="store_true",
                   help="Show per-model cost breakdown sub-rows.")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by week (default: asc).")
    p.add_argument("--reveal-projects", action="store_true", dest="reveal_projects",
                   help="In --format output, show real project basenames instead of "
                        "the default project-1, project-2, ... anonymization.")
    p.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
                   help="Display timezone: local, utc, or IANA name. "
                        "Overrides config display.tz for this call.")
    _add_ccusage_alias_args(p, ansi_emit=False)
    _add_mode_arg(p)
    _add_share_args(p)
    p.set_defaults(func=c.cmd_weekly)
    return p


def _build_session_parser(subparsers, name, *, help_text, xref):
    """Build the `session` leaf parser (issue #86 Session B; routes to cmd_session)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Claude usage grouped by JSONL sessionId. Resumed sessions (same "
                    "sessionId across multiple files) collapse into one row. 11-column "
                    "layout paralleling codex-session."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally session
              cctally session --since 20260401
              cctally session --since 20260401 --breakdown
              cctally session --json
              cctally session --order desc
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYYMMDD",
                   help="Filter from date (inclusive).")
    p.add_argument("-u", "--until", default=None, metavar="YYYYMMDD",
                   help="Filter until date (inclusive).")
    p.add_argument("-b", "--breakdown", action="store_true",
                   help="Show per-model cost breakdown sub-rows.")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by last activity (default: asc — earliest first).")
    p.add_argument("--reveal-projects", action="store_true", dest="reveal_projects",
                   help="In --format output, show real project basenames instead of "
                        "the default project-1, project-2, ... anonymization.")
    p.add_argument("--top-n", type=int, default=15, dest="top_n",
                   metavar="N",
                   help="In --format output, cap rows to top N by cost (default: 15). "
                        "Must be >= 1; values above 50 emit a readability warning. "
                        "Has no effect on terminal/JSON output.")
    p.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
                   help="Display timezone: local, utc, or IANA name. "
                        "Overrides config display.tz for this call.")
    p.add_argument(
        "-i", "--id", default=None, metavar="SESSION_ID", dest="id",
        help="Filter to a single session by exact-string sessionId. "
             "Match is against the post-resume-merge id (sessions "
             "resumed across multiple JSONL files collapse to one id). "
             "Unknown id → exit 0 with the empty-render branch.",
    )
    _add_ccusage_alias_args(p, ansi_emit=False)
    _add_mode_arg(p)
    _add_share_args(p)
    p.set_defaults(func=c.cmd_session)
    return p


def _build_blocks_parser(subparsers, name, *, help_text, xref):
    """Build the `blocks` leaf parser (issue #86 Session B; routes to cmd_blocks).

    Note: `blocks` intentionally has NO `_add_share_args` (matches the former
    inline block — it is not part of the shareable-output flag surface).
    """
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show usage grouped by 5-hour session blocks, matching upstream ccusage blocks output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally blocks --since 20260414
              cctally blocks --since 20260410 --until 20260416
              cctally blocks --since 20260414 --breakdown
              cctally blocks --since 20260414 --json
        """),
    )
    p.add_argument(
        "-s", "--since",
        default=None,
        metavar="YYYYMMDD",
        help="Filter from date (inclusive).",
    )
    p.add_argument(
        "-u", "--until",
        default=None,
        metavar="YYYYMMDD",
        help="Filter until date (inclusive).",
    )
    p.add_argument(
        "-b", "--breakdown",
        action="store_true",
        help="Show per-model cost breakdown.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json",
        help="Output JSON matching upstream ccusage blocks format.",
    )
    p.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    p.add_argument(
        "-a", "--active", action="store_true",
        help="Show only the active block, with burn-rate + projection "
             "(ccusage drop-in).",
    )
    p.add_argument(
        "-r", "--recent", action="store_true",
        help="Show only blocks from the last 3 days (plus the active block).",
    )
    p.add_argument(
        "-t", "--token-limit", dest="token_limit", default=None,
        metavar="N|max",
        help="Token limit for the quota %% column / projection warnings. "
             "An integer, or 'max' (default) to derive from the largest "
             "completed block.",
    )
    p.add_argument(
        "-n", "--session-length", dest="session_length", type=float,
        default=5.0, metavar="N",
        help="Accepted for ccusage drop-in compat; no-op — cctally blocks "
             "follow Anthropic's real 5-hour resets and are not re-sizable. "
             "A value <= 0 is rejected.",
    )
    _add_ccusage_alias_args(p, ansi_emit=False)
    _add_mode_arg(p)
    p.set_defaults(func=c.cmd_blocks)
    return p


def _build_statusline_parser(subparsers, name, *, help_text, xref):
    """Build the `statusline` (or `claude statusline`) leaf parser.

    Registered TWICE per the Session B build-once register-twice pattern:
    once on the flat ``cctally statusline`` subparser, once under the
    nested ``cctally claude statusline`` subgroup. Output is byte-identical
    between the two forms; only ``--help`` text differs (the ``xref``
    paragraph appended to ``description``).
    """
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description=(
            "Display a compact one-line status for Claude Code hooks "
            "(ccusage drop-in + cctally extensions).\n\n" + xref
        ),
    )
    # ccusage-shape flags
    p.add_argument(
        "-B", "--visual-burn-rate",
        dest="visual_burn_rate",
        default=None,
        choices=["off", "emoji", "text", "emoji-text"],
        help="Burn-rate visualization (default: off; config key "
             "statusline.visual_burn_rate).",
    )
    # NOTE: `ccusage` is intentionally NOT in `choices=` so it doesn't
    # appear in `--help` advertised options. Argparse runs the `choices`
    # check BEFORE the action's `__call__`, so we cannot list `ccusage`
    # in `choices=` AND catch it in the action. Instead we omit `choices=`
    # entirely, manually validate inside the action, and re-raise the
    # spec's rename hint via `parser.error` for `ccusage` specifically.
    # Help text below hardcodes the legal set so users still see it in
    # `--help`. A typo like `ccussage` falls through to a standard
    # argparse-style "invalid choice" error from `parser.error`.
    class _CostSourceAction(argparse.Action):
        _ACCEPTED = ("auto", "cctally", "cc", "both")
        _RENAMED = "ccusage"

        def __call__(self, parser, namespace, values, option_string=None):
            if values == self._RENAMED:
                parser.error(
                    f"argument {option_string}: invalid choice: "
                    f"{values!r} — cctally renamed it; try "
                    f"--cost-source cctally"
                )
            if values not in self._ACCEPTED:
                parser.error(
                    f"argument {option_string}: invalid choice: "
                    f"{values!r} (choose from "
                    + ", ".join(repr(c) for c in self._ACCEPTED)
                    + ")"
                )
            setattr(namespace, self.dest, values)

    p.add_argument(
        "--cost-source",
        dest="cost_source",
        default=None,
        action=_CostSourceAction,
        metavar="{auto,cctally,cc,both}",
        help="Session cost source (default: auto; config key "
             "statusline.cost_source). Note: 'ccusage' errors with a "
             "rename hint — use 'cctally' instead.",
    )
    p.add_argument(
        "--cache",
        dest="cache",
        action="store_true",
        default=None,
        help="Accepted for ccusage drop-in compat; cctally renders from "
             "cache.db directly without an extra output cache.",
    )
    p.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        help="(no-op alias)",
    )
    p.add_argument(
        "--refresh-interval",
        dest="refresh_interval",
        default=1,
        type=int,
        metavar="N",
        help="(no-op alias) Accepted for ccusage drop-in compat.",
    )
    p.add_argument(
        "--context-low-threshold",
        dest="context_low_threshold",
        default=50,
        type=int,
        metavar="N",
        help="Below this %% → segment 4 green (default: 50, 0-100).",
    )
    p.add_argument(
        "--context-medium-threshold",
        dest="context_medium_threshold",
        default=80,
        type=int,
        metavar="N",
        help="Below this %% → segment 4 yellow; else red (default: 80, 0-100).",
    )
    p.add_argument(
        "-z", "--timezone",
        dest="timezone",
        default=None,
        metavar="TZ",
        help="Display tz (IANA) for `today` calendar day. Overrides "
             "display.tz config.",
    )
    p.add_argument(
        "-O", "--offline",
        dest="offline",
        action="store_true",
        default=True,
        help="(no-op alias) cctally is always offline.",
    )
    p.add_argument(
        "--no-offline",
        dest="offline",
        action="store_false",
        help="(no-op alias)",
    )
    p.add_argument(
        "--color",
        dest="color",
        action="store_true",
        default=None,
        help="Force ANSI colors on (default: auto via NO_COLOR + TTY).",
    )
    p.add_argument(
        "--no-color",
        dest="color",
        action="store_false",
        help="Force ANSI colors off.",
    )
    p.add_argument(
        "--cctally-extensions",
        dest="cctally_extensions",
        action="store_true",
        default=None,
        help="Append cctally 5h%%/7d%% segment (default: on; config key "
             "statusline.cctally_extensions).",
    )
    p.add_argument(
        "--no-cctally-extensions",
        dest="cctally_extensions",
        action="store_false",
        help="Suppress cctally 5h%%/7d%% segment.",
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        metavar="PATH",
        help="Read config from PATH for this invocation only (no "
             "mutation of the default config). Missing/invalid PATH "
             "exits 2.",
    )
    p.add_argument(
        "--single-thread",
        dest="single_thread",
        action="store_true",
        help="(no-op alias) cctally is always single-threaded via the "
             "session-entry cache.",
    )
    p.add_argument(
        "-d", "--debug",
        dest="debug",
        action="store_true",
        help="Emit pricing-mismatch / config diagnostics on stderr.",
    )
    p.set_defaults(func=c.cmd_statusline, command=name)
    return p


def _build_codex_daily_parser(subparsers, name, *, help_text, xref):
    """Build the `codex-daily` leaf parser (issue #86 Session B; routes to cmd_codex_daily)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Codex usage grouped by date, matching upstream ccusage-codex daily output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally codex-daily --since 20260401
              cctally codex-daily --since 20260401 --breakdown
              cctally codex-daily --since 20260401 --json
              cctally codex-daily --order desc
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYY-MM-DD",
                   help="Filter from date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-u", "--until", default=None, metavar="YYYY-MM-DD",
                   help="Filter until date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-b", "--breakdown", action="store_true",
                   help="Show per-model cost breakdown sub-rows.")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by date (default: asc).")
    p.add_argument("--json", action="store_true", dest="json",
                   help="Output JSON matching upstream ccusage-codex daily format.")
    _add_codex_shared_args(p)
    p.set_defaults(func=c.cmd_codex_daily)
    return p


def _build_codex_monthly_parser(subparsers, name, *, help_text, xref):
    """Build the `codex-monthly` leaf parser (issue #86 Session B; routes to cmd_codex_monthly)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Codex usage grouped by calendar month, matching upstream ccusage-codex monthly output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally codex-monthly --since 20260101
              cctally codex-monthly --breakdown
              cctally codex-monthly --json
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYY-MM-DD",
                   help="Filter from date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-u", "--until", default=None, metavar="YYYY-MM-DD",
                   help="Filter until date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-b", "--breakdown", action="store_true",
                   help="Show per-model cost breakdown sub-rows.")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by month (default: asc).")
    p.add_argument("--json", action="store_true", dest="json",
                   help="Output JSON matching upstream ccusage-codex monthly format.")
    _add_codex_shared_args(p)
    p.set_defaults(func=c.cmd_codex_monthly)
    return p


def _build_codex_weekly_parser(subparsers, name, *, help_text, xref):
    """Build the `codex-weekly` leaf parser (issue #86 Session B; routes to cmd_codex_weekly)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Codex usage grouped by week. Week-start day is read from config.json "
                    "(collector.week_start, Monday default). Not a ccusage-codex drop-in — "
                    "upstream has no `codex weekly` command."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally codex-weekly
              cctally codex-weekly --since 20260301
              cctally codex-weekly --breakdown
              cctally codex-weekly --json
              cctally codex-weekly --order desc
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYY-MM-DD",
                   help="Filter from date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-u", "--until", default=None, metavar="YYYY-MM-DD",
                   help="Filter until date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-b", "--breakdown", action="store_true",
                   help="Show per-model cost breakdown sub-rows.")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by week (default: asc).")
    p.add_argument("--json", action="store_true", dest="json",
                   help="Output JSON.")
    _add_codex_shared_args(p)
    p.set_defaults(func=c.cmd_codex_weekly)
    return p


def _build_codex_session_parser(subparsers, name, *, help_text, xref):
    """Build the `codex-session` leaf parser (issue #86 Session B; routes to cmd_codex_session)."""
    c = _cctally()
    p = subparsers.add_parser(
        name,
        help=help_text,
        formatter_class=CLIHelpFormatter,
        description="Show Codex usage grouped by session, matching upstream ccusage-codex session output."
                    "\n\n" + xref,
        epilog=textwrap.dedent("""\
            Examples:
              cctally codex-session
              cctally codex-session --since 20260401
              cctally codex-session --json
        """),
    )
    p.add_argument("-s", "--since", default=None, metavar="YYYY-MM-DD",
                   help="Filter from date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-u", "--until", default=None, metavar="YYYY-MM-DD",
                   help="Filter until date (inclusive; accepts YYYY-MM-DD or YYYYMMDD).")
    p.add_argument("-o", "--order", choices=("asc", "desc"), default="asc",
                   help="Sort direction by last activity (default: asc — earliest first).")
    p.add_argument("--json", action="store_true", dest="json",
                   help="Output JSON matching upstream ccusage-codex session format.")
    _add_codex_shared_args(p)
    p.set_defaults(func=c.cmd_codex_session)
    return p


def build_parser() -> argparse.ArgumentParser:
    c = _cctally()
    p = argparse.ArgumentParser(
        prog="cctally",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Track Claude subscription weekly usage percent and weekly cost
            in a local SQLite database.

            Data flow:
              1) Claude Code status line captures rate limit data after each API call.
              2) record-usage stores usage snapshots and triggers percent milestones.
              3) sync-week computes weekly USD cost from Claude Code session data.
              4) report computes dollars per 1% and shows trend history.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Quick start:
              # Add record-usage call to ~/.claude/statusline-command.sh (see record-usage --help)
              cctally sync-week
              cctally report
            """
        ),
    )
    p.add_argument(
        "-v", "--version",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print cctally version (from CHANGELOG.md latest release header) and exit",
    )
    sub = p.add_subparsers(
        dest="command",
        required=False,
        title="commands",
        metavar="<command>",
    )

    py = sub.add_parser(
        "sync-week",
        help="Compute weekly cost from session data and store in SQLite",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Compute and store weekly cost (USD) for a selected week window.

            Week selection priority:
              1) Explicit --week-start/--week-end (date based)
              2) Latest usage snapshot weekStartAt/weekEndAt (hour-accurate)
              3) Current week from configured week-start rule
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally sync-week
              cctally sync-week --week-start 2026-02-05 --week-end 2026-02-12
              cctally sync-week --mode calculate --offline --json
            """
        ),
    )
    py.add_argument(
        "--week-start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Explicit week start date. If --week-end is omitted, uses start + 6 days.",
    )
    py.add_argument(
        "--week-end",
        default=None,
        metavar="YYYY-MM-DD",
        help="Explicit week end date (inclusive date for custom windows).",
    )
    py.add_argument(
        "--week-start-name",
        default=None,
        choices=list(WEEKDAY_MAP.keys()),
        help="Week-start day used when explicit/custom boundaries are not available.",
    )
    py.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "calculate", "display"],
        help="Cost calculation mode: auto, calculate, or display.",
    )
    py.add_argument(
        "--offline",
        action="store_true",
        help="Use embedded pricing data (no-op, always used).",
    )
    py.add_argument(
        "--project",
        default=None,
        help="Optional project filter for cost calculation.",
    )
    py.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    py.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress human-readable output (no effect with --json).",
    )
    py.set_defaults(func=c.cmd_sync_week)

    pr = sub.add_parser(
        "report",
        help="Show current and trend dollars-per-1%% statistics",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Report current and historical dollars per 1% weekly usage.

            For each week, report joins:
              - latest usage snapshot (%)
              - latest cost snapshot (USD)
            then computes USD / percent.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally report
              cctally report --sync-current
              cctally report --weeks 12 --json
            """
        ),
    )
    pr.add_argument(
        "--weeks",
        type=int,
        default=8,
        help="How many recent week windows to include in the trend.",
    )
    pr.add_argument(
        "--sync-current",
        action="store_true",
        help="Run sync-week first, then generate the report.",
    )
    pr.add_argument(
        "--week-start-name",
        default=None,
        choices=list(WEEKDAY_MAP.keys()),
        help="Week-start day used if report falls back to date-only week logic.",
    )
    pr.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "calculate", "display"],
        help="Mode passed to sync-week when --sync-current is used.",
    )
    pr.add_argument(
        "--offline",
        action="store_true",
        help="Pass --offline to sync-week when --sync-current is used.",
    )
    pr.add_argument(
        "--project",
        default=None,
        help="Project filter passed to sync-week when --sync-current is used.",
    )
    pr.add_argument(
        "--reveal-projects",
        action="store_true",
        dest="reveal_projects",
        help="In --format output, show real project basenames instead of "
             "the default project-1, project-2, ... anonymization.",
    )
    pr.add_argument(
        "--detail",
        action="store_true",
        help="Include per-percent cost milestones for the current week.",
    )
    pr.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    _add_share_args(pr)
    pr.set_defaults(func=c.cmd_report)

    fc = sub.add_parser(
        "forecast",
        help="Project current-week usage to reset; show daily budgets",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Forecast end-of-week usage % and daily $ / % budgets to stay under
            target ceilings (default 100% and 90%). Reads current-week
            `weekly_usage_snapshots` + `session_entries`; never writes.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally forecast
              cctally forecast --json
              cctally forecast --status-line --no-sync
              cctally forecast --targets 100,95,85

            Status-line integration (add to ~/.claude/statusline-command.sh):
              forecast_seg=$(cctally forecast --status-line --no-sync 2>/dev/null)
              # ...then include "$forecast_seg" in your prompt composition.
            """
        ),
    )
    fc.add_argument("--reveal-projects", action="store_true", dest="reveal_projects",
                    help="In --format output, show real project basenames instead of "
                         "the default project-1, project-2, ... anonymization.")
    fc.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
                    help="Display timezone: local, utc, or IANA name. "
                         "Overrides config display.tz for this call.")
    fc.add_argument("--targets", default="100,90",
                    help="Comma-separated integer ceilings (default: 100,90).")
    fc.add_argument("--explain", action="store_true",
                    help="Append rationale footer with rate values and source captions.")
    fc.add_argument("--no-sync", action="store_true", dest="no_sync",
                    help="Skip sync_cache(); recommended for status-line use.")
    fc.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="Color output control (also honors NO_COLOR).")
    # Dev-only: override "now" for deterministic fixture tests. Hidden from --help.
    fc.add_argument("--as-of", dest="as_of", default=None, help=argparse.SUPPRESS)
    _add_share_args(fc, has_status_line=True)
    fc.set_defaults(func=c.cmd_forecast)

    # budget — cctally-original (NOT a ccusage drop-in), so flat surface only;
    # no claude/codex subgroup. `--config` is honored read-only on bare status
    # but rejected on set/unset (F4). `--reveal-projects` is accepted for share
    # surface parity but inert (no per-project axis). `--tz` follows the sibling
    # reporting commands' precedence.
    bg = sub.add_parser(
        "budget",
        help="Weekly equivalent-$ budget + pace + spend alerts",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Track Claude equivalent-$ spend for the current subscription week
            against a weekly budget. Shows spend, pace, projected end-of-week,
            and a verdict (ok / warn / over). `budget set <amount>` and
            `budget unset` manage the budget; spend-crossing alerts fire from
            record-usage (see `cctally alerts`).
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally budget
              cctally budget set 300
              cctally budget unset
              cctally budget set 25 --project
              cctally budget --json
              cctally budget --format md
            """
        ),
    )
    bg.add_argument("action", nargs="?", choices=["set", "unset"], default=None,
                    help="`set <amount>` to set the weekly budget, `unset` to clear it.")
    bg.add_argument("amount", nargs="?", default=None,
                    help="Target USD for `budget set` (e.g. 300).")
    bg.add_argument("--config", default=None,
                    help="Read status from this config file (read-only; "
                         "rejected on set/unset).")
    bg.add_argument(
        "--project", nargs="?", const="__CWD__", default=None,
        help="Set/unset a per-project budget for this git repo "
             "(bare = current directory's git-root; or pass a path).")
    bg.add_argument(
        "--vendor", choices=["claude", "codex"], default="claude",
        help="Which vendor budget to set/unset (default claude). Codex "
             "budgets are calendar-period only.")
    bg.add_argument(
        "--period",
        # Accept both canonical and short spellings; the command handler
        # normalizes short->canonical and rejects `--vendor codex
        # --period subscription-week` (Codex has no Anthropic week). The choices
        # are single-sourced from `_BUDGET_PERIOD_CHOICES` (derived from the same
        # short→canonical map the normalizer uses), so they can't drift from the
        # handler (code-review #5).
        choices=c._BUDGET_PERIOD_CHOICES,
        default=None,
        help="Budget period: subscription-week (claude only) / calendar-week "
             "/ calendar-month. Default: preserve the stored period, else the "
             "per-vendor default (claude=subscription-week, codex="
             "calendar-month).")
    bg.add_argument("--reveal-projects", action="store_true", dest="reveal_projects",
                    help="Show real project basenames in the per-project section "
                         "of --format output (default anonymizes to project-1/…).")
    bg.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
                    help="Display timezone: local, utc, or IANA name. "
                         "Overrides config display.tz for this call.")
    _add_share_args(bg)
    bg.set_defaults(func=c.cmd_budget)

    pb = sub.add_parser(
        "percent-breakdown",
        help="Show per-percent cost milestones for a week",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Show the cumulative and marginal cost at each integer percent threshold
            for a given week. Milestones are recorded automatically when
            record-usage stores a snapshot crossing a new integer percent.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally percent-breakdown
              cctally percent-breakdown --week-start 2026-03-20
              cctally percent-breakdown --json
            """
        ),
    )
    pb.add_argument(
        "--week-start",
        default=None,
        metavar="YYYY-MM-DD",
        help="Week start date. Defaults to the current week.",
    )
    pb.add_argument(
        "--week-start-name",
        default=None,
        choices=list(WEEKDAY_MAP.keys()),
        help="Week-start day used when no explicit date or usage data is available.",
    )
    pb.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    pb.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    pb.set_defaults(func=c.cmd_percent_breakdown)

    fhbd = sub.add_parser(
        "five-hour-breakdown",
        help="Per-percent milestones inside one 5h block (mirror of percent-breakdown)",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Show cumulative + marginal cost at each integer percent threshold
            inside one 5h block. Mirrors percent-breakdown for the 5h axis.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally five-hour-breakdown
              cctally five-hour-breakdown --block-start 2026-04-30T19:30
              cctally five-hour-breakdown --ago 1
              cctally five-hour-breakdown --json
            """
        ),
    )
    fhbd.add_argument(
        "--block-start",
        default=None,
        metavar="ISO8601",
        dest="block_start",
        help="Block start (e.g. 2026-04-30T19:30, naive=UTC).",
    )
    fhbd.add_argument(
        "--ago",
        default=None,
        type=int,
        metavar="N",
        help="Relative selector: 0=current, 1=previous, etc.",
    )
    fhbd.add_argument(
        "--json",
        action="store_true",
        help="Emit camelCase JSON (schemaVersion 1).",
    )
    fhbd.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output (currently a no-op — table is plain text).",
    )
    fhbd.add_argument(
        "--tz",
        default=None,
        type=_argparse_tz,
        metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    fhbd.set_defaults(func=c.cmd_five_hour_breakdown)

    tp = sub.add_parser(
        "tui",
        help="Live refreshing dashboard (current week, forecast, trend, sessions)",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Live terminal dashboard with four refreshing panels:
              - Current week % and 5-hour window
              - Forecast verdict + projections + daily $ budgets
              - $/1% trend over the last 8 weeks (with sparkline)
              - Recent Claude sessions (last 100, scrollable)

            Two visual variants — conventional 2x2 grid and expressive
            hero layout — toggleable at runtime with `v`.

            Requires the `rich` Python package.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally tui
              cctally tui --expressive
              cctally tui --refresh 2 --sync-interval 30
              cctally tui --no-sync
            """
        ),
    )
    tp.add_argument(
        "--variant",
        choices=("conventional", "expressive"),
        default="conventional",
        help="Initial layout variant (press 'v' at runtime to toggle).",
    )
    tp.add_argument(
        "--expressive",
        action="store_const",
        dest="variant",
        const="expressive",
        help="Shortcut for --variant expressive.",
    )
    tp.add_argument(
        "--refresh",
        type=c._tui_refresh_interval_type,
        default=1.0,
        metavar="SECONDS",
        help="UI redraw cadence (default: 1.0).",
    )
    tp.add_argument(
        "--sync-interval",
        type=c._tui_sync_interval_type,
        default=10.0,
        metavar="SECONDS",
        dest="sync_interval",
        help="Background JSONL sync cadence (default: 10).",
    )
    tp.add_argument(
        "--no-sync",
        action="store_true",
        dest="no_sync",
        help="Disable background sync; render from cache only.",
    )
    tp.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        help="Disable ANSI color (NO_COLOR env var also respected).",
    )
    tp.add_argument(
        "--tz",
        default=None,
        type=_argparse_tz,
        metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    # Dev-only: pin "now" for deterministic fixture tests.
    tp.add_argument("--as-of", dest="as_of", default=None, help=argparse.SUPPRESS)
    # Dev-only: fixture injection — render one frame from a Python module that
    # exposes `SNAPSHOT` (DataSnapshot) and exits.
    tp.add_argument(
        "--snapshot-module", dest="snapshot_module", default=None, help=argparse.SUPPRESS
    )
    # Dev-only: one-shot render for golden capture.
    tp.add_argument(
        "--render-once", action="store_true", dest="render_once", help=argparse.SUPPRESS
    )
    # Dev-only: force terminal size for --render-once.
    tp.add_argument(
        "--force-size", dest="force_size", default=None, metavar="WxH",
        help=argparse.SUPPRESS
    )
    tp.set_defaults(func=c.cmd_tui)

    # ---- dashboard subcommand --------------------------------------------
    dp = sub.add_parser(
        "dashboard",
        help="Launch the live web dashboard on http://localhost:8789",
        description=(
            "Start a local web server rendering a live dashboard of your "
            "subscription usage, weekly cost trend, and recent sessions. "
            "Press Ctrl-C to stop. Two variants are served by the companion "
            "'tui' subcommand for terminal-only use."
        ),
    )
    dp.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port to bind (default: 8789; 8790 under the preview channel).",
    )
    dp.add_argument(
        "--host",
        default=None,
        help=("Bind host (default: from config dashboard.bind, fallback "
              "127.0.0.1 — loopback-only). Use --host 0.0.0.0 to opt in "
              "to LAN exposure (no auth on /api/* — trusted networks only)."),
    )
    dp.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip auto-opening the browser to the dashboard URL.",
    )
    dp.add_argument(
        "--sync-interval",
        type=c._tui_sync_interval_type,  # reuse the TUI validator
        default=5.0,
        dest="sync_interval",
        help="Background snapshot-rebuild cadence in seconds (default: 5).",
    )
    dp.add_argument(
        "--no-sync",
        action="store_true",
        dest="no_sync",
        help="Freeze the snapshot at startup; skip background rebuilds.",
    )
    dp.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    dp.set_defaults(func=c.cmd_dashboard)

    ru = sub.add_parser(
        "record-usage",
        help="Record usage data from Claude Code status line",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Record usage percentage from Claude Code status line rate_limits data.
            Called automatically by the status line script after each assistant message.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally record-usage --percent 14.2 --resets-at 1744531200
              cctally record-usage --percent 14.2 --resets-at 1744531200 \\
                --five-hour-percent 38.5 --five-hour-resets-at 1744502400

            Status line integration (add to ~/.claude/statusline-command.sh):
              if [ -n "$week_pct" ] && [ -n "$week_resets" ]; then
                  record_args="--percent $week_pct --resets-at ${week_resets%.*}"
                  if [ -n "$five_pct" ] && [ -n "$five_resets" ]; then
                      record_args="$record_args --five-hour-percent $five_pct --five-hour-resets-at ${five_resets%.*}"
                  fi
                  cctally record-usage $record_args &
              fi
            """
        ),
    )
    ru.add_argument(
        "--percent",
        required=True,
        type=float,
        help="7-day utilization percentage (0-100).",
    )
    ru.add_argument(
        "--resets-at",
        required=True,
        help="7-day window reset timestamp (Unix epoch seconds).",
    )
    ru.add_argument(
        "--five-hour-percent",
        type=float,
        default=None,
        help="5-hour utilization percentage (0-100).",
    )
    ru.add_argument(
        "--five-hour-resets-at",
        default=None,
        help="5-hour window reset timestamp (Unix epoch seconds).",
    )
    ru.set_defaults(func=c.cmd_record_usage)

    rc = sub.add_parser(
        "record-credit",
        help="Record an in-place weekly credit the auto-detector misses",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Record an in-place weekly (7d) credit that the auto-detector
            misses (a sub-25pp, non-zero drop — e.g. Anthropic lowered your
            7d % from 46 to 31 without a clean reset). Writes a
            weekly_credit_floors clamp row (no week re-anchor — the same week
            continues), lowers hwm-7d, and inserts a post-credit snapshot so
            reports and the statusline read the credited value.
            Preview + confirm by default.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally record-credit --to 31              # baseline auto-read from HWM
              cctally record-credit --to 31 --dry-run    # preview, write nothing
              cctally record-credit --to 31 --yes        # apply without prompting

            Exit codes: 0 success (incl. --dry-run and an interactive decline),
            2 validation/refuse, 3 on a database error.
            """
        ),
    )
    rc.add_argument("--to", required=True, type=float,
                    help="New post-credit weekly %% (0-100).")
    rc.add_argument("--from", dest="from_pct", metavar="FROM", type=float, default=None,
                    help="Pre-credit baseline %% (default: current HWM for the week).")
    rc.add_argument("--at", default=None,
                    help="Effective credit moment (ISO; naive=UTC; default now).")
    rc.add_argument("--week", default=None,
                    help="week_start_date YYYY-MM-DD (default: current week).")
    rc.add_argument("--dry-run", action="store_true",
                    help="Preview only; write nothing.")
    rc.add_argument("--yes", action="store_true",
                    help="Apply without the confirm prompt.")
    rc.add_argument("--force", action="store_true",
                    help="Re-record when a credit is already fully recorded for the week.")
    rc.add_argument("--json", action="store_true",
                    help="Machine output (schemaVersion 1).")
    rc.set_defaults(func=c.cmd_record_credit)

    rfu = sub.add_parser(
        "refresh-usage",
        help="Force-fetch 7d/5h percent from OAuth API and record it",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Force a fresh fetch of seven_day.utilization and five_hour.utilization
            from Anthropic's OAuth usage API, persist it via the same path
            record-usage uses (HWM, percent_milestones, weekly_usage_snapshots),
            and bust the statusline OAuth cache file at
            /tmp/claude-statusline-usage-cache.json so the next status-line tick
            also gets fresh data.

            Use this when the displayed 7d percent is stale (e.g., you've
            been away from Claude Code and the status-line hasn't fired
            recently). Otherwise the status-line script handles refresh
            automatically every minute.
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              ccusage-refresh-usage                  # one-liner output
              ccusage-refresh-usage --json | jq .    # scriptable
              ccusage-refresh-usage --quiet          # silent (exit code only)

            Exit codes: 0 success / 2 no OAuth token / 3 network failure
            / 4 malformed API response / 5 record-usage internal failure.
            """
        ),
    )
    rfu.add_argument("--json", action="store_true",
                     help="Emit schema_version=1 JSON to stdout instead of one-liner.")
    rfu.add_argument("--quiet", action="store_true",
                     help="Suppress stdout; exit code is the only success signal.")
    rfu.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                     help="Color output control (also honors NO_COLOR).")
    rfu.add_argument("--timeout", type=float, default=5.0,
                     help="HTTP timeout in seconds (default: 5.0).")
    rfu.set_defaults(func=c.cmd_refresh_usage)

    pc = sub.add_parser(
        "cache-report",
        help="Show daily cache hit rates per model from ccusage data",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Query ccusage for daily token breakdown and display cache hit
            percentages per model. Useful for spotting caching regressions
            after Claude Code updates.

            Cache hit % = cacheReadTokens / (input + cacheCreate + cacheRead)
            """
        ),
        epilog=textwrap.dedent(
            """\
            Examples:
              cctally cache-report
              cctally cache-report --days 14
              cctally cache-report --since 2026-04-10 --until 2026-04-18
              cctally cache-report --by-session --days 14
              cctally cache-report --by-session --sort cache
              cctally cache-report --json
            """
        ),
    )
    pc.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of recent days to include.",
    )
    pc.add_argument(
        "--since",
        default=None,
        help="Lower window bound (ISO 8601, e.g., '2026-04-10' or "
             "'2026-04-10T10:00:00Z'). If omitted, falls back to --days.",
    )
    pc.add_argument(
        "--until",
        default=None,
        help="Upper window bound (ISO 8601). If omitted, defaults to now.",
    )
    pc.add_argument(
        "--by-session",
        action="store_true",
        dest="by_session",
        help="Group by Claude sessionId (resumed-merged) instead of by date. "
             "Adds SessionId, Last Activity, and Project identity columns.",
    )
    pc.add_argument(
        "-O", "--offline",
        action=argparse.BooleanOptionalAction, default=False,
        help="Use cached pricing data in ccusage. Session A (spec §7.1.2)"
             " promotes the existing flag to BooleanOptionalAction + -O"
             " short form so the ccusage drop-in alias surface (-O,"
             " --offline, --no-offline) all work on cache-report; the"
             " behavior under each is unchanged (cctally is always"
             " offline — args.offline still lands as a bool).",
    )
    pc.add_argument(
        "--project",
        default=None,
        help="Filter to a specific project.",
    )
    pc.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    pc.add_argument(
        "--anomaly-threshold-pp",
        type=int,
        default=15,
        dest="anomaly_threshold_pp",
        help="Cache%% drop threshold (percentage points) vs. trailing-median "
             "baseline for the cache_drop anomaly trigger. Default: 15.",
    )
    pc.add_argument(
        "--anomaly-window-days",
        type=int,
        default=14,
        dest="anomaly_window_days",
        help="Trailing window (days) for baseline median computation. "
             "Default: 14.",
    )
    pc.add_argument(
        "--no-anomaly",
        action="store_true",
        dest="no_anomaly",
        help="Disable all anomaly triggers (both cache_drop and net_negative).",
    )
    pc.add_argument(
        "--sort",
        choices=["date", "net", "cache", "recent", "cost", "anomaly"],
        default=None,
        dest="sort",
        help="Override sort order. Defaults: 'date' in daily mode, 'net' in "
             "--by-session mode.",
    )
    pc.add_argument(
        "--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    # Session A (spec §7.6): ansi_emit=False; existing `--offline` is
    # skipped by the helper's `_argparse_has_arg` guard (the collision
    # case spec §7.1.2 calls out explicitly).
    _add_ccusage_alias_args(pc, ansi_emit=False)
    pc.set_defaults(func=c.cmd_cache_report)

    # -- range-cost --
    rc = sub.add_parser(
        "range-cost",
        help="Compute USD cost for a time range from session data",
        formatter_class=CLIHelpFormatter,
        description="Compute USD cost for Claude Code usage between start/end timestamps.",
        epilog=textwrap.dedent("""\
            Examples:
              cctally range-cost -s "2026-04-10T10:00:00+03:00"
              cctally range-cost -s "2026-04-10T10:00:00Z" -e "2026-04-12T10:00:00Z" --breakdown
              cctally range-cost -s "2026-04-10T10:00:00Z" --json
              cctally range-cost -s "2026-04-10T10:00:00Z" --total-only
        """),
    )
    rc.add_argument(
        "-s", "--start",
        required=True,
        help="Start timestamp (ISO 8601)",
    )
    rc.add_argument(
        "-e", "--end",
        default=None,
        help="End timestamp (ISO 8601, default: now)",
    )
    rc.add_argument(
        "-m", "--mode",
        default="auto",
        choices=["auto", "calculate", "display"],
        help="Cost calculation mode.",
    )
    rc.add_argument(
        "-p", "--project",
        default=None,
        help="Filter to a specific project.",
    )
    rc.add_argument(
        "-b", "--breakdown",
        action="store_true",
        help="Show per-model usage and cost breakdown.",
    )
    rc.add_argument(
        "--json",
        action="store_true",
        dest="json",
        help="Output JSON.",
    )
    rc.add_argument(
        "--total-only",
        action="store_true",
        dest="total_only",
        help="Print numeric USD total only.",
    )
    # Session A (spec §7.6): ansi_emit=False. range-cost has no --tz of
    # its own (ISO timestamps carry zone info), but the helper-added
    # -z/--timezone still lands on the namespace; the bridge promotes
    # it onto args.tz where the rest of the pipeline treats it as a
    # documented no-op (cmd_range_cost does not consume args.tz).
    _add_ccusage_alias_args(rc, ansi_emit=False)
    rc.set_defaults(func=c.cmd_range_cost)

    # -- blocks --
    _build_blocks_parser(
        sub, "blocks",
        help_text="Show usage report grouped by 5-hour session blocks",
        xref="Alias of `cctally claude blocks` (the canonical form).")

    # -- statusline --
    _build_statusline_parser(
        sub, "statusline",
        help_text="Compact one-line status for Claude Code hooks",
        xref="Alias of `cctally claude statusline` (the canonical form).")

    # -- five-hour-blocks --
    fhb = sub.add_parser(
        "five-hour-blocks",
        help="List API-anchored 5h blocks with rollup totals + 7d-drift columns",
        formatter_class=CLIHelpFormatter,
        description=(
            "Show usage grouped by API-anchored 5-hour blocks (analytics view, "
            "distinct from `cctally blocks` upstream-parity drop-in)."
        ),
        epilog=textwrap.dedent("""\
            Examples:
              cctally five-hour-blocks
              cctally five-hour-blocks --since 20260420
              cctally five-hour-blocks --breakdown model
              cctally five-hour-blocks --breakdown project --json
        """),
    )
    fhb.add_argument(
        "-s", "--since",
        default=None,
        metavar="YYYYMMDD",
        help="Filter from date (inclusive).",
    )
    fhb.add_argument(
        "-u", "--until",
        default=None,
        metavar="YYYYMMDD",
        help="Filter until date (inclusive).",
    )
    fhb.add_argument(
        "--breakdown",
        choices=("model", "project"),
        default=None,
        help="Add per-axis rollup-child rows under each block.",
    )
    fhb.add_argument(
        "--reveal-projects",
        action="store_true",
        dest="reveal_projects",
        help="In --format output, show real project basenames instead of "
             "the default project-1, project-2, ... anonymization.",
    )
    fhb.add_argument(
        "--no-color",
        action="store_true",
        help="Accepted for ccusage drop-in compat; this command emits "
             "plain-text output and no ANSI is suppressed.",
    )
    fhb.add_argument(
        "--tz",
        default=None,
        type=_argparse_tz,
        metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.",
    )
    # Session A (spec §7.6 / §7.6.3): ansi_emit=False. fhb already
    # declares --no-color (refreshed to no-op text in spec §7.6.3); the
    # helper's --no-color add is short-circuited by the existing-arg
    # guard. The helper's --color add lands as a parsed-and-ignored
    # no-op (the renderer emits plain text).
    _add_ccusage_alias_args(fhb, ansi_emit=False)
    _add_mode_arg(fhb, noop=True)
    _add_share_args(fhb)
    fhb.set_defaults(func=c.cmd_five_hour_blocks)

    # -- cache-sync --
    p_cache_sync = sub.add_parser(
        "cache-sync",
        help="Sync (or rebuild) the session-entry cache",
    )
    p_cache_sync.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop all cached entries and reingest from scratch",
    )
    p_cache_sync.add_argument(
        "--source",
        choices=("claude", "codex", "all"),
        default="all",
        help="Which ingest half to sync/rebuild (default: all).",
    )
    p_cache_sync.set_defaults(func=c.cmd_cache_sync)

    # -- daily --
    _build_daily_parser(
        sub, "daily",
        help_text="Show usage report grouped by date",
        xref="Alias of `cctally claude daily` (the canonical form).")

    # -- monthly --
    _build_monthly_parser(
        sub, "monthly",
        help_text="Show usage report grouped by month",
        xref="Alias of `cctally claude monthly` (the canonical form).")

    # -- weekly --
    _build_weekly_parser(
        sub, "weekly",
        help_text="Show usage grouped by subscription week (with Used %% and $/1%%)",
        xref="Alias of `cctally claude weekly` (the canonical form).")

    # -- codex-daily --
    _build_codex_daily_parser(
        sub, "codex-daily",
        help_text="Show Codex usage report grouped by date (drop-in for `ccusage-codex daily`)",
        xref="Alias of `cctally codex daily` (the canonical form).")

    # -- codex-monthly --
    _build_codex_monthly_parser(
        sub, "codex-monthly",
        help_text="Show Codex usage grouped by month (drop-in for `ccusage-codex monthly`)",
        xref="Alias of `cctally codex monthly` (the canonical form).")

    # -- codex-weekly --
    _build_codex_weekly_parser(
        sub, "codex-weekly",
        help_text="Show Codex usage grouped by week (week-start from config.json)",
        xref="Alias of `cctally codex weekly` (the canonical form).")

    # -- codex-session --
    _build_codex_session_parser(
        sub, "codex-session",
        help_text="Show Codex usage grouped by session (drop-in for `ccusage-codex session`)",
        xref="Alias of `cctally codex session` (the canonical form).")

    # -- project --
    p_project = sub.add_parser(
        "project",
        help="Roll usage up by project (git-root), with per-project Used %% attribution",
        formatter_class=CLIHelpFormatter,
        description=(
            "Aggregate Claude usage by project (git-root resolved). Default range is "
            "the current subscription week; use --since/--until or --weeks N to extend."
        ),
        epilog=textwrap.dedent("""\
            Examples:
              cctally project
              cctally project --weeks 4
              cctally project --since 20260401 --until 20260414
              cctally project --project ccusage --model sonnet
              cctally project --breakdown --sort used --order desc
              cctally project --group full-path --json
        """),
    )
    p_project.add_argument("-s", "--since", default=None, metavar="YYYYMMDD",
                           help="Inclusive start date (YYYY-MM-DD or YYYYMMDD).")
    p_project.add_argument("-u", "--until", default=None, metavar="YYYYMMDD",
                           help="Inclusive end date (YYYY-MM-DD or YYYYMMDD).")
    p_project.add_argument("--weeks", type=int, default=None,
                           help="Last N subscription weeks ending now.")
    p_project.add_argument("--project", action="append", default=[], metavar="PATTERN",
                           help="Substring filter on project display key (repeatable, OR).")
    p_project.add_argument("--model", action="append", default=[], metavar="PATTERN",
                           help="Substring filter on model name (repeatable, OR).")
    p_project.add_argument("-b", "--breakdown", action="store_true",
                           help="Add per-model child rows under each project.")
    p_project.add_argument("-o", "--order", choices=("asc", "desc"), default="desc",
                           help="Sort direction (default: desc).")
    p_project.add_argument("--sort", choices=("cost", "used", "name", "last-seen"),
                           default="cost",
                           help="Sort key (default: cost).")
    p_project.add_argument("--group", choices=("git-root", "full-path"), default="git-root",
                           help="Bucket by resolved git-root (default) or raw project_path.")
    p_project.add_argument("--reveal-projects", action="store_true", dest="reveal_projects",
                           help="In --format output, show real project basenames instead of "
                                "the default project-1, project-2, ... anonymization.")
    p_project.add_argument("--no-color", action="store_true", dest="no_color",
                           help="Disable ANSI color.")
    p_project.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
                           help="Display timezone: local, utc, or IANA name. "
                                "Overrides config display.tz for this call.")
    # Session A (spec §7.6): ansi_emit=True. project is one of the two
    # real ANSI emitters. The helper skips its --no-color add (already
    # declared at p_project above) and adds the new bool --color flag
    # whose precedence flows through _resolve_color_enabled (§7.3).
    _add_ccusage_alias_args(p_project, ansi_emit=True)
    _add_share_args(p_project)
    p_project.set_defaults(func=c.cmd_project)

    # -- diff --
    diff_p = sub.add_parser(
        "diff",
        help="Compare Claude usage between two windows.",
    )
    diff_p.add_argument("--a", required=True,
        help="Window A token (this-week | last-week | Nw-ago | this-month | last-month | Nm-ago | last-Nd | prev-Nd | YYYY-MM-DD..YYYY-MM-DD)")
    diff_p.add_argument("--b", required=True, help="Window B token (same grammar as --a)")
    diff_p.add_argument("--allow-mismatch", action="store_true",
        help="Permit mismatched window lengths (deltas normalized per-day)")
    diff_p.add_argument("--only", help="Comma-separated section list (overall,models,projects,cache)")
    diff_p.add_argument("--with", dest="with_extra",
        help="Comma-separated opt-in sections (trend,time)")
    diff_p.add_argument("--all", dest="show_all", action="store_true",
        help="Show all rows (bypass noise filter)")
    diff_p.add_argument("--min-delta", type=float, dest="min_delta_usd",
        help="Override |Δ$| noise threshold (default 0.10)")
    diff_p.add_argument("--min-delta-pct", type=float,
        help="Override |Δ%%| noise threshold (default 1.0)")
    diff_p.add_argument("--sort",
        choices=["delta", "cost-a", "cost-b", "name", "status"], default="delta")
    diff_p.add_argument("--top", type=int, help="Cap rows per section after filter+sort")
    diff_p.add_argument("--sync", action="store_true",
        help="Run sync_cache + sync-week before computing")
    diff_p.add_argument("--tz", default=None, type=_argparse_tz, metavar="TZ",
        help="Display timezone: local, utc, or IANA name. "
             "Overrides config display.tz for this call.")
    diff_p.add_argument("--no-color", action="store_true")
    diff_p.add_argument("--json", dest="emit_json", action="store_true")
    diff_p.add_argument("--width", type=int, help=argparse.SUPPRESS)
    diff_p.add_argument("--debug-now", action="store_true", help=argparse.SUPPRESS)
    # Session A (spec §7.6): ansi_emit=True. diff is the other real
    # ANSI emitter. The helper skips its --no-color add (declared
    # above) and adds the new bool --color flag wired through
    # _resolve_color_enabled (§7.3). Note: --debug here collides with
    # diff's existing `--debug-now` (SUPPRESS'd internal flag) but
    # `--debug-now` is a different option string; the helper still
    # adds plain `--debug` cleanly.
    _add_ccusage_alias_args(diff_p, ansi_emit=True)
    diff_p.set_defaults(func=c.cmd_diff)

    # -- session --
    _build_session_parser(
        sub, "session",
        help_text="Show Claude usage grouped by sessionId (merges resumed-across-files sessions)",
        xref="Alias of `cctally claude session` (the canonical form).")

    # --- `claude` subgroup (drop-in for `ccusage claude …`); issue #86 Session B ---
    # Build-once, register-twice: these reuse the same nine builders as the flat
    # forms above. Nested subparsers reuse dest="command" so args.command resolves
    # to the leaf name (e.g. "blocks"), keeping banner suppression byte-identical
    # to the flat form with zero hook-path changes.
    claude_p = sub.add_parser(
        "claude",
        help="Claude-source reports (drop-in for `ccusage claude …`)",
        formatter_class=CLIHelpFormatter,
        description="Claude-source usage reports. Each subcommand is a drop-in for the "
                    "matching `ccusage claude <cmd>` and shares its engine with the "
                    "top-level `cctally <cmd>` alias.")
    claude_sub = claude_p.add_subparsers(dest="command", required=True, metavar="<command>")
    _build_daily_parser(claude_sub, "daily",
        help_text="Show usage grouped by date",
        xref="Drop-in for `ccusage claude daily`. Same engine as `cctally daily`.")
    _build_monthly_parser(claude_sub, "monthly",
        help_text="Show usage grouped by month",
        xref="Drop-in for `ccusage claude monthly`. Same engine as `cctally monthly`.")
    _build_weekly_parser(claude_sub, "weekly",
        help_text="Show usage grouped by subscription week",
        xref="Drop-in for `ccusage claude weekly`. Same engine as `cctally weekly`.")
    _build_session_parser(claude_sub, "session",
        help_text="Show usage grouped by session",
        xref="Drop-in for `ccusage claude session`. Same engine as `cctally session`.")
    _build_blocks_parser(claude_sub, "blocks",
        help_text="Show usage grouped by 5-hour session blocks",
        xref="Drop-in for `ccusage claude blocks`. Same engine as `cctally blocks`.")
    _build_statusline_parser(claude_sub, "statusline",
        help_text="Compact one-line status for Claude Code hooks",
        xref="Canonical `cctally claude statusline` (flat alias: `cctally statusline`). "
             "Drop-in for `ccusage statusline` plus cctally extension segments.")

    # --- `codex` subgroup (drop-in for `ccusage codex …`); issue #86 Session B ---
    codex_p = sub.add_parser(
        "codex",
        help="Codex-source reports (drop-in for `ccusage codex …`)",
        formatter_class=CLIHelpFormatter,
        description="Codex-source usage reports. daily/monthly/session are drop-ins for "
                    "`ccusage codex <cmd>`; weekly is a cctally extension. Each shares its "
                    "engine with the matching `cctally codex-<cmd>` alias.")
    codex_sub = codex_p.add_subparsers(dest="command", required=True, metavar="<command>")
    _build_codex_daily_parser(codex_sub, "daily",
        help_text="Show Codex usage grouped by date",
        xref="Drop-in for `ccusage codex daily`. Same engine as `cctally codex-daily`.")
    _build_codex_monthly_parser(codex_sub, "monthly",
        help_text="Show Codex usage grouped by month",
        xref="Drop-in for `ccusage codex monthly`. Same engine as `cctally codex-monthly`.")
    _build_codex_session_parser(codex_sub, "session",
        help_text="Show Codex usage grouped by session",
        xref="Drop-in for `ccusage codex session`. Same engine as `cctally codex-session`.")
    _build_codex_weekly_parser(codex_sub, "weekly",
        help_text="Show Codex usage grouped by week",
        xref="cctally extension (no upstream `ccusage codex weekly`). Same engine as "
             "`cctally codex-weekly`.")

    # ---- config (persisted user preferences) ----
    cfg_p = sub.add_parser(
        "config",
        help="Get / set / unset persisted user preferences",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent("""\
            Manage cctally user preferences in ~/.local/share/cctally/config.json.

            Currently supported keys:
              display.tz       Display timezone. Values: 'local' (default; host
                               zone via the OS locale), 'utc', or any IANA name
                               like 'America/New_York'. Per-call --tz flag on
                               any subcommand still wins over the persisted value.
              alerts.enabled   Enable/disable threshold alerts (true/false).
              dashboard.bind   Host the `dashboard` subcommand binds. Values:
                               'loopback' (default; binds 127.0.0.1 —
                               loopback-only), 'lan' (binds 0.0.0.0 —
                               LAN-accessible), or any literal IP / hostname.

            Examples:
              cctally config get
              cctally config get display.tz
              cctally config set display.tz America/New_York
              cctally config set dashboard.bind lan
              cctally config unset dashboard.bind
        """),
    )
    cfg_sub = cfg_p.add_subparsers(dest="action", required=True)
    cfg_get = cfg_sub.add_parser("get", help="Print current value(s)")
    cfg_get.add_argument("key", nargs="?", help="Config key (omit to list all)")
    cfg_get.add_argument("--json", dest="emit_json", action="store_true",
                         help="Emit JSON instead of key=value lines.")
    cfg_get.set_defaults(func=c.cmd_config)
    cfg_set = cfg_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key", help="Config key")
    cfg_set.add_argument("value", help="New value")
    cfg_set.add_argument("--json", dest="emit_json", action="store_true",
                         help="Emit JSON instead of key=value confirmation.")
    cfg_set.set_defaults(func=c.cmd_config)
    cfg_unset = cfg_sub.add_parser("unset", help="Remove a config override")
    cfg_unset.add_argument("key", help="Config key")
    cfg_unset.set_defaults(func=c.cmd_config)

    # ---- alerts (threshold-actions Task 6) ----
    p_alerts = sub.add_parser(
        "alerts",
        help="Manage threshold alerts",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent("""\
            Manage cctally threshold alerts.

            Subcommands:
              test    Send a synthetic test alert through the dispatch
                      pipeline (osascript spawn + alerts.log line). Logs
                      with mode=test so it doesn't pollute real-alert
                      history.

            Examples:
              cctally alerts test
              cctally alerts test --axis five-hour --threshold 95
        """),
    )
    alerts_sub = p_alerts.add_subparsers(dest="alerts_command", required=True)
    p_alerts_test = alerts_sub.add_parser(
        "test",
        help="Send a synthetic test alert through the dispatch pipeline",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent("""\
            Send a synthetic test alert end-to-end through the alert
            dispatch pipeline.

            Builds a fake payload using the same content builders the
            real-alert path uses, then routes through the same osascript
            spawn and alerts.log writer as production. Distinguishes
            itself from real threshold-crossing alerts by writing the
            alerts.log line with mode=test (5th tab-delimited field) —
            no DB writes, no envelope mutation, so it cannot pollute
            real-alert history.

            Use this to verify Notification Center delivery is working
            (osascript present, notifications enabled, no Do Not Disturb)
            without waiting for a real percent crossing.

            Exit codes:
              0  alert was queued (osascript spawned successfully)
              1  osascript missing on this host (not macOS, or binary unavailable)
              2  --threshold out of [1, 100] range
              3  other spawn error (PermissionError, OSError, etc.)

            Examples:
              cctally alerts test
              cctally alerts test --axis five-hour --threshold 95
              cctally alerts test --axis budget --threshold 100
              cctally alerts test --axis project-budget --threshold 100
              cctally alerts test --axis codex-budget --threshold 100
              cctally alerts test --axis projected --metric budget_usd
              cctally alerts test --axis projected --metric codex_budget_usd
        """),
    )
    p_alerts_test.add_argument(
        "--axis",
        choices=[
            "weekly", "five-hour", "budget", "project-budget", "codex-budget",
            "projected",
        ],
        default="weekly",
        help="Alert axis to simulate: weekly subscription window, 5h block, "
             "equiv-$ budget, per-project equiv-$ budget, Codex budget, or "
             "projected-pace (default: weekly).",
    )
    p_alerts_test.add_argument(
        "--threshold",
        type=int,
        default=90,
        help="Threshold percent (1-100, default: 90).",
    )
    p_alerts_test.add_argument(
        "--metric",
        choices=["weekly_pct", "budget_usd", "codex_budget_usd"],
        default="weekly_pct",
        help="For --axis projected: which projected metric to preview "
             "(default: weekly_pct).",
    )
    p_alerts_test.set_defaults(func=c.cmd_alerts_test)

    # ---- setup (onboarding spec §2) ----
    sp = sub.add_parser(
        "setup",
        help="Install cctally into Claude Code (hooks + symlinks)",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Install cctally into Claude Code by adding hook entries to
            ~/.claude/settings.json (additive, idempotent) and creating
            user-facing symlinks under ~/.local/bin/.

            Modes (mutually exclusive):
              cctally setup                    # install (default)
              cctally setup --dry-run          # show planned changes, change nothing
              cctally setup --status           # report current install state
              cctally setup --uninstall        # remove hooks + symlinks (keep data)
              cctally setup --uninstall --purge   # also wipe ~/.local/share/cctally/
            """
        ),
    )
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Report current install state")
    mode.add_argument("--uninstall", action="store_true",
                      help="Remove hooks + symlinks (keep data unless --purge)")
    mode.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="Show planned changes without modifying anything")
    sp.add_argument("--purge", action="store_true",
                    help="With --uninstall: also wipe ~/.local/share/cctally/")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmations")
    sp.add_argument("--json", action="store_true",
                    help="Emit machine-readable output")
    sp.add_argument("--force-dev", action="store_true", dest="force_dev",
                    help="Allow setup to run from a dev checkout (writes "
                         "dev-pointing hooks into ~/.claude/settings.json)")
    # Legacy bespoke-hook migration flags (install-mode only — see cmd_setup
    # post-parse validation). Spec Section 2 mode×flag matrix.
    mig_group = sp.add_mutually_exclusive_group()
    mig_group.add_argument(
        "--migrate-legacy-hooks", action="store_true", dest="migrate_legacy_hooks",
        help="Auto-accept the legacy-bespoke-hook migration prompt (install only).",
    )
    mig_group.add_argument(
        "--no-migrate-legacy-hooks", action="store_true", dest="no_migrate_legacy_hooks",
        help="Auto-skip the legacy-bespoke-hook migration prompt (install only).",
    )
    sp.set_defaults(func=c.cmd_setup)

    # ---- db (migration framework — spec §4) ----
    db_parser = sub.add_parser(
        "db",
        help="Migration / DB management (status, skip, unskip)",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Inspect and manage cctally's SQLite migration state.

            Subcommands:
              status   List migrations + applied/pending/failed/skipped
                       state across stats.db and cache.db. Glyphs:
                         ✓ applied   ✗ failed   · pending   ~ skipped
              skip     Mark a migration as skipped (manual poison-pill
                       escape — bypass an offending migration).
              unskip   Remove a skip mark; the migration runs on next
                       open.

            Migration names accept either bare ("003_…") or qualified
            ("stats.db:003_…" / "cache.db:003_…") forms. Bare names are
            rejected with exit 2 if the same NNN_… exists in both
            registries.

            Examples:
              cctally db status
              cctally db status --json
              cctally db skip 003_merge_5h_block_duplicates_v1 --reason "perf hot"
              cctally db unskip stats.db:003_merge_5h_block_duplicates_v1
            """
        ),
    )
    db_sub = db_parser.add_subparsers(dest="db_action", required=True)

    db_status = db_sub.add_parser(
        "status",
        help="List migrations + applied/pending/failed/skipped state",
    )
    db_status.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout",
    )
    db_status.set_defaults(func=c.cmd_db_status)

    db_skip = db_sub.add_parser(
        "skip",
        help="Mark a migration as skipped",
    )
    db_skip.add_argument(
        "name",
        help="Migration name (NNN_… or stats.db:NNN_… / cache.db:NNN_…)",
    )
    db_skip.add_argument(
        "--reason",
        help="Free-text reason (shown in db status)",
    )
    db_skip.set_defaults(func=c.cmd_db_skip)

    db_unskip = db_sub.add_parser(
        "unskip",
        help="Remove a skip mark; migration runs on next open",
    )
    db_unskip.add_argument(
        "name",
        help="Migration name (NNN_… or qualified)",
    )
    db_unskip.set_defaults(func=c.cmd_db_unskip)

    db_recover = db_sub.add_parser(
        "recover",
        help="Revert a version-ahead DB to the known schema head (#145)",
    )
    db_recover.add_argument(
        "--db",
        required=True,
        choices=("cache", "stats"),
        help="Which DB to recover",
    )
    db_recover.add_argument(
        "--yes",
        action="store_true",
        help="Required for --db stats (non-re-derivable; may need a re-record)",
    )
    db_recover.set_defaults(func=c.cmd_db_recover)

    # ─── doctor (Diagnostics) ───────────────────────────────────────────
    doctor_p = sub.add_parser(
        "doctor",
        help="Diagnose data freshness and install state",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Run all read-only diagnostic checks and emit a report.

            Categories: install, hooks, auth, db, data, safety. Each
            category renders a severity (✓ ok / ⚠ warn / ✗ fail) and
            actionable remediation guidance for non-OK rows.

            Exit code: 0 unless any check is FAIL (then exit 2). WARN
            rows do not change the exit code — doctor is a read-only
            diagnostic and warn-class findings are advisories.

            See docs/commands/doctor.md for the full check inventory
            and JSON schema reference.
            """
        ),
    )
    doctor_p.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON to stdout (schema_version: 1)",
    )
    doctor_mutex = doctor_p.add_mutually_exclusive_group()
    doctor_mutex.add_argument(
        "--quiet", "-q", action="store_true",
        help="Hide OK rows (human mode only)",
    )
    doctor_mutex.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include each check's details block (human mode only)",
    )
    doctor_p.set_defaults(func=c.cmd_doctor)

    # ---- pricing-check (standalone diagnostic — NOT under claude/codex) ----
    pc_p = sub.add_parser(
        "pricing-check",
        help="Detect stale or missing embedded model pricing",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Check whether cctally's embedded model pricing is stale or
            missing, across three independently-degrading legs:

              • coverage  (offline, all-history) — models in your cached
                session data that cctally cannot price (Claude $0) or only
                approximates (Codex gpt-5 fallback).
              • drift     (network, LiteLLM) — embedded price values vs the
                LiteLLM snapshot (direction-aware; allowlist-suppressed).
              • existence (network, Anthropic /v1/models) — vendor models the
                API offers that our table lacks. Maintainer-local (needs
                OAuth); degrades to skipped/degraded otherwise.

            Exit codes:
              0 — no actionable findings (fully clean, OR partially/fully
                  network-degraded but nothing actionable; --json still
                  carries "status":"degraded").
              1 — any actionable finding (a coverage gap, value drift,
                  missing-from-us, or an existence gap) — EVEN IF a network
                  leg degraded. Findings always win over degradation.
              2 — argument/usage error.

            "status" (ok|degraded) reports check completeness; the exit code
            reports whether you must act. They are orthogonal.

            See docs/commands/pricing-check.md for the JSON schema.
            """
        ),
    )
    pc_p.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON to stdout (schemaVersion: 1)",
    )
    pc_p.add_argument(
        "--offline", action="store_true",
        help="Coverage only — skip both network legs (LiteLLM + /v1/models)",
    )
    pc_p.set_defaults(func=c.cmd_pricing_check)

    # `release` is its own standalone entry-point (bin/cctally-release);
    # no `release` subparser is registered on the main `cctally` CLI.
    # See docs/RELEASE.md.

    # ---- hook-tick (internal — hidden from --help, see onboarding spec §3) ----
    ht = sub.add_parser(
        "hook-tick",
        help=argparse.SUPPRESS,
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Internal subcommand invoked by Claude Code hooks.

            Reads CC's hook payload from stdin, runs sync_cache, and
            conditionally refreshes the OAuth usage cache (throttled).
            Returns 0 unconditionally in normal mode.
            """
        ),
    )
    ht.add_argument("--explain", action="store_true",
                    help="Run synchronously, print decision tree, exit informative code")
    ht.add_argument("--no-oauth", action="store_true",
                    help="Skip the OAuth refresh entirely (local sync only)")
    ht.add_argument("--throttle-seconds", type=float, default=None,
                    help=f"Override throttle (default {int(c.HOOK_TICK_DEFAULT_THROTTLE_SECONDS)}s)")
    ht.add_argument("--event", type=str, default=None,
                    help="Override the event name written to the log line "
                         "(used by --explain and tests)")
    ht.add_argument("--mock-oauth-response", type=str, default=None,
                    help=argparse.SUPPRESS)  # JSON string fed to mock fetch (tests only)
    ht.set_defaults(func=c.cmd_hook_tick)

    # ---- __preview (internal — maintainer-local preview channel) ----
    # The preview modules are maintainer-only (excluded from the public
    # mirror). On a public build `c.cmd_preview` is None — its load in
    # bin/cctally is wrapped in a tolerant try/except — so skip registering
    # the hidden subcommand entirely rather than wiring a None handler.
    if getattr(c, "cmd_preview", None) is not None:
        pv = sub.add_parser("__preview", help=argparse.SUPPRESS,
                            formatter_class=CLIHelpFormatter,
                            description="Internal: provision/manage the preview-channel data dir.")
        pv_sub = pv.add_subparsers(dest="action", required=True)
        pv_ensure = pv_sub.add_parser("ensure", help=argparse.SUPPRESS)
        pv_ensure.add_argument("--no-refresh", action="store_true")
        pv_ensure.add_argument("--reseed", action="store_true")
        pv_ensure.set_defaults(func=c.cmd_preview)
        pv_clean = pv_sub.add_parser("clean", help=argparse.SUPPRESS)
        pv_clean.add_argument("--dry-run", action="store_true")
        pv_clean.set_defaults(func=c.cmd_preview)
        pv_status = pv_sub.add_parser("status", help=argparse.SUPPRESS)
        pv_status.set_defaults(func=c.cmd_preview)

    # ---- update (user-facing self-update subcommand; spec §4) ----
    sub_update = sub.add_parser(
        "update",
        help="Update cctally to the latest version",
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Update cctally to the latest version (npm/brew installs only).

            Modes:
              cctally update                 install the latest version
              cctally update --check         show update info without installing
              cctally update --skip [VER]    don't remind about VER (default: latest)
              cctally update --remind-later [DAYS]  defer the banner (default: 7)
            """
        ),
    )
    update_modes = sub_update.add_mutually_exclusive_group()
    update_modes.add_argument(
        "--check", action="store_true",
        help="Show update info without installing",
    )
    update_modes.add_argument(
        "--skip", nargs="?", const=c.SKIP_USE_STATE_LATEST, metavar="VERSION",
        default=None,
        help="Skip a specific version (default: latest in cache)",
    )
    update_modes.add_argument(
        "--remind-later", nargs="?", type=int, const=7, metavar="DAYS",
        default=None,
        help="Defer reminders by N days (default: 7)",
    )
    # `--version` here is local to the update subparser. The subparser's
    # value is bound to `args.install_version` (NOT `args.version`) to
    # avoid a namespace collision with the top-level `--version`
    # (store_true) flag handled in `main()` before subcommand dispatch:
    # if both used `dest="version"`, `cctally update --version 1.2.3`
    # would set `args.version="1.2.3"`, which `main()`'s truthy check
    # would treat as "global --version requested" and short-circuit to
    # print the version banner before `cmd_update` ever ran.
    sub_update.add_argument(
        "--version", metavar="X.Y.Z", default=None, dest="install_version",
        help="Install a specific version (npm only; brew has no versioned formulae)",
    )
    sub_update.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen, don't install",
    )
    sub_update.add_argument(
        "--force", action="store_true",
        help="Bypass TTL on --check (force a fresh remote fetch)",
    )
    sub_update.add_argument(
        "--json", action="store_true",
        help="Emit JSON output (mostly with --check)",
    )
    sub_update.set_defaults(func=c.cmd_update)

    # ---- _update-check (internal — hidden, detached-refresh worker for `cctally update`) ----
    uc = sub.add_parser(
        "_update-check",
        help=argparse.SUPPRESS,
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Internal subcommand: detached version-check worker spawned
            by `cctally update` (spec §3.6). Touches the throttle
            marker, fetches the latest version from npm or homebrew
            depending on install method, and writes update-state.json.
            Always returns 0; failures are logged to update.log.
            """
        ),
    )
    uc.set_defaults(func=c.cmd_update_check_internal)

    # ---- repair-symlinks (internal — hidden; npm-postinstall self-heal, issue #114) ----
    rs = sub.add_parser(
        "repair-symlinks",
        help=argparse.SUPPRESS,
        formatter_class=CLIHelpFormatter,
        description=textwrap.dedent(
            """\
            Internal subcommand: additively create any missing
            ~/.local/bin/ symlinks for cctally subcommands (issue #114).

            Invoked best-effort by the npm postinstall on upgrade so new
            cctally-* binaries become reachable without re-running
            `cctally setup`. Gated to existing installs (>=1 symlink
            already present); a fresh install is a silent no-op. Touches
            only symlinks — no hooks, settings.json, or cache. Refuses
            from a dev checkout.
            """
        ),
    )
    rs.set_defaults(func=c.cmd_repair_symlinks)

    # Python 3.14 leaks `==SUPPRESS==` for hidden subparsers in --help; strip
    # the pseudo-action so the row disappears entirely. (The choice still
    # appears in the `{...}` choices header — there's no clean way to hide
    # that without a custom formatter, and it's harmless.)
    sub._choices_actions = [
        a for a in getattr(sub, "_choices_actions", [])
        if getattr(a, "help", None) is not argparse.SUPPRESS
    ]

    return p
