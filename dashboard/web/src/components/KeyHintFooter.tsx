// KeyHintFooter — the modal footer hint row (keyboard-shortcut chips
// separated by `·`, ending in a trailing slot that defaults to the
// SyncChip), extracted from ProjectsModal's inline footer so the Projects
// and History modals share one recipe (S8, issue #254).
//
// `hints` render left→right with an N-1 `·` separator between adjacent
// items; the trailing slot (default <SyncChip/>) sits at the end with no
// preceding separator. The generic `key-hint-footer` class carries the
// shared styling; callers may pass an extra `className` (e.g. the legacy
// `projects-modal-footer-hint`) so existing CSS + test selectors resolve.
import type { ReactNode } from 'react';
import { Fragment } from 'react';
import { SyncChip } from './SyncChip';

export interface KeyHint {
  keys: ReactNode;
  label: string;
}

interface Props {
  hints: KeyHint[];
  trailing?: ReactNode;
  className?: string;
  'data-testid'?: string;
}

export function KeyHintFooter({ hints, trailing, className, 'data-testid': dataTestId }: Props) {
  return (
    <div
      className={`key-hint-footer ${className ?? ''}`.trim()}
      data-testid={dataTestId}
      aria-live="off"
    >
      {hints.map((h, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="sep" aria-hidden="true">·</span>}
          <span>{h.keys} {h.label}</span>
        </Fragment>
      ))}
      {trailing ?? <SyncChip />}
    </div>
  );
}
