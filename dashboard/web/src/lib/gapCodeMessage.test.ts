// #834 S2 — every gap code the server can publish has human-readable copy.
//
// `no_retained_transcript` shipped on the server in this session's Tranche A
// and reached the client through `gapCodeMessage`'s documented fallback, so
// the modal rendered the literal string
// `no_retained_transcript (no description in this build)`. The fallback is
// correct for a code from a NEWER server; it is not correct for a code this
// build's own server emits.
import { describe, expect, it } from 'vitest';
import { gapCodeMessage } from './diagnosis';

//: The server's closed vocabulary, `bin/_lib_diagnosis.GAP_CODES`. Repeated
//: here because the two languages cannot share a constant; a code added there
//: without copy here renders the fallback, which is what this test refuses.
const SERVER_GAP_CODES = [
  'unknown_context_window',
  'scan_budget_exhausted',
  'unresolved_subagent_attribution',
  'ambiguous_origin_category',
  'no_retained_transcript',
];

describe('gapCodeMessage', () => {
  it('renders real copy for every code the server can publish', () => {
    for (const code of SERVER_GAP_CODES) {
      const message = gapCodeMessage(code);
      expect(message).not.toContain('no description in this build');
      expect(message).not.toContain(code);
      expect(message.length).toBeGreaterThan(0);
    }
  });

  it('names the missing transcript rather than the missing description', () => {
    expect(gapCodeMessage('no_retained_transcript')).toBe(
      'some conversations retained no transcript to count turns in',
    );
  });

  it('keeps the fallback for a code from a newer server', () => {
    expect(gapCodeMessage('a_code_from_the_future')).toBe(
      'a_code_from_the_future (no description in this build)',
    );
  });

  it('renders nothing for an absent code', () => {
    expect(gapCodeMessage(null)).toBe('');
    expect(gapCodeMessage(undefined)).toBe('');
    expect(gapCodeMessage('')).toBe('');
  });
});
