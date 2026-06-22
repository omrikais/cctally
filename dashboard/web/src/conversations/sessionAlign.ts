// Pure LCS alignment of two sessions' main-thread prompt spines (#217 S7, F10).
//
// Each session is reduced to an ordered list of human prompts (uuid + a short
// label — the prompt's normalized first line). We compute the longest common
// subsequence over the *normalized* labels and walk it into a flat list of
// alignment rows: shared prompts are `match`; an adjacent deleted+added run is a
// `replace` region (paired position-by-position, the divergence ⚡ surface); a
// purely one-sided insertion/deletion is `aOnly`/`bOnly` with no divergence. The
// remainder of an unequal replaced run stays inside the divergence region so the
// whole edited block reads as one divergence, not a match-then-insert.

export interface SpinePrompt { uuid: string; label: string; }
export type AlignKind = 'match' | 'replace' | 'aOnly' | 'bOnly';
export interface AlignmentRow {
  kind: AlignKind;
  a: SpinePrompt | null;
  b: SpinePrompt | null;
  /** true for `replace` rows and for one-sided rows that sit inside a replaced region (⚡). */
  divergence: boolean;
}

export function normalizeLabel(label: string): string {
  return label.trim().replace(/\s+/g, ' ').toLowerCase();
}

type Op = { tag: 'eq' | 'del' | 'add'; a: SpinePrompt | null; b: SpinePrompt | null };

function lcsOps(a: SpinePrompt[], b: SpinePrompt[]): Op[] {
  const na = a.map(x => normalizeLabel(x.label));
  const nb = b.map(x => normalizeLabel(x.label));
  const m = a.length, n = b.length;
  // DP table of LCS lengths.
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--)
    for (let j = n - 1; j >= 0; j--)
      dp[i][j] = na[i] === nb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops: Op[] = [];
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (na[i] === nb[j]) { ops.push({ tag: 'eq', a: a[i], b: b[j] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ tag: 'del', a: a[i], b: null }); i++; }
    else { ops.push({ tag: 'add', a: null, b: b[j] }); j++; }
  }
  while (i < m) { ops.push({ tag: 'del', a: a[i++], b: null }); }
  while (j < n) { ops.push({ tag: 'add', a: null, b: b[j++] }); }
  return ops;
}

export function computeSequenceDiff(a: SpinePrompt[], b: SpinePrompt[]): AlignmentRow[] {
  const ops = lcsOps(a, b);
  const rows: AlignmentRow[] = [];
  let k = 0;
  while (k < ops.length) {
    const op = ops[k];
    if (op.tag === 'eq') { rows.push({ kind: 'match', a: op.a, b: op.b, divergence: false }); k++; continue; }
    // Gather a maximal run of dels then adds (a replaced region when both present).
    const dels: SpinePrompt[] = [];
    const adds: SpinePrompt[] = [];
    while (k < ops.length && ops[k].tag === 'del') dels.push(ops[k++].a as SpinePrompt);
    while (k < ops.length && ops[k].tag === 'add') adds.push(ops[k++].b as SpinePrompt);
    const replaced = dels.length > 0 && adds.length > 0;
    const pairs = Math.min(dels.length, adds.length);
    for (let x = 0; x < pairs; x++) rows.push({ kind: 'replace', a: dels[x], b: adds[x], divergence: true });
    for (let x = pairs; x < dels.length; x++) rows.push({ kind: 'aOnly', a: dels[x], b: null, divergence: replaced });
    for (let x = pairs; x < adds.length; x++) rows.push({ kind: 'bOnly', a: null, b: adds[x], divergence: replaced });
  }
  return rows;
}
