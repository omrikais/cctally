/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { MARKER_TAGS } from './systemMarkers';

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
