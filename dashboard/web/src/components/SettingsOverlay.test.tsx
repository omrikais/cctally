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
import { _resetForTests, dispatch } from '../store/store';
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
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    // No /api/settings call should fire (body would be empty).
    const settingsCall = fetchMock.mock.calls.find(
      ([url]) => url === '/api/settings',
    );
    expect(settingsCall).toBeUndefined();
  });
});
