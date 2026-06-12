import { describe, expect, it } from 'vitest';
import { parseMcpName } from './parseMcpName';

describe('parseMcpName', () => {
  it('splits multi-underscore servers segment-wise', () => {
    const m = parseMcpName('mcp__plugin_playwright_playwright__browser_take_screenshot');
    expect(m).toEqual({
      server: 'plugin_playwright_playwright', serverLabel: 'playwright',
      action: 'browser_take_screenshot', kind: 'playwright',
    });
  });
  it('maps the known servers to friendly labels + kinds', () => {
    expect(parseMcpName('mcp__claude-in-chrome__computer')).toMatchObject({ serverLabel: 'chrome', kind: 'chrome' });
    expect(parseMcpName('mcp__computer-use__screenshot')).toMatchObject({ serverLabel: 'computer', kind: 'computer' });
    expect(parseMcpName('mcp__codex__codex-reply')).toMatchObject({ serverLabel: 'codex', kind: 'codex' });
    expect(parseMcpName('mcp__sm-skills__search_skills')).toMatchObject({ serverLabel: 'sm-skills', kind: 'generic' });
  });
  it('degrades the real no-action shape to server-only', () => {
    expect(parseMcpName('mcp__claude-in-chrome')).toMatchObject({
      serverLabel: 'chrome', action: 'mcp__claude-in-chrome',
    });
  });
  it('returns null for non-MCP names and the bare prefix', () => {
    expect(parseMcpName('Bash')).toBeNull();
    expect(parseMcpName(null)).toBeNull();
    expect(parseMcpName('mcp__')).toBeNull();
  });
  it('keeps double-underscore actions whole', () => {
    expect(parseMcpName('mcp__srv__a__b')).toMatchObject({ server: 'srv', action: 'a__b' });
  });
});
