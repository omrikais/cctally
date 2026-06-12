import { describe, it, expect } from 'vitest';
import { computeDiff, computeWrite, computeMultiEdit } from './computeDiff';

describe('computeDiff', () => {
  it('line diff with running numbers', () => {
    const rows = computeDiff('a\nb\nc\n', 'a\nB\nc\n');
    expect(rows.map((r) => r.type)).toEqual(['context', 'del', 'add', 'context']);
    const del = rows.find((r) => r.type === 'del')!;
    const add = rows.find((r) => r.type === 'add')!;
    expect(del.oldNo).toBe(2);
    expect(add.newNo).toBe(2);
    // context line numbers advance both gutters.
    const ctx = rows.filter((r) => r.type === 'context');
    expect(ctx[0].oldNo).toBe(1);
    expect(ctx[0].newNo).toBe(1);
    expect(ctx[1].oldNo).toBe(3);
    expect(ctx[1].newNo).toBe(3);
  });

  it('word-diff segments on the changed pair', () => {
    const rows = computeDiff('return x\n', 'return x + 1\n');
    const add = rows.find((r) => r.type === 'add')!;
    expect(add.segments!.some((s) => s.emph && s.text.includes('+ 1'))).toBe(true);
    const del = rows.find((r) => r.type === 'del')!;
    // The shared "return x" prefix is NOT emphasized on either side.
    expect(del.segments!.some((s) => s.text.includes('return x') && !s.emph)).toBe(true);
    // The del side reconstructs the old line; the add side reconstructs the new.
    expect(del.segments!.map((s) => s.text).join('')).toBe('return x');
    expect(add.segments!.map((s) => s.text).join('')).toBe('return x + 1');
  });

  it('computeWrite is all-add', () => {
    const rows = computeWrite('one\ntwo\n');
    expect(rows.every((r) => r.type === 'add')).toBe(true);
    expect(rows.map((r) => r.newNo)).toEqual([1, 2]);
    expect(rows.map((r) => r.oldNo)).toEqual([null, null]);
    expect(rows.map((r) => r.text)).toEqual(['one', 'two']);
  });

  it('equal input yields only context rows, no segments', () => {
    const rows = computeDiff('x\ny\n', 'x\ny\n');
    expect(rows.map((r) => r.type)).toEqual(['context', 'context']);
    expect(rows.every((r) => r.segments === undefined)).toBe(true);
  });

  it('empty old → all rows are adds (whole content added)', () => {
    const rows = computeDiff('', 'new1\nnew2\n');
    expect(rows.map((r) => r.type)).toEqual(['add', 'add']);
    // No preceding del run, so no word-diff pairing happens.
    expect(rows.every((r) => r.segments === undefined)).toBe(true);
  });

  it('unequal del/add line counts: extra unpaired lines stay plain', () => {
    const rows = computeDiff('a\nb\n', 'A\n');
    const dels = rows.filter((r) => r.type === 'del');
    const adds = rows.filter((r) => r.type === 'add');
    expect(dels.length).toBe(2);
    expect(adds.length).toBe(1);
    // Only the first del/add pair gets word segments; the trailing del stays plain.
    expect(adds[0].segments).toBeDefined();
    expect(dels[1].segments).toBeUndefined();
  });
});

describe('computeMultiEdit', () => {
  it('produces one hunk per edit, in order, each its own DiffRow[]', () => {
    const hunks = computeMultiEdit([
      { old_string: 'a', new_string: 'b' },
      { old_string: 'c\nc2\n', new_string: 'C\nc2\n' },
    ]);
    expect(hunks.length).toBe(2);
    // First hunk: a → b (a del/add pair).
    expect(hunks[0].map((r) => r.type)).toEqual(['del', 'add']);
    // Second hunk: only the first line changed; second line is context.
    expect(hunks[1].map((r) => r.type)).toEqual(['del', 'add', 'context']);
  });

  it('tolerates non-string edit leaves (defensive): missing strings → empty', () => {
    const hunks = computeMultiEdit([
      { old_string: 'x', new_string: 'y' },
      { old_string: undefined, new_string: 'z' } as unknown as { old_string: string; new_string: string },
    ]);
    expect(hunks.length).toBe(2);
    // Second hunk treats the missing old_string as '' → pure add.
    expect(hunks[1].every((r) => r.type === 'add')).toBe(true);
  });

  it('non-array input yields no hunks', () => {
    expect(computeMultiEdit(undefined as unknown as [])).toEqual([]);
  });
});
