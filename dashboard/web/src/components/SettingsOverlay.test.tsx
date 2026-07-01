// SettingsOverlay — "Send test alert" axis picker (issue #19 follow-up).
// Parent-modal integration test: mount the real SettingsOverlay, open it
// through the `s` keymap (the production open path), pick a non-default
// axis in the <select>, fire the test-alert button, and assert the POST
// body carries the SELECTED axis (not the old hardcoded 'weekly').
//
// This is the binding assertion: it must pin `axis === 'budget'`
// specifically so it fails against the prior hardcoded body. See the
// RED→GREEN non-vacuity proof in the implementor report.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { AlertsConfig } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
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

    // The overlay is now open — the axis <select> is labelled for a11y.
    const select = screen.getByLabelText('Test alert axis') as HTMLSelectElement;
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

    // The metric sub-select is hidden until the projected axis is chosen.
    expect(screen.queryByLabelText('Test alert projected metric')).toBeNull();

    const select = screen.getByLabelText('Test alert axis') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'projected' } });

    // Now the metric chooser appears; pick the budget_usd variant.
    const metricSelect = screen.getByLabelText(
      'Test alert projected metric',
    ) as HTMLSelectElement;
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
    expect(
      screen.getByText(/cctally budget set 200 --vendor codex/),
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

// SET-1 (#252): the unified deferred-commit form surfaces its pending-edit
// count on the Save button and disables Save when nothing is dirty — the
// missing "unsaved changes" feedback the issue called out. The Task-1 backbone
// also fixes the Codex blocker: an unrelated Save (e.g. only alerts.notifier)
// must NOT clobber the user's Recent-Sessions column-click sort.
describe('<SettingsOverlay /> dirty-state feedback (SET-1)', () => {
  it('Save reads plain "Save" and is disabled when nothing is dirty', () => {
    render(<SettingsOverlay />);
    openSettings();
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.textContent).toBe('Save');
    expect(save.disabled).toBe(true);
  });

  it('badges the change count and enables Save as fields dirty', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    const save = () => screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    expect(save().textContent).toBe('Save · 1 change');
    expect(save().disabled).toBe(false);
    // a second, different field (the Conversation-viewer live-tail toggle)
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    expect(save().textContent).toBe('Save · 2 changes');
    // revert both → back to disabled plain Save
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'auto' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Live-tail new turns/ }));
    expect(save().textContent).toBe('Save');
    expect(save().disabled).toBe(true);
  });

  it('does NOT clear the sessions column sort when only a server field is saved', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    // Simulate a user-set Recent Sessions column sort override. SortOverride's
    // real shape is { column, direction } (src/lib/tableSort.ts) — the plan
    // skeleton's { key, dir } is not the store type.
    act(() =>
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      }),
    );
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // The override the user never touched must survive an unrelated Save.
    expect(getState().prefs.sessionsSortOverride).toEqual({ column: 'cost', direction: 'desc' });
  });
});

// SET-5 (#252): the flat Alerts fieldset is split into two REAL domains —
// Threshold (governed by the `alerts.enabled` master) and Budget (gated by a
// configured budget, INDEPENDENT of the master). Guards against the issue's
// literal-but-wrong "one master over all" nesting.
describe('<SettingsOverlay /> alerts two-domain grouping (SET-5)', () => {
  it('renders distinct Threshold / Budget / Test groups', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ codex_budget_configured: true });
    openSettings();
    // Scope to <legend> — the regexes also substring-match control labels
    // ("Enable threshold alerts", "Codex budget alerts", …).
    expect(screen.getByText(/Threshold alerts/i, { selector: 'legend' })).toBeInTheDocument();
    expect(screen.getByText(/Budget alerts/i, { selector: 'legend' })).toBeInTheDocument();
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
  it('marks the Threshold-alerts fieldset changed after a notifier edit, not Sort default', () => {
    render(<SettingsOverlay />);
    seedAlertsConfig({ notifier: 'auto' });
    openSettings();
    fireEvent.change(screen.getByLabelText('Alert notifier'), { target: { value: 'none' } });
    // Scope to <legend> so the regex doesn't also match control labels.
    const thresh = screen.getByText(/Threshold alerts/i, { selector: 'legend' }).closest('fieldset')!;
    const sortFs = screen.getByText(/Sort default/i, { selector: 'legend' }).closest('fieldset')!;
    expect(thresh.className).toMatch(/is-changed/);
    expect(sortFs.className).not.toMatch(/is-changed/);
  });
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
