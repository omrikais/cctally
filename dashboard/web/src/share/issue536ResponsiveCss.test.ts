import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(join(process.cwd(), 'src', 'index.css'), 'utf8');

function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 'g').exec(css);
  if (!match) throw new Error(`missing CSS rule for ${selector}`);
  return match[1];
}

describe('#536 responsive share follow-ups', () => {
  it('gives the mobile Markdown preview enough room to reach report substance', () => {
    expect(css).toContain('max-height: min(50dvh, 380px)');
  });

  it('sizes both chip hit bands from the 44px token instead of border arithmetic', () => {
    for (const selector of ['.doctor-chip::before', '.basket-chip::before']) {
      expect(rule(selector)).toContain('block-size: var(--tap-target)');
      expect(rule(selector)).toContain('inset-block-start: 50%');
      expect(rule(selector)).toContain('translateY(-50%)');
    }
    expect(css).not.toContain('inset: -10px 0');
    expect(css).not.toContain('inset: -12px 0');
  });

  it('reserves one unwrapped action column and styles composer confirmations', () => {
    expect(rule('.share-manage-actions')).toContain('white-space: nowrap');
    expect(rule('.share-manage-actions')).toContain('width: 1%');
    expect(css).toContain('width: min(110px, 28vw)');
    expect(css).toContain('.composer-clear-all,\n.composer-clear-all + .share-confirm button');
  });
});
