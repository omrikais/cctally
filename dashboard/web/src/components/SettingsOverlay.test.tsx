// SettingsOverlay — "Send test alert" axis picker (issue #19 follow-up).
// Parent-modal integration test: mount the real SettingsOverlay, open it
// through the `s` keymap (the production open path), pick a non-default
// axis in the <select>, fire the test-alert button, and assert the POST
// body carries the SELECTED axis (not the old hardcoded 'weekly').
//
// This is the binding assertion: it must pin `axis === 'budget'`
// specifically so it fails against the prior hardcoded body. See the
// RED→GREEN non-vacuity proof in the implementor report.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { AlertsConfig } from '../store/store';
import { DEFAULT_PANEL_ORDER } from '../lib/panelRegistry';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  _resetForTests as _resetKeymapForTests,
  // The blocks folded in from `__tests__/` (#513 S2) spell the same reset under
  // their own alias. Binding both names keeps every moved callback verbatim.
  _resetForTests as _resetKeymap,
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

describe('<SettingsOverlay /> test-alert axis picker', () => {
  it('POSTs the SELECTED axis (budget) with threshold 90', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(
          JSON.stringify({
            alert: { axis: 'budget', threshold: 90, context: {} },
            dispatch: 'queued',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    openSettings();

    // The overlay is now open — the alert-type <select> is labelled for a11y.
    const select = screen.getByLabelText('Alert type') as HTMLSelectElement;
    expect(select.value).toBe('weekly'); // default seeded on open

    // Pick the Budget axis, then fire the test-alert button.
    fireEvent.change(select, { target: { value: 'budget' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // Find the /api/alerts/test call (the only fetch this flow makes).
    const call = fetchMock.mock.calls.find(
      ([url]) => url === '/api/alerts/test',
    );
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      axis: string;
      threshold: number;
    };
    // Binding assertion: the selected axis must reach the wire. Against the
    // old hardcoded `{ axis: 'weekly', threshold: 90 }` this fails with
    // "expected 'budget' … received 'weekly'".
    expect(parsed.axis).toBe('budget');
    expect(parsed.threshold).toBe(90);
  });

  // Issue #121: the projected axis is metric-aware. Picking it must reveal a
  // metric sub-select and carry the chosen `metric` to the wire — otherwise
  // the budget_usd projection is untestable and (before the endpoint fix) the
  // POST 400'd. Binding assertion: axis === 'projected' AND metric ===
  // 'budget_usd'. Against the prior no-metric body this fails on the missing
  // `metric` key.
  it('reveals a metric sub-select for projected and POSTs the chosen metric', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(
          JSON.stringify({
            alert: {
              axis: 'projected',
              metric: 'budget_usd',
              threshold: 90,
              context: {},
            },
            dispatch: 'queued',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    openSettings();

    // The metric sub-select is hidden until the projected type is chosen.
    expect(screen.queryByLabelText('Metric')).toBeNull();

    const select = screen.getByLabelText('Alert type') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'projected' } });

    // Now the metric chooser appears; pick the budget_usd variant.
    const metricSelect = screen.getByLabelText('Metric') as HTMLSelectElement;
    expect(metricSelect.value).toBe('weekly_pct'); // default seeded
    fireEvent.change(metricSelect, { target: { value: 'budget_usd' } });

    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(
      ([url]) => url === '/api/alerts/test',
    );
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as {
      axis: string;
      threshold: number;
      metric?: string;
    };
    expect(parsed.axis).toBe('projected');
    expect(parsed.threshold).toBe(90);
    expect(parsed.metric).toBe('budget_usd');
  });

  // The metric key must NOT ride along for non-projected axes — the endpoint
  // ignores it, but a leaking key would muddy the wire contract.
  it('omits metric from the body for non-projected axes', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(
          JSON.stringify({
            alert: { axis: 'weekly', threshold: 90, context: {} },
            dispatch: 'queued',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    openSettings();
    // Default axis is 'weekly'; fire without touching the select.
    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(
      ([url]) => url === '/api/alerts/test',
    );
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(parsed.axis).toBe('weekly');
    expect('metric' in parsed).toBe(false);
  });
});

// #207 D9 → S6 (#252): the old bottom "Reset view preferences" button applied
// RESET_PREFS instantly. It is now the deferred "Restore view preferences"
// control inside the Restore-defaults fieldset — clicking it mutates the
// working-copy sort field and persists NOTHING until Save.
describe('<SettingsOverlay /> restore view preferences button (#207 D9 → #252)', () => {
  it('Restore view preferences mutates the sort field and posts nothing until Save', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    // Seed a non-default sort so the button is enabled and its effect observable.
    act(() => dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc' } }));
    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('button', { name: /Restore view preferences/i }));
    expect(fetchMock).not.toHaveBeenCalled();              // deferred — no POST
    expect(getState().prefs.sortDefault).toBe('cost desc'); // not persisted yet
  });
});

describe('<SettingsOverlay /> test-alert inline confirmation (#207 D4)', () => {
  it('shows an inline confirmation when the test alert is queued', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({ dispatch: 'queued', alert: null }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));
    expect(await screen.findByText(/dispatched/i)).toBeTruthy();
  });

  it('shows the error, not the confirmation, on a non-queued dispatch', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({ dispatch: 'osascript-failed' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    openSettings();
    fireEvent.click(screen.getByRole('button', { name: 'Send test alert' }));
    expect(await screen.findByText(/test failed/i)).toBeTruthy();
    expect(screen.queryByText(/dispatched/i)).toBeNull();
  });
});

// Notifier dropdown (Phase B). Parent-modal integration tests: mount the real
// SettingsOverlay, seed `alerts_settings.notifier` / `command_configured`,
// open via the `s` keymap, and assert (a) the "Custom command" option is
// gated on `command_configured`, and (b) changing the select sends
// `alerts.notifier` on the POST body.
describe('<SettingsOverlay /> notifier dropdown', () => {
  it('disables the "Custom command" option when command_configured is false', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ command_configured: false });
    openSettings();

    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    const commandOption = Array.from(select.options).find(
      (o) => o.value === 'command',
    )!;
    expect(commandOption.disabled).toBe(true);
    // The label spells out where to configure the template.
    expect(commandOption.textContent).toMatch(/set via CLI/);
    // The raw template is never sent to the client; no hint surfaces when
    // unconfigured.
    expect(screen.queryByText(/Custom command configured/)).toBeNull();
  });

  it('enables the "Custom command" option when command_configured is true', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ command_configured: true, notifier: 'command' });
    openSettings();

    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    expect(select.value).toBe('command'); // seeded from the envelope
    const commandOption = Array.from(select.options).find(
      (o) => o.value === 'command',
    )!;
    expect(commandOption.disabled).toBe(false);
    // The "(set via CLI)" suffix is dropped once a template is configured.
    expect(commandOption.textContent).not.toMatch(/set via CLI/);
    // And the hint line surfaces, but never the raw template (we don't have
    // it client-side — only the boolean).
    expect(
      screen.getByText(/Custom command configured \(edit via CLI\)/),
    ).toBeInTheDocument();
  });

  it('POSTs alerts.notifier when the notifier select changes', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // Server reports the default 'auto' notifier; pick a different one.
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();

    const select = screen.getByLabelText('Alert notifier') as HTMLSelectElement;
    expect(select.value).toBe('auto'); // seeded default
    fireEvent.change(select, { target: { value: 'none' } });

    // Save commits the dirty notifier via POST /api/settings.
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      alerts?: { notifier?: string };
    };
    // Binding assertion: the selected notifier must travel in the alerts
    // block. Against a Save handler that ignored notifierDirty this fails
    // with `alerts` undefined.
    expect(parsed.alerts?.notifier).toBe('none');
  });

  it('does NOT POST alerts.notifier when the notifier is unchanged', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'osascript' });
    openSettings();

    // Touch nothing — no block is dirty, so Save makes no POST at all.
    // (SET-1: Save is disabled-when-clean by design; the click is a no-op and,
    // even if it fired, a clean save() builds an empty body and never POSTs.)
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    // No /api/settings call should fire (body would be empty).
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });
});

// Per-project budget alerts toggle (issue #19/#121). The toggle lives in the
// `budget` config block (`budget.project_alerts_enabled`), alongside the
// budget-projected toggle. Parent-modal integration tests: mount the real
// SettingsOverlay, seed `alerts_settings.project_alerts_enabled`, open via the
// `s` keymap, flip the checkbox, and assert the POST body carries
// `budget.project_alerts_enabled`.
describe('<SettingsOverlay /> per-project budget alerts toggle', () => {
  it('renders the toggle seeded from project_alerts_enabled', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ project_alerts_enabled: true });
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Per-project budget alerts/,
    }) as HTMLInputElement;
    // Seeded ON from the envelope's alerts_settings block.
    expect(toggle.checked).toBe(true);
  });

  it('POSTs budget.project_alerts_enabled when toggled on', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // Server reports the axis OFF; the user opts in.
    seedAlertsConfig({ project_alerts_enabled: false });
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Per-project budget alerts/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(false); // seeded default
    fireEvent.click(toggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      budget?: { project_alerts_enabled?: boolean };
    };
    // Binding assertion: the toggle travels in the `budget` block. Against a
    // Save handler that ignored projectAlertsDirty this fails with `budget`
    // undefined.
    expect(parsed.budget?.project_alerts_enabled).toBe(true);
  });

  it('does NOT POST when the per-project toggle is unchanged', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedAlertsConfig({ project_alerts_enabled: true });
    openSettings();

    // Touch nothing — the toggle matches the server, so Save makes no POST.
    // (SET-1: Save is disabled-when-clean; a clean save() builds an empty body.)
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });
});

// Codex budget toggles (#134). Two dashboard-writable sub-leaves of the
// nested `budget.codex` block: `alerts_enabled` and `projected_enabled`.
// Both render DISABLED + a CLI hint when no Codex budget exists
// (`codex_budget_configured:false`, Q2), and POST nested under
// `budget.codex` (partial-merge — only the dirty sub-leaf travels). Parent-
// modal integration tests mirror the per-project toggle block above.
describe('<SettingsOverlay /> Codex budget toggles', () => {
  it('renders both toggles disabled + a CLI hint when no Codex budget is configured', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: false });
    openSettings();

    const alertsToggle = screen.getByRole('checkbox', {
      name: /Codex budget alerts/,
    }) as HTMLInputElement;
    const projectedToggle = screen.getByRole('checkbox', {
      name: /Codex projected-pace alerts/,
    }) as HTMLInputElement;
    expect(alertsToggle.disabled).toBe(true);
    expect(projectedToggle.disabled).toBe(true);
    // The empty-state hint points at the CLI set command.
    expect(
      screen.getByText(/Set a Codex budget via the CLI first/),
    ).toBeInTheDocument();
    // #513 S2: the CLI-only manifest rows name this same command, so the
    // assertion is scoped to the Codex fieldset it is actually about.
    const codexFs = screen
      .getByText(/^Codex alerts/, { selector: 'legend' })
      .closest('fieldset')!;
    expect(
      within(codexFs).getByText(/cctally budget set 200 --vendor codex/),
    ).toBeInTheDocument();
  });

  it('enables + seeds both toggles when a Codex budget is configured', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({
      codex_budget_configured: true,
      codex_budget_alerts_enabled: true,
      codex_projected_enabled: true,
    });
    openSettings();

    const alertsToggle = screen.getByRole('checkbox', {
      name: /Codex budget alerts/,
    }) as HTMLInputElement;
    const projectedToggle = screen.getByRole('checkbox', {
      name: /Codex projected-pace alerts/,
    }) as HTMLInputElement;
    expect(alertsToggle.disabled).toBe(false);
    expect(projectedToggle.disabled).toBe(false);
    // Seeded ON from the envelope's alerts_settings block.
    expect(alertsToggle.checked).toBe(true);
    expect(projectedToggle.checked).toBe(true);
    // No empty-state hint once a budget exists.
    expect(screen.queryByText(/Set a Codex budget via the CLI first/)).toBeNull();
  });

  it('POSTs budget.codex.alerts_enabled (nested, no other codex keys) when toggled on', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedAlertsConfig({
      codex_budget_configured: true,
      codex_budget_alerts_enabled: false,
      codex_projected_enabled: false,
    });
    openSettings();

    const alertsToggle = screen.getByRole('checkbox', {
      name: /Codex budget alerts/,
    }) as HTMLInputElement;
    expect(alertsToggle.checked).toBe(false); // seeded default
    fireEvent.click(alertsToggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      budget?: { codex?: Record<string, unknown> };
    };
    // Binding assertion: the toggle travels nested under budget.codex, and
    // ONLY the dirty sub-leaf is sent (no projected_enabled leak).
    expect(parsed.budget?.codex?.alerts_enabled).toBe(true);
    expect('projected_enabled' in (parsed.budget?.codex ?? {})).toBe(false);
  });

  it('POSTs budget.codex.projected_enabled when the projected toggle flips on', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedAlertsConfig({
      codex_budget_configured: true,
      codex_budget_alerts_enabled: true,
      codex_projected_enabled: false,
    });
    openSettings();

    const projectedToggle = screen.getByRole('checkbox', {
      name: /Codex projected-pace alerts/,
    }) as HTMLInputElement;
    expect(projectedToggle.checked).toBe(false); // seeded default
    fireEvent.click(projectedToggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as {
      budget?: { codex?: Record<string, unknown> };
    };
    // Only the dirty projected sub-leaf travels — alerts_enabled is unchanged.
    expect(parsed.budget?.codex?.projected_enabled).toBe(true);
    expect('alerts_enabled' in (parsed.budget?.codex ?? {})).toBe(false);
  });

  it('does NOT POST when neither Codex toggle is dirty', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedAlertsConfig({
      codex_budget_configured: true,
      codex_budget_alerts_enabled: true,
      codex_projected_enabled: true,
    });
    openSettings();

    // Touch nothing — both toggles match the server, so Save makes no POST.
    // (SET-1: Save is disabled-when-clean; a clean save() builds an empty body.)
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });

  // Co-dirty flat + Codex merge (#134, code-review Minor). Flip BOTH a flat
  // Claude budget leaf (the per-project toggle → `budget.project_alerts_enabled`)
  // AND a Codex toggle (→ `budget.codex.alerts_enabled`) in ONE Save. The
  // production POST assembly spreads the pre-existing flat `body.budget` before
  // attaching `codex`, so the SINGLE POST body must carry BOTH leaves. Against a
  // naive `body.budget = { codex: codexBlock }` (no spread) the flat leaf is
  // DROPPED and this fails — the RED→GREEN proof for the spread-preservation.
  it('POSTs BOTH a flat Claude leaf and budget.codex when co-dirty in one Save', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // A Codex budget exists (toggles enabled) AND both flat + Codex leaves are
    // seeded OFF, so flipping each makes both blocks dirty.
    seedAlertsConfig({
      project_alerts_enabled: false,
      codex_budget_configured: true,
      codex_budget_alerts_enabled: false,
      codex_projected_enabled: false,
    });
    openSettings();

    // Flip the flat Claude leaf (per-project budget alerts → budget.project_alerts_enabled).
    const projectToggle = screen.getByRole('checkbox', {
      name: /Per-project budget alerts/,
    }) as HTMLInputElement;
    expect(projectToggle.checked).toBe(false); // seeded default
    fireEvent.click(projectToggle);

    // Flip the Codex leaf (Codex budget alerts → budget.codex.alerts_enabled).
    const codexToggle = screen.getByRole('checkbox', {
      name: /Codex budget alerts/,
    }) as HTMLInputElement;
    expect(codexToggle.checked).toBe(false); // seeded default
    fireEvent.click(codexToggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    // Exactly ONE settings POST — both blocks ride the same atomic round-trip.
    const settingsCalls = fetchMock.mock.calls.filter(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCalls).toHaveLength(1);
    const parsed = JSON.parse(init.body as string) as {
      budget?: {
        project_alerts_enabled?: boolean;
        codex?: Record<string, unknown>;
      };
    };
    // Binding assertion: the flat leaf is NOT dropped by the codex spread, AND
    // `codex` is nested alongside it in the SAME `budget` block.
    expect(parsed.budget?.project_alerts_enabled).toBe(true);
    expect(parsed.budget?.codex?.alerts_enabled).toBe(true);
  });
});

// cache-failure-markers spec §5 — the "Show cache-failure markers" checkbox.
// Seeds from the SSE-mirrored `dashboard_prefs` slice (markers ON by default),
// dirties independently, and travels in the SINGLE combined Save POST as
// `dashboard: { cache_failure_markers }`. Re-seeds on SSE tick (the TZ/alerts
// re-seed pattern). One modal, one Save — no split-save field drop.
function seedDashboardPrefs(cache_failure_markers: boolean) {
  act(() => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { cache_failure_markers } });
  });
}

describe('<SettingsOverlay /> cache-failure markers toggle', () => {
  it('defaults the checkbox checked (markers ON) before any tick', () => {
    render(<SettingsOverlay />);
    openSettings();
    const toggle = screen.getByRole('checkbox', {
      name: /Show cache-failure markers/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
  });

  it('seeds the checkbox from dashboard_prefs (OFF when the server reports false)', () => {
    render(<SettingsOverlay />);
    seedDashboardPrefs(false);
    openSettings();
    const toggle = screen.getByRole('checkbox', {
      name: /Show cache-failure markers/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });

  it('POSTs dashboard.cache_failure_markers=false in the combined Save body when toggled off', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // Server reports markers ON; the user opts out.
    seedDashboardPrefs(true);
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Show cache-failure markers/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true); // seeded ON
    fireEvent.click(toggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      dashboard?: { cache_failure_markers?: boolean };
    };
    // Binding assertion: the toggle travels in the `dashboard` block.
    expect(parsed.dashboard?.cache_failure_markers).toBe(false);
  });

  it('POSTs cache_failure_markers=true when re-enabled from an OFF server state', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedDashboardPrefs(false);
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Show cache-failure markers/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as {
      dashboard?: { cache_failure_markers?: boolean };
    };
    expect(parsed.dashboard?.cache_failure_markers).toBe(true);
  });

  it('does NOT POST when the markers toggle is unchanged', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedDashboardPrefs(true);
    openSettings();

    // Touch nothing — the toggle matches the server, so Save makes no POST.
    // (SET-1: Save is disabled-when-clean; a clean save() builds an empty body.)
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });

  it('re-seeds the checkbox when a fresh dashboard_prefs tick arrives while open', () => {
    render(<SettingsOverlay />);
    seedDashboardPrefs(true);
    openSettings();
    const toggle = () =>
      screen.getByRole('checkbox', { name: /Show cache-failure markers/ }) as HTMLInputElement;
    expect(toggle().checked).toBe(true);
    // A server flip (another tab's Save / CLI write) arrives via SSE.
    seedDashboardPrefs(false);
    expect(toggle().checked).toBe(false);
  });
});

// live-tail spec §4.2 — the "Live-tail new turns" checkbox. Mirrors the
// cache-failure-markers toggle exactly: seeds from the SSE-mirrored
// dashboard_prefs slice (live-tail ON by default), dirties independently, and
// travels in the SAME combined Save POST's `dashboard` block as
// `dashboard: { live_tail }`. Re-seeds on SSE tick. One modal, one Save.
function seedLiveTailPref(live_tail: boolean) {
  act(() => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { live_tail } });
  });
}

describe('<SettingsOverlay /> live-tail toggle', () => {
  it('defaults the checkbox checked (live-tail ON) before any tick', () => {
    render(<SettingsOverlay />);
    openSettings();
    const toggle = screen.getByRole('checkbox', {
      name: /Live-tail new turns/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
  });

  it('seeds the checkbox from dashboard_prefs (OFF when the server reports false)', () => {
    render(<SettingsOverlay />);
    seedLiveTailPref(false);
    openSettings();
    const toggle = screen.getByRole('checkbox', {
      name: /Live-tail new turns/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });

  it('POSTs dashboard.live_tail=false in the combined Save body when toggled off', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // Server reports live-tail ON; the user opts out.
    seedLiveTailPref(true);
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Live-tail new turns/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true); // seeded ON
    fireEvent.click(toggle);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      dashboard?: { live_tail?: boolean };
    };
    // Binding assertion: the toggle travels in the `dashboard` block.
    expect(parsed.dashboard?.live_tail).toBe(false);
  });

  it('POSTs live_tail=true when re-enabled from an OFF server state', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedLiveTailPref(false);
    openSettings();

    const toggle = screen.getByRole('checkbox', {
      name: /Live-tail new turns/,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as {
      dashboard?: { live_tail?: boolean };
    };
    expect(parsed.dashboard?.live_tail).toBe(true);
  });

  it('does NOT POST when the live-tail toggle is unchanged', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedLiveTailPref(true);
    openSettings();

    // Touch nothing — the toggle matches the server, so Save makes no POST.
    // (SET-1: Save is disabled-when-clean; a clean save() builds an empty body.)
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });

  it('carries both leaves in one dashboard block when both toggles are dirty', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    // Both server values ON; flip both off.
    act(() => {
      dispatch({
        type: 'INGEST_DASHBOARD_PREFS',
        prefs: { cache_failure_markers: true, live_tail: true },
      });
    });
    openSettings();

    fireEvent.click(
      screen.getByRole('checkbox', { name: /Show cache-failure markers/ }),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    const [, init] = call as [string, RequestInit];
    const parsed = JSON.parse(init.body as string) as {
      dashboard?: { cache_failure_markers?: boolean; live_tail?: boolean };
    };
    // Both leaves ride the SAME dashboard block in one combined POST.
    expect(parsed.dashboard?.cache_failure_markers).toBe(false);
    expect(parsed.dashboard?.live_tail).toBe(false);
  });

  it('re-seeds the checkbox when a fresh dashboard_prefs tick arrives while open', () => {
    render(<SettingsOverlay />);
    seedLiveTailPref(true);
    openSettings();
    const toggle = () =>
      screen.getByRole('checkbox', { name: /Live-tail new turns/ }) as HTMLInputElement;
    expect(toggle().checked).toBe(true);
    // A server flip (another tab's Save / CLI write) arrives via SSE.
    seedLiveTailPref(false);
    expect(toggle().checked).toBe(false);
  });
});

describe('<SettingsOverlay /> swallows `0` while open (#156)', () => {
  it('`0` does not reach the global 10th-panel opener while Settings is open', () => {
    const opener = vi.fn();
    // Stand-in for main.tsx's global `0` panel opener (scope:'global'). The
    // Settings `0` no-op is scope:'modal' → fires first by SCOPE_ORDER and
    // preventDefaults, so the opener never runs. (Non-vacuity: removing the
    // `0` no-op from SettingsOverlay lets `opener` fire.)
    registerKeymap([{ key: '0', scope: 'global', action: opener }]);
    render(<SettingsOverlay />);
    openSettings();            // 's' toggles it open
    fireEvent.keyDown(document, { key: '0' });
    expect(opener).not.toHaveBeenCalled();
    expect(getState().openModal).toBeNull();
  });
});

// #294 S5 Task 8 (supersedes SET-5 #252): the alerts fieldsets are regrouped
// into three SOURCE-scoped groups — Notifications (global notifier), Claude
// alerts (threshold master + projected weekly + a labeled Claude-budget
// subgroup), and Codex alerts (the mirrored budget.codex.* toggles). The
// two-domain "Threshold/Budget" split is retired; the underlying enablement
// semantics (the budget/Codex toggles are gated by a configured budget, NOT the
// threshold master) are unchanged and re-asserted below.
describe('<SettingsOverlay /> alerts source-scoped grouping (Task 8)', () => {
  it('renders distinct Notifications / Claude / Codex groups', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    // Scope to <legend> — the regexes also substring-match control labels.
    expect(screen.getByText(/^Notifications/i, { selector: 'legend' })).toBeInTheDocument();
    expect(screen.getByText(/^Claude alerts/i, { selector: 'legend' })).toBeInTheDocument();
    expect(screen.getByText(/^Codex alerts/i, { selector: 'legend' })).toBeInTheDocument();
  });

  it('flipping the threshold master does not disable the budget/Codex toggles', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    const master = screen.getByRole('checkbox', { name: /Enable threshold alerts/ }) as HTMLInputElement;
    // Master starts OFF (seed default enabled:false); budget/Codex stay operable.
    const projBudget = screen.getByRole('checkbox', { name: /Projected budget-\$ pace/ }) as HTMLInputElement;
    const codex = screen.getByRole('checkbox', { name: /Codex budget alerts/ }) as HTMLInputElement;
    expect(projBudget.disabled).toBe(false);
    expect(codex.disabled).toBe(false); // gated only by codex_budget_configured, not the master
    fireEvent.click(master); // turn threshold master ON
    expect(projBudget.disabled).toBe(false);
    expect(codex.disabled).toBe(false);
  });
});

// SET-2/SET-6 (#252): the three scattered Reset controls become one deferred
// "Restore defaults" fieldset. NOTHING applies until Save (closing the old
// Reset-then-close data-loss trap), and the view-pref restore is narrowed to
// the three view fields (no bulk RESET_PREFS nuking panelOrder/collapsed/etc.).
describe('<SettingsOverlay /> restore defaults — deferred, no data loss (SET-2/SET-6)', () => {
  it('staging Card order does NOT dispatch until Save, and does not drop a pending notifier edit', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    // Reorder panels so RESET_PANEL_ORDER would be observable.
    const original = [...getState().prefs.panelOrder];
    act(() => dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 }));
    const reordered = [...getState().prefs.panelOrder];
    expect(reordered).not.toEqual(original);
    openSettings();
    // Make a pending server edit AND stage a reset.
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: /Card order/i }));
    // Deferred: nothing applied yet, overlay still open.
    expect(getState().prefs.panelOrder).toEqual(reordered);
    expect(screen.getByLabelText('Alert notifier')).toBeInTheDocument(); // still open
    // Save applies both — the pending notifier survives.
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    const parsed = JSON.parse((call![1] as RequestInit).body as string);
    expect(parsed.alerts?.notifier).toBe('none');          // pending edit NOT discarded
    expect(getState().prefs.panelOrder).toEqual(original); // reset applied on Save
  });

  it('staging Table column sorting is deferred, then clears all overrides on Save', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    // A trend column override exists so the Table-sort reset button is enabled.
    act(() =>
      dispatch({ type: 'SET_TABLE_SORT', table: 'trend', override: { column: 'week', direction: 'asc' } }),
    );
    // Also dirty a server field so Save is enabled and fires a POST we can await.
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: /Table column sorting/i }));
    // Deferred: the override is untouched until Save.
    expect(getState().prefs.trendSortOverride).toEqual({ column: 'week', direction: 'asc' });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(getState().prefs.trendSortOverride).toBeNull(); // CLEAR_TABLE_SORTS applied on Save
  });

  it('a History table-sort override enables the Table-sort reset and Save clears it (S8 #254)', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    // Only a History override exists — the reset must still enable (it is the
    // fourth table axis, alongside trend/sessions/projects).
    act(() =>
      dispatch({ type: 'SET_TABLE_SORT', table: 'history', periodKind: 'week', override: { column: 'cost_usd', direction: 'asc' } }),
    );
    // Dirty a server field so Save is enabled and fires a POST we can await.
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const resetBtn = screen.getByRole('button', { name: /Table column sorting/i });
    expect(resetBtn).not.toBeDisabled();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(resetBtn);
    // Deferred: untouched until Save.
    expect(getState().prefs.historySortOverrides.week).toEqual({ column: 'cost_usd', direction: 'asc' });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(getState().prefs.historySortOverrides.week).toBeNull(); // CLEAR_TABLE_SORTS applied on Save
  });

  it('Restore view preferences resets the sort field only (narrowed), deferred', () => {
    render(<SettingsOverlay />);
    // Non-default sort saved AND a non-default panelOrder, so narrowing is non-vacuous.
    act(() => {
      dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc' } });
      dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    });
    const panelOrderBefore = [...getState().prefs.panelOrder];
    openSettings();
    fireEvent.click(screen.getByRole('button', { name: /Restore view preferences/i }));
    // The Sort-default working field flips to the default, but nothing is
    // persisted yet and panelOrder is untouched (proves the narrowing).
    const startedRadio = screen.getByRole('radio', { name: /Started \(newest first\)/ }) as HTMLInputElement;
    expect(startedRadio.checked).toBe(true);
    expect(getState().prefs.sortDefault).toBe('cost desc');        // not yet saved
    expect(getState().prefs.panelOrder).toEqual(panelOrderBefore); // NOT nuked (old RESET_PREFS would)
  });
});

// SET-1 (#252): each section whose fields are dirty gets a per-fieldset
// `.is-changed` marker (the decorative half of the dirty feedback; the Save
// badge is the authoritative machine-readable signal).
describe('<SettingsOverlay /> per-fieldset changed markers (SET-1)', () => {
  it('marks the Notifications fieldset changed after a notifier edit, not Sort default', () => {
    // #294 S5 Task 8 — the notifier now lives in its own global "Notifications"
    // group, so a notifier edit marks THAT fieldset (not the Claude group).
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    // Scope to <legend> so the regex doesn't also match control labels.
    const notif = screen.getByText(/^Notifications/i, { selector: 'legend' }).closest('fieldset')!;
    const sortFs = screen.getByText(/Sort default/i, { selector: 'legend' }).closest('fieldset')!;
    expect(notif.className).toMatch(/is-changed/);
    expect(sortFs.className).not.toMatch(/is-changed/);
  });

  // The one field this session added was the one field its owning fieldset's
  // aggregation left out. The rail and the section heading tracked it, so the
  // marker appeared and disappeared depending on which SIBLING happened to be
  // dirty at the time — the legend went quiet again the moment a sibling was
  // reverted, while the budget was still unsaved.
  it('marks the Claude fieldset changed for the weekly budget on its own', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ weekly_usd: 200 });
    openSettings();
    const claude = () =>
      screen.getByText(/^Claude alerts/i, { selector: 'legend' }).closest('fieldset')!;
    expect(claude().className).not.toMatch(/is-changed/);
    fireEvent.change(screen.getByLabelText(/Weekly budget/i), { target: { value: '300' } });
    expect(claude().className, 'the budget is dirty but its fieldset says nothing').toMatch(
      /is-changed/,
    );
    expect(claude().querySelector('legend .fs-changed')).toBeTruthy();
    // And the marker must not depend on a sibling: dirty a sibling, revert it,
    // and the budget alone must still hold the fieldset marked.
    const sibling = screen.getByRole('checkbox', { name: /Enable threshold alerts/ });
    fireEvent.click(sibling);
    fireEvent.click(sibling);
    expect(claude().className).toMatch(/is-changed/);
  });
});

describe('<SettingsOverlay /> Card-order restore gating (review P3)', () => {
  it('disables the Card order toggle at the default panel order, enables it after a reorder', () => {
    render(<SettingsOverlay />);
    openSettings();
    const cardBtn = () =>
      screen.getByRole('button', { name: /^Card order/ }) as HTMLButtonElement;
    // Fresh state → default panel order → nothing to restore.
    expect(cardBtn().disabled).toBe(true);
    // A user reorder makes the reset meaningful → the toggle enables.
    act(() => dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 }));
    expect(cardBtn().disabled).toBe(false);
  });
});

// Beta-channel (spec 2026-07-21 §3): the Update-channel toggle seeds from the
// SSE-mirrored update.configured_channel and POSTs `update.channel` on Save.
function seedChannel(channel: 'stable' | 'beta') {
  act(() => {
    dispatch({
      type: 'SET_UPDATE_STATE',
      state: {
        current_version: '1.5.0',
        latest_version: '1.9.0',
        available: true,
        method: 'npm',
        update_command:
          channel === 'beta'
            ? 'npm install -g cctally@1.9.0'
            : 'npm install -g cctally@latest',
        release_notes_url: null,
        check_status: 'ok',
        checked_at_utc: null,
        prerelease_note: null,
        configured_channel: channel,
      },
      suppress: { skipped_versions: [], remind_after: null },
    });
  });
}

describe('<SettingsOverlay /> update channel toggle', () => {
  it('seeds the toggle from the envelope configured_channel', () => {
    render(<SettingsOverlay />);
    seedChannel('beta');
    openSettings();
    const beta = screen.getByRole('radio', { name: 'Beta' }) as HTMLInputElement;
    expect(beta.checked).toBe(true);
  });

  it('POSTs update.channel when the toggle flips to beta', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsOverlay />);
    seedChannel('stable');
    openSettings();

    const beta = screen.getByRole('radio', { name: 'Beta' }) as HTMLInputElement;
    expect(beta.checked).toBe(false); // seeded stable
    fireEvent.click(beta);

    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
    expect(call).toBeTruthy();
    const [, init] = call as [string, RequestInit];
    expect(init.method).toBe('POST');
    const parsed = JSON.parse(init.body as string) as {
      update?: { channel?: string };
    };
    // Binding assertion: the flipped channel travels in the update block.
    expect(parsed.update?.channel).toBe('beta');
  });
});


// ---------------------------------------------------------------------------
// #513 S2 — consolidated here from the five superseded settings suites. Every
// callback below is byte-identical to its origin; only import specifiers moved
// (the `__tests__/` blocks resolved one directory further out) and the
// Table-column-sorting suite's private `openSettings(user)` helper now lives
// inside its own describe so it shadows this file's `openSettings()`.
// Origins: src/components/SettingsOverlay.lanAuth.test.tsx,
// src/components/SettingsOverlay.source.test.tsx,
// __tests__/SettingsOverlay.test.tsx, __tests__/SettingsOverlay-layout.test.tsx,
// __tests__/SettingsOverlay-tableSort.test.tsx.
// ---------------------------------------------------------------------------

describe('<SettingsOverlay /> LAN authentication', () => {
  it('defaults the restart-only access-token toggle on', () => {
    render(<SettingsOverlay />);
    openSettings();
    const toggle = screen.getByRole('checkbox', {
      name: /Require LAN access token/i,
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(screen.getByText(/only after restarting the dashboard/i)).toBeVisible();
  });

  it('persists the opt-out, stays open, and reports the required restart', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => (
      new Response(JSON.stringify({
        dashboard: {
          cache_failure_markers: true,
          live_tail: true,
          lan_auth: false,
        },
        restart_required: ['dashboard.lan_auth'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    ));
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    openSettings();

    fireEvent.click(screen.getByRole('checkbox', {
      name: /Require LAN access token/i,
    }));
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      dashboard: { lan_auth: false },
    });
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeVisible();
    expect(screen.getByText(
      'Saved. Restart the dashboard to apply LAN access authentication.',
    )).toBeVisible();
  });

  it('seeds an explicit server opt-out as unchecked', () => {
    act(() => {
      dispatch({
        type: 'INGEST_DASHBOARD_PREFS',
        prefs: { lan_auth: false },
      });
    });
    render(<SettingsOverlay />);
    openSettings();
    expect((screen.getByRole('checkbox', {
      name: /Require LAN access token/i,
    }) as HTMLInputElement).checked).toBe(false);
  });
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
    const { dispatch } = await import('../store/store');
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
      // #513 S2 §2.3 protocol 3: the test action's output moved to the
      // PERSISTENT error region, because a response can arrive after its row
      // has been filtered away.
      await waitFor(() => {
        const err = document.querySelector('.settings-error-summary');
        expect(err).toBeTruthy();
        expect(err?.textContent ?? '').toContain('spawn_error');
      });
      uninstallGlobalKeydown();
    });

    it('renders read-only threshold summary line from alertsConfig (spec §8.1)', async () => {
      // Seed the store with a non-default thresholds payload via SSE
      // path, then reopen Settings and verify the summary reflects it.
      const { dispatch } = await import('../store/store');
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

    // §5.2's positive-amount half. Only the blank→`null` case was covered, and
    // "no budget" is the one value the endpoint accepts as a bare null, so a
    // client that never sent a NUMBER would have passed that test.
    it('a positive weekly budget reaches the endpoint as a number', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
      vi.stubGlobal('fetch', fetchMock);
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const amount = screen.getByLabelText(/Weekly budget/i) as HTMLInputElement;
      expect(amount.value).toBe(''); // no budget configured
      fireEvent.change(amount, { target: { value: '200' } });
      await user.click(screen.getByRole('button', { name: /^Save/ }));
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          '/api/settings',
          expect.objectContaining({ method: 'POST' }),
        );
      });
      const call = fetchMock.mock.calls.find((c) => c[0] === '/api/settings')!;
      expect(JSON.parse(call[1].body as string)).toEqual({
        budget: { weekly_usd: 200 },
      });
      uninstallGlobalKeydown();
    });

    it('projected test axis option appears in the Send-test-alert axis select (issue #121)', async () => {
      render(<SettingsOverlay />);
      const user = userEvent.setup();
      await user.keyboard('s');
      const select = document.querySelector(
        'select[aria-label="Alert type"]',
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

// S6 (#252): the old instant "Reset card order" button (under a "Layout"
// legend) is now the deferred "Card order" toggle inside the consolidated
// "Restore defaults" fieldset. RESET_PANEL_ORDER is applied only on Save.
describe('<SettingsOverlay /> Card order reset (deferred)', () => {
  it('shows a "Card order" button in the Restore defaults fieldset', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    expect(
      screen.getByText(/Restore defaults/i, { selector: 'legend' }),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: /Card order/i })).toBeTruthy();
  });

  it('staging "Card order" is deferred, then Save restores DEFAULT_PANEL_ORDER and leaves other prefs', async () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsPerPage: 250 } });
    const reordered = [...getState().prefs.panelOrder];
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    await user.click(screen.getByRole('button', { name: /Card order/i }));
    // Deferred: not applied yet.
    expect(getState().prefs.panelOrder).toEqual(reordered);
    // Save applies RESET_PANEL_ORDER; the untouched sessions-per-page survives.
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
    expect(getState().prefs.sessionsPerPage).toBe(250);
  });
});

// S6 (#252): the old instant "Reset table sorting" button (in a `sorting-fs`
// fieldset) is now the deferred "Table column sorting" toggle inside the
// consolidated "Restore defaults" fieldset. Clicking it STAGES the reset
// (aria-pressed) and does NOT close the overlay; CLEAR_TABLE_SORTS is applied
// only on Save, and the disabled predicate now checks all three overrides
// (trend + sessions + projects) plus the staged flag.
describe('SettingsOverlay — Table column sorting reset (deferred)', () => {
  async function openSettings(user: ReturnType<typeof userEvent.setup>) {
    await user.keyboard('s');
  }

  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });

  afterEach(() => {
    uninstallGlobalKeydown();
  });

  it('renders a "Table column sorting" button when overlay is open', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', { name: /Table column sorting/i });
    expect(btn).not.toBeNull();
  });

  it('button is disabled when no override exists and nothing is staged', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('button is enabled when at least one override exists (any of the three)', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'projects',
        override: { column: 'cost', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('clicking stages the reset (deferred) — overlay stays open, overrides untouched until Save', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    await user.click(btn);
    // Staged, not applied: aria-pressed flips, override survives, overlay open.
    expect(btn.getAttribute('aria-pressed')).toBe('true');
    expect(getState().prefs.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
    expect(document.getElementById('settings-root')).toBeTruthy();
  });

  it('Save applies the staged reset: CLEAR_TABLE_SORTS clears all three overrides', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'trend',
        override: { column: 'week', direction: 'asc' },
      });
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'projects',
        override: { column: 'used', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    // Stage the table-sort reset (this alone dirties the form → Save enabled).
    await user.click(screen.getByRole('button', { name: /Table column sorting/i }));
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    expect(getState().prefs.projectsSortOverride).toBeNull();
    // Applied on Save → overlay closed.
    expect(document.getElementById('settings-root')).toBeNull();
  });

  it('Save does NOT clear column sorts when nothing is staged and the sort default is unchanged', async () => {
    // The old code unconditionally dispatched SET_TABLE_SORT sessions null on
    // every Save (the Codex blocker). Now an unrelated / no-op Save must not
    // touch the user's column-click sorts. With only overrides present and no
    // dirty field, Save is disabled-when-clean, so the override survives.
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing dirty → disabled
    await user.click(save);
    expect(getState().prefs.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
  });

  it('Save applies a staged table-sort reset: all FOUR overrides clear together', async () => {
    act(() => {
      dispatch({ type: 'SET_TABLE_SORT', table: 'trend', override: { column: 'cost', direction: 'desc' } });
      dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: { column: 'cost', direction: 'desc' } });
      dispatch({ type: 'SET_TABLE_SORT', table: 'projects', override: { column: 'cost', direction: 'desc' } });
      dispatch({ type: 'SET_TABLE_SORT', table: 'history', periodKind: 'month', override: { column: 'cost', direction: 'desc' } });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    await user.click(screen.getByRole('button', { name: /Table column sorting/i }));
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    expect(getState().prefs.projectsSortOverride).toBeNull();
    expect(getState().prefs.historySortOverrides).toEqual({ week: null, month: null });
  });
});
