// The inventory tool's own guard (#513 S2, Task 1).
//
// The tool exists to make silent coverage loss impossible during the six-file
// consolidation, so a bug that makes IT lose a file silently is the one bug it
// cannot have. Its stdout mode did exactly that: with no `--out`, the positional
// filter compared against `outIndex + 1`, which is `0` when `--out` is absent,
// so `argv[0]` — the first file named on the command line — was dropped. What
// the operator saw was 83 cases across two files where three files hold 165.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { parseInventoryArgs } from './settings-test-inventory.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = resolve(HERE, 'settings-test-inventory.mjs');

/** A suite file holding exactly `count` cases, so the totals are arithmetic. */
function suiteWith(count) {
  const cases = Array.from(
    { length: count },
    (_, index) => `  it('case ${index}', () => { expect(${index}).toBe(${index}); });`,
  ).join('\n');
  return `describe('suite', () => {\n${cases}\n});\n`;
}

function fixtureFiles() {
  const dir = mkdtempSync(join(tmpdir(), 'settings-inventory-'));
  const paths = [1, 2, 3].map((count, index) => {
    const path = join(dir, `suite-${index}.test.tsx`);
    writeFileSync(path, suiteWith(count));
    return path;
  });
  return paths;
}

describe('the inventory tool never loses a file it was told to read', () => {
  it('reads every positional file when no --out is given', () => {
    const paths = fixtureFiles();
    const stdout = execFileSync('node', [SCRIPT, ...paths], { encoding: 'utf8' });
    const payload = JSON.parse(stdout);
    // Three files holding 1 + 2 + 3 cases. Before the fix the first file was
    // dropped, so this read 2 files and 5 cases.
    expect(Object.keys(payload.perFile)).toHaveLength(3);
    expect(payload.total).toBe(6);
    expect(Object.values(payload.perFile)).toEqual([1, 2, 3]);
  });

  it('still consumes the --out value rather than treating it as a file', () => {
    const paths = fixtureFiles();
    const out = join(dirname(paths[0]), 'inventory.json');
    execFileSync('node', [SCRIPT, '--out', out, ...paths], { encoding: 'utf8' });
    const payload = JSON.parse(execFileSync('cat', [out], { encoding: 'utf8' }));
    expect(Object.keys(payload.perFile)).toHaveLength(3);
    expect(payload.total).toBe(6);
  });
});


describe('parseInventoryArgs', () => {
  it('keeps every positional when --out is absent', () => {
    expect(parseInventoryArgs(['a.tsx', 'b.tsx', 'c.tsx'])).toEqual({
      outPath: null,
      explicit: ['a.tsx', 'b.tsx', 'c.tsx'],
    });
  });

  it('drops only the value that belongs to --out', () => {
    expect(parseInventoryArgs(['--out', 'x.json', 'a.tsx'])).toEqual({
      outPath: 'x.json',
      explicit: ['a.tsx'],
    });
    expect(parseInventoryArgs(['a.tsx', '--out', 'x.json', 'b.tsx'])).toEqual({
      outPath: 'x.json',
      explicit: ['a.tsx', 'b.tsx'],
    });
  });

  it('reports no explicit targets when only flags are given', () => {
    expect(parseInventoryArgs(['--out', 'x.json'])).toEqual({
      outPath: 'x.json',
      explicit: [],
    });
    expect(parseInventoryArgs([])).toEqual({ outPath: null, explicit: [] });
  });
});
