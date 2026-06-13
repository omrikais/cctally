/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
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
// authority; this asserts the TS twin returns the SAME {name,args}|null verdict
// over a shared marker corpus (the cross-language values that gate whether a
// slash-command prompt shows up as a "You" turn). Pure name/args extraction is
// regex-symmetric across the two engines, so a literal-shared-corpus check is
// sufficient (the heavy block-aware guard is the caller's, mirrored on both
// sides).
describe('command-invocation cross-language parity (#188)', () => {
  it('extractCommandInvocation mirrors the Python args/name verdict', () => {
    const corpus: { text: string; expect: { name: string; args: string } | null }[] = [
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
      // stdout-only marker → null
      {
        text: '<local-command-stdout>Set model to Fable 5</local-command-stdout>',
        expect: null,
      },
      // prose quoting a tag → null
      { text: 'see <command-args>x</command-args> here', expect: null },
    ];
    for (const c of corpus) {
      expect(extractCommandInvocation(c.text), `verdict for: ${c.text}`).toEqual(c.expect);
    }
  });

  it('extractCommandInvocation is exported (TS twin exists for the Python helper)', () => {
    expect(typeof extractCommandInvocation).toBe('function');
  });
});
