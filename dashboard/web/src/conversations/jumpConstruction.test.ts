import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

// #463 S4 remediation round 4 — the structural guard behind "one builder
// constructs every `ConversationJump`".
//
// Round 3 extracted `buildConversationJump` and wrote that claim into both the
// interface comment and `docs/dashboard-gotchas.md`, but converted only the
// three sites the round happened to touch. Three more literal constructions
// stayed behind, so the guarantee the extraction exists to provide — a new
// field is threaded once — did not exist, and nothing said so. A comment cannot
// hold that rule up; the three-way equality test it replaced could not either,
// which is why the round that added `inner_anchor_key` reached two sites of
// three.
//
// This is a static source scan rather than a behavioral test because the rule
// is about how the payload is WRITTEN, not what it contains: a hand-written
// literal that happens to be correct today still breaks the guarantee.
//
// Round 5 states the scan's reach exactly, because round 4's comment claimed
// more than it had. WHAT IT CATCHES: a `jump:` property whose object literal
// opens anywhere after it, including on a later line; a local typed
// `: ConversationJump = {`; any `as` / `satisfies ConversationJump` assertion;
// and a local variable literally named `jump` assigned an object literal, which
// is the form the `{ jump }` shorthand is built from. Comments are stripped
// before the scan, so a doc comment that quotes `jump: {` is not an offender —
// round 4's scan would have failed on its own prose.
//
// WHAT IT CANNOT CATCH, and no line-based scan can: an object literal assigned
// to a variable under any OTHER name and then passed as `jump: thatName`, or a
// jump payload assembled field-by-field and spread in. Those need a real
// TypeScript AST walk. The rule is enforced by this scan plus review, not by
// this scan alone — do not restate it as total coverage.

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..');

// The builder itself lives beside the interface it builds, so it is the one
// file allowed to write the object literal.
const BUILDER_FILE = join(SRC, 'types', 'conversation.ts');

// Blank out comments while preserving every byte position, so a match index
// still resolves to the right source line. A naive `//`-to-end-of-line strip
// would corrupt string literals containing `//` (every URL in the tree), so
// this tracks quote and template state as it walks.
//
// Round 6 — the walker has a second failure direction, and which way it fails
// matters. The header above only discusses swallowing: a quote the walker
// wrongly opens on can carry it past comments it should have blanked. The
// opposite happens when a line's quotes pair ACROSS a comment opener, as in
// `const RE = /["']/; // note "x"` — the walker pairs the character-class quote
// with the quote inside the comment, skips over the `//`, and leaves that
// comment live in the stripped output. That produces a FALSE offender: the scan
// reports a line that is only prose. The scan therefore fails LOUD in this
// direction and quiet in the other, which is why the vacuity floors below exist
// for the swallowing direction specifically — a false offender gets read and
// corrected, a swallowed file reports nothing at all.
export function stripComments(text: string): string {
  const out = text.split('');
  let i = 0;
  const blank = (from: number, to: number) => {
    for (let k = from; k < to; k += 1) if (out[k] !== '\n') out[k] = ' ';
  };
  while (i < text.length) {
    const c = text[i];
    if (c === '\\') { i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') {
      // A ' or " string cannot span a line, so a quote with no partner before
      // the newline is a literal apostrophe — an English contraction inside a
      // regex or a comment ("doesn't"), not a string opener. Treating it as one
      // would swallow the rest of the file into string state and stop stripping
      // comments there. A backtick CAN span lines, so it always opens.
      const quote = c;
      const lineEnd = quote === '`' ? text.length : (text.indexOf('\n', i) === -1 ? text.length : text.indexOf('\n', i));
      let j = i + 1;
      let closed = false;
      while (j < lineEnd) {
        if (text[j] === '\\') { j += 2; continue; }
        if (text[j] === quote) { closed = true; j += 1; break; }
        j += 1;
      }
      if (!closed) { i += 1; continue; }
      i = j;
      continue;
    }
    if (c === '/' && text[i + 1] === '/') {
      const end = text.indexOf('\n', i);
      const stop = end === -1 ? text.length : end;
      blank(i, stop);
      i = stop;
      continue;
    }
    if (c === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2);
      const stop = end === -1 ? text.length : end + 2;
      blank(i, stop);
      i = stop;
      continue;
    }
    i += 1;
  }
  return out.join('');
}

// A dispatch-site literal: `jump: {`, with the brace on this line or a later
// one (`\s` spans newlines, and the scan runs over the whole file).
const JUMP_LITERAL = /\bjump\s*:\s*\{/g;
// A typed local: `const x: ConversationJump = {`.
const TYPED_LITERAL = /:\s*ConversationJump\s*=\s*\{/g;
// An assertion into the type, literal or not: `… as ConversationJump`,
// `… satisfies ConversationJump`. No production site needs either — the builder
// already returns the type — so any occurrence is a construction escaping it.
const ASSERTED = /\b(?:as|satisfies)\s+ConversationJump\b/g;
// A local named `jump` holding an object literal — the thing a `{ jump }`
// shorthand would carry.
const NAMED_LOCAL = /\b(?:const|let|var)\s+jump\s*(?::[^=;{]*)?=\s*\{/g;

const PATTERNS = { JUMP_LITERAL, TYPED_LITERAL, ASSERTED, NAMED_LOCAL };

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...sourceFiles(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry.name)) continue;
    // Tests build jump payloads as FIXTURES — that is the thing under test, not
    // a production construction, and forcing them through the builder would
    // make a test unable to assert on a payload the builder cannot produce.
    if (/\.test\.tsx?$/.test(entry.name)) continue;
    out.push(full);
  }
  return out;
}

const lineOf = (text: string, index: number) => text.slice(0, index).split('\n').length;

describe('#463 S4 — every ConversationJump comes from the one builder', () => {
  it('no production source file constructs a jump payload outside the builder', () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      if (file === BUILDER_FILE) continue;
      const text = stripComments(readFileSync(file, 'utf8'));
      for (const [name, pattern] of Object.entries(PATTERNS)) {
        pattern.lastIndex = 0;
        let match = pattern.exec(text);
        while (match != null) {
          offenders.push(`${relative(SRC, file)}:${lineOf(text, match.index)} (${name})`);
          match = pattern.exec(text);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('is non-vacuous: every pattern it scans for does match its form', () => {
    const hits = (pattern: RegExp, sample: string) => {
      pattern.lastIndex = 0;
      return pattern.test(sample);
    };
    expect(hits(JUMP_LITERAL, '        jump: { conversation_ref: r, session_id: r.key, uuid },')).toBe(true);
    // The multi-line form round 4's comment claimed and its line scan missed.
    expect(hits(JUMP_LITERAL, 'dispatch({\n  jump:\n    {\n      uuid,\n    },\n});')).toBe(true);
    expect(hits(TYPED_LITERAL, '  const j: ConversationJump = {')).toBe(true);
    expect(hits(ASSERTED, '  dispatch({ jump: payload as ConversationJump });')).toBe(true);
    expect(hits(ASSERTED, '  const j = { uuid } satisfies ConversationJump;')).toBe(true);
    expect(hits(NAMED_LOCAL, '  const jump = { session_id: id, uuid };')).toBe(true);
    expect(hits(NAMED_LOCAL, '  const jump: ConversationJump = { uuid };')).toBe(true);
    // And that it reads real files rather than an empty set.
    expect(sourceFiles(SRC).length).toBeGreaterThan(50);
  });

  // Round 6 — the two checks above prove the PATTERNS match their form and that
  // the file walk enumerates a real tree. Neither proves the text the patterns
  // are run against survived `stripComments`. If the walker over-blanked — a
  // `/*` it wrongly read as code with no `*/` after it blanks to end of file —
  // `offenders` would be `[]` and every assertion here would still pass. The two
  // floors below cover that direction, and they are disjoint: the first appends
  // a violation to every file and so reaches files holding no jump token at all,
  // while the second checks that tokens already present in named files survive.
  //
  // Round 7 — state the dependency between them rather than calling the first
  // one drift-proof on its own. It iterates `sourceFiles(SRC)`, so an empty
  // enumeration would make it pass over nothing, and what rules that out is the
  // `sourceFiles(SRC).length > 50` assertion in the non-vacuity block ABOVE.
  // These floors are therefore sound at SUITE level, not standalone: do not move
  // either one into a file that runs without the other.

  it('is non-vacuous per file: a violation appended to each real file is still caught', () => {
    // Appended at the END, because the swallowing failure runs to end of file:
    // if a file's tail is blanked, the injected construction is blanked with it
    // and the pattern stops matching. Strings are skipped rather than blanked,
    // so this floor is specific to over-blanking, which is the silent direction.
    const VIOLATION = '\nconst jump = { session_id: id, uuid };\n';
    const missed: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const stripped = stripComments(readFileSync(file, 'utf8') + VIOLATION);
      NAMED_LOCAL.lastIndex = 0;
      if (!NAMED_LOCAL.test(stripped)) missed.push(relative(SRC, file));
    }
    expect(missed).toEqual([]);
  });

  it('is non-vacuous per named file: each file\'s own jump tokens survive the strip', () => {
    // Round 7 — this was a TREE-WIDE floor of 6 against an actual 7 occurrences,
    // which meant a mid-file over-blank swallowing exactly one site still passed
    // in any file but `OutlinePanel.tsx` (the only one holding two). A floor is
    // the right shape — a new call site must not redden the test — but it has to
    // be applied PER FILE, because over-blanking runs to end of file and so
    // takes out one file's tail, not a share of the tree's total.
    //
    // `buildConversationJump(` is the control token for the six files that call
    // the builder by name; no production file mentions it inside a comment.
    // `ConversationReader.tsx` calls it through the `readerJump` alias, so its
    // control token is `readerJump(` — and it matters most there, because it is
    // both the largest file in the tree and the one holding nine of the fifteen
    // call sites.
    //
    // These are FLOORS. Adding a call site raises the real count and must not
    // fail; re-measure only when a site is legitimately removed.
    const FLOORS: [file: string, token: string, min: number][] = [
      ['types/conversation.ts', 'buildConversationJump(', 1],
      ['modals/CacheRebuildsSection.tsx', 'buildConversationJump(', 1],
      ['conversations/FindBar.tsx', 'buildConversationJump(', 1],
      ['conversations/OutlinePanel.tsx', 'buildConversationJump(', 2],
      ['conversations/ConversationRail.tsx', 'buildConversationJump(', 1],
      ['conversations/ComparisonView.tsx', 'buildConversationJump(', 1],
      ['conversations/ConversationReader.tsx', 'readerJump(', 9],
    ];
    const byPath = new Map(sourceFiles(SRC).map((f) => [relative(SRC, f), f]));
    const shortfalls: string[] = [];
    for (const [rel, token, min] of FLOORS) {
      const full = byPath.get(rel);
      // A named file that the walk did not enumerate is itself a failure: the
      // floor would otherwise be checked against nothing.
      if (full === undefined) { shortfalls.push(`${rel}: not enumerated`); continue; }
      const surviving = stripComments(readFileSync(full, 'utf8')).split(token).length - 1;
      if (surviving < min) shortfalls.push(`${rel}: ${surviving} × ${token} (floor ${min})`);
    }
    expect(shortfalls).toEqual([]);
  });

  it('strips comments, so prose quoting a jump literal is not an offender', () => {
    const stripped = stripComments([
      "// A doc comment explaining that a site must not write jump: { … } itself.",
      '/* Block form: jump: { uuid } is what the builder replaces. */',
      "const url = 'https://example.test/a//b';",
      // An unpaired apostrophe inside a regex must not open a string that runs
      // to the end of the file and stops every later comment being stripped.
      "const RE = /doesn't want to proceed/i;",
      '// Trailing prose: jump: { uuid } again.',
      'dispatch({ jump: buildConversationJump(ref, uuid, true) });',
    ].join('\n'));
    JUMP_LITERAL.lastIndex = 0;
    expect(JUMP_LITERAL.test(stripped)).toBe(false);
    // Byte positions and line count survive the blanking, and a `//` inside a
    // string literal is NOT treated as a comment opener.
    expect(stripped.split('\n')).toHaveLength(6);
    expect(stripped).toContain("'https://example.test/a//b'");
    expect(stripped).toContain("/doesn't want to proceed/i");
    expect(stripped).toContain('buildConversationJump');
  });
});
