import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { _resetForTests, dispatch } from '../store/store';

const mocked = vi.hoisted(() => ({
  env: {
    sync_age_s: 480 as number | null,
    last_sync_error: null as string | null,
    sync_failure: null as null | {
      kind: string;
      label: string;
      detail: string;
      action: string | null;
    },
  },
  disconnected: false,
}));

vi.mock('../hooks/useSnapshot', () => ({ useSnapshot: () => mocked.env }));
vi.mock('../hooks/useConnectionStatus', () => ({
  useConnectionStatus: () => ({ disconnected: mocked.disconnected }),
}));

import { SyncChip } from './SyncChip';

describe('SyncChip freshness (SYNC-1)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    mocked.env = {
      sync_age_s: 480,
      last_sync_error: null,
      sync_failure: null,
    };
    mocked.disconnected = false;
  });
  it('humanizes the age and tags the aging bucket', () => {
    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toContain('8m ago');
    expect(span.className).toContain('sync-chip--aging');
  });

  it('renders an actionable server cache-corruption state', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'raw path must not render',
      sync_failure: {
        kind: 'cache_corruption',
        label: '⚠ cache recovery needed',
        detail: 'The server cache database could not be read safely.',
        action: 'Run cctally cache-sync --rebuild.',
      },
    };

    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toBe('⚠ cache recovery needed');
    expect(span.getAttribute('title')).toContain('cctally cache-sync --rebuild');
    expect(span.getAttribute('title')).not.toContain('raw path');
    expect(span.className).toContain('sync-error');
  });

  it('distinguishes active server maintenance from a client disconnect', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'maintenance raw detail',
      sync_failure: {
        kind: 'maintenance_active',
        label: 'cache repair in progress',
        detail: 'Another cctally process is repairing the server cache.',
        action: null,
      },
    };
    const active = render(<SyncChip />);
    expect(active.container.querySelector('#sync-chip')!.textContent)
      .toBe('cache repair in progress');
    active.unmount();

    mocked.disconnected = true;
    const disconnected = render(<SyncChip />);
    const disconnectedChip = disconnected.container.querySelector('#sync-chip')!;
    expect(disconnectedChip.textContent).toBe('disconnected');
    expect(disconnectedChip.className).toContain('sync-error');
  });

  it('labels a failed manual POST as a client sync-request failure', () => {
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });

    const { container } = render(<SyncChip />);

    expect(container.querySelector('#sync-chip')!.textContent)
      .toBe('⚠ sync request failed');
  });
});
