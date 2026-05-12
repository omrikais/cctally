// Spec §8.5 / §10.4 — the formula behind the composer's real-name banner.
//
// The server overrides per-section reveal_projects with the composer's
// composite value at compose time, so the banner predicate ignores
// add-time reveal entirely. See anonFormula.ts header for the privacy
// rationale (Codex P1 on PR #35).
import { describe, expect, it } from 'vitest';
import { bannerVisible } from './anonFormula';

describe('anon banner formula (spec §8.5)', () => {
  it.each<[number, boolean, boolean]>([
    // sectionCount, compositeReveal, expected
    [0, false, false],
    [0, true,  false],   // empty basket: nothing to warn about
    [1, false, false],   // composite anon: no reveal possible
    [1, true,  true],    // composite reveal + ≥1 section: warn
    [3, false, false],
    [3, true,  true],
  ])('sectionCount=%s, compositeReveal=%s → bannerVisible=%s',
    (count, composite, expected) => {
      expect(bannerVisible(count, composite)).toBe(expected);
    });

  it('anonymous-at-add sections still trip the banner under composite reveal', () => {
    // Regression: under the prior AND formula a single anonymous-at-add
    // basket item would silence the banner even though the composite
    // override would still reveal names on export. Now the section's
    // add-time anon state has no effect on banner visibility — only
    // the composite + non-empty-basket gate does.
    expect(bannerVisible(1, true)).toBe(true);
  });
});
