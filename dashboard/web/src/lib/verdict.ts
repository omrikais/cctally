import type { Verdict } from '../types/envelope';

// Python emits lowercase verdict strings (see CLAUDE.md gotcha). This map
// is the ONLY place the display labels/classes/warn/accent/glyph live.

export interface VerdictInfo {
  label: 'OVER' | 'WARN' | 'OK';
  cls: 'over' | 'warn' | 'good';
  warn: boolean;
  accent: 'accent-red' | 'accent-amber' | 'accent-green';
  glyph: '⛔' | '⚠' | '✓';
}

export const VERDICT_MAP: Record<Verdict, VerdictInfo> = {
  capped: { label: 'OVER', cls: 'over', warn: true,  accent: 'accent-red',   glyph: '⛔' },
  cap:    { label: 'WARN', cls: 'warn', warn: true,  accent: 'accent-amber', glyph: '⚠' },
  ok:     { label: 'OK',   cls: 'good', warn: false, accent: 'accent-green', glyph: '✓' },
};

export function resolveVerdict(v: Verdict | null | undefined): VerdictInfo | null {
  if (v == null) return null;
  return VERDICT_MAP[v] ?? null;
}
