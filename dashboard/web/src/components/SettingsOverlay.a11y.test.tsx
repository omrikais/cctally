// SettingsOverlay — accessibility and dismissal. #513 S2 consolidates the six
// settings suites into three; this file holds the cases about how the overlay
// is dismissed, what it announces, and what it contains focus within, moved
// here verbatim from src/components/SettingsOverlay.test.tsx.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { AlertsConfig } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';

// Seed `state.alertsConfig` (the SSE-mirrored alerts_settings block) so the
// notifier dropdown reads the server-reported `notifier` / `command_configured`
// values. INGEST_SNAPSHOT_ALERTS replaces alertsConfig wholesale.
function seedAlertsConfig(patch: Partial<AlertsConfig>) {
  act(() => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: {
        enabled: false,
        weekly_thresholds: [90, 95],
        five_hour_thresholds: [90, 95],
        budget_thresholds: [90, 100],
        budget_enabled: false,
        projected_weekly_enabled: false,
        projected_budget_enabled: false,
        ...patch,
      },
      isFirstTick: true,
    });
  });
}

function openSettings() {
  // SettingsOverlay registers `{ key: 's', scope: 'global' }` via useKeymap
  // (see SettingsOverlay.tsx). The keymap module listens on `document`;
  // dispatching the keydown there mirrors the real user flow.
  fireEvent.keyDown(document, { key: 's' });
}

// cache-failure-markers toggle exactly: seeds from the SSE-mirrored
// dashboard_prefs slice (live-tail ON by default), dirties independently, and
// travels in the SAME combined Save POST's `dashboard` block as
// `dashboard: { live_tail }`. Re-seeds on SSE tick. One modal, one Save.
function seedLiveTailPref(live_tail: boolean) {
  act(() => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { live_tail } });
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  // useKeymap only registers bindings — production wires the listener via
  // installGlobalKeydown(). Tests must attach it so the dispatched keydown
  // reaches the bound handler. (Same pattern as HelpOverlay.test.tsx.)
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
  // restoreAllMocks undoes spies, NOT vi.stubGlobal — the fetch stub must be
  // torn down explicitly or it leaks onto globalThis and contaminates later
  // tests in the worker (same cleanup as ProjectsModal.test.tsx / ActionBar).
  vi.unstubAllGlobals();
});

// SET-2 (#252) dismiss guard: an accidental Esc/backdrop while dirty raises a
// contained confirm; the explicit × discards directly. (The `inert` focus
// containment + the confirm scrim visuals are pure-CSS/real-browser concerns
// verified at the ui-qa gate — jsdom can't evaluate them — so these unit tests
// pin only the structural behavior.)
describe('<SettingsOverlay /> dismiss guard', () => {
  it('Esc while dirty shows a confirm and keeps the overlay open; Keep editing dismisses it', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();       // confirm shown
    expect(screen.getByLabelText('Alert notifier')).toBeInTheDocument(); // still open
    fireEvent.click(screen.getByRole('button', { name: /Keep editing/i }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByLabelText('Alert notifier')).toBeInTheDocument(); // still open, edit intact
  });

  it('Esc while dirty then Discard closes the overlay', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: /Discard/i }));
    expect(screen.queryByLabelText('Alert notifier')).toBeNull(); // closed
  });

  it('Esc while clean closes immediately with no confirm', () => {
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.queryByLabelText('Alert notifier')).toBeNull(); // closed
  });

  it('the × button while dirty closes directly, no confirm', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    // ModalHeader's close button carries aria-label "Close" (ModalCloseButton default).
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.queryByLabelText('Alert notifier')).toBeNull(); // closed directly
  });
});

// #252 review fixes.
describe('<SettingsOverlay /> discards uncommitted edits on reopen (review P2)', () => {
  // The on-open effect must re-seed EVERY working field so a discarded edit
  // never survives Cancel + reopen. Live-tail is the witness — its dedicated
  // SSE effect only re-fires when the SERVER value changes, so before the fix
  // the toggle stayed at the discarded value on reopen and read as a phantom
  // "Save · 1 change". tzMode/tzCustom ride the same on-open re-seed line.
  it('re-seeds the live-tail toggle from the server after Cancel + reopen', () => {
    render(<SettingsOverlay />);
    seedLiveTailPref(true); // server: live-tail ON
    openSettings();
    const toggle = () =>
      screen.getByRole('checkbox', { name: /Live-tail new turns/ }) as HTMLInputElement;
    expect(toggle().checked).toBe(true);
    fireEvent.click(toggle()); // user turns it OFF → dirty
    expect(toggle().checked).toBe(false);
    expect(
      (screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement).textContent,
    ).toBe('Save · 1 change');
    // Cancel (a deliberate discard) then reopen — the edit must NOT resurface.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    openSettings();
    expect(toggle().checked).toBe(true); // re-seeded from the server value
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.textContent).toBe('Save');
    expect(save.disabled).toBe(true);
  });

  // The inert precondition for the focus-containment fix (useModalFocus now
  // skips [inert] subtrees). The actual Tab-escape prevention is a real-browser
  // ui-qa item — jsdom can't drive a trusted Tab through native inert — so this
  // pins only that the confirm marks the body inert / clears it.
  it('marks the modal body inert while the discard confirm is open', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.keyDown(document, { key: 'Escape' }); // dirty → confirm up
    const body = document.querySelector('.modal-body') as HTMLElement;
    expect(body.inert).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /Keep editing/i }));
    expect(body.inert).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// #513 S2 §3 — the issue model, honest save feedback, and numeric input.
// ---------------------------------------------------------------------------
import { REGISTRY } from './settings/registry';
import {
  GROUP_OWNERS,
  mismatchedIgnoredPaths,
  resolveIssueTarget,
} from './settings/issues';

function deferredFetch() {
  let settle: (value: unknown) => void = () => {};
  const pending = new Promise<unknown>((resolve) => {
    settle = resolve;
  });
  const mock = vi.fn(async () => {
    await pending;
    return {
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    } as unknown as Response;
  });
  vi.stubGlobal('fetch', mock);
  return { mock, finish: () => settle(null) };
}

function respondWith(status: number, body: Record<string, unknown>) {
  const mock = vi.fn(
    async () =>
      ({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(body),
      }) as unknown as Response,
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

describe('issue routing (#513 S2 §3.1)', () => {
  it.each([
    ['display.tz', { kind: 'leaf', id: 'display.tz' }],
    ['budget.codex.alerts_enabled', { kind: 'leaf', id: 'budget.codex.alerts_enabled' }],
    ['budget.weekly_usd', { kind: 'leaf', id: 'budget.weekly_usd' }],
    ['alerts', { kind: 'group', section: 'alerts' }],
    ['budget', { kind: 'group', section: 'alerts' }],
    ['budget.codex', { kind: 'group', section: 'alerts' }],
    ['dashboard', { kind: 'group', section: 'viewer' }],
    ['update', { kind: 'group', section: 'access' }],
    ['update.check', { kind: 'group', section: 'access' }],
    // Both render disclosure rows in Access & updates, so a 400 naming one of
    // them has a real place to land rather than falling to form level.
    ['update.check.enabled', { kind: 'group', section: 'access' }],
    ['update.check.ttl_hours', { kind: 'group', section: 'access' }],
    // Declared form-level rather than merely absent: nothing in this overlay
    // renders it. `tests/test_settings_manifest.py` is what keeps the two
    // kinds of "not a group" apart.
    ['cache_report', { kind: 'form' }],
    ['$', { kind: 'form' }],
    ['nonsense.path', { kind: 'form' }],
    // An inherited Object.prototype name must not resolve as a group.
    ['constructor', { kind: 'form' }],
  ])('routes %s', (field, expected) => {
    expect(resolveIssueTarget(field as string)).toEqual(expected);
  });

  it('keeps the ancestor map OUT of the registry, so the prefix guard stays valid', () => {
    // If `budget` were a registry path, `budget.weekly_usd` would be a path
    // under a path and the registry's own prefix guard could not hold.
    const paths = new Set(
      REGISTRY.filter((f) => f.kind === 'server').map((f) => (f as { path: string }).path),
    );
    for (const ancestor of Object.keys(GROUP_OWNERS)) {
      expect(paths.has(ancestor)).toBe(false);
    }
  });
});

describe('ignored_fields only interrupts a promise this overlay made (#557)', () => {
  it('proves the accepted-then-discarded paths are disclosure-only and unsendable', () => {
    const sentPaths = new Set(
      REGISTRY.filter((field) => field.kind === 'server').map(
        (field) => (field as { path: string }).path,
      ),
    );
    const disclosureOnly = SETTINGS_MANIFEST.filter(
      (entry) => entry.acceptedThenDiscarded,
    ).map((entry) => entry.key);
    expect(disclosureOnly).toHaveLength(4);
    expect(disclosureOnly.every((path) => !sentPaths.has(path))).toBe(true);
    expect(mismatchedIgnoredPaths(disclosureOnly)).toEqual([]);
  });

  it('treats a path this client declared writable as a contract mismatch', () => {
    expect(mismatchedIgnoredPaths(['alerts.notifier'])).toEqual(['alerts.notifier']);
  });

  it('ignores a path this client neither sends nor declares', () => {
    expect(mismatchedIgnoredPaths(['cache_report.anomaly_threshold_pp'])).toEqual([]);
  });
});

describe('Save enablement and numeric input (#513 S2 §3.2, §3.7)', () => {
  it('keeps Save clickable when a field is invalid, disabling only when clean or submitting', () => {
    render(<SettingsOverlay />);
    openSettings();
    const save = () => screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save().disabled).toBe(true); // clean
    fireEvent.change(screen.getByLabelText(/Sessions per page/i), { target: { value: '5' } });
    expect(save().disabled).toBe(false); // dirty AND invalid — still clickable
  });

  it('does not clamp: typing 5 into sessions-per-page never writes 10', () => {
    render(<SettingsOverlay />);
    openSettings();
    const input = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '5' } });
    expect(input.value).toBe('5');
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    // The stored value is untouched, and nothing was rewritten to 10.
    expect(getState().prefs.sessionsPerPage).toBe(100);
    expect(document.getElementById('settings-root')).toBeTruthy(); // stayed open
  });

  it('preserves an empty numeric draft as "" rather than rendering 0', () => {
    render(<SettingsOverlay />);
    openSettings();
    const input = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '' } });
    expect(input.value).toBe('');
  });

  it('surfaces a numeric issue on blur, and clears it when the field changes', () => {
    render(<SettingsOverlay />);
    openSettings();
    const input = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '5' } });
    expect(input.getAttribute('aria-invalid')).not.toBe('true'); // not from live typing
    fireEvent.blur(input);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/between 10 and 1000/i);
    fireEvent.change(input, { target: { value: '50' } });
    expect(input.getAttribute('aria-invalid')).not.toBe('true');
  });

  it('a Save attempt populates the summary and focuses the first invalid control', () => {
    const fetchMock = respondWith(200, {});
    render(<SettingsOverlay />);
    openSettings();
    const input = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/between 10 and 1000/i);
    expect(document.activeElement).toBe(input);
    expect(fetchMock).not.toHaveBeenCalled(); // no POST, no local dispatch
  });

  // §3.3 names ONE chokepoint through which every edit clears its issue. A
  // second write path that called the reducer directly left a surfaced issue on
  // screen after the restore had already made the field valid.
  it('restoring view preferences clears an issue the restore itself resolved', () => {
    render(<SettingsOverlay />);
    openSettings();
    const perPage = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(perPage, { target: { value: '5' } });
    fireEvent.blur(perPage);
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/between 10 and 1000/i);
    fireEvent.click(screen.getByRole('button', { name: /Restore view preferences/i }));
    expect(perPage.value).toBe('100'); // the restore made it valid
    expect(screen.getByRole('alert').textContent ?? '').not.toMatch(/between 10 and 1000/i);
    expect(perPage.getAttribute('aria-invalid')).not.toBe('true');
  });

  it('refuses a non-positive weekly budget without replacing the stored amount', () => {
    const fetchMock = respondWith(200, {});
    render(<SettingsOverlay />);
    seedAlertsConfig({ weekly_usd: 200 });
    openSettings();
    const input = screen.getByLabelText(/Weekly budget/i) as HTMLInputElement;
    expect(input.value).toBe('200');
    fireEvent.change(input, { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent ?? '').toMatch(/greater than 0/i);
  });
});

describe('routed server errors (#513 S2 §3.4)', () => {
  // Asserted against the SPECIFIC element, not against whatever happens to
  // carry `data-settings-field="display.tz"`. The attribute used to sit on the
  // Local radio, which can never be invalid and which a `--tz` pin disables, so
  // an assertion on the attribute alone was satisfied by the wrong control.
  it('a leaf 400 focuses the one control that can actually be invalid', async () => {
    respondWith(400, { error: 'display.tz must be an IANA zone', field: 'display.tz' });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^Custom/ }));
    const custom = document.getElementById('settings-tz-custom') as HTMLInputElement;
    fireEvent.change(custom, { target: { value: 'America/New_York' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/IANA zone/);
    });
    expect(document.activeElement).toBe(custom);
    // And it is the same element that carries `aria-invalid`, which is what
    // "the first invalid control" has to mean.
    expect(custom.getAttribute('aria-invalid')).toBe('true');
  });

  it('falls back to the summary when the named control cannot take focus', async () => {
    respondWith(400, { error: 'display.tz must be an IANA zone', field: 'display.tz' });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    // The custom-zone input is the display.tz target and it is disabled here,
    // so `.focus()` is a no-op. Reporting success would strand focus.
    expect((document.getElementById('settings-tz-custom') as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/IANA zone/);
    });
    expect(document.activeElement).toBe(screen.getByRole('alert'));
  });

  it('an ancestor 400 paints on the owning section, not at form level', async () => {
    respondWith(400, { error: 'budget.codex must be an object', field: 'budget.codex' });
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    fireEvent.click(screen.getByRole('checkbox', { name: /Codex budget alerts/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/must be an object/);
    });
    const active = document.activeElement as HTMLElement | null;
    expect(active?.getAttribute('data-settings-section')).toBe('alerts');
  });

  it('a whole-form 400 focuses the summary', async () => {
    respondWith(400, { error: 'malformed json', field: '$' });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/malformed json/);
    });
    expect(document.activeElement).toBe(screen.getByRole('alert'));
  });

  it('a 200 listing an accepted-then-discarded path still succeeds', async () => {
    respondWith(200, { ignored_fields: ['budget.period'] });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(document.getElementById('settings-root')).toBeNull(); // closed = saved
    });
  });

  it('an immediate reopen after a save stays clean while the server tick is pending', async () => {
    respondWith(200, {});
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(document.getElementById('settings-root')).toBeNull());

    openSettings();
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(document.querySelectorAll('.settings-fs.is-changed')).toHaveLength(0);
    expect(document.querySelectorAll('.settings-rail-link .fs-changed')).toHaveLength(0);
  });

  it('a 200 ignoring a leaf this client declared writable keeps the form open', async () => {
    respondWith(200, { ignored_fields: ['display.tz'] });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/display\.tz/);
    });
    expect(document.getElementById('settings-root')).toBeTruthy();
  });
});

describe('long saves are described honestly (#513 S2 §3.6)', () => {
  it('shows nothing extra before 3 seconds', async () => {
    vi.useFakeTimers();
    try {
      const { finish } = deferredFetch();
      render(<SettingsOverlay />);
      openSettings();
      fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
      await act(async () => { await Promise.resolve(); });
      act(() => { vi.advanceTimersByTime(2000); });
      expect(screen.getByRole('status').textContent ?? '').not.toMatch(/still saving/i);
      finish();
      await act(async () => { await Promise.resolve(); });
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows elapsed time and PHASE-NEUTRAL copy after 3 seconds', async () => {
    vi.useFakeTimers();
    try {
      const { finish } = deferredFetch();
      render(<SettingsOverlay />);
      openSettings();
      fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
      await act(async () => { await Promise.resolve(); });
      act(() => { vi.advanceTimersByTime(3000); });
      const status = screen.getByRole('status').textContent ?? '';
      expect(status).toMatch(/still saving/i);
      expect(status).toMatch(/3s/);
      expect(status).toMatch(/not stuck/i);
      // The copy must NOT assert that the configuration write has landed: at
      // three seconds the request may still be waiting on the config lock, and
      // there is no server phase signal to tell us otherwise.
      expect(status).not.toMatch(/saved/i);
      expect(status).not.toMatch(/written/i);
      expect(status).not.toMatch(/rebuilt/i);
      finish();
      await act(async () => { await Promise.resolve(); });
    } finally {
      vi.useRealTimers();
    }
  });

  it('suppresses dismissal while a submit is in flight', async () => {
    const { finish } = deferredFetch();
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('radio', { name: /^UTC/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await act(async () => { await Promise.resolve(); });
    // The affordance has to match the behaviour: a Cancel and a × that look
    // operable while every press is a no-op teach a user that the surface is
    // broken, not that it is busy.
    expect(
      (screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled,
      'Cancel looks operable while every press is a no-op',
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: /close/i }) as HTMLButtonElement).disabled,
      'the × looks operable while every press is a no-op',
    ).toBe(true);
    // None of the four dismissal gestures may strand the in-flight request.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(document.getElementById('settings-root')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    expect(document.getElementById('settings-root')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(document.getElementById('settings-root')).toBeTruthy();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    fireEvent.click(document.querySelector('.modal-backdrop') as HTMLElement);
    expect(document.getElementById('settings-root')).toBeTruthy();
    finish();
    await act(async () => { await Promise.resolve(); });
  });
});

// ---------------------------------------------------------------------------
// #513 S2 §2 — navigation, the DOM-removing filter, and its focus protocols.
// The rail's ACTIVE-section rule is measured against real layout, so it is the
// browser spec's to verify; what JSDOM owns is what the filter does to the DOM
// and to the form model.
// ---------------------------------------------------------------------------
import { SETTINGS_MANIFEST } from './settings/manifest';

function typeFilter(value: string) {
  fireEvent.change(screen.getByLabelText('Find a setting'), { target: { value } });
}

describe('the section rail and the filter (#513 S2 §2.1, §2.2)', () => {
  it('lists every non-empty section as a labelled navigation entry', () => {
    render(<SettingsOverlay />);
    openSettings();
    const rail = screen.getByRole('navigation', { name: /settings sections/i });
    for (const title of [
      'Display & time',
      'Recent Sessions',
      'Alerts',
      'Conversation viewer',
      'Access & updates',
      'Restore defaults',
      'Managed from the CLI',
    ]) {
      expect(within(rail).getByRole('button', { name: new RegExp(title, 'i') })).toBeTruthy();
    }
  });

  it('scrolls only the settings pane and focuses the selected heading without native scrolling', () => {
    render(<SettingsOverlay />);
    openSettings();

    const scroller = document.querySelector<HTMLElement>('#settings-scroller')!;
    const heading = document.querySelector<HTMLElement>('[data-settings-section="cli"]')!;
    const scrollTo = vi.fn();
    const nativeScroll = vi.spyOn(heading, 'scrollIntoView');
    const focus = vi.spyOn(heading, 'focus');

    Object.defineProperties(scroller, {
      scrollTop: { configurable: true, value: 120, writable: true },
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 300 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 100,
      top: 100,
      right: 500,
      bottom: 400,
      left: 0,
      width: 500,
      height: 300,
      toJSON: () => ({}),
    });
    vi.spyOn(heading, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 500,
      top: 500,
      right: 500,
      bottom: 520,
      left: 0,
      width: 500,
      height: 20,
      toJSON: () => ({}),
    });

    fireEvent.click(screen.getByRole('button', { name: /Managed from the CLI/i }));

    // 120 current + (500 heading - 100 pane) - 16px visual inset.
    expect(scrollTo).toHaveBeenCalledWith({ top: 504, behavior: 'smooth' });
    expect(nativeScroll).not.toHaveBeenCalled();
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it('REMOVES non-matching rows and emptied sections from the DOM', () => {
    render(<SettingsOverlay />);
    openSettings();
    expect(screen.getByRole('checkbox', { name: /Live-tail new turns/ })).toBeTruthy();
    typeFilter('timezone');
    expect(screen.queryByRole('checkbox', { name: /Live-tail new turns/ })).toBeNull();
    expect(screen.getByRole('radio', { name: /^UTC/ })).toBeTruthy();
    // The emptied sections are gone from the rail too, not merely hidden.
    const rail = screen.getByRole('navigation', { name: /settings sections/i });
    expect(within(rail).queryByRole('button', { name: /Conversation viewer/i })).toBeNull();
  });

  it('matches on the dotted key path and on the scope word', () => {
    render(<SettingsOverlay />);
    openSettings();
    typeFilter('budget.codex.projected_enabled');
    expect(
      screen.getByRole('checkbox', { name: /Codex projected-pace alerts/ }),
    ).toBeTruthy();
    typeFilter('this browser');
    expect(screen.getByRole('radio', { name: /Started \(newest first\)/ })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: /Live-tail new turns/ })).toBeNull();
  });

  it('renders an explicit empty state for a zero-result filter', () => {
    render(<SettingsOverlay />);
    openSettings();
    typeFilter('zzzzz-no-such-setting');
    expect(screen.getByText(/No setting matches/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /show all settings/i })).toBeTruthy();
  });

  it('updates the visible result count immediately but announces only after it settles', () => {
    vi.useFakeTimers();
    try {
      render(<SettingsOverlay />);
      openSettings();
      const count = document.querySelector('.settings-filter-count')!;
      const announcement = document.querySelector('.settings-filter-announcement')!;
      expect(count.textContent ?? '').toMatch(/\d+ settings/);
      expect(count.getAttribute('aria-live')).toBeNull();
      const initialAnnouncement = announcement.textContent;

      typeFilter('timezone');
      expect(count.textContent ?? '').toMatch(/of \d+ settings match/);
      expect(announcement.textContent).toBe(initialAnnouncement);
      act(() => vi.advanceTimersByTime(299));
      expect(announcement.textContent).toBe(initialAnnouncement);
      act(() => vi.advanceTimersByTime(1));
      expect(announcement.textContent).toMatch(/of \d+ settings match/);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not carry a no-op settings-row class on ordinary field labels', () => {
    render(<SettingsOverlay />);
    openSettings();
    expect(document.querySelectorAll('.settings-row')).toHaveLength(0);
  });

  it('keeps a filtered-out dirty field in the change count AND in the POST body', async () => {
    const fetchMock = respondWith(200, {});
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    const save = () => screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save().textContent).toBe('Save · 1 change');
    typeFilter('timezone'); // the live-tail row is now unmounted
    expect(screen.queryByRole('checkbox', { name: /Live-tail new turns/ })).toBeNull();
    expect(save().textContent).toBe('Save · 1 change');
    expect(screen.getByText(/1 unsaved change is hidden by this filter/i)).toBeTruthy();
    fireEvent.click(save());
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ dashboard: { live_tail: false } });
  });

  it('moves focus to the filter before removing the subtree holding activeElement', () => {
    render(<SettingsOverlay />);
    openSettings();
    const liveTail = screen.getByRole('checkbox', { name: /Live-tail new turns/ });
    liveTail.focus();
    expect(document.activeElement).toBe(liveTail);
    typeFilter('timezone');
    expect(document.activeElement).toBe(screen.getByLabelText('Find a setting'));
  });

  it('reveals, awaits the remount, then focuses when a summary entry is activated', () => {
    render(<SettingsOverlay />);
    openSettings();
    const perPage = screen.getByLabelText(/Sessions per page/i) as HTMLInputElement;
    fireEvent.change(perPage, { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    // Filter the invalid control away, then use the summary entry to get back.
    typeFilter('timezone');
    expect(screen.queryByLabelText(/Sessions per page/i)).toBeNull();
    fireEvent.click(
      within(screen.getByRole('alert')).getByRole('button', { name: /Sessions per page/i }),
    );
    const revealed = screen.getByLabelText(/Sessions per page/i);
    expect(document.activeElement).toBe(revealed);
  });

  it('routes test-action output to the persistent region, surviving its row being filtered away', async () => {
    respondWith(200, { dispatch: 'spawn_error: osascript missing' });
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('button', { name: /Send test alert/ }));
    typeFilter('timezone'); // the test action's own row is now unmounted
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent ?? '').toMatch(/spawn_error/);
    });
    expect(screen.queryByRole('button', { name: /Send test alert/ })).toBeNull();
  });
});

// A control that unmounts itself as a consequence of its own activation is the
// whole failure class, and the scroller is only one of the containers such a
// control lives in. The relocation protocol used to name the scroller, so the
// "Show all settings" button in `.settings-chrome` — which removes its own line
// the moment it clears the filter — dropped focus to `<body>`, where the
// card-level trap cannot recover it.
describe('a self-unmounting control never drops focus to the body (#513 S2 §2.3)', () => {
  const activeIsInsideTheDialog = () => {
    const card = document.querySelector('.modal-card');
    const active = document.activeElement as HTMLElement | null;
    return !!card && !!active && active !== document.body && card.contains(active);
  };

  it('keeps focus in the dialog when the hidden-dirty "Show all settings" unmounts itself', () => {
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    typeFilter('timezone'); // the dirty row is hidden; the chrome line appears
    const reveal = within(
      document.querySelector('.settings-hidden-dirty') as HTMLElement,
    ).getByRole('button', { name: /show all settings/i });
    reveal.focus();
    expect(document.activeElement).toBe(reveal);
    fireEvent.click(reveal);
    // Its own line is gone now: nothing is hidden any more.
    expect(document.querySelector('.settings-hidden-dirty')).toBeNull();
    expect(
      activeIsInsideTheDialog(),
      `focus fell to ${document.activeElement?.nodeName}`,
    ).toBe(true);
  });

  it('keeps focus in the dialog when the empty-state "show all settings" unmounts itself', () => {
    render(<SettingsOverlay />);
    openSettings();
    typeFilter('zzzzz-no-such-setting');
    const reveal = within(
      document.querySelector('.settings-empty') as HTMLElement,
    ).getByRole('button', { name: /show all settings/i });
    reveal.focus();
    fireEvent.click(reveal);
    expect(document.querySelector('.settings-empty')).toBeNull();
    expect(
      activeIsInsideTheDialog(),
      `focus fell to ${document.activeElement?.nodeName}`,
    ).toBe(true);
  });

  // The third path is a pointer press whose hit target cannot take focus — the
  // disabled `#settings-tz-custom`, which is disabled in the DEFAULT state. The
  // browser then focuses the nearest focusable ancestor and, finding none,
  // focuses `<body>`. JSDOM dispatches no pointer events to a disabled control
  // at all, so what it can decide is the precondition; the press itself is
  // asserted in `e2e/settings-surface.spec.ts`.
  it('gives the browser focus-fixup walk somewhere inside the dialog to stop', () => {
    render(<SettingsOverlay />);
    openSettings();
    expect(
      (document.getElementById('settings-tz-custom') as HTMLInputElement).disabled,
    ).toBe(true);
    const card = document.querySelector('.modal-card') as HTMLElement;
    // `element.tabIndex` reads -1 for a plain div too, so the ATTRIBUTE is what
    // discriminates: without it the walk has no focusable ancestor to stop on.
    expect(
      card.getAttribute('tabindex'),
      'the dialog is not a focusable area, so a dead-space press falls to <body>',
    ).toBe('-1');
    // And it must not become a tab stop: the trap cycles real controls.
    card.focus();
    expect(document.activeElement).toBe(card);
  });
});

// §2.7 retired "axis" as a user-facing noun. An `aria-label` IS user-facing:
// it is the only name a screen-reader user hears, and it overrides the visible
// label it sits next to.
describe('naming (#513 S2 §2.7)', () => {
  it('says "axis" nowhere a person can read or hear it', () => {
    render(<SettingsOverlay />);
    openSettings();
    const root = document.getElementById('settings-root')!;
    expect(root.textContent ?? '', 'visible text still says axis').not.toMatch(/\baxis\b/i);
    const labels = Array.from(
      root.querySelectorAll<HTMLElement>('[aria-label]'),
    ).map((node) => node.getAttribute('aria-label') ?? '');
    expect(labels.length, 'nothing carried an aria-label').toBeGreaterThan(2);
    for (const label of labels) {
      expect(label, `aria-label "${label}" still says axis`).not.toMatch(/\baxis\b/i);
    }
  });

  it('still names both test-alert selects, and the projected one only when it applies', () => {
    render(<SettingsOverlay />);
    openSettings();
    const alertType = screen.getByLabelText('Alert type') as HTMLSelectElement;
    expect(screen.queryByLabelText('Metric')).toBeNull();
    fireEvent.change(alertType, { target: { value: 'projected' } });
    expect(screen.getByLabelText('Metric')).toBeTruthy();
  });
});

describe('scope marking (#513 S2 §2.8)', () => {
  it('exposes the scope on EVERY row, and shows it visibly on per-browser rows only', () => {
    render(<SettingsOverlay />);
    openSettings();
    // Every rendered control carries a scope word in the text a screen reader
    // reads for it — on the control's own label or on its fieldset legend.
    const controls = Array.from(
      document.querySelectorAll<HTMLElement>('[data-settings-field]'),
    );
    expect(controls.length).toBeGreaterThan(5);
    for (const control of controls) {
      const label = control.closest('label')?.textContent ?? '';
      const fieldset = control.closest('fieldset')?.textContent ?? '';
      expect(
        `${label} ${fieldset}`,
        `${control.getAttribute('data-settings-field')} exposes no scope`,
      ).toMatch(/this (machine|browser)/i);
    }
    // A per-browser row shows it in visible text.
    const browserScopes = document.querySelectorAll('.settings-scope');
    expect(browserScopes.length).toBeGreaterThan(0);
    for (const node of Array.from(browserScopes)) {
      expect(node.textContent).toMatch(/this browser/);
    }
    // And the default is stated once, in the action bar.
    expect(
      document.querySelector('.settings-scope-note')?.textContent ?? '',
    ).toMatch(/apply to this machine unless marked/i);
  });
});

describe('the disposition manifest is rendered, not merely declared (#513 S2 §2.4)', () => {
  it('renders a row for every non-editable key, reachable by filter, with its pinned command', () => {
    render(<SettingsOverlay />);
    openSettings();
    for (const entry of SETTINGS_MANIFEST) {
      if (entry.disposition === 'editable') continue;
      typeFilter(entry.key);
      const rows = Array.from(document.querySelectorAll('.settings-disclosure'));
      const row = rows.find((node) => node.textContent?.includes(entry.key));
      expect(row, `no rendered row for ${entry.key}`).toBeTruthy();
      // Pinned command text, not merely some non-empty string.
      expect(row!.textContent).toContain(entry.command);
      expect(row!.textContent).toContain(entry.defaultText);
    }
  });

  it('states that the four accepted-then-discarded leaves are not stored', () => {
    render(<SettingsOverlay />);
    openSettings();
    for (const entry of SETTINGS_MANIFEST.filter((e) => e.acceptedThenDiscarded)) {
      typeFilter(entry.key);
      const row = Array.from(document.querySelectorAll('.settings-disclosure')).find(
        (node) => node.textContent?.includes(entry.key),
      );
      expect(row!.textContent).toMatch(/accepts this key on a save and deliberately does not store it/);
    }
  });

  it('discloses budget.alerts_enabled, so "budget set, alerts off" can state its remedy', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ weekly_usd: 200 });
    openSettings();
    typeFilter('budget.alerts_enabled');
    const row = Array.from(document.querySelectorAll('.settings-disclosure')).find(
      (node) => node.textContent?.includes('budget.alerts_enabled'),
    );
    expect(row!.textContent).toContain('cctally config set budget.alerts_enabled true');
  });
});

describe('accessible names and descriptions (#513 S2 AC9)', () => {
  const inventory = () =>
    Array.from(document.querySelectorAll<HTMLElement>('#settings-root input, #settings-root select, #settings-root button'))
      .map((control) => {
        const labelled = control.getAttribute('aria-label')
          ?? control.closest('label')?.textContent
          ?? (control.id
            ? document.querySelector(`label[for="${control.id}"]`)?.textContent
            : null)
          ?? control.textContent;
        return { control, name: (labelled ?? '').trim() };
      });

  // Every WAI-ARIA attribute whose value is an IDREF list. Checking only
  // `aria-describedby` is what let seven dangling `aria-labelledby` section
  // references ship: the narrower walk could not observe them, so it passed.
  const ID_REFERENCE_ATTRIBUTES = [
    'aria-labelledby',
    'aria-describedby',
    'aria-controls',
    'aria-owns',
    'aria-errormessage',
  ] as const;

  const assertNamedAndDescribed = (state: string) => {
    const rows = inventory();
    expect(rows.length, `${state}: nothing to inventory`).toBeGreaterThan(5);
    for (const { control, name } of rows) {
      expect(name, `${state}: ${control.outerHTML.slice(0, 80)} has no accessible name`).not.toBe('');
    }
    // No dangling ID reference of ANY kind anywhere in the overlay.
    let referencesChecked = 0;
    for (const attribute of ID_REFERENCE_ATTRIBUTES) {
      for (const node of Array.from(
        document.querySelectorAll<HTMLElement>(`#settings-root [${attribute}]`),
      )) {
        for (const id of (node.getAttribute(attribute) ?? '').trim().split(/\s+/)) {
          if (!id) continue;
          referencesChecked += 1;
          expect(
            document.getElementById(id),
            `${state}: dangling ${attribute}="${id}" on ${node.outerHTML.slice(0, 80)}`,
          ).toBeTruthy();
        }
      }
    }
    // Non-vacuity: a walk that found no reference at all would pass silently.
    expect(referencesChecked, `${state}: no ID references were checked`).toBeGreaterThan(3);
  };

  it('every section resolves its accessible name from a heading that exists', () => {
    render(<SettingsOverlay />);
    openSettings();
    const sections = Array.from(
      document.querySelectorAll<HTMLElement>('#settings-root section.settings-section'),
    );
    expect(sections.length, 'no sections were rendered').toBeGreaterThan(5);
    for (const section of sections) {
      const id = section.getAttribute('aria-labelledby') ?? '';
      const heading = document.getElementById(id);
      expect(heading, `section is labelled by a missing #${id}`).toBeTruthy();
      expect(heading!.textContent?.trim(), `#${id} names nothing`).not.toBe('');
    }
  });

  it('every control is named in the default, filtered, invalid and error-summary states', () => {
    render(<SettingsOverlay />);
    openSettings();
    assertNamedAndDescribed('default');
    typeFilter('budget');
    assertNamedAndDescribed('filtered');
    typeFilter('');
    fireEvent.change(screen.getByLabelText(/Sessions per page/i), { target: { value: '5' } });
    fireEvent.blur(screen.getByLabelText(/Sessions per page/i));
    assertNamedAndDescribed('invalid');
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    assertNamedAndDescribed('error summary');
  });
});

describe('the Claude budget toggles state reason and remedy in both states (#513 S2 AC11)', () => {
  const toggles = () => [
    screen.getByRole('checkbox', { name: /Projected budget-\$ pace alerts/ }) as HTMLInputElement,
    screen.getByRole('checkbox', { name: /Per-project budget alerts/ }) as HTMLInputElement,
  ];

  it('says why they cannot fire when NO budget is set, and stays operable', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ weekly_usd: null });
    openSettings();
    expect(screen.getByText(/No Claude budget is set/i).textContent).toMatch(
      /Enter an amount in the field above/i,
    );
    for (const toggle of toggles()) expect(toggle.disabled).toBe(false);
  });

  it('discloses the master switch when a budget EXISTS but alerts are off, and stays operable', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ weekly_usd: 200, budget_enabled: false });
    openSettings();
    expect(screen.queryByText(/No Claude budget is set/i)).toBeNull();
    const row = Array.from(document.querySelectorAll('.settings-disclosure')).find(
      (node) => node.textContent?.includes('budget.alerts_enabled'),
    );
    expect(row!.textContent).toMatch(/fire only when an amount is set AND this master is on/i);
    expect(row!.textContent).toContain('cctally config set budget.alerts_enabled true');
    for (const toggle of toggles()) expect(toggle.disabled).toBe(false);
  });
});
