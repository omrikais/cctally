// #513 S2 §1.2 — the Settings form reducer.
//
// State is `{draft, server}` per field plus a `staged` flag per staged action.
// Three operations act on it and nothing else writes it:
//
//   OPEN       server = read(sources), draft = toDraft(server), staged = false
//   RECONCILE  adopt the new server value into `draft` ONLY when the draft is
//              still equivalent to the server value captured BEFORE the
//              overwrite, then always advance `server`
//   SET_DRAFT  replace one draft
//
// The old baseline had to be captured before the overwrite for the same reason
// the pre-registry `reconcile()` helper did: overwriting first would make an
// untouched field compare against the NEW value and never adopt.
//
// The functions here are pure so the contract can be tested without a
// component; `useSettingsForm` is the thin React binding over them.
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Action } from '../../store/store';
import {
  REGISTRY,
  fieldById,
  type BrowserField,
  type SectionId,
  type ServerField,
  type SettingsSources,
} from './registry';

export interface FormState {
  draft: Record<string, unknown>;
  server: Record<string, unknown>;
  staged: Record<string, boolean>;
  sources: SettingsSources;
}

type AnyServerField = ServerField<unknown, unknown>;
type AnyBrowserField = BrowserField<unknown, unknown>;
type ValueField = AnyServerField | AnyBrowserField;

function valueFields(): ValueField[] {
  return REGISTRY.filter(
    (field) => field.kind === 'server' || field.kind === 'browser',
  ) as unknown as ValueField[];
}

export function openForm(sources: SettingsSources): FormState {
  const draft: Record<string, unknown> = {};
  const server: Record<string, unknown> = {};
  for (const field of valueFields()) {
    const value = field.read(sources);
    server[field.id] = value;
    draft[field.id] = field.toDraft(value);
  }
  const staged: Record<string, boolean> = {};
  for (const field of REGISTRY) {
    // A staged action resets to false on every open and never carries a server
    // value — inventing one would make "the server says you staged a reset"
    // expressible, which it is not.
    if (field.kind === 'stagedAction') staged[field.id] = false;
  }
  return { draft, server, staged, sources };
}

export function reconcileForm(state: FormState, sources: SettingsSources): FormState {
  const draft = { ...state.draft };
  const server = { ...state.server };
  for (const field of valueFields()) {
    const previous = server[field.id];
    const next = field.read(sources);
    if (field.equivalent(draft[field.id], previous)) {
      draft[field.id] = field.toDraft(next);
    }
    server[field.id] = next;
  }
  return { ...state, draft, server, sources };
}

export function setDraft(state: FormState, id: string, value: unknown): FormState {
  return { ...state, draft: { ...state.draft, [id]: value } };
}

export function toggleStaged(state: FormState, id: string): FormState {
  return { ...state, staged: { ...state.staged, [id]: !state.staged[id] } };
}

export function setStaged(state: FormState, id: string, value: boolean): FormState {
  return { ...state, staged: { ...state.staged, [id]: value } };
}

export function isDirty(state: FormState, id: string): boolean {
  const field = fieldById(id);
  if (!field) return false;
  if (field.kind === 'stagedAction') return state.staged[id] === true;
  const typed = field as unknown as ValueField;
  return !typed.equivalent(state.draft[id], state.server[id]);
}

export function dirtyIds(state: FormState): string[] {
  return REGISTRY.filter((field) => isDirty(state, field.id)).map((field) => field.id);
}

export function dirtyCount(state: FormState): number {
  return dirtyIds(state).length;
}

export function sectionDirty(state: FormState, section: SectionId): boolean {
  return REGISTRY.some(
    (field) => field.section === section && isDirty(state, field.id),
  );
}

// Per-field validation messages, computed on demand. The form decides WHEN a
// message becomes visible (§3.3: on blur or on a Save attempt, never from live
// typing); this only says what the message would be.
export function fieldIssues(state: FormState): Record<string, string> {
  const issues: Record<string, string> = {};
  for (const field of valueFields()) {
    const parsed = field.parse(state.draft[field.id]);
    if (!parsed.ok) issues[field.id] = parsed.message;
  }
  return issues;
}

export function isValid(state: FormState): boolean {
  return Object.keys(fieldIssues(state)).length === 0;
}

function deepSet(target: Record<string, unknown>, path: string, value: unknown): void {
  const segments = path.split('.');
  let cursor = target;
  for (const segment of segments.slice(0, -1)) {
    const existing = cursor[segment];
    if (typeof existing !== 'object' || existing === null) {
      cursor[segment] = {};
    }
    cursor = cursor[segment] as Record<string, unknown>;
  }
  cursor[segments[segments.length - 1]] = value;
}

// The POST body: one deep-set per dirty server field. Sparse by construction,
// so an untouched leaf is never sent and the server's nested partial merge
// never clobbers a sibling it was not asked about.
export function buildBody(state: FormState): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const field of REGISTRY) {
    if (field.kind !== 'server') continue;
    const typed = field as unknown as AnyServerField;
    if (!isDirty(state, typed.id)) continue;
    if (typed.suppressed?.(state.sources)) continue;
    const parsed = typed.parse(state.draft[typed.id]);
    if (!parsed.ok) continue;
    deepSet(body, typed.path, parsed.value);
  }
  return body;
}

// The local dispatches, each gated by its own field's dirty flag. Saving one
// field must never reset an untouched preference, which is exactly what an
// ungated `SAVE_PREFS` used to do.
export function localActions(state: FormState): Action[] {
  const actions: Action[] = [];
  for (const field of REGISTRY) {
    if (!isDirty(state, field.id)) continue;
    if (field.kind === 'browser') {
      const typed = field as unknown as AnyBrowserField;
      const parsed = typed.parse(state.draft[typed.id]);
      if (!parsed.ok) continue;
      actions.push(...typed.commit(parsed.value));
    } else if (field.kind === 'stagedAction') {
      actions.push(...field.commit());
    }
  }
  return actions;
}

export interface SettingsForm {
  state: FormState;
  drafts: Record<string, unknown>;
  dirtyIds: string[];
  dirtyCount: number;
  issues: Record<string, string>;
  valid: boolean;
  sectionDirty(section: SectionId): boolean;
  isDirty(id: string): boolean;
  draftOf<T>(id: string): T;
  staged(id: string): boolean;
  setDraft(id: string, value: unknown): void;
  toggleStaged(id: string): void;
  open(): void;
  buildBody(): Record<string, unknown>;
  localActions(): Action[];
}

// The React binding. `open` is edge-triggered by the caller so the overlay
// re-seeds every field exactly once per open — a discarded edit must not
// survive a Cancel and reopen, and the per-field reconcile only fires when the
// SERVER value changes.
export function useSettingsForm(sources: SettingsSources, open: boolean): SettingsForm {
  const [state, setState] = useState<FormState>(() => openForm(sources));
  const wasOpen = useRef(false);
  const latestSources = useRef(sources);
  latestSources.current = sources;

  useEffect(() => {
    if (open && !wasOpen.current) {
      setState(openForm(latestSources.current));
    } else if (open) {
      setState((previous) => reconcileForm(previous, latestSources.current));
    }
    wasOpen.current = open;
    // `sources` is rebuilt on every render, so depending on the object itself
    // would reconcile on every render. Depend on the values it carries.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    sources.tz,
    sources.tzPinned,
    sources.alerts,
    sources.markersEnabled,
    sources.liveTail,
    sources.lanAuth,
    sources.channel,
    sources.sortDefault,
    sources.sessionsPerPage,
    sources.filterText,
  ]);

  // Keep the sources snapshot on the state fresh even between reconciles, so
  // `buildBody`'s `--tz` suppression reads the current pin rather than the pin
  // as it stood at the last server tick.
  const current: FormState = { ...state, sources };

  const doSetDraft = useCallback((id: string, value: unknown) => {
    setState((previous) => setDraft(previous, id, value));
  }, []);
  const doToggleStaged = useCallback((id: string) => {
    setState((previous) => toggleStaged(previous, id));
  }, []);
  const doOpen = useCallback(() => {
    setState(openForm(latestSources.current));
  }, []);

  const issues = fieldIssues(current);
  return {
    state: current,
    drafts: current.draft,
    dirtyIds: dirtyIds(current),
    dirtyCount: dirtyCount(current),
    issues,
    valid: Object.keys(issues).length === 0,
    sectionDirty: (section) => sectionDirty(current, section),
    isDirty: (id) => isDirty(current, id),
    draftOf: <T,>(id: string) => current.draft[id] as T,
    staged: (id) => current.staged[id] === true,
    setDraft: doSetDraft,
    toggleStaged: doToggleStaged,
    open: doOpen,
    buildBody: () => buildBody(current),
    localActions: () => localActions(current),
  };
}
