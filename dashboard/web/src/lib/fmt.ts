const pad2 = (n: number): string => String(n).padStart(2, '0');

// FmtCtx — the per-call display context that drives every datetime
// formatter in this module. Per F1 of the localize-datetime-display
// spec, callers obtain `tz` and `offsetLabel` from the snapshot
// envelope's `display` block (via the `useDisplayTz` hook), so the
// browser never resolves "local" itself — it always receives a concrete
// IANA zone the server already resolved.
//
//   tz          — concrete IANA zone, e.g. "Etc/UTC", "America/New_York"
//   offsetLabel — human suffix shown after datetime ("UTC", "PDT", or
//                 numeric "+03" / "-04" when no zone abbrev exists)
export interface FmtCtx {
  tz:          string;
  offsetLabel: string;
}

function isoToDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

// formatToParts indexed by `type` for ergonomic lookup. Browsers may
// emit additional `type: "literal"` separators we ignore.
function partsByType(d: Date, tz: string, opts: Intl.DateTimeFormatOptions): Record<string, string> {
  const formatter = new Intl.DateTimeFormat('en-US', { ...opts, timeZone: tz });
  const parts: Record<string, string> = {};
  for (const p of formatter.formatToParts(d)) parts[p.type] = p.value;
  return parts;
}

// Per-timestamp zone suffix. `ctx.offsetLabel` is computed Python-side
// at the snapshot's `generated_at`, so a January NY timestamp rendered
// from a May (EDT) snapshot would be mislabeled "EDT" when its actual
// wall-clock offset is EST. We re-derive the suffix from the timestamp
// itself via `Intl.DateTimeFormat({ timeZoneName: 'short' })`, which
// yields zone abbrevs ("PDT" / "EST") for IANA zones that have them
// and "GMT-7" / "GMT+3" otherwise. Mirrors the GMT-strip + zero-pad
// cleanup that `fmtDatetimeShortZ` performs so "GMT+3" → "+03",
// "GMT-7" → "-07", and "UTC"/"GMT" → "UTC". Used only by body
// formatters; column-header chips still consume `ctx.offsetLabel`
// directly (those want the snapshot's "now" label, not per-row).
function tzSuffixForInstant(d: Date, tz: string): string {
  const p = partsByType(d, tz, { timeZoneName: 'short' });
  const raw = p.timeZoneName ?? '';
  // "GMT" alone (some browsers emit this for Etc/UTC) collapses to "UTC"
  // for parity with the Python display block default.
  if (raw === 'GMT') return 'UTC';
  // Strip leading "GMT" prefix on numeric forms, e.g. "GMT-7" → "-7".
  const stripped = raw.replace(/^GMT/, '');
  if (stripped === '') return raw; // e.g. "UTC", "EST", "PDT"
  // Pad single-digit numeric offsets ("+3" → "+03") to match the
  // dashboard's existing "+03" / "-07" convention.
  return stripped.replace(/^([+-])(\d)$/, '$10$2');
}

// "May 01 14:00 EST" — long-form, used in current-week pill / details.
// Suffix is derived per-timestamp (not from ctx.offsetLabel) so a
// timestamp from outside the snapshot's DST regime renders with its
// own zone abbrev. See `tzSuffixForInstant` for the derivation.
function fmtDatetimeShort(iso: string | null | undefined, ctx: FmtCtx): string {
  const d = isoToDate(iso);
  if (!d) return '—';
  const p = partsByType(d, ctx.tz, {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false,
  });
  return `${p.month} ${p.day} ${p.hour}:${p.minute} ${tzSuffixForInstant(d, ctx.tz)}`;
}

// "May 01 14:00-04" — compact numeric-offset variant for modal reset
// cells. Strips the leading "GMT" Intl emits with timeZoneName:
// 'shortOffset' and pads single-digit offsets ("+3" → "+03") to match
// upstream label conventions. When Intl returns no offset string
// (extremely old browsers) the trailing offset is dropped silently.
function fmtDatetimeShortZ(iso: string | null | undefined, ctx: FmtCtx): string {
  const d = isoToDate(iso);
  if (!d) return '—';
  const p = partsByType(d, ctx.tz, {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZoneName: 'shortOffset',
  });
  const offset = (p.timeZoneName ?? '').replace(/^GMT/, '');
  const offsetTrim = offset.replace(/^([+-])(\d)$/, '$10$2');
  return `${p.month} ${p.day} ${p.hour}:${p.minute}${offsetTrim || ''}`;
}

// "May 01" — day + month only. Date is not a clock time, so no offset
// suffix; the result is whichever calendar day the ISO falls on inside
// `ctx.tz` (which is what users want to see for "the day this happened").
function fmtDateShort(iso: string | null | undefined, ctx: FmtCtx): string | null {
  const d = isoToDate(iso);
  if (!d) return null;
  const p = partsByType(d, ctx.tz, { month: 'short', day: '2-digit' });
  return `${p.month} ${p.day}`;
}

// "Apr 27" — calendar-date formatter for `YYYY-MM-DD` inputs that
// represent a CALENDAR DAY, not a clock instant (e.g. weekly alert
// payloads' `week_start_date`). Bypasses the tz-conversion path
// `fmtDateShort` performs because passing a date-only ISO through
// `new Date(...)` interprets it as UTC midnight, which `Intl
// .DateTimeFormat({ timeZone: ctx.tz })` then shifts back a day for
// users west of UTC (rendering "Apr 26" for `America/Los_Angeles`).
// Returns null on null/invalid input, mirroring `fmtDateShort`.
function fmtCalendarDateShort(s: string | null | undefined): string | null {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const monthIdx = +m[2] - 1;
  if (monthIdx < 0 || monthIdx > 11) return null;
  return `${months[monthIdx]} ${m[3]}`;
}

// "2026-04-25 14:32 UTC" — row-level Started column. F4 of the spec
// allows callers to pass `{ noSuffix: true }` for contexts where the
// suffix is shown elsewhere (e.g. column header), trimming the body to
// "2026-04-25 14:32".
function fmtStartedShort(
  iso: string | null | undefined,
  ctx: FmtCtx,
  opts: { noSuffix?: boolean } = {},
): string {
  const d = isoToDate(iso);
  if (!d) return '—';
  const p = partsByType(d, ctx.tz, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const body = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  return opts.noSuffix ? body : `${body} ${tzSuffixForInstant(d, ctx.tz)}`;
}

// "just now" / "5m ago" / "2h ago" / "Yesterday" / "May 01" — coarse
// relative-time formatter for surfaces (Recent alerts panel/modal,
// future activity feeds) where exact-to-the-minute timestamps add
// noise. Threshold ladder mirrors common social/dashboard conventions:
//   < 60s        → "just now"
//   < 60min      → "Nm ago"
//   < 24h        → "Nh ago"
//   < 48h        → "Yesterday"
//   else         → calendar `dateShort` (no clock)
// Calendar transitions (today/yesterday) honor `ctx.tz` so a 23:30
// alert in a +03 zone correctly reads "Yesterday" the next morning
// even if `Date.now()` lives in UTC. Matches the chokepoint rule:
// every datetime render in the dashboard goes through `lib/fmt.ts`.
function fmtRelativeOrAbsolute(
  iso: string | null | undefined,
  ctx: FmtCtx,
  nowMs: number = Date.now(),
): string {
  const d = isoToDate(iso);
  if (!d) return '—';
  const deltaMs = nowMs - d.getTime();
  // Clock-skew safety: a future alerted_at (server clock < client clock,
  // or test fixtures with synthetic future times) reads as "just now"
  // rather than "-3m ago".
  if (deltaMs < 60_000) return 'just now';
  if (deltaMs < 3_600_000) return `${Math.floor(deltaMs / 60_000)}m ago`;
  if (deltaMs < 86_400_000) return `${Math.floor(deltaMs / 3_600_000)}h ago`;
  // 24h–48h "Yesterday" requires calendar-day comparison in ctx.tz —
  // a 25h-old alert near a midnight boundary may already be 2 days
  // ago by calendar. Use Intl to extract the calendar date in tz.
  const today = partsByType(new Date(nowMs), ctx.tz, {
    year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const then = partsByType(d, ctx.tz, {
    year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const todayKey = `${today.year}-${today.month}-${today.day}`;
  const thenKey  = `${then.year}-${then.month}-${then.day}`;
  if (todayKey !== thenKey && deltaMs < 172_800_000) {
    // Compute "yesterday" in ctx.tz by subtracting one day from today.
    const y = new Date(nowMs - 86_400_000);
    const yp = partsByType(y, ctx.tz, {
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
    const yKey = `${yp.year}-${yp.month}-${yp.day}`;
    if (thenKey === yKey) return 'Yesterday';
  }
  return fmtDateShort(iso, ctx) ?? '—';
}

// "14:00 UTC" — clock-only format. Replaces ad-hoc
// `iso.toISOString().slice(11, 16)` patterns that hard-coded UTC and
// silently rendered the wrong wall-clock time once a non-UTC tz was
// in play. Distinct from `hhmm` (duration formatter for seconds → "Xh
// YYm"); do not conflate. Per Codex F4, callers may pass
// `{ noSuffix: true }` when the surrounding context (e.g. SVG axis
// labels with no anchor text) doesn't have room for the tz suffix.
// Mirrors the existing `fmt.startedShort` shape verbatim.
function fmtTimeHHmm(
  iso: string | null | undefined,
  ctx: FmtCtx,
  opts: { noSuffix?: boolean } = {},
): string {
  const d = isoToDate(iso);
  if (!d) return '—';
  const p = partsByType(d, ctx.tz, {
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const body = `${p.hour}:${p.minute}`;
  return opts.noSuffix ? body : `${body} ${tzSuffixForInstant(d, ctx.tz)}`;
}

export const fmt = {
  pct0(v: number | null | undefined): string {
    return v == null ? '—' : `${Math.round(v)}%`;
  },
  pct1(v: number | null | undefined): string {
    return v == null ? '—' : `${(+v).toFixed(1)}%`;
  },
  usd2(v: number | null | undefined): string {
    return v == null ? '—' : `$${(+v).toFixed(2)}`;
  },
  usd3(v: number | null | undefined): string {
    return v == null ? '—' : `$${(+v).toFixed(3)}`;
  },
  usd2PerDay(v: number | null | undefined): string {
    return v == null ? '—' : `$${(+v).toFixed(2)} / day`;
  },
  hours1(v: number | null | undefined): string {
    return v == null ? '—' : `${(+v).toFixed(1)} h`;
  },
  ratePctPerHour(v: number | null | undefined): string {
    return v == null ? '—' : `from ${(+v).toFixed(4)} %/h`;
  },
  agoSec(v: number | null | undefined): string {
    return v == null ? '—' : `${Math.max(0, v | 0)}s ago`;
  },
  // Duration formatter — seconds → "Xh YYm". DISTINCT from timeHHmm
  // (which formats an ISO timestamp). Kept untouched by the F1
  // migration because callers pass elapsed seconds, not an ISO string.
  hhmm(v: number | null | undefined): string {
    if (v == null) return '—';
    const h = (v / 3600) | 0;
    const m = ((v % 3600) / 60) | 0;
    return `${h}h ${pad2(m)}m`;
  },
  ddhh(v: number | null | undefined): string {
    if (v == null) return '—';
    const d = (v / 86400) | 0;
    const h = ((v % 86400) / 3600) | 0;
    return `${d}d ${h}h`;
  },
  // "Xh Ym" — elapsed-since formatter for the Block kv sub-line.
  // Mirrors hhmm's null behavior; clamps non-positive to "just now" so
  // clock skew / fixture edge cases (env.generated_at < block_start_at)
  // degrade gracefully.
  elapsedHm(v: number | null | undefined): string {
    if (v == null) return '—';
    if (v <= 0) return 'just now';
    const h = (v / 3600) | 0;
    const m = ((v % 3600) / 60) | 0;
    return `${h}h ${m}m`;
  },
  // ---- Datetime formatters (F1: ctx-based, server-resolved tz) ----
  // All four take an FmtCtx whose tz/offsetLabel come from the
  // snapshot's `display` block via useDisplayTz(). The signature
  // intentionally has no overload preserving the old (iso) → string
  // shape — call sites must thread ctx through. Type errors at
  // unmigrated consumers are EXPECTED until Task 16 fixes them.
  datetimeShort:  fmtDatetimeShort,
  datetimeShortZ: fmtDatetimeShortZ,
  dateShort:      fmtDateShort,
  startedShort:   fmtStartedShort,
  timeHHmm:       fmtTimeHHmm,
  // Threshold-actions T8: Toast helpers. `weekStart` deliberately
  // bypasses the tz-conversion path because its input is a calendar
  // date (`YYYY-MM-DD`), NOT a clock instant — passing it through
  // `new Date(...)` would interpret it as UTC midnight and `Intl
  // .DateTimeFormat({ timeZone: ctx.tz })` would shift the wall-clock
  // day for users west of UTC (rendering "Apr 26" instead of "Apr 27"
  // in PDT). The signature still accepts `ctx` for parity with peer
  // formatters in the `fmt.*` family, but ignores it. `timeOnly` is
  // `timeHHmm` (input is an ISO-8601 timestamp, render "HH:MM <tz>")
  // and continues to use the tz-aware path.
  // Defined here so every datetime render in Toast.tsx routes through
  // lib/fmt.ts per the canonical render-path rule (CLAUDE.md
  // "format_display_dt / lib/fmt.ts are the canonical datetime render
  // paths").
  weekStart:      (s: string | null | undefined, _ctx: FmtCtx): string | null =>
    fmtCalendarDateShort(s),
  timeOnly:       fmtTimeHHmm,
  // Threshold-actions T10/T11: relative-time formatter for the Recent
  // alerts panel/modal (and any future "X ago" surface). Goes through
  // `dateShort` for the >48h fallback so the chokepoint rule still
  // applies. See `fmtRelativeOrAbsolute` for ladder + edge cases.
  relativeOrAbsolute: fmtRelativeOrAbsolute,
  delta(v: number | null | undefined): string {
    if (v == null) return '—';
    const arrow = v > 0 ? '↑' : v < 0 ? '↓' : '·';
    const sign = v > 0 ? '+' : '';
    return `${sign}${v.toFixed(2)} ${arrow}`;
  },
  // "+4.2pp" / "-1.3pp" / "±0.0pp" — percentage points, used by the 5h
  // block delta line. Renders "—" for null. Always 1 decimal.
  pp(v: number | null | undefined): string {
    if (v == null) return '—';
    if (Math.abs(v) < 0.05) return '±0.0pp';
    const sign = v > 0 ? '+' : '−';   // U+2212 minus, matches deltaPct
    return `${sign}${Math.abs(v).toFixed(1)}pp`;
  },
  // "+9%" / "−4%" / "—" — used by Weekly/Monthly panels and modal Δ chip.
  // Input is a fraction (0.09 = 9%); output uses Unicode U+2212 minus on
  // negatives to match the dashboard's typographic standard.
  deltaPct(v: number | null | undefined): string {
    if (v == null) return '—';
    const pct = Math.round(v * 100);
    if (pct === 0) return '0%';
    if (pct > 0) return `+${pct}%`;
    return `−${Math.abs(pct)}%`;
  },
  // 1500 -> "1.5k", 21_300_000 -> "21.3M", 2_500_000_000 -> "2.5B".
  // Drops trailing ".0" (414_000 -> "414k", not "414.0k").
  compact(v: number | null | undefined): string {
    if (v == null) return '—';
    const n = Math.abs(v);
    let out: string;
    if (n >= 1e9) out = (v / 1e9).toFixed(1) + 'B';
    else if (n >= 1e6) {
      const m = v / 1e6;
      out = (m >= 100 ? m.toFixed(0) : m.toFixed(1)) + 'M';
    } else if (n >= 1e3) {
      const k = v / 1e3;
      out = (k >= 100 ? k.toFixed(0) : k.toFixed(1)) + 'k';
    } else {
      return String(v | 0);
    }
    return out.replace(/\.0(?=[kMB]$)/, '');
  },
  deltaCls(v: number | null | undefined, isCurrent: boolean): string {
    if (v == null || isCurrent) return '';
    return v > 0 ? 'delta-pos' : v < 0 ? 'delta-neg' : '';
  },
};
