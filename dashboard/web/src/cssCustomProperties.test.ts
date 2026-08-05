import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const cssPath = join(process.cwd(), 'src', 'index.css');

describe('dashboard CSS custom properties (#481)', () => {
  it('gives every custom-property reference a definition or fallback', () => {
    const css = readFileSync(cssPath, 'utf8');
    const source = css.replace(/\/\*[\s\S]*?\*\//g, '');
    const definitions = new Set(
      Array.from(source.matchAll(/(--[A-Za-z0-9_-]+)\s*:/g), (match) => match[1]),
    );
    const unresolved: string[] = [];

    for (const match of source.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)\s*(,|\))/g)) {
      const [, property, terminator] = match;
      if (terminator === ',' || definitions.has(property)) continue;

      const line = source.slice(0, match.index).split('\n').length;
      unresolved.push(`${property} at index.css:${line}`);
    }

    expect(unresolved).toEqual([]);
  });
});
