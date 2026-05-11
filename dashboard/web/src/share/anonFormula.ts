// Real-name banner formula — spec §10.5.
//
// The composer's privacy nudge has one defining rule:
//
//   effective_reveal[i] = composite_reveal_projects && section_reveal_at_add_time[i]
//   banner_visible      = sections.some(i => effective_reveal[i])
//
// In words: a section's projects appear in the composed output ONLY IF
// both the composite ("Anon on export" UNCHECKED, i.e. composite_reveal
// = true) AND the section was added with reveal_projects = true. If
// composite "Anon on export" is checked, every section is anonymized
// regardless of how it was captured — the banner disappears.
//
// Lifted into its own module so the formula is the single source of
// truth: the modal renders against it, the unit test parameterizes all
// four truth-table combos, and a future implementor can't silently
// drift the predicate by editing the modal without touching the test.

export function effectiveReveal(
  sectionRevealAtAddTime: boolean,
  compositeRevealProjects: boolean,
): boolean {
  return compositeRevealProjects && sectionRevealAtAddTime;
}

export function bannerVisible(
  sectionReveals: boolean[],
  compositeRevealProjects: boolean,
): boolean {
  return sectionReveals.some((r) => effectiveReveal(r, compositeRevealProjects));
}
