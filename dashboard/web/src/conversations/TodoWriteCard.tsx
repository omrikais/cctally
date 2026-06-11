import type { ConversationBlock } from '../types/conversation';
import { ChecklistIcon, CheckIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
type Status = 'completed' | 'in_progress' | 'pending';
interface Todo { content: string; status: string; activeForm?: string }

function todosOf(call: Call): Todo[] {
  const t = (call.input as { todos?: unknown } | null | undefined)?.todos;
  return Array.isArray(t) ? (t as Todo[]) : [];
}
function norm(s: string): Status {
  return s === 'completed' || s === 'in_progress' ? s : 'pending';
}

export function TodoWriteCard({ call }: { call: Call }) {
  const todos = todosOf(call);
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
        <span className="conv-chip-name">TodoWrite</span>
        <span className="conv-todo-preview">{preview}</span>
        <span className="conv-todo-minibar" aria-hidden="true"><i style={{ width: `${pct}%` }} /></span>
        <span className="conv-todo-frac">{done} / {n}</span>
      </summary>
      <div className="conv-todo-body">
        <div className="conv-todo-head">
          <span className="conv-todo-title">Todos · {done} / {n} done</span>
          <CopyButton text={copyText} />
        </div>
        <div className="conv-todo-bar" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
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
      </div>
    </details>
  );
}
