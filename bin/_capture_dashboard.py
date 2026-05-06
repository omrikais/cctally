#!/usr/bin/env python3
"""Playwright helper for the public README's dashboard screenshots.

Driven by bin/build-readme-screenshots.sh after the orchestrator has
started `cctally dashboard`. Reads a small declarative table of
(name, viewport, post_open_action) and produces one PNG per row in
docs/img/.

Selector contract (verified against dashboard/web/src/):
  - panels expose data-panel-kind="<panel-id>" (NOT data-panel-id)
  - modals render under #modal-root .modal-card; the orchestrator only
    opens one modal per shot, so no kind-disambiguation is required.

Not run in CI. Requires the `playwright` Python package and pre-installed
browsers (`playwright install chromium`).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8789/"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "img"


@dataclass(frozen=True)
class Shot:
    name: str  # output basename without extension
    viewport: dict  # {"width": int, "height": int}
    is_mobile: bool
    post_open_action: Optional[Callable]  # called with `page` after navigation
    wait_for_selector: str  # selector that signals "page ready to screenshot"
    # Optional CSS selector — when set, capture only that element rather
    # than the full page (used for modal-only shots so the surrounding
    # desktop panels and dimmed backdrop are excluded).
    screenshot_element: Optional[str] = None
    # Whether to capture the entire scroll-height of the page. Mobile
    # marketing shot uses False so the screenshot is a single
    # phone-screen-height window (not a 7000+ px scroll).
    full_page: bool = True
    # Extra prefs to merge into the dashboard's localStorage prefs blob
    # before the page loads. Reserved for future per-shot pref overrides;
    # the panelOrder reconciler in
    # dashboard/web/src/lib/reconcilePanelOrder.ts re-appends any
    # canonical panel missing from saved state, so panel-visibility is
    # controlled via `hide_panels` (CSS injection) instead.
    extra_prefs: Optional[dict] = None
    # Panel ids (PanelHost data-panel-host values) to hide via injected
    # CSS — used to drop the Recent Alerts panel from the marketing
    # shots without forking the dashboard's prefs schema.
    hide_panels: tuple = ()


# Marketing-shot panels we hide via injected CSS so the dashboard
# overview stays focused on the 8 load-bearing panels. We can't drop
# panels via prefs.panelOrder alone — the reconciler in
# dashboard/web/src/lib/reconcilePanelOrder.ts re-appends any canonical
# panel id missing from saved state. CSS injection on the PanelHost's
# data-panel-host attribute side-steps that without forking the
# dashboard schema.
_HIDE_ALERTS_PANEL = ("alerts",)


def _click_trend_panel(page) -> None:
    """Open the Trend modal so dashboard-modal.png shows the 12-week history."""
    page.click('[data-panel-kind="trend"]')
    page.wait_for_selector('#modal-root .modal-card', timeout=5000)


def _click_forecast_panel(page) -> None:
    """Open the Forecast modal so dashboard-warn.png shows the WARN verdict."""
    page.click('[data-panel-kind="forecast"]')
    page.wait_for_selector('#modal-root .modal-card', timeout=5000)


def _shots() -> list[Shot]:
    return [
        Shot(
            name="dashboard-desktop",
            viewport={"width": 1440, "height": 900},
            is_mobile=False,
            post_open_action=None,
            wait_for_selector='[data-panel-kind="current-week"]',
            hide_panels=_HIDE_ALERTS_PANEL,
            # Default state for both panels is collapsed (max-height
            # 480/520px with internal scroll); the marketing shot wants
            # them fully expanded so the Daily heatmap shows all 30
            # cells uncropped and the Blocks fuel-gauge rows align to
            # the same height as Daily on the bottom row.
            extra_prefs={"blocksCollapsed": False, "dailyCollapsed": False},
        ),
        Shot(
            name="dashboard-modal",
            viewport={"width": 1440, "height": 900},
            is_mobile=False,
            post_open_action=_click_trend_panel,
            wait_for_selector='[data-panel-kind="current-week"]',
            screenshot_element="#modal-root .modal-card",
            hide_panels=_HIDE_ALERTS_PANEL,
        ),
        Shot(
            name="dashboard-mobile",
            viewport={"width": 393, "height": 852},  # iPhone 14 Pro CSS px
            is_mobile=True,
            post_open_action=None,
            wait_for_selector='[data-panel-kind="current-week"]',
            full_page=False,  # single-viewport, not full-scroll
            hide_panels=_HIDE_ALERTS_PANEL,
        ),
        Shot(
            name="dashboard-warn",
            viewport={"width": 1440, "height": 900},
            is_mobile=False,
            post_open_action=_click_forecast_panel,
            wait_for_selector='[data-panel-kind="current-week"]',
            screenshot_element="#modal-root .modal-card",
            # Hide alerts panel for parity with the desktop shot. The
            # forecast modal renders ON TOP of the panel grid; the
            # modal-only screenshot won't show panels at all, but the
            # panel grid still needs to render properly behind the
            # modal during page load — same prefs keeps that consistent
            # with the desktop pass.
            hide_panels=_HIDE_ALERTS_PANEL,
        ),
    ]


def capture_all(*, dashboard_url: str, out_dir: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "_capture_dashboard: playwright not installed.\n"
            "  install with: pip install playwright\n"
            "  then:         playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for shot in _shots():
                ctx_kwargs = {
                    "viewport": shot.viewport,
                    "device_scale_factor": 2,  # retina-ish output
                    "is_mobile": shot.is_mobile,
                    "has_touch": shot.is_mobile,
                }
                ctx = browser.new_context(**ctx_kwargs)
                # Pre-set the dashboard's prefs so OnboardingToast doesn't
                # appear in shots. The dashboard keys localStorage at
                # "ccusage.dashboard.prefs" (see
                # dashboard/web/src/store/store.ts:18 PREFS_KEY).
                # add_init_script runs before each page load in the
                # context, so by the time page.goto(...) triggers React
                # initialization, the prefs are already in localStorage
                # and OnboardingToast returns null on first render.
                #
                # Per-shot extra_prefs (e.g. panelOrder without "alerts")
                # are JSON-encoded in Python and merged into the same
                # PREFS blob so the marketing shots hide panels we don't
                # want to feature.
                extra_prefs_json = json.dumps(shot.extra_prefs or {})
                init_script = (
                    "const PREFS_KEY = 'ccusage.dashboard.prefs';\n"
                    "const existing = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');\n"
                    "localStorage.setItem(PREFS_KEY, JSON.stringify({\n"
                    "    ...existing,\n"
                    "    onboardingToastSeen: true,\n"
                    "    mobileOnboardingToastSeen: true,\n"
                    f"    ...{extra_prefs_json},\n"
                    "}));\n"
                )
                ctx.add_init_script(init_script)
                page = ctx.new_page()
                try:
                    # NOT "networkidle": the dashboard's persistent /api/events
                    # SSE stream (15s keep-alive ticks) resets the 500ms
                    # idle timer indefinitely. wait_for_selector below is
                    # the actual readiness signal.
                    page.goto(dashboard_url, wait_until="domcontentloaded")
                    page.wait_for_selector(shot.wait_for_selector, timeout=10_000)
                    # Disable first-mount stagger animations. The Daily
                    # panel staggers cell fade-in by 30ms × index across
                    # 30 cells (~900ms total); screenshotting before the
                    # last cell becomes visible drops most cells from
                    # the captured frame. Animations are decorative for
                    # live UX, irrelevant for static marketing shots.
                    page.add_style_tag(content=(
                        ".daily-cell.first-mount { "
                        "animation: none !important; opacity: 1 !important; "
                        "}"
                    ))
                    if shot.hide_panels:
                        # Inject a CSS rule to hide each named PanelHost
                        # (data-panel-host=<id>) AFTER the dashboard has
                        # rendered its panel grid. Adding the style tag
                        # via add_style_tag is safe to do post-mount
                        # (Playwright appends to <head>) and the rule
                        # takes effect synchronously before the next
                        # paint, so the screenshot below sees the
                        # collapsed grid.
                        css = "\n".join(
                            f'[data-panel-host="{pid}"] {{ display: none !important; }}'
                            for pid in shot.hide_panels
                        )
                        page.add_style_tag(content=css)
                    if shot.post_open_action:
                        shot.post_open_action(page)
                        page.wait_for_timeout(500)  # let modal animation settle
                    out_path = out_dir / f"{shot.name}.png"
                    # When screenshot_element is set, capture ONLY that
                    # element's bounding box (modal-only shots) — this
                    # excludes the dimmed backdrop and surrounding
                    # desktop panels. Otherwise screenshot the page;
                    # full_page=True captures the entire scroll-height
                    # (desktop overview), full_page=False the single
                    # viewport window (mobile shot — keeps the file
                    # small and shows what fits on a phone).
                    if shot.screenshot_element:
                        page.locator(shot.screenshot_element).screenshot(path=str(out_path))
                    else:
                        page.screenshot(path=str(out_path), full_page=shot.full_page)
                    print(f"wrote: {out_path}")
                finally:
                    ctx.close()
        finally:
            browser.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--url", default=DEFAULT_DASHBOARD_URL,
        help="dashboard URL (default: %(default)s)",
    )
    p.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR),
        help="output directory (default: %(default)s)",
    )
    args = p.parse_args()
    capture_all(dashboard_url=args.url, out_dir=Path(args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
