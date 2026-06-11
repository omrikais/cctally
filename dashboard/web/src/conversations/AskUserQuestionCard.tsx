import type { ConversationBlock } from '../types/conversation';
import { QuestionIcon, CheckIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { parseAnswersFromResult, matchSelectedLabels } from './askAnswers';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
interface Option { label: string; description?: string; preview?: string }
interface Question { question: string; header?: string; multiSelect?: boolean; options?: Option[] }

function questionsOf(call: Call): Question[] {
  const qs = (call.input as { questions?: unknown } | null | undefined)?.questions;
  return Array.isArray(qs) ? (qs as Question[]) : [];
}

// answers: structured map preferred; else parsed from the harness result string.
function answersOf(call: Call): Record<string, string> {
  if (call.answers && typeof call.answers === 'object') return call.answers;
  if (call.result?.text) return parseAnswersFromResult(call.result.text);
  return {};
}

export function AskUserQuestionCard({ call }: { call: Call }) {
  const questions = questionsOf(call);
  const answers = answersOf(call);
  const hasAnswers = Object.keys(answers).length > 0;
  const copyText = call.answers ? JSON.stringify(call.answers, null, 2) : (call.result?.text ?? '');

  return (
    <details className="conv-chip conv-ask" open>
      <summary className="conv-ask-eyebrow">
        <span className="conv-chev" aria-hidden="true" />
        <QuestionIcon />
        <span className="conv-chip-name">Asked you</span>
        <span className="conv-ask-count">
          · {questions.length} question{questions.length === 1 ? '' : 's'}
          {hasAnswers ? '' : ' · awaiting answer'}
        </span>
      </summary>
      <div className="conv-ask-body">
        {/* CopyButton lives in the body, NOT the summary — a click inside a
            <summary> would also toggle the <details> open/closed. */}
        {copyText && <div className="conv-ask-copy"><CopyButton text={copyText} /></div>}
        {questions.map((q, qi) => {
          const opts = q.options ?? [];
          const answer = answers[q.question];
          const { selected, custom } = answer != null
            ? matchSelectedLabels(answer, opts)
            : { selected: [] as string[], custom: null };
          const chosen = new Set(selected);
          return (
            <div className="conv-ask-q" key={qi}>
              <div className="conv-ask-qhead">
                {q.header && <span className="conv-ask-tag">{q.header}</span>}
                <span className="conv-ask-ms">{q.multiSelect ? 'select multiple' : 'single select'}</span>
              </div>
              <p className="conv-ask-qtext">{q.question}</p>
              <div className="conv-ask-opts">
                {opts.map((o, oi) => (
                  <div key={oi}
                    className={'conv-ask-opt' + (chosen.has(o.label) ? ' conv-ask-opt--chosen' : '')}>
                    <div className="conv-ask-opt-label">
                      {chosen.has(o.label) && <CheckIcon />}
                      {o.label}
                      {chosen.has(o.label) && <span className="conv-ask-pick">your choice</span>}
                    </div>
                    {o.description && <div className="conv-ask-opt-desc">{o.description}</div>}
                    {o.preview && <pre className="conv-ask-opt-preview">{o.preview}</pre>}
                  </div>
                ))}
              </div>
              {custom != null && (
                <div className="conv-ask-custom">
                  <span className="conv-ask-pick">your answer</span> {custom}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}
