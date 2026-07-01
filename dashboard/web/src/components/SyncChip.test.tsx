import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { _resetForTests } from '../store/store';

vi.mock('../hooks/useSnapshot', () => ({ useSnapshot: () => ({ sync_age_s: 480, last_sync_error: null }) }));
vi.mock('../hooks/useConnectionStatus', () => ({ useConnectionStatus: () => ({ disconnected: false }) }));

import { SyncChip } from './SyncChip';

describe('SyncChip freshness (SYNC-1)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });
  it('humanizes the age and tags the aging bucket', () => {
    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toContain('8m ago');
    expect(span.className).toContain('sync-chip--aging');
  });
});
