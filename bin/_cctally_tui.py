"""TUI subsystem for cctally (live terminal dashboard).

Eager I/O sibling: bin/cctally loads this at startup. Owns the entire
``cctally tui`` user-facing surface plus the shared dataclasses used
by both the TUI and the dashboard (Phase F #22 deferred these to this
extraction so the dashboard's existing 5 dataclass shims can resolve
through cctally's re-exported namespace transparently):

- ``cmd_tui`` — ``cctally tui`` entry point. Lazy-imports ``rich``
  inside the function body (CLAUDE.md TUI gotcha: keeps the rest of
  the script zero-dep). Resolves ``--variant`` (2x2 grid vs
  expressive hero), the sync/refresh interval pair, ``--render-once``
  / ``--snapshot-module`` fixture path, the alternate-screen / cursor
  / SIGWINCH lifecycle, and the main render loop.
- ``TuiKeyReader`` — raw-mode keyboard reader. Uses
  ``termios.tcgetattr`` / ``setcbreak`` + ``select.select`` to read
  one keystroke at a time without blocking the render loop.
  Re-installs the saved tty mode on ``__exit__`` even when the loop
  raises.
- ``_TuiSyncThread`` — periodic snapshot-rebuilder. Shared base
  class subclassed inline by the dashboard's
  ``_DashboardSyncThread`` (in ``bin/_cctally_dashboard.py`` via
  ``c._TuiSyncThread`` resolution at class-definition time). Owns
  the sync-interval cadence + the ``request_sync()`` / monotonic
  budget loop.
- ``_tui_handle_key`` — central keymap dispatcher. Routes single
  keystrokes through the filter / search input mode, the modal
  open/close lifecycle, and the global hotkeys (panel switching,
  sort cycling, help, refresh, quit). Honors
  CLAUDE.md "Global hotkeys need modal guard" — every global
  binding gates on ``openModal is None``.
- ``_tui_build_*`` snapshot builder family —
  ``_tui_build_current_week``, ``_tui_build_forecast``,
  ``_tui_build_trend``, ``_tui_build_weekly_history``,
  ``_tui_build_sessions``, ``_tui_build_session_detail``,
  ``_tui_build_percent_milestones``, ``_tui_build_snapshot`` —
  read from SQLite + the cache DB and produce one immutable
  ``DataSnapshot``. ``_tui_build_snapshot`` is the orchestrator;
  the rest are per-panel builders the dashboard's sync thread
  also calls (re-exported through cctally so the dashboard's
  ``c.X`` resolution lands).
- ``_tui_empty_snapshot`` — minimal placeholder ``DataSnapshot``
  used by the dashboard at boot before the first sync lands; also
  by the panel-level test harnesses via ``ns["_tui_empty_snapshot"]``.
- ``_tui_panel_*`` panel renderer family —
  ``_tui_panel_current_week``, ``_tui_panel_current_week_hero``,
  ``_tui_panel_forecast``, ``_tui_panel_trend``,
  ``_tui_panel_sessions``. Each takes a ``DataSnapshot`` + the
  current ``RuntimeState`` + width/height/focus hints and returns
  a list of rich-tagged text lines (NOT a rich.Panel — the variant
  renderers box them).
- ``_tui_modal_*`` modal renderer family —
  ``_tui_modal_current_week``, ``_tui_modal_forecast``,
  ``_tui_modal_trend``, ``_tui_modal_session``. Each rebuilds its
  body from the latest ``DataSnapshot`` every tick (CLAUDE.md
  "TUI v2 modal/input lifecycle" gotcha: modals are NOT frozen at
  open time; sync continues while open).
- ``_tui_render_variant_a`` / ``_tui_render_variant_b`` —
  full-frame composers for the two layout variants
  (``variant_a`` is the 2x2 grid; ``variant_b`` is the expressive
  hero). Each owns the focused-border ribbon, the toast slot, the
  modal/overlay positioning, and the header strip.
- ``_tui_render_help`` — full-frame help overlay (a rich.Panel
  bordered with the keymap legend).
- ``_tui_render_modal`` — modal dispatcher; selects the
  per-modal renderer by ``runtime.open_modal`` slot.
- ``_tui_render_once`` — dev hook for the
  ``--render-once --snapshot-module`` fixture path
  (argparse-SUPPRESSed; powers ``bin/cctally-tui-test``). Builds
  the console with ``record=True``, runs one frame, exports
  text/SVG, and writes to stdout. Honors ``RUNTIME_OVERRIDES``
  dict on the snapshot module per the spec's allow-list.
- ``_tui_header_strip_a`` / ``_tui_footer_keys`` /
  ``_tui_render_input_prompt`` — chrome helpers for header/footer
  rows and the in-prompt input line (filter ``f`` / search ``/``).
- ``_tui_render_toast`` — bottom-anchored toast notification line.
- ``_tui_colortag`` / ``_tui_escape_tags`` / ``_tui_strip_tags`` /
  ``_tui_tagged_box_lines`` / ``_tui_lines_to_text`` — markup
  helpers for the in-house ``{name}…{/}`` tag grammar (avoids
  rich's ``[…]`` syntax so panel content can embed literal
  square brackets verbatim).
- ``_tui_box_lines`` / ``_tui_bar_string`` / ``_tui_bar_color`` /
  ``_tui_sparkline_inline`` / ``_tui_sparkline_big`` /
  ``_tui_width_bucket`` — drawing primitives.
- ``_tui_verdict_of`` / ``_tui_session_model_cls`` /
  ``_tui_format_started`` / ``_tui_format_dur`` /
  ``_tui_sort_sessions`` / ``_tui_next_sort_key`` /
  ``_tui_apply_session_filter`` / ``_tui_sessions_title`` —
  data-presentation helpers for the sessions panel.
- ``_tui_sync_interval_type`` / ``_tui_refresh_interval_type`` —
  argparse type validators for the two CLI interval flags.
- ``_make_run_sync_now`` / ``_make_run_sync_now_locked`` —
  shared snapshot-rebuilder closures consumed by BOTH the TUI
  loop and the dashboard's ``POST /api/sync`` handler + periodic
  thread (re-exported through cctally so the dashboard's
  shim chain lands; the test harness patches
  ``ns["_tui_build_snapshot"]`` to stub the rebuild).

- Shared dataclasses (consumed by BOTH the TUI and the dashboard,
  via cctally's eager re-export → the dashboard's existing 5
  dataclass shims at ``bin/_cctally_dashboard.py:487-504``
  continue resolving transparently through
  ``sys.modules["cctally"].X``):
  ``DataSnapshot``, ``RuntimeState``, ``TuiCurrentWeek``,
  ``TuiTrendRow``, ``TuiSessionRow``, ``TuiSessionDetail``,
  ``TuiPercentMilestone``, ``WeeklyPeriodRow``,
  ``MonthlyPeriodRow``, ``BlocksPanelRow``, ``DailyPanelRow``.

What stays in bin/cctally:
- ``ForecastInputs``, ``ForecastOutput``, ``BudgetRow`` — the
  forecast inputs/output/budget dataclasses. Used by ``_compute_forecast``
  (whose definition stays in cctally alongside the forecast subcommand)
  and by the TUI builder which constructs them via the module-level
  callable shims below.
- ``_compute_forecast``, ``_resolve_forecast_now``,
  ``_fetch_current_week_snapshots``, ``_load_forecast_inputs``,
  ``_apply_midweek_reset_override``, ``_sum_cost_for_range``,
  ``_compute_cost_for_weekref``, ``_week_ref_has_reset_event`` —
  forecast/cost-aggregation helpers, called from this sibling via
  module-level shims (each resolves
  ``sys.modules["cctally"].X`` at call time).
- The ``Block`` / ``SubWeek`` dataclasses live in ``_lib_blocks``
  and ``_lib_subscription_weeks`` (Phase A lib siblings); accessed
  via cctally's re-export.

§5.6 audit on this extraction's monkeypatch surface
(``tests/test_dashboard_*.py`` + ``tests/test_tui_*.py``: 11
distinct ``ns["X"]`` direct-dict reads on moved symbols —
``ns["DataSnapshot"]`` (6 sites), ``ns["WeeklyPeriodRow"]`` (3),
``ns["MonthlyPeriodRow"]`` (3), ``ns["BlocksPanelRow"]`` (3),
``ns["DailyPanelRow"]`` (3), ``ns["TuiCurrentWeek"]`` (2),
``ns["_tui_empty_snapshot"]`` (2), ``ns["_tui_build_snapshot"]`` (1),
``ns["_make_run_sync_now"]`` (1), ``ns["_make_run_sync_now_locked"]`` (1),
plus ``monkeypatch.setitem`` on ``_tui_build_snapshot`` in
``tests/test_dashboard_api_sync_refresh.py``). Forces the **eager
re-export** carve-out per spec §4.8 (same precedent as Phase E
#19/#20 + Phase F #21/#22):

- ``ns["X"]`` dict-key reads on dataclass / function / class
  objects propagate via eager re-export at sibling-load time;
  PEP 562 ``__getattr__`` does NOT fire on ``ns["X"]`` (``ns`` is
  the module's ``__dict__``, not the module proxy).
- ``monkeypatch.setitem(ns, "_tui_build_snapshot", mock)`` mutates
  cctally's namespace. ``_make_run_sync_now_locked`` calls
  ``_tui_build_snapshot`` bare-name, which resolves in THIS
  sibling's ``__dict__`` — so the mock would not propagate.
  Pattern matches Phase D #17/#18 + F #21/#22: cross-call from
  one moved function to another moved function that's also a
  monkeypatch target routes through the
  ``sys.modules['cctally']._tui_build_snapshot`` callable shim
  at call time, ensuring the latest binding wins.

Except-clause audit (Phase F #22's P1 lesson): all ``except`` clauses
in the moved region are stdlib classes (``Exception``, ``ValueError``,
``ImportError``, ``FileNotFoundError``) — NO cross-module exception
classes. The ``except sys.modules["cctally"].X:`` form used in
``_cctally_dashboard.py`` for ``UpdateError`` is NOT required here.

``rich`` import policy: ``rich`` is lazy-imported INSIDE function
bodies (``cmd_tui``, ``_tui_build_theme``, ``_tui_render_*``,
``_tui_panel_*``, ``_tui_modal_*``, etc.) per CLAUDE.md TUI gotcha.
The module level intentionally carries NO ``import rich`` or
``from rich…`` line; ``Panel`` annotations on
``_tui_render_help`` / ``_tui_render_modal`` are pure string
annotations (lazy resolution via ``from __future__ import
annotations``).

``_TUI_VALID_STYLE_NAMES`` / ``_TUI_THEME_KEYS`` drift assertions
(CLAUDE.md TUI gotcha: keep style names in sync with theme) move
intact alongside the theme builder; the module-level assert at
load time + the function-level cross-check inside
``_tui_build_theme`` are preserved verbatim.

``RUNTIME_OVERRIDES`` allow-list (CLAUDE.md TUI gotcha: dev-only
fixture override) is inside ``_tui_render_once``; moved with the
rest. Same for the ``--render-once --snapshot-module`` argparse
dev path.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §7.2
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import io
import json
import math
import os
import re
import signal as _signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. Mirrors the precedent established in
# ``bin/_cctally_record.py``, ``bin/_cctally_cache.py``,
# ``bin/_cctally_db.py``, ``bin/_cctally_update.py``, and
# ``bin/_cctally_dashboard.py``.
def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


def parse_iso_datetime(*args, **kwargs):
    return sys.modules["cctally"].parse_iso_datetime(*args, **kwargs)


def _now_utc(*args, **kwargs):
    return sys.modules["cctally"]._now_utc(*args, **kwargs)


def open_db(*args, **kwargs):
    return sys.modules["cctally"].open_db(*args, **kwargs)


def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def format_display_dt(*args, **kwargs):
    return sys.modules["cctally"].format_display_dt(*args, **kwargs)


def resolve_display_tz(*args, **kwargs):
    return sys.modules["cctally"].resolve_display_tz(*args, **kwargs)


def normalize_display_tz_value(*args, **kwargs):
    return sys.modules["cctally"].normalize_display_tz_value(*args, **kwargs)


def _resolve_display_tz_obj(*args, **kwargs):
    return sys.modules["cctally"]._resolve_display_tz_obj(*args, **kwargs)


def _apply_display_tz_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_display_tz_override(*args, **kwargs)


def _apply_midweek_reset_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_midweek_reset_override(*args, **kwargs)


def _compute_display_block(*args, **kwargs):
    return sys.modules["cctally"]._compute_display_block(*args, **kwargs)


def _compute_forecast(*args, **kwargs):
    return sys.modules["cctally"]._compute_forecast(*args, **kwargs)


def _resolve_forecast_now(*args, **kwargs):
    return sys.modules["cctally"]._resolve_forecast_now(*args, **kwargs)


def _fetch_current_week_snapshots(*args, **kwargs):
    return sys.modules["cctally"]._fetch_current_week_snapshots(*args, **kwargs)


def _load_forecast_inputs(*args, **kwargs):
    return sys.modules["cctally"]._load_forecast_inputs(*args, **kwargs)


def _sum_cost_for_range(*args, **kwargs):
    return sys.modules["cctally"]._sum_cost_for_range(*args, **kwargs)


def _compute_cost_for_weekref(*args, **kwargs):
    return sys.modules["cctally"]._compute_cost_for_weekref(*args, **kwargs)


def _week_ref_has_reset_event(*args, **kwargs):
    return sys.modules["cctally"]._week_ref_has_reset_event(*args, **kwargs)


def _freshness_label(*args, **kwargs):
    return sys.modules["cctally"]._freshness_label(*args, **kwargs)


def _get_oauth_usage_config(*args, **kwargs):
    return sys.modules["cctally"]._get_oauth_usage_config(*args, **kwargs)


def _aggregate_claude_sessions(*args, **kwargs):
    return sys.modules["cctally"]._aggregate_claude_sessions(*args, **kwargs)


def _aggregate_monthly(*args, **kwargs):
    return sys.modules["cctally"]._aggregate_monthly(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def get_latest_usage_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_latest_usage_for_week(*args, **kwargs)


def get_latest_cost_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_latest_cost_for_week(*args, **kwargs)


def get_milestones_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_milestones_for_week(*args, **kwargs)


def _canonicalize_optional_iso(*args, **kwargs):
    return sys.modules["cctally"]._canonicalize_optional_iso(*args, **kwargs)


def get_recent_weeks(*args, **kwargs):
    return sys.modules["cctally"].get_recent_weeks(*args, **kwargs)


def sync_cache(*args, **kwargs):
    return sys.modules["cctally"].sync_cache(*args, **kwargs)


# Forecast dataclass shims — used as bare-name constructors inside
# ``_tui_build_forecast``. The classes themselves stay in bin/cctally
# alongside the forecast subcommand (``cmd_forecast``); call-time
# resolution keeps monkeypatches in sync.
def ForecastInputs(*args, **kwargs):
    return sys.modules["cctally"].ForecastInputs(*args, **kwargs)


def ForecastOutput(*args, **kwargs):
    return sys.modules["cctally"].ForecastOutput(*args, **kwargs)


def BudgetRow(*args, **kwargs):
    return sys.modules["cctally"].BudgetRow(*args, **kwargs)


# Dashboard back-refs consumed by the TUI's snapshot builders.
# These functions/classes live in bin/_cctally_dashboard.py (Phase F #22),
# re-exported through bin/cctally so the shim resolves correctly.
def _dashboard_build_blocks_panel(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_blocks_panel(*args, **kwargs)


def _dashboard_build_daily_panel(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_daily_panel(*args, **kwargs)


def _dashboard_build_monthly_periods(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_monthly_periods(*args, **kwargs)


def _dashboard_build_weekly_periods(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_weekly_periods(*args, **kwargs)


def _build_alerts_envelope_array(*args, **kwargs):
    return sys.modules["cctally"]._build_alerts_envelope_array(*args, **kwargs)


def _select_current_block_for_envelope(*args, **kwargs):
    return sys.modules["cctally"]._select_current_block_for_envelope(*args, **kwargs)


def _SnapshotRef(*args, **kwargs):
    return sys.modules["cctally"]._SnapshotRef(*args, **kwargs)


# Alerts back-refs.
# Module-level __getattr__ — lazy-resolves cctally globals at attribute-access
# time. PEP 562 fires on ``module.X``-shaped access from outside this module;
# bare-name lookups in function bodies bypass it. Used here for the
# non-callable ``_AlertsConfigError`` exception class (cross-module class
# identity is required for any future ``except _AlertsConfigError:`` site)
# and for ``Block`` / ``SubWeek`` dataclass type references that might land
# in annotations.
_LAZY_ATTRS = (
    "_AlertsConfigError",
    "Block",
    "SubWeek",
)


def __getattr__(name):  # pylint: disable=invalid-name
    if name in _LAZY_ATTRS:
        return getattr(sys.modules["cctally"], name)
    raise AttributeError(name)


# ============================================================
# ==== TUI ====                                              =
# ============================================================
# Live dashboard subcommand. Lazy rich import keeps the rest of the
# script dependency-free. All TUI-specific code lives in this block.

TUI_RICH_MISSING_MSG = (
    "tui: this subcommand requires the 'rich' package.\n"
    "install with: pip install rich\n"
    "(or: pipx inject cctally rich)"
)


# Palette — frozen TUI color values.
TUI_PALETTE = {
    "term_bg":      "#0a0b0d",
    "fg":           "#d7dce1",
    "fg_dim":       "#7a8290",
    "fg_faint":     "#4a5060",
    "fg_bright":    "#f4f6f8",
    "accent":       "#6fc5e0",
    "accent_dim":   "#3d7c92",
    "ok":           "#7bc47f",
    "ok_dim":       "#3f7f4e",
    "warn":         "#e8c76e",
    "warn_dim":     "#8a7735",
    "bad":          "#e07a7a",
    "bad_dim":      "#873f3f",
    "magenta":      "#c89acf",
    "blue":         "#8ab0d9",
    # Badge fg colors — dark tones for high contrast against the bg-dim
    # swatches.
    "badge_warn_fg": "#1b1405",
    "badge_ok_fg":   "#0a1a0c",
    "badge_bad_fg":  "#1b0808",
}


def _tui_build_theme():
    """Build a rich.theme.Theme mapping named styles to TUI_PALETTE colors.

    Named styles used by the renderer:
      fg, dim, faint, bright, accent, accent.dim,
      ok, warn, bad, magenta, blue,
      badge.ok, badge.warn, badge.bad,
      focused  (alias of accent, but semantically the "focused pane border"),
      bar.ok, bar.warn, bar.bad, bar.accent, bar.track
    """
    from rich.style import Style
    from rich.theme import Theme
    p = TUI_PALETTE
    styles_dict = {
        "fg":          Style(color=p["fg"]),
        "dim":         Style(color=p["fg_dim"]),
        "faint":       Style(color=p["fg_faint"]),
        "bright":      Style(color=p["fg_bright"], bold=True),
        "accent":      Style(color=p["accent"]),
        "accent.dim":  Style(color=p["accent_dim"]),
        "ok":          Style(color=p["ok"]),
        "warn":        Style(color=p["warn"]),
        "bad":         Style(color=p["bad"]),
        "magenta":     Style(color=p["magenta"]),
        "blue":        Style(color=p["blue"]),
        "badge.ok":    Style(color=p["badge_ok_fg"],  bgcolor=p["ok_dim"],   bold=True),
        "badge.warn":  Style(color=p["badge_warn_fg"], bgcolor=p["warn_dim"], bold=True),
        "badge.bad":   Style(color=p["badge_bad_fg"],  bgcolor=p["bad_dim"],  bold=True),
        "focused":     Style(color=p["accent"], bold=True),
        "bar.ok":      Style(color=p["ok"]),
        "bar.warn":    Style(color=p["warn"]),
        "bar.bad":     Style(color=p["bad"]),
        "bar.accent":  Style(color=p["accent"]),
        "bar.track":   Style(color=p["fg_faint"]),
        # v2 additions (spec §5.2)
        "chip":        Style(color=p["term_bg"], bgcolor=p["accent"], bold=True),
        "match":       Style(color=p["term_bg"], bgcolor=p["warn"], bold=True),
        "prompt":      Style(color=p["accent"], bold=True),
        "caret":       Style(color=p["term_bg"], bgcolor=p["fg"]),
    }
    # Function-level cross-check: the literal style dict above must match the
    # declarative _TUI_THEME_KEYS. Catches the case where someone edits the
    # dict but forgets to update the module-level set (or vice versa). The
    # module-level assert covers the validator↔keys axis; this covers the
    # keys↔actual-theme axis. We check the pre-Theme dict rather than
    # theme.styles because rich.Theme inherits DEFAULT_STYLES (markdown/log/
    # progress/traceback/…) which would dilute the equality check.
    assert frozenset(styles_dict.keys()) == _TUI_THEME_KEYS, (
        "theme/keys drift: "
        f"added={sorted(set(styles_dict) - _TUI_THEME_KEYS)} "
        f"removed={sorted(_TUI_THEME_KEYS - set(styles_dict))}"
    )
    return Theme(styles_dict)


# Style-name shorthand -> rich-style keyword. 'b', 'u', 'pulse' are CSS
# shorthands from the reference HTML; map them to rich equivalents.
_TUI_TAG_SHORTHAND = {
    "b": "bold",
    "u": "underline",
    "pulse": "blink",  # terminal blink — approximates the CSS pulse animation.
}

# Theme-defined style names accepted by _tui_colortag (mirrors the keys of
# _tui_build_theme()). Must be kept in sync with that function. Any tag part
# not in this set and not in _TUI_TAG_SHORTHAND raises ValueError.
_TUI_VALID_STYLE_NAMES = frozenset({
    "fg", "dim", "faint", "bright",
    "accent", "accent.dim",
    "ok", "warn", "bad", "magenta", "blue",
    "badge.ok", "badge.warn", "badge.bad",
    "focused",
    "bar.ok", "bar.warn", "bar.bad", "bar.accent", "bar.track",
    # v2 additions
    "chip", "match", "prompt", "caret",
})

# Declarative enumeration of every style key produced by _tui_build_theme().
# Single source of truth — the theme builder and the module-level drift guard
# both consult it, so adding a theme style means editing this set (and the
# function's dict literal) in one place.
_TUI_THEME_KEYS = frozenset({
    "fg", "dim", "faint", "bright",
    "accent", "accent.dim",
    "ok", "warn", "bad", "magenta", "blue",
    "badge.ok", "badge.warn", "badge.bad",
    "focused",
    "bar.ok", "bar.warn", "bar.bad", "bar.accent", "bar.track",
    # v2 additions
    "chip", "match", "prompt", "caret",
})

# Module-level drift guard (no rich required): every name recognised by the
# validator must be provided by the theme. Fires at first import — so
# `python3 -m py_compile` followed by any import of the script catches the
# case where someone edits one side of the pair without the other, without
# needing to launch the `tui` subcommand.
assert _TUI_VALID_STYLE_NAMES <= _TUI_THEME_KEYS, (
    "_TUI_VALID_STYLE_NAMES drift: "
    f"{sorted(_TUI_VALID_STYLE_NAMES - _TUI_THEME_KEYS)} not in theme keys"
)


def _tui_colortag(source: str):
    """Render a color-tag string to a rich.text.Text.

    Grammar:
      - "{name}...{/}" -> style 'name' over inner text
      - "{n1.n2}...{/}" -> joined styles "n1 n2" (e.g. "{ok.b}" -> "ok bold")
      - "{{" / "}}" -> literal "{" / "}"
      - Styles must be defined in the theme (Task 3) OR be in
        _TUI_TAG_SHORTHAND. Unknown style names raise ValueError.

    The function returns a rich.text.Text (not a string) so the caller
    can compose it into Layouts/Panels without double-escaping.
    """
    from rich.text import Text

    out = Text()
    stack: list[str] = []  # active style stack; top = innermost
    buf: list[str] = []    # pending chars for the current style run

    def _flush():
        if not buf:
            return
        style = " ".join(stack) if stack else ""
        out.append("".join(buf), style=style)
        buf.clear()

    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c == "{" and i + 1 < n and source[i + 1] == "{":
            buf.append("{")
            i += 2
            continue
        if c == "}" and i + 1 < n and source[i + 1] == "}":
            buf.append("}")
            i += 2
            continue
        if c == "{":
            _flush()
            end = source.find("}", i + 1)
            if end < 0:
                raise ValueError(f"unterminated tag at offset {i}")
            tag = source[i + 1:end]
            if tag == "/":
                if not stack:
                    raise ValueError(f"unmatched closing tag at offset {i}")
                stack.pop()
            else:
                # Tag name resolution: try longest whole-tag match, peeling
                # trailing shorthands (.b/.u/.pulse) from the end. This supports
                # both `{ok.b}` (split/compose) AND `{bar.ok.b}` (peel `.b`,
                # then `bar.ok` is a valid whole theme key).
                if tag in _TUI_VALID_STYLE_NAMES:
                    stack.append(tag)
                else:
                    parts = tag.split(".")
                    resolved: str | None = None
                    # Try progressively shorter prefixes, peeling trailing
                    # shorthand parts off the back. `prefix` must be a valid
                    # whole theme key; all peeled parts must be in
                    # `_TUI_TAG_SHORTHAND`.
                    for k in range(len(parts) - 1, 0, -1):
                        prefix = ".".join(parts[:k])
                        suffix = parts[k:]
                        if prefix in _TUI_VALID_STYLE_NAMES and all(
                            s in _TUI_TAG_SHORTHAND for s in suffix
                        ):
                            resolved = prefix + " " + " ".join(
                                _TUI_TAG_SHORTHAND[s] for s in suffix
                            )
                            break
                    if resolved is None:
                        # Fallback: split-and-compose; every part must be a
                        # known shorthand or valid style name. This supports
                        # {ok.b} and raises on unknown names.
                        for p in parts:
                            if (
                                p not in _TUI_TAG_SHORTHAND
                                and p not in _TUI_VALID_STYLE_NAMES
                            ):
                                raise ValueError(
                                    f"unknown style name {p!r} in tag "
                                    f"{{{tag}}} at offset {i}"
                                )
                        resolved = " ".join(
                            _TUI_TAG_SHORTHAND.get(p, p) for p in parts
                        )
                    stack.append(resolved)
            i = end + 1
            continue
        buf.append(c)
        i += 1

    _flush()
    if stack:
        raise ValueError(f"unclosed tags remaining: {stack}")
    return out


def _tui_escape_tags(s: str) -> str:
    """Escape literal `{` and `}` so user input can be safely interpolated
    into a colortag-formatted string without being parsed as style tags.

    `_tui_colortag` treats `{name}…{/}` as style tags. Doubling `{` → `{{`
    and `}` → `}}` is the colortag grammar's literal-brace escape and the
    parser converts each pair back to a single brace. Apply this at the
    render boundary on any string sourced from user input or external data.
    """
    if not s:
        return s
    return s.replace("{", "{{").replace("}", "}}")


# Double-line box-drawing glyphs.
_TUI_BOX = {
    "tl": "╔", "tr": "╗", "bl": "╚", "br": "╝",
    "h":  "═", "v":  "║",
}


def _tui_box_lines(
    *,
    width: int,
    body: list[str],
    title: str | None = None,
    pin: str | None = None,
) -> list[str]:
    """Return a list of length-`width` strings forming a double-line box.

    Each body line is padded (right) or truncated to interior width (= width-2).
    Title goes left: ╔═ title ═══╗. Pin goes right-adjacent: ╔═ title ═ pin ═╗.
    If both won't fit, drop pin; if title won't fit, drop title.

    Callers who need colored glyphs should wrap the returned strings via
    _tui_colortag on the outside — this function emits plain text.
    """
    if width < 4:
        raise ValueError(f"box width too small: {width}")
    H, V, TL, TR, BL, BR = (
        _TUI_BOX["h"], _TUI_BOX["v"],
        _TUI_BOX["tl"], _TUI_BOX["tr"],
        _TUI_BOX["bl"], _TUI_BOX["br"],
    )
    interior = width - 2

    # Top border assembly
    def _top() -> str:
        if title is None:
            return TL + H * interior + TR
        t_seg = f" {title} "
        # Can we fit both title and pin?
        if pin is not None:
            p_seg = f" {pin} "
            # Layout: TL + H + t_seg + H*fill + p_seg + H + TR
            # width = 1 + 1 + len(t_seg) + fill + len(p_seg) + 1 + 1
            # fill = width - 4 - len(t_seg) - len(p_seg)
            fill = width - 4 - len(t_seg) - len(p_seg)
            if fill >= 1:
                return TL + H + t_seg + H * fill + p_seg + H + TR
        # Pin dropped (or absent) — fit just the title.
        # Layout: TL + H + t_seg + H*fill + TR
        # width = 1 + 1 + len(t_seg) + fill + 1
        fill = width - 3 - len(t_seg)
        if fill >= 1:
            return TL + H + t_seg + H * fill + TR
        # Title too long — fall back to plain border.
        return TL + H * interior + TR

    top = _top()
    bot = BL + H * interior + BR
    body_rows = []
    for line in body:
        if len(line) > interior:
            line = line[:interior - 1] + "…"
        body_rows.append(V + line + " " * (interior - len(line)) + V)
    return [top, *body_rows, bot]


def _tui_bar_string(pct: float, width: int) -> str:
    """Render a filled/empty bar as a string of `█` and `░`.

    Coloring is the caller's job — wrap with _tui_colortag or Text.append(style=).
    """
    if width <= 0:
        return ""
    p = max(0.0, min(100.0, float(pct)))
    full = round((p / 100.0) * width)
    return "█" * full + "░" * (width - full)


def _tui_bar_color(pct: float, *, thresholds=(70.0, 90.0)) -> str:
    """Return the theme style name for the bar based on usage thresholds.

    Default thresholds match the reference design (green <70, yellow 70-90,
    red >=90). Returns one of: 'bar.ok', 'bar.warn', 'bar.bad'.
    """
    low, high = thresholds
    if pct >= high:
        return "bar.bad"
    if pct >= low:
        return "bar.warn"
    return "bar.ok"


_TUI_SPARK_GLYPHS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def _tui_sparkline_inline(points: list[int]) -> str:
    """Map 1..8 to `_TUI_SPARK_GLYPHS`; clamp out-of-range into the 0..7 index."""
    if not points:
        return ""
    return "".join(_TUI_SPARK_GLYPHS[max(0, min(7, p - 1))] for p in points)


def _tui_sparkline_big(points: list[int]) -> str:
    """Render a 3-row block chart, 2 chars wide per point, space-separated.

    Point values 1..8 scaled to a 0..9 height; distributed top-down across
    three segments each taking values 0..3; height-per-segment maps to
    {0:'  ', 1:'▂▂', 2:'▄▄', 3:'██'}.
    """
    if not points:
        return "\n\n"
    rows: list[list[str]] = [[], [], []]
    glyph_map = ["  ", "▂▂", "▄▄", "██"]
    for p in points:
        h = max(1, min(8, int(p))) / 8 * 9  # 0..9
        pieces = [0, 0, 0]
        for i in (2, 1, 0):
            if h >= 3:
                pieces[i] = 3
                h -= 3
            elif h >= 2:
                pieces[i] = 2
                h = 0
            elif h >= 1:
                pieces[i] = 1
                h = 0
        for r_idx in range(3):
            rows[r_idx].append(glyph_map[pieces[r_idx]])
    return "\n".join(" ".join(r) for r in rows)


def _tui_width_bucket(width: int) -> str:
    """Pick a layout bucket from terminal width.

    - >= 120: 'wide'    (full design, 120×36 as primary)
    - 100..119: 'compact' (drops Model/Project in A sessions, 4wk trend in B)
    - 80..99:  'narrow'  (same rules as compact + shows a narrow-warning line)
    - < 80:    'refuse'  (error message, exit 1)
    """
    if width >= 120:
        return "wide"
    if width >= 100:
        return "compact"
    if width >= 80:
        return "narrow"
    return "refuse"


# -------- data layer -----------------------------------------------------
# Dataclasses produced by the sync thread and consumed by the render
# thread. Treat DataSnapshot as immutable — the sync thread publishes a
# new instance and the renderer swaps the reference atomically.


@dataclass
class TuiCurrentWeek:
    week_start_at: dt.datetime
    week_end_at: dt.datetime
    used_pct: float
    five_hour_pct: float | None
    five_hour_resets_at: dt.datetime | None
    spent_usd: float
    dollars_per_percent: float | None
    latest_snapshot_at: dt.datetime
    # Freshness fields (Task C6). Computed by `_tui_build_current_week` via
    # `_freshness_label` against the configured oauth_usage thresholds. Default
    # None so fixture modules that construct `TuiCurrentWeek` directly without
    # populating these stay backwards-compatible — the renderer treats `None`
    # the same as `"fresh"` and hides the chip. Refs spec §3.4.
    freshness_label: str | None = None
    freshness_age: int | None = None
    # Current 5h block snapshot for the dashboard envelope (spec §4.1). Snake-case
    # dict with keys: block_start_at, seven_day_pct_at_block_start,
    # seven_day_pct_delta_pp, crossed_seven_day_reset. Populated by
    # `_tui_build_current_week` via `_select_current_block_for_envelope`; the
    # default `None` keeps fixture modules that construct TuiCurrentWeek
    # directly (without this field) backwards-compatible.
    five_hour_block: dict | None = None


@dataclass
class TuiTrendRow:
    week_label: str              # e.g. "Apr 14"
    week_start_at: dt.datetime
    used_pct: float | None       # None when the week has a cost snapshot
                                 # but no usage snapshot (phantom weeks)
    dollars_per_percent: float | None
    delta_dpp: float | None      # vs prior week
    spark_height: int            # 1..8 normalized
    is_current: bool


@dataclass
class WeeklyPeriodRow:
    """One subscription-week row for the dashboard's Weekly panel/modal.

    `models` is a list of `{model, display, chip, cost_usd, cost_pct}`
    dicts sorted by `cost_usd` descending. Pre-bucketed in Python so
    the React layer never re-derives per-model coloring.
    """
    label: str                          # "04-23" — MM-DD of the week start
    cost_usd: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    used_pct: float | None              # from weekly_usage_snapshots overlay
    dollar_per_pct: float | None        # cost / used_pct when used_pct > 0
    delta_cost_pct: float | None        # (cost - prev_cost) / prev_cost
    is_current: bool
    models: list[dict[str, Any]]
    week_start_at: str                  # ISO-8601 with tz, from SubWeek.start_ts
    week_end_at: str                    # ISO-8601 with tz, from SubWeek.end_ts


@dataclass
class MonthlyPeriodRow:
    """One calendar-month row for the dashboard's Monthly panel/modal."""
    label: str                          # "YYYY-MM"
    cost_usd: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    delta_cost_pct: float | None
    is_current: bool
    models: list[dict[str, Any]]


@dataclass
class BlocksPanelRow:
    """One row of the dashboard's Blocks panel.

    Subset of the `Block` dataclass — drops token counts (panel is
    cost-driven; tokens belong to a future modal), drops `entries_count`
    / `is_gap` / `burn_rate` / `projection` (panel doesn't render them),
    and pre-formats `label` server-side for the local-tz "HH:MM MMM DD"
    display.
    """
    start_at: str          # ISO-8601 UTC
    end_at: str            # ISO-8601 UTC, start_at + 5h
    anchor: str            # 'recorded' | 'heuristic'
    is_active: bool        # now_utc < end_at AND entries_count > 0
    cost_usd: float
    models: list[dict[str, Any]]   # ModelCostRow shape, sorted desc by cost
    label: str             # "HH:MM MMM DD" in local tz, e.g. "14:00 Apr 26"


@dataclass
class DailyPanelRow:
    """One row of the dashboard's Daily heatmap panel.

    `intensity_bucket` is the server-computed quintile bucket (0..5) —
    bucket 0 is reserved for zero-cost days; buckets 1..5 are quintiles
    over non-zero days.

    v2.3: Added per-day token rollup + `cache_hit_pct` so the Daily
    detail modal can surface the same fields the CLI's `daily` command
    shows. Defaults preserve compatibility with `_empty_dashboard_snapshot`
    and any pre-v2.3 fixture that omits the new fields.
    """
    date: str              # local-tz YYYY-MM-DD
    label: str             # "MM-DD" — pre-formatted, mirrors Weekly/Monthly idiom
    cost_usd: float
    is_today: bool
    intensity_bucket: int  # 0..5
    models: list[dict[str, Any]]   # ModelCostRow shape, sorted desc by cost
    # ---- v2.3 additions: Daily modal token + cache rollup ----
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    cache_hit_pct: float | None = None


@dataclass
class TuiSessionRow:
    started_at: dt.datetime
    duration_minutes: float
    model_primary: str           # first model used in the session
    cost_usd: float
    cache_hit_pct: float | None
    project_label: str           # basename of project_path
    session_id: str              # full session UUID (v2: needed for session-detail modal)


@dataclass
class TuiPercentMilestone:
    """One row in the Current-Week per-percent modal (spec §4.6.1)."""
    percent: int                           # 1..100
    crossed_at: dt.datetime                # captured_at_utc
    cumulative_cost_usd: float
    marginal_cost_usd: float | None
    five_hour_pct_at_crossing: float | None


def _tui_build_percent_milestones(
    conn: sqlite3.Connection,
) -> list[TuiPercentMilestone]:
    """Return per-percent crossings for the current week's ACTIVE
    segment, ascending by percent.

    Resolves `week_start_date` from the latest `weekly_usage_snapshots` row
    — the same path `cmd_percent_breakdown` takes. The post-override
    `TuiCurrentWeek.week_start_at` is NOT suitable here: after a mid-week
    reset, `_apply_midweek_reset_override` shifts that datetime forward to
    the reset instant, whose `.date()` no longer matches the `week_start_date`
    under which milestones were recorded.

    v1.7.2: when a `week_reset_events` row exists for the snapshot's
    `week_end_at`, narrow to the active segment so the dashboard /
    TUI milestone panel stays coherent with the already-credit-aware
    header. ``active_segment = 0`` (sentinel) preserves legacy
    behavior on un-credited weeks.

    Returns [] if no usage snapshot exists, OR if the active segment
    has no milestone rows yet (post-credit "fresh" state).
    """
    latest = conn.execute(
        "SELECT week_start_date, week_end_at FROM weekly_usage_snapshots "
        "WHERE week_end_at IS NOT NULL "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        # Legacy fallback: a snapshot without week_end_at can still have
        # milestones — keep the prior behavior in that path.
        latest = conn.execute(
            "SELECT week_start_date, NULL AS week_end_at "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return []

    # Resolve active segment via the canonical end_at.
    active_segment = 0
    if latest["week_end_at"]:
        try:
            canon_end = _canonicalize_optional_iso(
                latest["week_end_at"], "tui.pm.cur"
            )
        except (AttributeError, ValueError):
            canon_end = None
        if canon_end:
            seg_row = conn.execute(
                "SELECT id FROM week_reset_events "
                "WHERE new_week_end_at = ? "
                "ORDER BY id DESC LIMIT 1",
                (canon_end,),
            ).fetchone()
            if seg_row is not None:
                active_segment = int(seg_row["id"])

    rows = [
        r for r in get_milestones_for_week(conn, latest["week_start_date"])
        if int(r["reset_event_id"] or 0) == active_segment
    ]
    out: list[TuiPercentMilestone] = []
    for r in rows:
        try:
            crossed = parse_iso_datetime(r["captured_at_utc"], "captured_at_utc")
        except ValueError:
            continue
        out.append(TuiPercentMilestone(
            percent=int(r["percent_threshold"]),
            crossed_at=crossed,
            cumulative_cost_usd=float(r["cumulative_cost_usd"]),
            marginal_cost_usd=(float(r["marginal_cost_usd"])
                               if r["marginal_cost_usd"] is not None else None),
            five_hour_pct_at_crossing=(float(r["five_hour_percent_at_crossing"])
                                       if r["five_hour_percent_at_crossing"] is not None
                                       else None),
        ))
    return out


@dataclass
class DataSnapshot:
    """All data needed to render one TUI frame. Produced by sync thread,
    consumed by main thread. Treat as immutable."""
    current_week: TuiCurrentWeek | None
    forecast: Any | None          # ForecastOutput from _compute_forecast
    trend: list[TuiTrendRow]
    sessions: list[TuiSessionRow]
    last_sync_at: float | None    # monotonic (time.monotonic())
    last_sync_error: str | None
    generated_at: dt.datetime     # wall-clock UTC for displayed timestamps
    # ---- v2 additions (spec §4.5) ----
    percent_milestones: list[TuiPercentMilestone] = field(default_factory=list)
    weekly_history: list[TuiTrendRow] = field(default_factory=list)
    # ---- v2.1 additions: dashboard Weekly / Monthly panels ----
    weekly_periods:  list[WeeklyPeriodRow]  = field(default_factory=list)
    monthly_periods: list[MonthlyPeriodRow] = field(default_factory=list)
    # ---- v2.2 additions: dashboard Blocks / Daily panels ----
    blocks_panel: list[BlocksPanelRow] = field(default_factory=list)
    daily_panel:  list[DailyPanelRow]  = field(default_factory=list)
    # ---- threshold-actions T5: snapshot alerts envelope array ----
    # Populated at sync-thread snapshot-build time by
    # `_build_alerts_envelope_array(conn)`. Single source of truth for
    # both the dashboard panel (slices to 10) and the modal (renders all
    # 100). Empty list when alerts feature is disabled, no rows have
    # `alerted_at` set, or DB read fails (sub-build catches the exception
    # and records it on `last_sync_error`). Stored as
    # already-envelope-shaped dicts so `snapshot_to_envelope` stays a
    # pure renderer (no DB I/O on the dashboard hot path; mirrors how
    # `current_week.five_hour_block` is precomputed via
    # `_select_current_block_for_envelope`).
    alerts: list[dict] = field(default_factory=list)

    @classmethod
    def synthesize_for_marketing(cls, *, as_of_iso: str) -> "DataSnapshot":
        """Build a deterministic DataSnapshot for README screenshot pipelines.

        Used by tests/fixtures/readme/tui_snapshot.py when run via
        `cctally tui --render-once --snapshot-module ...`. Numbers are
        narratively coherent with the marketing fixture's stats.db /
        cache.db so the TUI shot, the dashboard shots, and the report/
        forecast SVGs all tell the same story (current-week 53% used,
        $28.62 spent, in-progress Thursday with a WARN ~104% projection).

        Mirrors the 8-week trend table seeded by build-readme-fixtures.py
        (`_populate_weeks`) so the TUI's Trend panel shows the same
        $/1% arc the dashboard's Trend modal does.

        Dev-only — production code paths never invoke this. Kept here so
        the `DataSnapshot` shape stays the single source of truth (mirror
        any future field additions to keep marketing renders in sync).
        """
        as_of = dt.datetime.strptime(
            as_of_iso, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        # Anchor the current subscription week to Monday 00:00 UTC of
        # `as_of`'s containing week so the marketing copy ("week of …")
        # lines up with the stats.db rows seeded by build-readme-fixtures.
        week_start = (as_of - dt.timedelta(days=as_of.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        week_end = week_start + dt.timedelta(days=7)
        used_pct = 53.0
        spent_usd = 28.62
        cw = TuiCurrentWeek(
            week_start_at=week_start,
            week_end_at=week_end,
            used_pct=used_pct,
            five_hour_pct=36.0,
            five_hour_resets_at=as_of.replace(minute=0, second=0, microsecond=0)
                + dt.timedelta(hours=3),
            spent_usd=spent_usd,
            dollars_per_percent=spent_usd / used_pct,
            latest_snapshot_at=as_of,
            freshness_label="fresh",
            freshness_age=12,
            five_hour_block=None,
        )
        # ---- Forecast: WARN, ~98% projected, fits within modal width.
        # The TUI's verdict mapping (`_tui_verdict_of`) reads
        # `final_percent_high >= 100` as OVER, `>= 90` as WARN. We want
        # WARN here, so synthesize r_avg/r_recent that land both
        # projection bars in the 90s. Note: this is the TUI-only render
        # path; the dashboard re-derives forecast from the seeded
        # fixture DB via `snapshot_to_envelope` and lands at ~103%
        # there (which the dashboard's verdict map calls WARN already
        # via the `cap` enum, with no >=100 threshold split).
        elapsed_hours = (as_of - week_start).total_seconds() / 3600.0
        remaining_hours = max(0.0, (week_end - as_of).total_seconds() / 3600.0)
        remaining_days = remaining_hours / 24.0
        # Headline projection target: ~98% → r_avg = (98 - 53) / 82 ≈ 0.549.
        r_avg = (98.0 - used_pct) / remaining_hours if remaining_hours > 0 else 0.0
        # Recent 24h slightly lower: ~94% → r_recent = (94 - 53) / 82 ≈ 0.500.
        r_recent = (94.0 - used_pct) / remaining_hours if remaining_hours > 0 else 0.0
        p_24h_ago = max(0.0, used_pct - r_recent * 24.0)
        dpp = spent_usd / used_pct
        final_low = used_pct + r_recent * remaining_hours
        final_high = used_pct + r_avg * remaining_hours
        # Two BudgetRows mirroring the TUI's hard-coded targets [100, 90].
        budgets = [
            BudgetRow(
                target_percent=100,
                pct_headroom=100.0 - used_pct,
                dollars_per_day=((100.0 - used_pct) * dpp / remaining_days)
                                 if remaining_days > 0 else None,
                percent_per_day=((100.0 - used_pct) / remaining_days)
                                 if remaining_days > 0 else None,
            ),
            BudgetRow(
                target_percent=90,
                pct_headroom=90.0 - used_pct,
                dollars_per_day=((90.0 - used_pct) * dpp / remaining_days)
                                 if remaining_days > 0 else None,
                percent_per_day=((90.0 - used_pct) / remaining_days)
                                 if remaining_days > 0 else None,
            ),
        ]
        forecast_inputs = ForecastInputs(
            now_utc=as_of,
            week_start_at=week_start,
            week_end_at=week_end,
            elapsed_hours=elapsed_hours,
            elapsed_fraction=elapsed_hours / 168.0,
            remaining_hours=remaining_hours,
            remaining_days=remaining_days,
            p_now=used_pct,
            five_hour_percent=36.0,
            spent_usd=spent_usd,
            snapshot_count=12,
            latest_snapshot_at=as_of,
            p_24h_ago=p_24h_ago,
            t_24h_actual_hours=24.0,
            dollars_per_percent=dpp,
            dollars_per_percent_source="this_week",
            confidence="high",
            low_confidence_reasons=[],
        )
        forecast = ForecastOutput(
            inputs=forecast_inputs,
            r_avg=r_avg,
            r_recent=r_recent,
            final_percent_low=final_low,
            final_percent_high=final_high,
            projected_cap=final_high >= 100.0,
            already_capped=False,
            cap_at=None,
            budgets=budgets,
        )
        # ---- Trend: 8 weeks oldest-first, mirroring the
        # `_populate_weeks` series in bin/build-readme-fixtures.py.
        # Spark heights computed the same way `_tui_build_trend` does
        # (normalize $/1% to 1..8 across the window).
        weekly_series = [
            (38.0, 24.70),
            (41.0, 25.83),
            (44.0, 25.96),
            (47.0, 24.91),
            (50.0, 25.00),
            (53.0, 22.79),
            (56.0, 25.20),
            (used_pct, spent_usd),  # current week
        ]
        dpps = [round(c / p, 4) for p, c in weekly_series]
        lo, hi = min(dpps), max(dpps)
        span = (hi - lo) or 1e-9
        trend: list[TuiTrendRow] = []
        prev_dpp: float | None = None
        for i, ((pct, cost), wd) in enumerate(zip(weekly_series, dpps)):
            offset = 7 - i
            wstart_dt = week_start - dt.timedelta(days=7 * offset)
            spark = max(1, min(8, int(round((wd - lo) / span * 7)) + 1))
            delta = (wd - prev_dpp) if prev_dpp is not None else None
            trend.append(TuiTrendRow(
                week_label=wstart_dt.strftime("%b %d"),
                week_start_at=wstart_dt,
                used_pct=pct,
                dollars_per_percent=wd,
                delta_dpp=delta,
                spark_height=spark,
                is_current=(i == 7),
            ))
            prev_dpp = wd
        # ---- Sessions: 6 recent rows spanning 4 projects + 3 models,
        # ordered last-activity desc (matches the aggregator's natural
        # output, which the TUI's default sort preserves).
        sessions = [
            TuiSessionRow(
                started_at=as_of - dt.timedelta(hours=1, minutes=22),
                duration_minutes=46.0,
                model_primary="claude-sonnet-4-6",
                cost_usd=2.84,
                cache_hit_pct=87.5,
                project_label="web-app",
                session_id="sess-web-app-00",
            ),
            TuiSessionRow(
                started_at=as_of - dt.timedelta(hours=3, minutes=10),
                duration_minutes=72.0,
                model_primary="claude-opus-4-7",
                cost_usd=4.97,
                cache_hit_pct=72.0,
                project_label="api-gateway",
                session_id="sess-api-gateway-01",
            ),
            TuiSessionRow(
                started_at=as_of - dt.timedelta(hours=5, minutes=44),
                duration_minutes=33.0,
                model_primary="claude-haiku-4-5-20251001",
                cost_usd=0.62,
                cache_hit_pct=91.3,
                project_label="data-pipeline",
                session_id="sess-data-pipeline-02",
            ),
            TuiSessionRow(
                started_at=as_of - dt.timedelta(hours=8, minutes=5),
                duration_minutes=58.0,
                model_primary="claude-sonnet-4-6",
                cost_usd=3.41,
                cache_hit_pct=79.8,
                project_label="mobile-client",
                session_id="sess-mobile-client-00",
            ),
            TuiSessionRow(
                started_at=as_of - dt.timedelta(days=1, hours=2),
                duration_minutes=104.0,
                model_primary="claude-opus-4-7",
                cost_usd=6.18,
                cache_hit_pct=68.4,
                project_label="web-app",
                session_id="sess-web-app-01",
            ),
            TuiSessionRow(
                started_at=as_of - dt.timedelta(days=1, hours=6, minutes=30),
                duration_minutes=29.0,
                model_primary="claude-sonnet-4-6",
                cost_usd=1.55,
                cache_hit_pct=84.1,
                project_label="api-gateway",
                session_id="sess-api-gateway-02",
            ),
        ]
        return cls(
            current_week=cw,
            forecast=forecast,
            trend=trend,
            sessions=sessions,
            last_sync_at=None,
            last_sync_error=None,
            generated_at=as_of,
        )


@dataclass
class RuntimeState:
    """Main-thread-only UI state. Not shared with sync thread."""
    variant: str                  # 'conventional' | 'expressive'
    focus_index: int              # 0..3 for A; always 3 (sessions) for B
    session_scroll: int           # topmost visible session row index
    show_help: bool
    toast: tuple[str, float] | None   # (message, monotonic_expiry)
    color_enabled: bool
    tz: str                        # 'utc' | 'local' | IANA name (legacy token; F4 moved _tui_format_started to consume display_tz directly. Field retained for back-compat call sites.)
    # Resolved display timezone (per spec §2: --tz flag > config.display.tz > host).
    # ZoneInfo means "render in this zone"; None means "host-local via bare
    # astimezone()". Threaded through renderers that call format_display_dt.
    display_tz: "ZoneInfo | None" = None
    # ---- v2 additions (spec §3.5, §4.4) ----
    sort_key: str = "last-activity"      # 'last-activity'|'cost'|'duration'|'model'|'project'
    filter_term: str | None = None        # None = no active filter
    search_term: str | None = None        # None = no search; "" = active but empty buffer
    search_matches: list[int] = field(default_factory=list)  # indices into post-filter+sort list
    search_index: int = 0                 # current match in search_matches[]
    input_mode: str | None = None         # 'filter' | 'search' | None
    input_buffer: str = ""                # live typing during input mode
    modal_kind: str | None = None         # 'current_week'|'forecast'|'trend'|'session'|None
    modal_scroll: int = 0                 # topmost visible modal content line
    # One-shot "snap to bottom on first render" flag for modals that default to
    # the newest rows (trend, current_week). Set by modal openers; cleared by
    # the first builder call that performs the snap. Avoids reusing
    # modal_scroll==0 as a sentinel — otherwise scrolling to the top would
    # bounce the view back to the bottom on the next redraw.
    modal_snap_pending: bool = False
    # ---- v2.4.4 fixture-injection hook (dev-only) ----
    session_detail_override: Any = None   # TuiSessionDetail | None — injected by fixtures only
    # Memoized session detail to avoid rebuilding (365-day rescan + re-aggregate)
    # on every modal redraw tick. Key: (session_id, snap.generated_at).
    session_detail_cache: Any = None      # tuple[str, dt.datetime, TuiSessionDetail | None] | None

    @classmethod
    def initial(cls, args) -> "RuntimeState":
        no_color_env = "NO_COLOR" in os.environ
        return cls(
            variant=args.variant,
            focus_index=3,        # sessions focused by default (design choice)
            session_scroll=0,
            show_help=False,
            toast=None,
            color_enabled=not (args.no_color or no_color_env),
            tz=args.tz,
            display_tz=getattr(args, "_resolved_tz", None),
        )


def _tui_build_current_week(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
) -> TuiCurrentWeek | None:
    """Build the TuiCurrentWeek from the latest snapshot + live cost.

    Returns None when no current-week usage snapshot exists.
    """
    fetched = _fetch_current_week_snapshots(conn, now_utc)
    if fetched is None:
        return None
    week_start_at, week_end_at, samples = fetched
    if not samples:
        return None
    # Mirror the reset override applied by `_load_forecast_inputs` so the
    # Current Week card's spent_usd and $/1% reflect the post-reset window.
    week_start_at, samples = _apply_midweek_reset_override(
        conn, week_start_at, week_end_at, samples
    )
    if not samples:
        return None
    # samples tuple shape: (captured_at_utc, weekly_percent, five_hour_percent).
    # See _fetch_current_week_snapshots at bin/cctally:9122
    # (lines ~9189-9194 and ~9221-9226). That helper does not surface
    # five_hour_resets_at, so do a targeted lookup here for the freshest
    # non-NULL reset timestamp on the current week.
    latest = samples[-1]
    used_pct = float(latest[1])
    five_hr_pct = float(latest[2]) if latest[2] is not None else None
    spent = _sum_cost_for_range(
        week_start_at, now_utc, mode="auto", skip_sync=skip_sync
    )
    dpp = (spent / used_pct) if used_pct > 0 else None
    # Collect every textual variant of week_start_at that parses to the same
    # instant — mirrors `_fetch_current_week_snapshots` lines 9199-9210 so
    # legacy local-offset rows and newly UTC-canonicalized rows both contribute.
    ws_texts = conn.execute(
        "SELECT DISTINCT week_start_at FROM weekly_usage_snapshots "
        "WHERE week_start_at IS NOT NULL"
    ).fetchall()
    matching_ws_texts: list[str] = []
    for r in ws_texts:
        try:
            rws = parse_iso_datetime(r[0], "week_start_at")
        except ValueError:
            continue
        if rws == week_start_at:
            matching_ws_texts.append(r[0])
    five_hr_resets_at: dt.datetime | None = None
    if matching_ws_texts:
        placeholders = ",".join("?" * len(matching_ws_texts))
        reset_row = conn.execute(
            f"SELECT five_hour_resets_at FROM weekly_usage_snapshots "
            f"WHERE week_start_at IN ({placeholders}) "
            f"  AND five_hour_resets_at IS NOT NULL "
            f"ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            tuple(matching_ws_texts),
        ).fetchone()
        if reset_row is not None:
            try:
                five_hr_resets_at = parse_iso_datetime(
                    reset_row[0], "five_hour_resets_at"
                )
            except ValueError:
                five_hr_resets_at = None
            # Suppress stale resets that have already elapsed so renderers
            # don't show "resets 0h 00m" or a negative duration at the boundary.
            if five_hr_resets_at is not None and five_hr_resets_at <= now_utc:
                five_hr_resets_at = None
    # Freshness — compute label/age from latest snapshot vs. now using the
    # configured oauth_usage thresholds. Mirrors the dashboard envelope's
    # cw_freshness derivation in `snapshot_to_envelope`. Refs spec §3.4.
    captured = latest[0]
    if isinstance(captured, dt.datetime):
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=dt.timezone.utc)
        age_s = max(0.0, (now_utc - captured).total_seconds())
        try:
            _fresh_cfg = _get_oauth_usage_config(load_config())
        except Exception:
            _fresh_cfg = _OAUTH_USAGE_DEFAULTS
        freshness_label = _freshness_label(age_s, _fresh_cfg)
        freshness_age = int(age_s)
    else:
        freshness_label = None
        freshness_age = None
    return TuiCurrentWeek(
        week_start_at=week_start_at,
        week_end_at=week_end_at,
        used_pct=used_pct,
        five_hour_pct=five_hr_pct,
        five_hour_resets_at=five_hr_resets_at,
        spent_usd=float(spent),
        dollars_per_percent=dpp,
        latest_snapshot_at=latest[0],
        freshness_label=freshness_label,
        freshness_age=freshness_age,
        five_hour_block=_select_current_block_for_envelope(
            conn, current_used_pct=used_pct, now_utc=now_utc,
        ),
    )


def _tui_build_forecast(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
):
    """Call into existing forecast internals. Returns a ForecastOutput or None."""
    inputs = _load_forecast_inputs(conn, now_utc, skip_sync=skip_sync)
    if inputs is None:
        return None
    return _compute_forecast(inputs, [100, 90])


def _tui_build_trend(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,  # noqa: ARG001 — unused today, kept for API symmetry
    count: int = 8,
    display_tz: "ZoneInfo | None" = None,
) -> list[TuiTrendRow]:
    """Build the last `count` trend rows, chronological (oldest first).

    `cmd_report` inlines its row build rather than delegating to a helper,
    so instead of refactoring the subcommand we call the same underlying
    loaders (`get_recent_weeks` + `get_latest_usage_for_week` +
    `get_latest_cost_for_week`) directly here. Output for the shared
    columns (`week_start_at`, `used_pct`, `dollars_per_percent`) matches
    `cmd_report` byte-for-byte — verified in the bundle regression diff.
    """
    # `get_recent_weeks` returns WeekRef rows DESC by week_start_date.
    week_refs = get_recent_weeks(conn, max(1, count))

    # Figure out which week_ref corresponds to the current subscription week.
    # Uses the same key derivation `cmd_report` does — latest usage snapshot's
    # week_start_date, canonicalized through `_get_canonical_boundary_for_date`.
    latest_usage = conn.execute(
        "SELECT week_start_date, week_end_date "
        "FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    current_key: str | None = None
    if latest_usage is not None:
        current_key = latest_usage["week_start_date"]

    # Build an intermediate list of (week_ref, used_pct, dpp) in oldest-first
    # chronological order.
    chrono = list(reversed(week_refs))
    intermediate: list[tuple[Any, float | None, float | None]] = []
    for week_ref in chrono:
        usage = get_latest_usage_for_week(conn, week_ref)
        # See cmd_report for why reset-affected weeks skip the cost cache
        # and live-compute from session_entries over the effective range.
        if _week_ref_has_reset_event(conn, week_ref):
            cost_usd = _compute_cost_for_weekref(week_ref)
        else:
            cost = get_latest_cost_for_week(conn, week_ref)
            cost_usd = float(cost["cost_usd"]) if cost else None
        percent = float(usage["weekly_percent"]) if usage else None
        ratio = (cost_usd / percent) if (
            cost_usd is not None and percent and percent > 0
        ) else None
        intermediate.append((week_ref, percent, ratio))

    # Normalize dpp into spark heights 1..8 across the window.
    dpps = [d for _, _, d in intermediate if d is not None]
    if dpps:
        lo, hi = min(dpps), max(dpps)
        span = (hi - lo) or 1e-9
    else:
        lo, hi, span = 0.0, 1.0, 1e-9

    out: list[TuiTrendRow] = []
    prev_dpp: float | None = None
    for week_ref, percent, dpp in intermediate:
        delta = (dpp - prev_dpp) if (dpp is not None and prev_dpp is not None) else None
        spark = 1
        if dpp is not None:
            spark = int(round((dpp - lo) / span * 7)) + 1
            spark = max(1, min(8, spark))
        # WeekRef.week_start is a date; synthesize a UTC datetime so
        # TuiTrendRow carries a timezone-aware instant (prefer the explicit
        # week_start_at if present).
        if week_ref.week_start_at:
            week_start_dt = parse_iso_datetime(
                week_ref.week_start_at, "week_start_at"
            )
            week_label = format_display_dt(
                week_start_dt, display_tz, fmt="%b %d", suffix=False,
            )
        else:
            week_start_dt = dt.datetime.combine(
                week_ref.week_start, dt.time(0, 0), dt.timezone.utc
            )
            # No real boundary instant — format the calendar date directly so
            # localizing midnight-UTC doesn't shift it to the prior day in
            # zones west of UTC (e.g. 2026-04-14 → "Apr 13" in America/New_York).
            week_label = week_ref.week_start.strftime("%b %d")
        out.append(TuiTrendRow(
            week_label=week_label,
            week_start_at=week_start_dt,
            # Preserve None when no usage snapshot exists for this week —
            # matches `cmd_report`'s "n/a" rendering (9980) and avoids
            # fabricating a 0.0% row for the phantom-week case (cost
            # snapshot present, usage snapshot absent).
            used_pct=float(percent) if percent is not None else None,
            dollars_per_percent=dpp,
            delta_dpp=delta,
            spark_height=spark,
            is_current=(current_key is not None and week_ref.key == current_key),
        ))
        if dpp is not None:
            prev_dpp = dpp
    return out


def _tui_build_weekly_history(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    count: int = 12,
    display_tz: "ZoneInfo | None" = None,
) -> list[TuiTrendRow]:
    """Return the last `count` weeks for the Trend modal (spec §4.6.3).

    Same data shape as `_tui_build_trend` (the panel) — just more rows.
    The panel renders 8; the modal renders up to 12. Wrapping rather
    than parameterising the call site keeps the snapshot fields
    semantically distinct (panel data vs. modal data) and avoids
    accidental cross-contamination.
    """
    return _tui_build_trend(
        conn, now_utc, skip_sync=skip_sync, count=count, display_tz=display_tz,
    )


def _tui_build_sessions(
    now_utc: dt.datetime,
    *,
    limit: int = 100,
    skip_sync: bool = False,
) -> list[TuiSessionRow]:
    """Load the last `limit` Claude sessions (merged across resumes).

    Started-time descending (matches `_aggregate_claude_sessions` —
    sorted by `last_activity` DESC). Uses the same aggregator as the
    `session` subcommand, so row identity and project labels match
    `cctally session --json` exactly.

    When `skip_sync=True`, honors the parent's `--no-sync` intent: no
    ingest pass, just read whatever is already cached.
    """
    # Bounded scan window — the sessions pane promises "last `limit`". A
    # 365-day scan covers virtually all users (even one-session-every-few-days
    # sparseness still nets the cap). Bounded rather than all-history so
    # sync-tick cost stays predictable on heavy DBs: the aggregator runs
    # on every entry in the window before slicing.
    range_start = now_utc - dt.timedelta(days=365)
    entries = get_claude_session_entries(range_start, now_utc, skip_sync=skip_sync)
    sessions = _aggregate_claude_sessions(entries)   # last_activity desc
    out: list[TuiSessionRow] = []
    for s in sessions[:limit]:
        duration_min = (s.last_activity - s.first_activity).total_seconds() / 60.0
        total_read = s.cache_read_tokens
        total_io = s.input_tokens + s.cache_creation_tokens + s.cache_read_tokens
        cache_pct = (total_read / total_io * 100) if total_io > 0 else None
        out.append(TuiSessionRow(
            started_at=s.first_activity,
            duration_minutes=duration_min,
            model_primary=(s.models[0] if s.models else "—"),
            cost_usd=s.cost_usd,
            cache_hit_pct=cache_pct,
            project_label=os.path.basename(s.project_path) or s.project_path,
            session_id=s.session_id,
        ))
    return out


@dataclass
class TuiSessionDetail:
    """Detailed view for the Session modal (spec §4.6.4).

    Built on demand when the modal opens — not part of DataSnapshot.
    """
    session_id: str
    started_at: dt.datetime
    last_activity_at: dt.datetime
    duration_minutes: float
    project_label: str
    project_path: str                     # full cwd
    source_paths: list[str]               # JSONL file paths (for resumed sessions, may be >1)
    models: list[tuple[str, str]]         # [(model_name, role)] role in {"primary","secondary"}
    input_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cache_hit_pct: float | None
    cost_per_model: list[tuple[str, float]]   # [(model_name, cost_usd)]
    cost_total_usd: float


def _tui_build_session_detail(
    session_id: str,
    *,
    now_utc: dt.datetime | None = None,
) -> TuiSessionDetail | None:
    """Look up one session by ID; return None if not found.

    Reuses the same `get_claude_session_entries` + `_aggregate_claude_sessions`
    pipeline as `_tui_build_sessions` but filters down to the matching ID.
    Bounded scan window matches the panel builder (365 days).
    """
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    range_start = now_utc - dt.timedelta(days=365)
    entries = get_claude_session_entries(range_start, now_utc, skip_sync=True)
    sessions = _aggregate_claude_sessions(entries)
    match: Any | None = None
    for s in sessions:
        if s.session_id == session_id:
            match = s
            break
    if match is None:
        return None
    duration_min = (match.last_activity - match.first_activity).total_seconds() / 60.0
    total_read = match.cache_read_tokens
    total_io = match.input_tokens + match.cache_creation_tokens + match.cache_read_tokens
    cache_pct = (total_read / total_io * 100) if total_io > 0 else None
    # Build per-model rows. Role is "primary" for the first model seen in
    # this session (matches `_aggregate_claude_sessions` `models_order`),
    # "secondary" for the rest.
    models_with_role: list[tuple[str, str]] = []
    for i, m in enumerate(match.models):
        models_with_role.append((m, "primary" if i == 0 else "secondary"))
    # Per-model cost: prefer the aggregator's `model_breakdowns` (list of
    # dicts with `"model"` / `"cost"` keys, sorted by cost desc). Fall back
    # defensively to a single-row total if the attribute is missing or empty
    # so the modal stays renderable on any aggregator shape change.
    cost_per_model: list[tuple[str, float]] = []
    breakdowns = getattr(match, "model_breakdowns", None)
    if isinstance(breakdowns, list) and breakdowns:
        for mb in breakdowns:
            try:
                cost_per_model.append((str(mb["model"]), float(mb["cost"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not cost_per_model and match.models:
        # Single-model fallback: attribute total to primary.
        cost_per_model.append((match.models[0], float(match.cost_usd)))
    return TuiSessionDetail(
        session_id=match.session_id,
        started_at=match.first_activity,
        last_activity_at=match.last_activity,
        duration_minutes=duration_min,
        project_label=os.path.basename(match.project_path) or match.project_path,
        project_path=match.project_path,
        source_paths=list(match.source_paths or []),
        models=models_with_role,
        input_tokens=int(match.input_tokens),
        cache_creation_tokens=int(match.cache_creation_tokens),
        cache_read_tokens=int(match.cache_read_tokens),
        output_tokens=int(match.output_tokens),
        cache_hit_pct=cache_pct,
        cost_per_model=cost_per_model,
        cost_total_usd=float(match.cost_usd),
    )


def _tui_build_snapshot(
    *,
    now_utc: dt.datetime | None = None,
    skip_sync: bool = False,
    display_tz_pref_override: "str | None" = None,
) -> DataSnapshot:
    """Single-shot build of a DataSnapshot from the DB + cache.

    Runs in the sync thread. Catches exceptions per sub-build and records
    them on `last_sync_error` so the UI can surface them without crashing.

    ``display_tz_pref_override`` (F3): a canonical tz token (``"local"``
    / ``"utc"`` / IANA name) that overrides ``config.display.tz`` for
    the lifetime of this build. Used by ``cmd_dashboard`` when ``--tz``
    is supplied so the in-memory zone wins over the persisted config
    without modifying it. ``None`` means "respect config".
    """
    import time
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    # Resolve the display tz once per snapshot so labels rendered into
    # BlocksPanelRow / future panel rows share a single zone with the
    # envelope's `display` block. Routed through the shared
    # `_resolve_display_tz_obj` helper so this site, `_compute_display_block`,
    # and `_handle_get_block_detail` share identical fallback semantics
    # (one-shot stderr warning on local-resolution failure). Not threaded
    # into label-FOR-LOOKUP paths like `_aggregate_monthly` keys (out of
    # scope for Task 11).
    _build_display_tz = _resolve_display_tz_obj(
        _apply_display_tz_override(load_config(), display_tz_pref_override)
    )
    conn = open_db()
    try:
        errors: list[str] = []
        cw: TuiCurrentWeek | None = None
        fc: Any | None = None
        trend: list[TuiTrendRow] = []
        sessions: list[TuiSessionRow] = []
        milestones: list[TuiPercentMilestone] = []
        history: list[TuiTrendRow] = []
        weekly_periods: list[WeeklyPeriodRow] = []
        monthly_periods: list[MonthlyPeriodRow] = []
        blocks_panel: list[BlocksPanelRow] = []
        daily_panel:  list[DailyPanelRow]  = []
        alerts: list[dict] = []
        try:
            cw = _tui_build_current_week(conn, now_utc, skip_sync=skip_sync)
        except Exception as exc:
            errors.append(f"current-week: {exc}")
        try:
            fc = _tui_build_forecast(conn, now_utc, skip_sync=skip_sync)
        except Exception as exc:
            errors.append(f"forecast: {exc}")
        try:
            trend = _tui_build_trend(
                conn, now_utc, skip_sync=skip_sync, display_tz=_build_display_tz,
            )
        except Exception as exc:
            errors.append(f"trend: {exc}")
        try:
            # The sessions aggregator goes through
            # `get_claude_session_entries`, which runs `sync_cache` unless
            # `skip_sync=True` is threaded through. Honor the caller's
            # intent so `--no-sync` and the initial cache-only paint
            # both avoid ingest latency/lock contention.
            sessions = _tui_build_sessions(now_utc, skip_sync=skip_sync)
        except Exception as exc:
            errors.append(f"sessions: {exc}")
        # ---- v2 additions ----
        try:
            if cw is not None:
                milestones = _tui_build_percent_milestones(conn)
        except Exception as exc:
            errors.append(f"milestones: {exc}")
        try:
            history = _tui_build_weekly_history(
                conn, now_utc, skip_sync=skip_sync, display_tz=_build_display_tz,
            )
        except Exception as exc:
            errors.append(f"weekly-history: {exc}")
        # ---- v2.1 additions: dashboard Weekly / Monthly panels ----
        try:
            weekly_periods = _dashboard_build_weekly_periods(
                conn, now_utc, n=12, skip_sync=skip_sync
            )
        except Exception as exc:
            errors.append(f"weekly-periods: {exc}")
        try:
            monthly_periods = _dashboard_build_monthly_periods(
                conn, now_utc, n=12, skip_sync=skip_sync,
                display_tz=_build_display_tz,
            )
        except Exception as exc:
            errors.append(f"monthly-periods: {exc}")
        # ---- v2.2 additions: dashboard Blocks / Daily panels ----
        try:
            if cw is not None:
                blocks_panel = _dashboard_build_blocks_panel(
                    conn, now_utc,
                    week_start_at=cw.week_start_at,
                    week_end_at=cw.week_end_at,
                    skip_sync=skip_sync,
                    display_tz=_build_display_tz,
                )
        except Exception as exc:
            errors.append(f"blocks-panel: {exc}")
        try:
            daily_panel = _dashboard_build_daily_panel(
                conn, now_utc, n=30, skip_sync=skip_sync,
                display_tz=_build_display_tz,
            )
        except Exception as exc:
            errors.append(f"daily-panel: {exc}")
        # ---- threshold-actions T5: alerts envelope array ----
        # Precomputed at sync time so `snapshot_to_envelope` stays a pure
        # renderer (no DB I/O on the dashboard hot path; mirrors how
        # `current_week.five_hour_block` is precomputed via
        # `_select_current_block_for_envelope`).
        try:
            alerts = _build_alerts_envelope_array(conn)
        except Exception as exc:
            errors.append(f"alerts: {exc}")
        return DataSnapshot(
            current_week=cw,
            forecast=fc,
            trend=trend,
            sessions=sessions,
            last_sync_at=time.monotonic(),
            last_sync_error=("; ".join(errors) if errors else None),
            generated_at=now_utc,
            percent_milestones=milestones,
            weekly_history=history,
            weekly_periods=weekly_periods,
            monthly_periods=monthly_periods,
            blocks_panel=blocks_panel,
            daily_panel=daily_panel,
            alerts=alerts,
        )
    finally:
        conn.close()


def _tui_empty_snapshot(now_utc: dt.datetime) -> DataSnapshot:
    """First-paint placeholder with no data loaded yet."""
    return DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None, generated_at=now_utc,
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )


class TuiKeyReader:
    """Context manager for raw-mode stdin reads.

    Non-TTY input degrades gracefully — read() always returns None.
    """

    _ESC_MAP = {
        "[A": "up",    "[B": "down",
        "[C": "right", "[D": "left",
        "[5~": "pgup", "[6~": "pgdn",
        "[H":  "home", "[F":  "end",
    }

    def __init__(self) -> None:
        self._fd = None
        self._saved = None

    def __enter__(self):
        try:
            import termios, tty
        except ImportError:
            return self  # non-posix: degrade to null reader
        if not sys.stdin.isatty():
            return self
        try:
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            # Degrade gracefully on any setup error.
            self._fd = None
            self._saved = None
        return self

    def __exit__(self, *exc):
        if self._fd is not None and self._saved is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass

    # SS3 arrows (ESC O X) — sent by terminals in DECCKM "application" mode.
    _SS3_MAP = {
        b"A": "up",    b"B": "down",
        b"C": "right", b"D": "left",
        b"H": "home",  b"F": "end",
    }

    def read(self, timeout: float) -> str | None:
        """Blocking read up to `timeout` seconds. Returns a key name or char.

        Reads via os.read on the raw fd rather than sys.stdin.read, because
        the TextIOWrapper on sys.stdin buffers ahead: sys.stdin.read(1) pulls
        an ESC sequence like b"\\x1b[B" into Python's buffer as a block, and
        the follow-up select() on the fd then sees nothing and times out —
        causing the reader to mis-return "esc" and the handler to quit.
        """
        import select
        if not sys.stdin.isatty() or self._fd is None:
            return None
        fd = self._fd
        try:
            r, _, _ = select.select([fd], [], [], max(0.0, timeout))
            if not r:
                return None
            b = os.read(fd, 1)
        except Exception:
            return None
        if not b:
            return None
        if b == b"\x1b":  # ESC; possibly start of CSI or SS3 sequence
            # 50ms grace to distinguish lone ESC from an arrow sequence.
            try:
                r2, _, _ = select.select([fd], [], [], 0.05)
                if not r2:
                    return "esc"
                rest = os.read(fd, 1)
            except Exception:
                return "esc"
            if rest == b"O":
                # SS3 (application keypad): ESC O X
                try:
                    r3, _, _ = select.select([fd], [], [], 0.05)
                    if not r3:
                        return "esc"
                    ch = os.read(fd, 1)
                except Exception:
                    return "esc"
                return self._SS3_MAP.get(ch, None)
            if rest != b"[":
                return "esc"
            seq = b"["
            # Read until terminator (letter, ~, or 4-char cap).
            for _ in range(4):
                try:
                    r3, _, _ = select.select([fd], [], [], 0.05)
                    if not r3:
                        break
                    ch = os.read(fd, 1)
                except Exception:
                    break
                if not ch:
                    break
                seq += ch
                if ch == b"~" or (b"A" <= ch <= b"Z") or (b"a" <= ch <= b"z"):
                    break
            return self._ESC_MAP.get(seq.decode("ascii", errors="replace"), None)
        if b == b"\t":
            return "tab"
        if b == b"\n" or b == b"\r":
            return "enter"
        if b == b"\x7f" or b == b"\x08":
            return "backspace"
        if b == b"\x03":
            return "ctrl-c"
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:
            return None


def _tui_handle_key(
    key: str,
    runtime: RuntimeState,
    snapshot_ref: "_SnapshotRef",
) -> tuple[bool, bool]:
    """Mutate `runtime` in place. Returns (should_redraw, should_quit).

    `snapshot_ref` is the shared-state holder; key handler may request a
    force sync via snapshot_ref.request_sync().
    """
    import time
    # Dismiss toast on any key.
    if runtime.toast is not None:
        runtime.toast = None

    # v2: modal state — captures most keys (spec §2.3 Modal column).
    # Placed first so Esc dismisses the modal instead of falling
    # through to the dashboard's default Esc-quits. modal_kind and
    # input_mode are mutually exclusive (modal openers gate on
    # input_mode is None; input openers gate on modal_kind is None),
    # so this branch never collides with input-mode dispatch.
    if runtime.modal_kind is not None:
        if key == "esc":
            runtime.modal_kind = None
            runtime.modal_scroll = 0
            return True, False
        if key in ("q", "ctrl-c"):
            return False, True   # quit always works
        if key in ("up", "k"):
            runtime.modal_scroll = max(0, runtime.modal_scroll - 1)
            return True, False
        if key in ("down", "j"):
            runtime.modal_scroll = runtime.modal_scroll + 1
            return True, False
        if key == "pgup":
            runtime.modal_scroll = max(0, runtime.modal_scroll - 10)
            return True, False
        if key == "pgdn":
            runtime.modal_scroll = runtime.modal_scroll + 10
            return True, False
        # All other dashboard-layer keys (Tab, s, f, /, v, r, ?, Enter, 1-4)
        # are silently swallowed per spec §2.4.
        return True, False

    # In input mode, only ctrl-c quits; esc/q are handled by the input
    # mode dispatch (esc cancels, q is just a printable character to append).
    if runtime.input_mode is not None and key == "ctrl-c":
        return False, True
    if runtime.input_mode is None and key in ("q", "ctrl-c", "esc"):
        # Esc only quits when no help overlay is showing.
        if key == "esc" and runtime.show_help:
            runtime.show_help = False
            return True, False
        return False, True
    if runtime.input_mode is None and key == "?":
        runtime.show_help = not runtime.show_help
        return True, False
    if runtime.input_mode is None and key == "v":
        runtime.variant = ("expressive" if runtime.variant == "conventional"
                           else "conventional")
        return True, False
    if runtime.input_mode is None and key == "r":
        snapshot_ref.request_sync()
        runtime.toast = ("syncing…", time.monotonic() + 1.0)
        return True, False
    if runtime.input_mode is None and key == "tab":
        runtime.focus_index = (runtime.focus_index + 1) % 4
        return True, False
    # Scroll (targets sessions when focused; in variant B, always sessions).
    is_sessions_focus = (runtime.variant == "expressive"
                         or runtime.focus_index == 3)
    if runtime.input_mode is None and is_sessions_focus:
        # v2: n / N — navigate confirmed search matches (spec §3.3).
        if (key in ("n", "N")
                and runtime.search_term is not None
                and runtime.search_matches
                and runtime.modal_kind is None):
            if key == "n":
                runtime.search_index = (runtime.search_index + 1) % len(runtime.search_matches)
            else:
                runtime.search_index = (runtime.search_index - 1) % len(runtime.search_matches)
            runtime.session_scroll = runtime.search_matches[runtime.search_index]
            return True, False
        if key in ("up", "k"):
            runtime.session_scroll = max(0, runtime.session_scroll - 1)
            return True, False
        if key in ("down", "j"):
            runtime.session_scroll = runtime.session_scroll + 1
            return True, False
        if key == "pgup":
            runtime.session_scroll = max(0, runtime.session_scroll - 10)
            return True, False
        if key == "pgdn":
            runtime.session_scroll = runtime.session_scroll + 10
            return True, False
    # v2: sessions sort cycle (spec §3.1). Sessions-scoped regardless of focus.
    if key == "s" and runtime.input_mode is None and runtime.modal_kind is None:
        runtime.sort_key = _tui_next_sort_key(runtime.sort_key)
        # Spec §3.3: search clears when sort changes — match indices were
        # computed against the previous ordering and would jump to wrong rows.
        runtime.search_term = None
        runtime.search_matches = []
        runtime.search_index = 0
        return True, False

    # v2: filter — open input mode (spec §3.2). Sessions-scoped regardless of focus.
    if key == "f" and runtime.input_mode is None and runtime.modal_kind is None:
        runtime.input_mode = "filter"
        # Edit-existing semantics: pre-load the buffer with current filter.
        runtime.input_buffer = runtime.filter_term or ""
        runtime.show_help = False  # mirror Enter/1-4: state change closes help
        return True, False

    # v2: filter input mode key dispatch (spec §2.3 + §3.2).
    if runtime.input_mode == "filter":
        if key == "esc":
            runtime.input_mode = None
            runtime.input_buffer = ""
            return True, False
        if key == "enter":
            buf = runtime.input_buffer.strip()
            runtime.filter_term = buf if buf else None
            runtime.input_mode = None
            runtime.input_buffer = ""
            # Reset session_scroll to top so user lands on first match.
            runtime.session_scroll = 0
            # Spec §3.3: search clears when filter changes — narrowing
            # invalidates the match index list.
            runtime.search_term = None
            runtime.search_matches = []
            runtime.search_index = 0
            return True, False
        if key == "backspace":
            runtime.input_buffer = runtime.input_buffer[:-1]
            return True, False
        # Printable: append. Multi-layer defense per memory:
        # clip on append (max 200), only printable, ignore unrecognised.
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            if len(runtime.input_buffer) < 200:
                runtime.input_buffer += key
            return True, False
        # All other keys swallowed silently in input mode.
        return True, False

    # v2: search — open input mode (spec §3.3).
    if key == "/" and runtime.input_mode is None and runtime.modal_kind is None:
        runtime.input_mode = "search"
        runtime.input_buffer = ""  # always start fresh per spec §3.3
        # Stale search_index from a prior query would make the first
        # post-confirm n/N wrap past the first matches of the new query.
        runtime.search_index = 0
        runtime.show_help = False  # mirror Enter/1-4: state change closes help
        return True, False

    # v2: search input mode key dispatch.
    if runtime.input_mode == "search":
        if key == "esc":
            runtime.input_mode = None
            runtime.input_buffer = ""
            # Cancel restores selection: clear matches/highlights.
            runtime.search_term = None
            runtime.search_matches = []
            runtime.search_index = 0
            return True, False
        if key == "enter":
            buf = runtime.input_buffer
            runtime.search_term = buf if buf else None
            runtime.input_mode = None
            runtime.input_buffer = ""
            # Match list will be populated by the renderer (it knows the
            # current post-filter+sort list). For now, scroll stays where
            # the live jump put it.
            return True, False
        if key == "backspace":
            runtime.input_buffer = runtime.input_buffer[:-1]
            return True, False
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            if len(runtime.input_buffer) < 200:
                runtime.input_buffer += key
            return True, False
        return True, False

    # v2: Enter — open detail modal (spec §2.3 + §4.2).
    # Both variants: focus_index maps to modal kind.
    if key == "enter" and runtime.input_mode is None and runtime.modal_kind is None:
        target_kind = ("current_week", "forecast", "trend", "session")[runtime.focus_index]
        runtime.modal_kind = target_kind
        runtime.modal_scroll = 0
        runtime.modal_snap_pending = True  # trend/current_week: snap to bottom on first render
        runtime.show_help = False  # mutually exclusive (spec §4.2)
        return True, False

    # v2: 1-4 universal modal shortcuts (spec §1, Q6a).
    if (key in ("1", "2", "3", "4")
            and runtime.input_mode is None
            and runtime.modal_kind is None):
        target_kind = ("current_week", "forecast", "trend", "session")[int(key) - 1]
        runtime.modal_kind = target_kind
        runtime.modal_scroll = 0
        runtime.modal_snap_pending = True
        runtime.show_help = False
        return True, False

    return False, False


class _TuiSyncThread:
    """Daemon thread that periodically rebuilds the DataSnapshot.

    Honors --no-sync by never syncing (only reading the current DB state).
    Force-refresh via `snapshot_ref.request_sync()` — thread interrupts its
    sleep and rebuilds immediately.

    When ``now_utc`` is provided (propagated from cmd_tui when --as-of is
    set), every rebuild pins the snapshot clock to that value so live mode
    mirrors --render-once determinism. When None, rebuilds use wall clock.
    """

    def __init__(
        self,
        snapshot_ref: _SnapshotRef,
        interval: float,
        *,
        skip_sync: bool,
        now_utc: dt.datetime | None = None,
        display_tz_pref_override: "str | None" = None,
    ) -> None:
        import threading
        self._ref = snapshot_ref
        self._interval = interval
        self._skip_sync = skip_sync
        self._now_utc = now_utc
        self._display_tz_pref_override = display_tz_pref_override
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tui-sync")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit and wait up to `interval + 0.5s`
        for it to finish. Because the thread is daemon=True, a failed
        join will not block process exit."""
        self._stop.set()
        self._thread.join(timeout=self._interval + 0.5)

    def _run(self) -> None:
        import time
        while not self._stop.is_set():
            try:
                # Route through cctally so test monkeypatches on
                # ``_tui_build_snapshot`` propagate into the sync thread
                # (cf. _make_run_sync_now_locked above).
                snap = sys.modules["cctally"]._tui_build_snapshot(
                    now_utc=self._now_utc, skip_sync=self._skip_sync,
                    display_tz_pref_override=self._display_tz_pref_override,
                )
                self._ref.set(snap)
            except Exception as exc:
                # Don't crash the thread on unexpected errors — surface in UI.
                prev = self._ref.get()
                self._ref.set(DataSnapshot(
                    current_week=prev.current_week,
                    forecast=prev.forecast,
                    trend=prev.trend,
                    sessions=prev.sessions,
                    last_sync_at=prev.last_sync_at,
                    last_sync_error=f"sync crashed: {exc}",
                    generated_at=dt.datetime.now(dt.timezone.utc),
                    percent_milestones=prev.percent_milestones,
                    weekly_history=prev.weekly_history,
                    weekly_periods=prev.weekly_periods,
                    monthly_periods=prev.monthly_periods,
                    blocks_panel=prev.blocks_panel,
                    daily_panel=prev.daily_panel,
                ))
            # Wait up to interval, or until forced.
            for _ in range(int(max(1, self._interval * 10))):
                if self._stop.is_set():
                    return
                if self._ref.take_sync_request():
                    break
                time.sleep(0.1)


def _tui_panel_current_week(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
    *,
    focused: bool,
) -> list[str]:
    """Return the list of pre-box body lines (color-tagged) for Variant A.

    Caller wraps in _tui_box_lines and color-tags + recolors the border.
    Width math is approximate here; the assembler strips tags before
    measuring.
    """
    cw = snap.current_week
    if cw is None:
        return [
            "",
            "  {dim}no current-week data yet{/}",
            "  {dim}run `record-usage` to start capturing{/}",
            "",
        ]
    # The panel interior is width - 2. The design uses leftW-20 as bar width.
    bar_w = max(10, width - 20)
    used_cls = _tui_bar_color(cw.used_pct)
    bar_fill = _tui_bar_string(cw.used_pct, bar_w)

    five = cw.five_hour_pct or 0.0
    five_bar = _tui_bar_string(five, bar_w)

    reset_delta = cw.week_end_at - snap.generated_at
    reset_days = max(0, reset_delta.days)
    reset_hrs = max(0, reset_delta.seconds // 3600)
    snap_age = int((snap.generated_at - cw.latest_snapshot_at).total_seconds())
    snap_age_m, snap_age_s = divmod(max(0, snap_age), 60)

    # Five-hour reset-in text (h, m precision)
    if cw.five_hour_resets_at:
        fr_delta = cw.five_hour_resets_at - snap.generated_at
        fr_hr = max(0, int(fr_delta.total_seconds()) // 3600)
        fr_mn = max(0, (int(fr_delta.total_seconds()) % 3600) // 60)
        fr_str = f"resets in {fr_hr}h {fr_mn:02d}m"
    else:
        fr_str = ""

    dpp_str = (
        f"${cw.dollars_per_percent:.2f}"
        if cw.dollars_per_percent is not None else "—"
    )
    lines = [
        "",
        f" Used   {{{used_cls}}}{bar_fill}{{/}} {{{used_cls}.b}}{cw.used_pct:>5.1f}%{{/}}",
        "",
        f" 5-hour {{bar.accent}}{five_bar}{{/}} {{bright}}{int(five):>3d}%{{/}}",
        f"        {{dim}}{fr_str}{{/}}" if fr_str else "",
        "",
        f" {{dim}}Spent{{/}}    {{bright}}${cw.spent_usd:.2f}{{/}}        "
        f"{{dim}}$/1%{{/}}  {{bright}}{dpp_str}{{/}}",
        f" {{dim}}Reset{{/}}    {{bright}}{format_display_dt(cw.week_end_at, runtime.display_tz, fmt='%b %d %H:%M', suffix=True)}{{/}}  "
        f"{{dim}}(in {reset_days}d {reset_hrs}h){{/}}",
        "",
        f" {{faint}}· last snapshot: {snap_age_m}m {snap_age_s:02d}s ago{{/}}",
    ]
    # Freshness chip (Task C6 / spec §3.4). Hidden when label is None or
    # 'fresh'; rendered dim for 'aging', warn (amber) for 'stale'. Mirrors
    # the dashboard CurrentWeekPanel chip in dashboard/web/src/panels/.
    if cw.freshness_label and cw.freshness_label != "fresh":
        captured_hms = format_display_dt(
            cw.latest_snapshot_at, runtime.display_tz,
            fmt="%H:%M:%S", suffix=False,
        )
        chip_style = "warn" if cw.freshness_label == "stale" else "dim"
        chip_age = cw.freshness_age if cw.freshness_age is not None else 0
        lines.append(
            f"  {{{chip_style}}}⏱ as of {captured_hms} · {chip_age}s ago{{/}}"
        )
    return lines


def _tui_panel_current_week_hero(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
) -> list[str]:
    """Variant B hero meter for current week."""
    cw = snap.current_week
    if cw is None:
        return ["", "  {dim}no data yet{/}", ""]
    bar_w = max(10, width - 10)
    used_cls = _tui_bar_color(cw.used_pct)
    big_bar = _tui_bar_string(cw.used_pct, bar_w)
    five_bar = _tui_bar_string(cw.five_hour_pct or 0.0, bar_w)
    snap_age_min = int((snap.generated_at - cw.latest_snapshot_at).total_seconds()) // 60

    if cw.five_hour_resets_at:
        sec = int((cw.five_hour_resets_at - snap.generated_at).total_seconds())
        fr_hr = max(0, sec) // 3600
        fr_mn = (max(0, sec) % 3600) // 60
        reset_suffix = f"   {{dim}}resets {fr_hr}h {fr_mn:02d}m{{/}}"
    else:
        reset_suffix = ""

    if snap.last_sync_error:
        health = "{warn}daemon error{/}"
    elif snap.last_sync_at is None:
        health = "{dim}sync paused{/}"
    else:
        health = "{dim}daemon healthy{/}"

    return [
        "",
        "  {dim}WEEK USAGE{/}",
        "",
        f"     {{{used_cls}.b}}{cw.used_pct:.1f}%{{/}}  {{dim}}of allowance used{{/}}",
        "",
        f"  {{{used_cls}}}{big_bar}{{/}}",
        f"  {{faint}}0%{' ' * (bar_w - 6)}100%{{/}}",
        "",
        f"  {{dim}}5-HOUR WINDOW{{/}}  {{bright}}{int(cw.five_hour_pct or 0)}%{{/}}{reset_suffix}",
        f"  {{bar.accent}}{five_bar}{{/}}",
        "",
        f"  {{dim}}snapshot {snap_age_min}m ago{{/}} · {health}",
        "",
    ]


_TUI_VERDICT_CLS = {
    "GOOD": "ok", "WARN": "warn", "OVER": "bad", "LOW CONF": "warn",
}
_TUI_VERDICT_SHORT = {
    "GOOD": "comfortable headroom",
    "WARN": "on track, no slack",
    "OVER": "throttle immediately",
    "LOW CONF": "not enough data",
}


def _tui_verdict_of(forecast) -> str:
    """Compute verdict name from a ForecastOutput. Matches design language."""
    if forecast is None or getattr(forecast.inputs, "confidence", "high") == "low":
        return "LOW CONF"
    high = forecast.final_percent_high
    if high >= 100:
        return "OVER"
    if high >= 90:
        return "WARN"
    return "GOOD"


def _tui_panel_forecast(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
) -> list[str]:
    """Variant A forecast panel body."""
    fc = snap.forecast
    if fc is None:
        return [
            "",
            "  {badge.warn} [ LOW CONF ] {/} {dim}no current-week data{/}",
            "",
            "  {dim}run record-usage first{/}",
            "",
        ]
    verdict = _tui_verdict_of(fc)
    vcls = _TUI_VERDICT_CLS[verdict]
    vmsg = _TUI_VERDICT_SHORT[verdict]

    bar_w = max(6, width - 36)

    def bar_tagged(val: float) -> str:
        b = _tui_bar_string(min(val, 100), bar_w)
        cls = _tui_bar_color(val)
        return f"{{{cls}}}{b}{{/}}"

    # Compute the two projection values DIRECTLY from the rate methods,
    # not from final_low/final_high which are min/max aggregates and
    # swap labels when the recent-24h rate is lower than week-avg.
    p_now = fc.inputs.p_now
    remaining = fc.inputs.remaining_hours
    wa = int(round(p_now + fc.r_avg * remaining))
    rc = wa if fc.r_recent is None else int(round(p_now + fc.r_recent * remaining))
    # Budget table row values
    b100 = next((r for r in fc.budgets if r.target_percent == 100), None)
    b90 = next((r for r in fc.budgets if r.target_percent == 90), None)
    b100_str = f"${b100.dollars_per_day:.2f}/day" if b100 and b100.dollars_per_day is not None else "—"
    b90_str  = f"${b90.dollars_per_day:.2f}/day"  if b90  and b90.dollars_per_day  is not None else "—"
    conf = "low" if verdict == "LOW CONF" else "high"

    return [
        "",
        f"  {{badge.{vcls}}} [ {verdict} ] {{/}} {{dim}}{vmsg}{{/}}",
        "",
        f" {{dim}}Projection by week-avg{{/}}    {bar_tagged(wa)} {{bright}}{wa:>3d}%{{/}}",
        f" {{dim}}Projection by recent 24h{{/}}  {bar_tagged(rc)} {{bright}}{rc:>3d}%{{/}}",
        "",
        f" {{dim}}Budget to stay ≤100%{{/}}   {{bright}}{b100_str}{{/}}",
        f" {{dim}}Budget to stay  ≤90%{{/}}   {{bright}}{b90_str}{{/}}",
        "",
        f" {{faint}}confidence: {conf} · based on 7-day rate{{/}}",
    ]


def _tui_panel_trend(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
    *,
    compact: bool = False,
) -> list[str]:
    """Variant A trend panel: 8-row table + inline sparkline row.

    When ``compact=True``, the leading blank, the pre-sparkline blank, and
    the trailing blank are skipped (3 rows recovered) so callers with tight
    vertical budgets can use the panel without the default padding.
    """
    rows = snap.trend
    if not rows:
        return ["", "  {dim}no trend data yet{/}", ""]
    lines: list[str] = []
    if not compact:
        lines.append("")
    lines.append(" {dim.b}Week      Used%    $/1%    Δ{/}")
    lines.append(" {faint}────────── ───── ──────── ──────{/}")
    for r in rows:
        marker = "{accent}▶{/}" if r.is_current else " "
        if r.used_pct is None:
            used_cls = "dim"
            used_fmt = "   — "  # 6 cols, matches "{:>5.1f}%" width
        else:
            used_cls = _tui_bar_color(r.used_pct)
            used_fmt = f"{r.used_pct:>5.1f}%"
        rate_str = (
            f"${r.dollars_per_percent:.2f}"
            if r.dollars_per_percent is not None else "   —"
        )
        if r.delta_dpp is None:
            delta_str = "  —  "
            delta_cls = "dim"
        else:
            sign = "+" if r.delta_dpp >= 0 else ""
            delta_str = f"{sign}{r.delta_dpp:.2f}"
            delta_cls = ("dim" if abs(r.delta_dpp) < 0.02
                         else ("warn" if r.delta_dpp > 0 else "ok"))
        lines.append(
            f" {marker} {{bright}}{r.week_label:<9}{{/}}  "
            f"{{{used_cls}}}{used_fmt}{{/}}   "
            f"{{bright}}{rate_str:<6}{{/}}  {{{delta_cls}}}{delta_str:<5}{{/}}"
        )
    # Sparkline row
    if not compact:
        lines.append("")
    heights = [r.spark_height for r in rows]
    spark = _tui_sparkline_inline(heights)
    lines.append(f"   {{dim}}spark $/1%{{/}}   {{accent.b}}{spark}{{/}}")
    if not compact:
        lines.append("")
    return lines


def _tui_session_model_cls(model: str) -> str:
    """Map primary model name to a color class for the Model column."""
    m = (model or "").lower()
    if m.startswith("opus"):
        return "magenta"
    if m.startswith("haiku"):
        return "blue"
    return "bright"


def _tui_format_started(
    ts: dt.datetime,
    now: dt.datetime,
    tz: "ZoneInfo | None",
) -> str:
    """Today -> 'HH:MM:SS', else 'Mon DD HH:MM'.

    F4 fix: takes a resolved ``ZoneInfo | None`` (as carried on
    ``RuntimeState.display_tz``) instead of the legacy "utc" / "local"
    string token. Previously, an explicit IANA zone like
    ``America/New_York`` reached this helper as a string, took the else
    branch, and rendered the raw UTC clock — so session rows displayed
    UTC even when reset/session-detail fields used the resolved zone.
    """
    # internal fallback: host-local intentional — picks the calendar bucket;
    # the actual rendered string flows through `format_display_dt`.
    disp = ts.astimezone(tz) if tz is not None else ts.astimezone()
    today = now.astimezone(disp.tzinfo).date()
    if disp.date() == today:
        return format_display_dt(ts, tz, fmt="%H:%M:%S", suffix=False)
    return format_display_dt(ts, tz, fmt="%b %d %H:%M", suffix=False)


def _tui_format_dur(minutes: float) -> str:
    """Human-friendly duration: '42m' or '3h 07m'."""
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m:02d}m"


# v2 sort: cycle order + direction (spec §3.1).
_TUI_SORT_KEYS = ("last-activity", "cost", "duration", "model", "project")
_TUI_SORT_ASC = frozenset({"model", "project"})  # ascending; rest descending


def _tui_sort_sessions(sessions: list[TuiSessionRow], key: str) -> list[TuiSessionRow]:
    """Return a new list sorted per spec §3.1.

    Default key 'last-activity' is pass-through — preserves the order
    `_aggregate_claude_sessions` already produces (last_activity desc).
    Other keys: hard-coded direction by type (numeric/recency desc;
    text asc) with a stable secondary on last-activity desc.
    """
    if not sessions:
        return []
    if key == "last-activity":
        return list(sessions)  # already sorted by aggregator — pass-through
    if key == "cost":
        primary = lambda s: -s.cost_usd
    elif key == "duration":
        primary = lambda s: -s.duration_minutes
    elif key == "model":
        primary = lambda s: s.model_primary.lower()
    elif key == "project":
        primary = lambda s: s.project_label.lower()
    else:
        return list(sessions)
    return sorted(sessions, key=lambda s: (primary(s), -s.started_at.timestamp()))


def _tui_next_sort_key(current: str) -> str:
    """Cycle to the next key. Wraps."""
    try:
        idx = _TUI_SORT_KEYS.index(current)
    except ValueError:
        return _TUI_SORT_KEYS[0]
    return _TUI_SORT_KEYS[(idx + 1) % len(_TUI_SORT_KEYS)]


def _tui_apply_session_filter(sessions, active_filter):
    """Narrow sessions by a filter substring (project_label|model_primary).
    Returns `sessions` unchanged when `active_filter` is None/empty. Mirrors
    the filter logic in `_tui_panel_sessions` so live match counts in the
    input prompt reflect the post-filter navigable list."""
    if not active_filter:
        return sessions
    af_lower = active_filter.lower()
    return [
        s for s in sessions
        if af_lower in s.project_label.lower()
        or af_lower in s.model_primary.lower()
    ]


def _tui_sessions_title(runtime: RuntimeState, *, narrow: bool) -> str:
    """Build the Sessions panel title with sort indicator and filter chip.

    Spec §3.1 (sort indicator) + §3.2 (filter chip). Narrow bucket
    abbreviates the sort key and chip per spec §5.1.

    Returns a tagged string (uses {name}…{/} markup); the caller passes
    it straight to `_tui_tagged_box_lines(title=…)` which materializes
    the tags via `_tui_colortag` downstream.
    """
    arrow = "↑" if runtime.sort_key in _TUI_SORT_ASC else "↓"
    if narrow:
        sort_part = f"{{dim}} · {runtime.sort_key}{arrow}{{/}}"
    else:
        sort_part = f"{{dim}} · sort: {runtime.sort_key} {arrow}{{/}}"
    chip_part = ""
    if runtime.filter_term is not None:
        if narrow:
            shown = _tui_escape_tags(runtime.filter_term[:8])
            chip_part = f" {{chip}} ▼{shown} {{/}}"
        else:
            shown = _tui_escape_tags(runtime.filter_term)
            chip_part = f" {{chip}} filter: {shown} {{/}}"
    return f"{{focused.b}}Recent Sessions{{/}}{sort_part}{chip_part}"


def _tui_panel_sessions(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
    *,
    rows_visible: int,
    show_project_col: bool,
) -> list[str]:
    """Variant A + B sessions panel body.

    Caller drives layout:
    - Variant A (right half of the 2x2) passes rightW as `width` and
      `show_project_col=False` or True depending on space.
    - Variant B (full-width) passes the terminal width as `width` and
      typically `show_project_col=True`.
    """
    sessions = _tui_sort_sessions(snap.sessions, runtime.sort_key)

    # v2: apply filter (spec §3.2). Use input_buffer as the live preview when
    # in filter input mode (incremental narrowing); otherwise use the
    # committed filter_term.
    active_filter: str | None
    if runtime.input_mode == "filter":
        active_filter = runtime.input_buffer or None
    else:
        active_filter = runtime.filter_term
    if active_filter:
        af_lower = active_filter.lower()
        sessions = [
            s for s in sessions
            if af_lower in s.project_label.lower()
            or af_lower in s.model_primary.lower()
        ]

    # Spec §3.2 empty-narrow result.
    if active_filter and not sessions:
        empty_lines: list[str] = [
            "",
            "",
            f"  {{dim}}no sessions match \"{_tui_escape_tags(active_filter)}\"  · f to edit{{/}}",
            "",
        ]
        # Pad to expected rows_visible (header+ruler+rows+trailing chrome).
        while len(empty_lines) < rows_visible + 5:
            empty_lines.append("")
        empty_lines.append("")
        empty_lines.append(
            f" {{dim}}↑↓ scroll · 0 of {len(snap.sessions)} match · 0 below{{/}}"
        )
        return empty_lines

    # v2: compute search matches (spec §3.3). Active term is input_buffer
    # while typing, search_term once confirmed, None otherwise.
    if runtime.input_mode == "search":
        active_search = runtime.input_buffer or None
        auto_jump = True  # live-type: reset scroll to first match each tick
    else:
        active_search = runtime.search_term
        auto_jump = False  # confirmed: preserve scroll (lets n/N work)
    match_indices: list[int] = []
    if active_search:
        as_lower = active_search.lower()
        for i, s in enumerate(sessions):
            haystack = (
                s.project_label.lower() + "|"
                + s.model_primary.lower() + "|"
                + _tui_format_started(s.started_at, snap.generated_at, runtime.display_tz).lower()
            )
            if as_lower in haystack:
                match_indices.append(i)
        # Live jump only while typing — n/N drives scroll after confirm.
        if match_indices and auto_jump:
            runtime.session_scroll = match_indices[0]
        # Persist for n/N when in confirmed state.
        runtime.search_matches = match_indices
        if runtime.search_index >= len(match_indices):
            runtime.search_index = 0

    def _hl(text: str, term: str | None) -> str:
        if not term:
            return text
        # Search and slice on UNESCAPED text (so positions are correct), but
        # emit ESCAPED segments so any user-source `{` `}` chars survive
        # the colortag pipeline literally.
        idx = text.lower().find(term.lower())
        if idx < 0:
            return text
        before = _tui_escape_tags(text[:idx])
        match  = _tui_escape_tags(text[idx:idx + len(term)])
        after  = _tui_escape_tags(text[idx + len(term):])
        return before + "{match}" + match + "{/}" + after

    interior = width - 2

    # Column widths derived from the design's 120-col and 100-col tables.
    # Cost column is 8 at the wide bucket so $1000+ session values don't
    # overflow (e.g., "$1234.56" is 8 chars). Medium/narrow buckets keep 7/6
    # — the layouts are already tight and four-digit costs are uncommon.
    if interior >= 70:
        c_start, c_dur, c_model, c_cost = 14, 6, 11, 8
    elif interior >= 55:
        c_start, c_dur, c_model, c_cost = 12, 6, 10, 7
    else:
        c_start, c_dur, c_model, c_cost = 8, 5, 10, 6
        show_project_col = False

    fixed = 8 + c_start + c_dur + c_model + c_cost
    last_avail = interior - fixed
    if show_project_col and last_avail >= 10:
        c_last = last_avail
        last_title = "Project"
        use_project = True
    else:
        c_last = min(5, max(0, last_avail))
        last_title = "Cache"
        use_project = False

    def _truncpad(s: str, n: int) -> str:
        if n <= 0:
            return ""
        if len(s) > n:
            return s[: n - 1] + "…"
        return s + " " * (n - len(s))

    lines: list[str] = [""]
    # Header row
    lines.append(
        f"   {{dim.b}}"
        f"{_truncpad('Started', c_start)} "
        f"{_truncpad('Dur', c_dur)} "
        f"{_truncpad('Model', c_model)} "
        f"{('Cost').rjust(c_cost)} "
        f"{_truncpad(last_title, c_last)}"
        f"{{/}}"
    )
    # Ruler
    lines.append(
        f"   {{faint}}"
        f"{'─' * c_start} {'─' * c_dur} {'─' * c_model} "
        f"{'─' * c_cost} {'─' * c_last}"
        f"{{/}}"
    )

    # Clamp scroll. Cap at len-1 (not len - rows_visible) so any session —
    # including search matches landing in the last rows_visible-1 positions —
    # can become the topmost-visible row. Spec §3.3 (Live jump) requires the
    # matched row to reach topmost; with the tighter clamp, selection (=
    # topmost per the "is_selected = i == 0" convention) would diverge from
    # the highlighted match and Enter would open the wrong session detail.
    # The existing padding loop below fills blank rows when the slice is short.
    max_scroll = max(0, len(sessions) - 1)
    scroll = min(runtime.session_scroll, max_scroll)
    runtime.session_scroll = scroll  # write clamped back

    visible = sessions[scroll: scroll + rows_visible]
    for i, s in enumerate(visible):
        is_selected = (i == 0)  # topmost = selected (design convention)
        start_s = _truncpad(_tui_format_started(s.started_at, snap.generated_at, runtime.display_tz), c_start)
        dur_s   = _truncpad(_tui_format_dur(s.duration_minutes), c_dur)
        model_s = _truncpad(s.model_primary, c_model)
        cost_s  = f"${s.cost_usd:.2f}".rjust(c_cost)
        if use_project:
            last_s = _truncpad(s.project_label, c_last)
        else:
            last_s = (f"{int(s.cache_hit_pct)}%" if s.cache_hit_pct is not None else "—").rjust(c_last)
        model_cls = _tui_session_model_cls(s.model_primary)
        cache_cls = "ok" if (s.cache_hit_pct or 0) >= 70 else ("warn" if (s.cache_hit_pct or 0) >= 50 else "dim")

        # v2: search highlight (spec §3.3). Wraps matched substring in
        # {match} tags; nests safely inside the row's outer style.
        model_h = _hl(model_s, active_search)
        last_h = _hl(last_s, active_search) if use_project else last_s
        start_h = _hl(start_s, active_search)

        if is_selected:
            body = (
                f"▸ {start_h} {dur_s} {model_h} {cost_s} {last_h}"
            )
            lines.append(f" {{focused.b}}{body}{{/}}")
        elif use_project:
            lines.append(
                f"   {{bright}}{start_h}{{/}} {{dim}}{dur_s}{{/}} "
                f"{{{model_cls}}}{model_h}{{/}} {{bright}}{cost_s}{{/}} "
                f"{{dim}}{last_h}{{/}}"
            )
        else:
            lines.append(
                f"   {{bright}}{start_h}{{/}} {{dim}}{dur_s}{{/}} "
                f"{{{model_cls}}}{model_h}{{/}} {{bright}}{cost_s}{{/}} "
                f"{{{cache_cls}}}{last_s}{{/}}"
            )
    # Pad to rows_visible
    while len(lines) - 3 < rows_visible:
        lines.append("")

    below = max(0, len(sessions) - scroll - rows_visible)
    lines.append("")
    lines.append(f" {{dim}}↑↓ scroll · {below} below{{/}}")
    return lines


def _tui_header_strip_a(
    snap: DataSnapshot, runtime: RuntimeState, width: int,
) -> list[str]:
    """Variant A header strip: top rule + summary line + bottom rule.

    Appends a one-line error banner below the bottom rule when
    snap.last_sync_error is set.
    """
    import time
    cw = snap.current_week
    fc = snap.forecast
    verdict = _tui_verdict_of(fc) if fc else "LOW CONF"
    vcls = _TUI_VERDICT_CLS[verdict]
    sync_age = 0
    if snap.last_sync_at is not None:
        sync_age = int(time.monotonic() - snap.last_sync_at)
    sync_txt = f"synced {sync_age}s ago" if snap.last_sync_at is not None else "synced —"
    err = snap.last_sync_error
    if cw is None:
        hdr = (
            f"{{bright.b}}Week — {{/}} {{faint}}│{{/}} "
            f"{{dim}}no data yet — run record-usage first{{/}}"
        )
    else:
        used_cls = _tui_bar_color(cw.used_pct)
        dpp_str = (
            f"${cw.dollars_per_percent:.2f}"
            if cw.dollars_per_percent is not None else "—"
        )
        fcst_pct = "—"
        if fc:
            # Use the measure that drives the verdict (_tui_verdict_of keys
            # on final_percent_high). Using low here would display e.g.
            # "Fcst 74% WARN" where the WARN comes from a >=90% high
            # projection, understating risk in the most glanceable line.
            fcst_pct = f"{int(round(fc.final_percent_high))}%"
        hdr = (
            f"{{bright.b}}Week {format_display_dt(cw.week_start_at, runtime.display_tz, fmt='%b %d', suffix=False)}–{format_display_dt(cw.week_end_at, runtime.display_tz, fmt='%b %d', suffix=False)}{{/}} "
            f"{{faint}}│{{/}} Used {{{used_cls}.b}}{cw.used_pct:.1f}%{{/}} "
            f"{{dim}}(5h {int(cw.five_hour_pct or 0)}%){{/}} {{faint}}│{{/}} "
            f"$/1% {{bright}}{dpp_str}{{/}} {{faint}}│{{/}} "
            f"Fcst {{{vcls}.b}}{fcst_pct}{{/}} {{{vcls}.b}}{verdict}{{/}} {{faint}}│{{/}} "
            f"{{dim.pulse}}● {sync_txt}{{/}}"
        )
    # Top/bottom rules framing the header.
    return [
        "{faint}" + ("═" * width) + "{/}",
        " " + hdr,
        "{faint}" + ("═" * width) + "{/}",
        *(["{warn}⚠ sync failed: " + err + "{/}"] if err else []),
    ]


def _tui_footer_keys(width: int) -> list[str]:
    """Variant A footer: top rule + keys legend."""
    return [
        "{faint}" + ("═" * width) + "{/}",
        (" {bright}Tab{/} {dim}focus{/}  "
         "{bright}↑↓{/} {dim}scroll{/}  "
         "{bright}r{/} {dim}refresh{/}  "
         "{bright}s{/} {dim}sort{/}  "
         "{bright}f{/} {dim}filter{/}  "
         "{bright}/{/} {dim}search{/}  "
         "{bright}Enter{/} {dim}open{/}  "
         "{bright}v{/} {dim}variant{/}  "
         "{bright}?{/} {dim}help{/}  "
         "{bright}q{/} {dim}quit{/}"),
    ]


def _tui_render_input_prompt(
    runtime: RuntimeState, width: int, *, match_count: int | None = None,
) -> list[str]:
    """Render the bottom-row input prompt. Replaces the keys-legend row
    while runtime.input_mode is set. Spec §3.2 (filter), §3.3 (search).
    """
    buf = runtime.input_buffer
    # Truncate displayed buffer if it would overflow.
    max_buf = max(10, width - 60)
    shown = buf if len(buf) <= max_buf else buf[-max_buf:]
    # Escape user input so a stray `{` or `}` doesn't get parsed as a
    # style tag by _tui_colortag (would crash the live render loop).
    shown = _tui_escape_tags(shown)
    if runtime.input_mode == "filter":
        prefix = "filter (project|model)"
        contract = "enter apply · esc cancel"
    else:  # 'search'
        prefix = "search"
        contract = "enter confirm · esc cancel · n/N next/prev"
    match_suffix = ""
    if match_count is not None:
        cls = "bad" if match_count == 0 else "dim"
        match_suffix = f" {{{cls}}}· {match_count} matches{{/}}"
    body = (f" {{prompt}}{prefix}:{{/}} {{bright}}{shown}{{/}}{{caret}} {{/}}"
            f"{match_suffix}     {{faint}}{contract}{{/}}")
    return [
        "{faint}" + ("═" * width) + "{/}",
        body,
    ]


# Tag matcher used to strip color tags for plain-text width math. Matches
# both opening tags ({name} or {name.mod}) and closing tags ({/}).
_TUI_TAG_RE = re.compile(r"\{(?:/|[a-zA-Z.]+)\}")


def _tui_strip_tags(s: str) -> str:
    """Return ``s`` with all color-tag markup removed (for width math)."""
    return _TUI_TAG_RE.sub("", s)


def _tui_tagged_box_lines(
    *,
    width: int,
    body_tagged: list[str],
    title: str | None,
    pin: str | None,
    border_style: str = "faint",
) -> list[str]:
    """Return a list of tagged strings forming a double-line box.

    Body lines may contain color tags. Width math strips tags before padding
    so that color markup does not inflate the visible length. ``border_style``
    is a theme style name applied to all border glyphs.

    When a body line's plain length exceeds the interior width it is truncated
    with ``{/}`` appended as a safety net — callers should size their content
    to avoid this branch.
    """
    H, V = _TUI_BOX["h"], _TUI_BOX["v"]
    TL, TR, BL, BR = _TUI_BOX["tl"], _TUI_BOX["tr"], _TUI_BOX["bl"], _TUI_BOX["br"]
    interior = width - 2

    def _wrap_border(s: str) -> str:
        return f"{{{border_style}}}{s}{{/}}"

    def _top() -> str:
        if title is None and pin is None:
            return _wrap_border(TL + H * interior + TR)
        t_seg = f" {title} " if title else ""
        p_seg = f" {pin} " if pin else ""
        if title and pin:
            fill = width - 4 - len(_tui_strip_tags(t_seg)) - len(_tui_strip_tags(p_seg))
            if fill >= 1:
                return (_wrap_border(TL + H) + t_seg
                        + _wrap_border(H * fill) + p_seg + _wrap_border(H + TR))
        if title:
            fill = width - 3 - len(_tui_strip_tags(t_seg))
            if fill >= 1:
                return _wrap_border(TL + H) + t_seg + _wrap_border(H * fill + TR)
        return _wrap_border(TL + H * interior + TR)

    lines: list[str] = [_top()]
    for line in body_tagged:
        plain = _tui_strip_tags(line)
        if len(plain) > interior:
            # On overflow, drop color markup entirely — partial-tag truncation
            # would produce malformed tokens that crash _tui_colortag. Plain-
            # text is always safe, and callers are expected to size content
            # to fit (this branch is a safety net only).
            line = plain[: max(0, interior - 1)] + "…"
            plain = line
        pad = interior - len(plain)
        lines.append(_wrap_border(V) + line + " " * pad + _wrap_border(V))
    lines.append(_wrap_border(BL + H * interior + BR))
    return lines


def _tui_lines_to_text(lines: list[str]):
    """Join a list of tagged strings into a single rich.text.Text blob.

    Each line is passed through ``_tui_colortag`` to materialize the style
    tags; adjacent lines are separated by a literal ``"\\n"``.
    """
    from rich.text import Text
    out = Text()
    for i, l in enumerate(lines):
        if i:
            out.append("\n")
        out.append(_tui_colortag(l))
    return out


def _tui_render_variant_a(
    snap: DataSnapshot, runtime: RuntimeState,
    width: int, height: int, bucket: str,
    *, overlay_panel: Panel | None = None,
) -> Layout:
    """Return a ``rich.layout.Layout`` for the whole Variant A frame.

    Assembles the 2x2 grid (Current Week | Forecast / Trend | Sessions)
    with header and footer strips.

    The returned Layout has real sub-regions:
      root (split_column):
        - header     (size = len(header_lines))
        - warn?      (size = 1, only when bucket == "narrow")
        - top_row    (split_row current_week + forecast)
        - sep        (size = 1)
        - bottom_row (split_row trend + sessions)
        - footer     (size = len(footer_lines))

    When ``overlay_panel`` is provided, the body regions (top_row/sep/bottom_row)
    are replaced with a single centered Align(overlay_panel) so the help
    overlay or v2 detail modal appears in the dashboard's body area.
    Header and footer remain visible. This is the body-region-swap overlay
    composition (Fallback A).
    """
    from rich.layout import Layout
    from rich.align import Align

    left_w = width // 2
    right_w = width - left_w

    header = _tui_header_strip_a(snap, runtime, width)
    if runtime.input_mode is not None:
        match_count = None
        if runtime.input_mode == "filter":
            match_count = sum(
                1 for s in snap.sessions
                if (runtime.input_buffer.lower() in s.project_label.lower()
                    or runtime.input_buffer.lower() in s.model_primary.lower())
            ) if runtime.input_buffer else None
        elif runtime.input_mode == "search":
            if runtime.input_buffer:
                needle = runtime.input_buffer.lower()
                # Count against the post-filter list: confirmed search matches
                # are computed against the already-filtered sessions in
                # _tui_panel_sessions, so the live count must use the same set
                # or the prompt overstates what n/N can reach.
                pool = _tui_apply_session_filter(snap.sessions, runtime.filter_term)
                count = 0
                for s in pool:
                    hay = (
                        s.project_label.lower() + "|"
                        + s.model_primary.lower() + "|"
                        + _tui_format_started(s.started_at, snap.generated_at, runtime.display_tz).lower()
                    )
                    if needle in hay:
                        count += 1
                match_count = count
            else:
                match_count = None
        footer = _tui_render_input_prompt(runtime, width, match_count=match_count)
    else:
        footer = _tui_footer_keys(width)

    warn_line = (
        "{warn}⚠ narrow terminal — some columns hidden{/}"
        if bucket == "narrow" else None
    )

    # Compute the body region (between header and footer).
    # When a narrow-warning line is present it occupies one extra row that
    # would otherwise belong to the body, matching the legacy behavior of
    # inserting the warn line at frame-row index 3.
    warn_rows = 1 if warn_line is not None else 0
    body_height = max(
        10,
        height - len(header) - warn_rows - len(footer) - 1,
    )
    top_h = body_height // 2
    bot_h = body_height - top_h

    # TOP: current week (left) | forecast (right)
    cw_body = _tui_panel_current_week(
        snap, runtime, left_w, focused=runtime.focus_index == 0
    )
    fc_body = _tui_panel_forecast(snap, runtime, right_w)
    cw_box = _tui_tagged_box_lines(
        width=left_w, body_tagged=cw_body,
        title="{accent.b}Current Week{/}", pin="{dim}[1]{/}",
        border_style=("focused" if runtime.focus_index == 0 else "faint"),
    )
    fc_box = _tui_tagged_box_lines(
        width=right_w, body_tagged=fc_body,
        title="{accent.b}Forecast{/}", pin="{dim}[2]{/}",
        border_style=("focused" if runtime.focus_index == 1 else "faint"),
    )
    # Pad shorter box with blank interior rows up to the taller one.
    maxlen = max(len(cw_box), len(fc_box))

    def _pad_box(lines: list[str], w: int, style: str) -> list[str]:
        while len(lines) < maxlen:
            lines.insert(-1, f"{{{style}}}║{{/}}{' ' * (w - 2)}{{{style}}}║{{/}}")
        return lines

    cw_box = _pad_box(cw_box, left_w, "focused" if runtime.focus_index == 0 else "faint")
    fc_box = _pad_box(fc_box, right_w, "focused" if runtime.focus_index == 1 else "faint")

    # BOTTOM: trend (left) | sessions (right). Use compact trend when the
    # cell height is tight (saves 3 rows of padding).
    trend_compact = (bot_h - 2) < 14
    trend_body = _tui_panel_trend(snap, runtime, left_w, compact=trend_compact)
    # Sessions chrome: 1 leading blank + 1 header + 1 ruler + 1 trailing blank
    # + 1 scroll footer = 5 rows around the data rows. Interior = bot_h - 2.
    rows_visible = max(3, bot_h - 2 - 5)
    show_proj = bucket == "wide"
    sess_body = _tui_panel_sessions(
        snap, runtime, right_w,
        rows_visible=rows_visible,
        show_project_col=show_proj,
    )
    trend_box = _tui_tagged_box_lines(
        width=left_w, body_tagged=trend_body,
        title="{accent.b}$/1% Trend{/} {dim}· 8 weeks{/}", pin="{dim}[3]{/}",
        border_style=("focused" if runtime.focus_index == 2 else "faint"),
    )
    sess_box = _tui_tagged_box_lines(
        width=right_w, body_tagged=sess_body,
        # Variant A puts Sessions in a half-pane; compact bucket needs narrow form
        # to keep the `focus` pin visible alongside the sort indicator. When a
        # filter chip is active, use narrow form even at the wide bucket because
        # the chip pushes the wide form past the half-pane width.
        title=_tui_sessions_title(
            runtime,
            narrow=(bucket in ("narrow", "compact") or runtime.filter_term is not None),
        ),
        pin=("{focused}focus{/}" if runtime.focus_index == 3 else "{dim}[4]{/}"),
        border_style=("focused" if runtime.focus_index == 3 else "faint"),
    )
    maxlen2 = max(len(trend_box), len(sess_box))

    def _pad_box2(lines: list[str], w: int, style: str) -> list[str]:
        while len(lines) < maxlen2:
            lines.insert(-1, f"{{{style}}}║{{/}}{' ' * (w - 2)}{{{style}}}║{{/}}")
        return lines

    trend_box = _pad_box2(trend_box, left_w, "focused" if runtime.focus_index == 2 else "faint")
    sess_box = _pad_box2(sess_box, right_w, "focused" if runtime.focus_index == 3 else "faint")

    # Build per-region Text blobs. Each row band renders as a single Text
    # so intra-band line breaks are not padded to the full width by
    # Layout's line-pad; only the region-tail gets padded.
    header_text = _tui_lines_to_text(header)
    footer_text = _tui_lines_to_text(footer)
    cw_text = _tui_lines_to_text(cw_box)
    fc_text = _tui_lines_to_text(fc_box)
    trend_text = _tui_lines_to_text(trend_box)
    sess_text = _tui_lines_to_text(sess_box)

    root = Layout()
    regions: list[Layout] = [Layout(name="header", size=len(header))]
    if warn_line is not None:
        regions.append(Layout(name="warn", size=1))

    if overlay_panel is not None:
        # Fallback A: collapse the body bands into a single region containing
        # a vertically-centered overlay Panel. Preserve header + footer.
        body_rows = maxlen + 1 + maxlen2
        regions.append(Layout(name="body", size=body_rows))
        regions.append(Layout(name="footer", size=len(footer)))
        root.split_column(*regions)
        root["header"].update(header_text)
        if warn_line is not None:
            root["warn"].update(_tui_lines_to_text([warn_line]))
        root["body"].update(Align.center(overlay_panel, vertical="middle"))
        root["footer"].update(footer_text)
        root._tui_natural_height = sum(r.size or 0 for r in regions)
        return root

    regions.extend([
        Layout(name="top", size=maxlen),
        Layout(name="sep", size=1),
        Layout(name="bot", size=maxlen2),
        Layout(name="footer", size=len(footer)),
    ])
    root.split_column(*regions)

    root["header"].update(header_text)
    if warn_line is not None:
        root["warn"].update(_tui_lines_to_text([warn_line]))

    root["top"].split_row(
        Layout(name="cw", size=left_w),
        Layout(name="fc", size=right_w),
    )
    root["top"]["cw"].update(cw_text)
    root["top"]["fc"].update(fc_text)

    # The separator is intentionally a single blank row. The leading rich
    # Text of a single empty string becomes a zero-column line; Layout pads
    # it to the full width (that padding is the trailing-whitespace drift
    # accepted by the scoped-relaxation protocol).
    root["sep"].update(_tui_lines_to_text([""]))

    root["bot"].split_row(
        Layout(name="trend", size=left_w),
        Layout(name="sess", size=right_w),
    )
    root["bot"]["trend"].update(trend_text)
    root["bot"]["sess"].update(sess_text)
    root["footer"].update(footer_text)
    # Stash the natural (content-filling) height so ``_tui_render_once``
    # can recover the pre-refactor row count without padding to the full
    # terminal. Live mode ignores this attribute — it lets Layout fill the
    # actual terminal height, which is the desired TUI behavior.
    root._tui_natural_height = sum(r.size or 0 for r in regions)
    return root


def _tui_render_variant_b(
    snap: DataSnapshot, runtime: RuntimeState,
    width: int, height: int, bucket: str,
    *, overlay_panel: Panel | None = None,
) -> Layout:
    """Return a ``rich.layout.Layout`` for the whole Variant B frame.

    Structure: ribbon -> subheader -> hero row (big meter + promoted
    sparkline) -> forecast-budget strip -> full-width sessions -> footer.

    Vertical bands as Layout regions:
      ribbon (size=1), sub (size=1), rule (size=1),
      warn? (size=1, narrow only), blank1 (size=1),
      hero_row (split_row hero + trend, size=len(hero_box)),
      blank2 (size=1), fc_strip (size=len(fc_strip_box)),
      blank3 (size=1),
      sessions (size=len(sess_box)), footer (size=len(footer_lines))

    When ``overlay_panel`` is provided, the body bands collapse into one
    ``body`` region filled with a centered Align(overlay_panel) so the
    help overlay or v2 detail modal appears in the dashboard's body area
    (Fallback A overlay). Ribbon / subheader / rule / footer remain visible.
    """
    from rich.layout import Layout
    from rich.align import Align

    # --- Ribbon ---------------------------------------------------------
    verdict = _tui_verdict_of(snap.forecast) if snap.forecast else "LOW CONF"
    vcls = _TUI_VERDICT_CLS[verdict]
    # _TUI_VERDICT_CLS always maps to ok/warn/bad after the SSoT fix, so vcls
    # is guaranteed to be a valid badge class here.
    if snap.forecast:
        # Compute projections directly from rate methods — final_low/final_high
        # are min/max aggregates and swap labels when recent-24h rate is lower
        # than week-average (mirrors the Variant A fix in commit 15b6fab).
        p_now = snap.forecast.inputs.p_now
        remaining = snap.forecast.inputs.remaining_hours
        wa = int(round(p_now + snap.forecast.r_avg * remaining))
        rc = wa if snap.forecast.r_recent is None else int(round(p_now + snap.forecast.r_recent * remaining))
    else:
        wa, rc = 0, 0
    vmsg = _TUI_VERDICT_SHORT[verdict]
    ribbon_text = f"  [ {verdict} ]   {vmsg}   ·   week-avg {wa}%   ·   recent-24h {rc}%"
    ribbon_pad = max(0, width - len(ribbon_text))
    ribbon = f"{{badge.{vcls}}}{ribbon_text}{' ' * ribbon_pad}{{/}}"

    # --- Subheader ------------------------------------------------------
    cw = snap.current_week
    if cw:
        import time as _t
        sync_age = 0
        if snap.last_sync_at is not None:
            sync_age = int(_t.monotonic() - snap.last_sync_at)
        sync_txt = f"synced {sync_age}s ago" if snap.last_sync_at is not None else "synced —"
        # Pre-compute interpolated fragments so nothing inside the f-string
        # uses a nested conditional format spec (which Python rejects).
        dpp_str = (
            f"${cw.dollars_per_percent:.2f}"
            if cw.dollars_per_percent is not None else "—"
        )
        reset_delta = cw.week_end_at - snap.generated_at
        reset_secs = max(0, int(reset_delta.total_seconds()))
        reset_days = reset_secs // 86400
        reset_hrs = (reset_secs % 86400) // 3600
        sub = (
            f" {{bright.b}}Week {format_display_dt(cw.week_start_at, runtime.display_tz, fmt='%b %d', suffix=False)}–"
            f"{format_display_dt(cw.week_end_at, runtime.display_tz, fmt='%b %d', suffix=False)}{{/}}   "
            f"{{dim}}${cw.spent_usd:.2f} spent · $/1% {dpp_str} · "
            f"resets in {reset_days}d {reset_hrs}h{{/}}"
            f"   ·   {{dim.pulse}}● {sync_txt}{{/}}"
        )
    else:
        sub = " {dim}no current-week data yet{/}"

    hero_left_w = 55 if bucket != "wide" else int(width * 0.56)
    hero_right_w = width - hero_left_w

    hero_body = _tui_panel_current_week_hero(snap, runtime, hero_left_w)

    # Big sparkline on the right (promoted trend view).
    heights = [r.spark_height for r in snap.trend] or [1]
    big = _tui_sparkline_big(heights).split("\n")
    cur_rate = (snap.trend[-1].dollars_per_percent if snap.trend else None)
    cur_delta = (snap.trend[-1].delta_dpp if snap.trend else None)
    rate_str = f"${cur_rate:.2f}" if cur_rate is not None else "—"
    if cur_delta is None:
        delta_str = "—"
    else:
        sign = "+" if cur_delta >= 0 else ""
        delta_str = f"{sign}{cur_delta:.2f}"

    trend_title_text = "this week" if bucket != "wide" else "$/1% OVER 8 WEEKS"
    trend_body = [
        "",
        f"  {{dim}}{trend_title_text}{{/}}  "
        f"{{bright.b}}{rate_str}{{/}}  {{ok}}{delta_str}{{/}}",
        "",
        f"  {{accent}}{big[0] if len(big) > 0 else ''}{{/}}",
        f"  {{accent}}{big[1] if len(big) > 1 else ''}{{/}}",
        f"  {{accent}}{big[2] if len(big) > 2 else ''}{{/}}",
        f"  {{faint}}{'─' * min(hero_right_w - 4, 24)}{{/}}",
        "",
    ]

    hero_box = _tui_tagged_box_lines(
        width=hero_left_w, body_tagged=hero_body,
        title="{accent.b}Current Week{/}",
        pin=("{focused}focus{/}" if runtime.focus_index == 0 else "{dim}[1]{/}"),
        border_style=("focused" if runtime.focus_index == 0 else "faint"),
    )
    trend_box = _tui_tagged_box_lines(
        width=hero_right_w, body_tagged=trend_body,
        title="{accent.b}$/1% Trend{/} {dim}· 8 weeks{/}",
        pin=("{focused}focus{/}" if runtime.focus_index == 2 else "{dim}[3]{/}"),
        border_style=("focused" if runtime.focus_index == 2 else "faint"),
    )
    m = max(len(hero_box), len(trend_box))

    def _pad(lines: list[str], w: int, style: str) -> list[str]:
        while len(lines) < m:
            lines.insert(-1, f"{{{style}}}║{{/}}{' ' * (w - 2)}{{{style}}}║{{/}}")
        return lines

    hero_box = _pad(hero_box, hero_left_w, "focused" if runtime.focus_index == 0 else "faint")
    trend_box = _pad(trend_box, hero_right_w, "focused" if runtime.focus_index == 2 else "faint")

    # --- Forecast & Budget strip ----------------------------------------
    if snap.forecast:
        b100 = next((r for r in snap.forecast.budgets if r.target_percent == 100), None)
        b90 = next((r for r in snap.forecast.budgets if r.target_percent == 90), None)
        b100_s = (
            f"${b100.dollars_per_day:.2f}/d"
            if b100 and b100.dollars_per_day is not None else "—"
        )
        b90_s = (
            f"${b90.dollars_per_day:.2f}/d"
            if b90 and b90.dollars_per_day is not None else "—"
        )
        reset_str = (
            format_display_dt(cw.week_end_at, runtime.display_tz, fmt="%b %d %H:%M", suffix=True)
            if cw else "—"
        )
        fcstrip_body = [
            "",
            (f"  {{dim}}wk-avg{{/}} {{{_tui_bar_color(wa)}.b}}{wa}%{{/}}  "
             f"{{dim}}24h{{/}} {{{_tui_bar_color(rc)}.b}}{rc}%{{/}}  "
             f"{{faint}}│{{/}}  "
             f"{{dim}}≤100%{{/}} {{bright.b}}{b100_s}{{/}}  "
             f"{{dim}}≤90%{{/}} {{bright.b}}{b90_s}{{/}}  "
             f"{{faint}}│{{/}}  "
             f"{{dim}}reset{{/}} {{bright}}{reset_str}{{/}}"),
            "",
        ]
    else:
        fcstrip_body = ["", "  {dim}forecast unavailable{/}", ""]
    fc_strip_box = _tui_tagged_box_lines(
        width=width, body_tagged=fcstrip_body,
        title="{accent.b}Forecast & Budget{/}",
        pin=("{focused}focus{/}" if runtime.focus_index == 1 else "{dim}[2]{/}"),
        border_style=("focused" if runtime.focus_index == 1 else "faint"),
    )

    # --- Sessions (full-width, always focused in B) --------------------
    # Chrome around the sessions panel:
    #   ribbon(1) + sub(1) + rule(1) + 1 blank
    #   + hero_box lines
    #   + 1 blank after hero
    #   + fc_strip_box lines
    #   + 1 blank after fc strip
    #   + 2 lines for the sessions box borders (top + bottom)
    #   + 5 sessions-panel chrome lines (leading blank + header + ruler
    #     + trailing blank + "↑↓ scroll · N below")
    #   + footer_lines(2)
    if runtime.input_mode is not None:
        match_count = None
        if runtime.input_mode == "filter":
            match_count = sum(
                1 for s in snap.sessions
                if (runtime.input_buffer.lower() in s.project_label.lower()
                    or runtime.input_buffer.lower() in s.model_primary.lower())
            ) if runtime.input_buffer else None
        elif runtime.input_mode == "search":
            if runtime.input_buffer:
                needle = runtime.input_buffer.lower()
                # See _tui_render_variant_a: count must match the navigable
                # post-filter set used by _tui_panel_sessions.
                pool = _tui_apply_session_filter(snap.sessions, runtime.filter_term)
                count = 0
                for s in pool:
                    hay = (
                        s.project_label.lower() + "|"
                        + s.model_primary.lower() + "|"
                        + _tui_format_started(s.started_at, snap.generated_at, runtime.display_tz).lower()
                    )
                    if needle in hay:
                        count += 1
                match_count = count
            else:
                match_count = None
        footer_lines = _tui_render_input_prompt(runtime, width, match_count=match_count)
    else:
        footer_lines = _tui_footer_keys(width)
    sessions_chrome = 5       # "" + header + ruler + "" + "↑↓ scroll · N below"
    box_borders = 2           # sessions box top + bottom
    non_session_rows = (
        4                      # ribbon + sub + rule + blank
        + len(hero_box)
        + 1                    # blank after hero
        + len(fc_strip_box)
        + 1                    # blank after fc strip
        + box_borders
        + sessions_chrome
        + len(footer_lines)
    )
    rows_visible = max(3, height - non_session_rows)
    sess_body = _tui_panel_sessions(
        snap, runtime, width,
        rows_visible=rows_visible,
        show_project_col=(bucket == "wide"),
    )
    sess_box = _tui_tagged_box_lines(
        width=width, body_tagged=sess_body,
        title=_tui_sessions_title(runtime, narrow=(bucket == "narrow")),
        pin=("{focused}focus{/}" if runtime.focus_index == 3 else "{dim}[4]{/}"),
        border_style=("focused" if runtime.focus_index == 3 else "faint"),
    )

    # --- Assemble -------------------------------------------------------
    rule_line = "{faint}" + ("═" * width) + "{/}"
    warn_line = (
        "{warn}⚠ narrow terminal — some columns hidden{/}"
        if bucket == "narrow" else None
    )

    hero_text = _tui_lines_to_text(hero_box)
    trend_text = _tui_lines_to_text(trend_box)
    fc_strip_text = _tui_lines_to_text(fc_strip_box)
    sess_text = _tui_lines_to_text(sess_box)
    footer_text = _tui_lines_to_text(footer_lines)

    root = Layout()
    regions: list[Layout] = [
        Layout(name="ribbon", size=1),
        Layout(name="sub", size=1),
        Layout(name="rule", size=1),
    ]
    if warn_line is not None:
        regions.append(Layout(name="warn", size=1))

    if overlay_panel is not None:
        # Fallback A: collapse the body bands into one ``body`` region
        # holding a centered overlay Panel. Ribbon/sub/rule/footer remain.
        body_rows = (
            1                 # blank after rule
            + len(hero_box)
            + 1               # blank after hero
            + len(fc_strip_box)
            + 1               # blank after fc strip
            + len(sess_box)
        )
        regions.append(Layout(name="body", size=body_rows))
        regions.append(Layout(name="footer", size=len(footer_lines)))
        root.split_column(*regions)
        root["ribbon"].update(_tui_lines_to_text([ribbon]))
        root["sub"].update(_tui_lines_to_text([sub]))
        root["rule"].update(_tui_lines_to_text([rule_line]))
        if warn_line is not None:
            root["warn"].update(_tui_lines_to_text([warn_line]))
        root["body"].update(Align.center(overlay_panel, vertical="middle"))
        root["footer"].update(footer_text)
        root._tui_natural_height = sum(r.size or 0 for r in regions)
        return root

    regions.extend([
        Layout(name="blank1", size=1),
        Layout(name="hero_row", size=len(hero_box)),
        Layout(name="blank2", size=1),
        Layout(name="fc_strip", size=len(fc_strip_box)),
        Layout(name="blank3", size=1),
        Layout(name="sessions", size=len(sess_box)),
        Layout(name="footer", size=len(footer_lines)),
    ])
    root.split_column(*regions)

    root["ribbon"].update(_tui_lines_to_text([ribbon]))
    root["sub"].update(_tui_lines_to_text([sub]))
    root["rule"].update(_tui_lines_to_text([rule_line]))
    if warn_line is not None:
        root["warn"].update(_tui_lines_to_text([warn_line]))

    root["blank1"].update(_tui_lines_to_text([""]))
    root["hero_row"].split_row(
        Layout(name="hero", size=hero_left_w),
        Layout(name="trend", size=hero_right_w),
    )
    root["hero_row"]["hero"].update(hero_text)
    root["hero_row"]["trend"].update(trend_text)
    root["blank2"].update(_tui_lines_to_text([""]))
    root["fc_strip"].update(fc_strip_text)
    root["blank3"].update(_tui_lines_to_text([""]))
    root["sessions"].update(sess_text)
    root["footer"].update(footer_text)
    root._tui_natural_height = sum(r.size or 0 for r in regions)
    return root


_TUI_HELP_LINES = [
    "",
    "  {accent.b}Dashboard{/}",
    "",
    "    {bright}q{/} {dim}·{/} {fg}quit{/}",
    "    {bright}r{/} {dim}·{/} {fg}force refresh{/}",
    "    {bright}v{/} {dim}·{/} {fg}toggle variant (conventional/expressive){/}",
    "    {bright}Tab{/} {dim}·{/} {fg}cycle focus across panels{/}",
    "    {bright}↑↓ / j k{/} {dim}·{/} {fg}scroll sessions or modal{/}",
    "    {bright}PgUp/PgDn{/} {dim}·{/} {fg}page scroll{/}",
    "",
    "  {accent.b}Sessions panel{/}",
    "    {bright}s{/} {dim}·{/} {fg}cycle sort key{/}",
    "    {bright}f{/} {dim}·{/} {fg}filter (project|model substring){/}",
    "    {bright}/{/} {dim}·{/} {fg}search (highlight + jump){/}",
    "    {bright}n / N{/} {dim}·{/} {fg}next/prev search match{/}",
    "",
    "  {accent.b}Detail modals{/}",
    "    {bright}Enter{/} {dim}·{/} {fg}open detail of focused panel{/}",
    "    {bright}1 2 3 4{/} {dim}·{/} {fg}open Current/Forecast/Trend/Sessions detail{/}",
    "    {bright}Esc{/} {dim}·{/} {fg}close modal · cancel input · close help{/}",
    "",
    "  {bright}?{/} {dim}·{/} {fg}toggle help{/}",
    "",
]


def _tui_render_help(width: int, height: int) -> Panel:
    """Return a ``rich.panel.Panel`` for the help overlay.

    The Panel lists the keybindings; the caller is responsible for
    centering it via ``rich.align.Align.center`` and composing it over
    the variant Layout (see the body-region-swap overlay in
    ``_tui_render_variant_a`` / ``_tui_render_variant_b``).

    ``height`` is accepted for API symmetry but not currently used —
    the Panel auto-sizes to its content.
    """
    from rich import box as _rich_box
    from rich.panel import Panel as _Panel

    # Build body Text from _TUI_HELP_LINES verbatim (tags unchanged).
    body = _tui_lines_to_text(_TUI_HELP_LINES)
    panel_w = min(max(width - 4, 20), 60)
    return _Panel(
        body,
        box=_rich_box.DOUBLE,
        title=_tui_colortag("{accent.b}Help{/}"),
        subtitle=_tui_colortag("{dim}? to close{/}"),
        border_style="accent",
        width=panel_w,
    )


def _tui_modal_max_width(width: int) -> int:
    """Per-bucket modal width (spec §5.1)."""
    bucket = _tui_width_bucket(width)
    if bucket == "wide":
        return min(width - 4, 90)
    if bucket == "compact":
        return min(width - 4, 70)
    # narrow
    return max(60, width - 2)


def _tui_render_modal(
    snap: DataSnapshot,
    runtime: RuntimeState,
    width: int,
    height: int,
) -> Panel:
    """Render the active detail modal as a centered Panel.

    Dispatches on runtime.modal_kind to a per-kind content builder.
    Per-kind builders return list[str] (color-tagged lines); this
    function slices them by runtime.modal_scroll, wraps in a Panel
    with the shared chrome, and returns it for body-region-swap
    composition.

    Spec §4.1 (chrome) + §4.6 (per-kind content).
    """
    from rich import box as _rich_box
    from rich.panel import Panel as _Panel

    kind = runtime.modal_kind or "current_week"
    if kind == "current_week":
        title, content_lines = _tui_modal_current_week(snap, runtime, width)
    elif kind == "forecast":
        title, content_lines = _tui_modal_forecast(snap, runtime, width)
    elif kind == "trend":
        title, content_lines = _tui_modal_trend(snap, runtime, width)
    elif kind == "session":
        title, content_lines = _tui_modal_session(snap, runtime, width)
    else:
        title = "Modal"
        content_lines = ["", "  {dim}placeholder{/}", ""]

    panel_w = _tui_modal_max_width(width)
    panel_h = min(height - 4, 30)
    viewport = max(5, panel_h - 4)  # subtract title + subtitle + padding

    total = len(content_lines)
    scroll = max(0, min(runtime.modal_scroll, max(0, total - viewport)))
    runtime.modal_scroll = scroll  # write back the clamp
    visible = content_lines[scroll : scroll + viewport]
    # Pad to viewport so the panel height is stable across scrolls.
    while len(visible) < viewport:
        visible.append("")

    body = _tui_lines_to_text(visible)
    subtitle = "{dim}Esc back{/}"
    if total > viewport:
        subtitle = f"{{dim}}Esc back · {scroll + 1}-{scroll + viewport}/{total} ↓{{/}}"

    return _Panel(
        body,
        box=_rich_box.DOUBLE,
        title=_tui_colortag(title),
        subtitle=_tui_colortag(subtitle),
        border_style="accent",
        width=panel_w,
    )


# ---- per-kind modal content builders (spec §4.6) ----
def _tui_modal_current_week(snap, runtime, width):
    """Per-percent milestones for the current week (spec §4.6.1)."""
    cw = snap.current_week
    milestones = snap.percent_milestones
    if cw is None:
        return ("{accent.b}Current Week · per-percent{/}",
                ["", "  {dim}No current week — run record-usage{/}", ""])
    if not milestones:
        return ("{accent.b}Current Week · per-percent{/}",
                ["", "  {dim}No milestones yet — keep recording usage.{/}", ""])
    avg_dpp = (cw.dollars_per_percent or 0.0)
    cumul = cw.spent_usd
    header = [
        "",
        f"  {{dim}}Week{{/}} {{b}}{format_display_dt(cw.week_start_at, runtime.display_tz, fmt='%b %d', suffix=False)} – {format_display_dt(cw.week_end_at, runtime.display_tz, fmt='%b %d', suffix=False)}{{/}}   "
        f"{{dim}}milestones reached{{/}} {{warn.b}}{len(milestones)}{{/}}",
        f"  {{dim}}avg $/1%{{/}} {{b}}${avg_dpp:.2f}{{/}}     {{dim}}cumulative{{/}} {{b}}${cumul:.2f}{{/}}",
        "",
    ]
    bucket = _tui_width_bucket(width)
    show_5h = bucket != "narrow"
    if show_5h:
        header.append("   {dim.b}  %  Crossed at             Cumul    Marginal   5-hr{/}")
        header.append("   {faint}─── ────────────────────── ──────── ────────── ──────{/}")
    else:
        header.append("   {dim.b}  %  Crossed at             Cumul    Marginal{/}")
        header.append("   {faint}─── ────────────────────── ──────── ──────────{/}")
    rows = []
    for ms in milestones:
        ts_str = format_display_dt(
            ms.crossed_at, runtime.display_tz,
            fmt="%b %d %H:%M:%S", suffix=True,
        )
        cumul_str = f"${ms.cumulative_cost_usd:.2f}".ljust(8)
        marg_str = (f"${ms.marginal_cost_usd:.2f}" if ms.marginal_cost_usd is not None else "—").ljust(10)
        line = f"   {{b}}{ms.percent:>3}{{/}} {{bright}}{ts_str:<22}{{/}} {{b}}{cumul_str}{{/}} {{b}}{marg_str}{{/}}"
        if show_5h:
            five_str = (f"{int(ms.five_hour_pct_at_crossing)}%"
                        if ms.five_hour_pct_at_crossing is not None else "—")
            line += f" {{dim}}{five_str:<5}{{/}}"
        rows.append(line)
    if runtime.modal_snap_pending:
        if len(rows) > 10:
            runtime.modal_scroll = len(rows) + len(header) - 10
        runtime.modal_snap_pending = False
    return ("{accent.b}Current Week · per-percent{/}", header + rows)

def _tui_modal_forecast(snap, runtime, width):
    """Forecast --explain content (spec §4.6.2)."""
    fc = snap.forecast
    if fc is None or getattr(fc, "inputs", None) is None:
        return ("{accent.b}Forecast · explain{/}",
                ["", "  {dim}Forecast unavailable — current week is empty.{/}", ""])
    inp = fc.inputs
    verdict = _tui_verdict_of(fc)
    vcls = _TUI_VERDICT_CLS[verdict]
    # Hero label band right-pads to 15 chars so value columns align:
    # "Now" + 12, "Week elapsed" + 3, "Used now" + 7, "Used 24h ago" + 3.
    lines = [
        "",
        f"  {{dim}}Now{{/}}            {{b}}{format_display_dt(inp.now_utc, runtime.display_tz, fmt='%Y-%m-%d %H:%M', suffix=True)}{{/}}",
        f"  {{dim}}Week elapsed{{/}}   {{b}}{inp.elapsed_hours:.1f}h / 168h{{/}}  "
        f"{{dim}}({(inp.elapsed_hours / 168 * 100):.1f}%){{/}}",
        f"  {{dim}}Used now{{/}}       {{warn.b}}{inp.p_now:.1f}%{{/}}",
    ]
    if inp.p_24h_ago is not None:
        lines.append(f"  {{dim}}Used 24h ago{{/}}   {{dim}}{inp.p_24h_ago:.1f}%{{/}}")
    else:
        lines.append("  {dim}Used 24h ago{/}   {dim}—  (insufficient history){/}")
    lines.append("")
    lines.append("  {dim.b}Two rate paths{/}")
    lines.append(f"  {{dim}}  r_avg     {inp.p_now:.1f} / {inp.elapsed_hours:.1f}      = {{/}}{{b}}{fc.r_avg:.4f} %/h{{/}}")
    if fc.r_recent is not None and inp.p_24h_ago is not None:
        lines.append(f"  {{dim}}  r_recent  ({inp.p_now:.1f}-{inp.p_24h_ago:.1f}) / {inp.t_24h_actual_hours:.1f}  = {{/}}{{b}}{fc.r_recent:.4f} %/h{{/}}")
    else:
        lines.append("  {dim}  r_recent  unavailable — no 24h-prior sample{/}")
    lines.append("")
    lines.append(f"  {{dim.b}}Project to week end ({inp.remaining_hours:.1f}h remaining){{/}}")
    if fc.r_recent is not None:
        wa = inp.p_now + fc.r_avg * inp.remaining_hours
        rc = inp.p_now + fc.r_recent * inp.remaining_hours
        lines.append(f"  {{dim}}  by week-avg    = {{/}}{{warn}}{wa:.1f}%{{/}}")
        lines.append(f"  {{dim}}  by recent-24h  = {{/}}{{ok}}{rc:.1f}%{{/}}")
        lines.append(f"  {{dim}}  high           = {{/}}{{{vcls}.b}}{fc.final_percent_high:.1f}%{{/}}     {{dim}}verdict:{{/}} {{{vcls}.b}}{verdict}{{/}}")
    else:
        lines.append(f"  {{dim}}  projection     = {{/}}{{{vcls}.b}}{fc.final_percent_high:.1f}%{{/}}     {{dim}}verdict:{{/}} {{{vcls}.b}}{verdict}{{/}}")
    lines.append("")
    lines.append(f"  {{dim.b}}Daily $ budgets ({inp.remaining_days:.3f} days remaining){{/}}")
    for b in fc.budgets:
        if b.dollars_per_day is not None:
            lines.append(f"  {{dim}}  ≤{b.target_percent}%   {{/}}{{b}}${b.dollars_per_day:.2f}/day{{/}}")
        else:
            lines.append(f"  {{dim}}  ≤{b.target_percent}%   {{/}}{{dim}}—  (already past){{/}}")
    lines.append("")
    confidence = inp.confidence
    lines.append(f"  {{dim}}confidence: {confidence} · based on 7-day rate{{/}}")
    return ("{accent.b}Forecast · explain{/}", lines)

def _tui_modal_trend(snap, runtime, width):
    """Weekly history for the Trend modal (spec §4.6.3)."""
    history = snap.weekly_history
    if not history:
        return ("{accent.b}Trend · weekly history{/}",
                ["", "  {dim}No weekly history available yet.{/}", ""])
    bucket = _tui_width_bucket(width)
    show_age = bucket != "narrow"
    # Header
    valid = [h for h in history if h.dollars_per_percent is not None]
    avg_dpp = sum(h.dollars_per_percent for h in valid) / len(valid) if valid else 0.0
    if len(valid) >= 2:
        first_dpp = valid[0].dollars_per_percent
        last_dpp = valid[-1].dollars_per_percent
        trend_pct = ((last_dpp - first_dpp) / first_dpp * 100) if first_dpp else 0.0
    else:
        trend_pct = 0.0
    trend_sign = "+" if trend_pct >= 0 else ""
    trend_cls = "warn" if abs(trend_pct) >= 5 else "ok"
    lines = [
        "",
        f"  {{dim}}Last{{/}} {{b}}{len(history)} weeks{{/}}     "
        f"{{dim}}avg $/1%{{/}} {{b}}${avg_dpp:.2f}{{/}}     "
        f"{{dim}}trend{{/}} {{{trend_cls}}}{trend_sign}{trend_pct:.0f}%{{/}}",
        "",
    ]
    if show_age:
        lines.append("   {dim.b}  Week starting    Used%    Cost     $/1%      Δ{/}")
        lines.append("   {faint}─────────────────  ──────  ────────  ──────  ──────{/}")
    else:
        lines.append("   {dim.b}  Week     Used%    Cost     $/1%{/}")
        lines.append("   {faint}─────────  ──────  ────────  ──────{/}")
    n = len(history)
    for i, h in enumerate(history):
        ago = n - 1 - i
        marker = "▶" if h.is_current else " "
        if h.used_pct is None:
            used_cell = "—"
            used_cls = "dim"
        else:
            used_cell = f"{h.used_pct:.1f}%"
            used_cls = "ok" if h.used_pct < 70 else ("warn" if h.used_pct < 90 else "bad")
        dpp_cell = f"${h.dollars_per_percent:.2f}" if h.dollars_per_percent is not None else "—"
        delta_cell = ""
        if h.delta_dpp is not None:
            sign = "+" if h.delta_dpp >= 0 else ""
            cls = "ok" if h.delta_dpp >= 0 else "dim"
            delta_cell = f"{{{cls}}}{sign}{h.delta_dpp:.2f}{{/}}"
        else:
            delta_cell = "{dim}  —{/}"
        cost_cell = (f"${(h.dollars_per_percent or 0) * (h.used_pct or 0):.2f}"
                     if h.used_pct is not None and h.dollars_per_percent else "—")
        if show_age:
            label = f"{h.week_label} ({ago:>2}w ago)" if ago > 0 else f"{h.week_label} (now)"
            lines.append(
                f"   {{focused.b}}{marker}{{/}} {{b}}{label:<17}{{/}}  "
                f"{{{used_cls}}}{used_cell:>5}{{/}}  {{b}}{cost_cell:>7}{{/}}  "
                f"{{b}}{dpp_cell:>5}{{/}}   {delta_cell}"
            )
        else:
            lines.append(
                f"   {{focused.b}}{marker}{{/}} {{b}}{h.week_label:<7}{{/}}  "
                f"{{{used_cls}}}{used_cell:>5}{{/}}  {{b}}{cost_cell:>7}{{/}}  {{b}}{dpp_cell:>5}{{/}}"
            )
    lines.append("")
    # Default scroll: bottom (current week is most relevant).
    if runtime.modal_snap_pending:
        if len(lines) > 12:
            runtime.modal_scroll = len(lines) - 12
        runtime.modal_snap_pending = False
    return ("{accent.b}Trend · weekly history{/}", lines)

def _tui_modal_session(snap, runtime, width):
    """Session detail modal (spec §4.6.4).

    Looks up the topmost-visible session by index and queries
    _tui_build_session_detail on demand. Fixture goldens may inject
    a deterministic detail via runtime.session_detail_override.
    """
    sessions = _tui_sort_sessions(snap.sessions, runtime.sort_key)
    if runtime.filter_term:
        af_lower = runtime.filter_term.lower()
        sessions = [
            s for s in sessions
            if af_lower in s.project_label.lower()
            or af_lower in s.model_primary.lower()
        ]
    if not sessions:
        return ("{accent.b}Session · detail{/}",
                ["", "  {dim}No session selected.{/}", ""])
    idx = max(0, min(runtime.session_scroll, len(sessions) - 1))
    sel = sessions[idx]
    # Fixture-injection hook (dev-only, spec §5.5)
    detail = getattr(runtime, "session_detail_override", None)
    if detail is None:
        cache = runtime.session_detail_cache
        if (cache is not None
                and cache[0] == sel.session_id
                and cache[1] == snap.generated_at):
            detail = cache[2]
        else:
            detail = _tui_build_session_detail(sel.session_id, now_utc=snap.generated_at)
            runtime.session_detail_cache = (sel.session_id, snap.generated_at, detail)
    if detail is None:
        return ("{accent.b}Session · detail{/}",
                ["", "  {warn}Session no longer available · Esc to return{/}", ""])
    bucket = _tui_width_bucket(width)
    show_cwd = bucket != "narrow"
    show_full_id = bucket != "narrow"
    sid_display = detail.session_id if show_full_id else detail.session_id[:8]

    title = f"{{accent.b}}Session · {format_display_dt(detail.started_at, runtime.display_tz, fmt='%H:%M:%S', suffix=True)} ({_tui_escape_tags(detail.project_label)}){{/}}"
    lines = [
        "",
        f"  {{dim}}Session ID{{/}}     {{b}}{sid_display}{{/}}",
        f"  {{dim}}Started{{/}}        {{b}}{format_display_dt(detail.started_at, runtime.display_tz, fmt='%Y-%m-%d %H:%M:%S', suffix=True)}{{/}}",
        f"  {{dim}}Last activity{{/}}  {{b}}{format_display_dt(detail.last_activity_at, runtime.display_tz, fmt='%Y-%m-%d %H:%M:%S', suffix=True)}{{/}}",
        f"  {{dim}}Duration{{/}}       {{b}}{_tui_format_dur(detail.duration_minutes)}{{/}}",
        f"  {{dim}}Project{{/}}        {{b}}{_tui_escape_tags(detail.project_label)}{{/}}",
    ]
    if show_cwd:
        cwd_max = max(20, _tui_modal_max_width(width) - 18)
        cwd_shown = detail.project_path
        if len(cwd_shown) > cwd_max:
            cwd_shown = "…" + cwd_shown[-(cwd_max - 1):]
        lines.append(f"  {{dim}}  cwd{{/}}          {{dim}}{_tui_escape_tags(cwd_shown)}{{/}}")
    src_count = len(detail.source_paths)
    src_note = "1 (no resumes across files)" if src_count == 1 else f"{src_count} (resumed across files)"
    lines.append(f"  {{dim}}Source files{{/}}   {{b}}{src_note}{{/}}")
    lines.append("")
    lines.append("  {dim.b}Models{/}")
    for model_name, role in detail.models:
        padded = f"{model_name:<16}"
        lines.append(f"  {{dim}}  {{/}}{{b}}{_tui_escape_tags(padded)}{{/}}{{dim}}{role}{{/}}")
    lines.append("")
    lines.append("  {dim.b}Tokens{/}")
    lines.append(f"  {{dim}}  Input        {{/}} {{b}}{detail.input_tokens:>10,}{{/}}")
    lines.append(f"  {{dim}}  Cache create {{/}} {{b}}{detail.cache_creation_tokens:>10,}{{/}}")
    cache_pct_str = (f"   {{ok}}{int(detail.cache_hit_pct)}% cache hit{{/}}"
                     if detail.cache_hit_pct is not None else "")
    lines.append(f"  {{dim}}  Cache read   {{/}} {{b}}{detail.cache_read_tokens:>10,}{{/}}{cache_pct_str}")
    lines.append(f"  {{dim}}  Output       {{/}} {{b}}{detail.output_tokens:>10,}{{/}}")
    lines.append("")
    lines.append("  {dim.b}Cost{/}")
    for model_name, cost in detail.cost_per_model:
        padded = f"{model_name:<13}"
        lines.append(f"  {{dim}}  {_tui_escape_tags(padded)}{{/}} {{b}}${cost:.2f}{{/}}")
    lines.append("  {faint}  ─────────────────────{/}")
    lines.append(f"  {{dim}}  Total         {{/}} {{b}}${detail.cost_total_usd:.2f}{{/}}")
    return (title, lines)


def _tui_render_toast(msg: str, width: int):
    """Render a one-line deferred-feature toast as a rich.text.Text.

    The toast surfaces the message in a warn-badge style. `width` is
    accepted for symmetry with the other renderers but is not used for
    padding — the toast sits inline wherever the caller places it.
    """
    content = f"  {msg}  "
    padded = f" {{badge.warn}}{content}{{/}} "
    return _tui_colortag(padded)


def _tui_sync_interval_type(s: str) -> float:
    """argparse type validator for --sync-interval: float >= 1.0."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--sync-interval must be a number (got {s!r})"
        )
    if v < 1.0:
        raise argparse.ArgumentTypeError(
            f"--sync-interval must be >= 1.0 seconds (got {v})"
        )
    return v


def _tui_refresh_interval_type(s: str) -> float:
    """argparse type validator for --refresh: float > 0.0.

    Non-positive values would make the keyboard-poll select() return
    immediately every iteration, busy-spinning the redraw loop.
    """
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--refresh must be a number (got {s!r})"
        )
    if v <= 0.0:
        raise argparse.ArgumentTypeError(
            f"--refresh must be > 0 seconds (got {v})"
        )
    return v


def _make_run_sync_now_locked(*, ref, hub, pinned_now, display_tz_pref_override):
    """Return a closure that does the snapshot-rebuild + SSE-publish work.

    Caller MUST hold sync_lock around the call. The naming convention
    (``_locked`` suffix) is the contract; threading.Lock has no
    "is_held_by_current_thread" check, so we don't introspect.

    Splitting the locked body out of the public wrapper lets ``/api/sync``
    callers that already hold ``sync_lock`` (e.g. so they can refresh OAuth
    + rebuild snapshot atomically without releasing between steps) reuse
    this body without recursive-acquire / self-deadlock.
    """
    def _locked(skip_sync: bool) -> None:
        try:
            # Resolve _tui_build_snapshot via cctally's namespace so the
            # eager re-export AND ``monkeypatch.setitem(ns, "_tui_build_snapshot", spy)``
            # in tests/test_dashboard_api_sync_refresh.py propagate into
            # this closure body (the bare-name lookup would resolve in
            # this sibling's __dict__ and miss the cctally-side patch).
            snap = sys.modules["cctally"]._tui_build_snapshot(
                now_utc=pinned_now, skip_sync=skip_sync,
                display_tz_pref_override=display_tz_pref_override,
            )
            if skip_sync:
                # Mirror the startup override: suppress the monotonic sync
                # stamp so the envelope keeps emitting sync_age_s=None and
                # the client keeps rendering "sync paused" after the user
                # hits r / clicks the sync chip.
                snap = dataclasses.replace(snap, last_sync_at=None)
            ref.set(snap)
            hub.publish(snap)
        except Exception as exc:
            prev = ref.get()
            crashed = dataclasses.replace(
                prev,
                last_sync_error=f"sync crashed: {exc}",
                generated_at=dt.datetime.now(dt.timezone.utc),
            )
            ref.set(crashed)
            hub.publish(crashed)
    return _locked


def _make_run_sync_now(*, sync_lock, ref, hub, pinned_now,
                       display_tz_pref_override):
    """Return a closure that acquires sync_lock then runs the locked variant.

    Used by the periodic background thread and by anything else that needs
    full lifecycle (acquire-do-release) in one call. ``/api/sync`` paths
    that compose multiple lock-protected steps should use the locked variant
    directly instead of nesting ``with sync_lock:`` (re-entrant acquire on
    a non-recursive ``threading.Lock`` self-deadlocks).
    """
    locked = _make_run_sync_now_locked(
        ref=ref, hub=hub, pinned_now=pinned_now,
        display_tz_pref_override=display_tz_pref_override,
    )
    def _public(skip_sync: bool) -> None:
        with sync_lock:
            locked(skip_sync)
    return _public



def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the live TUI dashboard. See docs/commands/tui.md.

    Live-path state machine:
      1. Resolve `now_utc` (honor --as-of).
      2. Build RuntimeState from args (honors NO_COLOR + --no-color).
      3. If --render-once: defer to `_tui_render_once` and return.
      4. Build theme, construct Console(theme, no_color=...).
      5. Refuse if terminal width < 80 columns.
      6. Build initial snapshot (skip_sync=True, non-blocking).
      7. Wrap in _SnapshotRef.
      8. Start _TuiSyncThread unless --no-sync.
      9. rich.live.Live with alternate screen, auto_refresh=False.
     10. TuiKeyReader (raw mode, cbreak).
     11. SIGINT → should_exit flag (no raising into Live).
         SIGWINCH → no-op (next tick picks up console.size).
     12. On every tick: read key → mutate runtime → live.update().
         On no key: natural tick redraw.
     13. Finally: stop sync thread.
    """
    try:
        import rich  # noqa: F401
    except ImportError:
        print(TUI_RICH_MISSING_MSG, file=sys.stderr)
        return 1

    # --- 1. Resolve now ----------------------------------------------------
    now_utc = _resolve_forecast_now(getattr(args, "as_of", None))

    # --- 2a. Resolve display tz via the unified --tz / config.display.tz.
    # RuntimeState.tz keeps the legacy token shape for any string-keyed
    # call sites; F4 moved _tui_format_started to consume display_tz
    # (ZoneInfo | None) directly so non-"local" values now localize
    # correctly instead of falling back to UTC. Normalize args.tz back to
    # a token shape: None -> "local"; Etc/UTC -> "utc"; explicit IANA ->
    # the verbatim IANA name.
    config = load_config()
    # Capture the raw `--tz` flag BEFORE resolution rewrites args.tz, so
    # `_tui_build_snapshot` can apply the same persisted-config override
    # that `cmd_dashboard` uses (parallel to lines 24927-24936). Without
    # this, panels that precompute labels at snapshot-build time (trend,
    # weekly-history) render the persisted `config.display.tz` instead of
    # honoring the explicit per-call `--tz` override.
    raw_tz_flag = getattr(args, "tz", None)
    if raw_tz_flag is not None and str(raw_tz_flag).strip() != "":
        try:
            display_tz_pref_override = normalize_display_tz_value(raw_tz_flag)
        except ValueError:
            display_tz_pref_override = None
    else:
        display_tz_pref_override = None
    tz_obj = resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    if tz_obj is None:
        args.tz = "local"
    elif tz_obj.key == "Etc/UTC":
        args.tz = "utc"
    else:
        args.tz = tz_obj.key
    # Stash the override on `args` so `_tui_render_once` (the dev path)
    # can pick it up uniformly without a separate kwarg.
    args._display_tz_pref_override = display_tz_pref_override

    # --- 2. Runtime state --------------------------------------------------
    runtime = RuntimeState.initial(args)

    # --- 3. Dev path: one-shot render -------------------------------------
    if getattr(args, "render_once", False):
        return _tui_render_once(args, runtime, now_utc=now_utc)

    # --- 3b. Require an interactive terminal ------------------------------
    # Live mode drives alt-screen via rich.live.Live(screen=True) and reads
    # keys from stdin in cbreak mode. Without a TTY on both ends, there is
    # no quit path short of SIGINT and Live's escape sequences are
    # meaningless. Refuse fast so cron/CI invocations fail instead of
    # wedging. `--render-once` (above) is the scriptable alternative.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "tui: requires an interactive terminal "
            "(stdin and stdout must be TTYs). "
            "For scripted use, try `report`, `forecast`, or "
            "`tui --render-once --snapshot-module PATH`.",
            file=sys.stderr,
        )
        return 2

    # --- 4. Theme ----------------------------------------------------------
    # Drift guard moved to module scope (see _TUI_THEME_KEYS) and to
    # _tui_build_theme itself, so both axes fire at import time / theme
    # construction rather than on subcommand invocation.
    theme = _tui_build_theme()

    # --- 5. Console + width refuse ----------------------------------------
    from rich.console import Console
    from rich.live import Live
    console = Console(theme=theme, no_color=not runtime.color_enabled)
    width = console.size.width
    if _tui_width_bucket(width) == "refuse":
        print(
            f"tui: terminal too narrow, need >=80 cols (got {width})",
            file=sys.stderr,
        )
        return 1

    # --- 6. Initial snapshot ----------------------------------------------
    try:
        initial_snap = _tui_build_snapshot(
            now_utc=now_utc, skip_sync=True,
            display_tz_pref_override=display_tz_pref_override,
        )
    except Exception:
        initial_snap = _tui_empty_snapshot(now_utc)

    # --- 7. Shared ref -----------------------------------------------------
    ref = _SnapshotRef(initial_snap)

    # --- 11. Signal handlers ----------------------------------------------
    # Install signal handlers BEFORE sync.start() so a SIGINT during
    # thread startup is caught by our flag-setter rather than the default
    # handler (which would unwind past the finally block that calls
    # sync.stop(), leaking a daemon thread). See Task 26 review I3.
    import signal
    import time as _time
    should_exit = {"flag": False}

    def _on_sigint(_signum, _frame):
        should_exit["flag"] = True

    prev_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _on_sigint)
    prev_sigwinch = None
    if hasattr(signal, "SIGWINCH"):
        # No-op — Live's next tick reads console.size fresh.
        prev_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda *_: None)
    # SIGCONT (resume from Ctrl-Z): same idea — just let next tick redraw.
    prev_sigcont = None
    if hasattr(signal, "SIGCONT"):
        prev_sigcont = signal.getsignal(signal.SIGCONT)
        signal.signal(signal.SIGCONT, lambda *_: None)

    # --- 8. Sync thread ----------------------------------------------------
    # Always start the rebuild thread — even under --no-sync we need
    # periodic refreshes so countdowns, "synced Xs ago", and external DB
    # writes keep the dashboard live. The thread's own skip_sync flag
    # gates the JSONL ingest pass; the rebuild itself is always cheap
    # (SQLite SELECTs only, no JSONL scan when skip_sync=True).
    # Only pin the sync thread's clock when a clock override was actually
    # supplied — either via --as-of (CLI) or CCTALLY_AS_OF (env). Without
    # one of those, `now_utc` is just "now captured once at startup";
    # feeding that to the sync thread would freeze every subsequent
    # rebuild on that instant. Mirroring the same check that
    # _resolve_forecast_now() performed above keeps the hidden test hook
    # consistent across the first frame and every subsequent tick.
    pinned_now = now_utc if (
        getattr(args, "as_of", None) or os.environ.get("CCTALLY_AS_OF")
    ) else None
    sync = _TuiSyncThread(
        ref, float(args.sync_interval),
        skip_sync=bool(getattr(args, "no_sync", False)),
        now_utc=pinned_now,
        display_tz_pref_override=display_tz_pref_override,
    )
    sync.start()

    # --- 10. Render closure -----------------------------------------------
    from rich.console import Group
    def render():
        snap = ref.get()
        w = console.size.width
        h = console.size.height
        bucket = _tui_width_bucket(w)
        # Build the help Panel once per render so both variants can receive
        # it via the body-region-swap overlay path (Fallback A). When
        # show_help is False, help_panel stays None and the variants render
        # the normal 2x2 / hero layout.
        help_panel = _tui_render_help(w, h) if runtime.show_help else None
        modal_panel = (_tui_render_modal(snap, runtime, w, h)
                       if runtime.modal_kind else None)
        # v2: modal wins when both would show. Spec §4.7.
        overlay = modal_panel or help_panel
        if runtime.variant == "expressive":
            frame = _tui_render_variant_b(
                snap, runtime, w, h, bucket, overlay_panel=overlay,
            )
        else:
            frame = _tui_render_variant_a(
                snap, runtime, w, h, bucket, overlay_panel=overlay,
            )
        # Toast handling: expire when clock passes expiry; else stack below
        # the frame via ``rich.console.Group`` (the rich-native stacking
        # primitive required by constraint #4).
        if runtime.toast is not None:
            msg, expiry = runtime.toast
            if _time.monotonic() < expiry:
                toast_frame = _tui_render_toast(msg, w)
                return Group(frame, toast_frame)
            runtime.toast = None
        return frame

    # --- 12. Main loop ----------------------------------------------------
    reader = TuiKeyReader()
    try:
        with reader, Live(
            render(), console=console, screen=True,
            auto_refresh=False, transient=False,
        ) as live:
            while not should_exit["flag"]:
                key = reader.read(timeout=float(args.refresh))
                if key is not None:
                    redraw, quit_ = _tui_handle_key(key, runtime, ref)
                    if quit_:
                        break
                    if redraw:
                        live.update(render(), refresh=True)
                        continue
                # No key (or key with no redraw) — natural tick redraw.
                live.update(render(), refresh=True)
    finally:
        # --- 13. Teardown -------------------------------------------------
        if sync is not None:
            sync.stop()
        # Restore previous signal handlers.
        try:
            signal.signal(signal.SIGINT, prev_sigint)
        except Exception:
            pass
        if hasattr(signal, "SIGWINCH") and prev_sigwinch is not None:
            try:
                signal.signal(signal.SIGWINCH, prev_sigwinch)
            except Exception:
                pass
        if hasattr(signal, "SIGCONT") and prev_sigcont is not None:
            try:
                signal.signal(signal.SIGCONT, prev_sigcont)
            except Exception:
                pass
    return 0


def _tui_render_once(
    args: argparse.Namespace,
    runtime: "RuntimeState",
    *,
    now_utc: dt.datetime | None = None,
) -> int:
    """Dev-only: render one frame and emit plain text to stdout.

    Used by fixture goldens (later Tasks 28-29). Honors:
      --snapshot-module (load SNAPSHOT from a Python module for deterministic data)
      --force-size WxH  (default 120x36 if unset / malformed)

    Does NOT check the width-refuse bucket — --render-once is a dev path
    expected to work at any size so authors can capture narrow-width goldens.
    Returns 0 on success, 2 on malformed --force-size.
    """
    now_utc = now_utc or _resolve_forecast_now(getattr(args, "as_of", None))

    # --- Parse --force-size -----------------------------------------------
    force_size = getattr(args, "force_size", None)
    w, h = 120, 36
    if force_size:
        parts = force_size.lower().split("x", 1)
        if len(parts) != 2:
            print(
                f"tui: --force-size must be WxH (got {force_size!r})",
                file=sys.stderr,
            )
            return 2
        try:
            w = int(parts[0])
            h = int(parts[1])
        except ValueError:
            print(
                f"tui: --force-size W/H must be integers (got {force_size!r})",
                file=sys.stderr,
            )
            return 2
        if w <= 0 or h <= 0:
            print(
                f"tui: --force-size W/H must be positive (got {force_size!r})",
                file=sys.stderr,
            )
            return 2

    # --- Load snapshot ----------------------------------------------------
    snapshot_module = getattr(args, "snapshot_module", None)
    snap: DataSnapshot
    if snapshot_module:
        try:
            import importlib
            import importlib.util
            if snapshot_module.endswith(".py") or "/" in snapshot_module:
                # Treat as file path.
                spec = importlib.util.spec_from_file_location(
                    "_tui_snapshot_fixture", snapshot_module
                )
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {snapshot_module}")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                mod = importlib.import_module(snapshot_module)
            snap = getattr(mod, "SNAPSHOT")
            # v2 dev-only: snapshot modules may export a dict of RuntimeState
            # field overrides. Lets fixture goldens exercise sort/filter/search/
            # modal states without adding CLI flags. Spec §5.5.
            overrides = getattr(mod, "RUNTIME_OVERRIDES", None)
            if isinstance(overrides, dict):
                _ALLOWED_OVERRIDES = {
                    "sort_key", "filter_term", "search_term",
                    "search_matches", "search_index",
                    "modal_kind", "modal_scroll", "modal_snap_pending",
                    "focus_index", "session_scroll",
                    "input_mode", "input_buffer",
                    "session_detail_override",
                }
                for k, v in overrides.items():
                    if k in _ALLOWED_OVERRIDES:
                        setattr(runtime, k, v)
        except FileNotFoundError as exc:
            print(f"tui: snapshot module not found: {exc}", file=sys.stderr)
            return 2
        except (ImportError, AttributeError) as exc:
            print(
                f"tui: failed to load snapshot from {snapshot_module!r}: {exc}",
                file=sys.stderr,
            )
            return 2
    else:
        try:
            snap = _tui_build_snapshot(
                now_utc=now_utc, skip_sync=True,
                display_tz_pref_override=getattr(
                    args, "_display_tz_pref_override", None
                ),
            )
        except Exception:
            snap = _tui_empty_snapshot(now_utc)

    # --- Render -----------------------------------------------------------
    # Drift guards run at module import + inside _tui_build_theme itself,
    # so the explicit inline check has been removed.
    theme = _tui_build_theme()
    import io
    from rich.console import Console
    # file=StringIO() so console.print() writes into the recording buffer only
    # (not twice to stdout). export_text() then emits the clean captured copy.
    console = Console(
        theme=theme,
        record=True,
        width=w,
        height=h,
        no_color=not runtime.color_enabled,
        force_terminal=True,
        file=io.StringIO(),
    )
    bucket = _tui_width_bucket(w)
    help_panel = _tui_render_help(w, h) if runtime.show_help else None
    modal_panel = (_tui_render_modal(snap, runtime, w, h)
                   if runtime.modal_kind else None)
    # v2: modal wins when both would show. Spec §4.7.
    overlay = modal_panel or help_panel
    if runtime.variant == "expressive":
        frame = _tui_render_variant_b(snap, runtime, w, h, bucket, overlay_panel=overlay)
    else:
        frame = _tui_render_variant_a(snap, runtime, w, h, bucket, overlay_panel=overlay)
    # Layout fills the requested render height with blank rows if the
    # natural content is shorter, and truncates if taller. Use the
    # ``_tui_natural_height`` stashed by the variant renderers so the
    # recorded frame matches the pre-refactor row count (only trailing
    # whitespace on individual lines is expected to drift; line count
    # stays identical). Live mode ignores this and renders at terminal
    # height, which is the desired TUI fill behavior.
    render_h = getattr(frame, "_tui_natural_height", h) or h
    console.print(frame, height=render_h)
    # Default behavior: plain text (matches existing fixture-golden
    # expectations). FORCE_COLOR=1 opts in to ANSI escapes — used by the
    # README screenshot pipeline so freeze can render the TUI as a
    # colored SVG. Goldens never set FORCE_COLOR, so this is byte-safe
    # for the existing harness.
    include_styles = os.environ.get("FORCE_COLOR") == "1"
    sys.stdout.write(console.export_text(styles=include_styles))
    return 0
