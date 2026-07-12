// #281 S3 — shared helpers for the reader smoke net.
//
// Waits discipline (spec §6): ZERO fixed sleeps. Every wait is condition-based.
// `settleScroller` is the two-tier settle helper — a base tier of
// (scrollTop, scrollHeight) stable across N consecutive animation frames, and,
// for jump/reveal/anchor scenarios, the mounted virtual-index range AND a named
// anchor's viewport rect top too (a scrollTop-only settle can read "stable" while
// Virtuoso's mounted range or the target rect is still moving — the reader's own
// layoutStable.ts contract). All under a bounded wall-clock budget.
import { appendFileSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page } from '@playwright/test';

const HERE = dirname(fileURLToPath(import.meta.url));
const RUNTIME = resolve(HERE, '.runtime');

export interface Manifest {
  long_session_id: string;
  long_turn_count: number;
  long_last_uuid: string;
  jump_target_uuid: string;
  jump_target_needle: string;
  below_giants_jump_target_uuid: string;
  below_giants_jump_target_index: number;
  below_giants_jump_target_needle: string;
  single_page_session_id: string;
  single_first_uuid: string;
  single_last_uuid: string;
  sidechain_session_id: string;
  sidechain_anchor_uuid: string;
  sidechain_subagent_key: string;
  sidechain_member_count: number;
  reveal_late_member_uuid: string;
  reveal_late_member_index: number;
  reveal_late_needle: string;
  live_session_id: string;
  live_jsonl_path: string;
  live_last_uuid: string;
  live_append_template: string;
  project_dir: string;
  cwd: string;
  page_size: number;
}

/** Read the fixture manifest the launcher's builder wrote into e2e/.runtime/. */
export function loadManifest(): Manifest {
  return JSON.parse(readFileSync(resolve(RUNTIME, 'manifest.json'), 'utf8'));
}

/** The reader's virtualized scroll surface (Virtuoso's own scroller). */
export const READER_BODY = '.conv-reader-body';

export interface SettleOptions {
  /** consecutive stable animation frames that declare a settle (default 5). */
  frames?: number;
  /** wall-clock ceiling in ms; reject past it (default 8000). */
  budgetMs?: number;
  /** additionally require this element's rounded viewport-rect top to be stable
   *  (a jump/anchor/reveal scenario). Pass a full CSS selector. */
  anchorSel?: string;
}

/**
 * Resolve once the scroller has settled. The in-page rAF loop tracks
 * (scrollTop, scrollHeight), the mounted `.conv-reader-item[data-item-index]`
 * range (min/max), and — when `anchorSel` is given — that element's rounded
 * `getBoundingClientRect().top`. Stable for `frames` consecutive frames → done;
 * rejects past `budgetMs`.
 */
export async function settleScroller(
  page: Page,
  sel: string = READER_BODY,
  opts: SettleOptions = {},
): Promise<void> {
  const { frames = 5, budgetMs = 8000, anchorSel = null } = opts;
  await page.evaluate(
    ({ sel, frames, budgetMs, anchorSel }) => {
      const scroller = document.querySelector(sel) as HTMLElement | null;
      if (!scroller) throw new Error(`settleScroller: no element for ${sel}`);
      const start = performance.now();
      const snap = () => {
        const items = Array.from(
          document.querySelectorAll('.conv-reader-item[data-item-index]'),
        ).map((e) => Number(e.getAttribute('data-item-index'))).filter(Number.isFinite);
        let anchorTop: number | null = null;
        if (anchorSel) {
          const a = document.querySelector(anchorSel) as HTMLElement | null;
          anchorTop = a ? Math.round(a.getBoundingClientRect().top) : null;
        }
        return {
          top: Math.round(scroller.scrollTop),
          h: Math.round(scroller.scrollHeight),
          first: items.length ? Math.min(...items) : null,
          last: items.length ? Math.max(...items) : null,
          anchorTop,
        };
      };
      return new Promise<void>((resolvePromise, reject) => {
        let prev = snap();
        let stable = 0;
        const tick = () => {
          if (performance.now() - start > budgetMs) {
            reject(new Error(`settleScroller: budget ${budgetMs}ms exceeded`));
            return;
          }
          const cur = snap();
          const same =
            cur.top === prev.top &&
            cur.h === prev.h &&
            cur.first === prev.first &&
            cur.last === prev.last &&
            cur.anchorTop === prev.anchorTop;
          stable = same ? stable + 1 : 0;
          prev = cur;
          if (stable >= frames) {
            resolvePromise();
            return;
          }
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      });
    },
    { sel, frames, budgetMs, anchorSel },
  );
}

export interface WheelUpOptions {
  /** trusted-wheel delta per step, px (default 6000). */
  stepPx?: number;
  /** wall-clock ceiling in ms; give up past it (default 25000). */
  budgetMs?: number;
}

/**
 * Trusted-wheel the reader upward, settling after each step, until `done()`
 * returns true or a wall-clock budget expires; returns whether `done` was met.
 *
 * The step COUNT is deliberately NOT fixed. Virtuoso's `startReached` fires the
 * next reverse `?before=` page only once the scroll reaches the HEAD of the
 * mounted window, and that window's height is machine-dependent: on a slower
 * renderer the reader auto-prefetches one reverse page during the initial mount,
 * so the mounted window is a full extra PAGE tall before the first wheel, and a
 * fixed number of steps that reaches the head on a fast local box falls short on
 * the CI runner (proven on ubuntu-latest chromium: a fixed 24/40-step loop
 * stopped ~100–250 items shy of the head, so the next `?before=` never fired).
 * Budgeting the wall clock instead of the steps makes the trigger robust across
 * renderers with zero sleeps and zero retries — every wait is still condition-
 * based (`settleScroller` between steps yields real frames).
 */
export async function wheelUpUntil(
  page: Page,
  done: () => boolean | Promise<boolean>,
  opts: WheelUpOptions = {},
): Promise<boolean> {
  const { stepPx = 6000, budgetMs = 25_000 } = opts;
  await page.locator(READER_BODY).hover();
  const deadline = Date.now() + budgetMs;
  if (await done()) return true;
  while (Date.now() < deadline) {
    await page.mouse.wheel(0, -stepPx);
    await settleScroller(page);
    if (await done()) return true;
  }
  return false;
}

// Monotonic append counter so successive live turns get distinct uuids, and a
// per-file parent chain so the appended turns thread correctly.
let appendCounter = 0;
const lastUuidByPath = new Map<string, string>();

/**
 * Append one templated assistant turn to the live-tail fixture file the watch
 * loop tracks, returning the new turn's uuid. The parent chains off the last
 * known uuid (seeded from the manifest) so document order stays coherent.
 */
export function appendLiveTurn(manifest: Manifest): string {
  const path = manifest.live_jsonl_path;
  const parent = lastUuidByPath.get(path) ?? manifest.live_last_uuid;
  const n = ++appendCounter;
  const uuid = `e2elive-append-0000-0000-${String(n).padStart(12, '0')}`;
  const ts = new Date(Date.UTC(2026, 5, 2, 0, 0, n)).toISOString();
  const line = manifest.live_append_template
    .split('__UUID__').join(uuid)
    .split('__PARENT__').join(parent)
    .split('__TS__').join(ts);
  appendFileSync(path, line + '\n');
  lastUuidByPath.set(path, uuid);
  return uuid;
}

/** A `[data-uuid="…"]` selector, CSS-escaped for the deterministic fixture ids. */
export function uuidSel(uuid: string): string {
  return `[data-uuid="${uuid}"]`;
}

/** Scroller geometry: scrollTop / scrollHeight / clientHeight + the at-bottom gap. */
export async function scrollerMetrics(page: Page, sel: string = READER_BODY) {
  return page.evaluate((s) => {
    const b = document.querySelector(s) as HTMLElement;
    return {
      top: Math.round(b.scrollTop),
      height: Math.round(b.scrollHeight),
      client: b.clientHeight,
      gap: Math.round(b.scrollHeight - b.scrollTop - b.clientHeight),
    };
  }, sel);
}

// The reader leaves a small trailing margin below the last turn even when parked
// at the bottom (measured ~73–79px, inside Virtuoso's atBottomThreshold=80), so
// "at bottom" means the gap is within this slack — clearly separable from a
// scrolled-up position (hundreds–thousands of px).
export const AT_BOTTOM_SLACK = 100;

/** True when the turn with `uuid` is mounted AND its rect intersects the
 *  scroller's viewport rect (vertically) — a real "visible inside the reader"
 *  check that survives the virtualized list. */
export async function turnVisibleInReader(page: Page, uuid: string): Promise<boolean> {
  return page.evaluate(
    ({ uuid, sel }) => {
      const b = document.querySelector(sel) as HTMLElement | null;
      const el = document.querySelector(`[data-uuid="${uuid}"]`) as HTMLElement | null;
      if (!b || !el) return false;
      const br = b.getBoundingClientRect();
      const er = el.getBoundingClientRect();
      return er.bottom > br.top + 1 && er.top < br.bottom - 1;
    },
    { uuid, sel: READER_BODY },
  );
}

/** Open a conversation at the tail via the URL hash route (no deep-link jump). */
export async function openConversation(page: Page, sessionId: string): Promise<void> {
  await page.goto(`/#/conversations/${encodeURIComponent(sessionId)}`);
}

/** Arm a MutationObserver that latches if the turn `uuid` ever gets the
 *  `conv-item--jumped` flash class (or an inner element does). Robust to the
 *  transient ~2s pulse — install BEFORE the jump, read with `flashWasSeen`.
 *
 *  Watches CLASS-attribute mutations ONLY (no `childList`): the flash class is
 *  applied to the already-mounted target element (the walk mounts it, then
 *  landedBookkeeping sets the jumped uuid → a class change on that node), and
 *  `subtree` observation extends to nodes mounted after arming. `childList` here
 *  would fire the callback on EVERY node add/remove during a big cold drain
 *  (thousands of mounts, each re-running a full-document querySelector), which
 *  starves the drain/landing past the test budget — a pure test-harness cost, not
 *  a product regression (#281 S5 B3). The arm-time `check()` catches an
 *  already-present flash. */
export async function armFlashWatch(page: Page, uuid: string): Promise<void> {
  await page.evaluate((u) => {
    const w = window as unknown as { __flashSeen?: boolean; __flashObs?: MutationObserver };
    w.__flashSeen = false;
    const check = () => {
      const el = document.querySelector(`[data-uuid="${u}"]`);
      if (el && (el.classList.contains('conv-item--jumped') || el.querySelector('.conv-item--jumped'))) {
        w.__flashSeen = true;
      }
    };
    w.__flashObs = new MutationObserver(check);
    w.__flashObs.observe(document.body, {
      subtree: true, attributes: true, attributeFilter: ['class'],
    });
    check();
  }, uuid);
}

/** True if the armed flash watcher latched (the target flashed at some point). */
export async function flashWasSeen(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as unknown as { __flashSeen?: boolean }).__flashSeen === true);
}
