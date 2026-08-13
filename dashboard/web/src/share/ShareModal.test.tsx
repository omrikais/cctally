// #293 S4 SHARE-1 gave the phone Share form a preview-FIRST render reorder,
// because `.share-preview-col` was nested inside `.share-main-section` — a
// separate flex parent from the sibling gallery — so a CSS `order` could not
// hoist it and a useIsMobile()-gated render reorder was required.
//
// #503 S4 §4.4 removed that constraint. The body is now ONE flat flex
// container holding the gallery, knobs and preview as siblings, so a single
// `<PreviewPane>` is placed by `order` at both breakpoints. The mutually
// exclusive DOM-order cases are therefore gone: what replaces them asserts one
// instance and that a viewport flip preserves it, which is the #520 item 2
// defect (the flip remounted the pane, discarded its fetched render, and
// reverted the privacy line for ~1.2s).
//
// JSDOM cannot evaluate `order` or the @media 16px / 44px rules — those are
// the ui-qa / hasTouch Playwright gate. Instance identity across a flip is the
// real, non-vacuous thing to assert here.
import { render, act, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ShareModalRoot } from './ShareModalRoot';
import { _resetForTests, dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

function stubMatchMedia(mobile: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: mobile ? q === MOBILE_MEDIA_QUERY : false,
    media: q,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(JSON.stringify({ presets: {} }), { status: 200 }));
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{
          id: 'weekly-recap',
          label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        }],
      }),
    });
  }));
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  stubFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// #503 S1 B1 — a fetch stub whose /api/share/render response carries the
// `has_project_names` flag, so the third status-line state can be exercised.
// The default `stubFetch` answers every non-preset URL with the template list,
// which has no such key — that is the "older server / not known yet" path, and
// the two original states must survive it unchanged.
function stubFetchWithRender(hasProjectNames: boolean | undefined) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(JSON.stringify({ presets: {} }), { status: 200 }));
    }
    if (typeof url === 'string' && url.includes('/api/share/render')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          body: '# Weekly\n',
          content_type: 'text/markdown',
          ...(hasProjectNames === undefined
            ? {}
            : { has_project_names: hasProjectNames }),
          snapshot: {
            kernel_version: '1', panel: 'weekly', template_id: 'weekly-recap',
            options: {}, generated_at: '2026-05-09T12:00:00Z', data_digest: 'd',
          },
        }),
      });
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{
          id: 'weekly-recap',
          label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        }],
      }),
    });
  }));
}

async function openShare() {
  render(<ShareModalRoot />);
  await act(async () => {
    dispatch(openShareModal('weekly', null));
  });
}

const FOLLOWING = 4; // Node.DOCUMENT_POSITION_FOLLOWING

// #503 S4 §4.4 / #520 item 2 — a flippable matchMedia. `useIsMobile` reads
// `mql.matches` in an effect and then follows `change` events, so a plain
// re-stub cannot move it: the test has to deliver the event the hook listens
// for. Without that, a "viewport flip" case would assert nothing.
function stubFlippableMatchMedia(initialMobile: boolean) {
  const listeners = new Set<(e: { matches: boolean }) => void>();
  let mobile = initialMobile;
  vi.stubGlobal('matchMedia', (q: string) => ({
    get matches() { return mobile && q === MOBILE_MEDIA_QUERY; },
    media: q,
    onchange: null,
    addEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => {
      if (q === MOBILE_MEDIA_QUERY) listeners.add(cb);
    },
    removeEventListener: (_t: string, cb: (e: { matches: boolean }) => void) => {
      listeners.delete(cb);
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
  return {
    flip(next: boolean) {
      mobile = next;
      listeners.forEach((cb) => cb({ matches: next }));
    },
    listenerCount() { return listeners.size; },
  };
}

describe('#503 S4 §4.4 / #520 item 2 — one PreviewPane, placed by CSS', () => {
  // These replace the #293 S4 cases that asserted mobile and desktop render
  // DIFFERENT preview instances in different DOM positions. That premise is
  // exactly what this session removes: the body is now one flat flex container
  // and the single instance is placed by `order`, which jsdom cannot evaluate.
  it('renders exactly one preview pane at either viewport', async () => {
    stubMatchMedia(true);
    await openShare();
    expect(document.querySelectorAll('.share-preview-col').length).toBe(1);
    cleanup();
    stubMatchMedia(false);
    await openShare();
    expect(document.querySelectorAll('.share-preview-col').length).toBe(1);
  });

  it('a viewport flip preserves the preview instance and never reverts the privacy line', async () => {
    // `has_project_names: false` puts the privacy line in its neutral state.
    // A remount resets the reported answer to `null`, which renders the
    // DEFAULT wording again — that visible revert is the #520 item 2 defect.
    stubFetchWithRender(false);
    const media = stubFlippableMatchMedia(true);
    await openShare();
    await waitFor(() => {
      expect(document.body.textContent)
        .toContain('This export contains no project names.');
    });
    const before = document.querySelector('.share-preview-col');
    const previewBefore = document.querySelector('.share-preview');
    expect(previewBefore).not.toBeNull();
    const renderCallsBefore = (fetch as ReturnType<typeof vi.fn>).mock.calls
      .filter((c) => String(c[0]).includes('/api/share/render')).length;
    expect(renderCallsBefore).toBeGreaterThan(0);   // guards the count assertion

    // What this test actually guards NOW. §4.4 made the PREVIEW's placement
    // CSS-only, so the identity assertions below hold trivially against the
    // FIXED code — they are kept because they were the RED discriminator
    // against the broken code (two instances at different tree positions, so
    // a flip built new nodes). The live invariant is this count.
    //
    // Exactly ONE subscriber, and it is <ComposerModal>: ShareModalRoot mounts
    // it unconditionally as a sibling slot and it calls useIsMobile() to pick
    // its own layout. Nothing on the share-preview path subscribes any more.
    // The literal is hardcoded so that adding a JS viewport branch anywhere
    // under <ShareModalRoot> makes this 2 and fails here, instead of silently
    // reintroducing the remount this session removed. If ComposerModal ever
    // stops subscribing, this becomes 0 and fails just as loudly — either way
    // the change gets read rather than absorbed.
    expect(media.listenerCount()).toBe(1);

    await act(async () => { media.flip(false); });

    // Node IDENTITY, not mere presence: a remount would satisfy a presence
    // assertion while still discarding the fetched state.
    expect(document.querySelector('.share-preview-col')).toBe(before);
    expect(document.querySelector('.share-preview')).toBe(previewBefore);
    expect(
      (fetch as ReturnType<typeof vi.fn>).mock.calls
        .filter((c) => String(c[0]).includes('/api/share/render')).length,
    ).toBe(renderCallsBefore);
    expect(document.body.textContent)
      .toContain('This export contains no project names.');
    expect(document.body.textContent)
      .not.toContain('Preview shows real names.');
  });

  it('desktop preview renders the source captured when the share flow opened', async () => {
    stubMatchMedia(false);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    await openShare();

    await waitFor(() => {
      const renderCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
        ([url]) => typeof url === 'string' && url.includes('/api/share/render'),
      );
      expect(renderCall).toBeDefined();
      const body = JSON.parse((renderCall?.[1] as RequestInit).body as string);
      expect(body.source).toBe('codex');
    });
  });

  it('desktop preview keeps the account captured when the share flow opened', async () => {
    stubMatchMedia(false);
    const captured = 'b'.repeat(32);
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch({
        type: 'OPEN_SHARE', panel: 'weekly', triggerId: null,
        source: 'codex', account: captured,
      });
      // The global selector can move while the modal is open. The request must
      // remain qualified by the flow's frozen account, not this later value.
      dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: 'a'.repeat(32) });
    });

    await waitFor(() => {
      const renderCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
        ([url]) => typeof url === 'string' && url.includes('/api/share/render'),
      );
      expect(renderCall).toBeDefined();
      const body = JSON.parse((renderCall?.[1] as RequestInit).body as string);
      expect(body.account).toBe(captured);
    });
  });

  it('does not claim an account scope for Claude panels that remain unfiltered', async () => {
    stubMatchMedia(false);
    render(<ShareModalRoot />);
    await act(async () => {
      dispatch({
        type: 'OPEN_SHARE', panel: 'weekly', triggerId: null,
        source: 'claude', account: 'b'.repeat(32),
      });
    });
    await waitFor(() => {
      const renderCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
        ([url]) => typeof url === 'string' && url.includes('/api/share/render'),
      );
      expect(renderCall).toBeDefined();
      const body = JSON.parse((renderCall?.[1] as RequestInit).body as string);
      expect(body).not.toHaveProperty('account');
    });
  });
});

// #503 S1 F6 — the modal must answer its own question.
//
// Three correct-in-isolation decisions combine into a modal that cannot:
// <PreviewPane> forces `reveal_projects: true` (correct per spec §6.3),
// <ShareModal> defaults the option to `false` (correct), and <Knobs> binds a
// checkbox to it (correct). The result is a checkbox that changes nothing
// visible, next to a preview that always shows real names, on a surface
// documented as the place to review what you are about to share.
//
// Asserted at MODAL level, exercising the real modal rather than an extracted
// helper, because the defect is precisely that the modal's individually
// correct pieces disagree.
describe('#503 S1 F6 — the export anonymization status line', () => {
  function statusLine() {
    return document.querySelector('.share-privacy-status');
  }

  it('states that the export will be anonymized when Anon is checked', async () => {
    stubMatchMedia(false);
    await openShare();
    const line = statusLine();
    expect(line).not.toBeNull();
    expect(line!.getAttribute('role')).toBe('status');
    expect(line!.getAttribute('aria-live')).toBe('polite');
    expect(line!.textContent).toContain(
      'Preview shows real names. Export is anonymized.');
  });

  it('warns when the export will show real project names', async () => {
    stubMatchMedia(false);
    await openShare();
    const box = document.querySelector<HTMLInputElement>(
      'input[aria-label="Anonymize project names on export"]');
    expect(box).not.toBeNull();
    expect(box!.checked).toBe(true);
    await act(async () => {
      box!.click();
    });
    expect(statusLine()!.textContent).toContain(
      'Export will show real project names.');
  });

  it('is ALWAYS present and changes text rather than appearing', async () => {
    // A line that vanishes in the safe state says nothing, which is the
    // current defect with extra steps. The composer's conditional
    // `composer-anon-banner` is right there because it carries an
    // "Anonymize all" call to action; F6 asks for something different.
    stubMatchMedia(false);
    await openShare();
    expect(document.querySelectorAll('.share-privacy-status').length).toBe(1);
    const box = document.querySelector<HTMLInputElement>(
      'input[aria-label="Anonymize project names on export"]')!;
    await act(async () => { box.click(); });
    expect(document.querySelectorAll('.share-privacy-status').length).toBe(1);
    await act(async () => { box.click(); });
    expect(document.querySelectorAll('.share-privacy-status').length).toBe(1);
  });

  it('carries info severity when anonymized and warn severity when revealing', async () => {
    // Three-tier vocabulary from dashboard/design-system/components/
    // alert-severity.html. Info and warn, NEVER critical: revealing is a
    // legitimate deliberate choice, not an error.
    stubMatchMedia(false);
    await openShare();
    expect(statusLine()!.className).toContain('share-privacy-status--info');
    const box = document.querySelector<HTMLInputElement>(
      'input[aria-label="Anonymize project names on export"]')!;
    await act(async () => { box.click(); });
    expect(statusLine()!.className).toContain('share-privacy-status--warn');
    expect(statusLine()!.className).not.toContain('critical');
  });

  it('renders directly above the preview pane, in both layouts', async () => {
    // Placement is above the preview because the preview is the misleading
    // object.
    for (const mobile of [false, true]) {
      _resetForTests();
      _resetKeymap();
      installGlobalKeydown();
      stubFetch();
      stubMatchMedia(mobile);
      await openShare();
      const line = statusLine();
      // The line lives INSIDE the preview column, so compare it against the
      // preview's own root — comparing against the column would only report
      // containment.
      const preview = document.querySelector('.share-preview');
      expect(line).not.toBeNull();
      expect(preview).not.toBeNull();
      expect(line!.closest('.share-preview-col')).not.toBeNull();
      expect(line!.compareDocumentPosition(preview!) & FOLLOWING).toBeTruthy();
      document.body.innerHTML = '';
    }
  });
});


// #503 S1 B1 — the third, NEUTRAL state.
//
// The two-state line was unconditional, and on `trend`, `forecast` and every
// other template whose artifact carries no project name it made a FALSE
// statement: the reveal state said "Export will show real project names" while
// the artifact contained only metric rows, and the anonymize state claimed the
// preview showed real names when it showed none. This is an over-warning
// rather than a leak, so it cannot cause the defect class #503 S1 exists to
// prevent — it matters because a line whose entire value is trustworthiness
// must not be wrong, and a warning users learn to disregard on Forecast is one
// they may disregard on Projects.
//
// The state is derived from the server's `has_project_names`, which comes from
// the same snapshot the renderer used, NEVER from a hardcoded panel list — the
// property is per TEMPLATE, not per panel (`weekly-recap` carries names,
// `weekly-detail` does not).
describe('#503 S1 B1 — the neutral no-project-names state', () => {
  function statusLine() {
    return document.querySelector('.share-privacy-status');
  }

  async function anonCheckbox() {
    return document.querySelector<HTMLInputElement>(
      'input[aria-label="Anonymize project names on export"]')!;
  }

  it('states plainly that the export contains no project names', async () => {
    stubMatchMedia(false);
    stubFetchWithRender(false);
    await openShare();
    await waitFor(() => {
      expect(statusLine()!.textContent).toContain(
        'This export contains no project names.');
    });
  });

  it('does not change when the user toggles anonymize', async () => {
    // The sentence is true in both toggle positions, which is the whole point
    // of a neutral state: there is nothing for the toggle to change.
    stubMatchMedia(false);
    stubFetchWithRender(false);
    await openShare();
    await waitFor(() => {
      expect(statusLine()!.className).toContain('share-privacy-status--neutral');
    });
    const before = statusLine()!.textContent;
    await act(async () => { (await anonCheckbox()).click(); });
    await waitFor(() => {
      expect(statusLine()!.textContent).toBe(before);
      expect(statusLine()!.className).toContain('share-privacy-status--neutral');
    });
  });

  it('carries neither the warn nor the critical severity', async () => {
    stubMatchMedia(false);
    stubFetchWithRender(false);
    await openShare();
    await waitFor(() => {
      const cls = statusLine()!.className;
      expect(cls).toContain('share-privacy-status--neutral');
      expect(cls).not.toContain('warn');
      expect(cls).not.toContain('critical');
    });
  });

  it('keeps the two original states when the export DOES carry names', async () => {
    stubMatchMedia(false);
    stubFetchWithRender(true);
    await openShare();
    await waitFor(() => {
      expect(statusLine()!.textContent).toContain(
        'Preview shows real names. Export is anonymized.');
    });
    await act(async () => { (await anonCheckbox()).click(); });
    await waitFor(() => {
      expect(statusLine()!.textContent).toContain(
        'Export will show real project names.');
      expect(statusLine()!.className).toContain('share-privacy-status--warn');
    });
  });

  it('falls back to the two original states when the server does not say', async () => {
    // A server without the additive key, and the interval before the first
    // render resolves. Never claim "no project names" without evidence: an
    // absent flag is unknown, not false.
    stubMatchMedia(false);
    stubFetchWithRender(undefined);
    await openShare();
    await waitFor(() => {
      expect(statusLine()!.textContent).toContain(
        'Preview shows real names. Export is anonymized.');
    });
    expect(statusLine()!.className).not.toContain('neutral');
  });

  it('still renders exactly one line, in the mobile layout too', async () => {
    stubMatchMedia(true);
    stubFetchWithRender(false);
    await openShare();
    await waitFor(() => {
      expect(document.querySelectorAll('.share-privacy-status').length).toBe(1);
      expect(statusLine()!.textContent).toContain(
        'This export contains no project names.');
    });
  });
});

// #503 S1 B2 — the mobile preview budget.
//
// The mobile layout pins the status line above a bounded sticky preview strip
// (`.share-preview-col` inside the max-width:640px block), so every line the
// status text wraps to comes straight out of the preview. Measured in a real
// browser at 390x844 against the then-current 128px cap: the two-line info
// wording left 37px of preview against 91px with the line hidden, i.e. the line
// took 42% of the budget on the state the user sees first. One line in every
// state left 58px. #503 S4 §4.2 has since raised the cap to
// `min(50dvh, 380px)`, which widens the budget but does not retire this rule —
// a wrapped line still costs the preview whatever it takes.
//
// JSDOM cannot measure the wrap — that is the real-browser gate's job. What
// it CAN pin, and what actually caused the regression, is the character
// count: the client renders monospace throughout, and 47 characters is the
// most that fits one line at 12px in the 359px mobile column. This test is
// the tripwire on a future rewording; the browser gate confirms the pixels.
describe('#503 S1 B2 — status-line strings fit one mobile line', () => {
  const MAX_CHARS = 47; // measured at 390px, 12px monospace, 8px padding

  async function textFor(hasProjectNames: boolean | undefined, reveal: boolean) {
    // Unmount the previous iteration's tree. `render()` appends a new root to
    // document.body rather than replacing one, and `document.querySelector`
    // returns the FIRST match — so without this every iteration after the
    // first reads the first iteration's modal and the loop silently asserts
    // the same state four times.
    cleanup();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    stubMatchMedia(true);
    stubFetchWithRender(hasProjectNames);
    await openShare();
    if (reveal) {
      const box = document.querySelector<HTMLInputElement>(
        'input[aria-label="Anonymize project names on export"]')!;
      await act(async () => { box.click(); });
    }
    // Wait for the RENDER to resolve, not merely for the line to exist. The
    // preview debounces 200ms, and the server's `has_project_names` is
    // reported in the same `.then` that puts the preview into its ready
    // state — so reading the text before then reads the deliberate
    // not-known-yet fallback rather than the state under test. That fallback
    // is correct behavior, and it is what made an earlier version of this
    // test see the info wording where it expected the neutral one.
    await waitFor(() => {
      expect(document.querySelector(
        '.share-preview-md, .share-preview-iframe')).not.toBeNull();
    });
    return document.querySelector('.share-privacy-status')!.textContent!.trim();
  }

  it('every state renders a string within the one-line budget', async () => {
    const seen: string[] = [];
    for (const [flag, reveal] of [
      [true, false], [true, true], [false, false], [false, true],
    ] as [boolean, boolean][]) {
      const text = await textFor(flag, reveal);
      seen.push(text);
      expect(text.length,
        `"${text}" is ${text.length} chars; over ${MAX_CHARS} it wraps to a `
        + 'second line and costs 17px of the mobile preview').toBeLessThanOrEqual(MAX_CHARS);
    }
    // Non-vacuity: the loop must have produced the three distinct states, or
    // the budget assertion could be passing on one short string four times.
    expect(new Set(seen).size, JSON.stringify(seen)).toBe(3);
  });
});


// #503 S3 §5 — the user learns that something failed BEFORE they export.
//
// Failures were never silent — every action writes an inline role="alert"
// banner AFTER the click. What was missing is a signal before it. Preview
// failure lived in <PreviewPane>'s local state and never reached <ActionBar>.
describe('#503 S3 §5 — the failed-preview note', () => {
  function stubFetchWithFailedRender() {
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
      if (typeof url === 'string' && url.includes('/api/share/presets')) {
        return Promise.resolve(
          new Response(JSON.stringify({ presets: {} }), { status: 200 }));
      }
      if (typeof url === 'string' && url.includes('/api/share/render')) {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: async () => ({ error: 'source render failed' }),
        });
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({
          panel: 'weekly',
          templates: [{
            id: 'weekly-recap', label: 'Recap', description: 'Text',
            default_options: { format: 'md', theme: 'light' },
          }],
        }),
      });
    }));
  }

  const NOTE = 'The preview failed — an export may fail too.';

  function note() {
    return document.querySelector('.share-preview-failed-note');
  }

  function exportButtons() {
    return Array.from(document.querySelectorAll<HTMLButtonElement>(
      '.share-action-copy, .share-action-download, .share-action-open,'
      + ' .share-action-png, .share-action-print'));
  }

  it('surfaces the note and leaves EVERY action clickable', async () => {
    stubMatchMedia(false);
    stubFetchWithFailedRender();
    await openShare();
    await waitFor(() => expect(note()).not.toBeNull());
    expect(note()!.textContent).toBe(NOTE);
    // Warn, do not disable: the export re-fetches, so a failed preview does
    // not imply a failed export. `disabled` here would remove a capability
    // the user still has.
    const enabled = exportButtons().filter((b) => !b.disabled);
    expect(enabled.length).toBeGreaterThan(0);
    for (const b of exportButtons()) {
      // Any disabled button is disabled by its FORMAT gate, not by us.
      if (b.disabled) expect(b.title).toMatch(/available for|only/i);
    }
  });

  it('replaces the privacy line rather than doubling it', async () => {
    stubMatchMedia(false);
    stubFetchWithFailedRender();
    await openShare();
    await waitFor(() => expect(note()).not.toBeNull());
    // With `hasProjectNames === null` the line still renders a definite
    // claim — including "Preview shows real names", which is false when the
    // preview shows an error.
    expect(document.querySelector('.share-privacy-status')).toBeNull();
  });

  it('is referenced by the export buttons through aria-describedby',
    async () => {
      stubMatchMedia(false);
      stubFetchWithFailedRender();
      await openShare();
      await waitFor(() => expect(note()).not.toBeNull());
      const id = note()!.getAttribute('id');
      expect(id).toBeTruthy();
      for (const b of exportButtons()) {
        expect(b.getAttribute('aria-describedby')).toBe(id);
      }
      // Announced ONCE: the preview keeps its own role="alert" banner for
      // the event; this note carries no live region of its own.
      expect(note()!.getAttribute('role')).toBeNull();
      expect(note()!.getAttribute('aria-live')).toBeNull();
    });

  it('clears when a later render succeeds, restoring the privacy line',
    async () => {
      stubMatchMedia(false);
      let fail = true;
      vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
        if (typeof url === 'string' && url.includes('/api/share/presets')) {
          return Promise.resolve(
            new Response(JSON.stringify({ presets: {} }), { status: 200 }));
        }
        if (typeof url === 'string' && url.includes('/api/share/render')) {
          if (fail) {
            return Promise.resolve({
              ok: false, status: 500,
              json: async () => ({ error: 'source render failed' }),
            });
          }
          return Promise.resolve({
            ok: true,
            json: async () => ({
              body: '# Weekly\n', content_type: 'text/markdown',
              has_project_names: true,
              snapshot: {
                kernel_version: 1, panel: 'weekly',
                template_id: 'weekly-recap', options: {},
                generated_at: '2026-05-09T12:00:00Z', data_digest: 'd',
              },
            }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: async () => ({
            panel: 'weekly',
            templates: [{
              id: 'weekly-recap', label: 'Recap', description: 'Text',
              default_options: { format: 'md', theme: 'light' },
            }],
          }),
        });
      }));
      await openShare();
      await waitFor(() => expect(note()).not.toBeNull());

      fail = false;
      const box = document.querySelector<HTMLInputElement>(
        'input[aria-label="Anonymize project names on export"]')!;
      await act(async () => { box.click(); });
      await waitFor(() => expect(note()).toBeNull());
      expect(document.querySelector('.share-privacy-status')).not.toBeNull();
    });

  it('shows no note while the preview is healthy', async () => {
    stubMatchMedia(false);
    stubFetchWithRender(true);
    await openShare();
    await waitFor(() =>
      expect(document.querySelector('.share-privacy-status')).not.toBeNull());
    expect(note()).toBeNull();
  });
});
