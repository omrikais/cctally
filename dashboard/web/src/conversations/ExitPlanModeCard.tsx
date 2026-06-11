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
// Three-state outcome — never default to "approved" on an ambiguous result.
function outcomeOf(call: Call): Outcome {
  const r = call.result;
  if (r == null) return 'awaiting';
  if (r.is_error || /doesn't want to proceed|rejected|keep planning|reject/i.test(r.text)) return 'rejected';
  if (/approv/i.test(r.text)) return 'approved';
  return 'responded';
}

export function ExitPlanModeCard({ call }: { call: Call }) {
  const plan = planOf(call);
  const outcome = outcomeOf(call);
  const [expanded, setExpanded] = useState(false);

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
        <div className={'conv-plan-md' + (expanded ? '' : ' conv-plan-md--clamp')}>
          <Markdown>{plan}</Markdown>
        </div>
        {!expanded && (
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
