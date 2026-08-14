import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import {
  dispatch,
  getState,
  defaultPrefs,
  selectMarkersEnabled,
  selectLiveTailEnabled,
  selectLanAuthEnabled,
  selectConfiguredChannel,
  subscribeStore,
  SESSION_SORT_KEYS,
  type SessionSortKey,
  type UpdateChannel,
} from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { useScrollLock } from '../hooks/useScrollLock';
import type {
  AlertAxis,
  AlertsSettingsEnvelope,
  ProjectedMetric,
} from '../types/envelope';
import { AXIS_TITLE_LABEL } from '../lib/alertAxis';
import { DEFAULT_PANEL_ORDER } from '../lib/panelIds';
import { ModalHeader } from '../modals/ModalHeader';
import {
  REGISTRY,
  fieldById,
  isValidIANA,
  type SettingsSources,
  type TzDraft,
} from './settings/registry';
import {
  SECTION_IDS,
  SECTION_TITLES,
  type SectionId,
} from './settings/registry';
import {
  classifyIgnored,
  resolveIssueTarget,
  type IssueTarget,
} from './settings/issues';
import {
  SETTINGS_MANIFEST,
  type ManifestEntry,
} from './settings/manifest';
import { SettingsRail, useActiveSection } from './settings/SettingsRail';
import { useSettingsForm } from './settings/useSettingsForm';

// Notifier dispatch backends (Phase B). The union mirrors
// `AlertsSettingsEnvelope.notifier` (single source of truth) so the dropdown
// can't drift from the wire contract. `NonNullable` strips the `?` so the
// registry's `read` can default to 'auto'.
type NotifierKind = NonNullable<AlertsSettingsEnvelope['notifier']>;

// Projected-axis metric sub-select labels (issue #121). The projected test
// alert mirrors the CLI's `--metric {weekly_pct,budget_usd}`: a single
// "Projected" axis option can't say WHICH projection to fire, so when that
// axis is picked we surface this secondary chooser and post `metric` too.
const PROJECTED_METRIC_LABEL: Record<ProjectedMetric, string> = {
  weekly_pct: 'Weekly %',
  budget_usd: 'Budget $',
  codex_budget_usd: 'Codex $',
};

// "America/New_York (GMT-04:00)" — preview of how the offset will read
// for a Custom-zone candidate, derived locally so the preview updates
// before Save round-trips through POST /api/settings + SSE rebroadcast.
// Offset comes from Intl's `shortOffset` partspec (returns "GMT-04:00"
// or similar). Falls back to the bare zone name on Intl errors.
function previewOffset(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, timeZoneName: 'shortOffset',
    }).formatToParts(new Date());
    const off = parts.find((p) => p.type === 'timeZoneName')?.value ?? '';
    return `${tz} (${off})`;
  } catch {
    return tz;
  }
}

// §2.3 — every row's searchable text. The filter matches label, help text,
// key path and scope word, so a user who knows the dotted key can find the row
// without knowing what we called it, and a user who wants "everything on this
// browser" can type that.
const EXTRA_ROW_HELP: Record<string, string> = {
  'display.tz': 'timezone zone clock IANA local utc offset',
  'alerts.notifier': 'osascript notify-send popup dispatch backend',
  'alerts.enabled': 'threshold master weekly 5h',
  'alerts.projected_enabled': 'pace projection weekly percent',
  'budget.weekly_usd': 'amount equivalent dollars spend cap',
  'budget.projected_enabled': 'pace projection budget dollars',
  'budget.project_alerts_enabled': 'per-project git root',
  'budget.codex.alerts_enabled': 'openai codex spend',
  'budget.codex.projected_enabled': 'openai codex pace projection',
  'dashboard.cache_failure_markers': 'conversation viewer cache rebuild marker',
  'dashboard.live_tail': 'conversation viewer follow new turns',
  'dashboard.lan_auth': 'token network security bind',
  'update.channel': 'stable beta release',
  'prefs.sortDefault': 'recent sessions order column',
  'prefs.sessionsPerPage': 'recent sessions paging rows',
  'prefs.filterText': 'recent sessions remembered search',
  'restore.tableSort': 'reset column click sorting',
  'restore.cardOrder': 'reset panel arrangement layout',
};

// Rows that are neither a registry field nor a manifest key: the test action
// and the view-preference restore. They are still filterable and still belong
// to a section.
const EXTRA_ROWS: readonly { key: string; section: SectionId; index: string }[] = [
  {
    key: 'action.test',
    section: 'alerts',
    index:
      'Send test alert synthetic dispatch pipeline toast log this machine',
  },
  {
    key: 'restore.viewPrefs',
    section: 'restore',
    index:
      'Restore view preferences sort default sessions per page remembered filter this browser',
  },
];

function manifestIndex(entry: ManifestEntry): string {
  return [entry.label, entry.key, entry.reason, entry.command, 'this machine'].join(' ');
}

// Focus `element` and report whether focus actually moved there. Being mounted
// is not the same as being focusable: a disabled control, an `inert` subtree and
// a `display: none` ancestor all swallow `.focus()` silently. Reporting success
// for one of those suppressed the summary fallback and left focus wherever it
// already was — or on `<body>`.
function focusMoved(element: HTMLElement): boolean {
  element.focus();
  return element.ownerDocument.activeElement === element;
}

// Move focus to whatever an issue names. Returns false when the target is not
// mounted OR could not take focus — the first is the case the DOM-removing
// filter creates and protocol 2 of §2.3 exists for, and the second is what the
// summary fallback exists for.
function focusTargetIn(card: HTMLElement | null, target: IssueTarget): boolean {
  if (!card) return false;
  if (target.kind === 'leaf') {
    const control = card.querySelector<HTMLElement>(
      `[data-settings-field="${target.id}"]`,
    );
    return control ? focusMoved(control) : false;
  }
  if (target.kind === 'group') {
    const heading = card.querySelector<HTMLElement>(
      `[data-settings-section="${target.section}"]`,
    );
    if (heading && focusMoved(heading)) return true;
    // No heading to land on — the first ENABLED control the section owns.
    for (const field of REGISTRY) {
      if (field.section !== target.section) continue;
      const control = card.querySelector<HTMLElement>(
        `[data-settings-field="${field.id}"]:not(:disabled)`,
      );
      if (control && focusMoved(control)) return true;
    }
    return false;
  }
  return false;
}

// One surfaced problem. `key` is stable per target so re-surfacing the same
// problem replaces rather than duplicates it.
interface SurfacedIssue {
  key: string;
  message: string;
  target: IssueTarget;
}

// `s` opens; Save / Reset / Cancel buttons close; backdrop click or Esc
// also close.

export function SettingsOverlay() {
  const [open, setOpen] = useState(false);
  const prefs = useSyncExternalStore(subscribeStore, () => getState().prefs);
  const filterTerm = useSyncExternalStore(subscribeStore, () => getState().filterText);
  const display = useDisplayTz();
  const reducedMotion = useReducedMotion();
  const alertsConfig = useSyncExternalStore(
    subscribeStore,
    () => getState().alertsConfig,
  );
  const markersEnabledServer = useSyncExternalStore(subscribeStore, () =>
    selectMarkersEnabled(getState()),
  );
  const liveTailServer = useSyncExternalStore(subscribeStore, () =>
    selectLiveTailEnabled(getState()),
  );
  const lanAuthServer = useSyncExternalStore(subscribeStore, () =>
    selectLanAuthEnabled(getState()),
  );
  const channelServer = useSyncExternalStore(subscribeStore, () =>
    selectConfiguredChannel(getState()),
  );
  // §5.6 — `store/update.ts` already coerces the install method to
  // `brew | npm | unknown`, so the Homebrew note is conditional on a POSITIVE
  // brew and stays silent on `unknown`. Telling an unknown install that it
  // always tracks stable would be a guess presented as a fact.
  const installMethod = useSyncExternalStore(
    subscribeStore,
    () => getState().update.state?.method ?? 'unknown',
  );

  // #513 S2 §1 — every persisted field now lives in the registry, and this is
  // the single structure the registry reads. The ten hand-coordinated per-field
  // edit sites (a useState, a lastSeen ref, a reconcile effect, a dirty
  // derivation, an aggregation and a save() branch each) are gone, and with
  // them the `tzTargetRef`-assigned-during-render hazard.
  const sources: SettingsSources = {
    tz: display.tz,
    tzPinned: display.pinned,
    alerts: alertsConfig,
    markersEnabled: markersEnabledServer,
    liveTail: liveTailServer,
    lanAuth: lanAuthServer,
    channel: channelServer,
    sortDefault: prefs.sortDefault,
    sessionsPerPage: prefs.sessionsPerPage,
    filterText: filterTerm,
  };
  const form = useSettingsForm(sources, open);

  // Ephemeral UI state (§1.1) — neither persisted nor staged, explicitly
  // outside the registry. It never dirties, never counts, and never reaches a
  // POST body.
  const [submitting, setSubmitting] = useState(false);
  // Surfaced issues (§3.3). Populated on blur or on a Save attempt — never
  // from live typing — and by a routed server rejection. An entry clears when
  // its field changes or validates.
  const [issues, setIssues] = useState<SurfacedIssue[]>([]);
  // A polite notice for a path the endpoint accepted and deliberately did not
  // persist. The save succeeded; this only says what happened to it.
  const [notice, setNotice] = useState<string | null>(null);
  // Whole seconds a submit has been in flight. Counted from ticks rather than
  // from a wall-clock delta so the behaviour is the same under fake timers.
  const [elapsed, setElapsed] = useState(0);
  const [lanAuthRestartSaved, setLanAuthRestartSaved] = useState(false);
  const [testSubmitting, setTestSubmitting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  // #207 D4: inline success confirmation for the happy path (dispatch ===
  // 'queued'), so the only feedback isn't the auto-dismissing alert toast.
  const [testOk, setTestOk] = useState(false);
  const [testAxis, setTestAxis] = useState<AlertAxis>('weekly');
  // Only consulted when testAxis === 'projected' (mirrors the CLI's
  // `alerts test --axis projected --metric`); ignored for other axes.
  const [testMetric, setTestMetric] = useState<ProjectedMetric>('weekly_pct');
  // S6 dismiss guard: an accidental Esc/backdrop-click while dirty raises this
  // contained confirm instead of discarding. Explicit ×/Cancel still discard.
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  // §2.2 — the filter over the one scroller. It REMOVES non-matching rows and
  // emptied sections from the DOM rather than hiding them, so a screen reader
  // and the tab order see the same list a sighted user does.
  const [query, setQuery] = useState('');
  // Set when an error-summary entry asks for a filtered-away control: clear the
  // filter, wait for the remount, then focus (§2.3 protocol 2). Focusing a node
  // that is not mounted is exactly the failure this exists to avoid.
  const [pendingFocus, setPendingFocus] = useState<IssueTarget | null>(null);
  const [barHeight, setBarHeight] = useState(0);

  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      // The registry re-seeds every persisted field itself; only the ephemeral
      // state is reset here.
      setTestError(null);
      setTestOk(false);
      setTestAxis('weekly');
      setLanAuthRestartSaved(false);
      setConfirmDiscard(false);
      setIssues([]);
      setNotice(null);
      setQuery('');
    }
    wasOpen.current = open;
  }, [open]);

  // §3.6 — nothing extra below three seconds. Past it the status region names
  // the elapsed wall time. There is no cancel and no AbortController timeout:
  // the configuration write completes before the rebuild begins, so aborting
  // the request could not undo it.
  useEffect(() => {
    if (!submitting) {
      setElapsed(0);
      return;
    }
    const timer = setInterval(() => setElapsed((seconds) => seconds + 1), 1000);
    return () => clearInterval(timer);
  }, [submitting]);

  useKeymap([
    // Parity with main's settings.js#152: don't stack Settings under an
    // open modal. Without this guard, pressing `s` over a modal opens
    // Settings hidden behind it and only becomes visible after the user
    // Escapes out of the front dialog.
    {
      key: 's',
      scope: 'global',
      view: 'any',     // all-views chrome (#156)
      action: () => setOpen(true),
      when: () => !getState().openModal,
    },
    // Esc at `modal` scope (z-index 100): SCOPE_ORDER beats the conversations
    // `global` Esc deterministically (#156). Routed through the S6 dismiss
    // guard: while dirty it raises the discard-confirm instead of closing (and
    // over the confirm it "keeps editing"). The closure is deferred, so it
    // safely references `requestClose` defined later in render.
    { key: 'Escape', scope: 'modal', action: () => requestClose(), when: () => open },
    // While Settings is open, swallow the digit modal-openers so they don't
    // mount a dashboard modal on top of the overlay. `0` (the 10th-panel
    // opener) MUST be swallowed too (#156): otherwise it opens the alerts
    // modal over Settings, and the modal-scope Esc tie strands it.
    { key: '0', scope: 'modal', action: () => {}, when: () => open },
    { key: '1', scope: 'modal', action: () => {}, when: () => open },
    { key: '2', scope: 'modal', action: () => {}, when: () => open },
    { key: '3', scope: 'modal', action: () => {}, when: () => open },
    { key: '4', scope: 'modal', action: () => {}, when: () => open },
    { key: '5', scope: 'modal', action: () => {}, when: () => open },
    { key: '6', scope: 'modal', action: () => {}, when: () => open },
    { key: '7', scope: 'modal', action: () => {}, when: () => open },
    { key: '8', scope: 'modal', action: () => {}, when: () => open },
    { key: '9', scope: 'modal', action: () => {}, when: () => open },
  ]);

  // a11y focus management (#207 A1). Settings is a local-state surface; it is
  // mutually exclusive with a panel modal (the `s` keybinding is guarded by
  // `!openModal`), so `trapEnabled` defaults to true and the contains-guard in
  // `useModalFocus` handles any Help-over-Settings case. Called BEFORE the
  // `!open` early-return so the hook order stays stable (Rules of Hooks).
  const cardRef = useRef<HTMLDivElement>(null);
  useModalFocus(cardRef, { active: open });

  // S6 (#252) dismiss guard: land focus on the safe default ("Keep editing")
  // when the confirm opens. Declared BEFORE the `!open` early-return.
  const keepEditingRef = useRef<HTMLButtonElement>(null);
  // §3.3 — the error summary is permanently mounted so assistive technology
  // always has an anchor to return to, and so focus can land on it for a
  // whole-form rejection that names no control.
  const summaryRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const actionsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (confirmDiscard) keepEditingRef.current?.focus();
  }, [confirmDiscard]);

  // S6 (#252) focus containment: while the discard-confirm is up, mark the
  // header + scrolling body `inert` so Tab/pointer can't reach the underlying
  // Settings controls — the confirm renders as their SIBLING inside
  // `.modal-card`, so the existing card-level focus trap then cycles only the
  // two confirm buttons (no separate trap needed). We set the DOM `inert`
  // property imperatively (typed on HTMLElement in lib.dom) rather than as a
  // JSX prop, because React 18's stable HTMLAttributes types lack `inert`, and
  // because wrapping header+body in a container div would break the
  // `.modal-card` flex column that gives `.modal-body` its scroll context.
  useEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    // Every child EXCEPT the confirm itself. The card's own children are the
    // header, the chrome, `.settings-main`, the feedback regions and the action
    // bar, so marking only the header and body would leave Tab able to reach
    // Save from behind the confirm. The rail and the scroller are not children
    // of the card — they sit inside `.settings-main` — which is why the
    // scroller is marked separately below.
    for (const child of Array.from(card.children)) {
      if (!(child instanceof HTMLElement)) continue;
      if (child.classList.contains('settings-confirm')) continue;
      child.inert = confirmDiscard;
    }
    const body = card.querySelector<HTMLElement>('.modal-body');
    if (body) body.inert = confirmDiscard;
  }, [confirmDiscard]);

  // §2.1 — the rail's anchor depends on how much of the scrollport a reader
  // can actually see, which the action bar decides. Measure it and recreate the
  // observer when it changes rather than assuming a constant.
  useEffect(() => {
    const bar = actionsRef.current;
    if (!bar || typeof ResizeObserver === 'undefined') return;
    const measure = () => setBarHeight(bar.getBoundingClientRect().height);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(bar);
    return () => observer.disconnect();
  }, [open]);

  // §2.2 — the searchable index for every row, and which rows survive the
  // current query. All draft state lives in the reducer above the JSX, so
  // unmounting a row changes neither reconcile behaviour nor the request body:
  // a dirty field the filter has hidden still counts and still saves.
  const rowIndex: { key: string; section: SectionId; index: string }[] = [
    ...REGISTRY.map((field) => ({
      key: field.id,
      section: field.section,
      index: [
        field.label,
        field.id,
        field.scopeWord,
        EXTRA_ROW_HELP[field.id] ?? '',
      ].join(' '),
    })),
    ...SETTINGS_MANIFEST.filter((entry) => entry.disposition !== 'editable').map(
      (entry) => ({
        key: `manifest:${entry.key}`,
        section: entry.section,
        index: manifestIndex(entry),
      }),
    ),
    ...EXTRA_ROWS,
  ];
  const query_ = query.trim().toLowerCase();
  const visibleRows = new Set(
    rowIndex
      .filter((row) => query_ === '' || row.index.toLowerCase().includes(query_))
      .map((row) => row.key),
  );
  const sectionHasRows = (id: SectionId) =>
    rowIndex.some((row) => row.section === id && visibleRows.has(row.key));
  const visibleSectionIds = SECTION_IDS.filter(sectionHasRows);
  const activeSection = useActiveSection(scrollerRef, visibleSectionIds, barHeight + 24);

  // §2.3 protocol 2 — reveal, await the remount, THEN focus. The focus trap
  // returns early when focus is outside the card and cannot recover focus that
  // has fallen to `<body>`, so focusing a node that is not mounted is not a
  // recoverable mistake.
  useEffect(() => {
    if (!pendingFocus) return;
    if (!focusTargetIn(cardRef.current, pendingFocus)) summaryRef.current?.focus();
    setPendingFocus(null);
  }, [pendingFocus]);

  // M1-1: lock background page scroll while Settings is open. Declared
  // BEFORE the `!open` early-return so the hook order stays stable.
  useScrollLock(open);

  // #207 D2: while Settings is open, the always-on hotkeys (digits, r/q/n/N,
  // c/S/B/f//) must be inert. Settings is component-local and invisible to the
  // store's modal fields, so it explicitly tracks itself via a depth counter.
  // Declared BEFORE the `!open` early-return so the hook order stays stable.
  useEffect(() => {
    if (!open) return;
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    return () => dispatch({ type: 'DECREMENT_CHROME_OVERLAY' });
  }, [open]);

  if (!open) return null;

  const tzDraft = form.draftOf<TzDraft>('display.tz');
  const tzCustomValid = tzDraft.mode !== 'custom' || isValidIANA(tzDraft.custom.trim());
  const setTzDraft = (patch: Partial<TzDraft>) =>
    form.setDraft('display.tz', { ...tzDraft, ...patch });

  const notifier = form.draftOf<NotifierKind>('alerts.notifier');
  // `commandConfigured` gates the "Custom command" option — when the server
  // has no `command_template`, picking 'command' would dispatch nothing, so
  // the option is disabled.
  const commandConfigured = alertsConfig.command_configured ?? false;

  const dirtyCount = form.dirtyCount;
  const issueFor = (id: string): string | undefined =>
    issues.find((issue) => issue.target.kind === 'leaf' && issue.target.id === id)?.message;
  // §3.3 — an issue clears the moment its field changes. Every control routes
  // its edit through here so no control can forget to.
  const editField = (id: string, value: unknown) => {
    form.setDraft(id, value);
    setIssues((previous) => previous.filter((issue) => issue.key !== id));
  };
  // §3.3 — a surfaced issue appears on blur, not from live typing, so a
  // half-typed zone name is not shouted at the user mid-keystroke.
  const blurField = (id: string) => {
    const message = form.issues[id];
    setIssues((previous) => {
      const rest = previous.filter((issue) => issue.key !== id);
      if (!message) return rest;
      const field = fieldById(id);
      return [
        ...rest,
        {
          key: id,
          message: `${field?.label ?? id}: ${message}`,
          target: { kind: 'leaf', id },
        },
      ];
    });
  };
  // §2.3 protocol 1 — before a subtree holding `document.activeElement` is
  // removed, move focus to the filter input. The card-level trap cannot
  // recover focus that has fallen to `<body>`.
  //
  // The container to test is the CARD, not the scroller. A control that unmounts
  // itself as a consequence of its own activation is the whole failure class,
  // and the scroller is only one of the places such a control lives: both
  // "Show all settings" buttons remove their own line by clearing the filter,
  // and the one in `.settings-chrome` sits outside the scroller entirely.
  const changeQuery = (next: string) => {
    const card = cardRef.current;
    const active = document.activeElement as HTMLElement | null;
    if (card && active && active !== filterRef.current && card.contains(active)) {
      filterRef.current?.focus();
    }
    setQuery(next);
  };
  const show = (key: string) => visibleRows.has(key);
  const focusIssue = (target: IssueTarget) => {
    if (focusTargetIn(cardRef.current, target)) return;
    if (target.kind === 'leaf' && !visibleRows.has(target.id)) {
      // The control exists in the form but the filter removed its row.
      changeQuery('');
      setPendingFocus(target);
      return;
    }
    if (target.kind === 'group' && !sectionHasRows(target.section)) {
      changeQuery('');
      setPendingFocus(target);
      return;
    }
    summaryRef.current?.focus();
  };
  const jumpToSection = (id: SectionId) => {
    const scroller = scrollerRef.current;
    const heading = scroller?.querySelector<HTMLElement>(
      `[data-settings-section="${id}"]`,
    );
    if (!scroller || !heading) return;

    // Native scrollIntoView walks every eligible ancestor. In Safari and
    // Chromium that included `.modal-card` despite its overflow:hidden, moving
    // the card's hidden scrollTop and permanently clipping the header, filter
    // and rail. Own the one intended scrollport instead, and preserve the
    // body's 16px content inset so the heading lands as content rather than
    // flush against the card border.
    const scrollerRect = scroller.getBoundingClientRect();
    const headingRect = heading.getBoundingClientRect();
    const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const desiredTop =
      scroller.scrollTop + headingRect.top - scrollerRect.top - 16;
    const top = Math.max(0, Math.min(desiredTop, maxScrollTop));
    if (typeof scroller.scrollTo === 'function') {
      scroller.scrollTo({ top, behavior: reducedMotion ? 'auto' : 'smooth' });
    } else {
      scroller.scrollTop = top;
    }
    // Keep the accessible focus handoff without letting the browser initiate a
    // second, ancestor-wide scroll of its own.
    heading.focus({ preventScroll: true });
  };

  // §3.2 — Save is disabled ONLY when nothing is dirty or a submit is in
  // flight. An invalid field no longer disables it: a greyed-out button that
  // does not say why is unreachable feedback, so the click is what surfaces
  // the reason and lands focus on it.
  const saveDisabled = dirtyCount === 0 || submitting;

  // Clear the discard-confirm on every close path (Cancel / × / Discard /
  // clean-Esc) so it can't paint for a frame on the next open before the
  // on-open effect resets it, and so the inert flag is never left stale (#252).
  // §3.6 — while a submit is in flight, no dismissal gesture unmounts the
  // overlay. `×` and Cancel used to unmount it while the fetch continued,
  // which stranded the result: the user would never learn whether the save
  // landed, and a routed rejection would have nowhere to paint.
  const close = () => {
    if (submitting) return;
    setConfirmDiscard(false);
    setLanAuthRestartSaved(false);
    setOpen(false);
  };

  const save = async () => {
    // §3.2 — a Save attempt validates EVERY field, surfaces what it finds, and
    // lands focus on the first invalid control in registry order. Save itself
    // stays clickable while a field is invalid: a disabled button with no
    // explanation is exactly the state the audit found unreachable.
    const parseIssues = form.issues;
    const invalid = REGISTRY.filter((field) => parseIssues[field.id] !== undefined);
    if (invalid.length > 0) {
      setNotice(null);
      setIssues(
        invalid.map((field) => ({
          key: field.id,
          message: `${field.label}: ${parseIssues[field.id]}`,
          target: { kind: 'leaf', id: field.id } as IssueTarget,
        })),
      );
      focusIssue({ kind: 'leaf', id: invalid[0].id });
      return;
    }
    setIssues([]);
    setNotice(null);

    // 1. If any server-persisted leaf is dirty, commit it via POST
    //    /api/settings BEFORE dispatching local prefs. The body is assembled
    //    by the registry: one deep-set per dirty leaf, so it stays sparse and
    //    the server's nested partial merge never touches a leaf we did not
    //    send. The `--tz` pin suppresses only the display leaf.
    const body = form.buildBody();
    if (Object.keys(body).length > 0) {
      setSubmitting(true);
      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const saved = (await res.json().catch(() => ({}))) as {
          error?: string;
          field?: string;
          ignored_fields?: string[];
          dashboard?: {
            cache_failure_markers?: boolean;
            live_tail?: boolean;
            lan_auth?: boolean;
          };
        };
        if (!res.ok) {
          // §3.4 — route the rejection at the element it names. `field` is not
          // always an editable leaf; `resolveIssueTarget` owns that.
          const target = resolveIssueTarget(saved.field ?? '$');
          const message = saved.error ?? `HTTP ${res.status}`;
          setIssues([{ key: saved.field ?? '$', message, target }]);
          setSubmitting(false);
          focusIssue(target);
          return;
        }
        // §3.5 — `ignored_fields` has two treatments. A path the client
        // declares accepted-then-discarded is expected and the save succeeds.
        // A path the client declared WRITABLE coming back ignored is a
        // contract mismatch between client and server, so the form stays open
        // and dirty and the mismatch enters the error summary.
        const classified = classifyIgnored(saved.ignored_fields ?? []);
        if (classified.mismatched.length > 0) {
          setIssues(
            classified.mismatched.map((path) => {
              const target = resolveIssueTarget(path);
              return {
                key: path,
                message:
                  `The server did not persist ${path}. This dashboard expected ` +
                  'to be able to write it, so the two disagree about the ' +
                  'settings contract. Set it from the CLI and report the ' +
                  'mismatch.',
                target,
              };
            }),
          );
          setSubmitting(false);
          focusIssue(resolveIssueTarget(classified.mismatched[0]));
          return;
        }
        if (classified.expected.length > 0) {
          setNotice(
            `Saved. ${classified.expected.join(', ')} ` +
            `${classified.expected.length === 1 ? 'is' : 'are'} accepted here but ` +
            'kept in the configuration file rather than written by the dashboard.',
          );
        }
        if (saved.dashboard) {
          dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: saved.dashboard });
        }
        // No optimistic UI: the F2 SSE broadcast arrives within ~100ms
        // and updates display.* / alertsConfig.* via the snapshot
        // store; the reducer re-seeds the form from the new server values.
      } catch (e) {
        const message = e instanceof Error ? e.message : 'unknown error';
        setIssues([{ key: '$', message, target: { kind: 'form' } }]);
        setSubmitting(false);
        focusIssue({ kind: 'form' });
        return;
      }
      setSubmitting(false);
    }

    // 2. Commit only the localStorage-backed prefs (and staged resets) that
    //    actually changed. Under the unified deferred model, an unrelated Save
    //    (e.g. only alerts.notifier) must NOT silently reset the user's
    //    sort/filter or wipe their Recent-Sessions column-click sort — so every
    //    local dispatch is gated on its own dirty/staged flag, which the
    //    registry now does by construction.
    for (const action of form.localActions()) dispatch(action);

    if (form.isDirty('dashboard.lan_auth')) {
      setLanAuthRestartSaved(true);
      return;
    }
    close();
  };

  // S6 (#252): the deferred "Restore view preferences" affordance. Sourced from
  // the store's canonical defaults (defaultPrefs) so the reset values can't
  // drift from the store; the remembered-filter default is the literal ''. This
  // only mutates the WORKING copy — the fields then show as changed and persist
  // via the normal Save path (no instant RESET_PREFS, no close()).
  //
  // It writes through `editField`, not through the reducer directly: §3.3's
  // rule is that an issue clears the moment its field changes, and a second
  // write path that skipped the chokepoint left a surfaced "between 10 and
  // 1000" on screen after the restore had already made the field valid.
  const restoreViewPrefs = () => {
    const d = defaultPrefs();
    editField('prefs.sortDefault', d.sortDefault);
    editField('prefs.sessionsPerPage', String(d.sessionsPerPage));
    editField('prefs.filterText', '');
  };
  const viewPrefDefaults = defaultPrefs();
  const perPageDraft = form.draftOf<string>('prefs.sessionsPerPage');
  const viewPrefsAtDefault =
    form.draftOf<SessionSortKey>('prefs.sortDefault') === viewPrefDefaults.sortDefault &&
    perPageDraft.trim() === String(viewPrefDefaults.sessionsPerPage) &&
    form.draftOf<string>('prefs.filterText') === '';
  // The "Table column sorting" reset is only meaningful when SOME table has a
  // column-click override — check all four (trend + sessions + projects +
  // history; the History Weekly/Monthly table is the fourth axis, S8 #254).
  // #556 S2 §5.3 — the History axis is now two independent overrides (weekly
  // and monthly expose different columns), and either one enables the reset.
  const tableSortHasOverride =
    !!prefs.trendSortOverride || !!prefs.sessionsSortOverride ||
    !!prefs.projectsSortOverride || !!prefs.historySortOverrides.week ||
    !!prefs.historySortOverrides.month;
  // "Card order" reset is only meaningful when the panel order differs from the
  // canonical default — gate the toggle the same way as the other two restores
  // so it can't stage a no-op reset (a phantom "1 change" + pointless
  // RESET_PANEL_ORDER on Save).
  const panelOrderIsDefault =
    prefs.panelOrder.length === DEFAULT_PANEL_ORDER.length &&
    prefs.panelOrder.every((id, i) => id === DEFAULT_PANEL_ORDER[i]);
  // S6 (#252) dismiss guard: Esc/backdrop route here. Over an open confirm,
  // treat the gesture as "keep editing" (dismiss the confirm). Otherwise raise
  // the confirm when dirty, or close outright when clean.
  //
  // The explicit ×/Cancel buttons bypass the guard and call `close()` directly,
  // which is the deliberate asymmetry: they discard without a confirm. They do
  // NOT bypass §3.6 — `close()` returns early while a submit is in flight, so
  // during a save all four gestures dismiss nothing, and both buttons are
  // disabled so the affordance says so.
  const requestClose = () => {
    if (submitting) return;
    if (confirmDiscard) {
      setConfirmDiscard(false);
      return;
    }
    if (dirtyCount > 0) {
      setConfirmDiscard(true);
      return;
    }
    close();
  };
  // S6 (#252) SET-1: decorative per-fieldset changed marker (aria-hidden); the
  // authoritative machine-readable signal is the Save badge count.
  const changedMark = (dirty: boolean) =>
    dirty ? (
      <span className="fs-changed" aria-hidden="true">
        {' '}
        ●
      </span>
    ) : null;
  const anyDirty = (...ids: string[]) => ids.some((id) => form.isDirty(id));
  const tzChanged = form.isDirty('display.tz');
  // #294 S5 Task 8 — three source-scoped alert groups (§6.7 Settings). The
  // notifier is global; the Claude group owns the threshold master + projected
  // weekly + the Claude-budget subgroup; the Codex group owns the mirrored
  // budget.codex.* toggles.
  const notifChanged = form.isDirty('alerts.notifier');
  // Every editable field the Claude fieldset RENDERS, the weekly budget
  // included. Leaving it out made the marker depend on which sibling happened
  // to be dirty: edit only the budget and the legend said nothing, dirty a
  // sibling and the dot appeared, revert that sibling and the dot left again
  // while the budget was still unsaved.
  const claudeChanged = anyDirty(
    'alerts.enabled',
    'alerts.projected_enabled',
    'budget.weekly_usd',
    'budget.projected_enabled',
    'budget.project_alerts_enabled',
  );
  const codexChanged = anyDirty(
    'budget.codex.alerts_enabled',
    'budget.codex.projected_enabled',
  );
  const viewerChanged = anyDirty(
    'dashboard.cache_failure_markers',
    'dashboard.live_tail',
  );
  const accessChanged = form.isDirty('dashboard.lan_auth');
  const channelDirty = form.isDirty('update.channel');
  const sortDirty = form.isDirty('prefs.sortDefault');
  const perPageDirty = form.isDirty('prefs.sessionsPerPage');
  const filterDirty = form.isDirty('prefs.filterText');
  const restoreChanged = anyDirty('restore.tableSort', 'restore.cardOrder');

  // §3.6 — phase-neutral copy. At three seconds the client CANNOT know the
  // configuration write has landed: the request may still be waiting on the
  // config lock before `save_config` runs, and the server sends no phase
  // signal. So the copy names elapsed wall time and says the dashboard is not
  // stuck; it never claims a phase or a percentage.
  const statusText = submitting
    ? elapsed >= 3
      ? `Still saving and refreshing… ${elapsed}s elapsed. A large history can ` +
        'make this take a while; the dashboard is not stuck.'
      : ''
    : (notice ??
      (dirtyCount === 0
        ? ''
        : `${dirtyCount} unsaved change${dirtyCount === 1 ? '' : 's'}.`));

  // §2.8 — every row exposes its scope to assistive technology and to the
  // filter index; VISIBLE scope text renders on per-browser rows only, and the
  // action bar states the default once. Marking only the minority visibly while
  // exposing it on all of them is what keeps the two halves consistent.
  const scopeNote = (scope: 'browser' | 'machine') =>
    scope === 'browser' ? (
      <span className="settings-scope"> (this browser)</span>
    ) : (
      <span className="sr-only"> Scope: this machine.</span>
    );
  // §2.5 / operator decision 5 — the key-path signature. The same treatment on
  // an editable row and on a CLI-only row, because it is the same fact: this is
  // the string you would type.
  const keyTag = (key: string) => <code className="settings-key">{key}</code>;
  const disclosureRow = (entry: ManifestEntry) => (
    <div className="settings-disclosure" key={entry.key}>
      <p className="settings-disclosure-label">
        {entry.label} {keyTag(entry.key)}
        {scopeNote('machine')}
      </p>
      <p className="settings-hint">
        {entry.reason}
        {entry.acceptedThenDiscarded
          ? ' The dashboard accepts this key on a save and deliberately does not store it.'
          : ''}
      </p>
      <p className="settings-hint">
        Default: <code>{entry.defaultText}</code>
      </p>
      <p className="settings-command">
        Run: <code>{entry.command}</code>
      </p>
    </div>
  );
  const disclosuresFor = (section: SectionId) =>
    SETTINGS_MANIFEST.filter(
      (entry) =>
        entry.section === section &&
        entry.disposition !== 'editable' &&
        show(`manifest:${entry.key}`),
    ).map(disclosureRow);
  // The `id` is what every `<section aria-labelledby="settings-h-…">` resolves
  // to. Without it none of the seven sections had an accessible name and none
  // was exposed as a landmark.
  const sectionHeading = (id: SectionId, dirty: boolean) => (
    <h3
      className="settings-section-title"
      id={`settings-h-${id}`}
      data-settings-section={id}
      tabIndex={-1}
    >
      {SECTION_TITLES[id]}
      {dirty && (
        <>
          <span className="fs-changed" aria-hidden="true"> ●</span>
          <span className="sr-only"> (has unsaved changes)</span>
        </>
      )}
    </h3>
  );
  const railSections = visibleSectionIds.map((id) => ({
    id,
    title: SECTION_TITLES[id],
    dirty: form.sectionDirty(id),
  }));
  // §2.3 — the change count counts every draft, including drafts the filter is
  // hiding, and a persistent line says how many and offers a way back.
  const hiddenDirty = form.dirtyIds.filter((id) => !visibleRows.has(id));
  const matchCount = visibleRows.size;
  const totalRows = rowIndex.length;
  const filterCountText =
    query_ === ''
      ? `${totalRows} settings`
      : `${matchCount} of ${totalRows} settings match “${query.trim()}”`;

  return (
    <div id="settings-root">
      {/* Backdrop click routes through the dismiss guard (confirm when dirty). */}
      <div className="modal-backdrop" onClick={requestClose} />
      <div
        ref={cardRef}
        className="modal-card accent-orange settings-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        /* The third focus-loss path (§2.3). A pointer press whose hit target
           cannot take focus — a disabled control, the gap between two rows —
           makes the browser focus the NEAREST FOCUSABLE ANCESTOR, and with no
           such ancestor it focuses `<body>`, which is outside the card where
           the trap returns early and cannot recover. Making the dialog itself
           programmatically focusable gives that walk somewhere to stop. It adds
           no tab stop: `tabindex="-1"` is out of the tab sequence and out of
           `useModalFocus`'s FOCUSABLE_SELECTOR. */
        tabIndex={-1}
      >
        {/* The header × discards directly (deliberate), so it stays wired to
            close(); the dismiss guard covers only Esc/backdrop. §3.6 makes it
            inoperable while a submit is in flight, and it now LOOKS that way. */}
        <ModalHeader
          title="Settings"
          titleId="settings-title"
          onClose={close}
          closeDisabled={submitting}
        />
        <div className="settings-chrome">
          <div className="settings-search" role="search">
            <label htmlFor="settings-filter">Find a setting</label>
            <input
              ref={filterRef}
              id="settings-filter"
              type="search"
              value={query}
              aria-controls="settings-scroller"
              aria-describedby="settings-filter-count"
              /* Names all three things the filter matches. "browser" is a real
                 substring of the scope word every per-browser row carries, so
                 the example still works when typed. The field is sized to hold
                 this string at both viewports rather than the string trimmed to
                 the field — see `.settings-search` in index.css. */
              placeholder="name, key path, or “browser”"
              onChange={(e) => changeQuery(e.target.value)}
            />
          </div>
          {/* §4.1 — the third live region: a polite result count of its own.
              `aria-live` rather than `role="status"`, because the form status
              already claims that role and two elements with it would leave a
              reader unable to tell which one just spoke. */}
          <p
            className="settings-filter-count"
            id="settings-filter-count"
            aria-live="polite"
            aria-atomic="true"
          >
            {filterCountText}
          </p>
          {hiddenDirty.length > 0 && (
            <p className="settings-hidden-dirty">
              {hiddenDirty.length === 1
                ? '1 unsaved change is hidden by this filter.'
                : `${hiddenDirty.length} unsaved changes are hidden by this filter.`}{' '}
              <button
                type="button"
                className="settings-link-btn"
                onClick={() => changeQuery('')}
              >
                Show all settings
              </button>
            </p>
          )}
        </div>
        <div className="settings-main">
          <SettingsRail
            sections={railSections}
            activeId={activeSection}
            onJump={jumpToSection}
          />
          <div className="modal-body" id="settings-scroller" ref={scrollerRef}>
            {visibleSectionIds.length === 0 && (
              <p className="settings-empty">
                No setting matches “{query.trim()}”. Try a shorter word, a dotted
                key path such as <code>budget.weekly_usd</code>, or{' '}
                <button
                  type="button"
                  className="settings-link-btn"
                  onClick={() => changeQuery('')}
                >
                  show all settings
                </button>
                .
              </p>
            )}

            {/* ---------------- Display &amp; time ---------------- */}
            {sectionHasRows('display') && (
              <section className="settings-section" aria-labelledby="settings-h-display">
                {sectionHeading('display', form.sectionDirty('display'))}
                {show('display.tz') && (
                  <fieldset className={`settings-fs${tzChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Display timezone {keyTag('display.tz')}
                      {scopeNote('machine')}
                      {changedMark(tzChanged)}
                    </legend>
                    {display.pinned && (
                      <small>
                        Pinned by --tz flag — restart the server without --tz to change here.
                      </small>
                    )}
                    <label>
                      <input
                        type="radio"
                        name="tz-mode"
                        value="local"
                        checked={tzDraft.mode === 'local'}
                        onChange={() => setTzDraft({ mode: 'local' })}
                        disabled={display.pinned}
                      />{' '}
                      Local ({display.resolvedTz})
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="tz-mode"
                        value="utc"
                        checked={tzDraft.mode === 'utc'}
                        onChange={() => setTzDraft({ mode: 'utc' })}
                        disabled={display.pinned}
                      />{' '}
                      UTC
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="tz-mode"
                        value="custom"
                        checked={tzDraft.mode === 'custom'}
                        onChange={() => setTzDraft({ mode: 'custom' })}
                        disabled={display.pinned}
                      />{' '}
                      Custom
                    </label>
                    {/* §4.3 — the custom-zone input used to sit INSIDE the label
                        that names the Custom radio, which made that radio's
                        accessible name read "Custom: America/New_York" and left
                        the input itself unnamed. */}
                    <label className="settings-row" htmlFor="settings-tz-custom">
                      Custom zone
                    </label>
                    {/* §3.2's "first invalid control" is THIS input and no
                        other: the three radios can never fail to parse, and
                        this is the element that carries `aria-invalid`. The
                        registry pointer therefore sits here rather than on the
                        Local radio, which under a `--tz` pin is disabled and
                        could not have taken focus anyway. */}
                    <input
                      id="settings-tz-custom"
                      type="text"
                      data-settings-field="display.tz"
                      value={tzDraft.custom}
                      onChange={(e) => setTzDraft({ custom: e.target.value })}
                      onBlur={() => blurField('display.tz')}
                      disabled={tzDraft.mode !== 'custom' || display.pinned}
                      placeholder="America/New_York"
                      aria-invalid={issueFor('display.tz') !== undefined}
                    />
                    {tzDraft.mode === 'custom' && tzDraft.custom.trim() && tzCustomValid && (
                      <small>resolves to: {previewOffset(tzDraft.custom.trim())}</small>
                    )}
                  </fieldset>
                )}
                {disclosuresFor('display')}
              </section>
            )}

            {/* ---------------- Recent Sessions ---------------- */}
            {sectionHasRows('sessions') && (
              <section className="settings-section" aria-labelledby="settings-h-sessions">
                {sectionHeading('sessions', form.sectionDirty('sessions'))}
                {show('prefs.sortDefault') && (
                  <fieldset className={`settings-fs${sortDirty ? ' is-changed' : ''}`}>
                    <legend>
                      Sort default{scopeNote('browser')}
                      {changedMark(sortDirty)}
                    </legend>
                    {SESSION_SORT_KEYS.map(({ key, label }) => (
                      <label key={key}>
                        <input
                          type="radio"
                          name="sort-default"
                          value={key}
                          data-settings-field={
                            key === SESSION_SORT_KEYS[0].key ? 'prefs.sortDefault' : undefined
                          }
                          checked={form.draftOf<SessionSortKey>('prefs.sortDefault') === key}
                          onChange={() => editField('prefs.sortDefault', key)}
                        />{' '}
                        {label}
                      </label>
                    ))}
                  </fieldset>
                )}
                {show('prefs.filterText') && (
                  <fieldset className={`settings-fs${filterDirty ? ' is-changed' : ''}`}>
                    <legend>
                      Remembered filter term{scopeNote('browser')}
                      {changedMark(filterDirty)}
                    </legend>
                    {/* §4.3 — named by a real label rather than by its
                        placeholder, which assistive technology need not read. */}
                    <label className="settings-row" htmlFor="settings-filter-term">
                      Remembered filter term
                    </label>
                    <input
                      id="settings-filter-term"
                      type="text"
                      placeholder="(none)"
                      data-settings-field="prefs.filterText"
                      value={form.draftOf<string>('prefs.filterText')}
                      onChange={(e) => editField('prefs.filterText', e.target.value)}
                    />
                  </fieldset>
                )}
                {show('prefs.sessionsPerPage') && (
                  <fieldset className={`settings-fs${perPageDirty ? ' is-changed' : ''}`}>
                    <legend>
                      Sessions per page{scopeNote('browser')}
                      {changedMark(perPageDirty)}
                    </legend>
                    <label className="settings-row" htmlFor="settings-per-page">
                      Sessions per page
                    </label>
                    {/* §3.7 — the draft is a STRING, parsed only at commit, so
                        `""` and a transiently out-of-range value survive
                        exactly as typed. */}
                    <input
                      id="settings-per-page"
                      type="number"
                      min={10}
                      max={1000}
                      data-settings-field="prefs.sessionsPerPage"
                      value={perPageDraft}
                      onChange={(e) => editField('prefs.sessionsPerPage', e.target.value)}
                      onBlur={() => blurField('prefs.sessionsPerPage')}
                      aria-invalid={issueFor('prefs.sessionsPerPage') !== undefined}
                    />
                  </fieldset>
                )}
                {disclosuresFor('sessions')}
              </section>
            )}

            {/* ---------------- Alerts ---------------- */}
            {sectionHasRows('alerts') && (
              <section className="settings-section" aria-labelledby="settings-h-alerts">
                {sectionHeading('alerts', form.sectionDirty('alerts'))}
                {show('alerts.notifier') && (
                  <fieldset className={`settings-fs${notifChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Notifications {keyTag('alerts.notifier')}
                      {scopeNote('machine')}
                      {changedMark(notifChanged)}
                    </legend>
                    {/* The "Custom command" option is disabled unless the server
                        reports a configured `command_template`: the raw template
                        never reaches the client, so the dashboard can SELECT the
                        command notifier but not author it. */}
                    <label className="settings-row">
                      Notifier{' '}
                      <select
                        className="settings-btn settings-select"
                        value={notifier}
                        aria-label="Alert notifier"
                        data-settings-field="alerts.notifier"
                        onChange={(e) =>
                          editField('alerts.notifier', e.target.value as NotifierKind)
                        }
                      >
                        <option value="auto">Auto-detect</option>
                        <option value="osascript">macOS (osascript)</option>
                        <option value="notify-send">Linux (notify-send)</option>
                        <option value="command" disabled={!commandConfigured}>
                          Custom command{commandConfigured ? '' : ' (set via CLI)'}
                        </option>
                        <option value="none">None (log only)</option>
                      </select>
                    </label>
                    {commandConfigured && (
                      <p className="settings-hint">Custom command configured (edit via CLI).</p>
                    )}
                    <p className="settings-hint">
                      The notifier backend applies to all alert dispatches (Claude and Codex).
                    </p>
                  </fieldset>
                )}
                {(show('alerts.enabled') ||
                  show('alerts.projected_enabled') ||
                  show('budget.weekly_usd') ||
                  show('budget.projected_enabled') ||
                  show('budget.project_alerts_enabled')) && (
                  <fieldset className={`settings-fs${claudeChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Claude alerts{scopeNote('machine')}
                      {changedMark(claudeChanged)}
                    </legend>
                    {show('alerts.enabled') && (
                      <>
                        <label>
                          <input
                            type="checkbox"
                            name="alerts-enabled"
                            data-settings-field="alerts.enabled"
                            checked={form.draftOf<boolean>('alerts.enabled')}
                            onChange={(e) => editField('alerts.enabled', e.target.checked)}
                          />{' '}
                          Enable threshold alerts {keyTag('alerts.enabled')}
                        </label>
                        {/* Read-only summary of the active threshold lists
                            (`budget.alert_thresholds` and its two siblings).
                            There is no editor in v1; the disclosure row below
                            names the command. */}
                        <p className="alerts-summary settings-hint">
                          Weekly: {alertsConfig.weekly_thresholds.map((t) => `${t}%`).join(', ')}
                          {' · '}
                          5h-block:{' '}
                          {alertsConfig.five_hour_thresholds.map((t) => `${t}%`).join(', ')}
                          {' · '}
                          Budget:{' '}
                          {(alertsConfig.budget_thresholds ?? [])
                            .map((t) => `${t}%`)
                            .join(', ') || '—'}
                        </p>
                      </>
                    )}
                    {show('alerts.projected_enabled') && (
                      <div className="settings-subgroup">
                        <label>
                          <input
                            type="checkbox"
                            name="projected-weekly-enabled"
                            data-settings-field="alerts.projected_enabled"
                            checked={form.draftOf<boolean>('alerts.projected_enabled')}
                            onChange={(e) =>
                              editField('alerts.projected_enabled', e.target.checked)
                            }
                          />{' '}
                          Projected weekly-% pace alerts {keyTag('alerts.projected_enabled')}
                          {scopeNote('machine')}
                        </label>
                        <p className="settings-hint">
                          Pace is where the week is heading if the last day&apos;s rate
                          continues to the reset.
                        </p>
                      </div>
                    )}
                    {(show('budget.weekly_usd') ||
                      show('budget.projected_enabled') ||
                      show('budget.project_alerts_enabled')) && (
                      <div
                        className="settings-subgroup"
                        role="group"
                        aria-label="Claude budget alerts"
                      >
                        <p className="settings-subgroup-label">Claude budget</p>
                        <p className="settings-hint">
                          Fire when a configured budget&apos;s pace or spend crosses a
                          threshold.
                        </p>
                        {show('budget.weekly_usd') && (
                          <>
                            {/* §5.2 — the one new editor. */}
                            <label className="settings-row" htmlFor="settings-weekly-budget">
                              Weekly budget (equivalent $) {keyTag('budget.weekly_usd')}
                              {scopeNote('machine')}
                            </label>
                            <input
                              id="settings-weekly-budget"
                              type="text"
                              inputMode="decimal"
                              data-settings-field="budget.weekly_usd"
                              value={form.draftOf<string>('budget.weekly_usd')}
                              onChange={(e) => editField('budget.weekly_usd', e.target.value)}
                              onBlur={() => blurField('budget.weekly_usd')}
                              placeholder="none"
                              aria-invalid={issueFor('budget.weekly_usd') !== undefined}
                              aria-describedby="settings-weekly-budget-help"
                            />
                            <p className="settings-hint" id="settings-weekly-budget-help">
                              Claude&apos;s figure is EQUIVALENT dollars — what the same
                              usage would have cost on the API, not a charge. Leave blank
                              for no budget.
                            </p>
                            {/* §5.4 — disclosure, never gating. Both toggles below
                                stay operable; the copy states why they cannot fire
                                yet and what to do about it. */}
                            {alertsConfig.weekly_usd === null && (
                              <p className="settings-hint">
                                No Claude budget is set, so the two budget alerts below
                                cannot fire yet. Enter an amount in the field above.
                              </p>
                            )}
                          </>
                        )}
                        {show('budget.projected_enabled') && (
                          <label>
                            <input
                              type="checkbox"
                              name="projected-budget-enabled"
                              data-settings-field="budget.projected_enabled"
                              checked={form.draftOf<boolean>('budget.projected_enabled')}
                              onChange={(e) =>
                                editField('budget.projected_enabled', e.target.checked)
                              }
                            />{' '}
                            Projected budget-$ pace alerts {keyTag('budget.projected_enabled')}
                            {scopeNote('machine')}
                          </label>
                        )}
                        {show('budget.project_alerts_enabled') && (
                          <label>
                            <input
                              type="checkbox"
                              name="project-alerts-enabled"
                              data-settings-field="budget.project_alerts_enabled"
                              checked={form.draftOf<boolean>('budget.project_alerts_enabled')}
                              onChange={(e) =>
                                editField('budget.project_alerts_enabled', e.target.checked)
                              }
                            />{' '}
                            Per-project budget alerts {keyTag('budget.project_alerts_enabled')}
                            {scopeNote('machine')}
                          </label>
                        )}
                      </div>
                    )}
                  </fieldset>
                )}
                {(show('budget.codex.alerts_enabled') ||
                  show('budget.codex.projected_enabled')) && (
                  <fieldset className={`settings-fs${codexChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Codex alerts{scopeNote('machine')}
                      {changedMark(codexChanged)}
                    </legend>
                    {/* §3.8 — the two `disabled` attributes below are a
                        CONVENIENCE. They keep a user from arming a toggle that
                        could not fire, and they are the reason the CLI pointer
                        is worth showing; they prevent nothing. The server is
                        the authority: `_handle_post_settings` fails closed on
                        any `budget.codex.*` leaf without a configured Codex
                        budget and answers 400 naming `budget.codex`, which §3.4
                        routes onto this group. A client that posted the leaf
                        anyway would be rejected there, not here. */}
                    <div
                      className="settings-subgroup"
                      role="group"
                      aria-label="Codex budget alerts"
                    >
                      <p className="settings-subgroup-label">Codex budget</p>
                      <p className="settings-hint">
                        Codex&apos;s figure is ACTUAL API charges, unlike Claude&apos;s
                        equivalent-dollar budget above.
                      </p>
                      {show('budget.codex.alerts_enabled') && (
                        <label>
                          <input
                            type="checkbox"
                            name="codex-budget-alerts-enabled"
                            data-settings-field="budget.codex.alerts_enabled"
                            checked={form.draftOf<boolean>('budget.codex.alerts_enabled')}
                            disabled={!alertsConfig.codex_budget_configured}
                            onChange={(e) =>
                              editField('budget.codex.alerts_enabled', e.target.checked)
                            }
                          />{' '}
                          Codex budget alerts {keyTag('budget.codex.alerts_enabled')}
                          {scopeNote('machine')}
                        </label>
                      )}
                      {show('budget.codex.projected_enabled') && (
                        <label>
                          <input
                            type="checkbox"
                            name="codex-projected-enabled"
                            data-settings-field="budget.codex.projected_enabled"
                            checked={form.draftOf<boolean>('budget.codex.projected_enabled')}
                            disabled={!alertsConfig.codex_budget_configured}
                            onChange={(e) =>
                              editField('budget.codex.projected_enabled', e.target.checked)
                            }
                          />{' '}
                          Codex projected-pace alerts {keyTag('budget.codex.projected_enabled')}
                          {scopeNote('machine')}
                        </label>
                      )}
                      {!alertsConfig.codex_budget_configured && (
                        <p className="settings-hint">
                          Set a Codex budget via the CLI first:{' '}
                          <code>cctally budget set 200 --vendor codex</code>
                        </p>
                      )}
                    </div>
                    <p className="settings-hint">
                      Codex quota-threshold alert rules are not configurable here — manage
                      them via the CLI (<code>cctally codex quota …</code>).
                    </p>
                  </fieldset>
                )}
                {show('action.test') && (
                  <fieldset className="settings-fs settings-fs-subordinate">
                    <legend>Send a test alert</legend>
                    <div className="alerts-test-row">
                      <label>
                        Alert type{' '}
                        <select
                          className="settings-btn settings-select"
                          value={testAxis}
                          disabled={testSubmitting}
                          aria-label="Alert type"
                          onChange={(e) => setTestAxis(e.target.value as AlertAxis)}
                        >
                          {(Object.keys(AXIS_TITLE_LABEL) as AlertAxis[]).map((ax) => (
                            <option key={ax} value={ax}>
                              {AXIS_TITLE_LABEL[ax]}
                            </option>
                          ))}
                        </select>
                      </label>{' '}
                      {testAxis === 'projected' && (
                        <label>
                          Metric{' '}
                          <select
                            className="settings-btn settings-select"
                            value={testMetric}
                            disabled={testSubmitting}
                            aria-label="Metric"
                            onChange={(e) => setTestMetric(e.target.value as ProjectedMetric)}
                          >
                            {(Object.keys(PROJECTED_METRIC_LABEL) as ProjectedMetric[]).map(
                              (m) => (
                                <option key={m} value={m}>
                                  {PROJECTED_METRIC_LABEL[m]}
                                </option>
                              ),
                            )}
                          </select>
                        </label>
                      )}{' '}
                      <button
                        className="settings-btn"
                        type="button"
                        disabled={testSubmitting}
                        onClick={async () => {
                          setTestSubmitting(true);
                          setTestError(null);
                          setTestOk(false);
                          try {
                            const res = await fetch('/api/alerts/test', {
                              method: 'POST',
                              headers: { 'Content-Type': 'application/json' },
                              body: JSON.stringify({
                                axis: testAxis,
                                threshold: 90,
                                // metric only matters for the projected axis; the
                                // endpoint ignores it elsewhere, but keep the wire
                                // minimal and send it only when it applies.
                                ...(testAxis === 'projected' ? { metric: testMetric } : {}),
                              }),
                            });
                            const body = (await res.json().catch(() => ({}))) as {
                              dispatch?: string;
                              alert?: import('../types/envelope').AlertEntry;
                              reason?: string;
                            };
                            // CLAUDE.md "Test alerts deliberately diverge from real
                            // alerts": the dashboard endpoint returns the payload
                            // directly to the caller so a toast renders even when
                            // osascript fails. Show the toast whenever a payload is
                            // present; show the error whenever dispatch is anything
                            // other than "queued". Both can surface at once.
                            if (body.alert) {
                              dispatch({ type: 'SHOW_ALERT_TOAST', alert: body.alert });
                            }
                            if (body.dispatch === 'queued') {
                              setTestOk(true);
                            } else {
                              setTestError(
                                body.dispatch ?? body.reason ?? `HTTP ${res.status}`,
                              );
                            }
                          } catch (e) {
                            setTestError(e instanceof Error ? e.message : 'unknown error');
                          }
                          setTestSubmitting(false);
                        }}
                      >
                        {testSubmitting ? 'Sending…' : 'Send test alert'}
                      </button>
                      <p className="settings-hint">
                        Sends a synthetic alert through the dispatch pipeline so you can
                        verify the toast and log path. It does not write to the database,
                        does not update the Recent alerts panel, and works whether or not
                        threshold alerts are enabled.
                      </p>
                    </div>
                  </fieldset>
                )}
                {disclosuresFor('alerts')}
              </section>
            )}

            {/* ---------------- Conversation viewer ---------------- */}
            {sectionHasRows('viewer') && (
              <section className="settings-section" aria-labelledby="settings-h-viewer">
                {sectionHeading('viewer', form.sectionDirty('viewer'))}
                {(show('dashboard.cache_failure_markers') || show('dashboard.live_tail')) && (
                  <fieldset className={`settings-fs${viewerChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Conversation viewer{scopeNote('machine')}
                      {changedMark(viewerChanged)}
                    </legend>
                    {show('dashboard.cache_failure_markers') && (
                      <>
                        <label>
                          <input
                            type="checkbox"
                            name="cache-failure-markers"
                            data-settings-field="dashboard.cache_failure_markers"
                            checked={form.draftOf<boolean>('dashboard.cache_failure_markers')}
                            onChange={(e) =>
                              editField('dashboard.cache_failure_markers', e.target.checked)
                            }
                          />{' '}
                          Show cache-failure markers {keyTag('dashboard.cache_failure_markers')}
                          {scopeNote('machine')}
                        </label>
                        <p className="settings-hint">
                          Marks assistant turns that re-created the bulk of their cached
                          prefix instead of reading it (a cost inefficiency, usually after
                          an idle gap past the cache TTL). On by default.
                        </p>
                      </>
                    )}
                    {show('dashboard.live_tail') && (
                      <>
                        <label>
                          <input
                            type="checkbox"
                            name="live-tail"
                            data-settings-field="dashboard.live_tail"
                            checked={form.draftOf<boolean>('dashboard.live_tail')}
                            onChange={(e) => editField('dashboard.live_tail', e.target.checked)}
                          />{' '}
                          Live-tail new turns {keyTag('dashboard.live_tail')}
                          {scopeNote('machine')}
                        </label>
                        <p className="settings-hint">
                          Fetch new turns the instant the session&apos;s file changes
                          (instead of waiting for the periodic refresh). On by default.
                        </p>
                      </>
                    )}
                  </fieldset>
                )}
                {disclosuresFor('viewer')}
              </section>
            )}

            {/* ---------------- Access &amp; updates ---------------- */}
            {sectionHasRows('access') && (
              <section className="settings-section" aria-labelledby="settings-h-access">
                {sectionHeading('access', form.sectionDirty('access'))}
                {show('dashboard.lan_auth') && (
                  <fieldset className={`settings-fs${accessChanged ? ' is-changed' : ''}`}>
                    <legend>
                      Dashboard access{scopeNote('machine')}
                      {changedMark(accessChanged)}
                    </legend>
                    <label>
                      <input
                        type="checkbox"
                        name="lan-auth"
                        data-settings-field="dashboard.lan_auth"
                        checked={form.draftOf<boolean>('dashboard.lan_auth')}
                        onChange={(e) => {
                          editField('dashboard.lan_auth', e.target.checked);
                          setLanAuthRestartSaved(false);
                        }}
                      />{' '}
                      Require LAN access token {keyTag('dashboard.lan_auth')}
                      {scopeNote('machine')}
                    </label>
                    <p className="settings-hint">
                      Protects API and live-update traffic on non-loopback binds. Changes
                      take effect only after restarting the dashboard; this running
                      dashboard keeps its current access mode.
                    </p>
                  </fieldset>
                )}
                {show('update.channel') && (
                  <fieldset className={`settings-fs${channelDirty ? ' is-changed' : ''}`}>
                    <legend>
                      Update channel {keyTag('update.channel')}
                      {scopeNote('machine')}
                      {changedMark(channelDirty)}
                    </legend>
                    <label>
                      <input
                        type="radio"
                        name="update-channel"
                        value="stable"
                        data-settings-field="update.channel"
                        checked={form.draftOf<UpdateChannel>('update.channel') === 'stable'}
                        onChange={() => editField('update.channel', 'stable')}
                      />{' '}
                      Stable
                    </label>
                    <label>
                      <input
                        type="radio"
                        name="update-channel"
                        value="beta"
                        checked={form.draftOf<UpdateChannel>('update.channel') === 'beta'}
                        onChange={() => editField('update.channel', 'beta')}
                      />{' '}
                      Beta
                    </label>
                    <p className="settings-hint">
                      Beta receives every release as it ships; stable only the
                      maintainer-promoted ones.
                      {installMethod === 'brew'
                        ? ' This install came from Homebrew, which always tracks stable.'
                        : ''}
                    </p>
                  </fieldset>
                )}
                {disclosuresFor('access')}
              </section>
            )}

            {/* ---------------- Restore defaults ---------------- */}
            {sectionHasRows('restore') && (
              <section className="settings-section" aria-labelledby="settings-h-restore">
                {sectionHeading('restore', form.sectionDirty('restore'))}
                <fieldset className={`settings-fs${restoreChanged ? ' is-changed' : ''}`}>
                  <legend>
                    Restore defaults{scopeNote('browser')}
                    {changedMark(restoreChanged)}
                  </legend>
                  {show('restore.viewPrefs') && (
                    <div className="settings-restore-row settings-restore-immediate">
                      <button
                        className="settings-btn"
                        type="button"
                        onClick={restoreViewPrefs}
                        disabled={viewPrefsAtDefault}
                      >
                        Restore view preferences
                      </button>
                      <p className="settings-hint">
                        Fills the three Recent Sessions fields with their defaults right
                        now; nothing is stored until you Save.
                      </p>
                    </div>
                  )}
                  {show('restore.tableSort') && (
                    <div className="settings-restore-row">
                      <button
                        className="settings-btn"
                        type="button"
                        aria-pressed={form.staged('restore.tableSort')}
                        onClick={() => form.toggleStaged('restore.tableSort')}
                        disabled={!tableSortHasOverride && !form.staged('restore.tableSort')}
                      >
                        {form.staged('restore.tableSort')
                          ? 'Table column sorting — staged ✓'
                          : 'Table column sorting'}
                      </button>
                      <p className="settings-hint">
                        Clears $/1% Trend, Recent Sessions, Projects &amp; Weekly/Monthly
                        column-click sorting on Save.
                      </p>
                    </div>
                  )}
                  {show('restore.cardOrder') && (
                    <div className="settings-restore-row">
                      <button
                        className="settings-btn"
                        type="button"
                        aria-pressed={form.staged('restore.cardOrder')}
                        onClick={() => form.toggleStaged('restore.cardOrder')}
                        disabled={panelOrderIsDefault && !form.staged('restore.cardOrder')}
                      >
                        {form.staged('restore.cardOrder')
                          ? 'Card order — staged ✓'
                          : 'Card order'}
                      </button>
                      <p className="settings-hint">
                        Restores the default panel arrangement on Save.
                      </p>
                    </div>
                  )}
                </fieldset>
                {disclosuresFor('restore')}
              </section>
            )}

            {/* ---------------- Managed from the CLI ---------------- */}
            {sectionHasRows('cli') && (
              <section className="settings-section" aria-labelledby="settings-h-cli">
                {sectionHeading('cli', false)}
                <p className="settings-hint">
                  These keys govern surfaces this dashboard does not render, so they are
                  set from the CLI. Each row states the exact command.
                </p>
                {disclosuresFor('cli')}
              </section>
            )}
          </div>
        </div>
        {/* §4.1 — three live regions, mounted as persistent dialog chrome
            OUTSIDE anything the filter can unmount, and §3.3's error summary
            sitting immediately above the action bar. The summary is permanently
            mounted so assistive technology always has an anchor; it is
            POPULATED only on blur or on a Save attempt, never from live typing.
            The test action's output lives here too (§2.3 protocol 3), because a
            response can arrive after its row has been filtered away. */}
        <div className="settings-feedback">
          {/* §4.8 — an indeterminate bar, which says work is happening and
              never how much of it is done, and which stops moving under
              `prefers-reduced-motion`. */}
          {submitting && <div className="settings-saving-bar" aria-hidden="true" />}
          <p
            className="settings-status"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            {statusText}
          </p>
          {testOk && <p className="settings-ok">Test alert dispatched ✓</p>}
          {lanAuthRestartSaved && (
            <p className="settings-ok">
              Saved. Restart the dashboard to apply LAN access authentication.
            </p>
          )}
          <div
            ref={summaryRef}
            className={`settings-error-summary${issues.length > 0 || testError ? ' is-populated' : ''}`}
            role="alert"
            aria-live="assertive"
            tabIndex={-1}
          >
            {testError && <p className="settings-error-title">Test failed: {testError}</p>}
            {issues.length > 0 && (
              <>
                <p className="settings-error-title">
                  {issues.length === 1 ? '1 problem to fix' : `${issues.length} problems to fix`}
                </p>
                <ul className="settings-error-list">
                  {issues.map((issue) => (
                    <li key={issue.key}>
                      <button
                        type="button"
                        className="settings-error-link"
                        onClick={() => focusIssue(issue.target)}
                      >
                        {issue.message}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>
        {/* §4.4 — the action bar sits OUTSIDE the scrollport: a direct child of
            `.modal-card`, beside `.settings-main`, with the scroller one level
            deeper inside `.settings-main` next to the rail. Chromium performs
            no corrective scroll for a control already inside the scrollport and
            merely behind the bar, so the only way a focused control cannot end
            up underneath it is for the scrollport to end above it. */}
        <div className="settings-actions" ref={actionsRef}>
          <button
            className="settings-btn"
            id="settings-save"
            type="button"
            onClick={save}
            disabled={saveDisabled}
          >
            {submitting
              ? 'Saving…'
              : dirtyCount === 0
                ? 'Save'
                : `Save · ${dirtyCount} change${dirtyCount === 1 ? '' : 's'}`}
          </button>
          {/* §3.6 suppresses dismissal while a submit is in flight, so Cancel
              is genuinely inoperable then and says so. */}
          <button
            className="settings-btn"
            type="button"
            onClick={close}
            disabled={submitting}
          >
            Cancel
          </button>
          {/* §2.8 — the default scope, stated once. */}
          <p className="settings-scope-note">
            Changes apply to this machine unless marked <em>this browser</em>.
          </p>
        </div>
        {/*
          SET-2 (#252) dismiss-guard confirm. Rendered as a SIBLING of the
          header + body inside `.modal-card` (which is position:relative), so the
          existing card-level `useModalFocus` trap contains focus; while it is up
          every other child of the card is marked `inert` (effect above) so Tab
          cycles only the two confirm buttons. [Discard] closes; [Keep editing]
          dismisses the confirm. Focus lands on the safe default.
        */}
        {confirmDiscard && (
          <div
            className="settings-confirm"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="settings-confirm-title"
          >
            <div className="settings-confirm-card">
              <p id="settings-confirm-title">
                Discard {dirtyCount} unsaved change{dirtyCount === 1 ? '' : 's'}?
              </p>
              <div className="settings-confirm-actions">
                <button
                  ref={keepEditingRef}
                  className="settings-btn"
                  type="button"
                  onClick={() => setConfirmDiscard(false)}
                >
                  Keep editing
                </button>
                <button
                  className="settings-btn settings-btn-danger"
                  type="button"
                  onClick={close}
                >
                  Discard
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
