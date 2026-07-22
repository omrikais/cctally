import type { ChecklistTodo } from '../types/conversation';
import { ChecklistIcon, CheckIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';

type Status = 'completed' | 'in_progress' | 'pending';

function norm(s: string): Status {
  return s === 'completed' || s === 'in_progress' ? s : 'pending';
}

// The shared approved checklist visual (collapsed chip → expanded checklist +
// progress bar). Both TodoWriteCard (legacy) and TaskChecklistCard (the live
// Task* family) are thin adapters around this; only the chip label differs.
// `todos` is already normalized to the {content, status, activeForm?} shape by
// the adapter, so the renderer stays a pure function of its props.
export function ChecklistCard({ todos, label, description, statusText, resultText }: {
  todos: ChecklistTodo[];
  label: string;
  description?: string | null;
  statusText?: string;
  resultText?: string;
}) {
  const n = todos.length;
  const done = todos.filter((t) => norm(t.status) === 'completed').length;
  const current = todos.find((t) => norm(t.status) === 'in_progress')
    ?? todos.find((t) => norm(t.status) === 'pending');
  const preview = n === 0 ? 'no todos' : (done === n ? 'all done' : (current?.content ?? ''));
  const pct = n === 0 ? 0 : Math.round((done / n) * 100);
  const copyText = todos.map((t) => {
    const mark = norm(t.status) === 'completed' ? 'x' : norm(t.status) === 'in_progress' ? '~' : ' ';
    return `[${mark}] ${t.content}`;
  }).join('\n');

  return (
    <details className="conv-chip conv-todo">
      <summary className="conv-todo-summary">
        <span className="conv-chev" aria-hidden="true" />
        <ChecklistIcon />
        <span className="conv-chip-name">{label}</span>
        <span className="conv-todo-preview">{preview}</span>
        <span className="conv-todo-minibar" aria-hidden="true"><i style={{ width: `${pct}%` }} /></span>
        <span className="conv-todo-frac">{done} / {n}</span>
        {statusText && <span className="conv-chip-status">· {statusText}</span>}
      </summary>
      <div className="conv-todo-body">
        <div className="conv-todo-head">
          <span className="conv-todo-title">Todos · {done} / {n} done</span>
          <CopyButton text={copyText} />
        </div>
        <div className="conv-todo-bar" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
        {description && <p className="conv-native-plan-explanation">{description}</p>}
        <ul className="conv-todo-items">
          {todos.map((t, i) => {
            const s = norm(t.status);
            const cls = s === 'completed' ? 'conv-todo-item--done'
              : s === 'in_progress' ? 'conv-todo-item--cur' : 'conv-todo-item--todo';
            return (
              <li key={i} className={`conv-todo-item ${cls}`}>
                {s === 'completed'
                  ? <CheckIcon />
                  : <span className={'conv-todo-ring' + (s === 'in_progress' ? ' conv-todo-ring--cur' : '')} aria-hidden="true" />}
                <span className="conv-todo-text">{t.content}</span>
                {s === 'in_progress' && <span className="conv-todo-tag">in progress</span>}
              </li>
            );
          })}
        </ul>
        {resultText && <div className="conv-native-plan-result">{resultText}</div>}
      </div>
    </details>
  );
}
