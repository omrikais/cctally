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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Sequence


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3.
import _cctally_core
from _cctally_core import (
    eprint,
    parse_iso_datetime,
    _now_utc,
    open_db,
    get_latest_usage_for_week,
    _canonicalize_optional_iso,
    make_week_ref,
)
from _lib_display_tz import (
    format_display_dt,
    resolve_display_tz,
    normalize_display_tz_value,
    _compute_display_block,
)
from _lib_aggregators import _aggregate_monthly, codex_path_scope
from _lib_fmt import stable_sum
# Opt-in backend phase-instrumentation collector (issue #276, Session A). Pure
# stdlib leaf; near-noop when CCTALLY_PERF_TRACE is unset (phase() returns a
# shared no-op singleton), so the _tui_build_snapshot seam wraps below cost
# nothing on the default path.
import _lib_perf as _perf
import _lib_tick_stats as _tick_stats
import _lib_log

import importlib.util as _ilu


def _ensure_sibling_loaded(name: str) -> None:
    """Register a NON-eager-loaded ``_lib_*`` sibling in ``sys.modules``.

    ``_lib_forecast`` (#279 S4 F2) is a NEW consumer-only sibling — kept out
    of ``bin/cctally``'s eager-load block so ``bin/cctally`` stays
    byte-untouched (spec §2). Under the ``SourceFileLoader`` harness path
    (``bin/`` absent from ``sys.path``) a bare ``from _lib_forecast import``
    would miss, so this pre-registers the sibling ``__file__``-relative when
    it is not already importable (mirrors ``_cctally_cache._load_lib``). The
    honest import that follows is a ``sys.modules`` hit in every load context.
    """
    if name in sys.modules:
        return
    try:
        __import__(name)  # bin/ on sys.path: prod script / conftest / pytest
        return
    except ModuleNotFoundError:
        pass
    _p = os.path.join(os.path.dirname(__file__), f"{name}.py")
    _spec = _ilu.spec_from_file_location(name, _p)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules[name] = _mod
    _spec.loader.exec_module(_mod)


_ensure_sibling_loaded("_lib_forecast")
from _lib_forecast import _compute_forecast, ForecastInputs, ForecastOutput, BudgetRow
_ensure_sibling_loaded("_lib_dashboard_sources")
_ensure_sibling_loaded("_cctally_dashboard_sources")
from _cctally_dashboard_sources import (
    DashboardReadContext,
    _claude_accounts_wire,
    _refresh_budget_status_clock,
    accounts_identity_digest,
    build_codex_source_state_from_capture,
    build_codex_source_state,
    capture_codex_source_state,
    codex_decision_deadline_passed,
    refresh_codex_source_clock,
    resolve_dashboard_source_semantics,
)
# #496 S5b §4.7. Module level rather than lazy, unlike this module's other
# `_cctally_quota` uses: an `except` clause needs the name bound before the
# `try`, and `_cctally_dashboard_sources` above already imports that module at
# ITS module level, so this adds no edge to the import graph.
from _cctally_quota import QuotaProjectionIncomplete
from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    SourceDashboardWarning,
    aggregate_range,
    aggregate_scope_failed,
    aggregate_scope_identity,
    build_aggregate_scope,
    claude_stats_digest,
    codex_stats_digest,
    combined_accounting_version,
    compose_all_state,
    dashboard_resource_key,
    degrade_source_state,
    reuse_coherent_source_state,
    unavailable_source_state,
)


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. `load_config` and `get_claude_session_entries` STAY as
# shims even though their natural homes are decentralized (_cctally_config
# / _cctally_cache) — tests monkeypatch them via `ns["X"]` (21 sites total,
# audited 2026-05-17); direct imports would silently bypass the patches.
# See spec §3.5 (carve-out) and §3.7 (stays-on-shim allowlist).
def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def _resolve_display_tz_obj(*args, **kwargs):
    return sys.modules["cctally"]._resolve_display_tz_obj(*args, **kwargs)


def _apply_display_tz_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_display_tz_override(*args, **kwargs)


def _apply_midweek_reset_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_midweek_reset_override(*args, **kwargs)


def _resolve_forecast_now(*args, **kwargs):
    return sys.modules["cctally"]._resolve_forecast_now(*args, **kwargs)


def _fetch_current_week_snapshots(*args, **kwargs):
    return sys.modules["cctally"]._fetch_current_week_snapshots(*args, **kwargs)


def _load_forecast_inputs(*args, **kwargs):
    return sys.modules["cctally"]._load_forecast_inputs(*args, **kwargs)


def _sum_cost_for_range(*args, **kwargs):
    return sys.modules["cctally"]._sum_cost_for_range(*args, **kwargs)


def _sum_cost_and_tokens_for_range(*args, **kwargs):
    return sys.modules["cctally"]._sum_cost_and_tokens_for_range(*args, **kwargs)


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


def get_latest_cost_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_latest_cost_for_week(*args, **kwargs)


def get_milestones_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_milestones_for_week(*args, **kwargs)


def get_recent_weeks(*args, **kwargs):
    return sys.modules["cctally"].get_recent_weeks(*args, **kwargs)


def sync_cache(*args, **kwargs):
    return sys.modules["cctally"].sync_cache(*args, **kwargs)


def sync_codex_cache(*args, **kwargs):
    return sys.modules["cctally"].sync_codex_cache(*args, **kwargs)


# ``_compute_forecast`` + the forecast dataclasses (``ForecastInputs`` /
# ``ForecastOutput`` / ``BudgetRow``) now live in ``bin/_lib_forecast.py``
# (#279 S4 F2) and are honest-imported at module top — used as bare-name
# constructors inside ``_tui_build_forecast``. Single class def per name in
# ``_lib_forecast``; everything imports it, so class identity stays unique.


# Dashboard back-refs consumed by the TUI's snapshot builders.
# These functions/classes live in bin/_cctally_dashboard.py (Phase F #22),
# re-exported through bin/cctally so the shim resolves correctly.
def _dashboard_build_blocks_panel(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_blocks_panel(*args, **kwargs)


def _dashboard_build_blocks_view(*args, **kwargs):
    return sys.modules["cctally"]._dashboard_build_blocks_view(*args, **kwargs)


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
    # #556 S1 §3.3 — the current cycle's #104 token total, accumulated in the
    # SAME pass that produces `spent_usd` so the two halves describe one entry
    # set. Appended last with a default so fixture modules that construct
    # `TuiCurrentWeek` positionally stay valid.
    total_tokens: int = 0


# ---- View-model row dataclasses moved to bin/_lib_view_models.py ----
# Re-exported here so `from _cctally_tui import TuiTrendRow` and
# `ns["TuiTrendRow"]` direct-dict reads in tests keep resolving. The
# **extended** TuiTrendRow (spec §4.1: +10 nullable fields) is imported
# from the same module; the new fields default to None so existing TUI
# / dashboard fixtures that construct TuiTrendRow positionally stay
# byte-stable.
from _lib_view_models import (  # noqa: E402
    TuiTrendRow,
    WeeklyPeriodRow,
    MonthlyPeriodRow,
)


# BlocksPanelRow + DailyPanelRow + TuiSessionRow moved to
# bin/_lib_view_models.py — re-exported here so historical
# ``from _cctally_tui import BlocksPanelRow`` (or ``ns["BlocksPanelRow"]``
# direct-dict reads in tests) keep resolving.
from _lib_view_models import (  # noqa: E402
    BlocksPanelRow,
    DailyPanelRow,
    TuiSessionRow,
)


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


def _tui_build_five_hour_milestones(
    conn: sqlite3.Connection,
    five_hour_window_key: int | None,
) -> list[dict]:
    """Return per-percent 5h-block milestones for the given window, in
    capture-time order. Spec §5.3 — drives the CurrentWeekModal's new
    5h-milestone timeline section.

    Bucket B per §3.2: NO ``reset_event_id`` filter — both pre- and
    post-credit segments render in the merged chronological stream so
    the user sees the full history of the active block including
    repeated threshold values after an in-place credit. The React layer
    differentiates rows by ``reset_event_id`` for key uniqueness.

    Returns [] when the current week has no API-anchored 5h block. The
    envelope-shaped dict mirrors the CLI ``five-hour-breakdown --json``
    milestone objects but with snake_case keys (envelope convention).
    """
    if five_hour_window_key is None:
        return []
    rows = conn.execute(
        """
        SELECT percent_threshold, captured_at_utc, block_cost_usd,
               marginal_cost_usd, seven_day_pct_at_crossing,
               reset_event_id
          FROM five_hour_milestones
         WHERE five_hour_window_key = ?
         ORDER BY captured_at_utc ASC, id ASC
        """,
        (int(five_hour_window_key),),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({
            "percent_threshold": int(r["percent_threshold"]),
            "captured_at_utc":   r["captured_at_utc"],
            "block_cost_usd":    float(r["block_cost_usd"]),
            "marginal_cost_usd": (
                None if r["marginal_cost_usd"] is None
                else float(r["marginal_cost_usd"])
            ),
            "seven_day_pct_at_crossing": (
                None if r["seven_day_pct_at_crossing"] is None
                else float(r["seven_day_pct_at_crossing"])
            ),
            "reset_event_id": int(r["reset_event_id"] or 0),
        })
    return out


@dataclass(frozen=True)
class SyncFailureAttribution:
    """Database ownership retained at the dashboard leg catch boundary.

    The raw compatibility string remains on ``DataSnapshot.last_sync_error``;
    this compact sidecar carries only the facts needed for truthful,
    privacy-safe envelope classification.
    """

    leg: str
    database: str
    corruption: bool
    # #583 S2 §7. True only when the exception's PRIMARY SQLite code is
    # SQLITE_BUSY or SQLITE_LOCKED. Defaulted so every existing construction
    # site is unchanged and no current input reaches the new classifier
    # branch; `_tui_capture_sync_failure` is the one normal site that supplies
    # the real value, and it already receives the exception.
    sqlite_busy: bool = False


class _StatsSnapshotCorruption(Exception):
    """Internal control signal for one post-query stats heal attempt."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))


def _tui_attribute_corruption(
    conn: sqlite3.Connection,
    exc: Exception,
    *,
    database: str,
) -> tuple[str, bool]:
    """Classify corruption against the actual database at the catch site.

    Mixed stats/cache builders can surface the same SQLite message from either
    family. Only after a corruption-shaped exception do we run the expensive
    stats ``quick_check``: a failed/non-ok result positively attributes stats;
    an intact stats family leaves the failure attributed to cache. Explicit
    third-store ownership, including ``conversations``, is preserved without a
    stats probe. No path or exception-text parsing is used for database
    identity.
    """

    corruption = bool(_cctally()._is_sqlite_corruption_error(exc))
    attributed = database
    if database == "conversations":
        return database, corruption
    if corruption and database == "stats_or_cache":
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            stats_ok = len(rows) == 1 and tuple(rows[0]) == ("ok",)
            attributed = "cache" if stats_ok else "stats"
        except sqlite3.DatabaseError:
            attributed = "stats"
    return attributed, corruption


def _tui_capture_sync_failure(
    conn: sqlite3.Connection,
    errors: list[str],
    failures: list[SyncFailureAttribution],
    *,
    leg: str,
    database: str,
    exc: Exception,
    stats_heal_attempted: bool,
) -> None:
    """Record one attributed leg failure or request the single stats retry."""

    attributed_database, corruption = _tui_attribute_corruption(
        conn, exc, database=database
    )
    # #583 S2 §7. Mask the primary code out of the extended one: SQLite
    # reports extended codes such as SQLITE_BUSY_SNAPSHOT (517), which this
    # repository already contends with in its multi-writer cache and
    # conversations locking, and an unmasked comparison would miss the most
    # likely case. Numeric only — a string-only hop carrying "database is
    # locked" must not set the flag, because Preserve 10 forbids widening raw
    # text matching. `_is_sqlite_corruption_error` is the precedent for
    # reading the code and is untouched.
    code = getattr(exc, "sqlite_errorcode", None)
    sqlite_busy = bool(
        isinstance(code, int)
        and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )
    failure = SyncFailureAttribution(
        leg=leg,
        database=attributed_database,
        corruption=corruption,
        sqlite_busy=sqlite_busy,
    )
    if (
        failure.database == "stats"
        and failure.corruption
        and not stats_heal_attempted
    ):
        raise _StatsSnapshotCorruption(exc)
    failures.append(failure)
    errors.append(f"{leg}: {exc}")


def _tui_heal_post_query_stats(exc: Exception) -> bool:
    """Invoke the existing stats replacement engine after all handles close."""

    import _cctally_store

    heal = getattr(_cctally_store, "HEAL_HOOK", None)
    if heal is None:
        return False
    try:
        return bool(heal("stats", exc, post_query=True))
    except Exception as heal_exc:  # noqa: BLE001 — snapshot must still degrade
        eprint(f"[heal] dashboard stats auto-heal failed: {heal_exc}")
        return False


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
    sync_failures: tuple[SyncFailureAttribution, ...] = ()
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
    # ---- 5h in-place credit (v1.7.x) ----
    # Already-envelope-shaped dicts for the CurrentWeekModal's new 5h
    # milestone timeline (spec §5.3, Codex r1 finding 3). Parallel to
    # ``percent_milestones`` (which carries the WEEKLY timeline). Loaded
    # at sync-thread time so ``snapshot_to_envelope`` stays a pure
    # renderer; empty list when no current 5h block is bound.
    five_hour_milestones: list[dict] = field(default_factory=list)
    # ---- hero-modal historical milestones (spec §1a/§3) ----
    # Compact per-week navigation index for the CurrentWeekModal's week
    # chip (build_claude_week_index). Built ONLY on the non-idle rebuild
    # and stored here so ``snapshot_to_envelope`` stays a pure serializer;
    # the idle path carries it forward via ``dataclasses.replace``. Empty
    # list on first paint / when the DB read fails.
    week_index: list[dict] = field(default_factory=list)
    # ---- view-model unification (Bundle 1): pre-computed totals ----
    # Populated by the sync thread as sum-over-visible-rows over the
    # panel rows ``_dashboard_build_{daily,monthly,weekly}_periods``
    # returned (see ``_tui_build_snapshot``); the dashboard envelope
    # adapter emits these as ``<domain>.total_cost_usd`` /
    # ``total_tokens`` so the React panels stop running
    # ``rows.reduce(...)`` in JS. Sum-over-visible-rows is a structural
    # invariant: ``total === sum(rows[*].cost_usd)`` by construction —
    # see ``test_weekly_envelope_total_matches_sum_of_visible_rows``.
    # ``trend_avg_dollars_per_pct`` is sourced from ``build_trend_view``
    # (3-sample-rule mean per spec §4.3). Defaults preserve
    # compatibility with pre-Bundle-1 fixture modules that construct
    # ``DataSnapshot`` positionally. Spec §6.6.
    daily_total_cost_usd: float = 0.0
    daily_total_tokens: int = 0
    monthly_total_cost_usd: float = 0.0
    monthly_total_tokens: int = 0
    weekly_total_cost_usd: float = 0.0
    weekly_total_tokens: int = 0
    # Blocks domain (issue #56). ``BlocksPanelRow`` doesn't carry token
    # columns, so the cost total alone preserves the structural
    # ``total === sum(visible rows).cost_usd`` invariant; ``blocks_total_tokens``
    # is sourced from the same ``BlocksView`` build so both scalars
    # come from a single typed pass.
    blocks_total_cost_usd: float = 0.0
    blocks_total_tokens: int = 0
    trend_avg_dollars_per_pct: float | None = None
    # Trend modal median (issue #59). Sourced from
    # ``build_trend_view``'s ``median_dpp_non_current_4w`` field — the
    # last-4-non-current dpp median TrendModal.tsx used to compute
    # client-side. Populated by the sync thread off the 12-row history
    # build (NOT the 8-row panel build); the dashboard envelope adapter
    # emits this as ``trend.history_median_dpp``. ``None`` for fixture
    # modules that construct ``DataSnapshot`` positionally without
    # going through ``_tui_build_snapshot``; the React modal keeps a
    # client-side fallback for that case.
    trend_history_median_dpp: float | None = None
    # Forecast domain (issue #57). ``ForecastView`` wraps
    # ``ForecastOutput`` and surfaces the per-method projection /
    # verdict / header-routing / budget fields the dashboard envelope
    # adapter used to re-derive inline. Field is ``None`` for fixture
    # modules that construct ``DataSnapshot`` directly without going
    # through ``_tui_build_snapshot``; the envelope adapter falls
    # back to the legacy inline routing in that case.
    forecast_view: Any | None = None
    # Projects panel + modal envelope block (spec §5.2 /
    # 2026-05-19-projects-panel-design.md). Populated on the sync
    # thread by ``_build_projects_envelope`` (per-tick DB-touching
    # aggregation that runs alongside the existing per-panel builds);
    # the dashboard's pure ``snapshot_to_envelope`` reads this back
    # unchanged and assigns it to ``envelope["projects"]``. ``None``
    # on first tick before sync completes — the TS envelope mirror
    # declares ``ProjectsEnvelope | null`` and the client renders the
    # panel-empty state until the next tick replaces it.
    projects_envelope: dict | None = None
    # Cache-report panel + modal envelope block (spec
    # 2026-05-21-cache-report-panel-design.md §4.2). Populated on the
    # sync thread by ``build_cache_report_snapshot`` alongside the
    # existing projects build. The dashboard's
    # ``snapshot_to_envelope`` reads this back unchanged and assigns it
    # to ``envelope["cache_report"]``. ``None`` on first tick before
    # sync completes — the TS envelope mirror declares
    # ``CacheReportEnvelope | null`` and the client renders the
    # panel-empty state until the next tick replaces it.
    cache_report: Any | None = None
    # ---- #268 M4: doctor / config / update-state precompute (spec §6) ----
    # ``snapshot_to_envelope`` used to fork the `security` keychain subprocess
    # (via ``doctor_gather_state``) + read ``config.json`` + the update-state
    # files once PER SSE CLIENT PER TICK. These fields carry those reads,
    # precomputed ONCE per rebuild on the sync thread (doctor behind a
    # short-TTL memo), so the envelope stays a pure renderer — mirroring how
    # ``alerts`` / ``five_hour_milestones`` are already precomputed.
    #   * ``doctor_payload`` — the small severity/counts/fingerprint envelope
    #     block (``{severity, counts, generated_at, fingerprint}`` or a
    #     ``_error`` FAIL block). ``None`` on the first/empty snapshot and on
    #     fixtures constructed positionally → the envelope falls back to
    #     computing inline (existing behavior).
    #   * ``envelope_precompute`` — ``{config, update_state, update_suppress}``
    #     the envelope derives its ``display`` / ``alerts_settings`` / ``budget``
    #     / ``dashboard`` / ``update`` blocks from purely. ``None`` → the
    #     envelope falls back to reading ``config.json`` / the update-state
    #     files inline.
    # Both default at the END with ``None`` so positional fixture constructors
    # keep working, and both are carried forward on a sync crash (Codex F6).
    doctor_payload: dict | None = None
    envelope_precompute: dict | None = None
    # ---- #278 Theme A: first-paint hydration latch ----
    # ``True`` ONLY on data that is genuinely still being assembled — the
    # cheap first-paint seed (``_dashboard_initial_snapshot`` on a normal
    # launch) and A2's throttled partial republishes (set on a
    # ``dataclasses.replace`` copy for the PUBLISH only, never dirtying the
    # object the dispatch memo retains). Every path that yields
    # complete/stable data leaves/forces it ``False``: a fresh full build is
    # ``False`` for free (this default), and each ``dataclasses.replace``
    # clone site that copies a prior snapshot's value (idle short-circuit,
    # update-check republish, run-sync/settings republish) forces it back to
    # ``False``. Serialized into the dashboard envelope by
    # ``snapshot_to_envelope`` (additive key ``"hydrating"``); consumers
    # tolerate unknown keys, and it appears in NO ``--json``/CLI surface.
    # Placed LAST with a default so positional fixture constructors keep
    # working.
    hydrating: bool = False
    # ---- #583 S2: queue / activity state ----
    # Owned by ``_SnapshotRef`` and merged in at ``set()`` / mutation time.
    # Builders never populate it: A2 and the final publish both replace the
    # snapshot wholesale, so a request accepted mid-build would have its
    # counter overwritten by the older object the builder had assembled.
    # ``None`` means "no activity known yet"; the envelope renders that as an
    # idle object. Trailing default so positional fixture constructors keep
    # working; appears in NO ``--json``/CLI surface.
    sync_activity: dict | None = None
    # ---- #300: change-signal for the dashboard's lazy detail fetchers ----
    # A compact, deterministic string derived from the whole DB dispatch
    # signature (``_snapshot_data_version(dispatch_sig)``): it changes iff ANY
    # DB input the detail endpoints read changed (session entries, weekly
    # usage/cost, reset events, codex entries, cache generation), and stays flat
    # on an idle tick (idle ⇔ signature unchanged). Empty string when no
    # dispatch signature was computed — the TUI / non-precompute path, which
    # never consumes it. Serialized into the dashboard envelope by
    # ``snapshot_to_envelope`` as ``"data_version"`` so the browser's
    # session-modal / projects-drill / conversation-outline fetchers revalidate
    # on an actual DATA-CHANGE signal instead of the 5s ``generated_at``
    # heartbeat (#300). The idle short-circuit carries it forward via
    # ``dataclasses.replace`` (idle ⇒ signature unchanged ⇒ same value).
    # Trailing default so positional fixture constructors keep working; appears
    # in NO ``--json``/CLI surface.
    data_version: str = ""
    # Dashboard-only #294 S4 provider bundle.  The terminal TUI's normal data
    # path deliberately leaves this ``None`` and never touches Codex dashboard
    # ingest/read-model work.  It remains trailing/defaulted for every legacy
    # positional fixture constructor.
    source_bundle: SourceDashboardBundle | None = None

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
            week_avg_projection_pct=used_pct + r_avg * remaining_hours,
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
    # #556 S1 §3.3: one walk yields both halves. The range is whatever
    # `spent_usd` already used — taken AFTER `_apply_midweek_reset_override`
    # above, so a mid-week reset shortens the accumulation and the published
    # period together.
    spent, total_tokens = _sum_cost_and_tokens_for_range(
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
        total_tokens=total_tokens,
    )


def _tui_build_forecast(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    use_weekref_cost_cache: bool = False,
):
    """Build the TUI/dashboard sync-thread forecast.

    Issue #57: routes through ``build_forecast_view`` (the kernel-pattern
    wrapper) and unwraps to a ``ForecastOutput`` for backward-compat with
    every existing ``snap.forecast`` consumer (TUI panels, envelope
    adapter, share builder). Use ``_tui_build_forecast_view`` when the
    full view is needed (e.g. ``snap.forecast_view`` population).
    """
    view = _tui_build_forecast_view(
        conn, now_utc, skip_sync=skip_sync,
        use_weekref_cost_cache=use_weekref_cost_cache,
    )
    return view.output if view is not None else None


def _tui_build_forecast_view(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    use_weekref_cost_cache: bool = False,
):
    """Build the ``ForecastView`` (issue #57). Returns ``None`` only on
    error in callers — the empty-state View is constructed by the
    builder itself with ``output=None`` + ``verdict="LOW CONF"``.

    ``use_weekref_cost_cache`` (#269 §4) threads into the trailing-4-week
    fallback so the dashboard sync thread serves closed prior weeks from the
    shared per-weekref cost cache; default off keeps CLI byte-identical.
    """
    c = _cctally()
    return c.build_forecast_view(
        conn, now_utc=now_utc, targets=(100, 90), skip_sync=skip_sync,
        use_weekref_cost_cache=use_weekref_cost_cache,
    )


def _tui_build_trend(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    count: int = 8,
    display_tz: "ZoneInfo | None" = None,
) -> list[TuiTrendRow]:
    """Build the last `count` trend rows, chronological (oldest first).

    Bundle 1 / Task 10: wraps the unified ``build_trend_view`` kernel
    (spec §5.4) — the loop body that used to live here moved into
    ``bin/_lib_view_models.build_trend_view``. The TUI snapshot module
    consumes the first 7 ``TuiTrendRow`` fields and ignores the 10
    extended fields (which exist for cmd_report's JSON contract).

    ``skip_sync`` threads into ``build_trend_view`` so the reset-event
    live-cost path reads the cache without a JSONL ingest. The share
    period-override handler (``_share_apply_period_override``) passes
    ``skip_sync=True`` on an HTTP thread that must not glob (#268).
    """
    c = _cctally()
    view = c.build_trend_view(conn, now_utc=now_utc, n=max(1, count),
                               display_tz=display_tz, skip_sync=skip_sync)
    return list(view.rows)


def _tui_build_weekly_history(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    count: int = 12,
    display_tz: "ZoneInfo | None" = None,
    use_weekref_cost_cache: bool = False,
) -> list[TuiTrendRow]:
    """Return the last `count` weeks for the Trend modal (spec §4.6.3).

    Same data shape as `_tui_build_trend` (the panel) — just more rows.
    The panel renders 8; the modal renders up to 12. Wrapping rather
    than parameterising the call site keeps the snapshot fields
    semantically distinct (panel data vs. modal data) and avoids
    accidental cross-contamination.

    Issue #59: list-returning shim kept for back-compat with the
    public re-export at ``bin/cctally:13871``. New callers (the sync
    thread populating ``snap.weekly_history`` + the modal-median
    scalar) should prefer ``_tui_build_weekly_history_view`` so they
    pick up the pre-computed ``median_dpp_non_current_4w`` scalar
    without re-deriving.
    """
    return list(
        _tui_build_weekly_history_view(
            conn, now_utc, skip_sync=skip_sync, count=count,
            display_tz=display_tz,
            use_weekref_cost_cache=use_weekref_cost_cache,
        ).rows
    )


def _tui_build_weekly_history_view(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    count: int = 12,
    display_tz: "ZoneInfo | None" = None,
    use_weekref_cost_cache: bool = False,
):
    """Build the full ``TrendView`` for the dashboard Trend modal
    (issue #59).

    Wraps ``build_trend_view`` with the 12-row default the modal
    consumes. The returned ``TrendView`` carries the
    ``median_dpp_non_current_4w`` pre-computed scalar so the sync
    thread can populate ``DataSnapshot.trend_history_median_dpp``
    without re-running the median derivation client-side. The 8-row
    panel call (``_tui_build_trend``) goes through the same
    ``build_trend_view`` kernel; both builds carry their own median
    field but only the 12-row build's value reaches the envelope
    (``trend.history_median_dpp``).
    """
    c = _cctally()
    return c.build_trend_view(
        conn, now_utc=now_utc, n=max(1, count), display_tz=display_tz,
        skip_sync=skip_sync, use_weekref_cost_cache=use_weekref_cost_cache,
    )


# #268 Group B session-cache master switch. Normally True: the sync-thread
# rebuild serves the sessions pane from the module-level ``SessionCache``,
# re-aggregating only the sessions touched since the last tick. Flip to False
# to force the from-scratch 365-day fetch (the pre-#268 behavior) — the parity
# tests toggle it to prove the cached and from-scratch rebuilds are
# byte-identical, and the builder falls back to it automatically if the cache
# DB can't be opened.
_SESSION_CACHE_ENABLED = True


def _tui_build_sessions(
    now_utc: dt.datetime,
    *,
    limit: int = 100,
    skip_sync: bool = False,
    use_session_cache: bool = False,
    with_titles: bool = False,
) -> list[TuiSessionRow]:
    """Load the last `limit` Claude sessions (merged across resumes).

    Started-time descending (matches `_aggregate_claude_sessions` —
    sorted by `last_activity` DESC). Uses the same aggregator as the
    `session` subcommand, so row identity and project labels match
    `cctally session --json` exactly.

    When `skip_sync=True`, honors the parent's `--no-sync` intent: no
    ingest pass, just read whatever is already cached.

    Bundle 2 / Task 15: wraps the unified ``build_sessions_view``
    kernel — the prior 40-line inline-derivation body now lives at
    ``bin/_lib_view_models.build_sessions_view``. The TUI keeps
    ownership of the bounded 365-day scan window (rationale below) and
    consumes ``view.rows`` (the typed ``TuiSessionRow`` tuple). The
    view's parallel ``view.aggregated`` is reserved for the CLI / share
    surfaces; the TUI doesn't need ``ClaudeSessionUsage`` fields.

    ``use_session_cache`` (#268 Group B): when True AND
    ``_SESSION_CACHE_ENABLED``, serve the sessions from the module-level
    ``SessionCache`` — re-aggregate ONLY the sessions changed since the last
    tick, then sort+truncate the FULL cached set (so a session below the top
    100 can still promote once it gets new activity, Codex F5). Set True ONLY
    by the sync-thread rebuild (``_tui_build_snapshot``), which runs on a
    process-consistent ``now``. Every OTHER caller keeps the default
    ``False`` → the from-scratch 365-day fetch, so a non-sync-thread caller
    with a shifted ``now`` can NEVER pollute the shared cache (the Bundle 2
    Group A lesson). The visible rows are byte-identical either way.

    ``with_titles``: attach each row's transcript-derived title from the
    independent conversation store. DASHBOARD-only — ``TuiSessionRow.title`` is
    read by the dashboard envelope alone (the terminal TUI never renders it), so
    the default keeps the core/TUI build free of any transcript-store access
    (#320). The read itself is bounded and fail-soft; see
    ``read_session_titles_bounded``.
    """
    # Bounded scan window — the sessions pane promises "last `limit`". A
    # 365-day scan covers virtually all users (even one-session-every-few-days
    # sparseness still nets the cap). Bounded rather than all-history so
    # sync-tick cost stays predictable on heavy DBs: the aggregator runs
    # on every entry in the window before slicing.
    range_start = now_utc - dt.timedelta(days=365)
    c = _cctally()
    aggregated_override = None
    if use_session_cache and _SESSION_CACHE_ENABLED:
        try:
            aggregated_override = _tui_sessions_cached(
                now_utc, range_start, limit, skip_sync,
            )
        except (sqlite3.DatabaseError, OSError):
            # Cache DB unavailable / read failure — fall back to the
            # from-scratch fetch so the pane still renders.
            aggregated_override = None
    if aggregated_override is None:
        entries = get_claude_session_entries(range_start, now_utc, skip_sync=skip_sync)
        view = c.build_sessions_view(
            entries, now_utc=now_utc, limit=limit, display_tz=None,
        )
    else:
        view = c.build_sessions_view(
            (), now_utc=now_utc, limit=limit, display_tz=None,
            aggregated_override=aggregated_override,
        )
    rows = list(view.rows)
    if not with_titles:
        # #320: transcript-derived titles are optional decoration, and the TUI
        # has no consumer for them (the field is dashboard-only), so the core
        # build never touches the independent transcript store at all.
        return rows
    # Dashboard build (see ``with_titles`` in the docstring): re-attach the
    # Session-column titles the #320 store split dropped. ``read_session_titles_bounded``
    # never uses ``open_conversations_db`` and never waits out a lock — a store
    # that is missing, locked, or mid-rebuild yields no titles and the panel
    # renders its em-dash fallback, which self-heals on a later tick. Titles are
    # stashed unconditionally on this server-internal row; the privacy gate is
    # applied later, at envelope serialization
    # (``snapshot_to_envelope(transcripts_visible=...)``).
    session_ids = [r.session_id for r in rows if r.session_id]
    if not session_ids:
        return rows
    titles = c._load_sibling("_cctally_cache").read_session_titles_bounded(
        session_ids,
    )
    if not titles:
        return rows
    return [
        dataclasses.replace(r, title=titles[r.session_id])
        if r.session_id in titles else r
        for r in rows
    ]


def _tui_sessions_cached(
    now_utc: dt.datetime,
    range_start: dt.datetime,
    limit: int,
    skip_sync: bool,
) -> "list":
    """Assemble the full session set from the module-level ``SessionCache`` (#268).

    Cold tick: aggregate every session in ``[range_start, now]`` and populate
    the cache. Warm tick: re-aggregate ONLY the sessions touched since the
    last tick — each from its OWN full in-window entry set (a straddling /
    resumed session re-aggregates whole, no split-row). Returns the FULL
    cached set, window-filtered to ``[range_start, now]`` and sorted by
    ``last_activity`` desc; ``build_sessions_view`` then truncates to the view
    limit, which is what preserves correct eviction/**promotion** at the
    100-row boundary (Codex F5).

    Raises ``sqlite3.DatabaseError`` / ``OSError`` on a cache-open failure so
    ``_tui_build_sessions`` can fall back to the from-scratch path.
    """
    c = _cctally()
    sc = c._load_sibling("_lib_snapshot_cache")
    cache_conn = c.open_cache_db()
    try:
        def _aggregate_all():
            entries = get_claude_session_entries(
                range_start, now_utc, skip_sync=skip_sync,
            )
            return _aggregate_claude_sessions(entries)

        def _reaggregate(last_seen, _affected):
            entries = _fetch_affected_session_entries(
                cache_conn, last_seen, range_start, now_utc,
            )
            return _aggregate_claude_sessions(entries)

        # Identity resolution here is the resolved ``session_id`` (via the
        # session_files join, filename-stem fallback when null). A late
        # null→session_id backfill on an existing path (the schema migration
        # that added the column shipped long ago) would re-key an already-cached
        # session; that is DELIBERATELY NOT handled per-tick (a session_files
        # rescan every tick would tax the exact hot path #268 optimizes, and
        # reachability is near-zero because files are named ``<sessionId>.jsonl``
        # so the stem == the sessionId in the overwhelming majority). Escape
        # hatches: a dashboard restart, the M5.2 orphan-prune generation bump, or
        # ``reset_session_cache_state()`` all re-cold-start the cache.
        full = sc.build_cached_sessions(
            cache_conn=cache_conn,
            aggregate_all=_aggregate_all,
            reaggregate=_reaggregate,
        )
    finally:
        try:
            cache_conn.close()
        except sqlite3.Error:
            pass
    # #268 M5-additional (a): DROP aged-out sessions from the STORE, not just
    # from the returned view. Under a sliding `now`, a session whose
    # last_activity has fallen before range_start is out of [range_start, now]
    # — the from-scratch fetch wouldn't return it either — so evict it from the
    # module cache. Without this the store retains every session ever
    # cold-populated and grows unboundedly over long dashboard uptime. No-op on
    # a pinned `now` (every cold-populated session's last_activity is >=
    # range_start by construction), and cheap (a scalar partition over a few
    # thousand aggregates, not a 365-day entry re-scan).
    aged_out = {s.session_id for s in full if s.last_activity < range_start}
    if aged_out:
        sc.session_cache().drop(aged_out)
    in_window = [s for s in full if s.last_activity >= range_start]
    in_window.sort(key=lambda s: s.last_activity, reverse=True)
    return in_window


def _fetch_affected_session_entries(
    cache_conn: "sqlite3.Connection",
    last_seen_seq: int,
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> "list":
    """Fetch (timestamp-ASC) every entry of every session touched by a row
    CHANGED since ``last_seen_seq`` — expanded across ``session_id`` siblings so
    a resumed session re-aggregates WHOLE — within ``[range_start, range_end]``.

    The column list + ``LEFT JOIN`` mirror ``get_claude_session_entries``
    EXACTLY (including the materialized ``speed`` column → ``usage_extra``), so
    the per-session aggregate is byte-identical to the from-scratch pass. The
    affected-source-paths set is inlined as a SQL subquery (parameterized only
    by ``last_seen_seq``) so the fetch stays a single timestamp-ordered result —
    no Python ``IN`` list to chunk, and stable first-seen model order.

    #270 (§7d, Codex-2c): the two affected-path subqueries key on
    ``mutation_seq > ?``, NOT ``id > ?`` — so an id-stable in-place finalization
    of an EXISTING session's row (which leaves ``MAX(id)`` flat) still selects
    that session's sibling paths and it re-aggregates WHOLE. On a pure-insert
    interval ``{mutation_seq > last}`` == ``{id > last}``, so byte-identical.
    """
    c = _cctally()
    _JoinedClaudeEntry = c._JoinedClaudeEntry
    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()
    sql = (
        "SELECT se.timestamp_utc, se.model, se.input_tokens, se.output_tokens, "
        "  se.cache_create_tokens, se.cache_read_tokens, se.source_path, "
        "  sf.session_id, sf.project_path, se.cost_usd_raw, se.speed, "
        "  se.cache_create_1h_tokens "
        "FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE se.timestamp_utc >= ? AND se.timestamp_utc <= ? "
        "  AND se.source_path IN ("
        # Sibling paths of every session_id touched by a changed row ...
        "    SELECT sf2.path FROM session_files sf2 WHERE sf2.session_id IN ("
        "      SELECT sf1.session_id FROM session_files sf1 "
        "      JOIN (SELECT DISTINCT source_path FROM session_entries "
        "            WHERE mutation_seq > ?) af "
        "        ON af.source_path = sf1.path "
        "      WHERE sf1.session_id IS NOT NULL"
        "    )"
        # ... UNION the touched files themselves (covers fallback / not-yet-
        # backfilled session_files rows keyed by filename stem).
        "    UNION "
        "    SELECT DISTINCT source_path FROM session_entries WHERE mutation_seq > ?"
        "  ) "
        "ORDER BY se.timestamp_utc ASC"
    )
    rows = cache_conn.execute(
        sql, (start_iso, end_iso, last_seen_seq, last_seen_seq),
    ).fetchall()
    return [
        _JoinedClaudeEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            model=row[1],
            input_tokens=row[2],
            output_tokens=row[3],
            cache_creation_tokens=row[4],
            cache_read_tokens=row[5],
            source_path=row[6],
            session_id=row[7],
            project_path=row[8],
            cost_usd=row[9],
            usage_extra=({"speed": row[10]} if row[10] is not None else None),
            cache_1h_tokens=row[11],   # #195
        )
        for row in rows
    ]


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


def _tui_build_session_detail_indexed(
    session_id: str,
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> Any | None:
    """Indexed direct lookup for one session by id.

    Walks ``session_files`` (indexed by ``session_id`` — migration
    ``idx_session_files_session_id``) for the 1-3 source_paths the
    session lives in, then fetches ONLY the entries from those paths
    in the supplied range. Aggregates the filtered list and returns
    the single matching ``ClaudeSessionUsage`` row.

    Returns ``None`` on three indistinguishable misses (the caller's
    fallback path handles them all):

    1. session_files row hasn't been backfilled yet for this id
       (CLAUDE.md "session_files is populated lazily" — first run
       after deploy).
    2. cache DB unavailable (open / lock contention).
    3. session_id is genuinely unknown.

    Falling back uniformly preserves correctness without distinguishing
    the cases; if the slow path also misses, the modal renders 404.
    """
    c = _cctally()
    open_cache_db = c.open_cache_db
    _JoinedClaudeEntry = c._JoinedClaudeEntry
    try:
        conn = open_cache_db()
    except (sqlite3.DatabaseError, OSError):
        return None
    try:
        # 1) Source paths for this session id (indexed lookup).
        rows = conn.execute(
            "SELECT path FROM session_files WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        if not rows:
            return None
        paths = [r[0] for r in rows]
        # 2) Entries restricted to those paths in the range. Typical
        # path-count is 1-3 (resume across files), well below SQLite's
        # 999 parameter cap.
        start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
        end_iso = range_end.astimezone(dt.timezone.utc).isoformat()
        placeholders = ",".join("?" * len(paths))
        cur = conn.execute(
            f"SELECT se.timestamp_utc, se.model, "
            f"  se.input_tokens, se.output_tokens, "
            f"  se.cache_create_tokens, se.cache_read_tokens, "
            f"  se.source_path, sf.session_id, sf.project_path, "
            f"  se.cost_usd_raw, se.speed, se.cache_create_1h_tokens "
            f"FROM session_entries se "
            f"LEFT JOIN session_files sf ON sf.path = se.source_path "
            f"WHERE se.timestamp_utc >= ? AND se.timestamp_utc <= ? "
            f"  AND se.source_path IN ({placeholders}) "
            f"ORDER BY se.timestamp_utc ASC",
            [start_iso, end_iso, *paths],
        )
        entries = [
            _JoinedClaudeEntry(
                timestamp=dt.datetime.fromisoformat(row[0]),
                model=row[1],
                input_tokens=row[2],
                output_tokens=row[3],
                cache_creation_tokens=row[4],
                cache_read_tokens=row[5],
                source_path=row[6],
                session_id=row[7],
                project_path=row[8],
                cost_usd=row[9],
                usage_extra=({"speed": row[10]} if row[10] is not None else None),
                cache_1h_tokens=row[11],   # #195
            )
            for row in cur
        ]
        if not entries:
            return None
        sessions = _aggregate_claude_sessions(entries)
        for s in sessions:
            if s.session_id == session_id:
                return s
        return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _tui_build_session_detail(
    session_id: str,
    *,
    now_utc: dt.datetime | None = None,
) -> TuiSessionDetail | None:
    """Look up one session by ID; return None if not found.

    Fast path: ``_tui_build_session_detail_indexed`` reads
    ``session_files`` by id, scopes the entries SELECT to the matching
    source_paths, and aggregates only those rows — turning the lookup
    from "build every session in 365 days" into an indexed direct
    fetch (~3000× fewer rows on real DBs).

    Slow-path fallback: when the indexed lookup misses (session_files
    not yet backfilled, cache unavailable, or genuinely unknown), the
    legacy bulk-fetch + linear scan still runs so the modal renders
    consistently with the panel's session list during the lazy-
    backfill window.
    """
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    range_start = now_utc - dt.timedelta(days=365)
    match: Any | None = _tui_build_session_detail_indexed(
        session_id, range_start, now_utc,
    )
    if match is None:
        # Fall back to the bulk-aggregate path. Same shape as before,
        # used only when the index lookup couldn't conclude.
        entries = get_claude_session_entries(
            range_start, now_utc, skip_sync=True,
        )
        sessions = _aggregate_claude_sessions(entries)
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


def _snapshot_data_version(sig) -> str:
    """#300 — compact, deterministic change-signal string from the DB dispatch
    signature (``_lib_snapshot_cache.SnapshotSignature``).

    Changes iff any DB leg the dashboard's detail endpoints read changed
    (session entries + their id-stable mutation counter, weekly usage/cost,
    reset events, codex entries, cache generation); stays flat on an idle tick
    (idle ⇔ signature unchanged). Returns ``""`` when no signature was computed
    (the non-precompute / TUI path, which never consumes it) — the browser's
    ``revalToken`` then falls back to ``generated_at``. Every leg is an int
    (``reset_sig`` is a 2-int tuple), so a ``"."``-join is process-stable (no
    hash-seed dependence, unlike ``hash()``)."""
    if sig is None:
        return ""
    rs = getattr(sig, "reset_sig", None) or (0, 0)
    numeric_legs = ".".join(str(int(x)) for x in (
        sig.max_entry_id, sig.entry_mutation_seq, sig.max_wus_id,
        sig.max_wcs_id, rs[0], rs[1], sig.max_codex_id, sig.generation,
        getattr(sig, "codex_physical_mutation_seq", 0),
    ))
    digest = getattr(sig, "codex_stats_digest", "")
    out = numeric_legs if not digest else f"{numeric_legs}.{digest}"
    # #341 finding 9: fold the account registry/active-identity digest so an
    # account switch with zero new rows still flips the SSE change-signal. Empty
    # for every <=1-account install (byte-neutral — never appended).
    acct = getattr(sig, "accounts_digest", "")
    out = out if not acct else f"{out}.a{acct}"
    # public #5: a budgeted tick can change the Codex ingest backlog while every
    # other leg stays flat, and the envelope publishes that backlog. Folding it
    # in is what leaves the idle short-circuit so the source bundle is rebuilt
    # at all. Empty once the backlog has drained, so it is byte-neutral there.
    backlog = getattr(sig, "codex_ingest_backlog_sig", "")
    out = out if not backlog else f"{out}.b{backlog}"
    # #556 S3 §2.9: the Claude alert relations. A fired or armed Claude alert
    # moves no numeric leg above, so without this the detail endpoints' change
    # signal stays flat across a tick that added an alert row.
    claude_digest = getattr(sig, "claude_stats_digest", "")
    return out if not claude_digest else f"{out}.x{claude_digest}"


def _tui_source_copy(value: object) -> object:
    """Copy only JSON-shaped legacy envelope values into a source state."""
    if isinstance(value, dict):
        return {str(key): _tui_source_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_tui_source_copy(item) for item in value]
    return value


def _tui_claude_resource_row(
    row: object,
    *,
    resource: str,
    identity: object,
    remove: tuple[str, ...] = (),
) -> dict[str, object]:
    """Attach S4's opaque owner key while dropping legacy raw identities."""
    raw = row if isinstance(row, dict) else {}
    wire = {
        str(name): _tui_source_copy(value)
        for name, value in raw.items()
        if name not in {"key", *remove}
    }
    wire["key"] = dashboard_resource_key(resource, "claude", identity)
    wire["source"] = "claude"
    return wire


def alert_row_owner(
    axis: object, vendor: object, metric: object,
) -> str:
    """Total ownership classifier for a legacy alert row (#556 S3 §3.4).

    Raises on an unregistered axis, so adding a seventh axis without deciding
    its owner fails a test instead of shipping a row invisible everywhere. The
    predicate this replaced answered `False` for an unknown axis, which reads
    as "Codex owns it" and is indistinguishable from a real Codex row.
    """
    if axis in {"weekly", "five_hour", "budget", "project_budget"}:
        # An absent vendor is the established Claude meaning: the legacy rows
        # predate the additive vendor field. An explicit non-Claude vendor is
        # never relabelled — `project_budget` gained that check here, having
        # previously claimed every row whatever its vendor said.
        return "codex" if vendor == "codex" else "claude"
    if axis == "projected":
        # The metric is the owner here, and it is enumerated rather than
        # defaulted. Defaulting an unrecognized metric to Claude would let a
        # future Codex-side projected metric render in the Claude tab, and
        # defaulting it to Codex would drop it from every surface without a
        # word — the two failure modes this classifier exists to prevent.
        if metric in {"weekly_pct", "budget_usd"}:
            return "claude"
        if metric == "codex_budget_usd":
            return "codex"
        raise ValueError(f"no ownership rule for projected metric {metric!r}")
    if axis == "codex_budget":
        return "codex"
    raise ValueError(f"no ownership rule for alert axis {axis!r}")


def _tui_project_claude_source_data(legacy_envelope: object) -> dict[str, object]:
    """Project one completed Claude legacy envelope without further DB reads.

    The legacy dashboard snapshot is still the authoritative Claude model.
    This adapter only places its already-rendered values under the S4 source
    contract, replacing native route identities with opaque provider-qualified
    resource keys.  It deliberately never opens a connection or invokes a
    loader, so the source bundle remains part of the one coordinated read.
    """
    legacy = legacy_envelope if isinstance(legacy_envelope, dict) else {}
    daily = legacy.get("daily") if isinstance(legacy.get("daily"), dict) else {}
    monthly = legacy.get("monthly") if isinstance(legacy.get("monthly"), dict) else {}
    weekly = legacy.get("weekly") if isinstance(legacy.get("weekly"), dict) else {}
    sessions = legacy.get("sessions") if isinstance(legacy.get("sessions"), dict) else {}
    projects = legacy.get("projects") if isinstance(legacy.get("projects"), dict) else {}
    blocks = legacy.get("blocks") if isinstance(legacy.get("blocks"), dict) else {}
    current_week = legacy.get("current_week") if isinstance(legacy.get("current_week"), dict) else {}

    session_rows: list[dict[str, object]] = []
    for ordinal, row in enumerate(sessions.get("rows", ()) or ()):
        raw = row if isinstance(row, dict) else {}
        native_id = raw.get("session_id")
        identity = native_id if isinstance(native_id, str) and native_id else (
            ordinal, raw.get("started_utc"),
        )
        session_rows.append(_tui_claude_resource_row(
            raw,
            resource="session",
            identity=identity,
            remove=("session_id", "project_key"),
        ))

    current_project = projects.get("current_week")
    current_project = current_project if isinstance(current_project, dict) else {}
    trend_project = projects.get("trend")
    trend_project = trend_project if isinstance(trend_project, dict) else {}

    def project_rows(rows: object) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for ordinal, row in enumerate(rows if isinstance(rows, (list, tuple)) else ()):
            raw = row if isinstance(row, dict) else {}
            legacy_key = raw.get("key")
            identity = legacy_key if isinstance(legacy_key, str) and legacy_key else ordinal
            result.append(_tui_claude_resource_row(
                raw,
                resource="project",
                identity=identity,
                remove=("bucket_path",),
            ))
        return result

    current_project_rows = project_rows(current_project.get("rows", ()))
    trend_project_rows = project_rows(trend_project.get("projects", ()))
    project_source = {
        "current_week": {
            str(name): _tui_source_copy(value)
            for name, value in current_project.items() if name != "rows"
        } | {"rows": current_project_rows},
        "trend": {
            str(name): _tui_source_copy(value)
            for name, value in trend_project.items() if name != "projects"
        } | {"projects": trend_project_rows},
        # Route lookup is a flat source resource collection; the existing
        # dashboard panel shapes remain above unchanged except for identity.
        "rows": current_project_rows or trend_project_rows,
    }

    block_rows: list[dict[str, object]] = []
    for ordinal, row in enumerate(blocks.get("rows", ()) or ()):
        raw = row if isinstance(row, dict) else {}
        start_at = raw.get("start_at")
        identity = start_at if isinstance(start_at, str) and start_at else ordinal
        block_rows.append(_tui_claude_resource_row(
            raw, resource="block", identity=identity,
        ))

    def milestone_rows(rows: object, kind: str) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for ordinal, row in enumerate(rows if isinstance(rows, (list, tuple)) else ()):
            raw = row if isinstance(row, dict) else {}
            result.append(_tui_claude_resource_row(
                raw,
                resource=kind,
                identity=(ordinal, raw.get("percent"), raw.get("crossed_at_utc")),
            ))
        return result

    weekly_milestones = milestone_rows(current_week.get("milestones", ()), "quota_milestone")
    five_hour_milestones = milestone_rows(
        current_week.get("five_hour_milestones", ()), "quota_milestone",
    )
    quota_current = {
        str(name): _tui_source_copy(value)
        for name, value in current_week.items()
        if name not in {"milestones", "five_hour_milestones"}
    }

    alert_rows: list[dict[str, object]] = []
    for ordinal, row in enumerate(legacy.get("alerts", ()) or ()):
        raw = row if isinstance(row, dict) else {}
        axis = raw.get("axis")
        vendor = raw.get("vendor")
        metric = raw.get("metric")
        if alert_row_owner(axis, vendor, metric) != "claude":
            continue
        alert_rows.append(_tui_claude_resource_row(
            raw,
            resource="alert",
            identity=(ordinal, raw.get("axis"), raw.get("threshold"), raw.get("alerted_at")),
        ))

    budget_settings = _tui_source_copy(legacy.get("alerts_settings"))
    if isinstance(budget_settings, dict):
        # The legacy top-level settings mirror contains Codex capability flags
        # for its combined/dashboard compatibility surface.  They are not
        # Claude-provider facts and would make a reused Claude state stale
        # after a Codex-only budget/config change.
        for key in (
            "codex_budget_configured",
            "codex_budget_alerts_enabled",
            "codex_projected_enabled",
        ):
            budget_settings.pop(key, None)
    return {
        "hero": {
            # #556 S1 §3.4: current-cycle accounting, NOT the thirty-day
            # rollup. Both providers' `hero.cost_usd` / `hero.total_tokens`
            # now mean the same thing; the thirty-day figures keep their own
            # home in `periods.daily`. `None` when no current week resolves —
            # composition distinguishes an empty provider from an unresolved
            # cycle by the provider's availability, and a zero here would make
            # both read as observed spend.
            "cost_usd": current_week.get("spent_usd"),
            "total_tokens": current_week.get("total_tokens"),
            "header": _tui_source_copy(legacy.get("header")),
            "current_week": _tui_source_copy(current_week),
            "forecast": _tui_source_copy(legacy.get("forecast")),
            "trend": _tui_source_copy(legacy.get("trend")),
        },
        "periods": {
            "daily": _tui_source_copy(daily),
            "monthly": _tui_source_copy(monthly),
            "weekly": _tui_source_copy(weekly),
        },
        "sessions": {
            **{
                str(name): _tui_source_copy(value)
                for name, value in sessions.items() if name != "rows"
            },
            "rows": session_rows,
        },
        "projects": project_source,
        "quota": {
            "current_week": quota_current,
            "blocks": block_rows,
            "milestones": weekly_milestones,
            "five_hour_milestones": five_hour_milestones,
        },
        "budget": {
            "forecast": _tui_source_copy(legacy.get("forecast")),
            "settings": budget_settings,
        },
        "alerts": {"rows": alert_rows},
    }


def _tui_claude_cycle_is_resolved(
    current_week: object, now_utc: dt.datetime,
) -> bool:
    """Whether Claude's current week resolves AND has not expired (#556 §4.1).

    A stale percent observation does not enter this: the boundary can be
    perfectly resolved while the number that reports progress against it is an
    hour old.
    """
    if not isinstance(current_week, dict):
        return False
    end = _tui_normalized_period_instant(current_week.get("reset_at_utc"))
    if end is None:
        return False
    return now_utc.astimezone(dt.timezone.utc) < dt.datetime.fromisoformat(end)


def _tui_claude_domain_freshness(
    source_data: dict[str, object] | None,
    *,
    now_utc: dt.datetime,
) -> dict[str, str]:
    """Derive Claude's axes from its selected weekly snapshot evidence.

    #556 S1 §4.1 repoints the two axes it publishes. ``quota`` carries the
    percent-OBSERVATION age, which is what it always described. ``hero`` now
    carries current-cycle ACCOUNTING resolvability: fresh while the boundary
    resolves and has not expired, so the backward-looking counters inside it
    are publishable. Pointing ``hero`` at the percent age is what kept All's
    combined caveat permanently on, because that clock's 90-second bound is
    forty times tighter than the Codex weekly one it was joined with.

    The legacy current-week label has a third presentation-only ``aging``
    state. The source contract deliberately keeps the frozen fresh/stale
    vocabulary: only the exact stale label moves the quota axis.
    """
    data = source_data if isinstance(source_data, dict) else {}
    hero = data.get("hero")
    current_week = hero.get("current_week") if isinstance(hero, dict) else None
    freshness = (
        current_week.get("freshness")
        if isinstance(current_week, dict) else None
    )
    quota = (
        "stale"
        if isinstance(freshness, dict) and freshness.get("label") == "stale"
        else "fresh"
    )
    accounting = (
        "fresh" if _tui_claude_cycle_is_resolved(current_week, now_utc) else "stale"
    )
    return {"hero": accounting, "quota": quota, "sessions": "fresh"}


def _refresh_claude_budget_clock(
    state: SourceDashboardState,
    *,
    now_utc: dt.datetime,
) -> SourceDashboardState:
    """Re-run the pure pace kernel over Claude's frozen budget facts.

    #556 S5 §3.7. Separate from ``_refresh_claude_source_clock`` because the two
    are called from different places: that one runs ONLY on the pure-idle short
    circuit and needs the legacy ``current_week`` object and the raw config,
    neither of which the source-bundle builder holds. This one needs only the
    published status and the server-private cost events, so it can be called
    unconditionally after every build, reuse and degrade branch — which is what
    §3.7 requires, because exact-version Claude reuse returns the prior object
    unchanged and would otherwise republish a budget frozen at the instant it
    was built.

    Same-instant identity is preserved: the underlying kernel is deterministic
    in ``now``, so a freshly built state reclocks to itself and the equality
    guards below hand the caller the exact object it passed in.
    """
    if state.source != "claude" or not isinstance(state.data, Mapping):
        return state
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    now_utc = now_utc.astimezone(dt.timezone.utc)
    budget_domain = state.data.get("budget")
    if not isinstance(budget_domain, Mapping):
        return state
    status = budget_domain.get("status")
    if not isinstance(status, Mapping):
        return state
    refreshed = _refresh_budget_status_clock(
        status,
        now_utc,
        cost_events=(
            state.clock_data.get("claude_budget_cost_events", ())
            if isinstance(state.clock_data, Mapping) else ()
        ),
    )
    if refreshed is None or refreshed == status:
        return state
    data = dict(state.data)
    data["budget"] = {**dict(budget_domain), "status": refreshed}
    refreshed_state = dataclasses.replace(state, data=data)
    return state if refreshed_state == state else refreshed_state


def _refresh_claude_source_clock(
    state: SourceDashboardState,
    *,
    current_week: object,
    now_utc: dt.datetime,
    raw_config: dict[str, object],
) -> SourceDashboardState:
    """Advance Claude's weekly axes from frozen snapshot evidence only."""
    if state.source != "claude":
        return state
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    domain_freshness = dict(state.domain_freshness or {})
    # #556 S1 §4.1: the two axes advance from DIFFERENT evidence. Both legs run
    # on every tick — this used to write one percent-derived label to both, and
    # it also returned early on a missing capture, which left the accounting
    # axis frozen at its build-time value for the whole idle stretch.
    week_end = getattr(current_week, "week_end_at", None)
    domain_freshness["hero"] = (
        "fresh"
        if isinstance(week_end, dt.datetime)
        and now_utc.astimezone(dt.timezone.utc) < (
            week_end if week_end.tzinfo is not None
            else week_end.replace(tzinfo=dt.timezone.utc)
        ).astimezone(dt.timezone.utc)
        else "stale"
    )
    captured = getattr(current_week, "latest_snapshot_at", None)
    if isinstance(captured, dt.datetime):
        if captured.tzinfo is None or captured.utcoffset() is None:
            captured = captured.replace(tzinfo=dt.timezone.utc)
        age_seconds = max(
            0.0,
            (
                now_utc.astimezone(dt.timezone.utc)
                - captured.astimezone(dt.timezone.utc)
            ).total_seconds(),
        )
        try:
            freshness_config = _get_oauth_usage_config(raw_config)
        except Exception:
            freshness_config = _OAUTH_USAGE_DEFAULTS
        domain_freshness["quota"] = (
            "stale"
            if _freshness_label(age_seconds, freshness_config) == "stale"
            else "fresh"
        )
    refreshed = dataclasses.replace(
        state,
        domain_freshness=domain_freshness,
    )
    # #556 S5 §3.7: the budget leg runs on the pure-idle path too. It is the
    # SAME helper the bundle builder calls after every other branch, so the two
    # paths cannot drift.
    refreshed = _refresh_claude_budget_clock(refreshed, now_utc=now_utc)
    return state if refreshed == state else refreshed


def _tui_normalized_period_instant(value: object) -> str | None:
    """One canonical UTC spelling for a published cycle bound (#556 S1 §3.6).

    Legacy rows carry local-offset spellings and current rows carry `Z`, so the
    raw text is not an identity: hashing it would rebuild the Claude source on
    a spelling change that moved no boundary. Returns ``None`` for anything
    that does not parse as an instant.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso_datetime(value, "period bound").astimezone(
            dt.timezone.utc).isoformat()
    except ValueError:
        return None


def _tui_claude_period_identity(claude_data: object) -> str:
    """The effective current-cycle bounds, as a version fragment (§3.6).

    `claude_version` carried the database signatures, the semantics identity
    and the accounts digest but no period, and `reuse_coherent_source_state`
    returns the exact prior object on an unchanged version — so a week that
    rolled over with no database movement could be republished as current.
    The snapshot-level `_snapshot_period_rolled_over` gate already covers the
    undecorated case; this is the second layer, so provider reuse cannot
    outlive the cycle even if that gate is later changed.

    The bounds are read from the SAME resolved object the aggregation used
    (`current_week`, stored after `_apply_midweek_reset_override`), so a stale
    identity can never accompany a shortened range.

    The literal `"none"` degradation is deliberate and is NOT an oversight. A
    caller that holds no `claude_data` — the capture and QA paths — contributes
    a constant fragment, so layer two adds no rollover protection there. Those
    paths are still covered by the snapshot-level `_snapshot_period_rolled_over`
    gate, which is layer one and is what production reuse actually runs behind.
    """
    data = claude_data if isinstance(claude_data, dict) else {}
    hero = data.get("hero")
    current_week = hero.get("current_week") if isinstance(hero, dict) else None
    if not isinstance(current_week, dict):
        return "none"
    start = _tui_normalized_period_instant(current_week.get("week_start_at"))
    end = _tui_normalized_period_instant(current_week.get("reset_at_utc"))
    if start is None and end is None:
        return "none"
    return f"{start or ''}~{end or ''}"


def _tui_resolve_account_scope(stats_conn, provider: str) -> dict[str, object] | None:
    """Read one provider's authoritative REAL account count (#556 S1 §3.8).

    Returns ``None`` when the count cannot be read. That is the FAIL-CLOSED
    path: composition withholds the combined figure rather than assuming a
    single account. It is deliberately the opposite of the builders' existing
    swallow-and-degrade behaviour for the accounts WIRE, where the fallback
    loses decoration styling; here it would publish a wrong number.

    The catch is narrow on purpose. ``real_account_count`` is one SELECT over
    ``accounts`` — no file I/O, no label lookups — so ``sqlite3.Error`` covers
    every failure the CALL can produce, and anything else is a real defect that
    must not be swallowed into a silent withholding. ``ImportError`` is caught
    alongside it because the deferred import is a different failure class from
    the query: an unimportable module is exactly the "count cannot be read"
    state this function exists to report, and letting it propagate would fail
    the whole dashboard tick instead of withholding one figure.
    """
    try:
        import _cctally_account
        return {
            "real_account_count": int(
                _cctally_account.real_account_count(stats_conn, provider)
            ),
        }
    except (ImportError, sqlite3.Error):
        return None


def _tui_with_account_scope(
    state: SourceDashboardState, scope: dict[str, object] | None,
) -> SourceDashboardState:
    """Attach ``scope`` without breaking reuse-by-identity.

    ``reuse_coherent_source_state`` returns the EXACT prior object, and callers
    assert on that identity, so an unconditional ``dataclasses.replace`` would
    defeat reuse on every tick. Frozen mappings compare equal to plain dicts,
    so an unchanged count returns the prior object untouched.
    """
    if state.account_scope == scope:
        return state
    return dataclasses.replace(state, account_scope=scope)


_AGGREGATE_FOLD_FAILED = {"state": "failed", "code": "claude_fold_failed"}


@dataclass(frozen=True)
class _ClaudeAggregateCapture:
    optimized: object | None = None
    rows: tuple | None = None
    read_failed: bool = False


def _tui_capture_claude_aggregates(
    cache_conn,
    *,
    shared_start: dt.datetime,
    shared_end_exclusive: dt.datetime,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    # NOT `legacy_project_labels`: that is the name of the public kernel
    # function this receives the RESULT of (`c.legacy_project_labels`), and a
    # parameter shadowing it inside a function that also calls it reads as a
    # recursive reference.
    legacy_labels: "dict[str, str] | None" = None,
    max_entry_id: "int | None" = None,
    entry_mutation_seq: "int | None" = None,
    generation: int = 0,
):
    """Capture the one-snapshot Claude inputs without folding them."""
    from zoneinfo import ZoneInfo

    c = _cctally()
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    dashboard_module = sys.modules["_cctally_dashboard"]
    cache_seams_unpatched = (
        c.build_project_aggregate_rows
        is dashboard_module.build_project_aggregate_rows
        and c.build_daily_aggregate_rows
        is dashboard_module.build_daily_aggregate_rows
    )
    if legacy_labels is not None and cache_seams_unpatched:
        try:
            return _ClaudeAggregateCapture(
                optimized=c.capture_cached_claude_range_aggregates(
                    cache_conn,
                    shared_start=shared_start,
                    shared_end_exclusive=shared_end_exclusive,
                    display_tz=display_tz,
                    max_entry_id=max_entry_id,
                    entry_mutation_seq=entry_mutation_seq,
                    generation=generation,
                ),
            )
        except Exception:
            _lib_log.get_logger("dashboard").error(
                "claude range aggregate capture failed", exc_info=True,
            )
    try:
        rows = tuple(c.iter_shared_range_entries(
            cache_conn, start=shared_start,
            end_exclusive=shared_end_exclusive,
        ))
    except Exception:
        _lib_log.get_logger("dashboard").error(
            "claude shared-range candidate read failed", exc_info=True,
        )
        return _ClaudeAggregateCapture(read_failed=True)
    return _ClaudeAggregateCapture(rows=rows)


def _tui_build_claude_aggregates_from_capture(
    capture: _ClaudeAggregateCapture,
    *,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    legacy_labels: "dict[str, str] | None" = None,
):
    """Build both All-only Claude legs after the cache pin closes.

    Returns ``(payload, outcomes)`` where ``payload`` holds the published rows
    for whichever legs succeeded and ``outcomes`` names each leg's state.

    The capture came from the caller's pinned connection beside the Codex read;
    this half consumes only that immutable carrier after rollback. Both legacy
    paths stay untouched: the attached-cache block continues to serve
    ``env.projects`` and the Group-A read continues to serve ``env.daily`` for
    the Claude tab.

    Each fold has its OWN error boundary, so one failure cannot take the bundle
    or the other leg down. A failure of the shared read itself fails both, since
    neither leg has rows. A failure is a typed withheld outcome rather than an
    escaped exception: today an exception inside this helper is caught by the
    outer handler, which publishes the prior bundle or none at all, so a
    cold-start fold failure could never become the outcome §3.7 promises.
    """
    c = _cctally()
    from zoneinfo import ZoneInfo
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    payload: dict[str, object] = {}
    outcomes: dict[str, object] = {
        "projects": {"state": "ok"}, "daily": {"state": "ok"},
    }
    if capture.read_failed:
        return {}, {
            "projects": dict(_AGGREGATE_FOLD_FAILED),
            "daily": dict(_AGGREGATE_FOLD_FAILED),
        }
    if capture.optimized is not None:
        try:
            return c.build_cached_claude_range_aggregates_from_capture(
                capture.optimized,
                now_utc=now_utc,
                display_tz=display_tz,
                legacy_labels=legacy_labels,
                tolerate_leg_failures=True,
            )
        except Exception:
            # No second cache generation is opened for a fallback after the
            # pin closes. A failed optimized fold is therefore withheld and
            # retried on the next authoritative build rather than rebuilt from
            # newer cache bytes under the old version.
            _lib_log.get_logger("dashboard").error(
                "claude range aggregate cache failed", exc_info=True,
            )
            return {}, {
                "projects": dict(_AGGREGATE_FOLD_FAILED),
                "daily": dict(_AGGREGATE_FOLD_FAILED),
            }
    rows = capture.rows or ()
    if capture.rows is None:
        return {}, {
            "projects": dict(_AGGREGATE_FOLD_FAILED),
            "daily": dict(_AGGREGATE_FOLD_FAILED),
        }
    prepared_daily_entries = None
    if legacy_labels is None:
        # No projects envelope was built this tick, so the routable population
        # is unknown. Publishing anyway would relabel every row from the
        # bounded population, mint different opaque keys, and hand them to a
        # drill-down that resolves against an envelope it rebuilds for itself
        # — the rows on screen and the rows the route can serve would be two
        # different populations, and nothing would say so. Withholding states
        # the failure instead, and `claude_fold_failed` also disqualifies the
        # bundle from idle reuse, so the next tick's envelope gets a chance.
        _lib_log.get_logger("dashboard").error(
            "claude range projects fold has no projects envelope",
        )
        outcomes["projects"] = dict(_AGGREGATE_FOLD_FAILED)
    else:
        candidate_daily_entries = []
        try:
            payload["projects"] = c.build_project_aggregate_rows(
                rows,
                legacy_labels=legacy_labels,
                prepared_daily_entries=candidate_daily_entries,
            )
            prepared_daily_entries = candidate_daily_entries
        except Exception:
            _lib_log.get_logger("dashboard").error(
                "claude range projects fold failed", exc_info=True,
            )
            outcomes["projects"] = dict(_AGGREGATE_FOLD_FAILED)
    try:
        payload["daily"] = [
            c.daily_panel_row_to_wire(row)
            for row in c.build_daily_aggregate_rows(
                rows,
                now_utc=now_utc,
                display_tz=display_tz,
                prepared_entries=prepared_daily_entries,
            )
        ]
    except Exception:
        _lib_log.get_logger("dashboard").error(
            "claude range daily fold failed", exc_info=True,
        )
        outcomes["daily"] = dict(_AGGREGATE_FOLD_FAILED)
    return payload, outcomes


def _tui_build_claude_aggregates(
    cache_conn,
    *,
    shared_start: dt.datetime,
    shared_end_exclusive: dt.datetime,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    legacy_labels: "dict[str, str] | None" = None,
    max_entry_id: "int | None" = None,
    entry_mutation_seq: "int | None" = None,
    generation: int = 0,
):
    """Compatibility wrapper for callers that do not split the cache pin."""
    capture = _tui_capture_claude_aggregates(
        cache_conn,
        shared_start=shared_start,
        shared_end_exclusive=shared_end_exclusive,
        now_utc=now_utc,
        display_tz_name=display_tz_name,
        legacy_labels=legacy_labels,
        max_entry_id=max_entry_id,
        entry_mutation_seq=entry_mutation_seq,
        generation=generation,
    )
    return _tui_build_claude_aggregates_from_capture(
        capture,
        now_utc=now_utc,
        display_tz_name=display_tz_name,
        legacy_labels=legacy_labels,
    )


def _tui_claude_data_with_aggregates(
    claude_data: dict[str, object] | None,
    payload: dict[str, object],
    *,
    fallback: dict[str, object],
) -> dict[str, object]:
    """Attach the rows-only siblings without mutating the caller's dict.

    ``providers.claude.projects.aggregate`` and
    ``providers.claude.periods.daily_aggregate`` are rows and nothing else — no
    range, no outcome. Those live once, on the All source.
    """
    base = dict(claude_data) if claude_data is not None else dict(fallback)
    if "projects" in payload:
        projects = dict(base.get("projects") or {})
        projects["aggregate"] = {"rows": payload["projects"]}
        base["projects"] = projects
    if "daily" in payload:
        periods = dict(base.get("periods") or {})
        periods["daily_aggregate"] = {"rows": payload["daily"]}
        base["periods"] = periods
    return base


# #556 S5 §3.5 — the five dispositions, encoded on the wire. `status` published
# means CONFIGURED AND COMPUTED. Everything else is an optional sibling that is
# omitted when inapplicable, so the ordinary no-budget payload is byte-identical
# to what shipped before this session:
#
#   provider_budget_unset  no key at all (the default the client assumes)
#   account_budgets_only   `not_configured.disposition`
#   period_unresolved      `status_unavailable.code`
#   budget_compute_failed  `status_unavailable.code`
#
# The unavailable shape follows S1's `combined_unavailable` — {code, message,
# provider} — so one client reader handles both.
_CLAUDE_BUDGET_UNAVAILABLE_MESSAGES = {
    "period_unresolved": (
        "Claude's budget period could not be resolved, so no budget status "
        "is published."
    ),
    "budget_compute_failed": (
        "Claude's budget status could not be computed."
    ),
}


def _tui_claude_budget_period(claude_budget: "Mapping[str, object] | None") -> str:
    """The configured Claude budget period, defaulting to ``subscription-week``.

    One reader for the capability record and the published status, so the two
    can never name different periods.
    """
    config = claude_budget if isinstance(claude_budget, Mapping) else {}
    return str(config.get("period") or "subscription-week")


def _tui_claude_budget_window_identity(
    claude_budget: "Mapping[str, object] | None",
    *,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    week_start_name: str,
) -> str:
    """The configured CALENDAR budget window's end, as a version fragment.

    #556 S5 Unit 1 review R5, kept as DEFENCE IN DEPTH — not a fix for a
    shipped user-visible defect. `claude_version` carries
    `_tui_claude_period_identity`, which reads the SUBSCRIPTION-WEEK bounds and
    therefore tracks that period's boundary and no other. On a `calendar-week`
    or `calendar-month` budget the fragment that already moved was S2's
    aggregate-range fragment, and that one moves at DISPLAY-TIMEZONE midnight:
    `resolve_shared_range` (`bin/_cctally_dashboard.py`) floors the earliest
    day to midnight in the resolved display zone, and production passes ONE
    resolved zone object to both legs — `_tui_build_snapshot` sets
    `source_display_tz_name` from `_build_display_tz` and hands the same object
    to `_tui_common_source_range_start`, while `_resolve_display_tz_obj` always
    returns a `ZoneInfo`. A `calendar-week` or `calendar-month` boundary falls
    at local midnight, so the aggregate fragment already moved with it.

    The Unit 2 review corrected the Unit 1 claim that this reproduced a
    production defect: the reproduction went red only because the test helper
    paired `display_tz_name="America/New_York"` with a UTC range start, which
    is a configuration production cannot construct. The fragment stays because
    it makes the budget window's own boundary the thing that invalidates the
    budget's own generation, rather than leaving that to a neighbouring
    fragment that happens to move at the same instant.

    Returns `""` for `subscription-week`, which `_tui_claude_period_identity`
    already covers from the same bounds, and for an unconfigured budget — so an
    install with no budget produces a byte-identical version string. Calendar
    resolution is PURE (no database), which is what lets it run before the reuse
    decision on every tick.

    A resolution failure contributes `""` rather than raising: the same failure
    reaches `_tui_claude_budget_domain`, which names `budget_compute_failed`.
    """
    config = claude_budget if isinstance(claude_budget, Mapping) else {}
    if config.get("weekly_usd") is None:
        return ""
    period = _tui_claude_budget_period(config)
    if period == "subscription-week":
        return ""
    try:
        window = _tui_claude_budget_window(
            None,
            period=period,
            now_utc=now_utc,
            display_tz_name=display_tz_name,
            week_start_name=week_start_name,
        )
    except Exception:
        return ""
    if window is None:
        return ""
    return window[1].isoformat()


def _tui_claude_budget_unavailable(
    code: str,
    *,
    budget_usd: float | None = None,
    period: str | None = None,
) -> dict[str, object]:
    """The `{code, message, provider}` sibling, plus what the user configured.

    #556 S5 Unit 2 review F5. §4.6 requires `period_unresolved` to render "the
    configured amount with the window named as unresolved", and the client had
    no amount to render: this payload carried the code, the message and the
    provider, so the block printed the bare code and nothing else. Both codes
    are reached ONLY from a configured budget, so both carry the configured
    amount and period; they are additive and omitted when the caller has
    nothing to state, which keeps every other consumer's shape unchanged.
    """
    payload: dict[str, object] = {
        "code": code,
        "message": _CLAUDE_BUDGET_UNAVAILABLE_MESSAGES[code],
        "provider": "claude",
    }
    if budget_usd is not None:
        try:
            payload["budget_usd"] = float(budget_usd)
        except (TypeError, ValueError):
            # The `budget_compute_failed` caller runs INSIDE the only exception
            # boundary the Claude build has, so a second raise here would take
            # the whole bundle down. Omitting the amount degrades this one line.
            pass
    if period is not None:
        payload["period"] = period
    return payload


def _tui_claude_budget_window(
    stats_conn,
    *,
    period: str,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    week_start_name: str,
):
    """Resolve the Claude budget window, or ``None`` when it cannot be resolved.

    #556 S5 §3.1: the IMPURE resolvers stay here and are injected into the pure
    kernel. Subscription-week resolution reads ``weekly_usage_snapshots`` (and
    returns ``None`` before the first snapshot lands, which the CLI reports as
    ``status: "no_data"``); calendar resolution goes through the same DST-correct
    `_resolve_calendar_window` the Codex side already uses, including its
    per-instant `display.tz = local` path.
    """
    from zoneinfo import ZoneInfo

    c = _cctally()
    forecast = c._load_sibling("_cctally_forecast")
    if period == "subscription-week":
        window = forecast._resolve_current_budget_window(stats_conn, now_utc)
        if window is None:
            return None
        start_at, end_at = window
    else:
        tz = ZoneInfo(display_tz_name) if display_tz_name else None
        start_at, end_at = forecast._resolve_calendar_window(
            period, now_utc, {"collector": {"week_start": week_start_name}}, tz,
        )
    return (
        start_at.astimezone(dt.timezone.utc),
        end_at.astimezone(dt.timezone.utc),
    )


def _tui_claude_budget_cost_events(
    cache_conn, *, start_at: dt.datetime, end_at: dt.datetime,
) -> tuple[tuple[dt.datetime, float], ...]:
    """Freeze every configured-window Claude cost event for idle pace updates.

    #556 S5 §3.7: trailing-24h spend CANNOT be derived from the status
    aggregate — `_refresh_budget_status_clock` iterates individual events — so
    the same per-event carrier Codex keeps in server-private `clock_data` is
    built here for Claude. It runs on the caller's PINNED cache connection and
    reuses `iter_shared_range_entries` plus `_shared_range_row_to_usage_entry`,
    which route through the `claude_usage_dict` chokepoint and therefore price
    the 1-hour cache-write portion correctly (#195).
    """
    c = _cctally()
    rows = tuple(c.iter_shared_range_entries(
        cache_conn, start=start_at, end_exclusive=end_at,
    ))
    return _tui_claude_budget_events_from_rows(rows)


def _tui_claude_budget_events_from_rows(
    rows: "Iterable[tuple]",
) -> tuple[tuple[dt.datetime, float], ...]:
    """Price already-captured Claude rows without touching cache.db."""
    c = _cctally()
    events: list[tuple[dt.datetime, float]] = []
    for row in rows:
        entry = c._shared_range_row_to_usage_entry(row)
        timestamp = entry.timestamp
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
        events.append((
            timestamp.astimezone(dt.timezone.utc),
            c._calculate_entry_cost(
                entry.model, entry.usage, mode="auto", cost_usd=entry.cost_usd,
            ),
        ))
    return tuple(events)


@dataclass(frozen=True)
class _ClaudeBudgetCapture:
    config: object
    target: object
    period: str | None
    window: tuple[dt.datetime, dt.datetime] | None = None
    rows: tuple = ()
    immediate_overlay: object | None = None


def _tui_capture_claude_budget_domain(
    cache_conn,
    stats_conn,
    *,
    claude_budget: "Mapping[str, object] | None",
    now_utc: dt.datetime,
    display_tz_name: str | None,
    week_start_name: str,
) -> _ClaudeBudgetCapture:
    """Capture the configured budget window's raw rows under the cache pin."""
    config = claude_budget if isinstance(claude_budget, Mapping) else {}
    target = config.get("weekly_usd")
    if target is None:
        overlay = (
            {"not_configured": {"disposition": "account_budgets_only"}}
            if config.get("accounts") else {}
        )
        return _ClaudeBudgetCapture(
            config=config, target=target, period=None,
            immediate_overlay=overlay,
        )
    period = _tui_claude_budget_period(config)
    try:
        window = _tui_claude_budget_window(
            stats_conn,
            period=period,
            now_utc=now_utc,
            display_tz_name=display_tz_name,
            week_start_name=week_start_name,
        )
        if window is None:
            return _ClaudeBudgetCapture(
                config=config, target=target, period=period,
                immediate_overlay={
                    "status_unavailable": _tui_claude_budget_unavailable(
                        "period_unresolved", budget_usd=target, period=period),
                },
            )
        start_at, end_at = window
        rows = tuple(_cctally().iter_shared_range_entries(
            cache_conn, start=start_at, end_exclusive=end_at,
        ))
        return _ClaudeBudgetCapture(
            config=config, target=target, period=period,
            window=(start_at, end_at), rows=rows,
        )
    except Exception:
        _lib_log.get_logger("dashboard").error(
            "claude budget inputs could not be captured", exc_info=True,
        )
        return _ClaudeBudgetCapture(
            config=config, target=target, period=period,
            immediate_overlay={
                "status_unavailable": _tui_claude_budget_unavailable(
                    "budget_compute_failed", budget_usd=target, period=period),
            },
        )


def _tui_build_claude_budget_domain_from_capture(
    capture: _ClaudeBudgetCapture,
    *,
    now_utc: dt.datetime,
) -> tuple[dict[str, object], tuple[tuple[dt.datetime, float], ...]]:
    """Price and interpret a captured Claude budget after rollback."""
    if capture.immediate_overlay is not None:
        return dict(capture.immediate_overlay), ()
    assert capture.window is not None and capture.period is not None
    config = capture.config
    target = capture.target
    period = capture.period
    start_at, end_at = capture.window
    try:
        c = _cctally()
        events = _tui_claude_budget_events_from_rows(capture.rows)
        recent_start = max(start_at, now_utc - dt.timedelta(hours=24))
        status = c.budget_status_payload(
            period=period,
            window_start_at=start_at,
            window_end_at=end_at,
            target_usd=target,
            spent_usd=stable_sum(
                cost for timestamp, cost in events
                if start_at <= timestamp < now_utc
            ),
            recent_24h_usd=stable_sum(
                cost for timestamp, cost in events
                if recent_start <= timestamp < now_utc
            ),
            now=now_utc,
            alert_thresholds=config["alert_thresholds"],
        )
    except Exception:
        _lib_log.get_logger("dashboard").error(
            "claude budget status could not be computed", exc_info=True,
        )
        return {
            "status_unavailable": _tui_claude_budget_unavailable(
                "budget_compute_failed", budget_usd=target, period=period),
        }, ()
    reclock_floor = now_utc - dt.timedelta(hours=24)
    retained = tuple(
        (timestamp, cost) for timestamp, cost in events
        if timestamp >= reclock_floor
    )
    return {"status": status}, retained


def _tui_claude_budget_domain(
    cache_conn,
    stats_conn,
    *,
    claude_budget: "Mapping[str, object] | None",
    now_utc: dt.datetime,
    display_tz_name: str | None,
    week_start_name: str,
) -> tuple[dict[str, object], tuple[tuple[dt.datetime, float], ...]]:
    """Return ``(budget_domain_overlay, cost_events)`` for the Claude provider.

    The status is VENDOR-WIDE (spec §3.4). A populated `budget.accounts` is NOT
    consumed to simulate per-account scoping: Claude publishes no
    `account_scopes` for such a map to describe, so an account-only
    configuration is its own disposition rather than a fabricated status.

    Every failure is caught HERE. The Claude build has no error boundary of its
    own — unlike the Codex build at `_tui_build_source_bundle`'s
    `source_build_failed` handler — so an escaping budget error would take the
    whole bundle down, not merely the provider.
    """
    capture = _tui_capture_claude_budget_domain(
        cache_conn,
        stats_conn,
        claude_budget=claude_budget,
        now_utc=now_utc,
        display_tz_name=display_tz_name,
        week_start_name=week_start_name,
    )
    return _tui_build_claude_budget_domain_from_capture(
        capture, now_utc=now_utc,
    )


def _tui_claude_data_with_budget(
    base: dict[str, object], overlay: dict[str, object],
) -> dict[str, object]:
    """Merge the budget overlay without mutating the caller's dict.

    An empty overlay returns the base unchanged, which is what keeps the
    `provider_budget_unset` payload byte-identical.
    """
    if not overlay:
        return base
    merged = dict(base)
    merged["budget"] = {**(merged.get("budget") or {}), **overlay}
    return merged


def _tui_note_codex_regime(value: str) -> None:
    """Stamp the REALISED Codex source-leg decision on the open tick (§1.5).

    Read from what the leg actually did, not from ``CodexIngestStats.
    rows_changed``. ``_write_codex_file_batch`` can leave ``rows_changed == 0``
    while unconditionally advancing ``codex_physical_mutation_seq`` after a
    quota, thread, root, cursor or metadata write; that sequence reaches
    ``codex_version`` through ``compute_signature``, ``reuse_coherent_source_
    state`` requires exact version equality, and the mismatch therefore drives
    a genuinely expensive Codex rebuild that ``rows_changed`` would stamp as
    idle. The repository already reached this conclusion for the #313 F4
    reconcile gate, in two comments in ``bin/_cctally_cache.py``.

    Aggregated over the refresh by ``TickContext.set_codex_regime``: several
    builds can run inside one refresh and disagree, and last-write
    classification would move an expensive tick into the idle population.
    """
    tick = _tick_stats.current()
    if tick is not None:
        tick.set_codex_regime(value)


def _tui_build_source_bundle(
    *,
    stats_conn,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    codex_ingest_contended: bool,
    codex_ingest_failed: bool = False,
    claude_ingest_contended: bool = False,
    claude_ingest_failed: bool = False,
    claude_cost_usd: float,
    claude_total_tokens: int,
    claude_data: dict[str, object] | None = None,
    common_range_start: dt.datetime | None = None,
    projects_envelope: dict | None = None,
    prior_bundle: SourceDashboardBundle | None = None,
    raw_config: dict[str, object] | None = None,
) -> SourceDashboardBundle:
    """Build one frozen source bundle after the dashboard's coordinated ingest.

    This helper is reachable only from ``precompute_envelope=True``.  It opens
    a fresh cache handle after the ingest handle has closed, then performs
    read-only provider adaptation with no implicit sync or rollout fallback.
    """
    c = _cctally()
    cache_conn = c.open_cache_db()
    cache_read_tx = False
    pin_started_ns: "int | None" = None
    codex_scope_cm = None
    codex_path_scope_value = None

    def _record_cache_pin() -> None:
        """Stamp the elapsed hold exactly once, on whichever path ends it.

        Called immediately before BOTH rollbacks — the normal one after the
        bundle is composed, and the `finally` one that runs when the build
        raises. A build that crashed still held the pin for however long it
        ran, and omitting that would bias the published figure toward the
        cheap ticks. `pin_started_ns` is cleared here so the second call on
        the exception path, where both rollbacks are reachable, is a no-op.
        """
        nonlocal pin_started_ns
        if pin_started_ns is None:
            return
        elapsed = time.monotonic_ns() - pin_started_ns
        pin_started_ns = None
        tick = _tick_stats.current()
        if tick is not None:
            tick.mark_cache_pin(elapsed)

    try:
        # Keep cache.db on one stable snapshot, but leave stats.db in
        # statement-scoped autocommit.  A dashboard source build can spend
        # seconds folding provider rows; holding one stats read transaction
        # across that CPU work pins every intervening WAL frame and defeats
        # SQLite's default 1,000-page autocheckpoint (#393).  The before/after
        # composite signature below is already the cross-database consistency
        # gate: any stats generation movement rejects this build, so a long
        # stats snapshot buys no correctness and creates unbounded WAL growth.
        if not cache_conn.in_transaction:
            cache_conn.execute("BEGIN")
            cache_read_tx = True
            # #583 S5 §2.4 / acceptance criterion 16: the hold is stamped at
            # the BEGIN and ROLLBACK boundaries themselves. This function's
            # cumulative duration also counts the work before BEGIN and after
            # ROLLBACK, so it is an upper bound on the hold rather than the
            # hold, and no document may quote it as one.
            pin_started_ns = time.monotonic_ns()
        if common_range_start is None:
            # Resolved through the SAME helper the callers use, with no daily
            # panel. A bare `now_utc - 30 days` here is a microsecond-precise
            # instant that advances on every tick, and the resolved start is
            # folded into both providers' version material at exactly the
            # granularity `compose_all_aggregates` compares it — so a start
            # that moves within a display day makes an unchanged provider's
            # retained carrier disagree with a rebuilt one's, and both
            # aggregates are then withheld as `retained_range_mismatch`
            # permanently. Both production callers pass a resolved start, so
            # this is the last producer that could reintroduce that shape.
            #
            # The zone lookup is guarded because this branch exists to be a
            # SAFE fallback. An unresolvable `display_tz_name` raising out of
            # it would take down the whole source build over the one path whose
            # purpose is to keep going, so an unusable name degrades to UTC —
            # which is what `resolve_shared_range` already does for `None`.
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            _fallback_tz = None
            if display_tz_name:
                try:
                    _fallback_tz = ZoneInfo(display_tz_name)
                except (ZoneInfoNotFoundError, ValueError, OSError):
                    _fallback_tz = None
            common_range_start, _fallback_end = c.resolve_shared_range(
                None,
                now_utc=now_utc,
                display_tz=_fallback_tz,
            )
        if common_range_start.tzinfo is None or common_range_start.utcoffset() is None:
            raise ValueError("common_range_start must be timezone-aware")
        common_range_start = common_range_start.astimezone(dt.timezone.utc)
        # #556 S2 §3.2: ONE interval, resolved once and passed immutably to
        # both Claude folds and to the Codex read. The exclusive upper bound is
        # what the Codex projects read already applies, so the Codex tab stays
        # byte-stable. The PUBLISHED `end_at` is `now_utc` itself.
        shared_end_exclusive = now_utc.astimezone(
            dt.timezone.utc,
        ) + dt.timedelta(microseconds=1)
        published_range = aggregate_range(
            common_range_start.isoformat(),
            now_utc.astimezone(dt.timezone.utc).isoformat(),
        )
        semantics = resolve_dashboard_source_semantics(
            raw_config if raw_config is not None else c.load_config(),
            display_tz_name=display_tz_name,
        )
        # `codex_stats_digest`'s relation table reads `quota_projection_state`
        # and `quota_window_blocks` from a pure kernel that may not import
        # `_cctally_quota`, so its callers gate it (#496 S5b section 4.7).
        #
        # This ONE call covers the second `codex_stats_digest` below as well,
        # and the reason is not that the gate is a property of the connection:
        # this connection's statement-scoped autocommit reads may see mixed
        # generations, which is exactly what `stats_generation_moved` exists to
        # catch, so the post-build read CAN see a generation this gate never
        # checked. It is safe because that digest is never rendered — it is only
        # compared against this one. A generation carrying an incomplete
        # projection produces a different digest, `stats_generation_moved` is
        # then true, and the build returns the prior bundle or raises. An EQUAL
        # digest means the relations did not move, so the post-build read saw
        # the generation this gate did check.
        from _cctally_quota import assert_projection_readable
        assert_projection_readable(stats_conn)
        stats_digest = codex_stats_digest(stats_conn)
        # #556 S3 §2.9: the Claude alert relations. Nothing else in the
        # signature moves when a Claude alert fires or is armed, so without
        # this the idle path can keep serving a prior bundle that predates the
        # alert.
        claude_digest = claude_stats_digest(stats_conn)
        # #341 finding 9: the account registry/active-identity digest. Empty for
        # every <=1-account install (byte-neutral — appended only when non-empty),
        # so single-account source versions stay byte-identical to today; a
        # multi-account switch flips it -> the source rebuilds (re-resolving the
        # `active` marker). Folded into BOTH providers' versions: a switch is a
        # rare event and a rebuild is safe, so per-provider narrowing is not worth
        # the split.
        accounts_digest = accounts_identity_digest(stats_conn)
        signature = c.compute_signature(
            cache_conn,
            stats_conn,
            generation=c.current_generation(),
            codex_stats_digest=stats_digest,
            accounts_digest=accounts_digest,
            claude_stats_digest=claude_digest,
        )
        _acct_suffix = f":a{accounts_digest}" if accounts_digest else ""
        # public #5: the hook's budgeted ingest can change what the Codex
        # envelope owes without moving `codex_physical_mutation_seq` — a tick
        # whose walk consumed only deduped or non-`token_count` bytes commits
        # no row. Without this leg `reuse_coherent_source_state` hands back the
        # prior Codex object and the `ingest_backlog` field never reaches the
        # wire. Empty (and so byte-neutral) once the backlog has drained.
        _backlog = getattr(signature, "codex_ingest_backlog_sig", "")
        _backlog_suffix = f":b{_backlog}" if _backlog else ""
        # #556 S2 §3.6: the resolved range and the per-aggregate outcome enter
        # BOTH providers' version material. Both, so a shared-start change (a
        # display-day rollover) rebuilds them in lockstep and a coherent pair
        # can never disagree about the interval their rows cover. The fragment
        # below assumes SUCCESS, which is what makes it also the reuse gate: a
        # prior generation whose fold failed carries a different fragment and
        # therefore cannot be reused.
        _aggregate_suffix = ":g" + aggregate_scope_identity(
            build_aggregate_scope(published_range),
        )
        codex_version = (
            f"codex:{signature.max_codex_id}:"
            f"{signature.codex_physical_mutation_seq}:{stats_digest}:"
            f"{semantics.codex_identity}{_acct_suffix}{_backlog_suffix}"
            f"{_aggregate_suffix}"
        )
        # #582: the dedicated accounting ledger is deliberately absent from
        # the published version string (byte-stable API contract), but an
        # id-stable token/account/project mutation still has to bypass exact
        # provider reuse so the incremental path can consume it.
        _codex_accounting_pending = c._load_sibling(
            "_lib_snapshot_cache"
        ).codex_accounting_cache_pending(cache_conn)
        # #556 S1 §3.6: normalized period identity, so a nominal week rollover
        # invalidates the generation even when no database signature moved.
        _period_identity = _tui_claude_period_identity(claude_data)
        # #556 S5 (Unit 1 review R5) — the CONFIGURED budget period has its own
        # boundary, and `_period_identity` above tracks only the subscription
        # week. Empty for an unconfigured or subscription-week budget, so the
        # version string is byte-identical on every install that had one before.
        _budget_identity = _tui_claude_budget_window_identity(
            semantics.claude_budget,
            now_utc=now_utc,
            display_tz_name=display_tz_name,
            week_start_name=semantics.week_start_name,
        )
        _budget_suffix = f":b{_budget_identity}" if _budget_identity else ""
        claude_version = (
            f"claude:{signature.max_entry_id}:{signature.entry_mutation_seq}:"
            f"{signature.max_wus_id}:{signature.max_wcs_id}:"
            f"{signature.reset_sig[0]}:{signature.reset_sig[1]}:"
            f"{signature.generation}:{semantics.claude_identity}"
            f":p{_period_identity}{_budget_suffix}{_acct_suffix}"
            f"{_aggregate_suffix}:x{claude_digest}"
        )
        prior_claude = (
            prior_bundle.sources.get("claude")
            if prior_bundle is not None else None
        )
        prior_codex = (
            prior_bundle.sources.get("codex")
            if prior_bundle is not None else None
        )
        claude_reuse_version = combined_accounting_version(
            claude_version,
            prior_claude.combined_accounting if prior_claude is not None else None,
        )
        codex_reuse_version = combined_accounting_version(
            codex_version,
            prior_codex.combined_accounting if prior_codex is not None else None,
        )
        if claude_ingest_failed:
            warning = SourceDashboardWarning(
                "source_ingest_failed", "Source ingest failed.", "ingest",
            )
            claude = (
                degrade_source_state(prior_claude, warning)
                if prior_claude is not None
                else unavailable_source_state("claude", warning)
            )
        elif claude_ingest_contended:
            warning = SourceDashboardWarning(
                "source_ingest_contended", "Source ingest is in progress.", "ingest",
            )
            claude = (
                degrade_source_state(prior_claude, warning)
                if prior_claude is not None
                else unavailable_source_state("claude", warning)
            )
        else:
            claude = reuse_coherent_source_state(
                prior_claude, data_version=claude_reuse_version,
            )
            # #556 S2 §3.6, gate 2 of 2. The version fragment above already
            # rejects a failed generation, but this gate is stated explicitly
            # rather than left implicit in string arithmetic: exact-version
            # provider reuse returns the PRIOR OBJECT unchanged, so a caught
            # fold failure that survived reuse would withhold the aggregate for
            # the life of the process. Gate 1 is the bundle-level idle guard.
            if claude is not None and aggregate_scope_failed(claude):
                claude = None
        if codex_ingest_failed:
            warning = SourceDashboardWarning(
                "source_ingest_failed", "Source ingest failed.", "ingest",
            )
            codex = (
                degrade_source_state(prior_codex, warning)
                if prior_codex is not None
                else unavailable_source_state("codex", warning)
            )
        elif codex_ingest_contended:
            warning = SourceDashboardWarning(
                "source_ingest_contended", "Source ingest is in progress.", "ingest",
            )
            codex = (
                degrade_source_state(prior_codex, warning)
                if prior_codex is not None
                else unavailable_source_state("codex", warning)
            )
        else:
            codex = (
                None if prior_codex is not None and (
                    _codex_accounting_pending
                    or any(
                        warning.code == "codex_projection_incoherent"
                        for warning in prior_codex.warnings
                    )
                    or codex_decision_deadline_passed(prior_codex, now_utc)
                ) else reuse_coherent_source_state(
                    prior_codex, data_version=codex_reuse_version,
                )
            )
            if codex is not None and aggregate_scope_failed(codex):
                codex = None
            _tui_note_codex_regime("active" if codex is None else "idle")

        # Capture every cache-backed carrier while one read transaction names
        # the generation. Public view construction happens only after rollback.
        legacy_labels = (
            c.legacy_project_labels(projects_envelope)
            if projects_envelope is not None else None
        )
        claude_aggregate_capture = None
        claude_budget_capture = None
        if claude is None:
            claude_aggregate_capture = _tui_capture_claude_aggregates(
                cache_conn,
                shared_start=common_range_start,
                shared_end_exclusive=shared_end_exclusive,
                now_utc=now_utc,
                display_tz_name=semantics.display_tz_name,
                legacy_labels=legacy_labels,
                max_entry_id=signature.max_entry_id,
                entry_mutation_seq=signature.entry_mutation_seq,
                generation=signature.generation,
            )
            claude_budget_capture = _tui_capture_claude_budget_domain(
                cache_conn,
                stats_conn,
                claude_budget=semantics.claude_budget,
                now_utc=now_utc,
                display_tz_name=semantics.display_tz_name,
                week_start_name=semantics.week_start_name,
            )

        codex_capture = None
        codex_capture_failed = False
        codex_context = None
        codex_split_seams_unpatched = (
            build_codex_source_state
            is sys.modules["_cctally_dashboard_sources"].build_codex_source_state
        )
        if codex is None:
            try:
                codex_context = DashboardReadContext(
                    cache_conn=cache_conn,
                    stats_conn=stats_conn,
                    range_start=common_range_start,
                    now_utc=now_utc,
                    display_tz_name=semantics.display_tz_name,
                    week_start_idx=semantics.week_start_idx,
                    week_start_name=semantics.week_start_name,
                    speed=semantics.speed,
                    codex_budget=semantics.codex_budget,
                    codex_quota_actual_thresholds=semantics.codex_quota_actual_thresholds,
                    codex_quota_projected_thresholds=semantics.codex_quota_projected_thresholds,
                    cache_report_anomaly_threshold_pp=semantics.cache_report_anomaly_threshold_pp,
                    stats_identity=(stats_digest, accounts_digest, claude_digest),
                )
                if codex_split_seams_unpatched:
                    codex_scope_cm = codex_path_scope()
                    codex_path_scope_value = codex_scope_cm.__enter__()
                    codex_capture = capture_codex_source_state(
                        codex_context, path_scope=codex_path_scope_value,
                    )
            except Exception:
                codex_capture_failed = True
                _lib_log.get_logger("dashboard").error(
                    "codex_read_model source capture failed",
                    exc_info=True,
                )

        if cache_read_tx:
            _record_cache_pin()
            cache_conn.rollback()
            cache_read_tx = False
        if claude is None:
            claude_available = "ok" if (claude_cost_usd or claude_total_tokens) else "empty"
            # #341 Task 4 (Ruling C): the conditional per-account Claude wire,
            # symmetric with Codex. Built ONLY when the Claude provider has >1
            # REAL account (R8) — a <=1-real-account install (every envelope
            # golden) adds nothing, so its wire is byte-identical. The generic
            # chip row / hero cards light up automatically for a decorated Claude
            # source. A read failure degrades to the byte-stable undecorated shape.
            claude_accounts: list[dict[str, object]] = []
            claude_combined_accounting: dict[str, object] | None = None
            try:
                import _cctally_account
                if _cctally_account.provider_is_decorated(stats_conn, "claude"):
                    (
                        claude_accounts,
                        claude_combined_accounting,
                    ) = _claude_accounts_wire(stats_conn, now_utc=now_utc)
            except Exception:
                # #341 Task 4 P3 — degrade to no-accounts-wire on ANY read
                # failure, symmetric with the Codex path. Beyond sqlite, the wire
                # reaches the identity read (file I/O on ~/.claude.json via
                # resolve_active_account_keys) and the account registry/label
                # lookups; a transient failure in this best-effort DECORATIVE
                # wire must never fail the whole dashboard tick — it just falls
                # back to the byte-stable undecorated shape.
                claude_accounts = []
                claude_combined_accounting = {
                    "scope": "account_cycles",
                    "status": "unresolved",
                    "cause": "account_cost_unresolved",
                    "contributions": (),
                }
            if claude_aggregate_capture is None:
                raise RuntimeError("Claude aggregate capture is missing")
            aggregate_payload, aggregate_outcomes = (
                _tui_build_claude_aggregates_from_capture(
                    claude_aggregate_capture,
                    now_utc=now_utc,
                    display_tz_name=semantics.display_tz_name,
                    # The legacy display keys the drill-down route resolves
                    # against. Published rows adopt them wherever they exist,
                    # so the aggregate identity and the legacy one agree and
                    # the bounded rows stay routable. The raw envelope is
                    # required — `claude_data` has already replaced every
                    # legacy display key with an opaque key and dropped
                    # `bucket_path`, so the map cannot be recovered from it.
                    # `None` — not an empty map — when no envelope was built,
                    # so the fold can tell "the legacy population is empty"
                    # from "the legacy population is unknown" and withhold on
                    # the second.
                    legacy_labels=legacy_labels,
                )
            )
            # #617: price and interpret the budget rows captured from this
            # function's one cache generation. The legacy Claude envelope was
            # constructed before this call, and stats.db deliberately stays in
            # statement-scoped autocommit, so the claim remains cache-local.
            if claude_budget_capture is None:
                raise RuntimeError("Claude budget capture is missing")
            claude_budget_overlay, claude_budget_events = (
                _tui_build_claude_budget_domain_from_capture(
                    claude_budget_capture, now_utc=now_utc,
                )
            )
            claude_aggregate_scope = build_aggregate_scope(
                published_range, aggregate_outcomes,
            )
            if aggregate_scope_failed(claude_aggregate_scope):
                # The published version must distinguish a failed fold from a
                # successful one over the same signature and the same bounds;
                # otherwise both would publish different rows under one
                # `data_version`. It also makes the next tick's success-shaped
                # candidate version mismatch, forcing the rebuild §3.6 requires.
                claude_version = (
                    f"{claude_version}:x"
                    f"{aggregate_scope_identity(claude_aggregate_scope)}"
                )
            claude = SourceDashboardState(
                source="claude",
                availability=claude_available,
                freshness="fresh",
                warnings=(),
                data_version=combined_accounting_version(
                    claude_version, claude_combined_accounting,
                ),
                last_success_at=now_utc,
                capabilities={
                    "hero": CapabilityRecord("supported", "subscription-week"),
                    "daily": CapabilityRecord("supported", "calendar-day"),
                    "monthly": CapabilityRecord("supported", "calendar-month"),
                    "weekly": CapabilityRecord("supported", "subscription-week"),
                    "sessions": CapabilityRecord("supported", "legacy-session-rollup"),
                    "forensics": CapabilityRecord("supported", "legacy-projection"),
                    "quota": CapabilityRecord("supported", "subscription-week"),
                    # #556 S5 §3.6: the CONFIGURED period, not a constant. This
                    # record used to say `subscription-week` unconditionally
                    # while Codex advertised `calendar-period`, and once the
                    # published status beside it can carry `calendar-week` or
                    # `calendar-month` the constant contradicts the very object
                    # it describes. The default period IS `subscription-week`,
                    # so an install with no budget configured advertises exactly
                    # what it advertised before.
                    "budget": CapabilityRecord(
                        "supported", _tui_claude_budget_period(
                            semantics.claude_budget),
                    ),
                    "projects": CapabilityRecord("supported", "legacy-projection"),
                    "alerts": CapabilityRecord("supported", "provider-native"),
                },
                data={
                    **_tui_claude_data_with_budget(
                        _tui_claude_data_with_aggregates(
                            claude_data,
                            aggregate_payload,
                            fallback={
                                "hero": {
                                    "cost_usd": claude_cost_usd,
                                    "total_tokens": claude_total_tokens,
                                },
                                "periods": {"daily": {"total_cost_usd": claude_cost_usd, "total_tokens": claude_total_tokens}},
                                "sessions": {"rows": ()},
                                "projects": {"rows": ()},
                                "quota": {"blocks": (), "milestones": ()},
                                "budget": {"label": "Claude subscription budget"},
                                "alerts": {"rows": ()},
                            },
                        ),
                        claude_budget_overlay,
                    ),
                    **({"accounts": claude_accounts} if claude_accounts else {}),
                },
                domain_freshness=_tui_claude_domain_freshness(
                    claude_data, now_utc=now_utc,
                ),
                # #556 S5 §3.7: server-private, NEVER published. The idle clock
                # recomputes trailing-24h spend by iterating individual events,
                # which the status aggregate cannot supply.
                clock_data={"claude_budget_cost_events": claude_budget_events},
                combined_accounting=claude_combined_accounting,
                aggregate_scope=claude_aggregate_scope,
            )
        if codex is None:
            try:
                if codex_capture_failed or codex_context is None:
                    raise RuntimeError("Codex source capture failed")
                if codex_split_seams_unpatched:
                    if codex_capture is None:
                        raise RuntimeError("Codex source capture failed")
                    codex = build_codex_source_state_from_capture(
                        codex_capture,
                        data_version=codex_version,
                        path_scope=codex_path_scope_value,
                    )
                else:
                    codex = build_codex_source_state(
                        codex_context, data_version=codex_version,
                    )
                # Attached ONLY on a fresh build, never on the reuse or degrade
                # paths: those carry rows this tick did not produce, and their
                # own carrier already describes the range that bounds them.
                codex = dataclasses.replace(
                    codex,
                    aggregate_scope=build_aggregate_scope(published_range),
                )
            except Exception:
                _lib_log.get_logger("dashboard").error(
                    "codex_read_model source build failed",
                    exc_info=True,
                )
                warning = SourceDashboardWarning(
                    "source_build_failed", "Source data could not be built.", "read_model",
                )
                codex = (
                    degrade_source_state(prior_codex, warning)
                    if prior_codex is not None
                    else unavailable_source_state("codex", warning)
                )
        # #350 spec §3.3: clock Codex UNCONDITIONALLY — after every build /
        # reuse / degrade branch and before composition — so the retained
        # cycle's expiry invariant holds on EVERY path, including the reuse
        # path that returns the exact prior object (§2.5). Same-instant identity
        # is preserved by ``refresh_codex_source_clock``'s own data-equality
        # guard, so a freshly built state is handed back unchanged.
        codex = refresh_codex_source_clock(codex, now_utc=now_utc)
        # #556 S5 §3.7: Claude is clocked here too, on the same terms and for
        # the same reason. This comment used to say Claude was deliberately
        # untouched, and that was correct only while Claude published nothing
        # time-dependent. It now publishes a budget pace, and exact-version
        # reuse returns the PRIOR OBJECT unchanged — so any tick another source
        # forced would have republished a budget frozen at its build instant.
        # Only the budget leg runs here: the two freshness axes need the legacy
        # `current_week` object, which this builder does not hold, and they are
        # already advanced by the pure-idle clock that does.
        claude = _refresh_claude_budget_clock(claude, now_utc=now_utc)
        # #556 S1 §3.8: the decoration fact reaches composition as authoritative
        # server-only metadata. It is attached HERE, after every build / reuse /
        # degrade / clock branch, so no branch can publish a state without it.
        claude = _tui_with_account_scope(
            claude, _tui_resolve_account_scope(stats_conn, "claude"),
        )
        codex = _tui_with_account_scope(
            codex, _tui_resolve_account_scope(stats_conn, "codex"),
        )
        combined = compose_all_state(claude, codex)
        bundle = SourceDashboardBundle(
            source_schema_version=SOURCE_SCHEMA_VERSION,
            default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={"claude": claude, "codex": codex, "all": combined},
        )
        # The cache pin ended after carrier capture. These cheap post-build
        # signatures intentionally reject only stats-side movement; a cache
        # commit during the pure folds belongs to the next generation.
        post_stats_digest = codex_stats_digest(stats_conn)
        post_accounts_digest = accounts_identity_digest(stats_conn)
        post_claude_digest = claude_stats_digest(stats_conn)
        post_signature = c.compute_signature(
            cache_conn,
            stats_conn,
            generation=c.current_generation(),
            codex_stats_digest=post_stats_digest,
            accounts_digest=post_accounts_digest,
            claude_stats_digest=post_claude_digest,
        )
        stats_generation_moved = (
            post_signature.max_wus_id != signature.max_wus_id
            or post_signature.max_wcs_id != signature.max_wcs_id
            or post_signature.reset_sig != signature.reset_sig
            or post_stats_digest != stats_digest
            or post_accounts_digest != accounts_digest
            or post_claude_digest != claude_digest
        )
        if stats_generation_moved:
            if prior_bundle is not None:
                return prior_bundle
            raise RuntimeError("source read generation moved during build")
        return bundle
    finally:
        _record_cache_pin()
        if cache_read_tx:
            cache_conn.rollback()
        if codex_scope_cm is not None:
            codex_scope_cm.__exit__(None, None, None)
        cache_conn.close()


def _tui_hydrating_source_bundle() -> SourceDashboardBundle:
    """Return the honest no-ingest source state used by the cheap dashboard seed.

    The seed has not coordinated either provider ingest or derived projection,
    so it must not present those partial headline fields as a coherent provider
    generation.  The dashboard's existing ``hydrating`` flag identifies this
    short-lived state; the first background rebuild replaces the whole frozen
    bundle atomically.
    """
    claude = SourceDashboardState(
        source="claude",
        availability="partial",
        freshness="stale",
        warnings=(),
        data_version="hydrating:claude",
        last_success_at=None,
        capabilities={},
        data=None,
        domain_freshness={"hero": "stale", "quota": "stale", "sessions": "stale"},
    )
    codex = SourceDashboardState(
        source="codex",
        availability="partial",
        freshness="stale",
        warnings=(),
        data_version="hydrating:codex",
        last_success_at=None,
        capabilities={},
        data=None,
        domain_freshness={"hero": "stale", "quota": "stale", "sessions": "stale"},
    )
    return SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": compose_all_state(claude, codex)},
    )


def _tui_source_bundle_can_idle(bundle: SourceDashboardBundle | None) -> bool:
    """Return whether both physical provider generations are safe to retain.

    A stable dispatch key alone cannot prove that a previously unavailable or
    degraded provider remains unavailable: a projection certificate can be
    repaired without changing accounting facts.  Such a source must take the
    full source-bundle path again; that path still independently reuses the
    healthy provider by its own data version.
    """
    if not isinstance(bundle, SourceDashboardBundle):
        return False
    for source in ("claude", "codex"):
        state = bundle.sources.get(source)
        if not isinstance(state, SourceDashboardState):
            return False
        # Idle eligibility is provider-generation coherence, deliberately not a
        # hero/quota/sessions age aggregate.
        if (state.availability not in ("ok", "empty")
                or state.freshness != "fresh"
                or state.data is None):
            return False
        # #556 S2 §3.6, gate 1 of 2. A locally caught fold failure leaves an
        # otherwise `ok` and `fresh` provider, so without this leg the bundle
        # would qualify for idle reuse and one transient failure would withhold
        # the aggregate for the life of the process. Falling through here routes
        # to the bounded source-adapter rebuild, which re-folds — at most one
        # rebuild per tick, so it creates no retry loop.
        if aggregate_scope_failed(state):
            return False
    return True


def _tui_common_source_range_start(
    daily_panel: Sequence[TuiDailyPanelRow],
    *,
    now_utc: dt.datetime,
    display_tz: dt.tzinfo | None,
) -> dt.datetime:
    """Return the shared provider interval from the already-built daily rows.

    #556 S2 §3.2: the start bound is now resolved by
    ``_cctally_dashboard.resolve_shared_range``, which also owns the exclusive
    upper bound the Claude folds enforce. This wrapper stays because every
    existing caller wants only the start, and because it is the monkeypatch
    surface the source-invalidation tests already use.
    """
    start, _end_exclusive = _cctally().resolve_shared_range(
        daily_panel, now_utc=now_utc, display_tz=display_tz,
    )
    return start


def _tui_publish_final(tick, hub, snap, *, publication="final",
                       monotonic_ns=None, utcnow=None):
    """Publish a tick's closing frame, then close its record (#583 S1 §1.2).

    The order is the contract and it is asserted by spec §7.3: the record is
    written AFTER ``hub.publish`` returns, so a reader that sees a ring entry
    knows the frame reached the hub rather than merely having been built. The
    two clocks are injectable for that gate; production passes neither.

    ``tick`` may be None so a caller outside a tick boundary still publishes.
    """
    hub.publish(snap)
    if tick is None:
        return
    monotonic_ns = monotonic_ns or time.monotonic_ns
    utcnow = utcnow or (lambda: dt.datetime.now(dt.timezone.utc))
    tick.set_publication(publication)
    tick.finish(published_ns=monotonic_ns(), published_at=utcnow().isoformat())


def _tui_build_snapshot(
    *,
    now_utc: dt.datetime | None = None,
    skip_sync: bool = False,
    display_tz_pref_override: "str | None" = None,
    precompute_envelope: bool = False,
    runtime_bind: "str | None" = None,
) -> DataSnapshot:
    """Build once, then perform at most one post-query stats heal/reopen.

    #583 S1 §1.2/§1.3: opens a STANDALONE tick context only when no dashboard
    tick is already open on this thread, so a build made outside a refresh is
    recorded while an A2 partial build nested inside a live refresh is not
    double-counted as a second tick. THREE callers reach it that way — ``tui
    --render-once``, ``cctally-snapshot-measure``, and the dashboard's own
    pre-bind seed on the ``--no-sync`` branch of
    ``_dashboard_initial_snapshot_once``. The spec names only the first two and
    says the A1 seed is outside the tick boundary because "it bypasses
    ``_tui_build_snapshot``"; that is true of the ingesting branch and false of
    the ``--no-sync`` one, which calls straight through here. Recording it is
    right — it is a real build with a real cost — so the surface names the
    class rather than enumerating callers. The
    builder span is installed here as well as at the dashboard's own ``_build``
    wrapper, because those two standalone callers never reach that wrapper and
    would otherwise carry a total duration with no split.
    """
    if _tick_stats.current() is not None:
        return _tui_build_snapshot_impl(
            now_utc=now_utc, skip_sync=skip_sync,
            display_tz_pref_override=display_tz_pref_override,
            precompute_envelope=precompute_envelope,
            runtime_bind=runtime_bind,
        )
    tick = _tick_stats.begin_tick(standalone=True)
    try:
        with tick.build_span():
            snap = _tui_build_snapshot_impl(
                now_utc=now_utc, skip_sync=skip_sync,
                display_tz_pref_override=display_tz_pref_override,
                precompute_envelope=precompute_envelope,
                runtime_bind=runtime_bind,
            )
    except BaseException:
        tick.mark_degraded()
        raise
    finally:
        tick.finish(
            published_ns=time.monotonic_ns(),
            published_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
    return snap


def _tui_build_snapshot_impl(
    *,
    now_utc: dt.datetime | None = None,
    skip_sync: bool = False,
    display_tz_pref_override: "str | None" = None,
    precompute_envelope: bool = False,
    runtime_bind: "str | None" = None,
) -> DataSnapshot:
    """The build-and-heal body. See ``_tui_build_snapshot`` for the boundary."""

    try:
        return _tui_build_snapshot_once(
            now_utc=now_utc,
            skip_sync=skip_sync,
            display_tz_pref_override=display_tz_pref_override,
            precompute_envelope=precompute_envelope,
            runtime_bind=runtime_bind,
            stats_heal_attempted=False,
        )
    except _StatsSnapshotCorruption as fault:
        # ``_tui_build_snapshot_once`` closes its live stats connection before
        # this boundary. The replacement-capable hook can therefore satisfy
        # the whole-family drain gate, and the retry opens the published family.
        #
        # #496 S3 §6: the heal now DEFERS, and its signal is a
        # `BaseException`. This call sits before the retry's own `except`, so
        # the shared deferral base is caught AT the heal-call boundary and the
        # corruption-typed degraded frame is built directly. The retry must not
        # spin: after deferral the index is still corrupt, so a second attempt
        # would only fail again, and the frame converges on a later tick.
        try:
            _tui_heal_post_query_stats(fault.cause)
        except _cctally().StatsRebuildDeferred as deferred:
            return _tui_stats_retry_degraded_snapshot(
                now_utc=now_utc or dt.datetime.now(dt.timezone.utc),
                exc=deferred,
                precompute_envelope=precompute_envelope,
                runtime_bind=runtime_bind,
            )
        try:
            return _tui_build_snapshot_once(
                now_utc=now_utc,
                # The first attempt already completed the one cache ingest plan.
                skip_sync=True,
                display_tz_pref_override=display_tz_pref_override,
                precompute_envelope=precompute_envelope,
                runtime_bind=runtime_bind,
                stats_heal_attempted=True,
            )
        except Exception as retry_exc:
            # A fresh opener can still lose a race to damage/maintenance after
            # the heal returns. Corruption on this one retry is a stable,
            # typed degraded frame—not a third attempt or a dashboard crash.
            if not _cctally()._is_sqlite_corruption_error(retry_exc):
                raise
            return _tui_stats_retry_degraded_snapshot(
                now_utc=now_utc or dt.datetime.now(dt.timezone.utc),
                exc=retry_exc,
                precompute_envelope=precompute_envelope,
                runtime_bind=runtime_bind,
            )


def _tui_build_snapshot_once(
    *,
    now_utc: dt.datetime | None = None,
    skip_sync: bool = False,
    display_tz_pref_override: "str | None" = None,
    precompute_envelope: bool = False,
    runtime_bind: "str | None" = None,
    stats_heal_attempted: bool,
) -> DataSnapshot:
    """Single-shot build of a DataSnapshot from the DB + cache.

    Runs in the sync thread. Catches exceptions per sub-build and records
    them on `last_sync_error` so the UI can surface them without crashing.

    ``display_tz_pref_override`` (F3): a canonical tz token (``"local"``
    / ``"utc"`` / IANA name) that overrides ``config.display.tz`` for
    the lifetime of this build. Used by ``cmd_dashboard`` when ``--tz``
    is supplied so the in-memory zone wins over the persisted config
    without modifying it. ``None`` means "respect config".

    ``precompute_envelope`` (#268 M4, spec §6): when True, precompute the
    dashboard envelope's doctor / config / update-state reads once here (on
    the sync thread, doctor behind a short-TTL memo) and attach them to
    ``DataSnapshot.doctor_payload`` / ``.envelope_precompute`` so
    ``snapshot_to_envelope`` stays a pure renderer. Set True ONLY by the
    DASHBOARD callers (the sync-thread rebuild + the initial snapshot); the
    terminal TUI leaves it False so it never forks the `security` keychain
    subprocess it doesn't render. ``runtime_bind`` is the actual host the
    dashboard bound to, threaded into the doctor gather so
    ``safety.dashboard_bind`` reflects the running state.
    """
    import time
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    # #276 perf: reset any prior tree on this thread, then bracket the whole
    # snapshot build as the "snapshot" root phase. Opened via the context-
    # manager protocol so the long body below is not reindented; closed +
    # stashed at each return. Near-noop when CCTALLY_PERF_TRACE is unset.
    _perf.reset_thread()
    _p_snapshot = _perf.phase("snapshot")
    _p_snapshot.__enter__()
    # Read config ONCE per rebuild and reuse it for the display-tz resolution
    # here AND the envelope precompute below (#268 M4) — the envelope used to
    # call ``load_config()`` twice per SSE client per tick.
    raw_config = load_config()
    # Resolve the display tz once per snapshot so labels rendered into
    # BlocksPanelRow / future panel rows share a single zone with the
    # envelope's `display` block. Routed through the shared
    # `_resolve_display_tz_obj` helper so this site, `_compute_display_block`,
    # and `_handle_get_block_detail` share identical fallback semantics
    # (one-shot stderr warning on local-resolution failure). Not threaded
    # into label-FOR-LOOKUP paths like `_aggregate_monthly` keys (out of
    # scope for Task 11).
    _build_display_tz = _resolve_display_tz_obj(
        _apply_display_tz_override(raw_config, display_tz_pref_override)
    )
    conn = open_db()
    try:
        errors: list[str] = []
        sync_failures: list[SyncFailureAttribution] = []

        def capture_failure(
            leg: str,
            database: str,
            exc: Exception,
        ) -> None:
            _tui_capture_sync_failure(
                conn,
                errors,
                sync_failures,
                leg=leg,
                database=database,
                exc=exc,
                stats_heal_attempted=stats_heal_attempted,
            )

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
        # ── Sync-once (spec §4, #268) ──────────────────────────────────
        # Ingest new JSONL bytes into the cache EXACTLY ONCE at the top of
        # the rebuild, then read every builder with skip_sync=True (pure
        # SQLite reads). Before this, each of the ~8 wide builders called
        # get_entries(..., skip_sync=False) which ran its own sync_cache —
        # ~8-10 redundant whole-tree globs + per-file stats per rebuild,
        # the CPU-side cost that pegs a core on a large instance.
        #
        # The caller's skip_sync flag expresses the --no-sync intent:
        # honor it by gating ONLY this top-of-rebuild ingest. Downstream
        # builders always read pure regardless (skip_sync is reassigned to
        # True below), so --no-sync means "no ingest, still read".
        do_ingest = not skip_sync
        claude_ingest_contended = False
        claude_ingest_failed = False
        codex_ingest_contended = False
        codex_ingest_failed = False
        _tick = _tick_stats.current()
        with _perf.phase("sync") as _p_sync:
            _p_sync.set_meta(ingest=do_ingest)
            if do_ingest:
                # #583 S1 §1.3: the internal-sync ingest span, for the direct
                # build path (`tui --render-once`, `cctally-snapshot-measure`).
                # Bracketed rather than a `with` block so the body below is not
                # reindented; a leaked span is closed by `TickContext.finish`.
                _ingest_span = (
                    _tick.ingest_span() if _tick is not None else None
                )
                if _ingest_span is not None:
                    _ingest_span.__enter__()
                try:
                    cache_conn = _cctally().open_cache_db()
                    cache_mod = _cctally()._load_sibling("_cctally_cache")
                    try:
                        def _claude_leg(active_conn):
                            try:
                                return sync_cache(active_conn), None
                            except sqlite3.DatabaseError as exc:
                                if cache_mod._cctally_db_sib._is_sqlite_corruption_error(
                                    exc
                                ):
                                    raise
                                return None, exc
                            except Exception as exc:
                                return None, exc

                        operations = [_claude_leg]
                        operation_origins = ["view_model.claude.sync"]
                        if precompute_envelope:
                            def _codex_leg(active_conn):
                                try:
                                    return sync_codex_cache(active_conn), None
                                except sqlite3.DatabaseError as exc:
                                    if cache_mod._cctally_db_sib._is_sqlite_corruption_error(
                                        exc
                                    ):
                                        raise
                                    return None, exc
                                except Exception as exc:
                                    return None, exc

                            operations.append(_codex_leg)
                            operation_origins.append("view_model.codex.sync")

                        results, cache_conn = (
                            cache_mod._run_cache_plan_with_recovery(
                                cache_conn,
                                tuple(operations),
                                origins=tuple(operation_origins),
                            )
                        )
                        claude_ingest, claude_error = results[0]
                        if claude_error is None:
                            claude_ingest_contended = bool(
                                getattr(claude_ingest, "lock_contended", False)
                            )
                        else:
                            claude_ingest_failed = True
                            capture_failure(
                                "sync-cache", "cache", claude_error
                            )
                        if precompute_envelope:
                            codex_ingest, codex_error = results[1]
                            if codex_error is None:
                                codex_ingest_contended = bool(
                                    getattr(codex_ingest, "lock_contended", False)
                                )
                            else:
                                codex_ingest_failed = True
                                capture_failure(
                                    "sync-codex-cache", "cache", codex_error
                                )
                    finally:
                        cache_conn.close()
                except Exception as exc:
                    claude_ingest_failed = True
                    if precompute_envelope:
                        codex_ingest_failed = True
                    capture_failure("sync-cache-open", "cache", exc)
                if _ingest_span is not None:
                    _ingest_span.__exit__(None, None, None)
        # Force pure reads for every view builder below, independent of the
        # caller's flag: the single ingest above is the only glob per tick.
        skip_sync = True
        # ── #269 M3.1: shared per-weekref immutable-cost cache (spec §4/§6) ──
        # Gated OFF by default; flipped ON only on the dashboard sync-thread
        # NON-IDLE path below, AFTER the once-per-rebuild `reconcile_weekref_cache`
        # has run. Off ⇒ the trend / weekly-history / forecast builders compute
        # per-closed-week cost directly (CLI / TUI byte-identical). B2 (the
        # cache-report per-day cache) was DROPPED at the M2.0 byte-identity gate
        # (by_project net_usd is not associative across the day partition — see
        # the spec §5 finding note), so only the weekref cache is wired here.
        use_weekref_cost_cache = False
        # ── #269 M4.5: projects-envelope per-(project, week) cache (spec §14) ──
        # Same gating as the weekref cache: OFF by default, flipped ON only on
        # the dashboard sync-thread NON-IDLE path after the once-per-rebuild
        # `reconcile_projects_env_cache` succeeds. Off ⇒ `_build_projects_envelope`
        # does the full-window walk (CLI / drill / test byte-identical).
        use_projects_env_cache = False
        # ── #271 M3: Bug-K pre-credit segment cache (spec §18) ──────────────
        # A DEDICATED flag (Codex-BK-2 — NOT piggybacked on `use_group_a_cache`):
        # OFF by default, flipped ON only on the dashboard sync-thread NON-IDLE
        # path after the once-per-rebuild `reconcile_bugk_cache` succeeds. Off ⇒
        # the Weekly builder's Bug-K synthesis re-folds the closed pre-credit
        # window directly each tick (CLI / share / test byte-identical). A
        # failed/absent reconcile must fall back to direct compute, never reuse
        # stale segment state.
        use_bugk_segment_cache = False
        # ── #272: cache-report per-day cache (spec §5/§6) ───────────────────
        # Same gating: OFF by default, flipped ON only on the dashboard
        # sync-thread NON-IDLE path after the once-per-rebuild
        # `reconcile_cache_report_cache` succeeds. Off ⇒ `build_cache_report_snapshot`
        # fetches + folds the full 14d window every tick (byte-identical to the
        # cached path). A failed/absent reconcile falls back to full recompute.
        use_cache_report_cache = False
        # ── Three-path dispatch — idle short-circuit (spec §3, #268 M5.1) ──
        # Compute the composite data-version signature (cheap MAX(id) descents
        # over cache.db + stats.db + the reset-event change-signal + the
        # generation counter). When it is UNCHANGED versus the last published
        # rebuild AND no wall-clock day/week/month boundary has rolled over,
        # take the IDLE path: reuse the prior snapshot's heavy period/session
        # rows and re-patch ONLY the time-derived fields + doctor-on-TTL, then
        # return — NO re-aggregation, so an idle dashboard sits near 0% CPU.
        # Dashboard-only (``precompute_envelope``) + sync-thread-only, mirroring
        # the Group A / session cache gating; the shared ``(signature,
        # snapshot)`` memo lives in ``_lib_snapshot_cache`` (single-writer here).
        # The signature is computed AFTER the top-of-rebuild ingest so a fresh
        # tail-ingested row is reflected before the idle decision is made.
        dispatch_key = None
        # #300: the change signal surfaced on the dashboard envelope. Empty
        # unless a dispatch signature is computed below (the non-precompute /
        # TUI path never consumes it). The idle short-circuit carries the prior
        # value forward via ``dataclasses.replace`` (idle ⇒ signature unchanged).
        data_version = ""
        prior_source_bundle: SourceDashboardBundle | None = None
        _sc = None
        prior_key = None
        prior_snap = None
        if precompute_envelope:
            # The last published provider generation is independently useful
            # when calculating today's signature fails.  Read it before the
            # new digest so an identity/build failure can retain the complete
            # old bundle rather than publishing a missing replacement.
            try:
                _sc = _cctally()._load_sibling("_lib_snapshot_cache")
                prior_key, prior_snap = _sc.dispatch_state()
                # #583 S1 §1.1: cold versus warm. A build with no prior
                # dispatch memo has nothing to reuse, so it pays the whole
                # source construction; the flag is sticky across the refresh,
                # because one cold build makes the tick a cold tick.
                if _tick is not None:
                    _tick.set_cold(prior_snap is None)
                if prior_snap is not None:
                    prior_source_bundle = getattr(prior_snap, "source_bundle", None)
            except Exception as exc:
                capture_failure("prior-source-bundle", "other", exc)
            dispatch_sig = None
            with _perf.phase("signature"):
                try:
                    dispatch_sig = _tui_compute_dispatch_signature(conn)
                except QuotaProjectionIncomplete as exc:
                    # #496 S5b §4.7: ahead of the generic handler on purpose.
                    # Below it the refusal is sanitized into a generic
                    # stats-or-cache failure, which names no cause and no
                    # remedy; the message this leg records carries both.
                    capture_failure("quota-projection", "other", exc)
                    dispatch_sig = None
                except Exception as exc:
                    capture_failure("dispatch-signature", "stats_or_cache", exc)
                    dispatch_sig = None
            if dispatch_sig is not None:
                # The idle decision keys on the DB signature AND a render key
                # that captures the config-derived inputs the composite
                # signature does NOT cover (spec §3 is DB-only): the resolved
                # display-tz override + the full raw config (display.tz,
                # alerts_settings, budget, dashboard prefs). A `POST /api/settings`
                # edit advances neither `MAX(id)` nor the reset legs, so without
                # this a settings change would idle-serve the stale envelope; a
                # render-key change forces a full rebuild that re-buckets the
                # calendar builders (their Group A cache already keys tz) and
                # refreshes every config-derived envelope block.
                render_key = (
                    display_tz_pref_override,
                    json.dumps(raw_config, sort_keys=True, default=str),
                )
                dispatch_key = (dispatch_sig, render_key)
                # #300: carry the change signal onto the non-idle snapshot built
                # below. (The idle path returns `idle_snap`, which inherits the
                # prior — equal, since idle ⇒ signature unchanged — value.)
                data_version = _snapshot_data_version(dispatch_sig)
                if (prior_snap is not None and prior_key is not None
                        and dispatch_key == prior_key
                        and not any(
                            failure.database == "stats" and failure.corruption
                            for failure in getattr(
                                prior_snap, "sync_failures", ()
                            )
                        )
                        and not _snapshot_period_rolled_over(
                            prior_snap, now_utc, _build_display_tz)):
                    with _perf.phase("idle-decision"):
                        idle_snap = _tui_build_idle_snapshot(
                            prior_snap, now_utc=now_utc,
                            precompute_envelope=precompute_envelope,
                            runtime_bind=runtime_bind, raw_config=raw_config,
                            display_tz_pref_override=display_tz_pref_override,
                            source_stats_conn=conn,
                            failures=sync_failures,
                            stats_heal_attempted=stats_heal_attempted,
                            source_display_tz_name=(
                                getattr(_build_display_tz, "key", None)
                                if _build_display_tz is not None else None
                            ),
                            source_display_tz=_build_display_tz,
                            codex_ingest_contended=codex_ingest_contended,
                            codex_ingest_failed=codex_ingest_failed,
                            claude_ingest_contended=claude_ingest_contended,
                            claude_ingest_failed=claude_ingest_failed,
                            errors=errors,
                        )
                        assert _sc is not None
                        _sc.store_dispatch_state(dispatch_key, idle_snap)
                    if _tick is not None:
                        _tick.set_dispatch("idle")   # §1.4: the reuse branch
                    _p_snapshot.__exit__(None, None, None)
                    if _perf.enabled():
                        _perf.stash_last(
                            _perf.current_root(), generation=None,
                            generated_at=now_utc.isoformat(),
                        )
                    return idle_snap
                # ── #269 M3.1: non-idle rebuild — reconcile the shared
                # per-weekref immutable-cost cache ONCE here (spec §4/§6),
                # before the builders run, using the dispatch-signature legs
                # already computed for the idle decision (no extra MAX(id) /
                # reset query). A SHORT-LIVED cache.db conn is opened only for
                # reconcile's new-entry watermark query (Codex-4), mirroring
                # `_tui_compute_dispatch_signature`. On success the trend /
                # weekly-history / forecast builders opt into the cache via
                # `use_weekref_cost_cache=True`; any failure leaves the flag OFF
                # (safe direct-compute fallback, byte-identical output).
                try:
                    with _perf.phase("reconcile.cache_open"):
                        _rc_cache_conn = _cctally().open_cache_db()
                    try:
                        with _perf.phase("reconcile.weekref") as _pr:
                            _pr.set_meta(hit=False)
                            _sc.reconcile_weekref_cache(
                                _rc_cache_conn,
                                max_entry_id=dispatch_sig.max_entry_id,
                                max_mutation_seq=dispatch_sig.entry_mutation_seq,
                                reset_sig=dispatch_sig.reset_sig,
                            )
                            use_weekref_cost_cache = True
                            _pr.set_meta(hit=True)
                        # #269 M4.5: reconcile the projects-envelope cache with
                        # the SAME short-lived cache conn (session_files_sig +
                        # the new-entry watermark both live in cache.db). Only
                        # after this succeeds does `_build_projects_envelope`
                        # opt into the cache below.
                        with _perf.phase("reconcile.projects_env") as _pr:
                            _pr.set_meta(hit=False)
                            _sc.reconcile_projects_env_cache(
                                _rc_cache_conn,
                                max_entry_id=dispatch_sig.max_entry_id,
                                max_mutation_seq=dispatch_sig.entry_mutation_seq,
                                max_wus_id=dispatch_sig.max_wus_id,
                                sf_sig=_sc.session_files_sig(_rc_cache_conn),
                            )
                            use_projects_env_cache = True
                            _pr.set_meta(hit=True)
                        # #271 M3: reconcile the Bug-K pre-credit segment cache
                        # with the SAME short-lived cache conn (its watermark
                        # query lives in cache.db), using the dispatch-signature
                        # legs already computed. Only after this succeeds does
                        # `_dashboard_build_weekly_periods` opt into the cache
                        # below (`use_bugk_segment_cache=True`).
                        with _perf.phase("reconcile.bugk") as _pr:
                            _pr.set_meta(hit=False)
                            _sc.reconcile_bugk_cache(
                                _rc_cache_conn,
                                max_entry_id=dispatch_sig.max_entry_id,
                                max_mutation_seq=dispatch_sig.entry_mutation_seq,
                                reset_sig=dispatch_sig.reset_sig,
                            )
                            use_bugk_segment_cache = True
                            _pr.set_meta(hit=True)
                        # #272: reconcile the cache-report per-day cache with the
                        # SAME short-lived cache conn (its watermark query +
                        # session_files_sig both live in cache.db), using the
                        # dispatch-signature legs already computed. `sf_sig`
                        # closes the lazy-`project_path`-backfill hole (Codex-1);
                        # `tz_key` full-clears on a display-tz change (every
                        # calendar-day key shifts). Only after this succeeds does
                        # `build_cache_report_snapshot` opt into the cache below.
                        with _perf.phase("reconcile.cache_report") as _pr:
                            _pr.set_meta(hit=False)
                            _sc.reconcile_cache_report_cache(
                                _rc_cache_conn,
                                max_entry_id=dispatch_sig.max_entry_id,
                                max_mutation_seq=dispatch_sig.entry_mutation_seq,
                                reset_sig=dispatch_sig.reset_sig,
                                sf_sig=_sc.session_files_sig(_rc_cache_conn),
                                # `_load_sibling` is the canonical idempotent
                                # sibling-access idiom (it returns the already-loaded
                                # cctally-bound kernel instance). `_lib_cache_report`
                                # is loaded eagerly at import (bin/cctally), so this
                                # is a plain re-fetch of a populated `sys.modules`
                                # entry — NOT a first-tick KeyError guard.
                                bucket_tz=_cctally()._load_sibling(
                                    "_lib_cache_report"
                                )._resolve_bucket_tz(_build_display_tz),
                                tz_key=str(_build_display_tz),
                            )
                            use_cache_report_cache = True
                            _pr.set_meta(hit=True)
                    finally:
                        _rc_cache_conn.close()
                except Exception as exc:
                    capture_failure("snapshot-cache-reconcile", "cache", exc)
        with _perf.phase("build.current_week"):
            try:
                cw = _tui_build_current_week(conn, now_utc, skip_sync=skip_sync)
            except Exception as exc:
                capture_failure("current-week", "stats_or_cache", exc)
        fc_view = None
        with _perf.phase("build.forecast"):
            try:
                # Issue #57: build the ForecastView once so we capture both
                # the legacy ``ForecastOutput`` (for ``snap.forecast``, which
                # the many TUI panel consumers still read) and the surface
                # fields the envelope adapter used to re-derive inline.
                fc_view = _tui_build_forecast_view(
                    conn, now_utc, skip_sync=skip_sync,
                    use_weekref_cost_cache=use_weekref_cost_cache,
                )
                fc = fc_view.output if fc_view is not None else None
            except Exception as exc:
                capture_failure("forecast", "stats_or_cache", exc)
        # Trend: source from build_trend_view so we capture the 3-sample
        # avg_dollars_per_pct alongside the rows. The TUI build path
        # historically called _tui_build_trend (which now wraps the
        # builder); calling the builder directly here saves one
        # `_aggregate_*` round-trip.
        trend_avg_dpp = None
        with _perf.phase("build.trend"):
            try:
                c = _cctally()
                _trend_view = c.build_trend_view(
                    conn, now_utc=now_utc, n=8, display_tz=_build_display_tz,
                    skip_sync=skip_sync,
                    use_weekref_cost_cache=use_weekref_cost_cache,
                )
                trend = list(_trend_view.rows)
                trend_avg_dpp = _trend_view.avg_dollars_per_pct
            except Exception as exc:
                capture_failure("trend", "stats_or_cache", exc)
        with _perf.phase("build.sessions"):
            try:
                # The sessions aggregator goes through
                # `get_claude_session_entries`, which runs `sync_cache` unless
                # `skip_sync=True` is threaded through. Honor the caller's
                # intent so `--no-sync` and the initial cache-only paint
                # both avoid ingest latency/lock contention.
                sessions = _tui_build_sessions(
                    now_utc, skip_sync=skip_sync, use_session_cache=True,
                    # ``precompute_envelope`` is this build's documented
                    # DASHBOARD marker (set by the sync-thread rebuild + the
                    # initial snapshot, never by the terminal TUI), and the
                    # Session-column title is a dashboard-only field — so it
                    # also gates the bounded transcript-store title read.
                    with_titles=precompute_envelope,
                )
            except Exception as exc:
                capture_failure("sessions", "cache", exc)
        # ---- v2 additions ----
        with _perf.phase("build.milestones"):
            try:
                if cw is not None:
                    milestones = _tui_build_percent_milestones(conn)
            except Exception as exc:
                capture_failure("milestones", "stats", exc)
        history: list = []
        history_median_dpp: "float | None" = None
        with _perf.phase("build.weekly_history"):
            try:
                # Issue #59: build the full TrendView so we capture the
                # pre-computed 4-week-median-non-current scalar alongside
                # the row list; the dashboard envelope adapter surfaces
                # the scalar as ``trend.history_median_dpp`` so
                # TrendModal.tsx stops re-deriving it client-side.
                history_view = _tui_build_weekly_history_view(
                    conn, now_utc, skip_sync=skip_sync, display_tz=_build_display_tz,
                    use_weekref_cost_cache=use_weekref_cost_cache,
                )
                history = list(history_view.rows)
                history_median_dpp = history_view.median_dpp_non_current_4w
            except Exception as exc:
                capture_failure("weekly-history", "stats_or_cache", exc)
        # ---- v2.1 additions: dashboard Weekly / Monthly panels ----
        # Sync-thread view-model totals (spec §6.6): sum directly over
        # the panel rows the dashboard ACTUALLY renders. The previous
        # implementation called ``build_weekly_view`` a second time to
        # capture totals, but that builder doesn't see the Bug-K
        # pre-credit synthesized rows that ``_dashboard_build_weekly_periods``
        # layers on top (``_apply_reset_events_to_subweeks`` shifts the
        # post-reset SubWeek's ``start_ts`` so the pre-credit interval
        # has no SubWeek for ``_aggregate_weekly`` to bucket). On credit
        # weeks the sync-thread total understated the rendered footer by
        # hundreds of dollars (~$372 in the v1.7.2 round-5 data).
        # Sum-over-visible-rows is a structural invariant — see
        # ``test_weekly_envelope_total_matches_sum_of_visible_rows``.
        weekly_total_cost_usd = 0.0
        weekly_total_tokens = 0
        with _perf.phase("build.weekly_periods"):
            try:
                weekly_periods = _dashboard_build_weekly_periods(
                    conn, now_utc, n=12, skip_sync=skip_sync,
                    use_group_a_cache=True,
                    use_bugk_segment_cache=use_bugk_segment_cache,
                )
                # ``stable_sum`` (math.fsum) returns float ``0.0`` on empty rows,
                # so the envelope stays byte-stable with the pre-fix ``0.0`` shape
                # (the dashboard fixture goldens assert exact JSON match).
                weekly_total_cost_usd = stable_sum(
                    r.cost_usd for r in weekly_periods
                )
                weekly_total_tokens = sum(
                    (r.total_tokens for r in weekly_periods), 0,
                )
            except Exception as exc:
                capture_failure("weekly-periods", "stats_or_cache", exc)
        # Sync-thread view-model totals (spec §6.6): sum-over-visible-rows
        # (same invariant as weekly above). Monthly has no Bug-K analogue,
        # but coupling the footer total to the panel-row source of truth
        # eliminates a parallel ``build_monthly_view`` pass that did the
        # same arithmetic with no behavioral upside.
        monthly_total_cost_usd = 0.0
        monthly_total_tokens = 0
        with _perf.phase("build.monthly_periods"):
            try:
                monthly_periods = _dashboard_build_monthly_periods(
                    conn, now_utc, n=12, skip_sync=skip_sync,
                    use_group_a_cache=True,
                    display_tz=_build_display_tz,
                )
                monthly_total_cost_usd = stable_sum(
                    r.cost_usd for r in monthly_periods
                )
                monthly_total_tokens = sum(
                    (r.total_tokens for r in monthly_periods), 0,
                )
            except Exception as exc:
                capture_failure("monthly-periods", "cache", exc)
        # ---- v2.2 additions: dashboard Blocks / Daily panels ----
        # Issue #56: build the BlocksView once and read both rows
        # (presentation) and totals (envelope scalars) from the same
        # pass. ``_dashboard_build_blocks_view`` is the view-returning
        # counterpart to ``_dashboard_build_blocks_panel`` (which is
        # kept as a thin shim for monkeypatch surfaces).
        blocks_total_cost_usd = 0.0
        blocks_total_tokens = 0
        with _perf.phase("build.blocks"):
            try:
                if cw is not None:
                    _blocks_view = _dashboard_build_blocks_view(
                        conn, now_utc,
                        week_start_at=cw.week_start_at,
                        week_end_at=cw.week_end_at,
                        skip_sync=skip_sync,
                        display_tz=_build_display_tz,
                    )
                    blocks_panel = list(_blocks_view.rows)
                    blocks_total_cost_usd = _blocks_view.total_cost_usd
                    blocks_total_tokens = _blocks_view.total_tokens
            except Exception as exc:
                capture_failure("blocks-panel", "stats_or_cache", exc)
        # Sync-thread view-model totals (Bundle 1 / spec §6.6):
        # sum-over-visible-rows (same invariant as weekly/monthly above).
        # Gap days in the materialized panel carry ``cost_usd=0.0`` /
        # ``total_tokens=0``, so summing the panel rows preserves the
        # gap-free totals semantically — and removes a duplicate
        # ``build_daily_view`` pass that did the same arithmetic.
        daily_total_cost_usd = 0.0
        daily_total_tokens = 0
        with _perf.phase("build.daily"):
            try:
                daily_panel = _dashboard_build_daily_panel(
                    conn, now_utc, n=30, skip_sync=skip_sync,
                    use_group_a_cache=True,
                    display_tz=_build_display_tz,
                )
                daily_total_cost_usd = stable_sum(
                    r.cost_usd for r in daily_panel
                )
                daily_total_tokens = sum(
                    (r.total_tokens for r in daily_panel), 0,
                )
            except Exception as exc:
                capture_failure("daily-panel", "cache", exc)
        # ---- threshold-actions T5: alerts envelope array ----
        # Precomputed at sync time so `snapshot_to_envelope` stays a pure
        # renderer (no DB I/O on the dashboard hot path; mirrors how
        # `current_week.five_hour_block` is precomputed via
        # `_select_current_block_for_envelope`).
        with _perf.phase("build.alerts"):
            try:
                alerts = _build_alerts_envelope_array(conn)
            except Exception as exc:
                capture_failure("alerts", "stats", exc)
        # ---- 5h in-place credit (v1.7.x) ----
        # Load 5h milestones (pre + post credit) for the current
        # block's window so CurrentWeekModal can render a merged
        # chronological timeline alongside its weekly milestones.
        # Spec §5.3 (Codex r1 finding 3).
        fh_milestones: list[dict] = []
        with _perf.phase("build.five_hour_milestones"):
            try:
                win_key = None
                if cw is not None and isinstance(cw.five_hour_block, dict):
                    win_key = cw.five_hour_block.get("five_hour_window_key")
                fh_milestones = _tui_build_five_hour_milestones(conn, win_key)
            except Exception as exc:
                capture_failure("five-hour-milestones", "stats", exc)
        # ---- hero-modal historical milestones week index (spec §1a/§3) ----
        # Built ONLY here on the non-idle rebuild (the idle short-circuit
        # returns before this phase and carries the prior index forward via
        # ``dataclasses.replace``), so an idle dashboard issues NONE of the
        # index queries. Reached via the ``cctally`` namespace so tests can
        # monkeypatch it (a call-counter drives the hot-path guard).
        week_index: list[dict] = []
        with _perf.phase("build.week_index"):
            try:
                week_index = sys.modules["cctally"].build_claude_week_index(conn)
            except Exception as exc:
                capture_failure("week-index", "stats", exc)
        # ---- Projects panel + modal envelope (spec §5.2, plan Task 1) -----
        # Per-tick aggregation lives on the sync thread; the dashboard's
        # pure ``snapshot_to_envelope`` reads ``snap.projects_envelope``
        # back unchanged. Errors are recorded on ``last_sync_error`` —
        # the client renders the panel-empty state when the field is
        # None (first tick, or sub-build failure).
        #
        # ATTACH cache.db onto the open stats conn so
        # ``_build_projects_envelope`` (which reads ``session_entries`` +
        # ``session_files`` + ``weekly_usage_snapshots`` off one conn —
        # the test contract per tests/test_projects_envelope.py) sees
        # all three tables. ATTACH/DETACH is cheap and scoped to this
        # sub-build; no schema migration / lock acquisition is needed.
        projects_envelope_block: dict | None = None
        # #276 perf: time the projects-envelope build (ATTACH + walk + DETACH).
        # CM protocol, not a ``with`` block, to avoid reindenting the ~40-line
        # try/except/finally; the broad except + finally guarantee no escape.
        _p_pe = _perf.phase("build.projects_envelope")
        _p_pe.__enter__()
        try:
            c = _cctally()
            cache_db_path = _cctally_core.CACHE_DB_PATH
            conn.execute(
                "ATTACH DATABASE ? AS cache_db",
                (str(cache_db_path),),
            )
            # session_entries / session_files live in cache.db; the
            # builder reads them via raw SQL keyed by the unqualified
            # table names. SQLite's name resolution prefers the `main`
            # schema, so create temporary views in `main` that point
            # at the attached schema's tables. Aliasing via VIEWs keeps
            # the builder portable: unit tests pass one conn carrying
            # both schemas; production wiring uses an attached cache.
            conn.execute(
                "CREATE TEMP VIEW IF NOT EXISTS session_entries AS "
                "SELECT * FROM cache_db.session_entries"
            )
            conn.execute(
                "CREATE TEMP VIEW IF NOT EXISTS session_files AS "
                "SELECT * FROM cache_db.session_files"
            )
            projects_envelope_block = c._build_projects_envelope(
                conn,
                now_utc=now_utc,
                current_week=cw,
                weeks_back=12,
                use_projects_env_cache=use_projects_env_cache,
            )
        except Exception as exc:
            capture_failure("projects-envelope", "stats_or_cache", exc)
        finally:
            try:
                conn.execute("DROP VIEW IF EXISTS session_entries")
                conn.execute("DROP VIEW IF EXISTS session_files")
            except Exception:
                pass
            try:
                conn.execute("DETACH DATABASE cache_db")
            except Exception:
                pass
        _p_pe.__exit__(None, None, None)
        # Late-bind disambiguated `project_key` onto each SessionsPanel
        # row so the SessionsPanel → ProjectsModal cross-nav (spec §4.1)
        # routes by the same identity the Projects envelope emits.
        # Cheap dict-lookup per row; no second aggregation pass.
        #
        # `key_by_bucket_path` is indexed by git-root bucket_path (the
        # envelope builder calls `_resolve_project_key(..., "git-root")`
        # in `_build_projects_envelope`), but `srow.project_path` is the
        # raw cwd from `_aggregate_claude_sessions` — typically a
        # subdirectory of the repo root for monorepo sessions. We must
        # resolve each cwd through the same production resolver so the
        # lookup hits; otherwise `project_key` stays None and the
        # cross-nav button degrades to plain text.
        if projects_envelope_block is not None:
            try:
                key_by_bucket_path: dict[str, str] = {}
                for r in projects_envelope_block.get(
                    "current_week", {}
                ).get("rows", []):
                    bp = r.get("bucket_path")
                    k = r.get("key")
                    if bp and k:
                        key_by_bucket_path[bp] = k
                for r in projects_envelope_block.get(
                    "trend", {}
                ).get("projects", []):
                    bp = r.get("bucket_path")
                    k = r.get("key")
                    if bp and k and bp not in key_by_bucket_path:
                        key_by_bucket_path[bp] = k
                _resolve = _cctally()._resolve_project_key
                resolver_cache: dict = {}
                annotated: list[TuiSessionRow] = []
                for srow in sessions:
                    pkey = None
                    if srow.project_path:
                        bp = _resolve(
                            srow.project_path, "git-root", resolver_cache,
                        ).bucket_path
                        pkey = key_by_bucket_path.get(bp)
                    if pkey is None:
                        annotated.append(srow)
                    else:
                        annotated.append(
                            dataclasses.replace(srow, project_key=pkey)
                        )
                sessions = annotated
            except Exception as exc:
                capture_failure(
                    "projects-cross-nav-bind", "stats_or_cache", exc
                )

        # Cache-report panel + modal envelope block (spec
        # 2026-05-21-cache-report-panel-design.md §5.2). Per-tick build
        # alongside the projects envelope. Threshold is read from
        # ``config.json:cache_report.anomaly_threshold_pp`` and resolved
        # by ``_lib_cache_report.resolve_cache_report_threshold`` — the
        # one definition shared with the Codex read path and the
        # persistence gate (#443 S3 F17), strict and silent (anything
        # that is not an in-range int becomes the default 15);
        # ``anomaly_window_days`` is hardcoded at 14 in v1.
        # display_tz inherits the same resolved zone as every other
        # panel so today-bucketing matches the envelope's ``display``
        # block. Errors record on ``last_sync_error``; ``None`` lands
        # on the DataSnapshot field and the client renders the empty
        # state.
        cache_report_block = None
        with _perf.phase("build.cache_report"):
            try:
                cfg_cr = load_config().get("cache_report") or {}
                threshold_pp = _cctally()._load_sibling(
                    "_lib_cache_report"
                ).resolve_cache_report_threshold(
                    cfg_cr.get("anomaly_threshold_pp")
                )
                _dash_mod = sys.modules["_cctally_dashboard"]
                _bcr = _dash_mod.build_cache_report_snapshot
                cache_report_block = _bcr(
                    now_utc=now_utc,
                    anomaly_threshold_pp=threshold_pp,
                    # Hardcoded for v1; F10 tracks lifting via cache_report.anomaly_window_days config.
                    anomaly_window_days=_dash_mod.CACHE_REPORT_ANOMALY_WINDOW_DAYS,
                    display_tz=_build_display_tz,
                    skip_sync=skip_sync,
                    use_cache_report_cache=use_cache_report_cache,
                )
            except Exception as exc:
                capture_failure("cache-report", "cache", exc)

        # ---- #268 M4: doctor / config / update-state precompute (spec §6) ----
        # Precompute the envelope's doctor / config / update-state reads ONCE
        # here (dashboard callers only), so `snapshot_to_envelope` stays a pure
        # renderer that never forks `security` / reads config.json per SSE
        # client per tick. Errors are folded into `errors` (recorded on
        # `last_sync_error`); the fields stay None on failure → the envelope
        # falls back to its inline computation, preserving behavior.
        doctor_payload_block: "dict | None" = None
        envelope_precompute_block: "dict | None" = None
        if precompute_envelope:
            with _perf.phase("doctor"):
                try:
                    doctor_payload_block = _tui_precompute_doctor_payload(
                        now_utc, runtime_bind,
                    )
                except Exception as exc:
                    capture_failure("doctor-precompute", "other", exc)
            with _perf.phase("envelope.precompute"):
                try:
                    envelope_precompute_block = _tui_precompute_envelope_config(
                        raw_config,
                    )
                except Exception as exc:
                    capture_failure("envelope-precompute", "other", exc)

        # Determine the shared visible interval before publishing either source.
        # The actual source bundle is built after ``snap`` exists, so Claude's
        # entry can be projected from this exact completed legacy snapshot.
        common_range_start: dt.datetime | None = None
        if precompute_envelope:
            common_range_start = _tui_common_source_range_start(
                daily_panel, now_utc=now_utc, display_tz=_build_display_tz,
            )

        snap = DataSnapshot(
            current_week=cw,
            forecast=fc,
            trend=trend,
            sessions=sessions,
            last_sync_at=time.monotonic(),
            last_sync_error=("; ".join(errors) if errors else None),
            generated_at=now_utc,
            sync_failures=tuple(sync_failures),
            percent_milestones=milestones,
            weekly_history=history,
            weekly_periods=weekly_periods,
            monthly_periods=monthly_periods,
            blocks_panel=blocks_panel,
            daily_panel=daily_panel,
            alerts=alerts,
            five_hour_milestones=fh_milestones,
            week_index=week_index,
            daily_total_cost_usd=daily_total_cost_usd,
            daily_total_tokens=daily_total_tokens,
            monthly_total_cost_usd=monthly_total_cost_usd,
            monthly_total_tokens=monthly_total_tokens,
            weekly_total_cost_usd=weekly_total_cost_usd,
            weekly_total_tokens=weekly_total_tokens,
            blocks_total_cost_usd=blocks_total_cost_usd,
            blocks_total_tokens=blocks_total_tokens,
            trend_avg_dollars_per_pct=trend_avg_dpp,
            trend_history_median_dpp=history_median_dpp,
            forecast_view=fc_view,
            projects_envelope=projects_envelope_block,
            cache_report=cache_report_block,
            doctor_payload=doctor_payload_block,
            envelope_precompute=envelope_precompute_block,
            data_version=data_version,
            source_bundle=None,
        )
        if precompute_envelope:
            source_bundle: SourceDashboardBundle | None = None
            try:
                # ``snapshot_to_envelope`` is a pure snapshot projection when
                # its precomputed doctor/config blocks are present. If the
                # optional doctor precompute failed, use a local sentinel for
                # this source-only projection so it cannot recover by opening
                # another database connection; the Claude adapter ignores the
                # doctor field entirely.
                source_snapshot = snap
                if source_snapshot.doctor_payload is None:
                    source_snapshot = dataclasses.replace(
                        source_snapshot,
                        doctor_payload={
                            "severity": "fail",
                            "counts": {"ok": 0, "warn": 0, "fail": 1},
                            "generated_at": now_utc.isoformat(),
                            "fingerprint": "source-projection",
                        },
                    )
                # #566 §5.1 item 6: both calls carry their own phase. The two
                # of them are the tail of the build and were the only region
                # the trace never entered, so a slow store attributed 79% of
                # its build to the root's unnamed remainder.
                with _perf.phase("envelope.legacy_projection"):
                    legacy_envelope = _cctally().snapshot_to_envelope(
                        source_snapshot,
                        now_utc=now_utc,
                        display_tz_pref_override=display_tz_pref_override,
                        runtime_bind=runtime_bind,
                    )
                with _perf.phase("build.source_bundle"):
                    source_bundle = _tui_build_source_bundle(
                        stats_conn=conn,
                        now_utc=now_utc,
                        display_tz_name=(
                            getattr(_build_display_tz, "key", None)
                            if _build_display_tz is not None else None
                        ),
                        codex_ingest_contended=codex_ingest_contended,
                        codex_ingest_failed=codex_ingest_failed,
                        claude_ingest_contended=claude_ingest_contended,
                        claude_ingest_failed=claude_ingest_failed,
                        claude_cost_usd=daily_total_cost_usd,
                        claude_total_tokens=daily_total_tokens,
                        claude_data=_tui_project_claude_source_data(legacy_envelope),
                        common_range_start=common_range_start,
                        projects_envelope=projects_envelope_block,
                        prior_bundle=prior_source_bundle,
                        raw_config=raw_config,
                    )
                if source_bundle is None:
                    raise RuntimeError("source bundle builder returned no bundle")
            except QuotaProjectionIncomplete as exc:
                # #496 S5b §4.7: ahead of the generic handler on purpose. Below
                # it the refusal was sanitized into a generic stats-or-cache
                # failure and the bundle fell back to `prior_source_bundle`,
                # which is `None` on a cold start — a permanently blank Codex
                # source panel with no cause and no remedy stated. The message
                # this leg records carries both.
                capture_failure("quota-projection", "other", exc)
                source_bundle = prior_source_bundle
            except Exception as exc:
                # Public source warnings are stable/sanitized; the detailed
                # exception remains only on the internal rebuild-error string.
                capture_failure("source-bundle", "stats_or_cache", exc)
                source_bundle = prior_source_bundle
            snap = dataclasses.replace(
                snap,
                last_sync_error=("; ".join(errors) if errors else None),
                sync_failures=tuple(sync_failures),
                source_bundle=source_bundle,
            )
        # #268 M5.1: record the (signature+render key, snapshot) so the next
        # dashboard tick can idle-short-circuit when nothing changed. Full-build
        # path only sets it when the key was computed (precompute_envelope); the
        # TUI path never touches the dispatch memo.
        if precompute_envelope and dispatch_key is not None:
            _cctally()._load_sibling("_lib_snapshot_cache").store_dispatch_state(
                dispatch_key, snap,
            )
        # #276 perf: close the "snapshot" root and freeze the completed tree
        # into the process-global slot for the loopback /api/debug/backend
        # endpoint. Whole-dict atomic assignment; never mutated after. No-op
        # when tracing is off.
        if _tick is not None:
            # §1.4: the full branch. `full` outranks a later `idle` inside the
            # same refresh, because a refresh containing one full build cost
            # what a full build costs.
            _tick.set_dispatch("full")
        _p_snapshot.__exit__(None, None, None)
        if _perf.enabled():
            _perf.stash_last(
                _perf.current_root(), generation=None,
                generated_at=now_utc.isoformat(),
            )
        return snap
    except _StatsSnapshotCorruption:
        _p_snapshot.__exit__(*sys.exc_info())
        raise
    finally:
        conn.close()


def _tui_precompute_doctor_payload(
    now_utc: dt.datetime,
    runtime_bind: "str | None",
) -> dict:
    """Precompute the dashboard envelope's small doctor block, via the
    short-TTL memo (#268 M4, spec §6).

    Returns the SAME ``{severity, counts, generated_at, fingerprint}`` dict
    ``snapshot_to_envelope`` used to build inline (or a synthetic FAIL block
    with ``_error`` on a doctor gather/checks failure), so moving the
    computation onto the snapshot is byte-identical for a given
    ``now_utc``/``runtime_bind``. Guarded by ``doctor_payload_memo`` so
    back-to-back warm rebuilds don't re-fork the `security` keychain
    subprocess every tick.
    """
    c = _cctally()
    sc = c._load_sibling("_lib_snapshot_cache")

    def _compute(now: dt.datetime, bind: "str | None") -> dict:
        _ld = c._load_sibling("_lib_doctor")
        try:
            with _perf.phase("doctor.gather"):
                _doc_state = c.doctor_gather_state(
                    now_utc=now, runtime_bind=bind)
            with _perf.phase("doctor.checks"):
                _doc_report = _ld.run_checks(_doc_state)
            return {
                "severity": _doc_report.overall_severity,
                "counts": dict(_doc_report.counts),
                "generated_at": _ld._iso_z(_doc_report.generated_at),
                "fingerprint": _ld.fingerprint(_doc_report),
            }
        except Exception as exc:  # noqa: BLE001 — never crash the rebuild
            # Mirror `snapshot_to_envelope`'s inline FAIL fallback (dashboard
            # `_iso_z`: strftime, no microseconds) so a doctor failure serves
            # the same shape whether computed here or in the envelope.
            return {
                "severity": "fail",
                "counts": {"ok": 0, "warn": 0, "fail": 1},
                "generated_at": now.astimezone(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "fingerprint": "sha1:" + ("0" * 40),
                "_error": f"{type(exc).__name__}: {exc}",
            }

    return sc.doctor_payload_memo(
        now_utc, runtime_bind, ttl_s=sc.DOCTOR_MEMO_TTL_S, compute=_compute,
    )


def _tui_precompute_envelope_config(raw_config: dict) -> dict:
    """Precompute the config / update-state reads `snapshot_to_envelope`
    needs (#268 M4, spec §6).

    Returns ``{config, update_state, update_suppress}``: the raw
    ``load_config()`` dict (the envelope layers the display-tz override on it
    purely and reads the alerts/budget/dashboard blocks off it) plus the
    update-state / update-suppress payloads with the SAME error sentinels the
    envelope used inline, so the derived ``update`` block is unchanged.
    """
    c = _cctally()
    try:
        update_state = c._load_update_state()
    except c.UpdateError:
        update_state = {"_error": "update-state.json invalid"}
    except Exception:
        update_state = {"_error": "update-state.json read failed"}
    try:
        update_suppress = c._load_update_suppress()
    except Exception:
        update_suppress = {"skipped_versions": [], "remind_after": None}
    return {
        "config": raw_config,
        "update_state": update_state,
        "update_suppress": update_suppress,
    }


def _tui_compute_dispatch_signature(stats_conn):
    """Composite data-version signature for the three-path dispatch (#268 M5.1).

    Cheap ``MAX(id)`` b-tree descents over cache.db + stats.db, the reset-event
    change-signal, and the module generation counter (spec §3). ``stats_conn``
    is the already-open stats.db connection the rebuild holds; a throwaway
    cache.db connection is opened for the ``session_entries`` / ``codex`` legs.
    ``compute_signature`` never raises (each leg degrades to 0 on a missing
    table); a cache-open failure propagates so the caller skips the idle path
    and does a full rebuild.
    """
    c = _cctally()
    sc = c._load_sibling("_lib_snapshot_cache")
    # Same gate as `_tui_build_source_bundle`, for the same reason: this is the
    # second caller of `codex_stats_digest`, whose relation table reads the two
    # projection tables from a pure kernel (#496 S5b section 4.7).
    from _cctally_quota import assert_projection_readable
    assert_projection_readable(stats_conn)
    cache_conn = c.open_cache_db()
    try:
        return sc.compute_signature(
            cache_conn,
            stats_conn,
            generation=sc.current_generation(),
            codex_stats_digest=codex_stats_digest(stats_conn),
            accounts_digest=accounts_identity_digest(stats_conn),
            claude_stats_digest=claude_stats_digest(stats_conn),
        )
    finally:
        cache_conn.close()


def _snapshot_period_rolled_over(prior, now_utc, display_tz):
    """True if a day/week/month boundary was crossed since ``prior`` was built.

    The idle short-circuit reuses ``prior``'s period rows, which is only valid
    while the current day/week/month is unchanged (spec §3). A day change in the
    display tz subsumes a month change (a new month starts on a new day), so
    daily+monthly reduce to one day-label compare. Weekly rollover is checked
    against the subscription week's absolute boundaries carried on
    ``prior.current_week`` (reset-anchored UTC datetimes) — a week can roll at a
    mid-day reset hour without the calendar day changing.

    The 5-hour block is also boundary-checked here (#268 M5 Finding 4): a 5h
    reset with zero new usage across a full window on a truly-idle dashboard
    advances no DB signature and crosses no calendar day, so without this leg the
    idle path would keep serving the prior "current block" past its reset. The
    reset-anchored UTC instant carried on ``prior.current_week.five_hour_resets_at``
    forces a full rebuild once ``now`` reaches it, re-anchoring the block.
    """
    prev = getattr(prior, "generated_at", None)
    if prev is None:
        return True

    def _day(dtm):
        # Day label in the resolved display tz — matches how the daily builder
        # buckets (display tz, host-local when None). Not a user-facing render,
        # only a same-day equality check.
        localized = dtm.astimezone(display_tz) if display_tz is not None \
            else dtm.astimezone()  # internal fallback: host-local intentional
        return localized.strftime("%Y-%m-%d")

    if _day(prev) != _day(now_utc):
        return True
    cw = getattr(prior, "current_week", None)
    if cw is not None:
        week_end = getattr(cw, "week_end_at", None)
        week_start = getattr(cw, "week_start_at", None)
        if week_end is not None and now_utc >= week_end:
            return True
        if week_start is not None and now_utc < week_start:
            return True
        # 5h block rollover (#268 M5 Finding 4): the reset instant is the
        # reset-anchored UTC end of the active block (already suppressed to None
        # by `_tui_build_current_week` once it has elapsed, so a non-None value is
        # a future boundary at build time). Once now crosses it, force a full
        # rebuild so the blocks panel re-anchors even with zero new usage.
        five_hour_reset = getattr(cw, "five_hour_resets_at", None)
        if five_hour_reset is not None and now_utc >= five_hour_reset:
            return True
    return False


def _tui_build_idle_snapshot(prior, *, now_utc, precompute_envelope,
                             runtime_bind, raw_config, errors,
                             display_tz_pref_override=None,
                             source_stats_conn=None,
                             source_display_tz_name=None,
                             source_display_tz: dt.tzinfo | None = None,
                             codex_ingest_contended=False,
                             codex_ingest_failed=False,
                             claude_ingest_contended=False,
                             claude_ingest_failed=False,
                             failures=None,
                             stats_heal_attempted=False):
    """Fresh snapshot reusing ``prior``'s heavy rows, re-patching only the
    time-derived fields + the doctor payload / envelope precompute on each idle
    tick (spec §3 idle path).

    ``dataclasses.replace`` builds a NEW ``DataSnapshot`` sharing ``prior``'s
    immutable row objects (never mutated in place — Codex F7), so an SSE client
    thread serializing the previously-published snapshot can't observe a torn
    value. ``generated_at`` moves to ``now_utc`` (drives the envelope's display
    block + the emitted timestamp); ``last_sync_at`` refreshes (the ingest ran,
    so "synced Xs ago" resets); the doctor payload is refreshed through the TTL
    memo so the doctor chip stays live on a long-idle dashboard (Codex F6). All
    the wall-clock countdowns / freshness ages are computed from a LIVE now at
    envelope-render time (the SSE loop passes its own ``now_utc`` /
    ``time.monotonic()``), so nothing else needs patching here.

    ``envelope_precompute`` is ALSO refreshed each idle tick (#268 M5 Finding 1):
    since M4 the envelope reads update-state / update-suppress off the snapshot's
    ``envelope_precompute`` rather than re-reading the JSON files per client.
    ``_DashboardUpdateCheckThread`` writes ``update-state.json`` OUT OF BAND
    (advancing no DB signature and no config render key), so carrying
    ``prior.envelope_precompute`` forward would freeze the version banner on a
    long-idle / --no-sync dashboard until new usage or a config edit lands. This
    mirrors the doctor-on-idle treatment (spec §6): config is render-key-stable on
    the idle path (the short-circuit only runs when the render key — which bundles
    the full raw config — is unchanged), so recomputing is cheap small-JSON I/O,
    not CPU, and byte-matches a full rebuild's envelope for the same state.
    """
    import time
    doctor_payload = prior.doctor_payload
    envelope_precompute = prior.envelope_precompute
    if precompute_envelope:
        try:
            doctor_payload = _tui_precompute_doctor_payload(now_utc, runtime_bind)
        except Exception as exc:  # noqa: BLE001 — never crash the rebuild
            errors.append(f"doctor-precompute: {exc}")
        try:
            envelope_precompute = _tui_precompute_envelope_config(raw_config)
        except Exception as exc:  # noqa: BLE001 — never crash the rebuild
            errors.append(f"envelope-precompute: {exc}")
    idle_failures = failures if failures is not None else []
    source_bundle = prior.source_bundle
    if source_bundle is not None:
        try:
            prior_claude = source_bundle.sources["claude"]
            prior_codex = source_bundle.sources["codex"]
            # #350 spec §3.3: once the Codex cycle decision deadline has passed
            # the idle clock is no longer entitled to speak for the cycle — its
            # public-history view cannot re-resolve it — so fall through to the
            # bounded source-adapter path, which rebuilds Codex authoritatively.
            if (
                _tui_source_bundle_can_idle(source_bundle)
                and not codex_decision_deadline_passed(prior_codex, now_utc)
            ):
                claude = _refresh_claude_source_clock(
                    prior_claude,
                    current_week=prior.current_week,
                    now_utc=now_utc,
                    raw_config=raw_config,
                )
                codex = refresh_codex_source_clock(prior_codex, now_utc=now_utc)
                if claude is not prior_claude or codex is not prior_codex:
                    source_bundle = SourceDashboardBundle(
                        source_schema_version=source_bundle.source_schema_version,
                        default_source=source_bundle.default_source,
                        source_order=source_bundle.source_order,
                        sources={
                            "claude": claude,
                            "codex": codex,
                            "all": compose_all_state(claude, codex),
                        },
                    )
            elif source_stats_conn is not None:
                # A provider can become coherent without changing the global
                # data signature (notably after its post-projection certificate
                # is written). Re-run only the bounded source adapter while
                # preserving the already-idle legacy snapshot rows.
                source_snapshot = prior
                if source_snapshot.doctor_payload is None:
                    source_snapshot = dataclasses.replace(
                        source_snapshot,
                        doctor_payload={
                            "severity": "fail",
                            "counts": {"ok": 0, "warn": 0, "fail": 1},
                            "generated_at": now_utc.isoformat(),
                            "fingerprint": "source-projection",
                        },
                    )
                legacy_envelope = _cctally().snapshot_to_envelope(
                    source_snapshot,
                    now_utc=now_utc,
                    display_tz_pref_override=display_tz_pref_override,
                    runtime_bind=runtime_bind,
                )
                source_bundle = _tui_build_source_bundle(
                    stats_conn=source_stats_conn,
                    now_utc=now_utc,
                    display_tz_name=source_display_tz_name,
                    codex_ingest_contended=codex_ingest_contended,
                    codex_ingest_failed=codex_ingest_failed,
                    claude_ingest_contended=claude_ingest_contended,
                    claude_ingest_failed=claude_ingest_failed,
                    claude_cost_usd=prior.daily_total_cost_usd,
                    claude_total_tokens=prior.daily_total_tokens,
                    claude_data=_tui_project_claude_source_data(legacy_envelope),
                    common_range_start=_tui_common_source_range_start(
                        prior.daily_panel,
                        now_utc=now_utc,
                        display_tz=source_display_tz,
                    ),
                    projects_envelope=prior.projects_envelope,
                    prior_bundle=source_bundle,
                    raw_config=raw_config,
                )
        except _StatsSnapshotCorruption:
            raise
        except QuotaProjectionIncomplete as exc:
            # #496 S5b §4.7, same reason as the two handlers in
            # `_tui_build_snapshot_once`: the branch below classifies against
            # the connection that faulted and would report a stats-or-cache
            # fault, which is the wrong cause and the wrong remedy.
            errors.append(f"quota-projection: {exc}")
            idle_failures.append(SyncFailureAttribution(
                leg="quota-projection", database="other", corruption=False,
            ))
            source_bundle = prior.source_bundle
        except Exception as exc:  # noqa: BLE001 — retain prior complete bundle
            # #496 S3 §8 (F16). This branch read stats through
            # `source_stats_conn` and swallowed the failure into a plain
            # string, so it built no `SyncFailureAttribution`, never raised
            # `_StatsSnapshotCorruption`, and never reached the heal — and
            # `_sync_failure_envelope`, finding no typed attribution, fell to
            # its raw-text matcher and told the user to run
            # `cctally cache-sync --rebuild` for a STATS fault. Classify it at
            # the catch site instead, against the connection that faulted.
            if source_stats_conn is not None:
                _tui_capture_sync_failure(
                    source_stats_conn,
                    errors,
                    idle_failures,
                    leg="source-clock-refresh",
                    database="stats_or_cache",
                    exc=exc,
                    stats_heal_attempted=stats_heal_attempted,
                )
            else:
                errors.append(f"source-clock-refresh: {exc}")
            source_bundle = prior.source_bundle
    return dataclasses.replace(
        prior,
        generated_at=now_utc,
        # #583 S2 §6.1: `last_sync_at` means "last SUCCESSFUL validation". A
        # clean idle tick IS one — it re-verified through four independent
        # gates that nothing changed, so the reused rows are genuinely
        # current. A tick that recorded an error retains the prior stamp
        # rather than reporting itself as a fresh success.
        last_sync_at=(time.monotonic() if not errors
                      else getattr(prior, "last_sync_at", None)),
        last_sync_error=("; ".join(errors) if errors else None),
        sync_failures=tuple(idle_failures),
        doctor_payload=doctor_payload,
        envelope_precompute=envelope_precompute,
        source_bundle=source_bundle,
        # #278 §1.4.1: an idle snapshot means the data-version signature is
        # unchanged (data stable) → force the hydration latch clear even if
        # ``prior`` was a hydrating seed/partial.
        hydrating=False,
    )


def _tui_empty_snapshot(now_utc: dt.datetime) -> DataSnapshot:
    """First-paint placeholder with no data loaded yet."""
    return DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None, generated_at=now_utc,
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )


def _stats_open_failure_is_corruption(exc: BaseException) -> bool:
    """Keep epoch deferral distinct from heal deferral and read failures."""
    c = _cctally()
    if isinstance(exc, c.StatsRebuildDeferred):
        return isinstance(exc, c.StatsHealDeferred)
    return True


def _tui_stats_retry_degraded_snapshot(
    *,
    now_utc: dt.datetime,
    exc: BaseException,
    precompute_envelope: bool,
    runtime_bind: "str | None",
) -> DataSnapshot:
    """Return a stable typed frame when the one fresh retry cannot open."""

    errors = [f"stats-open: {exc}"]
    doctor_payload = None
    envelope_precompute = None
    if precompute_envelope:
        try:
            envelope_precompute = _tui_precompute_envelope_config(load_config())
        except Exception as precompute_exc:  # noqa: BLE001
            errors.append(f"envelope-precompute: {precompute_exc}")
        try:
            doctor_payload = _tui_precompute_doctor_payload(now_utc, runtime_bind)
        except Exception as doctor_exc:  # noqa: BLE001
            errors.append(f"doctor-precompute: {doctor_exc}")
    return dataclasses.replace(
        _tui_empty_snapshot(now_utc),
        # #583 S2 §6.1: this produced no successful snapshot, and it builds
        # from the empty snapshot, so there is no earlier success to preserve.
        last_sync_at=None,
        last_sync_error="; ".join(errors),
        sync_failures=(
            SyncFailureAttribution(
                leg="stats-open",
                database="stats",
                corruption=_stats_open_failure_is_corruption(exc),
            ),
        ),
        doctor_payload=doctor_payload,
        envelope_precompute=envelope_precompute,
        hydrating=False,
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
                # Carry every additive view-model scalar through verbatim so
                # the prior frame's panel rows and their envelope totals stay
                # consistent. Bundle 1 / #56 / #57 / #59 each added envelope
                # scalars the React panels now trust over a client-side
                # ``rows.reduce``; without preserving them here, a sync crash
                # leaves populated rows next to a ``$0.00`` footer (the
                # dataclass defaults kick in for any field not explicitly
                # passed). The structural-equality invariant
                # ``total === sum(visible rows).cost_usd`` must survive a
                # crash recovery, not just the happy path.
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
                    # Carry the navigation index forward on a sync crash so the
                    # hero modal's week chip stays usable (spec §3 idle-carry).
                    week_index=prev.week_index,
                    weekly_history=prev.weekly_history,
                    weekly_periods=prev.weekly_periods,
                    monthly_periods=prev.monthly_periods,
                    blocks_panel=prev.blocks_panel,
                    daily_panel=prev.daily_panel,
                    daily_total_cost_usd=prev.daily_total_cost_usd,
                    daily_total_tokens=prev.daily_total_tokens,
                    monthly_total_cost_usd=prev.monthly_total_cost_usd,
                    monthly_total_tokens=prev.monthly_total_tokens,
                    weekly_total_cost_usd=prev.weekly_total_cost_usd,
                    weekly_total_tokens=prev.weekly_total_tokens,
                    blocks_total_cost_usd=prev.blocks_total_cost_usd,
                    blocks_total_tokens=prev.blocks_total_tokens,
                    trend_avg_dollars_per_pct=prev.trend_avg_dollars_per_pct,
                    trend_history_median_dpp=prev.trend_history_median_dpp,
                    forecast_view=prev.forecast_view,
                    # #268 M4 (Codex F6): carry the precomputed doctor payload
                    # + config/update-state forward so the envelope stays a
                    # pure renderer (and the doctor chip stays populated)
                    # across a transient sync crash, instead of dropping to the
                    # dataclass defaults and re-forking `security` per client.
                    doctor_payload=prev.doctor_payload,
                    envelope_precompute=prev.envelope_precompute,
                    source_bundle=prev.source_bundle,
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
    # Spec §5.4 — credit badge next to the 5h percent. Source: same
    # ``cw.five_hour_block.credits`` channel that drives the dashboard
    # chip; only show when at least one credit is present for the
    # current block. Format: ``⚡ -Xpp`` (single) / ``⚡ -Xpp, -Ypp``
    # (stacked across distinct 10-min slots).
    fh_credit_badge = ""
    fhb = getattr(cw, "five_hour_block", None)
    if isinstance(fhb, dict):
        fh_credits = fhb.get("credits") or []
        if fh_credits:
            deltas = ", ".join(
                f"{float(c.get('delta_pp', 0.0)):+.0f}pp"
                for c in fh_credits
            )
            fh_credit_badge = f" {{bright}}⚡ {deltas}{{/}}"
    lines = [
        "",
        f" Used   {{{used_cls}}}{bar_fill}{{/}} {{{used_cls}.b}}{cw.used_pct:>5.1f}%{{/}}",
        "",
        f" 5-hour {{bar.accent}}{five_bar}{{/}} {{bright}}{int(five):>3d}%{{/}}{fh_credit_badge}",
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

    # Spec §5.4 — credit badge in the hero variant. Same source as the
    # grid variant; append after the reset suffix so the badge follows
    # the "resets in" timer.
    fhb_hero = getattr(cw, "five_hour_block", None)
    if isinstance(fhb_hero, dict):
        fh_credits_hero = fhb_hero.get("credits") or []
        if fh_credits_hero:
            deltas_hero = ", ".join(
                f"{float(c.get('delta_pp', 0.0)):+.0f}pp"
                for c in fh_credits_hero
            )
            reset_suffix = f"{reset_suffix}  {{bright}}⚡ {deltas_hero}{{/}}"

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
    # Preview channel (CCTALLY_CHANNEL=preview): a marker-gated PREVIEW segment
    # so a preview-channel TUI (running against a real-data snapshot) is
    # unmistakable next to the live prod one. Reuses the existing `{warn}` style
    # token, so no `_TUI_VALID_STYLE_NAMES` change is needed. Empty when the
    # marker is unset → the TUI goldens stay byte-identical.
    preview_prefix = ""
    if _cctally_core.is_preview_channel():
        preview_prefix = "{warn}PREVIEW{/} {faint}│{/} "
    if cw is None:
        hdr = preview_prefix + (
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
        hdr = preview_prefix + (
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
    # #620 S1 D5/A5: `TuiCurrentWeek.dollars_per_percent` is `None` when no
    # usage has been observed. `or 0.0` printed `avg $/1% $0.00`, which is a
    # measured-looking answer to a question nothing measured. Every other TUI
    # site already renders that absence as an em dash; this one did not.
    avg_dpp_str = (
        f"${cw.dollars_per_percent:.2f}"
        if cw.dollars_per_percent is not None else "—"
    )
    cumul = cw.spent_usd
    header = [
        "",
        f"  {{dim}}Week{{/}} {{b}}{format_display_dt(cw.week_start_at, runtime.display_tz, fmt='%b %d', suffix=False)} – {format_display_dt(cw.week_end_at, runtime.display_tz, fmt='%b %d', suffix=False)}{{/}}   "
        f"{{dim}}milestones reached{{/}} {{warn.b}}{len(milestones)}{{/}}",
        f"  {{dim}}avg $/1%{{/}} {{b}}{avg_dpp_str}{{/}}     {{dim}}cumulative{{/}} {{b}}${cumul:.2f}{{/}}",
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
        elif b.pct_headroom is None:
            lines.append(f"  {{dim}}  ≤{b.target_percent}%   {{/}}{{dim}}—  (already past){{/}}")
        else:
            # #620 S1 D5: `dollars_per_day` is also None when the $/1% rate
            # itself is withheld, and there the target has NOT been passed —
            # there is headroom and no rate to price it with. Naming the
            # wrong cause is the defect this session removes.
            lines.append(f"  {{dim}}  ≤{b.target_percent}%   {{/}}{{dim}}—  (no rate observed){{/}}")
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
    avg_dpp = stable_sum(h.dollars_per_percent for h in valid) / len(valid) if valid else 0.0
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


# ── #278 Theme A (A2): progressive first-run ingest fill ─────────────────────
# A first-run / long-gap dashboard sync walks many files and takes seconds to
# tens-of-seconds; before this it published exactly once at the end (empty →
# single jump). A2 wires the existing ``sync_cache(progress=…)`` seam to a
# THROTTLED partial republish so the heavy panels fill progressively. It is
# self-limiting to first-run: a warm returning user's sync finishes under T, so
# the throttle never fires and A2 is a pure no-op (exactly one publish). Not
# user-configurable (YAGNI).
_A2_PARTIAL_THROTTLE_S = 2.0  # T


class _A2ThrottleClock:
    """Completion-measured throttle for A2 partial republishes.

    ``should_fire(now)`` is True at most once per ``interval_s``, measured from
    the LAST fire's COMPLETION — call ``mark_done(now)`` after the partial build
    finishes so a slow partial self-spaces rather than stacking (spec §2.2).
    Seeded at the sync's START, so the first partial waits a full interval and a
    sub-T warm sync fires zero partials.
    """
    __slots__ = ("_interval", "_last")

    def __init__(self, interval_s: float, *, start: float):
        self._interval = interval_s
        self._last = start

    def should_fire(self, now: float) -> bool:
        return (now - self._last) >= self._interval

    def mark_done(self, now: float) -> None:
        self._last = now


def _make_a2_progress_cb(*, ref, hub, build_partial, throttle, monotonic,
                         perf=_perf):
    """Build the A2 throttled ``sync_cache`` progress callback (extracted so the
    throttle + isolation logic is unit-testable with injected deps).

    On proceed it builds a partial via ``build_partial()`` (a fresh, complete
    ``skip_sync=True`` snapshot over the current committed cache — NOT nested in
    another build) and stores the clean non-hydrating object on ``ref``, then
    publishes a ``hydrating=True`` COPY over the hub (§1.4.1 shared-object-leak:
    only the publish carries the latch, so the memo's retained object stays
    clean). The dispatch-memo write itself happens inside ``build_partial()`` /
    ``_tui_build_snapshot`` (its snapshot-cache reconcile step), NOT in this cb.

    #583 S1 §2.1: this used to return early whenever perf tracing was active,
    which meant arming the trace silently switched progressive fill off. The
    hazard that justified the suppression is real but narrower than a blanket
    skip — the partial build's unconditional ``_perf.reset_thread()`` rebinds
    ``_tls.stack`` while the enclosing ``sync_cache`` still holds its ``walk``
    phase, whose ``Phase._stack`` is the original list, so the outer phase
    would later close into a detached or fragmented root. Isolating the partial
    build's thread state addresses exactly that, and the publication now
    happens whether or not tracing is on.
    """
    def cb(_stats) -> None:
        now = monotonic()
        if not throttle.should_fire(now):
            return
        with perf.isolated_thread_state():
            snap = build_partial()
        # #583 S2 §6.2 publication point 3: publish what the reference now
        # HOLDS, not the local build. A request accepted while `build_partial`
        # ran is on the reference and absent from `snap`, so publishing `snap`
        # would erase its counter and the client would never settle.
        snap = ref.set(snap)
        hub.publish(dataclasses.replace(snap, hydrating=True))
        throttle.mark_done(monotonic())
    return cb


def _make_run_sync_now_locked(*, ref, hub, pinned_now, display_tz_pref_override,
                              runtime_bind=None):
    """Return a closure that does the snapshot-rebuild + SSE-publish work.

    Caller MUST hold sync_lock around the call. The naming convention
    (``_locked`` suffix) is the contract; threading.Lock has no
    "is_held_by_current_thread" check, so we don't introspect.

    Splitting the locked body out of the public wrapper lets ``/api/sync``
    callers that already hold ``sync_lock`` (e.g. so they can refresh OAuth
    + rebuild snapshot atomically without releasing between steps) reuse
    this body without recursive-acquire / self-deadlock.

    ``runtime_bind`` (#268 M4) is the dashboard's actual bound host; this is a
    DASHBOARD-only factory, so every rebuild here precomputes the envelope's
    doctor / config / update-state onto the snapshot (``precompute_envelope=True``)
    — keeping ``snapshot_to_envelope`` a pure renderer across all SSE clients.

    #278 A2 (§2.1): the ``skip_sync=False`` path DECOUPLES the ingest from the
    build — it runs ``sync_cache`` STANDALONE (with a throttled progress cb),
    then builds the final snapshot with ``skip_sync=True``. Routing the ingest
    through ``_tui_build_snapshot`` instead would be unsafe: that function
    unconditionally ``_perf.reset_thread()``s (corrupting the trace §0 fixes),
    writes dispatch state, and mutates single-writer accelerator caches — so a
    re-entrant partial build from inside its ``sync`` phase would corrupt all
    three. The decoupled final build reads the same post-sync cache and computes
    the same post-sync dispatch signature, so it is byte-identical to today's
    ``_tui_build_snapshot(skip_sync=False)``. The ``skip_sync=True`` path (POST
    /api/settings) stays the single-build path.
    """
    def _build(skip_sync: bool):
        # Resolve _tui_build_snapshot via cctally's namespace so the eager
        # re-export AND ``monkeypatch.setitem(ns, "_tui_build_snapshot", spy)``
        # in tests propagate into this closure body (a bare-name lookup would
        # resolve in this sibling's __dict__ and miss the cctally-side patch).
        #
        # #583 S1 §1.3: one of the TWO builder-span sites. This wrapper is
        # dashboard-local, so `tui --render-once` and `cctally-snapshot-measure`
        # never reach it and take the span at `_tui_build_snapshot`'s standalone
        # boundary instead. The span subtracts any ingest nested inside it.
        tick = _tick_stats.current()
        if tick is None:
            return sys.modules["cctally"]._tui_build_snapshot(
                now_utc=pinned_now, skip_sync=skip_sync,
                display_tz_pref_override=display_tz_pref_override,
                precompute_envelope=True, runtime_bind=runtime_bind,
            )
        with tick.build_span():
            return sys.modules["cctally"]._tui_build_snapshot(
                now_utc=pinned_now, skip_sync=skip_sync,
                display_tz_pref_override=display_tz_pref_override,
                precompute_envelope=True, runtime_bind=runtime_bind,
            )

    def _locked(skip_sync: bool) -> None:
        # #279 S5 F6.3 (gate P1-1): arm the snapshot-cache owner-thread tripwire
        # for whichever thread holds sync_lock for THIS rebuild — the periodic
        # sync thread, or a /api/sync / /api/settings request thread. Overwrite-
        # on-call, so ownership transfers to the current rebuilder; the guards in
        # _lib_snapshot_cache then catch a lock-bypassing foreign-thread mutation.
        _cctally()._load_sibling("_lib_snapshot_cache").mark_owner_thread()
        # #583 S1 §1.2: the tick opens here and closes after the final publish,
        # so the progressive, final, deferred and crash publication paths are
        # all inside it. The A1 pre-bind seed is deliberately outside, because
        # it bypasses `_tui_build_snapshot` and writes no dispatch state.
        tick = _tick_stats.begin_tick()
        try:
            if not skip_sync:
                # ── Decoupled ingest + build (§2.1) ─────────────────────────
                import time as _time
                sync_error = None
                # #583 S2 §6.1: the last SUCCESSFUL validation, read before
                # A2's partial republishes can overwrite the held snapshot.
                prior_sync_at = ref.get().last_sync_at
                start = _time.monotonic()
                throttle = _A2ThrottleClock(_A2_PARTIAL_THROTTLE_S, start=start)
                cb = _make_a2_progress_cb(
                    ref=ref, hub=hub,
                    build_partial=lambda: _build(skip_sync=True),
                    throttle=throttle, monotonic=_time.monotonic,
                )
                cache_conn = _cctally().open_cache_db()
                cache_mod = _cctally()._load_sibling("_cctally_cache")
                # #583 S1 §1.3: the ingest span, opened through the
                # contextmanager protocol rather than a `with` block so the
                # long try/except/finally below is not reindented. The A2
                # progress callback runs `build_partial()` SYNCHRONOUSLY inside
                # this region, so the builder spans it opens nest here and
                # their time is subtracted — without that, every progress build
                # is counted once as ingest and again as builder and
                # `ingest_ns + builder_ns` can exceed `duration_ns`. A span
                # left open by an escaping exception is closed by
                # `TickContext.finish`, so no try/finally is needed to bound it.
                _ingest = tick.ingest_span()
                _ingest.__enter__()
                try:
                    # Under CCTALLY_PERF_TRACE the phase tree this standalone
                    # sync_cache builds is intentionally NOT surfaced in the live
                    # dashboard trace: the final _build(skip_sync=True) below runs
                    # _tui_build_snapshot, which resets the thread perf stack, so
                    # these phases never reach an emitter. §0's trace-attribution
                    # target is `cctally-bench --trace`, which builds directly
                    # (no decoupled standalone ingest) and keeps its phase tree.
                    # Dashboard S4's physical identity includes both providers.
                    # They are one recovery plan because quarantine replaces
                    # the shared physical family: corruption in the second leg
                    # must restart the first leg too.
                    _, cache_conn = cache_mod._run_cache_plan_with_recovery(
                        cache_conn,
                        (
                            lambda active_conn: sync_cache(
                                active_conn, progress=cb
                            ),
                            lambda active_conn: sync_codex_cache(active_conn),
                        ),
                        origins=(
                            "dashboard.refresh.claude_sync",
                            "dashboard.refresh.codex_sync",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — surfaced on the snap
                    sync_error = f"sync-cache: {exc}"
                finally:
                    cache_conn.close()
                _ingest.__exit__(None, None, None)
                # #583 S1 §2.2: the ONE place a pending trace-arm request is
                # consumed. After the cache connection closes and immediately
                # before the authoritative build, so a request landing
                # mid-ingest cannot split one ingest across two tracing states,
                # and A2 partial builds never consume it.
                _perf.apply_pending()
                snap = _build(skip_sync=True)  # final: hydrating=False (default)
                if sync_error is not None:
                    # Thread the standalone sync error into last_sync_error, sync
                    # error FIRST (mirrors the internal path where the `sync`
                    # phase's error is errors[0]) so the surface is unchanged.
                    existing = snap.last_sync_error
                    merged = (sync_error if not existing
                              else f"{sync_error}; {existing}")
                    snap = dataclasses.replace(snap, last_sync_error=merged)
                # #583 S2 §6.1: `last_sync_at` means "last SUCCESSFUL
                # validation", and `_tui_build_snapshot` stamps it
                # unconditionally. A build that recorded any failure carries
                # the prior successful value forward instead of reporting
                # itself as freshly synced.
                if snap.last_sync_error:
                    snap = dataclasses.replace(snap, last_sync_at=prior_sync_at)
                # #583 S2 §6.2 publication point 4: publish what the reference
                # holds, so a request accepted during the build is not erased.
                # `set_final` also clears `rebuilding` in the same acquisition,
                # so this ONE frame reports the rebuild finished; the loop's
                # trailing `mark_rebuilding(False)` then finds no transition and
                # publishes nothing. That is what keeps an automatic tick at two
                # frames instead of three, PLUS one per A2 progress publish that
                # cleared the throttle above — several on a cold first-run
                # ingest, none on a warm one.
                snap = ref.set_final(snap)
                _tui_publish_final(tick, hub, snap)
                return
            # ── skip_sync=True: single-build path (POST /api/settings) ──────
            _perf.apply_pending()   # §2.2: the no-ingest build's arm boundary
            snap = _build(skip_sync=True)
            # Mirror the startup override: suppress the monotonic sync stamp so
            # the envelope keeps emitting sync_age_s=None and the client keeps
            # rendering "sync paused" after the user hits r / clicks the sync
            # chip. #278 §1.4.1: force the hydration latch clear (default False
            # from _tui_build_snapshot, restated here since this is a replace()
            # clone site).
            snap = dataclasses.replace(snap, last_sync_at=None, hydrating=False)
            # Terminal for this branch too, for the same reason: POST
            # /api/settings brackets its rebuild with the same flag pair.
            snap = ref.set_final(snap)
            _tui_publish_final(tick, hub, snap)
        except _cctally().StatsRebuildDeferred as exc:
            # #453: the first periodic tick runs before HTTP bind. Preserve the
            # initial hydrating/degraded frame while the dedicated replay owns
            # stats maintenance; a generic crash frame would clear the latch
            # before any client could observe it. The loop retries normally on
            # its next cadence and publishes a full frame after convergence.
            #
            # #496 S3: the two deferral classes must NOT flatten here. A wrong
            # EPOCH is a readable index (`corruption=False`); a deferred heal is
            # an index that could not be read, and reporting that as
            # non-corruption makes the envelope name the wrong fault.
            prev = ref.get()
            pending = dataclasses.replace(
                prev,
                last_sync_error=f"stats-open: {exc}",
                sync_failures=(
                    SyncFailureAttribution(
                        leg="stats-open",
                        database="stats",
                        corruption=_stats_open_failure_is_corruption(exc),
                    ),
                ),
                generated_at=dt.datetime.now(dt.timezone.utc),
                hydrating=True,
            )
            pending = ref.set(pending)   # #583 S2 §6.2: publish what is held
            tick.mark_degraded()
            _tui_publish_final(tick, hub, pending, publication="degraded")
        except Exception as exc:
            prev = ref.get()
            crashed = dataclasses.replace(
                prev,
                last_sync_error=f"sync crashed: {exc}",
                # The new crash supersedes the prior typed leg failures. Keeping
                # them would let a stale stats attribution win this unrelated
                # failure in the privacy-safe envelope classifier.
                sync_failures=(),
                generated_at=dt.datetime.now(dt.timezone.utc),
                # #278 §1.4.1: a crash-carry snapshot is stable (not mid-
                # assembly); clear the latch even if ``prev`` was a hydrating
                # seed/partial, so the client doesn't stay stuck in skeletons.
                hydrating=False,
            )
            crashed = ref.set(crashed)   # #583 S2 §6.2: publish what is held
            tick.mark_degraded()
            _tui_publish_final(tick, hub, crashed, publication="degraded")
        finally:
            # A tick always closes. Every publication path above finishes it,
            # and `finish` is idempotent, so this only catches an escape none
            # of them handled — a BaseException such as KeyboardInterrupt.
            if not tick.finished:
                tick.mark_degraded()
                tick.finish(
                    published_ns=time.monotonic_ns(),
                    published_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                )
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
        # #276 perf: flush the snapshot phase tree to stderr when tracing is on
        # (the rendered frame still goes to stdout only). No-op when off.
        if _perf.enabled():
            _perf.flush_stderr(_perf.current_root())

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
