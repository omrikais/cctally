// Spec §10.5 — the formula behind the composer's real-name banner.
//
// We parameterize all four truth-table combos so a future edit to the
// formula can't silently drift the predicate (e.g. flipping `&&` → `||`
// would surface here, not just downstream in the composer UI).
import { describe, expect, it } from 'vitest';
import { bannerVisible, effectiveReveal } from './anonFormula';

describe('anon formula (spec §10.5)', () => {
  // Truth table: effective_reveal[i] = composite_reveal && section_reveal_at_add[i]
  it.each<[boolean, boolean, boolean]>([
    // composite, section, expected effective_reveal
    [false, false, false],
    [false, true,  false],
    [true,  false, false],
    [true,  true,  true],
  ])('composite=%s, section=%s → effective_reveal=%s',
    (comp, sec, expected) => {
      expect(effectiveReveal(sec, comp)).toBe(expected);
    });

  it('banner_visible = sections.some(effectiveReveal=true)', () => {
    // All anon at add-time → composite reveal doesn't matter, banner hidden.
    expect(bannerVisible([false, false], true)).toBe(false);
    // One section captured with reveal → composite reveal exposes it → banner visible.
    expect(bannerVisible([false, true], true)).toBe(true);
    // Composite anon → all sections suppressed → banner hidden.
    expect(bannerVisible([true, true], false)).toBe(false);
  });

  it('banner_visible on an empty sections list is false', () => {
    // Trivially: Array.prototype.some on [] is false. Documenting it
    // here because the composer's empty-state path renders no banner.
    expect(bannerVisible([], true)).toBe(false);
    expect(bannerVisible([], false)).toBe(false);
  });
});
