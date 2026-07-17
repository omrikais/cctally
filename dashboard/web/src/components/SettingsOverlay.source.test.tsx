// #294 S5 Task 8 — the three source-scoped alert-settings groups (§6.7 Settings).
// Regrouping is PRESENTATION-only: the POST /api/settings body keys stay
// byte-identical, and the reconcile() clobber-guard is preserved.
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
  fireEvent.keyDown(document, { key: 's' });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('<SettingsOverlay /> three source-scoped alert groups', () => {
  it('renders Notifications / Claude alerts / Codex alerts groups', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    expect(screen.getByText(/^Notifications/i, { selector: 'legend' })).toBeInTheDocument();
    expect(screen.getByText(/^Claude alerts/i, { selector: 'legend' })).toBeInTheDocument();
    expect(screen.getByText(/^Codex alerts/i, { selector: 'legend' })).toBeInTheDocument();
  });

  it('the global group carries the notifier select with command gating disabled', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ command_configured: false });
    openSettings();
    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    const cmd = within(select).getByRole('option', { name: /Custom command/ }) as HTMLOptionElement;
    expect(cmd.disabled).toBe(true);
  });

  it('enables the command option when a command template is configured', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ command_configured: true });
    openSettings();
    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    const cmd = within(select).getByRole('option', { name: /Custom command/ }) as HTMLOptionElement;
    expect(cmd.disabled).toBe(false);
  });

  it('the Claude group has a labeled Claude-budget subgroup with both budget toggles', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({});
    openSettings();
    expect(screen.getByText(/Claude budget/i, { selector: '.settings-subgroup-label' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Enable threshold alerts/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Projected weekly-% pace/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Projected budget-\$ pace/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Per-project budget alerts/ })).toBeInTheDocument();
  });

  it('the Codex group states quota rules are not configurable here (CLI pointer)', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    expect(
      screen.getByText(/quota-threshold alert rules are not configurable here/i),
    ).toBeInTheDocument();
    // The two mirrored Codex budget toggles live here, gated on the flag.
    expect(screen.getByRole('checkbox', { name: /Codex budget alerts/ })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /Codex projected-pace alerts/ })).toBeInTheDocument();
  });

  it('POST /api/settings body keys are byte-identical after regrouping', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    seedAlertsConfig({
      enabled: false,
      projected_weekly_enabled: false,
      projected_budget_enabled: false,
      project_alerts_enabled: false,
      codex_budget_configured: true,
      codex_budget_alerts_enabled: false,
      codex_projected_enabled: false,
      notifier: 'auto',
    });
    openSettings();
    fireEvent.click(screen.getByRole('checkbox', { name: /Enable threshold alerts/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Projected weekly-% pace/ }));
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Projected budget-\$ pace/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Per-project budget alerts/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Codex budget alerts/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Codex projected-pace alerts/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    const [, init] = call as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      alerts: { enabled: true, projected_enabled: true, notifier: 'none' },
      budget: {
        projected_enabled: true,
        project_alerts_enabled: true,
        codex: { alerts_enabled: true, projected_enabled: true },
      },
    });
  });

  it('reconcile() does not clobber a dirty edit on an unrelated SSE tick', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ enabled: false, notifier: 'auto' });
    openSettings();
    const master = screen.getByRole('checkbox', { name: /Enable threshold alerts/ }) as HTMLInputElement;
    fireEvent.click(master); // user dirties: turns threshold master ON
    expect(master.checked).toBe(true);
    // An SSE tick that changes an UNRELATED field (notifier) must not reset the
    // user's dirty master (its server value `enabled` is unchanged).
    seedAlertsConfig({ enabled: false, notifier: 'none' });
    expect(
      (screen.getByRole('checkbox', { name: /Enable threshold alerts/ }) as HTMLInputElement).checked,
    ).toBe(true);
    expect(getState().alertsConfig.notifier).toBe('none');
  });
});
