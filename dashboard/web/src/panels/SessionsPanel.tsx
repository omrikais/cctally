import { SourceSessionsGrid } from './SourceSessionsGrid';

// One canonical Sessions component is mounted for Claude, Codex, and All.
// Provider-specific envelope vocabulary is normalized by sourceRows.ts; native
// token counters remain available through the source-bound detail route.
export function SessionsPanel() {
  return <SourceSessionsGrid />;
}
