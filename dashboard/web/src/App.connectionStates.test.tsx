import { render } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { App } from './App';
import { _resetForTests as _resetStore, updateSnapshot } from './store/store';
import * as conn from './hooks/useConnectionStatus';
import type { Envelope } from './types/envelope';

const ENV = { header: {}, sessions: { total: 0, rows: [], sort_key: 'started_desc' } } as unknown as Envelope;

function stubConn(disconnected: boolean, bootstrapError: boolean) {
  vi.spyOn(conn, 'useConnectionStatus').mockReturnValue({ disconnected, bootstrapError });
}

beforeEach(() => { localStorage.clear(); _resetStore(); vi.restoreAllMocks(); });

describe('App connection states (B2/B3)', () => {
  it('loading: skeleton grid, no banner, no live panels', () => {
    stubConn(false, false); // env stays null
    const { container } = render(<App />);
    expect(container.querySelector('.panel.is-skeleton')).not.toBeNull();
    expect(container.querySelector('.stale-banner')).toBeNull();
  });

  it('error: error banner, no skeleton', () => {
    stubConn(false, true);
    const { container } = render(<App />);
    expect(container.querySelector('.stale-banner-error')).not.toBeNull();
    expect(container.querySelector('.panel.is-skeleton')).toBeNull();
  });

  it('ready + disconnected: live grid dimmed + stale banner', () => {
    stubConn(true, false);
    updateSnapshot(ENV);
    const { container } = render(<App />);
    expect(container.querySelector('.stale-banner-stale')).not.toBeNull();
    expect(container.querySelector('.grid.is-stale')).not.toBeNull();
    expect(container.querySelector('.panel.is-skeleton')).toBeNull();
  });

  it('ready + connected: live grid, no banner, no dim', () => {
    stubConn(false, false);
    updateSnapshot(ENV);
    const { container } = render(<App />);
    expect(container.querySelector('.stale-banner')).toBeNull();
    expect(container.querySelector('.grid.is-stale')).toBeNull();
  });
});
