// Pure, JSX-free unified-diff helper over jsdiff (#177 S3, spec §4.1). Produces
// a flat row model the DiffCard renders as a unified red/green diff with
// intra-line word emphasis. Imports only diffLines + diffWordsWithSpace
// (tree-shaken). Line numbers are SYNTHESIZED running counters within the diff
// — jsdiff gives counts, not absolute file offsets, and old_string/new_string
// alone don't carry the real `cat -n` numbers (those live in the result
// sub-panel, spec §4.3).

import { diffLines, diffWordsWithSpace } from 'diff';

export interface WordSeg {
  text: string;
  emph: boolean;
}

export interface DiffRow {
  type: 'context' | 'add' | 'del';
  oldNo: number | null;
  newNo: number | null;
  text: string;
  segments?: WordSeg[];
}

// Split a jsdiff chunk value into its constituent lines, dropping the trailing
// empty produced by a final newline (so a "\n"-terminated chunk doesn't emit a
// phantom blank row).
function splitLines(value: string): string[] {
  const lines = value.split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

// Line + intra-line word diff of old vs new. Each line becomes a DiffRow with
// running old/new gutter numbers; changed del/add line pairs additionally carry
// word `segments` for the brighter intra-line emphasis (second pass below).
export function computeDiff(oldStr: string, newStr: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = 1;
  let newNo = 1;
  for (const part of diffLines(oldStr, newStr)) {
    for (const line of splitLines(part.value)) {
      if (part.added) rows.push({ type: 'add', oldNo: null, newNo: newNo++, text: line });
      else if (part.removed) rows.push({ type: 'del', oldNo: oldNo++, newNo: null, text: line });
      else rows.push({ type: 'context', oldNo: oldNo++, newNo: newNo++, text: line });
    }
  }
  return applyWordDiff(rows);
}

// Second pass: for each maximal del-run immediately followed by an add-run,
// word-diff the i-th del line against the i-th add line (the natural
// changed-line pairing) and attach `segments` to both sides. diffWordsWithSpace
// can span multiple lines, but pairing line-by-line keeps the emphasis aligned
// to each visible row. Unequal del/add counts are fine — extra unpaired lines
// stay plain (no segments).
function applyWordDiff(rows: DiffRow[]): DiffRow[] {
  for (let i = 0; i < rows.length; ) {
    if (rows[i].type !== 'del') {
      i++;
      continue;
    }
    let d = i;
    while (d < rows.length && rows[d].type === 'del') d++;
    let a = d;
    while (a < rows.length && rows[a].type === 'add') a++;
    const dels = rows.slice(i, d);
    const adds = rows.slice(d, a);
    const n = Math.min(dels.length, adds.length);
    for (let k = 0; k < n; k++) {
      const w = diffWordsWithSpace(dels[k].text, adds[k].text);
      // del side: keep removed + common parts; emphasize the removed ones.
      dels[k].segments = w.filter((p) => !p.added).map((p) => ({ text: p.value, emph: !!p.removed }));
      // add side: keep added + common parts; emphasize the added ones.
      adds[k].segments = w.filter((p) => !p.removed).map((p) => ({ text: p.value, emph: !!p.added }));
    }
    i = a;
  }
  return rows;
}

// Write has no prior content — every line is an add. Honest "wrote N lines",
// not a real diff (spec §4.1: create-vs-overwrite is not knowable from input).
export function computeWrite(content: string): DiffRow[] {
  return splitLines(content).map((line, i) => ({
    type: 'add' as const,
    oldNo: null,
    newNo: i + 1,
    text: line,
  }));
}
