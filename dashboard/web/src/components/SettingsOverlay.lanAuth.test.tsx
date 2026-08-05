import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { _resetForTests, dispatch } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as resetKeymap,
} from '../store/keymap';

function openSettings() {
  fireEvent.keyDown(document, { key: 's' });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

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
