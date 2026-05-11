import { useEffect, useRef } from 'react';
import type { UpdateStreamEvent } from '../store/store';

interface UpdateStreamProps {
  events: UpdateStreamEvent[];
  // Cap on how many lines we render — the in-memory buffer can grow
  // unboundedly during a long brew install; truncating keeps DOM
  // size stable. Prefer the tail (last N) since that's what the user
  // is actively reading.
  maxLines?: number;
}

// Scrollable log viewer. Auto-scrolls to bottom on each new line as
// long as the user hasn't scrolled up (preserving manual scroll-back
// to inspect earlier output).
//
// Color convention matches the visual companion mockup
// (.superpowers/brainstorm/26273-1778392417/content/dashboard-update-notification.html):
//   stdout — neutral text
//   stderr — red (.update-stream-line.err)
//   step   — section header (.update-stream-step)
//   exit   — neutral italic ("exited rc=N after Ts")
//
// `event_type === 'execvp' | 'done' | 'error_event' | 'heartbeat'` are
// terminal/keep-alive events handled in the modal (status transitions);
// they don't render here.
export function UpdateStream({ events, maxLines = 500 }: UpdateStreamProps) {
  const scrollRef = useRef<HTMLPreElement>(null);
  const stickyBottomRef = useRef(true);

  const renderable = events.filter(
    (e) => e.type === 'stdout' || e.type === 'stderr' || e.type === 'step' || e.type === 'exit',
  );
  const visible = renderable.slice(-maxLines);
  const truncatedCount = renderable.length - visible.length;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickyBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [events.length]);

  function onScroll(e: React.UIEvent<HTMLPreElement>) {
    const el = e.currentTarget;
    // Within 8px of the bottom counts as "still tailing" — re-stick.
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
    stickyBottomRef.current = atBottom;
  }

  if (visible.length === 0) {
    return (
      <pre
        className="update-stream"
        ref={scrollRef}
        onScroll={onScroll}
        aria-live="polite"
        aria-label="Update output"
      >
        <span className="update-stream-empty">Waiting for output…</span>
      </pre>
    );
  }

  return (
    <pre
      className="update-stream"
      ref={scrollRef}
      onScroll={onScroll}
      aria-live="polite"
      aria-label="Update output"
    >
      {truncatedCount > 0 ? (
        <span className="update-stream-trunc">
          (… {truncatedCount} earlier line{truncatedCount === 1 ? '' : 's'} truncated)
          {'\n'}
        </span>
      ) : null}
      {visible.map((ev, i) => {
        if (ev.type === 'step') {
          return (
            <span key={i} className="update-stream-step">
              {'==> '}
              {ev.name ?? ev.step ?? ev.data ?? ''}
              {'\n'}
            </span>
          );
        }
        if (ev.type === 'stderr') {
          return (
            <span key={i} className="update-stream-line err">
              {ev.data ?? ''}
              {'\n'}
            </span>
          );
        }
        if (ev.type === 'exit') {
          return (
            <span key={i} className="update-stream-exit">
              {`(exited rc=${ev.rc ?? '?'})`}
              {'\n'}
            </span>
          );
        }
        return (
          <span key={i} className="update-stream-line">
            {ev.data ?? ''}
            {'\n'}
          </span>
        );
      })}
    </pre>
  );
}
