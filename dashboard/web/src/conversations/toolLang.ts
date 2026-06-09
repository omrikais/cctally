// Tool-semantics language inference for the conversation reader's tool I/O
// panels. Pure: returns a canonical refractor language name (matching the
// CodeBlock registered set) or '' to degrade to plain. Never imports refractor.

const EXT_LANG: Record<string, string> = {
  py: 'python',
  ts: 'typescript', tsx: 'tsx',
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx',
  json: 'json',
  sh: 'bash', bash: 'bash', zsh: 'bash',
  css: 'css',
  md: 'markdown', markdown: 'markdown',
  yaml: 'yaml', yml: 'yaml',
  diff: 'diff', patch: 'diff',
};

// Map a file path's extension to a registered language, else '' (degrade).
// `dot <= 0` covers no-extension AND dotfiles (`.zshrc` → dot index 0 → '').
export function langFromExtension(path: string): string {
  const base = path.split('/').pop() ?? path;
  const dot = base.lastIndexOf('.');
  if (dot <= 0) return '';
  return EXT_LANG[base.slice(dot + 1).toLowerCase()] ?? '';
}

// The RESULT-side language for a tool. Read → its file's language, derived from
// the block's `preview` (the full, untruncated file_path; see spec §3 / Codex
// finding 1). Every other tool → '' (Bash stdout, Grep/Glob lists, etc. are not
// a single language).
export function resultLang(toolName: string | null, filePath: string): string {
  if (toolName === 'Read') return langFromExtension(filePath);
  return '';
}
