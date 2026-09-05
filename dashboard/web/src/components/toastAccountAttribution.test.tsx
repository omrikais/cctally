// #700 — every alert-toast family names its account; #693 D2 — the toast's
// rate-change chip carries the same severity modifier the modal's does.
//
// Both defects were "one surface out of several". The Recent Alerts modal
// already renders `.alert-account-chip` for both families and already renders
// `chip chip-rate-change severity-<severity>`; the toast rendered neither, so
// one event read amber in the toast and red in the modal, and a decorated
// install could not tell which account a toast belonged to.
//
// The three families are covered SEPARATELY on purpose. Claude threshold and
// Codex threshold share a head today, but that is an implementation fact the
// tests must not depend on: #700 was filed precisely because a change can ship
// having fixed one family, and a test that exercises only the shared path
// cannot see that.
import { act, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Toast } from './Toast';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { collectToastAlertRows } from '../lib/alertIdentity';
import { _peekFocusOriginForTests } from '../lib/toastFocus';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { AlertsConfig } from '../store/store';
import type {
  Envelope,
  MeterRateChangeEntry,
  SourceAlertRow,
} from '../types/envelope';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
  weekly_usd: null,
};

// The label is deliberately not a substring of anything else the toast prints,
// so `textContent` can be searched for it directly in the absence cases.
const ACCOUNT_LABEL = 'work-account';

function claudeRow(over: Partial<SourceAlertRow> = {}): SourceAlertRow {
  return {
    source: 'claude',
    key: 'alert:claude:0:weekly:90',
    id: 'weekly:2026-08-31:90:0',
    axis: 'weekly',
    threshold: 90,
    crossed_at: '2026-09-02T12:00:00Z',
    alerted_at: '2026-09-02T12:00:00Z',
    context: { week_start_date: '2026-08-31' },
    ...over,
  } as SourceAlertRow;
}

function codexRow(over: Partial<SourceAlertRow> = {}): SourceAlertRow {
  return {
    source: 'codex',
    key: 'alert:codex:codex_budget:calendar-month:100',
    axis: 'codex_budget',
    period: 'calendar-month',
    threshold: 100,
    value: 105,
    created_at: '2026-09-02T12:00:00Z',
    ...over,
  } as SourceAlertRow;
}

// A Codex `quota` row is the one alert shape that renders NO
// `.toast--alert-body`: it carries a severity and nothing to state a value
// from, so `CodexToastBody` renders a title alone.
function codexQuotaRow(over: Partial<SourceAlertRow> = {}): SourceAlertRow {
  return {
    source: 'codex',
    key: 'alert:codex:quota:95',
    axis: 'quota',
    threshold: 95,
    severity: 'warn',
    created_at: '2026-09-02T12:00:00Z',
    ...over,
  } as SourceAlertRow;
}

function rateChange(over: Partial<MeterRateChangeEntry> = {}): MeterRateChangeEntry {
  return {
    id: 'meter_rate_change:claude:unattributed:2026-08-25T00:00:00+00:00',
    family: 'meter_rate_change',
    provider: 'claude',
    owner: 'claude',
    severity: 'alarm',
    effective_from: '2026-08-25T00:00:00+00:00',
    detected_at: '2026-08-29T12:00:00+00:00',
    recorded_at: '2026-08-29T12:00:00+00:00',
    previous_units_per_point: 2_442_620,
    new_units_per_point: 1_685_000,
    ...over,
  };
}

// A threshold toast surfaces on the SECOND tick: the first seeds the forward-
// only seen-set so a cold start does not replay history.
function surfaceThreshold(row: SourceAlertRow): void {
  act(() => dispatch({
    type: 'INGEST_SOURCE_ALERTS', rows: [], alertsSettings: CONFIG, isFirstTick: true,
  }));
  act(() => dispatch({
    type: 'INGEST_SOURCE_ALERTS', rows: [row], alertsSettings: CONFIG, isFirstTick: false,
  }));
}

function envWith(changes: MeterRateChangeEntry[]): Envelope {
  const slice = makeSourceEnvelope();
  const claude = makeClaudeSourceEntry({
    data: { ...slice.sources.claude.data!, alerts: { rows: [] } },
  });
  const codex = makeCodexSourceEntry({
    data: { ...slice.sources.codex.data!, alerts: { rows: [] } },
  });
  return {
    header: { used_pct: 11 },
    alerts: [],
    alerts_settings: CONFIG,
    meter_rate_changes: changes,
    ...makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    }),
  } as unknown as Envelope;
}

function seedRateChange(changes: MeterRateChangeEntry[], firstTick: boolean): void {
  const snap = envWith(changes);
  act(() => {
    if (updateSnapshot(snap)) {
      dispatch({
        type: 'INGEST_SOURCE_ALERTS',
        rows: collectToastAlertRows(snap),
        rateChanges: changes,
        alertsSettings: snap.alerts_settings ?? CONFIG,
        isFirstTick: firstTick,
      });
    }
  });
}

function surfaceRateChange(entry: MeterRateChangeEntry): void {
  seedRateChange([], true);
  seedRateChange([entry], false);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('#693 D2 — the toast rate-change chip carries its severity', () => {
  it('reddens the chip on an alarm transition, as the modal already does', () => {
    surfaceRateChange(rateChange({ severity: 'alarm' }));
    const { container } = render(<Toast />);
    const chip = container.querySelector('.chip-rate-change');
    expect(chip).not.toBeNull();
    expect(chip!.className).toContain('severity-alarm');
  });

  it('dims the chip on an info transition', () => {
    surfaceRateChange(rateChange({ severity: 'info' }));
    const { container } = render(<Toast />);
    expect(
      container.querySelector('.chip-rate-change')!.className,
    ).toContain('severity-info');
  });
});

describe('#700 — every toast family names its account when the wire says so', () => {
  it('the Claude threshold toast renders the account chip after the source chip', () => {
    surfaceThreshold(claudeRow({
      accountKey: 'acct-work', accountLabel: ACCOUNT_LABEL,
    } as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const chip = container.querySelector('.toast--alert-head .alert-account-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toBe(ACCOUNT_LABEL);
    expect(chip!.getAttribute('title')).toBe(ACCOUNT_LABEL);
    // Head order: axis chip, source chip, THEN the account chip.
    const head = container.querySelector('.toast--alert-head')!;
    const order = [...head.children].map((el) => el.className.split(/\s+/)[0]);
    expect(order.indexOf('alert-account-chip')).toBeGreaterThan(
      order.indexOf('source-chip'),
    );
  });

  it('the Codex threshold toast renders the account chip after the source chip', () => {
    surfaceThreshold(codexRow({
      accountKey: 'acct-work', accountLabel: ACCOUNT_LABEL,
    } as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const chip = container.querySelector('.toast--alert-head .alert-account-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toBe(ACCOUNT_LABEL);
    const head = container.querySelector('.toast--alert-head')!;
    const order = [...head.children].map((el) => el.className.split(/\s+/)[0]);
    expect(order.indexOf('alert-account-chip')).toBeGreaterThan(
      order.indexOf('source-chip'),
    );
  });

  it('the metering-rate toast renders the account chip after the source chip', () => {
    surfaceRateChange(rateChange({
      accountKey: 'acct-work', accountLabel: ACCOUNT_LABEL,
    }));
    const { container } = render(<Toast />);
    const toast = container.querySelector('[data-testid="toast-meter-rate-change"]')!;
    const chip = toast.querySelector('.toast--alert-head .alert-account-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toBe(ACCOUNT_LABEL);
    const head = toast.querySelector('.toast--alert-head')!;
    const order = [...head.children].map((el) => el.className.split(/\s+/)[0]);
    expect(order.indexOf('alert-account-chip')).toBeGreaterThan(
      order.indexOf('source-chip'),
    );
  });
});

describe('#700 D5 — an undecorated install is unchanged', () => {
  it('the Claude threshold toast renders no chip and no account text', () => {
    surfaceThreshold(claudeRow());
    const { container } = render(<Toast />);
    expect(container.querySelector('.alert-account-chip')).toBeNull();
    expect(container.textContent).not.toContain(ACCOUNT_LABEL);
    expect(container.textContent).not.toContain('All accounts');
  });

  it('the Codex threshold toast renders no chip and no account text', () => {
    surfaceThreshold(codexRow());
    const { container } = render(<Toast />);
    expect(container.querySelector('.alert-account-chip')).toBeNull();
    expect(container.textContent).not.toContain(ACCOUNT_LABEL);
    expect(container.textContent).not.toContain('All accounts');
  });

  it('the metering-rate toast renders no chip and no account text', () => {
    surfaceRateChange(rateChange());
    const { container } = render(<Toast />);
    const toast = container.querySelector('[data-testid="toast-meter-rate-change"]');
    expect(toast).not.toBeNull();
    expect(toast!.querySelector('.alert-account-chip')).toBeNull();
    expect(toast!.textContent).not.toContain(ACCOUNT_LABEL);
    expect(toast!.textContent).not.toContain('All accounts');
  });
});

// #749 — the dismiss hint is not dropped when the head names an account; it
// moves to its own line BELOW the head.
//
// #700 dropped it because the head could not carry both. The toast is a fixed
// 360px with no @media rule of its own, leaving about 326px of content width,
// and with an account chip present the hint wrapped onto two or three lines on
// every decorated install — the R8 sentinel labels `All accounts` and
// `Unattributed` are twelve characters, so that was the common case rather
// than an edge one. Dropping it left a decorated install with NO statement of
// how to dismiss, which #749 filed. Its own line costs a measured +17.9px
// (149.4px to 167.3px at 390px) against 652.7px of headroom, and nothing
// constrains it: `max-height` none, `max-width` none, parent `overflow:
// visible`.
//
// The two strings differ DELIBERATELY. R8 byte-stability forbids editing what
// a single-account install renders, so the undecorated head keeps the literal
// `click to dismiss` in its current position, and only the new own-line form
// is viewport-neutral (`tap or click to dismiss`). The undecorated cases below
// are the guard on acceptance criterion 10 and are unchanged from #700.
describe('#749 — the dismiss hint moves out of the head instead of vanishing', () => {
  const DECORATED = { accountKey: 'acct-work', accountLabel: ACCOUNT_LABEL };
  const hint = (c: HTMLElement) => c.querySelector('.toast--alert-dismiss-hint');
  const chip = (c: HTMLElement) => c.querySelector('.alert-account-chip');

  function expectOwnLine(container: HTMLElement): void {
    expect(chip(container)).not.toBeNull();
    const el = hint(container);
    expect(el).not.toBeNull();
    expect(el!.textContent).toBe('tap or click to dismiss');
    expect(el!.closest('.toast--alert-head')).toBeNull();
    // #750 S2 review — and BELOW the alert, not between the head and the
    // title. The root is a `role="alert"` live region, so DOM order IS the
    // announcement order: with the hint above the title a screen reader read
    // "tap or click to dismiss" before it read which alert had fired.
    //
    // The anchor is the last block the alert's OWN content renders, which is
    // not always `.toast--alert-body`. A Codex `quota` row carries no value to
    // state, so `CodexToastBody` renders a title and no body at all; keying
    // this helper on the body alone made a decorated quota toast throw here
    // instead of being checked, which is what the review found.
    const anchor = container.querySelector('.toast--alert-body')
      ?? container.querySelector('.toast--alert-title');
    expect(anchor, 'the toast rendered no body and no title to place the hint after').not.toBeNull();
    expect(
      anchor!.compareDocumentPosition(el!) & Node.DOCUMENT_POSITION_FOLLOWING,
      "the own-line hint must come after the alert's own content",
    ).toBeTruthy();
  }

  it('renders it on its own line on a decorated Claude threshold toast', () => {
    surfaceThreshold(claudeRow(DECORATED as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    expectOwnLine(container);
  });

  it('keeps it on an undecorated Claude threshold toast', () => {
    surfaceThreshold(claudeRow());
    const { container } = render(<Toast />);
    expect(chip(container)).toBeNull();
    expect(hint(container)!.textContent).toBe('click to dismiss');
    expect(hint(container)!.closest('.toast--alert-head')).not.toBeNull();
  });

  it('renders it on its own line on a decorated Codex threshold toast', () => {
    surfaceThreshold(codexRow(DECORATED as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    expectOwnLine(container);
  });

  it('keeps it on an undecorated Codex threshold toast', () => {
    surfaceThreshold(codexRow());
    const { container } = render(<Toast />);
    expect(chip(container)).toBeNull();
    expect(hint(container)!.textContent).toBe('click to dismiss');
    expect(hint(container)!.closest('.toast--alert-head')).not.toBeNull();
  });

  it('renders it on its own line on a decorated Codex quota toast, which has no body', () => {
    surfaceThreshold(codexQuotaRow(DECORATED as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    expect(
      container.querySelector('.toast--alert-body'),
      'a Codex quota toast must still render no body, or this case proves nothing',
    ).toBeNull();
    expectOwnLine(container);
  });

  it('renders it on its own line on a decorated metering-rate toast', () => {
    surfaceRateChange(rateChange(DECORATED));
    const { container } = render(<Toast />);
    expectOwnLine(container);
  });

  it('keeps it on an undecorated metering-rate toast', () => {
    surfaceRateChange(rateChange());
    const { container } = render(<Toast />);
    expect(chip(container)).toBeNull();
    expect(hint(container)!.textContent).toBe('click to dismiss');
    expect(hint(container)!.closest('.toast--alert-head')).not.toBeNull();
  });
});

// #749 — the toast is dismissible from the keyboard, not only by pointer.
//
// Both roots are plain `div`s carrying `onClick`. Making such an element
// focusable is NOT sufficient: unlike a native button, a focusable div does
// not synthesize a click for Enter or Space, so `tabIndex` plus `aria-label`
// would leave the toast focusable and still not dismissible. The requirement
// is explicit activation semantics — focusable, labelled, and activated by
// Enter and by Space through the same dismiss path the click uses.
describe('#749 — both toast roots activate from the keyboard', () => {
  const ROOTS: Array<[string, () => void, string]> = [
    ['threshold', () => surfaceThreshold(claudeRow()), '.toast--alert'],
    [
      'metering-rate',
      () => surfaceRateChange(rateChange()),
      '[data-testid="toast-meter-rate-change"]',
    ],
  ];

  it.each(ROOTS)('the %s toast root is focusable and labelled', (_name, surface, sel) => {
    surface();
    const { container } = render(<Toast />);
    const root = container.querySelector(sel) as HTMLElement;
    expect(root.getAttribute('tabindex')).toBe('0');
    expect(root.getAttribute('aria-label')).toBeTruthy();
  });

  it.each(ROOTS)('the %s toast dismisses on Enter', async (_name, surface, sel) => {
    surface();
    const { container } = render(<Toast />);
    (container.querySelector(sel) as HTMLElement).focus();
    await userEvent.keyboard('{Enter}');
    expect(container.querySelector(sel)).toBeNull();
  });

  it.each(ROOTS)('the %s toast dismisses on Space', async (_name, surface, sel) => {
    surface();
    const { container } = render(<Toast />);
    (container.querySelector(sel) as HTMLElement).focus();
    await userEvent.keyboard(' ');
    expect(container.querySelector(sel)).toBeNull();
  });

  it.each(ROOTS)('the %s toast does not steal focus when it arrives', (_name, surface, sel) => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    surface();
    const { container } = render(<Toast />);
    expect(container.querySelector(sel)).not.toBeNull();
    expect(document.activeElement).toBe(input);
    input.remove();
  });

  // #750 S2 review — the root handler must not claim keys aimed at a control
  // INSIDE the toast.
  //
  // `keydown` bubbles, and the threshold toast carries a native `<button>`
  // (`AlertFollowCell`). With the root handler running on any bubbled keydown
  // and calling `preventDefault()`, a person who tabbed to "Open this block"
  // and pressed Enter had the button's own activation cancelled — no `click`
  // was ever synthesized — while the root dismissed the toast. The alert
  // vanished and nothing opened. `stopPropagation` on the button's `onClick`
  // guards the pointer path only; there is no keydown guard there.
  //
  // A `five_hour` row is used because its target resolves from
  // `block_start_at` alone: `alertNavigation` reaches it before the liveness
  // test and reads no envelope, so the follow button renders without a store
  // snapshot.
  describe('a key pressed on the follow button reaches the button', () => {
    const BLOCK_START = '2026-09-02T10:00:00Z';

    function surfaceFollowable(): HTMLElement {
      surfaceThreshold(claudeRow({
        id: 'five_hour:2026-09-02T10:00:00Z:90',
        key: 'alert:claude:0:five_hour:90',
        axis: 'five_hour',
        context: { block_start_at: BLOCK_START },
      } as Partial<SourceAlertRow>));
      return render(<Toast />).container;
    }

    it('renders the follow button, so the two cases below are not vacuous', () => {
      const container = surfaceFollowable();
      expect(container.querySelector('.alert-row-open')).not.toBeNull();
      expect(getState().openModal).toBeNull();
    });

    it('opens the block on Enter rather than swallowing it', async () => {
      const container = surfaceFollowable();
      (container.querySelector('.alert-row-open') as HTMLElement).focus();
      await userEvent.keyboard('{Enter}');
      expect(getState().openModal).toBe('block');
      expect(getState().openBlockStartAt).toBe(BLOCK_START);
    });

    it('opens the block on Space rather than swallowing it', async () => {
      const container = surfaceFollowable();
      (container.querySelector('.alert-row-open') as HTMLElement).focus();
      await userEvent.keyboard(' ');
      expect(getState().openModal).toBe('block');
      expect(getState().openBlockStartAt).toBe(BLOCK_START);
    });

    // #750 S2 review — `AlertFollowCell` has TWO focusable controls and the
    // guard above was exercised on only one of them. When the primary target
    // cannot be resolved the cell renders `.alert-row-explain` instead, which
    // opens the window diagnosis; a root handler that claimed bubbled keys
    // would swallow that one identically.
    //
    // A `five_hour` row carrying `five_hour_window_key` and NO `block_start_at`
    // is the reachable shape: `scopeFiveHour` recovers the window from the
    // reset key, so the scope is available and an explain target exists, while
    // `_targetFor` withholds the block target because the row names no block.
    // Neither branch reads a clock or an envelope.
    describe('and the same holds for the diagnosis control beside it', () => {
      function surfaceExplainable(): HTMLElement {
        surfaceThreshold(claudeRow({
          id: 'five_hour:key:90',
          key: 'alert:claude:0:five_hour_key:90',
          axis: 'five_hour',
          context: { five_hour_window_key: 1_788_000_000 },
        } as Partial<SourceAlertRow>));
        return render(<Toast />).container;
      }

      it('renders the explain button, so the two cases below are not vacuous', () => {
        const container = surfaceExplainable();
        expect(container.querySelector('.alert-row-open')).toBeNull();
        expect(container.querySelector('.alert-row-explain')).not.toBeNull();
        expect(getState().openModal).toBeNull();
      });

      it('opens the diagnosis on Enter rather than swallowing it', async () => {
        const container = surfaceExplainable();
        (container.querySelector('.alert-row-explain') as HTMLElement).focus();
        await userEvent.keyboard('{Enter}');
        expect(getState().openModal).toBe('explain');
      });

      it('opens the diagnosis on Space rather than swallowing it', async () => {
        const container = surfaceExplainable();
        (container.querySelector('.alert-row-explain') as HTMLElement).focus();
        await userEvent.keyboard(' ');
        expect(getState().openModal).toBe('explain');
      });
    });
  });

  // #750 S2 review — dismissing from the keyboard must not orphan the caret.
  //
  // Removing the focused element sends `document.activeElement` to <body>, so
  // the reader's next Tab restarts at the top of the document rather than
  // beside whatever they were reading when the toast arrived.
  it.each(ROOTS)('the %s toast returns focus where it came from', async (_name, surface, sel) => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    surface();
    const { container } = render(<Toast />);
    (container.querySelector(sel) as HTMLElement).focus();
    await userEvent.keyboard('{Enter}');
    expect(container.querySelector(sel)).toBeNull();
    expect(document.activeElement).toBe(input);
    input.remove();
  });

  // #750 S2 review — the three ways that restoration was still wrong.
  describe('and it restores the right element, on every dismiss path', () => {
    const BLOCK_START = '2026-09-02T10:00:00Z';

    function surfaceWithFollowButton(): HTMLElement {
      surfaceThreshold(claudeRow({
        id: 'five_hour:2026-09-02T10:00:00Z:90',
        key: 'alert:claude:0:five_hour:90',
        axis: 'five_hour',
        context: { block_start_at: BLOCK_START },
      } as Partial<SourceAlertRow>));
      return render(<Toast />).container;
    }

    // Tab order inside a threshold toast is root, then follow button, so
    // Shift-Tab from the button back to the root arrives with `relatedTarget`
    // pointing INSIDE the toast. Recorded, that element is still
    // `isConnected` when Enter dismisses — React has not flushed the unmount —
    // so focus landed on the follow button and then dropped to <body> as it
    // unmounted, which is the defect restoration exists to remove.
    it('never records an element the toast itself contains', async () => {
      const input = document.createElement('input');
      document.body.appendChild(input);
      input.focus();
      const container = surfaceWithFollowButton();
      const root = container.querySelector('.toast--alert') as HTMLElement;
      const follow = container.querySelector('.alert-row-open') as HTMLElement;
      expect(follow, 'no follow button, so the containment case is vacuous').not.toBeNull();
      root.focus();
      follow.focus();
      root.focus(); // Shift-Tab back: relatedTarget is the follow button
      await userEvent.keyboard('{Enter}');
      expect(container.querySelector('.toast--alert')).toBeNull();
      expect(document.activeElement).toBe(input);
      input.remove();
    });

    it('returns focus after a pointer dismissal, not only a key press', async () => {
      const input = document.createElement('input');
      document.body.appendChild(input);
      input.focus();
      surfaceThreshold(claudeRow());
      const { container } = render(<Toast />);
      const root = container.querySelector('.toast--alert') as HTMLElement;
      root.focus();
      await userEvent.click(root);
      expect(container.querySelector('.toast--alert')).toBeNull();
      expect(document.activeElement).toBe(input);
      input.remove();
    });

    it('returns focus when the 8-second expiry retires the toast', () => {
      vi.useFakeTimers();
      try {
        const input = document.createElement('input');
        document.body.appendChild(input);
        input.focus();
        surfaceThreshold(claudeRow());
        const { container } = render(<Toast />);
        (container.querySelector('.toast--alert') as HTMLElement).focus();
        act(() => { vi.advanceTimersByTime(8000); });
        expect(container.querySelector('.toast--alert')).toBeNull();
        expect(document.activeElement).toBe(input);
        input.remove();
      } finally {
        vi.useRealTimers();
      }
    });

    // The reader tabbed INTO the toast and then moved on somewhere else. The
    // expiry must not pull them back out of wherever they went.
    it('forgets the element once focus leaves the toast for somewhere else', () => {
      vi.useFakeTimers();
      try {
        const input = document.createElement('input');
        const later = document.createElement('input');
        document.body.append(input, later);
        input.focus();
        surfaceThreshold(claudeRow());
        const { container } = render(<Toast />);
        (container.querySelector('.toast--alert') as HTMLElement).focus();
        later.focus();
        act(() => { vi.advanceTimersByTime(8000); });
        expect(container.querySelector('.toast--alert')).toBeNull();
        expect(document.activeElement).toBe(later);
        input.remove();
        later.remove();
      } finally {
        vi.useRealTimers();
      }
    });
  });
});

// #750 S2 review — the remembered element is module state, and module state a
// test reset cannot reach is cross-test state that leaks silently. It also
// pinned a detached element and its whole subtree for the life of the tab
// whenever a dismissal path did not clear it.
describe('#750 S2 — the toast forgets where focus came from', () => {
  it('is cleared by the store reset the suite already runs between tests', () => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    surfaceThreshold(claudeRow());
    const { container, unmount } = render(<Toast />);
    (container.querySelector('.toast--alert') as HTMLElement).focus();
    expect(_peekFocusOriginForTests()).toBe(input);
    _resetForTests();
    expect(_peekFocusOriginForTests()).toBeNull();
    unmount();
    input.remove();
  });

  it('is cleared when the component unmounts', () => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    surfaceThreshold(claudeRow());
    const { container, unmount } = render(<Toast />);
    (container.querySelector('.toast--alert') as HTMLElement).focus();
    expect(_peekFocusOriginForTests()).toBe(input);
    unmount();
    expect(_peekFocusOriginForTests()).toBeNull();
    input.remove();
  });
});

// #750 S2 review — the accessible name says what the alert says.
//
// `role="alert"` takes its name from the author, so an `aria-label` carrying
// only the dismiss instruction REPLACED the toast's content as its accessible
// name: a person who deliberately focused the toast was told how to close it
// and never which threshold on which account had fired. The arrival
// announcement still read the content, so this degraded the focus path alone —
// but that is the path the keyboard affordance exists to serve.
describe('#750 S2 — a focused toast announces the alert, not only how to close it', () => {
  const DECORATED = { accountKey: 'acct-work', accountLabel: ACCOUNT_LABEL };

  // #750 S2 review — these two named the axis and the provider in their titles
  // and asserted neither, so deleting the axis from the label left both green.
  // Each now pins the whole sentence the visible title states, which is what
  // the label is derived from.
  it('the threshold toast names its provider, its axis, its threshold and its account', () => {
    surfaceThreshold(claudeRow(DECORATED as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const label = container.querySelector('.toast--alert')!.getAttribute('aria-label')!;
    expect(label).toContain('Claude');
    expect(label).toContain('Weekly usage 90% reached');
    expect(label).toContain(ACCOUNT_LABEL);
    expect(label).toMatch(/Enter or Space to dismiss/);
  });

  it('the metering-rate toast names the provider and the account', () => {
    surfaceRateChange(rateChange(DECORATED));
    const { container } = render(<Toast />);
    const label = container
      .querySelector('[data-testid="toast-meter-rate-change"]')!
      .getAttribute('aria-label')!;
    expect(label).toContain('Claude metering rate changed');
    expect(label).toContain(ACCOUNT_LABEL);
    expect(label).toMatch(/Enter or Space to dismiss/);
  });

  // #750 S2 review — the label said "reached" on an axis that explicitly does
  // not claim it. `projected`'s whole point is the distinction between having
  // crossed a threshold and being forecast to cross it, and the visible title
  // states that distinction while the label contradicted it.
  it('says "projected to reach" on the projected axis, never "reached"', () => {
    surfaceThreshold(claudeRow({
      id: 'projected:2026-08-31:95',
      key: 'alert:claude:0:projected:95',
      axis: 'projected',
      threshold: 95,
      context: { week_start_date: '2026-08-31' },
    } as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const root = container.querySelector('.toast--alert')!;
    const label = root.getAttribute('aria-label')!;
    expect(root.querySelector('.toast--alert-title')!.textContent)
      .toBe('Projected to reach 95%');
    expect(label).toContain('Projected to reach 95%');
    expect(label).not.toMatch(/95% reached/);
  });

  // The smaller version of the same defect: the title names the project and
  // the label did not.
  it('names the project the title names on a project-budget alert', () => {
    surfaceThreshold(claudeRow({
      id: 'project_budget:2026-08-31:90',
      key: 'alert:claude:0:project_budget:90',
      axis: 'project_budget',
      threshold: 90,
      context: { project: 'cctally-dev', spent_usd: 9, budget_usd: 10 },
    } as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const root = container.querySelector('.toast--alert')!;
    expect(root.querySelector('.toast--alert-title')!.textContent)
      .toBe('cctally-dev budget 90% reached');
    expect(root.getAttribute('aria-label')).toContain('cctally-dev budget 90% reached');
  });

  it('the Codex quota toast label repeats the title it renders', () => {
    surfaceThreshold(codexQuotaRow(DECORATED as Partial<SourceAlertRow>));
    const { container } = render(<Toast />);
    const root = container.querySelector('.toast--alert')!;
    expect(root.querySelector('.toast--alert-title')!.textContent)
      .toBe('Codex quota 95% reached');
    expect(root.getAttribute('aria-label')).toContain('Codex quota 95% reached');
  });

  it('an undecorated toast names the alert without inventing an account', () => {
    surfaceThreshold(claudeRow());
    const { container } = render(<Toast />);
    const label = container.querySelector('.toast--alert')!.getAttribute('aria-label')!;
    expect(label).toContain('90%');
    expect(label).not.toContain(ACCOUNT_LABEL);
    expect(label).not.toContain('All accounts');
  });
});
