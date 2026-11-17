// #923 S2 — where a disambiguated project label may be truncated.
//
// Both producers append the text that tells two same-name projects apart as a
// trailing parenthesized group: Codex an ordinal (`repo (1)`), Claude a parent
// qualifier (`api (work)`, `api (work/acme)`) or an ordinal. A surface that
// ellipsizes the whole label cuts exactly that text away. This splits a label
// into a base, which may truncate, and a suffix, which may not.
//
// It is a presentation heuristic, not a decoder: neither producer guarantees
// balanced parentheses in the directory text it interpolates. So it is
// conservative in three outcomes —
//   - `plain`: no parenthesis, or balanced ones without a qualifying trailing
//     group (`(unknown)`, `foo(1)`); truncates as before.
//   - `split`: balanced, ending in one or more space-preceded groups. The
//     suffix is the WHOLE trailing run of groups, separator space included, so
//     a qualifier that itself contains `) (` stays protected. `base + suffix`
//     is always the label, byte for byte; nothing is trimmed or normalized.
//   - `ambiguous`: an unbalanced parenthesis. The qualifier cannot be located,
//     so the caller must show the whole label rather than truncate any of it.
export type ProjectLabelParts =
  | { kind: 'plain' }
  | { kind: 'split'; base: string; suffix: string }
  | { kind: 'ambiguous' };

export function classifyProjectLabel(label: string): ProjectLabelParts {
  let depth = 0;
  let sawParen = false;
  for (const ch of label) {
    if (ch === '(') {
      depth += 1;
      sawParen = true;
    } else if (ch === ')') {
      depth -= 1;
      sawParen = true;
      if (depth < 0) return { kind: 'ambiguous' };
    }
  }
  if (depth !== 0) return { kind: 'ambiguous' };
  if (!sawParen || !label.endsWith(')')) return { kind: 'plain' };

  let start = label.length;
  while (label[start - 1] === ')') {
    // The matching `(` of the `)` at start - 1, by a backward balanced scan.
    // The whole label is balanced, so it exists.
    let level = 0;
    let open = -1;
    for (let i = start - 1; i >= 0; i -= 1) {
      if (label[i] === ')') level += 1;
      else if (label[i] === '(') {
        level -= 1;
        if (level === 0) { open = i; break; }
      }
    }
    if (open < 1 || label[open - 1] !== ' ') break;
    start = open - 1;
  }

  const base = label.slice(0, start);
  if (start === label.length || !/\S/.test(base)) return { kind: 'plain' };
  return { kind: 'split', base, suffix: label.slice(start) };
}
