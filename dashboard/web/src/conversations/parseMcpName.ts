// #177 S4: parse an MCP tool name (mcp__<server>__<action>) into its parts.
// Returns null for non-MCP names so callers fall through to existing
// rendering. Server names contain SINGLE underscores
// (plugin_playwright_playwright) — the split is segment-wise on '__', never
// first-underscore. The real malformed shape `mcp__claude-in-chrome` (no
// action segment) degrades to server-only with the raw name as the action.

export type McpServerKind = 'playwright' | 'chrome' | 'computer' | 'codex' | 'generic';

const SERVER_LABEL: Record<string, string> = {
  plugin_playwright_playwright: 'playwright',
  playwright: 'playwright',
  'claude-in-chrome': 'chrome',
  'computer-use': 'computer',
  codex: 'codex',
};
const LABEL_KIND: Record<string, McpServerKind> = {
  playwright: 'playwright', chrome: 'chrome', computer: 'computer', codex: 'codex',
};

export interface McpName {
  server: string;       // raw server segment
  serverLabel: string;  // friendly display name (raw when unknown)
  action: string;       // action segment(s), '__'-joined; raw name when missing
  kind: McpServerKind;  // per-server icon family
}

export function parseMcpName(name: string | null | undefined): McpName | null {
  if (!name || !name.startsWith('mcp__')) return null;
  const segs = name.split('__'); // ['mcp', server, ...action]
  const server = segs[1] ?? '';
  if (!server) return null;
  const action = segs.length > 2 ? segs.slice(2).join('__') : name;
  const serverLabel = SERVER_LABEL[server] ?? server;
  return { server, serverLabel, action, kind: LABEL_KIND[serverLabel] ?? 'generic' };
}
