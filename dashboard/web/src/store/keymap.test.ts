// Dispatcher-level coverage for the #156 view filter + scope-aware default,
// and the SCOPE_ORDER-over-insertion-order Esc contract. Uses synthetic
// bindings (no React render) driven through the real document keydown
// listener — the same wiring production installs.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import { _resetForTests as _resetStore, dispatch } from './store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  _resetForTests as _resetKeymap,
} from './keymap';

beforeEach(() => {
  _resetStore();   // view resets to 'dashboard'
  _resetKeymap();
  installGlobalKeydown();
});
afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
});

describe('keymap view filter', () => {
  it('default-scope global binding fires in dashboard, is inert in conversations', () => {
    const fn = vi.fn();
    registerKeymap([{ key: 'x', scope: 'global', action: fn }]);
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).toHaveBeenCalledTimes(1);
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).toHaveBeenCalledTimes(1); // unchanged — filtered out in conversations
  });

  it("view:'conversations' binding fires only in conversations", () => {
    const fn = vi.fn();
    registerKeymap([{ key: 'x', scope: 'global', view: 'conversations', action: fn }]);
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).not.toHaveBeenCalled(); // dashboard
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("view:'any' binding fires in both views", () => {
    const fn = vi.fn();
    registerKeymap([{ key: 'x', scope: 'global', view: 'any', action: fn }]);
    fireEvent.keyDown(document, { key: 'x' });
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("scope:'modal' binding defaults to 'any' (fires in both views)", () => {
    const fn = vi.fn();
    registerKeymap([{ key: 'x', scope: 'modal', action: fn }]);
    fireEvent.keyDown(document, { key: 'x' });
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("scope:'sessions' binding defaults to 'dashboard' (inert in conversations)", () => {
    const fn = vi.fn();
    registerKeymap([{ key: 'x', scope: 'sessions', action: fn }]);
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'x' });
    expect(fn).not.toHaveBeenCalled();
  });

  it('non-vacuity control: with no view filter a global binding WOULD fire — so the inert assertions above are real', () => {
    // A view:'any' binding is the "filter disabled" control: it proves the
    // keydown plumbing reaches the action in conversations view, so the
    // "not called" assertions for default-dashboard bindings are caused by
    // the filter, not by dead plumbing.
    const fn = vi.fn();
    registerKeymap([{ key: 'z', scope: 'global', view: 'any', action: fn }]);
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'z' });
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

describe('Esc layering is SCOPE_ORDER-driven, not insertion-order', () => {
  it('overlay Esc beats a global Esc registered earlier, in conversations view', () => {
    const convEsc = vi.fn();
    const overlayEsc = vi.fn();
    // Register the conversations (global) Esc FIRST and the overlay Esc SECOND
    // — reversed from real mount order. SCOPE_ORDER must still pick overlay.
    registerKeymap([{ key: 'Escape', scope: 'global', view: 'conversations', action: convEsc }]);
    registerKeymap([{ key: 'Escape', scope: 'overlay', when: () => true, action: overlayEsc }]);
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(overlayEsc).toHaveBeenCalledTimes(1);
    expect(convEsc).not.toHaveBeenCalled();
  });
});

describe('layer breaks same-scope ties (#159)', () => {
  it('higher layer fires first even when registered later', () => {
    const lower = vi.fn();
    const higher = vi.fn();
    // Register the LOWER layer FIRST so insertion order would favour it.
    // The layer tiebreaker must pick the higher-layer binding anyway.
    registerKeymap([{ key: 'Escape', scope: 'overlay', layer: 1, when: () => true, action: lower }]);
    registerKeymap([{ key: 'Escape', scope: 'overlay', layer: 2, when: () => true, action: higher }]);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(higher).toHaveBeenCalledTimes(1);
    expect(lower).not.toHaveBeenCalled();
  });

  it('layerless same-scope bindings keep insertion order (stable sort, no regression)', () => {
    const first = vi.fn();
    const second = vi.fn();
    registerKeymap([{ key: 'Escape', scope: 'overlay', when: () => true, action: first }]);
    registerKeymap([{ key: 'Escape', scope: 'overlay', when: () => true, action: second }]);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).not.toHaveBeenCalled();
  });
});
