import type { ToolOutcome } from '../types/conversation';

// #463 S3 §5.4 — the trailing outcome on a Codex tool chip. Every wording is
// paired wide/narrow because the summary row's rigid children starve its two
// flexible ones at 390px; the shipped .conv-status-wide / .conv-status-narrow
// media pair selects one (index.css, equal specificity, media rule winning on
// source order — never a specificity hack).
//
// `unknown` is a REAL state covering 17.6% of outputs, not an absence, so it
// gets an explicit neutral wording rather than rendering nothing.
const WORDS: Record<ToolOutcome['status'], { wide: string; narrow: string }> = {
  completed: { wide: 'ok', narrow: 'ok' },
  failed: { wide: 'error', narrow: 'err' },
  running: { wide: 'running', narrow: 'run' },
  unknown: { wide: 'outcome unknown', narrow: 'unknown' },
};

const COMPLETED_STATUSES = new Set(['completed', 'ok', 'returned', 'success']);
const FAILED_STATUSES = new Set(['error', 'failed', 'interrupted']);
const RUNNING_STATUSES = new Set(['in_progress', 'requested', 'running']);

// Provider-native secondary cards retain several equivalent status dialects.
// Normalize them at the shared presentation boundary so every card uses the
// same four user-facing outcomes without rewriting the retained provider data.
export function outcomeFromStatus(status: string | null | undefined, isError = false): ToolOutcome {
  const normalized = status?.toLowerCase() ?? '';
  const normalizedStatus: ToolOutcome['status'] = isError || FAILED_STATUSES.has(normalized) ? 'failed'
    : COMPLETED_STATUSES.has(normalized) ? 'completed'
    : RUNNING_STATUSES.has(normalized) ? 'running'
    : 'unknown';
  return { status: normalizedStatus, exit_code: null, wall_time_seconds: null };
}

// The evidence behind the word, kept out of the collapsed row itself. Exit code
// and wall time are rigid text nobody needs at a glance, so they ride the
// tooltip and the expanded card body rather than the summary line.
function evidence(outcome: ToolOutcome): string | undefined {
  const parts: string[] = [];
  if (outcome.exit_code != null) parts.push(`exit ${outcome.exit_code}`);
  if (outcome.wall_time_seconds != null) parts.push(`${outcome.wall_time_seconds}s`);
  return parts.length ? parts.join(' · ') : undefined;
}

// F11a recovered the exit code and the wall time from the harness preamble, and
// they reached the reader through a `title` attribute alone. A `title` has no
// touch affordance, so on the 390px viewport the spec targets they were
// unreachable — which is the viewport where the collapsed row is shortest and
// the evidence matters most. Every module that renders an OutcomeBadge renders
// this line in its expanded body as well, with no exception; a card whose
// summary swaps the badge for its own failure marker still renders the line,
// because the exit code is what the reader wanted. That universal is enforced
// by outcomeEvidenceParity.test.tsx, which asserts it per family AND scans the
// source tree, so a new card family cannot ship a badge without the line.
export function OutcomeEvidence({ outcome }: { outcome: ToolOutcome }) {
  const text = evidence(outcome);
  if (!text) return null;
  return <p className="conv-outcome-evidence">{text}</p>;
}

export function OutcomeBadge({ outcome, isError, truncated }: {
  outcome: ToolOutcome;
  // The block-level error flag is the UNION of every failure signal (a patch
  // whose `success` is false, an MCP error, a failed outcome), so it wins over
  // the card's own status word — otherwise a failed patch whose output card
  // resolved to `completed` would show "ok" beside an error result.
  isError?: boolean;
  truncated?: boolean;
}) {
  const status = isError ? 'failed' : outcome.status;
  // `?? WORDS.unknown`: the adapter normalizes an unmodelled status today, but
  // an unguarded lookup here throws INSIDE the render tree, which blanks the
  // whole conversation rather than one chip. The neutral wording degrades to
  // exactly what an unresolved outcome already means.
  const words = WORDS[status] ?? WORDS.unknown;
  return (
    <span className={`conv-outcome conv-outcome--${status}`} title={evidence(outcome)}>
      <span className="conv-status-wide">{words.wide}{truncated ? ' · truncated' : ''}</span>
      <span className="conv-status-narrow">{words.narrow}{truncated ? ' · trunc' : ''}</span>
    </span>
  );
}
