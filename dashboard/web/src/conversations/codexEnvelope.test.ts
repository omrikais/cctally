import { describe, it, expect } from 'vitest';
import { parseCodexEnvelope, codexMeta, responseIsLong } from './codexEnvelope';

describe('parseCodexEnvelope', () => {
  it('parses a success envelope to ok with content + threadId', () => {
    const r = parseCodexEnvelope(JSON.stringify({ threadId: 'abc-1234', content: '**Findings**' }));
    expect(r).toEqual({ kind: 'ok', content: '**Findings**', threadId: 'abc-1234' });
  });

  it('parses an error envelope to error with status + type + message', () => {
    const r = parseCodexEnvelope(
      JSON.stringify({ type: 'error', status: 400, error: { type: 'invalid_request_error', message: 'nope' } }),
    );
    expect(r).toEqual({ kind: 'error', status: 400, errorType: 'invalid_request_error', message: 'nope' });
  });

  it('returns raw on malformed/truncated JSON', () => {
    const cut = '{"threadId":"x","content":"trunc';
    expect(parseCodexEnvelope(cut)).toEqual({ kind: 'raw', text: cut });
  });

  it('returns raw on empty input', () => {
    expect(parseCodexEnvelope('')).toEqual({ kind: 'raw', text: '' });
  });

  it('returns raw on non-object JSON', () => {
    expect(parseCodexEnvelope('"just a string"')).toEqual({ kind: 'raw', text: '"just a string"' });
  });

  it('does NOT recurse into a content that is itself JSON', () => {
    const inner = JSON.stringify({ type: 'error', status: 500, error: { message: 'inner' } });
    const r = parseCodexEnvelope(JSON.stringify({ threadId: 't', content: inner }));
    expect(r.kind).toBe('ok');
    if (r.kind === 'ok') expect(r.content).toBe(inner);
  });

  it('falls back to a generic message when an error envelope omits message', () => {
    const r = parseCodexEnvelope(JSON.stringify({ type: 'error', status: 500 }));
    expect(r.kind).toBe('error');
    if (r.kind === 'error') {
      expect(r.status).toBe(500);
      expect(r.message.length).toBeGreaterThan(0);
    }
  });
});

describe('codexMeta', () => {
  it('extracts model/effort/sandbox/approval/cwd basename', () => {
    expect(
      codexMeta({
        model: 'gpt-5.2-codex',
        config: { model_reasoning_effort: 'high' },
        sandbox: 'read-only',
        'approval-policy': 'never',
        cwd: '/a/b/fix-239',
      }),
    ).toEqual({
      model: 'gpt-5.2-codex',
      effort: 'high',
      sandbox: 'read-only',
      approval: 'never',
      cwdBase: 'fix-239',
      threadId: undefined,
    });
  });

  it('extracts threadId for codex-reply', () => {
    expect(codexMeta({ threadId: 'abc', prompt: 'x' }).threadId).toBe('abc');
  });

  it('tolerates null/empty input', () => {
    expect(codexMeta(null)).toEqual({
      model: undefined, effort: undefined, sandbox: undefined,
      approval: undefined, cwdBase: undefined, threadId: undefined,
    });
  });
});

describe('responseIsLong', () => {
  it('true past 24 lines', () => { expect(responseIsLong('x\n'.repeat(25))).toBe(true); });
  it('true past 1400 chars', () => { expect(responseIsLong('a'.repeat(1401))).toBe(true); });
  it('false when short', () => { expect(responseIsLong('hello')).toBe(false); });
});
