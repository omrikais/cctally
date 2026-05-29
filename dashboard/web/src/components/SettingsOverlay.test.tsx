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
});
