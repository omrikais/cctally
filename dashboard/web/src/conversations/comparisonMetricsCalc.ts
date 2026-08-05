import type { ConversationOutline } from '../types/conversation';

// #217 S7 F10 — pure metric extraction for the comparison delta strip. Sourced
// from each session's /outline, with the exact shapes verified against the
// backend builder (bin/_lib_conversation_query.py) + the TS types:
//   cost     = stats.cost_usd
//   tokens   = the SUM of the stats.tokens OBJECT (input + output +
//              cache_creation + cache_read) — cache-inclusive, matching the
//              issue-#104 "Total Tokens includes cache" convention. NOT a scalar.
//   prompts  = the length of the computed prompt spine (passed in) — NOT
//              stats.turns.human, which counts every assembled item BEFORE the
//              sidechain/subagent filter and so over-counts.
//   errors   = stats.error_count
//   duration = stats.duration_seconds (NULLABLE — preserved as null, rendered
//              "—" by the strip)
//   files    = the outline's top-level files[] length (the S5 additive key, not
//              under stats; absent on an older server → 0).
export interface ComparisonMetrics {
  cost: number;
  tokens: number;
  prompts: number;
  // #463 S4 §6.5 — NULLABLE, following durationSeconds. `?? 0` turned "nobody
  // could tell" into "nothing failed", so Compare rendered a confident zero for
  // a conversation whose failure evidence is gone. The strip renders the
  // declined state instead.
  errors: number | null;
  durationSeconds: number | null;
  files: number;
}

export function metricsFromOutline(
  o: ConversationOutline,
  promptSpineLength: number,
): ComparisonMetrics {
  const t = o.stats.tokens;
  return {
    cost: o.stats.cost_usd ?? 0,
    tokens: t?.source === 'codex'
      ? (t.input ?? 0) + (t.output ?? 0)
      : (t?.input ?? 0) + (t?.output ?? 0) + (t?.cache_creation ?? 0) + (t?.cache_read ?? 0),
    prompts: promptSpineLength,
    errors: o.stats.error_count ?? null,
    durationSeconds: o.stats.duration_seconds ?? null,
    // #463 S4 §6.5 — prefer whichever array has entries. The precedence read
    // `provider_files?.length ?? files?.length`, and `0` is not nullish, so an
    // EMPTY provider list masked a populated rich one. §6.2 omits
    // `provider_files` for Codex so the mask does not fire today; the
    // precedence is wrong regardless of whether anything currently trips it.
    files: (o.provider_files?.length || o.files?.length) ?? 0,
  };
}
