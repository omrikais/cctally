import { useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { Markdown } from '../components/Markdown';
import { PlanIcon, CheckIcon, WarningIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
type Outcome = 'approved' | 'rejected' | 'responded' | 'awaiting';

function planOf(call: Call): string {
  const p = (call.input as { plan?: unknown } | null | undefined)?.plan;
  return typeof p === 'string' ? p : '';
}

// Cheap content-length proxy for "is this plan long enough to need clamping?".
// A CSS-only clamp can't know the rendered height without JS (ResizeObserver /
// scrollHeight) — measuring would couple this pure render to a layout pass — so
// we approximate: many lines OR a lot of characters → clamp + a "Show full
// plan" toggle; short plans render fully with no clamp and no button.
function planIsLong(plan: string): boolean {
  return plan.split('\n').length > 24 || plan.length > 1400;
}
// #217 S3 E10#6 — hardened (still client-side, best-effort) outcome detection.
// The previous heuristic matched the bare substrings `approv` / `reject`
// ANYWHERE in result.text, so a free-text user RESPONSE that merely mentioned
// those words ("I rejected that earlier idea, but…", "get my manager to approve
// …") was mis-badged Rejected/Approved. We anchor to the canonical Claude Code
// tool-result phrasings instead, and fall through to a neutral `responded` for
// anything else (a free-text user reply IS a response, just not a structured
// approve/reject — a structured signal would need backend work, out of scope).
//   approval : "User has approved your plan…"
//   rejection: "The user doesn't want to proceed with this tool use. The tool
//               use was rejected…" (and the older "keep planning" variant)
// is_error keeps short-circuiting to `rejected` (a denied tool use). The detection
// table lives in ExitPlanModeCard.test.tsx.
const REJECT_RE = /doesn't want to proceed|tool use was rejected|keep planning/i;
const APPROVE_RE = /\b(?:has approved|approved your plan)\b/i;
// Three-state outcome — never default to "approved" on an ambiguous result.
function outcomeOf(call: Call): Outcome {
  const r = call.result;
  if (r == null) return 'awaiting';
  if (r.is_error || REJECT_RE.test(r.text)) return 'rejected';
  if (APPROVE_RE.test(r.text)) return 'approved';
  return 'responded';
}

export function ExitPlanModeCard({ call }: { call: Call }) {
  const plan = planOf(call);
  const outcome = outcomeOf(call);
  const [expanded, setExpanded] = useState(false);
  // Short plans render fully — no clamp, no toggle. Only a long plan is clamped
  // (and gets the "Show full plan" button) until the user expands it.
  const longPlan = planIsLong(plan);
  const clamped = longPlan && !expanded;

  return (
    <details className="conv-chip conv-plan" open>
      <summary className="conv-plan-eyebrow">
        <span className="conv-chev" aria-hidden="true" />
        <PlanIcon />
        <span className="conv-chip-name">Plan proposed</span>
        {outcome === 'approved' && <span className="conv-plan-badge conv-plan-badge--ok"><CheckIcon /> Approved</span>}
        {outcome === 'rejected' && <span className="conv-plan-badge conv-plan-badge--no"><WarningIcon /> Rejected</span>}
        {outcome === 'responded' && <span className="conv-plan-badge conv-plan-badge--neutral">Responded</span>}
      </summary>
      <div className="conv-plan-body">
        {/* CopyButton in the body, not the summary (a summary-click toggles details). */}
        {plan && <div className="conv-plan-copy"><CopyButton text={plan} /></div>}
        <div className={'conv-plan-md' + (clamped ? ' conv-plan-md--clamp' : '')}>
          <Markdown>{plan}</Markdown>
        </div>
        {clamped && (
          <div className="conv-plan-more">
            <button type="button" onClick={() => setExpanded(true)}>Show full plan ↓</button>
          </div>
        )}
        {call.input_truncated && (
          <div className="conv-plan-trunc">
            plan input truncated in this view; full text remains in the original JSONL session file
          </div>
        )}
      </div>
    </details>
  );
}
