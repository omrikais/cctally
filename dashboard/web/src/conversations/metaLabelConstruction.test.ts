/// <reference types="node" />
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #463 S5 Task 1 — the meta label and the meta sections value are presentation
// decisions made ONCE, in MetaLabel.tsx. Any other production file that writes
// the class literal has bypassed that decision, which is how #335's fix ended up
// scoped to one meta kind while three others shipped unprotected.
const SRC = resolve(process.cwd(), 'src');
const OWNER = join(SRC, 'conversations', 'MetaLabel.tsx');

function productionFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) { productionFiles(path, out); continue; }
    if (!/\.tsx?$/.test(name)) continue;
    if (/\.test\.tsx?$/.test(name)) continue;
    if (name === 'test-utils' || path.includes(`${'/'}test-utils${'/'}`)) continue;
    out.push(path);
  }
  return out;
}

describe('#463 S5 — meta label construction has one owner', () => {
  const files = productionFiles(SRC);

  it('scans a non-trivial number of production files', () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it('the owner really does construct both class literals', () => {
    const owner = readFileSync(OWNER, 'utf8');
    expect(owner).toContain('conv-meta-label');
    expect(owner).toContain('conv-meta-name');
  });

  for (const cls of ['conv-meta-label', 'conv-meta-name']) {
    it(`no production file outside MetaLabel.tsx writes ${cls}`, () => {
      const offenders = files
        .filter((f) => f !== OWNER)
        .filter((f) => readFileSync(f, 'utf8').includes(cls));
      expect(offenders, `construct ${cls} outside MetaLabel.tsx`).toEqual([]);
    });
  }
});
