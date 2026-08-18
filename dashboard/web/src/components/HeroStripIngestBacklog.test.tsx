/**
 * public #5 — the Codex hero's ingest-backlog disclosure.
 *
 * The hook's Codex ingest leg is budgeted, so on a large or freshly upgraded
 * store some rollout history has not been read yet and the totals on screen are
 * correct for LESS than everything. That is a caveat about the number, not a
 * failure: it must be disclosed without touching `availability` or `freshness`,
 * which a long and explicitly non-exhaustive list of gates reads.
 *
 * The first round of this file tested only the two pure helpers. That is how a
 * green suite shipped a note which never rendered in the landing view of a
 * multi-account install and had no visible form anywhere: neither defect is
 * observable below `HeroStrip`. The component block at the bottom renders it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';

import {
  CODEX_STALE_CYCLE_NOTE,
  HeroStrip,
  codexIngestBacklogCompactLabel,
  codexIngestBacklogLabel,
  codexIngestBacklogNote,
  joinHeroNotes,
} from './HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  ACCOUNT_B,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  AllCombinedQualification,
  CodexSourceData,
  Envelope,
  SourcesMap,
} from '../types/envelope';

describe('codexIngestBacklogNote', () => {
  it('is absent when the field is absent — the zero state is omission', () => {
    expect(codexIngestBacklogNote(undefined)).toBeNull();
    expect(codexIngestBacklogNote(null)).toBeNull();
  });

  it('is absent for a zero-valued record, which a mid-drain tick can emit', () => {
    expect(codexIngestBacklogNote({ files: 0 })).toBeNull();
  });

  it('names the remaining count and says the totals will rise', () => {
    const note = codexIngestBacklogNote({ files: 4 });
    expect(note).toContain('4 sessions');
    expect(note).toContain('still loading');
  });

  it('agrees with itself on the singular', () => {
    expect(codexIngestBacklogNote({ files: 1 })).toContain('1 session left');
  });
});

describe('codexIngestBacklogLabel', () => {
  it('applies exactly when the sentence does', () => {
    expect(codexIngestBacklogLabel(undefined)).toBeNull();
    expect(codexIngestBacklogLabel(null)).toBeNull();
    expect(codexIngestBacklogLabel({ files: 0 })).toBeNull();
  });

  it('carries the count and stays short enough for the hero sub-line', () => {
    const label = codexIngestBacklogLabel({ files: 3 })!;
    expect(label).toContain('3 sessions');
    // The hero-spent zone is the narrowest of the three at desktop and half a
    // phone width on mobile; the full sentence lives in the tooltip instead.
    expect(label.length).toBeLessThanOrEqual(32);
  });

  it('agrees with itself on the singular', () => {
    expect(codexIngestBacklogLabel({ files: 1 })).toContain('1 session');
  });

  it('has a phone-width form that fits the narrow spent zone', () => {
    expect(codexIngestBacklogCompactLabel({ files: 3 })).toBe('+3 more');
    expect(codexIngestBacklogCompactLabel({ files: 1 })).toBe('+1 more');
    expect(codexIngestBacklogCompactLabel({ files: 0 })).toBeNull();
  });
});

describe('joinHeroNotes', () => {
  it('keeps BOTH disclosures when both apply', () => {
    // Suppressing either would hide a real caveat about the number on screen:
    // one says the forecast is paused, the other says the total is incomplete.
    const joined = joinHeroNotes(
      CODEX_STALE_CYCLE_NOTE, codexIngestBacklogNote({ files: 2 }),
    );
    expect(joined).toContain('forecast is paused');
    expect(joined).toContain('still loading');
  });

  it('collapses to null when nothing applies', () => {
    expect(joinHeroNotes(null, null)).toBeNull();
  });

  it('does not introduce separator noise for a single note', () => {
    expect(joinHeroNotes(null, 'only this')).toBe('only this');
  });
});

// =========================================================================
// Component level.
// =========================================================================

const NOW = '2026-04-24T13:07:00Z';
const BACKLOG = { files: 3, bytes: 8192, since: '2026-07-16T09:00:00Z' };

type Backlog = { files: number; bytes: number; since: string | null };

function withBacklog(
  data: CodexSourceData, backlog: Backlog = BACKLOG,
): CodexSourceData {
  return { ...data, ingest_backlog: backlog };
}

/** Stale evidence for the PARENT cycle — what an undecorated store renders. */
function withStaleParentCycle(data: CodexSourceData): CodexSourceData {
  return { ...data, hero: { ...data.hero, cycle_freshness: 'stale' as const } };
}

/**
 * Stale evidence for ONE account's own cycle.
 *
 * `focusedHero` derives the focused `cycle_freshness` from the CHILD's quota
 * summary, never from the parent's marker, so a focused-view stale case has to
 * be expressed on the child or the fixture asserts nothing.
 */
function withStaleAccountCycle(
  data: CodexSourceData, accountKey: string,
): CodexSourceData {
  const scopes = data.account_scopes!;
  const child = scopes[accountKey];
  return {
    ...data,
    account_scopes: {
      ...scopes,
      [accountKey]: {
        ...child,
        quota: {
          ...child.quota,
          summary: { ...child.quota.summary, freshness: 'stale' as const },
        },
      },
    },
  };
}

function envWith(codexData: CodexSourceData): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry({ data: codexData });
  const sources = {
    claude,
    codex,
    all: makeAllSourceEntry(claude, codex),
  } as unknown as SourcesMap;
  return makeSourceEnvelope({ sources }) as unknown as Envelope;
}

function renderCodex(codexData: CodexSourceData, focus?: string): HTMLElement {
  updateSnapshot(envWith(codexData));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  if (focus != null) {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: focus });
  }
  const { container } = render(<HeroStrip />);
  return container.querySelector('.hero-spent') as HTMLElement;
}

// #556 S1 §4.3 — All's disclosure comes from `combined.qualifications`, which
// composition LIFTS from the provider. `qualifications` is therefore an explicit
// argument here: deriving it from `codexData.ingest_backlog` would rebuild
// inside the harness the very coupling the change removes, and the inverse tests
// below could not distinguish the two sources.
function renderAll(
  codexData: CodexSourceData,
  {
    combinedAvailable = true,
    qualifications,
  }: {
    combinedAvailable?: boolean;
    qualifications?: AllCombinedQualification[];
  } = {},
) {
  const env = envWith(codexData);
  const allData = env.sources!.all.data!;
  if (!combinedAvailable) {
    allData.combined = null;
    allData.combined_unavailable = {
      code: 'codex_cycle_unavailable',
      message: 'Codex native reset cycle is unavailable.',
      causes: [{ provider: 'codex', code: 'codex_cycle_unavailable' }],
    };
  } else if (qualifications != null) {
    allData.combined!.qualifications = qualifications;
  }
  updateSnapshot(env);
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  const rendered = render(<HeroStrip />);
  return {
    ...rendered,
    spent: rendered.getByTestId('shared-hero-spent'),
  };
}

const BACKLOG_QUALIFICATION: AllCombinedQualification = {
  code: 'codex_ingest_backlog',
  message: 'Codex has pending accounting to ingest, so its cycle total may be '
    + 'incomplete.',
  provider: 'codex',
};

function qualificationsIn(spent: HTMLElement): string[] {
  return Array.from(
    spent.querySelectorAll('[data-testid="hero-combined-qualification"]'),
    (el) => el.getAttribute('data-code') ?? '',
  );
}

function noteIn(spent: HTMLElement): HTMLElement | null {
  return spent.querySelector('[data-testid="hero-spent-note"]');
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(NOW));
});

describe('<HeroStrip /> ingest-backlog disclosure — scope', () => {
  // QA P1. "All accounts" is the LANDING state of every decorated Codex
  // install, and its merged headline spend is precisely the incomplete number
  // this note exists to qualify. The backlog is a store-wide INGEST condition,
  // not an account-scoped one, so #416's D6 blanking sweep — which is about
  // never blending independent quota cycles into one number — does not reach it.
  it('discloses the backlog in the merged "All accounts" view', () => {
    const spent = renderCodex(withBacklog(makeDecoratedCodexSourceData()));
    expect(noteIn(spent)?.textContent).toContain('3 sessions');
    expect(spent.getAttribute('aria-label')).toMatch(/still loading/i);
    expect(spent.getAttribute('title')).toMatch(/still loading/i);
  });

  // public #5 QA P2. The visible note is `aria-hidden` and defers to the zone's
  // `aria-label` — which a bare `<div>` does not expose, because `aria-label`
  // is not honoured on the implicit `generic` role. Both channels were
  // unreliable at once. Query by ROLE, not by attribute: an attribute
  // assertion passes on an element whose name nothing can read.
  it('exposes the disclosure through an accessible name, not just an attribute',
    () => {
      updateSnapshot(envWith(withBacklog(makeDecoratedCodexSourceData())));
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
      const { getByRole } = render(<HeroStrip />);
      const named = getByRole('group', { name: /still loading/i });
      expect(named.classList.contains('hero-spent')).toBe(true);
    });

  it('discloses it under a focused account too', () => {
    const spent = renderCodex(
      withBacklog(makeDecoratedCodexSourceData()), ACCOUNT_B);
    expect(noteIn(spent)?.textContent).toContain('3 sessions');
    expect(spent.getAttribute('aria-label')).toMatch(/still loading/i);
  });

  it('discloses it on an undecorated single-account store (R8)', () => {
    const spent = renderCodex(withBacklog(makeCodexSourceData()));
    expect(noteIn(spent)?.textContent).toContain('3 sessions');
  });

  // Guard-rail, not new coverage. The stale-cycle note is ACCOUNT-scoped and
  // #416 suppressed it under All accounts deliberately: the forecast slot it
  // talks about already reads "per account" there. This change must not undo
  // that while it lifts the store-wide note out of the same gate.
  it('leaves the account-scoped stale-cycle note suppressed under All accounts', () => {
    const spent = renderCodex(
      withBacklog(withStaleParentCycle(makeDecoratedCodexSourceData())));
    expect(spent.getAttribute('aria-label')).toMatch(/still loading/i);
    expect(spent.getAttribute('aria-label')).not.toMatch(/forecast is paused/i);
    expect(spent.getAttribute('title')).not.toMatch(/forecast is paused/i);
  });

  it('carries BOTH disclosures once an account is focused', () => {
    const spent = renderCodex(
      withBacklog(withStaleAccountCycle(
        makeDecoratedCodexSourceData(), ACCOUNT_B)),
      ACCOUNT_B,
    );
    expect(spent.getAttribute('aria-label')).toMatch(/forecast is paused/i);
    expect(spent.getAttribute('aria-label')).toMatch(/still loading/i);
    expect(noteIn(spent)?.textContent).toContain('3 sessions');
  });

  // Non-vacuity for the guard-rail above: the stale note DOES still reach an
  // undecorated hero, so its absence under All accounts is the gate and not a
  // fixture that never sets the marker.
  it('still discloses a stale cycle on an undecorated store', () => {
    const spent = renderCodex(withStaleParentCycle(makeCodexSourceData()));
    expect(spent.getAttribute('aria-label')).toMatch(/forecast is paused/i);
  });
});

describe('<HeroStrip /> ingest-backlog disclosure — visibility', () => {
  // QA P2. The point of the note is to TELL the user the totals are incomplete.
  // A `title` on a non-interactive div is hover-only — unreachable on touch —
  // and an aria-label leaves a sighted touch user with nothing at all.
  it('renders the disclosure as text, not only as a tooltip', () => {
    const spent = renderCodex(withBacklog(makeCodexSourceData()));
    const note = noteIn(spent);
    expect(note).not.toBeNull();
    expect(note!.textContent?.trim()).not.toBe('');
    // Low emphasis: the hero-spent zone's own dim meta sub-line vocabulary,
    // not a chip and not an alert-severity surface.
    expect(note!.className).toContain('hs-sub');
  });

  it('renders distinct full and compact labels for the responsive CSS gate', () => {
    const spent = renderCodex(withBacklog(makeCodexSourceData()));
    const note = noteIn(spent)!;
    expect(note.querySelector('.hero-ingest-backlog-label-full'))
      .toHaveTextContent('+3 sessions still loading');
    expect(note.querySelector('.hero-ingest-backlog-label-compact'))
      .toHaveTextContent('+3 more');
  });

  it('does not double-announce — the zone aria-label is the single reading', () => {
    const spent = renderCodex(withBacklog(makeCodexSourceData()));
    expect(noteIn(spent)!.getAttribute('aria-hidden')).toBe('true');
    expect(spent.getAttribute('aria-label')).toMatch(/still loading/i);
  });

  it('renders no note, no empty element and no stray separator at zero', () => {
    const spent = renderCodex(makeCodexSourceData());
    expect(noteIn(spent)).toBeNull();
    expect(spent.getAttribute('title')).toBeNull();
    expect(spent.getAttribute('aria-label')).toBeNull();
    expect(spent.textContent?.trimEnd()).not.toMatch(/[·—-]$/);
  });

  it('renders no note for a zero-valued record either', () => {
    const spent = renderCodex(
      withBacklog(makeCodexSourceData(), { files: 0, bytes: 0, since: null }));
    expect(noteIn(spent)).toBeNull();
    expect(spent.getAttribute('title')).toBeNull();
  });

  // The suppression its own commit body called load-bearing, and which nothing
  // pinned. With no cost the hero has no number to qualify, so the visible line
  // must yield to the unavailable reason rather than sit under a blank figure
  // saying more of it is on the way — and the tooltip and the label must not
  // then disagree with the visible line about which note is showing.
  it('suppresses the visible note while an unavailable reason is showing', () => {
    const data = withBacklog(makeCodexSourceData());
    const spent = renderCodex({
      ...data, hero: { ...data.hero, cost_usd: null },
    } as CodexSourceData);
    expect(noteIn(spent)).toBeNull();
    expect(spent.getAttribute('title')).not.toMatch(/still loading/i);
    expect(spent.getAttribute('aria-label')).toMatch(/unavailable/i);
  });

  // The note is a caveat about a real number, never a replacement for it.
  it('never hides the spend it qualifies', () => {
    const spent = renderCodex(withBacklog(makeCodexSourceData()));
    expect((spent.querySelector('.hs-big') as HTMLElement).textContent)
      .toBe('$12.30');
    expect(spent.textContent).toContain('/ 1% used');
  });
});

describe('<HeroStrip /> ingest-backlog disclosure — Combined hero (#556 §4.3)', () => {
  it('qualifies the Combined spend from combined.qualifications', () => {
    const { spent } = renderAll(withBacklog(makeCodexSourceData()), {
      qualifications: [BACKLOG_QUALIFICATION],
    });

    expect(qualificationsIn(spent)).toEqual(['codex_ingest_backlog']);
    const chip = spent.querySelector(
      '[data-testid="hero-combined-qualification"]',
    ) as HTMLElement;
    // Visible text, not a hover-only tooltip: `title` alone is unreachable on
    // touch, which is the defect the visible short label exists to fix.
    expect(chip.textContent).toContain('Codex still loading');
    expect(chip.getAttribute('title')).toMatch(/pending accounting/i);
    expect(chip.getAttribute('aria-label')).toMatch(/pending accounting/i);
    expect(spent.textContent).toContain('$20.70');
  });

  // The two inverse directions. The provider field stays published for the
  // Codex tab's own use, so a test that only checks the happy path cannot tell
  // which of the two sources All is actually reading.
  it('follows combined.qualifications even when the provider field disagrees', () => {
    const { spent } = renderAll(makeCodexSourceData(), {
      qualifications: [BACKLOG_QUALIFICATION],
    });

    expect(qualificationsIn(spent)).toEqual(['codex_ingest_backlog']);
  });

  it('renders no qualification carried only by the provider field', () => {
    const { spent } = renderAll(withBacklog(makeCodexSourceData()), {
      qualifications: [],
    });

    expect(qualificationsIn(spent)).toEqual([]);
    expect(spent.textContent).not.toMatch(/still loading/i);
  });

  it('omits every qualification channel when no work is owed', () => {
    const { spent } = renderAll(makeCodexSourceData());

    expect(qualificationsIn(spent)).toEqual([]);
    expect(spent.getAttribute('title')).toBeNull();
  });

  it('shows the withheld reason rather than a qualification when there is no number', () => {
    const { spent } = renderAll(withBacklog(makeCodexSourceData()), {
      combinedAvailable: false,
    });

    // With no figure on screen there is nothing for a qualification to qualify.
    expect(qualificationsIn(spent)).toEqual([]);
    expect(spent.textContent).toContain('Combined withheld');
    expect(spent.getAttribute('title'))
      .toBe('Codex native reset cycle is unavailable.');
  });
});
