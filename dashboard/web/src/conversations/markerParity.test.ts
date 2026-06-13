/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { extractCommandInvocation, MARKER_TAGS } from './systemMarkers';

// #186 — cross-language marker-tag parity (spec §5, Codex P1d). The slash-command
// marker tuple lives in TWO places: the parser kernel
// (bin/_lib_conversation.py::_MARKER_TAGS) drives ingest classification + the
// title-skip predicate; the client (systemMarkers.ts::MARKER_TAGS) drives the
// reader's fold-to-pill heuristic. They MUST agree element-for-element, same
// order — a divergence means a tag is recognized on one side but not the other
// (the exact bug class #186 fixed: local-command-stdout was in neither list).
// This reads the Python source at test time and regex-extracts the literal
// tuple, so adding/removing/reordering a tag on either side fails here.
describe('marker-tag cross-language parity (#186)', () => {
  it('client MARKER_TAGS matches the server _MARKER_TAGS element-for-element', () => {
    // vitest runs with cwd at dashboard/web; the parser kernel lives two levels
    // up under bin/. Resolve from cwd (a real fs path, unlike import.meta.url
    // which carries a non-file scheme under vitest's transform) and assert it
    // exists so a moved file fails loudly instead of as an empty-tuple match.
    const pyPath = resolve(process.cwd(), '../../bin/_lib_conversation.py');
    expect(existsSync(pyPath), `expected parser kernel at ${pyPath}`).toBe(true);
    const src = readFileSync(pyPath, 'utf8');

    // Capture the `_MARKER_TAGS = ( … )` tuple body (may span multiple lines).
    const m = src.match(/_MARKER_TAGS\s*=\s*\(([\s\S]*?)\)/);
    expect(m, 'could not locate _MARKER_TAGS tuple in _lib_conversation.py').not.toBeNull();

    // Pull every single- or double-quoted string literal out of the tuple body.
    const serverTags = Array.from(m![1].matchAll(/["']([^"']+)["']/g)).map(
      (g: RegExpMatchArray) => g[1],
    );

    expect(serverTags.length).toBeGreaterThan(0);
    // Element-for-element, same order. (Array equality is order-sensitive.)
    expect([...MARKER_TAGS]).toEqual(serverTags);
  });
});

// #188 — extractCommandInvocation must exist on both sides and agree on the
// promote/skip decision. The Python kernel _extract_command_invocation is the
// authority; the TS twin must return the SAME {name,args}|null verdict over a
// shared marker corpus (the cross-language values that gate whether a
// slash-command prompt shows up as a "You" turn). Pure name/args extraction is
// regex-symmetric across the two engines, so a shared-corpus check is sufficient
// (the heavy block-aware guard is the caller's, mirrored on both sides).
//
// CORPUS is shared between two assertions: (1) a hand-maintained expected-value
// table (pins what each case SHOULD resolve to), and (2) a LIVE cross-check that
// pipes the same corpus through Python actually invoking _extract_command_invocation
// (subprocess, mirroring the source-read MARKER_TAGS test above) and diffs the
// Python verdict against the TS one case-for-case. (1) catches a shared-bug where
// both sides are wrong the same way; (2) catches silent drift where one engine's
// regex changes and the other doesn't. Neither alone is enough.
type Verdict = { name: string; args: string } | null;
const CMD_PARITY_CORPUS: { text: string; expect: Verdict }[] = [
  {
    text:
      '<command-message>frontend-design:frontend-design</command-message>' +
      '<command-name>/frontend-design</command-name>' +
      '<command-args>Audit the reader UI.</command-args>',
    expect: { name: '/frontend-design', args: 'Audit the reader UI.' },
  },
  {
    text: '<command-name>/effort</command-name><command-args>max</command-args>',
    expect: { name: '/effort', args: 'max' },
  },
  // empty-args control command → null (stays hidden)
  {
    text: '<command-name>/clear</command-name><command-args></command-args>',
    expect: null,
  },
  // whitespace-only args → null (stripped to empty)
  {
    text: '<command-name>/compact</command-name><command-args>   \n  </command-args>',
    expect: null,
  },
  // no <command-args> tag at all (/exit, /model) → null
  {
    text: '<command-name>/exit</command-name><command-message>exit</command-message>',
    expect: null,
  },
  // args present, name omitted → { name: '', args }
  {
    text: '<command-args>just the args</command-args>',
    expect: { name: '', args: 'just the args' },
  },
  // stdout-only marker → null
  {
    text: '<local-command-stdout>Set model to Fable 5</local-command-stdout>',
    expect: null,
  },
  // prose quoting a tag → null
  { text: 'see <command-args>x</command-args> here', expect: null },
];

describe('command-invocation cross-language parity (#188)', () => {
  it('extractCommandInvocation matches the pinned expected verdict', () => {
    for (const c of CMD_PARITY_CORPUS) {
      expect(extractCommandInvocation(c.text), `verdict for: ${c.text}`).toEqual(c.expect);
    }
  });

  it('extractCommandInvocation mirrors the LIVE Python _extract_command_invocation verdict', () => {
    // vitest runs with cwd at dashboard/web; the parser kernel lives two levels
    // up under bin/ (same resolution as the MARKER_TAGS source-read above).
    const binDir = resolve(process.cwd(), '../../bin');
    expect(existsSync(resolve(binDir, '_lib_conversation.py'))).toBe(true);

    // Pipe the shared corpus through Python actually CALLING the kernel helper.
    // Each text is wrapped as a single text block so the Python all-text guard
    // (which the text-only TS twin lacks) is satisfied — the two engines then
    // compare on the same pure-marker text. The script reads the corpus from
    // stdin (no shell-quoting hazards) and prints one JSON verdict per case.
    const PY = [
      'import sys, json, pathlib',
      'sys.path.insert(0, sys.argv[1])',
      'import _lib_conversation as lc',
      'texts = json.load(sys.stdin)',
      'out = []',
      'for t in texts:',
      '    blocks = [{"kind": "text", "text": t}]',
      '    out.append(lc._extract_command_invocation(blocks, lc._join_text_blocks(blocks)))',
      'print(json.dumps(out))',
    ].join('\n');

    const texts = CMD_PARITY_CORPUS.map((c) => c.text);
    const proc = spawnSync('python3', ['-c', PY, binDir], {
      input: JSON.stringify(texts),
      encoding: 'utf8',
    });
    expect(proc.error, `python3 spawn failed: ${proc.error?.message}`).toBeUndefined();
    expect(proc.status, `python3 exited ${proc.status}; stderr:\n${proc.stderr}`).toBe(0);

    const pythonVerdicts = JSON.parse(proc.stdout) as Verdict[];
    expect(pythonVerdicts.length).toBe(CMD_PARITY_CORPUS.length);

    // Case-for-case: the TS twin must agree with the live Python verdict, and
    // both must match the pinned expectation (so a drift on EITHER side fails).
    CMD_PARITY_CORPUS.forEach((c, i) => {
      expect(extractCommandInvocation(c.text), `TS verdict for: ${c.text}`).toEqual(
        pythonVerdicts[i],
      );
      expect(pythonVerdicts[i], `Python verdict for: ${c.text}`).toEqual(c.expect);
    });
  });

  it('extractCommandInvocation is exported (TS twin exists for the Python helper)', () => {
    expect(typeof extractCommandInvocation).toBe('function');
  });
});
