import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsOverlay } from '../src/components/SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../src/store/store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  registerKeymap,
  uninstallGlobalKeydown,
} from '../src/store/keymap';

describe('<SettingsOverlay />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });

  it('opens on "s" and Save persists prefs', async () => {
    render(<SettingsOverlay />);
    const user = userEvent.setup();
    await user.keyboard('s');
    const costRadio = document.querySelector(
      'input[type="radio"][value="cost desc"]',
    ) as HTMLInputElement;
    await user.click(costRadio);
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.sortDefault).toBe('cost desc');
    expect(getState().sessionsSort).toBe('cost desc');
    uninstallGlobalKeydown();
  });

  it('Restore view preferences is deferred — mutates the working copy, persists on Save', async () => {
    // S6 (#252): the old bottom "Reset view preferences" applied RESET_PREFS
    // instantly. It is now the deferred "Restore view preferences" control:
    // clicking it only resets the WORKING copy (sort/perPage/filter); nothing
    // is persisted until Save (narrowed to the three view fields — it no longer
    // nukes the whole pref blob).
    dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc', sessionsPerPage: 250 } });
    render(<SettingsOverlay />);
    const user = userEvent.setup();
    await user.keyboard('s');
    await user.click(screen.getByRole('button', { name: /Restore view preferences/i }));
    // Deferred: the persisted prefs are unchanged until Save.
    expect(getState().prefs.sortDefault).toBe('cost desc');
    expect(getState().prefs.sessionsPerPage).toBe(250);
    // The working-copy sort radio flipped to the default.
    const startedRadio = document.querySelector(
      'input[type="radio"][value="started desc"]',
    ) as HTMLInputElement;
    expect(startedRadio.checked).toBe(true);
    // Save persists the restored view defaults.
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.sortDefault).toBe('started desc');
    expect(parsed.sessionsPerPage).toBe(100);
    uninstallGlobalKeydown();
  });

  it('reopen after Cancel shows current prefs, not stale local values', async () => {
    render(<SettingsOverlay />);
    const user = userEvent.setup();
    // Open, change sort to cost desc, Cancel
    await user.keyboard('s');
    const costRadio = document.querySelector(
      'input[type="radio"][value="cost desc"]',
    ) as HTMLInputElement;
    await user.click(costRadio);
    await user.click(screen.getByText('Cancel'));
    // Reopen — should show the still-current prefs default (started desc), NOT stale 'cost desc'
    await user.keyboard('s');
    const startedRadio = document.querySelector(
      'input[type="radio"][value="started desc"]',
    ) as HTMLInputElement;
    expect(startedRadio.checked).toBe(true);
    uninstallGlobalKeydown();
  });

  it('`s` is a no-op while a modal is open (no stacked overlay)', async () => {
    render(<SettingsOverlay />);
    const user = userEvent.setup();
    const { dispatch } = await import('../src/store/store');
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    await user.keyboard('s');
    // SettingsOverlay renders null when closed; no #settings-root appears.
    expect(document.getElementById('settings-root')).toBeNull();
    uninstallGlobalKeydown();
  });

  it('Escape closes the overlay', async () => {
    render(<SettingsOverlay />);
    const user = userEvent.setup();
    await user.keyboard('s');
    expect(document.getElementById('settings-root')).toBeTruthy();
    await user.keyboard('{Escape}');
    expect(document.getElementById('settings-root')).toBeNull();
    uninstallGlobalKeydown();
  });

  describe('alerts fieldset (T9)', () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it('renders alerts fieldset with toggle bound to alertsConfig.enabled (default false → unchecked)', async () => {
      // Default mirrors the Python source-of-truth (`enabled=False`).
      // See bin/cctally::_validate_alerts_config and the
      // defaultAlertsConfig() helper in store.ts.
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const toggle = document.querySelector(
        'input[type="checkbox"][name="alerts-enabled"]',
      ) as HTMLInputElement;
      expect(toggle).toBeTruthy();
      expect(toggle.checked).toBe(false);
      uninstallGlobalKeydown();
    });

    it('clicking toggle dirties Save, then Save POSTs /api/settings with {alerts: {enabled: true}}', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const toggle = document.querySelector(
        'input[type="checkbox"][name="alerts-enabled"]',
      ) as HTMLInputElement;
      await user.click(toggle);
      expect(toggle.checked).toBe(true);
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/settings',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      const call = fetchMock.mock.calls.find(
        (c) => c[0] === '/api/settings',
      )!;
      const body = JSON.parse(call[1].body as string);
      expect(body).toEqual({ alerts: { enabled: true } });
      uninstallGlobalKeydown();
    });

    it('test alert button POSTs /api/alerts/test and dispatches SHOW_ALERT_TOAST on queued', async () => {
      const fakeAlert = {
        id: 'weekly:2026-04-21:90',
        axis: 'weekly' as const,
        threshold: 90,
        crossed_at: '2026-04-23T12:00:00Z',
        alerted_at: '2026-04-23T12:00:00Z',
        context: { week_start_date: '2026-04-21', cumulative_cost_usd: 12.34 },
      };
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ dispatch: 'queued', alert: fakeAlert }),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      await user.click(screen.getByText('Send test alert'));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/alerts/test',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      await waitFor(() => {
        expect(getState().toast).toEqual({
          kind: 'alert',
          payload: fakeAlert,
        });
      });
      uninstallGlobalKeydown();
    });

    it('test alert button surfaces toast AND error when dispatch returns spawn_error', async () => {
      // CLAUDE.md "Test alerts deliberately diverge from real alerts":
      // the dashboard endpoint returns the payload directly to the
      // caller so a toast renders even when osascript fails. Regression:
      // the click handler used to gate the toast on `dispatch === 'queued'`,
      // silently suppressing the toast on spawn_error.
      const fakeAlert = {
        id: 'weekly:2026-04-21:90',
        axis: 'weekly' as const,
        threshold: 90,
        crossed_at: '2026-04-23T12:00:00Z',
        alerted_at: '2026-04-23T12:00:00Z',
        context: { week_start_date: '2026-04-21', cumulative_cost_usd: 12.34 },
      };
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            dispatch: 'spawn_error: FileNotFoundError: osascript not found',
            alert: fakeAlert,
          }),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      await user.click(screen.getByText('Send test alert'));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/alerts/test',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      // Toast still surfaces — the payload is present.
      await waitFor(() => {
        expect(getState().toast).toEqual({
          kind: 'alert',
          payload: fakeAlert,
        });
      });
      // Error message also surfaces — dispatch !== 'queued'.
      await waitFor(() => {
        const err = document.querySelector('.modal-error');
        expect(err).toBeTruthy();
        expect(err?.textContent ?? '').toContain('spawn_error');
      });
      uninstallGlobalKeydown();
    });

    it('renders read-only threshold summary line from alertsConfig (spec §8.1)', async () => {
      // Seed the store with a non-default thresholds payload via SSE
      // path, then reopen Settings and verify the summary reflects it.
      const { dispatch } = await import('../src/store/store');
      dispatch({
        type: 'INGEST_SNAPSHOT_ALERTS',
        alerts: [],
        alertsSettings: {
          enabled: false,
          weekly_thresholds: [80, 90, 95],
          five_hour_thresholds: [85, 95],
          budget_thresholds: [90, 100],
          budget_enabled: true,
        },
        isFirstTick: true,
      });
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const summary = document.querySelector('.alerts-summary')!;
      expect(summary).toBeTruthy();
      const txt = summary.textContent ?? '';
      expect(txt).toContain('Weekly: 80%, 90%, 95%');
      expect(txt).toContain('5h-block: 85%, 95%');
      expect(txt).toContain('Budget: 90%, 100%');
      uninstallGlobalKeydown();
    });

    it('test alert button stays enabled when alertsConfig.enabled toggles to false', async () => {
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const toggle = document.querySelector(
        'input[type="checkbox"][name="alerts-enabled"]',
      ) as HTMLInputElement;
      await user.click(toggle);
      const testBtn = screen.getByText('Send test alert') as HTMLButtonElement;
      expect(testBtn.disabled).toBe(false);
      uninstallGlobalKeydown();
    });

    it('projected toggles render unchecked by default (issue #121)', async () => {
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const weekly = document.querySelector(
        'input[type="checkbox"][name="projected-weekly-enabled"]',
      ) as HTMLInputElement;
      const budget = document.querySelector(
        'input[type="checkbox"][name="projected-budget-enabled"]',
      ) as HTMLInputElement;
      expect(weekly).toBeTruthy();
      expect(budget).toBeTruthy();
      expect(weekly.checked).toBe(false);
      expect(budget.checked).toBe(false);
      uninstallGlobalKeydown();
    });

    it('projected-weekly toggle POSTs {alerts: {projected_enabled: true}} (issue #121)', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const weekly = document.querySelector(
        'input[type="checkbox"][name="projected-weekly-enabled"]',
      ) as HTMLInputElement;
      await user.click(weekly);
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/settings',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      const call = fetchMock.mock.calls.find((c) => c[0] === '/api/settings')!;
      const body = JSON.parse(call[1].body as string);
      expect(body).toEqual({ alerts: { projected_enabled: true } });
      uninstallGlobalKeydown();
    });

    it('projected-budget toggle POSTs {budget: {projected_enabled: true}} in its own block (issue #121)', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const budget = document.querySelector(
        'input[type="checkbox"][name="projected-budget-enabled"]',
      ) as HTMLInputElement;
      await user.click(budget);
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/settings',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      const call = fetchMock.mock.calls.find((c) => c[0] === '/api/settings')!;
      const body = JSON.parse(call[1].body as string);
      expect(body).toEqual({ budget: { projected_enabled: true } });
      uninstallGlobalKeydown();
    });

    it('master alerts + projected-weekly both dirty → one alerts block with both keys (issue #121)', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      await user.click(
        document.querySelector(
          'input[type="checkbox"][name="alerts-enabled"]',
        ) as HTMLInputElement,
      );
      await user.click(
        document.querySelector(
          'input[type="checkbox"][name="projected-weekly-enabled"]',
        ) as HTMLInputElement,
      );
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/settings',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      const call = fetchMock.mock.calls.find((c) => c[0] === '/api/settings')!;
      const body = JSON.parse(call[1].body as string);
      expect(body).toEqual({
        alerts: { enabled: true, projected_enabled: true },
      });
      uninstallGlobalKeydown();
    });

    it('projected test axis option appears in the Send-test-alert axis select (issue #121)', async () => {
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const select = document.querySelector(
        'select[aria-label="Test alert axis"]',
      ) as HTMLSelectElement;
      const options = Array.from(select.options).map((o) => o.value);
      expect(options).toContain('projected');
      uninstallGlobalKeydown();
    });

    it('combined save: tz dirty + alerts dirty → single POST with both blocks', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const utcRadio = document.querySelector(
        'input[type="radio"][name="tz-mode"][value="utc"]',
      ) as HTMLInputElement;
      await user.click(utcRadio);
      const toggle = document.querySelector(
        'input[type="checkbox"][name="alerts-enabled"]',
      ) as HTMLInputElement;
      await user.click(toggle);
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        const settingsCalls = fetchMock.mock.calls.filter(
          (c) => c[0] === '/api/settings',
        );
        expect(settingsCalls.length).toBe(1);
      });
      const call = fetchMock.mock.calls.find(
        (c) => c[0] === '/api/settings',
      )!;
      const body = JSON.parse(call[1].body as string);
      expect(body).toEqual({
        display: { tz: 'utc' },
        alerts: { enabled: true },
      });
      uninstallGlobalKeydown();
    });
  });

  // S8 (#254): the weekly/monthly/daily modal kinds collapsed into 'daily';
  // these synthetic digit bindings route to it. The test still proves the
  // Settings overlay swallows the digit while open and it fires once closed.
  it.each([
    ['5', 'daily' as const],
    ['6', 'daily' as const],
    ['8', 'daily' as const],
  ])(
    'swallows "%s" while open so it does not stack the %s modal',
    async (key, kind) => {
      // Register the same global bindings main.tsx installs so the test
      // exercises the real precedence (modal-scope captures must beat them).
      registerKeymap([
        { key: '5', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }) },
        { key: '6', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }) },
        { key: '8', scope: 'global', action: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }) },
      ]);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      expect(document.getElementById('settings-root')).toBeTruthy();
      await user.keyboard(key);
      expect(getState().openModal).toBeNull();
      // Sanity: confirm the binding *would* have opened the modal if Settings
      // were closed — close Settings then press again.
      await user.keyboard('{Escape}');
      await user.keyboard(key);
      expect(getState().openModal).toBe(kind);
      uninstallGlobalKeydown();
    },
  );
});
