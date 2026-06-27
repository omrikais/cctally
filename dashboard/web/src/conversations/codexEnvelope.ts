// Pure (no I/O, no React) decoder for the Codex MCP result envelope and a
// metadata extractor for its input. The Codex result is ALWAYS a JSON envelope:
//   success: { threadId, content }   — content is Markdown
//   error:   { type: 'error', status, error: { type, message } }
// Today the viewer dumps that raw envelope as monospace text; CodexCard renders
// the decoded form. See the design spec dated 2026-06-27.

export type CodexEnvelope =
  | { kind: 'ok'; content: string; threadId?: string }
  | { kind: 'error'; status?: number; errorType?: string; message: string }
  | { kind: 'raw'; text: string };

// Parse ONLY the top-level envelope. Never recurse into `content` even if it is
// itself valid JSON. Any malformed/truncated/unexpected text → { kind: 'raw' }.
export function parseCodexEnvelope(text: string): CodexEnvelope {
  if (!text.trim()) return { kind: 'raw', text };
  let obj: unknown;
  try {
    obj = JSON.parse(text);
  } catch {
    return { kind: 'raw', text };
  }
  if (obj === null || typeof obj !== 'object') return { kind: 'raw', text };
  const o = obj as Record<string, unknown>;

  // Error envelope: { type: 'error', status, error: { type, message } }. An
  // explicit type:'error' always wins; an `error` object alone counts only when
  // there's no `content` string (so a success envelope that happens to carry an
  // `error`-shaped key still decodes as ok).
  if (o.type === 'error' || (typeof o.content !== 'string' && o.error && typeof o.error === 'object')) {
    const err = (o.error && typeof o.error === 'object') ? (o.error as Record<string, unknown>) : {};
    const message =
      typeof err.message === 'string' ? err.message
      : typeof o.message === 'string' ? o.message
      : 'Codex returned an error.';
    const errorType =
      typeof err.type === 'string' ? err.type
      : (typeof o.type === 'string' && o.type !== 'error') ? o.type
      : undefined;
    return {
      kind: 'error',
      status: typeof o.status === 'number' ? o.status : undefined,
      errorType,
      message,
    };
  }

  // Success envelope: { threadId, content }.
  if (typeof o.content === 'string') {
    return {
      kind: 'ok',
      content: o.content,
      threadId: typeof o.threadId === 'string' ? o.threadId : undefined,
    };
  }

  return { kind: 'raw', text };
}

export interface CodexMeta {
  model?: string;
  effort?: string;
  sandbox?: string;
  approval?: string;
  cwdBase?: string;
  threadId?: string;
}

// Defensive extraction from the bounded structured input dict. effort lives
// under the nested `config.model_reasoning_effort`; cwd is reduced to its
// basename; threadId is present only on codex-reply.
export function codexMeta(input: Record<string, unknown> | null | undefined): CodexMeta {
  const i = input ?? {};
  const str = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined);
  const config = (i.config && typeof i.config === 'object') ? (i.config as Record<string, unknown>) : {};
  const cwd = str(i.cwd);
  return {
    model: str(i.model),
    effort: str(config.model_reasoning_effort),
    sandbox: str(i.sandbox),
    approval: str(i['approval-policy']),
    cwdBase: cwd ? cwd.split('/').filter(Boolean).pop() : undefined,
    threadId: str(i.threadId),
  };
}

// Same content-length proxy as WebFetchCard's resultIsLong (#177 S4).
export function responseIsLong(text: string): boolean {
  return text.split('\n').length > 24 || text.length > 1400;
}
