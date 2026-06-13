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
// MUST stay element-for-element identical (same order) to the server
// _MARKER_TAGS in bin/_lib_conversation.py — enforced by markerParity.test.ts
// (#186). local-command-stdout / -stderr were added in #186 so a slash-command
// stdout echo is recognized as plumbing, not a "You" prompt.
export const MARKER_TAGS = [
  'command-name',
  'command-message',
  'command-args',
  'local-command-caveat',
  'local-command-stdout',
  'local-command-stderr',
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

// #188 — a slash-command invocation carries the user's real prompt in
// <command-args>; the <command-name>/<command-message> wrappers are plumbing.
// Mirrors the Python kernel _extract_command_invocation (bin/_lib_conversation.py):
// a pure command marker (isSystemMarker) whose <command-args> is non-empty after
// strip ⇒ { name, args } (name from <command-name>, '' when omitted); else null.
// Empty-args control commands (/clear, /exit, /compact, /model) and stdout-only
// markers return null and stay hidden as system markers.
//
// This twin has NO production caller — the reader promotes slash-command turns
// from the server-supplied command_name/text, computed once at ingest by the
// Python kernel; the client never re-derives the promote decision. It exists for
// (a) cross-language parity testing against the Python helper (markerParity.test.ts),
// so the two regex extractors can't silently drift, and (b) future client-side use.
// Unlike the Python kernel it takes only `text` (no block-aware all-text guard):
// a future caller wanting Python-faithful behavior must apply the all-text guard
// itself (blocks.every(text)), same posture as isSystemMarker. Anchored mid-string
// is fine because isSystemMarker already proved the whole text is ONLY markers.
const CMD_NAME_RE = /<command-name>([\s\S]*?)<\/command-name>/;
const CMD_ARGS_RE = /<command-args>([\s\S]*?)<\/command-args>/;

export function extractCommandInvocation(
  text: string,
): { name: string; args: string } | null {
  if (!isSystemMarker(text)) return null;
  const am = CMD_ARGS_RE.exec(text);
  const args = am ? am[1].trim() : '';
  if (!args) return null;
  const nm = CMD_NAME_RE.exec(text);
  return { name: nm ? nm[1].trim() : '', args };
}
