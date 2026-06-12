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

// Language for a tool call inferred from its STRUCTURED `input.file_path` (#177
// S3). More robust than parsing the `preview` string. Used by the DiffCard hunks
// and the edit-family result sub-panel (spec §4.3); '' when there's no usable
// file path. This is the path that broadens highlighting from Read to the edit
// family — `resultLang` deliberately stays Read-scoped (no generic-path change).
export function fileLangForCall(call: { input?: Record<string, unknown> | null }): string {
  const fp = (call.input as { file_path?: unknown } | null | undefined)?.file_path;
  return typeof fp === 'string' ? langFromExtension(fp) : '';
}
