import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const css = readFileSync(join(process.cwd(), 'src', 'index.css'), 'utf8');

describe('Cache Report responsive CSS contract (#452)', () => {
  it('keeps the desktop table labeled through 719px', () => {
    const deadBand = css.match(/@media \(max-width:\s*719px\)\s*\{([\s\S]*?)\n\}/)?.[1] ?? '';
    expect(deadBand).not.toMatch(/\.ch-table thead\s*\{\s*display:\s*none/);
  });

  it('caps the mini net bars in the design-reviewed 48–64px band', () => {
    const rule = css.match(/\.cr-netbars-mini-wrap\s*\{([\s\S]*?)\}/)?.[1] ?? '';
    expect(rule).toMatch(/height:\s*(?:4[8-9]|5\d|6[0-4])px/);
    expect(rule).not.toMatch(/flex:\s*1\s+1\s+auto/);
  });

  it('protects the modal title/subtitle through the card-constrained band', () => {
    const compact = css.match(/@media \(max-width:\s*783px\)\s*\{([\s\S]*?)\n\}\n\n@media \(max-width:\s*640px\)/)?.[1] ?? '';
    expect(compact).toMatch(/\.crm-subtitle\s*\{/);
    expect(compact).toMatch(/white-space:\s*nowrap/);
    expect(compact).toMatch(/modal-header:has\(\.crm-subtitle\) h2/);
  });
});
