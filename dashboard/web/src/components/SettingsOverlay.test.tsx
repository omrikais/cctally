// SettingsOverlay — "Send test alert" axis picker (issue #19 follow-up).
// Parent-modal integration test: mount the real SettingsOverlay, open it
// through the `s` keymap (the production open path), pick a non-default
// axis in the <select>, fire the test-alert button, and assert the POST
// body carries the SELECTED axis (not the old hardcoded 'weekly').
//
// This is the binding assertion: it must pin `axis === 'budget'`
// specifically so it fails against the prior hardcoded body. See the
// RED→GREEN non-vacuity proof in the implementor report.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';

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
