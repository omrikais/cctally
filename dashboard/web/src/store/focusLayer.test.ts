import { describe, it, expect } from 'vitest';
import { topmostStoreFocusLayer, type UIState } from './store';

function s(partial: Partial<UIState>): UIState {
  return {
    openModal: null,
    shareModal: null,
    composerModal: null,
    doctorModalOpen: false,
    update: { modalOpen: false },
    ...partial,
  } as UIState;
}

describe('topmostStoreFocusLayer', () => {
  it('returns null when nothing store-tracked is open', () => {
    expect(topmostStoreFocusLayer(s({}))).toBeNull();
  });
  it('returns "panel" for an open panel modal', () => {
    expect(topmostStoreFocusLayer(s({ openModal: 'projects' as never }))).toBe('panel');
  });
  it('share above a panel modal yields "share" (so the panel suspends)', () => {
    expect(
      topmostStoreFocusLayer(s({ openModal: 'projects' as never, shareModal: {} as never })),
    ).toBe('share');
  });
  it('composer outranks share', () => {
    expect(
      topmostStoreFocusLayer(s({ shareModal: {} as never, composerModal: {} as never })),
    ).toBe('composer');
  });
});
