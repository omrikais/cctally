// Pure helpers for AskUserQuestion answer sourcing + option matching (#177 S2).

// Fallback for pre-capture transcripts: extract {question: answer} from the
// harness result string. Preamble-agnostic — the regex only matches the
// quoted "Q"="A" pairs, so "Your questions have been answered" /
// "User has answered your questions" both work.
export function parseAnswersFromResult(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /"([^"]*)"\s*=\s*"([^"]*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) out[m[1]] = m[2];
  return out;
}

// Map a chosen-answer string to the option label(s) it selected, WITHOUT a
// naive split on ", " (an option label may legitimately contain ", ").
// 1) exact full-answer match → that one option (single-select / single multi);
// 2) greedy longest-label-first partition into labels joined by ", ";
// 3) if the answer is not cleanly covered → a custom ("Other") free-text answer.
export function matchSelectedLabels(
  answer: string,
  options: { label: string }[],
): { selected: string[]; custom: string | null } {
  const labels = options.map((o) => o.label);
  if (labels.includes(answer)) return { selected: [answer], custom: null };
  const byLen = [...labels].sort((a, b) => b.length - a.length);
  let rest = answer;
  const selected: string[] = [];
  while (rest.length) {
    const hit = byLen.find((l) => rest === l || rest.startsWith(l + ', '));
    if (!hit) break;
    selected.push(hit);
    rest = rest === hit ? '' : rest.slice(hit.length + 2);
  }
  if (rest.length === 0 && selected.length) return { selected, custom: null };
  return { selected: [], custom: answer };
}
