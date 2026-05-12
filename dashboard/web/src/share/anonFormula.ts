// Real-name banner formula — spec §8.5 / §10.4.
//
// At compose time, the server re-renders every section from recipe with
// the composer's composite `reveal_projects` value — per-section
// add-time `reveal_projects` is unconditionally ignored (the explicit
// `composite_opts` override at bin/cctally:33563-33567). So the banner
// fires whenever the composite would reveal real names AND there is at
// least one section in the basket:
//
//   banner_visible = composite_reveal_projects && section_count > 0
//
// Codex review on PR #35 flagged that the prior AND-with-add-time
// formula (§10.5) was silent when a section captured anonymously was
// nonetheless revealed by the composite override — i.e. the banner
// hid a real-name export. Aligning with §8.5 (server behavior) closes
// that gap; the §10.5 wording in the design doc was amended in the
// same PR.

export function bannerVisible(
  sectionCount: number,
  compositeRevealProjects: boolean,
): boolean {
  return compositeRevealProjects && sectionCount > 0;
}
