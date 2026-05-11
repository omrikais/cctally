import { describe, expect, it } from 'vitest';
import { shareReducer, initialShareState, openShareModal, closeShareModal } from './shareSlice';

describe('shareSlice', () => {
  it('openShareModal sets the panel', () => {
    const next = shareReducer(initialShareState, openShareModal('weekly', null));
    expect(next.shareModal).not.toBeNull();
    expect(next.shareModal?.panel).toBe('weekly');
  });

  it('closeShareModal clears the slot', () => {
    const open = shareReducer(initialShareState, openShareModal('daily', null));
    const next = shareReducer(open, closeShareModal());
    expect(next.shareModal).toBeNull();
  });

  it('opening one panel replaces the previous', () => {
    const a = shareReducer(initialShareState, openShareModal('weekly', null));
    const b = shareReducer(a, openShareModal('daily', null));
    expect(b.shareModal?.panel).toBe('daily');
  });
});
