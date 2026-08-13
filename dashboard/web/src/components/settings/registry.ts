// #513 S2 §1 — the Settings field registry.
//
// Before this file, each Settings field was spelled out in roughly six places:
// a `useState`, a `lastSeen*` ref, a reconcile effect, a `*Dirty` derivation, a
// per-section aggregation, and a branch inside `save()`. Nothing tied those six
// together, so the normalization used by the `#258` re-seed guard could — and
// did — drift from the normalization used by the dirty check. A registry entry
// states each field once and both consumers read the same declaration.
//
// The entry carries `equivalent(draft, server)` rather than leaving the dirty
// check to compare `draft` against `toDraft(server)`. That literal comparison
// is wrong for any field whose draft is a string the user types: with a stored
// budget of 50 and a typed "50.0" the field is canonically clean but lexically
// unequal, so a server tick would be held and the field would read dirty for
// ever. Declaring the equality once is the whole point of the registry, so it
// is declared here and consumed by both the dirty derivation and reconcile.
//
// An INVALID draft is never equivalent to anything. It is therefore always
// dirty and always held against an incoming tick, which is what a user
// mid-edit expects.
import type { Action, AlertsConfig, SessionSortKey, UpdateChannel } from '../../store/store';

export type FieldScope = 'browser' | 'machine';

// The six groups of §2.6. `sessions` is Recent Sessions; `viewer` is the
// conversation viewer; `access` covers dashboard access and updates.
//
// `cli` is the seventh entry and the trailing catch-all: the CLI-only keys
// whose topical home is a surface this dashboard does not render at all (the
// Claude Code status line, the retention policy, the Codex hook deadline).
// No REGISTRY entry ever sits there — it holds manifest rows only, which is
// why the six-group rule and this list do not contradict each other.
export type SectionId =
  | 'display'
  | 'sessions'
  | 'alerts'
  | 'viewer'
  | 'access'
  | 'restore'
  | 'cli';

export const SECTION_IDS: readonly SectionId[] = [
  'display',
  'sessions',
  'alerts',
  'viewer',
  'access',
  'restore',
  'cli',
];

export const SECTION_TITLES: Record<SectionId, string> = {
  display: 'Display & time',
  sessions: 'Recent Sessions',
  alerts: 'Alerts',
  viewer: 'Conversation viewer',
  access: 'Access & updates',
  restore: 'Restore defaults',
  cli: 'Managed from the CLI',
};

export type ParseResult<T> =
  | { ok: true; value: T }
  | { ok: false; message: string };

// Everything the registry can read. One structure for both kinds, because the
// split between "came from the envelope" and "came from local UI state" is
// already carried by `kind`, and a second split in the read argument would only
// mean two shapes to thread through the same reducer.
export interface SettingsSources {
  tz: string;
  // True when the server was launched with `--tz`. The display block is then
  // suppressed from the POST body (the server rejects it with a 409) while the
  // rest of the form saves normally.
  tzPinned: boolean;
  alerts: AlertsConfig;
  markersEnabled: boolean;
  liveTail: boolean;
  lanAuth: boolean;
  channel: UpdateChannel;
  sortDefault: SessionSortKey;
  sessionsPerPage: number;
  filterText: string;
}

interface FieldBase {
  id: string;
  // The human name of the row. One declaration, read by the control's own
  // label, by an error-summary entry, and by the filter index — so a renamed
  // setting cannot end up called two different things in two places.
  label: string;
  section: SectionId;
  scope: FieldScope;
  // The short scope word every row exposes to assistive technology and to the
  // filter index (§2.8). Visible text renders on per-browser rows only.
  scopeWord: string;
}

export interface ServerField<T, D> extends FieldBase {
  kind: 'server';
  path: string;
  scope: 'machine';
  read(sources: SettingsSources): T;
  toDraft(value: T): D;
  equivalent(draft: D, server: T): boolean;
  parse(draft: D): ParseResult<T>;
  // Suppresses this leaf from the POST body even when dirty. Only the
  // timezone uses it, and only under a `--tz` pin.
  suppressed?(sources: SettingsSources): boolean;
}

export interface BrowserField<T, D> extends FieldBase {
  kind: 'browser';
  path: null;
  scope: 'browser';
  read(sources: SettingsSources): T;
  toDraft(value: T): D;
  equivalent(draft: D, server: T): boolean;
  parse(draft: D): ParseResult<T>;
  commit(value: T): Action[];
}

// The two field-less resets. They have no server value and none may be
// invented for them: they reset to `false` on every open, are dirty when
// `true`, and dispatch only on Save.
export interface StagedAction extends FieldBase {
  kind: 'stagedAction';
  path: null;
  scope: 'browser';
  commit(): Action[];
}

// `unknown` rather than a generic parameter: the registry is heterogeneous, so
// a single type argument would have to collapse every field's value type. The
// members are declared as METHODS, whose parameters TypeScript checks
// bivariantly, so each concrete field still assigns to this union while its own
// declaration keeps the precise types the controllers read.
export type SettingsField =
  | ServerField<unknown, unknown>
  | BrowserField<unknown, unknown>
  | StagedAction;

// --- shared field constructors ---------------------------------------------

type NotifierKind = NonNullable<AlertsConfig['notifier']>;

function boolField(
  spec: {
    id: string;
    label: string;
    path: string;
    section: SectionId;
    read: (s: SettingsSources) => boolean;
  },
): ServerField<boolean, boolean> {
  return {
    kind: 'server',
    scope: 'machine',
    scopeWord: 'this machine',
    path: spec.path,
    id: spec.id,
    label: spec.label,
    section: spec.section,
    read: spec.read,
    toDraft: (value) => value,
    equivalent: (draft, server) => draft === server,
    parse: (draft) => ({ ok: true, value: draft }),
  };
}

export type TzDraft = { mode: 'local' | 'utc' | 'custom'; custom: string };

export function tzModeOf(tz: string): TzDraft['mode'] {
  if (tz === 'local') return 'local';
  if (tz === 'utc') return 'utc';
  return 'custom';
}

export function tzTargetOf(draft: TzDraft): string {
  if (draft.mode === 'local') return 'local';
  if (draft.mode === 'utc') return 'utc';
  return draft.custom.trim();
}

// `Intl.DateTimeFormat` throws RangeError on an unknown zone, and that answer
// tracks the host's tzdata rather than a static allowlist we would have to keep
// in step with it.
export function isValidIANA(value: string): boolean {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value }).format(new Date());
    return true;
  } catch {
    return false;
  }
}

// Sessions per page: a base-10 integer between 10 and 1000, parsed only at
// commit (§3.7). The draft is a string so `""` and transient states survive
// exactly as typed; the old implementation clamped on every keystroke, which
// silently rewrote a stored 50 to 10 the moment a user typed "5".
const PER_PAGE_MESSAGE = 'Enter a whole number between 10 and 1000.';

function parsePerPage(draft: string): ParseResult<number> {
  const text = draft.trim();
  if (!/^\d+$/.test(text)) return { ok: false, message: PER_PAGE_MESSAGE };
  const value = Number.parseInt(text, 10);
  if (!Number.isFinite(value) || value < 10 || value > 1000) {
    return { ok: false, message: PER_PAGE_MESSAGE };
  }
  return { ok: true, value };
}

// The weekly budget amount (§5.2). Blank serialises to null — "no budget" is a
// real state and the only way to express it from here. Anything else must
// parse as a finite number greater than zero, which is the server's own
// vocabulary for this leaf.
const BUDGET_MESSAGE = 'Enter an amount greater than 0, or leave blank for no budget.';

function parseWeeklyUsd(draft: string): ParseResult<number | null> {
  const text = draft.trim();
  if (text === '') return { ok: true, value: null };
  if (!/^\d*\.?\d+$/.test(text)) return { ok: false, message: BUDGET_MESSAGE };
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0) {
    return { ok: false, message: BUDGET_MESSAGE };
  }
  return { ok: true, value };
}

// --- the registry ----------------------------------------------------------

const DISPLAY_TZ: ServerField<string, TzDraft> = {
  kind: 'server',
  id: 'display.tz',
  label: 'Display timezone',
  path: 'display.tz',
  section: 'display',
  scope: 'machine',
  scopeWord: 'this machine',
  read: (s) => s.tz,
  toDraft: (value) => ({
    mode: tzModeOf(value),
    custom: tzModeOf(value) === 'custom' ? value : '',
  }),
  equivalent: (draft, server) => {
    const target = tzTargetOf(draft);
    if (draft.mode === 'custom' && !isValidIANA(target)) return false;
    return target === server;
  },
  parse: (draft) => {
    const target = tzTargetOf(draft);
    if (draft.mode === 'custom' && !isValidIANA(target)) {
      return {
        ok: false,
        message: target
          ? `"${target}" is not an IANA zone name. Try a Region/City name such as America/New_York.`
          : 'Type an IANA zone name, such as America/New_York.',
      };
    }
    return { ok: true, value: target };
  },
  suppressed: (s) => s.tzPinned,
};

const ALERTS_NOTIFIER: ServerField<NotifierKind, NotifierKind> = {
  kind: 'server',
  id: 'alerts.notifier',
  label: 'Alert notifier',
  path: 'alerts.notifier',
  section: 'alerts',
  scope: 'machine',
  scopeWord: 'this machine',
  read: (s) => s.alerts.notifier ?? 'auto',
  toDraft: (value) => value,
  equivalent: (draft, server) => draft === server,
  parse: (draft) => ({ ok: true, value: draft }),
};

const UPDATE_CHANNEL: ServerField<UpdateChannel, UpdateChannel> = {
  kind: 'server',
  id: 'update.channel',
  label: 'Update channel',
  path: 'update.channel',
  section: 'access',
  scope: 'machine',
  scopeWord: 'this machine',
  read: (s) => s.channel,
  toDraft: (value) => value,
  equivalent: (draft, server) => draft === server,
  parse: (draft) => ({ ok: true, value: draft }),
};

const BUDGET_WEEKLY_USD: ServerField<number | null, string> = {
  kind: 'server',
  id: 'budget.weekly_usd',
  label: 'Weekly budget',
  path: 'budget.weekly_usd',
  section: 'alerts',
  scope: 'machine',
  scopeWord: 'this machine',
  read: (s) => s.alerts.weekly_usd,
  toDraft: (value) => (value === null ? '' : String(value)),
  equivalent: (draft, server) => {
    const parsed = parseWeeklyUsd(draft);
    return parsed.ok && parsed.value === server;
  },
  parse: parseWeeklyUsd,
};

const SORT_DEFAULT: BrowserField<SessionSortKey, SessionSortKey> = {
  kind: 'browser',
  id: 'prefs.sortDefault',
  label: 'Sort default',
  path: null,
  section: 'sessions',
  scope: 'browser',
  scopeWord: 'this browser',
  read: (s) => s.sortDefault,
  toDraft: (value) => value,
  equivalent: (draft, server) => draft === server,
  parse: (draft) => ({ ok: true, value: draft }),
  // Clearing the sessions header-click override is part of changing the saved
  // default: without it the user's column click would keep winning and the new
  // default would look ignored.
  commit: (value) => [
    { type: 'SAVE_PREFS', patch: { sortDefault: value } },
    { type: 'SET_SORT', key: value },
    { type: 'SET_TABLE_SORT', table: 'sessions', override: null },
  ],
};

const SESSIONS_PER_PAGE: BrowserField<number, string> = {
  kind: 'browser',
  id: 'prefs.sessionsPerPage',
  label: 'Sessions per page',
  path: null,
  section: 'sessions',
  scope: 'browser',
  scopeWord: 'this browser',
  read: (s) => s.sessionsPerPage,
  toDraft: (value) => String(value),
  equivalent: (draft, server) => {
    const parsed = parsePerPage(draft);
    return parsed.ok && parsed.value === server;
  },
  parse: parsePerPage,
  commit: (value) => [{ type: 'SAVE_PREFS', patch: { sessionsPerPage: value } }],
};

const FILTER_TERM: BrowserField<string, string> = {
  kind: 'browser',
  id: 'prefs.filterText',
  label: 'Remembered filter term',
  path: null,
  section: 'sessions',
  scope: 'browser',
  scopeWord: 'this browser',
  read: (s) => s.filterText,
  toDraft: (value) => value,
  equivalent: (draft, server) => draft === server,
  parse: (draft) => ({ ok: true, value: draft }),
  commit: (value) => [{ type: 'SET_FILTER', text: value }],
};

const RESET_TABLE_SORT: StagedAction = {
  kind: 'stagedAction',
  id: 'restore.tableSort',
  label: 'Table column sorting',
  path: null,
  section: 'restore',
  scope: 'browser',
  scopeWord: 'this browser',
  commit: () => [
    { type: 'SET_TABLE_SORT', table: 'sessions', override: null },
    { type: 'CLEAR_TABLE_SORTS' },
  ],
};

const RESET_CARD_ORDER: StagedAction = {
  kind: 'stagedAction',
  id: 'restore.cardOrder',
  label: 'Card order',
  path: null,
  section: 'restore',
  scope: 'browser',
  scopeWord: 'this browser',
  commit: () => [{ type: 'RESET_PANEL_ORDER' }],
};

export const REGISTRY = [
  DISPLAY_TZ,
  ALERTS_NOTIFIER,
  boolField({
    id: 'alerts.enabled',
    label: 'Enable threshold alerts',
    path: 'alerts.enabled',
    section: 'alerts',
    read: (s) => s.alerts.enabled,
  }),
  boolField({
    id: 'alerts.projected_enabled',
    label: 'Projected weekly-% pace alerts',
    path: 'alerts.projected_enabled',
    section: 'alerts',
    read: (s) => s.alerts.projected_weekly_enabled ?? false,
  }),
  BUDGET_WEEKLY_USD,
  boolField({
    id: 'budget.projected_enabled',
    label: 'Projected budget-$ pace alerts',
    path: 'budget.projected_enabled',
    section: 'alerts',
    read: (s) => s.alerts.projected_budget_enabled ?? false,
  }),
  boolField({
    id: 'budget.project_alerts_enabled',
    label: 'Per-project budget alerts',
    path: 'budget.project_alerts_enabled',
    section: 'alerts',
    read: (s) => s.alerts.project_alerts_enabled ?? false,
  }),
  boolField({
    id: 'budget.codex.alerts_enabled',
    label: 'Codex budget alerts',
    path: 'budget.codex.alerts_enabled',
    section: 'alerts',
    read: (s) => s.alerts.codex_budget_alerts_enabled ?? false,
  }),
  boolField({
    id: 'budget.codex.projected_enabled',
    label: 'Codex projected-pace alerts',
    path: 'budget.codex.projected_enabled',
    section: 'alerts',
    read: (s) => s.alerts.codex_projected_enabled ?? false,
  }),
  boolField({
    id: 'dashboard.cache_failure_markers',
    label: 'Show cache-failure markers',
    path: 'dashboard.cache_failure_markers',
    section: 'viewer',
    read: (s) => s.markersEnabled,
  }),
  boolField({
    id: 'dashboard.live_tail',
    label: 'Live-tail new turns',
    path: 'dashboard.live_tail',
    section: 'viewer',
    read: (s) => s.liveTail,
  }),
  boolField({
    id: 'dashboard.lan_auth',
    label: 'Require LAN access token',
    path: 'dashboard.lan_auth',
    section: 'access',
    read: (s) => s.lanAuth,
  }),
  UPDATE_CHANNEL,
  SORT_DEFAULT,
  SESSIONS_PER_PAGE,
  FILTER_TERM,
  RESET_TABLE_SORT,
  RESET_CARD_ORDER,
] as const satisfies readonly SettingsField[];

export type RegistryEntry = (typeof REGISTRY)[number];

const BY_ID = new Map<string, RegistryEntry>(REGISTRY.map((field) => [field.id, field]));

export function fieldById(id: string): RegistryEntry | undefined {
  return BY_ID.get(id);
}
