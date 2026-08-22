// #620 S2 — the dashboard's diagnosis surface.
//
// What JSDOM can decide is asserted here: which element carries which text,
// what the modal declares to a screen reader, which key opens it, and — the
// part that matters most — that the surface never states a figure the server
// withheld. What it cannot decide (real `@media` at 390px, trusted Tab order,
// a real focus trap, whether the text is legible) is the browser gate's.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ModalRoot } from '../src/modals/ModalRoot';
import { Header } from '../src/components/Header';
import { HELP_ROWS } from '../src/components/HelpOverlay';
import { buildGlobalKeyBindings } from '../src/store/globalBindings';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../src/store/store';
import envelopeFixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  registerKeymap,
  uninstallGlobalKeydown,
} from '../src/store/keymap';
import {
  diagnosisUrl,
  diagnosisWithheldMessage,
  evidenceLabel,
  evidenceValue,
  evidenceWithheldMessage,
  gapCodeMessage,
  reachesContributorFloor,
  TRANSCRIPT_ASYMMETRY_NOTE,
} from '../src/lib/diagnosis';

const CONSTANTS = {
  contributorShareFloor: 0.2,
  shareFloorEpsilon: 1e-9,
  tieEpsilonUsd: 1e-9,
  confidenceHighMinSupport: 20,
  confidenceMediumMinSupport: 5,
  confidenceMediumMinCoverage: 0.8,
  withholdMinCoverage: 0.5,
  withholdMinSupport: 2,
  registry: [
    {
      contributorClass: 'model_mix',
      subjectKind: 'model',
      label: 'Expensive model mix',
      minDistinctSubjects: 2,
      minPricedEntries: 20,
    },
    {
      contributorClass: 'project_concentration',
      subjectKind: 'project',
      label: 'One project dominating',
      minDistinctSubjects: 2,
      minPricedEntries: 20,
    },
  ],
};

function coverage(over: Record<string, unknown> = {}) {
  return {
    requestedStart: '2026-08-10T00:00:00Z',
    requestedEnd: '2026-08-17T00:00:00Z',
    observedStart: '2026-08-10T01:00:00Z',
    observedEnd: '2026-08-16T23:00:00Z',
    supportUnits: 60,
    gapCodes: [],
    usdCoverage: 1.0,
    ...over,
  };
}

function available(value: number, over: Record<string, unknown> = {}) {
  return { state: 'available', value, population: coverage(), qualifications: [], ...over };
}

function withheld(code: string, over: Record<string, unknown> = {}) {
  return {
    state: 'withheld', value: null, population: coverage(),
    qualifications: [], code, ...over,
  };
}

function report(over: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    contractVersion: 1,
    measuredAt: '2026-08-17T00:00:00Z',
    window: {
      startAt: '2026-08-10T00:00:00Z',
      endAt: '2026-08-17T00:00:00Z',
      tz: 'Etc/UTC',
      label: '',
    },
    constants: CONSTANTS,
    notes: {
      scope: 'This explains locally retained cost and tokens, not quota percentage.',
      overlap: 'The classes overlap — one session is also a project and a model — '
        + 'so shares do not sum to 100 percent.',
    },
    overallVerdict: 'contributor_detected',
    overallCode: null,
    applicableClassCount: 2,
    withheldClassCount: 0,
    results: [
      {
        source: 'claude',
        accountKey: null,
        effectiveSpeed: null,
        generation: { stats: 'a', cache: 'b', configuration: 'c', generationId: 'gid' },
        denominator: {
          identity: 'totalExplainedRetainedCost',
          source: 'claude',
          accountKey: null,
          windowStart: '2026-08-10T00:00:00Z',
          windowEnd: '2026-08-17T00:00:00Z',
          populationDigest: 'digest',
          usd: available(120.5),
        },
        coverage: coverage(),
        verdict: 'contributor_detected',
        code: null,
        applicableClassCount: 2,
        withheldClassCount: 0,
        contributors: [
          {
            contributorClass: 'model_mix',
            subjectKind: 'model',
            subjectKey: 'claude-opus-4-20250514',
            subjectLabel: 'claude-opus-4-20250514',
            rank: 1,
            observedUsd: available(96.4),
            share: available(0.8),
            baseline: available(0.5),
            confidence: 'high',
            isFallbackPricing: false,
            nextStep: 'cctally daily --source claude --since 2026-08-10 --until 2026-08-16',
          },
        ],
        classes: [
          {
            contributorClass: 'model_mix',
            verdict: 'contributor',
            code: null,
            supportShortfall: null,
            confidence: 'high',
            population: coverage(),
            rows: [],
          },
          {
            contributorClass: 'project_concentration',
            verdict: 'no_contributor',
            code: null,
            supportShortfall: null,
            confidence: 'high',
            population: coverage(),
            rows: [],
          },
        ],
      },
    ],
    ...over,
  };
}

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src');

/** Publish a snapshot whose display block resolves to `tz`.
 *
 *  The modal builds its `Ctx` from the envelope's display block, so a test
 *  that leaves the snapshot null runs every datetime through the hook's
 *  `Etc/UTC` default — the one zone whose rendering looked correct while the
 *  defect was live. */
function displayZone(tz: string, offsetLabel: string, offsetSeconds: number): void {
  updateSnapshot({
    ...(envelopeFixture as unknown as Envelope),
    display: {
      tz, resolved_tz: tz, offset_label: offsetLabel,
      offset_seconds: offsetSeconds, pinned: false,
    },
  } as unknown as Envelope);
}

function stubFetch(payload: unknown, status = 200): void {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  })));
}

function openExplain(over: Record<string, unknown> = {}): void {
  dispatch({ type: 'OPEN_MODAL', kind: 'explain', ...over });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

// ─── discovery ──────────────────────────────────────────────────────────────

describe('#620 S2 — discovery', () => {
  it('has a HELP_ROWS entry, without which the coverage test fails red', () => {
    expect(HELP_ROWS.some((r) => r.keys.includes('e'))).toBe(true);
  });

  it('opens on e', () => {
    registerKeymap(buildGlobalKeyBindings());
    fireEvent.keyDown(document, { key: 'e' });
    expect(getState().openModal).toBe('explain');
  });

  it('is inert behind another modal, so `e` never stacks two', () => {
    registerKeymap(buildGlobalKeyBindings());
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    fireEvent.keyDown(document, { key: 'e' });
    expect(getState().openModal).toBe('alerts');
  });

  it('has a persistent labelled control in the top area', () => {
    registerKeymap(buildGlobalKeyBindings());
    const { container } = render(<Header />);
    const button = container.querySelector('.topbar-explain');
    expect(button).not.toBeNull();
    expect(button?.tagName).toBe('BUTTON');
    // Labelled in words: a glyph says nothing about which question it answers.
    expect(button?.textContent?.trim()).not.toBe('');
    fireEvent.click(button as HTMLButtonElement);
    expect(getState().openModal).toBe('explain');
  });
});

// ─── the modal itself ───────────────────────────────────────────────────────

describe('#620 S2 — the modal', () => {
  it('declares role, aria-modal and an accessible name', async () => {
    stubFetch(report());
    openExplain();
    render(<ModalRoot />);
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName();
  });

  it('closes on Escape', async () => {
    stubFetch(report());
    openExplain();
    render(<ModalRoot />);
    await screen.findByRole('dialog');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().openModal).toBeNull();
  });

  it('states both the alert instant and the measured instant when followed', async () => {
    stubFetch(report());
    openExplain({
      alertFiredAt: '2026-08-16T09:00:00Z',
      windowStartAt: '2026-08-10T00:00:00Z',
      windowEndAt: '2026-08-17T00:00:00Z',
    });
    render(<ModalRoot />);
    expect(await screen.findByText(/alert fired/i)).toBeInTheDocument();
    expect(screen.getByText(/^measured$/i)).toBeInTheDocument();
  });

  it('states only the measured instant when nothing fired', async () => {
    stubFetch(report());
    openExplain();
    render(<ModalRoot />);
    await screen.findByText(/^measured$/i);
    expect(screen.queryByText(/alert fired/i)).toBeNull();
  });

  it('requests the exact half-open bounds it was opened with', async () => {
    stubFetch(report());
    openExplain({
      windowStartAt: '2026-08-10T00:00:00Z',
      windowEndAt: '2026-08-17T00:00:00Z',
      accountKey: 'acct-1',
    });
    render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      const url = (globalThis.fetch as unknown as { mock: { calls: string[][] } })
        .mock.calls[0][0];
      expect(url).toContain('start_at=2026-08-10T00%3A00%3A00Z');
      expect(url).toContain('end_at=2026-08-17T00%3A00%3A00Z');
      expect(url).toContain('account=acct-1');
    });
  });

  it('renders a 503 body as the report it is, not as a transport failure', async () => {
    // A store that could not be READ is published with 503 AND with the whole
    // withheld report as the body, because the typed cause is what the reader
    // needs. Reporting "HTTP 503" instead would say less than the server did.
    const withheldReport = report({
      overallVerdict: 'withheld',
      overallCode: 'provider_unavailable',
      results: [{
        ...report().results[0],
        verdict: 'withheld',
        code: 'provider_unavailable',
        contributors: [],
        denominator: {
          ...report().results[0].denominator,
          usd: withheld('provider_unavailable'),
        },
        classes: [{
          contributorClass: 'model_mix',
          verdict: 'withheld',
          code: 'provider_unavailable',
          supportShortfall: null,
          population: coverage({ supportUnits: null, usdCoverage: undefined }),
          rows: [],
        }],
      }],
    });
    stubFetch(withheldReport, 503);
    openExplain();
    render(<ModalRoot />);
    expect((await screen.findAllByText(/store could not be read/i)).length)
      .toBeGreaterThan(0);
    expect(screen.queryByText(/HTTP 503/)).toBeNull();
  });
});

// ─── the three things it must never state ───────────────────────────────────

describe('#620 S2 — the surface never invents a figure', () => {
  it('never renders a withheld denominator as a dollar amount', async () => {
    const pricingless = report({
      results: [{
        ...report().results[0],
        denominator: {
          ...report().results[0].denominator,
          usd: withheld('pricing_unavailable'),
        },
        contributors: [],
      }],
    });
    stubFetch(pricingless);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.textContent).toContain('Nothing in this window could be priced');
    });
    expect(container.textContent).not.toContain('$0.00');
  });

  it('never renders an absent coverage dimension as zero', async () => {
    const preempted = report({
      results: [{
        ...report().results[0],
        contributors: [],
        classes: [{
          contributorClass: 'model_mix',
          verdict: 'withheld',
          code: 'provider_unavailable',
          supportShortfall: null,
          // The provider preempted this class, so it shaped no subjects: every
          // dimension is absent and `supportUnits` is null, not zero.
          population: coverage({
            supportUnits: null, usdCoverage: undefined, identityCoverage: undefined,
          }),
          rows: [],
        }],
      }],
    });
    stubFetch(preempted);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.textContent).toContain('support not measured');
    });
    expect(container.textContent).not.toContain('cost coverage 0%');
    expect(container.textContent).not.toContain('support 0 units');
  });

  it('states the coverage a reported contributor rests on', async () => {
    // Spec §4: a reported contributor states observed USD, share, baseline,
    // COVERAGE, confidence and one next step. The class lines already state
    // their population, so a contributor row that states none is the one
    // figure on this surface a reader cannot weigh.
    stubFetch(report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const row = await waitFor(() => {
      const found = container.querySelector('.explain-row');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    const note = row.querySelector('.explain-coverage');
    expect(note).not.toBeNull();
    expect(note?.textContent).toContain('support 60 units');
    expect(note?.textContent).toContain('cost coverage 100%');
  });

  it('never renders an absent contributor coverage dimension as zero', async () => {
    const thin = report({
      results: [{
        ...report().results[0],
        contributors: [{
          ...report().results[0].contributors[0],
          // The adapter could measure neither the identity nor the pricing
          // dimension for this row. Absence is not zero.
          observedUsd: {
            state: 'available', value: 96.4, qualifications: [],
            population: coverage({
              identityCoverage: undefined, pricingCoverage: undefined,
            }),
          },
        }],
      }],
    });
    stubFetch(thin);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const row = await waitFor(() => {
      const found = container.querySelector('.explain-row');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    const note = row.querySelector('.explain-coverage');
    // Asserted before the negatives, so a missing node cannot satisfy them.
    expect(note).not.toBeNull();
    expect(note?.textContent).toContain('support 60 units');
    expect(note?.textContent).not.toContain('identity coverage');
    expect(note?.textContent).not.toContain('priced without fallback');
    // A word boundary, because `cost coverage 100%` contains the substring
    // `0%` and a bare `toContain` check would pass over a real `0%`.
    expect(note?.textContent).not.toMatch(/\b0%/);
  });

  it('renders a fallback branch for an unknown withheld cause', async () => {
    const future = report({
      results: [{
        ...report().results[0],
        contributors: [],
        classes: [{
          contributorClass: 'model_mix',
          verdict: 'withheld',
          code: 'a_cause_from_the_future',
          supportShortfall: null,
          population: coverage(),
          rows: [],
        }],
      }],
    });
    stubFetch(future);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.textContent).toMatch(/unavailable/i);
    });
    // The code is carried so a bug report can name it.
    expect(container.textContent).toContain('a_cause_from_the_future');
  });

  it('discloses every value in text, never only in a title attribute', async () => {
    stubFetch(report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    // A `title` never appears on a touch device, so it may not be the only
    // carrier of anything (R9). The simplest form of that rule: this surface
    // uses no `title` at all.
    expect(container.querySelectorAll('[title]').length).toBe(0);
  });
});

// ─── a reader can read what it states ───────────────────────────────────────

describe('#620 S2 — the surface states its facts legibly', () => {
  const JERUSALEM_WINDOW = {
    startAt: '2026-08-10T00:00:00Z',
    endAt: '2026-08-17T00:00:00Z',
    tz: 'Asia/Jerusalem',
    label: '',
  };

  it('names the window zone, because a bare offset does not identify one', async () => {
    // The browser gate read `Aug 10 03:00 +03 to Aug 16 15:00 +03` and could
    // not reconstruct which window had been measured: several zones are `+03`,
    // and Asia/Jerusalem itself is `+02` in winter. Spec §4 requires the window
    // to be stated with its IANA zone, and the CLI states it as `[Etc/UTC]` on
    // the same report. Every existing test pinned `Etc/UTC`, where the same
    // code path emits the literal `UTC` and looks correct, so the whole
    // fixture estate was structurally blind to this.
    displayZone('Asia/Jerusalem', 'IDT', 10800);
    stubFetch(report({ window: JERUSALEM_WINDOW }));
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const dd = await waitFor(() => {
      const found = container.querySelector('.explain-instants dd');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    // The whole statement, not a substring: the bounds must still be
    // converted into the display zone (so this cannot pass on a surface that
    // quietly reverted to UTC text), and the zone name must sit beside them
    // rather than anywhere in the modal.
    expect(dd.textContent).toBe(
      'Aug 10 03:00 +03 to Aug 17 03:00 +03 [Asia/Jerusalem] '
      + '(the start is included, the end is not)',
    );
  });

  it('names the zone on EVERY instant line, not only the window line', async () => {
    // Browser round 2 of the same defect: the window line named the zone and
    // the `Measured` line rendered `Aug 16 15:00 +03`, which identifies no
    // zone at all — `+03` is shared by several, and a zone that is `+03` in
    // August is `+02` in January. `Etc/UTC` hides it, because the suffix there
    // is the literal `UTC` and reads as a zone name, so this test pins a
    // non-UTC zone and reads every line rather than the first one.
    displayZone('Asia/Jerusalem', 'IDT', 10800);
    stubFetch(report({ window: JERUSALEM_WINDOW }));
    openExplain({ alertFiredAt: '2026-08-16T09:00:00Z' });
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const lines = await waitFor(() => {
      const found = container.querySelectorAll('.explain-instants dd');
      expect(found.length).toBe(3);
      return Array.from(found) as HTMLElement[];
    });
    for (const line of lines) {
      expect(line.textContent).toContain('[Asia/Jerusalem]');
    }
    // The two lines the window line's own tag never covered, whole rather
    // than by substring, so a surface that quietly reverted to UTC text still
    // fails here.
    expect(lines[1].textContent).toBe('Aug 16 12:00 +03 [Asia/Jerusalem]');
    expect(lines[2].textContent).toBe('Aug 17 03:00 +03 [Asia/Jerusalem]');
  });

  it('names the zone under Etc/UTC too, where the bare suffix looked right', async () => {
    displayZone('Etc/UTC', 'UTC', 0);
    stubFetch(report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const dd = await waitFor(() => {
      const found = container.querySelector('.explain-instants dd');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(dd.textContent).toBe(
      'Aug 10 00:00 UTC to Aug 17 00:00 UTC [Etc/UTC] '
      + '(the start is included, the end is not)',
    );
  });

  it('separates the next-step label from the command it precedes', async () => {
    // The gate measured the label's right edge and the `<code>` left edge at
    // the same x on all 14 rows: JSX strips the newline between the two
    // elements, so the emitted HTML carries no text node between them and the
    // uppercase label read as one word with the command —
    // `NEXT STEPcctally claude daily …`. The separation is the rule's, so the
    // rule is what this pins.
    stubFetch(report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const next = await waitFor(() => {
      const found = container.querySelector('.explain-row-next');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(next.querySelector('.explain-figure-label')?.textContent?.trim())
      .toBe('next step');
    expect(next.querySelector('code')?.textContent).toContain('cctally');
    const css = readFileSync(path.join(SRC_DIR, 'index.css'), 'utf8');
    const rule = /\n\.explain-row-next\s*\{([^}]*)\}/.exec(css);
    expect(rule).not.toBeNull();
    // The two sibling rows that already state a label beside a value —
    // `.explain-row-figures` and `.explain-denominator` — both carry a flex
    // gap. This is the same construction, not a third one.
    expect(rule?.[1]).toMatch(/display:\s*flex/);
    expect(rule?.[1]).toMatch(/gap:/);
  });

  it('keeps the denominator identifier readable and names it as the CLI does', async () => {
    // `.explain-figure-label` uppercases, and the identifier's camelCase
    // boundaries were its only readability cue, so the wire name
    // `totalExplainedRetainedCost` rendered `TOTALEXPLAINEDRETAINEDCOST`. The
    // wire identifier is the denominator's stable identity and does not
    // change; where it is placed does. The CLI prints
    // `Denominator totalExplainedRetainedCost: $2.38 of locally retained cost`.
    stubFetch(report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const p = await waitFor(() => {
      const found = container.querySelector('.explain-denominator');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(p.querySelector('.explain-figure-label')?.textContent?.trim())
      .toBe('Denominator');
    const identity = p.querySelector('.explain-denominator-identity');
    expect(identity).not.toBeNull();
    expect(identity?.textContent).toBe('totalExplainedRetainedCost');
    // The uppercasing element must not be the one carrying the identifier.
    expect(identity?.classList.contains('explain-figure-label')).toBe(false);
  });
});

// ─── one vocabulary across the terminal and the modal ───────────────────────

describe('#620 S2 — the two surfaces say the same words about the same facts', () => {
  it('rounds an exact-half coverage the way the terminal rounds it', async () => {
    // `0.125` is 12.5 percent exactly. `toFixed` rounds that tie up and
    // Python's format spec rounds it to even, so one published number printed
    // `13%` here and `12%` in the terminal. The terminal moved to half-up;
    // this pins the client half so the pair cannot drift apart again.
    const halves = report({
      results: [{
        ...report().results[0],
        contributors: [],
        classes: [{
          contributorClass: 'model_mix',
          verdict: 'no_contributor',
          code: null,
          supportShortfall: null,
          confidence: 'high',
          population: coverage({ usdCoverage: 0.625, identityCoverage: 0.125 }),
          rows: [],
        }],
      }],
    });
    stubFetch(halves);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const note = await waitFor(() => {
      const found = container.querySelector('.explain-class .explain-coverage');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(note.textContent).toContain('cost coverage 63%');
    expect(note.textContent).toContain('identity coverage 13%');
  });

  it('states one support unit in the singular, as the terminal now does', async () => {
    const single = report({
      results: [{
        ...report().results[0],
        contributors: [],
        classes: [{
          contributorClass: 'model_mix',
          verdict: 'no_contributor',
          code: null,
          supportShortfall: null,
          confidence: 'low',
          population: coverage({ supportUnits: 1 }),
          rows: [],
        }],
      }],
    });
    stubFetch(single);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.textContent).toContain('support 1 unit');
    });
    expect(container.textContent).not.toContain('support 1 units');
  });

  it('states the confidence of a measured class, and none for a withheld one', async () => {
    // The terminal printed confidence on a measured class line and the modal
    // printed none, so a reader moving between the surfaces saw the fact on
    // only one of them. A WITHHELD class still states none on both: confidence
    // is a statement about a measurement, and it made none.
    const mixed = report({
      results: [{
        ...report().results[0],
        contributors: [],
        classes: [
          {
            contributorClass: 'model_mix',
            verdict: 'no_contributor',
            code: null,
            supportShortfall: null,
            confidence: 'medium',
            population: coverage(),
            rows: [],
          },
          {
            contributorClass: 'project_concentration',
            verdict: 'withheld',
            code: 'insufficient_population',
            supportShortfall: null,
            confidence: null,
            population: coverage(),
            rows: [],
          },
        ],
      }],
    });
    stubFetch(mixed);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.querySelectorAll('.explain-class').length).toBe(2);
    });
    const measured = container.querySelector('.explain-class[data-class="model_mix"]');
    const withheldLine = container
      .querySelector('.explain-class[data-class="project_concentration"]');
    expect(measured?.textContent).toContain('confidence medium');
    expect(measured?.textContent).toContain('cost coverage 100%');
    expect(withheldLine?.textContent).not.toContain('confidence');
    // The withheld line still states the population it shaped, which IS a
    // measurement, so the two surfaces still agree there.
    expect(withheldLine?.textContent).toContain('support 60 units');
  });
});

// ─── the published rule, applied with the published constants ───────────────

describe('#620 S2 — the contributor floor is the PUBLISHED rule', () => {
  it('treats a float-ragged one fifth as a contributor', () => {
    // The `model-dominant` golden ships exactly this share beside
    // `verdict: "contributor"`. A client applying a bare `share >= 0.2` to it
    // computes `no_contributor` and contradicts the verdict on that row.
    expect(0.19999999999999998 >= 0.2).toBe(false);
    expect(reachesContributorFloor(0.19999999999999998, CONSTANTS)).toBe(true);
  });

  it('still refuses a share genuinely below the floor', () => {
    expect(reachesContributorFloor(0.1999, CONSTANTS)).toBe(false);
  });

  it('refuses a withheld share rather than treating null as zero', () => {
    expect(reachesContributorFloor(null, CONSTANTS)).toBe(false);
  });

  it('badges the ragged row on the rendered surface', async () => {
    const ragged = report({
      results: [{
        ...report().results[0],
        contributors: [{
          ...report().results[0].contributors[0],
          share: available(0.19999999999999998),
        }],
      }],
    });
    stubFetch(ragged);
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    await waitFor(() => {
      expect(container.querySelector('.explain-chip-contributor')).not.toBeNull();
    });
  });
});

// ─── the URL contract ───────────────────────────────────────────────────────

describe('#620 S2 — diagnosisUrl', () => {
  it('omits the bounds entirely when the modal asks about the current window', () => {
    const url = diagnosisUrl({
      source: 'claude', accountKey: null, windowStartAt: null, windowEndAt: null,
    });
    expect(url).toBe('/api/diagnosis?source=claude');
  });

  it('sends bounds only when BOTH are known', () => {
    const url = diagnosisUrl({
      source: 'codex', accountKey: null,
      windowStartAt: '2026-08-10T00:00:00Z', windowEndAt: null,
    });
    expect(url).not.toContain('start_at');
  });
});

describe('#620 S2 — withheld copy', () => {
  it('names every server-closed cause', () => {
    for (const code of [
      'provider_unavailable', 'retained_range_mismatch', 'pricing_unavailable',
      'unattributed_evidence', 'stale_evidence', 'insufficient_population',
      'calculation_failed', 'baseline_insufficient',
    ]) {
      expect(diagnosisWithheldMessage(code)).not.toContain(code);
    }
  });

  it('carries an unknown cause verbatim in its generic copy', () => {
    // The exemplar has to be a code the server does not publish. It was
    // `signal_unavailable` until #620 S3 added that cause, at which point this
    // test stopped exercising the fallback and started asserting the new
    // branch's copy — which is exactly the way a fallback gate goes quiet.
    expect(diagnosisWithheldMessage('a_cause_from_a_newer_server'))
      .toContain('a_cause_from_a_newer_server');
  });
});


// ─── #620 S3 — the conversation-derived classes on this surface ─────────────
//
// Two new vocabularies arrive with them, both closed on the server and
// deliberately open here: the in-place update path lets an old client meet a
// newer server without reloading the JavaScript, so each mapper carries a
// REQUIRED fallback branch that names the code it did not recognise.
//
// What JSDOM can decide is asserted here: which element carries which text,
// that a withheld member prints its cause rather than a zero, and that the
// evidence is in the document rather than in a `title` attribute a touch
// device never reveals. Real `@media` at 390px is the browser gate's.

const S3_RULE = {
  sentence: 'Conversations with at most 3 human turns where at least one '
    + 'request used at least 80% of that request\'s model context window.',
  parameters: { maxHumanTurns: 3, minWindowFraction: 0.8 },
};

const S3_CONSTANTS = {
  ...CONSTANTS,
  shortConversationMaxHumanTurns: 3,
  largeContextMinWindowFraction: 0.8,
  minSubagentBuckets: 2,
  gapCodes: ['ambiguous_origin_category', 'scan_budget_exhausted',
    'unknown_context_window', 'unresolved_subagent_attribution'],
  turnDefinition: 'Turn definition: main-thread human messages only.',
  registry: [
    ...CONSTANTS.registry,
    {
      contributorClass: 'short_high_context',
      subjectKind: 'qualifying_set',
      label: 'Short conversations carrying large context',
      minDistinctSubjects: 1,
      minPricedEntries: 20,
      rule: S3_RULE,
    },
    {
      contributorClass: 'subagent_fanout',
      subjectKind: 'qualifying_set',
      label: 'Subagent fan-out',
      minDistinctSubjects: 1,
      minPricedEntries: 20,
      rule: { sentence: 'Cost attributable to delegated subagent work.',
        parameters: { minBuckets: 2 } },
    },
  ],
};

function s3Report(over: Record<string, unknown> = {}) {
  const base = report() as Record<string, unknown>;
  const result = (base.results as Record<string, unknown>[])[0];
  return {
    ...base,
    constants: S3_CONSTANTS,
    results: [{
      ...result,
      contributors: [
        ...(result.contributors as unknown[]),
        {
          contributorClass: 'short_high_context',
          subjectKind: 'qualifying_set',
          subjectKey: 'qualifying-set/short-high-context',
          subjectLabel: 'Short conversations carrying large context',
          rank: 2,
          observedUsd: available(24.1),
          share: available(0.2),
          baseline: withheld('baseline_insufficient'),
          confidence: 'high',
          isFallbackPricing: false,
          nextStep: 'cctally explain --source claude '
            + '--start-at 2026-08-10T00:00:00Z --end-at 2026-08-17T00:00:00Z '
            + '--json',
          evidence: {
            conversationCount: available(4),
            medianHumanTurns: withheld('insufficient_population'),
            maxContextWindowFraction: available(0.905),
          },
        },
      ],
      classes: [
        ...(result.classes as unknown[]),
        {
          contributorClass: 'subagent_fanout',
          verdict: 'withheld',
          code: 'transcripts_not_visible',
          supportShortfall: null,
          confidence: null,
          population: coverage({
            gapCodes: ['ambiguous_origin_category'],
            evaluabilityCoverage: 0.75,
          }),
          rows: [],
        },
      ],
    }],
    ...over,
  };
}

describe('#620 S3 — the two open vocabularies', () => {
  it('names both causes S3 added, and leaves the asymmetry to its own note',
    () => {
      const denied = diagnosisWithheldMessage('transcripts_not_visible');
      expect(denied).toContain('not authorized');
      // The asymmetry clause used to end this string, so it rendered under
      // every class denied for this cause on every report. It explains a
      // contrast between two providers, and this mapper is handed one code
      // and nothing else, so it cannot tell whether the report contains one.
      expect(denied).not.toContain('Codex subagent attribution');
      expect(TRANSCRIPT_ASYMMETRY_NOTE).toContain('Codex subagent attribution');
      expect(diagnosisWithheldMessage('signal_unavailable'))
        .toContain('transcript store');
    });

  it('names every gap code the server publishes today', () => {
    for (const code of S3_CONSTANTS.gapCodes) {
      expect(gapCodeMessage(code)).not.toBe('');
      expect(gapCodeMessage(code)).not.toContain(code);
    }
  });

  it('renders an unrecognised GAP code through a required fallback', () => {
    // R11's twin: Step 3.5.1 required a fallback for causes only, so a gap
    // code from a newer server would have rendered as nothing at all.
    expect(gapCodeMessage('a_gap_from_a_newer_server'))
      .toContain('a_gap_from_a_newer_server');
    expect(gapCodeMessage(null)).toBe('');
  });

  it('renders each evidence figure in the unit its NAME carries', () => {
    expect(evidenceValue('estWastedUsd', available(1.5) as never)).toBe('$1.50');
    expect(evidenceValue('unallocatedUsd', available(0) as never)).toBe('$0.00');
    expect(evidenceValue('largestSubagentShare', available(0.75) as never))
      .toBe('75.0%');
    expect(evidenceValue('maxContextWindowFraction', available(0.905) as never))
      .toBe('90.5%');
    expect(evidenceValue('flaggedTurnCount', available(12) as never)).toBe('12');
  });

  it('returns null for a withheld member rather than a zero', () => {
    expect(evidenceValue('medianHumanTurns',
      withheld('insufficient_population') as never)).toBeNull();
    expect(evidenceValue('estWastedUsd', undefined)).toBeNull();
  });

  it('labels every published figure and falls back to the raw name', () => {
    for (const name of ['flaggedTurnCount', 'affectedConversationCount',
      'estWastedUsd', 'conversationCount', 'medianHumanTurns',
      'maxContextWindowFraction', 'identifiedSubagentCount',
      'largestSubagentShare', 'unallocatedUsd']) {
      expect(evidenceLabel(name)).not.toBe(name);
    }
    expect(evidenceLabel('aFigureFromANewerServer'))
      .toBe('aFigureFromANewerServer');
  });
});

describe('#620 S3 — the modal renders the new evidence', () => {
  it('renders each evidence figure in the document, never in a title', async () => {
    stubFetch(s3Report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const evidence = await waitFor(() => {
      const node = container.querySelector('.explain-evidence');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    expect(evidence.textContent).toContain('qualifying conversations');
    expect(evidence.textContent).toContain('4');
    expect(evidence.textContent).toContain('90.5%');
    // R9: nothing on this row depends on a hover.
    expect(container.querySelectorAll('.explain-row [title]')).toHaveLength(0);
  });

  it('prints the cause of a withheld member rather than a zero', async () => {
    stubFetch(s3Report());
    openExplain();
    const { container } = render(<ModalRoot />);
    await screen.findByRole('dialog');
    const evidence = await waitFor(() => {
      const node = container.querySelector('.explain-evidence');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    expect(evidence.textContent).toContain('median human turns');
    const member = evidence
      .querySelector('[data-evidence="medianHumanTurns"]') as HTMLElement;
    expect(member.textContent)
      .toContain(evidenceWithheldMessage('insufficient_population'));
    expect(member.textContent).not.toContain('0');
    // The CLASS sentence says the class was not measured. The browser gate
    // read it beneath one withheld member of a row ranked #1 that reported
    // $7.21, a 91.3% share and six qualifying conversations, so the sentence
    // contradicted the row it sat on. A member is withheld while its class is
    // measured, and it states that in its own words.
    expect(member.textContent)
      .not.toContain(diagnosisWithheldMessage('insufficient_population'));
    expect(member.textContent).toContain('this figure');
  });

  it('states the published rule from the registry rather than a local copy',
    async () => {
      stubFetch(s3Report());
      openExplain();
      render(<ModalRoot />);
      await screen.findByRole('dialog');
      expect(await screen.findAllByText(S3_RULE.sentence)).not.toHaveLength(0);
      expect(screen.getByText(S3_CONSTANTS.turnDefinition)).toBeInTheDocument();
    });

  it('states why rows could not be decided, and the decidable share',
    async () => {
      stubFetch(s3Report());
      openExplain();
      const { container } = render(<ModalRoot />);
      await screen.findByRole('dialog');
      const gaps = await waitFor(() => {
        const node = container.querySelector('.explain-gaps');
        expect(node).not.toBeNull();
        return node as HTMLElement;
      });
      expect(gaps.textContent)
        .toContain(gapCodeMessage('ambiguous_origin_category'));
      expect(container.textContent).toContain('decidable 75%');
      // The lead-in the terminal writes (`undecided: unknown_context_window`),
      // written ONCE over the joined list. A recognised code rendered without
      // it, and the gap line sits directly under the coverage line at the same
      // size and family, so the word is the only non-colour cue between them.
      expect((gaps.textContent ?? '').trim().startsWith('undecided:'))
        .toBe(true);
      // ...and it is not repeated per code, so two gap codes read as one
      // statement rather than two.
      expect(gapCodeMessage('ambiguous_origin_category'))
        .not.toContain('undecided');
      expect(gapCodeMessage('a_gap_from_a_newer_server'))
        .not.toContain('undecided');
    });

  it('states a qualification once per row, and never drops an unstated one',
    async () => {
      // The browser gate read `identifiable_subset_unknown_completeness` twice
      // in one row — once in the row's own meta line from
      // `observedUsd.qualifications`, once under `IDENTIFIED SUBAGENTS` from
      // the field's — with the class rule directly beneath both stating the
      // same fact in English. The row states it; the figures do not repeat it.
      const base = s3Report() as Record<string, unknown>;
      const result = (base.results as Record<string, unknown>[])[0];
      const subset = 'identifiable_subset_unknown_completeness';
      stubFetch({
        ...base,
        results: [{
          ...result,
          contributors: [
            ...(result.contributors as unknown[]),
            {
              contributorClass: 'subagent_fanout',
              subjectKind: 'qualifying_set',
              subjectKey: 'qualifying-set/subagent-fanout',
              subjectLabel: 'Delegated subagent work',
              rank: 3,
              observedUsd: available(12.0, { qualifications: [subset] }),
              share: available(0.2),
              baseline: withheld('baseline_insufficient'),
              confidence: 'high',
              isFallbackPricing: false,
              nextStep: 'cctally explain --source codex --json',
              evidence: {
                identifiedSubagentCount: available(2,
                  { qualifications: [subset] }),
                largestSubagentShare: available(0.5,
                  { qualifications: ['session_level_context_window'] }),
                unallocatedUsd: available(0),
              },
            },
          ],
        }],
      });
      openExplain();
      const { container } = render(<ModalRoot />);
      await screen.findByRole('dialog');
      const row = await waitFor(() => {
        const node = container
          .querySelector('.explain-row[data-class="subagent_fanout"]');
        expect(node).not.toBeNull();
        return node as HTMLElement;
      });
      expect((row.textContent ?? '').split(subset).length - 1).toBe(1);
      // The surviving one is the ROW's, which is the statement that covers
      // the observed cost the row is ranked by.
      expect(row.querySelector('.explain-row-meta')?.textContent)
        .toContain(subset);
      // A qualification the row did NOT state still renders on its own
      // figure: this drops duplicates, never qualifications.
      expect(row.querySelector('[data-evidence="largestSubagentShare"]')
        ?.textContent).toContain('session_level_context_window');
    });

  it('presents generation_incoherent as a retryable state, not a failure',
    async () => {
      // A component that moved twice while it was read is a store under active
      // write rather than a broken one, so the surface offers the same request
      // again instead of reporting a transport failure.
      const fetchMock = vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: 'the conversations component changed twice '
          + 'while it was read', code: 'generation_incoherent' }),
      }));
      vi.stubGlobal('fetch', fetchMock);
      openExplain();
      const { container } = render(<ModalRoot />);
      await screen.findByRole('dialog');
      const button = await waitFor(() => {
        const node = container.querySelector('.explain-retry');
        expect(node).not.toBeNull();
        return node as HTMLButtonElement;
      });
      // A real <button>, so it is in the tab order and activates on Enter and
      // Space without the modal's absent panel-focus flow (R7).
      expect(button.tagName).toBe('BUTTON');
      expect(container.textContent).toContain('Nothing is wrong with the data');
      const before = fetchMock.mock.calls.length;
      fireEvent.click(button);
      await waitFor(() => {
        expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
      });
    });
});


// ─── #620 S3 browser round 2 — three findings the earlier tests could not see ─
//
// Each one was invisible to the tests above for a reason worth stating, because
// the same blind spot would hide the next instance:
//
//   * the gap-line tests only ever supplied codes the client RECOGNISES, so
//     the fallback branch was exercised as a pure function and never on the
//     surface, where the two missing guards live;
//   * the retry test asserted that a second request was made and stopped
//     there, which is true of a control nobody can reach a second time;
//   * the asymmetry test asserted the sentence was IN the message, which is
//     the behaviour the finding is about.

/** One provider section carrying exactly the classes given, plus any further
 *  provider sections. The base report's own contributors are dropped so a
 *  class line is the only thing under test. */
function classesReport(classes: unknown[], extraResults: unknown[] = []) {
  const base = s3Report() as Record<string, unknown>;
  const result = (base.results as Record<string, unknown>[])[0];
  return {
    ...base,
    results: [
      { ...result, contributors: [], classes },
      ...extraResults,
    ],
  };
}

/** A Codex provider section that MEASURED the subagent fan-out class — the
 *  other half of the contrast the transcript note describes. */
function codexFanoutMeasured() {
  const base = s3Report() as Record<string, unknown>;
  const result = (base.results as Record<string, unknown>[])[0];
  return {
    ...result,
    source: 'codex',
    contributors: [{
      contributorClass: 'subagent_fanout',
      subjectKind: 'qualifying_set',
      subjectKey: 'qualifying-set/subagent-fanout',
      subjectLabel: 'Delegated subagent work',
      rank: 1,
      observedUsd: available(9.0),
      share: available(0.3),
      baseline: withheld('baseline_insufficient'),
      confidence: 'high',
      isFallbackPricing: false,
      nextStep: 'cctally explain --source codex --json',
      evidence: {},
    }],
    classes: [],
  };
}

async function openWith(payload: unknown) {
  stubFetch(payload);
  openExplain();
  const rendered = render(<ModalRoot />);
  await screen.findByRole('dialog');
  return rendered;
}

function classLine(container: HTMLElement, kind: string) {
  return waitFor(() => {
    const node = container.querySelector(`.explain-class[data-class="${kind}"]`);
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
}

describe('#620 S3 R2-A — the gap line states only what it is entitled to', () => {
  it('publishes no undecided line for a class that cannot leave a row undecided',
    async () => {
      // `unresolved_project_identity` belongs to an ACCOUNTING class and is
      // not in `constants.gapCodes`, so the modal rendered it through the
      // unrecognised-code fallback as an undecided row. The terminal never
      // prints it: `_publishes_gaps` restricts the line to the three
      // conversation-derived classes, which are the only ones whose predicate
      // can fail to decide a row.
      const { container } = await openWith(classesReport([{
        contributorClass: 'project_concentration',
        verdict: 'withheld',
        code: 'unattributed_evidence',
        supportShortfall: null,
        confidence: null,
        population: coverage({ gapCodes: ['unresolved_project_identity'] }),
        rows: [],
      }]));
      const line = await classLine(container, 'project_concentration');
      expect(line.querySelector('.explain-gaps')).toBeNull();
      expect(line.textContent).not.toContain('unresolved_project_identity');
      // The class still states its cause and its population.
      expect(line.textContent)
        .toContain(diagnosisWithheldMessage('unattributed_evidence'));
      expect(line.querySelector('.explain-coverage')).not.toBeNull();
    });

  it('never repeats a class’s own cause as an undecided reason', async () => {
    // The server writes the cause FIRST into `gap_codes`, so a class withheld
    // as `signal_unavailable` carries that code in both places. The modal
    // printed the cause in English and then the same code one line later,
    // through the unrecognised-code fallback — `signal_unavailable` is a
    // CAUSE, not a gap code this client maps. `_gap_phrase(exclude=…)` is the
    // guard the terminal applies and the modal did not.
    const { container } = await openWith(classesReport([{
      contributorClass: 'subagent_fanout',
      verdict: 'withheld',
      code: 'signal_unavailable',
      supportShortfall: null,
      confidence: null,
      population: coverage({
        gapCodes: ['signal_unavailable', 'unresolved_subagent_attribution'],
      }),
      rows: [],
    }]));
    const line = await classLine(container, 'subagent_fanout');
    expect(line.textContent)
      .toContain(diagnosisWithheldMessage('signal_unavailable'));
    expect(line.textContent).not.toContain('signal_unavailable');
    // Only the duplicate is dropped. A second, genuinely undecided reason
    // still states itself.
    const gaps = line.querySelector('.explain-gaps') as HTMLElement;
    expect(gaps).not.toBeNull();
    expect(gaps.textContent)
      .toContain(gapCodeMessage('unresolved_subagent_attribution'));
  });

  it('names an unrecognised gap code without denying that it can name it',
    () => {
      const message = gapCodeMessage('a_gap_from_a_newer_server');
      expect(message).toContain('a_gap_from_a_newer_server');
      // It read `a cause this build cannot name (<code>)` while displaying
      // the code, beneath a line where the same build had already stated that
      // code's meaning in English.
      expect(message).not.toMatch(/cannot name/);
    });
});

describe('#620 S3 R2-B — the retry stays reachable from the keyboard', () => {
  it('returns focus to the retry control after the request fails again',
    async () => {
      const fetchMock = vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: 'the conversations component changed twice '
          + 'while it was read', code: 'generation_incoherent' }),
      }));
      vi.stubGlobal('fetch', fetchMock);
      openExplain();
      const { container } = render(<ModalRoot />);
      await screen.findByRole('dialog');
      const first = await waitFor(() => {
        const node = container.querySelector('.explain-retry');
        expect(node).not.toBeNull();
        return node as HTMLButtonElement;
      });
      first.focus();
      expect(document.activeElement).toBe(first);
      const before = fetchMock.mock.calls.length;
      fireEvent.click(first);
      await waitFor(() => {
        expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
      });
      // The hook sets `loading` before the refetch, which unmounts the error
      // block and this button with it; the button that returns is a new
      // element. Nothing restored focus to it, so a keyboard user had to Tab
      // back to the control they had just pressed — which is how the browser
      // gate came to record the retry as doing nothing.
      const second = await waitFor(() => {
        const node = container.querySelector('.explain-retry');
        expect(node).not.toBeNull();
        return node as HTMLButtonElement;
      });
      await waitFor(() => {
        expect(document.activeElement).toBe(second);
      });
    });

  it('leaves focus inside the dialog when the retry succeeds', async () => {
    // The sibling test above covers the retry that fails AGAIN, where the
    // button returns and can take focus back. The retry that SUCCEEDS renders
    // no button at all, so the element holding focus is removed and focus
    // falls to document.body. That matters beyond the missing highlight: the
    // Tab-trap in useModalFocus returns early when document.activeElement is
    // outside the card, so from body the next Tab leaves the dialog entirely
    // and reaches the page skip link while the card is still aria-modal.
    let call = 0;
    const fetchMock = vi.fn(async () => {
      call += 1;
      if (call === 1) {
        return {
          ok: false,
          status: 503,
          json: async () => ({ error: 'the conversations component changed '
            + 'twice while it was read', code: 'generation_incoherent' }),
        };
      }
      return { ok: true, status: 200, json: async () => report() };
    });
    vi.stubGlobal('fetch', fetchMock);
    openExplain();
    const { container } = render(<ModalRoot />);
    const dialog = await screen.findByRole('dialog');
    const button = await waitFor(() => {
      const node = container.querySelector('.explain-retry');
      expect(node).not.toBeNull();
      return node as HTMLButtonElement;
    });
    button.focus();
    expect(document.activeElement).toBe(button);
    fireEvent.click(button);
    await waitFor(() => {
      expect(container.querySelector('.explain-retry')).toBeNull();
    });
    const active = document.activeElement as HTMLElement | null;
    expect(active).not.toBe(document.body);
    expect(active == null ? false : dialog.contains(active)).toBe(true);
  });
});

describe('#620 S3 R2-C — the asymmetry note appears only where it is true',
  () => {
    it('omits it when no other provider measured the class', async () => {
      // A Claude-only report has no Codex row for the sentence to contrast
      // with, so it described a comparison that was not on the screen.
      const { container } = await openWith(classesReport([{
        contributorClass: 'subagent_fanout',
        verdict: 'withheld',
        code: 'transcripts_not_visible',
        supportShortfall: null,
        confidence: null,
        population: coverage(),
        rows: [],
      }]));
      const line = await classLine(container, 'subagent_fanout');
      expect(line.textContent)
        .toContain(diagnosisWithheldMessage('transcripts_not_visible'));
      expect(container.textContent).not.toContain(TRANSCRIPT_ASYMMETRY_NOTE);
      expect(container.textContent).not.toContain('Codex subagent attribution');
    });

    it('states it when another provider did measure the class', async () => {
      const { container } = await openWith(classesReport(
        [{
          contributorClass: 'subagent_fanout',
          verdict: 'withheld',
          code: 'transcripts_not_visible',
          supportShortfall: null,
          confidence: null,
          population: coverage(),
          rows: [],
        }],
        [codexFanoutMeasured()],
      ));
      const line = await classLine(container, 'subagent_fanout');
      expect(line.textContent).toContain(TRANSCRIPT_ASYMMETRY_NOTE);
    });

    it('leaves it off a class it says nothing about, asymmetry or not',
      async () => {
        // `cache_churn` is denied for the same cause on the same report, and
        // the sentence is about subagent attribution. It was rendered there
        // because it was part of the cause's own message.
        const { container } = await openWith(classesReport(
          [
            {
              contributorClass: 'subagent_fanout',
              verdict: 'withheld',
              code: 'transcripts_not_visible',
              supportShortfall: null,
              confidence: null,
              population: coverage(),
              rows: [],
            },
            {
              contributorClass: 'cache_churn',
              verdict: 'withheld',
              code: 'transcripts_not_visible',
              supportShortfall: null,
              confidence: null,
              population: coverage(),
              rows: [],
            },
          ],
          [codexFanoutMeasured()],
        ));
        const churn = await classLine(container, 'cache_churn');
        expect(churn.textContent)
          .toContain(diagnosisWithheldMessage('transcripts_not_visible'));
        expect(churn.textContent).not.toContain('Codex subagent attribution');
        // ...while the class it IS about still carries it.
        const fanout = await classLine(container, 'subagent_fanout');
        expect(fanout.textContent).toContain(TRANSCRIPT_ASYMMETRY_NOTE);
      });
  });
