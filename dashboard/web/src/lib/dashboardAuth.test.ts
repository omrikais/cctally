import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DashboardAuthError,
  bootstrapDashboardAuth,
  renderDashboardAuthFailure,
} from './dashboardAuth';

describe('dashboard LAN auth bootstrap', () => {
  beforeEach(() => {
    history.replaceState(null, '', '/dashboard');
    localStorage.clear();
    sessionStorage.clear();
    document.body.innerHTML = '<div id="root"></div>';
  });

  it('exchanges only an exact token fragment and erases it before fetch', async () => {
    history.replaceState(null, '', '/dashboard?x=1#token=run-token');
    let hashDuringFetch = 'unread';
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      hashDuringFetch = location.hash;
      expect(init?.headers).toEqual({ Authorization: 'Bearer run-token' });
      return new Response(null, { status: 204 });
    });
    vi.stubGlobal('fetch', fetchMock);

    await bootstrapDashboardAuth();

    expect(fetchMock).toHaveBeenCalledWith('/api/auth', {
      method: 'POST',
      headers: { Authorization: 'Bearer run-token' },
    });
    expect(hashDuringFetch).toBe('');
    expect(location.pathname + location.search).toBe('/dashboard?x=1');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('is a no-op on cookie reloads and preserves conversation hashes', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await bootstrapDashboardAuth();
    expect(fetchMock).not.toHaveBeenCalled();

    history.replaceState(null, '', '/#/conversations/session-1');
    await bootstrapDashboardAuth();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(location.hash).toBe('#/conversations/session-1');
  });

  it('clears malformed token fragments and throws a typed failure', async () => {
    history.replaceState(null, '', '/#token=%E0%A4%A');
    await expect(bootstrapDashboardAuth()).rejects.toBeInstanceOf(
      DashboardAuthError,
    );
    expect(location.hash).toBe('');
  });

  it('throws on a non-204 exchange without persisting the token', async () => {
    history.replaceState(null, '', '/#token=secret-token');
    vi.stubGlobal('fetch', vi.fn(async () => (
      new Response('{"error":"unauthorized"}', { status: 401 })
    )));

    await expect(bootstrapDashboardAuth()).rejects.toBeInstanceOf(
      DashboardAuthError,
    );
    expect(location.hash).toBe('');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('renders a token-free failure surface', () => {
    const root = document.getElementById('root') as HTMLElement;
    renderDashboardAuthFailure(root);
    expect(root).toHaveTextContent(/authentication failed/i);
    expect(root.textContent).not.toContain('secret-token');
  });
});
