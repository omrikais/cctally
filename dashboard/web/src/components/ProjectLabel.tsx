// #923 S2 — a project label as source prefix, base and suffix spans, so the
// layout can truncate the base and never the suffix that tells two same-name
// projects apart (see `lib/projectLabel.ts`). Its text content is exactly the
// string the surface rendered before: `Claude · ` / `Codex · ` + label when a
// source is given, the label otherwise. The fitted layout is opt-in per site
// in index.css; without it the spans flow inline and change nothing.
import { classifyProjectLabel } from '../lib/projectLabel';

export function ProjectLabel({ label, source }: { label: string; source?: 'claude' | 'codex' }) {
  const parts = classifyProjectLabel(label);
  return (
    <span className="project-label" data-label-kind={parts.kind}>
      {source && <span className="pl-source">{source === 'claude' ? 'Claude' : 'Codex'} · </span>}
      <span className="pl-base">{parts.kind === 'split' ? parts.base : label}</span>
      {parts.kind === 'split' && (
        // The suffix always starts with its separator space (the classifier
        // only splits before a space-preceded group). The space gets its own
        // span so the suffix's `pre` forbids a break right after it, while the
        // qualifier wraps: a long qualifier never leaves the space alone on a line.
        <span className="pl-suffix">
          <span className="pl-sep">{parts.suffix.slice(0, 1)}</span>
          <span className="pl-qual">{parts.suffix.slice(1)}</span>
        </span>
      )}
    </span>
  );
}
