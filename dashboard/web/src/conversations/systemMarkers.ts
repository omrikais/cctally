// Detects a conversation item whose text is ONLY generated command-marker
// plumbing (the wrappers Claude Code injects for slash commands like /clear,
// /compact) so the reader can fold it into an expandable pill instead of a
// full prose turn (spec §4.4 / #161 finding F-series).
//
// Anchored + exact: the WHOLE text must consist of one or more concatenated
// command-marker wrappers (optionally whitespace-separated), with no
// surrounding prose. This deliberately FAILS TOWARD SHOWING content — a
// sentence that merely quotes <command-name>, or a marker inside a fenced
// code block, must NOT match (the fold is heuristic; the API exposes no
// system-marker metadata, only kind/text/blocks). The pill is always
// expandable, so a false negative just shows the raw turn (safe); a false
// positive could hide real user text (unsafe) — hence the strict anchoring.
const MARKER_TAGS = [
  'command-name',
  'command-message',
  'command-args',
  'local-command-caveat',
] as const;

// ^\s* ... \s*$  -> anchored to the whole string (no leading/trailing prose).
// (?:<(tag)>(?:(?!</\1>)[\s\S])*</\1>\s*)+  -> one or more wrappers; the body
// is the unrolled-lazy form (a greedy run of chars that are not the wrapper's
// own close tag), which matches the same text as a lazy [\s\S]*? but in LINEAR
// time — the prior lazy quantifier under the outer + backtracked
// catastrophically (ReDoS) on a valid-prefix-then-trailing-prose input. The \1
// backreference still forces each close tag to match its own open tag.
const MARKER_RE = new RegExp(
  `^\\s*(?:<(${MARKER_TAGS.join('|')})>(?:(?!</\\1>)[\\s\\S])*</\\1>\\s*)+$`,
);

export function isSystemMarker(text: string): boolean {
  if (!text) return false;
  return MARKER_RE.test(text);
}
