import { describe, expect, it } from 'vitest';
import { extractCommandInvocation, isSystemMarker } from './systemMarkers';

describe('isSystemMarker', () => {
  it('matches a single command-name marker', () => {
    expect(isSystemMarker('<command-name>clear</command-name>')).toBe(true);
  });

  it('matches each anchored wrapper tag', () => {
    expect(isSystemMarker('<command-message>compact</command-message>')).toBe(true);
    expect(isSystemMarker('<command-args></command-args>')).toBe(true);
    expect(isSystemMarker('<local-command-caveat>note</local-command-caveat>')).toBe(true);
  });

  // #186 — the two slash-command output carriers added to the marker tuple so a
  // `<local-command-stdout>…</local-command-stdout>` echo (the title-poisoning
  // line in #186) is recognized as plumbing, not a "You" prompt.
  it('matches the #186 local-command output carriers', () => {
    expect(isSystemMarker('<local-command-stdout>x</local-command-stdout>')).toBe(true);
    expect(isSystemMarker('<local-command-stderr>err</local-command-stderr>')).toBe(true);
  });

  it('matches several concatenated markers (whitespace between)', () => {
    const t = '<command-name>clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>';
    expect(isSystemMarker(t)).toBe(true);
  });

  it('tolerates leading/trailing whitespace', () => {
    expect(isSystemMarker('  \n<command-name>clear</command-name>\n  ')).toBe(true);
  });

  it('does NOT match ordinary prose', () => {
    expect(isSystemMarker('Please run the clear command for me.')).toBe(false);
    expect(isSystemMarker('')).toBe(false);
  });

  it('does NOT match prose that merely quotes a marker mid-sentence', () => {
    expect(isSystemMarker('The <command-name>clear</command-name> tag resets context.')).toBe(false);
  });

  it('does NOT match a marker inside a fenced code block', () => {
    const fenced = '```\n<command-name>clear</command-name>\n```';
    expect(isSystemMarker(fenced)).toBe(false);
  });

  it('does NOT match a marker followed by trailing prose', () => {
    expect(isSystemMarker('<command-name>clear</command-name> and then some text')).toBe(false);
  });

  it('does NOT match an unrelated/unknown tag', () => {
    expect(isSystemMarker('<thinking>hmm</thinking>')).toBe(false);
  });

  it('rejects a wrapper whose close tag is a different marker tag (the \\1 backreference is load-bearing)', () => {
    expect(isSystemMarker('<command-name>x</command-args>')).toBe(false);
  });

  it('runs in linear time on a large valid-prefix + trailing-prose input (no catastrophic backtracking)', () => {
    const pathological = '<command-name>x</command-name>'.repeat(2000) + ' trailing prose';
    const start = performance.now();
    const result = isSystemMarker(pathological);
    const elapsed = performance.now() - start;
    expect(result).toBe(false);
    expect(elapsed).toBeLessThan(100); // the prior lazy-quantifier regex hung here
  });
});

// #188 — a slash-command invocation carrying a real prompt in <command-args> is
// a user turn. extractCommandInvocation mirrors the Python kernel
// _extract_command_invocation: pure marker + non-empty args ⇒ {name, args};
// empty-args control commands and stdout-only markers ⇒ null. The block-aware
// all-text guard is applied by the caller (MessageItem), mirroring the Python
// caller's all-text check — the same posture as isSystemMarker.
describe('extractCommandInvocation (#188)', () => {
  it('extracts non-empty command args with the name', () => {
    const raw =
      '<command-message>frontend-design:frontend-design</command-message>' +
      '<command-name>/frontend-design</command-name>' +
      '<command-args>Audit the reader UI and file issues.</command-args>';
    expect(extractCommandInvocation(raw)).toEqual({
      name: '/frontend-design',
      args: 'Audit the reader UI and file issues.',
    });
  });

  it('extracts terse args', () => {
    expect(
      extractCommandInvocation('<command-name>/effort</command-name><command-args>max</command-args>'),
    ).toEqual({ name: '/effort', args: 'max' });
  });

  it('returns null for empty args (/clear and friends)', () => {
    expect(
      extractCommandInvocation('<command-name>/clear</command-name><command-args></command-args>'),
    ).toBeNull();
  });

  it('returns null for whitespace-only args', () => {
    expect(
      extractCommandInvocation('<command-name>/compact</command-name><command-args>  \n </command-args>'),
    ).toBeNull();
  });

  it('returns null when there is no <command-args> tag', () => {
    expect(
      extractCommandInvocation('<command-name>/exit</command-name><command-message>exit</command-message>'),
    ).toBeNull();
  });

  it('returns null for a stdout-only marker', () => {
    expect(
      extractCommandInvocation('<local-command-stdout>Set model to Fable 5</local-command-stdout>'),
    ).toBeNull();
  });

  it('returns null for ordinary prose that merely quotes a tag', () => {
    expect(extractCommandInvocation('see <command-args>x</command-args> mid sentence')).toBeNull();
  });

  it('returns an empty name when the marker omits <command-name>', () => {
    expect(extractCommandInvocation('<command-args>just args</command-args>')).toEqual({
      name: '',
      args: 'just args',
    });
  });
});
