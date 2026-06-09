// Relative date-section label for a browse rail row (#165 Q5). Pure: computed
// in the display tz against an injected `now` (testable). Browse-only.
const MS_DAY = 86_400_000;

function ymdInTz(ms: number, tz: string): { y: number; m: number; d: number } {
  const f = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const [y, m, d] = f.format(new Date(ms)).split('-').map(Number);
  return { y, m, d };
}

export function railDateBucket(startedUtc: string, tz: string, now: number): string {
  const t = Date.parse(startedUtc);
  const a = ymdInTz(t, tz);
  const b = ymdInTz(now, tz);
  // calendar-day delta via UTC-midnight of the tz-local Y/M/D
  const dayA = Date.UTC(a.y, a.m - 1, a.d);
  const dayB = Date.UTC(b.y, b.m - 1, b.d);
  const days = Math.round((dayB - dayA) / MS_DAY);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'This Week';
  if (a.y === b.y && a.m === b.m) return 'This Month';
  return new Intl.DateTimeFormat('en-US', { timeZone: tz, month: 'long', year: 'numeric' })
    .format(new Date(t));
}
