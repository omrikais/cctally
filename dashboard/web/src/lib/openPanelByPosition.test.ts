import { describe, it, expect, beforeEach } from 'vitest';
import { openPanelByPosition } from './openPanelByPosition';
import { _resetForTests, getState, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

const FAKE_ENV = { header: {} } as unknown as Envelope;

beforeEach(() => { localStorage.clear(); _resetForTests(); });

describe('openPanelByPosition — no-data guard (B2/B3)', () => {
  it('is a no-op when no snapshot has loaded', () => {
    expect(getState().snapshot).toBeNull();
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
  });

  it('opens a panel modal once a snapshot exists', () => {
    updateSnapshot(FAKE_ENV);
    openPanelByPosition(1);
    expect(getState().openModal).not.toBeNull();
  });
});
