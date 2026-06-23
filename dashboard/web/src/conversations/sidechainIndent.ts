// #228 S2 (A2) — the depth→indent-class mapping for a subagent card.
// VISUAL hierarchy keys on `depth`, NOT on the legacy `nested` boolean: a
// depth-0 agent (whether spawned from a main turn OR an orphan) stays on the
// main spine with the magenta dot and NO indent; only true agent-in-agent
// nesting (depth >= 1) indents. The indent MAGNITUDE is a single CSS rule that
// reads an inline `--sc-depth` custom property (so arbitrary depth works) — this
// helper only decides whether the `--nested` marker class is present.
export function sidechainIndentClass(depth: number): string {
  return depth >= 1 ? 'conv-sidechain--nested' : '';
}
